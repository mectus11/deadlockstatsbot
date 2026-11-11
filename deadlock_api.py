"""Client for the community Deadlock API (https://api.deadlock-api.com).

Used for what Statlocker's API does not offer: match lists, per-match items, the
hero/item catalogue with image URLs, Steam personas/avatars and match ingest status.
"""
import asyncio
import logging
import time

import aiohttp

from models import (HERO_IMAGE_KEYS, ITEM_IMAGE_KEYS, Asset, Match, MatchInfo, Salts,
                    SteamProfile, _int, mate_ids, normalize_entry, normalize_metadata, parse_assets,
                    parse_match_info, parse_salts, parse_steam_profile, parse_steam_profiles)

log = logging.getLogger("deadlock_api")

BASE = "https://api.deadlock-api.com"
ASSET_TTL = 6 * 3600
STEAM_TTL = 6 * 3600          # persona + avatar rarely change; the response carries a friends list
STEAM_MISS_TTL = 600          # do not hammer a private/failed profile
INFO_TTL = 30                 # /v1/matches/metadata allows only 30 req/min per IP
MATE_TTL = 6 * 3600           # party membership of an ingested match never changes
RECENT_TTL = 60               # the recently-fetched feed is ~1 MB


class ApiUnavailable(Exception):
    """The match-history service could not be reached (network/DNS/timeout)."""


class DeadlockAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str | None = None):
        self.session = session
        self.headers = {"X-API-KEY": api_key} if api_key else {}
        self._assets: dict[str, tuple[float, dict[int, Asset]]] = {}
        self._steam: dict[int, tuple[float, SteamProfile | None]] = {}
        self._info: dict[int, tuple[float, int | None, MatchInfo | None]] = {}
        self._mates: dict[tuple[int, int], tuple[float, set[int]]] = {}
        self._recent: tuple[float, set[int]] | None = None

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

    async def mate_stats(self, account_id: int, match_id: int,
                         game_mode: int | None = None) -> set[int]:
        """Accounts the API treats as same-party for this match. This is its own
        heuristic (same team + Steam friends at match start), not exact lobby
        membership. Cached per match; failures are not cached."""
        key = (account_id, match_id)
        now = time.monotonic()
        hit = self._mates.get(key)
        if hit and now - hit[0] < MATE_TTL:
            return hit[1]
        params = {"same_party": "true", "min_match_id": match_id, "max_match_id": match_id,
                  "min_matches_played": 1}
        if game_mode == 4:  # the endpoint defaults to normal games
            params["game_mode"] = "street_brawl"
        status, data = await self._request(f"{BASE}/v1/players/{account_id}/mate-stats", params)
        if status != 200:
            return set()
        mates = mate_ids(data, match_id)
        self._mates[key] = (now, mates)
        return mates

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

    async def steam_profiles(self, account_ids) -> dict[int, SteamProfile]:
        """Batch personas/avatars in one request (the endpoint takes up to 1000
        IDs). Reuses and fills the single-profile cache; missing rows are cached
        as misses so a private profile is not retried every card."""
        ids = [int(a) for a in dict.fromkeys(account_ids) if a]
        if not ids:
            return {}
        now = time.monotonic()
        out, missing = {}, []
        for aid in ids:
            hit = self._steam.get(aid)
            if hit and now - hit[0] < (STEAM_TTL if hit[1] else STEAM_MISS_TTL):
                if hit[1]:
                    out[aid] = hit[1]
            else:
                missing.append(aid)
        if missing:
            status, data = await self._request(
                f"{BASE}/v1/players/steam", {"account_ids": ",".join(map(str, missing))})
            parsed = parse_steam_profiles(data) if status == 200 else {}
            for aid in missing:
                profile = parsed.get(aid)
                self._steam[aid] = (now, profile)
                if profile:
                    out[aid] = profile
        return out

    # --- ingest status (/matchstatus) --------------------------------------
    async def match_info(self, match_id: int) -> tuple[int | None, MatchInfo | None]:
        """Cheap existence check: a match_info row means the match is ingested.
        Returns (status, row); 404 means not ingested, None means unreachable."""
        now = time.monotonic()
        hit = self._info.get(match_id)
        if hit and now - hit[0] < INFO_TTL:
            return hit[1], hit[2]
        status, data = await self._request(f"{BASE}/v1/matches/metadata",
                                           {"match_ids": match_id, "include_info": "true", "limit": 1})
        info = parse_match_info(data) if status == 200 else None
        self._info[match_id] = (now, status, info)
        return status, info

    async def metadata_available(self, match_id: int) -> bool | None:
        """Whether the full metadata (items/stats) is cached. disable_steam keeps this
        fast and avoids the 3-req/h Steam fallback budget. None = could not tell
        (unreachable, or a transient error such as a 429 that is not "not ingested")."""
        status, _ = await self._request(f"{BASE}/v1/matches/{match_id}/metadata",
                                        {"disable_steam": "true"}, retries=1)
        if status is None or status not in (200, 404):
            return None
        return status == 200

    async def match_salts(self, match_id: int) -> tuple[int | None, Salts | None]:
        """Salts stored for this match = the API knows it. Same disable_steam rule."""
        status, data = await self._request(f"{BASE}/v1/matches/{match_id}/salts",
                                           {"disable_steam": "true"}, retries=1)
        return status, parse_salts(data) if status == 200 else None

    async def recently_fetched_ids(self) -> set[int] | None:
        """Matches fetched in the last 10 minutes (~1 MB), cached briefly."""
        now = time.monotonic()
        if self._recent and now - self._recent[0] < RECENT_TTL:
            return self._recent[1]
        status, data = await self._request(f"{BASE}/v1/matches/recently-fetched", retries=1)
        if status != 200 or not isinstance(data, list):
            return None
        ids = {i for i in (_int(row.get("match_id")) for row in data if isinstance(row, dict))
               if i is not None}
        self._recent = (now, ids)
        return ids
