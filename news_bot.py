#!/usr/bin/env python3
"""
📚 Novel & Story Bot v10.0
مصادر: Standard Ebooks + Gutendex (Project Gutenberg) + Open Library
الترتيب: نص كامل مترجم → فيديو صوتي
"""

import os, json, logging, sqlite3, re, time, html as html_lib
import threading, subprocess, tempfile, asyncio, random
from datetime import datetime, timezone, timedelta

import feedparser, requests
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont
import shutil as _shutil
import edge_tts
from flask import Flask

# ── ffmpeg ──────────────────────────────────────────
_FFMPEG = None
def _get_ffmpeg():
    global _FFMPEG
    if _FFMPEG: return _FFMPEG
    try:
        import imageio_ffmpeg as _iio
        _FFMPEG = _iio.get_ffmpeg_exe()
    except Exception:
        _FFMPEG = _shutil.which("ffmpeg") or "ffmpeg"
    return _FFMPEG

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
RENDER_URL   = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
DEFAULT_CHAT = int(os.environ.get("DEFAULT_CHAT_ID", "0"))
DB_PATH      = "/tmp/storybot.db"
FONT_PATH    = "/tmp/arabic_font.ttf"
FONT_URL     = "https://github.com/google/fonts/raw/main/ofl/cairo/Cairo%5Bslnt%2Cwght%5D.ttf"
CHECK_EVERY  = 6 * 60 * 60      # كل 6 ساعات
MECCA_TZ     = timezone(timedelta(hours=3))
ARABIC_VOICE = "ar-SA-ZariyahNeural"
UA           = "Mozilla/5.0 (compatible; StoryBot/10.0)"

# ─────────────────────────────────────────────────────
# ① المصادر الثلاثة
# ─────────────────────────────────────────────────────

# الفئات المطلوبة من Gutendex
GUTENDEX_TOPICS = [
    "adventure", "mystery", "historical+fiction",
    "romance", "gothic", "detective",
]

# الفئات المطلوبة من Open Library
OPENLIBRARY_SUBJECTS = [
    "historical_fiction",
    "mystery_and_detective_stories",
    "adventure_stories",
    "science_fiction",
    "gothic_fiction",
    "romance",
]

# ─────────────────────────────────────────────────────
# ② جلب الكتب من كل مصدر
# ─────────────────────────────────────────────────────

def fetch_standard_ebooks(limit=5):
    """Standard Ebooks — آخر الإصدارات عبر Atom Feed"""
    books = []
    try:
        r    = requests.get(
            "https://standardebooks.org/feeds/atom/new-releases",
            headers={"User-Agent": UA}, timeout=12
        )
        feed = feedparser.parse(r.content)
        for e in feed.entries[:limit]:
            title   = e.get("title", "").strip()
            summary = e.get("summary", "").strip()
            link    = e.get("link", "").strip()
            # نص تفصيلي من content
            content = ""
            if hasattr(e, "content") and e.content:
                content = re.sub(r"<[^>]+>", " ", e.content[0].get("value",""))
                content = re.sub(r"\s+", " ", content).strip()
            # المؤلف
            authors = [a.get("name","") for a in e.get("authors", [])]
            author  = ", ".join(a for a in authors if a)
            body    = content or summary
            if title and body:
                books.append({
                    "title":  title,
                    "author": author,
                    "body":   body,
                    "url":    link,
                    "source": "Standard Ebooks",
                    "emoji":  "📘",
                    "cat":    "novel",
                })
        logging.info(f"📘 Standard Ebooks: {len(books)} كتاب")
    except Exception as ex:
        logging.warning(f"❌ Standard Ebooks: {ex}")
    return books


def fetch_gutendex(per_topic=3):
    """Gutendex (Project Gutenberg API) — ملخصات كلاسيكيات"""
    books = []
    topic = random.choice(GUTENDEX_TOPICS)
    try:
        r = requests.get(
            "https://gutendex.com/books/",
            params={"topic": topic, "languages": "en",
                    "page": random.randint(1, 3)},
            headers={"User-Agent": UA}, timeout=20
        )
        if r.status_code != 200:
            logging.warning(f"❌ Gutendex HTTP {r.status_code}")
            return books
        data = r.json()
        results = data.get("results", [])
        random.shuffle(results)
        for book in results[:per_topic * 3]:
            title    = book.get("title", "").strip()
            authors  = [a["name"] for a in book.get("authors", [])]
            author   = ", ".join(authors[:2])
            summaries = book.get("summaries", [])
            body     = summaries[0] if summaries else ""
            subjects = book.get("subjects", [])
            url      = f"https://www.gutenberg.org/ebooks/{book['id']}"
            if title and body and len(body) > 100:
                books.append({
                    "title":  title,
                    "author": author,
                    "body":   body,
                    "url":    url,
                    "source": "Project Gutenberg",
                    "emoji":  "📜",
                    "cat":    "history" if any("hist" in s.lower() for s in subjects) else "novel",
                })
            if len(books) >= per_topic:
                break
        logging.info(f"📜 Gutendex [{topic}]: {len(books)} كتاب")
    except Exception as ex:
        logging.warning(f"❌ Gutendex: {ex}")
    return books


def fetch_openlibrary(per_subject=3):
    """Open Library — موضوعات منوّعة مع وصف مفصّل"""
    books = []
    subject = random.choice(OPENLIBRARY_SUBJECTS)
    try:
        # 1. جلب قائمة كتب الموضوع
        r = requests.get(
            f"https://openlibrary.org/subjects/{subject}.json",
            params={"limit": 12},
            headers={"User-Agent": UA}, timeout=15
        )
        if r.status_code != 200:
            logging.warning(f"❌ Open Library HTTP {r.status_code}")
            return books
        works = r.json().get("works", [])
        random.shuffle(works)

        for w in works:
            if len(books) >= per_subject:
                break
            title  = w.get("title","").strip()
            key    = w.get("key","")
            author = w.get("authors",[{}])[0].get("name","") if w.get("authors") else ""

            if not title or not key:
                continue

            # 2. جلب الوصف من Works API
            try:
                wr = requests.get(
                    f"https://openlibrary.org{key}.json",
                    headers={"User-Agent": UA}, timeout=10
                )
                wd   = wr.json()
                desc = wd.get("description","")
                if isinstance(desc, dict):
                    desc = desc.get("value","")
                desc = str(desc).strip()
            except Exception:
                desc = ""

            if len(desc) < 80:
                continue   # بدون وصف → تخطّ

            url = f"https://openlibrary.org{key}"
            books.append({
                "title":  title,
                "author": author,
                "body":   desc,
                "url":    url,
                "source": "Open Library",
                "emoji":  "📚",
                "cat":    "history" if "histor" in subject else "novel",
            })
        logging.info(f"📚 Open Library [{subject}]: {len(books)} كتاب")
    except Exception as ex:
        logging.warning(f"❌ Open Library: {ex}")
    return books


def fetch_all_books():
    """يجمع الكتب من المصادر الثلاثة"""
    all_books = []
    all_books += fetch_standard_ebooks(limit=5)
    all_books += fetch_gutendex(per_topic=4)
    all_books += fetch_openlibrary(per_subject=3)
    random.shuffle(all_books)
    logging.info(f"📦 الإجمالي: {len(all_books)} كتاب من 3 مصادر")
    return all_books


# ─────────────────────────────────────────────────────
# خط عربي
# ─────────────────────────────────────────────────────
def download_font():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 100_000:
        return
    logging.info("⬇️ تحميل خط Cairo...")
    try:
        r = requests.get(FONT_URL, timeout=30)
        with open(FONT_PATH, "wb") as f:
            f.write(r.content)
        logging.info(f"✅ الخط {os.path.getsize(FONT_PATH)//1024}KB")
    except Exception as ex:
        logging.warning(f"font: {ex}")

def ar(text):
    try:   return get_display(arabic_reshaper.reshape(str(text)))
    except: return str(text)

# ─────────────────────────────────────────────────────
# صورة الغلاف 1280×720
# ─────────────────────────────────────────────────────
def make_thumbnail(title_ar, author_ar, source, emoji, cat, output_path):
    W, H = 1280, 720
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    if cat == "history":
        bg_top, bg_bot = (35, 20, 8),  (55, 35, 12)
        accent         = (212, 160, 40)
        header_bg      = (45, 25, 8)
    else:
        bg_top, bg_bot = (12, 8, 38),  (20, 16, 68)
        accent         = (212, 175, 55)
        header_bg      = (20, 10, 50)

    for y in range(H):
        t  = y / H
        r_ = int(bg_top[0] + t * (bg_bot[0] - bg_top[0]))
        g_ = int(bg_top[1] + t * (bg_bot[1] - bg_top[1]))
        b_ = int(bg_top[2] + t * (bg_bot[2] - bg_top[2]))
        draw.line([(0, y), (W, y)], fill=(r_, g_, b_))

    for pad, w in [(10, 4), (20, 1)]:
        draw.rectangle([pad, pad, W-pad, H-pad], outline=accent, width=w)
    draw.rectangle([0, 0, W, 115],   fill=header_bg)
    draw.line([(60, 114), (W-60, 114)],     fill=accent, width=2)
    draw.rectangle([0, H-130, W, H], fill=header_bg)
    draw.line([(60, H-130), (W-60, H-130)], fill=accent, width=2)
    for x in [75, 165, W-75, W-165]:
        draw.regular_polygon((x, 58, 9), 6, fill=accent)

    try:
        fnt_hdr = ImageFont.truetype(FONT_PATH, 28)
        fnt_big = ImageFont.truetype(FONT_PATH, 74)
        fnt_med = ImageFont.truetype(FONT_PATH, 58)
        fnt_sub = ImageFont.truetype(FONT_PATH, 32)
        fnt_src = ImageFont.truetype(FONT_PATH, 28)
    except:
        fnt_hdr = fnt_big = fnt_med = fnt_sub = fnt_src = ImageFont.load_default()

    hdr_txt = "✦  تاريخ وحضارات  ✦" if cat == "history" else "✦  ملخصات الروايات والقصص  ✦"
    draw.text((W//2, 58), ar(hdr_txt), font=fnt_hdr, fill=accent, anchor="mm")

    # العنوان
    words = title_ar.split()
    l1 = " ".join(words[:4])
    l2 = " ".join(words[4:8]) if len(words) > 4 else ""
    cy = H//2 - (30 if author_ar else 15)
    if l2:
        draw.text((W//2, cy - 45), ar(l1), font=fnt_med, fill=(255, 255, 255), anchor="mm")
        draw.text((W//2, cy + 30), ar(l2), font=fnt_med, fill=(240, 235, 200), anchor="mm")
    else:
        draw.text((W//2, cy),      ar(l1), font=fnt_big, fill=(255, 255, 255), anchor="mm")

    # المؤلف
    if author_ar:
        draw.text((W//2, H - 90), ar(f"✍️  {author_ar}"),
                  font=fnt_sub, fill=(200, 200, 240), anchor="mm")

    # المصدر
    draw.text((W//2, H - 48), ar(f"{emoji}   {source}"),
              font=fnt_src, fill=(160, 155, 200), anchor="mm")

    img.save(output_path, "PNG")

# ─────────────────────────────────────────────────────
# الترجمة
# ─────────────────────────────────────────────────────
def _is_arabic(text):
    if not text: return False
    alpha  = [c for c in text if c.isalpha()]
    if not alpha: return False
    arabic = [c for c in alpha if "\u0600" <= c <= "\u06ff"]
    return len(arabic) / len(alpha) > 0.45

def translate_chunk(text, retries=3):
    if not text or not text.strip(): return ""
    text = text[:4000]
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client":"gtx","sl":"en","tl":"ar","dt":"t","q":text},
                headers={"User-Agent":"Mozilla/5.0"}, timeout=20,
            )
            if r.status_code == 200:
                translated = "".join(p[0] for p in r.json()[0] if p[0]).strip()
                if translated and _is_arabic(translated):
                    return translated
                if translated and len(text) < 40:
                    return translated
                time.sleep(2)
        except Exception as ex:
            logging.warning(f"translate [{attempt+1}]: {ex}")
            time.sleep(2 * (attempt + 1))
    logging.error(f"ترجمة فشلت: {text[:50]}")
    return ""

def translate(text):
    if not text: return ""
    CHUNK = 3800
    if len(text) <= CHUNK:
        return translate_chunk(text)
    parts, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > CHUNK:
            if current.strip(): parts.append(current.strip())
            current = para
        else:
            current = (current + "\n" + para).strip()
    if current.strip(): parts.append(current.strip())
    out = []
    for p in parts:
        t = translate_chunk(p)
        if t: out.append(t)
        time.sleep(1)
    return "\n".join(out)

# ─────────────────────────────────────────────────────
# TTS
# ─────────────────────────────────────────────────────
async def _tts_async(text, path, voice):
    comm = edge_tts.Communicate(text, voice)
    await comm.save(path)

def text_to_audio(text, path, voice=ARABIC_VOICE):
    MAX = 4500
    if len(text) <= MAX:
        asyncio.run(_tts_async(text, path, voice))
        return
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!؟،\n])", text):
        if len(current) + len(sentence) > MAX:
            if current.strip(): chunks.append(current.strip())
            current = sentence
        else:
            current += sentence
    if current.strip(): chunks.append(current.strip())
    tmp = tempfile.mkdtemp()
    parts = []
    for i, chunk in enumerate(chunks):
        p = os.path.join(tmp, f"p{i}.mp3")
        asyncio.run(_tts_async(chunk, p, voice))
        parts.append(p)
        time.sleep(0.3)
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        for p in parts: f.write(f"file '{p}'\n")
    subprocess.run([_get_ffmpeg(), "-y", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", path],
                   capture_output=True, timeout=120)
    for p in parts:
        try: os.remove(p)
        except: pass

# ─────────────────────────────────────────────────────
# الفيديو
# ─────────────────────────────────────────────────────
def make_video(img_path, audio_path, out_path):
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

# ─────────────────────────────────────────────────────
# قاعدة البيانات
# ─────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, sent_at TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS channels (
        chat_id INTEGER PRIMARY KEY, title TEXT, added_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title_ar TEXT, author_ar TEXT, source TEXT, cat TEXT, url TEXT, sent_at TEXT)""")
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

def save_sent(title_ar, author_ar, source, cat, url):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO sent_items(title_ar,author_ar,source,cat,url,sent_at) VALUES(?,?,?,?,?,?)",
            (title_ar, author_ar, source, cat, url, now)
        )
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

# ─────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(chat_id, text):
    try:
        r = requests.post(f"{TG_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True},
                          timeout=15)
        d = r.json()
        if not d.get("ok"):
            err = d.get("description","")
            logging.warning(f"TG [{chat_id}]: {err}")
            if any(w in err for w in ["blocked","not found","kicked","deactivated"]):
                remove_channel(chat_id)
        return d.get("ok", False)
    except Exception as ex:
        logging.warning(f"tg_send: {ex}"); return False

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
            err = d.get("description","")
            logging.warning(f"sendVideo [{chat_id}]: {err}")
            if any(w in err for w in ["blocked","not found","kicked","deactivated"]):
                remove_channel(chat_id)
        return d.get("ok", False)
    except Exception as ex:
        logging.warning(f"tg_send_video: {ex}"); return False

def _split_text(text, max_len=4000):
    if len(text) <= max_len: return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            if current.strip(): parts.append(current.strip())
            current = line
        else:
            current = (current + "\n" + line).strip() if current else line
    if current.strip(): parts.append(current.strip())
    return parts or [text[:max_len]]

def broadcast_text(text):
    for cid in get_channels():
        tg_send(cid, text)
        time.sleep(0.3)

def broadcast_video(video_path, caption):
    chats = get_channels()
    if not chats:
        logging.warning("⚠️ لا قنوات"); return
    sz = os.path.getsize(video_path) / 1024 / 1024
    logging.info(f"📤 {sz:.1f}MB → {len(chats)} قناة")
    for cid in chats:
        tg_send_video(cid, video_path, caption)
        time.sleep(2)

# ─────────────────────────────────────────────────────
# Telegram Polling
# ─────────────────────────────────────────────────────
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
            if not r.ok: time.sleep(5); continue
            for u in r.json().get("result", []):
                _tg_offset = u["update_id"] + 1
                _handle(u)
        except Exception as ex:
            logging.warning(f"poll: {ex}"); time.sleep(5)

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
                "أرسل لك ملخصات من:\n"
                "📘 Standard Ebooks — روايات كلاسيكية محققة\n"
                "📜 Project Gutenberg — أكثر من 70,000 كتاب\n"
                "📚 Open Library — قاعدة بيانات عالمية\n\n"
                "لكل كتاب: 📝 ملخص عربي كامل + 🎬 فيديو صوتي\n"
                "/now — لجلب ملخص الآن")
        elif status in ("left", "kicked"):
            remove_channel(cid)

    if "message" in u:
        msg  = u["message"]
        text = msg.get("text","")
        chat = msg["chat"]
        cid  = chat["id"]
        if text.startswith("/start"):
            title = chat.get("title") or chat.get("first_name") or str(cid)
            add_channel(cid, title)
            tg_send(cid,
                "📚 <b>أهلاً بك في بوت الروايات العالمية!</b>\n\n"
                "<b>المصادر:</b>\n"
                "📘 <b>Standard Ebooks</b> — روايات كلاسيكية محققة\n"
                "📜 <b>Project Gutenberg</b> — 70,000+ كتاب\n"
                "📚 <b>Open Library</b> — قاعدة بيانات شاملة\n\n"
                "<b>لكل رواية ترسل:</b>\n"
                "1️⃣ ملخص عربي كامل (نص)\n"
                "2️⃣ فيديو صوتي 1280×720\n\n"
                "⏱️ يتحقق كل 6 ساعات  |  /now للجلب الآن")
        elif text.startswith("/now"):
            tg_send(cid, "🔄 جارٍ البحث والإرسال...")
            threading.Thread(target=story_cycle, daemon=True).start()

# ─────────────────────────────────────────────────────
# معالجة كتاب واحد
# ─────────────────────────────────────────────────────
_stats = {"cycles": 0, "sent": 0, "skipped": 0}

def process_book(book):
    url    = book.get("url","").strip()
    title  = book.get("title","").strip()
    if not url or not title or is_seen(url):
        return False

    mark_seen(url)

    body   = book.get("body","").strip()
    author = book.get("author","").strip()
    source = book["source"]
    emoji  = book["emoji"]
    cat    = book.get("cat","novel")

    if not body or len(body) < 80:
        logging.info(f"  ⏭️ نص قصير: {title[:50]}")
        return False

    logging.info(f"  🌐 ترجمة: {title[:55]}")

    # ── الترجمة ─────────────────────────────────────
    title_ar  = translate_chunk(title[:200])
    time.sleep(1)
    author_ar = translate_chunk(author[:100]) if author else ""
    time.sleep(0.5)
    summary_ar = translate(body[:2500])

    if not title_ar or not _is_arabic(title_ar):
        logging.warning(f"  ❌ ترجمة العنوان فشلت"); return False
    if not summary_ar or not _is_arabic(summary_ar):
        logging.warning(f"  ❌ ترجمة الملخص فشلت"); return False

    logging.info(f"  ✅ {title_ar[:55]}")

    cat_label = "تاريخ وحضارات" if cat == "history" else "روايات وقصص"
    today     = datetime.now(MECCA_TZ).strftime("%d/%m/%Y")

    # ─────────────────────────────────────────────────
    # 1️⃣ أرسل النص الكامل أولاً
    # ─────────────────────────────────────────────────
    author_line    = f"\n✍️ <i>{author_ar}</i>" if author_ar else ""
    paras          = [p.strip() for p in summary_ar.split("\n") if p.strip()]
    body_formatted = "\n\n".join(paras)
    text_msg = (
        f"{emoji} <b>{title_ar}</b>{author_line}\n"
        f"{'─'*30}\n\n"
        f"{body_formatted}\n\n"
        f"{'━'*15}\n"
        f"📂 {cat_label}  •  📚 {source}  •  📅 {today}"
    )
    logging.info(f"  📤 إرسال النص ({len(text_msg)} حرف)...")
    for part in _split_text(text_msg):
        broadcast_text(part)
        time.sleep(1)

    # ─────────────────────────────────────────────────
    # 2️⃣ ثم أنشئ الفيديو وأرسله
    # ─────────────────────────────────────────────────
    tmpdir = tempfile.mkdtemp()
    img_p  = os.path.join(tmpdir, "thumb.png")
    aud_p  = os.path.join(tmpdir, "audio.mp3")
    vid_p  = os.path.join(tmpdir, "video.mp4")

    try:
        make_thumbnail(title_ar, author_ar, source, emoji, cat, img_p)
        tts_text = f"{title_ar}. {summary_ar}"
        text_to_audio(tts_text, aud_p)

        if not os.path.exists(aud_p) or os.path.getsize(aud_p) < 1000:
            logging.warning("  ❌ صوت فارغ"); return False
        if not make_video(img_p, aud_p, vid_p):
            logging.warning("  ❌ ffmpeg فشل"); return False

        caption = (
            f"{emoji} <b>{title_ar}</b>\n"
            f"📂 {cat_label}  |  📚 {source}"
        )
        broadcast_video(vid_p, caption)
        save_sent(title_ar, author_ar, source, cat, url)
        _stats["sent"] += 1
        logging.info(f"  🎬 أُرسل: {title_ar[:55]}")
        return True

    except Exception as ex:
        logging.error(f"  ❌ process_book: {ex}")
        return False
    finally:
        for f in [img_p, aud_p, vid_p]:
            try: os.remove(f)
            except: pass
        try: os.rmdir(tmpdir)
        except: pass

# ─────────────────────────────────────────────────────
# الدورة الرئيسية
# ─────────────────────────────────────────────────────
_cycle_lock = threading.Lock()

def story_cycle():
    if not _cycle_lock.acquire(blocking=False):
        logging.info("⏭️ دورة جارية"); return
    try:
        logging.info("📚 [Cycle] بدأ — يجلب من 3 مصادر...")
        books = fetch_all_books()
        sent  = 0
        for book in books:
            if process_book(book):
                sent += 1
                time.sleep(5)
            if sent >= 3:   # أقصى 3 روايات لكل دورة
                break
        _stats["cycles"] += 1
        logging.info(f"🏁 [Cycle] أُرسل {sent} رواية")
    finally:
        _cycle_lock.release()

# ─────────────────────────────────────────────────────
# Scheduler + Self-Ping
# ─────────────────────────────────────────────────────
def scheduler():
    while True:
        try: story_cycle()
        except Exception as ex: logging.error(f"Scheduler: {ex}")
        time.sleep(CHECK_EVERY)

def self_ping():
    if not RENDER_URL: return
    while True:
        time.sleep(4 * 60)
        try: requests.get(f"{RENDER_URL}/health", timeout=8)
        except: pass

# ─────────────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────────────
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
    return (f"📚 Story Bot v10.1 | قنوات: {chats} | "
            f"أُرسل: {sent} رواية | معالج: {total}")

@app.route("/health")
def health(): return "OK", 200

@app.route("/trigger")
def trigger():
    threading.Thread(target=story_cycle, daemon=True).start()
    return "🚀 جارٍ الجلب والإرسال...", 200

@app.route("/stats")
def stats():
    try:
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        sent  = conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        last  = conn.execute(
            "SELECT title_ar,author_ar,source,cat,sent_at FROM sent_items ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        return json.dumps({
            "version": "10.1",
            "channels": chats, "sent": sent, "seen": total,
            "cycles": _stats["cycles"],
            "last_10": [
                {"title": r[0], "author": r[1], "source": r[2],
                 "cat": r[3], "at": r[4]} for r in last
            ],
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as ex:
        return json.dumps({"error": str(ex)}), 500

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
        return json.dumps({"ok": True, "deleted": c}), 200, \
               {"Content-Type": "application/json"}
    except Exception as ex:
        return json.dumps({"error": str(ex)}), 500

@app.route("/sources")
def list_sources():
    return json.dumps({
        "sources": [
            {"name": "Standard Ebooks", "url": "https://standardebooks.org/feeds/atom/new-releases",
             "type": "Atom Feed", "content": "روايات كلاسيكية محققة"},
            {"name": "Project Gutenberg", "url": "https://gutendex.com/books/",
             "type": "JSON API", "content": "70,000+ كتاب بملخصات"},
            {"name": "Open Library", "url": "https://openlibrary.org/subjects/",
             "type": "JSON API", "content": "قاعدة بيانات شاملة"},
        ]
    }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}

# ─────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────
def _startup():
    time.sleep(3)
    download_font()
    init_db()
    if DEFAULT_CHAT:
        add_channel(DEFAULT_CHAT, "default")
    threading.Thread(target=tg_poll,   daemon=True, name="poll").start()
    threading.Thread(target=scheduler, daemon=True, name="sched").start()
    threading.Thread(target=self_ping, daemon=True, name="ping").start()
    logging.info("🚀 Story Bot v10.1 | 3 مصادر + Wikipedia enrichment")

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
