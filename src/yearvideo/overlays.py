"""Pillow-drawn overlays: colour cards and caption images (so no ffmpeg font setup is needed)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter

from .fonts import load_font

LINE_HEIGHT = 1.25  # in text heights; the editor's live preview uses the same value

CAPTION_DEFAULTS = {
    "font": "", "size": 5.5, "color": "#ffffff", "align": "center", "x": 0.5, "y": 0.9, "rotation": 0.0,
    "wrap_width": 86.0, "background": "#000000", "background_opacity": 0.0, "box_padding": 0.4,
    "box_radius": 0.4, "outline_color": "#000000", "outline_width": 0.0, "shadow": False,
}  # fmt: skip
_POSITION_Y = {"top": 0.08, "center": 0.5, "bottom": 0.9}  # legacy captions with only `position`


def caption_style(c: dict) -> dict:
    """A caption's settings with every missing field filled from the defaults."""
    s = {**CAPTION_DEFAULTS, **{k: v for k, v in c.items() if v is not None}}
    if "y" not in c and "position" in c:
        s["y"] = _POSITION_Y.get(c["position"], s["y"])
    return s


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
        font = load_font(None, h // 8)
        lines = _wrap(d, text, font, w * 0.8)
        line_h = font.size * 1.25
        y = (h - line_h * len(lines)) / 2
        for ln in lines:
            d.text(((w - d.textlength(ln, font=font)) / 2, y), ln, font=font, fill=ImageColor.getrgb(text_color))
            y += line_h
    img.save(path)
    return path


def render_caption_block(c: dict, frame: tuple[int, int]) -> Image.Image:
    """The caption (box, text, outline, shadow) on a transparent image, before rotation."""
    s = caption_style(c)
    w, h = frame
    px = max(8, round(h * float(s["size"]) / 100))
    font = load_font(s["font"], px)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    pad_x = px * float(s["box_padding"])
    lines = _wrap(probe, c.get("text", ""), font, w * float(s["wrap_width"]) / 100 - 2 * pad_x)
    widths = [probe.textlength(ln, font=font) for ln in lines]
    line_h = px * LINE_HEIGHT
    text_w, text_h = max(widths + [1.0]), line_h * len(lines)
    pad_y = pad_x * 0.75
    stroke = round(px * float(s["outline_width"]) / 100)
    margin = max(stroke, int(px * 0.3)) + 2  # room for shadow / outline beyond the box
    bw, bh = text_w + 2 * pad_x, text_h + 2 * pad_y
    img = Image.new("RGBA", (int(bw + 2 * margin) + 1, int(bh + 2 * margin) + 1), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if float(s["background_opacity"]) > 0 and c.get("text", "").strip():
        rgb = ImageColor.getrgb(s["background"])
        d.rounded_rectangle((margin, margin, margin + bw, margin + bh), radius=px * float(s["box_radius"]),
                            fill=(*rgb, int(255 * float(s["background_opacity"]))))  # fmt: skip
    asc, desc = font.getmetrics()
    base_off = (line_h - (asc + desc)) / 2 + asc  # baseline inside a line box, like CSS line-height

    def draw_lines(layer: ImageDraw.ImageDraw, dx: float, dy: float, fill, **kw):
        y = margin + pad_y
        for ln, lw in zip(lines, widths, strict=True):
            x = {"left": margin + pad_x, "right": margin + bw - pad_x - lw}.get(s["align"], margin + (bw - lw) / 2)
            layer.text((x + dx, y + base_off + dy), ln, font=font, fill=fill, anchor="ls", **kw)
            y += line_h

    if s["shadow"]:
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw_lines(ImageDraw.Draw(sh), px * 0.06, px * 0.08, (0, 0, 0, 200))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(px * 0.06)))
        d = ImageDraw.Draw(img)
    kw = {"stroke_width": stroke, "stroke_fill": ImageColor.getrgb(s["outline_color"])} if stroke else {}
    draw_lines(d, 0, 0, ImageColor.getrgb(s["color"]), **kw)
    return img


def make_caption_image(c: dict, size: tuple[int, int], path: Path) -> Path:
    """Full-frame transparent PNG with the caption placed, rotated and drawn, ready to overlay."""
    s = caption_style(c)
    w, h = size
    block = render_caption_block(c, size)
    if float(s["rotation"]):
        block = block.rotate(-float(s["rotation"]), expand=True, resample=Image.BICUBIC)  # clockwise
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.alpha_composite(
        block.crop((max(0, -round(float(s["x"]) * w - block.width / 2)),
                    max(0, -round(float(s["y"]) * h - block.height / 2)), block.width, block.height)),
        dest=(max(0, round(float(s["x"]) * w - block.width / 2)), max(0, round(float(s["y"]) * h - block.height / 2))),
    )  # fmt: skip
    canvas.save(path)
    return path
