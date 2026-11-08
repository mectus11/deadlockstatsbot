"""Client for the Statlocker public API (https://statlocker.gg/api).

Requires an API key (applied for on that page). Everything here is optional:
without a key the bot still works, just without PP score / rank / lobby rank.

Documented endpoints used:
  GET /api/public/profile/{accountId}
  GET /api/public/match/{matchId}
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("statlocker")

BASE = "https://statlocker.gg"
# Statlocker's docs still point at assets.deadlock-api.com, which no longer resolves; the
# Deadlock API moved its images here (same file names). Rank-badge path not verified live:
# if badges are missing the card simply renders without them.
IMAGE_BASE = "https://assets-bucket.deadlock-api.com/assets-api-res/images"
TIER_NAMES = [
    "Initiate", "Seeker", "Acolyte", "Sentinel", "Mystic", "Ritualist",
    "Emissary", "Oracle", "Phantom", "Ascendant", "Eternus",
]


def rank_name(rank_number: int | None) -> str | None:
    if not rank_number:
        return None
    tier, sub = divmod(int(rank_number), 10)
    if not 1 <= tier <= len(TIER_NAMES):
        return None
    return f"{TIER_NAMES[tier - 1]} {sub}"


def rank_badge_url(rank_number: int | None) -> str | None:
    if not rank_number:
        return None
    tier, sub = divmod(int(rank_number), 10)
    if not 1 <= tier <= len(TIER_NAMES):
        return None
    return f"{IMAGE_BASE}/ranks/rank{tier}/badge_lg_subrank{sub}.webp"


@dataclass
class Profile:
    name: str | None
    avatar_url: str | None
    rank_number: int | None
    rank_label: str  # which ladder the rank came from
    pp_score: int | None


@dataclass
class LobbyInfo:
    own_rank: int | None
    enemy_rank: int | None


def parse_profile(d: dict) -> Profile:
    """Prefer the live Season 1 ladders (docs: always use the returned
    rankNumber there); fall back to the frozen pre-matchmaking estimate."""
    s1 = d.get("season1") or {}
    for key, label in (("ranked", "Season 1 Ranked"), ("casual", "Season 1 Casual")):
        ladder = s1.get(key)
        if isinstance(ladder, dict) and ladder.get("rankNumber"):
            return Profile(d.get("name"), d.get("avatarUrl"), ladder["rankNumber"], label,
                           ladder.get("ppScore"))
    return Profile(d.get("name"), d.get("avatarUrl"), d.get("estimatedRankNumber"),
                   "Estimated (pre-matchmaking)", d.get("ppScore"))


def parse_lobby(match: dict, account_id: int) -> LobbyInfo | None:
    """Mean rank of the player's team vs the enemy team in this match."""
    amber = match.get("amberHandMeanPPScoreRankNumber")
    sapphire = match.get("sapphireFlameMeanPPScoreRankNumber")
    if amber is None and sapphire is None:
        return None
    for p in match.get("players") or []:
        if str(p.get("account_id")) == str(account_id):
            team = str(p.get("team", "")).lower()
            if "amber" in team:
                return LobbyInfo(amber, sapphire)
            if "sapphire" in team:
                return LobbyInfo(sapphire, amber)
    return None


class StatlockerAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str | None):
        self.session = session
        self.api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str, retries: int = 2) -> dict | None:
        if not self.enabled:
            return None
        try:
            for attempt in range(retries + 1):
                async with self.session.get(
                    BASE + path,
                    headers={"X-API-Key": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status == 429 and attempt < retries:
                        await asyncio.sleep(min(float(r.headers.get("Retry-After", 5)), 30))
                        continue
                    if r.status == 200:
                        return await r.json(content_type=None)
                    if r.status in (401, 403):
                        log.error("Statlocker rejected the API key (HTTP %s) for %s", r.status, path)
                    elif r.status != 404:
                        log.warning("Statlocker GET %s -> HTTP %s", path, r.status)
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log.warning("Could not reach Statlocker (%s): %s", path, e)
        return None

    async def profile(self, account_id: int) -> Profile | None:
        d = await self._get(f"/api/public/profile/{account_id}")
        return parse_profile(d) if isinstance(d, dict) else None

    async def lobby(self, match_id: int, account_id: int) -> LobbyInfo | None:
        """None if Statlocker has not processed the match yet (404)."""
        d = await self._get(f"/api/public/match/{match_id}")
        return parse_lobby(d, account_id) if isinstance(d, dict) else None
