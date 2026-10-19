"""Pillow-drawn overlays: colour cards and caption images (so no ffmpeg font setup is needed)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

from .titlecard import _font


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if cur and draw.textlength(trial, font=font) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
    return lines


def make_card_image(color: str, text: str, text_color: str, size: tuple[int, int], path: Path) -> Path:
    """A plain colour card, optionally with centred text."""
    w, h = size
    img = Image.new("RGB", size, ImageColor.getrgb(color))
    if text:
        d = ImageDraw.Draw(img)
        font = _font(h // 8)
        lines = _wrap(d, text, font, w * 0.8)
        line_h = font.size * 1.25
        y = (h - line_h * len(lines)) / 2
        for ln in lines:
            d.text(((w - d.textlength(ln, font=font)) / 2, y), ln, font=font, fill=ImageColor.getrgb(text_color))
            y += line_h
    img.save(path)
    return path


def make_caption_image(
    text: str,
    size: tuple[int, int],
    path: Path,
    *,
    height_pct: float = 5.5,
    color: str = "#ffffff",
    background: str = "#000000",
    background_opacity: float = 0.45,
    position: str = "bottom",
) -> Path:
    """Full-frame transparent PNG with the caption drawn on it, ready to overlay."""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(max(8, int(h * height_pct / 100)))
    lines = _wrap(d, text, font, w * 0.86)
    line_h = font.size * 1.25
    block_h = line_h * len(lines)
    pad = font.size * 0.4
    top = {"top": h * 0.07, "center": (h - block_h) / 2}.get(position, h * 0.93 - block_h)
    widths = [d.textlength(ln, font=font) for ln in lines]
    if background_opacity > 0 and text.strip():
        bw = max(widths) + 2 * pad
        rgb = ImageColor.getrgb(background)
        d.rounded_rectangle(
            ((w - bw) / 2, top - pad, (w + bw) / 2, top + block_h + pad * 0.5),
            radius=pad,
            fill=(*rgb, int(255 * background_opacity)),
        )
    y = top
    for ln, lw in zip(lines, widths, strict=True):
        d.text(((w - lw) / 2, y), ln, font=font, fill=ImageColor.getrgb(color))
        y += line_h
    img.save(path)
    return path
