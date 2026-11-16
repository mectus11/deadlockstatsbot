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
import time
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


@dataclass(frozen=True)
class RankSnapshot:
    """PP/rank before and after the match a card shows. Stored with the card so a refresh
    re-renders the same delta instead of dropping it, and so late data can be filled in."""
    pp_before: int | None = None
    pp_after: int | None = None
    rank_before: int | None = None
    rank_after: int | None = None

    @property
    def pp_delta(self) -> int | None:
        if self.pp_before is None or self.pp_after is None:
            return None
        return self.pp_after - self.pp_before

    @property
    def rank_changed(self) -> bool:
        return (self.rank_before is not None and self.rank_after is not None
                and self.rank_before != self.rank_after)

    @property
    def promoted(self) -> bool:
        """A real promotion, not a first-ever rank: both sides must be known."""
        return self.rank_changed and self.rank_after > self.rank_before


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


def _ago(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def format_top(entries: list[tuple[str, "Profile | None"]],
               limit: int = 10) -> tuple[str, int, int]:
    """Server ladder body: `entries` are (already escaped display name, profile).
    Returns (description, number shown, number without PP data)."""
    ranked = sorted(((name, p) for name, p in entries if p and p.pp_score is not None),
                    key=lambda np: (-np[1].pp_score, -(np[1].rank_number or 0), np[0].lower()))
    unranked = len(entries) - len(ranked)
    shown = ranked[:limit]
    lines = [f"{i}. **{name}** — {rank_name(p.rank_number) or 'unranked'} · {p.pp_score} PP"
             for i, (name, p) in enumerate(shown, 1)]
    return "\n".join(lines), len(shown), unranked

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
        self.last_status: int | None = None  # HTTP status of the last call; None = never/network
        self.last_status_at: float | None = None
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def status_text(self) -> str:
        """One line for /status; never includes the key itself."""
        if not self.enabled:
            return "not configured (no `STATLOCKER_API_KEY`)"
        if self.last_status_at is None:
            return "key set · no calls yet"
        when = _ago(self.last_status_at)
        if self.last_status == 200:
            return f"OK · last call {when}"
        if self.last_status in (401, 403):
            return f"key rejected (HTTP {self.last_status}) · {when}"
        if self.last_status is None:
            return f"unreachable · {when}"
        return f"HTTP {self.last_status} · {when}"

    async def _get(self, path: str, retries: int = 2) -> dict | None:
        if not self.enabled:
            return None
        self.calls += 1
        try:
            for attempt in range(retries + 1):
                async with self.session.get(
                    BASE + path,
                    headers={"X-API-Key": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    self.last_status = r.status
                    self.last_status_at = time.time()
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
            self.last_status = None
            self.last_status_at = time.time()
            log.warning("Could not reach Statlocker (%s): %s", path, e)
        return None

    async def profile(self, account_id: int) -> Profile | None:
        d = await self._get(f"/api/public/profile/{account_id}")
        return parse_profile(d) if isinstance(d, dict) else None

    async def lobby(self, match_id: int, account_id: int) -> LobbyInfo | None:
        """None if Statlocker has not processed the match yet (404)."""
        d = await self._get(f"/api/public/match/{match_id}")
        return parse_lobby(d, account_id) if isinstance(d, dict) else None
