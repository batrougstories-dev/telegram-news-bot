#!/usr/bin/env python3
"""
📚 Novel & Story Summaries Bot v8.0
يجلب ملخصات الروايات ← يترجمها للعربية ← يحوّلها لفيديو ← يرسلها
"""

import os, json, logging, sqlite3, re, time, html as html_lib
import threading, subprocess, tempfile, asyncio
from datetime import datetime, timezone, timedelta

import feedparser, requests
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont
import shutil
_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
import edge_tts
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
FONT_PATH    = "/tmp/arabic_font.ttf"
FONT_URL     = "https://github.com/google/fonts/raw/main/ofl/cairo/Cairo%5Bslnt%2Cwght%5D.ttf"
CHECK_EVERY  = 6 * 60 * 60          # كل 6 ساعات
MECCA_TZ     = timezone(timedelta(hours=3))
ARABIC_VOICE = "ar-SA-ZariyahNeural"   # أفضل صوت عربي سعودي نسائي

# ─────────────────────────────────────────
# مصادر ملخصات الروايات والقصص
# ─────────────────────────────────────────
SOURCES = [
    {"name": "Four Minute Books",   "url": "https://fourminutebooks.com/feed/",               "emoji": "📖"},
    {"name": "Crime Fiction Lover", "url": "https://crimefictionlover.com/feed/",              "emoji": "🔍"},
    {"name": "Novel Suspects",      "url": "https://novelsuspects.com/feed/",                  "emoji": "📚"},
    {"name": "Book Summary Club",   "url": "https://booksummaryclub.com/feed",                 "emoji": "📝"},
    {"name": "Shortform",           "url": "https://www.shortform.com/blog/feed/",             "emoji": "✨"},
    {"name": "Literary Hub",        "url": "https://lithub.com/feed/",                         "emoji": "🖊️"},
    {"name": "Book Riot",           "url": "https://bookriot.com/feed/",                       "emoji": "📕"},
    {"name": "The Guardian Books",  "url": "https://www.theguardian.com/books/rss",            "emoji": "🏛️"},
    {"name": "NPR Books",           "url": "https://feeds.npr.org/1032/rss.xml",               "emoji": "🎙️"},
]

FETCH_HEADERS = {"User-Agent": "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)"}

# ─────────────────────────────────────────
# خط عربي
# ─────────────────────────────────────────
def download_font():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 100_000:
        return
    logging.info("⬇️ تحميل خط Cairo العربي...")
    try:
        r = requests.get(FONT_URL, timeout=30)
        with open(FONT_PATH, "wb") as f:
            f.write(r.content)
        logging.info(f"✅ الخط جاهز ({os.path.getsize(FONT_PATH)//1024}KB)")
    except Exception as e:
        logging.warning(f"font download: {e}")

def ar(text):
    """إعادة تشكيل النص العربي لـ Pillow (RTL + reshaping)"""
    try:
        return get_display(arabic_reshaper.reshape(str(text)))
    except:
        return str(text)

# ─────────────────────────────────────────
# صورة الغلاف (Thumbnail)
# ─────────────────────────────────────────
def make_thumbnail(title_ar, source, emoji, output_path):
    """ينشئ صورة غلاف 1280×720 جاهزة لليوتيوب"""
    W, H = 1280, 720
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    # خلفية: تدرّج بنفسجي-أزرق داكن
    for y in range(H):
        t   = y / H
        r_c = int(12 + t * 8)
        g_c = int(8  + t * 12)
        b_c = int(38 + t * 30)
        draw.line([(0, y), (W, y)], fill=(r_c, g_c, b_c))

    # إطار ذهبي مزدوج
    for pad, w in [(10, 4), (20, 1)]:
        draw.rectangle([pad, pad, W-pad, H-pad], outline=(212, 175, 55), width=w)

    # شريط علوي
    draw.rectangle([0, 0, W, 115], fill=(20, 10, 50))
    draw.line([(60, 114), (W-60, 114)], fill=(212, 175, 55), width=2)

    # شريط سفلي
    draw.rectangle([0, H-110, W, H], fill=(20, 10, 50))
    draw.line([(60, H-110), (W-60, H-110)], fill=(212, 175, 55), width=2)

    # زخرفة نجوم
    for x in [80, 170, W-80, W-170]:
        draw.regular_polygon((x, 58, 9), 6, fill=(212, 175, 55))

    # خطوط وسطية خفيفة
    for dy in [-6, 6]:
        draw.line([(80, H//2 + dy), (W-80, H//2 + dy)], fill=(70, 55, 110), width=1)

    # تحميل الخطوط
    try:
        fnt_hdr  = ImageFont.truetype(FONT_PATH, 29)
        fnt_big  = ImageFont.truetype(FONT_PATH, 82)
        fnt_med  = ImageFont.truetype(FONT_PATH, 65)
        fnt_src  = ImageFont.truetype(FONT_PATH, 36)
    except:
        fnt_hdr = fnt_big = fnt_med = fnt_src = ImageFont.load_default()

    # نص الهيدر
    draw.text((W//2, 58), ar("✦  ملخصات الروايات العالمية  ✦"),
              font=fnt_hdr, fill=(212, 175, 55), anchor="mm")

    # العنوان — أول 4 كلمات في سطر، الباقي في سطر ثانٍ
    words = title_ar.split()
    line1 = " ".join(words[:4])
    line2 = " ".join(words[4:8]) if len(words) > 4 else ""

    if line2:
        draw.text((W//2, H//2 - 55), ar(line1), font=fnt_med, fill=(255, 255, 255), anchor="mm")
        draw.text((W//2, H//2 + 40), ar(line2), font=fnt_med, fill=(220, 220, 255), anchor="mm")
    else:
        draw.text((W//2, H//2 - 15), ar(line1), font=fnt_big, fill=(255, 255, 255), anchor="mm")

    # اسم المصدر
    draw.text((W//2, H - 55), ar(f"{emoji}   {source}"),
              font=fnt_src, fill=(180, 180, 230), anchor="mm")

    img.save(output_path, "PNG")
    return output_path

# ─────────────────────────────────────────
# تحويل النص لصوت (TTS)
# ─────────────────────────────────────────
async def _tts_async(text, path, voice):
    comm = edge_tts.Communicate(text, voice)
    await comm.save(path)

def text_to_audio(text, path, voice=ARABIC_VOICE):
    """يحوّل النص العربي لملف صوتي MP3"""
    # edge-tts لها حد ~5000 حرف لكل طلب — نقسّم إن احتجنا
    MAX = 4500
    if len(text) <= MAX:
        asyncio.run(_tts_async(text, path, voice))
        return
    # نص طويل: نقسّم ونجمع
    parts_paths = []
    chunks = []
    current = ""
    for sentence in re.split(r'(?<=[.!؟،\n])', text):
        if len(current) + len(sentence) > MAX:
            if current:
                chunks.append(current.strip())
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current.strip())

    tmp_dir = tempfile.mkdtemp()
    for i, chunk in enumerate(chunks):
        p = os.path.join(tmp_dir, f"part_{i}.mp3")
        asyncio.run(_tts_async(chunk, p, voice))
        parts_paths.append(p)
        time.sleep(0.5)

    # دمج الأجزاء بـ ffmpeg
    ffmpeg = _FFMPEG
    list_file = os.path.join(tmp_dir, "list.txt")
    with open(list_file, "w") as f:
        for p in parts_paths:
            f.write(f"file '{p}'\n")
    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file, "-c", "copy", path],
                   capture_output=True, timeout=120)
    # تنظيف
    for p in parts_paths:
        try: os.remove(p)
        except: pass
    try: os.rmdir(tmp_dir)
    except: pass

# ─────────────────────────────────────────
# إنشاء الفيديو
# ─────────────────────────────────────────
def make_video(img_path, audio_path, out_path):
    """يدمج الصورة مع الصوت في فيديو MP4 جاهز لليوتيوب"""
    ffmpeg = _FFMPEG
    cmd = [
        ffmpeg, "-y",
        "-loop", "1", "-i", img_path,
        "-i", audio_path,
        "-c:v", "libx264", "-tune", "stillimage", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p", "-shortest",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
               "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black",
        "-movflags", "+faststart",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    if result.returncode != 0:
        logging.error(f"ffmpeg error: {result.stderr.decode()[-400:]}")
        return False
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    logging.info(f"🎬 فيديو جاهز: {size_mb:.1f}MB")
    return True

# ─────────────────────────────────────────
# ترجمة النصوص
# ─────────────────────────────────────────
def translate_chunk(text):
    if not text or not text.strip():
        return ""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": text},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        return "".join(p[0] for p in r.json()[0] if p[0]).strip()
    except Exception as e:
        logging.warning(f"translate: {e}")
        return text

def translate(text):
    """يترجم نصاً كاملاً مهما كان طوله"""
    if not text or not text.strip():
        return ""
    CHUNK = 4000
    if len(text) <= CHUNK:
        return translate_chunk(text)
    parts, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > CHUNK:
            if current: parts.append(current)
            current = para
        else:
            current = (current + "\n" + para).strip()
    if current: parts.append(current)
    translated = []
    for p in parts:
        translated.append(translate_chunk(p))
        time.sleep(0.8)
    return "\n".join(translated)

# ─────────────────────────────────────────
# قاعدة البيانات
# ─────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, sent_at TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS channels (
        chat_id INTEGER PRIMARY KEY, title TEXT, added_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title_ar TEXT, source TEXT, url TEXT, sent_at TEXT)""")
    conn.commit(); conn.close()
    logging.info("✅ DB جاهزة")

def is_seen(url):
    try:
        conn = sqlite3.connect(DB_PATH)
        r = conn.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone()
        conn.close(); return r is not None
    except: return False

def mark_seen(url):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO seen(url,sent_at) VALUES(?,?)", (url, now))
        conn.commit(); conn.close()
    except: pass

def save_sent(title_ar, source, url):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO sent_items(title_ar,source,url,sent_at) VALUES(?,?,?,?)",
                     (title_ar, source, url, now))
        conn.commit(); conn.close()
    except: pass

def get_channels():
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT chat_id FROM channels").fetchall()
        conn.close(); return [r[0] for r in rows]
    except: return []

def add_channel(chat_id, title=""):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO channels(chat_id,title,added_at) VALUES(?,?,?)",
                     (chat_id, title, now))
        conn.commit(); conn.close()
        logging.info(f"➕ قناة: {title} ({chat_id})")
    except: pass

def remove_channel(chat_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
        conn.commit(); conn.close()
    except: pass

# ─────────────────────────────────────────
# مساعدات
# ─────────────────────────────────────────
def clean_html(text):
    if not text: return ""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def get_entry_text(entry):
    content = ""
    if hasattr(entry, "content") and entry.content:
        content = entry.content[0].get("value", "")
    if not content and hasattr(entry, "summary"):
        content = entry.summary
    return clean_html(content)

# ─────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(chat_id, text):
    try:
        r = requests.post(f"{TG_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text,
                                "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=12)
        d = r.json()
        if not d.get("ok"):
            err = d.get("description", "")
            logging.warning(f"TG [{chat_id}]: {err}")
            if any(w in err for w in ["blocked","not found","kicked","deactivated"]):
                remove_channel(chat_id)
        return d.get("ok", False)
    except Exception as e:
        logging.warning(f"tg_send: {e}"); return False

def tg_send_video(chat_id, video_path, caption):
    """يرسل فيديو MP4 للمستخدم"""
    try:
        with open(video_path, "rb") as vf:
            r = requests.post(
                f"{TG_API}/sendVideo",
                data={"chat_id": chat_id, "caption": caption,
                      "parse_mode": "HTML", "supports_streaming": "true"},
                files={"video": ("video.mp4", vf, "video/mp4")},
                timeout=180,
            )
        d = r.json()
        if not d.get("ok"):
            err = d.get("description", "")
            logging.warning(f"sendVideo [{chat_id}]: {err}")
            if any(w in err for w in ["blocked","not found","kicked","deactivated"]):
                remove_channel(chat_id)
        return d.get("ok", False)
    except Exception as e:
        logging.warning(f"tg_send_video: {e}"); return False

def send_to_all(text):
    for cid in get_channels():
        tg_send(cid, text); time.sleep(0.3)

def send_video_to_all(video_path, caption):
    chats = get_channels()
    if not chats:
        logging.warning("⚠️ لا قنوات"); return
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    logging.info(f"📤 إرسال فيديو {size_mb:.1f}MB → {len(chats)} قناة")
    for cid in chats:
        tg_send_video(cid, video_path, caption)
        time.sleep(2)

# ─────────────────────────────────────────
# Telegram Polling
# ─────────────────────────────────────────
_tg_offset = 0

def tg_poll():
    global _tg_offset
    logging.info("🤖 Polling بدأ")
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates",
                             params={"offset": _tg_offset, "timeout": 25,
                                     "allowed_updates": '["message","my_chat_member"]'},
                             timeout=35)
            if not r.ok:
                time.sleep(5); continue
            for u in r.json().get("result", []):
                _tg_offset = u["update_id"] + 1
                _handle(u)
        except Exception as e:
            logging.warning(f"poll: {e}"); time.sleep(5)

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
                "📚 <b>مرحباً! بوت ملخصات الروايات العالمية</b>\n\n"
                "أرسل لك يومياً فيديوهات مترجمة:\n"
                "🎬 ملخص الرواية صوتاً وصورةً\n"
                "🌐 مترجم للعربية بالكامل\n"
                "📺 جاهز للنشر على يوتيوب\n\n"
                "المصادر: Guardian • NPR • LiteraryHub وأكثر")
        elif status in ("left", "kicked"):
            remove_channel(cid)

    if "message" in u:
        msg  = u["message"]
        text = msg.get("text", "")
        chat = msg["chat"]
        cid  = chat["id"]
        if text.startswith("/start"):
            title = chat.get("title") or chat.get("first_name") or str(cid)
            add_channel(cid, title)
            tg_send(cid,
                "📚 <b>أهلاً بك في بوت ملخصات الروايات!</b>\n\n"
                "سأرسل لك <b>فيديوهات</b> ملخصات روايات مترجمة 🎬\n\n"
                "كل فيديو يحتوي:\n"
                "🖼️ صورة غلاف احترافية مع العنوان\n"
                "🎙️ صوت عربي عالي الجودة\n"
                "📺 1280×720 جاهز لليوتيوب\n\n"
                "⏱️ يتحقق كل 6 ساعات\n\n"
                "/now — لجلب الملخصات الآن")
        elif text.startswith("/now"):
            tg_send(cid, "🔄 جارٍ إنشاء الفيديوهات...")
            threading.Thread(target=story_cycle, daemon=True).start()

# ─────────────────────────────────────────
# الدورة الرئيسية
# ─────────────────────────────────────────
_cycle_lock = threading.Lock()
_stats      = {"cycles": 0, "sent": 0}

def process_entry(entry, src):
    """يعالج مقالاً واحداً ← فيديو ← إرسال"""
    url   = getattr(entry, "link", "").strip()
    title = clean_html(getattr(entry, "title", ""))
    if not url or not title or is_seen(url):
        return False

    mark_seen(url)   # تسجيل فوري قبل أي معالجة

    # تاريخ النشر
    pub = getattr(entry, "published", "") or getattr(entry, "updated", "")
    try:
        from email.utils import parsedate_to_datetime
        pub_ar = parsedate_to_datetime(pub).astimezone(MECCA_TZ).strftime("%d/%m/%Y")
    except:
        pub_ar = datetime.now(MECCA_TZ).strftime("%d/%m/%Y")

    # نص الملخص
    body = get_entry_text(entry)
    if not body:
        logging.info(f"  ⏭️ بدون نص: {title[:50]}")
        return False

    logging.info(f"  🌐 ترجمة: {title[:55]}")

    # ترجمة
    title_ar = translate_chunk(title[:200]) or title
    time.sleep(1)
    summary_ar = translate(body)

    if not summary_ar:
        return False

    # نص الفيديو الكامل = العنوان + الملخص
    tts_text = f"{title_ar}. {summary_ar}"

    logging.info(f"  🎬 إنشاء فيديو: {title_ar[:55]}")
    tmpdir = tempfile.mkdtemp()

    try:
        img_path   = os.path.join(tmpdir, "thumb.png")
        audio_path = os.path.join(tmpdir, "audio.mp3")
        video_path = os.path.join(tmpdir, "video.mp4")

        # 1️⃣ صورة الغلاف
        make_thumbnail(title_ar, src["name"], src["emoji"], img_path)

        # 2️⃣ الصوت
        text_to_audio(tts_text, audio_path)
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
            logging.warning(f"  ❌ الصوت فارغ: {title_ar[:40]}")
            return False

        # 3️⃣ الفيديو
        if not make_video(img_path, audio_path, video_path):
            logging.warning(f"  ❌ فشل ffmpeg: {title_ar[:40]}")
            return False

        # 4️⃣ الإرسال
        caption = (
            f"{src['emoji']} <b>{title_ar}</b>\n\n"
            f"📚 {src['name']}  |  📅 {pub_ar}"
        )
        send_video_to_all(video_path, caption)
        save_sent(title_ar, src["name"], url)
        _stats["sent"] += 1
        logging.info(f"  ✅ أُرسل: {title_ar[:55]}")
        return True

    except Exception as e:
        logging.error(f"  ❌ process_entry: {e}")
        return False

    finally:
        # تنظيف الملفات المؤقتة
        for f in [img_path, audio_path, video_path]:
            try: os.remove(f)
            except: pass
        try: os.rmdir(tmpdir)
        except: pass

def story_cycle():
    if not _cycle_lock.acquire(blocking=False):
        logging.info("⏭️ دورة جارية"); return
    try:
        logging.info("📚 [Cycle] بدأ")
        total = 0
        for src in SOURCES:
            try:
                r    = requests.get(src["url"], headers=FETCH_HEADERS, timeout=12)
                feed = feedparser.parse(r.content)
                if not feed.entries:
                    logging.warning(f"⚠️ {src['name']}: لا مقالات"); continue

                new_count = 0
                for entry in feed.entries[:3]:   # أحدث 3 مقالات لكل مصدر
                    if process_entry(entry, src):
                        new_count += 1; total += 1
                        time.sleep(5)    # استراحة بين الفيديوهات

                if new_count:
                    logging.info(f"  📚 {src['name']}: {new_count} فيديو")

            except Exception as e:
                logging.warning(f"❌ {src['name']}: {e}")

        _stats["cycles"] += 1
        logging.info(f"🏁 [Cycle] أُرسل {total} فيديو")
    finally:
        _cycle_lock.release()

# ─────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────
def scheduler():
    logging.info(f"⏱️ Scheduler: كل {CHECK_EVERY//3600} ساعات")
    while True:
        try: story_cycle()
        except Exception as e: logging.error(f"Scheduler: {e}")
        time.sleep(CHECK_EVERY)

# ─────────────────────────────────────────
# Self-Ping
# ─────────────────────────────────────────
def self_ping():
    if not RENDER_URL: return
    logging.info(f"🏓 Self-ping: {RENDER_URL}")
    while True:
        time.sleep(4 * 60)
        try: requests.get(f"{RENDER_URL}/health", timeout=8)
        except: pass

# ─────────────────────────────────────────
# Flask
# ─────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        sent  = conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        conn.close()
    except: total = sent = chats = 0
    return f"📚 Story Bot v8.0 | قنوات: {chats} | أُرسل: {sent} فيديو | معالج: {total}"

@app.route("/health")
def health():
    return "OK", 200

@app.route("/trigger")
def trigger():
    threading.Thread(target=story_cycle, daemon=True).start()
    return "🚀 جارٍ إنشاء الفيديوهات...", 200

@app.route("/stats")
def stats():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        sent  = conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        last  = conn.execute(
            "SELECT title_ar,source,sent_at FROM sent_items ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        return json.dumps({
            "version": "8.0", "channels": chats, "sent": sent,
            "seen": total, "cycles": _stats["cycles"],
            "last_10": [{"title": r[0], "source": r[1], "at": r[2]} for r in last],
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

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
        conn.commit(); conn.close()
        return json.dumps({"ok": True, "deleted": c}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route("/sources")
def list_sources():
    return json.dumps(
        [{"name": s["name"], "url": s["url"]} for s in SOURCES],
        ensure_ascii=False, indent=2
    ), 200, {"Content-Type": "application/json"}

# ─────────────────────────────────────────
# Startup
# ─────────────────────────────────────────
def _startup():
    time.sleep(3)
    download_font()
    init_db()
    if DEFAULT_CHAT:
        add_channel(DEFAULT_CHAT, "default")
    threading.Thread(target=tg_poll,   daemon=True, name="poll").start()
    threading.Thread(target=scheduler, daemon=True, name="sched").start()
    threading.Thread(target=self_ping, daemon=True, name="ping").start()
    logging.info("🚀 Story Bot v8.0 جاهز")

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
