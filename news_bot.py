#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║       بوت أخبار عاجلة — مدعوم بـ GitHub AI (GPT-4o mini)       ║
║  Reuters | AP | BBC | Al Jazeera | Guardian | Axios | Bloomberg  ║
╠══════════════════════════════════════════════════════════════════╣
║  ✅ كشف التكرار بالذكاء الاصطناعي                               ║
║  ✅ ترجمة احترافية بدل الترجمة الآلية                           ║
║  ✅ تصنيف الأخبار (عسكري/سياسي/اقتصادي...)                      ║
║  ✅ تقييم الأهمية 1-10                                          ║
║  ✅ أخبار الشرق الأوسط بأولوية قصوى                             ║
║  ✅ إرسال العاجلة فور حدوثها                                    ║
║  ✅ Self-ping لمنع نوم Render                                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio, feedparser, sqlite3, logging, re, html
import os, threading, time, json
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
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
AI_MODEL       = "gpt-4o-mini"
AI_ENDPOINT    = "https://models.inference.ai.azure.com/chat/completions"
SERVICE_URL    = "https://telegram-news-bot-zcn8.onrender.com"
DB_FILE        = "news_bot.db"
PORT           = int(os.environ.get("PORT", 10000))

# الحد الأقصى للأخبار لكل دورة (لتوفير استهلاك AI)
MAX_PER_BREAKING = 3
MAX_PER_REGULAR  = 1

BREAKING_EVERY = 3    # دقائق
REGULAR_EVERY  = 15   # دقائق
PING_EVERY     = 5    # دقائق

# ══════════════════════════════════════════════
#    📍 كلمات الشرق الأوسط (فلتر أولي سريع)
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

# ══════════════════════════════════════════════
#            📰 مصادر الأخبار RSS
# ══════════════════════════════════════════════
NEWS_SOURCES = [
    # ─── الشرق الأوسط المتخصصة ───
    {"name":"Al Jazeera",            "priority":"high",
     "rss":"https://www.aljazeera.com/xml/rss/all.xml"},
    {"name":"BBC Middle East",       "priority":"high",
     "rss":"http://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
    {"name":"Reuters — Middle East", "priority":"high",
     "rss":"https://news.google.com/rss/search?q=reuters+middle+east+breaking&hl=en-US&gl=US&ceid=US:en"},
    {"name":"AP — Middle East",      "priority":"high",
     "rss":"https://news.google.com/rss/search?q=associated+press+middle+east&hl=en-US&gl=US&ceid=US:en"},
    # ─── Bloomberg ───
    {"name":"Bloomberg",             "priority":"high",
     "rss":"https://feeds.bloomberg.com/markets/news.rss"},
    {"name":"Bloomberg Politics",    "priority":"high",
     "rss":"https://feeds.bloomberg.com/politics/news.rss"},
    {"name":"Bloomberg — Middle East","priority":"high",
     "rss":"https://news.google.com/rss/search?q=bloomberg+middle+east&hl=en-US&gl=US&ceid=US:en"},
    # ─── Axios ───
    {"name":"Axios",                 "priority":"high",
     "rss":"https://api.axios.com/feed/"},
    # ─── عامة ───
    {"name":"BBC News",              "priority":"normal",
     "rss":"http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name":"The Guardian",          "priority":"normal",
     "rss":"https://www.theguardian.com/world/middleeast/rss"},
    {"name":"Reuters — World",       "priority":"normal",
     "rss":"https://news.google.com/rss/search?q=reuters+breaking+news&hl=en-US&gl=US&ceid=US:en"},
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
    conn.execute("DELETE FROM sent_news WHERE sent_at < datetime('now','-7 days')")
    conn.commit()
    conn.close()
    logging.info("✅ DB جاهز")

def add_chat(chat_id, title, ctype):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO chats (chat_id,chat_title,chat_type,active) VALUES(?,?,?,1)
        ON CONFLICT(chat_id) DO UPDATE SET chat_title=excluded.chat_title, active=1
    """, (chat_id, title, ctype))
    conn.commit(); conn.close()
    logging.info(f"➕ {title} ({chat_id})")

def remove_chat(chat_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

def get_chats():
    conn  = sqlite3.connect(DB_FILE)
    rows  = conn.execute("SELECT chat_id,chat_title FROM chats WHERE active=1").fetchall()
    conn.close(); return rows

def is_url_sent(url):
    conn = sqlite3.connect(DB_FILE)
    r    = conn.execute("SELECT 1 FROM sent_news WHERE url=?", (url,)).fetchone()
    conn.close(); return r is not None

def get_recent_titles(hours=48):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT title_en FROM sent_news WHERE sent_at > datetime('now',? || ' hours')",
        (f"-{hours}",)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]

def save_news(url, title_en, title_ar, category, importance, is_breaking, source):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            "INSERT INTO sent_news(url,title_en,title_ar,category,importance,is_breaking,source)"
            " VALUES(?,?,?,?,?,?,?)",
            (url, title_en, title_ar, category, importance, is_breaking, source)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

# ══════════════════════════════════════════════
#        🤖 GitHub AI — القلب الذكي
# ══════════════════════════════════════════════
_ai_lock = threading.Lock()

def call_ai(system_prompt: str, user_prompt: str, max_tokens=500) -> dict | None:
    """استدعاء GitHub AI (GPT-4o-mini) وإرجاع JSON"""
    with _ai_lock:
        try:
            resp = requests.post(
                AI_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":           AI_MODEL,
                    "messages":        [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "max_tokens":      max_tokens,
                    "temperature":     0.1,
                    "response_format": {"type": "json_object"},
                },
                timeout=20,
            )
            data = resp.json()
            if "choices" in data:
                return json.loads(data["choices"][0]["message"]["content"])
        except Exception as e:
            logging.warning(f"⚠️ AI: {e}")
        return None

def ai_analyze_news(title_en: str, source: str, recent_titles: list) -> dict:
    """
    يحلل الخبر بالذكاء الاصطناعي:
    - هل هو مكرر؟
    - الترجمة الاحترافية للعربية
    - التصنيف والأهمية
    - هل هو متعلق بالشرق الأوسط؟
    """
    recent_sample = "\n".join(f"- {t}" for t in recent_titles[-30:]) or "لا توجد أخبار سابقة"

    system = """أنت محرر أخبار عربي محترف متخصص في الشرق الأوسط.
مهمتك تحليل الأخبار الواردة وإرجاع JSON فقط بدون أي نص إضافي."""

    user = f"""حلل هذا الخبر:
العنوان: {title_en}
المصدر: {source}

الأخبار المرسلة مسبقاً (آخر 48 ساعة):
{recent_sample}

أجب بـ JSON بهذا الشكل الدقيق:
{{
  "is_duplicate": false,
  "duplicate_reason": "اذكر العنوان المشابه إن وجد أو اتركه فارغاً",
  "is_middle_east": true,
  "is_breaking": false,
  "category": "military",
  "importance": 8,
  "title_ar": "الترجمة العربية الاحترافية هنا"
}}

قواعد:
- category: military / politics / economy / diplomacy / security / humanitarian / other
- importance: من 1 (عادي) إلى 10 (بالغ الأهمية)
- is_breaking: true إذا كان حدثاً طارئاً يستوجب الإرسال الفوري
- is_middle_east: true إذا كان متعلقاً بدول أو أحداث الشرق الأوسط
- is_duplicate: true إذا كان نفس حدث خبر سابق بصياغة مختلفة
- title_ar: ترجمة عربية سلسة واحترافية (لا تستخدم مصطلحات حرفية)"""

    result = call_ai(system, user, max_tokens=300)
    if result:
        return result

    # Fallback: تحليل يدوي بدون AI
    title_lower = title_en.lower()
    me  = any(kw in title_lower for kw in ME_KEYWORDS)
    brk = any(kw in title_lower for kw in BREAKING_KEYWORDS)
    return {
        "is_duplicate":   False,
        "duplicate_reason": "",
        "is_middle_east": me,
        "is_breaking":    brk,
        "category":       "other",
        "importance":     7 if me else 4,
        "title_ar":       title_en,  # سيُترجم لاحقاً
    }

# ══════════════════════════════════════════════
#        🌐 جلب الأخبار من RSS
# ══════════════════════════════════════════════
def clean(t: str) -> str:
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", t).strip()

def fetch_all_rss() -> list:
    """يجلب الأخبار الجديدة من كل المصادر"""
    items   = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for src in NEWS_SOURCES:
        try:
            r    = requests.get(src["rss"], headers=headers, timeout=12)
            feed = feedparser.parse(r.content)
            for e in feed.entries[:12]:
                url   = getattr(e, "link",  "")
                title = clean(getattr(e, "title", ""))
                if url and title and not is_url_sent(url):
                    items.append({
                        "url":      url,
                        "title_en": title,
                        "source":   src["name"],
                        "priority": src["priority"],
                    })
        except Exception as ex:
            logging.warning(f"⚠️ RSS {src['name']}: {ex}")
    return items

# ══════════════════════════════════════════════
#    🧹 فلتر أولي سريع (قبل استدعاء AI)
# ══════════════════════════════════════════════
def quick_filter(items: list) -> list:
    """
    يصفي الأخبار بسرعة (بدون AI) ليُرسل فقط المهم للـ AI
    يوفر استهلاك AI بنسبة 60-70%
    """
    filtered = []
    for item in items:
        tl = item["title_en"].lower()
        me_score  = sum(1 for kw in ME_KEYWORDS       if kw in tl)
        brk_score = sum(1 for kw in BREAKING_KEYWORDS if kw in tl)
        if item["priority"] == "high":
            me_score += 2
        # أرسل للـ AI فقط إذا كان له أي اتصال بالشرق الأوسط أو العاجل
        if me_score > 0 or brk_score > 0:
            item["quick_me"]  = me_score
            item["quick_brk"] = brk_score
            filtered.append(item)
    # ترتيب: الأعلى أهمية أولاً
    filtered.sort(key=lambda x: x["quick_me"] + x["quick_brk"] * 2, reverse=True)
    return filtered

# ══════════════════════════════════════════════
#        📤 إرسال الأخبار
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

def format_message(title_ar, source, category, is_breaking) -> str:
    emoji = CATEGORY_EMOJI.get(category, "🌐")
    if is_breaking:
        return f"🔴 <b>عاجل</b> {emoji}\n\n<b>{title_ar}</b>\n\n— {source}"
    return f"{emoji} <b>{title_ar}</b>\n\n— {source}"

async def send_to_all(message: str):
    chats = get_chats()
    if not chats:
        logging.warning("⚠️ لا توجد قنوات مسجلة")
        return
    bot = Bot(token=BOT_TOKEN)
    for chat_id, chat_title in chats:
        try:
            await bot.send_message(
                chat_id                  = chat_id,
                text                     = message,
                parse_mode               = ParseMode.HTML,
                disable_web_page_preview = True,
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
    """
    يفحص الأخبار الجديدة ويرسل العاجلة المتعلقة بالشرق الأوسط فوراً
    """
    try:
        logging.info("🔍 [عاجل] فحص...")
        raw_items      = fetch_all_rss()
        filtered_items = quick_filter(raw_items)
        recent_titles  = get_recent_titles(hours=48)

        sent_count = 0
        for item in filtered_items:
            if sent_count >= MAX_PER_BREAKING:
                break

            analysis = ai_analyze_news(item["title_en"], item["source"], recent_titles)

            if analysis.get("is_duplicate"):
                logging.info(f"   ♻️ مكرر: {item['title_en'][:50]}")
                # احفظه لمنع فحصه مجدداً
                save_news(item["url"], item["title_en"], "", "", 0, 0, item["source"])
                continue

            if not analysis.get("is_middle_east"):
                continue

            if not analysis.get("is_breaking") and analysis.get("importance", 0) < 8:
                continue  # في دورة العاجل، أرسل فقط العاجل أو البالغ الأهمية

            title_ar   = analysis.get("title_ar", item["title_en"])
            category   = analysis.get("category", "other")
            importance = analysis.get("importance", 5)
            is_breaking= analysis.get("is_breaking", False)

            msg = format_message(title_ar, item["source"], category, is_breaking)
            asyncio.run(send_to_all(msg))

            save_news(
                item["url"], item["title_en"], title_ar,
                category, importance, int(is_breaking), item["source"]
            )
            recent_titles.append(item["title_en"])
            sent_count += 1
            time.sleep(2)

        if sent_count == 0:
            logging.info("   ℹ️ لا أخبار عاجلة جديدة")

    except Exception as e:
        logging.error(f"❌ breaking_cycle: {e}")

# ══════════════════════════════════════════════
#   📰 دورة الأخبار العادية (كل 15 دقيقة)
# ══════════════════════════════════════════════
def regular_cycle():
    """
    يرسل أفضل خبر شرق أوسط جديد كل 15 دقيقة
    """
    try:
        logging.info("📰 [عادي] فحص...")
        raw_items      = fetch_all_rss()
        filtered_items = quick_filter(raw_items)
        recent_titles  = get_recent_titles(hours=48)

        for item in filtered_items:
            analysis = ai_analyze_news(item["title_en"], item["source"], recent_titles)

            if analysis.get("is_duplicate"):
                logging.info(f"   ♻️ مكرر: {item['title_en'][:50]}")
                save_news(item["url"], item["title_en"], "", "", 0, 0, item["source"])
                continue

            if not analysis.get("is_middle_east"):
                continue

            title_ar   = analysis.get("title_ar", item["title_en"])
            category   = analysis.get("category", "other")
            importance = analysis.get("importance", 5)
            is_breaking= analysis.get("is_breaking", False)

            msg = format_message(title_ar, item["source"], category, is_breaking)
            asyncio.run(send_to_all(msg))

            save_news(
                item["url"], item["title_en"], title_ar,
                category, importance, int(is_breaking), item["source"]
            )
            logging.info(f"   ✅ أُرسل: {title_ar[:60]}")
            break  # خبر واحد فقط في الدورة العادية

    except Exception as e:
        logging.error(f"❌ regular_cycle: {e}")

# ══════════════════════════════════════════════
#   🔄 Self-Ping (يمنع نوم Render)
# ══════════════════════════════════════════════
def self_ping():
    try:
        r = requests.get(f"{SERVICE_URL}/health", timeout=10)
        logging.info(f"🔄 ping {r.status_code}")
    except Exception as e:
        logging.warning(f"⚠️ ping: {e}")

# ══════════════════════════════════════════════
#   🤖 اكتشاف القنوات تلقائياً
# ══════════════════════════════════════════════
async def on_status_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    chat   = result.chat
    status = result.new_chat_member.status
    if status in ("administrator", "member"):
        add_chat(chat.id, chat.title or "بدون اسم", chat.type)
        try:
            await context.bot.send_message(
                chat_id    = chat.id,
                text       = (
                    "🤖 <b>بوت أخبار الشرق الأوسط — مدعوم بالذكاء الاصطناعي</b>\n\n"
                    "• أخبار عاجلة فور حدوثها\n"
                    "• ترجمة احترافية\n"
                    "• بدون تكرار\n"
                    "• تركيز على الشرق الأوسط"
                ),
                parse_mode = ParseMode.HTML,
            )
        except Exception:
            pass
    elif status in ("kicked", "left"):
        remove_chat(chat.id)

def discover_chats():
    async def _run():
        bot = Bot(token=BOT_TOKEN)
        try:
            updates = await bot.get_updates(limit=100)
            for u in updates:
                if u.my_chat_member:
                    c  = u.my_chat_member.chat
                    st = u.my_chat_member.new_chat_member.status
                    if st in ("administrator","member"):
                        add_chat(c.id, c.title or "بدون اسم", c.type)
        except Exception as e:
            logging.warning(f"⚠️ discover: {e}")
    try:
        asyncio.run(_run())
    except Exception:
        pass

def start_polling():
    async def _poll():
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(ChatMemberHandler(on_status_change, ChatMemberHandler.MY_CHAT_MEMBER))
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
    total = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    conn.close()
    return (
        f"✅ بوت الأخبار يعمل | القنوات: {len(chats)} | "
        f"أخبار مُرسلة: {total}"
    ), 200

@flask_app.route("/health")
def health():
    return "OK", 200

@flask_app.route("/stats")
def stats():
    conn = sqlite3.connect(DB_FILE)
    total   = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    breaking= conn.execute("SELECT COUNT(*) FROM sent_news WHERE is_breaking=1").fetchone()[0]
    by_cat  = conn.execute(
        "SELECT category, COUNT(*) FROM sent_news GROUP BY category ORDER BY 2 DESC"
    ).fetchall()
    by_src  = conn.execute(
        "SELECT source, COUNT(*) FROM sent_news GROUP BY source ORDER BY 2 DESC LIMIT 5"
    ).fetchall()
    conn.close()
    result = {
        "total": total, "breaking": breaking,
        "by_category": dict(by_cat),
        "top_sources":  dict(by_src),
    }
    return json.dumps(result, ensure_ascii=False, indent=2), 200

# ══════════════════════════════════════════════
#              🚀 التشغيل الرئيسي
# ══════════════════════════════════════════════
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)s | %(message)s",
    datefmt = "%H:%M:%S",
)

# تهيئة
init_db()
discover_chats()

chats = get_chats()
logging.info("═" * 55)
logging.info("  🤖 بوت أخبار الشرق الأوسط — GitHub AI")
logging.info("═" * 55)
logging.info(f"  🧠 النموذج   : {AI_MODEL}")
logging.info(f"  📋 القنوات   : {len(chats)}")
for cid, t in chats: logging.info(f"     • {t} ({cid})")
logging.info(f"  🚨 عاجل كل  : {BREAKING_EVERY} دقيقة")
logging.info(f"  📰 عادي كل  : {REGULAR_EVERY} دقيقة")
logging.info(f"  🔄 ping كل  : {PING_EVERY} دقيقة")
logging.info("═" * 55)

# الدورة الأولى
logging.info("▶️ الدورة الأولى...")
breaking_cycle()

# الجدولة
scheduler = BackgroundScheduler(timezone="Asia/Riyadh")
scheduler.add_job(breaking_cycle, "interval", minutes=BREAKING_EVERY, id="breaking")
scheduler.add_job(regular_cycle,  "interval", minutes=REGULAR_EVERY,  id="regular")
scheduler.add_job(self_ping,      "interval", minutes=PING_EVERY,      id="ping")
scheduler.start()
logging.info("⏰ الجدول يعمل")

# Polling
threading.Thread(target=start_polling, daemon=True).start()

# Flask
if __name__ == "__main__":
    logging.info(f"🌐 Flask على port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)
