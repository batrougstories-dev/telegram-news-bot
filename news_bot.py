#!/usr/bin/env python3
"""
📚 Novel Summary Bot v2.0
==========================
• يختار رواية كلاسيكية من Gutendex (Project Gutenberg)
• يقرأها كاملةً ويقسّمها إلى فصولها الحقيقية
• يلخّص كل فصل تلخيصاً أدبياً احترافياً باستخدام GPT-4o
• يرسل ملخص كل فصل على حدة إلى تيليغرام
• أوامر: "جديد" | "ستب"
• إشارة اكتمال عند انتهاء جميع الفصول
"""

import os, json, logging, sqlite3, re, time, threading, random
from datetime import datetime, timezone, timedelta

import requests
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

DB_PATH    = "/tmp/novelbot.db"
MECCA_TZ   = timezone(timedelta(hours=3))
TG_API     = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA         = "Mozilla/5.0 (compatible; NovelSummaryBot/2.0)"

# نماذج Llama فقط للتلخيص الأدبي
AI_MODELS = [
    "Meta-Llama-3.1-405B-Instruct",  # Llama الرئيسي (مثبت أنه يعمل)
    "Meta-Llama-3.3-70B-Instruct",   # fallback
]

TG_MSG       = 3_800   # حد رسالة تيليغرام
CHAPTER_DELAY = 90     # ثانية بين إرسال كل فصل
CHAPTER_MAX   = 12_000 # حد أقصى لنص الفصل المُرسَل للذكاء الاصطناعي

# مواضيع تعطي روايات حصراً
TOPICS = [
    "fiction", "novel", "romance",
    "gothic+fiction", "historical+fiction",
    "adventure", "detective", "mystery",
]

# كلمات تثبت أن الكتاب رواية
NOVEL_ACCEPT = [
    "novel", "fiction", "-- fiction", "novels",
    "domestic fiction", "love stories", "adventure stories",
    "gothic fiction", "psychological fiction", "historical fiction",
    "detective", "mystery", "romance",
]

# كلمات تؤكد أن الكتاب ليس رواية
NOVEL_REJECT = [
    "poetry", "epic poem", " poem", "poems",
    "essays", "non-fiction",
    "biography", "autobiography",
    "philosophy",
    "category: plays", "category: drama", "plays/films",
    "cookery", "mathematics",
    "category: travel", "travel writing",
    "sermons", "category: mythology", "category: folklore",
]

# روايات احتياطية مضمونة (كلها روايات بلا استثناء)
FALLBACK_NOVELS = [
    (84,   "Frankenstein",                       "Mary Shelley"),
    (98,   "A Tale of Two Cities",               "Charles Dickens"),
    (1342, "Pride and Prejudice",               "Jane Austen"),
    (1400, "Great Expectations",                "Charles Dickens"),
    (2701, "Moby Dick",                         "Herman Melville"),
    (345,  "Dracula",                           "Bram Stoker"),
    (174,  "The Picture of Dorian Gray",        "Oscar Wilde"),
    (1260, "Jane Eyre",                         "Charlotte Brontë"),
    (161,  "Sense and Sensibility",             "Jane Austen"),
    (730,  "Oliver Twist",                      "Charles Dickens"),
    (1184, "The Count of Monte Cristo",         "Alexandre Dumas"),
    (2097, "Treasure Island",                   "Robert Louis Stevenson"),
    (768,  "Wuthering Heights",                 "Emily Brontë"),
    (43,   "The Strange Case of Dr. Jekyll",    "Robert Louis Stevenson"),
    (1232, "The Prince",                        "Niccolò Machiavelli"),
]

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
            CREATE TABLE IF NOT EXISTS novels (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                gid          INTEGER UNIQUE,
                title        TEXT,
                author       TEXT,
                cover_url    TEXT,
                title_ar     TEXT   DEFAULT '',
                author_ar    TEXT   DEFAULT '',
                status       TEXT   DEFAULT 'idle',
                total_chaps  INTEGER DEFAULT 0,
                selected_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS chapters (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id   INTEGER,
                num        INTEGER,
                title_en   TEXT,
                text_en    TEXT,
                summary_ar TEXT   DEFAULT '',
                status     TEXT   DEFAULT 'pending',
                FOREIGN KEY(novel_id) REFERENCES novels(id)
            );
        """)

# ── قنوات ────────────────────────────────────────────
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
    except:
        pass

def remove_channel(cid):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("DELETE FROM channels WHERE chat_id=?", (cid,))
    except:
        pass

# ── روايات وفصول ─────────────────────────────────────
def save_novel(gid, title, author, cover_url):
    now = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR IGNORE INTO novels(gid,title,author,cover_url,status,selected_at)"
            " VALUES(?,?,?,?,'preparing',?)",
            (gid, title, author, cover_url, now),
        )
        return c.execute("SELECT id FROM novels WHERE gid=?", (gid,)).fetchone()[0]

def save_chapters(novel_id, chapters):
    with sqlite3.connect(DB_PATH) as c:
        c.executemany(
            "INSERT INTO chapters(novel_id,num,title_en,text_en) VALUES(?,?,?,?)",
            [(novel_id, i+1, ch["title"], ch["text"]) for i, ch in enumerate(chapters)],
        )
        c.execute(
            "UPDATE novels SET total_chaps=?,status='ready' WHERE id=?",
            (len(chapters), novel_id),
        )

def update_novel(novel_id, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    vals = list(kw.values()) + [novel_id]
    with sqlite3.connect(DB_PATH) as c:
        c.execute(f"UPDATE novels SET {sets} WHERE id=?", vals)

def get_next_chapter(novel_id):
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT id,num,title_en,text_en FROM chapters "
            "WHERE novel_id=? AND status='pending' ORDER BY num LIMIT 1",
            (novel_id,),
        ).fetchone()
    if row:
        return {"id": row[0], "num": row[1], "title": row[2], "text": row[3]}
    return None

def mark_chapter(chap_id, summary_ar):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE chapters SET status='sent', summary_ar=? WHERE id=?",
            (summary_ar, chap_id),
        )

def count_done(novel_id):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id=? AND status='sent'",
            (novel_id,),
        ).fetchone()[0]

# ─────────────────────────────────────────────────────
# Gutendex + Project Gutenberg
# ─────────────────────────────────────────────────────
def _is_novel(book):
    """
    يتحقق أن الكتاب رواية حقيقية.
    - الرفض يعتمد على bookshelves فقط (لتجنب الرفض الخاطئ)
    - القبول يعتمد على subjects + bookshelves
    """
    subjects    = " ".join(book.get("subjects",    [])).lower()
    bookshelves = " ".join(book.get("bookshelves", [])).lower()

    # رفض بناءً على bookshelves (أكثر دقة)
    for kw in NOVEL_REJECT:
        if kw in bookshelves:
            return False

    # قبول إذا وُجد مؤشر رواية في أي مكان
    combined = subjects + " " + bookshelves
    for kw in NOVEL_ACCEPT:
        if kw in combined:
            return True

    # إذا لم يوجد مؤشر واضح → نرفض احتياطاً
    return False

def pick_novel_gutendex():
    """يختار رواية حصراً من Gutendex مع التحقق من النوع"""
    topic = random.choice(TOPICS)
    page  = random.randint(1, 6)
    logging.info(f"🔍 Gutendex [{topic}] صفحة {page}…")
    try:
        r = requests.get(
            "https://gutendex.com/books/",
            params={"topic": topic, "languages": "en", "page": page},
            headers={"User-Agent": UA}, timeout=50,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
        random.shuffle(results)
        for book in results:
            # ① تحقق أنه رواية
            if not _is_novel(book):
                logging.info(f"  ⏭️ ليس رواية: {book.get('title','')[:40]}")
                continue

            fmts = book.get("formats", {})
            txt  = next((u for f, u in fmts.items() if "text/plain" in f), None)
            if not txt:
                continue

            gid    = book["id"]
            title  = book.get("title", "").strip()
            author = ", ".join(a["name"] for a in book.get("authors", [])[:2])
            cover  = next((u for f, u in fmts.items() if "image/jpeg" in f),
                          f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.cover.medium.jpg")

            # ② لم تُستخدم من قبل
            with sqlite3.connect(DB_PATH) as c:
                if c.execute("SELECT 1 FROM novels WHERE gid=?", (gid,)).fetchone():
                    continue

            logging.info(f"  ✅ رواية: {title[:50]}")
            return {"gid": gid, "title": title, "author": author,
                    "cover": cover, "txt_url": txt}
    except Exception as ex:
        logging.warning(f"Gutendex: {ex}")
    return None

def pick_novel_fallback():
    """
    يختار من قائمة الروايات الاحتياطية المضمونة عبر Gutendex API.
    يستخدم فقط إذا فشل Gutendex في إيجاد رواية.
    """
    used = set()
    try:
        with sqlite3.connect(DB_PATH) as c:
            used = {r[0] for r in c.execute("SELECT gid FROM novels")}
    except:
        pass
    pool = [n for n in FALLBACK_NOVELS if n[0] not in used]
    if not pool:
        pool = FALLBACK_NOVELS
    gid, title, author = random.choice(pool)

    # نحاول جلب البيانات من Gutendex أولاً (للحصول على الصورة الصحيحة)
    try:
        r = requests.get(
            f"https://gutendex.com/books/{gid}",
            headers={"User-Agent": UA}, timeout=30,
        )
        if r.status_code == 200:
            book = r.json()
            fmts  = book.get("formats", {})
            cover = next((u for f, u in fmts.items() if "image/jpeg" in f),
                         f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.cover.medium.jpg")
            txt   = next((u for f, u in fmts.items() if "text/plain" in f),
                         f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8")
            logging.info(f"  ✅ fallback (Gutendex): {title[:50]}")
            return {"gid": gid, "title": title, "author": author,
                    "cover": cover, "txt_url": txt}
    except Exception as ex:
        logging.warning(f"  fallback Gutendex: {ex}")

    # إذا فشل Gutendex → روابط مباشرة من Gutenberg (احتياط أخير)
    cover   = f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.cover.medium.jpg"
    txt_url = f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8"
    logging.info(f"  ✅ fallback (direct): {title[:50]}")
    return {"gid": gid, "title": title, "author": author,
            "cover": cover, "txt_url": txt_url}

def download_text(url):
    """تحميل النص من Project Gutenberg"""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=50)
    r.encoding = r.apparent_encoding or "utf-8"
    text = r.text

    # إزالة ترويسة وذيل Gutenberg
    for m in ["*** START OF THE PROJECT GUTENBERG EBOOK",
              "*** START OF THIS PROJECT GUTENBERG EBOOK"]:
        idx = text.find(m)
        if idx != -1:
            text = text[text.find("\n", idx) + 1:]
            break
    for m in ["*** END OF THE PROJECT GUTENBERG EBOOK",
              "*** END OF THIS PROJECT GUTENBERG EBOOK",
              "End of the Project Gutenberg", "End of Project Gutenberg"]:
        idx = text.find(m)
        if idx != -1:
            text = text[:idx]
            break

    return re.sub(r"\n{4,}", "\n\n\n", re.sub(r"\r\n", "\n", text)).strip()

def split_chapters(text):
    """
    يقسّم النص إلى فصوله الحقيقية.
    يدعم أنماطاً متعددة: CHAPTER I / Chapter 1 / BOOK I / PART ONE
    إذا لم توجد فصول → يقسّم إلى أقسام بالحجم.
    """
    chap_re = re.compile(
        r"\n\s{0,4}("
        r"CHAPTER\s+[IVXLC\d]+\.?[^\n]{0,80}"
        r"|Chapter\s+[IVXLC\d]+\.?[^\n]{0,80}"
        r"|BOOK\s+[IVXLC\d]+\.?[^\n]{0,80}"
        r"|Book\s+[IVXLC\d]+\.?[^\n]{0,80}"
        r"|PART\s+[IVXLC\d]+\.?[^\n]{0,80}"
        r"|Part\s+(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|[IVXLC\d]+)\.?[^\n]{0,60}"
        r"|ADVENTURE\s+[IVXLC\d]+\.?[^\n]{0,80}"
        r"|SECTION\s+[IVXLC\d]+\.?[^\n]{0,80}"
        r")\n",
        re.MULTILINE,
    )

    matches = list(chap_re.finditer(text))

    # هل الفصول في النصف الأول فقط؟ (قد تكون فهرساً)
    if matches:
        real = [m for m in matches if m.start() > len(text) * 0.05]
        if len(real) >= 2:
            matches = real

    if len(matches) >= 2:
        chapters = []
        for i, m in enumerate(matches):
            title_line = m.group(1).strip()
            start = m.end()
            end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body  = text[start:end].strip()
            if len(body) < 200:
                continue
            chapters.append({"title": title_line, "text": body})
        if chapters:
            logging.info(f"📑 تقسيم بالفصول: {len(chapters)} فصل")
            return chapters

    # لا فصول واضحة → تقسيم بالحجم (كل 8000 حرف = قسم)
    logging.info("📑 لا فصول → تقسيم بالحجم")
    section_size = 8_000
    paragraphs   = text.split("\n\n")
    sections, cur = [], ""
    sec_num = 1
    for para in paragraphs:
        if len(cur) + len(para) + 2 <= section_size:
            cur = (cur + "\n\n" + para).strip() if cur else para
        else:
            if len(cur) > 500:
                sections.append({"title": f"القسم {sec_num}", "text": cur})
                sec_num += 1
            cur = para
    if len(cur) > 500:
        sections.append({"title": f"القسم {sec_num}", "text": cur})
    return sections

# ─────────────────────────────────────────────────────
# الذكاء الاصطناعي — تلخيص الفصول
# ─────────────────────────────────────────────────────
_SYS_SUMMARY = """أنت ناقد أدبي وباحث متخصص في الأدب الكلاسيكي العالمي.

مهمتك: كتابة ملخص شامل ومفصّل لفصل من رواية، بالعربية الفصحى الجميلة.

يجب أن يتضمن ملخصك:
١. **الأحداث الرئيسية** — بترتيبها مع تفاصيل مهمة
٢. **الشخصيات** — تصرفاتها، مشاعرها، تطورها في هذا الفصل
٣. **الحوارات المحورية** — أبرز ما قيل وما يكشفه
٤. **التوتر الدرامي** — اللحظات المشحونة والتحولات المفصلية
٥. **الدلالات والرموز** — إن وجدت في النص
٦. **ختام الفصل** — بماذا ينتهي وكيف يفتح شهية القارئ للتالي

الأسلوب: فقرات متدفقة، لغة أدبية راقية، تفاصيل كافية تجعل القارئ يعيش الفصل.
لا قوائم نقطية. لا عناوين فرعية. نص أدبي متواصل."""

def summarize_chapter(title, text_en, novel_title, author):
    """
    يلخّص فصلاً واحداً بـ GPT-4o (أو Llama كـ fallback).
    يُرسِل ما يصل إلى CHAPTER_MAX حرف من نص الفصل.
    """
    # إذا الفصل طويل جداً → خذ الجزء الأهم (بداية + نهاية)
    body = text_en.strip()
    if len(body) > CHAPTER_MAX:
        half = CHAPTER_MAX // 2
        body = body[:half] + "\n\n[...]\n\n" + body[-half:]

    user_msg = (
        f"الرواية: «{novel_title}» — {author}\n"
        f"الفصل: {title}\n\n"
        f"نص الفصل:\n{body}"
    )

    if not GITHUB_TOKEN or not _OAI_OK:
        logging.warning("  ⚠️ لا GITHUB_TOKEN — Google Translate فقط")
        return _simple_translate(text_en[:3000])

    client = _OAI(
        base_url="https://models.inference.ai.azure.com",
        api_key=GITHUB_TOKEN,
    )

    for model in AI_MODELS:
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _SYS_SUMMARY},
                        {"role": "user",   "content": user_msg},
                    ],
                    max_tokens=1500,
                    temperature=0.4,
                )
                ar = resp.choices[0].message.content.strip()
                if ar and _has_arabic(ar):
                    logging.info(f"  🧠 [{model[:18]}]: {len(ar)} حرف")
                    return ar
            except Exception as ex:
                logging.warning(f"  AI [{model[:18]}][{attempt+1}]: {ex}")
                time.sleep(4 * (attempt + 1))

    return _simple_translate(text_en[:3000])

def _simple_translate(text):
    """ترجمة بسيطة كـ fallback أخير"""
    for _ in range(3):
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "en", "tl": "ar",
                        "dt": "t", "q": text[:4800]},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
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
    return bool(alpha) and sum(1 for c in alpha if "\u0600" <= c <= "\u06ff") / len(alpha) > 0.3

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
# Worker الرئيسي
# ─────────────────────────────────────────────────────
_worker_thread = None
_stop_event    = threading.Event()

def _worker(novel_id):
    global _stop_event

    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT title, title_ar, author, total_chaps FROM novels WHERE id=?",
            (novel_id,),
        ).fetchone()
    if not row:
        return
    title, title_ar, author, total = row
    label = title_ar or title

    update_novel(novel_id, status="summarizing")
    logging.info(f"🚀 Worker بدأ: «{title}» — {total} فصل")

    while not _stop_event.is_set():
        chap = get_next_chapter(novel_id)
        if not chap:
            _on_complete(novel_id, label, total)
            return

        num   = chap["num"]
        ctitle = chap["title"]
        logging.info(f"  📖 فصل {num}/{total}: {ctitle[:50]}")

        # ترجمة عنوان الفصل
        ctitle_ar = _simple_translate(ctitle[:150]) or ctitle

        # تلخيص الفصل
        summary = summarize_chapter(ctitle, chap["text"], title, author)
        if not summary:
            logging.warning(f"  ❌ تخطي الفصل {num}")
            mark_chapter(chap["id"], "")
            continue

        # بناء الرسالة
        done    = count_done(novel_id)
        msg_header = (
            f"📖 <b>{label}</b>\n"
            f"{'─' * 26}\n"
            f"<b>الفصل {num}: {ctitle_ar}</b>\n"
            f"{'─' * 26}\n\n"
        )

        parts = split_tg(summary)

        # الجزء الأول يحمل العنوان
        broadcast(msg_header + parts[0])
        for part in parts[1:]:
            time.sleep(3)
            broadcast(part)

        mark_chapter(chap["id"], summary)
        logging.info(f"  ✅ أُرسل الفصل {num}")

        if not _stop_event.is_set():
            time.sleep(CHAPTER_DELAY)

    # إيقاف
    if _stop_event.is_set():
        update_novel(novel_id, status="stopped")
        broadcast(
            f"⏹️ <b>توقفت عند الفصل {count_done(novel_id)}</b>\n\n"
            f"أرسل <b>جديد</b> لرواية أخرى أو <b>استمر</b> لمتابعة هذه الرواية."
        )

def _on_complete(novel_id, title_ar, total):
    update_novel(novel_id, status="complete")
    broadcast(
        f"{'═' * 26}\n"
        f"✅ <b>اكتملت جميع فصول الرواية</b>\n"
        f"{'═' * 26}\n\n"
        f"📚 <b>{title_ar}</b>\n"
        f"📊 {total} فصل — ملخص شامل لكل فصل\n\n"
        f"🎉 شكراً لمتابعتكم!\n\n"
        f"أرسل <b>جديد</b> لبدء رواية جديدة 📖"
    )
    logging.info(f"🎊 اكتملت: «{title_ar}»")

def cmd_new():
    """ينفَّذ في خيط منفصل عند أمر 'جديد'"""
    global _worker_thread, _stop_event

    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=8)
    _stop_event = threading.Event()

    broadcast("🔍 <b>جارٍ اختيار رواية جديدة…</b>")

    # اختيار الرواية
    meta = pick_novel_gutendex() or pick_novel_fallback()
    if not meta:
        broadcast("⚠️ خطأ في الاختيار. أعِد المحاولة.")
        return

    broadcast(f"⬇️ <b>تحميل:</b> «{meta['title']}» بقلم {meta['author']}…")

    # تحميل النص
    try:
        text = download_text(meta["txt_url"])
    except Exception as ex:
        broadcast(f"⚠️ فشل التحميل: {ex}")
        return

    if len(text) < 3_000:
        broadcast("⚠️ النص قصير جداً. أعِد المحاولة.")
        return

    logging.info(f"📥 النص: {len(text):,} حرف")

    # تقسيم الفصول
    chapters = split_chapters(text)
    if not chapters:
        broadcast("⚠️ تعذّر تقسيم الفصول. أعِد المحاولة.")
        return

    # حد أقصى 30 فصلاً (لتجنب إرسال مئات الرسائل)
    if len(chapters) > 30:
        chapters = chapters[:30]

    # حفظ في DB
    try:
        novel_id = save_novel(meta["gid"], meta["title"], meta["author"], meta["cover"])
        save_chapters(novel_id, chapters)
    except Exception as ex:
        logging.error(f"DB save: {ex}")
        broadcast("⚠️ خطأ في قاعدة البيانات.")
        return

    # ترجمة العنوان والمؤلف
    title_ar  = _simple_translate(meta["title"][:200]) or meta["title"]
    author_ar = _simple_translate(meta["author"][:100]) if meta["author"] else ""
    update_novel(novel_id, title_ar=title_ar, author_ar=author_ar)

    # إرسال رسالة التعريف مع الغلاف
    intro = _build_intro(meta, title_ar, author_ar, len(chapters))
    broadcast_photo(meta["cover"], intro)
    time.sleep(3)

    # إطلاق Worker
    _worker_thread = threading.Thread(
        target=_worker, args=(novel_id,), daemon=True, name="worker"
    )
    _worker_thread.start()
    logging.info(f"▶️ Worker انطلق: #{novel_id} «{meta['title']}» — {len(chapters)} فصل")

def _build_intro(meta, title_ar, author_ar, total_chaps):
    model_label = AI_MODELS[0] if (GITHUB_TOKEN and _OAI_OK) else "Google Translate"
    return (
        f"📚 <b>رواية جديدة</b>\n{'━' * 24}\n\n"
        f"📖 <b>{title_ar}</b>\n"
        f"✍️ <i>{author_ar or meta['author']}</i>\n\n"
        f"{'─' * 24}\n"
        f"📑 عدد الفصول: <b>{total_chaps} فصل</b>\n"
        f"🧠 التلخيص بـ: <b>{model_label}</b>\n"
        f"⏱️ فصل كل <b>{CHAPTER_DELAY//60} دقيقة</b>\n"
        f"{'─' * 24}\n\n"
        f"🔜 <i>يبدأ التلخيص الآن…</i>\n\n"
        f"<b>أوامر:</b>\n"
        f"⏹️ <b>ستب</b> — إيقاف | 📖 <b>جديد</b> — رواية أخرى"
    )

def cmd_stop():
    _stop_event.set()
    logging.info("⏹️ أمر الإيقاف")

def cmd_resume():
    """يتابع الرواية الحالية إن كانت موقوفة"""
    global _worker_thread, _stop_event
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute(
                "SELECT id FROM novels WHERE status='stopped' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return False
        novel_id = row[0]
        _stop_event = threading.Event()
        update_novel(novel_id, status="summarizing")
        _worker_thread = threading.Thread(
            target=_worker, args=(novel_id,), daemon=True, name="worker"
        )
        _worker_thread.start()
        return True
    except:
        return False

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
        mc    = u["my_chat_member"]
        chat  = mc["chat"]
        st    = mc["new_chat_member"]["status"]
        cid   = chat["id"]
        title = chat.get("title") or chat.get("username") or str(cid)
        if st in ("member", "administrator"):
            add_channel(cid, title)
            _send_welcome(cid)
        elif st in ("left", "kicked"):
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
        _send_welcome(cid)

    elif text in ("جديد", "جديده", "جديدة", "new", "New", "NEW"):
        title = chat.get("title") or chat.get("first_name") or str(cid)
        add_channel(cid, title)
        threading.Thread(target=cmd_new, daemon=True).start()

    elif text.lower() in ("ستب", "stop", "وقف", "إيقاف", "ايقاف"):
        try:
            with sqlite3.connect(DB_PATH) as c:
                active = c.execute(
                    "SELECT id FROM novels WHERE status='summarizing'"
                ).fetchone()
        except:
            active = None
        if active:
            cmd_stop()
            tg_send(cid, "⏹️ <b>جارٍ إيقاف التلخيص…</b>")
        else:
            tg_send(cid, "📭 لا يوجد تلخيص جارٍ حالياً.")

    elif text in ("استمر", "تابع", "continue"):
        ok = cmd_resume()
        tg_send(cid, "▶️ <b>متابعة التلخيص…</b>" if ok
                else "📭 لا توجد رواية موقوفة.")

    elif text.startswith("/status"):
        _send_status(cid)

def _send_welcome(cid):
    tg_send(cid,
        "📚 <b>بوت ملخصات الروايات الكلاسيكية</b>\n"
        f"{'━' * 26}\n\n"
        "يختار روايةً كلاسيكية ويلخّص كل فصل فيها\n"
        "تلخيصاً أدبياً احترافياً مفصّلاً بالعربية\n\n"
        "📡 المصدر: Project Gutenberg\n"
        f"🧠 الذكاء: {AI_MODELS[0]}\n\n"
        "<b>الأوامر:</b>\n"
        "📖 <b>جديد</b> — اختر رواية جديدة\n"
        "⏹️ <b>ستب</b> — أوقف التلخيص\n"
        "▶️ <b>استمر</b> — تابع رواية موقوفة\n"
        "📊 <b>/status</b> — التقدم الحالي"
    )

def _send_status(cid):
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute(
                "SELECT title_ar, title, total_chaps, status FROM novels "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            chats = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        if row:
            ta, t, total, st = row
            done = 0
            with sqlite3.connect(DB_PATH) as c:
                nid  = c.execute("SELECT id FROM novels ORDER BY id DESC LIMIT 1").fetchone()[0]
                done = c.execute(
                    "SELECT COUNT(*) FROM chapters WHERE novel_id=? AND status='sent'",
                    (nid,),
                ).fetchone()[0]
            pct  = int(done / total * 100) if total else 0
            bar  = "█" * (pct // 10) + "░" * (10 - pct // 10)
            labels = {"summarizing": "🔄 جارٍ التلخيص", "complete": "✅ مكتمل",
                      "stopped": "⏹️ موقوف", "ready": "⏳ جاهز", "preparing": "⚙️ تحضير"}
            tg_send(cid,
                f"📊 <b>التقدم</b>\n{'─' * 22}\n\n"
                f"📖 {ta or t}\n"
                f"[{bar}] <b>{pct}%</b>\n"
                f"📑 {done}/{total} فصل\n"
                f"📌 {labels.get(st, st)}\n"
                f"📣 قنوات: {chats}"
            )
        else:
            tg_send(cid, "📭 لا توجد رواية. أرسل <b>جديد</b> للبدء.")
    except:
        tg_send(cid, "⚠️ خطأ في قراءة الحالة.")

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
            row   = c.execute(
                "SELECT title_ar, title, total_chaps, status FROM novels ORDER BY id DESC LIMIT 1"
            ).fetchone()
            chats = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        if row:
            ta, t, total, st = row
            nid  = c.execute("SELECT id FROM novels ORDER BY id DESC LIMIT 1") if row else None
            info = f"{ta or t} | {st}"
        else:
            info = "لا توجد رواية"
        return f"📚 Novel Summary Bot v2.1 | قنوات: {chats} | {info}"
    except:
        return "📚 Novel Summary Bot v2.1"

@app.route("/health")
def health():
    return "OK", 200

@app.route("/add/<int:cid>")
def add_ep(cid):
    add_channel(cid, f"manual-{cid}")
    return json.dumps({"ok": True, "chat_id": cid}), 200, {
        "Content-Type": "application/json"
    }

@app.route("/new")
def new_ep():
    threading.Thread(target=cmd_new, daemon=True).start()
    return "🔄 جارٍ الاختيار…", 200

@app.route("/stop")
def stop_ep():
    cmd_stop()
    return "⏹️ إشارة إيقاف", 200

@app.route("/status")
def status_ep():
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows  = c.execute(
                "SELECT id,title,title_ar,total_chaps,status FROM novels ORDER BY id DESC LIMIT 5"
            ).fetchall()
            chats = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        result = []
        for r in rows:
            nid   = r[0]
            done  = c.execute(
                "SELECT COUNT(*) FROM chapters WHERE novel_id=? AND status='sent'",
                (nid,),
            ).fetchone()[0] if False else 0
            with sqlite3.connect(DB_PATH) as c2:
                done = c2.execute(
                    "SELECT COUNT(*) FROM chapters WHERE novel_id=? AND status='sent'",
                    (nid,),
                ).fetchone()[0]
            result.append({
                "id": r[0], "title": r[1], "title_ar": r[2],
                "total": r[3], "done": done,
                "pct": int(done / r[3] * 100) if r[3] else 0,
                "status": r[4],
            })
        return json.dumps({
            "version": "2.1", "channels": chats,
            "ai_model": AI_MODELS[0], "chapter_delay": CHAPTER_DELAY,
            "novels": result,
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as ex:
        return json.dumps({"error": str(ex)}), 500

@app.route("/reset")
def reset_ep():
    try:
        cmd_stop()
        with sqlite3.connect(DB_PATH) as c:
            c.execute("DELETE FROM chapters")
            c.execute("DELETE FROM novels")
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}
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
    threading.Thread(target=tg_poll,   daemon=True, name="poll").start()
    threading.Thread(target=self_ping, daemon=True, name="ping").start()
    model = AI_MODELS[0] if (GITHUB_TOKEN and _OAI_OK) else "Google Translate"
    logging.info(
        f"🚀 Novel Summary Bot v2.0 | نموذج: {model} | "
        f"فصل كل {CHAPTER_DELAY}s"
    )

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
