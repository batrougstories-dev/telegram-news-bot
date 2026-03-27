#!/usr/bin/env python3
"""
📚 Novel & Story Summaries Bot v7.1
يجلب ملخصات الروايات والقصص من 9 مواقع متخصصة
ويترجمها للعربية ويرسلها على Telegram
"""

import os, json, logging, sqlite3, re, time, html as html_lib, threading
from datetime import datetime, timezone, timedelta
import feedparser, requests
from flask import Flask

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ["BOT_TOKEN"]
RENDER_URL   = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
DEFAULT_CHAT = int(os.environ.get("DEFAULT_CHAT_ID", "0"))
DB_PATH      = "/tmp/storybot.db"
CHECK_EVERY  = 6 * 60 * 60   # كل 6 ساعات (مرتين يومياً)
MECCA_TZ     = timezone(timedelta(hours=3))

# ─────────────────────────────────────────
# مصادر ملخصات الروايات والقصص
# ─────────────────────────────────────────
SOURCES = [
    {
        "name":  "Four Minute Books",
        "url":   "https://fourminutebooks.com/feed/",
        "emoji": "📖",
        "desc":  "ملخصات كتب في 4 دقائق",
    },
    {
        "name":  "Crime Fiction Lover",
        "url":   "https://crimefictionlover.com/feed/",
        "emoji": "🔍",
        "desc":  "روايات بوليسية وجريمة",
    },
    {
        "name":  "Novel Suspects",
        "url":   "https://novelsuspects.com/feed/",
        "emoji": "📚",
        "desc":  "روايات وقصص متنوعة",
    },
    {
        "name":  "Book Summary Club",
        "url":   "https://booksummaryclub.com/feed",
        "emoji": "📝",
        "desc":  "ملخصات الكتب الشهيرة",
    },
    {
        "name":  "Shortform",
        "url":   "https://www.shortform.com/blog/feed/",
        "emoji": "✨",
        "desc":  "تحليل عميق للكتب",
    },
    {
        "name":  "Literary Hub",
        "url":   "https://lithub.com/feed/",
        "emoji": "🖊️",
        "desc":  "أدب وروايات عالمية",
    },
    {
        "name":  "Book Riot",
        "url":   "https://bookriot.com/feed/",
        "emoji": "📕",
        "desc":  "أخبار عالم الكتب",
    },
    {
        "name":  "The Guardian Books",
        "url":   "https://www.theguardian.com/books/rss",
        "emoji": "🏛️",
        "desc":  "مراجعات الروايات - الغارديان",
    },
    {
        "name":  "NPR Books",
        "url":   "https://feeds.npr.org/1032/rss.xml",
        "emoji": "🎙️",
        "desc":  "توصيات ومراجعات NPR",
    },
]

# ─────────────────────────────────────────
# Google Translate مجاني
# ─────────────────────────────────────────
def translate_chunk(text):
    """يترجم قطعة نص واحدة (حتى 4500 حرف) للعربية"""
    if not text or not text.strip():
        return ""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": text},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        data = r.json()
        return "".join(part[0] for part in data[0] if part[0]).strip()
    except Exception as e:
        logging.warning(f"translate_chunk: {e}")
        return text   # أعد الأصل عند الفشل

def translate(text, max_chars=99999):
    """يترجم نصاً كاملاً مهما كان طوله (يقسمه إلى قطع)"""
    if not text or not text.strip():
        return ""
    text = text[:max_chars]
    CHUNK = 4000   # حد آمن لـ Google Translate
    if len(text) <= CHUNK:
        return translate_chunk(text)
    # قسّم على فقرات أولاً
    parts = []
    current = ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > CHUNK:
            if current:
                parts.append(current)
            current = para
        else:
            current = (current + "\n" + para).strip()
    if current:
        parts.append(current)
    translated = []
    for p in parts:
        translated.append(translate_chunk(p))
        time.sleep(0.8)   # تفادي حظر Google
    return "\n".join(translated)

# ─────────────────────────────────────────
# Database
# ─────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            url      TEXT PRIMARY KEY,
            sent_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id  INTEGER PRIMARY KEY,
            title    TEXT,
            added_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title_ar   TEXT,
            source     TEXT,
            url        TEXT,
            sent_at    TEXT
        )
    """)
    conn.commit()
    conn.close()
    logging.info("✅ DB جاهزة")

def is_seen(url):
    try:
        conn = sqlite3.connect(DB_PATH)
        r = conn.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone()
        conn.close()
        return r is not None
    except:
        return False

def mark_seen(url):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO seen(url,sent_at) VALUES(?,?)", (url, now))
        conn.commit()
        conn.close()
    except:
        pass

def save_sent(title_ar, source, url):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO sent_items(title_ar,source,url,sent_at) VALUES(?,?,?,?)",
            (title_ar, source, url, now),
        )
        conn.commit()
        conn.close()
    except:
        pass

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
    except:
        pass

def remove_channel(chat_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
        conn.commit()
        conn.close()
    except:
        pass

# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def clean_html(text):
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def get_entry_text(entry):
    """استخرج أفضل نص متاح من مقال RSS"""
    # محاولة جلب المحتوى الكامل
    content = ""
    if hasattr(entry, "content") and entry.content:
        content = entry.content[0].get("value", "")
    if not content and hasattr(entry, "summary"):
        content = entry.summary
    if not content and hasattr(entry, "description"):
        content = entry.description
    return clean_html(content)

# ─────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(chat_id, text):
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id":                  chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": False,
            },
            timeout=12,
        )
        d = r.json()
        if not d.get("ok"):
            err = d.get("description", "")
            logging.warning(f"TG send [{chat_id}]: {err}")
            if any(w in err for w in ["blocked", "not found", "kicked", "deactivated"]):
                remove_channel(chat_id)
        return d.get("ok", False)
    except Exception as e:
        logging.warning(f"tg_send: {e}")
        return False

def send_to_all(text):
    chats = get_channels()
    if not chats:
        logging.warning("⚠️ لا قنوات")
        return
    for cid in chats:
        tg_send(cid, text)
        time.sleep(0.3)

PART_SIZE = 2000   # الحد الأقصى لكل جزء

def split_message(text, limit=PART_SIZE):
    """يقسّم النص إلى أجزاء بحجم limit مع الحفاظ على الكلمات"""
    parts = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts

def fmt_messages(title_ar, summary_ar, emoji, source, pub_date):
    """يبني قائمة رسائل Telegram (بدون روابط، مع تقسيم الملخص الطويل)"""
    header = f"{emoji} <b>{title_ar}</b>\n\n"
    footer = f"\n\n📚 <b>المصدر:</b> {source}\n📅 {pub_date}"

    if not summary_ar:
        return [header.strip() + footer]

    # أول جزء = header + بداية الملخص
    body_parts = split_message(summary_ar)
    msgs = []
    for i, part in enumerate(body_parts):
        if i == 0:
            chunk = header + part
        else:
            chunk = f"📖 <b>تابع — {title_ar}</b>\n\n" + part
        if i == len(body_parts) - 1:
            chunk += footer
        msgs.append(chunk)
    return msgs

# ─────────────────────────────────────────
# Telegram Polling
# ─────────────────────────────────────────
_tg_offset = 0

def tg_poll():
    global _tg_offset
    logging.info("🤖 Polling بدأ")
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": _tg_offset, "timeout": 25,
                        "allowed_updates": '["message","my_chat_member"]'},
                timeout=35,
            )
            if not r.ok:
                time.sleep(5)
                continue
            for u in r.json().get("result", []):
                _tg_offset = u["update_id"] + 1
                _handle(u)
        except Exception as e:
            logging.warning(f"poll: {e}")
            time.sleep(5)

def _handle(u):
    if "my_chat_member" in u:
        mc     = u["my_chat_member"]
        chat   = mc["chat"]
        status = mc["new_chat_member"]["status"]
        cid    = chat["id"]
        title  = chat.get("title") or chat.get("username") or str(cid)
        if status in ("member", "administrator"):
            add_channel(cid, title)
            tg_send(cid,
                "📚 <b>مرحباً! بوت ملخصات الروايات والقصص</b>\n\n"
                "سأرسل لك يومياً:\n"
                "📖 ملخصات روايات جديدة\n"
                "🔍 قصص بوليسية وغموض\n"
                "✨ تحليل الكتب الشهيرة\n"
                "🏛️ أحدث إصدارات الأدب العالمي\n\n"
                "جميع الملخصات مترجمة للعربية 🌐\n"
                "المصادر: Guardian، NPR، LiteraryHub وأكثر"
            )
        elif status in ("left", "kicked"):
            remove_channel(cid)
    if "message" in u:
        msg  = u["message"]
        text = msg.get("text", "")
        chat = msg["chat"]
        if text.startswith("/start"):
            cid   = chat["id"]
            title = chat.get("title") or chat.get("first_name") or str(cid)
            add_channel(cid, title)
            tg_send(cid,
                "📚 <b>أهلاً بك في بوت ملخصات الروايات!</b>\n\n"
                "سأرسل لك ملخصات الروايات والقصص العالمية\n"
                "مترجمة للعربية — مرتين يومياً ☀️🌙\n\n"
                "<b>المواقع المتابَعة:</b>\n"
                "• Four Minute Books 📖\n"
                "• Crime Fiction Lover 🔍\n"
                "• Novel Suspects 📚\n"
                "• Literary Hub 🖊️\n"
                "• The Guardian Books 🏛️\n"
                "• NPR Books 🎙️\n"
                "• Book Riot 📕\n"
                "• Shortform ✨\n"
                "• Book Summary Club 📝\n\n"
                "⏱️ يتحقق كل 6 ساعات"
            )
        elif text.startswith("/now"):
            tg_send(chat["id"], "🔄 جارٍ جلب أحدث الملخصات...")
            threading.Thread(target=story_cycle, daemon=True).start()

# ─────────────────────────────────────────
# دورة الملخصات الرئيسية
# ─────────────────────────────────────────
_cycle_lock = threading.Lock()
_stats      = {"cycles": 0, "sent": 0}

def story_cycle():
    if not _cycle_lock.acquire(blocking=False):
        logging.info("⏭️ دورة جارية بالفعل")
        return
    try:
        logging.info("📚 [Cycle] بدأ")
        headers = {"User-Agent": "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)"}
        sent_count = 0

        for src in SOURCES:
            try:
                r    = requests.get(src["url"], headers=headers, timeout=12)
                feed = feedparser.parse(r.content)

                if not feed.entries:
                    logging.warning(f"⚠️ {src['name']}: لا مقالات")
                    continue

                new_src = 0
                for entry in feed.entries[:5]:   # أحدث 5 مقالات
                    url   = getattr(entry, "link", "").strip()
                    title = clean_html(getattr(entry, "title", ""))
                    if not url or not title:
                        continue
                    if is_seen(url):
                        continue

                    mark_seen(url)   # سجّل فوراً لتجنب التكرار

                    # تاريخ النشر
                    pub = getattr(entry, "published", "") or getattr(entry, "updated", "")
                    try:
                        from email.utils import parsedate_to_datetime
                        dt  = parsedate_to_datetime(pub)
                        pub_ar = dt.astimezone(MECCA_TZ).strftime("%d/%m/%Y %H:%M")
                    except:
                        pub_ar = datetime.now(MECCA_TZ).strftime("%d/%m/%Y")

                    # النص للترجمة — بدون حد للطول
                    body = get_entry_text(entry)

                    logging.info(f"  🌐 ترجمة: {title[:55]}")

                    # ترجمة العنوان
                    title_ar = translate(title, max_chars=200)
                    if not title_ar:
                        title_ar = title   # احتفظ بالأصل إذا فشلت الترجمة

                    # ترجمة الملخص كاملاً
                    summary_ar = ""
                    if body:
                        time.sleep(1)
                        summary_ar = translate(body)

                    # صياغة الرسائل (مقسّمة، بدون روابط)
                    msgs = fmt_messages(
                        title_ar   = title_ar,
                        summary_ar = summary_ar,
                        emoji      = src["emoji"],
                        source     = src["name"],
                        pub_date   = pub_ar,
                    )
                    for msg in msgs:
                        send_to_all(msg)
                        time.sleep(1)
                    save_sent(title_ar, src["name"], url)

                    new_src    += 1
                    sent_count += 1
                    _stats["sent"] += 1
                    logging.info(f"  ✅ أُرسل: {title_ar[:55]}")
                    time.sleep(3)   # استراحة بين المقالات

                if new_src:
                    logging.info(f"  📚 {src['name']}: {new_src} أُرسل")

            except Exception as e:
                logging.warning(f"❌ {src['name']}: {e}")

        _stats["cycles"] += 1
        logging.info(f"🏁 [Cycle] أُرسل {sent_count} ملخص")

    finally:
        _cycle_lock.release()

# ─────────────────────────────────────────
# Scheduler — مرتين يومياً
# ─────────────────────────────────────────
def scheduler():
    logging.info(f"⏱️ Scheduler: كل {CHECK_EVERY//3600} ساعات")
    while True:
        try:
            story_cycle()
        except Exception as e:
            logging.error(f"Scheduler: {e}")
        time.sleep(CHECK_EVERY)

# ─────────────────────────────────────────
# Self-Ping — يمنع Render من النوم
# ─────────────────────────────────────────
def self_ping():
    if not RENDER_URL:
        return
    logging.info(f"🏓 Self-ping: {RENDER_URL}")
    while True:
        time.sleep(4 * 60)
        try:
            requests.get(f"{RENDER_URL}/health", timeout=8)
        except:
            pass

# ─────────────────────────────────────────
# Flask
# ─────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    try:
        conn   = sqlite3.connect(DB_PATH)
        total  = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        sent   = conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
        chats  = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        conn.close()
    except:
        total = sent = chats = 0
    return (
        f"📚 Story Bot v7.1 | "
        f"قنوات: {chats} | "
        f"أُرسل: {sent} ملخص | "
        f"معالج: {total}"
    )

@app.route("/health")
def health():
    return "OK", 200

@app.route("/trigger")
def trigger():
    threading.Thread(target=story_cycle, daemon=True).start()
    return "🚀 جارٍ جلب الملخصات...", 200

@app.route("/stats")
def stats():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        sent  = conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        last  = conn.execute(
            "SELECT title_ar, source, sent_at, url FROM sent_items ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        return json.dumps({
            "version":  "7.1",
            "channels": chats,
            "sent":     sent,
            "seen":     total,
            "cycles":   _stats["cycles"],
            "last_10":  [{"title": r[0], "source": r[1], "at": r[2], "url": r[3]} for r in last],
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
def add_manual(chat_id):
    add_channel(chat_id, f"manual-{chat_id}")
    return json.dumps({"ok": True, "chat_id": chat_id}), 200, {"Content-Type": "application/json"}

@app.route("/reset")
def reset():
    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        conn.execute("DELETE FROM seen")
        conn.execute("DELETE FROM sent_items")
        conn.commit()
        conn.close()
        return json.dumps({"ok": True, "deleted": c}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route("/sources")
def list_sources():
    return json.dumps(
        [{"name": s["name"], "desc": s["desc"], "url": s["url"]} for s in SOURCES],
        ensure_ascii=False, indent=2
    ), 200, {"Content-Type": "application/json"}

# ─────────────────────────────────────────
# Startup
# ─────────────────────────────────────────
def _startup():
    time.sleep(3)
    init_db()
    if DEFAULT_CHAT:
        add_channel(DEFAULT_CHAT, "default")
        logging.info(f"📌 Default chat: {DEFAULT_CHAT}")
    threading.Thread(target=tg_poll,    daemon=True, name="poll").start()
    threading.Thread(target=scheduler,  daemon=True, name="sched").start()
    threading.Thread(target=self_ping,  daemon=True, name="ping").start()
    logging.info("🚀 Story Bot v7.1 جاهز")

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
