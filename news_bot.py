#!/usr/bin/env python3
"""
Breaking News Bot v6.0
- Google Translate مجاني (بدون مفتاح)
- فلترة بالكلمات المفتاحية فقط (سريع وموثوق)
- تشغيل مستمر مع self-ping كل 5 دقائق
"""

import os, json, logging, sqlite3, re, time, html as html_lib, threading, urllib.parse
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
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ["BOT_TOKEN"]
RENDER_URL  = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
DB_PATH     = "/tmp/newsbot.db"
CHECK_EVERY = 5 * 60       # كل 5 دقائق
NEWS_MAX_AGE = 360          # آخر 6 ساعات
MECCA_TZ    = timezone(timedelta(hours=3))

# ──────────────────────────────────────────────
# مصادر غربية موثوقة فقط
# ──────────────────────────────────────────────
SOURCES = [
    # 🌍 أخبار عالمية وشرق أوسط
    {"name": "BBC World",       "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "BBC Middle East", "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
    {"name": "Al Jazeera EN",   "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Reuters",         "url": "https://feeds.reuters.com/reuters/worldNews"},
    {"name": "AP News",         "url": "https://feeds.apnews.com/rss/apf-topnews"},
    {"name": "Guardian World",  "url": "https://www.theguardian.com/world/rss"},
    {"name": "France24 EN",     "url": "https://www.france24.com/en/rss"},
    # 💻 تقنية
    {"name": "TechCrunch",      "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge",       "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica",    "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Wired",           "url": "https://www.wired.com/feed/rss"},
]

# ──────────────────────────────────────────────
# كلمات مفتاحية — شرق أوسط + تقنية
# ──────────────────────────────────────────────
ACCEPT_KW = [
    # دول الشرق الأوسط
    "saudi", "arabia", "riyadh", "jeddah",
    "kuwait",
    "uae", "emirates", "dubai", "abu dhabi",
    "bahrain",
    "egypt", "cairo",
    "yemen", "houthi",
    "iraq", "baghdad",
    "iran", "tehran", "iranian",
    "pakistan", "islamabad",
    "india", "delhi", "modi",
    "afghanistan", "kabul", "taliban",
    "china", "beijing", "xi jinping",
    "israel", "gaza", "palestine", "hamas", "netanyahu",
    "lebanon", "beirut", "hezbollah",
    "syria", "damascus",
    "middle east", "persian gulf", "red sea", "gulf state",
    "opec", "oil price", "crude oil",
    # تقنية
    "artificial intelligence", " ai ", "chatgpt", "openai",
    "google", "microsoft", "apple", "meta ", "nvidia",
    "cybersecurity", "cyber attack", "data breach", "hack",
    "semiconductor", "chip",
    "spacex", "nasa",
    "robot", "automation",
    "electric vehicle", " ev ", "tesla",
    "tech giant", "silicon valley",
]

REJECT_KW = [
    "football", "soccer", "basketball", "tennis", "golf", "cricket",
    "nba", "nfl", "premier league", "champions league", "world cup",
    "oscar", "grammy", "celebrity", "actor", "singer", "musician",
    "fashion", "makeup", "skincare", "beauty tip",
    "recipe", "cooking", "restaurant",
    "horoscope", "zodiac",
    "dating", "romance",
    "movie review", "tv show", "netflix", "disney+",
    "lottery", "casino",
]

# ──────────────────────────────────────────────
# Google Translate (مجاني - بدون API key)
# ──────────────────────────────────────────────
def google_translate(text):
    """ترجمة فورية مجانية عبر Google"""
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl":     "en",
            "tl":     "ar",
            "dt":     "t",
            "q":      text,
        }
        r = requests.get(url, params=params, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        # نجمع كل أجزاء الترجمة
        translated = "".join(part[0] for part in data[0] if part[0])
        return translated.strip()
    except Exception as e:
        logging.warning(f"translate error: {e}")
        return None

# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_news (
            url      TEXT PRIMARY KEY,
            title_ar TEXT,
            source   TEXT,
            sent     INTEGER DEFAULT 0,
            saved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id  INTEGER PRIMARY KEY,
            title    TEXT,
            added_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    logging.info("✅ DB جاهزة")

def is_seen(url):
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute("SELECT 1 FROM seen_news WHERE url=? LIMIT 1", (url,)).fetchone()
        conn.close()
        return row is not None
    except:
        return False

def mark_seen(url, title_ar, source, sent=0):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO seen_news(url,title_ar,source,sent,saved_at) VALUES(?,?,?,?,?)",
            (url, title_ar, source, sent, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"mark_seen: {e}")

def get_channels():
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT chat_id FROM channels").fetchall()
        conn.close()
        return [r[0] for r in rows]
    except:
        return []

def add_channel(chat_id, title=""):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO channels(chat_id,title,added_at) VALUES(?,?,?)",
            (chat_id, title, now),
        )
        conn.commit()
        conn.close()
        logging.info(f"➕ قناة: {title} ({chat_id})")
    except Exception as e:
        logging.warning(f"add_channel: {e}")

def remove_channel(chat_id):
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
def get_age_min(entry):
    pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not pub:
        return None
    try:
        dt = datetime(*pub[:6], tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    except:
        return None

def clean_title(t):
    if not t:
        return ""
    t = html_lib.unescape(t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def is_relevant(title):
    """فلترة بالكلمات المفتاحية فقط — سريع وموثوق"""
    tl = title.lower()
    if any(kw in tl for kw in REJECT_KW):
        return False
    return any(kw in tl for kw in ACCEPT_KW)

def pick_emoji(title, source):
    tl = title.lower()
    if any(w in tl for w in ["attack","war","strike","bomb","kill","dead","missile","explosion"]):
        return "⚠️"
    if any(w in tl for w in ["ceasefire","deal","agreement","peace","talks","negotiat"]):
        return "🕊️"
    if any(w in tl for w in ["ai","artificial intelligence","chip","tech","robot","cyber","hack","software"]):
        return "💻"
    if any(w in tl for w in ["oil","opec","economy","market","gdp","inflation","trade"]):
        return "💰"
    if any(w in tl for w in ["earthquake","flood","fire","disaster","storm"]):
        return "🚨"
    return "🔴"

# ──────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(chat_id, text):
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id":                  chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        d = r.json()
        if not d.get("ok"):
            err = d.get("description", "?")
            logging.warning(f"TG [{chat_id}]: {err}")
            # إذا حُذف البوت من القناة
            if "blocked" in err or "not found" in err or "kicked" in err:
                remove_channel(chat_id)
    except Exception as e:
        logging.warning(f"tg_send [{chat_id}]: {e}")

def send_to_all(text):
    chats = get_channels()
    if not chats:
        logging.info("⚠️ لا توجد قنوات مسجلة")
        return
    for cid in chats:
        tg_send(cid, text)
        time.sleep(0.3)

def fmt_news(title_ar, emoji, source, url):
    now = datetime.now(MECCA_TZ)
    return (
        f"{emoji} <b>{title_ar}</b>\n\n"
        f"📰 {source}\n"
        f'🔗 <a href="{url}">اقرأ التفاصيل</a>\n'
        f"🕐 {now.strftime('%H:%M')}  {now.strftime('%d/%m/%Y')}"
    )

# ──────────────────────────────────────────────
# Telegram Polling
# ──────────────────────────────────────────────
_tg_offset = 0

def tg_poll():
    global _tg_offset
    logging.info("🤖 Telegram polling بدأ")
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={
                    "offset":           _tg_offset,
                    "timeout":          25,
                    "allowed_updates":  '["message","my_chat_member"]',
                },
                timeout=35,
            )
            if not r.ok:
                time.sleep(5)
                continue
            for u in r.json().get("result", []):
                _tg_offset = u["update_id"] + 1
                _handle_update(u)
        except Exception as e:
            logging.warning(f"tg_poll: {e}")
            time.sleep(5)

def _handle_update(u):
    # إضافة/حذف قناة
    if "my_chat_member" in u:
        mc     = u["my_chat_member"]
        chat   = mc["chat"]
        status = mc["new_chat_member"]["status"]
        cid    = chat["id"]
        title  = chat.get("title") or chat.get("username") or str(cid)
        if status in ("member", "administrator"):
            add_channel(cid, title)
            tg_send(cid,
                "✅ <b>تم تفعيل البوت!</b>\n\n"
                "📌 سأرسل الأخبار العاجلة فور صدورها:\n"
                "🌍 الشرق الأوسط (السعودية، الكويت، الإمارات، إيران، غزة، الهند، الصين...)\n"
                "💻 التقنية والذكاء الاصطناعي\n\n"
                "⏱️ يتحقق كل 5 دقائق"
            )
        elif status in ("left", "kicked"):
            remove_channel(cid)
    # /start في الخاص
    if "message" in u:
        msg  = u["message"]
        text = msg.get("text", "")
        chat = msg.get("chat", {})
        if text.startswith("/start"):
            cid   = chat["id"]
            title = chat.get("title") or chat.get("first_name") or str(cid)
            add_channel(cid, title)
            tg_send(cid,
                "✅ <b>مرحباً!</b>\n\n"
                "سأرسل لك الأخبار العاجلة عن:\n"
                "🌍 الشرق الأوسط والدول المجاورة\n"
                "💻 التقنية والذكاء الاصطناعي\n\n"
                "⏱️ يتحقق من الأخبار كل 5 دقائق"
            )

# ──────────────────────────────────────────────
# دورة الأخبار الرئيسية
# ──────────────────────────────────────────────
_cycle_lock  = threading.Lock()
_stats       = {"processed": 0, "sent": 0, "last_sent": []}

def news_cycle():
    if not _cycle_lock.acquire(blocking=False):
        return
    try:
        logging.info("🔄 [Cycle] بدء")
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/6.0)"}
        sent_this_cycle = 0

        for src in SOURCES:
            try:
                r    = requests.get(src["url"], headers=headers, timeout=12)
                feed = feedparser.parse(r.content)
                new_count = 0

                for entry in feed.entries[:15]:
                    url   = getattr(entry, "link", "").strip()
                    title = clean_title(getattr(entry, "title", ""))

                    if not url or not title:
                        continue
                    if is_seen(url):
                        continue

                    # فلتر العمر
                    age = get_age_min(entry)
                    if age is not None and age > NEWS_MAX_AGE:
                        mark_seen(url, "", src["name"], sent=0)
                        continue

                    # فلتر الكلمات المفتاحية
                    if not is_relevant(title):
                        mark_seen(url, "", src["name"], sent=0)
                        continue

                    # ترجمة Google
                    logging.info(f"  🌐 ترجمة: {title[:60]}")
                    title_ar = google_translate(title)

                    if not title_ar:
                        mark_seen(url, title, src["name"], sent=0)
                        continue

                    emoji = pick_emoji(title, src["name"])
                    msg   = fmt_news(title_ar, emoji, src["name"], url)

                    send_to_all(msg)
                    mark_seen(url, title_ar, src["name"], sent=1)
                    new_count += 1
                    sent_this_cycle += 1
                    _stats["sent"] += 1
                    _stats["last_sent"].insert(0, {
                        "title_ar": title_ar[:70],
                        "source":   src["name"],
                        "at":       datetime.now(MECCA_TZ).strftime("%H:%M"),
                    })
                    _stats["last_sent"] = _stats["last_sent"][:10]
                    logging.info(f"  ✅ أُرسل: {title_ar[:60]}")
                    time.sleep(2)

                if new_count:
                    logging.info(f"  📡 {src['name']}: {new_count} أُرسل")

            except Exception as e:
                logging.warning(f"❌ [{src['name']}]: {e}")

        _stats["processed"] += 1
        logging.info(f"🏁 [Cycle] أُرسل: {sent_this_cycle}")
    finally:
        _cycle_lock.release()

# ──────────────────────────────────────────────
# Scheduler — يعمل كل 5 دقائق
# ──────────────────────────────────────────────
def scheduler():
    logging.info("⏱️ Scheduler بدأ")
    while True:
        try:
            news_cycle()
        except Exception as e:
            logging.error(f"Scheduler: {e}")
        time.sleep(CHECK_EVERY)

# ──────────────────────────────────────────────
# Self-Ping — يمنع Render من إيقاف الخادم
# ──────────────────────────────────────────────
def self_ping():
    """يرسل طلب لنفسه كل 5 دقائق لإبقاء الخادم نشطاً"""
    if not RENDER_URL:
        logging.warning("⚠️ RENDER_EXTERNAL_URL غير محدد — self-ping معطل")
        return
    logging.info(f"🏓 Self-ping: {RENDER_URL}/health")
    while True:
        time.sleep(4 * 60)   # كل 4 دقائق (أقل من حد Render البالغ 15 دق)
        try:
            requests.get(f"{RENDER_URL}/health", timeout=8)
            logging.info("🏓 ping OK")
        except Exception as e:
            logging.warning(f"ping failed: {e}")

# ──────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen_news").fetchone()[0]
        sent  = conn.execute("SELECT COUNT(*) FROM seen_news WHERE sent=1").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        conn.close()
    except:
        total = sent = chats = 0
    return (
        f"✅ Breaking News Bot v6.0 | "
        f"قنوات: {chats} | "
        f"أُرسل: {sent} | "
        f"معالجة: {total}"
    )

@app.route("/health")
def health():
    return "OK", 200

@app.route("/trigger")
def trigger():
    threading.Thread(target=news_cycle, daemon=True).start()
    return "🚀 تم تشغيل الدورة", 200

@app.route("/stats")
def stats():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen_news").fetchone()[0]
        sent  = conn.execute("SELECT COUNT(*) FROM seen_news WHERE sent=1").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        last5 = conn.execute(
            "SELECT title_ar, source, saved_at FROM seen_news WHERE sent=1 ORDER BY rowid DESC LIMIT 5"
        ).fetchall()
        conn.close()
        return json.dumps({
            "version":   "6.0",
            "channels":  chats,
            "sent":      sent,
            "processed": total,
            "last_sent": [{"title": r[0][:70], "source": r[1], "at": r[2]} for r in last5],
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route("/channels")
def list_channels():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT chat_id, title, added_at FROM channels").fetchall()
    conn.close()
    return json.dumps(
        [{"id": r[0], "title": r[1], "added": r[2]} for r in rows],
        ensure_ascii=False, indent=2
    ), 200, {"Content-Type": "application/json"}

@app.route("/add/<int:chat_id>")
def add_chat_route(chat_id):
    add_channel(chat_id, f"manual-{chat_id}")
    return json.dumps({"ok": True, "chat_id": chat_id}), 200, {"Content-Type": "application/json"}

@app.route("/reset")
def reset_db():
    """مسح سجل الأخبار — يُعيد المعالجة من جديد"""
    try:
        conn  = sqlite3.connect(DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM seen_news").fetchone()[0]
        conn.execute("DELETE FROM seen_news")
        conn.commit()
        conn.close()
        return json.dumps({"ok": True, "deleted": count}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

# ──────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────
def _startup():
    time.sleep(3)   # انتظر حتى يبدأ Flask
    init_db()
    threading.Thread(target=tg_poll,   daemon=True, name="tg-poll").start()
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
    threading.Thread(target=self_ping, daemon=True, name="self-ping").start()
    logging.info("🚀 Bot v6.0 جاهز — كل المكونات تعمل")

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logging.info(f"🌐 Flask على المنفذ {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
