# Container image for the bot.
#
# Railway also builds this file (a Dockerfile takes precedence over Nixpacks).
# It deliberately does NOT declare VOLUME: Railway rejects that instruction
# ("docker VOLUME at Line 20 is not supported, use Railway Volumes") because the
# platform attaches volumes itself. Mount your volume at /data in the Railway UI
# instead — DB_PATH and LOG_PATH below already point there.
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

# The mount point for the volume (created, but not declared as VOLUME).
RUN mkdir -p /data

CMD ["python", "run.py"]
