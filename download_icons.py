"""Download every item icon from the Deadlock API into ./assets/items.

    python download_icons.py

Files are named after the item ("Basic Magazine.webp") and are then used by the bot
in place of on-the-fly downloads. Already downloaded files are skipped.
"""
import asyncio
import os
import re
import sys
from pathlib import Path

import aiohttp

from deadlock_api import DeadlockAPI


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", name).strip() or "item"


async def download_all(session, dest: Path) -> dict:
    items = await DeadlockAPI(session, os.getenv("DEADLOCK_API_KEY") or None).items()
    dest.mkdir(parents=True, exist_ok=True)
    stats = {"items": len(items), "downloaded": 0, "skipped": 0, "no_image": 0, "failed": []}
    gate, used = asyncio.Semaphore(8), set()

    async def one(a, stem):
        ext = (re.search(r"\.(png|webp|jpe?g)(?:\?|$)", a.image, re.I) or [None, "png"])[1].lower()
        path = dest / f"{stem}.{ext}"
        if path.exists():
            stats["skipped"] += 1
            return
        try:
            async with gate, session.get(a.image, timeout=aiohttp.ClientTimeout(total=20)) as r:
                body = await r.read() if r.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            body = None
        if body:
            path.write_bytes(body)
            stats["downloaded"] += 1
        else:
            stats["failed"].append(a.name)

    jobs = []
    for a in items.values():
        if not a.image:
            stats["no_image"] += 1
            continue
        stem = safe_name(a.name)
        if stem.lower() in used:  # two items with the same name: keep both
            stem = f"{stem} {a.id}"
        used.add(stem.lower())
        jobs.append(one(a, stem))
    await asyncio.gather(*jobs)
    return stats


async def main():
    dest = Path(os.getenv("LOCAL_ITEM_ICONS", "assets/items"))
    async with aiohttp.ClientSession(headers={"User-Agent": "deadlockstatsbot-icons"}) as s:
        st = await download_all(s, dest)
    print(f"{st['items']} items in the catalogue: {st['downloaded']} downloaded, {st['skipped']} already there, "
          f"{st['no_image']} without an image URL, {len(st['failed'])} failed. Folder: {dest.resolve()}")
    if st["failed"]:
        print("Failed:", ", ".join(st["failed"][:30]), "..." if len(st["failed"]) > 30 else "")
    if not st["items"]:
        print("The item catalogue could not be loaded (see the warnings above); run probe.py for details.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
