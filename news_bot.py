#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║         بوت أخبار عاجلة — الشرق الأوسط أولاً               ║
║  Reuters | AP | BBC | Al Jazeera | Guardian | Axios          ║
╚══════════════════════════════════════════════════════════════╝

الميزات:
  ✅ أخبار الشرق الأوسط بأولوية قصوى
  ✅ إرسال الأخبار العاجلة فور حدوثها (كل 3 دقائق)
  ✅ نظام نقاط ذكي لتصنيف الأخبار
  ✅ Self-ping لمنع نوم Render
  ✅ يرسل تلقائياً لأي قناة/مجموعة يُضاف إليها
"""

import asyncio
import feedparser
import sqlite3
import logging
import re
import html
import os
import time
import threading
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from deep_translator import GoogleTranslator
import requests
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, ContextTypes

# ════════════════════════════════════════
#           ⚙️ الإعدادات
# ════════════════════════════════════════
BOT_TOKEN       = "8638837552:AAH4bOAipi9ipV336iYHblL-sMKh91WrD1M"
SERVICE_URL     = "https://telegram-news-bot-zcn8.onrender.com"
DB_FILE         = "news_bot.db"
PORT            = int(os.environ.get("PORT", 10000))

BREAKING_EVERY  = 3    # دقائق — فحص الأخبار العاجلة
REGULAR_EVERY   = 15   # دقائق — خبر عادي
PING_EVERY      = 5    # دقائق — self-ping لمنع النوم

# ════════════════════════════════════════
#   📍 كلمات الشرق الأوسط (فلتر ذكي)
# ════════════════════════════════════════
MIDDLE_EAST_KEYWORDS = [
    # الدول
    "saudi", "uae", "iran", "iraq", "syria", "yemen", "lebanon",
    "palestine", "israel", "jordan", "egypt", "kuwait", "qatar",
    "bahrain", "oman", "turkey", "libya", "tunisia", "algeria",
    "morocco", "sudan",
    # المدن
    "riyadh", "dubai", "abu dhabi", "cairo", "beirut", "damascus",
    "baghdad", "tehran", "amman", "doha", "muscat", "sanaa",
    "tripoli", "jerusalem", "tel aviv", "gaza", "ramallah",
    "mosul", "aleppo", "idlib",
    # المصطلحات
    "arab", "middle east", "persian gulf", "red sea", "suez",
    "hamas", "hezbollah", "houthi", "idf", "irgc", "isis", "isil",
    "plo", "fatah", "west bank", "golan", "sinai", "tigris",
    "euphrates", "opec", "aramco",
    # الشخصيات
    "netanyahu", "erdogan", "mbs", "sisi", "khamenei",
    "nasrallah", "haniyeh", "bin salman",
    # الأحداث الشائعة
    "ceasefire", "airstrike", "missile", "drone strike",
    "oil price", "natural gas", "nuclear deal",
]

# ════════════════════════════════════════
#   🚨 كلمات الأخبار العاجلة
# ════════════════════════════════════════
BREAKING_KEYWORDS = [
    "breaking", "urgent", "alert", "just in", "flash",
    "developing", "emergency", "explosion", "attack", "killed",
    "war", "ceasefire", "strike", "bomb", "missile", "assassination",
    "coup", "invaded", "crisis", "conflict", "shoot", "fire",
    "casualties", "dead", "arrested", "detained", "collapsed",
    "crash", "disaster", "earthquake", "flood",
]

# ════════════════════════════════════════
#        📰 مصادر الأخبار RSS
# ════════════════════════════════════════
NEWS_SOURCES = [
    # ── المصادر المتخصصة بالشرق الأوسط (أولوية قصوى) ──
    {
        "name":     "Al Jazeera",
        "priority": "high",
        "rss":      "https://www.aljazeera.com/xml/rss/all.xml",
    },
    {
        "name":     "BBC Middle East",
        "priority": "high",
        "rss":      "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    },
    {
        "name":     "Reuters — Middle East",
        "priority": "high",
        "rss":      "https://news.google.com/rss/search?q=reuters+middle+east+breaking&hl=en-US&gl=US&ceid=US:en",
    },
    {
        "name":     "AP — Middle East",
        "priority": "high",
        "rss":      "https://news.google.com/rss/search?q=associated+press+middle+east&hl=en-US&gl=US&ceid=US:en",
    },
    # ── المصادر العامة (أولوية عادية) ──
    {
        "name":     "BBC News",
        "priority": "normal",
        "rss":      "http://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "name":     "The Guardian",
        "priority": "normal",
        "rss":      "https://www.theguardian.com/world/middleeast/rss",
    },
    {
        "name":     "Axios",
        "priority": "high",
        "rss":      "https://api.axios.com/feed/",
    },
    {
        "name":     "Reuters — World",
        "priority": "normal",
        "rss":      "https://news.google.com/rss/search?q=reuters+breaking+news&hl=en-US&gl=US&ceid=US:en",
    },
    # ── Bloomberg ──
    {
        "name":     "Bloomberg",
        "priority": "high",
        "rss":      "https://feeds.bloomberg.com/markets/news.rss",
    },
    {
        "name":     "Bloomberg Politics",
        "priority": "high",
        "rss":      "https://feeds.bloomberg.com/politics/news.rss",
    },
    {
        "name":     "Bloomberg — Middle East",
        "priority": "high",
        "rss":      "https://news.google.com/rss/search?q=bloomberg+middle+east&hl=en-US&gl=US&ceid=US:en",
    },
]

# ════════════════════════════════════════
#   🌐 Flask Server (لـ Render)
# ════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    chats = get_all_chats()
    return f"✅ بوت الأخبار يعمل | القنوات: {len(chats)}", 200

@flask_app.route("/health")
def health():
    return "OK", 200

# ════════════════════════════════════════
#        🗂️ قاعدة البيانات
# ════════════════════════════════════════
def init_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_news (
            news_url  TEXT UNIQUE NOT NULL,
            title_ar  TEXT,
            source    TEXT,
            is_breaking INTEGER DEFAULT 0,
            sent_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id    INTEGER PRIMARY KEY,
            chat_title TEXT,
            chat_type  TEXT,
            active     INTEGER DEFAULT 1,
            added_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # تنظيف الأخبار القديمة (+7 أيام)
    conn.execute("DELETE FROM sent_news WHERE sent_at < datetime('now', '-7 days')")
    conn.commit()
    conn.close()
    logging.info("✅ DB جاهز")

def add_chat(chat_id, title, chat_type):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO chats (chat_id, chat_title, chat_type, active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET
            chat_title = excluded.chat_title, active = 1
    """, (chat_id, title, chat_type))
    conn.commit()
    conn.close()
    logging.info(f"➕ قناة جديدة: {title}")

def remove_chat(chat_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def get_all_chats():
    conn  = sqlite3.connect(DB_FILE)
    rows  = conn.execute("SELECT chat_id, chat_title FROM chats WHERE active=1").fetchall()
    conn.close()
    return rows

def is_sent(url: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    r    = conn.execute("SELECT 1 FROM sent_news WHERE news_url=?", (url,)).fetchone()
    conn.close()
    return r is not None

def mark_sent(url, title_ar, source, is_breaking=0):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            "INSERT INTO sent_news VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
            (url, title_ar, source, is_breaking)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

# ════════════════════════════════════════
#   🤖 اكتشاف القنوات تلقائياً
# ════════════════════════════════════════
async def on_bot_status_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    chat   = result.chat
    status = result.new_chat_member.status

    if status in ("administrator", "member"):
        add_chat(chat.id, chat.title or "بدون اسم", chat.type)
        try:
            await context.bot.send_message(
                chat_id    = chat.id,
                text       = (
                    "بوت أخبار الشرق الأوسط العاجلة يعمل الآن.\n\n"
                    "سيصلكم:\n"
                    "• الأخبار العاجلة فور حدوثها\n"
                    "• تركيز على أخبار الشرق الأوسط\n"
                    "• ترجمة فورية للعربية"
                ),
                parse_mode = ParseMode.HTML
            )
        except Exception:
            pass
    elif status in ("kicked", "left"):
        remove_chat(chat.id)
        logging.info(f"➖ أُزيل البوت من: {chat.title}")

# ════════════════════════════════════════
#   📊 نظام النقاط لتصنيف الأخبار
# ════════════════════════════════════════
def score_news(title: str, source_priority: str) -> dict:
    """
    يعطي كل خبر نقاط لتحديد:
    - هل هو متعلق بالشرق الأوسط؟
    - هل هو عاجل؟
    """
    title_lower  = title.lower()
    me_score     = 0
    break_score  = 0

    for kw in MIDDLE_EAST_KEYWORDS:
        if kw in title_lower:
            me_score += 10

    for kw in BREAKING_KEYWORDS:
        if kw in title_lower:
            break_score += 20

    # أولوية المصدر
    if source_priority == "high":
        me_score    += 15

    is_middle_east = me_score  >= 10
    is_breaking    = break_score >= 20

    return {
        "me_score":      me_score,
        "break_score":   break_score,
        "is_middle_east": is_middle_east,
        "is_breaking":    is_breaking,
        "total":          me_score + break_score,
    }

# ════════════════════════════════════════
#        🌐 جلب الأخبار من RSS
# ════════════════════════════════════════
def clean_text(t: str) -> str:
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r'<[^>]+>', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def fetch_rss(source: dict) -> list:
    items   = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r    = requests.get(source["rss"], headers=headers, timeout=15)
        feed = feedparser.parse(r.content)
        for e in feed.entries[:15]:
            url   = getattr(e, "link",  "")
            title = clean_text(getattr(e, "title", ""))
            if not url or not title or is_sent(url):
                continue
            scores = score_news(title, source["priority"])
            items.append({
                "url":      url,
                "title":    title,
                "source":   source["name"],
                "priority": source["priority"],
                **scores,
            })
    except Exception as ex:
        logging.warning(f"⚠️ {source['name']}: {ex}")
    return items

# ════════════════════════════════════════
#    🤖 الترجمة
# ════════════════════════════════════════
_translator = GoogleTranslator(source="en", target="ar")
_trans_lock  = threading.Lock()

def translate(text: str) -> str:
    if not text: return text
    with _trans_lock:
        try:
            r = _translator.translate(text[:4000])
            return r if r else text
        except Exception as ex:
            logging.warning(f"⚠️ ترجمة: {ex}")
            return text

# ════════════════════════════════════════
#    📢 تنسيق الرسائل
# ════════════════════════════════════════
def format_breaking(title_ar: str, source: str) -> str:
    """تنسيق خبر عاجل"""
    return f"🔴 <b>عاجل</b>\n\n<b>{title_ar}</b>\n\n— {source}"

def format_regular(title_ar: str, source: str) -> str:
    """تنسيق خبر عادي"""
    return f"<b>{title_ar}</b>\n\n— {source}"

# ════════════════════════════════════════
#    📤 إرسال الأخبار
# ════════════════════════════════════════
async def broadcast(news_items: list):
    """إرسال قائمة أخبار لكل القنوات"""
    chats = get_all_chats()
    if not chats:
        return

    bot = Bot(token=BOT_TOKEN)

    for news in news_items:
        try:
            title_ar = translate(news["title"])
            msg      = (
                format_breaking(title_ar, news["source"])
                if news["is_breaking"]
                else format_regular(title_ar, news["source"])
            )

            for chat_id, chat_title in chats:
                try:
                    await bot.send_message(
                        chat_id                  = chat_id,
                        text                     = msg,
                        parse_mode               = ParseMode.HTML,
                        disable_web_page_preview = True,
                    )
                    logging.info(f"   ✅ → {chat_title}")
                except TelegramError as te:
                    logging.error(f"   ❌ → {chat_title}: {te}")
                    if any(x in str(te).lower() for x in ["kicked", "chat not found", "blocked"]):
                        remove_chat(chat_id)
                await asyncio.sleep(0.5)

            mark_sent(news["url"], title_ar, news["source"], int(news["is_breaking"]))
            tag = "🔴 عاجل" if news["is_breaking"] else "📰"
            logging.info(f"{tag} {title_ar[:65]}")
            await asyncio.sleep(2)

        except Exception as e:
            logging.error(f"❌ broadcast: {e}")

# ════════════════════════════════════════
#   🚨 دورة الأخبار العاجلة (كل 3 دقائق)
# ════════════════════════════════════════
def breaking_news_cycle():
    """
    يفحص كل 3 دقائق ويرسل فوراً:
    - أخبار عاجلة من الشرق الأوسط
    - أخبار عاجلة جداً من أي مصدر
    """
    try:
        all_news = []
        for source in NEWS_SOURCES:
            all_news.extend(fetch_rss(source))

        # فلتر: فقط العاجلة المتعلقة بالشرق الأوسط
        urgent = [
            n for n in all_news
            if n["is_breaking"] and n["is_middle_east"]
        ]

        # أو عاجلة جداً من أي مكان (break_score عالي جداً)
        very_urgent = [
            n for n in all_news
            if n["break_score"] >= 40 and n not in urgent
        ]

        to_send = urgent + very_urgent

        if not to_send:
            return

        # ترتيب حسب النقاط (الأعلى أولاً)
        to_send.sort(key=lambda x: x["total"], reverse=True)

        logging.info(f"🚨 أخبار عاجلة: {len(to_send)}")
        asyncio.run(broadcast(to_send[:3]))  # أقصى 3 في كل دورة

    except Exception as e:
        logging.error(f"❌ breaking_cycle: {e}")

# ════════════════════════════════════════
#   📰 دورة الأخبار العادية (كل 15 دقيقة)
# ════════════════════════════════════════
def regular_news_cycle():
    """
    يرسل كل 15 دقيقة أفضل خبر من الشرق الأوسط
    """
    try:
        all_news = []
        for source in NEWS_SOURCES:
            all_news.extend(fetch_rss(source))

        if not all_news:
            logging.info("ℹ️ لا أخبار جديدة")
            return

        # أولوية: الشرق الأوسط أولاً، ثم الأعلى نقاطاً
        me_news      = [n for n in all_news if n["is_middle_east"]]
        other_news   = [n for n in all_news if not n["is_middle_east"]]

        me_news.sort(key=lambda x: x["total"], reverse=True)
        other_news.sort(key=lambda x: x["total"], reverse=True)

        # اختر أفضل خبر
        pool    = me_news if me_news else other_news
        to_send = [pool[0]] if pool else []

        if to_send:
            logging.info(f"📰 خبر عادي: {to_send[0]['title'][:50]}")
            asyncio.run(broadcast(to_send))

    except Exception as e:
        logging.error(f"❌ regular_cycle: {e}")

# ════════════════════════════════════════
#   🔄 Self-Ping (يمنع نوم Render)
# ════════════════════════════════════════
def self_ping():
    try:
        r = requests.get(f"{SERVICE_URL}/health", timeout=10)
        if r.status_code == 200:
            logging.info("🔄 ping OK")
    except Exception as e:
        logging.warning(f"⚠️ ping: {e}")

# ════════════════════════════════════════
#   🔍 اكتشاف القنوات من التحديثات
# ════════════════════════════════════════
def discover_chats():
    async def _run():
        bot     = Bot(token=BOT_TOKEN)
        updates = await bot.get_updates(limit=100)
        for u in updates:
            if u.my_chat_member:
                c  = u.my_chat_member.chat
                st = u.my_chat_member.new_chat_member.status
                if st in ("administrator", "member"):
                    add_chat(c.id, c.title or "بدون اسم", c.type)
            if u.channel_post:
                c = u.channel_post.chat
                add_chat(c.id, c.title or "بدون اسم", "channel")
    try:
        asyncio.run(_run())
    except Exception as e:
        logging.warning(f"⚠️ discover: {e}")

# ════════════════════════════════════════
#   🤖 Polling لاكتشاف القنوات الجديدة
# ════════════════════════════════════════
def start_polling():
    async def _poll():
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(ChatMemberHandler(on_bot_status_change, ChatMemberHandler.MY_CHAT_MEMBER))
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=False)
            # يبقى شغّالاً
            while True:
                await asyncio.sleep(60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_forever() if False else loop.run_until_complete(_poll())

# ════════════════════════════════════════
#    🚀 التشغيل الرئيسي
# ════════════════════════════════════════
# إعداد اللوجز
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(message)s",
    datefmt = "%H:%M:%S",
)

# تهيئة DB
init_database()

# اكتشاف القنوات الموجودة
discover_chats()

chats = get_all_chats()
logging.info("═" * 50)
logging.info("  🚀 بوت أخبار الشرق الأوسط العاجلة")
logging.info("═" * 50)
logging.info(f"  📋 القنوات  : {len(chats)}")
for cid, title in chats:
    logging.info(f"     • {title} ({cid})")
logging.info(f"  🚨 عاجل كل  : {BREAKING_EVERY} دقيقة")
logging.info(f"  📰 عادي كل  : {REGULAR_EVERY} دقيقة")
logging.info(f"  🔄 ping كل  : {PING_EVERY} دقيقة")
logging.info("═" * 50)

# ── أول تشغيل فوري ──
logging.info("▶️ الدورة الأولى...")
breaking_news_cycle()
regular_news_cycle()

# ── جدولة الدورات ──
scheduler = BackgroundScheduler(timezone="Asia/Riyadh")
scheduler.add_job(breaking_news_cycle, "interval", minutes=BREAKING_EVERY,  id="breaking")
scheduler.add_job(regular_news_cycle,  "interval", minutes=REGULAR_EVERY,   id="regular")
scheduler.add_job(self_ping,           "interval", minutes=PING_EVERY,       id="ping")
scheduler.start()
logging.info("⏰ الجدول يعمل")

# ── Polling في thread منفصل ──
poll_thread = threading.Thread(target=start_polling, daemon=True)
poll_thread.start()

# ── Flask في main thread ──
if __name__ == "__main__":
    logging.info(f"🌐 Flask على port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)
