"""Steam news feed parsing for Deadlock patch notes.

Only `fetch_feed` touches the network; everything else is pure, so the parser can be tested
on Windows without Discord, aiohttp or Steam being reachable.

Steam's RSS feed lists the last 10 Valve posts, newest first. Patch notes are recognised by
their title ("Minor Update - 11-04-2026", "… Update") or, when Valve uses a themed title
("Halloween 2026 + Gameplay Update"), by the number of hero lines in the body.
"""
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime

import aiohttp

FEED_URL = "https://store.steampowered.com/feeds/news/app/1422450/"
USER_AGENT = "Mozilla/5.0 deadlockstatsbot/2.0"
MAX_PREVIEW_LINES = 8
MAX_MESSAGE_CHARS = 1900  # leave room under Discord's 2000-char content limit
MIN_THEMED_HEROES = 5     # an untitled update needs at least this many matched hero lines

# "- Hero: rest of the change" (also "- Mo & Krill: …", "- Deadman Danny: …")
_HERO_LINE = re.compile(r"^-\s*([A-Z][A-Za-z0-9'&.\- ]{0,30}?)\s*:\s*(\S.*)$")
_UPDATE_TITLE = re.compile(r"^\s*(?:minor\s+)?update\b", re.IGNORECASE)


@dataclass
class PatchNote:
    guid: str
    title: str
    link: str
    pub_ts: int
    lines: list[str] = field(default_factory=list)
    # hero name -> the change text after "- Hero: " (items/modes are not heroes and stay out)
    heroes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def hero_count(self) -> int:
        return len(self.heroes)


def _text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag) or ""
    value = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", value.strip())
    return value.strip()


def _pub_ts(value: str) -> int:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def html_to_lines(markup: str) -> list[str]:
    """Steam sends HTML inside an XML-escaped <description>; turn it into plain lines.

    Tags are stripped before entities are decoded: the XML parser has already restored the
    HTML tags, so decoding first would turn a literal "&lt;" in the text into "<" and let the
    tag stripper eat part of the line.
    """
    text = markup or ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text)
    return [line for line in (re.sub(r"\s+", " ", raw).strip() for raw in text.splitlines()) if line]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _name_variants(name: str) -> set[str]:
    """Catalogue name variants, so "- Doorman:" matches "The Doorman"."""
    normalized = _norm(name)
    variants = {normalized}
    if normalized.startswith("the") and len(normalized) > 3:
        variants.add(normalized[3:])
    return variants


def _resolve_hero(prefix: str, by_norm: dict[str, str]) -> str | None:
    """Exact catalogue name first, then a unique prefix.

    Steam writes "- Doorman:" and "- Nurse:" where the catalogue says "The Doorman" and
    "Nurse Harrow", so aliases and prefixes are needed. Item names ("Healbane") match no
    hero and fall through, which is what keeps them out of Hero Changes.
    """
    normalized = _norm(prefix)
    if not normalized:
        return None
    if normalized in by_norm:
        return by_norm[normalized]
    if len(normalized) < 4:
        return None
    candidates = list(dict.fromkeys(
        name for hero_norm, name in by_norm.items() if hero_norm.startswith(normalized)))
    return candidates[0] if len(candidates) == 1 else None


def extract_hero_changes(lines: list[str], hero_names=()) -> dict[str, list[str]]:
    by_norm: dict[str, str] = {}
    for name in hero_names:
        for variant in _name_variants(name):
            by_norm.setdefault(variant, name)
    changes: dict[str, list[str]] = {}
    for line in lines:
        match = _HERO_LINE.match(line)
        if not match:
            continue
        hero = _resolve_hero(match.group(1), by_norm)
        if hero:
            changes.setdefault(hero, []).append(match.group(2).strip())
    return changes


def parse_feed(xml_text: str, hero_names=()) -> list[PatchNote]:
    """Newest first, in the order Steam lists them."""
    root = ET.fromstring(xml_text)
    notes: list[PatchNote] = []
    for item in root.iter("item"):
        title = _text(item, "title")
        link = _text(item, "link")
        lines = html_to_lines(_text(item, "description"))
        notes.append(PatchNote(
            guid=_text(item, "guid") or link,
            title=title,
            link=link,
            pub_ts=_pub_ts(_text(item, "pubDate")),
            lines=lines,
            heroes=extract_hero_changes(lines, hero_names),
        ))
    return notes


def is_patch_note(note: PatchNote) -> bool:
    return bool(_UPDATE_TITLE.match(note.title)) or note.hero_count >= MIN_THEMED_HEROES


def preview(note: PatchNote, limit: int = MAX_PREVIEW_LINES) -> tuple[list[str], int]:
    """(lines to quote, how many were left out). Prefers the "- …" change lines."""
    bullets = [line for line in note.lines if line.startswith("- ")]
    source = bullets or note.lines
    shown = source[:limit]
    return shown, max(0, len(source) - len(shown))


def render_patch_content(note: PatchNote, limit: int = MAX_PREVIEW_LINES,
                         max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """The normal Discord message body: bold title, date, hero count and a quoted preview."""
    meta = f"<t:{note.pub_ts}:D>" if note.pub_ts else ""
    if note.hero_count:
        hero_bit = f"{note.hero_count} hero{'es' if note.hero_count != 1 else ''} changed"
        meta = f"{meta} · {hero_bit}" if meta else hero_bit
    shown, remaining = preview(note, limit)
    while True:
        parts = [f"**{note.title}**"]
        if meta:
            parts.append(meta)
        if shown:
            parts.append("")
            parts.extend(f"> {line}" for line in shown)
        if remaining:
            parts.append(f"> … and {remaining} more change{'s' if remaining != 1 else ''}")
        content = "\n".join(parts).strip()
        if len(content) <= max_chars or not shown:
            return content
        shown = shown[:-1]
        remaining += 1


def render_hero_content(note: PatchNote, hero: str,
                        max_chars: int = MAX_MESSAGE_CHARS) -> tuple[str, list[str]]:
    """(first message, follow-up chunks) for one hero's changes, each under `max_chars`."""
    header = f"**{note.title}** — {hero}"
    bullets = [f"> {line}" for line in note.heroes.get(hero, [])]
    whole = "\n".join([header, ""] + bullets).strip()
    if len(whole) <= max_chars:
        return whole, []

    chunks: list[str] = []
    current = ""
    for bullet in bullets:
        candidate = f"{current}\n{bullet}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = bullet
        else:
            current = candidate
    if current:
        chunks.append(current)
    if not chunks:  # a title longer than the limit with no change lines
        return header[:max_chars], []
    return chunks[0], chunks[1:]


def find_new_notes(notes: list[PatchNote], last_guid: str | None,
                   cap: int = 3) -> tuple[list[PatchNote], str | None]:
    """(notes to post oldest-first, new baseline guid).

    `notes` is Steam's newest-first order. Everything above `last_guid` is new. A missing
    baseline (first run) or a guid that fell out of the 10-item window returns no posts and
    the newest guid, so a long outage never floods the channel.
    """
    if not notes:
        return [], last_guid
    newest = notes[0].guid
    if last_guid is None:
        return [], newest
    for index, note in enumerate(notes):
        if note.guid == last_guid:
            new = list(reversed(notes[:index]))
            if len(new) > cap:
                new = new[-cap:]  # keep the newest few, skip the rest
            return new, newest
    return [], newest


async def fetch_feed(session: aiohttp.ClientSession, hero_names=()) -> list[PatchNote]:
    async with session.get(FEED_URL, headers={"User-Agent": USER_AGENT},
                           timeout=aiohttp.ClientTimeout(total=20)) as response:
        response.raise_for_status()
        return parse_feed(await response.text(), hero_names)
