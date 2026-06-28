FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        lftp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backup/ backup/
COPY web/ web/
COPY main.py .

VOLUME ["/backups", "/data"]

EXPOSE 8080

ENV STATUS_FILE=/data/status.json \
    CONFIG_FILE=/app/ftp_config.json

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

CMD ["python", "main.py", "serve"]
