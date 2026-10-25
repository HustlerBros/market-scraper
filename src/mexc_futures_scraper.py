"""Public MEXC futures gainer scanner with persistent Telegram subscriptions."""

import asyncio
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from functools import partial
import logging
import os
from pathlib import Path
import time
from urllib.parse import quote

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from prometheus_client import start_http_server, Counter, Histogram, Gauge

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(ROOT_DIR / "data/mexc_futures.db"))
API_BASE = "https://api.mexc.com/api/v1/contract"
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "60"))
HEALTH_PATH = Path("/tmp/mexc-scanner-health")
MIN_GAIN_PERCENT = Decimal(os.getenv("MIN_GAIN_PERCENT", "50"))
MIN_TURNOVER_USDT = Decimal(os.getenv("MIN_TURNOVER_USDT", "1000000"))
MIN_OI_TURNOVER_RATIO = Decimal(os.getenv("MIN_OI_TURNOVER_RATIO", "0.10"))
MAX_OI_TURNOVER_RATIO = (
    Decimal(os.environ["MAX_OI_TURNOVER_RATIO"]) if os.getenv("MAX_OI_TURNOVER_RATIO") else None
)
ADMIN_IDS = frozenset(
    value.strip() for value in os.getenv("TG_ADMIN_IDS", "").split(",") if value.strip()
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScannerConfig:
    interval: int
    min_gain_percent: Decimal
    min_turnover_usdt: Decimal
    min_oi_turnover_ratio: Decimal
    max_oi_turnover_ratio: Decimal | None


def default_config():
    return ScannerConfig(SCRAPE_INTERVAL, MIN_GAIN_PERCENT, MIN_TURNOVER_USDT,
                         MIN_OI_TURNOVER_RATIO, MAX_OI_TURNOVER_RATIO)


# --- PROMETHEUS METRICS ---
SCAN_DURATION = Histogram("mexc_scan_duration_seconds", "Duration of market scan cycle")
API_LATENCY = Histogram("mexc_http_request_duration_seconds", "HTTP latency", ["endpoint"])
API_ERRORS = Counter("mexc_http_errors_total", "HTTP failures", ["endpoint"])
EVALUATED_COUNTER = Counter("mexc_contracts_evaluated_total", "Contracts checked")
GAINERS_COUNTER = Counter("mexc_gainers_detected_total", "Gainers meeting criteria")
ALERTS_SENT = Counter("mexc_telegram_alerts_total", "Alerts dispatched", ["status"])
SUBSCRIBERS_COUNT = Gauge("mexc_subscribers_total", "Current unique subscribers")
LAST_SCAN_TIMESTAMP = Gauge("mexc_last_successful_scan_timestamp", "Epoch timestamp of last scan")


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
        conn.execute("""CREATE TABLE IF NOT EXISTS scanner_settings (
            name TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        conn.executemany(
            "INSERT OR IGNORE INTO scanner_settings (name, value) VALUES (?, ?)",
            [(key, "off" if value is None else str(value))
             for key, value in config_values(default_config()).items()],
        )


def config_values(config):
    return {
        "SCRAPE_INTERVAL": config.interval,
        "MIN_GAIN_PERCENT": config.min_gain_percent,
        "MIN_TURNOVER_USDT": config.min_turnover_usdt,
        "MIN_OI_TURNOVER_RATIO": config.min_oi_turnover_ratio,
        "MAX_OI_TURNOVER_RATIO": config.max_oi_turnover_ratio,
    }


def load_config():
    values = config_values(default_config())
    with closing(get_db()) as conn:
        values.update(dict(conn.execute("SELECT name, value FROM scanner_settings")))
    return ScannerConfig(
        interval=int(values["SCRAPE_INTERVAL"]),
        min_gain_percent=Decimal(values["MIN_GAIN_PERCENT"]),
        min_turnover_usdt=Decimal(values["MIN_TURNOVER_USDT"]),
        min_oi_turnover_ratio=Decimal(values["MIN_OI_TURNOVER_RATIO"]),
        max_oi_turnover_ratio=(None if values["MAX_OI_TURNOVER_RATIO"] == "off"
                                else Decimal(values["MAX_OI_TURNOVER_RATIO"])),
    )


def validate_config(config):
    decimal_values = (config.min_gain_percent, config.min_turnover_usdt,
                      config.min_oi_turnover_ratio)
    if (config.interval < 10 or config.interval > 86_400
            or any(not value.is_finite() or value < 0 for value in decimal_values)
            or config.min_gain_percent > 10_000
            or config.min_turnover_usdt <= 0
            or config.min_turnover_usdt > Decimal("1e15")
            or config.min_oi_turnover_ratio > 1_000
            or (config.max_oi_turnover_ratio is not None and
                (not config.max_oi_turnover_ratio.is_finite()
                 or config.max_oi_turnover_ratio > 1_000
                 or config.max_oi_turnover_ratio < config.min_oi_turnover_ratio))):
        raise ValueError("Settings are outside valid ranges")


def save_config(config):
    validate_config(config)
    with closing(get_db()) as conn, conn:
        conn.executemany(
            "INSERT INTO scanner_settings (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            [(key, "off" if value is None else str(value))
             for key, value in config_values(config).items()],
        )


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
    config = load_config()
    await update.effective_message.reply_text(
        "✅ You are subscribed to MEXC USDT perpetual futures gainer alerts!\n"
        f"Rolling 24h gain (1-minute candles) ≥ {config.min_gain_percent}%, "
        f"turnover ≥ ${config.min_turnover_usdt:,.0f}, "
        f"OI/turnover ≥ {config.min_oi_turnover_ratio:.0%}."
        + (f" Maximum OI/turnover: {config.max_oi_turnover_ratio:.0%}."
           if config.max_oi_turnover_ratio is not None else "")
        + f"\nScans run every {config.interval} seconds."
        + "\nEach ticker is sent at most once per UTC day. Use /settings and /help."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Scanner commands:\n"
        "/settings — show current scanner settings\n"
        "/set_gain <percent> — minimum rolling 24h gain\n"
        "/set_turnover <USDT> — minimum 24h turnover\n"
        "/set_min_oi_ratio <ratio> — minimum OI/turnover (0.10 = 10%)\n"
        "/set_max_oi_ratio <ratio|off> — maximum ratio or disable it\n"
        "/set_interval <seconds> — scan interval (10–86400)\n"
        "Changes are restricted to configured Telegram admins."
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Current scanner settings:\n" + format_settings(load_config()))


async def set_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, setting):
    user_id = str(update.effective_user.id) if update.effective_user else ""
    if user_id not in ADMIN_IDS:
        await update.effective_message.reply_text(
            "Only configured admins can change scanner settings. Set TG_ADMIN_IDS "
            "to comma-separated Telegram user IDs and restart the bot."
        )
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Provide exactly one value. See /help.")
        return
    try:
        current = load_config()
        value = context.args[0]
        if setting == "set_gain":
            updated = replace(current, min_gain_percent=Decimal(value))
        elif setting == "set_turnover":
            updated = replace(current, min_turnover_usdt=Decimal(value))
        elif setting == "set_min_oi_ratio":
            updated = replace(current, min_oi_turnover_ratio=Decimal(value))
        elif setting == "set_max_oi_ratio":
            updated = replace(current, max_oi_turnover_ratio=(
                None if value.lower() == "off" else Decimal(value)))
        elif setting == "set_interval":
            updated = replace(current, interval=int(value))
        else:
            raise ValueError("Unknown setting")
        validate_config(updated)
        save_config(updated)
    except (ValueError, InvalidOperation):
        await update.effective_message.reply_text(
            "Invalid value. Use nonnegative numbers, keep max OI/turnover ≥ min, "
            "and set the interval from 10 to 86400 seconds."
        )
        return
    if setting == "set_interval":
        schedule_scraper(context.application, updated.interval)
    await update.effective_message.reply_text(f"Saved.\n\n{format_settings(updated)}")


def format_settings(config):
    max_ratio = ("off" if config.max_oi_turnover_ratio is None
                 else f"{config.max_oi_turnover_ratio} ({config.max_oi_turnover_ratio:.0%})")
    return (
        f"Minimum gain: {config.min_gain_percent}%\n"
        f"Minimum turnover: {config.min_turnover_usdt} USDT\n"
        f"Minimum OI/turnover: {config.min_oi_turnover_ratio} "
        f"({config.min_oi_turnover_ratio:.0%})\n"
        f"Maximum OI/turnover: {max_ratio}\n"
        f"Scan interval: {config.interval} seconds"
    )


def fetch_payload(endpoint, params=None):
    clean_endpoint = endpoint.split("/")[0]  # Normalizes "kline/SYMBOL" to "kline" for metrics
    try:
        with API_LATENCY.labels(endpoint=clean_endpoint).time():
            response = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("code") != 0:
                raise ValueError(f"MEXC {endpoint} request was unsuccessful")
            return payload.get("data")
    except Exception:
        API_ERRORS.labels(endpoint=clean_endpoint).inc()
        raise


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


def select_gainers(tickers, contracts, config=None):
    """Return sorted gainers and successfully evaluated symbols for scan statistics."""
    config = config or default_config()
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
            if (turnover < config.min_turnover_usdt or ratio < config.min_oi_turnover_ratio
                    or (config.max_oi_turnover_ratio is not None
                        and ratio > config.max_oi_turnover_ratio)):
                evaluated.add(symbol)
                continue
            reference = fetch_reference_price(symbol, ticker["timestamp"])
            gain = (price / reference - 1) * 100
        except (KeyError, ValueError, TypeError, InvalidOperation, requests.RequestException):
            logger.warning("Skipping invalid market data for %s", symbol)
            continue
        evaluated.add(symbol)
        if (gain >= config.min_gain_percent and turnover >= config.min_turnover_usdt
                and ratio >= config.min_oi_turnover_ratio
                and (config.max_oi_turnover_ratio is None
                     or ratio <= config.max_oi_turnover_ratio)):
            gainers.append(dict(symbol=symbol, gain=gain, turnover=turnover, price=price,
                                oi=oi, ratio=ratio, funding=funding))
    return sorted(gainers, key=lambda row: row["gain"], reverse=True), evaluated


def scrape():
    config = load_config()
    contracts = fetch_market_data("detail")
    tickers = fetch_market_data("ticker")
    return select_gainers(tickers, contracts, config)


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
        SUBSCRIBERS_COUNT.set(len(users))  # Metric: Update subscriber gauge
        sent = {(chat_id, symbol): sent_at[:10] for chat_id, symbol, sent_at in
                conn.execute("SELECT chat_id, symbol, sent_at FROM gainer_alerts")}
    for coin in gainers:
        for (chat_id,) in users:
            if sent.get((chat_id, coin["symbol"])) == now()[:10]:
                ALERTS_SENT.labels(status="deduplicated").inc()
                continue
            try:
                await app.bot.send_message(chat_id=chat_id, text=format_alert(coin),
                                           parse_mode="HTML", disable_web_page_preview=True)
                ALERTS_SENT.labels(status="sent").inc()
            except Exception:
                ALERTS_SENT.labels(status="failed").inc()
                logger.warning("Failed to notify chat %s; will retry next scan", chat_id)
                continue
            sent_at = now()
            with closing(get_db()) as conn, conn:
                conn.execute("""INSERT INTO gainer_alerts VALUES (?, ?, ?)
                             ON CONFLICT(chat_id, symbol) DO UPDATE SET sent_at = excluded.sent_at""",
                             (chat_id, coin["symbol"], sent_at))
            sent[(chat_id, coin["symbol"])] = sent_at[:10]


async def scrape_loop(context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    try:
        gainers, evaluated = await asyncio.to_thread(scrape)
        
        # Metrics: Update scan statistics
        EVALUATED_COUNTER.inc(len(evaluated))
        GAINERS_COUNTER.inc(len(gainers))
        LAST_SCAN_TIMESTAMP.set_to_current_time()
        
        await deliver_alerts(context.application, gainers)
        HEALTH_PATH.touch()
        logger.info("Evaluated %d contracts; %d qualify", len(evaluated), len(gainers))
    except Exception:
        logger.exception("Market scan failed; retaining alert state for the next scan")
    finally:
        # Metric: Record full loop duration
        SCAN_DURATION.observe(time.time() - start_time)


def schedule_scraper(app, interval):
    for job in app.job_queue.get_jobs_by_name("market_scan"):
        job.schedule_removal()
    return app.job_queue.run_repeating(
        scrape_loop, interval=interval, first=5, name="market_scan",
        job_kwargs={"max_instances": 1, "coalesce": True},
    )


def main():
    if not BOT_TOKEN:
        raise ValueError("TG_BOT_TOKEN must be set")
    validate_config(default_config())
    
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    # Start Prometheus metrics server
    start_http_server(8000)
    logger.info("Prometheus metrics server running on port 8000")
    
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    for command in ("set_gain", "set_turnover", "set_min_oi_ratio",
                    "set_max_oi_ratio", "set_interval"):
        app.add_handler(CommandHandler(command, partial(set_setting, setting=command)))
    schedule_scraper(app, load_config().interval)
    logger.info("MEXC futures gainer bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
