#!/usr/bin/env python3
"""
📚 Novel Translation Bot v11.0
==============================
المصدر الوحيد : Gutendex API  (gutendex.com)
- يختار رواية واحدة كل يوم
- يحمّل نصها الكامل من Project Gutenberg
- يُقسّمه إلى دفعات 3000 حرف
- يترجم كل دفعة ويرسلها تلقائياً كل ساعة
- عند اكتمال الترجمة يُرسل إشارة إتمام
- في اليوم التالي: رواية جديدة
"""

import os, json, logging, sqlite3, re, time, threading, random
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask

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

DB_PATH      = "/tmp/novelbot.db"
MECCA_TZ     = timezone(timedelta(hours=3))

BATCH_SIZE   = 3000          # حروف إنجليزية لكل دفعة
BATCH_DELAY  = 60              # ثانية بين دفعة وأخرى (دقيقة واحدة)
NOVEL_MAX    = 60_000        # أقصى حروف إنجليزية تُترجم للرواية الواحدة

TOPICS = [
    "fiction", "adventure", "mystery", "detective",
    "romance", "gothic", "historical+fiction", "science+fiction",
]

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA     = "Mozilla/5.0 (compatible; NovelBot/11.0)"

# ─────────────────────────────────────────────────────
# قاعدة البيانات
# ─────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id  INTEGER PRIMARY KEY,
            title    TEXT,
            added_at TEXT
        );

        CREATE TABLE IF NOT EXISTS novels (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            gutenberg_id  INTEGER UNIQUE,
            title         TEXT,
            author        TEXT,
            summary       TEXT,
            total_batches INTEGER,
            selected_date TEXT,
            status        TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS batches (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            novel_id  INTEGER,
            batch_num INTEGER,
            text_en   TEXT,
            text_ar   TEXT    DEFAULT '',
            status    TEXT    DEFAULT 'pending',
            sent_at   TEXT,
            UNIQUE(novel_id, batch_num)
        );
    """)
    conn.commit()
    conn.close()

# ── قنوات ─────────────────────────────────────────────
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

# ── روايات ────────────────────────────────────────────
def today_str():
    return datetime.now(MECCA_TZ).strftime("%Y-%m-%d")

def get_today_novel():
    """إرجاع رواية اليوم النشطة أو None"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            "SELECT id,title,author,total_batches,status "
            "FROM novels WHERE selected_date=? AND status='active'",
            (today_str(),),
        ).fetchone()
        conn.close()
        if row:
            return {
                "id": row[0], "title": row[1],
                "author": row[2], "total_batches": row[3],
                "status": row[4],
            }
    except:
        pass
    return None

def get_next_batch(novel_id):
    """الدفعة التالية المعلّقة"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            "SELECT batch_num, text_en FROM batches "
            "WHERE novel_id=? AND status='pending' ORDER BY batch_num LIMIT 1",
            (novel_id,),
        ).fetchone()
        conn.close()
        return {"batch_num": row[0], "text_en": row[1]} if row else None
    except:
        return None

def mark_batch_sent(novel_id, batch_num, text_ar):
    try:
        now  = datetime.now(MECCA_TZ).strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE batches SET status='sent',text_ar=?,sent_at=? "
            "WHERE novel_id=? AND batch_num=?",
            (text_ar, now, novel_id, batch_num),
        )
        conn.commit()
        conn.close()
    except:
        pass

def count_sent(novel_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        n    = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE novel_id=? AND status='sent'",
            (novel_id,),
        ).fetchone()[0]
        conn.close()
        return n
    except:
        return 0

def mark_novel_done(novel_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE novels SET status='completed' WHERE id=?", (novel_id,))
        conn.commit()
        conn.close()
    except:
        pass

# ─────────────────────────────────────────────────────
# Gutendex: اختيار رواية
# ─────────────────────────────────────────────────────
def pick_novel_from_gutendex():
    """
    يتصل بـ Gutendex API، يختار رواية عشوائية
    لها نص قابل للتنزيل، ولم تُستخدم قبلاً.
    """
    topic = random.choice(TOPICS)
    page  = random.randint(1, 8)
    logging.info(f"🔍 Gutendex [{topic}] صفحة {page}...")

    for attempt in range(3):
        try:
            r = requests.get(
                "https://gutendex.com/books/",
                params={"topic": topic, "languages": "en", "page": page},
                headers={"User-Agent": UA},
                timeout=35,
            )
            if r.status_code != 200:
                time.sleep(3)
                continue

            results = r.json().get("results", [])
            random.shuffle(results)

            for book in results:
                # رابط النص
                text_url = None
                for fmt, url in book.get("formats", {}).items():
                    if "text/plain" in fmt:
                        text_url = url
                        break
                if not text_url:
                    continue

                title   = book.get("title", "").strip()
                authors = [a["name"] for a in book.get("authors", [])]
                author  = ", ".join(authors[:2])
                summary = (book.get("summaries", [""])[0]
                           if book.get("summaries") else "")
                gid     = book.get("id")

                if not title:
                    continue

                # هل استُخدمت من قبل؟
                try:
                    conn   = sqlite3.connect(DB_PATH)
                    exists = conn.execute(
                        "SELECT 1 FROM novels WHERE gutenberg_id=?", (gid,)
                    ).fetchone()
                    conn.close()
                    if exists:
                        continue
                except:
                    pass

                logging.info(f"✅ وُجدت: {title} — {author}")
                # رابط الغلاف
                cover_url = None
                for fmt, url in book.get("formats", {}).items():
                    if "image/jpeg" in fmt:
                        cover_url = url
                        break
                # إذا لم يوجد في formats → نبني الرابط المعياري
                if not cover_url:
                    cover_url = (
                        f"https://www.gutenberg.org/cache/epub/{gid}"
                        f"/pg{gid}.cover.medium.jpg"
                    )

                return {
                    "gutenberg_id": gid,
                    "title":        title,
                    "author":       author,
                    "summary":      summary,
                    "text_url":     text_url,
                    "cover_url":    cover_url,
                }
        except Exception as ex:
            logging.warning(f"Gutendex محاولة {attempt+1}: {ex}")
            time.sleep(5)

    return None

# ─────────────────────────────────────────────────────
# تنزيل وتنظيف النص
# ─────────────────────────────────────────────────────
def download_text(url):
    """تنزيل نص من Gutenberg وإزالة ترويسة/ذيل المشروع"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=40)
        r.encoding = r.apparent_encoding or "utf-8"
        text = r.text

        # إزالة الترويسة
        for m in [
            "*** START OF THE PROJECT GUTENBERG EBOOK",
            "*** START OF THIS PROJECT GUTENBERG EBOOK",
            "*END*THE SMALL PRINT",
        ]:
            idx = text.find(m)
            if idx != -1:
                text = text[text.find("\n", idx) + 1 :]
                break

        # إزالة الذيل
        for m in [
            "*** END OF THE PROJECT GUTENBERG EBOOK",
            "*** END OF THIS PROJECT GUTENBERG EBOOK",
            "End of the Project Gutenberg",
            "End of Project Gutenberg",
        ]:
            idx = text.find(m)
            if idx != -1:
                text = text[:idx]
                break

        text = re.sub(r"\r\n", "\n", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()

    except Exception as ex:
        logging.warning(f"download_text: {ex}")
        return ""

# ─────────────────────────────────────────────────────
# تقسيم النص إلى دفعات
# ─────────────────────────────────────────────────────
def split_batches(text, size=BATCH_SIZE):
    """
    يُقسّم النص إلى دفعات ≤ size حرف،
    مع الحفاظ على حدود الفقرات والجمل.
    """
    batches = []
    current = ""

    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue

        if len(current) + len(para) + 2 <= size:
            current = (current + "\n\n" + para).strip() if current else para
        else:
            if current:
                batches.append(current)
            if len(para) <= size:
                current = para
            else:
                # فقرة أطول من الدفعة → تقسيم بالجمل
                current = ""
                for sent in re.split(r"(?<=[.!?؟])\s+", para):
                    if len(current) + len(sent) + 1 <= size:
                        current = (current + " " + sent).strip() if current else sent
                    else:
                        if current:
                            batches.append(current)
                        current = sent[:size]  # حد أقصى صارم

    if current:
        batches.append(current)

    return batches

# ─────────────────────────────────────────────────────
# الترجمة
# ─────────────────────────────────────────────────────
def _tr_chunk(chunk):
    """ترجمة جزء ≤ 4900 حرف"""
    for attempt in range(4):
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx", "sl": "en", "tl": "ar",
                    "dt": "t", "q": chunk[:4900],
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=25,
            )
            if r.status_code == 200:
                translated = "".join(p[0] for p in r.json()[0] if p[0]).strip()
                if translated:
                    return translated
        except Exception as ex:
            logging.warning(f"  translate [{attempt+1}]: {ex}")
        time.sleep(2 * (attempt + 1))
    return ""

def translate_batch(text):
    """
    يترجم دفعة نصية (≤ 3000 حرف).
    يُقسّمها داخلياً إذا احتاج (حد Google = 4900 حرف).
    """
    if not text or not text.strip():
        return ""

    if len(text) <= 4800:
        return _tr_chunk(text)

    # نادراً ما يحدث (batch_size=3000 < 4800) لكن احتياطاً
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 <= 4700:
            cur = (cur + "\n\n" + para).strip() if cur else para
        else:
            if cur:
                parts.append(cur)
            cur = para
    if cur:
        parts.append(cur)

    result = []
    for p in parts:
        t = _tr_chunk(p)
        if t:
            result.append(t)
        time.sleep(0.8)
    return "\n\n".join(result)

# ─────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────
def tg_send(chat_id, text, parse_mode="HTML"):
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id":               chat_id,
                "text":                  text,
                "parse_mode":            parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        d = r.json()
        if not d.get("ok"):
            err = d.get("description", "")
            logging.warning(f"TG [{chat_id}]: {err}")
            if any(w in err for w in ["blocked", "not found", "kicked", "deactivated"]):
                remove_channel(chat_id)
        return d.get("ok", False)
    except Exception as ex:
        logging.warning(f"tg_send: {ex}")
        return False

def broadcast(text, parse_mode="HTML"):
    for cid in get_channels():
        tg_send(cid, text, parse_mode)
        time.sleep(0.4)

def tg_send_photo(chat_id, photo_url, caption, parse_mode="HTML"):
    """إرسال صورة (غلاف الرواية) مع caption"""
    try:
        r = requests.post(
            f"{TG_API}/sendPhoto",
            json={
                "chat_id":    chat_id,
                "photo":      photo_url,
                "caption":    caption,
                "parse_mode": parse_mode,
            },
            timeout=20,
        )
        d = r.json()
        if not d.get("ok"):
            err = d.get("description", "")
            logging.warning(f"TG photo [{chat_id}]: {err}")
            if any(w in err for w in ["blocked", "not found", "kicked", "deactivated"]):
                remove_channel(chat_id)
            # fallback: أرسل نصاً عادياً إذا فشلت الصورة
            tg_send(chat_id, caption, parse_mode)
        return d.get("ok", False)
    except Exception as ex:
        logging.warning(f"tg_send_photo: {ex}")
        tg_send(chat_id, caption, parse_mode)
        return False

def broadcast_photo(photo_url, caption, parse_mode="HTML"):
    for cid in get_channels():
        tg_send_photo(cid, photo_url, caption, parse_mode)
        time.sleep(0.5)

def _progress_bar(done, total, width=10):
    pct  = int(done / total * 100) if total else 0
    fill = int(done / total * width) if total else 0
    return "█" * fill + "░" * (width - fill), pct

# ─────────────────────────────────────────────────────
# اختيار رواية اليوم وإعداد الدفعات
# ─────────────────────────────────────────────────────
_novel_lock = threading.Lock()

def select_daily_novel(force=False):
    """
    يختار رواية اليوم:
    - إذا كانت رواية اليوم موجودة فعلاً → يعود فوراً
    - يتصل بـ Gutendex، يُنزّل النص، يُقسّمه، يُخزّنه
    - يُرسل رسالة تمهيدية ثم يبدأ الدفعة الأولى
    """
    with _novel_lock:
        # تحقق إذا كانت رواية اليوم موجودة
        if not force and get_today_novel():
            logging.info("📚 رواية اليوم محمّلة مسبقاً")
            return

        logging.info("🎲 اختيار رواية جديدة من Gutendex...")
        novel_meta = pick_novel_from_gutendex()
        if not novel_meta:
            logging.warning("❌ لم يُعثر على رواية متاحة")
            broadcast("⚠️ لم يتمكن البوت من اختيار رواية اليوم. يُحاول مجدداً لاحقاً.")
            return

        logging.info(f"⬇️ تحميل نص: {novel_meta['title']}")
        raw_text = download_text(novel_meta["text_url"])
        if not raw_text or len(raw_text) < 2000:
            logging.warning(f"❌ النص قصير جداً ({len(raw_text)} حرف)")
            return

        # تقييد الحجم الأقصى
        if len(raw_text) > NOVEL_MAX:
            cut = raw_text.rfind("\n\n", 0, NOVEL_MAX)
            raw_text = raw_text[: cut if cut > NOVEL_MAX * 0.8 else NOVEL_MAX]
            logging.info(f"✂️  مقطوع عند {len(raw_text):,} حرف")

        batches   = split_batches(raw_text)
        total     = len(batches)
        logging.info(f"📦 {total} دفعة × ~{BATCH_SIZE} حرف")

        # ترجمة عنوان ومؤلف ومقتطف تمهيدي
        title_ar  = translate_batch(novel_meta["title"][:200]) or novel_meta["title"]
        author_ar = translate_batch(novel_meta["author"][:120]) if novel_meta["author"] else ""
        summ_ar   = translate_batch(novel_meta["summary"][:600]) if novel_meta["summary"] else ""

        # تخزين في قاعدة البيانات
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO novels(gutenberg_id,title,author,summary,total_batches,selected_date,status)"
                " VALUES(?,?,?,?,?,?,'active')",
                (
                    novel_meta["gutenberg_id"],
                    novel_meta["title"],
                    novel_meta["author"],
                    novel_meta["summary"],
                    total,
                    today_str(),
                ),
            )
            novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            for i, batch_text in enumerate(batches, 1):
                conn.execute(
                    "INSERT INTO batches(novel_id,batch_num,text_en,status) VALUES(?,?,?,'pending')",
                    (novel_id, i, batch_text),
                )
            conn.commit()
            conn.close()
            logging.info(f"✅ رواية #{novel_id} مُحفوظة")
        except Exception as ex:
            logging.error(f"DB insert: {ex}")
            return

        # رسالة تمهيدية
        today     = today_str()
        intro_msg = (
            f"📚 <b>رواية اليوم</b>  —  {today}\n"
            f"{'━'*22}\n\n"
            f"📖 <b>{title_ar}</b>\n"
            f"✍️ <i>{author_ar or novel_meta['author']}</i>\n\n"
            f"{summ_ar or ''}\n\n"
            f"{'─'*22}\n"
            f"📦 إجمالي الدفعات: <b>{total}</b>\n"
            f"📝 كل دفعة: <b>{BATCH_SIZE:,}</b> حرف إنجليزي\n"
            f"⏱️  فاصل بين الدفعات: <b>دقيقة</b>\n\n"
            f"🔜 <i>تبدأ الدفعة الأولى الآن…</i>"
        )
        # إرسال الغلاف مع الرسالة التمهيدية
        cover_url = novel_meta.get("cover_url", "")
        if cover_url:
            logging.info(f"🖼️  إرسال الغلاف: {cover_url}")
            broadcast_photo(cover_url, intro_msg)
        else:
            broadcast(intro_msg)
        time.sleep(3)

        # إرسال الدفعة الأولى فوراً
        _send_batch(novel_id, total)

# ─────────────────────────────────────────────────────
# إرسال دفعة واحدة
# ─────────────────────────────────────────────────────
def _send_batch(novel_id, total_batches):
    """
    يترجم الدفعة التالية ويرسلها.
    يُعيد True إذا بقيت دفعات، وFalse إذا اكتملت.
    """
    batch = get_next_batch(novel_id)
    if not batch:
        logging.info(f"🏁 رواية #{novel_id}: لا دفعات متبقية")
        return False

    bn      = batch["batch_num"]
    text_en = batch["text_en"]

    logging.info(f"  🌐 ترجمة دفعة {bn}/{total_batches}  ({len(text_en)} حرف)…")
    text_ar = translate_batch(text_en)

    if not text_ar:
        text_ar = f"⚠️ [تعذّرت الترجمة — النص الأصلي]\n\n{text_en[:500]}…"

    # جلب معلومات الرواية
    try:
        conn      = sqlite3.connect(DB_PATH)
        row       = conn.execute(
            "SELECT title, author FROM novels WHERE id=?", (novel_id,)
        ).fetchone()
        conn.close()
        title_en  = row[0] if row else "Novel"
        author_en = row[1] if row else ""
    except:
        title_en  = "Novel"
        author_en = ""

    title_ar_cached = translate_batch(title_en[:200]) or title_en
    bar, pct  = _progress_bar(bn, total_batches)
    is_last   = (bn == total_batches)

    # صياغة رسالة الدفعة
    header = (
        f"📖 <b>{title_ar_cached}</b>\n"
        f"📦 الدفعة <b>{bn}</b> من <b>{total_batches}</b>  "
        f"[{bar}] {pct}%\n"
        f"{'─'*28}\n\n"
    )
    message = header + text_ar

    # رسالة الإتمام في نهاية الدفعة الأخيرة
    if is_last:
        message += (
            f"\n\n{'═'*25}\n"
            f"✅ <b>اكتملت ترجمة الرواية كاملةً</b>\n"
            f"🎉 شكراً لمتابعتكم!\n"
            f"📅 <b>رواية جديدة غداً</b>  —  {_next_day_str()}"
        )

    broadcast(message)
    mark_batch_sent(novel_id, bn, text_ar)
    sent = count_sent(novel_id)
    logging.info(f"  ✅ دفعة {bn}/{total_batches} أُرسلت  |  مكتمل: {sent}")

    if is_last:
        mark_novel_done(novel_id)
        logging.info(f"🎊 رواية #{novel_id} اكتملت!")
        return False

    return True   # دفعات متبقية

def _next_day_str():
    tomorrow = datetime.now(MECCA_TZ) + timedelta(days=1)
    return tomorrow.strftime("%Y-%m-%d")

# ─────────────────────────────────────────────────────
# مجدوِل الدفعات
# ─────────────────────────────────────────────────────
_batch_timer = None

def _schedule_next_batch(novel_id, total):
    """يجدول إرسال الدفعة التالية بعد BATCH_DELAY ثانية"""
    global _batch_timer
    if _batch_timer:
        _batch_timer.cancel()

    def _job():
        logging.info(f"⏰ إرسال دفعة مجدولة: رواية #{novel_id}")
        more = _send_batch(novel_id, total)
        if more:
            _schedule_next_batch(novel_id, total)

    _batch_timer = threading.Timer(BATCH_DELAY, _job)
    _batch_timer.daemon = True
    _batch_timer.start()
    mins = BATCH_DELAY // 60
    logging.info(f"⏳ الدفعة التالية خلال {mins} دقيقة")

# ─────────────────────────────────────────────────────
# حلقة اليومية: تُختار رواية جديدة كل يوم
# ─────────────────────────────────────────────────────
def daily_loop():
    last_date = ""
    while True:
        try:
            today = today_str()
            if today != last_date:
                last_date = today
                logging.info(f"🌅 يوم جديد: {today}")
                time.sleep(5)

                novel = get_today_novel()
                if not novel:
                    select_daily_novel()
                    novel = get_today_novel()

                # إذا بقيت دفعات → جدول المتابعة
                if novel:
                    nb = get_next_batch(novel["id"])
                    if nb:
                        _schedule_next_batch(novel["id"], novel["total_batches"])
        except Exception as ex:
            logging.error(f"daily_loop: {ex}")
        time.sleep(60)

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
    # ── إضافة/إزالة قناة ─────────────────────────────
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
                "📚 <b>مرحباً في بوت ترجمة الروايات الكلاسيكية!</b>\n\n"
                "🎯 <b>كيف يعمل؟</b>\n"
                "• كل يوم تُختار رواية من <b>Project Gutenberg</b>\n"
                "• يُترجم نصها كاملاً إلى العربية\n"
                "• <b>دفعة 3000 حرف</b> كل ساعة تلقائياً\n"
                "• عند الاكتمال تصلك رسالة إتمام 🎉\n\n"
                "/now — ابدأ رواية اليوم فوراً\n"
                "/status — تقدّم الترجمة الحالية\n"
                "/next — أرسل الدفعة التالية الآن",
            )
        elif status in ("left", "kicked"):
            remove_channel(cid)

    if "message" not in u:
        return

    msg  = u["message"]
    text = msg.get("text", "").strip()
    chat = msg["chat"]
    cid  = chat["id"]

    if text.startswith("/start"):
        title = chat.get("title") or chat.get("first_name") or str(cid)
        add_channel(cid, title)
        tg_send(
            cid,
            "📚 <b>بوت ترجمة الروايات الكلاسيكية</b>\n"
            f"{'━'*22}\n\n"
            "📡 المصدر: <b>Gutendex API</b> (Project Gutenberg)\n"
            "📦 كل دفعة: <b>3000 حرف</b> مترجمة للعربية\n"
            "⏱️ فاصل: <b>دقيقة واحدة</b> بين دفعة وأخرى\n"
            "📅 رواية جديدة كل يوم\n\n"
            "<b>الأوامر:</b>\n"
            "/now — بدء / متابعة رواية اليوم\n"
            "/status — تقدّم الترجمة\n"
            "/next — الدفعة التالية الآن",
        )

    elif text.startswith("/now"):
        tg_send(cid, "🔄 جارٍ اختيار رواية اليوم…")
        def _job():
            select_daily_novel()
            novel = get_today_novel()
            if novel:
                nb = get_next_batch(novel["id"])
                if nb:
                    _schedule_next_batch(novel["id"], novel["total_batches"])
        threading.Thread(target=_job, daemon=True).start()

    elif text.startswith("/next"):
        novel = get_today_novel()
        if novel:
            tg_send(cid, "⏩ إرسال الدفعة التالية فوراً…")
            def _job2():
                more = _send_batch(novel["id"], novel["total_batches"])
                if more:
                    _schedule_next_batch(novel["id"], novel["total_batches"])
            threading.Thread(target=_job2, daemon=True).start()
        else:
            tg_send(cid, "📭 لا توجد رواية نشطة اليوم. أرسل /now للبدء.")

    elif text.startswith("/status"):
        novel = get_today_novel()
        if novel:
            try:
                conn  = sqlite3.connect(DB_PATH)
                sent  = conn.execute(
                    "SELECT COUNT(*) FROM batches WHERE novel_id=? AND status='sent'",
                    (novel["id"],),
                ).fetchone()[0]
                conn.close()
                total = novel["total_batches"]
                bar, pct = _progress_bar(sent, total)
                tg_send(
                    cid,
                    f"📊 <b>تقدّم الترجمة</b>\n"
                    f"{'─'*22}\n\n"
                    f"📖 {novel['title']}\n"
                    f"✍️ {novel['author']}\n\n"
                    f"[{bar}] <b>{pct}%</b>\n"
                    f"✅ مُرسل: <b>{sent}</b> / <b>{total}</b> دفعة\n"
                    f"⏳ متبقي: <b>{total-sent}</b> دفعة",
                )
            except:
                tg_send(cid, "⚠️ خطأ في قراءة البيانات")
        else:
            tg_send(cid, "📭 لا توجد رواية نشطة اليوم. أرسل /now للبدء.")

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
        conn  = sqlite3.connect(DB_PATH)
        novel = conn.execute(
            "SELECT id,title,total_batches FROM novels WHERE selected_date=?",
            (today_str(),),
        ).fetchone()
        if novel:
            sent  = conn.execute(
                "SELECT COUNT(*) FROM batches WHERE novel_id=? AND status='sent'",
                (novel[0],),
            ).fetchone()[0]
            info = f"{novel[1][:30]} ({sent}/{novel[2]})"
        else:
            info = "لا توجد رواية اليوم"
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        conn.close()
        return f"📚 Novel Bot v11.0 | قنوات: {chats} | {info}"
    except:
        return "📚 Novel Bot v11.0"

@app.route("/health")
def health():
    return "OK", 200

@app.route("/add/<int:chat_id>")
def add_manual(chat_id):
    add_channel(chat_id, f"manual-{chat_id}")
    return json.dumps({"ok": True, "chat_id": chat_id}), 200, {
        "Content-Type": "application/json"
    }

@app.route("/start-novel")
def start_novel_ep():
    threading.Thread(target=select_daily_novel, daemon=True).start()
    return "🔄 جارٍ اختيار رواية اليوم…", 200

@app.route("/next-batch")
def next_batch_ep():
    novel = get_today_novel()
    if not novel:
        return "❌ لا رواية نشطة", 404

    def _job():
        more = _send_batch(novel["id"], novel["total_batches"])
        if more:
            _schedule_next_batch(novel["id"], novel["total_batches"])

    threading.Thread(target=_job, daemon=True).start()
    return "⏩ جارٍ إرسال الدفعة التالية…", 200

@app.route("/status")
def status_ep():
    try:
        conn   = sqlite3.connect(DB_PATH)
        novels = conn.execute(
            "SELECT id,title,author,total_batches,status,selected_date "
            "FROM novels ORDER BY id DESC LIMIT 7"
        ).fetchall()
        result = []
        for n in novels:
            sent = conn.execute(
                "SELECT COUNT(*) FROM batches WHERE novel_id=? AND status='sent'",
                (n[0],),
            ).fetchone()[0]
            result.append(
                {
                    "id": n[0], "title": n[1], "author": n[2],
                    "total": n[3], "sent": sent,
                    "status": n[4], "date": n[5],
                }
            )
        chats = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        conn.close()
        return (
            json.dumps(
                {
                    "version": "11.0",
                    "today":    today_str(),
                    "channels": chats,
                    "batch_size":  BATCH_SIZE,
                    "batch_delay": BATCH_DELAY,
                    "novels":      result,
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
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM novels")
        conn.execute("DELETE FROM batches")
        conn.commit()
        conn.close()
        return json.dumps({"ok": True, "msg": "DB cleared"}), 200, {
            "Content-Type": "application/json"
        }
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
    threading.Thread(target=tg_poll,    daemon=True, name="poll").start()
    threading.Thread(target=daily_loop, daemon=True, name="daily").start()
    threading.Thread(target=self_ping,  daemon=True, name="ping").start()
    logging.info(
        "🚀 Novel Bot v11.0 | Gutendex فقط | "
        f"دفعات {BATCH_SIZE} حرف | فاصل {BATCH_DELAY//60} دق"
    )

threading.Thread(target=_startup, daemon=True, name="startup").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
