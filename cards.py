"""Render stat cards as PNG images with Pillow.

Theme v2: 1940s New York noir, Art Deco brasswork, absinthe shadow and occult
geometry. The layout is unchanged from v1; the visual language is new: a poster
frame, stepped corner rules, fluted rails, stamped plates, gauges, duotone hero
art and a Maelstrom sigil. Drawn at 2x and downsampled so edges and text are smooth.

Display type is Cinzel, labels are Josefin Sans, numbers and body are Barlow
Condensed (assets/fonts, OFL). If a face is missing the renderer falls back to
the system fonts, then Pillow's built-in font. Any image that is missing or can't
be decoded is replaced by a labelled placeholder tile.
"""
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil, cos, radians, sin
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from models import MatchStats
from stats import Overall
from theme import (BRASS, EMBER, EMERALD, FAINT, GOLD, GREEN, INK, INK_BOTTOM,
                   INK_TOP, LINE, LINE_2, LINE_3, MINT, MODE_COLORS, MUTED, PANEL,
                   PANEL_2, PANEL_3, RED, SLOT_COLORS, SLOT_ORDER, STEEL, TEXT, VIOLET)

log = logging.getLogger("cards")

# backwards-compatible aliases for anything outside this module that imported them
BG = INK
PANEL_LINE = LINE
AMBER = EMBER
SOUL = MINT

# --- visual density ---------------------------------------------------------
# "minimal"  single rules, no sigil
# "balanced" double rules, corner steps, light sigil, fine grain
# "ornate"   full Art Deco: double rules with lozenges, fluted caps, strong sigil
ORNAMENT_LEVEL = "balanced"
TEXTURE = "print"  # "print" adds grain + vignette; "clean" leaves flat colour
DISPLAY_FACE = "cinzel"  # "cinzel" | "bodoni" | "marcellus"

ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}
SOUL_FILE = Path(__file__).parent / "assets" / "souls.png"  # drop the real souls icon here to use it
_soul_cache: list = []

SCALE = 2
ITEM_SIZE, ITEM_GAP, ITEMS_PER_ROW, MAX_ITEMS = 64, 8, 12, 24
ITEM_LABEL_H = 15  # a name under each icon, so items are identifiable even when the art is unfamiliar
ITEM_CELL_H = ITEM_SIZE + ITEM_LABEL_H
STATS_PANEL_H = 196
PARTY_ROW_H = 44
SCORE_ROW_H = 50
FONT_DIR = Path(__file__).parent / "assets" / "fonts"

# (file, variable-font weight or None)
_FONT_FILES = {
    "display": {
        True: [("Cinzel-Variable.ttf", 700), ("DejaVuSerif-Bold.ttf", None),
               ("LiberationSerif-Bold.ttf", None), ("timesbd.ttf", None)],
        False: [("Cinzel-Variable.ttf", 500), ("DejaVuSerif.ttf", None),
                ("LiberationSerif-Regular.ttf", None), ("times.ttf", None)],
    },
    "label": {
        True: [("JosefinSans-Variable.ttf", 600), ("DejaVuSans-Bold.ttf", None),
               ("arialbd.ttf", None), ("LiberationSans-Bold.ttf", None)],
        False: [("JosefinSans-Variable.ttf", 400), ("DejaVuSans.ttf", None),
                ("arial.ttf", None), ("LiberationSans-Regular.ttf", None)],
    },
    # long small text (item names) reads better in a narrow neutral face
    "body": {
        True: [("BarlowCondensed-SemiBold.ttf", None), ("bold.ttf", None),
               ("DejaVuSans-Bold.ttf", None), ("arialbd.ttf", None)],
        False: [("BarlowCondensed-Regular.ttf", None), ("regular.ttf", None),
                ("DejaVuSans.ttf", None), ("arial.ttf", None)],
    },
    # scoreboard numerals
    "number": {
        True: [("BarlowCondensed-Bold.ttf", None), ("BarlowCondensed-SemiBold.ttf", None),
               ("bold.ttf", None), ("DejaVuSans-Bold.ttf", None)],
        False: [("BarlowCondensed-Medium.ttf", None), ("BarlowCondensed-Regular.ttf", None),
                ("regular.ttf", None), ("DejaVuSans.ttf", None)],
    },
}
_font_cache: dict[tuple, ImageFont.ImageFont] = {}


def set_display_face(name: str):
    """Switch the display face for mockup comparisons; clears the font cache."""
    global DISPLAY_FACE
    DISPLAY_FACE = name
    _font_cache.clear()


def _font_spec(family: str, bold: bool):
    spec = _FONT_FILES.get(family, _FONT_FILES["display"])[bold]
    if family == "display":
        if DISPLAY_FACE == "bodoni":
            spec = [("BodoniModa-Variable.ttf", 700 if bold else 500)] + spec
        elif DISPLAY_FACE == "marcellus":
            spec = [("Marcellus-Regular.ttf", None)] + spec
    return spec


def _font(size: int, bold: bool, family: str = "display") -> ImageFont.ImageFont:
    key = (size, bold, family, DISPLAY_FACE if family == "display" else "")
    if key not in _font_cache:
        font = None
        for name, weight in _font_spec(family, bold):
            for candidate in (str(FONT_DIR / name), name):
                try:
                    font = ImageFont.truetype(candidate, size)
                    break
                except OSError:
                    continue
            if font:
                if weight is not None:
                    try:
                        if "Bodoni" in name:
                            font.set_variation_by_axes([weight, min(96.0, max(11.0, size / 2))])
                        else:
                            font.set_variation_by_axes([weight])
                    except Exception:
                        pass
                break
        _font_cache[key] = font or ImageFont.load_default(size)
    return _font_cache[key]


def _load(data: bytes | None, label: str = "") -> Image.Image | None:
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im
    except Exception as e:
        # Surfaces in the bot's logs so a blank/placeholder icon can be told apart from a
        # failed download (images.py logs that separately) vs. undecodable image bytes here
        # (e.g. a Pillow build without WEBP support, or a genuinely corrupt/truncated file).
        log.warning("could not decode image%s (%d bytes): %r", f" for {label}" if label else "",
                   len(data), e)
        return None


def _fmt(v, spec="{:.0f}", suffix="") -> str:
    return "-" if v is None else spec.format(v) + suffix


def _mix(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _chamfer(x, y, w, h, cut):
    cut = min(cut, w / 2, h / 2)
    return [(x + cut, y), (x + w - cut, y), (x + w, y + cut), (x + w, y + h - cut),
            (x + w - cut, y + h), (x + cut, y + h), (x, y + h - cut), (x, y + cut)]


class Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h, self.s = w, h, SCALE
        self._finished = False
        self._background()
        self.frame()

    def _background(self):
        """A barely-there vertical gradient, so large dark areas don't look flat."""
        s = self.s
        hpx = self.h * s
        grad = Image.new("RGB", (1, hpx))
        grad.putdata([tuple(int(round(INK_TOP[i] + (INK_BOTTOM[i] - INK_TOP[i]) * (y / max(1, hpx - 1))))
                            for i in range(3)) for y in range(hpx)])
        self.img = grad.resize((self.w * s, hpx), Image.BILINEAR)
        self.d = ImageDraw.Draw(self.img)

    # --- low level ----------------------------------------------------------
    def _pts(self, pts):
        s = self.s
        return [(x * s, y * s) for x, y in pts]

    def rect(self, x, y, w, h, fill=PANEL, r=0, outline=None, width=1):
        """Plain rectangle (kept for internal fills); rounded corners are gone by design."""
        s = self.s
        self.d.rectangle((x * s, y * s, (x + w) * s, (y + h) * s), fill=fill)
        if outline:
            self.d.rectangle((x * s, y * s, (x + w) * s, (y + h) * s),
                             outline=outline, width=max(1, int(width * s)))

    def panel(self, x, y, w, h, fill=PANEL, outline=LINE, cut=8, rail=None, rail_w=4, width=1):
        """Chamfered poster panel: hairline frame, stamped inner rule, fluted accent rail."""
        s = self.s
        pts = _chamfer(x, y, w, h, cut)
        self.d.polygon(self._pts(pts), fill=fill)
        if rail:
            rx, ry, rh = x + 1, y + cut, h - 2 * cut
            self.d.polygon(self._pts([(rx, ry), (rx + rail_w, ry), (rx + rail_w, ry + rh),
                                      (rx, ry + rh)]), fill=rail)
            if ORNAMENT_LEVEL != "minimal" and rail_w >= 3:
                for i in (1, 2):  # flutes cut into the rail
                    fx = rx + rail_w * i / 3
                    self.d.line((fx * s, ry * s, fx * s, (ry + rh) * s),
                                fill=_mix(rail, INK, 0.5), width=1)
            if ORNAMENT_LEVEL == "ornate":
                self.lozenge(rx + rail_w / 2, ry - 4, 3, GOLD, fill=INK)
                self.lozenge(rx + rail_w / 2, ry + rh + 4, 3, GOLD, fill=INK)
            if h > 48:  # gold inner rule beside the rail
                self.d.line(((x + rail_w + 5) * s, (y + cut + 5) * s,
                             (x + rail_w + 5) * s, (y + h - cut - 5) * s),
                            fill=_mix(fill, LINE_3, 0.5), width=1)
        if ORNAMENT_LEVEL == "ornate" and w >= 120 and h >= 40:
            corners = [(x + 3, y + 3, 1, 1), (x + w - 3, y + 3, -1, 1),
                       (x + 3, y + h - 3, 1, -1), (x + w - 3, y + h - 3, -1, -1)]
            if rail:
                corners = corners[1::2]  # right-hand corners only, clear of the rail
            elif h < 110:
                corners = [corners[1], corners[2]]  # top-right + bottom-left
            for cx, cy, sx, sy in corners:
                self._steps(cx, cy, sx, sy, _mix(fill, GOLD, 0.34))
        elif outline and ORNAMENT_LEVEL != "minimal" and w >= 120 and h >= 40:
            ipts = _chamfer(x + 3, y + 3, w - 6, h - 6, max(2, cut - 2))
            self.d.line(self._pts(ipts) + [self._pts(ipts)[0]],
                        fill=_mix(fill, LINE_2, 0.6), width=1)
        if outline:
            line = self._pts(pts) + [self._pts(pts)[0]]
            self.d.line(line, fill=outline, width=max(1, int(round(width * s))), joint="curve")

    def _steps(self, x, y, sx, sy, color):
        """Nested Art Deco corner steps (three short Ls)."""
        s = self.s
        for i, off in enumerate((0, 3, 6)):
            ln = 16 - i * 4
            self.d.line(((x + sx * off) * s, (y + sy * off) * s,
                         (x + sx * (off + ln)) * s, (y + sy * off) * s), fill=color, width=1)
            self.d.line(((x + sx * off) * s, (y + sy * off) * s,
                         (x + sx * off) * s, (y + sy * (off + ln)) * s), fill=color, width=1)


    def frame(self):
        """Poster frame: double rule around the card, stepped corner marks, centre lozenges."""
        s = self.s
        inset = 13
        outer = _mix(INK, BRASS, 0.52)
        self.d.rectangle((inset * s, inset * s, (self.w - inset) * s, (self.h - inset) * s),
                         outline=outer, width=2)
        if ORNAMENT_LEVEL == "minimal":
            return
        self.d.rectangle(((inset + 4) * s, (inset + 4) * s,
                          (self.w - inset - 4) * s, (self.h - inset - 4) * s),
                         outline=_mix(INK, BRASS, 0.26), width=1)
        if ORNAMENT_LEVEL != "ornate":
            return
        # stepped corner brackets
        x0, y0, x1, y1 = inset + 8, inset + 8, self.w - inset - 8, self.h - inset - 8
        for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            for i, off in enumerate((0, 4, 8)):
                ln = 20 - i * 5
                col = _mix(INK, GOLD if i == 0 else BRASS, 0.55 - i * 0.1)
                self.d.line(((cx + sx * off) * s, (cy + sy * off) * s,
                             (cx + sx * (off + ln)) * s, (cy + sy * off) * s), fill=col, width=1)
                self.d.line(((cx + sx * off) * s, (cy + sy * off) * s,
                             (cx + sx * off) * s, (cy + sy * (off + ln)) * s), fill=col, width=1)
        self.lozenge(self.w / 2, inset + 4, 3.2, GOLD, fill=INK)
        self.lozenge(self.w / 2, self.h - inset - 4, 3.2, GOLD, fill=INK)

    def lozenge(self, cx, cy, r, color=GOLD, fill=None, width=1):
        """Small diamond, the recurring Art Deco punctuation mark."""
        s = self.s
        pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
        if fill is not None:
            self.d.polygon(self._pts(pts), fill=fill)
        self.d.line(self._pts(pts) + [self._pts(pts)[0]], fill=color,
                    width=max(1, int(round(width * s))))

    def bracket(self, x, y, size=12, color=LINE_2, corner="tl", width=1):
        """Small L-shaped corner mark."""
        s = self.s
        lw = max(1, int(round(width * s)))
        if corner == "tl":
            self.d.line([(x * s, y * s), ((x + size) * s, y * s)], fill=color, width=lw)
            self.d.line([(x * s, y * s), (x * s, (y + size) * s)], fill=color, width=lw)
        else:  # bottom-right
            self.d.line([((x - size) * s, y * s), (x * s, y * s)], fill=color, width=lw)
            self.d.line([(x * s, y * s), (x * s, (y - size) * s)], fill=color, width=lw)

    def divider(self, x, y, w, color=LINE, width=1, double=False, lozenge=False, major=False):
        s = self.s
        if major and ORNAMENT_LEVEL == "ornate":
            double = lozenge = True
        lw = max(1, int(round(width * s)))
        self.d.line((x * s, y * s, (x + w) * s, y * s), fill=color, width=lw)
        if double:
            self.d.line((x * s, (y + 3) * s, (x + w) * s, (y + 3) * s),
                        fill=_mix(color, INK, 0.35), width=1)
        if lozenge:
            self.lozenge(x + w / 2, y + (1.5 if double else 0), 3.4, GOLD, fill=INK)
        if major and ORNAMENT_LEVEL == "ornate":
            for ex in (x, x + w):
                self.d.line((ex * s, (y - 3) * s, ex * s, (y + 6) * s),
                            fill=_mix(color, GOLD, 0.45), width=1)

    def text(self, x, y, txt, size=16, fill=TEXT, bold=False, anchor="la", family="number"):
        self.d.text((x * self.s, y * self.s), str(txt), font=_font(size * self.s, bold, family),
                    fill=fill, anchor=anchor)

    def text_w(self, txt, size=16, bold=False, family="number") -> float:
        return self.d.textlength(str(txt), font=_font(size * self.s, bold, family)) / self.s

    def tracked_w(self, txt, size=16, bold=False, tracking=0.0, family="label") -> float:
        txt = str(txt)
        if not txt:
            return 0.0
        font = _font(size * self.s, bold, family)
        total = sum(self.d.textlength(ch, font=font) for ch in txt)
        total += tracking * self.s * (len(txt) - 1)
        return total / self.s

    def tracked_text(self, x, y, txt, size=16, fill=TEXT, bold=False, anchor="la",
                     tracking=1.0, family="label") -> float:
        """Letter-spaced text. PIL has no tracking, so glyphs are placed one by one.

        Returns the drawn width in card pixels. Vertical anchors follow PIL's names
        (a=top of the font box, m=middle, s=baseline, d=descender).
        """
        txt = str(txt)
        if not txt:
            return 0.0
        s = self.s
        font = _font(size * s, bold, family)
        widths = [self.d.textlength(ch, font=font) for ch in txt]
        total = sum(widths) + tracking * s * (len(txt) - 1)
        h_anchor, v_anchor = anchor[0], anchor[1]
        if h_anchor == "l":
            x0 = x * s
        elif h_anchor == "m":
            x0 = x * s - total / 2
        else:
            x0 = x * s - total
        ascent, descent = font.getmetrics()
        if v_anchor == "a":
            top = y * s
        elif v_anchor == "m":
            top = y * s - (ascent + descent) / 2
        elif v_anchor == "d":
            top = y * s - (ascent + descent)
        else:  # baseline
            top = y * s - ascent
        baseline = top + ascent
        px = x0
        for ch, w in zip(txt, widths):
            self.d.text((px, baseline), ch, font=font, fill=fill, anchor="ls")
            px += w + tracking * s
        return total / s

    def fit(self, txt: str, size: int, bold: bool, max_w: float, family="number") -> str:
        txt = str(txt)
        if self.text_w(txt, size, bold, family) <= max_w:
            return txt
        while len(txt) > 1 and self.text_w(txt + "...", size, bold, family) > max_w:
            txt = txt[:-1]
        return txt + "..."

    def fit_tracked(self, txt: str, size: int, bold: bool, max_w: float, tracking=1.0,
                    family="label") -> str:
        txt = str(txt)
        if self.tracked_w(txt, size, bold, tracking, family) <= max_w:
            return txt
        while len(txt) > 1 and self.tracked_w(txt + "...", size, bold, tracking, family) > max_w:
            txt = txt[:-1]
        return txt + "..."

    # --- motifs -------------------------------------------------------------
    def tag(self, x, y, txt, color=MUTED, size=9, height=None, tracking=1.2) -> float:
        """Stamped plate: tinted fill, double hairline border, tracked uppercase text."""
        h = height or size + 8
        tw = self.tracked_w(txt, size, True, tracking)
        w = tw + 14
        pts = _chamfer(x, y, w, h, 4)
        self.d.polygon(self._pts(pts), fill=_mix(PANEL, color, 0.15))
        if ORNAMENT_LEVEL != "minimal" and w > 30:
            ipts = _chamfer(x + 2, y + 2, w - 4, h - 4, 3)
            self.d.line(self._pts(ipts) + [self._pts(ipts)[0]],
                        fill=_mix(PANEL, color, 0.38), width=1)
        self.d.line(self._pts(pts) + [self._pts(pts)[0]], fill=_mix(PANEL, color, 0.82),
                    width=max(1, int(self.s * 0.75)), joint="curve")
        self.tracked_text(x + w / 2, y + h / 2, txt, size, color, True, "mm", tracking)
        return w

    def meter(self, x, y, w, pct, color=GREEN, segments=10, h=8):
        """Continuous gauge with tick divisions and an Art Deco end lozenge."""
        pct = 0 if pct is None else max(0, min(100, pct))
        s = self.s
        self.d.rectangle((x * s, y * s, (x + w) * s, (y + h) * s),
                         fill=_mix(PANEL, INK, 0.35), outline=LINE, width=1)
        fw = w * pct / 100
        if fw > 0:
            self.d.rectangle((x * s, y * s, (x + fw) * s, (y + h) * s), fill=color)
            self.d.line(((x + 1) * s, (y + 1) * s, (x + max(1, fw - 1)) * s, (y + 1) * s),
                        fill=_mix(color, TEXT, 0.35), width=1)
        if ORNAMENT_LEVEL != "minimal":
            for i in range(1, segments):
                tx = x + w * i / segments
                self.d.line((tx * s, y * s, tx * s, (y + h) * s),
                            fill=_mix(INK, color, 0.3) if tx <= x + fw else LINE, width=1)
        if ORNAMENT_LEVEL == "ornate" and fw > 2:
            self.lozenge(x + fw, y + h / 2, h * 0.62, GOLD, fill=INK)

    def panel_header(self, x, y, w, title, right="", color=MUTED, right_color=FAINT):
        """Section title + optional right-aligned meta + double rule. Returns the content y."""
        if ORNAMENT_LEVEL != "minimal":
            self.lozenge(x + 3, y + 5, 3.2, GOLD, fill=INK)
            tx = x + 11
        else:
            tx = x
        self.tracked_text(tx, y, title, 10, color, True, "la", 2.8)
        if right:
            self.tracked_text(x + w, y, right, 9, right_color, True, "ra", 1.6)
        self.divider(x, y + 18, w, LINE_2, double=ORNAMENT_LEVEL != "minimal",
                     lozenge=ORNAMENT_LEVEL == "ornate")
        if ORNAMENT_LEVEL == "ornate":
            for ex in (x, x + w):
                self.d.line((ex * self.s, (y + 15) * self.s, ex * self.s, (y + 24) * self.s),
                            fill=_mix(LINE_2, GOLD, 0.4), width=1)
        return y + 30

    def hatch(self, x, y, w, h, color=LINE, step=7, alpha=70):
        """Diagonal hairlines, used sparingly behind header bands."""
        s = self.s
        tw, th = max(1, int(w * s)), max(1, int(h * s))
        ov = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for off in range(-th, tw, max(1, int(step * s))):
            od.line([(off, th), (off + th, 0)], fill=color + (alpha,), width=1)
        self.img.paste(ov.convert("RGB"), (int(x * s), int(y * s)), ov)

    def watermark(self, data: bytes | None, x, y, w, h, alpha=0.34, cut=8):
        """Hero art as a brass/absinthe duotone, faded into the right side of a panel."""
        im = _load(data)
        if im is None:
            return
        s = self.s
        tw, th = max(1, int(w * s)), max(1, int(h * s))
        im = im.convert("RGB")
        ratio = th / im.height
        nw = max(1, int(im.width * ratio))
        im = im.resize((nw, th), Image.LANCZOS)
        if nw > tw:
            left = max(0, (nw - tw) // 2)
            im = im.crop((left, 0, left + tw, th))
        else:
            bg = Image.new("RGB", (tw, th), INK)
            bg.paste(im, ((tw - nw) // 2, 0))
            im = bg
        # duotone: emerald in the mids, brass in the highlights, ink in the shadows
        gray = ImageOps.grayscale(im)
        im = ImageOps.colorize(gray, black=INK, mid=_mix(INK, EMERALD, 0.42),
                               white=_mix(INK, GOLD, 0.72))
        im = Image.blend(im, Image.new("RGB", (tw, th), INK), 0.62)
        im = im.filter(ImageFilter.GaussianBlur(2 * s))

        mask = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(mask).polygon(
            [(min(cut * s, tw / 2), 0), (tw - min(cut * s, tw / 2), 0),
             (tw, min(cut * s, th / 2)), (tw, th - min(cut * s, th / 2)),
             (tw - min(cut * s, tw / 2), th), (min(cut * s, tw / 2), th),
             (0, th - min(cut * s, th / 2)), (0, min(cut * s, th / 2))], fill=255)
        fade = Image.new("L", (tw, 1))
        fade.putdata([int(255 * alpha * (i / max(1, tw - 1)) ** 1.6) for i in range(tw)])
        mask = ImageChops.multiply(mask, fade.resize((tw, th)))
        self.img.paste(im, (int(x * s), int(y * s)), mask)

    def sigil(self, cx, cy, r, color=BRASS, alpha=42):
        """Maelstrom sigil: broken concentric rings, radial ticks and a centre diamond."""
        if ORNAMENT_LEVEL == "minimal":
            return
        if ORNAMENT_LEVEL == "balanced":
            alpha = int(alpha * 0.68)
        s = self.s
        pad = 4
        size = int((2 * r + pad * 2) * s)
        ov = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        X = Y = size / 2

        def ring(rr, a0, a1, w=2):
            od.arc((X - rr * s, Y - rr * s, X + rr * s, Y + rr * s), a0, a1,
                   fill=color + (alpha,), width=max(1, int(w * s)))

        ring(r, -62, 38)
        ring(r, 58, 158)
        ring(r, 178, 278)
        ring(r * 0.72, -18, 118, 1.5)
        ring(r * 0.72, 142, 258, 1.5)
        ring(r * 0.46, 0, 360, 1)
        for i in range(8):  # radial ticks between the middle and outer rings
            a = radians(i * 45)
            od.line((X + 0.78 * r * s * cos(a), Y + 0.78 * r * s * sin(a),
                     X + 0.92 * r * s * cos(a), Y + 0.92 * r * s * sin(a)),
                    fill=color + (alpha,), width=max(1, int(s * 0.75)))
        c = 0.16 * r * s
        od.polygon([(X, Y - c), (X + c, Y), (X, Y + c), (X - c, Y)],
                   outline=color + (alpha,), width=max(1, int(s * 0.75)))
        self.img.paste(ov.convert("RGB"), (int(cx * s - size / 2), int(cy * s - size / 2)), ov)

    # --- image tiles --------------------------------------------------------
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

    def tri(self, cx, cy, size, up: bool, color):
        """A small filled up/down triangle (for a rank/PP change) — drawn, no icon file needed."""
        s, h = self.s, size * 0.9
        pts = [(cx, cy - h / 2), (cx - size / 2, cy + h / 2), (cx + size / 2, cy + h / 2)] if up else \
              [(cx, cy + h / 2), (cx - size / 2, cy - h / 2), (cx + size / 2, cy - h / 2)]
        self.d.polygon([(px * s, py * s) for px, py in pts], fill=color)

    def icon(self, data: bytes | None, x, y, w, h, cut=6, border=None, label="", focus=(0.5, 0.5),
             bg=PANEL_2, fit="cover", width=1):
        """Chamfered image tile; labelled placeholder if no image.
        fit="cover" crops to fill (portraits, with `focus`); fit="contain" shows the whole
        image letterboxed (item icons, so no artwork is cropped)."""
        s = self.s
        tw, th = max(1, int(w * s)), max(1, int(h * s))
        tile = Image.new("RGBA", (tw, th), bg + (255,))
        im = _load(data, label)
        if im is not None:
            im = im.convert("RGBA")
            if fit == "contain":
                shrunk = ImageOps.contain(im, tile.size, Image.LANCZOS)
                tile.alpha_composite(shrunk, ((tw - shrunk.width) // 2, (th - shrunk.height) // 2))
            else:
                tile.alpha_composite(ImageOps.fit(im, tile.size, Image.LANCZOS, centering=focus))
        mask = Image.new("L", (tw, th), 0)
        c = min(cut * s, tw / 2, th / 2)
        ImageDraw.Draw(mask).polygon(
            [(c, 0), (tw - c, 0), (tw, c), (tw, th - c), (tw - c, th), (c, th), (0, th - c), (0, c)],
            fill=255)
        self.img.paste(tile.convert("RGB"), (int(x * s), int(y * s)), mask)
        if im is None and label:
            words = label.split()
            lines = [words[0], " ".join(words[1:])] if len(words) > 1 else [words[0]]
            for i, line in enumerate(lines):
                if line:
                    self.text(x + w / 2, y + h / 2 + (i - 0.5 * (len(lines) > 1)) * 12,
                              self.fit(line, 10, True, w - 6), 10, MUTED, True, "mm")
        if border:
            pts = _chamfer(x, y, w, h, cut)
            self.d.line(self._pts(pts) + [self._pts(pts)[0]], fill=border,
                        width=max(1, int(round(width * s))), joint="curve")

    def avatar(self, data, x, y, size, cut=9, border=LINE_2):
        """Chamfered square profile picture."""
        self.icon(data, x, y, size, size, cut=cut, border=border, bg=PANEL_2, fit="cover",
                  focus=(0.5, 0.35), width=1)

    def _finish(self):
        """Grain and vignette, applied once at 2x before the final downsample."""
        if TEXTURE != "print" or self._finished:
            return
        img = self.img
        w, h = img.size
        noise = Image.effect_noise((w, h), 24).convert("RGB")
        img = Image.blend(img, ImageChops.overlay(img, noise), 0.26)
        vw, vh = 120, max(1, int(120 * h / w))
        mask = Image.new("L", (vw, vh), 0)
        md = ImageDraw.Draw(mask)
        for yy in range(vh):
            for xx in range(vw):
                dx = (xx / max(1, vw - 1) - 0.5) * 2
                dy = (yy / max(1, vh - 1) - 0.5) * 2
                d = (dx * dx + dy * dy) ** 0.5
                t = max(0.0, min(1.0, (d - 0.6) / 0.62))
                mask.putpixel((xx, yy), int(96 * t * t))
        mask = mask.resize((w, h), Image.BILINEAR)
        img = Image.composite(Image.new("RGB", (w, h), (2, 4, 3)), img, mask)
        self.img = img
        self.d = ImageDraw.Draw(img)
        self._finished = True

    def png(self) -> io.BytesIO:
        self._finish()
        out = io.BytesIO()
        self.img.resize((self.w, self.h), Image.LANCZOS).save(out, "PNG", optimize=True)
        out.seek(0)
        return out


# =============================================================================
# shared pieces
# =============================================================================
def _footer(c: Canvas, y: int, statlocker: bool):
    if statlocker:
        c.tracked_text(40, y, "POWERED BY STATLOCKER", 9, BRASS, True, "la", 2.0)
    c.tracked_text(960, y, "MATCH DATA · DEADLOCK-API.COM", 9, FAINT, True, "ra", 1.5)


def _mmss(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _stat_tile(c: Canvas, x, y, w, h, label, value, color=TEXT, soul=False,
               sub=None, meter_pct=None, meter_color=None):
    c.panel(x, y, w, h)
    c.tracked_text(x + 16, y + 14, label.upper(), 9, MUTED, True, "la", 2.4)
    if soul:
        c.soul(x + 25, y + 54, 18)
        c.text(x + 40, y + 36, value, 32, color, True)
    else:
        c.text(x + 16, y + 36, value, 32, color, True)
    if meter_pct is not None:
        c.meter(x + 16, y + h - 20, w - 32, meter_pct, meter_color or color, segments=10, h=7)
    elif sub:
        c.tracked_text(x + 16, y + h - 22, sub.upper(), 9, FAINT, True, "la", 1.4)


def _stats_panel(c: Canvas, x, y, w, h, s: MatchStats):
    """Compact 5x2 hairline grid of combat/farming stats plus a kill-breakdown line."""
    c.panel(x, y, w, h)
    c.panel_header(x + 24, y + 14, w - 48, "MATCH STATS", "END OF MATCH")

    cols, pad, tile_h = 5, 24, 58
    colw = (w - 2 * pad) / cols
    rows = [
        [("Hero damage", _fmt(s.hero_damage, "{:,.0f}"), BRASS),
         ("Damage taken", _fmt(s.damage_taken, "{:,.0f}"), RED),
         ("Healing", _fmt(s.healing, "{:,.0f}"), GREEN),
         ("Mitigated", _fmt(s.damage_mitigated, "{:,.0f}"), TEXT),
         ("Accuracy", _fmt(s.accuracy, "{:.1f}", "%"), TEXT)],
        [("Creep damage", _fmt(s.creep_damage, "{:,.0f}"), TEXT),
         ("Boss damage", _fmt(s.boss_damage, "{:,.0f}"), TEXT),
         ("Neutral damage", _fmt(s.neutral_damage, "{:,.0f}"), TEXT),
         ("Crit rate", _fmt(s.crit_rate, "{:.1f}", "%"), TEXT),
         ("Level", _fmt(s.level), TEXT)],
    ]
    top = y + 44
    for row, tiles in enumerate(rows):
        for i, (label, value, color) in enumerate(tiles):
            tx = x + pad + i * colw
            ty = top + row * tile_h
            c.tracked_text(tx + 14, ty + 6, label.upper(), 8.5, MUTED, True, "la", 1.8)
            c.text(tx + 14, ty + 20, value, 21, color, True)
    for i in range(1, cols):
        c.d.line(((x + pad + i * colw) * c.s, (top + 4) * c.s,
                  (x + pad + i * colw) * c.s, (top + 2 * tile_h - 4) * c.s),
                 fill=LINE, width=1)
    c.divider(x + pad, top + tile_h, w - 2 * pad, LINE)

    parts = [f"{v} {name}" for name, v in (("bullet", s.bullet_kills), ("melee", s.melee_kills),
                                           ("ability", s.ability_kills), ("headshot", s.headshot_kills))
             if v is not None]
    line = "KILLS  " + " · ".join(parts) if parts else ""
    if s.creep_kills is not None:
        creeps = f"Creeps {s.creep_kills}" + (f"/{s.possible_creeps}" if s.possible_creeps is not None else "")
        line += ("   |   " if line else "") + creeps
    if s.neutral_kills is not None:
        line += ("   |   " if line else "") + f"Neutrals {s.neutral_kills}"
    if line:
        c.tracked_text(x + 24, y + h - 24, c.fit_tracked(line.upper(), 9, True, w - 48, 1.2),
                       9, MUTED, True, "la", 1.2)


def _party_panel(c: Canvas, x, y, w, h, party: list["PartyMemberView"]):
    """Same-party mates: hero tile, name, hero played, K/D/A and net worth."""
    c.panel(x, y, w, h)
    c.panel_header(x + 24, y + 16, w - 48, "PARTY",
                   f"YOU + {len(party)} MATE{'S' if len(party) != 1 else ''}")
    top = y + 46
    for i, p in enumerate(party):
        cy = top + i * PARTY_ROW_H + PARTY_ROW_H / 2
        c.icon(p.hero_icon, x + 24, cy - 16, 32, 32, cut=5, border=LINE, label=p.hero,
               focus=(0.5, 0.3))
        c.icon(p.avatar, x + 62, cy - 16, 32, 32, cut=5, border=LINE_2, bg=PANEL_3,
               label=(p.name[:1] or "?").upper(), focus=(0.5, 0.35))
        c.tracked_text(x + 106, cy, c.fit_tracked(p.name.upper(), 14, True, 160, 1.2, "display"),
                       14, TEXT, True, "lm", 1.2, family="display")
        c.tracked_text(x + 292, cy, c.fit_tracked(p.hero.upper(), 9.5, True, 190, 1.6),
                       9.5, MUTED, True, "lm", 1.6)
        kx = x + 564
        for j, (val, col) in enumerate(((p.kills, GREEN), (p.deaths, RED), (p.assists, TEXT))):
            c.text(kx + j * 46, cy, _fmt(val), 15, col, True, "rm")
            if j < 2:
                c.text(kx + j * 46 + 6, cy, "/", 11, FAINT, False, "lm")
        if p.net_worth is not None:
            nw = _fmt(p.net_worth, "{:,.0f}")
            c.text(x + w - 24, cy, nw, 14, MINT, True, "rm")
            c.soul(x + w - 24 - c.text_w(nw, 14, True) - 12, cy, 15)
        if i < len(party) - 1:
            c.divider(x + 24, top + (i + 1) * PARTY_ROW_H, w - 48, LINE)


# =============================================================================
@dataclass
class ScoreRow:
    hero: str
    name: str
    kills: int | None
    deaths: int | None
    assists: int | None
    net_worth: int | None
    damage: int | None
    hero_icon: bytes | None = None
    rank_badge: bytes | None = None
    is_self: bool = False
    mvp: bool = False
    global_rank: int | None = None
    kill_part: float | None = None
    obj_damage: int | None = None
    dmg_per_min: float | None = None
    souls_per_min: float | None = None
    accuracy: float | None = None
    damage_taken: int | None = None
    healing: int | None = None
    level: int | None = None


@dataclass
class ScoreTeam:
    label: str
    won: bool | None
    rows: list[ScoreRow] = field(default_factory=list)


@dataclass
class Scoreboard:
    own: ScoreTeam
    enemy: ScoreTeam


def _pct(v) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if f <= 1.0:
        f *= 100
    return f"{f:.0f}%"


def _scoreboard_panel(c: Canvas, x, y, w, b: Scoreboard) -> int:
    """Stacked two-team scoreboard. Returns panel height."""
    teams = [b.own, b.enemy]
    h = 46 + sum(30 + max(1, len(t.rows)) * SCORE_ROW_H + 6 for t in teams) + 4
    c.panel(x, y, w, h)
    c.panel_header(x + 24, y + 16, w - 48, "SCOREBOARD", None)
    # top row: combat; bottom row: level/econ/survivability (Statlocker when available)
    c.tracked_text(x + 360, y + 18, "K / D / A", 8, FAINT, True, "la", 1.4)
    c.tracked_text(x + 455, y + 18, "KP", 8, FAINT, True, "la", 1.4)
    c.tracked_text(x + 510, y + 18, "ACC", 8, FAINT, True, "la", 1.4)
    c.tracked_text(x + w - 170, y + 18, "DMG", 8, FAINT, True, "ra", 1.4)
    c.tracked_text(x + w - 95, y + 18, "OBJ", 8, FAINT, True, "ra", 1.4)
    c.tracked_text(x + w - 24, y + 18, "TAKEN", 8, FAINT, True, "ra", 1.4)
    yy = y + 46
    for team in teams:
        col = GREEN if team.won is True else RED if team.won is False else BRASS
        c.tracked_text(x + 24, yy + 4, team.label.upper(), 9, col, True, "la", 2.0)
        kills = sum(r.kills or 0 for r in team.rows) if team.rows else 0
        c.tracked_text(x + w - 24, yy + 4, f"{kills} KILLS", 9, MUTED, True, "ra", 1.6)
        c.divider(x + 24, yy + 24, w - 48, LINE)
        yy += 30
        if not team.rows:
            c.text(x + 40, yy + 8, "No player data.", 11, MUTED)
            yy += SCORE_ROW_H
        for i, r in enumerate(team.rows):
            top = yy + 4
            mid1 = yy + 16
            mid2 = yy + 36
            if r.is_self:
                c.d.rectangle(((x + 8) * c.s, (yy + 2) * c.s, (x + w - 8) * c.s, (yy + SCORE_ROW_H - 2) * c.s),
                              fill=_mix(PANEL_2, EMERALD if team.won is not False else PANEL_3, 0.16))
                c.d.rectangle(((x + 10) * c.s, (yy + 6) * c.s, (x + 13) * c.s, (yy + SCORE_ROW_H - 6) * c.s), fill=col)
            c.icon(r.hero_icon, x + 24, top + 3, 30, 30, cut=5, border=col, label=r.hero,
                   focus=(0.5, 0.3), bg=PANEL_2, width=1)
            nx = x + 62
            name = c.fit(r.name, 12, True, 180, family="body")
            c.text(nx, mid1, name, 12, TEXT if r.is_self else MUTED, True, "lm", family="body")
            after = nx + c.text_w(name, 12, True) + 6
            if r.mvp:
                c.tracked_text(after, mid1 - 4, "MVP", 8, BRASS, True, "la", 1.0)
            elif r.global_rank is not None and 0 < r.global_rank <= 500:
                c.tracked_text(after, mid1 - 4, f"#{r.global_rank}", 8, FAINT, True, "la", 0.8)
            if r.rank_badge:
                c.icon(r.rank_badge, x + 274, top + 5, 20, 20, cut=4, border=LINE_2, bg=PANEL)

            c.text(x + 360, mid1, f"{_fmt(r.kills)}/{_fmt(r.deaths)}/{_fmt(r.assists)}", 13, TEXT, True, "lm")
            if r.kill_part is not None:
                c.text(x + 455, mid1, _pct(r.kill_part), 12, TEXT, True, "lm")
            if r.accuracy is not None:
                c.text(x + 510, mid1, _pct(r.accuracy), 12, MUTED, True, "lm")
            if r.damage is not None:
                c.text(x + w - 170, mid1, _fmt(r.damage, "{:,.0f}"), 12, BRASS, True, "rm")
            if r.obj_damage is not None:
                c.text(x + w - 95, mid1, _fmt(r.obj_damage, "{:,.0f}"), 12, STEEL, True, "rm")
            if r.damage_taken is not None:
                c.text(x + w - 24, mid1, _fmt(r.damage_taken, "{:,.0f}"), 12, RED, True, "rm")

            # second line: level, net worth, healing (+ dpm/spm when known)
            details = []
            if r.level is not None:
                details.append((f"Lv {r.level}", TEXT))
            if r.net_worth is not None:
                details.append((f"{_fmt(r.net_worth, '{:,.0f}')} souls", MINT))
            if r.healing is not None:
                details.append((f"Heal {_fmt(r.healing, '{:,.0f}')}", GREEN))
            if r.dmg_per_min is not None:
                details.append((f"{_fmt(r.dmg_per_min, '{:,.0f}')}/min dmg", MUTED))
            if r.souls_per_min is not None:
                details.append((f"{_fmt(r.souls_per_min, '{:,.0f}')}/min souls", MUTED))
            dx = nx
            for j, (txt, tcol) in enumerate(details):
                if j:
                    c.text(dx, mid2, "·", 9, FAINT, False, "lm")
                    dx += c.text_w("·", 9, False) + 8
                c.text(dx, mid2, txt, 10, tcol, False, "lm")
                dx += c.text_w(txt, 10, False) + 8
                if dx > x + w - 40:
                    break

            if i < len(team.rows) - 1:
                c.divider(x + 24, yy + SCORE_ROW_H, w - 48, LINE)
            yy += SCORE_ROW_H
        yy += 6
    return h


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
    c = Canvas(1000, 680)

    # header ---------------------------------------------------------------
    hx, hy, hw, hh = 40, 28, 920, 152
    c.panel(hx, hy, hw, hh, rail=GREEN)
    c.sigil(884, 106, 54, EMERALD, 30)
    c.bracket(hx + 10, hy + 10, 12, LINE_2)
    c.bracket(hx + hw - 10, hy + hh - 10, 12, LINE_2)

    c.avatar(d.avatar, 64, 52, 100)
    if d.avatar is None:
        c.text(114, 102, (d.name[:1] or "?").upper(), 44, MUTED, True, "mm", family="display")
    if d.rank_badge:
        c.panel(126, 114, 54, 54, fill=INK, outline=LINE_2, cut=6)
        c.icon(d.rank_badge, 130, 118, 46, 46, cut=5, border=BRASS, bg=PANEL)

    c.tracked_text(196, 56, c.fit_tracked(d.name.upper(), 36, True, 700, 1.4, "display"), 36,
                   TEXT, True, "la", 1.4, family="display")
    line2 = f"LAST {o.matches} MATCHES  ·  ACCOUNT {d.account_id}"
    lw = c.tracked_text(196, 112, line2, 10, MUTED, True, "la", 1.8)
    if o.streak >= 2:  # a streak of 1 isn't worth calling out
        col = GREEN if o.streak_won else RED
        c.tag(196 + lw + 12, 105, f"{'W' if o.streak_won else 'L'}{o.streak}", col, size=9)
    if d.rank_name:
        rx = 196 + c.tracked_text(196, 140, d.rank_name.upper(), 13, BRASS, True, "la", 2.0) + 14
        if d.pp_score:
            c.tracked_text(rx, 141, f"{d.pp_score} PP", 10, MUTED, True, "la", 1.5)

    # headline tiles --------------------------------------------------------
    ty, th, gap = 196, 108, 12
    tw = (920 - 3 * gap) / 4
    wr = o.winrate
    wr_color = TEXT if wr is None else (GREEN if wr >= 50 else RED)
    tiles = [
        ("Matches", _fmt(o.matches), TEXT, False, f"{o.wins}W · {o.losses}L", None, None),
        ("Win rate", _fmt(wr, "{:.1f}", "%"), wr_color, False, None, wr, wr_color),
        ("KDA", _fmt(o.kda, "{:.2f}"), TEXT, False, None, None, None),
        ("Souls / min", _fmt(o.souls_per_min, "{:,.0f}"), MINT, True, None, None, None),
    ]
    for i, (label, value, color, soul, sub, m_pct, m_col) in enumerate(tiles):
        _stat_tile(c, 40 + i * (tw + gap), ty, tw, th, label, value, color, soul, sub, m_pct, m_col)

    rec = f"{o.wins}W  ·  {o.losses}L"
    if o.avg_kills is not None:
        rec += (f"      AVG K/D/A  {o.avg_kills:.1f} / {_fmt(o.avg_deaths, '{:.1f}')}"
                f" / {_fmt(o.avg_assists, '{:.1f}')}")
    c.tracked_text(40, 320, rec.upper(), 10, MUTED, True, "la", 2.0)

    # most played heroes ----------------------------------------------------
    py, ph = 348, 268
    c.panel(40, py, 920, ph)
    c.panel_header(64, py + 16, 872, "MOST PLAYED HEROES", "TOP 5 · BY GAMES")
    if not d.heroes:
        c.tracked_text(64, py + 96, "NO HERO DATA AVAILABLE", 11, MUTED, True, "la", 2.0)
    row_h, y0 = 40, py + 46
    for i, h in enumerate(d.heroes[:5]):
        cy = y0 + i * row_h + row_h / 2
        c.tracked_text(64, cy, f"{i + 1:02d}", 10, FAINT, True, "lm", 1.2)
        c.icon(h.icon, 88, cy - 16, 32, 32, cut=5, border=LINE, label=h.name, focus=(0.5, 0.3))
        c.tracked_text(132, cy, c.fit_tracked(h.name.upper(), 14, True, 230, 1.2, "display"), 14,
                       TEXT, True, "lm", 1.2, family="display")
        c.tracked_text(400, cy, f"{h.games} GAMES", 9, MUTED, True, "lm", 1.6)
        wr_h = h.winrate or 0
        c.meter(560, cy - 4, 250, wr_h, GREEN if wr_h >= 50 else RED, segments=10, h=8)
        c.text(936, cy, f"{wr_h:.0f}%", 13, GREEN if wr_h >= 50 else RED, True, "rm")
        if i < min(len(d.heroes), 5) - 1:
            c.divider(64, y0 + (i + 1) * row_h, 872, LINE)

    c.divider(40, 632, 920, LINE, major=True)
    _footer(c, 642, d.statlocker)
    return c.png()


# =============================================================================
@dataclass
class ItemView:
    name: str
    icon: bytes | None = None
    slot: str | None = None
    tier: int | None = None


@dataclass
class PartyMemberView:
    """One same-party mate on the match card. `hero_icon`/`avatar` are already
    downloaded bytes by render time (the bot swaps the URLs it used to load them)."""
    name: str
    hero: str
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    net_worth: int | None = None
    hero_icon: bytes | None = None
    avatar: bytes | None = None


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
    party: list[PartyMemberView] = field(default_factory=list)  # same-party mates, excluding you
    unnamed_items: int = 0  # bought, but no catalogue entry to show
    mode: str | None = None             # match-type tag, e.g. "Ranked" / "Street Brawl"
    mode_kind: str | None = None        # ranked / brawl / bot / standard / other (tag colour)
    stats: MatchStats | None = None     # end-of-match combat/farming stats
    brawl_score: tuple[int, int] | None = None  # (own, enemy) rounds won, brawl only
    brawl_round_avg_s: int | None = None
    own_lobby_rank: str | None = None
    enemy_lobby_rank: str | None = None
    own_lobby_rank_badge: bytes | None = None
    enemy_lobby_rank_badge: bytes | None = None
    rank_name: str | None = None        # player's own current rank (e.g. "Archon 3")
    rank_prev: str | None = None        # rank before this match, when it moved
    rank_up: bool | None = None         # True promoted, False demoted, None no change
    rank_badge: bytes | None = None
    pp_score: int | None = None
    pp_delta: int | None = None         # change since the last match tracked for this player
    scoreboard: Scoreboard | None = None
    statlocker: bool = False


def render_match_card(d: MatchCardData) -> io.BytesIO:
    items = sorted(d.items, key=lambda i: SLOT_ORDER.get(i.slot, 3))  # slot groups, purchase order inside
    shown = items[:MAX_ITEMS]
    rows = max(1, ceil(len(shown) / ITEMS_PER_ROW))
    items_h = 40 + rows * (ITEM_CELL_H + ITEM_GAP) + 12 if shown else 92
    has_lobby = bool(d.own_lobby_rank or d.enemy_lobby_rank)
    color = GREEN if d.won is True else RED if d.won is False else BRASS

    y_kda, kda_h = 192, 150
    y_items = (y_kda + kda_h) + 14 + (STATS_PANEL_H + 14 if d.stats else 0)
    party = d.party[:5]  # a party is at most six including you
    party_h = 58 + len(party) * PARTY_ROW_H if party else 0
    y_party = y_items + items_h + 14
    board = d.scoreboard if d.scoreboard and (d.scoreboard.own.rows or d.scoreboard.enemy.rows) else None
    score_h = (46 + sum(30 + max(1, len(t.rows)) * SCORE_ROW_H + 6
                        for t in (board.own, board.enemy)) + 4) if board else 0
    after_party = (y_party + party_h) if party else (y_items + items_h)
    y_score = after_party + 14 if board else None
    y_end = (y_score + score_h) if board else after_party
    y = y_end + 18 + (26 if has_lobby else 0)
    c = Canvas(1000, y + 48)

    # banner: watermark + sigil + portrait + result ------------------------
    c.panel(40, 28, 920, 152, rail=color)
    c.watermark(d.hero_icon, 42, 30, 916, 148, alpha=0.34)
    c.sigil(878, 108, 60, BRASS, 36)
    c.bracket(50, 38, 12, LINE_2)
    c.bracket(950, 170, 12, LINE_2)
    c.icon(d.hero_icon, 64, 46, 116, 116, cut=10, border=color, label=d.hero, focus=(0.5, 0.3),
           bg=PANEL_2)

    result = "VICTORY" if d.won is True else "DEFEAT" if d.won is False else "MATCH"
    c.tracked_text(200, 44, result, 44, color, True, "la", 3.0, family="display")
    c.tracked_text(200, 98, c.fit_tracked(d.hero.upper(), 21, True, 330, 1.4, "display"), 21,
                   TEXT, True, "la", 1.4, family="display")

    x = 200
    x += c.tracked_text(x, 132, f"MATCH #{d.match_id}", 9.5, MUTED, True, "la", 1.8) + 14
    if d.mode:
        x += c.tag(x, 124, d.mode.upper(), MODE_COLORS.get(d.mode_kind or "", MUTED), size=9) + 8
    if d.rank_badge and (d.rank_name or d.rank_prev):
        c.icon(d.rank_badge, x, 122, 22, 22, cut=4, border=BRASS, bg=PANEL)
        x += 28
    if d.rank_prev:  # moved: old tag muted, new tag's colour carries the direction
        x += c.tag(x, 124, d.rank_prev.upper(), MUTED, size=9) + 6
    if d.rank_name:
        rank_txt = d.rank_name.upper() + (f" · {d.pp_score} PP" if d.pp_score is not None else "")
        rank_col = BRASS if d.rank_up is None else GREEN if d.rank_up else RED
        x += c.tag(x, 124, rank_txt, rank_col, size=9) + 8
    if d.pp_delta:
        up, col = d.pp_delta > 0, GREEN if d.pp_delta > 0 else RED
        c.tri(x + 6, 130, 9, up, col)
        c.text(x + 15, 132, f"{'+' if up else ''}{d.pp_delta}", 11, col, True)

    right = 860 if d.avatar else 936
    if d.avatar:
        c.avatar(d.avatar, 936 - 56, 46, 56)
    c.tracked_text(right, 46, c.fit_tracked(d.name.upper(), 17, True, 290, 1.2, "display"), 17,
                   TEXT, True, "ra", 1.2, family="display")
    if d.start_time:
        c.tracked_text(right, 78, datetime.fromtimestamp(d.start_time, timezone.utc)
                       .strftime("%d %b %Y · %H:%M UTC").upper(), 9, MUTED, True, "ra", 1.4)
    if d.duration_s:
        c.tracked_text(right, 96, f"DURATION {_mmss(d.duration_s)}", 9, MUTED, True, "ra", 1.4)

    # K / D / A + economy ---------------------------------------------------
    c.panel(40, y_kda, 920, kda_h)
    for i, (label, val, col) in enumerate([("Kills", d.kills, GREEN), ("Deaths", d.deaths, RED),
                                           ("Assists", d.assists, TEXT)]):
        cx = 40 + 540 * (i * 2 + 1) / 6
        c.tracked_text(cx, 212, label.upper(), 9, MUTED, True, "ma", 3.0)
        c.text(cx, 244, _fmt(val), 58, col, True, "ma")
    if None not in (d.kills, d.deaths, d.assists):
        c.tracked_text(310, 322, f"KDA {(d.kills + d.assists) / max(d.deaths, 1):.2f}", 9.5, MUTED,
                       True, "ma", 2.0)
    c.d.line((580 * c.s, 212 * c.s, 580 * c.s, 324 * c.s), fill=LINE, width=1)

    if d.brawl_score is not None:
        # Street Brawl has no soul economy: round results are the useful numbers there.
        own, enemy = d.brawl_score
        right_tiles = [("Rounds won", str(own), False), ("Rounds lost", str(enemy), False),
                       ("Round score", f"{own} - {enemy}", False),
                       ("Avg round", _mmss(d.brawl_round_avg_s), False)]
    else:
        spm = d.net_worth / (d.duration_s / 60) if d.net_worth and d.duration_s else None
        right_tiles = [("Net worth", _fmt(d.net_worth, "{:,.0f}"), True),
                       ("Souls / min", _fmt(spm, "{:,.0f}"), True),
                       ("Last hits", _fmt(d.last_hits), False), ("Denies", _fmt(d.denies), False)]
    for (label, value, soul), (x, y) in zip(right_tiles,
                                            [(612, 212), (792, 212), (612, 274), (792, 274)]):
        c.tracked_text(x, y, label.upper(), 8.5, MUTED, True, "la", 1.8)
        if soul:
            c.soul(x + 10, y + 30, 17)
        c.text(x + (24 if soul else 0), y + 14, value, 25, TEXT, True)

    if d.stats:
        _stats_panel(c, 40, y_kda + kda_h + 14, 920, STATS_PANEL_H, d.stats)

    # items bought ----------------------------------------------------------
    c.panel(40, y_items, 920, items_h)
    count = f"{len(items) + d.unnamed_items} ITEMS"
    if d.unnamed_items:
        count += f" · {d.unnamed_items} UNLISTED"
    c.panel_header(64, y_items + 16, 872, "ITEMS BOUGHT", count)
    if not shown:
        msg = "Item data isn't available for this match yet." if not d.unnamed_items else \
            f"{d.unnamed_items} items bought, but their names/icons couldn't be loaded."
        c.text(64, y_items + 52, msg, 13, MUTED)
    grid_w = ITEMS_PER_ROW * ITEM_SIZE + (ITEMS_PER_ROW - 1) * ITEM_GAP
    for i, it in enumerate(shown):
        x = 40 + (920 - grid_w) // 2 + (i % ITEMS_PER_ROW) * (ITEM_SIZE + ITEM_GAP)
        y = y_items + 46 + (i // ITEMS_PER_ROW) * (ITEM_CELL_H + ITEM_GAP)
        col = SLOT_COLORS.get(it.slot, MUTED)
        c.icon(it.icon, x, y, ITEM_SIZE, ITEM_SIZE, cut=6, border=col, label=it.name, fit="contain",
               width=1.5)
        if it.tier in ROMAN:
            c.panel(x + ITEM_SIZE - 25, y + ITEM_SIZE - 19, 21, 15, fill=INK, outline=col, cut=3,
                    width=1)
            c.text(x + ITEM_SIZE - 14.5, y + ITEM_SIZE - 11.5, ROMAN[it.tier], 9, col, True, "mm")
        c.text(x + ITEM_SIZE / 2, y + ITEM_SIZE + 2,
               c.fit(it.name, 9, False, ITEM_SIZE + ITEM_GAP - 4, family="body"), 9, MUTED, False,
               "ma", family="body")
    if len(items) > MAX_ITEMS:
        c.tracked_text(936, y_items + items_h - 16, f"+{len(items) - MAX_ITEMS} MORE", 9, MUTED,
                       True, "ra", 1.4)

    # party ----------------------------------------------------------------
    if party:
        _party_panel(c, 40, y_party, 920, party_h, party)

    # scoreboard -----------------------------------------------------------
    if board:
        _scoreboard_panel(c, 40, y_score, 920, board)

    # lobby + footer --------------------------------------------------------
    y = y_end + 18
    if has_lobby:
        c.tracked_text(40, y, "LOBBY AVERAGE RANK", 9, FAINT, True, "la", 2.0)
        lx = 40 + c.tracked_w("LOBBY AVERAGE RANK", 9, True, 2.0) + 18
        own_label = f"YOUR TEAM · {d.own_lobby_rank or '-'}"
        if d.own_lobby_rank_badge:
            c.icon(d.own_lobby_rank_badge, lx, y - 6, 18, 18, cut=4, border=LINE_2, bg=PANEL)
            lx += 22
        lx += c.tag(lx, y - 5, own_label, GREEN, size=9) + 10
        if d.enemy_lobby_rank_badge:
            c.icon(d.enemy_lobby_rank_badge, lx, y - 6, 18, 18, cut=4, border=LINE_2, bg=PANEL)
            lx += 22
        c.tag(lx, y - 5, f"ENEMY TEAM · {d.enemy_lobby_rank or '-'}", RED, size=9)
        y += 26
    c.divider(40, y + 6, 920, LINE, major=True)
    _footer(c, y + 14, d.statlocker)
    return c.png()


# =============================================================================
@dataclass
class MatchRow:
    hero: str
    won: bool | None
    kills: int | None
    deaths: int | None
    assists: int | None
    match_id: int | None = None
    start_time: int | None = None
    duration_s: int | None = None
    hero_icon: bytes | None = None
    mode: str | None = None
    mode_kind: str | None = None


@dataclass
class MatchesCardData:
    name: str
    account_id: int
    matches: list[MatchRow] = field(default_factory=list)
    avatar: bytes | None = None
    statlocker: bool = False


ROW_H = 64


def render_matches_card(d: MatchesCardData) -> io.BytesIO:
    rows = d.matches
    body_h = max(1, len(rows)) * ROW_H + 12
    y0 = 124
    c = Canvas(1000, y0 + body_h + 60)

    # header ---------------------------------------------------------------
    c.panel(40, 28, 920, 84, rail=GREEN)
    c.bracket(50, 38, 12, LINE_2)
    c.bracket(950, 102, 12, LINE_2)
    c.avatar(d.avatar, 56, 46, 48)
    if d.avatar is None:
        c.text(80, 70, (d.name[:1] or "?").upper(), 20, MUTED, True, "mm", family="display")
    c.tracked_text(120, 46, c.fit_tracked(d.name.upper(), 22, True, 560, 1.2, "display"), 22,
                   TEXT, True, "la", 1.2, family="display")
    c.tracked_text(120, 74, c.fit_tracked(f"LAST {len(rows)} MATCHES  ·  ACCOUNT {d.account_id}",
                                          9, True, 620, 1.6), 9, MUTED, True, "la", 1.6)
    wins = sum(1 for r in rows if r.won is True)
    losses = sum(1 for r in rows if r.won is False)
    lw = c.tracked_w(f"{losses}L", 16, True, 1.5)
    c.tracked_text(936, 60, f"{losses}L", 16, RED, True, "ra", 1.5)
    c.tracked_text(936 - lw - 16, 60, f"{wins}W", 16, GREEN, True, "ra", 1.5)

    # rows -----------------------------------------------------------------
    c.panel(40, y0, 920, body_h)
    if not rows:
        c.tracked_text(500, y0 + body_h / 2, "NO RECENT MATCHES FOUND", 11, MUTED, True, "mm", 2.0)
    for i, r in enumerate(rows):
        y = y0 + 6 + i * ROW_H
        cy = y + ROW_H / 2
        color = GREEN if r.won is True else RED if r.won is False else BRASS
        if i % 2 == 1:
            c.d.rectangle((41 * c.s, y * c.s, 959 * c.s, (y + ROW_H) * c.s),
                          fill=_mix(PANEL, PANEL_2, 0.6))
        c.d.rectangle((42 * c.s, (y + 9) * c.s, 45 * c.s, (y + ROW_H - 9) * c.s), fill=color)
        c.icon(r.hero_icon, 58, y + 12, 40, 40, cut=6, border=color, label=r.hero,
               focus=(0.5, 0.3), width=1)
        c.tracked_text(112, cy - 9, "WIN" if r.won is True else "LOSS" if r.won is False else "—",
                       9, color, True, "lm", 2.4)
        c.tracked_text(112, cy + 5, c.fit_tracked(r.hero.upper(), 15, True, 160, 1.0, "display"),
                       15, TEXT, True, "lm", 1.0, family="display")
        if r.mode:
            c.tag(292, cy - 9, r.mode.upper(), MODE_COLORS.get(r.mode_kind or "", MUTED), size=9)
        if None not in (r.kills, r.deaths, r.assists):
            c.text(540, cy, f"{r.kills} / {r.deaths} / {r.assists}", 16, TEXT, True, "lm")
            ratio = (r.kills + r.assists) / max(r.deaths, 1)
            c.tag(660, cy - 9, f"KDA {ratio:.2f}", MUTED, size=9)
        if r.duration_s:
            c.tracked_text(770, cy, _mmss(r.duration_s), 12, MUTED, True, "lm", 1.2)
        if r.start_time:
            c.tracked_text(936, cy + 3, datetime.fromtimestamp(r.start_time, timezone.utc)
                           .strftime("%d %b · %H:%M").upper(), 9, MUTED, True, "rm", 1.2)
        if r.match_id:
            c.tracked_text(936, cy - 11, f"#{r.match_id}", 9, FAINT, True, "rm", 1.0)
        if i < len(rows) - 1:
            c.divider(58, y + ROW_H, 878, LINE)

    y = y0 + body_h + 14
    c.divider(40, y, 920, LINE, major=True)
    _footer(c, y + 8, d.statlocker)
    return c.png()
