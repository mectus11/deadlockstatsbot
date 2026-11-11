"""Client for the community Deadlock API (https://api.deadlock-api.com).

Used for what Statlocker's API does not offer: match lists, per-match items, the
hero/item catalogue with image URLs and Steam personas/avatars.
"""
import asyncio
import logging
import time

import aiohttp

from models import (HERO_IMAGE_KEYS, ITEM_IMAGE_KEYS, Asset, Match,
                    SteamProfile, normalize_entry, normalize_metadata, parse_assets,
                    parse_steam_profile)

log = logging.getLogger("deadlock_api")

BASE = "https://api.deadlock-api.com"
ASSET_TTL = 6 * 3600
STEAM_TTL = 6 * 3600          # persona + avatar rarely change; the response carries a friends list
STEAM_MISS_TTL = 600          # do not hammer a private/failed profile


class ApiUnavailable(Exception):
    """The match-history service could not be reached (network/DNS/timeout)."""


class DeadlockAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str | None = None):
        self.session = session
        self.headers = {"X-API-KEY": api_key} if api_key else {}
        self._assets: dict[str, tuple[float, dict[int, Asset]]] = {}
        self._steam: dict[int, tuple[float, SteamProfile | None]] = {}

    async def _request(self, url: str, params: dict | None = None, retries: int = 2,
                       critical: bool = False) -> tuple[int | None, object | None]:
        """GET + decode JSON. Returns (status, data); status is None when no response
        arrived (network/DNS/timeout). Raises ApiUnavailable when `critical`."""
        try:
            for attempt in range(retries + 1):
                async with self.session.get(url, params=params, headers=self.headers,
                                            timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 429 and attempt < retries:
                        await asyncio.sleep(min(float(r.headers.get("Retry-After", 2 * (attempt + 1))), 30))
                        continue
                    if r.status == 200:
                        return 200, await r.json(content_type=None)
                    log.warning("GET %s -> HTTP %s", url, r.status)
                    return r.status, None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log.warning("Could not reach %s: %s", url, e)
            if critical:
                raise ApiUnavailable(str(e)) from e
        return None, None

    async def _get_json(self, url: str, params: dict | None = None, retries: int = 2,
                        critical: bool = False):
        """GET + decode JSON. Network failures return None, or raise ApiUnavailable
        when `critical` so callers can tell the user."""
        return (await self._request(url, params, retries, critical))[1]

    # --- matches -----------------------------------------------------------
    async def match_history(self, account_id: int) -> list[Match]:
        """Newest first. Uses the plain (cached) response; force_refetch is
        limited to ~1/hour/IP so we never use it."""
        data = await self._get_json(f"{BASE}/v1/players/{account_id}/match-history", critical=True)
        if not isinstance(data, list):
            return []
        matches = [m for m in (normalize_entry(e) for e in data if isinstance(e, dict)) if m]
        matches.sort(key=lambda m: m.match_id, reverse=True)
        return matches

    async def match_detail(self, match_id: int, account_id: int) -> Match | None:
        """Per-player stats and final items for one match; None if unavailable
        (metadata can lag a few minutes behind the match)."""
        data = await self._get_json(f"{BASE}/v1/matches/{match_id}/metadata")
        return normalize_metadata(data, account_id, match_id) if data else None

    # --- catalogue (names + image URLs) ------------------------------------
    async def _catalogue(self, kind: str, image_keys) -> dict[int, Asset]:
        hit = self._assets.get(kind)
        if hit and time.monotonic() - hit[0] < ASSET_TTL:
            return hit[1]
        data = await self._get_json(f"{BASE}/v1/assets/{kind}")
        parsed = parse_assets(data, image_keys)
        if parsed:
            self._assets[kind] = (time.monotonic(), parsed)
            return parsed
        if data:
            log.warning("/v1/assets/%s returned data in an unexpected shape; run probe.py", kind)
        return hit[1] if hit else {}

    async def heroes(self) -> dict[int, Asset]:
        return await self._catalogue("heroes", HERO_IMAGE_KEYS)

    async def items(self) -> dict[int, Asset]:
        return await self._catalogue("items", ITEM_IMAGE_KEYS)

    async def hero_name(self, hero_id: int | None) -> str:
        if hero_id is None:
            return "Unknown hero"
        hero = (await self.heroes()).get(hero_id)
        return hero.name if hero else f"Hero #{hero_id}"

    async def steam_profile(self, account_id: int) -> SteamProfile | None:
        """Steam persona + avatar. Cached: the response also carries a large friends
        list, so it must not be re-fetched for every card render."""
        now = time.monotonic()
        hit = self._steam.get(account_id)
        if hit and now - hit[0] < (STEAM_TTL if hit[1] else STEAM_MISS_TTL):
            return hit[1]
        status, data = await self._request(f"{BASE}/v1/players/steam", {"account_ids": account_id})
        profile = parse_steam_profile(data) if status == 200 else None
        self._steam[account_id] = (now, profile)
        return profile
