"""Render a month's timeline.json to an mp4 with ffmpeg.

Each item is rendered to a uniform segment, segments are joined with the concat demuxer
(no re-encode), then the music is mixed in. Segments make renders resumable and cacheable.
"""

from __future__ import annotations

import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .ingest import find_tool
from .titlecard import make_title_card

ENCODERS = {
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"],
}


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


@lru_cache(maxsize=1)
def pick_encoder() -> str:
    """Use the GPU encoder when a real test encode works, otherwise fall back to x264."""
    try:
        _run([find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
              "-c:v", "h264_nvenc", "-f", "null", "-"])  # fmt: skip
        return "h264_nvenc"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "libx264"


def _upright(src: Path, work: Path) -> Path:
    """Apply EXIF rotation (phone photos) by writing a rotated copy when needed."""
    with Image.open(src) as im:
        if im.getexif().get(274, 1) == 1:
            return src
        out = work / f"upright_{src.stem}.jpg"
        from PIL import ImageOps

        ImageOps.exif_transpose(im).convert("RGB").save(out, quality=95)
        return out


def _fit(w: int, h: int) -> str:
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    )


def _item_fades(tl: dict, it: dict, n: int, count: int) -> tuple[float, float]:
    """Fade-in/out seconds for one item: its own setting, else the month's start/end fade."""
    fade = tl["fade"]
    f_in = it.get("fade_in")
    f_out = it.get("fade_out")
    if f_in is None:
        f_in = fade["video_in"] if n == 0 else 0.0
    if f_out is None:
        f_out = fade["video_out"] if n == count - 1 else 0.0
    return f_in, f_out


def wants_clip_sound(tl: dict, it: dict) -> bool:
    if it["kind"] != "video" or not it.get("has_audio"):
        return False
    forced = it.get("keep_audio")
    return tl["video_sound"]["keep_default"] if forced is None else bool(forced)


def audio_graph(tl: dict, month_dir: Path, items: list[dict], video: Path) -> tuple[list[str], str]:
    """ffmpeg inputs + filter graph: the music (ducked under clip sound) mixed with clip sounds."""
    fade, vs = tl["fade"], tl["video_sound"]
    inputs, chains = ["-i", str(video)], []
    songs = tl["audio"]
    for k, a in enumerate(songs, start=1):
        dur = a["end"] - a["start"]
        inputs += ["-ss", f"{a['start']:.3f}", "-t", f"{dur:.3f}", "-i", str(month_dir / a["file"])]
        f = ""
        if a["fade_out"] and fade["music_out"] > 0:
            d = min(fade["music_out"], dur / 2)
            f += f"afade=t=out:st={dur - d:.3f}:d={d:.3f},"
        if k == 1 and fade["music_in"] > 0:
            f += f"afade=t=in:st=0:d={min(fade['music_in'], dur / 2):.3f},"
        chains.append(f"[{k}:a]{f}asetpts=PTS-STARTPTS[a{k}]")
    labels = "".join(f"[a{k}]" for k in range(1, len(songs) + 1))
    music = f"{labels}concat=n={len(songs)}:v=0:a=1"

    clips = [it for it in items if wants_clip_sound(tl, it)]
    ramp = max(vs["ramp"], 0.01)
    for it in clips:  # dip the music while this clip's own sound plays
        s, e = it["start"], it["start"] + it["duration"]
        music += f",volume='1-(1-{vs['duck']})*clip(min(t-{s:.3f},{e:.3f}-t)/{ramp},0,1)':eval=frame"
    chains.append(f"{music}[mus]")

    mix = ["[mus]"]
    for j, it in enumerate(clips):
        idx = len(songs) + 1 + j
        inputs += ["-ss", f"{it['src_in']:.3f}", "-t", f"{it['duration']:.3f}", "-i", str(month_dir / it["id"])]
        ms = round(it["start"] * 1000)
        chains.append(
            f"[{idx}:a]aformat=channel_layouts=stereo,volume={it.get('audio_volume', 1.0)},"
            f"adelay={ms}:all=1[c{j}]"
        )
        mix.append(f"[c{j}]")
    if clips:
        chains.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=longest[aout]")
    else:
        chains.append("[mus]anull[aout]")
    return inputs, ";".join(chains)


def render_month(tl: dict, month_dir: Path, out: Path, encoder: str | None = None) -> Path:
    ff = find_tool("ffmpeg")
    w, h = tl["output"]["resolution"]
    fps = tl["output"]["fps"]
    enc = ENCODERS[encoder or pick_encoder()] + ["-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2)]
    items = [i for i in tl["items"] if i["keep"] and i["duration"]]
    if not items:
        raise ValueError("timeline has no kept items")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        segs = []
        for n, it in enumerate(items):
            src = month_dir / it["id"]
            seg = work / f"seg_{n:04d}.mp4"
            if it["kind"] in ("photo", "title"):
                if it["kind"] == "title":
                    src = make_title_card(it["text"], (w, h), work / f"title_{n}.png")
                else:
                    src = _upright(src, work)
                inp = ["-loop", "1", "-framerate", str(fps), "-t", f"{it['duration']:.3f}", "-i", str(src)]
            else:
                inp = ["-ss", f"{it['src_in']:.3f}", "-t", f"{it['duration']:.3f}", "-i", str(src)]
            vf = _fit(w, h)
            dur = it["duration"]
            f_in, f_out = _item_fades(tl, it, n, len(items))
            f_in, f_out = min(f_in, dur), min(f_out, dur)
            color = tl["fade"]["color"]
            if f_in:
                vf += f",fade=t=in:st=0:d={f_in:.3f}:color={color}"
            if f_out:
                vf += f",fade=t=out:st={dur - f_out:.3f}:d={f_out:.3f}:color={color}"
            _run([ff, "-y", "-v", "error", *inp, "-vf", vf, "-an", *enc, str(seg)])
            segs.append(seg)

        listing = work / "list.txt"
        listing.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
        video = work / "video.mp4"
        _run([ff, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-c", "copy", str(video)])  # fmt: skip

        out.parent.mkdir(parents=True, exist_ok=True)
        inputs, graph = audio_graph(tl, month_dir, items, video)
        _run([ff, "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[aout]",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(out)])  # fmt: skip
    return out
