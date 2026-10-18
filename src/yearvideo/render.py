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
            fade = min(tl.get("fade_seconds", 0.0), it["duration"] / 2)
            if fade and n == 0:
                vf += f",fade=t=in:st=0:d={fade:.3f}"
            if fade and n == len(items) - 1:
                vf += f",fade=t=out:st={it['duration'] - fade:.3f}:d={fade:.3f}"
            _run([ff, "-y", "-v", "error", *inp, "-vf", vf, "-an", *enc, str(seg)])
            segs.append(seg)

        listing = work / "list.txt"
        listing.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
        video = work / "video.mp4"
        _run([ff, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-c", "copy", str(video)])  # fmt: skip

        inputs, chains = ["-i", str(video)], []
        for k, a in enumerate(tl["audio"], start=1):
            dur = a["end"] - a["start"]
            inputs += ["-ss", f"{a['start']:.3f}", "-t", f"{dur:.3f}", "-i", str(month_dir / a["file"])]
            fade = min(3.0, dur / 2)
            f = f"afade=t=out:st={dur - fade:.3f}:d={fade:.3f}," if a["fade_out"] else ""
            chains.append(f"[{k}:a]{f}asetpts=PTS-STARTPTS[a{k}]")
        n = len(tl["audio"])
        labels = "".join(f"[a{k}]" for k in range(1, n + 1))
        graph = ";".join([*chains, f"{labels}concat=n={n}:v=0:a=1[aout]"])
        out.parent.mkdir(parents=True, exist_ok=True)
        _run([ff, "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[aout]",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(out)])  # fmt: skip
    return out
