#!/bin/bash

# check if environment variables are set
ENV_VARS=("TG_BOT_TOKEN")
for VAR in "${ENV_VARS[@]}"; do
  if [ -z "${!VAR}" ]; then
    echo "Error: Environment variable $VAR is not set."
    exit 1
  fi
done

# run the scraper
python src/mexc_futures_scraper.py