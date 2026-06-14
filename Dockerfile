FROM python:3.12-slim

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

CMD ["python", "main.py", "serve"]
