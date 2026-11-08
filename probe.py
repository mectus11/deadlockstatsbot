"""Print what the Deadlock API really returns, to check the parsers against live data.

    python probe.py <steamid>

Share the output (it contains no secrets) if cards come out empty or wrong.
"""
import asyncio
import io
import json
import sys

import aiohttp
from dotenv import load_dotenv
from PIL import Image
import os

from deadlock_api import BASE
from models import (HERO_IMAGE_KEYS, ITEM_IMAGE_KEYS, normalize_entry, normalize_metadata, parse_assets)
from steam import parse_steam_input

load_dotenv()


def show(title, obj, limit=900):
    text = json.dumps(obj, indent=1, ensure_ascii=False, default=str)
    print(f"\n=== {title}\n{text[:limit]}{' ...(cut)' if len(text) > limit else ''}")


async def get(s, url):
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
            print(f"GET {url} -> HTTP {r.status}")
            return await r.json(content_type=None) if r.status == 200 else None
    except Exception as e:
        print(f"GET {url} FAILED: {e!r}")
        return None


async def check_image(s, label, url):
    if not url:
        print(f"  {label}: NO IMAGE URL FOUND")
        return
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            body = await r.read()
        Image.open(io.BytesIO(body)).load()
        print(f"  {label}: OK  {url}  ({len(body)} bytes)")
    except Exception as e:
        print(f"  {label}: PROBLEM  {url}  -> {e!r}")


async def main(steamid):
    acc = parse_steam_input(steamid)
    if acc is None:
        raise SystemExit("Could not read that Steam ID.")
    headers = {"User-Agent": "deadlockstatsbot-probe"}
    if os.getenv("DEADLOCK_API_KEY"):
        headers["X-API-KEY"] = os.environ["DEADLOCK_API_KEY"]
    async with aiohttp.ClientSession(headers=headers) as s:
        hist = await get(s, f"{BASE}/v1/players/{acc}/match-history")
        if isinstance(hist, list) and hist:
            show("first match-history entry (raw)", hist[0])
            print("parsed:", normalize_entry(hist[0]))
            mid = normalize_entry(hist[0]).match_id
            meta = await get(s, f"{BASE}/v1/matches/{mid}/metadata")
            if meta:
                info = meta.get("match_info", meta) if isinstance(meta, dict) else {}
                print("\n=== metadata keys:", sorted(info) if isinstance(info, dict) else type(info))
                me = next((p for p in info.get("players", []) if p.get("account_id") == acc), None)
                if me:
                    print("player keys:", sorted(me))
                    show("first 2 raw items of your player", (me.get("items") or [])[:2])
                m = normalize_metadata(meta, acc, mid)
                print("parsed:", m)
        else:
            print("No match history returned.")
            m = None

        for kind, keys in (("heroes", HERO_IMAGE_KEYS), ("items", ITEM_IMAGE_KEYS)):
            data = await get(s, f"{BASE}/v1/assets/{kind}")
            sample = (data[0] if isinstance(data, list) and data else data)
            show(f"first {kind} entry (raw)", sample, 1400)
            parsed = parse_assets(data, keys)
            print(f"parsed {len(parsed)} {kind}")
            for a in list(parsed.values())[:2]:
                print(" ", a)
                await check_image(s, f"{kind} image", a.image)
            if kind == "items" and m and m.items:
                found = [parsed.get(b.item_id) for b in m.items]
                print(f"  your {len(m.items)} items: {sum(1 for f in found if f)} found in the catalogue")
                await check_image(s, "your first item image", next((f.image for f in found if f), None))


if len(sys.argv) < 2:
    raise SystemExit("usage: python probe.py <steamid>")
asyncio.run(main(sys.argv[1]))
