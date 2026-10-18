"""Timeline planner: turns kept items + music into per-item durations for one month.

Pure logic (no file or ffmpeg access) so it is easy to test with fake data.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field


@dataclass
class Item:
    id: str
    kind: str  # "photo" | "video"
    source_duration: float | None = None  # videos only
    duration_override: float | None = None  # user-fixed length, never rescaled
    keep: bool = True


@dataclass
class Track:
    id: str
    music_start: float  # where the real music begins (after any intro silence)
    music_end: float  # where the real music ends (before trailing quiet)
    beats: list[float] = field(default_factory=list)  # absolute times in the file
    sections: list[tuple[float, float]] = field(default_factory=list)  # e.g. chorus loops


@dataclass
class Limits:
    photo_min: float = 2.0
    photo_max: float = 5.0
    video_min: float = 1.5
    video_max: float = 8.0


@dataclass
class AudioSegment:
    track_id: str
    start: float
    end: float
    fade_out: bool = False


@dataclass
class PlannedItem:
    id: str
    start: float
    duration: float


@dataclass
class MonthPlan:
    items: list[PlannedItem]
    audio: list[AudioSegment]
    total: float
    fits: bool
    note: str = ""
    next_offset: float | None = None  # shared_song: where the next month resumes


def _bounds(item: Item, lim: Limits) -> tuple[float, float]:
    if item.duration_override is not None:
        return item.duration_override, item.duration_override
    if item.kind == "photo":
        return lim.photo_min, lim.photo_max
    hi = lim.video_max
    if item.source_duration is not None:
        hi = min(hi, item.source_duration)
    return min(lim.video_min, hi), hi


def _nearest(sorted_vals: list[float], x: float) -> float:
    i = bisect_left(sorted_vals, x)
    cands = sorted_vals[max(0, i - 1) : i + 1]
    return min(cands, key=lambda v: abs(v - x))


def distribute(bounds: list[tuple[float, float]], budget: float) -> list[float]:
    """Split budget across items, each within its (lo, hi), as evenly (in ratio) as possible."""
    d = [lo for lo, _ in bounds]
    free = [i for i, (lo, hi) in enumerate(bounds) if hi > lo]
    remaining = budget - sum(d)
    while remaining > 1e-9 and free:
        share = remaining / len(free)
        still = []
        for i in free:
            room = bounds[i][1] - d[i]
            add = min(share, room)
            d[i] += add
            remaining -= add
            if bounds[i][1] - d[i] > 1e-9:
                still.append(i)
        free = still
    return d


def _build_audio(
    tracks: list[Track], mode: str, lo: float, hi: float, offset: float, section: int
) -> tuple[list[AudioSegment], float, str]:
    """Pick audio for a month so its length lands in [lo, hi] when possible."""
    segs: list[AudioSegment] = []
    total = 0.0
    note = ""
    first = tracks[0]

    def cut_at_beat(track: Track, start: float, want_end: float) -> float:
        beats = sorted(b for b in track.beats if start < b <= track.music_end)
        return _nearest(beats, want_end) if beats else min(want_end, track.music_end)

    if mode == "fit":
        segs.append(AudioSegment(first.id, first.music_start, first.music_end))
        total = first.music_end - first.music_start
    elif mode == "shared_song":
        start = max(first.music_start, offset)
        end = cut_at_beat(first, start, min(start + hi, first.music_end))
        # prefer the longest span that still fits the month's range
        end = min(end, start + hi)
        segs.append(AudioSegment(first.id, start, end, fade_out=end < first.music_end))
        total = end - start
    elif mode == "loop_cut":
        if first.sections:
            s = first.sections[min(section, len(first.sections) - 1)]
            end = s[1]
        else:
            end = first.music_end
            note = "no sections analysed; using full track"
        segs.append(AudioSegment(first.id, first.music_start, end, fade_out=True))
        total = end - first.music_start
    elif mode == "multi_song":
        pass
    else:
        raise ValueError(f"unknown music mode: {mode}")

    # Top up with further songs if the month is longer than the audio so far.
    pool = tracks if mode == "multi_song" else tracks[1:]
    for t in pool:
        if total >= lo:
            break
        length = t.music_end - t.music_start
        if total + length <= hi:
            segs.append(AudioSegment(t.id, t.music_start, t.music_end))
            total += length
        else:
            want = t.music_start + (hi - total)
            end = cut_at_beat(t, t.music_start, want)
            if total + (end - t.music_start) < lo:
                end = min(want, t.music_end)
            segs.append(AudioSegment(t.id, t.music_start, end, fade_out=True))
            total += end - t.music_start
    return segs, total, note


def plan_month(
    items: list[Item],
    tracks: list[Track],
    mode: str = "fit",
    limits: Limits | None = None,
    offset: float = 0.0,
    section: int = 0,
) -> MonthPlan:
    lim = limits or Limits()
    kept = [i for i in items if i.keep]
    if not kept or not tracks:
        return MonthPlan([], [], 0.0, False, "nothing to plan")

    bounds = [_bounds(i, lim) for i in kept]
    lo, hi = sum(b[0] for b in bounds), sum(b[1] for b in bounds)
    audio, total, note = _build_audio(tracks, mode, lo, hi, offset, section)

    fits = lo - 1e-6 <= total <= hi + 1e-6
    if not fits:
        side = "short" if total < lo else "long"
        note = (
            f"audio {total:.1f}s is too {side} for items ({lo:.1f}-{hi:.1f}s); "
            "choose another music mode, add/remove items, or loosen limits"
        )
        total = min(max(total, lo), hi)  # still produce a best-effort plan to review

    durs = distribute(bounds, total)

    # Snap interior cut points to beats where doing so keeps both neighbours in bounds.
    month_beats: list[float] = []
    pos = 0.0
    for seg in audio:
        t = next(t for t in tracks if t.id == seg.track_id)
        month_beats += [pos + (b - seg.start) for b in t.beats if seg.start <= b <= seg.end]
        pos += seg.end - seg.start
    month_beats.sort()
    if month_beats:
        for k in range(len(durs) - 1):
            cut = sum(durs[: k + 1])
            snapped = _nearest(month_beats, cut)
            delta = snapped - cut
            a, b = bounds[k], bounds[k + 1]
            if (
                a[0] != a[1]
                and b[0] != b[1]
                and a[0] <= durs[k] + delta <= a[1]
                and b[0] <= durs[k + 1] - delta <= b[1]
            ):
                durs[k] += delta
                durs[k + 1] -= delta

    planned, t = [], 0.0
    for item, d in zip(kept, durs, strict=True):
        planned.append(PlannedItem(item.id, round(t, 3), round(d, 3)))
        t += d
    next_off = (audio[-1].end if mode == "shared_song" else None)
    return MonthPlan(planned, audio, round(t, 3), fits, note, next_off)
