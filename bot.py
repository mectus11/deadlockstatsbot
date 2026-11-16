"""Deadlock stats Discord bot.

Commands
  /save <steamid> [name]   save your Steam account; /stats and /latestmatch then use it right away
  /link <steamid> [name]   same as /save, and also enables automatic posting of new matches
  /unlink                  forget your saved account, and stop auto-posting for you
  /setchannel [channel]    (admins) choose the channel for automatic match cards and patch notes
  /stats [steamid]         overall stats card (defaults to your saved account)
  /latestmatch [steamid]   latest match card (defaults to your saved account)
  /matches [steamid] [count]  compact list of recent matches: result, hero, mode, KDA (default 10)
  /matchstatus [match_id]  check whether the Deadlock API has ingested a match (or it is queued)
  /patchnotes [count]      post the latest Deadlock patch notes from Steam (default 1, max 3)
  /pause, /resume          stop/start automatic match posts for your linked account
  /settings show|channel   (admins) see and route match cards and patch notes per feature
  /help, /status, /invite  command help, bot/API health, and the bot's invite link
"""
import asyncio
import io
import logging
import os
import time
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import cards
import theme as T
from db import DB
from deadlock_api import ApiUnavailable, DeadlockAPI
from images import ImageStore
from local_icons import LocalIcons
from models import Match, ingest_verdict, match_type, merge_match, resolve_identity
from patchnote_views import HeroChangesView, PatchNoteView
from statlocker import (Profile, RankSnapshot, StatlockerAPI, format_top, rank_badge_url,
                        rank_name)
from stats import compute_overall
from steam import STEAM_INPUT_HELP, find_steam_input, parse_steam_input
from steam_news import fetch_feed, find_new_notes, is_patch_note, render_patch_content

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

SHOP_SLOTS = {"weapon", "vitality", "spirit"}  # excludes hero ability upgrades, which share the items catalogue
MAX_AUTOPOST_PER_CYCLE = 3  # per player, so a long gap never floods the channel
MAX_WAIT_FOR_ITEMS = 5      # poll cycles to wait for a match's item data before posting without it
PATCH_COOLDOWN = 30         # seconds between manual /patchnotes posts in the same channel
# shortcut: backfill covers the newest match per account for 12 h, 3 tries; raise if Statlocker lags longer
BACKFILL_WINDOW = 12 * 3600
BACKFILL_ATTEMPTS = 3
BACKFILL_PER_CYCLE = 3
_GREEN = discord.Colour.from_rgb(*T.GREEN)
_RED = discord.Colour.from_rgb(*T.RED)
_BRASS = discord.Colour.from_rgb(*T.BRASS)
UNREACHABLE_MSG = ("I couldn't reach api.deadlock-api.com (network or DNS problem on the bot's computer). "
                   "Check the internet connection and try again in a minute.")
_SHORT_MODE = {"ranked": "Ranked", "brawl": "Brawl", "bot": "Bot", "standard": "Standard"}


def _short_mode(m: Match) -> str:
    """Compact label for the /matches list (the latest-match card uses the full one)."""
    kind, label = match_type(m)
    return _SHORT_MODE.get(kind, label)


class StatsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = DB(os.getenv("DB_PATH", "bot.db"))
        self.poll_seconds = max(30, int(os.getenv("POLL_SECONDS", "120")))
        self.patch_poll_seconds = max(300, int(os.getenv("PATCH_POLL_SECONDS", "600")))
        self.patch_cooldown: dict[int, float] = {}  # channel id -> last manual /patchnotes post
        self.waits: dict[tuple, int] = defaultdict(int)  # (user, guild, match) -> cycles waited for items
        self.session: aiohttp.ClientSession | None = None
        self.started_at = time.time()
        self.last_match_poll: float | None = None
        self.last_match_poll_ok: bool | None = None
        self.last_patch_poll: float | None = None
        self.last_patch_poll_ok: bool | None = None
        self._profiles: dict[int, tuple[float, Profile | None]] = {}  # /top cache: account -> (at, profile)

    async def setup_hook(self):
        log.info("starting — build %s", os.getenv("BUILD_SHA", "dev"))
        self.session = aiohttp.ClientSession(headers={"User-Agent": "deadlockstatsbot/2.0"})
        self.dl = DeadlockAPI(self.session, os.getenv("DEADLOCK_API_KEY") or None)
        self.sl = StatlockerAPI(self.session, os.getenv("STATLOCKER_API_KEY") or None)
        self.images = ImageStore(self.session, os.getenv("CACHE_DIR", "cache"))
        self.local = LocalIcons(os.getenv("LOCAL_ITEM_ICONS", "assets/items"))
        if self.local.files:
            log.info("Using %d local item icons from %s", len(self.local.files),
                     os.getenv("LOCAL_ITEM_ICONS", "assets/items"))
            asyncio.create_task(self._log_local_icon_coverage())
        if not self.sl.enabled:
            log.warning("STATLOCKER_API_KEY not set: cards will show match data only.")
        await self.tree.sync()
        self.poll_matches.change_interval(seconds=self.poll_seconds)
        self.poll_matches.start()
        self.poll_patches.change_interval(seconds=self.patch_poll_seconds)
        self.poll_patches.start()
        if self.sl.enabled:
            self.backfill_cards.start()  # only useful (and only allowed) with Statlocker access
        self.add_dynamic_items(ScoreboardButton)
        # Re-register the Hero Changes buttons of notes posted before this restart. The Steam
        # link button needs no handler, so only the Hero button view has to be restored.
        stored = [note for note in self.db.all_patch_notes() if note and note.heroes]
        for note in stored:
            self.add_view(HeroChangesView(self, note.guid, len(note.heroes)))
        if stored:
            log.info("patch notes: re-registered %d stored note(s) with hero changes", len(stored))

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    # --- card building ------------------------------------------------------
    async def _log_local_icon_coverage(self):
        """Runs once at startup: how many of your assets/items files actually match a real
        Deadlock item, so a naming/folder problem shows up in the logs without running probe.py."""
        try:
            catalogue = await self.dl.items()
        except Exception:
            return  # network hiccup at boot; not worth logging as an error
        if not catalogue:
            return
        shop = {a.id: a for a in catalogue.values() if a.slot in SHOP_SLOTS}
        found_keys = [self.local.match_key(a.name, a.tier) for a in shop.values()]
        matched = sum(1 for k in found_keys if k)
        matched_keys = {k for k in found_keys if k}
        orphans = [str(p.name) for key, p in self.local.files.items() if key not in matched_keys]
        log.info("local item icons: %d/%d real shop items matched, %d local file(s) matched nothing",
                 matched, len(shop), len(orphans))
        if orphans:
            log.info("unmatched local files (first 15, check names against the real item list): %s",
                     ", ".join(sorted(orphans)[:15]))

    async def build_stats_card(self, account_id: int, history: list[Match] | None = None,
                               fallback_name: str | None = None):
        history = history if history is not None else await self.dl.match_history(account_id)
        profile, steam = await asyncio.gather(self.sl.profile(account_id),
                                              self.dl.steam_profile(account_id))
        if not history and not profile:
            return None
        identity = resolve_identity(profile, steam, fallback_name, account_id)
        overall = compute_overall(history)
        hero_cat = await self.dl.heroes()
        top = [(h, hero_cat.get(h.hero_id)) for h in overall.top_heroes]
        pics = await self.images.get_many(
            [a.image if a else None for _, a in top]
            + [identity.avatar_url, rank_badge_url(profile.rank_number) if profile else None])
        return cards.render_stats_card(cards.StatsCardData(
            name=identity.name,
            account_id=account_id,
            overall=overall,
            heroes=[cards.HeroRow(a.name if a else f"Hero #{h.hero_id}", h.games, h.winrate,
                                  pics.get(a.image) if a else None) for h, a in top],
            avatar=pics.get(identity.avatar_url) if identity.avatar_url else None,
            rank_name=rank_name(profile.rank_number) if profile else None,
            rank_badge=pics.get(rank_badge_url(profile.rank_number)) if profile else None,
            pp_score=profile.pp_score if profile else None,
            statlocker=profile is not None,
        ))

    async def build_matches_card(self, account_id: int, history: list[Match] | None = None,
                                 fallback_name: str | None = None, count: int = 10):
        """Compact list of the last `count` matches — result, hero, mode, KDA. Unlike /latestmatch,
        this only needs the match-history entries themselves (already has K/D/A/hero/duration),
        so it's a single fast request with no per-match detail lookups."""
        history = history if history is not None else await self.dl.match_history(account_id)
        profile, steam = await asyncio.gather(self.sl.profile(account_id),
                                              self.dl.steam_profile(account_id))
        if not history and not profile:
            return None
        identity = resolve_identity(profile, steam, fallback_name, account_id)
        recent = history[:count]
        hero_cat = await self.dl.heroes()
        heroes = [hero_cat.get(m.hero_id) for m in recent]
        pics = await self.images.get_many([h.image if h else None for h in heroes] +
                                          [identity.avatar_url])
        return cards.render_matches_card(cards.MatchesCardData(
            name=identity.name,
            account_id=account_id,
            matches=[cards.MatchRow(
                hero=h.name if h else f"Hero #{m.hero_id}", won=m.won, kills=m.kills, deaths=m.deaths,
                assists=m.assists, match_id=m.match_id, start_time=m.start_time, duration_s=m.duration_s,
                hero_icon=pics.get(h.image) if h else None,
                mode=_short_mode(m), mode_kind=match_type(m)[0],
            ) for m, h in zip(recent, heroes)],
            avatar=pics.get(identity.avatar_url) if identity.avatar_url else None,
            statlocker=profile is not None,
        ))

    async def load_match(self, account_id: int, m: Match) -> Match:
        """History entry enriched with per-player stats and items from match metadata."""
        detail = await self.dl.match_detail(m.match_id, account_id)
        return merge_match(m, detail) if detail else m

    async def cached_profile(self, account_id: int, ttl: int = 600) -> Profile | None:
        """Profile with a TTL cache, for /top: one ladder must not spend one API call per
        linked player on every use. Match cards stay uncached — a stale PP would skew deltas."""
        hit = self._profiles.get(account_id)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        profile = await self.sl.profile(account_id)
        self._profiles[account_id] = (time.time(), profile)
        return profile

    async def _build_scoreboard(self, account_id: int, m: Match, heroes: dict | None = None):
        """Build a full scoreboard from roster+metadata (+ Statlocker enrichment), or None."""
        if not m.roster:
            return None
        if heroes is None:
            heroes = await self.dl.heroes()
        mates = await self.dl.mate_stats(account_id, m.match_id, m.game_mode)
        party_ids = {p.account_id for p in m.roster
                     if p.account_id in mates and p.account_id != account_id
                     and (m.team is None or p.team == m.team)}
        sl_players: dict[int, object] = {}
        if self.sl.enabled:
            sl_match = await self.sl.match(m.match_id)
            if isinstance(sl_match, dict):
                from statlocker import parse_match_players
                sl_players = {p.account_id: p for p in parse_match_players(sl_match)}
        roster_ids = [p.account_id for p in m.roster]
        steam_profiles = await self.dl.steam_profiles(roster_ids)
        rank_urls: dict[int, str | None] = {}
        avatar_urls: dict[int, str | None] = {}
        for rp in m.roster:
            sp = sl_players.get(rp.account_id)
            steam_p = steam_profiles.get(rp.account_id)
            rank_urls[rp.account_id] = rank_badge_url(getattr(sp, "rank_number", None))
            avatar_urls[rp.account_id] = steam_p.avatar_url if steam_p else None
        hero_urls = [(heroes.get(rp.hero_id).image if heroes.get(rp.hero_id) else None) for rp in m.roster]
        badge_urls = [u for u in rank_urls.values() if u]
        board_pics = await self.images.get_many([u for u in hero_urls if u] + badge_urls
                                                + [u for u in avatar_urls.values() if u])
        own_team = m.team
        def slot_key(p):
            try:
                return int(p.player_slot) if p.player_slot is not None else 99
            except (TypeError, ValueError):
                return 99
        own_order = [p for p in m.roster if own_team is not None and p.team == own_team]
        enemy_order = [p for p in m.roster if own_team is not None and p.team != own_team]
        if own_team is None:
            own_order, enemy_order = list(m.roster), []
        def rows_for(order):
            out = []
            for rp in sorted(order, key=slot_key):
                h = heroes.get(rp.hero_id)
                sp = sl_players.get(rp.account_id)
                steam_p = steam_profiles.get(rp.account_id)
                name = (getattr(sp, "name", None) or (steam_p.name if steam_p else None)
                        or f"Account {rp.account_id}")
                hero_name = h.name if h else (f"Hero #{rp.hero_id}" if rp.hero_id is not None else "Unknown hero")
                rbadge = board_pics.get(rank_urls.get(rp.account_id)) if rank_urls.get(rp.account_id) else None
                hicon = board_pics.get(h.image) if h and h.image else None
                av = board_pics.get(avatar_urls.get(rp.account_id)) if avatar_urls.get(rp.account_id) else None
                sp_acc = getattr(sp, "accuracy", None)
                out.append(cards.ScoreRow(
                    hero=hero_name, name=name,
                    kills=rp.kills, deaths=rp.deaths, assists=rp.assists,
                    net_worth=rp.net_worth,
                    damage=getattr(sp, "hero_damage", None) if getattr(sp, "hero_damage", None) is not None else getattr(rp, "hero_damage", None),
                    hero_icon=hicon, rank_badge=rbadge, avatar=av,
                    is_self=(rp.account_id == account_id),
                    is_party=(rp.account_id in party_ids),
                    mvp=(getattr(sp, "team_mvp_place", None) == 1),
                    global_rank=getattr(sp, "global_rank", None),
                    kill_part=getattr(sp, "kill_part", None),
                    obj_damage=getattr(sp, "obj_damage", None) if getattr(sp, "obj_damage", None) is not None else getattr(rp, "obj_damage", None),
                    dmg_per_min=getattr(sp, "dmg_per_min", None),
                    souls_per_min=getattr(sp, "souls_per_min", None),
                    accuracy=sp_acc if sp_acc is not None else getattr(rp, "accuracy", None),
                    damage_taken=getattr(sp, "damage_taken", None) if getattr(sp, "damage_taken", None) is not None else getattr(rp, "damage_taken", None),
                    healing=getattr(sp, "healing", None) if getattr(sp, "healing", None) is not None else getattr(rp, "healing", None),
                    level=getattr(sp, "level", None) if getattr(sp, "level", None) is not None else getattr(rp, "level", None),
                ))
            return out
        own_rows = rows_for(own_order)
        enemy_rows = rows_for(enemy_order)
        own_label, enemy_label = "Your Team", "Enemy Team"
        if sl_players:
            me = sl_players.get(account_id)
            my_team = str(getattr(me, "team", "") or "").lower() if me else ""
            if "amber" in my_team:
                own_label, enemy_label = "Amber Hand", "Sapphire Flame"
            elif "sapphire" in my_team:
                own_label, enemy_label = "Sapphire Flame", "Amber Hand"
        return cards.Scoreboard(
            own=cards.ScoreTeam(own_label, m.won, own_rows),
            enemy=cards.ScoreTeam(enemy_label, (not m.won) if m.won is not None else None, enemy_rows),
        )

    async def build_scoreboard_card(self, account_id: int, match_id: int,
                                    fallback_name: str | None = None):
        """Re-fetch and render the separate scoreboard card for a match, or None."""
        history = await self.dl.match_history(account_id)
        base = next((m for m in history if m.match_id == match_id), None)
        if base is None:
            return None
        m = await self.load_match(account_id, base)
        heroes = await self.dl.heroes()
        board = await self._build_scoreboard(account_id, m, heroes=heroes)
        if board is None:
            return None
        profile, steam = await asyncio.gather(self.sl.profile(account_id), self.dl.steam_profile(account_id))
        name = fallback_name or (steam.name if steam else None) or (profile.name if profile else None) or f"Account {account_id}"
        hero = heroes.get(m.hero_id)
        mode_kind, mode = match_type(m)
        return cards.render_scoreboard_card(cards.ScoreboardCardData(
            match_id=m.match_id,
            hero=hero.name if hero else await self.dl.hero_name(m.hero_id),
            name=name, won=m.won, mode=mode, duration_s=m.duration_s, scoreboard=board,
        ))

    async def render_match(self, account_id: int, m: Match, fallback_name: str | None = None,
                           track: tuple[int, int] | None = None,
                           snapshot: RankSnapshot | None = None,
                           ) -> tuple[io.BytesIO, RankSnapshot, bool]:
        """Render a match card and return (png, PP/rank snapshot, has_scoreboard).

        track=(discord_id, guild_id): compares PP to the value stored for this player's own
        account and remembers the new one, so the next match card can show the change. Only pass
        this for the player's own card, never someone else's steamid.
        snapshot: re-render with the PP/rank the card already had (backfill); the stored
        baseline is neither read nor written then."""
        profile, lobby, steam = await asyncio.gather(
            self.sl.profile(account_id), self.sl.lobby(m.match_id, account_id),
            self.dl.steam_profile(account_id))
        identity = resolve_identity(profile, steam, fallback_name, account_id)
        snap = snapshot or RankSnapshot()
        if track and profile and profile.pp_score is not None:
            baseline = self.db.get_link(*track)
            if baseline:
                snap = RankSnapshot(baseline.last_pp_score, profile.pp_score,
                                    baseline.last_rank_number, profile.rank_number)
            self.db.set_rank_baseline(*track, profile.pp_score, profile.rank_number)
        # Statlocker can lag a fresh match: keep what the card already showed rather than
        # dropping its rank/PP when the profile has none yet.
        pp_after = profile.pp_score if profile and profile.pp_score is not None else snap.pp_after
        rank_after = profile.rank_number if profile and profile.rank_number is not None else snap.rank_after
        snap = RankSnapshot(snap.pp_before, pp_after, snap.rank_before, rank_after)
        heroes, catalogue = await asyncio.gather(self.dl.heroes(), self.dl.items())
        hero = heroes.get(m.hero_id)
        # the items catalogue also lists hero ability upgrades (spent with ability points, not bought
        # from the shop); only weapon/vitality/spirit entries are real purchases for this grid
        catalogued = [(b, catalogue.get(b.item_id)) for b in m.items]
        known = [a for _, a in catalogued if a and a.slot in SHOP_SLOTS]
        unknown = sum(1 for _, a in catalogued if a is None)
        own = {a.id: self.local.get(a.name, a.tier) for a in known}  # your files in assets/items win

        # party (mates on same team, not self) for the match card
        party_views: list[cards.PartyMemberView] = []
        if m.roster:
            mates = await self.dl.mate_stats(account_id, m.match_id, m.game_mode)
            party_ids = {p.account_id for p in m.roster
                         if p.account_id in mates and p.account_id != account_id
                         and (m.team is None or p.team == m.team)}
            if party_ids:
                roster_ids = [p.account_id for p in m.roster]
                steam_profiles = await self.dl.steam_profiles(roster_ids)
                hero_urls = [heroes.get(p.hero_id).image if heroes.get(p.hero_id) else None for p in m.roster if p.account_id in party_ids]
                avatar_urls = [steam_profiles[p.account_id].avatar_url for p in m.roster
                               if p.account_id in party_ids and steam_profiles.get(p.account_id) and steam_profiles[p.account_id].avatar_url]
                pics_party = await self.images.get_many([u for u in hero_urls if u] + [u for u in avatar_urls if u])
                def slot_key(p):
                    try:
                        return int(p.player_slot) if p.player_slot is not None else 99
                    except (TypeError, ValueError):
                        return 99
                for rp in sorted([p for p in m.roster if p.account_id in party_ids], key=slot_key):
                    h = heroes.get(rp.hero_id)
                    steam_p = steam_profiles.get(rp.account_id)
                    pname = (steam_p.name if steam_p else None) or f"Account {rp.account_id}"
                    hero_name = h.name if h else (f"Hero #{rp.hero_id}" if rp.hero_id is not None else "Unknown hero")
                    hicon = pics_party.get(h.image) if h and h.image else None
                    av = pics_party.get(steam_p.avatar_url) if steam_p and steam_p.avatar_url else None
                    party_views.append(cards.PartyMemberView(
                        hero=hero_name, name=pname,
                        kills=rp.kills, deaths=rp.deaths, assists=rp.assists,
                        net_worth=rp.net_worth, hero_icon=hicon, avatar=av,
                    ))

        has_scoreboard = bool(m.roster)
        badge_url = rank_badge_url(snap.rank_after)
        own_lobby_badge_url = rank_badge_url(lobby.own_rank) if lobby else None
        enemy_lobby_badge_url = rank_badge_url(lobby.enemy_rank) if lobby else None
        pics = await self.images.get_many([hero.image if hero else None, identity.avatar_url,
                                           badge_url, own_lobby_badge_url, enemy_lobby_badge_url,
                                           *(a.image for a in known if not own[a.id])])
        mode_kind, mode = match_type(m)
        return cards.render_match_card(cards.MatchCardData(
            name=identity.name,
            hero=hero.name if hero else await self.dl.hero_name(m.hero_id),
            match_id=m.match_id, won=m.won, kills=m.kills, deaths=m.deaths, assists=m.assists,
            duration_s=m.duration_s, start_time=m.start_time, net_worth=m.net_worth,
            last_hits=m.last_hits, denies=m.denies,
            mode=mode, mode_kind=mode_kind, stats=m.stats,
            brawl_score=m.brawl_score, brawl_round_avg_s=m.brawl_avg_round_s if m.brawl_score else None,
            hero_icon=pics.get(hero.image) if hero else None,
            avatar=pics.get(identity.avatar_url) if identity.avatar_url else None,
            items=[cards.ItemView(a.name, own[a.id] or pics.get(a.image), a.slot, a.tier) for a in known],
            unnamed_items=unknown,
            own_lobby_rank=rank_name(lobby.own_rank) if lobby else None,
            enemy_lobby_rank=rank_name(lobby.enemy_rank) if lobby else None,
            own_lobby_rank_badge=pics.get(own_lobby_badge_url) if own_lobby_badge_url else None,
            enemy_lobby_rank_badge=pics.get(enemy_lobby_badge_url) if enemy_lobby_badge_url else None,
            rank_name=rank_name(snap.rank_after),
            rank_prev=rank_name(snap.rank_before) if snap.rank_changed else None,
            rank_up=snap.promoted if snap.rank_changed else None,
            rank_badge=pics.get(badge_url) if badge_url else None,
            pp_score=snap.pp_after,
            pp_delta=snap.pp_delta,
            party=party_views,
            statlocker=bool(profile or lobby),
        )), snap, has_scoreboard

    # --- match cards ---------------------------------------------------------
    async def post_match_card(self, target, account_id: int, m: Match, *,
                              display_name: str | None = None,
                              track: tuple[int, int] | None = None,
                              content: str | None = None,
                              announce: bool = False) -> discord.Message:
        """Send a match card and remember where it went.

        `target` is a channel or an interaction followup (both expose `.send`).
        `announce` sends a second, plain message on a promotion; only the poller sets it, so
        a command the user ran themselves never announces.
        """
        png, snap, has_scoreboard = await self.render_match(account_id, m, fallback_name=display_name, track=track)
        view = None
        if has_scoreboard:
            view = discord.ui.View(timeout=None)
            view.add_item(ScoreboardButton(self, m.match_id, account_id))
        msg = await target.send(
            content=content,
            file=discord.File(png, filename=f"match_{m.match_id}.png"),
            view=view,
            allowed_mentions=discord.AllowedMentions.none())
        self.db.save_match_post(msg.id, account_id, m.match_id, display_name, int(time.time()),
                                channel_id=msg.channel.id, pp_before=snap.pp_before,
                                pp_after=snap.pp_after, rank_before=snap.rank_before,
                                rank_after=snap.rank_after)
        if announce and snap.promoted:
            who = display_name or f"Account {account_id}"
            await msg.channel.send(
                f"**{who}** promoted to **{rank_name(snap.rank_after)}** "
                f"(from {rank_name(snap.rank_before)})",
                allowed_mentions=discord.AllowedMentions.none())
        return msg

    # --- patch notes ---------------------------------------------------------
    async def fetch_patch_notes(self):
        heroes = await self.dl.heroes()
        notes = await fetch_feed(self.session, [asset.name for asset in heroes.values()])
        return [note for note in notes if is_patch_note(note)]

    async def send_patch_note(self, channel, note) -> None:
        self.db.save_patch_note(note, int(time.time()))
        await channel.send(content=render_patch_content(note),
                           view=PatchNoteView(self, note=note),
                           allowed_mentions=discord.AllowedMentions.none())

    # --- automatic posting ----------------------------------------------------
    @tasks.loop(seconds=120)
    async def poll_matches(self):
        histories: dict[int, list[Match]] = {}  # one fetch per account per cycle
        links = self.db.all_links()
        log.info("poll cycle: checking %d linked account(s)", len(links))
        self.last_match_poll = time.time()
        self.last_match_poll_ok = True
        for link in links:
            try:
                if link.paused:
                    continue
                channel_id = self.db.get_settings(link.guild_id).channel_for("matches")
                if channel_id is None:
                    log.info("skip account %s: no channel set for guild %s (run /setchannel)",
                            link.account_id, link.guild_id)
                    continue
                channel = self.get_channel(channel_id)
                if channel is None:
                    log.warning("skip account %s: channel %s not found/visible to the bot (guild %s) — "
                               "check it still exists and the bot can see it", link.account_id, channel_id,
                               link.guild_id)
                    continue
                if link.account_id not in histories:
                    histories[link.account_id] = await self.dl.match_history(link.account_id)
                    await asyncio.sleep(1)  # be gentle with the API
                new = sorted((m for m in histories[link.account_id] if m.match_id > link.last_match_id),
                             key=lambda m: m.match_id)  # oldest first
                log.info("account %s: %d matches fetched, %d newer than last_match_id=%s",
                        link.account_id, len(histories[link.account_id]), len(new), link.last_match_id)
                last_posted = None
                for m in new[-MAX_AUTOPOST_PER_CYCLE:]:
                    full = await self.load_match(link.account_id, m)
                    key = (link.discord_id, link.guild_id, m.match_id)
                    if not full.items and self.waits[key] < MAX_WAIT_FOR_ITEMS:
                        self.waits[key] += 1  # item data lags the match; retry next cycle, keep order
                        log.info("match %s: no item data yet (attempt %d/%d), waiting",
                                m.match_id, self.waits[key], MAX_WAIT_FOR_ITEMS)
                        break
                    self.waits.pop(key, None)
                    log.info("posting match %s for account %s to channel %s", m.match_id, link.account_id,
                             channel_id)
                    poster_name = link.display_name
                    if not poster_name:  # links made without a name: use the Steam persona
                        steam = await self.dl.steam_profile(link.account_id)
                        poster_name = steam.name if steam else None
                    poster_name = poster_name or f"Account {link.account_id}"
                    await self.post_match_card(
                        channel, link.account_id, full, display_name=poster_name,
                        track=(link.discord_id, link.guild_id),
                        content=f"**{poster_name}** just finished a match", announce=True)
                    last_posted = m.match_id
                if last_posted:  # skipped older matches (beyond the cap) are not revisited
                    self.db.update_last_match(link.discord_id, link.guild_id, last_posted)
            except ApiUnavailable:
                self.last_match_poll_ok = False
                log.warning("poll skipped for account %s: match-history API unreachable", link.account_id)
            except Exception:
                self.last_match_poll_ok = False
                log.exception("poll failed for link %s", link)

    @poll_matches.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=10)
    async def backfill_cards(self):
        """Re-edit cards that Statlocker had no rank/PP for when they were posted."""
        rows = self.db.cards_awaiting_rank(int(time.time()) - BACKFILL_WINDOW,
                                           limit=BACKFILL_PER_CYCLE,
                                           max_attempts=BACKFILL_ATTEMPTS)
        if rows:
            await self.sl.matches([r["match_id"] for r in rows])  # prime lobby data in one batch
        for row in rows:
            channel = self.get_channel(row["channel_id"])
            if channel is None:
                self.db.set_match_post_attempts(row["message_id"], BACKFILL_ATTEMPTS)
                continue
            try:
                msg = await channel.fetch_message(row["message_id"])
                history = await self.dl.match_history(row["account_id"])
            except (discord.NotFound, discord.Forbidden):
                self.db.set_match_post_attempts(row["message_id"], BACKFILL_ATTEMPTS)
                continue
            except ApiUnavailable:
                return  # Deadlock API down: nothing to render with, retry next cycle
            base = next((m for m in history if m.match_id == row["match_id"]), None)
            if base is None:  # fell out of the history window
                self.db.set_match_post_attempts(row["message_id"], BACKFILL_ATTEMPTS)
                continue
            try:
                stored = RankSnapshot(row["pp_before"], row["pp_after"],
                                      row["rank_before"], row["rank_after"])
                png, snap, _ = await self.render_match(
                    row["account_id"], await self.load_match(row["account_id"], base),
                    fallback_name=row["display_name"], snapshot=stored)
                if (snap.pp_after, snap.rank_after) == (row["pp_after"], row["rank_after"]):
                    # Statlocker still has nothing new for this one: count the attempt
                    self.db.set_match_post_attempts(row["message_id"],
                                                    row["backfill_attempts"] + 1)
                    continue
                await msg.edit(attachments=[discord.File(
                    png, filename=f"match_{row['match_id']}.png")])
            except ApiUnavailable:
                return
            except (discord.NotFound, discord.Forbidden):
                self.db.set_match_post_attempts(row["message_id"], BACKFILL_ATTEMPTS)
                continue
            self.db.set_match_post_rank(row["message_id"], snap.pp_after, snap.rank_after)
            log.info("backfill: match %s card %s now has %s PP / rank %s", row["match_id"],
                     row["message_id"], snap.pp_after, snap.rank_after)
            await asyncio.sleep(1)

    @backfill_cards.before_loop
    async def _backfill_ready(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=600)
    async def poll_patches(self):
        self.last_patch_poll = time.time()
        self.last_patch_poll_ok = True
        channels = {s.guild_id: c for s in self.db.all_settings()
                    if (c := s.channel_for("patches"))}  # patch channel, else match channel
        if not channels:  # nobody has configured a channel yet: don't touch the feed
            return
        states = {state.guild_id: state for state in self.db.all_patch_channels()}
        try:
            notes = await self.fetch_patch_notes()
        except Exception:
            self.last_patch_poll_ok = False
            log.exception("patch poll: could not read the Steam news feed")
            return
        if not notes:
            log.warning("patch poll: the Steam feed contained no patch notes")
            return
        for guild_id, channel_id in channels.items():
            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    log.warning("patch poll: channel %s not found/visible for guild %s",
                                channel_id, guild_id)
                    continue
                state = states.get(guild_id)
                if state is None or not state.announced:
                    # First poll for this channel: post the newest note once so the channel
                    # is not empty until the next patch, then only post future notes.
                    await self.send_patch_note(channel, notes[0])
                    self.db.set_patch_baseline(guild_id, notes[0].guid, notes[0].pub_ts,
                                               announced=True)
                    log.info("patch poll: posted latest note %r to guild %s (first run)",
                             notes[0].title, guild_id)
                    await asyncio.sleep(1)
                    continue
                new, baseline = find_new_notes(notes, state.last_guid)
                if not new:
                    if baseline and baseline != state.last_guid:
                        # the old guid fell off the 10-item feed: record the newest note
                        # without posting so a long outage never floods the channel
                        self.db.set_patch_baseline(guild_id, baseline, notes[0].pub_ts,
                                                   announced=True)
                        log.info("patch poll: baseline for guild %s set to %s (nothing posted)",
                                 guild_id, baseline)
                    continue
                for note in new:
                    await self.send_patch_note(channel, note)
                    self.db.set_patch_baseline(guild_id, note.guid, note.pub_ts, announced=True)
                    log.info("patch poll: posted %r to guild %s", note.title, guild_id)
                    await asyncio.sleep(1)
            except Exception:
                self.last_patch_poll_ok = False
                log.exception("patch poll failed for guild %s", guild_id)

    @poll_patches.before_loop
    async def _wait_ready_patches(self):
        await self.wait_until_ready()


bot = StatsBot()


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    ephemeral = False
    if isinstance(original, app_commands.TransformerError):
        # Raised before the callback runs, e.g. /setchannel channel:deadlock typed by hand
        command = interaction.command.name if interaction.command else "this command"
        log.warning("could not convert option value %r for /%s", original.value, command)
        msg = ("I couldn't read that channel. Pick one from Discord's channel list, or leave the option "
               "empty to use the current channel. For a private channel, give the bot **View Channel**, "
               "**Send Messages** and **Attach Files**.")
        ephemeral = True
    elif isinstance(original, ApiUnavailable):
        msg = UNREACHABLE_MSG
    else:
        log.error("command failed", exc_info=original)
        msg = "Something went wrong while building that card. The details are in the bot's console."
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send(msg, ephemeral=ephemeral)


async def resolve_account(interaction: discord.Interaction, steamid: str | None) -> tuple[int | None, str | None]:
    """(account_id, fallback_name) from the argument, else from /save or /link. Replies on failure."""
    if steamid:
        acc = parse_steam_input(steamid)
        if acc is None:
            await interaction.followup.send(STEAM_INPUT_HELP, ephemeral=True)
        return acc, None
    link = bot.db.get_link(interaction.user.id, interaction.guild_id or 0)
    if link:
        return link.account_id, link.display_name
    await interaction.followup.send("Give me a Steam ID (`/stats steamid:...`) or run `/save` first.",
                                    ephemeral=True)
    return None, None


async def _save_account(interaction: discord.Interaction, steamid: str, name: str | None) -> str:
    """Shared by /save and /link: validates the Steam ID, records a baseline match so
    auto-posting only covers new matches, and stores an optional display name."""
    acc = parse_steam_input(steamid)
    if acc is None:
        return STEAM_INPUT_HELP
    history, steam = await asyncio.gather(bot.dl.match_history(acc), bot.dl.steam_profile(acc))
    display_name = name or (steam.name if steam else None)  # no name: use the Steam persona
    bot.db.set_link(interaction.user.id, interaction.guild_id, acc,
                    history[0].match_id if history else 0, display_name)
    msg = f"Saved account `{acc}`" + (f" as **{display_name}**" if display_name else "") + "."
    msg += " `/stats` and `/latestmatch` will use it by default now."
    msg += f" Found {len(history)} matches." if history else \
        " I found no match history yet; that's fine, new matches will show up once they're available."
    return msg


@bot.tree.command(name="save", description="Save your Steam account so /stats and /latestmatch use it immediately")
@app_commands.describe(steamid="SteamID64, 32-bit account ID, or steamcommunity.com/profiles/ link",
                       name="Optional name to show on your cards (defaults to your Steam persona)")
@app_commands.guild_only()
async def save(interaction: discord.Interaction, steamid: str, name: str | None = None):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(await _save_account(interaction, steamid, name))


@bot.tree.command(name="link", description="Link your Steam account so new matches are posted automatically")
@app_commands.describe(steamid="SteamID64, 32-bit account ID, or steamcommunity.com/profiles/ link",
                       name="Optional name to show on your cards (defaults to your Steam persona)")
@app_commands.guild_only()
async def link(interaction: discord.Interaction, steamid: str, name: str | None = None):
    await interaction.response.defer(ephemeral=True)
    msg = await _save_account(interaction, steamid, name)
    if msg is not STEAM_INPUT_HELP:
        bot.db.set_paused(interaction.user.id, interaction.guild_id, False)  # /link always resumes
        channel_id = bot.db.get_channel(interaction.guild_id)
        msg += (f" New matches will be posted in <#{channel_id}>." if channel_id
                else " An admin still needs to run `/setchannel` so I know where to post.")
    await interaction.followup.send(msg)


@bot.tree.command(name="unlink", description="Forget your saved account and stop automatic match posts for you")
@app_commands.guild_only()
async def unlink(interaction: discord.Interaction):
    removed = bot.db.delete_link(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message("Unlinked." if removed else "You weren't linked.", ephemeral=True)


@bot.tree.command(name="pause", description="Stop automatic match posts without unlinking your account")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    if not bot.db.set_paused(interaction.user.id, interaction.guild_id, True):
        return await interaction.response.send_message(
            "You're not linked here. Run `/link` first.", ephemeral=True)
    await interaction.response.send_message(
        "Automatic match posts are paused. `/stats` and `/latestmatch` still work.", ephemeral=True)


@bot.tree.command(name="resume", description="Start automatic match posts again")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    link = bot.db.get_link(interaction.user.id, interaction.guild_id)
    if link is None:
        return await interaction.response.send_message(
            "You're not linked here. Run `/link` first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    # Move the baseline to the newest match first, so resuming does not post a backlog.
    # shortcut: no queued backlog; if the fetch fails, MAX_AUTOPOST_PER_CYCLE caps the catch-up.
    try:
        history = await bot.dl.match_history(link.account_id)
    except ApiUnavailable:
        history = None
    if history:
        bot.db.update_last_match(interaction.user.id, interaction.guild_id, history[0].match_id)
    bot.db.set_paused(interaction.user.id, interaction.guild_id, False)
    msg = "Automatic match posts are back on."
    if history is None:
        msg += (" I couldn't refresh your baseline, so I'll post at most "
                f"{MAX_AUTOPOST_PER_CYCLE} recent matches.")
    await interaction.followup.send(msg, ephemeral=True)


async def _enable_patch_notes(interaction: discord.Interaction, channel, channel_changed: bool):
    """First /setchannel for a guild (or a new channel) also posts the newest patch note.

    Returns an extra sentence for the confirmation reply, or None when nothing was posted.
    """
    state = bot.db.get_patch_channel(interaction.guild_id)
    if state is not None and not channel_changed:
        return None
    try:
        notes = await bot.fetch_patch_notes()
    except Exception:
        log.exception("could not fetch patch notes while setting the channel")
        return "I couldn't reach the Steam news feed, so patch notes will start with the next one."
    if not notes:
        return None
    note = notes[0]
    try:
        await bot.send_patch_note(channel, note)
    except Exception:
        log.exception("could not post the latest patch note while setting the channel")
        return "I couldn't post the latest patch note here, so patch notes will start with the next one."
    bot.db.set_patch_baseline(interaction.guild_id, note.guid, note.pub_ts, announced=True)
    return f"I also posted the latest patch note, **{note.title}**."


@bot.tree.command(name="setchannel", description="Choose the channel for automatic match cards and patch notes")
@app_commands.describe(channel="Channel to post in (defaults to this one)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.channel
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.attach_files):
        return await interaction.response.send_message(
            f"I need Send Messages and Attach Files in {channel.mention}.", ephemeral=True)
    previous = bot.db.get_channel(interaction.guild_id)
    bot.db.set_channel(interaction.guild_id, channel.id)
    # Reading the Steam feed can take a few seconds, so acknowledge before it starts;
    # otherwise Discord times the interaction out.
    await interaction.response.defer()
    extra = await _enable_patch_notes(interaction, channel, previous != channel.id)
    msg = f"Match cards will be posted in {channel.mention}."
    if not bot.db.get_settings(interaction.guild_id).patch_channel_id:
        msg += " Patch notes use the same channel; `/settings channel feature:patches` gives them their own."
    if extra:
        msg += " " + extra
    await interaction.followup.send(msg)


settings = app_commands.Group(name="settings", description="Route match cards and patch notes",
                              default_permissions=discord.Permissions(manage_guild=True),
                              guild_only=True)


def _channel_text(channel_id: int | None, fallback: int | None = None) -> str:
    if channel_id:
        return f"<#{channel_id}>"
    return f"<#{fallback}> (fallback)" if fallback else "not set"


@settings.command(name="show", description="Show where match cards and patch notes are posted")
async def settings_show(interaction: discord.Interaction):
    s = bot.db.get_settings(interaction.guild_id)
    embed = discord.Embed(title="Settings", colour=_BRASS)
    embed.add_field(name="Match cards",
                    value=_channel_text(s.match_channel_id), inline=False)
    embed.add_field(name="Patch notes",
                    value=_channel_text(s.patch_channel_id, s.match_channel_id), inline=False)
    embed.set_footer(text="/settings channel sets one feature; leave the channel empty to fall back")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@settings.command(name="channel", description="Choose the channel for match cards or patch notes")
@app_commands.describe(feature="Which posts to route", channel="Leave empty to fall back to the match channel")
@app_commands.choices(feature=[app_commands.Choice(name="Match cards", value="matches"),
                               app_commands.Choice(name="Patch notes", value="patches")])
async def settings_channel(interaction: discord.Interaction,
                           feature: app_commands.Choice[str],
                           channel: discord.TextChannel | None = None):
    if channel is None:
        bot.db.set_feature_channel(interaction.guild_id, feature.value, None)
        effective = bot.db.get_settings(interaction.guild_id).channel_for(feature.value)
        where = f"<#{effective}>" if effective else "no channel yet (set one with `/setchannel`)"
        return await interaction.response.send_message(
            f"{feature.name} now go to {where}.", ephemeral=True)
    perms = channel.permissions_for(interaction.guild.me)
    if not perms.send_messages or (feature.value == "matches" and not perms.attach_files):
        need = "Send Messages and Attach Files" if feature.value == "matches" else "Send Messages"
        return await interaction.response.send_message(
            f"I need {need} in {channel.mention}.", ephemeral=True)
    bot.db.set_feature_channel(interaction.guild_id, feature.value, channel.id)
    await interaction.response.send_message(
        f"{feature.name} will be posted in {channel.mention}.", ephemeral=True)


bot.tree.add_command(settings)


@bot.tree.context_menu(name="Deadlock stats")
@app_commands.guild_only()
async def user_stats(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    link = bot.db.get_link(user.id, interaction.guild_id or 0)
    if link is None:
        return await interaction.followup.send(
            f"{user.display_name} hasn't linked a Steam account in this server.", ephemeral=True)
    png = await bot.build_stats_card(link.account_id, fallback_name=link.display_name)
    if png is None:
        return await interaction.followup.send("No data found for that account.", ephemeral=True)
    await interaction.followup.send(file=discord.File(png, filename="stats.png"), ephemeral=True)


@bot.tree.context_menu(name="Deadlock lookup")
async def message_lookup(interaction: discord.Interaction, message: discord.Message):
    acc = find_steam_input(message.content)
    if acc is None:
        return await interaction.response.send_message(
            "I couldn't find a Steam ID in that message.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    png = await bot.build_stats_card(acc)
    if png is None:
        return await interaction.followup.send("No data found for that account.", ephemeral=True)
    await interaction.followup.send(file=discord.File(png, filename="stats.png"), ephemeral=True)


def _ago(ts: float | None) -> str:
    return f"<t:{int(ts)}:R>" if ts else "never"


@bot.tree.command(name="help", description="What this bot can do")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Deadlock stats", colour=_GREEN,
        description="Stats and match cards for Deadlock, plus automatic post-game cards.")
    embed.add_field(
        name="Account",
        value="`/link` link Steam and turn on automatic match posts\n"
              "`/save` save Steam without auto-posting\n"
              "`/unlink` forget the account\n"
              "`/pause` / `/resume` stop or restart auto-posting", inline=False)
    embed.add_field(
        name="Stats",
        value="`/stats`, `/latestmatch`, `/matches`, `/matchstatus`, `/top`\n"
              "Right-click a user → **Deadlock stats**, or a message with a Steam ID → "
              "**Deadlock lookup**.", inline=False)
    embed.add_field(
        name="Patches",
        value="`/patchnotes` posts the latest Steam patch notes; a new patch is also posted "
              "automatically. **Hero Changes** on a note lists what changed per hero.", inline=False)
    embed.add_field(
        name="Server admin",
        value="`/setchannel` sets the match-card channel\n"
              "`/settings show` shows both channels\n"
              "`/settings channel` routes match cards or patch notes separately", inline=False)
    embed.add_field(
        name="Other",
        value="`/status` bot and API health · `/invite` add the bot to another server", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Bot and Deadlock API health")
async def status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    api_status, health = await bot.dl.health()
    services = (health or {}).get("services") or {}
    api_text = "unreachable" if api_status is None else f"HTTP {api_status}"
    if services:
        api_text += " · " + " · ".join(
            f"{name} {'✅' if ok else '❌'}" for name, ok in services.items())
    links = bot.db.all_links()
    embed = discord.Embed(title="Status", colour=_GREEN if api_status == 200 else _RED)
    embed.add_field(name="Bot", value=(
        f"uptime {_fmt_duration(int(time.time() - bot.started_at))}\n"
        f"latency {bot.latency * 1000:.0f} ms · build `{os.getenv('BUILD_SHA', 'dev')}`\n"
        f"{len(bot.guilds)} server(s)"), inline=True)
    embed.add_field(name="Matches", value=(
        f"{len(links)} linked · {bot.db.count_paused_links()} paused\n"
        f"last poll {_ago(bot.last_match_poll)} "
        f"{'✅' if bot.last_match_poll_ok else '⚠️' if bot.last_match_poll_ok is False else ''}\n"
        f"{bot.db.count_match_posts()} tracked card(s) · "
        f"{bot.db.count_cards_awaiting_rank(int(time.time()) - BACKFILL_WINDOW, BACKFILL_ATTEMPTS)} "
        f"awaiting rank"), inline=True)
    embed.add_field(name="Patches", value=(
        f"last poll {_ago(bot.last_patch_poll)} "
        f"{'✅' if bot.last_patch_poll_ok else '⚠️' if bot.last_patch_poll_ok is False else ''}\n"
        f"{len(bot.db.all_patch_notes())} stored note(s)"), inline=True)
    embed.add_field(name="Statlocker", value=bot.sl.status_text(), inline=True)
    embed.add_field(name="Deadlock API", value=api_text, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="invite", description="Get a link to add this bot to your server")
async def invite(interaction: discord.Interaction):
    url = discord.utils.oauth_url(
        bot.user.id,
        permissions=discord.Permissions(
            view_channel=True, send_messages=True, send_messages_in_threads=True,
            create_public_threads=True, embed_links=True, attach_files=True,
            read_message_history=True),
        scopes=("bot", "applications.commands"))
    embed = discord.Embed(
        title="Add Deadlock stats to your server", colour=_GREEN,
        description="Needs Send Messages and Attach Files to post cards, and Manage Server to "
                    "run `/settings`.")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Add to server", url=url))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="patchnotes", description="Post the latest Deadlock patch notes from Steam")
@app_commands.describe(count="How many recent patch notes to post (default 1, max 3)")
async def patchnotes(interaction: discord.Interaction, count: int = 1):
    await interaction.response.defer(thinking=True, ephemeral=True)
    channel = interaction.channel
    if interaction.guild is not None:
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.send_messages:
            return await interaction.followup.send(
                f"I need Send Messages in {channel.mention}.", ephemeral=True)

    now = time.monotonic()
    last = bot.patch_cooldown.get(channel.id, 0.0)
    if now - last < PATCH_COOLDOWN:
        wait = max(1, int(PATCH_COOLDOWN - (now - last)))
        return await interaction.followup.send(
            f"I just posted patch notes here. Try again in {wait}s.", ephemeral=True)

    try:
        notes = await bot.fetch_patch_notes()
    except Exception:
        log.exception("/patchnotes: could not read the Steam news feed")
        return await interaction.followup.send(
            "I couldn't reach the Steam news feed. Try again in a minute.", ephemeral=True)
    if not notes:
        return await interaction.followup.send(
            "I couldn't find any patch notes in the Steam feed right now.", ephemeral=True)

    chosen = list(reversed(notes[:max(1, min(count, 3))]))  # oldest first
    for note in chosen:
        await bot.send_patch_note(channel, note)
        await asyncio.sleep(1)
    bot.patch_cooldown[channel.id] = time.monotonic()
    await interaction.followup.send(
        f"Posted {len(chosen)} patch note{'s' if len(chosen) != 1 else ''} in {channel.mention}.",
        ephemeral=True)


@bot.tree.command(name="stats", description="Overall stats card")
@app_commands.describe(steamid="Leave empty to use your linked account")
async def stats(interaction: discord.Interaction, steamid: str | None = None):
    await interaction.response.defer()
    acc, name = await resolve_account(interaction, steamid)
    if acc is None:
        return
    png = await bot.build_stats_card(acc, fallback_name=name)
    if png is None:
        return await interaction.followup.send(
            "No data found for that account. The profile may be private, or the match history isn't available yet.")
    await interaction.followup.send(file=discord.File(png, filename="stats.png"))


@bot.tree.command(name="matches", description="Compact list of your recent matches (result, hero, mode, KDA)")
@app_commands.describe(steamid="Leave empty to use your linked account",
                       count="How many matches to show (default 10, max 10)")
async def matches(interaction: discord.Interaction, steamid: str | None = None, count: int = 10):
    await interaction.response.defer()
    acc, name = await resolve_account(interaction, steamid)
    if acc is None:
        return
    png = await bot.build_matches_card(acc, fallback_name=name, count=max(1, min(count, 10)))
    if png is None:
        return await interaction.followup.send(
            "No data found for that account. The profile may be private, or the match history isn't available yet.")
    await interaction.followup.send(file=discord.File(png, filename="matches.png"))


@bot.tree.command(name="latestmatch", description="Card for the latest match")
@app_commands.describe(steamid="Leave empty to use your linked account")
async def latestmatch(interaction: discord.Interaction, steamid: str | None = None):
    await interaction.response.defer()
    acc, name = await resolve_account(interaction, steamid)
    if acc is None:
        return
    history = await bot.dl.match_history(acc)
    if not history:
        return await interaction.followup.send("No matches found for that account.")
    track = (interaction.user.id, interaction.guild_id) if steamid is None else None  # never track on someone else's card
    await bot.post_match_card(interaction.followup, acc, await bot.load_match(acc, history[0]),
                              display_name=name, track=track)


TOP_LIMIT = 10
TOP_MAX_ACCOUNTS = 100  # shortcut: links beyond this aren't fetched; raise if a server gets huge


@bot.tree.command(name="top", description="Best ranked players linked in this server")
@app_commands.guild_only()
async def top(interaction: discord.Interaction):
    if not bot.sl.enabled:
        return await interaction.response.send_message(
            "Rank data isn't configured on this bot, so there's no ladder to show.", ephemeral=True)
    await interaction.response.defer()
    links = bot.db.links_for_guild(interaction.guild_id)
    if not links:
        return await interaction.followup.send("Nobody here is linked yet — `/link` to join.")
    checked = links[:TOP_MAX_ACCOUNTS]
    now = time.time()
    profiles: dict[int, Profile | None] = {}
    missing: list[int] = []
    for link in checked:
        hit = bot._profiles.get(link.account_id)
        if hit and now - hit[0] < 600:
            profiles[link.account_id] = hit[1]
        else:
            missing.append(link.account_id)
    if missing:
        fetched = await bot.sl.profiles(missing)  # one batch account request (up to 100)
        for aid in missing:
            p = fetched.get(aid)
            bot._profiles[aid] = (now, p)
            profiles[aid] = p
    entries = []
    for link in checked:
        member = interaction.guild.get_member(link.discord_id)
        name = member.display_name if member else None
        name = name or link.display_name or f"Account {link.account_id}"
        entries.append((discord.utils.escape_markdown(name), profiles.get(link.account_id)))
    body, shown, unranked = format_top(entries, limit=TOP_LIMIT)
    if not shown:
        return await interaction.followup.send(
            "Nobody linked here has rank data yet — it appears once Statlocker has a ranked match.",
            allowed_mentions=discord.AllowedMentions.none())
    notes = [f"Powered by Statlocker · {len(links)} linked"]
    if unranked:
        notes.append(f"{unranked} without rank data")
    if len(links) > TOP_MAX_ACCOUNTS:
        notes.append(f"first {TOP_MAX_ACCOUNTS} checked")
    embed = discord.Embed(title=f"{interaction.guild.name} · top {shown} by PP", colour=_BRASS,
                          description=body)
    embed.set_footer(text=" · ".join(notes))
    # plain display names only: no user mentions, so nothing here can ping anyone
    await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


_VERDICT_COLOURS = {"full": 0x3FB950, "partial": 0xD29922, "queued": 0xD29922,
                    "processing": 0xD29922, "missing": 0xF85149, "unreachable": 0x8B949E}


def _fmt_duration(seconds: int | None) -> str | None:
    if not seconds:
        return None
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _status_label(status: int | None, ok: str, missing: str) -> str:
    if status == 200:
        return f"✅ {ok}"
    if status == 404:
        return f"❌ {missing}"
    if status is None:
        return "⚠️ unreachable"
    return f"⚠️ HTTP {status}"


@bot.tree.command(name="matchstatus", description="Check if the Deadlock API has ingested a match (or if it is queued)")
@app_commands.describe(match_id="Match ID (leave empty to check your latest match)")
async def matchstatus(interaction: discord.Interaction, match_id: int | None = None):
    await interaction.response.defer(ephemeral=True)
    link = bot.db.get_link(interaction.user.id, interaction.guild_id or 0)
    if match_id is None:
        if link is None:
            return await interaction.followup.send(
                "Give me a match ID (`/matchstatus match_id:...`) or run `/save` first.")
        history = await bot.dl.match_history(link.account_id)
        if not history:
            return await interaction.followup.send("No matches found for your saved account.")
        match_id = history[0].match_id

    info_status, info = await bot.dl.match_info(match_id)
    salts_status, salts = await bot.dl.match_salts(match_id)
    metadata_ok = await bot.dl.metadata_available(match_id) if info_status == 200 else None
    recent = None
    if info_status != 200 and salts_status != 200:  # only the "unknown" branch needs the 1 MB feed
        ids = await bot.dl.recently_fetched_ids()
        recent = (match_id in ids) if ids is not None else None

    verdict = ingest_verdict(info_status, metadata_ok, salts_status, recent)
    embed = discord.Embed(title=f"Match {match_id} — {verdict.emoji} {verdict.title}",
                          description=verdict.detail,
                          colour=_VERDICT_COLOURS.get(verdict.kind, 0x8B949E))
    if info:
        bits = [info.mode_label]
        if duration := _fmt_duration(info.duration_s):
            bits.append(duration)
        if info.start_time:
            bits.append(f"started {info.start_time} UTC")
        embed.add_field(name="Match", value=" · ".join(bits), inline=False)
    embed.add_field(name="Scoreboard", value=_status_label(info_status, "ingested", "not found"), inline=True)
    embed.add_field(name="Item/stats metadata",
                    value={True: "✅ available", False: "🟡 pending", None: "— unknown"}[metadata_ok],
                    inline=True)
    salts_text = _status_label(salts_status, "stored", "none")
    if salts and salts.has_demo:
        salts_text += " · demo replay"
    embed.add_field(name="Salts", value=salts_text, inline=True)
    if recent is not None:
        embed.add_field(name="Recently fetched", value="✅ yes (last 10 min)" if recent else "❌ no",
                        inline=True)
    if link:
        if not link.last_match_id:
            state = "nothing posted yet"
        elif match_id > link.last_match_id:
            state = "newer than your last posted match"
        elif match_id == link.last_match_id:
            state = "your last posted match"
        else:
            state = "already covered"
        embed.add_field(name="Your account",
                        value=f"last posted `{link.last_match_id}` — {state}", inline=False)
        waited = bot.waits.get((link.discord_id, link.guild_id, match_id), 0)
        if waited:
            embed.add_field(name="Bot retry",
                            value=f"waiting for item data (attempt {waited}/{MAX_WAIT_FOR_ITEMS})",
                            inline=False)
    embed.set_footer(text=f"checked {discord.utils.utcnow():%Y-%m-%d %H:%M:%S} UTC")
    await interaction.followup.send(embed=embed)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example).")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
