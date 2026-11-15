"""Combined months: two or more months edited as ONE month that replaces them in the year video.

The files stay in their original month folders. A combined month is its own folder holding only a
timeline.json whose photos/clips point at the originals as "@<month folder>/<path inside it>".
"""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

from .ingest import PHOTO_EXT, VIDEO_EXT
from .pipeline import (
    build_timeline,
    item_src,
    load_config,
    load_timeline,
    replan,
    save_timeline,
    song_path,
)

CACHE_NAMES = {"timeline.json", "timeline.json.bak", "month.mp4", "month_preview.mp4", ".thumbs", ".proxy"}


def resolve_src(month_dir: Path, rel: str) -> Path:
    """The file for an item: inside the month folder, or "@month/path" in a sibling month folder."""
    if rel.startswith("@"):
        return month_dir.parent / rel[1:]
    return month_dir / rel


def _read(d: Path) -> dict:
    p = d / "timeline.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def is_combined(d: Path) -> bool:
    return bool(_read(d).get("combined_from"))


def combined_sources(year_dir: Path) -> dict[str, str]:
    """{source month name: the combined month that replaced it}."""
    out = {}
    for d in sorted(p for p in year_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for m in _read(d).get("combined_from") or []:
            out[m] = d.name
    return out


def combined_name(names: list[str]) -> str:
    rest = [n.split("-", 1)[1] if n[:2].isdigit() and "-" in n else n for n in names[1:]]
    return "+".join([names[0], *rest])


def combine(year_dir: Path, names: list[str]) -> Path:
    """Create the combined month folder (never touches the source months' files)."""
    names = sorted(dict.fromkeys(names))
    if len(names) < 2:
        raise ValueError("choose at least two months to combine")
    taken = combined_sources(year_dir)
    for n in names:
        if not (year_dir / n).is_dir():
            raise ValueError(f"unknown month: {n}")
        if n in taken:
            raise ValueError(f"{n} is already part of {taken[n]}")
        if is_combined(year_dir / n):
            raise ValueError(f"{n} is itself a combined month; split it first")
    dest = year_dir / combined_name(names)
    if dest.exists():
        raise ValueError(f"{dest.name} already exists")
    cfg = load_config()
    items, captions, tracks, playlist, t0, base = [], [], [], [], 0.0, None
    for k, n in enumerate(names):
        d = year_dir / n
        tl_path = d / "timeline.json"
        if not tl_path.exists():
            save_timeline(build_timeline(d, cfg), tl_path)
        tl = replan(load_timeline(tl_path))
        base = base or tl
        for it in tl["items"]:
            it = deepcopy(it)
            it["from_month"] = n
            if it["kind"] == "card":
                it["id"] = f"m{k}_{it['id']}"
            else:
                it["id"], it["src"] = f"@{n}/{it['id']}", f"@{n}/{item_src(it)}"
            items.append(it)
        for c in tl["captions"]:
            c = deepcopy(c)
            c["start"], c["end"] = c["start"] + t0, c["end"] + t0
            captions.append(c)
        for t in tl["tracks"]:
            ref = song_path(d, t["file"]).resolve().as_posix()
            if all(x["file"] != ref for x in tracks):
                tracks.append({**deepcopy(t), "id": ref, "file": ref})
                playlist.append(ref)
        t0 += tl.get("total") or 0.0
    out = deepcopy(base)
    out.update(month=dest.name, combined_from=names, items=items, captions=captions, tracks=tracks,
               borrowed=[], similar_groups=[])  # fmt: skip
    out["music"] = {**out["music"], "mode": "multi_song" if tracks else out["music"]["mode"], "section": 0,
                    "offset": 0.0, "playlist": playlist, "layers": [], "clips": []}  # fmt: skip
    dest.mkdir()
    save_timeline(replan(out), dest / "timeline.json")
    return dest


def uncombine(year_dir: Path, name: str) -> list[str]:
    """Remove a combined month (its sources come back as separate months). Only cache files may be in it."""
    d = year_dir / name
    if not is_combined(d):
        raise ValueError(f"{name} is not a combined month")
    extra = [p.name for p in d.iterdir() if p.name not in CACHE_NAMES]
    if extra:
        raise ValueError(f"{name} holds other files ({', '.join(extra[:3])}); not removing it")
    sources = _read(d)["combined_from"]
    shutil.rmtree(d)
    return sources


def has_media_or_combined(d: Path) -> bool:
    return is_combined(d) or any(f.suffix.lower() in PHOTO_EXT | VIDEO_EXT for f in d.rglob("*") if f.is_file())
