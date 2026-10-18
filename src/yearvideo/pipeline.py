"""Pipeline: month folder -> editable timeline.json (the single source of truth for a month).

The timeline stores every item (kept and dropped, with reasons) plus the analysed songs, so
a reviewer can flip `keep`, set `duration_override`, change the music mode, and call
`replan()` without re-scanning or re-analysing anything.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import yaml

from .dedupe import dedupe_photos
from .ingest import scan_month
from .moments import pick_start, score_video
from .music import analyze_track
from .planner import Item, Limits, Track, plan_month
from .titlecard import month_title

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def load_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or DEFAULT_CONFIG).read_text(encoding="utf-8"))


def limits_from(cfg: dict) -> Limits:
    return Limits(
        photo_min=cfg["photo"]["min_seconds"],
        photo_max=cfg["photo"]["max_seconds"],
        video_min=cfg["video"]["min_seconds"],
        video_max=cfg["video"]["max_seconds"],
    )


def _track_to_dict(t: Track, file: str) -> dict:
    d = asdict(t)
    d["file"] = file
    return d


def build_timeline(month_dir: Path, cfg: dict, mode: str | None = None) -> dict:
    catalog = scan_month(month_dir)
    dd = dedupe_photos(month_dir, catalog)
    items = []
    if cfg.get("title", {}).get("enabled"):
        items.append(
            {
                "id": "__title__",
                "kind": "title",
                "text": month_title(month_dir.name),
                "source_duration": None,
                "keep": True,
                "drop_reason": None,
                "duration_override": cfg["title"]["seconds"],
            }
        )
    for m in catalog:
        dropped = dd.dropped.get(m.id)
        items.append(
            {
                "id": m.id,
                "kind": m.kind,
                "source_duration": m.duration,
                "keep": dropped is None,
                "drop_reason": dropped,
                "duration_override": None,
                "src_in": 0.0,  # videos: where in the source the clip starts
                "src_in_auto": m.kind == "video",  # False once the user sets src_in by hand
                "scores": score_video(month_dir / m.id) if m.kind == "video" else None,
            }
        )
    songs = sorted(p for p in (month_dir / "music").glob("*") if p.suffix.lower() in AUDIO_EXT)
    tracks = [_track_to_dict(analyze_track(p), f"music/{p.name}") for p in songs]
    tl = {
        "month": month_dir.name,
        "output": cfg["output"],
        "fade_seconds": cfg.get("fade_seconds", 0.0),
        "limits": asdict(limits_from(cfg)),
        "music": {"mode": mode or cfg["music"]["mode"], "section": 0, "offset": 0.0},
        "tracks": tracks,
        "items": items,
        "similar_groups": dd.similar_groups,
    }
    return replan(tl)


def replan(tl: dict) -> dict:
    """Recompute durations/audio from the current keep flags, overrides, limits and music mode."""
    items = [
        Item(i["id"], i["kind"], i["source_duration"], i["duration_override"], i["keep"])
        for i in tl["items"]
    ]
    tracks = [
        Track(t["id"], t["music_start"], t["music_end"], t["beats"], [tuple(s) for s in t["sections"]])
        for t in tl["tracks"]
    ]
    by_id = {t["id"]: t for t in tl["tracks"]}
    m = tl["music"]
    plan = plan_month(
        items, tracks, m["mode"], Limits(**tl["limits"]), offset=m["offset"], section=m["section"]
    )
    planned = {p.id: p for p in plan.items}
    for i in tl["items"]:
        p = planned.get(i["id"])
        i["start"], i["duration"] = (p.start, p.duration) if p else (None, None)
        if p and i["kind"] == "video" and i.get("src_in_auto") and i.get("scores"):
            i["src_in"] = pick_start(i["scores"], p.duration, i["source_duration"])
    tl["audio"] = [
        {"file": by_id[a.track_id]["file"], "start": a.start, "end": a.end, "fade_out": a.fade_out}
        for a in plan.audio
    ]
    tl["total"], tl["fits"], tl["note"] = plan.total, plan.fits, plan.note
    tl["next_offset"] = plan.next_offset
    return tl


def save_timeline(tl: dict, path: Path) -> None:
    path.write_text(json.dumps(tl, indent=2), encoding="utf-8")


def load_timeline(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
