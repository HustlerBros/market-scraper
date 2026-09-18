"""Public MEXC futures gainer scanner with persistent Telegram subscriptions."""

import asyncio
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
import logging
import os
from pathlib import Path
import time
from urllib.parse import quote

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(ROOT_DIR / "data/mexc_futures.db"))
API_BASE = "https://api.mexc.com/api/v1/contract"
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "60"))
HEALTH_PATH = Path("/tmp/mexc-scanner-health")
MIN_GAIN_PERCENT = Decimal(os.getenv("MIN_GAIN_PERCENT", "80"))
MIN_TURNOVER_USDT = Decimal(os.getenv("MIN_TURNOVER_USDT", "1000000"))
MIN_OI_TURNOVER_RATIO = Decimal(os.getenv("MIN_OI_TURNOVER_RATIO", "0.10"))
MAX_OI_TURNOVER_RATIO = (
    Decimal(os.environ["MAX_OI_TURNOVER_RATIO"]) if os.getenv("MAX_OI_TURNOVER_RATIO") else None
)
logger = logging.getLogger(__name__)


def get_db():
    import sqlite3
    return sqlite3.connect(DB_PATH)


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with closing(get_db()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_users (
            chat_id TEXT PRIMARY KEY, username TEXT, first_seen TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS gainer_alerts (
            chat_id TEXT NOT NULL, symbol TEXT NOT NULL, sent_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, symbol))""")


def now():
    return datetime.now(timezone.utc).isoformat()


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_db()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO telegram_users (chat_id, username, first_seen) VALUES (?, ?, ?)",
            (str(update.effective_chat.id), update.effective_user.username, now()),
        )
    await update.effective_message.reply_text(
        "✅ You are subscribed to MEXC USDT perpetual futures gainer alerts!\n"
        f"Rolling 24h gain (1-minute candles) ≥ {MIN_GAIN_PERCENT}%, turnover ≥ ${MIN_TURNOVER_USDT:,.0f}, "
        f"OI/turnover ≥ {MIN_OI_TURNOVER_RATIO:.0%}."
        + (f" Maximum OI/turnover: {MAX_OI_TURNOVER_RATIO:.0%}." if MAX_OI_TURNOVER_RATIO is not None else "")
        + "\nEach ticker is sent at most once per UTC day."
    )


def fetch_payload(endpoint, params=None):
    response = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("code") != 0:
        raise ValueError(f"MEXC {endpoint} request was unsuccessful")
    return payload.get("data")


def fetch_market_data(endpoint):
    rows = fetch_payload(endpoint)
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"MEXC {endpoint} returned an invalid or empty market snapshot")
    return rows


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite market value")
    return result


def fetch_reference_price(symbol, timestamp):
    """Approximate price 24h before the ticker with that minute's opening price."""
    timestamp = number(timestamp)
    if timestamp < 86_400_000:
        raise ValueError("Invalid ticker timestamp in milliseconds")
    target = int(timestamp / 1000) - 86_400
    minute = target // 60 * 60
    # Sequential requests stay below the documented 20 requests / 2 seconds.
    time.sleep(0.12)
    data = fetch_payload(f"kline/{quote(symbol, safe='')}",
                         {"interval": "Min1", "start": minute, "end": minute + 60})
    if not isinstance(data, dict):
        raise ValueError("Invalid candle response")
    times, opens = data.get("time"), data.get("open")
    if (not isinstance(times, list) or not isinstance(opens, list)
            or len(times) != len(opens)):
        raise ValueError("Invalid candle arrays")
    matches = [price for stamp, price in zip(times, opens) if number(stamp) == minute]
    if len(matches) != 1:
        raise ValueError("Missing or duplicate 24h reference candle")
    price = number(matches[0])
    if price <= 0:
        raise ValueError("Invalid reference price")
    return price


def select_gainers(tickers, contracts):
    """Return sorted gainers and successfully evaluated symbols for scan statistics."""
    details = {row.get("symbol"): row for row in contracts}
    gainers, evaluated = [], set()
    for ticker in tickers:
        symbol = ticker.get("symbol")
        contract = details.get(symbol)
        if not isinstance(symbol, str) or contract is None:
            continue
        if (contract.get("quoteCoin") != "USDT" or contract.get("settleCoin") != "USDT"
                or contract.get("futureType") != 1 or contract.get("state") != 0):
            evaluated.add(symbol)
            continue
        try:
            price = number(ticker["lastPrice"])
            turnover = number(ticker["amount24"])
            holdings = number(ticker["holdVol"])
            size = number(contract["contractSize"])
            funding = number(ticker["fundingRate"]) * 100
            if price <= 0 or size <= 0 or turnover < 0 or holdings < 0:
                raise ValueError("Invalid market values")
            oi = holdings * size * price
            ratio = oi / turnover if turnover else Decimal(0)
            if (turnover < MIN_TURNOVER_USDT or ratio < MIN_OI_TURNOVER_RATIO
                    or (MAX_OI_TURNOVER_RATIO is not None and ratio > MAX_OI_TURNOVER_RATIO)):
                evaluated.add(symbol)
                continue
            reference = fetch_reference_price(symbol, ticker["timestamp"])
            gain = (price / reference - 1) * 100
        except (KeyError, ValueError, TypeError, InvalidOperation, requests.RequestException):
            logger.warning("Skipping invalid market data for %s", symbol)
            continue
        evaluated.add(symbol)
        if (gain >= MIN_GAIN_PERCENT and turnover >= MIN_TURNOVER_USDT
                and ratio >= MIN_OI_TURNOVER_RATIO
                and (MAX_OI_TURNOVER_RATIO is None or ratio <= MAX_OI_TURNOVER_RATIO)):
            gainers.append(dict(symbol=symbol, gain=gain, turnover=turnover, price=price,
                                oi=oi, ratio=ratio, funding=funding))
    return sorted(gainers, key=lambda row: row["gain"], reverse=True), evaluated


def scrape():
    contracts = fetch_market_data("detail")
    tickers = fetch_market_data("ticker")
    return select_gainers(tickers, contracts)


def format_alert(coin):
    symbol = escape(coin["symbol"])
    url = f"https://www.mexc.com/futures/{quote(coin['symbol'], safe='')}"
    return (
        f"🚀 <b>MEXC FUTURES GAINER</b>\n\n🔥 <b>{symbol}</b>\n"
        f"Rolling 24h Gain (1m): {coin['gain']:+.2f}%\n"
        f"24h Turnover: ${coin['turnover']:,.0f}\n"
        f"Open Interest: ${coin['oi']:,.0f}\n"
        f"OI / Turnover: {coin['ratio']:.1%}\n"
        f"Funding: {coin['funding']:+.4f}%\n"
        f"Price: ${coin['price']:f}\n\n"
        f'<a href="{url}">Open MEXC futures</a>'
    )


async def deliver_alerts(app, gainers):
    """Keep the latest successful delivery per subscriber/ticker as a daily cache."""
    with closing(get_db()) as conn, conn:
        users = conn.execute("SELECT chat_id FROM telegram_users").fetchall()
        sent = {(chat_id, symbol): sent_at[:10] for chat_id, symbol, sent_at in
                conn.execute("SELECT chat_id, symbol, sent_at FROM gainer_alerts")}
    for coin in gainers:
        for (chat_id,) in users:
            if sent.get((chat_id, coin["symbol"])) == now()[:10]:
                continue
            try:
                await app.bot.send_message(chat_id=chat_id, text=format_alert(coin),
                                           parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                logger.warning("Failed to notify chat %s; will retry next scan", chat_id)
                continue
            sent_at = now()
            with closing(get_db()) as conn, conn:
                conn.execute("""INSERT INTO gainer_alerts VALUES (?, ?, ?)
                             ON CONFLICT(chat_id, symbol) DO UPDATE SET sent_at = excluded.sent_at""",
                             (chat_id, coin["symbol"], sent_at))
            sent[(chat_id, coin["symbol"])] = sent_at[:10]


async def scrape_loop(context: ContextTypes.DEFAULT_TYPE):
    try:
        gainers, evaluated = await asyncio.to_thread(scrape)
        await deliver_alerts(context.application, gainers)
        HEALTH_PATH.touch()
        logger.info("Evaluated %d contracts; %d qualify", len(evaluated), len(gainers))
    except Exception:
        logger.exception("Market scan failed; retaining alert state for the next scan")


def main():
    if not BOT_TOKEN:
        raise ValueError("TG_BOT_TOKEN must be set")
    thresholds = [MIN_GAIN_PERCENT, MIN_TURNOVER_USDT, MIN_OI_TURNOVER_RATIO]
    if MAX_OI_TURNOVER_RATIO is not None:
        thresholds.append(MAX_OI_TURNOVER_RATIO)
    if SCRAPE_INTERVAL <= 0 or any(not value.is_finite() or value < 0 for value in thresholds):
        raise ValueError("Scanner interval must be positive and thresholds finite and nonnegative")
    if MIN_TURNOVER_USDT <= 0 or (
        MAX_OI_TURNOVER_RATIO is not None and MAX_OI_TURNOVER_RATIO < MIN_OI_TURNOVER_RATIO
    ):
        raise ValueError("Turnover must be positive and maximum ratio must be at least the minimum")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("health", health))
    app.job_queue.run_repeating(scrape_loop, interval=SCRAPE_INTERVAL, first=5,
                                job_kwargs={"max_instances": 1, "coalesce": True})
    logger.info("MEXC futures gainer bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
