#!/usr/bin/env python3
"""
Breaking News Bot v5.0
- مصادر غربية فقط
- إرسال فوري (بدون موجز)
- ترجمة AI بـ Llama 3.1 405B
- الشرق الأوسط + التقنية
"""

import os, json, logging, sqlite3, re, time, html as html_lib, threading
from datetime import datetime, timezone, timedelta
import feedparser, requests
from flask import Flask

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
RENDER_URL   = os.environ.get("RENDER_EXTERNAL_URL", "")
DB_PATH      = "/tmp/newsbot.db"
CHECK_EVERY  = 5 * 60      # كل 5 دقائق
NEWS_MAX_AGE = 120          # آخر ساعتين فقط
MECCA_TZ     = timezone(timedelta(hours=3))

# ──────────────────────────────────────────────
# AI
# ──────────────────────────────────────────────
AI_ENDPOINT = "https://models.inference.ai.azure.com"
AI_MODEL    = "Meta-Llama-3.1-405B-Instruct"

# ──────────────────────────────────────────────
# مصادر غربية فقط
# ──────────────────────────────────────────────
SOURCES = [
    # 🌍 عالمية / شرق أوسط
    {"name": "Reuters World",   "url": "https://feeds.reuters.com/reuters/worldNews",              "cat": "world"},
    {"name": "BBC World",       "url": "https://feeds.bbci.co.uk/news/world/rss.xml",              "cat": "world"},
    {"name": "BBC Middle East", "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",  "cat": "mideast"},
    {"name": "Al Jazeera EN",   "url": "https://www.aljazeera.com/xml/rss/all.xml",               "cat": "world"},
    {"name": "Guardian World",  "url": "https://www.theguardian.com/world/rss",                   "cat": "world"},
    {"name": "AP News",         "url": "https://feeds.apnews.com/rss/apf-topnews",                "cat": "world"},
    {"name": "CNN World",       "url": "http://rss.cnn.com/rss/edition_world.rss",                "cat": "world"},
    {"name": "France24 EN",     "url": "https://www.france24.com/en/rss",                         "cat": "world"},
    # 💻 تقنية
    {"name": "TechCrunch",      "url": "https://techcrunch.com/feed/",                            "cat": "tech"},
    {"name": "The Verge",       "url": "https://www.theverge.com/rss/index.xml",                  "cat": "tech"},
    {"name": "Ars Technica",    "url": "https://feeds.arstechnica.com/arstechnica/index",          "cat": "tech"},
    {"name": "Wired",           "url": "https://www.wired.com/feed/rss",                          "cat": "tech"},
]

# ──────────────────────────────────────────────
# كلمات مفتاحية
# ──────────────────────────────────────────────
MIDEAST_KW = [
    "saudi", "arabia", "riyadh", "jeddah", "ksa",
    "kuwait",
    "uae", "emirates", "dubai", "abu dhabi",
    "bahrain",
    "egypt", "cairo", "egyptian",
    "yemen", "sanaa", "houthi",
    "iraq", "baghdad", "iraqi",
    "iran", "tehran", "iranian",
    "pakistan", "islamabad", "karachi", "pakistani",
    "india", "delhi", "mumbai", "modi", "indian",
    "afghanistan", "kabul", "taliban",
    "china", "beijing", "xi jinping", "chinese",
    "israel", "gaza", "palestine", "west bank", "hamas", "netanyahu",
    "lebanon", "beirut", "hezbollah",
    "syria", "damascus", "syrian",
    "jordan", "amman",
    "middle east", "persian gulf", "gulf state",
    "red sea", "strait of hormuz",
    "opec", "oil price", "crude oil",
]

TECH_KW = [
    "artificial intelligence", " ai ", "chatgpt", "openai", "gemini",
    "machine learning", "deep learning", "neural",
    "google", "microsoft", "apple", "meta ", "nvidia", "amazon",
    "semiconductor", "chip", "microchip",
    "cybersecurity", "cyber attack", "hack", "data breach",
    "robot", "automation", "autonomous",
    "5g", "6g", "satellite",
    "spacex", "nasa", "space mission",
    "electric vehicle", "ev ", "tesla",
    "iphone", "android", "smartphone",
    "quantum", "blockchain", "crypto",
    "tech giant", "silicon valley", "startup",
]

ALL_KW = MIDEAST_KW + TECH_KW

BLACKLIST_KW = [
    "sport", "football", "soccer", "basketball", "tennis", "golf",
    "nba", "nfl", "premier league", "champions league", "world cup",
    "oscar", "grammy", "celebrity", "actor", "actress", "singer",
    "fashion", "beauty", "makeup", "skincare",
    "recipe", "cooking", "food review",
    "horoscope", "zodiac",
    "lottery", "casino",
    "dating", "romance",
    "pet ", "dog ", "cat ",
    "movie review", "tv show", "netflix series",
]

# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_news (
            url      TEXT PRIMARY KEY,
            title    TEXT,
            source   TEXT,
            sent_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id  INTEGER PRIMARY KEY,
            title    TEXT
        )
    """)
    conn.commit()
    conn.close()
    logging.info("✅ DB جاهزة")

def is_seen(url: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute("SELECT 1 FROM seen_news WHERE url=? LIMIT 1", (url,)).fetchone()
        conn.close()
        return row is not None
    except:
        return False

def mark_seen(url: str, title: str, source: str):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO seen_news(url,title,source,sent_at) VALUES(?,?,?,?)",
            (url, title, source, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"mark_seen error: {e}")

def get_channels() -> list[int]:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT chat_id FROM channels").fetchall()
        conn.close()
        return [r[0] for r in rows]
    except:
        return []

def add_channel(chat_id: int, title: str = ""):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO channels(chat_id,title) VALUES(?,?)",
            (chat_id, title),
        )
        conn.commit()
        conn.close()
        logging.info(f"➕ قناة مضافة: {title} ({chat_id})")
    except Exception as e:
        logging.warning(f"add_channel error: {e}")

def remove_channel(chat_id: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
        conn.commit()
        conn.close()
    except:
        pass

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def get_age_min(entry) -> int | None:
    pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not pub:
        return None
    try:
        dt = datetime(*pub[:6], tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    except:
        return None

def clean_title(t: str) -> str:
    if not t:
        return ""
    t = html_lib.unescape(t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

# ──────────────────────────────────────────────
# Telegram - إرسال مباشر عبر HTTP
# ──────────────────────────────────────────────
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(chat_id: int, text: str):
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not r.ok:
            logging.warning(f"TG [{chat_id}]: {r.json().get('description','?')}")
    except Exception as e:
        logging.warning(f"tg_send error [{chat_id}]: {e}")

def send_to_all(text: str):
    for cid in get_channels():
        tg_send(cid, text)
        time.sleep(0.5)

# ──────────────────────────────────────────────
# Telegram Polling (للقنوات الجديدة)
# ──────────────────────────────────────────────
_tg_offset = 0

def tg_poll():
    global _tg_offset
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": _tg_offset, "timeout": 30, "allowed_updates": '["message","my_chat_member"]'},
                timeout=40,
            )
            if not r.ok:
                time.sleep(5)
                continue
            updates = r.json().get("result", [])
            for u in updates:
                _tg_offset = u["update_id"] + 1

                # إضافة قناة عند إضافة البوت
                if "my_chat_member" in u:
                    mc     = u["my_chat_member"]
                    chat   = mc["chat"]
                    status = mc["new_chat_member"]["status"]
                    if status in ("member", "administrator"):
                        add_channel(chat["id"], chat.get("title", str(chat["id"])))
                        tg_send(chat["id"],
                            "✅ <b>تم تفعيل البوت!</b>\n"
                            "سأرسل لك الأخبار العاجلة فور صدورها 🔴\n\n"
                            "المواضيع: الشرق الأوسط 🌍 | التقنية 💻"
                        )
                    elif status in ("left", "kicked"):
                        remove_channel(chat["id"])

                # /start في الخاص أو مجموعة
                if "message" in u:
                    msg  = u["message"]
                    text = msg.get("text", "")
                    chat = msg.get("chat", {})
                    if text.startswith("/start"):
                        add_channel(chat["id"], chat.get("title") or chat.get("first_name", ""))
                        tg_send(chat["id"],
                            "✅ <b>مرحباً!</b>\n"
                            "سأرسل لك الأخبار العاجلة فور صدورها 🔴\n\n"
                            "📌 المواضيع:\n"
                            "🌍 الشرق الأوسط (السعودية، الكويت، الإمارات، البحرين، مصر، اليمن، العراق، إيران، باكستان، الهند، أفغانستان، الصين)\n"
                            "💻 التقنية والذكاء الاصطناعي"
                        )
        except Exception as e:
            logging.warning(f"tg_poll error: {e}")
            time.sleep(5)

# ──────────────────────────────────────────────
# AI - ترجمة وفلترة
# ──────────────────────────────────────────────
def ai_process(title_en: str, source: str) -> dict | None:
    prompt = f"""أنت محرر أخبار عربي محترف. حلّل هذا العنوان الإنجليزي:

"{title_en}"
المصدر: {source}

قرر:
1. هل الخبر يخص أياً من هذه المواضيع؟
   أ) دول: السعودية، الكويت، الإمارات، البحرين، مصر، اليمن، العراق، إيران، باكستان، الهند، أفغانستان، الصين، إسرائيل، غزة، لبنان، سوريا، الخليج العربي
   ب) التقنية: ذكاء اصطناعي، أمن سيبراني، شركات تقنية كبرى، فضاء

2. إذا كان ذا صلة: ترجم العنوان للعربية الفصحى البسيطة بشكل طبيعي وغير حرفي.
   اختر إيموجي مناسب: 🔴 (سياسي/أمني) | 💰 (اقتصادي) | 💻 (تقني) | ⚠️ (طوارئ) | 🌐 (دولي)

❌ ارفض: الرياضة، الترفيه، المحليات التافهة، الطقس، الطعام، الوصفات

أجب بـ JSON فقط بدون شرح:
{{"relevant": true, "title_ar": "العنوان بالعربية", "emoji": "🔴"}}
أو:
{{"relevant": false}}"""

    try:
        r = requests.post(
            f"{AI_ENDPOINT}/chat/completions",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Content-Type":  "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens":  200,
                "response_format": {"type": "json_object"},
            },
            timeout=25,
        )
        raw = r.json()["choices"][0]["message"]["content"].strip()
        m   = re.search(r"\{.*?\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        logging.warning(f"AI error: {e}")
    return None

# ──────────────────────────────────────────────
# تنسيق الرسالة
# ──────────────────────────────────────────────
def fmt_news(title_ar: str, emoji: str, source: str, url: str) -> str:
    now      = datetime.now(MECCA_TZ)
    time_str = now.strftime("%H:%M")
    date_str = now.strftime("%d/%m/%Y")
    return (
        f"{emoji} <b>{title_ar}</b>\n\n"
        f"📰 {source}\n"
        f"🕐 {time_str} | {date_str} (مكة)\n"
        f'🔗 <a href="{url}">اقرأ المزيد</a>'
    )

# ──────────────────────────────────────────────
# دورة الأخبار الرئيسية
# ──────────────────────────────────────────────
_cycle_lock = threading.Lock()

def news_cycle():
    if not _cycle_lock.acquire(blocking=False):
        logging.info("⏭️ دورة تعمل بالفعل، تخطي")
        return
    try:
        logging.info("🔄 [Cycle] بدء")
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/5.0; +https://github.com)"}
        sent = 0

        for src in SOURCES:
            try:
                r    = requests.get(src["url"], headers=headers, timeout=15)
                feed = feedparser.parse(r.content)
                n    = len(feed.entries)
                logging.info(f"  📡 {src['name']}: {n} مقالة")

                for entry in feed.entries[:12]:
                    url   = getattr(entry, "link", "").strip()
                    title = clean_title(getattr(entry, "title", ""))

                    if not url or not title:
                        continue

                    # تجنب التكرار
                    if is_seen(url):
                        continue

                    # فلتر العمر: آخر ساعتين
                    age = get_age_min(entry)
                    if age is not None and age > NEWS_MAX_AGE:
                        mark_seen(url, title, src["name"])
                        continue

                    tl = title.lower()

                    # ❌ رفض سريع: رياضة/ترفيه
                    if any(kw in tl for kw in BLACKLIST_KW):
                        mark_seen(url, title, src["name"])
                        continue

                    # ✅ تحقق من الصلة بالمواضيع
                    cat = src["cat"]
                    in_scope = (cat in ("mideast", "tech")) or \
                               any(kw in tl for kw in ALL_KW)

                    if not in_scope:
                        mark_seen(url, title, src["name"])
                        continue

                    # 🤖 AI: ترجمة + فلترة نهائية
                    logging.info(f"  🤖 AI يعالج: {title[:60]}")
                    result = ai_process(title, src["name"])
                    mark_seen(url, title, src["name"])

                    if not result or not result.get("relevant"):
                        logging.info(f"  ↩️ AI رفض الخبر")
                        continue

                    title_ar = result.get("title_ar", "").strip()
                    emoji    = result.get("emoji", "🔴")

                    if not title_ar:
                        continue

                    # 📤 إرسال
                    msg = fmt_news(title_ar, emoji, src["name"], url)
                    send_to_all(msg)
                    sent += 1
                    logging.info(f"  ✅ أُرسل [{sent}]: {title_ar[:60]}")
                    time.sleep(3)  # تأخير بين الرسائل لتجنب spam

            except Exception as e:
                logging.warning(f"❌ [{src['name']}]: {e}")

        logging.info(f"🏁 [Cycle] انتهى | أُرسل: {sent}")
    finally:
        _cycle_lock.release()

# ──────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────
def scheduler():
    logging.info(f"⏱️ Scheduler: كل {CHECK_EVERY//60} دقائق")
    while True:
        try:
            news_cycle()
        except Exception as e:
            logging.error(f"Scheduler error: {e}")
        time.sleep(CHECK_EVERY)

# ──────────────────────────────────────────────
# Self-Ping (يمنع Render من إيقاف الخادم)
# ──────────────────────────────────────────────
def self_ping():
    if not RENDER_URL:
        return
    while True:
        time.sleep(14 * 60)
        try:
            requests.get(f"{RENDER_URL}/health", timeout=10)
            logging.info("🏓 self-ping OK")
        except:
            pass

# ──────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen_news").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        conn.close()
    except:
        total = chats = 0
    return f"✅ Breaking News Bot v5.0 | قنوات: {chats} | أخبار معالجة: {total}"

@app.route("/health")
def health():
    return "OK", 200

@app.route("/trigger")
def trigger():
    threading.Thread(target=news_cycle, daemon=True).start()
    return "🚀 تم تشغيل الدورة — انتظر دقيقة", 200

@app.route("/stats")
def stats():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen_news").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        last5 = conn.execute(
            "SELECT title, source, sent_at FROM seen_news ORDER BY rowid DESC LIMIT 5"
        ).fetchall()
        conn.close()
        return json.dumps({
            "version":   "5.0",
            "processed": total,
            "channels":  chats,
            "last_5":    [{"title": r[0][:70], "source": r[1], "at": r[2]} for r in last5],
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route("/channels")
def list_channels():
    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute("SELECT chat_id, title FROM channels").fetchall()
    conn.close()
    return json.dumps([{"id": r[0], "title": r[1]} for r in rows],
                      ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}

# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
if __name__ == "__main__":
    init_db()

    # Telegram polling
    threading.Thread(target=tg_poll,    daemon=True, name="tg-poll").start()
    # News scheduler
    threading.Thread(target=scheduler,  daemon=True, name="scheduler").start()
    # Self-ping
    threading.Thread(target=self_ping,  daemon=True, name="self-ping").start()

    port = int(os.environ.get("PORT", 8080))
    logging.info(f"🚀 Bot v5.0 | منفذ: {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
