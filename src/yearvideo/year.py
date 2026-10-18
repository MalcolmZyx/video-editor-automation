"""Whole-year build: plan + render every month folder, then join the months into one video."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .ingest import PHOTO_EXT, VIDEO_EXT, find_tool
from .pipeline import build_timeline, load_config, load_timeline, save_timeline
from .render import render_month


def month_folders(year_dir: Path) -> list[Path]:
    """Subfolders containing media, in name order (01-January, 02-February, ...)."""
    out = []
    for d in sorted(p for p in year_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if any(f.suffix.lower() in PHOTO_EXT | VIDEO_EXT for f in d.rglob("*") if f.is_file()):
            out.append(d)
    return out


def join_months(parts: list[Path], out: Path) -> Path:
    """Concatenate month renders without re-encoding (they share codec, size and fps)."""
    with tempfile.TemporaryDirectory() as td:
        listing = Path(td) / "list.txt"
        listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        subprocess.run(
            [find_tool("ffmpeg"), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(out)],
            check=True, capture_output=True,
        )  # fmt: skip
    return out


def build_year(year_dir: Path, out: Path | None = None, config: Path | None = None) -> Path:
    """Reviewed months (timeline.json present) are never re-planned, so your edits are safe.

    A month is re-rendered only when its timeline.json is newer than its month.mp4.
    """
    cfg = load_config(config)
    parts = []
    for d in month_folders(year_dir):
        tl_path, mp4 = d / "timeline.json", d / "month.mp4"
        if not tl_path.exists():
            save_timeline(build_timeline(d, cfg), tl_path)
        if not mp4.exists() or mp4.stat().st_mtime < tl_path.stat().st_mtime:
            render_month(load_timeline(tl_path), d, mp4)
        parts.append(mp4)
    if not parts:
        raise ValueError(f"no month folders with media in {year_dir}")
    return join_months(parts, out or year_dir / "year.mp4")
