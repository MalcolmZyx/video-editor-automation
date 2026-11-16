"""Ingest: scan a month folder into a catalog of media items with metadata."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image

from .cache import FileCache

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}


OWN_OUTPUTS = {"month.mp4", "month_preview.mp4", "month.part.mp4", "month_preview.part.mp4", "year.mp4", "year.part.mp4"}


def is_own_output(rel_parts: tuple[str, ...]) -> bool:
    """The editor's own renders sitting in a folder (never treat them as source clips)."""
    return len(rel_parts) == 1 and rel_parts[0].lower() in OWN_OUTPUTS


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
    has_audio: bool = False  # videos only


def _photo_info(path: Path) -> tuple[int, int, str | None]:
    with Image.open(path) as im:  # header only: the picture itself is never decoded here
        w, h = im.size
        ex = im.getexif()
        if ex.get(0x0112) in (5, 6, 7, 8):  # phone photos store rotation in EXIF; those swap width/height
            w, h = h, w
        raw = ex.get_ifd(ExifTags.IFD.Exif).get(ExifTags.Base.DateTimeOriginal)
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


def _has_audio(path: Path) -> bool:
    out = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return bool(out.stdout.strip())


def _read_one(p: Path, rel: str, ext: str, cache: FileCache) -> MediaItem:
    """Everything we need to know about one file; looked up in the cache first, asked of the file if new."""
    info = cache.get(rel, p, "info")
    if info is None:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
        if ext in PHOTO_EXT:
            w, h, taken = _photo_info(p)
            info = {"kind": "photo", "w": w, "h": h, "taken": taken or mtime, "dur": None, "audio": False}
        else:
            w, h, dur, created = _video_info(p)
            info = {"kind": "video", "w": w, "h": h, "taken": created or mtime, "dur": dur, "audio": _has_audio(p)}
        cache.put(rel, p, "info", info)
    return MediaItem(rel, info["kind"], info["w"], info["h"], info["taken"], p.stat().st_size, info["dur"], info["audio"])


def scan_month(root: Path, cache: FileCache | None = None, skipped: list[dict] | None = None) -> list[MediaItem]:
    """Catalog of the folder. Unreadable files are left out and listed in `skipped` instead of aborting."""
    cache = cache or FileCache(root)
    todo: list[tuple[Path, str, str]] = []
    for p in sorted(root.rglob("*")):
        ext = p.suffix.lower()
        rel_parts = p.relative_to(root).parts
        if p.parent.name == "music" or not p.is_file() or any(x.startswith(".") for x in rel_parts) or is_own_output(rel_parts):
            continue  # songs, and the editor's own hidden caches (.thumbs, .proxy)
        if ext in PHOTO_EXT or ext in VIDEO_EXT:
            todo.append((p, p.relative_to(root).as_posix(), ext))

    def one(args):
        try:
            return _read_one(*args, cache)
        except Exception as e:  # noqa: BLE001 - a damaged file must not stop the whole scan
            return {"id": args[1], "error": f"{type(e).__name__}: {e}"[:200]}

    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
        results = list(pool.map(one, todo))
    items = [r for r in results if isinstance(r, MediaItem)]
    if skipped is not None:
        skipped.extend(r for r in results if isinstance(r, dict))
    cache.forget_missing({rel for _, rel, _ in todo})
    cache.save()
    items.sort(key=lambda i: (i.taken, i.id))  # chronological story order
    return items


def write_catalog(items: list[MediaItem], path: Path) -> None:
    path.write_text(json.dumps([asdict(i) for i in items], indent=2), encoding="utf-8")
