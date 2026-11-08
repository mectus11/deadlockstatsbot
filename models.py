"""Pure data model + parsing of Deadlock API payloads (no network, no Discord).

Field names were matched against the public docs and community projects, not a
live response, so every lookup accepts several spellings and tolerates gaps.
Run `probe.py` on your own PC to print the real shapes if something is missing.
"""
import re
from dataclasses import dataclass, field, fields

IMAGE_HOST = "https://assets-bucket.deadlock-api.com"
HERO_IMAGE_KEYS = ("icon_hero_card", "icon_image_small_webp", "icon_image_small",
                   "minimap_image_webp", "minimap_image")
ITEM_IMAGE_KEYS = ("image_webp", "image", "shop_image_small_webp", "shop_image_small",
                   "shop_image_webp", "shop_image")


@dataclass
class ItemBuy:
    item_id: int
    bought_s: int | None = None
    sold_s: int | None = None


@dataclass
class Match:
    match_id: int
    hero_id: int | None = None
    start_time: int | None = None  # unix seconds
    duration_s: int | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    net_worth: int | None = None
    last_hits: int | None = None
    denies: int | None = None
    won: bool | None = None
    items: list[ItemBuy] = field(default_factory=list)  # final build, purchase order


@dataclass
class Asset:
    id: int
    name: str
    image: str | None = None
    slot: str | None = None  # items only: weapon / vitality / spirit
    tier: int | None = None  # items only


def _pick(d: dict, *names):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _team(v):
    i = _int(v)
    return i if i is not None else (str(v).lower() if v is not None else None)


# --- match history entries ------------------------------------------------
def normalize_entry(e: dict) -> Match | None:
    match_id = _int(_pick(e, "match_id", "matchId"))
    if match_id is None:
        return None
    won = _pick(e, "won", "win", "is_win")
    if won is None:
        winner = _pick(e, "match_result", "winning_team")
        team = _pick(e, "player_team", "team")
        if winner is not None and team is not None:
            won = str(winner) == str(team)
    return Match(
        match_id=match_id,
        hero_id=_int(_pick(e, "hero_id", "heroId")),
        start_time=_int(_pick(e, "start_time", "startTime")),
        duration_s=_int(_pick(e, "match_duration_s", "duration_s", "match_duration", "duration")),
        kills=_int(_pick(e, "player_kills", "kills")),
        deaths=_int(_pick(e, "player_deaths", "deaths")),
        assists=_int(_pick(e, "player_assists", "assists")),
        net_worth=_int(_pick(e, "net_worth", "player_net_worth")),
        last_hits=_int(_pick(e, "last_hits")),
        denies=_int(_pick(e, "denies")),
        won=bool(won) if won is not None else None,
    )


# --- match metadata (per-player stats + items bought) ---------------------
def final_items(buys: list[ItemBuy]) -> list[ItemBuy]:
    """Items still owned at the end (sold ones, and components merged into an
    upgrade, carry a sold time), in purchase order."""
    return sorted((b for b in buys if not b.sold_s), key=lambda b: b.bought_s or 0)


def normalize_metadata(data, account_id: int, match_id: int | None = None) -> Match | None:
    info = data.get("match_info") if isinstance(data, dict) and isinstance(data.get("match_info"), dict) else data
    if not isinstance(info, dict) or not isinstance(info.get("players"), list):
        return None
    me = next((p for p in info["players"]
               if isinstance(p, dict) and _int(p.get("account_id")) == account_id), None)
    if me is None:
        return None
    winner, team = _team(_pick(info, "winning_team")), _team(_pick(me, "team", "player_team"))
    buys = []
    for it in me.get("items") or []:
        iid = _int(_pick(it, "item_id", "upgrade_id", "id")) if isinstance(it, dict) else None
        if iid:
            buys.append(ItemBuy(iid, _int(_pick(it, "game_time_s", "bought_time_s")),
                                _int(_pick(it, "sold_time_s"))))
    return Match(
        match_id=_int(_pick(info, "match_id")) or match_id or 0,
        hero_id=_int(_pick(me, "hero_id")),
        start_time=_int(_pick(info, "start_time")),
        duration_s=_int(_pick(info, "duration_s", "match_duration_s")),
        kills=_int(_pick(me, "kills", "player_kills")),
        deaths=_int(_pick(me, "deaths", "player_deaths")),
        assists=_int(_pick(me, "assists", "player_assists")),
        net_worth=_int(_pick(me, "net_worth")),
        last_hits=_int(_pick(me, "last_hits")),
        denies=_int(_pick(me, "denies")),
        won=None if winner is None or team is None else winner == team,
        items=final_items(buys),
    )


def merge_match(base: Match, detail: Match) -> Match:
    """History entry first, metadata fills the gaps; items come from metadata."""
    vals = {f.name: getattr(base, f.name) if getattr(base, f.name) is not None else getattr(detail, f.name)
            for f in fields(Match) if f.name != "items"}
    return Match(**vals, items=detail.items or base.items)


# --- assets (heroes, items) ------------------------------------------------
def _abs_url(v: str) -> str:
    return v if v.startswith("http") else f"{IMAGE_HOST}/{v.lstrip('/')}"


def pick_image(obj: dict, keys) -> str | None:
    """Best raster image URL (never SVG: Pillow cannot draw it) from an asset."""
    images = obj.get("images") if isinstance(obj.get("images"), dict) else {}

    def ok(v):
        return isinstance(v, str) and v and not v.lower().split("?")[0].endswith(".svg")

    for pool in (images, obj):
        for k in keys:
            if ok(pool.get(k)):
                return _abs_url(pool[k])
    for v in images.values():
        if ok(v) and re.search(r"\.(png|webp|jpe?g)(\?|$)", v, re.I):
            return _abs_url(v)
    return None


def _slot(v) -> str | None:
    s = str(v or "").lower()
    return next((k for k in ("weapon", "vitality", "spirit") if k in s), None)


def _tier(v) -> int | None:
    i = _int(v)
    if i is not None:
        return i
    m = re.search(r"(\d+)\s*$", str(v or ""))
    return int(m.group(1)) if m else None


def parse_assets(data, image_keys) -> dict[int, Asset]:
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), list(data.values()))
    out: dict[int, Asset] = {}
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        aid, name = _int(_pick(d, "id", "hero_id", "item_id")), _pick(d, "name", "hero_name", "display_name")
        if aid is None or not name:
            continue
        out[aid] = Asset(aid, str(name), pick_image(d, image_keys),
                         _slot(_pick(d, "item_slot_type", "slot_type")),
                         _tier(_pick(d, "item_tier", "tier")))
    return out
