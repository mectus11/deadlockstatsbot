"""Pure data model + parsing of Deadlock API payloads (no network, no Discord).

Field names were matched against the public docs and community projects, not a
live response, so every lookup accepts several spellings and tolerates gaps.
Run `probe.py` on your own PC to print the real shapes if something is missing.
"""
import html
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
class MatchStats:
    """End-of-match totals from the metadata `stats` time series (last snapshot)."""
    hero_damage: int | None = None        # player_damage (damage dealt to heroes)
    damage_taken: int | None = None       # player_damage_taken
    healing: int | None = None            # player_healing
    damage_mitigated: int | None = None
    shots_hit: int | None = None
    shots_missed: int | None = None
    hero_bullets_hit: int | None = None
    hero_bullets_hit_crit: int | None = None
    creep_damage: int | None = None
    neutral_damage: int | None = None
    boss_damage: int | None = None        # damage dealt to bosses/objectives
    creep_kills: int | None = None
    possible_creeps: int | None = None
    neutral_kills: int | None = None
    bullet_kills: int | None = None
    melee_kills: int | None = None
    ability_kills: int | None = None
    headshot_kills: int | None = None
    level: int | None = None

    @property
    def accuracy(self) -> float | None:
        """Bullet accuracy in percent, or None when the API didn't report shots."""
        if self.shots_hit is None or self.shots_missed is None:
            return None
        total = self.shots_hit + self.shots_missed
        return self.shots_hit / total * 100 if total else None

    @property
    def crit_rate(self) -> float | None:
        """Share of hero bullet hits that were headshots, in percent."""
        if not self.hero_bullets_hit or self.hero_bullets_hit_crit is None:
            return None
        return self.hero_bullets_hit_crit / self.hero_bullets_hit * 100


@dataclass
class BrawlRound:
    winning_team: int | None = None
    duration_s: int | None = None


@dataclass(frozen=True)
class RosterPlayer:
    """One row of the 12-player match roster. Metadata has no party field, so the
    party is derived from account IDs (see `mate_ids`) intersected with this list."""
    account_id: int
    hero_id: int | None = None
    team: int | str | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    net_worth: int | None = None
    player_slot: int | None = None
    hero_damage: int | None = None
    obj_damage: int | None = None
    damage_taken: int | None = None
    healing: int | None = None
    level: int | None = None
    accuracy: float | None = None


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
    game_mode: int | None = None   # 1 normal, 4 street brawl
    match_mode: int | None = None  # 1 unranked, 3 coop bot, 4 ranked, ...
    ranked_type: int | None = None
    bot_difficulty: int | None = None
    team: int | str | None = None
    level: int | None = None
    stats: MatchStats | None = None
    brawl_rounds: list[BrawlRound] = field(default_factory=list)
    items: list[ItemBuy] = field(default_factory=list)  # final build, purchase order
    buys: list[ItemBuy] = field(default_factory=list)  # every purchase, sold/merged included
    roster: list[RosterPlayer] = field(default_factory=list)  # all players, from metadata only

    @property
    def brawl_score(self) -> tuple[int, int] | None:
        """(own rounds won, enemy rounds won) for a Street Brawl, else None."""
        if not self.brawl_rounds or self.team is None:
            return None
        own = sum(1 for r in self.brawl_rounds if str(r.winning_team) == str(self.team))
        return own, len(self.brawl_rounds) - own

    @property
    def brawl_avg_round_s(self) -> int | None:
        durations = [r.duration_s for r in self.brawl_rounds if r.duration_s]
        return round(sum(durations) / len(durations)) if durations else None


@dataclass
class Asset:
    id: int
    name: str
    image: str | None = None
    slot: str | None = None  # items only: weapon / vitality / spirit
    tier: int | None = None  # items only
    description: str | None = None  # items only: plain text, markup stripped


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


def _str(v):
    return str(v) if isinstance(v, str) and v else None


def _team(v):
    i = _int(v)
    return i if i is not None else (str(v).lower() if v is not None else None)


# --- match type ------------------------------------------------------------
# Only 1 (normal) and 4 (street brawl) are confirmed by live payloads; the rest are
# best-effort labels from the Source 2 enums. Unknown values fall back to "Unknown".
GAME_MODE_NAMES = {1: "Standard", 4: "Street Brawl"}
MATCH_MODE_NAMES = {1: "Standard", 2: "Private Lobby", 3: "Bot Match", 4: "Ranked",
                    5: "Server Test", 6: "Tutorial", 7: "Hero Labs", 8: "New Player"}
BOT_DIFFICULTY_NAMES = {1: "Easy", 2: "Medium", 3: "Hard", 4: "Nightmare", 5: "Guided"}


def match_type(m: Match) -> tuple[str, str]:
    """(kind, label) for the card pill. kind drives the colour: ranked/brawl/bot/standard/other."""
    brawl = m.game_mode == 4
    if m.match_mode == 4:
        return "ranked", "Ranked"
    if m.match_mode == 3:  # coop bot, which can also be played on the brawl map
        difficulty = BOT_DIFFICULTY_NAMES.get(m.bot_difficulty)
        if brawl:
            return "brawl", "Street Brawl · " + (f"{difficulty} Bots" if difficulty else "Bots")
        return "bot", "Bot Match" + (f" · {difficulty}" if difficulty else "")
    if brawl:
        return "brawl", "Street Brawl"
    if m.match_mode == 1:
        return "standard", "Standard"
    if m.match_mode in MATCH_MODE_NAMES:
        return "other", MATCH_MODE_NAMES[m.match_mode]
    if m.game_mode in GAME_MODE_NAMES:
        return "other", GAME_MODE_NAMES[m.game_mode]
    return "other", "Unknown"


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
        game_mode=_int(_pick(e, "game_mode", "gameMode")),
        match_mode=_int(_pick(e, "match_mode", "matchMode")),
        team=_team(_pick(e, "player_team", "team")),
    )


# --- match metadata (per-player stats + items bought) ---------------------
def final_items(buys: list[ItemBuy]) -> list[ItemBuy]:
    """Items still owned at the end (sold ones, and components merged into an
    upgrade, carry a sold time), in purchase order."""
    return sorted((b for b in buys if not b.sold_s), key=lambda b: b.bought_s or 0)


def _last_stats(me: dict) -> MatchStats | None:
    """End-of-match totals: the stats array is a ~180s time series, so take the
    snapshot with the highest timestamp (not blindly the last element)."""
    snapshots = [s for s in (me.get("stats") or []) if isinstance(s, dict)]
    if not snapshots:
        return None
    s = max(snapshots, key=lambda x: _int(x.get("time_stamp_s")) or 0)

    def g(*names):
        return _int(_pick(s, *names))

    return MatchStats(
        hero_damage=g("player_damage"),
        damage_taken=g("player_damage_taken"),
        healing=g("player_healing"),
        damage_mitigated=g("damage_mitigated"),
        shots_hit=g("shots_hit"),
        shots_missed=g("shots_missed"),
        hero_bullets_hit=g("hero_bullets_hit"),
        hero_bullets_hit_crit=g("hero_bullets_hit_crit"),
        creep_damage=g("creep_damage"),
        neutral_damage=g("neutral_damage"),
        boss_damage=g("boss_damage"),
        creep_kills=g("creep_kills"),
        possible_creeps=g("possible_creeps"),
        neutral_kills=g("neutral_kills"),
        bullet_kills=g("bullet_kills"),
        melee_kills=g("melee_kills"),
        ability_kills=g("ability_kills"),
        headshot_kills=g("headshot_kills"),
        level=g("level"),
    )


def _brawl_rounds(info: dict) -> list[BrawlRound]:
    out = []
    for r in info.get("street_brawl_rounds") or []:
        if isinstance(r, dict):
            out.append(BrawlRound(_int(_pick(r, "winning_team")),
                                  _int(_pick(r, "round_duration_s", "duration_s"))))
    return out


def normalize_metadata(data, account_id: int, match_id: int | None = None) -> Match | None:
    info = data.get("match_info") if isinstance(data, dict) and isinstance(data.get("match_info"), dict) else data
    if not isinstance(info, dict) or not isinstance(info.get("players"), list):
        return None
    me = next((p for p in info["players"]
               if isinstance(p, dict) and _int(p.get("account_id")) == account_id), None)
    if me is None:
        return None
    winner, team = _team(_pick(info, "winning_team")), _team(_pick(me, "team", "player_team"))
    stats = _last_stats(me)
    roster = []
    for p in info["players"]:
        if not isinstance(p, dict):
            continue
        aid = _int(p.get("account_id"))
        if aid is None:
            continue
        ps = _last_stats(p)
        acc = None
        if ps and ps.shots_hit is not None and ps.shots_missed is not None:
            total = ps.shots_hit + ps.shots_missed
            acc = ps.shots_hit / total * 100 if total else None
        roster.append(RosterPlayer(
            account_id=aid,
            hero_id=_int(_pick(p, "hero_id")),
            team=_team(_pick(p, "team", "player_team")),
            kills=_int(_pick(p, "kills", "player_kills")),
            deaths=_int(_pick(p, "deaths", "player_deaths")),
            assists=_int(_pick(p, "assists", "player_assists")),
            net_worth=_int(_pick(p, "net_worth")),
            player_slot=_int(_pick(p, "player_slot")),
            hero_damage=ps.hero_damage if ps else None,
            obj_damage=ps.boss_damage if ps else None,
            damage_taken=ps.damage_taken if ps else None,
            healing=ps.healing if ps else None,
            level=_int(_pick(p, "level")) or (ps.level if ps else None),
            accuracy=acc,
        ))
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
        game_mode=_int(_pick(info, "game_mode", "gameMode")),
        match_mode=_int(_pick(info, "match_mode", "matchMode")),
        ranked_type=_int(_pick(info, "ranked_type")),
        bot_difficulty=_int(_pick(info, "bot_difficulty")),
        team=team,
        level=_int(_pick(me, "level")) or (stats.level if stats else None),
        stats=stats,
        brawl_rounds=_brawl_rounds(info),
        items=final_items(buys),
        buys=buys,
        roster=roster,
    )


def merge_match(base: Match, detail: Match) -> Match:
    """History entry first, metadata fills the gaps; items and purchases come from metadata."""
    vals = {}
    for f in fields(Match):
        if f.name in ("items", "buys"):
            continue
        b, d = getattr(base, f.name), getattr(detail, f.name)
        # an empty list is "missing" for brawl rounds and the roster, otherwise it
        # would shadow the metadata
        vals[f.name] = (b or d) if f.name in ("brawl_rounds", "roster") else (
            b if b is not None else d)
    return Match(**vals, items=detail.items or base.items, buys=detail.buys or base.buys)


def mate_ids(rows, match_id: int) -> set[int]:
    """Accounts the API considers same-party for one match, from a mate-stats
    response. Keeps only rows whose `matches` list contains this match."""
    out = set()
    for row in (rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        aid = _int(_pick(row, "mate_id", "account_id"))
        if aid is None:
            continue
        ids = row.get("matches")
        if isinstance(ids, list) and ids and _int(match_id) not in {_int(x) for x in ids}:
            continue
        out.add(aid)
    return out


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


_SVG = re.compile(r"(?is)<svg.*?</svg>")
_TAG = re.compile(r"<[^>]+>")


def _plain_html(text) -> str:
    """Item descriptions carry <span> markup and inline <svg> icons; keep the words."""
    text = _SVG.sub(" ", str(text or ""))
    text = _TAG.sub(" ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    # removing an inline icon leaves a space before the punctuation that followed it
    return re.sub(r"\s+([,.;:!?%)\]])", r"\1", re.sub(r"\(\s+", "(", text))


def _item_description(d: dict) -> str | None:
    """Prose for one item: the `description` table first, else the tooltip sections'
    localised strings. Stat-only items (Extended Magazine) have neither."""
    desc = d.get("description")
    text = " ".join(str(v) for v in desc.values() if v) if isinstance(desc, dict) \
        else str(desc or "")
    if not text.strip():
        text = " ".join(
            a.get("loc_string") or ""
            for s in d.get("tooltip_sections") or [] if isinstance(s, dict)
            for a in s.get("section_attributes") or [] if isinstance(a, dict))
    return _plain_html(text) or None


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
                         _tier(_pick(d, "item_tier", "tier")),
                         _item_description(d))
    return out


# --- player identity (Steam profile + card name/avatar) --------------------
@dataclass(frozen=True)
class SteamProfile:
    name: str | None
    avatar_url: str | None
    profile_url: str | None = None


def parse_steam_profile(payload) -> SteamProfile | None:
    """One row from /v1/players/steam (the endpoint returns a list; tolerate a
    single object and simple wrappers). Avatar: avatarfull is the largest Steam offers."""
    if isinstance(payload, list):
        row = next((x for x in payload if isinstance(x, dict)), None)
    elif isinstance(payload, dict):
        if any(k in payload for k in ("personaname", "persona_name", "avatarfull", "account_id")):
            row = payload
        else:
            row = next((x for x in payload.values() if isinstance(x, dict)), None)
            if row is None:
                row = next((x for v in payload.values() if isinstance(v, list)
                            for x in v if isinstance(x, dict)), None)
    else:
        row = None
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "personaname", "persona_name", "name"))
    avatar = _str(_pick(row, "avatarfull", "avatarmedium", "avatar"))
    if not name and not avatar:
        return None
    return SteamProfile(name, avatar, _str(_pick(row, "profileurl", "profile_url")))


def parse_steam_profiles(payload) -> dict[int, SteamProfile]:
    """Batch form of parse_steam_profile, keyed by account_id. Rows without an
    account ID or a usable persona/avatar are skipped."""
    rows = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        aid = _int(_pick(row, "account_id", "accountId"))
        profile = parse_steam_profile(row)
        if aid is not None and profile:
            out[aid] = profile
    return out


@dataclass(frozen=True)
class Identity:
    name: str
    avatar_url: str | None = None


def resolve_identity(statlocker, steam: SteamProfile | None, fallback_name: str | None,
                     account_id: int) -> Identity:
    """Card name + avatar. The name keeps its existing precedence (Statlocker, then
    the saved name, then Steam, then "Account N"); Steam owns the avatar because it
    works without a Statlocker key, with Statlocker as the fallback."""
    name = ((getattr(statlocker, "name", None) if statlocker else None)
            or fallback_name
            or (steam.name if steam else None)
            or f"Account {account_id}")
    avatar = (steam.avatar_url if steam else None) or \
        (getattr(statlocker, "avatar_url", None) if statlocker else None)
    return Identity(str(name), avatar or None)


# --- match ingest status (/matchstatus) ------------------------------------
@dataclass(frozen=True)
class Salts:
    match_id: int | None
    metadata_salt: int | None = None
    replay_salt: int | None = None
    metadata_url: str | None = None
    demo_url: str | None = None

    @property
    def has_demo(self) -> bool:
        return bool(self.demo_url)


def parse_salts(data) -> Salts | None:
    if not isinstance(data, dict):
        return None
    if not any(k in data for k in ("metadata_salt", "replay_salt", "metadata_url", "demo_url")):
        return None
    return Salts(_int(_pick(data, "match_id")), _int(_pick(data, "metadata_salt")),
                 _int(_pick(data, "replay_salt")), _str(_pick(data, "metadata_url")),
                 _str(_pick(data, "demo_url")))


@dataclass(frozen=True)
class MatchInfo:
    match_id: int
    start_time: str | None = None  # "YYYY-MM-DD HH:MM:SS" (UTC)
    duration_s: int | None = None
    match_mode: str | None = None
    game_mode: str | None = None
    winning_team: str | None = None

    @property
    def mode_label(self) -> str:
        if (self.game_mode or "").lower() in ("streetbrawl", "street_brawl"):
            return "Street Brawl"
        return self.match_mode or self.game_mode or "Unknown"


def parse_match_info(data) -> MatchInfo | None:
    """Row from /v1/matches/metadata?include_info=true (a list, or a single object)."""
    row = next((x for x in data if isinstance(x, dict)), None) if isinstance(data, list) else \
        (data if isinstance(data, dict) else None)
    if not isinstance(row, dict):
        return None
    match_id = _int(_pick(row, "match_id"))
    if match_id is None:
        return None
    return MatchInfo(match_id, _str(_pick(row, "start_time")), _int(_pick(row, "duration_s")),
                     _str(_pick(row, "match_mode")), _str(_pick(row, "game_mode")),
                     _str(_pick(row, "winning_team")))


@dataclass(frozen=True)
class IngestVerdict:
    kind: str  # full / partial / queued / processing / missing / unreachable
    emoji: str
    title: str
    detail: str


def ingest_verdict(info_status: int | None, metadata_ok: bool | None,
                   salts_status: int | None, recent: bool | None) -> IngestVerdict:
    """Map the public ingest signals to one verdict. Status is the HTTP code, or None
    when the API could not be reached. `recent` is the last-10-minutes feed (None = not checked)."""
    if info_status == 200:
        if metadata_ok:
            return IngestVerdict("full", "✅", "Ingested",
                                 "Scoreboard and item/stats metadata are available.")
        if metadata_ok is False:
            return IngestVerdict("partial", "🟡", "Partially ingested",
                                 "Scoreboard is in, but item/stats metadata is still pending.")
        return IngestVerdict("full", "✅", "Ingested (scoreboard)",
                             "Scoreboard is in; item/stats metadata could not be checked.")
    if salts_status == 200:
        return IngestVerdict("queued", "🟡", "Queued",
                             "The API knows this match (salts stored) but has not ingested it yet.")
    if info_status is None or salts_status is None:
        return IngestVerdict("unreachable", "⚠️", "API unreachable",
                             "Could not reach api.deadlock-api.com; try again in a minute.")
    if recent:
        return IngestVerdict("processing", "🟡", "Processing",
                             "Fetched within the last 10 minutes; ingestion is still in progress.")
    return IngestVerdict("missing", "❌", "Not ingested",
                         "No scoreboard, salts, or recent fetch for this match id.")
