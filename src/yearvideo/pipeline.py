"""Pipeline: month folder -> editable timeline.json (the single source of truth for a month).

The timeline stores every item (kept and dropped, with reasons), the analysed songs, captions
and all the month's settings, so a reviewer can change anything and call `replan()` without
re-scanning or re-analysing media.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from copy import deepcopy
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
ANALYSIS_VERSION = "v1"
# top-level config sections copied into every timeline (key in config -> key in timeline)
SETTINGS = {
    "transition": "transition",
    "fade": "fade",
    "sound": "sound",
    "card": "card",
    "captions": "caption_style",
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path | None = None) -> dict:
    cfg = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    if path and Path(path).resolve() != DEFAULT_CONFIG.resolve():
        cfg = _deep_merge(cfg, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    return cfg


def limits_from(cfg: dict) -> Limits:
    return Limits(
        photo_min=cfg["photo"]["min_seconds"],
        photo_max=cfg["photo"]["max_seconds"],
        video_min=cfg["video"]["min_seconds"],
        video_max=cfg["video"]["max_seconds"],
    )


# ---------------------------------------------------------------- songs


def cache_dir() -> Path:
    d = Path(os.environ.get("YEARVIDEO_CACHE") or Path.home() / ".yearvideo") / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    return d


def analyze_cached(path: Path) -> Track:
    """Song analysis takes seconds, so results are cached by file path + modified time."""
    st = path.stat()
    raw = f"{ANALYSIS_VERSION}{path.resolve()}{st.st_mtime_ns}{st.st_size}"
    f = cache_dir() / f"{hashlib.sha1(raw.encode()).hexdigest()}.json"
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        return Track(d["id"], d["music_start"], d["music_end"], d["beats"], [tuple(s) for s in d["sections"]])
    t = analyze_track(path)
    f.write_text(json.dumps(asdict(t)), encoding="utf-8")
    return t


def song_ref(month_dir: Path, path: Path) -> str:
    """How a song is stored in the timeline: relative to the month folder if inside it."""
    p = path.resolve()
    try:
        return p.relative_to(month_dir.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def song_path(month_dir: Path, ref: str) -> Path:
    p = Path(ref)
    return p if p.is_absolute() else month_dir / p


def _analyse_one(month_dir: Path, ref: str) -> dict:
    t = analyze_cached(song_path(month_dir, ref))
    d = asdict(t)
    d["id"], d["file"] = ref, ref  # unique per file, even for same-named songs in different folders
    return d


def list_songs(month_dir: Path, tl: dict) -> list[dict]:
    """Songs you can pick from: the month's own music folder plus the optional library folder."""
    out, seen = [], set()
    roots = [("month", month_dir / "music")]
    lib = (tl.get("music") or {}).get("library")
    if lib and Path(lib).is_dir():
        roots.append(("library", Path(lib)))
    for source, root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in AUDIO_EXT:
                ref = song_ref(month_dir, p)
                if ref not in seen:
                    seen.add(ref)
                    out.append({"file": ref, "name": p.name, "source": source})
    return out


def ensure_track(tl: dict, month_dir: Path, ref: str) -> dict:
    """The analysed track for a song, analysing it first if needed (does not touch the playlist)."""
    for t in tl["tracks"]:
        if t["file"] == ref:
            return t
    t = _analyse_one(month_dir, ref)
    tl["tracks"].append(t)
    return t


def add_song(tl: dict, month_dir: Path, ref: str) -> dict:
    ensure_track(tl, month_dir, ref)
    if ref not in tl["music"]["playlist"]:
        tl["music"]["playlist"].append(ref)
    return replan(tl)


def _compress_lanes(clips: list[dict], floor: int) -> None:
    """Close gaps in lane numbers (lanes 1 and 3 become 1 and 2) so no empty lanes are left behind."""
    used = sorted({max(floor, int(c.get("lane", floor))) for c in clips})
    rank = {lane: floor + n for n, lane in enumerate(used)}
    for c in clips:
        c["lane"] = rank[max(floor, int(c.get("lane", floor)))]


def _layer_defaults(c: dict) -> dict:
    return {"id": uuid.uuid4().hex[:8], "lane": 1, "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0, **c}


def add_layer(tl: dict, month_dir: Path, ref: str, start: float = 0.0) -> dict:
    """Play another song AT THE SAME TIME as the main music (its own lane in the editor)."""
    t = ensure_track(tl, month_dir, ref)
    start = max(0.0, min(start, max(tl.get("total", 0) - 1.0, 0.0)))
    length = min(t["music_end"] - t["music_start"], max(1.0, tl.get("total", 0) - start))
    manual = tl["music"]["mode"] == "manual"
    target = tl["music"].setdefault("clips" if manual else "layers", [])
    lane = 0 if manual else 1
    while any(
        c["lane"] == lane and c["start"] < start + length and start < c["start"] + c["src_out"] - c["src_in"]
        for c in target
    ):
        lane += 1
    target.append(_layer_defaults(
        {"file": ref, "lane": lane, "start": round(start, 3), "src_in": t["music_start"],
         "src_out": round(t["music_start"] + length, 3)}
    ))  # fmt: skip
    return replan(tl)


def to_manual(tl: dict) -> dict:
    """Turn the automatically planned music (and any song layers) into clips you can move/trim by hand.

    The month keeps its current length; change it with `music.manual_length` (None = follow the music).
    """
    tl = replan(tl)
    m = tl["music"]
    m["clips"] = [{k: v for k, v in c.items() if k != "auto"} for c in tl["audio_clips"]]
    m["layers"] = []
    m["manual_length"] = round(tl["total"], 3)
    m["mode"] = "manual"
    return replan(tl)


def remove_song(tl: dict, ref: str) -> dict:
    tl["music"]["playlist"] = [f for f in tl["music"]["playlist"] if f != ref]
    return replan(tl)


# ---------------------------------------------------------------- items


def _base_item(item_id: str, kind: str) -> dict:
    return {
        "id": item_id,
        "kind": kind,
        "taken": None,
        "source_duration": None,
        "keep": True,
        "drop_reason": None,
        "duration_override": None,  # seconds; None = automatic
        "src_in": 0.0,  # videos: where in the source the clip starts
        "src_in_auto": False,  # videos: True = pick the best moment automatically
        "scores": None,
        "has_audio": False,
        "keep_audio": None,  # videos: None = follow sound.keep_video_sound, True/False = force
        "audio_volume": 1.0,  # videos: multiplier on sound.video_volume for this clip
        "fade_in": None,  # seconds to fade in from the fade colour; None = automatic
        "fade_out": None,  # seconds to fade out to the fade colour; None = automatic
        "transition_type": None,  # how this item leads into the next; None = month default
        "transition_out": None,  # seconds; None = month default, 0 = cut
        "transition_align": None,  # start | center | end; None = month default
    }


def _new_item(m, month_dir: Path, reason: str | None) -> dict:
    d = _base_item(m.id, m.kind)
    d.update(
        taken=m.taken,
        source_duration=m.duration,
        keep=reason is None,
        drop_reason=reason,
        has_audio=m.has_audio,
        src_in_auto=m.kind == "video",
        scores=score_video(month_dir / m.id) if m.kind == "video" else None,
    )
    return d


def make_card(tl: dict, text: str = "", color: str | None = None, seconds: float | None = None) -> dict:
    c = tl["card"]
    d = _base_item(f"__card_{uuid.uuid4().hex[:6]}", "card")
    d.update(
        color=color or c["color"],
        text=text,
        text_color=c["text_color"],
        duration_override=seconds if seconds is not None else c["seconds"],
    )
    return d


def insert_card(tl: dict, after_id: str | None = None, **kw) -> dict:
    """Add a blank colour card after the given item (or at the end)."""
    card = make_card(tl, **kw)
    pos = len(tl["items"])
    if after_id is not None:
        pos = next((n + 1 for n, i in enumerate(tl["items"]) if i["id"] == after_id), pos)
    tl["items"].insert(pos, card)
    return replan(tl)


def make_caption(tl: dict, text: str, start: float, end: float, **over) -> dict:
    d = {"id": uuid.uuid4().hex[:8], "text": text, "start": start, "end": end}
    d.update(tl["caption_style"])
    d.update(over)
    return d


# ---------------------------------------------------------------- timeline


def build_timeline(month_dir: Path, cfg: dict, mode: str | None = None, library: str | None = None) -> dict:
    catalog = scan_month(month_dir)
    dd = dedupe_photos(month_dir, catalog)
    tl: dict = {
        "month": month_dir.name,
        "output": cfg["output"],
        "limits": asdict(limits_from(cfg)),
        "beat_sync": cfg.get("beat_sync", "snap"),
        "beat_strength": cfg.get("beat_strength", 0.6),
        "beat_every": cfg.get("beat_every", 1),
        **{dst: deepcopy(cfg[src]) for src, dst in SETTINGS.items()},
        "music": {
            "mode": mode or cfg["music"]["mode"],
            "section": 0,
            "offset": 0.0,
            "playlist": [],
            "layers": [],
            "clips": [],
            "library": library or cfg["music"].get("library"),
        },
        "tracks": [],
        "items": [],
        "captions": [],
        "similar_groups": dd.similar_groups,
    }
    if cfg.get("title", {}).get("enabled"):
        title = make_card(tl, text=month_title(month_dir.name), seconds=cfg["title"]["seconds"])
        title["id"] = "__title__"
        tl["items"].append(title)
    for m in catalog:
        tl["items"].append(_new_item(m, month_dir, dd.dropped.get(m.id)))
    for s in sorted(p for p in (month_dir / "music").glob("*") if p.suffix.lower() in AUDIO_EXT):
        ref = song_ref(month_dir, s)
        tl["tracks"].append(_analyse_one(month_dir, ref))
        tl["music"]["playlist"].append(ref)
    return replan(tl)


def _fill_defaults(tl: dict) -> None:
    """Timelines made by older versions get any settings that did not exist yet."""
    cfg = load_config()
    tl.setdefault("beat_sync", cfg["beat_sync"])
    tl.setdefault("beat_strength", cfg["beat_strength"])
    tl.setdefault("beat_every", cfg["beat_every"])
    for src, dst in SETTINGS.items():
        tl[dst] = {**cfg[src], **tl.get(dst, {})}
    tl.setdefault("captions", [])
    m = tl.setdefault("music", {})
    m.setdefault("mode", cfg["music"]["mode"])
    m.setdefault("section", 0)
    m.setdefault("offset", 0.0)
    m.setdefault("library", cfg["music"].get("library"))
    m.setdefault("playlist", [t["file"] for t in tl.get("tracks", [])])
    m.setdefault("layers", [])
    m.setdefault("manual_length", None)
    m.setdefault("clips", [])
    for c in tl["captions"]:  # captions made before a style option existed get its default
        for k, v in tl["caption_style"].items():
            c.setdefault(k, v)


def replan(tl: dict) -> dict:
    """Recompute durations/audio from the current keep flags, overrides, limits and music mode."""
    _fill_defaults(tl)
    items = [
        Item(i["id"], i["kind"], i["source_duration"], i["duration_override"], i["keep"])
        for i in tl["items"]
    ]
    by_file = {t["file"]: t for t in tl["tracks"]}
    chosen = [by_file[f] for f in tl["music"]["playlist"] if f in by_file]
    tracks = [
        Track(t["id"], t["music_start"], t["music_end"], t["beats"], [tuple(s) for s in t["sections"]])
        for t in chosen
    ]
    by_id = {t["id"]: t for t in chosen}
    m = tl["music"]
    _compress_lanes(m["layers"], 1)
    _compress_lanes(m["clips"], 0)
    manual = m["mode"] == "manual"
    override = None
    if manual:
        clips0 = [_layer_defaults({**c, "lane": c.get("lane", 0)}) for c in m["clips"]]
        main = [c for c in clips0 if c["lane"] == 0] or clips0  # beats come from the main lane
        beats: list[float] = []
        for c in main:
            t = by_file.get(c["file"])
            if t:
                beats += [c["start"] + b - c["src_in"] for b in t["beats"] if c["src_in"] <= b <= c["src_out"]]
        if clips0:
            end = max(c["start"] + c["src_out"] - c["src_in"] for c in clips0)
            override = (m.get("manual_length") or end, sorted(beats))
    plan = plan_month(
        items,
        tracks,
        "fit" if manual else m["mode"],
        Limits(**tl["limits"]),
        offset=m["offset"],
        section=m["section"],
        beat_sync=tl["beat_sync"],
        beat_strength=tl["beat_strength"],
        beat_every=tl["beat_every"],
        audio_override=override,
    ) if not (manual and override is None) else plan_month(items, [], "fit", Limits(**tl["limits"]))
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
    fade = tl["fade"]
    clips: list[dict] = []
    if manual:
        clips += [_layer_defaults({**c, "lane": c.get("lane", 0)}) for c in m["clips"]]
    else:
        pos = 0.0
        for n, a in enumerate(plan.audio):
            clips.append({
                "id": f"auto{n}", "file": by_id[a.track_id]["file"], "lane": 0, "start": round(pos, 3),
                "src_in": a.start, "src_out": a.end, "volume": 1.0,
                "fade_in": fade["music_in"] if n == 0 else 0.0,
                "fade_out": fade["music_out"] if a.fade_out else 0.0, "auto": True,
            })  # fmt: skip
            pos += a.end - a.start
    for c in m["layers"]:
        clips.append(_layer_defaults({**c, "lane": max(1, c.get("lane", 1))}))
    tl["audio_clips"] = clips
    tl["total"], tl["fits"], tl["note"] = plan.total, plan.fits, plan.note
    tl["next_offset"] = plan.next_offset
    return tl


def save_timeline(tl: dict, path: Path) -> None:
    """Write the timeline, keeping the previous version next to it as timeline.json.bak."""
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(tl, indent=2), encoding="utf-8")


def load_timeline(path: Path) -> dict:
    tl = json.loads(path.read_text(encoding="utf-8"))
    _fill_defaults(tl)
    return tl


def refresh_timeline(tl: dict, month_dir: Path) -> dict:
    """Pick up files added to / removed from the month folder, keeping all review decisions."""
    catalog = scan_month(month_dir)
    on_disk = {m.id for m in catalog}
    dd = dedupe_photos(month_dir, catalog)
    known = {i["id"] for i in tl["items"]}
    tl["items"] = [i for i in tl["items"] if i["kind"] == "card" or i["id"] in on_disk]
    for m in sorted(catalog, key=lambda m: (m.taken, m.id)):
        if m.id in known:
            continue
        item = _new_item(m, month_dir, dd.dropped.get(m.id))
        pos = next(
            (n for n, i in enumerate(tl["items"]) if i.get("taken") and i["taken"] > m.taken),
            len(tl["items"]),
        )
        tl["items"].insert(pos, item)
    tl["similar_groups"] = dd.similar_groups
    have = {t["file"] for t in tl["tracks"]}
    for s in sorted(p for p in (month_dir / "music").glob("*") if p.suffix.lower() in AUDIO_EXT):
        ref = song_ref(month_dir, s)
        if ref not in have:
            tl["tracks"].append(_analyse_one(month_dir, ref))
            tl["music"]["playlist"].append(ref)
    return replan(tl)
