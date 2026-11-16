"""Whole-year build: plan + render every month folder, then join the months into one video."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .combine import combined_sources, has_media_or_combined
from .ingest import find_tool
from .pipeline import build_timeline, load_config, load_timeline, save_timeline
from .render import render_month


def month_folders(year_dir: Path) -> list[Path]:
    """Subfolders containing media, in name order (01-January, 02-February, ...)."""
    out = []
    for d in sorted(p for p in year_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if has_media_or_combined(d):
            out.append(d)
    return out


def join_months(parts: list[Path], out: Path) -> Path:
    """Concatenate month renders without re-encoding (they share codec, size and fps)."""
    part = out.with_name(out.stem + ".part" + out.suffix)
    with tempfile.TemporaryDirectory() as td:
        listing = Path(td) / "list.txt"
        listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        subprocess.run(
            [find_tool("ffmpeg"), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(part)],
            check=True, capture_output=True,
        )  # fmt: skip
        part.replace(out)
    return out


def build_year(year_dir: Path, out: Path | None = None, config: Path | None = None, progress=None) -> Path:
    """Reviewed months (timeline.json present) are never re-planned, so your edits are safe.

    A month is re-rendered only when its timeline.json is newer than its month.mp4.
    """
    cfg = load_config(config)
    parts = []
    replaced = combined_sources(year_dir)  # months that were combined play inside their combined month
    todo = [d for d in month_folders(year_dir) if d.name not in replaced]
    for i, d in enumerate(todo):
        if progress:
            progress(i, len(todo), d.name, "working")
        tl_path, mp4 = d / "timeline.json", d / "month.mp4"
        if not tl_path.exists():
            save_timeline(build_timeline(d, cfg), tl_path)
        if not mp4.exists() or mp4.stat().st_mtime < tl_path.stat().st_mtime:
            try:
                render_month(load_timeline(tl_path), d, mp4)
            except Exception as e:  # name the month that failed; the other months' videos are kept for next time
                raise RuntimeError(f"{d.name}: {e}") from e
        parts.append(mp4)
    if progress:
        progress(len(todo), len(todo), "", "joining")
    if not parts:
        raise ValueError(f"no month folders with media in {year_dir}")
    return join_months(parts, out or year_dir / "year.mp4")
