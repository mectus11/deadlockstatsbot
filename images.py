"""Download images once; keep them in memory and on disk (./cache)."""
import asyncio
import hashlib
import logging
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("images")
MAX_BYTES = 3_000_000
RETRY_FAILED_AFTER = 600  # seconds


class ImageStore:
    def __init__(self, session: aiohttp.ClientSession, cache_dir: str = "cache"):
        self.session = session
        self.dir = Path(cache_dir)
        self._mem: dict[str, bytes] = {}
        self._failed: dict[str, float] = {}
        self._gate = asyncio.Semaphore(8)

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha1(url.encode()).hexdigest() + ".img")

    async def get(self, url: str | None) -> bytes | None:
        if not url:
            return None
        if url in self._mem:
            return self._mem[url]
        p = self._path(url)
        try:
            if p.exists():
                self._mem[url] = p.read_bytes()
                return self._mem[url]
        except OSError:
            pass
        if time.monotonic() - self._failed.get(url, -RETRY_FAILED_AFTER) < RETRY_FAILED_AFTER:
            return None
        try:
            async with self._gate, self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.read() if r.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log.warning("image %s: %s", url, e)
            data = None
        if not data or len(data) > MAX_BYTES:
            self._failed[url] = time.monotonic()
            return None
        self._mem[url] = data
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        except OSError:
            pass
        return data

    async def get_many(self, urls) -> dict[str, bytes]:
        """url -> bytes for every url that could be fetched (None/failed ones omitted)."""
        unique = list(dict.fromkeys(u for u in urls if u))
        got = await asyncio.gather(*(self.get(u) for u in unique))
        return {u: b for u, b in zip(unique, got) if b}
