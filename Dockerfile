FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY run_app.sh .

VOLUME ["/data"]


CMD ["bash", "run_app.sh"]
