#!/usr/bin/env python3
"""
📚 Book Summary Bot v1.0
=========================
المصدر الوحيد : booksummaryclub.com
- يجلب ملخصات الكتب من RSS + صفحة المقالة الكاملة
- يترجمها كاملةً بـ Llama 3.3 (أدبي احترافي)
- يرسلها لقناة تيليغرام
- يتجنب تكرار المقالات المُرسَلة
- يفحص الموقع كل 6 ساعات تلقائياً
"""

import os, json, logging, sqlite3, re, time, threading
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from bs4 import BeautifulSoup
from flask import Flask

try:
    from openai import OpenAI as _OAI
    _OAI_OK = True
except ImportError:
    _OAI_OK = False

# ─────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ["BOT_TOKEN"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RENDER_URL   = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
DEFAULT_CHAT = int(os.environ.get("DEFAULT_CHAT_ID", "0"))

DB_PATH   = "/tmp/summarybot.db"
MECCA_TZ  = timezone(timedelta(hours=3))
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA        = "Mozilla/5.0 (compatible; SummaryBot/1.0)"

RSS_URL       = "https://www.booksummaryclub.com/feed/"
CHECK_EVERY   = 6 * 3600   # فحص كل 6 ساعات

LLAMA_PRIMARY  = "Meta-Llama-3.3-70B-Instruct"
LLAMA_FALLBACK = "Meta-Llama-3.1-405B-Instruct"
TG_MSG         = 3_800     # حد رسالة تيليغرام

# ─────────────────────────────────────────────────────
# قاعدة البيانات
# ─────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                chat_id  INTEGER PRIMARY KEY,
                title    TEXT,
                added_at TEXT
            );
            CREATE TABLE IF NOT EXISTS seen (
                url      TEXT PRIMARY KEY,
                title    TEXT,
                sent_at  TEXT
            );
        """)

def get_channels():
    try:
        with sqlite3.connect(DB_PATH) as c:
            return [r[0] for r in c.execute("SELECT chat_id FROM channels")]
    except:
        return []

def add_channel(cid, title=""):
    try:
        now = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT OR IGNORE INTO channels(chat_id,title,added_at) VALUES(?,?,?)",
                (cid, title, now),
            )
        logging.info(f"➕ قناة: {title} ({cid})")
    except:
        pass

def remove_channel(cid):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("DELETE FROM channels WHERE chat_id=?", (cid,))
    except:
        pass

def is_seen(url):
    try:
        with sqlite3.connect(DB_PATH) as c:
            return bool(c.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone())
    except:
        return False

def mark_seen(url, title):
    try:
        now = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT OR IGNORE INTO seen(url,title,sent_at) VALUES(?,?,?)",
                (url, title, now),
            )
    except:
        pass

# ─────────────────────────────────────────────────────
# جلب المحتوى من الموقع
# ─────────────────────────────────────────────────────
def get_rss_entries():
    """جلب المقالات الجديدة من RSS"""
    try:
        feed = feedparser.parse(RSS_URL)
        entries = []
        for e in feed.entries:
            url   = e.get("link", "")
            title = e.get("title", "").strip()
            if url and title:
                entries.append({"url": url, "title": title})
        logging.info(f"📡 RSS: {len(entries)} مقالة")
        return entries
    except Exception as ex:
        logging.warning(f"RSS: {ex}")
        return []

def fetch_full_content(url):
    """جلب الملخص الكامل من صفحة المقالة"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # استخراج الصورة الرئيسية
        img_url = ""
        og_img = soup.find("meta", property="og:image")
        if og_img:
            img_url = og_img.get("content", "")

        # استخراج محتوى المقالة
        article = (
            soup.find("div", class_=re.compile(r"entry-content|post-content|article-content"))
            or soup.find("article")
        )
        if not article:
            return None

        # تنظيف العناصر غير المرغوبة
        for tag in article(["script", "style", "nav", "aside", "footer",
                             "form", "iframe", ".sharedaddy", ".jp-relatedposts"]):
            tag.decompose()

        # استخراج النص
        text = article.get_text(separator="\n", strip=True)

        # تنظيف: احذف الأسطر القصيرة جداً والروابط
        lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 40]
        clean = "\n\n".join(lines)

        logging.info(f"  📄 محتوى: {len(clean)} حرف")
        return {"text": clean, "img": img_url}

    except Exception as ex:
        logging.warning(f"fetch [{url}]: {ex}")
        return None

# ─────────────────────────────────────────────────────
# الترجمة
# ─────────────────────────────────────────────────────
_SYS_PROMPT = """أنت مترجم أدبي محترف متخصص في ملخصات الكتب والروايات.

أسلوبك:
- عربية فصحى سلسة وجذابة، ليست حرفية
- احتفظ بحماس النص وأسلوبه الترويجي
- حافظ على أسماء الكتب والمؤلفين كما هي أو عرِّبها صوتياً
- أخرج الترجمة فقط بلا تعليقات"""

def translate(text, prev_ctx=""):
    """يترجم النص كاملاً — Llama أولاً ثم Google كـ fallback"""
    if not text.strip():
        return ""

    # تقسيم إلى أجزاء 4000 حرف مع سياق متصل
    chunks = _split_chunks(text, 4000)
    result = []
    context = prev_ctx

    for i, chunk in enumerate(chunks):
        logging.info(f"  🧠 ترجمة جزء {i+1}/{len(chunks)} ({len(chunk)} حرف)…")
        ar = _translate_chunk(chunk, context)
        if ar:
            result.append(ar)
            context = ar[-400:]   # آخر 400 حرف = سياق للجزء التالي
        time.sleep(1)

    return "\n\n".join(result)

def _split_chunks(text, size):
    """تقسيم النص إلى أجزاء ≤ size حرف على حدود الفقرات"""
    if len(text) <= size:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 <= size:
            cur = (cur + "\n\n" + para).strip() if cur else para
        else:
            if cur:
                chunks.append(cur)
            cur = para if len(para) <= size else para[:size]
    if cur:
        chunks.append(cur)
    return chunks

def _translate_chunk(text, prev_ar=""):
    """ترجمة جزء واحد"""
    user_msg = text
    if prev_ar.strip():
        user_msg = (
            f"[سياق سابق للاستمرارية — لا تُعِد ترجمته]\n{prev_ar}\n\n"
            f"[النص الجديد]\n{text}"
        )

    # Llama
    if GITHUB_TOKEN and _OAI_OK:
        client = _OAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=GITHUB_TOKEN,
        )
        for model in [LLAMA_PRIMARY, LLAMA_FALLBACK]:
            for attempt in range(2):
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": _SYS_PROMPT},
                            {"role": "user",   "content": user_msg},
                        ],
                        max_tokens=2048,
                        temperature=0.3,
                    )
                    ar = resp.choices[0].message.content.strip()
                    if ar and _has_arabic(ar):
                        return ar
                except Exception as ex:
                    logging.warning(f"  Llama [{attempt+1}]: {ex}")
                    time.sleep(3)

    # Google Translate fallback
    return _gtr(text)

def _gtr(text):
    for _ in range(3):
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "en", "tl": "ar",
                        "dt": "t", "q": text[:4800]},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            if r.status_code == 200:
                t = "".join(p[0] for p in r.json()[0] if p[0]).strip()
                if t:
                    return t
        except:
            pass
        time.sleep(2)
    return ""

def _has_arabic(text):
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    return sum(1 for c in alpha if "\u0600" <= c <= "\u06ff") / len(alpha) > 0.25

# ─────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────
def tg_send(cid, text, parse_mode="HTML"):
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": parse_mode,
                  "disable_web_page_preview": True},
            timeout=15,
        )
        d = r.json()
        if not d.get("ok"):
            err = d.get("description", "")
            if any(w in err for w in ["blocked", "not found", "kicked", "deactivated"]):
                remove_channel(cid)
        return d.get("ok", False)
    except:
        return False

def tg_photo(cid, photo_url, caption):
    try:
        r = requests.post(
            f"{TG_API}/sendPhoto",
            json={"chat_id": cid, "photo": photo_url,
                  "caption": caption, "parse_mode": "HTML"},
            timeout=20,
        )
        if not r.json().get("ok"):
            tg_send(cid, caption)
    except:
        tg_send(cid, caption)

def broadcast(text):
    for cid in get_channels():
        tg_send(cid, text)
        time.sleep(0.4)

def broadcast_photo(photo_url, caption):
    for cid in get_channels():
        tg_photo(cid, photo_url, caption)
        time.sleep(0.5)

def split_tg(text, max_len=TG_MSG):
    """تقسيم النص لرسائل تيليغرام"""
    if len(text) <= max_len:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 <= max_len:
            cur = (cur + "\n" + line).strip() if cur else line
        else:
            if cur:
                parts.append(cur)
            cur = line[:max_len]
    if cur:
        parts.append(cur)
    return parts or [text[:max_len]]

# ─────────────────────────────────────────────────────
# معالجة المقالة ونشرها
# ─────────────────────────────────────────────────────
def process_entry(entry):
    """جلب → ترجمة → إرسال مقالة واحدة"""
    url   = entry["url"]
    title = entry["title"]

    if is_seen(url):
        return False

    logging.info(f"📖 معالجة: {title}")

    # جلب المحتوى الكامل
    content = fetch_full_content(url)
    if not content or not content["text"]:
        logging.warning(f"  ❌ لا محتوى")
        return False

    text_en = content["text"]
    img_url = content["img"]

    # ترجمة العنوان
    title_ar = _gtr(title[:200]) or title
    logging.info(f"  📝 العنوان: {title_ar[:60]}")

    # ترجمة المحتوى الكامل
    logging.info(f"  🌐 ترجمة {len(text_en)} حرف…")
    text_ar = translate(text_en)

    if not text_ar or not _has_arabic(text_ar):
        logging.warning(f"  ❌ الترجمة فشلت")
        return False

    logging.info(f"  ✅ الترجمة: {len(text_ar)} حرف عربي")

    # إرسال الصورة + العنوان أولاً
    today = datetime.now(MECCA_TZ).strftime("%d/%m/%Y")
    header = (
        f"📚 <b>{title_ar}</b>\n"
        f"{'─' * 25}\n"
        f"📅 {today}  •  📡 booksummaryclub.com"
    )

    if img_url:
        broadcast_photo(img_url, header)
    else:
        broadcast(header)

    time.sleep(2)

    # إرسال الملخص المترجم (مقسَّم إن احتاج)
    parts = split_tg(text_ar)
    logging.info(f"  📤 إرسال {len(parts)} رسالة…")
    for i, part in enumerate(parts):
        broadcast(part)
        if i < len(parts) - 1:
            time.sleep(3)

    mark_seen(url, title)
    logging.info(f"  ✅ أُرسل بنجاح")
    return True

# ─────────────────────────────────────────────────────
# حلقة الجلب التلقائي
# ─────────────────────────────────────────────────────
_checking = False

def check_and_send():
    global _checking
    if _checking:
        return
    _checking = True
    try:
        entries = get_rss_entries()
        new_count = 0
        for entry in entries:
            if not is_seen(entry["url"]):
                ok = process_entry(entry)
                if ok:
                    new_count += 1
                    time.sleep(5)  # فاصل بين مقالتين
        logging.info(f"✅ دورة فحص: {new_count} ملخص جديد أُرسل")
    except Exception as ex:
        logging.error(f"check_and_send: {ex}")
    finally:
        _checking = False

def scheduler_loop():
    """يفحص الموقع كل CHECK_EVERY ثانية"""
    while True:
        try:
            check_and_send()
        except Exception as ex:
            logging.error(f"scheduler: {ex}")
        time.sleep(CHECK_EVERY)

# ─────────────────────────────────────────────────────
# Telegram Polling
# ─────────────────────────────────────────────────────
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
        except Exception as ex:
            logging.warning(f"poll: {ex}")
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
                "📚 <b>مرحباً في بوت ملخصات الكتب!</b>\n\n"
                "يُرسل لك ملخصات كتب مترجمة للعربية\n"
                "من موقع booksummaryclub.com\n\n"
                "/now — اجلب أحدث الملخصات الآن\n"
                "/status — إحصائيات البوت")
        elif status in ("left", "kicked"):
            remove_channel(cid)

    if "message" not in u:
        return

    msg  = u["message"]
    text = msg.get("text", "").strip()
    cid  = msg["chat"]["id"]
    chat = msg["chat"]

    if text.startswith("/start"):
        title = chat.get("title") or chat.get("first_name") or str(cid)
        add_channel(cid, title)
        tg_send(cid,
            "📚 <b>بوت ملخصات الكتب</b>\n"
            f"{'━' * 22}\n\n"
            "📡 المصدر: booksummaryclub.com\n"
            "🧠 الترجمة: Llama 3.3 (أدبية احترافية)\n"
            "🔄 يفحص تلقائياً كل 6 ساعات\n\n"
            "<b>الأوامر:</b>\n"
            "/now — اجلب الآن\n"
            "/status — الإحصائيات")

    elif text.startswith("/now"):
        title = chat.get("title") or chat.get("first_name") or str(cid)
        add_channel(cid, title)
        tg_send(cid, "🔄 <b>جارٍ جلب أحدث الملخصات…</b>")
        threading.Thread(target=check_and_send, daemon=True).start()

    elif text.startswith("/status"):
        try:
            with sqlite3.connect(DB_PATH) as c:
                total  = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
                chats  = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
                recent = c.execute(
                    "SELECT title, sent_at FROM seen ORDER BY sent_at DESC LIMIT 5"
                ).fetchall()
            lines = "\n".join(f"• {r[0][:45]}  <i>{r[1]}</i>" for r in recent)
            tg_send(cid,
                f"📊 <b>إحصائيات البوت</b>\n{'─' * 22}\n\n"
                f"📚 ملخصات أُرسلت: <b>{total}</b>\n"
                f"📣 قنوات: <b>{chats}</b>\n\n"
                f"<b>آخر 5 ملخصات:</b>\n{lines or 'لا يوجد بعد'}")
        except:
            tg_send(cid, "⚠️ خطأ في قراءة الإحصائيات")

# ─────────────────────────────────────────────────────
# Self-Ping
# ─────────────────────────────────────────────────────
def self_ping():
    if not RENDER_URL:
        return
    while True:
        time.sleep(4 * 60)
        try:
            requests.get(f"{RENDER_URL}/health", timeout=8)
        except:
            pass

# ─────────────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    try:
        with sqlite3.connect(DB_PATH) as c:
            total = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            chats = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        return f"📚 Summary Bot v1.0 | قنوات: {chats} | أُرسل: {total} ملخص"
    except:
        return "📚 Summary Bot v1.0"

@app.route("/health")
def health():
    return "OK", 200

@app.route("/add/<int:cid>")
def add_ep(cid):
    add_channel(cid, f"manual-{cid}")
    return json.dumps({"ok": True, "chat_id": cid}), 200, {
        "Content-Type": "application/json"
    }

@app.route("/now")
def now_ep():
    threading.Thread(target=check_and_send, daemon=True).start()
    return "🔄 جارٍ الجلب…", 200

@app.route("/reset")
def reset_ep():
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("DELETE FROM seen")
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}
    except Exception as ex:
        return json.dumps({"error": str(ex)}), 500

@app.route("/status")
def status_ep():
    try:
        with sqlite3.connect(DB_PATH) as c:
            total  = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            chats  = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
            recent = c.execute(
                "SELECT title, sent_at FROM seen ORDER BY sent_at DESC LIMIT 10"
            ).fetchall()
        return json.dumps({
            "version": "1.0", "channels": chats, "sent": total,
            "recent": [{"title": r[0], "sent_at": r[1]} for r in recent],
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as ex:
        return json.dumps({"error": str(ex)}), 500

# ─────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────
def _startup():
    time.sleep(3)
    init_db()
    if DEFAULT_CHAT:
        add_channel(DEFAULT_CHAT, "default")
    threading.Thread(target=tg_poll,       daemon=True, name="poll").start()
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    threading.Thread(target=self_ping,     daemon=True, name="ping").start()
    engine = f"Llama {LLAMA_PRIMARY[:20]}" if (GITHUB_TOKEN and _OAI_OK) else "Google Translate"
    logging.info(
        f"🚀 Summary Bot v1.0 | {engine} | "
        f"فحص كل {CHECK_EVERY//3600}h | booksummaryclub.com"
    )

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
