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
BADGE_BASE = "https://assets.deadlock-api.com"  # rank badge images (per Statlocker API docs)
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
    return f"{BADGE_BASE}/images/ranks/rank{tier}/badge_lg_subrank{sub}.webp"


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
    account_id: int | None = None
    calibrated: bool | None = None  # Season 1 ladder: None when unknown/pre-matchmaking fallback
    calibration_matches: int | None = None
    region: str | None = None


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

def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_profile(d: dict) -> Profile:
    """Prefer the live Season 1 ladders (docs: always use the returned
    rankNumber there); fall back to the frozen pre-matchmaking estimate."""
    s1 = d.get("season1") or {}
    account_id = _int(d.get("accountId"))
    region = d.get("region")
    for key, label in (("ranked", "Season 1 Ranked"), ("casual", "Season 1 Casual")):
        ladder = s1.get(key)
        if isinstance(ladder, dict) and ladder.get("rankNumber"):
            return Profile(d.get("name"), d.get("avatarUrl"), ladder["rankNumber"], label,
                           ladder.get("ppScore"), account_id=account_id,
                           calibrated=ladder.get("calibrated"),
                           calibration_matches=_int(ladder.get("calibrationMatches")),
                           region=region)
    return Profile(d.get("name"), d.get("avatarUrl"), d.get("estimatedRankNumber"),
                   "Estimated (pre-matchmaking)", d.get("ppScore"),
                   account_id=account_id, calibrated=None,
                   calibration_matches=_int(d.get("calibrationMatches")), region=region)


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
        self._match_cache: dict[int, dict] = {}  # raw match payloads, primed by batch fetches
        self.rate: dict[str, dict[str, str]] = {"match": {}, "account": {}}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _note_rate_limits(self, headers) -> None:
        for h, v in headers.items():
            hl = h.lower()
            for kind in ("match", "account"):
                if hl.startswith(f"x-ratelimit-{kind}-"):
                    self.rate[kind][hl.split("-")[-1]] = v

    def _rate_summary(self) -> str | None:
        parts = []
        for kind, label in (("match", "match"), ("account", "acct")):
            r = self.rate.get(kind) or {}
            if "remaining" in r and "limit" in r:
                parts.append(f"{label} {r['remaining']}/{r['limit']}")
        return " · ".join(parts) if parts else None

    def status_text(self) -> str:
        """One line for /status; never includes the key itself."""
        if not self.enabled:
            return "not configured (no `STATLOCKER_API_KEY`)"
        if self.last_status_at is None:
            return "key set · no calls yet"
        when = _ago(self.last_status_at)
        rate = self._rate_summary()
        suffix = f" · {rate}" if rate else ""
        if self.last_status == 200:
            return f"OK · last call {when}{suffix}"
        if self.last_status in (401, 403):
            return f"key rejected (HTTP {self.last_status}) · {when}{suffix}"
        if self.last_status is None:
            return f"unreachable · {when}{suffix}"
        return f"HTTP {self.last_status} · {when}{suffix}"

    async def _request(self, method: str, path: str, json_body=None, retries: int = 2):
        if not self.enabled:
            return None
        self.calls += 1
        try:
            for attempt in range(retries + 1):
                async with self.session.request(
                    method, BASE + path,
                    headers={"X-API-Key": self.api_key},
                    json=json_body,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    self.last_status = r.status
                    self.last_status_at = time.time()
                    self._note_rate_limits(r.headers)
                    if r.status == 429 and attempt < retries:
                        await asyncio.sleep(min(float(r.headers.get("Retry-After", 5)), 30))
                        continue
                    if r.status == 200:
                        return await r.json(content_type=None)
                    if r.status in (401, 403):
                        log.error("Statlocker rejected the API key (HTTP %s) for %s", r.status, path)
                    elif r.status != 404:
                        log.warning("Statlocker %s %s -> HTTP %s", method, path, r.status)
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            self.last_status = None
            self.last_status_at = time.time()
            log.warning("Could not reach Statlocker (%s): %s", path, e)
        return None

    async def _get(self, path: str, retries: int = 2):
        return await self._request("GET", path, retries=retries)

    async def match(self, match_id: int) -> dict | None:
        """Raw match payload; cached if a recent batch fetch primed it."""
        cached = self._match_cache.get(match_id)
        if isinstance(cached, dict):
            return cached
        d = await self._get(f"/api/public/match/{match_id}")
        if isinstance(d, dict):
            self._match_cache[match_id] = d
        return d if isinstance(d, dict) else None

    async def matches(self, match_ids: list[int]) -> dict[int, dict]:
        """Batch-fetch matches (max 10 per request) and prime the match cache."""
        ids = [int(i) for i in match_ids]
        need = [i for i in ids if i not in self._match_cache]
        out: dict[int, dict] = {}
        for start in range(0, len(need), 10):
            chunk = need[start:start + 10]
            data = await self._request("POST", "/api/public/matches", json_body=chunk)
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    mid = _int(item.get("matchId") or item.get("match_id"))
                    if mid is not None:
                        self._match_cache[mid] = item
        for i in ids:
            d = self._match_cache.get(i)
            if isinstance(d, dict):
                out[i] = d
        return out

    async def profile(self, account_id: int) -> Profile | None:
        d = await self._get(f"/api/public/profile/{account_id}")
        return parse_profile(d) if isinstance(d, dict) else None

    async def profiles(self, account_ids: list[int]) -> dict[int, Profile]:
        """Batch-fetch profiles (max 100 per request). Order not guaranteed by the API."""
        ids = [int(i) for i in account_ids]
        out: dict[int, Profile] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            data = await self._request("POST", "/api/public/profiles", json_body=chunk)
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    p = parse_profile(item)
                    if p.account_id is not None:
                        out[p.account_id] = p
                    elif (aid := _int(item.get("accountId"))) is not None:
                        out[aid] = p
        return out

    async def lobby(self, match_id: int, account_id: int) -> LobbyInfo | None:
        """None if Statlocker has not processed the match yet (404)."""
        d = await self.match(match_id)
        return parse_lobby(d, account_id) if isinstance(d, dict) else None
