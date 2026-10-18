"""Title cards (month names) drawn with Pillow so no ffmpeg font setup is needed."""

from __future__ import annotations

import calendar
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuil.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def month_title(folder_name: str) -> str:
    """'01-January' -> 'January', '03' -> 'March', anything else is used as-is."""
    m = re.match(r"^\s*(\d{1,2})[\s_.-]*(.*)$", folder_name)
    if not m:
        return folder_name.replace("_", " ").strip()
    rest = m.group(2).replace("_", " ").strip()
    if rest:
        return rest
    n = int(m.group(1))
    return calendar.month_name[n] if 1 <= n <= 12 else folder_name


def _font(size: int) -> ImageFont.ImageFont:
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def make_title_card(text: str, size: tuple[int, int], path: Path) -> Path:
    w, h = size
    img = Image.new("RGB", size, (14, 16, 22))
    d = ImageDraw.Draw(img)
    font = _font(h // 7)
    box = d.textbbox((0, 0), text, font=font)
    d.text(((w - (box[2] - box[0])) / 2, (h - (box[3] - box[1])) / 2 - box[1]), text, font=font,
           fill=(240, 240, 240))  # fmt: skip
    img.save(path)
    return path
