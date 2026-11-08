"""Item icons supplied by you: put image files in ./assets/items (png/webp/jpg), subfolders allowed
(folders named "Old ..." and files ending in `_Old` are ignored).

A file is matched to an item by name, ignoring case, spaces, punctuation and filler
words like "icon" / "item" / "v2": `Basic Magazine.png`, `basic_magazine_icon.webp`
and `Item Basic Magazine v2.png` all belong to "Basic Magazine". Some items come in
tiers with one icon per tier (`Trophy Collector` tier II/III): name those
`Trophy Collector T2.png` / `Trophy Collector_T3.png`; the bot tries the tiered name
first, then falls back to the plain name. Local files win over downloaded pictures.
"""
import re
from pathlib import Path

EXTS = {".png", ".webp", ".jpg", ".jpeg"}
NOISE = re.compile(r"\b(icons?|items?|v\d+|deadlock|gameasset|shop|png|webp|psd)\b", re.I)


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", NOISE.sub(" ", name.lower().replace("_", " ")))


def _outdated(p: Path, root: Path) -> bool:
    """Old art: files in a folder named like "Old Icons", or named `Something_Old.png`."""
    parts = p.relative_to(root).parts
    return any(re.search(r"\bold\b", d, re.I) for d in parts[:-1]) or bool(re.search(r"[_ ]old$", p.stem, re.I))


class LocalIcons:
    def __init__(self, folder: str = "assets/items"):
        self.files: dict[str, Path] = {}
        root = Path(folder)
        if root.is_dir():
            for p in sorted(root.rglob("*")):  # subfolders count too
                if p.suffix.lower() not in EXTS or not norm(p.stem) or _outdated(p, root):
                    continue
                self.files.setdefault(norm(p.stem), p)

    def get(self, item_name: str, tier: int | None = None) -> bytes | None:
        keys = [norm(f"{item_name} T{tier}"), norm(f"{item_name} {tier}")] if tier else []
        keys.append(norm(item_name))  # plain name is the fallback even for a tiered item
        for k in keys:
            p = self.files.get(k)
            if p:
                break
        else:
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None
