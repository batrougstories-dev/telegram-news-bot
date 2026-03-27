#!/usr/bin/env python3
"""
📚 Novel Bot v12.0
==================
• مصدر وحيد  : Gutendex API (gutendex.com)
• ترجمة أدبية: Llama 3.3-70B (GitHub Models) — fallback: Google Translate
• يتخطى المقدمات والخواتم تلقائياً
• بنية الترجمة:
    - Llama يترجم 5000 حرف مع سياق 600 حرف من الجزء السابق
    - النتيجة تُخزَّن كاملةً ثم تُقسَّم لرسائل تيليغرام 3800 حرف
    - الإرسال كل دقيقة مستقل عن الترجمة
• أوامر تيليغرام: "جديد" | "ستب"
• إشارة اكتمال فور انتهاء الرواية
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

# ─────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ["BOT_TOKEN"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RENDER_URL   = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
DEFAULT_CHAT = int(os.environ.get("DEFAULT_CHAT_ID", "0"))

DB_PATH    = "/tmp/novelbot.db"
MECCA_TZ   = timezone(timedelta(hours=3))
TG_API     = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA         = "Mozilla/5.0 (compatible; NovelBot/12.0)"

# ── ترجمة ────────────────────────────────────────────────
LLAMA_PRIMARY  = "Meta-Llama-3.3-70B-Instruct"    # أحدث نموذج
LLAMA_FALLBACK = "Meta-Llama-3.1-405B-Instruct"   # بديل

CHUNK_EN   = 5_000   # حروف إنجليزية لكل استدعاء Llama
CONTEXT_AR = 600     # حروف عربية من الجزء السابق (سياق)
TG_MSG     = 3_800   # حد رسالة تيليغرام
SEND_DELAY = 60      # ثانية بين كل رسالة
NOVEL_MAX  = 80_000  # أقصى حروف إنجليزية للرواية

TOPICS = [
    "fiction", "adventure", "mystery", "detective",
    "romance", "gothic", "historical+fiction",
]

# ─────────────────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────────────────
_worker_thread: threading.Thread = None
_stop_event = threading.Event()

# ─────────────────────────────────────────────────────────
# قاعدة البيانات
# ─────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                chat_id  INTEGER PRIMARY KEY,
                title    TEXT,
                added_at TEXT
            );
            CREATE TABLE IF NOT EXISTS novels (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                gid           INTEGER UNIQUE,
                title         TEXT,
                author        TEXT,
                cover_url     TEXT,
                text_en       TEXT,
                trans_pos     INTEGER DEFAULT 0,
                prev_context  TEXT    DEFAULT '',
                title_ar      TEXT    DEFAULT '',
                status        TEXT    DEFAULT 'idle',
                selected_date TEXT
            );
        """)

# ── قنوات ────────────────────────────────────────────────
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

# ── روايات ───────────────────────────────────────────────
def get_active_novel():
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute(
                "SELECT id,title,title_ar,trans_pos,length(text_en),status "
                "FROM novels WHERE status NOT IN ('complete','stopped') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            return {
                "id": row[0], "title": row[1], "title_ar": row[2],
                "pos": row[3], "total": row[4], "status": row[5],
            }
    except:
        pass
    return None

def save_novel(gid, title, author, cover_url, text_en):
    today = datetime.now(MECCA_TZ).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR IGNORE INTO novels"
            "(gid,title,author,cover_url,text_en,selected_date,status)"
            " VALUES(?,?,?,?,?,?,'idle')",
            (gid, title, author, cover_url, text_en, today),
        )
        return c.execute("SELECT id FROM novels WHERE gid=?", (gid,)).fetchone()[0]

def update_progress(novel_id, new_pos, new_ctx):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "UPDATE novels SET trans_pos=?,prev_context=? WHERE id=?",
                (new_pos, new_ctx, novel_id),
            )
    except:
        pass

def set_status(novel_id, status):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("UPDATE novels SET status=? WHERE id=?", (status, novel_id))
    except:
        pass

def set_title_ar(novel_id, title_ar):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("UPDATE novels SET title_ar=? WHERE id=?", (title_ar, novel_id))
    except:
        pass

# ─────────────────────────────────────────────────────────
# Gutendex API
# ─────────────────────────────────────────────────────────
def pick_novel():
    topic = random.choice(TOPICS)
    page  = random.randint(1, 8)
    logging.info(f"🔍 Gutendex [{topic}] صفحة {page}…")

    for attempt in range(3):
        try:
            r = requests.get(
                "https://gutendex.com/books/",
                params={"topic": topic, "languages": "en", "page": page},
                headers={"User-Agent": UA},
                timeout=40,
            )
            if r.status_code != 200:
                time.sleep(5)
                continue

            results = r.json().get("results", [])
            random.shuffle(results)

            for book in results:
                fmts = book.get("formats", {})

                txt_url = next(
                    (u for f, u in fmts.items() if "text/plain" in f), None
                )
                if not txt_url:
                    continue

                cover_url = next(
                    (u for f, u in fmts.items() if "image/jpeg" in f), None
                )
                gid = book.get("id")
                if not cover_url:
                    cover_url = (
                        f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.cover.medium.jpg"
                    )

                title   = book.get("title", "").strip()
                authors = [a["name"] for a in book.get("authors", [])[:2]]
                author  = ", ".join(authors)
                summary = (
                    book["summaries"][0][:350]
                    if book.get("summaries")
                    else ""
                )
                if not title:
                    continue

                # هل استُخدمت؟
                try:
                    with sqlite3.connect(DB_PATH) as c:
                        if c.execute(
                            "SELECT 1 FROM novels WHERE gid=?", (gid,)
                        ).fetchone():
                            continue
                except:
                    pass

                logging.info(f"✅ {title} — {author}")
                return {
                    "gid":     gid,
                    "title":   title,
                    "author":  author,
                    "summary": summary,
                    "cover":   cover_url,
                    "txt_url": txt_url,
                }

        except Exception as ex:
            logging.warning(f"Gutendex [{attempt+1}]: {ex}")
            time.sleep(5)

    return None

def download_clean(url):
    """تنزيل النص وإزالة ترويسة/ذيل Gutenberg"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
        r.encoding = r.apparent_encoding or "utf-8"
        text = r.text

        for m in [
            "*** START OF THE PROJECT GUTENBERG EBOOK",
            "*** START OF THIS PROJECT GUTENBERG EBOOK",
        ]:
            idx = text.find(m)
            if idx != -1:
                text = text[text.find("\n", idx) + 1:]
                break

        for m in [
            "*** END OF THE PROJECT GUTENBERG EBOOK",
            "*** END OF THIS PROJECT GUTENBERG EBOOK",
            "End of the Project Gutenberg",
        ]:
            idx = text.find(m)
            if idx != -1:
                text = text[:idx]
                break

        text = re.sub(r"\r\n", "\n", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()
    except Exception as ex:
        logging.warning(f"download: {ex}")
        return ""

def extract_main(text):
    """
    يتخطى المقدمات والخواتم:
    - يبحث عن أول فصل (CHAPTER / PART / BOOK)
    - يقطع عند أول ذيل (Appendix / Notes / THE END)
    """
    start = 0
    chapter_re = re.compile(
        r"\n\s{0,4}("
        r"CHAPTER\s+[IVXLC\d]+"
        r"|Chapter\s+[IVXLC\d]+"
        r"|PART\s+[IVXLC\d]+"
        r"|Part\s+(?:One|Two|Three|Four|Five|[IVXLC\d]+)"
        r"|BOOK\s+[IVXLC\d]+"
        r")",
        re.MULTILINE,
    )
    m = chapter_re.search(text)
    if m and m.start() < len(text) * 0.45:
        start = m.start()
        logging.info(f"📍 الفصل الأول عند موضع {start:,}")

    end = len(text)
    end_re = re.compile(
        r"\n\s{0,4}("
        r"THE END\b|FINIS\b"
        r"|Appendix\b|APPENDIX\b"
        r"|Notes\b|NOTES\b"
        r"|Bibliography\b|BIBLIOGRAPHY\b"
        r"|Index\b|INDEX\b"
        r"|Glossary\b|GLOSSARY\b"
        r")",
        re.MULTILINE | re.IGNORECASE,
    )
    m2 = end_re.search(text, start + 5_000)
    if m2:
        end = m2.start()
        logging.info(f"📍 نهاية المحتوى الرئيسي عند موضع {end:,}")

    main = text[start:end].strip()
    logging.info(f"📖 المحتوى الرئيسي: {len(main):,} حرف")
    return main

# ─────────────────────────────────────────────────────────
# الترجمة بـ Llama (أدبية احترافية)
# ─────────────────────────────────────────────────────────
_SYSTEM = """أنت مترجم أدبي خبير ومتمرس في ترجمة الروايات الكلاسيكية من الإنجليزية إلى العربية.

مبادئ ترجمتك:
١. عربية فصحى سلسة بأسلوب أدبي راقٍ — تجنب الترجمة الحرفية الجامدة
٢. انقل روح النص ومشاعره، لا مجرد كلماته
٣. حافظ على نبرة كل شخصية وأسلوبها في الحوار
٤. أسماء الأشخاص والأماكن: عرِّبها صوتياً بشكل طبيعي
٥. أخرج الترجمة العربية فقط — بلا تعليق ولا ملاحظات"""

def llama_translate(text_en, prev_ar=""):
    """
    يترجم نصاً إنجليزياً إلى عربية أدبية باستخدام Llama.
    prev_ar: آخر 600 حرف عربي من الترجمة السابقة (للسياق).
    """
    if not text_en.strip():
        return ""

    # بناء رسالة المستخدم
    if prev_ar.strip():
        user_msg = (
            f"[سياق الترجمة السابقة — لا تُعِد ترجمته، فقط لضمان الاتصال]\n"
            f"{prev_ar.strip()}\n\n"
            f"[النص الجديد للترجمة]\n{text_en}"
        )
    else:
        user_msg = f"[النص للترجمة]\n{text_en}"

    # محاولة مع Llama عبر GitHub Models
    if GITHUB_TOKEN and _OAI_OK:
        client = _OAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=GITHUB_TOKEN,
        )
        for model in [LLAMA_PRIMARY, LLAMA_FALLBACK]:
            for attempt in range(3):
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": _SYSTEM},
                            {"role": "user",   "content": user_msg},
                        ],
                        max_tokens=4096,
                        temperature=0.35,
                    )
                    ar = resp.choices[0].message.content.strip()
                    if ar and _has_arabic(ar):
                        logging.info(f"  🧠 Llama [{model[:20]}]: {len(ar)} حرف")
                        return ar
                except Exception as ex:
                    logging.warning(f"  Llama [{model[:20]}] [{attempt+1}]: {ex}")
                    time.sleep(4 * (attempt + 1))

    # Fallback: Google Translate
    logging.info("  🌐 Google Translate (fallback)…")
    return _google_tr(text_en)

def _google_tr(text):
    """Google Translate — يدعم النصوص الطويلة بتقسيم داخلي"""
    if len(text) <= 4700:
        return _gtr_chunk(text)

    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 <= 4600:
            cur = (cur + "\n\n" + para).strip() if cur else para
        else:
            if cur:
                parts.append(cur)
            cur = para
    if cur:
        parts.append(cur)

    result = []
    for p in parts:
        t = _gtr_chunk(p)
        if t:
            result.append(t)
        time.sleep(0.6)
    return "\n\n".join(result)

def _gtr_chunk(text):
    for _ in range(3):
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "en", "tl": "ar", "dt": "t",
                        "q": text[:4800]},
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
    ar = [c for c in alpha if "\u0600" <= c <= "\u06ff"]
    return len(ar) / len(alpha) > 0.25

# ─────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────
def tg_send(cid, text, parse_mode="HTML"):
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": cid, "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
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
    """يقسّم النص إلى أجزاء ≤ max_len مع الحفاظ على الفقرات"""
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

# ─────────────────────────────────────────────────────────
# Worker: الترجمة والإرسال
# ─────────────────────────────────────────────────────────
def _worker(novel_id):
    """
    الخيط الرئيسي:
    - يترجم 5000 حرف مع سياق 600 حرف من الجزء السابق
    - يخزّن النتيجة ثم يُرسلها على رسائل 3800 حرف (دقيقة بين كل رسالة)
    - يكرر حتى ينتهي النص أو يصل إشارة "ستب"
    """
    global _stop_event
    logging.info(f"🚀 Worker بدأ: رواية #{novel_id}")
    set_status(novel_id, "translating")

    while not _stop_event.is_set():
        # قراءة الحالة الحالية
        try:
            with sqlite3.connect(DB_PATH) as c:
                row = c.execute(
                    "SELECT text_en, trans_pos, prev_context, title_ar "
                    "FROM novels WHERE id=?",
                    (novel_id,),
                ).fetchone()
            if not row:
                break
            text_en, pos, prev_ctx, title_ar = row
        except Exception as ex:
            logging.error(f"Worker DB: {ex}")
            break

        total = len(text_en)

        # اكتملت الترجمة؟
        if pos >= total:
            logging.info(f"🎊 الترجمة اكتملت للرواية #{novel_id}")
            _on_complete(novel_id, title_ar)
            return

        # الجزء التالي
        chunk = text_en[pos: pos + CHUNK_EN]
        logging.info(
            f"  🧠 [{pos:,}/{total:,}] ترجمة {len(chunk)} حرف…"
        )

        ar = llama_translate(chunk, prev_ctx[-CONTEXT_AR:] if prev_ctx else "")

        if not ar:
            logging.warning("  ❌ ترجمة فارغة — تخطي الجزء")
            update_progress(novel_id, pos + len(chunk), prev_ctx)
            continue

        # حفظ التقدم
        update_progress(novel_id, pos + len(chunk), ar[-CONTEXT_AR:])

        # إرسال على تيليغرام (أجزاء 3800 حرف، دقيقة بينها)
        parts = split_tg(ar)
        logging.info(f"  📤 إرسال {len(parts)} رسالة…")
        for i, part in enumerate(parts):
            if _stop_event.is_set():
                set_status(novel_id, "stopped")
                broadcast(
                    "⏹️ <b>تم إيقاف الترجمة.</b>\n\n"
                    "أرسل <b>جديد</b> لبدء رواية جديدة."
                )
                return
            broadcast(part)
            if i < len(parts) - 1:
                time.sleep(SEND_DELAY)

        # فاصل قصير قبل الجزء التالي
        time.sleep(3)

    # وصل إشارة الإيقاف أثناء الحلقة
    if _stop_event.is_set():
        logging.info(f"⏹️ Worker أُوقف: رواية #{novel_id}")
        set_status(novel_id, "stopped")
        broadcast(
            "⏹️ <b>تم إيقاف الترجمة.</b>\n\n"
            "أرسل <b>جديد</b> لبدء رواية جديدة."
        )

def _on_complete(novel_id, title_ar):
    """إشارة اكتمال الترجمة"""
    set_status(novel_id, "complete")
    msg = (
        f"{'═' * 24}\n"
        f"✅ <b>اكتملت ترجمة الرواية كاملةً</b>\n"
        f"{'═' * 24}\n\n"
        f"📖 <b>{title_ar or 'الرواية'}</b>\n\n"
        f"🎉 شكراً لمتابعتكم!\n\n"
        f"أرسل <b>جديد</b> لبدء رواية جديدة 📚"
    )
    broadcast(msg)
    logging.info(f"✅ إشارة اكتمال أُرسلت للرواية #{novel_id}")

# ─────────────────────────────────────────────────────────
# بدء رواية جديدة
# ─────────────────────────────────────────────────────────
def cmd_new():
    """يُنفَّذ في خيط منفصل عند أمر 'جديد'"""
    global _worker_thread, _stop_event

    # إيقاف أي عمل جارٍ
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=8)
    _stop_event = threading.Event()

    broadcast("🔍 <b>جارٍ البحث عن رواية جديدة من Gutenberg…</b>")

    meta = pick_novel()
    if not meta:
        broadcast("⚠️ لم يُعثر على رواية متاحة. أعِد المحاولة.")
        return

    broadcast(f"⬇️ تحميل <b>{meta['title']}</b>…")
    raw = download_clean(meta["txt_url"])
    if not raw or len(raw) < 2_000:
        broadcast("⚠️ فشل تحميل النص. أعِد المحاولة.")
        return

    # استخراج المحتوى الرئيسي (تخطي المقدمات والخواتم)
    main = extract_main(raw)
    if len(main) < 2_000:
        main = raw  # fallback إذا لم يُعثر على فصول

    # تقييد الحجم
    if len(main) > NOVEL_MAX:
        cut  = main.rfind("\n\n", 0, NOVEL_MAX)
        main = main[: cut if cut > NOVEL_MAX * 0.8 else NOVEL_MAX]
        logging.info(f"✂️ مقيَّد عند {len(main):,} حرف")

    # حفظ في DB
    try:
        novel_id = save_novel(
            meta["gid"], meta["title"], meta["author"],
            meta["cover"], main,
        )
    except Exception as ex:
        logging.error(f"save_novel: {ex}")
        broadcast("⚠️ خطأ في قاعدة البيانات.")
        return

    # ترجمة العنوان والمؤلف للعرض
    title_ar  = _google_tr(meta["title"][:200]) or meta["title"]
    author_ar = _google_tr(meta["author"][:100]) if meta["author"] else ""
    summ_ar   = _google_tr(meta["summary"][:350]) if meta["summary"] else ""
    set_title_ar(novel_id, title_ar)

    # إرسال الغلاف مع رسالة تمهيدية
    summ_line = f"{summ_ar}\n\n" if summ_ar else ""
    tr_engine = f"Llama {LLAMA_PRIMARY[:20]}" if (GITHUB_TOKEN and _OAI_OK) else "Google Translate"
    caption = (
        f"📚 <b>رواية جديدة</b>\n{'━' * 22}\n\n"
        f"📖 <b>{title_ar}</b>\n"
        f"✍️ <i>{author_ar}</i>\n\n"
        f"{summ_line}"
        f"{'─' * 22}\n"
        f"📡 Project Gutenberg\n"
        f"🧠 الترجمة: <b>{tr_engine}</b>\n"
        f"✨ أسلوب أدبي احترافي\n\n"
        f"🔜 <i>تبدأ الترجمة الآن…</i>"
    )
    broadcast_photo(meta["cover"], caption)
    time.sleep(3)

    # إطلاق خيط الترجمة
    _worker_thread = threading.Thread(
        target=_worker, args=(novel_id,), daemon=True, name="worker"
    )
    _worker_thread.start()
    logging.info(f"▶️  Worker انطلق: #{novel_id} — {meta['title']}")

def cmd_stop():
    _stop_event.set()
    logging.info("⏹️ أمر الإيقاف")

# ─────────────────────────────────────────────────────────
# Telegram Polling
# ─────────────────────────────────────────────────────────
_tg_offset = 0

def tg_poll():
    global _tg_offset
    logging.info("🤖 Polling بدأ")
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={
                    "offset":          _tg_offset,
                    "timeout":         25,
                    "allowed_updates": '["message","my_chat_member"]',
                },
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
    # إضافة / إزالة قناة
    if "my_chat_member" in u:
        mc     = u["my_chat_member"]
        chat   = mc["chat"]
        status = mc["new_chat_member"]["status"]
        cid    = chat["id"]
        title  = chat.get("title") or chat.get("username") or str(cid)
        if status in ("member", "administrator"):
            add_channel(cid, title)
            tg_send(
                cid,
                "📚 <b>بوت ترجمة الروايات الكلاسيكية</b>\n\n"
                "أرسل <b>جديد</b> لبدء ترجمة رواية جديدة\n"
                "أرسل <b>ستب</b> لإيقاف الترجمة الحالية",
            )
        elif status in ("left", "kicked"):
            remove_channel(cid)

    if "message" not in u:
        return

    msg  = u["message"]
    text = msg.get("text", "").strip()
    cid  = msg["chat"]["id"]
    chat = msg["chat"]

    # ── /start ──────────────────────────────────────────
    if text.startswith("/start"):
        title = chat.get("title") or chat.get("first_name") or str(cid)
        add_channel(cid, title)
        tg_send(
            cid,
            "📚 <b>بوت ترجمة الروايات الكلاسيكية</b>\n"
            f"{'━' * 24}\n\n"
            "📡 المصدر: Project Gutenberg (Gutendex API)\n"
            "🧠 الترجمة: Llama 3.3 — أدبية احترافية\n"
            "✂️ يتخطى المقدمات والخواتم تلقائياً\n"
            "🔗 سياق متصل بين أجزاء الترجمة\n\n"
            "<b>الأوامر:</b>\n"
            "📖 <b>جديد</b> — ابدأ رواية جديدة\n"
            "⏹️ <b>ستب</b> — أوقف الترجمة الحالية\n"
            "📊 <b>/status</b> — حالة الترجمة",
        )

    # ── جديد ────────────────────────────────────────────
    elif text in ("جديد", "جديده", "جديدة", "new", "New", "NEW"):
        title = chat.get("title") or chat.get("first_name") or str(cid)
        add_channel(cid, title)
        threading.Thread(target=cmd_new, daemon=True).start()

    # ── ستب (stop) ──────────────────────────────────────
    elif text.lower() in ("ستب", "stop", "وقف", "إيقاف", "ايقاف"):
        novel = get_active_novel()
        if novel:
            cmd_stop()
            tg_send(cid, "⏹️ <b>جارٍ إيقاف الترجمة…</b>")
        else:
            tg_send(cid, "📭 لا توجد ترجمة جارية حالياً.")

    # ── /status ─────────────────────────────────────────
    elif text.startswith("/status"):
        try:
            with sqlite3.connect(DB_PATH) as c:
                row = c.execute(
                    "SELECT title, title_ar, trans_pos, length(text_en), status "
                    "FROM novels ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if row:
                t, ta, pos, total, st = row
                pct = int(pos / total * 100) if total else 0
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                labels = {
                    "translating": "🔄 جارٍ الترجمة",
                    "complete":    "✅ مكتملة",
                    "stopped":     "⏹️ موقوفة",
                    "idle":        "⏳ انتظار",
                }
                tg_send(
                    cid,
                    f"📊 <b>حالة الترجمة</b>\n{'─' * 22}\n\n"
                    f"📖 {ta or t}\n"
                    f"[{bar}] <b>{pct}%</b>\n"
                    f"📌 {labels.get(st, st)}\n"
                    f"📝 {pos:,} / {total:,} حرف",
                )
            else:
                tg_send(cid, "📭 لا توجد رواية حالياً. أرسل <b>جديد</b> للبدء.")
        except:
            tg_send(cid, "⚠️ خطأ في قراءة الحالة.")

# ─────────────────────────────────────────────────────────
# Self-Ping
# ─────────────────────────────────────────────────────────
def self_ping():
    if not RENDER_URL:
        return
    while True:
        time.sleep(4 * 60)
        try:
            requests.get(f"{RENDER_URL}/health", timeout=8)
        except:
            pass

# ─────────────────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    try:
        with sqlite3.connect(DB_PATH) as c:
            row   = c.execute(
                "SELECT title_ar,title,trans_pos,length(text_en),status "
                "FROM novels ORDER BY id DESC LIMIT 1"
            ).fetchone()
            chats = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        if row:
            ta, t, pos, total, st = row
            pct  = int(pos / total * 100) if total else 0
            info = f"{ta or t} — {pct}% [{st}]"
        else:
            info = "لا توجد رواية"
        return f"📚 Novel Bot v12.0 | قنوات: {chats} | {info}"
    except:
        return "📚 Novel Bot v12.0"

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
    return "🔄 جارٍ البحث عن رواية…", 200

@app.route("/stop")
def stop_ep():
    cmd_stop()
    return "⏹️ إشارة إيقاف أُرسلت", 200

@app.route("/status")
def status_ep():
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows  = c.execute(
                "SELECT gid,title,title_ar,trans_pos,length(text_en),status,selected_date "
                "FROM novels ORDER BY id DESC LIMIT 5"
            ).fetchall()
            chats = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        novels = [
            {
                "gid":      r[0], "title":    r[1], "title_ar": r[2],
                "pos":      r[3], "total":    r[4],
                "pct":      int(r[3] / r[4] * 100) if r[4] else 0,
                "status":   r[5], "date":     r[6],
            }
            for r in rows
        ]
        return (
            json.dumps(
                {
                    "version":     "12.0",
                    "channels":    chats,
                    "llama_model": LLAMA_PRIMARY,
                    "chunk_size":  CHUNK_EN,
                    "send_delay":  SEND_DELAY,
                    "novels":      novels,
                },
                ensure_ascii=False,
                indent=2,
            ),
            200,
            {"Content-Type": "application/json"},
        )
    except Exception as ex:
        return json.dumps({"error": str(ex)}), 500

@app.route("/reset")
def reset_ep():
    try:
        cmd_stop()
        with sqlite3.connect(DB_PATH) as c:
            c.execute("DELETE FROM novels")
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}
    except Exception as ex:
        return json.dumps({"error": str(ex)}), 500

# ─────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────
def _startup():
    time.sleep(3)
    init_db()
    if DEFAULT_CHAT:
        add_channel(DEFAULT_CHAT, "default")
    threading.Thread(target=tg_poll,   daemon=True, name="poll").start()
    threading.Thread(target=self_ping, daemon=True, name="ping").start()
    engine = f"Llama {LLAMA_PRIMARY}" if (GITHUB_TOKEN and _OAI_OK) else "Google Translate"
    logging.info(
        f"🚀 Novel Bot v12.0 | محرك الترجمة: {engine} | "
        f"chunk={CHUNK_EN} | delay={SEND_DELAY}s"
    )

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
