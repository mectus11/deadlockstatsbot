"""Item icons supplied by you: put image files in ./assets/items (png/webp/jpg), subfolders allowed
(folders named "Old ..." and files ending in `_Old` are ignored).

A file is matched to an item by name, ignoring case, spaces, punctuation and filler
words like "icon" / "item" / "v2": `Basic Magazine.png`, `basic_magazine_icon.webp`
and `Item Basic Magazine v2.png` all belong to "Basic Magazine". Some items come in
tiers with one icon per tier (`Trophy Collector` tier II/III): name those
`Trophy Collector T2.png` / `Trophy Collector_T3.png`; the bot tries the tiered name
first, then falls back to the plain name. Local files win over downloaded pictures.
"""
import logging
import re
from pathlib import Path

log = logging.getLogger("local_icons")
EXTS = {".png", ".webp", ".jpg", ".jpeg"}
NOISE = re.compile(r"\b(icons?|items?|v\d+|deadlock|gameasset|shop|png|webp|psd)\b", re.I)


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", NOISE.sub(" ", name.lower().replace("_", " ")))


def _outdated(p: Path, root: Path) -> bool:
    """Old art: files in a folder named like "Old Icons", or named `Something_Old.png`."""
    parts = p.relative_to(root).parts
    return any(re.search(r"\bold\b", d, re.I) for d in parts[:-1]) or bool(re.search(r"[_ ]old$", p.stem, re.I))


def _resolve_case_insensitive(folder: str) -> Path | None:
    """Finds `folder` even if its case doesn't match the real directory on disk. Windows
    filesystems are case-insensitive so a mismatch like assets/items vs assets/ITEMS is
    invisible there, but Linux (e.g. inside Docker) is case-sensitive and would silently
    find nothing — so every path segment is matched against the real directory listing."""
    path = Path(folder)
    if path.is_dir():
        return path
    cur = Path(path.anchor or ".")
    for part in path.parts[1:] if path.anchor else path.parts:
        if not cur.is_dir():
            return None
        match = next((e for e in cur.iterdir() if e.name.lower() == part.lower()), None)
        if match is None:
            return None
        cur = match
    return cur if cur.is_dir() else None


class LocalIcons:
    def __init__(self, folder: str = "assets/items"):
        self.files: dict[str, Path] = {}
        root = _resolve_case_insensitive(folder)
        if root is None:
            return
        if root.name != Path(folder).name:
            log.info("%r not found as-is; using %r instead (case differs — fine on Windows, "
                     "but matters on Linux/Docker)", folder, str(root))
        for p in sorted(root.rglob("*")):  # subfolders count too
            if p.suffix.lower() not in EXTS or not norm(p.stem) or _outdated(p, root):
                continue
            self.files.setdefault(norm(p.stem), p)

    def match_key(self, item_name: str, tier: int | None = None) -> str | None:
        """Which key in self.files actually matches this item, if any (the tiered key, when
        present, otherwise the plain one) — used by get(), and by callers checking coverage."""
        keys = [norm(f"{item_name} T{tier}"), norm(f"{item_name} {tier}")] if tier else []
        keys.append(norm(item_name))  # plain name is the fallback even for a tiered item
        return next((k for k in keys if k in self.files), None)

    def get(self, item_name: str, tier: int | None = None) -> bytes | None:
        key = self.match_key(item_name, tier)
        if key is None:
            return None
        try:
            return self.files[key].read_bytes()
        except OSError:
            return None
