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
from local_icons import LocalIcons
from models import (HERO_IMAGE_KEYS, ITEM_IMAGE_KEYS, normalize_entry, normalize_metadata, parse_assets)
from steam import parse_steam_input

load_dotenv()
SHOP_SLOTS = {"weapon", "vitality", "spirit"}  # mirrors bot.py: ability upgrades are never shown on cards


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
            if kind == "items":
                await check_local_items(parsed)


async def check_local_items(catalogue):
    folder = os.getenv("LOCAL_ITEM_ICONS", "assets/items")
    local = LocalIcons(folder)
    print(f"\n=== local item icons in {folder!r}: {len(local.files)} file(s) loaded")
    if not local.files:
        print("  No files loaded. Either the folder doesn't exist here, has no .png/.webp/.jpg/.jpeg files,")
        print("  or (if running in Docker) LOCAL_ITEM_ICONS points somewhere the files weren't actually copied to.")
        return

    shop_items = {a.id: a for a in catalogue.values() if a.slot in SHOP_SLOTS}  # ability upgrades never shown
    matched_keys = set()
    missing = []
    for a in shop_items.values():
        key = local.match_key(a.name, a.tier)
        if key:
            matched_keys.add(key)
        else:
            missing.append(a.name)
    print(f"  {len(shop_items) - len(missing)}/{len(shop_items)} real (shop) catalogue items have a matching local icon")
    if missing:
        print(f"  catalogue items with NO local icon ({len(missing)}): {', '.join(sorted(missing)[:40])}"
             + (" ..." if len(missing) > 40 else ""))

    orphans = [str(p) for key, p in local.files.items() if key not in matched_keys]
    print(f"  local files that matched NO catalogue item ({len(orphans)}):")
    for o in sorted(orphans)[:40]:
        print(f"    {o}")
    if len(orphans) > 40:
        print(f"    ... and {len(orphans) - 40} more")
    if orphans:
        print("  Each one above is either misnamed (compare it to the matching entry under 'catalogue items")
        print("  with NO local icon'), or isn't a real Deadlock item at all (e.g. art from a generic icon pack).")


if len(sys.argv) < 2:
    raise SystemExit("usage: python probe.py <steamid>")
asyncio.run(main(sys.argv[1]))
