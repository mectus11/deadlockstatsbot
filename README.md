# Deadlock Stats Bot

Posts a stats card after every Deadlock match you play, plus on-demand cards.

| Command | What it does |
|---|---|
| `/save <steamid> [name]` | Saves your Steam account; `/stats` and `/latestmatch` use it right away. |
| `/link <steamid> [name]` | Same as `/save`, and also turns on automatic posting of new matches. |
| `/unlink` | Forgets your saved account and stops automatic posts for you. |
| `/setchannel [channel]` | (Admins) Channel for automatic cards. Defaults to the current one. |
| `/stats [steamid]` | Overall card: matches, win rate, KDA, souls/min, top heroes with portraits, rank. |
| `/matches [steamid] [count]` | Compact list of recent matches: result, hero, mode, KDA (default 10). |
| `/latestmatch [steamid]` | Match card: hero portrait, result, match type (ranked/standard/bot/Street Brawl), K/D/A, combat stats, souls, and every item bought with its icon. |

`steamid` can be a SteamID64 (17 digits), a 32-bit account ID, or a `steamcommunity.com/profiles/...` link
(custom `/id/name` links can't be resolved). Leave it empty to use your saved account. `name` is optional and
is only shown on the card when Statlocker isn't set up (it doesn't change your in-game profile).

## Setup (Windows)

1. Create a bot at <https://discord.com/developers/applications>, copy its token, invite it with the `bot` and
   `applications.commands` scopes and *Send Messages* + *Attach Files*.
2. Put the token in `.env` (`DISCORD_TOKEN=...`; see `.env.example`).
3. Double-click `run.bat`. It installs everything and starts the bot. Keep the window open.
4. In Discord: `/save <your steamid>` to get your own stats right away. For automatic posts after every
   match, an admin also runs `/setchannel`, and you use `/link` instead of `/save`.

Elsewhere: `pip install -r requirements.txt`, then `python bot.py` (Python 3.10+).

## Where the data comes from

- **Matches, items, hero/item names and pictures:** [deadlock-api.com](https://deadlock-api.com), no key needed.
  Pictures are downloaded once and cached in `./cache`.
- **Rank, PP score, lobby average rank:** the [Statlocker API](https://statlocker.gg/api). It needs a key you apply
  for on that page. Without `STATLOCKER_API_KEY` the cards simply leave those parts out. Cards that use Statlocker data
  carry the required "Powered by Statlocker" line.

The bot checks every `POLL_SECONDS` (default 120). `/link` records your latest match as a baseline, so only newer
matches are posted; at most 3 per cycle. Item data can lag a match by a few minutes, so a new match waits up to 5 cycles
for it before being posted without items.

## If something is empty or wrong

Several response shapes were matched against documentation, not a live response. Run `probe.bat` (it asks for your
Steam ID and writes `probe_output.txt`): it prints the real fields, what the parsers made of them, and whether each
image URL actually downloads. Typical results:

- Cards show `-` for kills/souls, or the result is wrong: the match-history/metadata field names differ; send the output.
- No item or hero pictures (labelled grey tiles instead): the image field names or host differ; send the output.
- Hero names show as `Hero #12`: the hero list couldn't be loaded (check the console for a warning).

The souls icon is `assets/souls.png`; replace that file to change it
(PNG, transparent background works best); it is picked up automatically after a restart.

To download every item icon in one go, double-click `download_icons.bat` (needs internet; it saves them into
`assets/items`, skipping files you already have).

Your own item icons: put image files in `assets/items` (for example `Basic Magazine.png`). They are matched to items by
name, ignoring case, spaces and words like "icon", and are used instead of the downloaded ones.

Item icons are outlined by slot (orange weapon, green vitality, purple spirit) with the tier as a small badge, and
grouped by slot in purchase order. Up to 24 items are shown; hero ability upgrades (spent with ability points,
not bought from the shop) are left out of the grid on purpose. Icons are shown whole, not cropped.

## Development

`python -m unittest test_core -v` runs the parsing/stats/database tests. `python preview_cards.py` renders sample
cards with generated stand-in icons into `./preview/`. A custom font: put `bold.ttf` and `regular.ttf` in `assets/fonts/`.

Files: `bot.py` commands + poller · `cards.py` image cards · `deadlock_api.py` / `statlocker.py` API clients ·
`models.py` parsing · `images.py` download cache · `stats.py` aggregates · `steam.py` ID parsing · `db.py` storage ·
`probe.py` live-data check.
