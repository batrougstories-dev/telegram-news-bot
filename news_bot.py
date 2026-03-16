#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت أخبار عاجلة مترجمة لتليجرام
"""

import feedparser
import sqlite3
import logging
import re
import html
import os
import asyncio
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from deep_translator import GoogleTranslator
import requests
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ═══════════════════════════════════════
#           ⚙️ الإعدادات
# ═══════════════════════════════════════
BOT_TOKEN        = "8638837552:AAH4bOAipi9ipV336iYHblL-sMKh91WrD1M"
CHECK_EVERY      = 10
MAX_NEWS_PER_RUN = 1
DB_FILE          = "news_bot.db"
PORT             = int(os.environ.get("PORT", 10000))

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(message)s",
    datefmt = "%H:%M:%S"
)

# ═══════════════════════════════════════
#   🌐 Flask Server
# ═══════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ بوت الأخبار يعمل", 200

@flask_app.route("/health")
def health():
    return "OK", 200

# ═══════════════════════════════════════
#        📰 مصادر الأخبار
# ═══════════════════════════════════════
NEWS_SOURCES = [
    {"name": "BBC News",           "rss": "http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera English", "rss": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "The Guardian",       "rss": "https://www.theguardian.com/world/rss"},
    {"name": "Axios",              "rss": "https://api.axios.com/feed/"},
    {"name": "Reuters",            "rss": "https://news.google.com/rss/search?q=reuters+breaking+news&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Associated Press",   "rss": "https://news.google.com/rss/search?q=associated+press+breaking&hl=en-US&gl=US&ceid=US:en"},
]

# ═══════════════════════════════════════
#        🗂️ قاعدة البيانات
# ═══════════════════════════════════════
def init_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_news (
        news_url TEXT UNIQUE NOT NULL, title_ar TEXT,
        source TEXT, sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY, chat_title TEXT,
        chat_type TEXT, active INTEGER DEFAULT 1
    )""")
    conn.execute("DELETE FROM sent_news WHERE sent_at < datetime('now', '-7 days')")
    conn.commit()
    conn.close()
    logging.info("✅ DB جاهز")

def add_chat(chat_id, title, chat_type):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""INSERT INTO chats (chat_id,chat_title,chat_type,active) VALUES (?,?,?,1)
        ON CONFLICT(chat_id) DO UPDATE SET chat_title=excluded.chat_title,active=1
    """, (chat_id, title, chat_type))
    conn.commit(); conn.close()
    logging.info(f"➕ {title}")

def remove_chat(chat_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

def get_all_chats():
    conn  = sqlite3.connect(DB_FILE)
    chats = conn.execute("SELECT chat_id,chat_title FROM chats WHERE active=1").fetchall()
    conn.close(); return chats

def is_sent(url):
    conn = sqlite3.connect(DB_FILE)
    r    = conn.execute("SELECT 1 FROM sent_news WHERE news_url=?", (url,)).fetchone()
    conn.close(); return r is not None

def mark_sent(url, title_ar, source):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("INSERT INTO sent_news VALUES (?,?,?,CURRENT_TIMESTAMP)", (url,title_ar,source))
        conn.commit()
    except: pass
    conn.close()

# ═══════════════════════════════════════
#        🌐 جلب الأخبار
# ═══════════════════════════════════════
def clean_text(t):
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r'<[^>]+>', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def fetch_rss(source):
    items = []
    try:
        r    = requests.get(source["rss"], headers={'User-Agent':'Mozilla/5.0'}, timeout=15)
        feed = feedparser.parse(r.content)
        for e in feed.entries[:10]:
            url   = getattr(e,'link','')
            title = clean_text(getattr(e,'title',''))
            if url and title and not is_sent(url):
                items.append({"url":url,"title":title,"source":source["name"]})
    except Exception as ex:
        logging.warning(f"⚠️ {source['name']}: {ex}")
    return items

# ═══════════════════════════════════════
#    🤖 الترجمة
# ═══════════════════════════════════════
translator = GoogleTranslator(source='en', target='ar')

def translate(text):
    if not text: return text
    try:
        r = translator.translate(text[:4000])
        return r if r else text
    except: return text

def format_message(title_ar, source):
    return f"<b>{title_ar}</b>\n\n— {source}"

# ═══════════════════════════════════════
#    🔄 دورة الأخبار
# ═══════════════════════════════════════
def news_cycle():
    logging.info("🔍 فحص الأخبار...")
    try:
        chats = get_all_chats()
        if not chats:
            logging.warning("⚠️ لا توجد قنوات")
            return

        all_news = []
        for source in NEWS_SOURCES:
            all_news.extend(fetch_rss(source))

        if not all_news:
            logging.info("ℹ️  لا أخبار جديدة")
            return

        async def send():
            bot = Bot(token=BOT_TOKEN)
            for news in all_news[:MAX_NEWS_PER_RUN]:
                title_ar = translate(news["title"])
                msg      = format_message(title_ar, news["source"])
                for chat_id, chat_title in chats:
                    try:
                        await bot.send_message(
                            chat_id=chat_id, text=msg,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True
                        )
                        logging.info(f"✅ → {chat_title}")
                    except TelegramError as te:
                        logging.error(f"❌ {te}")
                        if any(x in str(te).lower() for x in ["kicked","chat not found","blocked"]):
                            remove_chat(chat_id)
                    await asyncio.sleep(1)
                mark_sent(news["url"], title_ar, news["source"])
                logging.info(f"📰 {title_ar[:60]}")

        asyncio.run(send())

    except Exception as e:
        logging.error(f"❌ news_cycle: {e}")

# ═══════════════════════════════════════
#    🚀 التشغيل
# ═══════════════════════════════════════
def discover_chats():
    """اكتشاف القنوات الموجودة من getUpdates"""
    async def _get():
        bot     = Bot(token=BOT_TOKEN)
        updates = await bot.get_updates(limit=100)
        for u in updates:
            if u.my_chat_member:
                c  = u.my_chat_member.chat
                st = u.my_chat_member.new_chat_member.status
                if st in ("administrator","member"):
                    add_chat(c.id, c.title or "بدون اسم", c.type)
            if u.channel_post:
                c = u.channel_post.chat
                add_chat(c.id, c.title or "بدون اسم", "channel")
    try:
        asyncio.run(_get())
    except Exception as e:
        logging.warning(f"⚠️ discover: {e}")

# تهيئة DB
init_database()

# اكتشاف القنوات
discover_chats()

chats = get_all_chats()
logging.info(f"📋 القنوات: {len(chats)}")
for cid, title in chats:
    logging.info(f"   • {title} ({cid})")

# أول دورة فوراً
news_cycle()

# جدولة الدورات كل 10 دقائق
scheduler = BackgroundScheduler()
scheduler.add_job(news_cycle, 'interval', minutes=CHECK_EVERY)
scheduler.start()
logging.info(f"⏱️  جدول يعمل — خبر كل {CHECK_EVERY} دقيقة")

# Flask في main thread (لـ Render health check)
if __name__ == "__main__":
    logging.info(f"🌐 Flask يعمل على port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)
