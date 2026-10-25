"""Docker health probe: require a recently completed scanner job."""

import os
from pathlib import Path
import sqlite3
import time

HEALTH_PATH = Path("/tmp/mexc-scanner-health")


def is_healthy():
    try:
        max_age = max(180, int(os.getenv("SCRAPE_INTERVAL", "60")) * 3)
        database = Path(os.getenv("DB_PATH", "data/mexc_futures.db"))
        if not database.is_absolute():
            database = Path(__file__).resolve().parent.parent / database
        if database.exists():
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT value FROM scanner_settings WHERE name = 'SCRAPE_INTERVAL'"
                ).fetchone()
            if row:
                max_age = max(180, int(row[0]) * 3)
        return 0 <= time.time() - HEALTH_PATH.stat().st_mtime <= max_age
    except (OSError, ValueError, sqlite3.Error):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if is_healthy() else 1)
