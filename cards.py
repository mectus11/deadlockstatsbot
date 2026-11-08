"""Render stat cards as PNG images with Pillow.

Drawn at 2x and downsampled so edges and text are smooth. To use another font put
`bold.ttf` / `regular.ttf` in ./assets/fonts; otherwise DejaVu / Arial / Liberation
are tried, then Pillow's built-in font. Any image that is missing or can't be
decoded is replaced by a labelled placeholder tile.
"""
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from stats import Overall

BG = (11, 13, 15)
PANEL = (21, 25, 29)
PANEL_LINE = (38, 45, 52)
TEXT = (240, 244, 246)
MUTED = (138, 150, 160)
GREEN = (74, 222, 128)
RED = (239, 84, 84)
AMBER = (245, 190, 70)
SOUL = (84, 212, 232)
SLOT_COLORS = {"weapon": (240, 160, 60), "vitality": (110, 200, 110), "spirit": (176, 124, 236)}
SLOT_ORDER = {"weapon": 0, "vitality": 1, "spirit": 2}
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}
SOUL_FILE = Path(__file__).parent / "assets" / "souls.png"  # drop the real souls icon here to use it
_soul_cache: list = []

SCALE = 2
ITEM_SIZE, ITEM_GAP, ITEMS_PER_ROW, MAX_ITEMS = 64, 8, 12, 24
ITEM_LABEL_H = 15  # a name under each icon, so items are identifiable even when the art is unfamiliar
ITEM_CELL_H = ITEM_SIZE + ITEM_LABEL_H
FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_FONT_NAMES = {
    True: ["bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf"],
    False: ["regular.ttf", "DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf"],
}
_font_cache: dict[tuple[int, bool], ImageFont.ImageFont] = {}


def _font(size: int, bold: bool) -> ImageFont.ImageFont:
    key = (size, bold)
    if key not in _font_cache:
        font = None
        for name in _FONT_NAMES[bold]:
            for candidate in (str(FONT_DIR / name), name):
                try:
                    font = ImageFont.truetype(candidate, size)
                    break
                except OSError:
                    continue
            if font:
                break
        _font_cache[key] = font or ImageFont.load_default(size)
    return _font_cache[key]


def _load(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im
    except Exception:
        return None


def _fmt(v, spec="{:.0f}", suffix="") -> str:
    return "-" if v is None else spec.format(v) + suffix


class Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h, self.s = w, h, SCALE
        self.img = Image.new("RGB", (w * self.s, h * self.s), BG)
        self.d = ImageDraw.Draw(self.img)

    # --- primitives ---------------------------------------------------------
    def rect(self, x, y, w, h, fill=PANEL, r=14, outline=None, width=1):
        s = self.s
        self.d.rounded_rectangle((x * s, y * s, (x + w) * s, (y + h) * s), radius=r * s, fill=fill,
                                 outline=outline, width=width * s if outline else 0)

    def text(self, x, y, txt, size=16, fill=TEXT, bold=False, anchor="la"):
        self.d.text((x * self.s, y * self.s), str(txt), font=_font(size * self.s, bold),
                    fill=fill, anchor=anchor)

    def text_w(self, txt, size=16, bold=False) -> float:
        return self.d.textlength(str(txt), font=_font(size * self.s, bold)) / self.s

    def fit(self, txt: str, size: int, bold: bool, max_w: float) -> str:
        if self.text_w(txt, size, bold) <= max_w:
            return txt
        while len(txt) > 1 and self.text_w(txt + "...", size, bold) > max_w:
            txt = txt[:-1]
        return txt + "..."

    def soul(self, cx, cy, size, color=SOUL):
        """Souls icon: assets/souls.png if present, else a drawn teardrop."""
        s = self.s
        if not _soul_cache:
            try:
                _soul_cache.append(Image.open(SOUL_FILE).convert("RGBA"))
            except Exception:
                _soul_cache.append(None)
        if _soul_cache[0] is not None:
            im = ImageOps.contain(_soul_cache[0], (int(size * 1.4 * s),) * 2, Image.LANCZOS)  # tall icon: draw a bit larger
            self.img.paste(im.convert("RGB"), (int(cx * s - im.width / 2), int(cy * s - im.height / 2)),
                           im.getchannel("A"))
            return
        r, X, Y = size * s / 2, cx * s, cy * s
        by, br = Y + 0.18 * r, 0.78 * r
        self.d.ellipse((X - br, by - br, X + br, by + br), fill=color)
        self.d.polygon([(X - 0.72 * br, by - 0.69 * br), (X, Y - r), (X + 0.72 * br, by - 0.69 * br)], fill=color)
        hl = 0.22 * br
        self.d.ellipse((X - 0.35 * br - hl, by - 0.1 * br - hl, X - 0.35 * br + hl, by - 0.1 * br + hl),
                       fill=(205, 246, 252))

    def pill(self, x, y, txt, color, size=12) -> float:
        """Small rounded badge (e.g. a win/loss streak); returns its width."""
        w, h = self.text_w(txt, size, True) + 16, size + 10
        self.rect(x, y, w, h, fill=tuple(int(v * 0.22) for v in color), r=h / 2, outline=color, width=1)
        self.text(x + w / 2, y + h / 2, txt, size, color, True, "mm")
        return w

    def tri(self, cx, cy, size, up: bool, color):
        """A small filled up/down triangle (for a rank/PP change) — drawn, no icon file needed."""
        s, h = self.s, size * 0.9
        pts = [(cx, cy - h / 2), (cx - size / 2, cy + h / 2), (cx + size / 2, cy + h / 2)] if up else \
              [(cx, cy + h / 2), (cx - size / 2, cy - h / 2), (cx + size / 2, cy - h / 2)]
        self.d.polygon([(px * s, py * s) for px, py in pts], fill=color)

    def icon(self, data: bytes | None, x, y, w, h, r=10, border=None, label="", focus=(0.5, 0.5), bg=PANEL_LINE,
             fit="cover"):
        """Rounded image tile; labelled placeholder if no image.
        fit="cover" crops to fill (good for portraits, with `focus`); fit="contain" shows the
        whole image letterboxed (good for item icons, so none of the artwork gets cropped off)."""
        s = self.s
        tile = Image.new("RGBA", (w * s, h * s), bg + (255,))
        im = _load(data)
        if im is not None:
            im = im.convert("RGBA")
            if fit == "contain":
                shrunk = ImageOps.contain(im, tile.size, Image.LANCZOS)
                tile.alpha_composite(shrunk, ((tile.size[0] - shrunk.width) // 2, (tile.size[1] - shrunk.height) // 2))
            else:
                tile.alpha_composite(ImageOps.fit(im, tile.size, Image.LANCZOS, centering=focus))
        mask = Image.new("L", tile.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile.size[0] - 1, tile.size[1] - 1), radius=r * s, fill=255)
        self.img.paste(tile.convert("RGB"), (x * s, y * s), mask)
        if im is None and label:
            words = label.split()
            for i, line in enumerate([words[0], " ".join(words[1:])] if len(words) > 1 else [words[0]]):
                if line:
                    self.text(x + w / 2, y + h / 2 + (i - 0.5 * (len(words) > 1)) * 12,
                              self.fit(line, 10, True, w - 6), 10, MUTED, True, "mm")
        if border:
            self.rect(x, y, w, h, fill=None, r=r, outline=border, width=2)

    def avatar(self, data, x, y, size):
        """Round profile picture; grey disc with an initial-less fallback."""
        s = self.s
        im = _load(data)
        disc = Image.new("RGB", (size * s, size * s), PANEL_LINE)
        if im is not None:
            disc = ImageOps.fit(im.convert("RGB"), (size * s, size * s), Image.LANCZOS)
        mask = Image.new("L", disc.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, disc.size[0] - 1, disc.size[1] - 1), fill=255)
        self.img.paste(disc, (x * s, y * s), mask)

    def png(self) -> io.BytesIO:
        out = io.BytesIO()
        self.img.resize((self.w, self.h), Image.LANCZOS).save(out, "PNG", optimize=True)
        out.seek(0)
        return out


def _tile(c: Canvas, x, y, w, h, label, value, color=TEXT, soul=False):
    c.rect(x, y, w, h, PANEL, outline=PANEL_LINE)
    c.text(x + w / 2, y + 20, label.upper(), 12, MUTED, True, "ma")
    cy = y + h / 2 + 12
    if soul:
        total = 28 + 8 + c.text_w(value, 34, True)
        sx = x + w / 2 - total / 2
        c.soul(sx + 14, cy, 26)
        c.text(sx + 36, cy, value, 34, color, True, "lm")
    else:
        c.text(x + w / 2, cy, value, 34, color, True, "mm")


def _footer(c: Canvas, y: int, statlocker: bool):
    if statlocker:
        c.text(40, y, "Powered by Statlocker (statlocker.gg)", 12, GREEN)
    c.text(c.w - 40, y, "Match data: deadlock-api.com", 12, MUTED, False, "ra")


# =============================================================================
@dataclass
class HeroRow:
    name: str
    games: int
    winrate: float | None
    icon: bytes | None = None


@dataclass
class StatsCardData:
    name: str
    account_id: int
    overall: Overall
    heroes: list[HeroRow] = field(default_factory=list)
    avatar: bytes | None = None
    rank_name: str | None = None
    rank_badge: bytes | None = None
    pp_score: int | None = None
    statlocker: bool = False


def render_stats_card(d: StatsCardData) -> io.BytesIO:
    o = d.overall
    c = Canvas(1000, 650)

    # header
    c.rect(40, 30, 920, 150, PANEL, outline=PANEL_LINE)
    c.rect(40, 30, 6, 150, GREEN, r=3)
    c.avatar(d.avatar, 74, 56, 98)
    if d.avatar is None:
        c.text(123, 105, (d.name[:1] or "?").upper(), 44, MUTED, True, "mm")
    has_badge = d.rank_badge is not None
    c.text(196, 62, c.fit(d.name, 36, True, 960 - 40 - (120 if has_badge else 0) - 196), 36, TEXT, True)
    line2 = f"Last {o.matches} matches  |  Account {d.account_id}"
    c.text(196, 112, line2, 15, MUTED)
    if o.streak >= 2:  # a streak of 1 isn't worth calling out
        col = GREEN if o.streak_won else RED
        c.pill(196 + c.text_w(line2, 15) + 14, 103, f"{'W' if o.streak_won else 'L'}{o.streak}", col)
    if d.rank_name:
        c.text(196, 138, d.rank_name + (f"  -  PP {d.pp_score}" if d.pp_score else ""), 16, GREEN, True)
    if has_badge:
        c.icon(d.rank_badge, 960 - 40 - 100, 52, 100, 100, r=8, bg=PANEL)

    # tiles
    wr = o.winrate
    tiles = [("Matches", _fmt(o.matches), TEXT, False),
             ("Win rate", _fmt(wr, "{:.1f}", "%"), TEXT if wr is None else (GREEN if wr >= 50 else RED), False),
             ("KDA", _fmt(o.kda, "{:.2f}"), TEXT, False),
             ("Souls / min", _fmt(o.souls_per_min, "{:,.0f}"), TEXT, True)]
    for i, (label, value, color, soul) in enumerate(tiles):
        _tile(c, 40 + i * 235, 200, 215, 100, label, value, color, soul)

    rec = f"{o.wins}W  {o.losses}L"
    if o.avg_kills is not None:
        rec += f"   |   Avg K/D/A  {o.avg_kills:.1f} / {_fmt(o.avg_deaths, '{:.1f}')} / {_fmt(o.avg_assists, '{:.1f}')}"
    c.text(40, 316, rec, 15, MUTED)

    # most played heroes
    c.rect(40, 346, 920, 258, PANEL, outline=PANEL_LINE)
    c.text(64, 362, "MOST PLAYED HEROES", 12, MUTED, True)
    if not d.heroes:
        c.text(64, 440, "No hero data available.", 15, MUTED)
    for i, h in enumerate(d.heroes[:5]):
        y0 = 388 + i * 42
        cy = y0 + 17
        c.text(64, cy, i + 1, 15, MUTED, True, "lm")
        c.icon(h.icon, 90, y0, 34, 34, r=8, label=h.name, focus=(0.5, 0.3))
        c.text(142, cy, c.fit(h.name, 16, True, 250), 16, TEXT, True, "lm")
        c.text(420, cy, f"{h.games} games", 14, MUTED, False, "lm")
        wr_h = h.winrate or 0
        c.rect(540, cy - 6, 290, 12, PANEL_LINE, r=6)
        if wr_h > 0:
            c.rect(540, cy - 6, max(12, int(290 * wr_h / 100)), 12, GREEN if wr_h >= 50 else RED, r=6)
        c.text(940, cy, f"{wr_h:.0f}% WR", 14, TEXT, True, "rm")

    _footer(c, 620, d.statlocker)
    return c.png()


# =============================================================================
@dataclass
class ItemView:
    name: str
    icon: bytes | None = None
    slot: str | None = None
    tier: int | None = None


@dataclass
class MatchCardData:
    name: str
    hero: str
    match_id: int
    won: bool | None
    kills: int | None
    deaths: int | None
    assists: int | None
    duration_s: int | None
    start_time: int | None
    net_worth: int | None
    last_hits: int | None
    denies: int | None
    hero_icon: bytes | None = None
    avatar: bytes | None = None
    items: list[ItemView] = field(default_factory=list)
    unnamed_items: int = 0  # bought, but no catalogue entry to show
    own_lobby_rank: str | None = None
    enemy_lobby_rank: str | None = None
    rank_name: str | None = None        # player's own current rank (e.g. "Archon 3")
    pp_score: int | None = None
    pp_delta: int | None = None         # change since the last match tracked for this player
    statlocker: bool = False


def render_match_card(d: MatchCardData) -> io.BytesIO:
    items = sorted(d.items, key=lambda i: SLOT_ORDER.get(i.slot, 3))  # slot groups, purchase order inside
    shown = items[:MAX_ITEMS]
    rows = max(1, ceil(len(shown) / ITEMS_PER_ROW))
    items_h = 44 + rows * (ITEM_CELL_H + ITEM_GAP) + 8 if shown else 84
    has_lobby = bool(d.own_lobby_rank or d.enemy_lobby_rank)
    y_items = 340
    y_end = y_items + items_h
    c = Canvas(1000, y_end + (44 if has_lobby else 20) + 30)
    color = GREEN if d.won is True else RED if d.won is False else AMBER

    # banner: hero portrait + result
    c.rect(40, 30, 920, 130, PANEL, outline=PANEL_LINE)
    c.rect(40, 30, 6, 130, color, r=3)
    c.icon(d.hero_icon, 68, 45, 100, 100, r=14, border=color, label=d.hero, focus=(0.5, 0.3))
    c.text(194, 40, "VICTORY" if d.won is True else "DEFEAT" if d.won is False else "MATCH", 40, color, True)
    c.text(194, 90, c.fit(d.hero, 22, True, 400), 22, TEXT, True)
    info = f"Match #{d.match_id}"
    c.text(194, 122, info, 14, MUTED)
    x = 194 + c.text_w(info, 14) + 10
    if d.rank_name:
        rank_txt = d.rank_name + (f"  · {d.pp_score} PP" if d.pp_score is not None else "")
        c.text(x, 122, rank_txt, 14, MUTED, True)
        x += c.text_w(rank_txt, 14, True) + 8
        if d.pp_delta:
            up, col = d.pp_delta > 0, GREEN if d.pp_delta > 0 else RED
            c.tri(x + 5, 119, 9, up, col)
            c.text(x + 14, 122, f"{'+' if up else ''}{d.pp_delta}", 13, col, True)
    right = 960 - 28
    if d.avatar:
        c.avatar(d.avatar, 960 - 28 - 64, 48, 64)
        right -= 64 + 16
    c.text(right, 52, c.fit(d.name, 22, True, 330), 22, TEXT, True, "ra")
    if d.start_time:
        c.text(right, 88, datetime.fromtimestamp(d.start_time, timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
               14, MUTED, False, "ra")
    if d.duration_s:
        m, s = divmod(d.duration_s, 60)
        c.text(right, 108, f"Duration {m}:{s:02d}", 14, MUTED, False, "ra")

    # K / D / A and souls
    c.rect(40, 180, 920, 140, PANEL, outline=PANEL_LINE)
    for i, (label, val, col) in enumerate([("Kills", d.kills, GREEN), ("Deaths", d.deaths, RED),
                                           ("Assists", d.assists, TEXT)]):
        cx = 40 + 520 * (i * 2 + 1) / 6
        c.text(cx, 198, label.upper(), 12, MUTED, True, "ma")
        c.text(cx, 248, _fmt(val), 60, col, True, "mm")
    if None not in (d.kills, d.deaths, d.assists):
        c.text(300, 298, f"KDA {(d.kills + d.assists) / max(d.deaths, 1):.2f}", 14, MUTED, True, "mm")
    c.d.line((580 * 2, 200 * 2, 580 * 2, 300 * 2), fill=PANEL_LINE, width=2)
    spm = d.net_worth / (d.duration_s / 60) if d.net_worth and d.duration_s else None
    for (label, value, soul), (x, y) in zip(
            [("Net worth", _fmt(d.net_worth, "{:,.0f}"), True), ("Souls / min", _fmt(spm, "{:,.0f}"), True),
             ("Last hits", _fmt(d.last_hits), False), ("Denies", _fmt(d.denies), False)],
            [(612, 198), (790, 198), (612, 254), (790, 254)]):
        c.text(x, y, label.upper(), 11, MUTED, True)
        if soul:
            c.soul(x + 11, y + 30, 22)
        c.text(x + (30 if soul else 0), y + 16, value, 28, TEXT, True)

    # items bought
    c.rect(40, y_items, 920, items_h, PANEL, outline=PANEL_LINE)
    c.text(64, y_items + 16, "ITEMS BOUGHT", 12, MUTED, True)
    count = f"{len(items) + d.unnamed_items} items" + (f"  ({d.unnamed_items} not in catalogue)" if d.unnamed_items else "")
    c.text(936, y_items + 16, count, 12, MUTED, False, "ra")
    if not shown:
        msg = "Item data isn't available for this match yet." if not d.unnamed_items else \
            f"{d.unnamed_items} items bought, but their names/icons couldn't be loaded."
        c.text(64, y_items + 52, msg, 15, MUTED)
    grid_w = ITEMS_PER_ROW * ITEM_SIZE + (ITEMS_PER_ROW - 1) * ITEM_GAP
    for i, it in enumerate(shown):
        x = 40 + (920 - grid_w) // 2 + (i % ITEMS_PER_ROW) * (ITEM_SIZE + ITEM_GAP)
        y = y_items + 40 + (i // ITEMS_PER_ROW) * (ITEM_CELL_H + ITEM_GAP)
        col = SLOT_COLORS.get(it.slot, (90, 100, 110))
        c.icon(it.icon, x, y, ITEM_SIZE, ITEM_SIZE, r=10, border=col, label=it.name, fit="contain")
        if it.tier in ROMAN:
            c.rect(x + ITEM_SIZE - 22, y + ITEM_SIZE - 16, 20, 14, BG, r=5)
            c.text(x + ITEM_SIZE - 12, y + ITEM_SIZE - 9, ROMAN[it.tier], 10, col, True, "mm")
        c.text(x + ITEM_SIZE / 2, y + ITEM_SIZE + 2, c.fit(it.name, 10, False, ITEM_SIZE + ITEM_GAP - 4),
               10, MUTED, False, "ma")
    if len(items) > MAX_ITEMS:
        c.text(936, y_items + items_h - 14, f"+{len(items) - MAX_ITEMS} more", 12, MUTED, False, "rm")

    y = y_end + 14
    if has_lobby:
        c.text(40, y, f"Lobby average rank   Your team: {d.own_lobby_rank or '-'}"
                      f"   |   Enemy team: {d.enemy_lobby_rank or '-'}", 14, MUTED)
        y += 26
    _footer(c, y + 6, d.statlocker)
    return c.png()
