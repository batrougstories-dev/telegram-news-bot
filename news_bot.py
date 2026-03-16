#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════
        بوت أخبار عاجلة - Telegram Breaking News Bot
═══════════════════════════════════════════════════════════════
   المصادر: Reuters | AP | BBC | Al Jazeera | Guardian | Axios
   الترجمة: Google Translate (مجاني)
   الإرسال: لكل القنوات والمجموعات تلقائياً
═══════════════════════════════════════════════════════════════
"""

import asyncio
import feedparser
import sqlite3
import logging
import re
import html
from deep_translator import GoogleTranslator
import requests
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, ContextTypes

# ═══════════════════════════════════════
#           ⚙️ الإعدادات
# ═══════════════════════════════════════
BOT_TOKEN        = "8638837552:AAH4bOAipi9ipV336iYHblL-sMKh91WrD1M"
CHECK_EVERY      = 10    # دقائق
MAX_NEWS_PER_RUN = 1
DB_FILE          = "news_bot.db"

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_news (
            news_url TEXT UNIQUE NOT NULL,
            title_ar TEXT,
            source   TEXT,
            sent_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id    INTEGER PRIMARY KEY,
            chat_title TEXT,
            chat_type  TEXT,
            active     INTEGER DEFAULT 1
        )
    """)
    conn.execute("DELETE FROM sent_news WHERE sent_at < datetime('now', '-7 days')")
    conn.commit()
    conn.close()

def add_chat(chat_id, title, chat_type):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO chats (chat_id, chat_title, chat_type, active) VALUES (?,?,?,1)
        ON CONFLICT(chat_id) DO UPDATE SET chat_title=excluded.chat_title, active=1
    """, (chat_id, title, chat_type))
    conn.commit()
    conn.close()
    logging.info(f"➕ {title} ({chat_id})")

def remove_chat(chat_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def get_all_chats():
    conn  = sqlite3.connect(DB_FILE)
    chats = conn.execute("SELECT chat_id, chat_title FROM chats WHERE active=1").fetchall()
    conn.close()
    return chats

def is_sent(url):
    conn = sqlite3.connect(DB_FILE)
    r    = conn.execute("SELECT 1 FROM sent_news WHERE news_url=?", (url,)).fetchone()
    conn.close()
    return r is not None

def mark_sent(url, title_ar, source):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("INSERT INTO sent_news VALUES (?,?,?,CURRENT_TIMESTAMP)", (url, title_ar, source))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

# ═══════════════════════════════════════
#   🤖 اكتشاف القنوات تلقائياً
# ═══════════════════════════════════════
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
                text       = "بوت الأخبار العاجلة يعمل الآن.\nستصلكم أهم الأخبار العالمية مترجمة كل 10 دقائق.",
                parse_mode = ParseMode.HTML
            )
        except Exception:
            pass
    elif status in ("kicked", "left"):
        remove_chat(chat.id)

# ═══════════════════════════════════════
#        🌐 جلب الأخبار
# ═══════════════════════════════════════
def clean_text(t):
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r'<[^>]+>', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def fetch_rss(source):
    items   = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        r    = requests.get(source["rss"], headers=headers, timeout=15)
        feed = feedparser.parse(r.content)
        for e in feed.entries[:10]:
            url   = getattr(e, 'link',  '')
            title = clean_text(getattr(e, 'title', ''))
            if url and title and not is_sent(url):
                items.append({"url": url, "title": title, "source": source["name"]})
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
    except Exception as ex:
        logging.warning(f"⚠️ ترجمة: {ex}")
        return text

# ═══════════════════════════════════════
#    📢 تنسيق الرسالة
# ═══════════════════════════════════════
def format_message(title_ar, source):
    return f"<b>{title_ar}</b>\n\n— {source}"

# ═══════════════════════════════════════
#    🔄 الدورة الرئيسية
# ═══════════════════════════════════════
async def fetch_and_send(bot: Bot):
    logging.info("🔍 فحص الأخبار...")
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

    logging.info(f"📊 جديد: {len(all_news)} — يُرسل: {min(len(all_news), MAX_NEWS_PER_RUN)}")

    sent = 0
    for news in all_news[:MAX_NEWS_PER_RUN]:
        try:
            title_ar = translate(news["title"])
            msg      = format_message(title_ar, news["source"])

            for chat_id, chat_title in chats:
                try:
                    await bot.send_message(
                        chat_id                  = chat_id,
                        text                     = msg,
                        parse_mode               = ParseMode.HTML,
                        disable_web_page_preview = True
                    )
                    logging.info(f"   ✅ → {chat_title}")
                except TelegramError as te:
                    logging.error(f"   ❌ → {chat_title}: {te}")
                    if any(x in str(te).lower() for x in ["kicked", "chat not found", "blocked"]):
                        remove_chat(chat_id)
                await asyncio.sleep(1)

            mark_sent(news["url"], title_ar, news["source"])
            sent += 1
            logging.info(f"✅ [{sent}] {title_ar[:60]}")
            await asyncio.sleep(2)

        except Exception as e:
            logging.error(f"❌ {e}")

    logging.info(f"🎉 أُرسل {sent} خبر")

# ═══════════════════════════════════════
#    🚀 التشغيل
# ═══════════════════════════════════════
async def main():
    logging.basicConfig(
        level    = logging.INFO,
        format   = "%(asctime)s | %(message)s",
        datefmt  = "%H:%M:%S",
        handlers = [
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    logging.info("═" * 45)
    logging.info("  🚀 بوت الأخبار العاجلة يعمل")
    logging.info("═" * 45)

    init_database()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(ChatMemberHandler(on_bot_status_change, ChatMemberHandler.MY_CHAT_MEMBER))

    bot = Bot(token=BOT_TOKEN)

    # اكتشاف القنوات الموجودة
    try:
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
    except Exception as e:
        logging.warning(f"⚠️ {e}")

    chats = get_all_chats()
    logging.info(f"📋 القنوات: {len(chats)}")
    for cid, title in chats:
        logging.info(f"   • {title} ({cid})")
    logging.info(f"⏱️  كل {CHECK_EVERY} دقيقة")
    logging.info("═" * 45)

    await fetch_and_send(bot)

    async def loop():
        while True:
            await asyncio.sleep(CHECK_EVERY * 60)
            await fetch_and_send(bot)

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=False)
        await loop()

if __name__ == "__main__":
    asyncio.run(main())
