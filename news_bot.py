#!/usr/bin/env python3
"""
📚 Novel & Story Summaries Bot v9.0
يجلب ملخصات الروايات والقصص والتاريخ ← يفلترها ← يترجم ← فيديو ← يرسل
"""

import os, json, logging, sqlite3, re, time, html as html_lib
import threading, subprocess, tempfile, asyncio
from datetime import datetime, timezone, timedelta

import feedparser, requests
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont
import shutil as _shutil
import edge_tts
from flask import Flask

# ── ffmpeg ──────────────────────────────
_FFMPEG = None

def _get_ffmpeg():
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    try:
        import imageio_ffmpeg as _iio
        _FFMPEG = _iio.get_ffmpeg_exe()
    except Exception:
        _FFMPEG = _shutil.which("ffmpeg") or "ffmpeg"
    return _FFMPEG

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
CHECK_EVERY  = 6 * 60 * 60
MECCA_TZ     = timezone(timedelta(hours=3))
ARABIC_VOICE = "ar-SA-ZariyahNeural"
FETCH_HEADERS= {"User-Agent": "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)"}

# ─────────────────────────────────────────
# ① مصادر القصص والروايات والتاريخ
# ─────────────────────────────────────────
SOURCES = [
    # ── روايات وقصص ──────────────────────
    {"name": "Crime Fiction Lover",   "url": "https://crimefictionlover.com/feed/",          "emoji": "🔍", "cat": "novel"},
    {"name": "Historical Novel Soc.", "url": "https://historicalnovelsociety.org/feed/",      "emoji": "📜", "cat": "history"},
    {"name": "The Marginalian",       "url": "https://www.themarginalian.org/feed/",          "emoji": "✨", "cat": "novel"},
    {"name": "Literary Hub",          "url": "https://lithub.com/feed/",                      "emoji": "🖊️", "cat": "novel"},
    {"name": "Open Culture Books",    "url": "https://www.openculture.com/category/books/feed","emoji": "📖", "cat": "novel"},
    {"name": "Book Riot",             "url": "https://bookriot.com/feed/",                    "emoji": "📕", "cat": "novel"},
    {"name": "Novel Suspects",        "url": "https://novelsuspects.com/feed/",               "emoji": "📚", "cat": "novel"},
    {"name": "The Guardian Books",    "url": "https://www.theguardian.com/books/rss",         "emoji": "🏛️", "cat": "novel"},
    # ── تاريخ ────────────────────────────
    {"name": "History Extra",         "url": "https://www.historyextra.com/feed/",            "emoji": "⚔️", "cat": "history"},
    {"name": "JSTOR Daily",           "url": "https://daily.jstor.org/feed/",                 "emoji": "🗺️", "cat": "history"},
    {"name": "NPR Books",             "url": "https://feeds.npr.org/1032/rss.xml",            "emoji": "🎙️", "cat": "novel"},
    {"name": "Shortform Blog",        "url": "https://www.shortform.com/blog/feed/",          "emoji": "📝", "cat": "novel"},
]

# ─────────────────────────────────────────
# ② فلتر القصص / الروايات / التاريخ فقط
# ─────────────────────────────────────────

# كلمات تدل على المحتوى المرغوب
ACCEPT_KW = [
    # روايات وقصص وأدب
    "novel","fiction","story","tale","narrative","plot","chapter","protagonist",
    "character","author","series","thriller","mystery","adventure","fantasy",
    "literary","novella","memoir","biography","autobiography","saga","epic",
    "romance","detective","crime","fairy tale","folklore","legend","myth",
    "fable","classic","literature","book","reads","readers","reading","book club",
    "award","bestseller","review","published","genre","short story","prose","poem",
    # تاريخ وحضارات
    "history","historical","ancient","medieval","empire","civilization","war",
    "revolution","dynasty","century","era","kingdom","battle","historian",
    "chronicle","heritage","antiquity","colonial","roman","greek","ottoman",
    "egypt","renaissance","napoleon","world war","civil war","pharaoh","sultan",
    "king","queen","conquest","expedition","archaeology","artifact","ruins",
    "medieval","byzantine","mongol","viking","crusade","pirate","explorer",
]

# كلمات تدل على محتوى يجب رفضه
REJECT_KW = [
    "software","coding","programming","api ","blockchain","cryptocurrency",
    "stock market","investment portfolio","quarterly earnings","revenue growth",
    "fitness routine","workout plan","diet plan","weight loss",
    "how to start a business","startup funding","venture capital",
    "machine learning","artificial intelligence","data science",
]

def is_relevant(title: str, body: str = "") -> bool:
    """يتحقق إذا كان المحتوى ضمن فئة الروايات/القصص/التاريخ"""
    combined = (title + " " + body[:400]).lower()
    has_accept = any(kw in combined for kw in ACCEPT_KW)
    has_reject = any(kw in combined for kw in REJECT_KW)
    return has_accept and not has_reject

# ─────────────────────────────────────────
# خط عربي
# ─────────────────────────────────────────
def download_font():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 100_000:
        return
    logging.info("⬇️ تحميل خط Cairo...")
    try:
        r = requests.get(FONT_URL, timeout=30)
        with open(FONT_PATH, "wb") as f:
            f.write(r.content)
        logging.info(f"✅ الخط جاهز ({os.path.getsize(FONT_PATH)//1024}KB)")
    except Exception as e:
        logging.warning(f"font download: {e}")

def ar(text: str) -> str:
    try:
        return get_display(arabic_reshaper.reshape(str(text)))
    except:
        return str(text)

# ─────────────────────────────────────────
# صورة الغلاف 1280×720
# ─────────────────────────────────────────
def make_thumbnail(title_ar: str, source: str, emoji: str, cat: str, output_path: str):
    """ينشئ صورة غلاف 1280×720 بتصميم مختلف حسب الفئة"""
    W, H = 1280, 720
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    # لون الخلفية حسب الفئة
    if cat == "history":
        # بني-ذهبي (تاريخ)
        bg_top, bg_bot = (35, 20, 8), (55, 35, 12)
        accent         = (212, 160, 40)
        header_bg      = (45, 25, 8)
    else:
        # بنفسجي-أزرق (روايات)
        bg_top, bg_bot = (12, 8, 38), (20, 16, 68)
        accent         = (212, 175, 55)
        header_bg      = (20, 10, 50)

    for y in range(H):
        t   = y / H
        r_c = int(bg_top[0] + t * (bg_bot[0] - bg_top[0]))
        g_c = int(bg_top[1] + t * (bg_bot[1] - bg_top[1]))
        b_c = int(bg_top[2] + t * (bg_bot[2] - bg_top[2]))
        draw.line([(0, y), (W, y)], fill=(r_c, g_c, b_c))

    # إطار مزدوج
    for pad, w in [(10, 4), (20, 1)]:
        draw.rectangle([pad, pad, W-pad, H-pad], outline=accent, width=w)

    # شريط علوي وسفلي
    draw.rectangle([0, 0, W, 115], fill=header_bg)
    draw.line([(60, 114), (W-60, 114)], fill=accent, width=2)
    draw.rectangle([0, H-110, W, H], fill=header_bg)
    draw.line([(60, H-110), (W-60, H-110)], fill=accent, width=2)

    # نجوم زخرفية
    for x in [75, 165, W-75, W-165]:
        draw.regular_polygon((x, 58, 9), 6, fill=accent)

    # خطوط وسطية
    for dy in [-6, 6]:
        draw.line([(80, H//2 + dy), (W-80, H//2 + dy)], fill=(70, 55, 110), width=1)

    # خطوط
    try:
        fnt_hdr = ImageFont.truetype(FONT_PATH, 29)
        fnt_big = ImageFont.truetype(FONT_PATH, 80)
        fnt_med = ImageFont.truetype(FONT_PATH, 63)
        fnt_src = ImageFont.truetype(FONT_PATH, 35)
    except:
        fnt_hdr = fnt_big = fnt_med = fnt_src = ImageFont.load_default()

    # نص الهيدر
    header_text = "✦  تاريخ وحضارات  ✦" if cat == "history" else "✦  ملخصات الروايات والقصص  ✦"
    draw.text((W//2, 58), ar(header_text), font=fnt_hdr, fill=accent, anchor="mm")

    # العنوان — أول 4 كلمات، ثم الباقي في سطر ثانٍ
    words = title_ar.split()
    line1 = " ".join(words[:4])
    line2 = " ".join(words[4:8]) if len(words) > 4 else ""

    if line2:
        draw.text((W//2, H//2 - 55), ar(line1), font=fnt_med, fill=(255, 255, 255), anchor="mm")
        draw.text((W//2, H//2 + 40), ar(line2), font=fnt_med, fill=(225, 225, 255), anchor="mm")
    else:
        draw.text((W//2, H//2 - 15), ar(line1), font=fnt_big, fill=(255, 255, 255), anchor="mm")

    # المصدر
    draw.text((W//2, H - 55), ar(f"{emoji}   {source}"),
              font=fnt_src, fill=(180, 180, 230), anchor="mm")

    img.save(output_path, "PNG")
    return output_path

# ─────────────────────────────────────────
# ③ الترجمة — مع retry وتحقق من النتيجة
# ─────────────────────────────────────────
def _is_arabic(text: str) -> bool:
    """يتحقق إذا كان النص يحتوي على عربية كافية"""
    arabic_chars = sum(1 for c in text if "\u0600" <= c <= "\u06ff")
    return arabic_chars / max(len(text), 1) > 0.25

def translate_chunk(text: str, retries: int = 3) -> str:
    """يترجم جزءاً من النص مع إعادة المحاولة عند الفشل"""
    if not text or not text.strip():
        return ""
    text = text[:4000]
    last_err = ""
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": text},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            if r.status_code == 200:
                result = r.json()
                translated = "".join(p[0] for p in result[0] if p[0]).strip()
                # تحقق: إذا أُعيد النص الإنجليزي بدون ترجمة → أعد المحاولة
                if translated and _is_arabic(translated):
                    return translated
                elif translated:
                    # ربما النص قصير جداً أو اسم خاص
                    if len(text) < 40:
                        return translated
                    logging.warning(f"ترجمة غير عربية (محاولة {attempt+1}): {translated[:40]}")
                    time.sleep(2)
        except Exception as e:
            last_err = str(e)
            logging.warning(f"translate attempt {attempt+1}: {e}")
            time.sleep(2 * (attempt + 1))

    logging.error(f"فشل الترجمة نهائياً: {text[:50]} | {last_err}")
    return ""   # ← نُعيد فراغاً لا النص الإنجليزي

def translate(text: str) -> str:
    """يترجم نصاً كاملاً — يُقسّم عند 4000 حرف"""
    if not text or not text.strip():
        return ""
    CHUNK = 3800
    if len(text) <= CHUNK:
        return translate_chunk(text)

    # تقسيم بالفقرات
    parts, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > CHUNK:
            if current.strip():
                parts.append(current.strip())
            current = para
        else:
            current = (current + "\n" + para).strip()
    if current.strip():
        parts.append(current.strip())

    translated_parts = []
    for p in parts:
        t = translate_chunk(p)
        if t:
            translated_parts.append(t)
        time.sleep(1)
    return "\n".join(translated_parts)

# ─────────────────────────────────────────
# TTS — تحويل النص لصوت
# ─────────────────────────────────────────
async def _tts_async(text: str, path: str, voice: str):
    comm = edge_tts.Communicate(text, voice)
    await comm.save(path)

def text_to_audio(text: str, path: str, voice: str = ARABIC_VOICE):
    MAX = 4500
    if len(text) <= MAX:
        asyncio.run(_tts_async(text, path, voice))
        return

    # نص طويل: نقسّم ثم ندمج
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!؟،\n])", text):
        if len(current) + len(sentence) > MAX:
            if current.strip():
                chunks.append(current.strip())
            current = sentence
        else:
            current += sentence
    if current.strip():
        chunks.append(current.strip())

    tmp_dir = tempfile.mkdtemp()
    parts   = []
    for i, chunk in enumerate(chunks):
        p = os.path.join(tmp_dir, f"part_{i}.mp3")
        asyncio.run(_tts_async(chunk, p, voice))
        parts.append(p)
        time.sleep(0.5)

    # دمج
    ffmpeg    = _get_ffmpeg()
    list_file = os.path.join(tmp_dir, "list.txt")
    with open(list_file, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file, "-c", "copy", path],
                   capture_output=True, timeout=120)
    for p in parts:
        try: os.remove(p)
        except: pass
    try: os.rmdir(tmp_dir)
    except: pass

# ─────────────────────────────────────────
# إنشاء الفيديو
# ─────────────────────────────────────────
def make_video(img_path: str, audio_path: str, out_path: str) -> bool:
    ffmpeg = _get_ffmpeg()
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
        logging.error(f"ffmpeg: {result.stderr.decode()[-300:]}")
        return False
    logging.info(f"🎬 {os.path.getsize(out_path)/1024/1024:.1f}MB")
    return True

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
        title_ar TEXT, source TEXT, cat TEXT, url TEXT, sent_at TEXT)""")
    conn.commit(); conn.close()

def is_seen(url):
    try:
        conn = sqlite3.connect(DB_PATH)
        r    = conn.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone()
        conn.close(); return r is not None
    except: return False

def mark_seen(url):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO seen(url,sent_at) VALUES(?,?)", (url, now))
        conn.commit(); conn.close()
    except: pass

def save_sent(title_ar, source, cat, url):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO sent_items(title_ar,source,cat,url,sent_at) VALUES(?,?,?,?,?)",
                     (title_ar, source, cat, url, now))
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
def clean_html(text: str) -> str:
    if not text: return ""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def get_entry_text(entry) -> str:
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
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True},
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
    logging.info(f"📤 {size_mb:.1f}MB → {len(chats)} قناة")
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
                "📚 <b>مرحباً! بوت الروايات والتاريخ</b>\n\n"
                "سأرسل لك فيديوهات مترجمة من:\n"
                "🔍 روايات وقصص\n"
                "⚔️ تاريخ وحضارات\n"
                "📜 قصص تاريخية\n\n"
                "🎬 كل فيديو: صوت عربي + صورة غلاف + 1280×720")
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
                "📚 <b>أهلاً بك في بوت الروايات والتاريخ!</b>\n\n"
                "أرسل لك <b>فيديوهات مترجمة</b> عن:\n"
                "🔍 الروايات والقصص والأدب\n"
                "⚔️ التاريخ والحضارات\n\n"
                "📺 كل فيديو جاهز للنشر على يوتيوب\n"
                "⏱️ يتحقق كل 6 ساعات\n\n"
                "/now — لجلب الفيديوهات الآن")
        elif text.startswith("/now"):
            tg_send(cid, "🔄 جارٍ البحث والإنشاء...")
            threading.Thread(target=story_cycle, daemon=True).start()

# ─────────────────────────────────────────
# الدورة الرئيسية
# ─────────────────────────────────────────
_cycle_lock = threading.Lock()
_stats      = {"cycles": 0, "sent": 0, "filtered": 0}

def process_entry(entry, src: dict) -> bool:
    url   = getattr(entry, "link", "").strip()
    title = clean_html(getattr(entry, "title", ""))
    if not url or not title or is_seen(url):
        return False

    # ── فلتر الفئة ──────────────────────────
    body_raw = get_entry_text(entry)
    if not is_relevant(title, body_raw):
        logging.info(f"  ⛔ خارج الفئة: {title[:55]}")
        mark_seen(url)        # نتجنب إعادة الفحص
        _stats["filtered"] += 1
        return False

    mark_seen(url)

    # تاريخ النشر
    pub = getattr(entry, "published", "") or getattr(entry, "updated", "")
    try:
        from email.utils import parsedate_to_datetime
        pub_ar = parsedate_to_datetime(pub).astimezone(MECCA_TZ).strftime("%d/%m/%Y")
    except:
        pub_ar = datetime.now(MECCA_TZ).strftime("%d/%m/%Y")

    if not body_raw:
        logging.info(f"  ⏭️ بدون نص: {title[:50]}")
        return False

    logging.info(f"  🌐 ترجمة: {title[:55]}")

    # ── الترجمة ────────────────────────────
    title_ar   = translate_chunk(title[:200])
    time.sleep(1)
    summary_ar = translate(body_raw)

    # تحقق: يجب أن تكون الترجمة عربية
    if not title_ar or not _is_arabic(title_ar):
        logging.warning(f"  ❌ ترجمة العنوان فشلت: {title[:50]}")
        return False
    if not summary_ar or not _is_arabic(summary_ar):
        logging.warning(f"  ❌ ترجمة الملخص فشلت: {title[:50]}")
        return False

    logging.info(f"  ✅ مترجم: {title_ar[:55]}")

    # ── إنشاء الفيديو ──────────────────────
    cat    = src.get("cat", "novel")
    tmpdir = tempfile.mkdtemp()
    img_p  = os.path.join(tmpdir, "thumb.png")
    aud_p  = os.path.join(tmpdir, "audio.mp3")
    vid_p  = os.path.join(tmpdir, "video.mp4")

    try:
        # صورة الغلاف
        make_thumbnail(title_ar, src["name"], src["emoji"], cat, img_p)

        # الصوت
        tts_text = f"{title_ar}. {summary_ar}"
        text_to_audio(tts_text, aud_p)
        if not os.path.exists(aud_p) or os.path.getsize(aud_p) < 1000:
            logging.warning(f"  ❌ الصوت فارغ"); return False

        # الفيديو
        if not make_video(img_p, aud_p, vid_p):
            logging.warning(f"  ❌ ffmpeg فشل"); return False

        # الإرسال
        cat_label = "تاريخ وحضارات" if cat == "history" else "روايات وقصص"
        caption   = (
            f"{src['emoji']} <b>{title_ar}</b>\n\n"
            f"📂 {cat_label}  |  📚 {src['name']}\n"
            f"📅 {pub_ar}"
        )
        send_video_to_all(vid_p, caption)
        save_sent(title_ar, src["name"], cat, url)
        _stats["sent"] += 1
        logging.info(f"  🎬 أُرسل: {title_ar[:55]}")
        return True

    except Exception as e:
        logging.error(f"  ❌ process_entry: {e}")
        return False
    finally:
        for f in [img_p, aud_p, vid_p]:
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
                    logging.warning(f"⚠️ {src['name']}: فارغ"); continue
                count = 0
                for entry in feed.entries[:4]:
                    if process_entry(entry, src):
                        count += 1; total += 1
                        time.sleep(5)
                if count:
                    logging.info(f"  📚 {src['name']}: {count} فيديو")
            except Exception as e:
                logging.warning(f"❌ {src['name']}: {e}")
        _stats["cycles"] += 1
        logging.info(f"🏁 [Cycle] أُرسل {total} | مُفلتر {_stats['filtered']}")
    finally:
        _cycle_lock.release()

# ─────────────────────────────────────────
# Scheduler + Self-Ping
# ─────────────────────────────────────────
def scheduler():
    while True:
        try: story_cycle()
        except Exception as e: logging.error(f"Scheduler: {e}")
        time.sleep(CHECK_EVERY)

def self_ping():
    if not RENDER_URL: return
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
    return (f"📚 Story Bot v9.0 | قنوات: {chats} | "
            f"أُرسل: {sent} فيديو | معالج: {total}")

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
            "SELECT title_ar,source,cat,sent_at FROM sent_items ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        return json.dumps({
            "version": "9.0", "channels": chats,
            "sent": sent, "seen": total,
            "filtered": _stats["filtered"], "cycles": _stats["cycles"],
            "last_10": [
                {"title": r[0], "source": r[1], "cat": r[2], "at": r[3]}
                for r in last
            ],
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route("/add/<int:chat_id>")
def add_manual(chat_id):
    add_channel(chat_id, f"manual-{chat_id}")
    return json.dumps({"ok": True, "chat_id": chat_id}), 200, \
           {"Content-Type": "application/json"}

@app.route("/reset")
def reset():
    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        conn.execute("DELETE FROM seen")
        conn.execute("DELETE FROM sent_items")
        conn.commit(); conn.close()
        _stats["filtered"] = 0
        return json.dumps({"ok": True, "deleted": c}), 200, \
               {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route("/sources")
def list_sources():
    return json.dumps(
        [{"name": s["name"], "cat": s["cat"]} for s in SOURCES],
        ensure_ascii=False, indent=2
    ), 200, {"Content-Type": "application/json"}

@app.route("/filter-test")
def filter_test():
    """اختبار الفلتر على محتوى RSS الحالي"""
    results = []
    UA = FETCH_HEADERS
    for src in SOURCES[:4]:
        try:
            r    = requests.get(src["url"], headers=UA, timeout=8)
            feed = feedparser.parse(r.content)
            for e in feed.entries[:3]:
                t   = clean_html(e.get("title",""))
                b   = get_entry_text(e)[:200]
                rel = is_relevant(t, b)
                results.append({"src": src["name"], "title": t[:60],
                                 "pass": rel})
        except: pass
    return json.dumps(results, ensure_ascii=False, indent=2), 200, \
           {"Content-Type": "application/json"}

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
    logging.info("🚀 Story Bot v9.0 جاهز | فلتر: روايات + قصص + تاريخ")

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
