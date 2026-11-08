FROM python:3.12-slim

# Pillow needs these at build time to decode/encode jpeg & webp
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g libwebp7 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Everything the bot writes lives under /app/data and /app/cache, both volumes
# (see docker-compose.yml) so it survives a rebuild/restart.
ENV DB_PATH=/app/data/bot.db \
    CACHE_DIR=/app/cache \
    LOCAL_ITEM_ICONS=/app/assets/items \
    PYTHONUNBUFFERED=1
RUN mkdir -p /app/data /app/cache

# Runs as its own user, not root
RUN useradd -m bot && chown -R bot:bot /app
USER bot

CMD ["python", "bot.py"]
