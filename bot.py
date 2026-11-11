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
"""
import asyncio
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
from db import DB
from deadlock_api import ApiUnavailable, DeadlockAPI
from images import ImageStore
from local_icons import LocalIcons
from models import Match, ingest_verdict, match_type, merge_match, resolve_identity
from patchnote_views import HeroChangesView, PatchNoteView
from statlocker import StatlockerAPI, rank_badge_url, rank_name
from stats import compute_overall
from steam import STEAM_INPUT_HELP, parse_steam_input
from steam_news import fetch_feed, find_new_notes, is_patch_note, render_patch_content

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

SHOP_SLOTS = {"weapon", "vitality", "spirit"}  # excludes hero ability upgrades, which share the items catalogue
MAX_AUTOPOST_PER_CYCLE = 3  # per player, so a long gap never floods the channel
MAX_WAIT_FOR_ITEMS = 5      # poll cycles to wait for a match's item data before posting without it
PATCH_COOLDOWN = 30         # seconds between manual /patchnotes posts in the same channel
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

    async def render_match(self, account_id: int, m: Match, fallback_name: str | None = None,
                           track: tuple[int, int] | None = None):
        """track=(discord_id, guild_id): compares PP to the value stored for this player's own
        account and remembers the new one, so the next match card can show the change. Only pass
        this for the player's own card, never someone else's steamid."""
        profile, lobby, steam = await asyncio.gather(
            self.sl.profile(account_id), self.sl.lobby(m.match_id, account_id),
            self.dl.steam_profile(account_id))
        identity = resolve_identity(profile, steam, fallback_name, account_id)
        pp_delta = None
        if track and profile and profile.pp_score is not None:
            baseline = self.db.get_link(*track)
            if baseline and baseline.last_pp_score is not None:
                pp_delta = profile.pp_score - baseline.last_pp_score
            self.db.set_rank_baseline(*track, profile.pp_score, profile.rank_number)
        heroes, catalogue = await asyncio.gather(self.dl.heroes(), self.dl.items())
        hero = heroes.get(m.hero_id)
        # the items catalogue also lists hero ability upgrades (spent with ability points, not bought
        # from the shop); only weapon/vitality/spirit entries are real purchases for this grid
        catalogued = [(b, catalogue.get(b.item_id)) for b in m.items]
        known = [a for _, a in catalogued if a and a.slot in SHOP_SLOTS]
        unknown = sum(1 for _, a in catalogued if a is None)
        own = {a.id: self.local.get(a.name, a.tier) for a in known}  # your files in assets/items win
        pics = await self.images.get_many([hero.image if hero else None, identity.avatar_url,
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
            rank_name=rank_name(profile.rank_number) if profile else None,
            pp_score=profile.pp_score if profile else None,
            pp_delta=pp_delta,
            statlocker=bool(profile or lobby),
        ))

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
        for link in links:
            try:
                channel_id = self.db.get_channel(link.guild_id)
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
                    poster_name = link.display_name or f"Account {link.account_id}"
                    await channel.send(
                        content=f"**{poster_name}** just finished a match",
                        file=discord.File(await self.render_match(link.account_id, full, link.display_name,
                                                                   track=(link.discord_id, link.guild_id)),
                                          filename=f"match_{m.match_id}.png"),
                        allowed_mentions=discord.AllowedMentions.none())
                    last_posted = m.match_id
                if last_posted:  # skipped older matches (beyond the cap) are not revisited
                    self.db.update_last_match(link.discord_id, link.guild_id, last_posted)
            except ApiUnavailable:
                log.warning("poll skipped for account %s: match-history API unreachable", link.account_id)
            except Exception:
                log.exception("poll failed for link %s", link)

    @poll_matches.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=600)
    async def poll_patches(self):
        states = self.db.all_patch_channels()
        if not states:  # nobody has run /setchannel yet: don't touch the feed
            return
        try:
            notes = await self.fetch_patch_notes()
        except Exception:
            log.exception("patch poll: could not read the Steam news feed")
            return
        if not notes:
            log.warning("patch poll: the Steam feed contained no patch notes")
            return
        for state in states:
            try:
                channel_id = self.db.get_channel(state.guild_id)
                if channel_id is None:
                    continue
                channel = self.get_channel(channel_id)
                if channel is None:
                    log.warning("patch poll: channel %s not found/visible for guild %s",
                                channel_id, state.guild_id)
                    continue
                new, baseline = find_new_notes(notes, state.last_guid)
                if not new:
                    if baseline and baseline != state.last_guid:
                        # first run for this guild, or the old guid fell off the 10-item feed:
                        # record the newest note without posting so nobody gets flooded
                        self.db.set_patch_baseline(state.guild_id, baseline, notes[0].pub_ts)
                        log.info("patch poll: baseline for guild %s set to %s (nothing posted)",
                                 state.guild_id, baseline)
                    continue
                for note in new:
                    await self.send_patch_note(channel, note)
                    self.db.set_patch_baseline(state.guild_id, note.guid, note.pub_ts)
                    log.info("patch poll: posted %r to guild %s", note.title, state.guild_id)
                    await asyncio.sleep(1)
            except Exception:
                log.exception("patch poll failed for guild %s", state.guild_id)

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
    history = await bot.dl.match_history(acc)
    bot.db.set_link(interaction.user.id, interaction.guild_id, acc, history[0].match_id if history else 0, name)
    msg = f"Saved account `{acc}`" + (f" as **{name}**" if name else "") + "."
    msg += " `/stats` and `/latestmatch` will use it by default now."
    msg += f" Found {len(history)} matches." if history else \
        " I found no match history yet; that's fine, new matches will show up once they're available."
    return msg


@bot.tree.command(name="save", description="Save your Steam account so /stats and /latestmatch use it immediately")
@app_commands.describe(steamid="SteamID64, 32-bit account ID, or steamcommunity.com/profiles/ link",
                       name="Optional name to show on your cards")
@app_commands.guild_only()
async def save(interaction: discord.Interaction, steamid: str, name: str | None = None):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(await _save_account(interaction, steamid, name))


@bot.tree.command(name="link", description="Link your Steam account so new matches are posted automatically")
@app_commands.describe(steamid="SteamID64, 32-bit account ID, or steamcommunity.com/profiles/ link",
                       name="Optional name to show on your cards")
@app_commands.guild_only()
async def link(interaction: discord.Interaction, steamid: str, name: str | None = None):
    await interaction.response.defer(ephemeral=True)
    msg = await _save_account(interaction, steamid, name)
    if msg is not STEAM_INPUT_HELP:
        channel_id = bot.db.get_channel(interaction.guild_id)
        msg += (f" New matches will be posted in <#{channel_id}>." if channel_id
                else " An admin still needs to run `/setchannel` so I know where to post.")
    await interaction.followup.send(msg)


@bot.tree.command(name="unlink", description="Forget your saved account and stop automatic match posts for you")
@app_commands.guild_only()
async def unlink(interaction: discord.Interaction):
    removed = bot.db.delete_link(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message("Unlinked." if removed else "You weren't linked.", ephemeral=True)


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
    bot.db.set_patch_baseline(interaction.guild_id, note.guid, note.pub_ts)
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
    msg = f"Match cards and patch notes will be posted in {channel.mention}."
    if extra:
        msg += " " + extra
    await interaction.followup.send(msg)


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
    png = await bot.render_match(acc, await bot.load_match(acc, history[0]), fallback_name=name, track=track)
    await interaction.followup.send(file=discord.File(png, filename="latest_match.png"))


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
