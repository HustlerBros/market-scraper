FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mexc_futures_scraper.py .

VOLUME ["/data"]

ENV DB_PATH=/data/mexc_futures.db
ENV FILE_PATH=/data/response.txt
ENV TG_BOT_TOKEN=TOKEN_GOES_HERE

CMD ["python", "mexc_futures_scraper.py"]
