"""Render sample cards to ./preview/ with generated stand-in icons (no network)."""
import colorsys
import io
from pathlib import Path

from PIL import Image, ImageDraw

import cards
from models import Match
from stats import compute_overall


def fake_icon(seed: int, size=128, tall=False) -> bytes:
    r, g, b = (int(255 * v) for v in colorsys.hsv_to_rgb((seed * 0.137) % 1, 0.55, 0.75))
    im = Image.new("RGB", (size, size * (2 if tall else 1)), (r, g, b))
    d = ImageDraw.Draw(im)
    d.ellipse((size * .2, size * .2, size * .8, size * .8), fill=(255 - r, 255 - g, 255 - b))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


out = Path("preview")
out.mkdir(exist_ok=True)
names = ["Infernus", "Seven", "Vindicta", "Lady Geist", "Abrams", "Haze"]
history = [Match(100 + i, hero_id=[1, 1, 2, 3, 1, 4, 5, 2, 6, 1][i % 10], duration_s=1800 + i * 37,
                 kills=6 + i % 9, deaths=3 + i % 6, assists=5 + i % 11, net_worth=30000 + i * 900,
                 won=(i % 3 != 0)) for i in range(48)]
o = compute_overall(history)
rows = [cards.HeroRow(names[h.hero_id - 1], h.games, h.winrate, fake_icon(h.hero_id, tall=True)) for h in o.top_heroes]

slots = ["weapon", "vitality", "spirit"]
items = [cards.ItemView(f"Item {i}", fake_icon(i + 10), slots[i % 3] if i < 14 else None, 1 + i % 4) for i in range(15)]
items[3].icon = None  # shows the labelled placeholder
items[3].name = "Rapid Rounds"


def save(name, buf):
    (out / name).write_bytes(buf.getvalue())


save("stats.png", cards.render_stats_card(cards.StatsCardData(
    name="ExamplePlayer", account_id=123456789, overall=o, heroes=rows, avatar=fake_icon(3), rank_name="Oracle 4",
    rank_badge=fake_icon(5), pp_score=4250, statlocker=True)))
save("stats_no_statlocker.png", cards.render_stats_card(cards.StatsCardData(
    name="Account 123456789", account_id=123456789, overall=o,
    heroes=[cards.HeroRow(h.name, h.games, h.winrate) for h in rows])))
common = dict(name="ExamplePlayer", hero="Infernus", match_id=41525919, kills=11, deaths=4, assists=17,
              duration_s=2174, start_time=1757346341, net_worth=48210, last_hits=212, denies=14)
save("match.png", cards.render_match_card(cards.MatchCardData(
    won=True, hero_icon=fake_icon(1, tall=True), avatar=fake_icon(3), items=items, own_lobby_rank="Oracle 5",
    enemy_lobby_rank="Oracle 3", statlocker=True, **common)))
save("match_loss_few_items.png", cards.render_match_card(cards.MatchCardData(
    won=False, hero_icon=fake_icon(2), items=items[:5], unnamed_items=2, **common)))
save("match_sparse.png", cards.render_match_card(cards.MatchCardData(
    name="Account 123456789", hero="Hero #99", match_id=1, won=None, kills=None, deaths=None, assists=None,
    duration_s=None, start_time=None, net_worth=None, last_hits=None, denies=None)))
save("match_many_items.png", cards.render_match_card(cards.MatchCardData(
    won=True, hero_icon=fake_icon(4), items=items * 2, **common)))
print(sorted(p.name for p in out.iterdir()))
