#!/bin/bash
set -eu

if [ -z "${TG_BOT_TOKEN:-}" ]; then
  echo "Error: Environment variable TG_BOT_TOKEN is not set." >&2
  exit 1
fi

exec python src/mexc_futures_scraper.py
