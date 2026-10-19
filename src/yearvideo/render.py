"""Render a month's timeline.json to an mp4 with ffmpeg.

Every item becomes a small uniform clip. Where two items are joined by a cross-fade, the
overlap is rendered as its own short transition clip (the tail of one item blended into the
head of the next), so all clips can still be joined without re-encoding. Captions are drawn
as overlays and the music (plus any kept clip sound) is mixed in the final pass.
"""

from __future__ import annotations

import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

from .ingest import find_tool
from .overlays import make_caption_image, make_card_image
from .pipeline import song_path

ENCODERS = {
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"],
    "preview": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"],
}
XFADES = {
    "fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown", "circleopen", "circleclose", "pixelize", "radial",
}  # fmt: skip
MIN_BODY = 0.1  # every item keeps at least this much un-blended screen time
PREVIEW_SIZE = (640, 360)


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(e.stderr.decode(errors="replace")[-1500:] or "ffmpeg failed") from e


@lru_cache(maxsize=1)
def pick_encoder() -> str:
    """Use the GPU encoder when a real test encode works, otherwise fall back to x264."""
    try:
        _run([find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
              "-c:v", "h264_nvenc", "-f", "null", "-"])  # fmt: skip
        return "h264_nvenc"
    except (RuntimeError, FileNotFoundError):
        return "libx264"


def ffcolor(c: str) -> str:
    """'#rrggbb' -> ffmpeg's '0xrrggbb'; colour names pass through."""
    return "0x" + c[1:] if c.startswith("#") else c


def _upright(src: Path, work: Path) -> Path:
    """Apply EXIF rotation (phone photos) by writing a rotated copy when needed."""
    with Image.open(src) as im:
        if im.getexif().get(274, 1) == 1:
            return src
        out = work / f"upright_{src.stem}.jpg"
        ImageOps.exif_transpose(im).convert("RGB").save(out, quality=95)
        return out


def _fit(w: int, h: int, fps: int) -> str:
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
    )


def month_fades(tl: dict, it: dict, n: int, count: int) -> tuple[float, float]:
    """Fade-in/out seconds (to/from the fade colour) for one item."""
    fade = tl["fade"]
    f_in, f_out = it.get("fade_in"), it.get("fade_out")
    if f_in is None:
        f_in = fade["video_in"] if n == 0 else 0.0
    if f_out is None:
        f_out = fade["video_out"] if n == count - 1 else 0.0
    return f_in, f_out


def plan_transitions(tl: dict, items: list[dict]) -> list[tuple[str, float]]:
    """(type, seconds) for the join after each item except the last; seconds 0 = hard cut."""
    base = tl["transition"]
    out: list[tuple[str, float]] = []
    for i, it in enumerate(items[:-1]):
        typ = it.get("transition_type") or base["type"]
        secs = it.get("transition_out")
        secs = base["seconds"] if secs is None else secs
        if typ not in XFADES or secs <= 0:
            out.append(("cut", 0.0))
            continue
        secs = min(secs, 3.0, items[i + 1]["duration"] - MIN_BODY)
        out.append((typ, max(secs, 0.0)) if secs > 0.05 else ("cut", 0.0))
    return out


def wants_clip_sound(tl: dict, it: dict) -> bool:
    if it["kind"] != "video" or not it.get("has_audio"):
        return False
    forced = it.get("keep_audio")
    return tl["sound"]["keep_video_sound"] if forced is None else bool(forced)


def audio_graph(
    tl: dict, month_dir: Path, items: list[dict], video: Path, total: float
) -> tuple[list[str], str, int]:
    """Inputs + filter graph: the music with any kept clip sounds mixed quietly underneath.

    Returns (inputs, graph, number of inputs used) so the caller can add more inputs after them.
    """
    fade, snd = tl["fade"], tl["sound"]
    inputs, chains = ["-i", str(video)], []
    songs = tl["audio"]
    idx = 1
    if songs:
        for k, a in enumerate(songs):
            dur = a["end"] - a["start"]
            inputs += ["-ss", f"{a['start']:.3f}", "-t", f"{dur:.3f}", "-i", str(song_path(month_dir, a["file"]))]
            f = ""
            if a["fade_out"] and fade["music_out"] > 0:
                d = min(fade["music_out"], dur / 2)
                f += f"afade=t=out:st={dur - d:.3f}:d={d:.3f},"
            if k == 0 and fade["music_in"] > 0:
                f += f"afade=t=in:st=0:d={min(fade['music_in'], dur / 2):.3f},"
            chains.append(f"[{idx}:a]{f}asetpts=PTS-STARTPTS[a{k}]")
            idx += 1
        labels = "".join(f"[a{k}]" for k in range(len(songs)))
        chains.append(f"{labels}concat=n={len(songs)}:v=0:a=1,volume={snd['music_volume']}[mus]")
    else:  # photos-only month: silence so the file still has an audio track
        inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        chains.append(f"[{idx}:a]anull[mus]")
        idx += 1

    mix = ["[mus]"]
    for j, it in enumerate(i for i in items if wants_clip_sound(tl, i)):
        inputs += ["-ss", f"{it['src_in']:.3f}", "-t", f"{it['duration']:.3f}", "-i", str(month_dir / it["id"])]
        ms = round(it["start"] * 1000)
        vol = snd["video_volume"] * it.get("audio_volume", 1.0)
        chains.append(f"[{idx}:a]aformat=channel_layouts=stereo,volume={vol},adelay={ms}:all=1[c{j}]")
        mix.append(f"[c{j}]")
        idx += 1
    if len(mix) > 1:
        chains.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=longest[aout]")
    else:
        chains.append("[mus]anull[aout]")
    return inputs, ";".join(chains), idx


def caption_graph(
    tl: dict, work: Path, size: tuple[int, int], fps: int, total: float, first_input: int
) -> tuple[list[str], str, bool]:
    """Overlay chain for the month's captions; each caption fades in/out over its time range."""
    inputs: list[str] = []
    parts: list[str] = []
    last = "[0:v]"
    k = 0
    for c in tl.get("captions", []):
        s, e = max(0.0, c["start"]), min(total, c["end"])
        if e - s < 0.05 or not c["text"].strip():
            continue
        png = make_caption_image(
            c["text"], size, work / f"cap_{k}.png", height_pct=c["size"], color=c["color"],
            background=c["background"], background_opacity=c["background_opacity"], position=c["position"],
        )  # fmt: skip
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{total:.3f}", "-i", str(png)]
        f = max(0.0, min(c.get("fade", 0.3), (e - s) / 2))
        fi = f"fade=t=in:st={s:.3f}:d={f:.3f}:alpha=1," if f else ""
        fo = f"fade=t=out:st={e - f:.3f}:d={f:.3f}:alpha=1," if f else ""
        if not f:  # hard on/off: blank the stream outside the time range
            fi = f"fade=t=in:st={s:.3f}:d=0.001:alpha=1,"
            fo = f"fade=t=out:st={e:.3f}:d=0.001:alpha=1,"
        parts.append(f"[{first_input + k}:v]format=rgba,{fi}{fo.rstrip(',')}[cap{k}]")
        parts.append(f"{last}[cap{k}]overlay=0:0:format=auto[o{k}]")
        last = f"[o{k}]"
        k += 1
    if not k:
        return [], "", False
    parts.append(f"{last}format=yuv420p[vout]")
    return inputs, ";".join(parts), True


def render_month(
    tl: dict, month_dir: Path, out: Path, encoder: str | None = None, preview: bool = False
) -> Path:
    ff = find_tool("ffmpeg")
    w, h = PREVIEW_SIZE if preview else tl["output"]["resolution"]
    fps = min(tl["output"]["fps"], 24) if preview else tl["output"]["fps"]
    enc = ENCODERS["preview" if preview else (encoder or pick_encoder())]
    enc = enc + ["-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2)]
    items = [i for i in tl["items"] if i["keep"] and i["duration"]]
    if not items:
        raise ValueError("timeline has no kept items")
    count = len(items)
    trans = plan_transitions(tl, items)
    color = ffcolor(tl["fade"]["color"])

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)

        def source(n: int) -> tuple[str, Path]:
            it = items[n]
            if it["kind"] == "card":
                return "img", make_card_image(it.get("color", "#000000"), it.get("text", ""),
                                              it.get("text_color", "#ffffff"), (w, h), work / f"card_{n}.png")  # fmt: skip
            if it["kind"] == "photo":
                return "img", _upright(month_dir / it["id"], work)
            return "vid", month_dir / it["id"]

        def inputs_for(n: int, offset: float, dur: float) -> tuple[list[str], str]:
            kind, path = source(n)
            if kind == "img":
                return ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(path)], ""
            it = items[n]
            ss = it["src_in"] + offset
            limit = it.get("source_duration")
            pad = max(0.0, ss + dur - limit) if limit else 0.0
            extra = f",tpad=stop_mode=clone:stop_duration={pad + 0.2:.3f}" if pad > 0 else ""
            return ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(path)], extra

        clips: list[Path] = []
        for n, it in enumerate(items):
            t_in = trans[n - 1][1] if n > 0 else 0.0
            t_out = trans[n][1] if n < count - 1 else 0.0
            body = it["duration"] - t_in
            inp, extra = inputs_for(n, t_in, body)
            vf = _fit(w, h, fps) + extra
            f_in, f_out = month_fades(tl, it, n, count)
            f_in = 0.0 if t_in else min(f_in, body)
            f_out = 0.0 if t_out else min(f_out, body)
            if f_in:
                vf += f",fade=t=in:st=0:d={f_in:.3f}:color={color}"
            if f_out:
                vf += f",fade=t=out:st={body - f_out:.3f}:d={f_out:.3f}:color={color}"
            clip = work / f"clip_{n:04d}.mp4"
            _run([ff, "-y", "-v", "error", *inp, "-vf", vf, "-an", *enc, str(clip)])
            clips.append(clip)
            if n < count - 1 and t_out > 0:  # blend the tail of this item into the head of the next
                typ, secs = trans[n]
                a_in, a_x = inputs_for(n, it["duration"], secs)
                b_in, b_x = inputs_for(n + 1, 0.0, secs)
                graph = (
                    f"[0:v]{_fit(w, h, fps)}{a_x},setpts=PTS-STARTPTS[a];"
                    f"[1:v]{_fit(w, h, fps)}{b_x},setpts=PTS-STARTPTS[b];"
                    f"[a][b]xfade=transition={typ}:duration={secs:.3f}:offset=0,format=yuv420p[v]"
                )
                tclip = work / f"trans_{n:04d}.mp4"
                _run([ff, "-y", "-v", "error", *a_in, *b_in, "-filter_complex", graph, "-map", "[v]",
                      "-an", *enc, str(tclip)])  # fmt: skip
                clips.append(tclip)

        listing = work / "list.txt"
        listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
        video = work / "video.mp4"
        _run([ff, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(video)])

        total = tl["total"]
        inputs, agraph, nxt = audio_graph(tl, month_dir, items, video, total)
        cap_inputs, cgraph, has_caps = caption_graph(tl, work, (w, h), fps, total, nxt)
        graph = ";".join(g for g in (agraph, cgraph) if g)
        out.parent.mkdir(parents=True, exist_ok=True)
        vmap, vcodec = ("[vout]", enc) if has_caps else ("0:v", ["-c:v", "copy"])
        _run([ff, "-y", "-v", "error", *inputs, *cap_inputs, "-filter_complex", graph, "-map", vmap,
              "-map", "[aout]", *vcodec, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
              "-shortest", str(out)])  # fmt: skip
    return out
