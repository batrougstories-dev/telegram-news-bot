#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║          🌐  Global News Digest Bot  v4.0                ║
║  ─────────────────────────────────────────────────────── ║
║  Collector : كل 5 دقائق  — جمع كل الأخبار بدون فلترة   ║
║  Breaking  : كل 5 دقائق  — عاجل ≥ 9 → يُرسل فوراً 🔴  ║
║  Digest    : كل 30 دقيقة — AI يختار أهم 12-22 خبر       ║
╚══════════════════════════════════════════════════════════╝
"""

import os, json, html, re, logging, sqlite3
import asyncio, threading, time
import requests, feedparser
from datetime import datetime, timezone, timedelta
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from telegram.ext import ApplicationBuilder, ChatMemberHandler, ContextTypes
from deep_translator import GoogleTranslator

# ══════════════════════════════════════════════════════════
#  ⚙️  CONFIG
# ══════════════════════════════════════════════════════════
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SERVICE_URL  = os.environ.get("SERVICE_URL", "https://telegram-news-bot-zcn8.onrender.com")
DB_FILE      = "/tmp/news.db"
PORT         = int(os.environ.get("PORT", 10000))

# GitHub AI (GPT-4o-mini) — للتحليل والاختيار
AI_MODEL    = "gpt-4o-mini"
AI_ENDPOINT = "https://models.inference.ai.azure.com/chat/completions"

# Google Gemini — لكتابة التغريدات بالعامية
GEMINI_KEY  = os.environ.get("GEMINI_KEY", "")
GEMINI_URL  = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

COLLECT_EVERY = 5    # دقائق — جمع الأخبار
DIGEST_EVERY  = 30   # دقيقة  — إرسال الموجز
PING_EVERY    = 5    # دقائق — self-ping
NEWS_MAX_AGE  = 75   # دقيقة  — أقصى عمر للخبر
DIGEST_MIN    = 5    # أقل عدد في الموجز
DIGEST_MAX    = 15   # أكثر عدد في الموجز
DEDUP_HOURS   = 6    # ساعات فحص التكرار

# كلمات الكشف المبدئي عن العاجل (قبل سؤال AI)
BREAKING_KW = [
    "breaking","urgent","alert","flash",
    "war","attack","strike","bomb","explosion","blast",
    "killed","dead","casualties","massacre",
    "crisis","emergency","invasion","collapse","coup",
    "nuclear","missile","rocket","earthquake","tsunami",
    "assassination","assassinated","ceasefire","sanctions",
]

# ══════════════════════════════════════════════════════════
#  📡  SOURCES — كل الأخبار العالمية بدون تصنيف
# ══════════════════════════════════════════════════════════
SOURCES = [
    # ── غربي (3 مصادر) ────────────────────────────
    {"name": "Axios",              "url": "https://api.axios.com/feed/"},
    {"name": "Fox News",           "url": "https://moxie.foxnews.com/google-publisher/latest.xml"},
    {"name": "Bloomberg",          "url": "https://feeds.bloomberg.com/markets/news.rss"},
    # ── عربي موثوق ────────────────────────────────
    {"name": "الجزيرة",            "url": "https://www.aljazeera.net/rss"},
    {"name": "الجزيرة إنجليزي",    "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "سكاي نيوز عربية",    "url": "https://www.skynewsarabia.com/rss"},
    # ── خليجي وسعودي (Google News موثوق) ──────────
    {"name": "سعودي وخليجي",       "url": "https://news.google.com/rss/search?q=site:alarabiya.net+OR+site:skynewsarabia.com&hl=ar&gl=SA&ceid=SA:ar"},
    {"name": "الشرق الأوسط",       "url": "https://news.google.com/rss/search?q=السعودية+الإمارات+قطر+site:reuters.com+OR+site:bbc.com&hl=ar&gl=SA&ceid=SA:ar"},
]

flask_app = Flask(__name__)

# ══════════════════════════════════════════════════════════
#  🗄️  DATABASE
# ══════════════════════════════════════════════════════════
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id  INTEGER PRIMARY KEY,
            title    TEXT DEFAULT '',
            active   INTEGER DEFAULT 1,
            added_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS raw_news (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT UNIQUE,
            title_en   TEXT,
            source     TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            is_breaking INTEGER DEFAULT 0,
            brk_sent    INTEGER DEFAULT 0,
            in_digest   INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS digests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at    TEXT    DEFAULT (datetime('now')),
            item_count INTEGER,
            preview    TEXT
        );
        CREATE TABLE IF NOT EXISTS sent_titles (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            title_ar TEXT,
            sent_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    # تنظيف
    conn.execute("DELETE FROM raw_news    WHERE fetched_at < datetime('now','-2 days')")
    conn.execute("DELETE FROM sent_titles WHERE sent_at    < datetime('now','-1 day')")
    conn.commit()
    conn.close()
    logging.info("✅ DB جاهز")

# ── chats ─────────────────────────────────────────────────
def get_chats() -> list[int]:
    conn = get_db()
    rows = conn.execute("SELECT chat_id FROM chats WHERE active=1").fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]

def add_chat(chat_id: int, title: str = ""):
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO chats(chat_id,title) VALUES(?,?)", (chat_id, title))
    conn.execute("UPDATE chats SET active=1, title=? WHERE chat_id=?", (title, chat_id))
    conn.commit(); conn.close()

def remove_chat(chat_id: int):
    conn = get_db()
    conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

# ── raw_news ──────────────────────────────────────────────
def is_url_seen(url: str) -> bool:
    conn = get_db()
    r = conn.execute("SELECT 1 FROM raw_news WHERE url=?", (url,)).fetchone()
    conn.close()
    return r is not None

def save_raw(url: str, title_en: str, source: str, is_breaking: int = 0):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO raw_news(url,title_en,source,is_breaking) VALUES(?,?,?,?)",
            (url, title_en, source, is_breaking)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def mark_brk_sent(url: str):
    conn = get_db()
    conn.execute("UPDATE raw_news SET brk_sent=1, is_breaking=1 WHERE url=?", (url,))
    conn.commit(); conn.close()

def get_pending_digest(minutes: int = 33) -> list[dict]:
    """أخبار آخر X دقيقة لم تُدرج في موجز ولم تُرسل كعاجل"""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, title_en, source FROM raw_news
           WHERE in_digest=0 AND brk_sent=0
             AND fetched_at >= datetime('now', ? || ' minutes')
           ORDER BY id ASC""",
        (f"-{minutes}",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_in_digest(ids: list[int]):
    conn = get_db()
    for _id in ids:
        conn.execute("UPDATE raw_news SET in_digest=1 WHERE id=?", (_id,))
    conn.commit(); conn.close()

# ── sent_titles (dedup) ───────────────────────────────────
def get_recent_titles(hours: int = 6) -> list[str]:
    conn = get_db()
    rows = conn.execute(
        "SELECT title_ar FROM sent_titles WHERE sent_at >= datetime('now', ? || ' hours')",
        (f"-{hours}",)
    ).fetchall()
    conn.close()
    return [r["title_ar"] for r in rows]

def save_sent_titles(titles: list[str]):
    conn = get_db()
    for t in titles:
        if t:
            conn.execute("INSERT INTO sent_titles(title_ar) VALUES(?)", (t,))
    conn.commit(); conn.close()

# ── digests ───────────────────────────────────────────────
def save_digest(item_count: int, preview: str):
    conn = get_db()
    conn.execute("INSERT INTO digests(item_count,preview) VALUES(?,?)", (item_count, preview))
    conn.commit(); conn.close()

# ══════════════════════════════════════════════════════════
#  🔤  TRANSLATION — 3-Layer Guarantee
# ══════════════════════════════════════════════════════════
_translator     = GoogleTranslator(source="en", target="ar")
_translate_lock = threading.Lock()

def is_arabic(text: str) -> bool:
    if not text: return False
    ar = sum(1 for c in text if "\u0600" <= c <= "\u06FF")
    return ar > max(len(text) * 0.25, 3)

def force_translate(text: str) -> str:
    """Google Translate كـ fallback موثوق"""
    if not text: return text
    with _translate_lock:
        try:
            r = _translator.translate(text[:500])
            return r if r and is_arabic(r) else text
        except Exception as e:
            logging.warning(f"translate: {e}")
            return text

def ensure_arabic(ar: str, en: str) -> str:
    """يضمن العربية دائماً"""
    return ar if is_arabic(ar) else force_translate(en)

# ══════════════════════════════════════════════════════════
#  🤖  AI — GitHub Models (GPT-4o-mini)
# ══════════════════════════════════════════════════════════
_ai_lock = threading.Lock()

def call_ai(system: str, user: str, max_tokens: int = 500) -> dict | None:
    with _ai_lock:
        try:
            r = requests.post(
                AI_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      AI_MODEL,
                    "messages":   [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    "max_tokens":      max_tokens,
                    "temperature":     0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=25,
            )
            data = r.json()
            if "choices" in data:
                return json.loads(data["choices"][0]["message"]["content"])
        except Exception as e:
            logging.warning(f"AI error: {e}")
        return None

# ── 0) Gemini — تغريدات بالعامية ────────────────────────
def call_gemini(prompt: str) -> str | None:
    """يستدعي Gemini ويُرجع نصاً"""
    if not GEMINI_KEY:
        return None
    try:
        r = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": 2000,
                    "temperature":     0.7,
                },
            },
            timeout=30,
        )
        data = r.json()
        if "candidates" in data:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        logging.warning(f"Gemini error: {data.get('error',{}).get('message','')}")
    except Exception as e:
        logging.warning(f"Gemini exception: {e}")
    return None

def gemini_style_digest(items: list[dict]) -> list[dict]:
    """
    يحوّل الأخبار المختارة إلى تغريدات بالعامية السعودية/الخليجية
    أسلوب خفيف، مباشر، وجذاب — مثل تغريدة تويتر
    """
    numbered = "\n".join(
        f"{i+1}. {it['title_ar']} — {it['source']}"
        for i, it in enumerate(items)
    )

    prompt = f"""أنت محرر أخبار سعودي ظريف على تويتر.

عندك هالأخبار:
{numbered}

المطلوب:
- أعد كتابة كل خبر كتغريدة واحدة قصيرة
- باللهجة العامية السعودية أو الخليجية
- أسلوب خفيف، مباشر، وجذاب
- أضف إيموجي مناسب لكل تغريدة في البداية
- الخبر العاجل أضف 🔴 في البداية
- لا تزيد كل تغريدة عن سطرين
- حافظ على اسم المصدر في النهاية

أجب بهذا الشكل فقط (رقم ثم التغريدة):
1. 🔴 نص التغريدة — المصدر
2. 💡 نص التغريدة — المصدر
..."""

    result = call_gemini(prompt)
    if not result:
        return items  # fallback: الأخبار كما هي

    # تحليل النتيجة
    styled = []
    lines  = [l.strip() for l in result.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if i >= len(items): break
        # أزل الرقم من البداية إن وجد
        line = re.sub(r"^\d+\.\s*", "", line)
        if line:
            styled.append({
                "title_ar":   line,
                "source":     items[i]["source"],
                "is_breaking": items[i].get("is_breaking", False),
            })

    # إذا Gemini لم يُرجع بعض الأخبار، أكمل من الأصلية
    if len(styled) < len(items):
        styled += items[len(styled):]

    return styled

# ── 1) فحص سريع للعاجل ───────────────────────────────────
def ai_check_breaking(title: str, source: str) -> dict:
    res = call_ai(
        "أنت محرر أخبار دولي. أجب بـ JSON فقط.",
        f"""هل هذا الخبر عاجل ومهم عالمياً؟

العنوان: {title}
المصدر:  {source}

{{"is_breaking": true, "importance": 9, "title_ar": "الترجمة العربية الاحترافية"}}

ملاحظة: importance من 1 إلى 10. العاجل الحقيقي ≥ 9."""
    )
    if res:
        res["title_ar"] = ensure_arabic(res.get("title_ar", ""), title)
        return res
    return {
        "is_breaking": False,
        "importance":  0,
        "title_ar":    force_translate(title),
    }

# ── 2) بناء الموجز ───────────────────────────────────────
def ai_build_digest(items: list[dict], recent_ar: list[str]) -> list[dict]:
    """
    يأخذ أخبار خام → يختار أهم 12-22 عالمياً → يترجمها
    """
    headlines = "\n".join(
        f"{i+1}. [{it['source']}] {it['title_en']}"
        for i, it in enumerate(items)
    )
    recent_s = "\n".join(f"- {t}" for t in recent_ar[:30]) or "لا يوجد"

    system = "أنت محرر أخبار عالمي كبير تصدر موجزاً إخبارياً مختصراً. أجب بـ JSON فقط."
    user   = f"""لديك {len(items)} خبر. اختر منها أهم {DIGEST_MIN}-{DIGEST_MAX} خبراً فقط:

{headlines}

أخبار سبق إرسالها — لا تكررها:
{recent_s}

قواعد الاختيار (صارمة):
✅ اقبل: أحداث سياسية كبرى، حروب، اتفاقيات، كوارث، قرارات اقتصادية كبرى، تقنية مؤثرة
❌ ارفض تماماً: نتائج رياضية، أخبار محلية أمريكية تافهة، بيانات مالية دقيقة (سندات/عملات/مؤشرات صغيرة)، إعلانات دعاية، أخبار إثارة بدون مضمون

الترجمة للعربية:
• جملة واحدة قصيرة ومفهومة
• لا تترجم حرفياً — اكتب المعنى بوضوح
• بالعربية الفصحى المبسطة

أجب بهذا الشكل:
{{
  "items": [
    {{"rank": 1, "title_ar": "عنوان مختصر وواضح", "source": "المصدر", "is_breaking": false}},
    {{"rank": 2, "title_ar": "عنوان مختصر وواضح", "source": "المصدر", "is_breaking": true}}
  ]
}}"""

    res = call_ai(system, user, max_tokens=2500)

    if res and "items" in res:
        out = []
        for it in res["items"][:DIGEST_MAX]:
            ar = ensure_arabic(it.get("title_ar", ""), "")
            if ar:
                out.append({
                    "title_ar":   ar,
                    "source":     it.get("source", ""),
                    "is_breaking": bool(it.get("is_breaking", False)),
                })
        return out

    # Fallback: AI فشل → ترجم أفضل ما عندنا
    logging.warning("⚠️ AI digest فشل — fallback Google Translate")
    return [
        {
            "title_ar":   force_translate(p["title_en"]),
            "source":     p["source"],
            "is_breaking": False,
        }
        for p in items[:DIGEST_MAX]
    ]

# ══════════════════════════════════════════════════════════
#  📡  RSS FETCHER
# ══════════════════════════════════════════════════════════
def clean_title(t: str) -> str:
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", t).strip()

def get_age_min(entry) -> int | None:
    pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not pub: return None
    try:
        dt = datetime(*pub[:6], tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    except:
        return None

def fetch_all() -> list[dict]:
    """يجلب كل الأخبار من كل المصادر — بدون أي فلترة"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/4.0)"}
    items   = []
    for src in SOURCES:
        try:
            r    = requests.get(src["url"], headers=headers, timeout=12)
            feed = feedparser.parse(r.content)
            for e in feed.entries[:15]:
                url   = getattr(e, "link", "").strip()
                title = clean_title(getattr(e, "title", ""))
                if not url or not title:
                    continue
                age = get_age_min(e)
                # تخطي الأخبار القديمة جداً
                if age is not None and age > NEWS_MAX_AGE:
                    continue
                items.append({
                    "url":    url,
                    "title":  title,
                    "source": src["name"],
                    "age":    age,
                })
        except Exception as ex:
            logging.warning(f"RSS [{src['name']}]: {ex}")
    return items

# ══════════════════════════════════════════════════════════
#  📤  TELEGRAM
# ══════════════════════════════════════════════════════════
async def _send_async(chat_id: int, text: str):
    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

def send_to_all(text: str):
    for chat_id in get_chats():
        try:
            asyncio.run(_send_async(chat_id, text))
            time.sleep(0.3)
        except Exception as e:
            logging.warning(f"send [{chat_id}]: {e}")

# ── تنسيق الرسائل ─────────────────────────────────────────
def fmt_breaking(title_ar: str, source: str) -> str:
    return (
        f"🔴 <b>عاجل</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{title_ar}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"— {source}"
    )

# أسماء الأيام والأشهر بالعربية
DAYS_AR   = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
MONTHS_AR = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
             "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]

def fmt_digest(items: list[dict]) -> str:
    now_mecca = datetime.now(timezone.utc) + timedelta(hours=3)
    day_ar    = DAYS_AR[now_mecca.weekday()]
    month_ar  = MONTHS_AR[now_mecca.month - 1]
    date_str  = f"{day_ar} {now_mecca.day} {month_ar} {now_mecca.year}"
    # تحويل الساعة لـ 12h عربي
    h  = now_mecca.hour
    m  = now_mecca.strftime("%M")
    ap = "صباحاً" if h < 12 else "مساءً"
    h12 = h % 12 or 12
    time_str = f"{h12}:{m} {ap}"

    lines = [
        f"🗞 <b>هاك الأخبار</b>",
        f"🕌 {date_str}",
        f"🕐 {time_str} — بتوقيت مكة المكرمة",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for it in items:
        bullet = "🔴" if it.get("is_breaking") else "▪️"
        lines.append(f"{bullet} {it['title_ar']}  —  <i>{it['source']}</i>")

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════
#  🔄  CYCLES
# ══════════════════════════════════════════════════════════
def collect_cycle():
    """كل 5 دقائق — جمع الأخبار + كشف العاجل"""
    logging.info("📡 [Collect] بدء")
    all_items = fetch_all()
    new, brk  = 0, 0

    for item in all_items:
        if is_url_seen(item["url"]):
            continue

        tl             = item["title"].lower()
        looks_breaking = any(kw in tl for kw in BREAKING_KW)

        if looks_breaking:
            # سؤال AI سريع للتأكيد
            res        = ai_check_breaking(item["title"], item["source"])
            importance = res.get("importance", 0)
            confirmed  = res.get("is_breaking", False) and importance >= 9

            if confirmed:
                title_ar = res["title_ar"]
                save_raw(item["url"], item["title"], item["source"], is_breaking=1)
                mark_brk_sent(item["url"])
                save_sent_titles([title_ar])
                send_to_all(fmt_breaking(title_ar, item["source"]))
                logging.info(f"  🔴 عاجل [{importance}/10]: {title_ar[:60]}")
                brk += 1
                continue

        save_raw(item["url"], item["title"], item["source"])
        new += 1

    logging.info(f"  ✅ جديد: {new} | عاجل مُرسل: {brk} | إجمالي الجلسة: {len(all_items)}")


def digest_cycle():
    """كل 30 دقيقة — AI يختار أهم 12-22 ويرسل موجزاً"""
    logging.info("📰 [Digest] بدء")

    pending = get_pending_digest(minutes=33)
    logging.info(f"  📋 معلقة: {len(pending)} خبر")

    if len(pending) < 3:
        logging.info("  ⏭ عدد غير كافٍ — تخطي")
        return

    recent_ar = get_recent_titles(hours=DEDUP_HOURS)
    items     = ai_build_digest(pending, recent_ar)

    if not items:
        logging.warning("  ⚠️ الموجز فارغ")
        return

    # تأكد من الحد الأدنى
    if len(items) < DIGEST_MIN and len(pending) >= DIGEST_MIN:
        extra = [
            {
                "title_ar":   force_translate(p["title_en"]),
                "source":     p["source"],
                "is_breaking": False,
            }
            for p in pending[len(items):DIGEST_MIN]
        ]
        items += extra

    # Gemini: يحوّل الأخبار لتغريدات بالعامية (إذا كان المفتاح متوفراً)
    if GEMINI_KEY:
        logging.info("  ✨ Gemini يكتب التغريدات...")
        items = gemini_style_digest(items)

    msg = fmt_digest(items)
    send_to_all(msg)

    # حفظ في DB
    ids     = [p["id"] for p in pending]
    preview = items[0]["title_ar"][:80] if items else ""
    mark_in_digest(ids)
    save_digest(len(items), preview)
    save_sent_titles([it["title_ar"] for it in items])

    logging.info(f"  ✅ موجز أُرسل: {len(items)} خبر")


def self_ping():
    try:
        requests.get(SERVICE_URL + "/health", timeout=10)
        logging.info("🔄 ping OK")
    except:
        pass

# ══════════════════════════════════════════════════════════
#  🌐  FLASK ENDPOINTS
# ══════════════════════════════════════════════════════════
@flask_app.route("/")
def home():
    chats = get_chats()
    conn  = get_db()
    total = conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
    digs  = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    brk   = conn.execute("SELECT COUNT(*) FROM raw_news WHERE brk_sent=1").fetchone()[0]
    conn.close()
    return (
        f"✅ News Digest Bot v4 | قنوات: {len(chats)} | "
        f"أخبار مجموعة: {total} | موجزات: {digs} | عاجلة: {brk}"
    ), 200

@flask_app.route("/health")
def health():
    return "OK", 200

@flask_app.route("/trigger")
def trigger():
    """تشغيل فوري للدورتين — للاختبار"""
    import threading
    def run():
        collect_cycle()
        import time; time.sleep(5)
        digest_cycle()
    threading.Thread(target=run, daemon=True).start()
    return "🚀 تم تشغيل Collect + Digest — انتظر 30 ثانية", 200

@flask_app.route("/stats")
def stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
    brk   = conn.execute("SELECT COUNT(*) FROM raw_news WHERE brk_sent=1").fetchone()[0]
    digs  = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    last3 = conn.execute(
        "SELECT item_count, sent_at, preview FROM digests ORDER BY id DESC LIMIT 3"
    ).fetchall()
    top_src = conn.execute(
        "SELECT source, COUNT(*) c FROM raw_news GROUP BY source ORDER BY c DESC LIMIT 8"
    ).fetchall()
    conn.close()
    return json.dumps({
        "raw_collected": total,
        "breaking_sent": brk,
        "digests_sent":  digs,
        "last_digests": [
            {"items": r["item_count"], "at": r["sent_at"], "preview": r["preview"]}
            for r in last3
        ],
        "top_sources": {r["source"]: r["c"] for r in top_src},
    }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}

# ══════════════════════════════════════════════════════════
#  🤖  TELEGRAM BOT — كشف القنوات
# ══════════════════════════════════════════════════════════
async def on_status_change(update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result: return
    chat  = result.chat
    status = result.new_chat_member.status
    if status in ("member", "administrator"):
        add_chat(chat.id, chat.title or "")
        logging.info(f"➕ قناة جديدة: {chat.title} ({chat.id})")
    elif status in ("left", "kicked"):
        remove_chat(chat.id)
        logging.info(f"➖ غادر: {chat.title} ({chat.id})")

def start_polling():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(
        ChatMemberHandler(on_status_change, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    app.run_polling(allowed_updates=["my_chat_member"])

# ══════════════════════════════════════════════════════════
#  🚀  MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    init_db()
    add_chat(-1003622255773, "تجربة")   # القناة الافتراضية

    chats = get_chats()
    logging.info("═" * 55)
    logging.info("  🌐  Global News Digest Bot  v4.0")
    logging.info("═" * 55)
    logging.info(f"  🤖 النموذج      : {AI_MODEL}")
    logging.info(f"  📋 القنوات      : {len(chats)}")
    logging.info(f"  📡 Collect      : كل {COLLECT_EVERY} دقائق")
    logging.info(f"  📰 Digest       : كل {DIGEST_EVERY} دقيقة")
    logging.info(f"  📊 حجم الموجز   : {DIGEST_MIN}–{DIGEST_MAX} خبر")
    logging.info(f"  ⏰ عمر الخبر    : آخر {NEWS_MAX_AGE} دقيقة")
    logging.info("═" * 55)

    # أول تشغيل فوري
    threading.Thread(target=collect_cycle, daemon=True).start()

    # Scheduler
    now = datetime.now(timezone.utc)
    first_digest = now + timedelta(minutes=DIGEST_EVERY)

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(collect_cycle, "interval", minutes=COLLECT_EVERY, id="collect")
    scheduler.add_job(digest_cycle,  "interval", minutes=DIGEST_EVERY,  id="digest",
                      next_run_time=first_digest)
    scheduler.add_job(self_ping,     "interval", minutes=PING_EVERY,    id="ping")
    scheduler.start()

    # Telegram في الخلفية
    threading.Thread(target=start_polling, daemon=True).start()

    # Flask في الـ main thread (Render يحتاجه)
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
