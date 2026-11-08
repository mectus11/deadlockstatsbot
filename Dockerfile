FROM python:3.12-slim

# Pillow needs these at build time to decode/encode jpeg & webp
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g libwebp7 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Which commit this image was built from, so the logs can confirm a redeploy actually picked up
# a new image rather than restarting the old one (GitHub Actions passes this automatically).
ARG BUILD_SHA=dev
ENV BUILD_SHA=$BUILD_SHA

# Everything the bot writes lives under /app/data and /app/cache, both volumes
# (see docker-compose.yml) so it survives a rebuild/restart.
ENV DB_PATH=/app/data/bot.db \
    CACHE_DIR=/app/cache \
    LOCAL_ITEM_ICONS=/app/assets/items \
    PYTHONUNBUFFERED=1
RUN mkdir -p /app/data /app/cache

# Runs as root (the default for most container panels, including the one hosting this), not a
# custom user: a mounted volume's write permissions are controlled by the host/panel, not the
# image, so a baked-in non-root user here can end up unable to write to it. At startup: create
# whatever directory DB_PATH/CACHE_DIR actually point at (they may be overridden by an env var to
# a different mount, e.g. a panel's own /data), and print whether that directory is actually
# writable by this process before even starting the bot — if the database still can't open, this
# diagnostic line says whether it's a permissions problem (and as which user) or something else.
CMD ["sh", "-c", "DIR=\"$(dirname \"$DB_PATH\")\"; mkdir -p \"$DIR\" \"$CACHE_DIR\"; \
echo \"[diag] running as uid=$(id -u) gid=$(id -g)\"; ls -la \"$DIR\" 2>&1; \
if touch \"$DIR/.write_test\" 2>/tmp/write_err; then rm -f \"$DIR/.write_test\"; \
echo \"[diag] $DIR is writable\"; else echo \"[diag] $DIR is NOT writable: $(cat /tmp/write_err)\"; fi; \
exec python bot.py"]
