"""Render sample cards to ./preview/theme-v2/ with generated stand-in icons (no network).

v1 mockups stay in ./preview/theme-v1/ and the original theme in ./preview/before/,
so all three can be compared side by side. `preview/` is gitignored — nothing here
can be pushed by accident.
"""
import colorsys
import io
import shutil
import time
from pathlib import Path

from PIL import Image, ImageDraw

import cards
import theme as T
from models import Match, MatchStats
from stats import compute_overall

OUT = Path("preview/theme-v2")
BEFORE = Path("preview/before")


def fake_icon(seed: int, size=128, tall=False) -> bytes:
    r, g, b = (int(255 * v) for v in colorsys.hsv_to_rgb((seed * 0.137) % 1, 0.55, 0.75))
    im = Image.new("RGB", (size, size * (2 if tall else 1)), (r, g, b))
    d = ImageDraw.Draw(im)
    d.ellipse((size * .2, size * .2, size * .8, size * .8), fill=(255 - r, 255 - g, 255 - b))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _lum(rgb) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def palette_sheet() -> bytes:
    tokens = [
        ("INK", T.INK, "card background"),
        ("PANEL", T.PANEL, "panel fill"),
        ("PANEL_2", T.PANEL_2, "raised tiles"),
        ("LINE", T.LINE, "hairline borders"),
        ("LINE_2", T.LINE_2, "strong frame"),
        ("TEXT", T.TEXT, "ivory type"),
        ("PARCHMENT", T.PARCHMENT, "secondary type"),
        ("MUTED", T.MUTED, "labels"),
        ("FAINT", T.FAINT, "footer"),
        ("GREEN", T.GREEN, "absinthe / win"),
        ("MINT", T.MINT, "souls"),
        ("BRASS", T.BRASS, "ranked / PP"),
        ("GOLD", T.GOLD, "rules / accents"),
        ("COPPER", T.COPPER, "deep accent"),
        ("EMBER", T.EMBER, "brawl"),
        ("RED", T.RED, "loss text"),
        ("OXBLOOD", T.OXBLOOD, "fills / rules only"),
        ("RED_SOFT", T.RED_SOFT, "small red text"),
        ("STEEL", T.STEEL, "bot matches"),
        ("VIOLET", T.VIOLET, "spirit items"),
    ]
    c = cards.Canvas(1000, 812)
    c.panel(40, 28, 920, 82, rail=T.GREEN)
    c.tracked_text(64, 46, "DEADLOCK CARD PALETTE", 20, T.TEXT, True, "la", 2.0, family="display")
    c.tracked_text(64, 76, "NOIR / ART DECO / ABSINTHE TOKENS · V2", 9, T.MUTED, True, "la", 1.8)

    cols, gap, sw, rowh = 4, 16, (920 - 3 * 16) / 4, 104
    for i, (name, col, role) in enumerate(tokens):
        x = 40 + (i % cols) * (sw + gap)
        y = 128 + (i // cols) * rowh
        c.panel(x, y, sw, 88)
        c.panel(x + 1, y + 1, sw - 2, 34, fill=col, outline=None, cut=4)
        c.tracked_text(x + 12, y + 44, name, 10, T.TEXT, True, "la", 1.8)
        c.text(x + 12, y + 60, "#%02X%02X%02X" % col, 10, T.MUTED, family="body")
        c.tracked_text(x + sw - 12, y + 62, role.upper(), 7.5, T.FAINT, True, "ra", 1.2)

    c.tracked_text(40, 662, "CONTRAST ON PANEL", 9, T.MUTED, True, "la", 2.4)
    c.divider(40, 680, 920, T.LINE_2, double=True, lozenge=cards.ORNAMENT_LEVEL == "ornate")
    samples = [("TEXT", T.TEXT), ("MUTED", T.MUTED), ("GREEN", T.GREEN), ("BRASS", T.BRASS),
               ("PARCHMENT", T.PARCHMENT), ("GOLD", T.GOLD), ("RED_SOFT", T.RED_SOFT),
               ("VIOLET", T.VIOLET)]
    for row in range(2):
        x = 40
        for name, col in samples[row * 4:(row + 1) * 4]:
            ratio = contrast(col, T.PANEL)
            label = f"{name}  {ratio:.1f}:1"
            w = c.tracked_w(label, 10, True, 1.6) + 30
            y = 692 + row * 42
            c.panel(x, y, w, 34)
            c.tracked_text(x + 15, y + 10, label, 10, col, True, "la", 1.6)
            x += w + 12
    c.tracked_text(40, 776, "ALL VALUES MEASURED AGAINST PANEL FILL · TARGET ≥ 4.5:1 FOR BODY TEXT",
                   8, T.FAINT, True, "la", 1.4)
    return c.png().getvalue()


def components_sheet() -> bytes:
    c = cards.Canvas(1000, 760)
    c.panel(40, 28, 920, 82, rail=T.GREEN)
    c.tracked_text(64, 46, "COMPONENT SHEET", 20, T.TEXT, True, "la", 2.0, family="display")
    c.tracked_text(64, 76, "PANELS · TAGS · GAUGES · ITEM CELLS · TYPE SCALE", 9, T.MUTED, True,
                   "la", 1.8)

    # panels
    c.tracked_text(40, 130, "PANELS", 9, T.MUTED, True, "la", 2.4)
    c.panel(40, 150, 440, 118, rail=T.GREEN)
    c.bracket(50, 160, 12, T.LINE_2)
    c.bracket(470, 258, 12, T.LINE_2)
    c.tracked_text(64, 168, "PANEL + FLUTED RAIL", 11, T.TEXT, True, "la", 1.8)
    c.tracked_text(64, 190, "8 PX CHAMFER · STAMPED INNER RULE", 8, T.MUTED, True, "la", 1.4)
    c.tracked_text(64, 214, "CONTENT LINE", 10, T.MUTED, True, "la", 1.4)
    c.tracked_text(64, 232, "CONTENT LINE", 10, T.MUTED, True, "la", 1.4)
    c.panel(500, 150, 460, 118)
    c.panel_header(524, 164, 412, "SECTION HEADER", "RIGHT META")
    c.tracked_text(524, 208, "TITLE + META + DOUBLE RULE", 11, T.TEXT, True, "la", 1.4)
    c.tracked_text(524, 228, "BODY TEXT IN BARLOW CONDENSED REGULAR", 10, T.MUTED, False, "la", 1.2)

    # tags + gauges
    c.tracked_text(40, 298, "STAMPED PLATES", 9, T.MUTED, True, "la", 2.4)
    x = 40
    for label, col in [("RANKED", T.BRASS), ("STREET BRAWL", T.EMBER), ("BOT MATCH", T.STEEL),
                       ("STANDARD", T.MUTED), ("ORACLE 4 · 4250 PP", T.BRASS)]:
        x += c.tag(x, 316, label, col, size=10) + 10

    c.tracked_text(40, 360, "GAUGES", 9, T.MUTED, True, "la", 2.4)
    for i, (label, pct, col) in enumerate([("WIN RATE", 62, T.GREEN), ("LOSS RATE", 38, T.RED),
                                           ("HERO WR", 75, T.GREEN)]):
        y = 382 + i * 30
        c.tracked_text(40, y, label, 8.5, T.MUTED, True, "la", 1.8)
        c.meter(160, y, 300, pct, col, segments=10, h=9)
        c.tracked_text(474, y, f"{pct}%", 9, col, True, "la", 1.2)

    # item cells
    c.tracked_text(40, 484, "ITEM CELLS", 9, T.MUTED, True, "la", 2.4)
    for i, (slot, col) in enumerate([("weapon", T.BRASS), ("vitality", T.GREEN), ("spirit", T.VIOLET)]):
        x = 40 + i * 96
        y = 504
        c.icon(fake_icon(i + 10), x, y, 64, 64, cut=6, border=col, label=f"Item {i}", fit="contain",
               width=1.5)
        c.panel(x + 64 - 25, y + 64 - 19, 21, 15, fill=T.INK, outline=col, cut=3, width=1)
        c.text(x + 64 - 14.5, y + 64 - 11.5, "I", 9, col, True, "mm")
        c.text(x + 32, y + 66, f"Item {i}", 9, T.MUTED, False, "ma", family="body")

    # stat tile
    c.panel(360, 504, 221, 96)
    c.tracked_text(376, 518, "SOULS / MIN", 9, T.MUTED, True, "la", 2.4)
    c.soul(385, 558, 18)
    c.text(400, 540, "1,331", 32, T.MINT, True)
    c.panel(600, 504, 360, 96)
    c.tracked_text(616, 518, "STAT GRID CELL", 9, T.MUTED, True, "la", 2.4)
    c.text(616, 538, "17,883", 24, T.BRASS, True)
    c.divider(616, 574, 328, T.LINE)

    # type scale
    c.tracked_text(40, 620, "TYPE SCALE · CINZEL / JOSEFIN SANS / BARLOW CONDENSED", 9, T.MUTED,
                   True, "la", 2.4)
    c.divider(40, 638, 920, T.LINE)
    c.tracked_text(40, 650, "VICTORY", 38, T.GREEN, True, "la", 3.0, family="display")
    c.tracked_text(300, 656, "INFERNUS", 24, T.TEXT, True, "la", 1.6, family="display")
    c.tracked_text(520, 664, "SECTION TITLE", 11, T.MUTED, True, "la", 2.8)
    c.tracked_text(760, 666, "LABEL 9", 9, T.FAINT, True, "la", 2.0)
    c.text(40, 712, "Body 14 regular — item names and long values use Barlow Condensed.",
           12, T.MUTED, family="body")
    return c.png().getvalue()


def style_sheet(level: str) -> bytes:
    """One-page demonstration of the v2 visual language at a given ornament density."""
    cards.ORNAMENT_LEVEL = level
    c = cards.Canvas(1000, 900)
    c.panel(40, 28, 920, 82, rail=T.GREEN)
    c.tracked_text(64, 46, f"STYLE SHEET · {level.upper()}", 20, T.TEXT, True, "la", 2.0,
                   family="display")
    c.tracked_text(64, 76, "NOIR POSTER FRAME · BRASSWORK · ABSINTHE SHADOW · OCCULT GEOMETRY",
                   9, T.MUTED, True, "la", 1.8)

    # banner sample: duotone art + sigil
    c.panel(40, 132, 920, 140, rail=T.GREEN)
    c.watermark(fake_icon(1, tall=True), 42, 134, 916, 136, alpha=0.36)
    c.sigil(878, 200, 54, T.BRASS, 44)
    c.icon(fake_icon(1, tall=True), 64, 148, 108, 108, cut=10, border=T.GREEN, label="Hero",
           focus=(0.5, 0.3))
    c.tracked_text(196, 146, "VICTORY", 40, T.GREEN, True, "la", 3.0, family="display")
    c.tracked_text(196, 198, "INFERNUS", 20, T.TEXT, True, "la", 1.4, family="display")
    c.tracked_text(196, 230, "MATCH #41525919", 9.5, T.MUTED, True, "la", 1.8)
    c.tag(390, 222, "RANKED", T.BRASS, size=9)
    c.tag(478, 222, "ORACLE 4 · 4250 PP", T.BRASS, size=9)

    # panels
    c.tracked_text(40, 296, "PANELS", 9, T.MUTED, True, "la", 2.4)
    c.panel(40, 316, 440, 126, rail=T.GREEN)
    c.tracked_text(64, 334, "FLUTED RAIL + STAMPED PLATE", 11, T.TEXT, True, "la", 1.8)
    c.tracked_text(64, 356, "DOUBLE RULE · STEPPED CORNERS", 8, T.MUTED, True, "la", 1.4)
    c.tracked_text(64, 382, "CONTENT LINE", 10, T.MUTED, True, "la", 1.4)
    c.tracked_text(64, 400, "CONTENT LINE", 10, T.MUTED, True, "la", 1.4)
    c.panel(500, 316, 460, 126)
    c.panel_header(524, 330, 412, "SECTION HEADER", "RIGHT META")
    c.tracked_text(524, 374, "TITLE + META + RULE", 11, T.TEXT, True, "la", 1.4)
    c.tracked_text(524, 394, "BODY TEXT IN BARLOW CONDENSED", 10, T.MUTED, False, "la", 1.2)

    # plates + gauges
    c.tracked_text(40, 474, "STAMPED PLATES", 9, T.MUTED, True, "la", 2.4)
    x = 40
    for label, col in [("RANKED", T.BRASS), ("STREET BRAWL", T.EMBER), ("BOT MATCH", T.STEEL),
                       ("STANDARD", T.MUTED)]:
        x += c.tag(x, 492, label, col, size=10) + 10

    c.tracked_text(40, 540, "GAUGES", 9, T.MUTED, True, "la", 2.4)
    for i, (label, pct, col) in enumerate([("WIN RATE", 62, T.GREEN), ("LOSS RATE", 38, T.RED)]):
        y = 562 + i * 30
        c.tracked_text(40, y, label, 8.5, T.MUTED, True, "la", 1.8)
        c.meter(160, y, 300, pct, col, segments=10, h=9)
        c.tracked_text(474, y, f"{pct}%", 9, col, True, "la", 1.2)

    # cells + stat tiles
    c.tracked_text(40, 640, "ITEM CELLS", 9, T.MUTED, True, "la", 2.4)
    for i, (slot, col) in enumerate([("weapon", T.BRASS), ("vitality", T.GREEN), ("spirit", T.VIOLET)]):
        x = 40 + i * 96
        y = 660
        c.icon(fake_icon(i + 10), x, y, 64, 64, cut=6, border=col, label=f"Item {i}", fit="contain",
               width=1.5)
        c.panel(x + 64 - 25, y + 64 - 19, 21, 15, fill=T.INK, outline=col, cut=3, width=1)
        c.text(x + 64 - 14.5, y + 64 - 11.5, "I", 9, col, True, "mm")
        c.text(x + 32, y + 66, f"Item {i}", 9, T.MUTED, False, "ma", family="body")
    c.panel(360, 660, 221, 96)
    c.tracked_text(376, 674, "SOULS / MIN", 9, T.MUTED, True, "la", 2.4)
    c.soul(385, 714, 18)
    c.text(400, 696, "1,331", 32, T.MINT, True)
    c.panel(600, 660, 360, 96)
    c.panel_header(616, 674, 328, "STAT CELL", "")
    c.text(616, 700, "17,883", 26, T.BRASS, True)

    # type
    c.tracked_text(40, 786, "TYPE", 9, T.MUTED, True, "la", 2.4)
    c.divider(40, 804, 920, T.LINE_2, double=True, lozenge=level == "ornate")
    c.tracked_text(40, 816, "VICTORY", 34, T.GREEN, True, "la", 3.0, family="display")
    c.tracked_text(300, 822, "INFERNUS", 22, T.TEXT, True, "la", 1.4, family="display")
    c.tracked_text(520, 828, "SECTION TITLE", 11, T.MUTED, True, "la", 2.8)
    c.tracked_text(760, 830, "LABEL 9", 9, T.FAINT, True, "la", 2.0)
    c.text(40, 862, "Barlow Condensed — numbers, item names and long values.",
           12, T.MUTED, family="body")
    return c.png().getvalue()


# ---------------------------------------------------------------------------
OUT.mkdir(parents=True, exist_ok=True)
if not BEFORE.exists():
    BEFORE.mkdir(parents=True)
    for p in Path("preview").glob("*.png"):
        shutil.copy2(p, BEFORE / p.name)

names = ["Infernus", "Seven", "Vindicta", "Lady Geist", "Abrams", "Haze"]
history = [Match(100 + i, hero_id=[1, 1, 2, 3, 1, 4, 5, 2, 6, 1][i % 10], duration_s=1800 + i * 37,
                 kills=6 + i % 9, deaths=3 + i % 6, assists=5 + i % 11, net_worth=30000 + i * 900,
                 won=(i % 3 != 0)) for i in range(48)]
history[0].won = history[1].won = history[2].won = history[3].won = True  # demo a current win streak
o = compute_overall(history)
rows = [cards.HeroRow(names[h.hero_id - 1], h.games, h.winrate, fake_icon(h.hero_id, tall=True))
        for h in o.top_heroes]

slots = ["weapon", "vitality", "spirit"]
items = [cards.ItemView(f"Item {i}", fake_icon(i + 10), slots[i % 3] if i < 14 else None, 1 + i % 4)
         for i in range(15)]
items[3].icon = None  # shows the labelled placeholder
items[3].name = "Rapid Rounds"


def save(name, buf):
    _write(OUT / name, buf.getvalue())


def _write(path: Path, data: bytes):
    """OneDrive occasionally refuses a write mid-sync (EINVAL); retry briefly."""
    for attempt in range(6):
        try:
            path.write_bytes(data)
            return
        except OSError:
            if attempt == 5:
                raise
            time.sleep(0.3)


cards.ORNAMENT_LEVEL = "balanced"
cards.TEXTURE = "print"

save("stats.png", cards.render_stats_card(cards.StatsCardData(
    name="ExamplePlayer", account_id=123456789, overall=o, heroes=rows, avatar=fake_icon(3),
    rank_name="Oracle 4", rank_badge=fake_icon(5), pp_score=4250, statlocker=True)))
save("stats_no_statlocker.png", cards.render_stats_card(cards.StatsCardData(
    name="Account 123456789", account_id=123456789, overall=o,
    heroes=[cards.HeroRow(h.name, h.games, h.winrate) for h in rows])))
common = dict(name="ExamplePlayer", hero="Infernus", match_id=41525919, kills=11, deaths=4, assists=17,
              duration_s=2174, start_time=1757346341, net_worth=48210, last_hits=212, denies=14)
sample_stats = MatchStats(
    hero_damage=17883, damage_taken=2452, healing=323, damage_mitigated=968,
    shots_hit=1913, shots_missed=1321, hero_bullets_hit=1759, hero_bullets_hit_crit=226,
    creep_damage=8832, neutral_damage=937, boss_damage=3977, creep_kills=31, possible_creeps=38,
    neutral_kills=2, bullet_kills=9, melee_kills=1, ability_kills=4, headshot_kills=1, level=24)
# scoreboard/party samples
def _srow(hero_id, name, hero_name, k, d, a, nw, dmg, *, self_=False, mvp=False, gr=None, badge=None,
          kp=None, obj=None, acc=None, taken=None, heal=None, lvl=None, dpm=None, spm=None, party=False, avatar=None):
    return cards.ScoreRow(hero=hero_name, name=name, kills=k, deaths=d, assists=a, net_worth=nw,
                          damage=dmg, hero_icon=fake_icon(hero_id), rank_badge=badge, avatar=avatar,
                          is_self=self_, is_party=party, mvp=mvp, global_rank=gr, kill_part=kp, obj_damage=obj,
                          accuracy=acc, damage_taken=taken, healing=heal, level=lvl,
                          dmg_per_min=dpm, souls_per_min=spm)


own_team = cards.ScoreTeam("Amber Hand", True, [
    _srow(1, "ExamplePlayer", "Infernus", 11, 4, 17, 48210, 17883, self_=True, mvp=True, badge=fake_icon(5), kp=0.64, obj=3977, acc=0.59, taken=2452, heal=323, lvl=24, dpm=494, spm=1331, avatar=fake_icon(3)),
    _srow(5, "Mectus", "Abrams", 7, 5, 19, 41230, 12450, badge=fake_icon(5), kp=0.59, obj=2100, acc=0.52, taken=3100, heal=820, lvl=22, dpm=344, spm=1138, party=True, avatar=fake_icon(4)),
    _srow(6, "Nyx", "Haze", 14, 2, 9, 52880, 20110, gr=412, kp=0.52, obj=5200, acc=0.61, taken=1980, heal=150, lvl=25, dpm=555, spm=1460, party=True, avatar=fake_icon(8)),
    _srow(4, "Corvid", "Lady Geist", 4, 7, 22, 37110, 9800, kp=0.59, obj=1400, acc=0.48, taken=4200, heal=2100, lvl=21, dpm=270, spm=1024, avatar=fake_icon(9)),
    _srow(2, "Vesper", "Seven", 9, 6, 13, 44900, 14320, kp=0.50, obj=3300, acc=0.55, taken=3600, heal=400, lvl=23, dpm=395, spm=1240, avatar=fake_icon(7)),
    _srow(3, "Ash", "Vindicta", 3, 9, 6, 31850, 7200, kp=0.20, obj=900, acc=0.44, taken=5100, heal=0, lvl=19, dpm=199, spm=879, avatar=fake_icon(6)),
])
enemy_team = cards.ScoreTeam("Sapphire Flame", False, [
    _srow(2, "Rook", "Seven", 8, 7, 10, 40100, 13200, badge=fake_icon(4), kp=0.55, obj=2800, acc=0.51, taken=3400, heal=260, lvl=22, dpm=364, spm=1107, avatar=fake_icon(5)),
    _srow(3, "Juno", "Vindicta", 6, 8, 11, 35500, 9100, kp=0.52, obj=1100, acc=0.46, taken=3900, heal=120, lvl=20, dpm=251, spm=980, avatar=fake_icon(2)),
    _srow(4, "Kite", "Lady Geist", 5, 10, 14, 33000, 10500, gr=1880, kp=0.58, obj=1600, acc=0.49, taken=4500, heal=1900, lvl=21, dpm=290, spm=911, avatar=fake_icon(1)),
    _srow(5, "Pike", "Abrams", 4, 9, 8, 28900, 8400, kp=0.36, obj=700, acc=0.43, taken=4800, heal=700, lvl=19, dpm=232, spm=798, avatar=fake_icon(8)),
    _srow(6, "Wren", "Haze", 7, 8, 6, 36200, 11800, kp=0.39, obj=2400, acc=0.57, taken=3300, heal=90, lvl=21, dpm=326, spm=999, avatar=fake_icon(4)),
    _srow(1, "Sable", "Infernus", 3, 11, 7, 27400, 7600, kp=0.30, obj=500, acc=0.41, taken=5200, heal=200, lvl=18, dpm=210, spm=757, avatar=fake_icon(7)),
])
sample_scoreboard = cards.Scoreboard(own=own_team, enemy=enemy_team)

sample_party = [
    cards.PartyMemberView("Abrams", "Mectus", 7, 5, 19, 41230, hero_icon=fake_icon(5), avatar=fake_icon(4)),
    cards.PartyMemberView("Haze", "Nyx", 14, 2, 9, 52880, hero_icon=fake_icon(6), avatar=fake_icon(8)),
]
sample_party_full = sample_party + [
    cards.PartyMemberView("Lady Geist", "Corvid", 4, 7, 22, 37110, hero_icon=fake_icon(4), avatar=fake_icon(9)),
]

match_data = cards.MatchCardData(
    won=True, hero_icon=fake_icon(1, tall=True), avatar=fake_icon(3), items=items,
    own_lobby_rank="Oracle 5", enemy_lobby_rank="Oracle 3",
    own_lobby_rank_badge=fake_icon(5), enemy_lobby_rank_badge=fake_icon(4),
    rank_name="Oracle 4", rank_badge=fake_icon(5), pp_score=4250, pp_delta=86, statlocker=True,
    mode="Ranked", mode_kind="ranked", stats=sample_stats, party=sample_party, **common)
save("match.png", cards.render_match_card(match_data))
save("match_no_party.png", cards.render_match_card(cards.MatchCardData(
    won=True, hero_icon=fake_icon(1, tall=True), avatar=fake_icon(3), items=items[:10],
    own_lobby_rank="Oracle 5", enemy_lobby_rank="Oracle 3",
    own_lobby_rank_badge=fake_icon(5), enemy_lobby_rank_badge=fake_icon(4),
    rank_name="Oracle 4", rank_badge=fake_icon(5), pp_score=4250, pp_delta=86, statlocker=True,
    mode="Ranked", mode_kind="ranked", stats=sample_stats, **common)))
save("match_party_full.png", cards.render_match_card(cards.MatchCardData(
    won=False, hero_icon=fake_icon(1, tall=True), avatar=fake_icon(3), items=items[:8],
    mode="Standard", mode_kind="standard", stats=sample_stats, party=sample_party_full, **common)))
save("match_brawl.png", cards.render_match_card(cards.MatchCardData(
    won=True, hero_icon=fake_icon(6), avatar=fake_icon(3), items=items[:8], mode="Street Brawl",
    mode_kind="brawl", brawl_score=(3, 1), brawl_round_avg_s=154, stats=sample_stats, **common)))
save("match_bot.png", cards.render_match_card(cards.MatchCardData(
    won=False, hero_icon=fake_icon(4), items=items[:6], mode="Bot Match · Easy", mode_kind="bot",
    stats=sample_stats, **common)))
save("match_loss_few_items.png", cards.render_match_card(cards.MatchCardData(
    won=False, hero_icon=fake_icon(2), items=items[:5], unnamed_items=2, rank_name="Archon 3",
    rank_badge=fake_icon(7), pp_score=3690, pp_delta=-54, statlocker=True, mode="Standard",
    mode_kind="standard", stats=sample_stats, **common)))
save("match_sparse.png", cards.render_match_card(cards.MatchCardData(
    name="Account 123456789", hero="Hero #99", match_id=1, won=None, kills=None, deaths=None,
    assists=None, duration_s=None, start_time=None, net_worth=None, last_hits=None, denies=None)))
save("match_many_items.png", cards.render_match_card(cards.MatchCardData(
    won=True, hero_icon=fake_icon(4), items=items * 2, mode="Ranked", mode_kind="ranked",
    rank_name="Oracle 4", rank_badge=fake_icon(5), pp_score=4250, pp_delta=86, statlocker=True,
    stats=sample_stats, **common)))

# separate scoreboard card previews (button-triggered in bot)
save("scoreboard.png", cards.render_scoreboard_card(cards.ScoreboardCardData(
    match_id=41525919, hero="Infernus", name="ExamplePlayer", won=True, mode="Ranked",
    duration_s=2174, scoreboard=sample_scoreboard)))
save("scoreboard_loss.png", cards.render_scoreboard_card(cards.ScoreboardCardData(
    match_id=41525918, hero="Infernus", name="ExamplePlayer", won=False, mode="Standard",
    duration_s=1800, scoreboard=cards.Scoreboard(
        own=cards.ScoreTeam("Amber Hand", False, own_team.rows),
        enemy=cards.ScoreTeam("Sapphire Flame", True, enemy_team.rows)))))
save("scoreboard_unavailable.png", cards.render_scoreboard_card(cards.ScoreboardCardData(
    match_id=41525917, hero="Infernus", name="ExamplePlayer", won=None, scoreboard=None)))
save("matches.png", cards.render_matches_card(cards.MatchesCardData(
    name="ExamplePlayer", account_id=123456789, avatar=fake_icon(3), statlocker=True,
    matches=[cards.MatchRow("Infernus", True, 11, 4, 17, 41525919, 1757346341, 2174,
                            fake_icon(1), "Ranked", "ranked"),
             cards.MatchRow("Seven", False, 6, 9, 8, 41525918, 1757340000, 1800,
                            fake_icon(2), "Brawl", "brawl"),
             cards.MatchRow("Haze", True, 14, 0, 15, 41525917, 1757330000, 858,
                            fake_icon(6), "Bot", "bot"),
             cards.MatchRow("Abrams", True, 9, 3, 12, 41525916, 1757320000, 2100,
                            fake_icon(5), "Standard", "standard"),
             cards.MatchRow("Vindicta", False, 4, 7, 6, 41525915, 1757310000, 1920,
                            fake_icon(3), "Ranked", "ranked"),
             cards.MatchRow("Unknown", None, None, None, None, None, None, None)])))

_write(OUT / "palette.png", palette_sheet())
_write(OUT / "components.png", components_sheet())

# density comparison (Cinzel, print texture)
for level in ("minimal", "balanced", "ornate"):
    cards.ORNAMENT_LEVEL = level
    _write(OUT / f"style-{level}.png", style_sheet(level))
    _write(OUT / f"match-{level}.png", cards.render_match_card(match_data).getvalue())

# display-face comparison (balanced density, print texture)
cards.ORNAMENT_LEVEL = "balanced"
# one extra preview to showcase rank transition + badge on the card
save("match_rankup.png", cards.render_match_card(cards.MatchCardData(
    won=True, hero_icon=fake_icon(1, tall=True), avatar=fake_icon(3), items=items[:10],
    own_lobby_rank="Oracle 5", enemy_lobby_rank="Oracle 3",
    own_lobby_rank_badge=fake_icon(5), enemy_lobby_rank_badge=fake_icon(4),
    rank_name="Phantom 1", rank_prev="Oracle 4", rank_up=True, rank_badge=fake_icon(5),
    pp_score=4312, pp_delta=38, statlocker=True, mode="Ranked", mode_kind="ranked",
    stats=sample_stats, **common)))
for face in ("cinzel", "bodoni", "marcellus"):
    cards.set_display_face(face)
    _write(OUT / f"style-{face}.png", style_sheet("balanced"))
    _write(OUT / f"match-{face}.png", cards.render_match_card(match_data).getvalue())

# texture comparison (Cinzel, balanced density)
cards.set_display_face("cinzel")
cards.TEXTURE = "clean"
_write(OUT / "style-clean.png", style_sheet("balanced"))
_write(OUT / "match-clean.png", cards.render_match_card(match_data).getvalue())

cards.TEXTURE = "print"
cards.ORNAMENT_LEVEL = "balanced"
print("\n".join(str(p) for p in sorted(OUT.iterdir())))
