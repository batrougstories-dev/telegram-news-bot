#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║       بوت أخبار عاجلة — مدعوم بـ GitHub AI (GPT-4o mini)       ║
║  Reuters | AP | BBC | Al Jazeera | Guardian | Axios | Bloomberg  ║
╠══════════════════════════════════════════════════════════════════╣
║  ✅ أخبار الساعة الأخيرة فقط (طازجة)                            ║
║  ✅ كشف التكرار بالذكاء الاصطناعي                               ║
║  ✅ ترجمة احترافية                                              ║
║  ✅ مقالَي تحليل سياسي يومياً مع ذكر الكاتب                    ║
║  ✅ تصنيف الأخبار + تقييم الأهمية                              ║
║  ✅ الشرق الأوسط أولاً                                         ║
║  ✅ Self-ping لمنع نوم Render                                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio, feedparser, sqlite3, logging, re, html
import os, threading, time, json
from datetime import datetime, timezone, timedelta
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, ContextTypes

# ══════════════════════════════════════════════
#                ⚙️ الإعدادات
# ══════════════════════════════════════════════
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AI_MODEL     = "gpt-4o-mini"
AI_ENDPOINT  = "https://models.inference.ai.azure.com/chat/completions"
SERVICE_URL  = "https://telegram-news-bot-zcn8.onrender.com"
DB_FILE      = "news_bot.db"
PORT         = int(os.environ.get("PORT", 10000))

NEWS_MAX_AGE_MINUTES   = 60    # ← أخبار آخر ساعة فقط
ANALYSIS_PER_DAY       = 2     # ← مقالَي تحليل فقط يومياً
MAX_PER_BREAKING       = 3
MAX_PER_REGULAR        = 1
BREAKING_EVERY         = 3     # دقائق
REGULAR_EVERY          = 15    # دقائق
ANALYSIS_EVERY         = 120   # دقائق (مرتين في اليوم تقريباً)
PING_EVERY             = 5     # دقائق

# ══════════════════════════════════════════════
#    📍 كلمات الشرق الأوسط
# ══════════════════════════════════════════════
ME_KEYWORDS = [
    "saudi","uae","iran","iraq","syria","yemen","lebanon","palestine",
    "israel","jordan","egypt","kuwait","qatar","bahrain","oman","turkey",
    "libya","sudan","morocco","algeria","tunisia",
    "riyadh","dubai","abu dhabi","cairo","beirut","damascus","baghdad",
    "tehran","amman","doha","muscat","sanaa","jerusalem","tel aviv",
    "gaza","ramallah","mosul","aleppo","idlib","tripoli",
    "arab","middle east","persian gulf","red sea","suez",
    "hamas","hezbollah","houthi","idf","irgc","isis","isil",
    "plo","fatah","west bank","golan","sinai","opec","aramco",
    "netanyahu","erdogan","mbs","sisi","khamenei","nasrallah",
    "ceasefire","airstrike","drone strike","nuclear deal","oil price",
]
BREAKING_KEYWORDS = [
    "breaking","urgent","alert","just in","flash","developing",
    "emergency","explosion","attack","killed","war","ceasefire",
    "strike","bomb","missile","assassination","coup","crisis",
    "casualties","dead","fire","arrested","collapsed","disaster",
]
ANALYSIS_KEYWORDS = [
    "analysis","opinion","commentary","perspective","column","essay",
    "viewpoint","explainer","why","how","what","deep dive","in depth",
    "background","context","explained","the big picture",
]

# ══════════════════════════════════════════════
#   📰 مصادر الأخبار العاجلة RSS
# ══════════════════════════════════════════════
NEWS_SOURCES = [
    {"name":"Al Jazeera",             "priority":"high",
     "rss":"https://www.aljazeera.com/xml/rss/all.xml"},
    {"name":"BBC Middle East",        "priority":"high",
     "rss":"http://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
    {"name":"Reuters — Middle East",  "priority":"high",
     "rss":"https://news.google.com/rss/search?q=reuters+middle+east+breaking&hl=en-US&gl=US&ceid=US:en"},
    {"name":"AP — Middle East",       "priority":"high",
     "rss":"https://news.google.com/rss/search?q=associated+press+middle+east&hl=en-US&gl=US&ceid=US:en"},
    {"name":"Bloomberg",              "priority":"high",
     "rss":"https://feeds.bloomberg.com/markets/news.rss"},
    {"name":"Bloomberg Politics",     "priority":"high",
     "rss":"https://feeds.bloomberg.com/politics/news.rss"},
    {"name":"Bloomberg — Middle East","priority":"high",
     "rss":"https://news.google.com/rss/search?q=bloomberg+middle+east&hl=en-US&gl=US&ceid=US:en"},
    {"name":"Axios",                  "priority":"high",
     "rss":"https://api.axios.com/feed/"},
    {"name":"BBC News",               "priority":"normal",
     "rss":"http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name":"The Guardian",           "priority":"normal",
     "rss":"https://www.theguardian.com/world/middleeast/rss"},
    {"name":"Reuters — World",        "priority":"normal",
     "rss":"https://news.google.com/rss/search?q=reuters+breaking+news&hl=en-US&gl=US&ceid=US:en"},
]

# ══════════════════════════════════════════════
#   📝 مصادر التحليل السياسي
# ══════════════════════════════════════════════
ANALYSIS_SOURCES = [
    {"name":"Foreign Affairs",
     "rss":"https://www.foreignaffairs.com/rss.xml"},
    {"name":"The Guardian — تحليل",
     "rss":"https://www.theguardian.com/commentisfree/rss"},
    {"name":"Atlantic Council",
     "rss":"https://www.atlanticcouncil.org/feed/"},
    {"name":"Al Monitor",
     "rss":"https://www.al-monitor.com/rss"},
    {"name":"Reuters — تحليل",
     "rss":"https://news.google.com/rss/search?q=reuters+analysis+middle+east&hl=en-US&gl=US&ceid=US:en"},
    {"name":"Bloomberg — رأي",
     "rss":"https://news.google.com/rss/search?q=bloomberg+opinion+middle+east&hl=en-US&gl=US&ceid=US:en"},
]

# ══════════════════════════════════════════════
#        🗂️ قاعدة البيانات
# ══════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_news (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT UNIQUE NOT NULL,
            title_en    TEXT,
            title_ar    TEXT,
            category    TEXT,
            importance  INTEGER DEFAULT 5,
            is_breaking INTEGER DEFAULT 0,
            is_analysis INTEGER DEFAULT 0,
            source      TEXT,
            sent_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id    INTEGER PRIMARY KEY,
            chat_title TEXT,
            chat_type  TEXT,
            active     INTEGER DEFAULT 1,
            added_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("DELETE FROM sent_news WHERE sent_at < datetime('now','-3 days')")
    conn.commit(); conn.close()
    logging.info("✅ DB جاهز")

def add_chat(chat_id, title, ctype):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO chats(chat_id,chat_title,chat_type,active) VALUES(?,?,?,1)
        ON CONFLICT(chat_id) DO UPDATE SET chat_title=excluded.chat_title,active=1
    """, (chat_id, title, ctype))
    conn.commit(); conn.close()
    logging.info(f"➕ {title}")

def remove_chat(chat_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

def get_chats():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("SELECT chat_id,chat_title FROM chats WHERE active=1").fetchall()
    conn.close(); return rows

def is_url_sent(url):
    conn = sqlite3.connect(DB_FILE)
    r    = conn.execute("SELECT 1 FROM sent_news WHERE url=?", (url,)).fetchone()
    conn.close(); return r is not None

def get_recent_titles(hours=48):
    conn  = sqlite3.connect(DB_FILE)
    rows  = conn.execute(
        "SELECT title_en FROM sent_news WHERE sent_at > datetime('now',? || ' hours')",
        (f"-{hours}",)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]

def count_analysis_today():
    """عدد مقالات التحليل المرسلة اليوم"""
    conn = sqlite3.connect(DB_FILE)
    n    = conn.execute(
        "SELECT COUNT(*) FROM sent_news WHERE is_analysis=1 AND sent_at > datetime('now','start of day')"
    ).fetchone()[0]
    conn.close(); return n

def save_news(url, title_en, title_ar, category, importance,
              is_breaking, is_analysis, source):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            "INSERT INTO sent_news(url,title_en,title_ar,category,importance,"
            "is_breaking,is_analysis,source) VALUES(?,?,?,?,?,?,?,?)",
            (url, title_en, title_ar, category, importance,
             is_breaking, is_analysis, source)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

# ══════════════════════════════════════════════
#   ⏰ فلتر التاريخ — الساعة الأخيرة فقط
# ══════════════════════════════════════════════
def get_entry_age_minutes(entry) -> int | None:
    """
    يُرجع عمر الخبر بالدقائق.
    None = لا يوجد تاريخ.
    """
    pub = (getattr(entry, "published_parsed", None)
           or getattr(entry, "updated_parsed", None))
    if not pub:
        return None
    try:
        pub_dt  = datetime(*pub[:6], tzinfo=timezone.utc)
        now     = datetime.now(timezone.utc)
        age_min = int((now - pub_dt).total_seconds() / 60)
        return age_min
    except Exception:
        return None

def is_fresh_news(entry, max_age: int = NEWS_MAX_AGE_MINUTES) -> bool:
    """True إذا كان الخبر خلال آخر max_age دقيقة"""
    age = get_entry_age_minutes(entry)
    if age is None:
        return False   # لا تاريخ = نتجاهله
    return age <= max_age

def is_fresh_analysis(entry, max_age: int = 1440) -> bool:
    """للتحليل: نقبل مقالات آخر 24 ساعة"""
    age = get_entry_age_minutes(entry)
    if age is None:
        return False
    return age <= max_age

# ══════════════════════════════════════════════
#        🤖 GitHub AI
# ══════════════════════════════════════════════
_ai_lock = threading.Lock()

def call_ai(system: str, user: str, max_tokens=400) -> dict | None:
    with _ai_lock:
        try:
            r = requests.post(
                AI_ENDPOINT,
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                         "Content-Type":  "application/json"},
                json={"model": AI_MODEL,
                      "messages": [{"role":"system","content":system},
                                   {"role":"user",  "content":user}],
                      "max_tokens":  max_tokens,
                      "temperature": 0.15,
                      "response_format": {"type":"json_object"}},
                timeout=20,
            )
            data = r.json()
            if "choices" in data:
                return json.loads(data["choices"][0]["message"]["content"])
        except Exception as e:
            logging.warning(f"⚠️ AI: {e}")
        return None

def ai_analyze_news(title_en: str, source: str, recent: list) -> dict:
    """يحلل خبراً عاجلاً"""
    recent_s = "\n".join(f"- {t}" for t in recent[-25:]) or "لا يوجد"
    system = "أنت محرر أخبار عربي محترف. أجب بـ JSON فقط."
    user = f"""حلل الخبر:
العنوان: {title_en}
المصدر: {source}

الأخبار المرسلة مسبقاً (آخر 48 ساعة):
{recent_s}

{{
  "is_duplicate": false,
  "is_middle_east": true,
  "is_breaking": false,
  "category": "military",
  "importance": 8,
  "title_ar": "الترجمة العربية الاحترافية"
}}

القواعد:
- category: military/politics/economy/diplomacy/security/humanitarian/other
- importance: 1-10
- is_duplicate: true إذا كان نفس حدث خبر سابق"""
    res = call_ai(system, user)
    if res:
        return res
    # Fallback
    tl  = title_en.lower()
    me  = any(kw in tl for kw in ME_KEYWORDS)
    brk = any(kw in tl for kw in BREAKING_KEYWORDS)
    return {"is_duplicate":False,"is_middle_east":me,"is_breaking":brk,
            "category":"other","importance":6,"title_ar":title_en}

def ai_analyze_article(title_en: str, summary_en: str,
                       author: str, source: str) -> dict:
    """يحلل مقال تحليلي ويلخصه"""
    system = "أنت محرر أخبار عربي متخصص في التحليل السياسي. أجب بـ JSON فقط."
    user = f"""حلل هذا المقال التحليلي:
العنوان: {title_en}
الكاتب: {author or 'غير محدد'}
المصدر: {source}
المحتوى المتاح: {summary_en[:600] if summary_en else 'غير متوفر'}

{{
  "is_middle_east": true,
  "title_ar": "العنوان بالعربية",
  "author_ar": "اسم الكاتب أو 'غير محدد'",
  "summary_ar": "ملخص عربي مختصر بـ 2-3 جمل فقط عن محور المقال وأبرز حججه",
  "importance": 7
}}"""
    res = call_ai(system, user, max_tokens=350)
    if res:
        return res
    return {"is_middle_east":True,"title_ar":title_en,
            "author_ar":author or "غير محدد",
            "summary_ar":"تحليل سياسي","importance":6}

# ══════════════════════════════════════════════
#        🌐 جلب الأخبار
# ══════════════════════════════════════════════
def clean(t: str) -> str:
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r"<[^>]+>","",t)
    return re.sub(r"\s+"," ",t).strip()

def fetch_news() -> list:
    """يجلب الأخبار الطازجة (آخر ساعة) فقط"""
    items   = []
    headers = {"User-Agent":"Mozilla/5.0"}
    for src in NEWS_SOURCES:
        try:
            r    = requests.get(src["rss"], headers=headers, timeout=12)
            feed = feedparser.parse(r.content)
            for e in feed.entries[:15]:
                url   = getattr(e,"link","")
                title = clean(getattr(e,"title",""))
                if not url or not title or is_url_sent(url):
                    continue
                if not is_fresh_news(e):      # ← فلتر الساعة الأخيرة
                    continue
                age = get_entry_age_minutes(e)
                items.append({
                    "url":      url,
                    "title_en": title,
                    "source":   src["name"],
                    "priority": src["priority"],
                    "age_min":  age,
                })
        except Exception as ex:
            logging.warning(f"⚠️ RSS {src['name']}: {ex}")
    return items

def fetch_analysis() -> list:
    """يجلب مقالات التحليل (آخر 24 ساعة)"""
    items   = []
    headers = {"User-Agent":"Mozilla/5.0"}
    for src in ANALYSIS_SOURCES:
        try:
            r    = requests.get(src["rss"], headers=headers, timeout=12)
            feed = feedparser.parse(r.content)
            for e in feed.entries[:10]:
                url     = getattr(e,"link","")
                title   = clean(getattr(e,"title",""))
                summary = clean(getattr(e,"summary","") or getattr(e,"description",""))
                author  = getattr(e,"author","") or getattr(e,"dc_creator","")
                if not url or not title or is_url_sent(url):
                    continue
                if not is_fresh_analysis(e):   # آخر 24 ساعة
                    continue
                items.append({
                    "url":        url,
                    "title_en":   title,
                    "summary_en": summary,
                    "author":     clean(author),
                    "source":     src["name"],
                })
        except Exception as ex:
            logging.warning(f"⚠️ Analysis {src['name']}: {ex}")
    return items

# ══════════════════════════════════════════════
#    🧹 فلتر أولي سريع (يوفر استهلاك AI)
# ══════════════════════════════════════════════
def quick_filter(items: list) -> list:
    scored = []
    for item in items:
        tl = item["title_en"].lower()
        me  = sum(1 for kw in ME_KEYWORDS       if kw in tl)
        brk = sum(1 for kw in BREAKING_KEYWORDS if kw in tl)
        if item.get("priority") == "high": me += 2
        if me > 0 or brk > 0:
            item["_score"] = me + brk * 2
            scored.append(item)
    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored

# ══════════════════════════════════════════════
#        📤 إرسال الرسائل
# ══════════════════════════════════════════════
CATEGORY_EMOJI = {
    "military":    "⚔️",
    "politics":    "🏛️",
    "economy":     "📈",
    "diplomacy":   "🤝",
    "security":    "🔒",
    "humanitarian":"🆘",
    "other":       "🌐",
}

def format_news(title_ar, source, category, is_breaking) -> str:
    emoji = CATEGORY_EMOJI.get(category,"🌐")
    if is_breaking:
        return f"🔴 <b>عاجل</b> {emoji}\n\n<b>{title_ar}</b>\n\n— {source}"
    return f"{emoji} <b>{title_ar}</b>\n\n— {source}"

def format_analysis(title_ar, author_ar, summary_ar, source) -> str:
    return (
        f"📌 <b>تحليل سياسي</b>\n\n"
        f"<b>{title_ar}</b>\n\n"
        f"{summary_ar}\n\n"
        f"✍️ {author_ar} | {source}"
    )

async def send_to_all(message: str):
    chats = get_chats()
    if not chats:
        logging.warning("⚠️ لا توجد قنوات")
        return
    bot = Bot(token=BOT_TOKEN)
    for chat_id, chat_title in chats:
        try:
            await bot.send_message(
                chat_id=chat_id, text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logging.info(f"   ✅ → {chat_title}")
        except TelegramError as te:
            logging.error(f"   ❌ → {chat_title}: {te}")
            if any(x in str(te).lower() for x in ["kicked","chat not found","blocked"]):
                remove_chat(chat_id)
        await asyncio.sleep(0.4)

# ══════════════════════════════════════════════
#   🚨 دورة الأخبار العاجلة (كل 3 دقائق)
# ══════════════════════════════════════════════
def breaking_cycle():
    try:
        logging.info("🔍 [عاجل] فحص الأخبار الطازجة...")
        raw   = fetch_news()
        items = quick_filter(raw)
        if not items:
            logging.info("   ℹ️ لا أخبار طازجة")
            return

        recent = get_recent_titles(48)
        sent   = 0

        for item in items:
            if sent >= MAX_PER_BREAKING: break
            a = ai_analyze_news(item["title_en"], item["source"], recent)

            if a.get("is_duplicate"):
                logging.info(f"   ♻️ مكرر: {item['title_en'][:50]}")
                save_news(item["url"],item["title_en"],"","",0,0,0,item["source"])
                continue
            if not a.get("is_middle_east"):
                continue
            if not a.get("is_breaking") and a.get("importance",0) < 8:
                continue

            title_ar   = a.get("title_ar", item["title_en"])
            category   = a.get("category","other")
            importance = a.get("importance",5)
            is_breaking= a.get("is_breaking",False)
            age        = item.get("age_min",0)

            msg = format_news(title_ar, item["source"], category, is_breaking)
            asyncio.run(send_to_all(msg))
            save_news(item["url"],item["title_en"],title_ar,
                      category,importance,int(is_breaking),0,item["source"])
            recent.append(item["title_en"])
            sent += 1
            time.sleep(1.5)

        if sent == 0:
            logging.info("   ℹ️ لا أخبار عاجلة جديدة مؤهلة")

    except Exception as e:
        logging.error(f"❌ breaking_cycle: {e}")

# ══════════════════════════════════════════════
#   📰 دورة الأخبار العادية (كل 15 دقيقة)
# ══════════════════════════════════════════════
def regular_cycle():
    try:
        logging.info("📰 [عادي] فحص...")
        raw   = fetch_news()
        items = quick_filter(raw)
        if not items:
            logging.info("   ℹ️ لا أخبار طازجة")
            return

        recent = get_recent_titles(48)
        for item in items:
            a = ai_analyze_news(item["title_en"], item["source"], recent)
            if a.get("is_duplicate"):
                save_news(item["url"],item["title_en"],"","",0,0,0,item["source"])
                continue
            if not a.get("is_middle_east"):
                continue

            title_ar   = a.get("title_ar", item["title_en"])
            category   = a.get("category","other")
            importance = a.get("importance",5)
            is_breaking= a.get("is_breaking",False)

            msg = format_news(title_ar, item["source"], category, is_breaking)
            asyncio.run(send_to_all(msg))
            save_news(item["url"],item["title_en"],title_ar,
                      category,importance,int(is_breaking),0,item["source"])
            logging.info(f"   ✅ {title_ar[:60]}")
            break

    except Exception as e:
        logging.error(f"❌ regular_cycle: {e}")

# ══════════════════════════════════════════════
#   📝 دورة التحليل السياسي (مرتين يومياً)
# ══════════════════════════════════════════════
def analysis_cycle():
    try:
        today_count = count_analysis_today()
        if today_count >= ANALYSIS_PER_DAY:
            logging.info(f"📌 [تحليل] اكتمل الحد اليومي ({today_count}/{ANALYSIS_PER_DAY})")
            return

        logging.info(f"📌 [تحليل] فحص المقالات... ({today_count}/{ANALYSIS_PER_DAY} اليوم)")
        articles = fetch_analysis()
        if not articles:
            logging.info("   ℹ️ لا مقالات جديدة")
            return

        remaining = ANALYSIS_PER_DAY - today_count
        sent = 0

        for art in articles:
            if sent >= remaining: break
            a = ai_analyze_article(
                art["title_en"], art["summary_en"],
                art["author"],   art["source"]
            )
            if not a.get("is_middle_east"):
                logging.info(f"   ⏭️ غير متعلق بالشرق الأوسط: {art['title_en'][:45]}")
                continue

            title_ar  = a.get("title_ar",  art["title_en"])
            author_ar = a.get("author_ar", art["author"] or "غير محدد")
            summary_ar= a.get("summary_ar","")
            importance= a.get("importance",6)

            msg = format_analysis(title_ar, author_ar, summary_ar, art["source"])
            asyncio.run(send_to_all(msg))
            save_news(art["url"],art["title_en"],title_ar,
                      "analysis",importance,0,1,art["source"])
            logging.info(f"   ✅ تحليل: {title_ar[:55]}")
            sent += 1
            time.sleep(2)

        if sent == 0:
            logging.info("   ℹ️ لا مقالات مؤهلة عن الشرق الأوسط")

    except Exception as e:
        logging.error(f"❌ analysis_cycle: {e}")

# ══════════════════════════════════════════════
#   🔄 Self-Ping
# ══════════════════════════════════════════════
def self_ping():
    try:
        r = requests.get(f"{SERVICE_URL}/health", timeout=10)
        logging.info(f"🔄 ping {r.status_code}")
    except Exception as e:
        logging.warning(f"⚠️ ping: {e}")

# ══════════════════════════════════════════════
#   🤖 اكتشاف القنوات
# ══════════════════════════════════════════════
async def on_status_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result: return
    chat   = result.chat
    status = result.new_chat_member.status
    if status in ("administrator","member"):
        add_chat(chat.id, chat.title or "بدون اسم", chat.type)
        try:
            await context.bot.send_message(
                chat_id=chat.id, parse_mode=ParseMode.HTML,
                text=(
                    "🤖 <b>بوت أخبار الشرق الأوسط — مدعوم بالذكاء الاصطناعي</b>\n\n"
                    "• أخبار طازجة من آخر ساعة فقط\n"
                    "• أخبار عاجلة فور حدوثها\n"
                    "• مقالَي تحليل سياسي يومياً\n"
                    "• ترجمة احترافية بلا تكرار"
                ),
            )
        except Exception: pass
    elif status in ("kicked","left"):
        remove_chat(chat.id)

def discover_chats():
    async def _run():
        bot = Bot(token=BOT_TOKEN)
        try:
            for u in await bot.get_updates(limit=100):
                if u.my_chat_member:
                    c  = u.my_chat_member.chat
                    st = u.my_chat_member.new_chat_member.status
                    if st in ("administrator","member"):
                        add_chat(c.id, c.title or "بدون اسم", c.type)
        except Exception as e:
            logging.warning(f"⚠️ discover: {e}")
    try:
        asyncio.run(_run())
    except Exception: pass

def start_polling():
    async def _poll():
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(ChatMemberHandler(on_status_change,ChatMemberHandler.MY_CHAT_MEMBER))
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=False)
            while True:
                await asyncio.sleep(60)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_poll())

# ══════════════════════════════════════════════
#   🌐 Flask Server
# ══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    chats = get_chats()
    conn  = sqlite3.connect(DB_FILE)
    total    = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    breaking = conn.execute("SELECT COUNT(*) FROM sent_news WHERE is_breaking=1").fetchone()[0]
    analysis = conn.execute("SELECT COUNT(*) FROM sent_news WHERE is_analysis=1").fetchone()[0]
    today_a  = conn.execute(
        "SELECT COUNT(*) FROM sent_news WHERE is_analysis=1 AND sent_at>datetime('now','start of day')"
    ).fetchone()[0]
    conn.close()
    return (
        f"✅ بوت الأخبار | القنوات: {len(chats)} | "
        f"إجمالي: {total} | عاجلة: {breaking} | "
        f"تحليلات: {analysis} (اليوم: {today_a}/{ANALYSIS_PER_DAY})"
    ), 200

@flask_app.route("/health")
def health():
    return "OK", 200

@flask_app.route("/stats")
def stats():
    conn = sqlite3.connect(DB_FILE)
    total    = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    breaking = conn.execute("SELECT COUNT(*) FROM sent_news WHERE is_breaking=1").fetchone()[0]
    analysis = conn.execute("SELECT COUNT(*) FROM sent_news WHERE is_analysis=1").fetchone()[0]
    today_a  = conn.execute(
        "SELECT COUNT(*) FROM sent_news WHERE is_analysis=1 AND sent_at>datetime('now','start of day')"
    ).fetchone()[0]
    by_cat   = conn.execute(
        "SELECT category,COUNT(*) FROM sent_news GROUP BY category ORDER BY 2 DESC"
    ).fetchall()
    by_src   = conn.execute(
        "SELECT source,COUNT(*) FROM sent_news GROUP BY source ORDER BY 2 DESC LIMIT 6"
    ).fetchall()
    conn.close()
    return json.dumps({
        "total": total, "breaking": breaking,
        "analysis": analysis, "analysis_today": f"{today_a}/{ANALYSIS_PER_DAY}",
        "by_category": dict(by_cat),
        "top_sources":  dict(by_src),
    }, ensure_ascii=False, indent=2), 200

# ══════════════════════════════════════════════
#              🚀 التشغيل
# ══════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
)

init_db()
discover_chats()

chats = get_chats()
logging.info("═" * 55)
logging.info("  🤖 بوت أخبار الشرق الأوسط — GitHub AI")
logging.info("═" * 55)
logging.info(f"  🧠 النموذج     : {AI_MODEL}")
logging.info(f"  ⏰ أخبار آخر   : {NEWS_MAX_AGE_MINUTES} دقيقة فقط")
logging.info(f"  📌 تحليلات/يوم : {ANALYSIS_PER_DAY}")
logging.info(f"  📋 القنوات     : {len(chats)}")
for cid, t in chats:
    logging.info(f"     • {t} ({cid})")
logging.info("═" * 55)

# أول تشغيل
logging.info("▶️ الدورة الأولى...")
breaking_cycle()
analysis_cycle()

# جدولة
scheduler = BackgroundScheduler(timezone="Asia/Riyadh")
scheduler.add_job(breaking_cycle, "interval", minutes=BREAKING_EVERY,  id="breaking")
scheduler.add_job(regular_cycle,  "interval", minutes=REGULAR_EVERY,   id="regular")
scheduler.add_job(analysis_cycle, "interval", minutes=ANALYSIS_EVERY,  id="analysis")
scheduler.add_job(self_ping,      "interval", minutes=PING_EVERY,       id="ping")
scheduler.start()
logging.info("⏰ الجدول يعمل")

# Polling
threading.Thread(target=start_polling, daemon=True).start()

# Flask
if __name__ == "__main__":
    logging.info(f"🌐 Flask على port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)
