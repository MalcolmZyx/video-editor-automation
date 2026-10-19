"""Find the fonts installed on this computer so captions can use any of them.

Only .ttf / .otf files are listed (the browser preview and the renderer must both load the very
same file, and .ttc collections can't be used by browsers). Drop extra font files into the
project's `fonts/` folder, or point YEARVIDEO_FONTS at other folders, to make them available.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_EXT = {".ttf", ".otf"}
PROJECT_FONTS = Path(__file__).resolve().parents[2] / "fonts"
PREFERRED_DEFAULTS = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttf"]


def font_dirs() -> list[Path]:
    dirs = [PROJECT_FONTS]
    dirs += [Path(p) for p in os.environ.get("YEARVIDEO_FONTS", "").split(os.pathsep) if p]
    win = os.environ.get("WINDIR")
    if win:
        dirs.append(Path(win) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    home = Path.home()
    dirs += [
        Path("/Library/Fonts"), Path("/System/Library/Fonts"), home / "Library" / "Fonts",
        Path("/usr/share/fonts"), home / ".fonts", home / ".local" / "share" / "fonts",
    ]  # fmt: skip
    return [d for d in dirs if d.is_dir()]


@lru_cache(maxsize=1)
def list_fonts() -> list[dict]:
    """[{file, family, style}] for every usable font, sorted by family; `file` is the file name."""
    seen: dict[str, dict] = {}
    for d in font_dirs():
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() not in FONT_EXT or p.name in seen:
                continue
            try:
                family, style = ImageFont.truetype(str(p), 20).getname()
            except OSError:
                continue  # corrupt / unsupported font file
            seen[p.name] = {"file": p.name, "family": family, "style": style, "path": str(p)}
    return sorted(seen.values(), key=lambda f: (f["family"].lower(), f["style"].lower()))


def resolve_font(name: str | None) -> Path | None:
    """Path of a listed font by file name (never an arbitrary path), or None."""
    if not name:
        return None
    for f in list_fonts():
        if f["file"] == name:
            return Path(f["path"])
    return None


def default_font() -> Path | None:
    for want in PREFERRED_DEFAULTS:
        p = resolve_font(want)
        if p:
            return p
    fonts = list_fonts()
    return Path(fonts[0]["path"]) if fonts else None


def load_font(name: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    p = resolve_font(name) or default_font()
    if p:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)
