import requests
import sqlite3
import os
import asyncio
from bs4 import BeautifulSoup
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from pathlib import Path

ROOT_DIR = Path(__file__).absolute().parent.parent
URL = "https://www.mexc.com/announcements/new-listings/futures-19"
DB_PATH = f"{ROOT_DIR}/data/mexc_futures.db"
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SCRAPE_INTERVAL = 300  # seconds (5 minutes)
HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

FILE_PATH = f"{ROOT_DIR}/data/response.txt"

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS futures_announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT UNIQUE,
            published_at TEXT,
            scraped_at TEXT NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telegram_users (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            first_seen TEXT NOT NULL
        )
    """)
    
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)


###TG
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    username = update.effective_user.username

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO telegram_users (chat_id, username, first_seen)
        VALUES (?, ?, ?)
    """, (
        str(chat.id),
        username,
        datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ You are subscribed to MEXC Futures listings alerts!\n"
        "You will receive notifications when new futures are listed."
    )

async def notify_all(app, message: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM telegram_users")
    users = cur.fetchall()
    conn.close()

    for (chat_id,) in users:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Failed to notify {chat_id}: {e}")
            
            
###Scraper
def scrape():
    response = requests.get(URL, headers=HEADERS, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select("a")
    
    announcements = soup.find_all("div", class_="SearchResultItem_searchResultItem-community__ap55T")
    
    results = []
    
    for announcement in announcements:
        ann_title = announcement.find("div", class_="SearchResultItem_titleWrapper-community__gNJc0")
        title_text = ann_title.get_text(strip=True)
        title_timestamp = ann_title.find("time").get("datetime")
        title_link = "https://www.mexc.com" + ann_title.find("a").get("href")
        
        results.append({
            "title": title_text,
            "link": title_link,
            "published_at": title_timestamp
        })
        
    with open(FILE_PATH, "w") as f:
        for result in results:
            f.write(str(result))
    
    return results

def save_to_db(entries):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for e in entries:
        cursor.execute("""
            INSERT OR IGNORE INTO futures_announcements
            (title, link, published_at, scraped_at)
            VALUES (?, ?, ?, ?)
        """, (
            e["title"],
            e["link"],
            e["published_at"],
            datetime.utcnow().isoformat()
        ))

    conn.commit()
    conn.close()

def detect_new_announcements(entries):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    to_notify_and_save = []
    
    print("Entries:")
    print(entries)
    
    for e in entries:
        cursor.execute("SELECT EXISTS(SELECT 1 FROM futures_announcements WHERE link=? LIMIT 1)", [e["link"]])
        exists = cursor.fetchone()[0]
        
        if exists:
            print("Existing: {0}".format(e))
        else:
            to_notify_and_save.append(e)
            
    return to_notify_and_save

async def scrape_loop(app):
    try:
        entries = scrape()
        new_announcements = detect_new_announcements(entries)
        save_to_db(new_announcements)
        
        print(f"NEW: {str(new_announcements)}")
        print(f"Saved {len(new_announcements)} #Futures announcements")
        for announcement in new_announcements:
            title = announcement["title"]
            link = announcement["link"]
            
            await notify_all(
                app,
                f"🚀 <b>New MEXC Futures Listing</b>\n\n"
                f"{title}\n"
                f"<a href='{link}'>Open announcement</a>"
            )

        if new_announcements:
            print(f"Notified users about {len(new_announcements)} new listings")

    except Exception as e:
        print("Scrape error:", e)

def main():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        init_db()
        app = ApplicationBuilder().token(BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("health", health))
    except Exception as e:
        print(e)
        raise e
    # Background scraper task
    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(scrape_loop(ctx.application)),
        interval=SCRAPE_INTERVAL,
        first=5
    )

    print("🤖 Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
