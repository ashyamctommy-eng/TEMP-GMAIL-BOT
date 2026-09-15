# Optional: Railway/Nixpacks works without this, but a Dockerfile pins the
# runtime exactly and works on any container host (Fly, Render, VPS, k8s).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/bot.db \
    LOG_PATH=/data/bot.log

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite lives on a volume so aliases and messages survive a redeploy.
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "run.py"]
