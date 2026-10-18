"""Ingest: scan a month folder into a catalog of media items with metadata."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}


def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe: PATH first, then the winget install location on Windows."""
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        hits = sorted(Path(local).glob(f"Microsoft/WinGet/Packages/Gyan.FFmpeg*/*/bin/{name}.exe"))
        if hits:
            return str(hits[-1])
    raise FileNotFoundError(f"{name} not found; install ffmpeg")


@dataclass
class MediaItem:
    id: str  # path relative to the month folder
    kind: str  # photo | video
    width: int
    height: int
    taken: str  # ISO timestamp (EXIF/creation time, else file mtime)
    size_bytes: int
    duration: float | None = None  # videos only


def _photo_info(path: Path) -> tuple[int, int, str | None]:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)  # phone photos store rotation in EXIF
        w, h = im.size
        raw = im.getexif().get_ifd(ExifTags.IFD.Exif).get(ExifTags.Base.DateTimeOriginal)
    taken = None
    if raw:
        try:
            taken = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    return w, h, taken


def _video_info(path: Path) -> tuple[int, int, float, str | None]:
    out = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:stream_side_data=rotation:format=duration:format_tags=creation_time",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    data = json.loads(out.stdout)
    st = data["streams"][0]
    w, h = st["width"], st["height"]
    rot = next((abs(int(s["rotation"])) for s in st.get("side_data_list", []) if "rotation" in s), 0)
    if rot in (90, 270):
        w, h = h, w
    created = data.get("format", {}).get("tags", {}).get("creation_time")
    return w, h, float(data["format"]["duration"]), created


def scan_month(root: Path) -> list[MediaItem]:
    items: list[MediaItem] = []
    for p in sorted(root.rglob("*")):
        ext = p.suffix.lower()
        if p.parent.name == "music" or not p.is_file():
            continue
        mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
        rel = p.relative_to(root).as_posix()
        if ext in PHOTO_EXT:
            w, h, taken = _photo_info(p)
            items.append(MediaItem(rel, "photo", w, h, taken or mtime, p.stat().st_size))
        elif ext in VIDEO_EXT:
            w, h, dur, created = _video_info(p)
            items.append(MediaItem(rel, "video", w, h, created or mtime, p.stat().st_size, dur))
    items.sort(key=lambda i: (i.taken, i.id))  # chronological story order
    return items


def write_catalog(items: list[MediaItem], path: Path) -> None:
    path.write_text(json.dumps([asdict(i) for i in items], indent=2), encoding="utf-8")
