"""Deadlock stats Discord bot.

Commands
  /save <steamid> [name]   save your Steam account; /stats and /latestmatch then use it right away
  /link <steamid> [name]   same as /save, and also enables automatic posting of new matches
  /unlink                  forget your saved account, and stop auto-posting for you
  /setchannel [channel]    (admins) choose the channel for automatic match cards
  /stats [steamid]         overall stats card (defaults to your saved account)
  /latestmatch [steamid]   latest match card (defaults to your saved account)
"""
import asyncio
import logging
import os
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
from models import Match, merge_match
from statlocker import StatlockerAPI, rank_badge_url, rank_name
from stats import compute_overall
from steam import STEAM_INPUT_HELP, parse_steam_input

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

SHOP_SLOTS = {"weapon", "vitality", "spirit"}  # excludes hero ability upgrades, which share the items catalogue
MAX_AUTOPOST_PER_CYCLE = 3  # per player, so a long gap never floods the channel
MAX_WAIT_FOR_ITEMS = 5      # poll cycles to wait for a match's item data before posting without it
UNREACHABLE_MSG = ("I couldn't reach api.deadlock-api.com (network or DNS problem on the bot's computer). "
                   "Check the internet connection and try again in a minute.")


class StatsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = DB(os.getenv("DB_PATH", "bot.db"))
        self.poll_seconds = max(30, int(os.getenv("POLL_SECONDS", "120")))
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

    async def display_name(self, account_id: int, profile, fallback: str | None = None) -> str:
        if profile and profile.name:
            return profile.name
        if fallback:
            return fallback
        return (await self.dl.steam_name(account_id)) or f"Account {account_id}"

    async def build_stats_card(self, account_id: int, history: list[Match] | None = None,
                               fallback_name: str | None = None):
        history = history if history is not None else await self.dl.match_history(account_id)
        profile = await self.sl.profile(account_id) if self.sl.enabled else None
        if not history and not profile:
            return None
        overall = compute_overall(history)
        hero_cat = await self.dl.heroes()
        top = [(h, hero_cat.get(h.hero_id)) for h in overall.top_heroes]
        pics = await self.images.get_many([a.image if a else None for _, a in top] + (
            [profile.avatar_url, rank_badge_url(profile.rank_number)] if profile else []))
        return cards.render_stats_card(cards.StatsCardData(
            name=await self.display_name(account_id, profile, fallback_name),
            account_id=account_id,
            overall=overall,
            heroes=[cards.HeroRow(a.name if a else f"Hero #{h.hero_id}", h.games, h.winrate,
                                  pics.get(a.image) if a else None) for h, a in top],
            avatar=pics.get(profile.avatar_url) if profile else None,
            rank_name=rank_name(profile.rank_number) if profile else None,
            rank_badge=pics.get(rank_badge_url(profile.rank_number)) if profile else None,
            pp_score=profile.pp_score if profile else None,
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
        profile = await self.sl.profile(account_id) if self.sl.enabled else None
        lobby = await self.sl.lobby(m.match_id, account_id) if self.sl.enabled else None
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
        pics = await self.images.get_many([hero.image if hero else None, profile.avatar_url if profile else None,
                                           *(a.image for a in known if not own[a.id])])
        return cards.render_match_card(cards.MatchCardData(
            name=await self.display_name(account_id, profile, fallback_name),
            hero=hero.name if hero else await self.dl.hero_name(m.hero_id),
            match_id=m.match_id, won=m.won, kills=m.kills, deaths=m.deaths, assists=m.assists,
            duration_s=m.duration_s, start_time=m.start_time, net_worth=m.net_worth,
            last_hits=m.last_hits, denies=m.denies,
            hero_icon=pics.get(hero.image) if hero else None,
            avatar=pics.get(profile.avatar_url) if profile else None,
            items=[cards.ItemView(a.name, own[a.id] or pics.get(a.image), a.slot, a.tier) for a in known],
            unnamed_items=unknown,
            own_lobby_rank=rank_name(lobby.own_rank) if lobby else None,
            enemy_lobby_rank=rank_name(lobby.enemy_rank) if lobby else None,
            rank_name=rank_name(profile.rank_number) if profile else None,
            pp_score=profile.pp_score if profile else None,
            pp_delta=pp_delta,
            statlocker=bool(profile or lobby),
        ))

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


bot = StatsBot()


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, ApiUnavailable):
        msg = UNREACHABLE_MSG
    else:
        log.error("command failed", exc_info=original)
        msg = "Something went wrong while building that card. The details are in the bot's console."
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send(msg)


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


@bot.tree.command(name="setchannel", description="Choose the channel for automatic match cards")
@app_commands.describe(channel="Channel to post in (defaults to this one)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.channel
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.attach_files):
        return await interaction.response.send_message(
            f"I need Send Messages and Attach Files in {channel.mention}.", ephemeral=True)
    bot.db.set_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(f"Match cards will be posted in {channel.mention}.")


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


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example).")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
