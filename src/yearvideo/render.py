"""Render a month's timeline.json to an mp4 with ffmpeg.

Every item becomes a small uniform clip. Where two items are joined by a cross-fade, the
overlap is rendered as its own short transition clip (the end of one item blended into the
start of the next), so all clips can still be joined without re-encoding. Captions are drawn
as overlays and the music (all song layers, plus any kept clip sound) is mixed in the final pass.
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
ALIGN = {"start": 0.0, "center": 0.5, "end": 1.0}  # how much of a blend lies BEFORE the cut
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


def plan_transitions(tl: dict, items: list[dict]) -> list[tuple[str, float, float]]:
    """(type, seconds, align) for the join after each item except the last; seconds 0 = hard cut.

    Every join can have its own style, length and alignment (an item's transition_type /
    transition_out / transition_align), falling back to the month's settings. A blend of length t at
    a cut lies from (cut - a*t) to (cut + (1-a)*t), where a is 0, 0.5 or 1 for start / center / end.
    Lengths are clamped so every item keeps some clear screen time.
    """
    base = tl["transition"]
    out: list[tuple[str, float, float]] = []
    prev_t, prev_a = 0.0, 0.5
    for i, it in enumerate(items[:-1]):
        typ = it.get("transition_type") or base["type"]
        secs = it.get("transition_out")
        secs = base["seconds"] if secs is None else secs
        a = ALIGN.get(it.get("transition_align") or base.get("align", "center"), 0.5)
        if typ not in XFADES or secs <= 0:
            out.append(("cut", 0.0, a))
            prev_t = 0.0
            continue
        cap = 3.0
        if a > 0:
            cap = min(cap, (it["duration"] - (1 - prev_a) * prev_t - MIN_BODY) / a)
        if a < 1:
            cap = min(cap, (items[i + 1]["duration"] - MIN_BODY) / (1 - a))
        secs = min(secs, cap)
        if secs > 0.05:
            out.append((typ, secs, a))
            prev_t, prev_a = secs, a
        else:
            out.append(("cut", 0.0, a))
            prev_t = 0.0
    return out


def wants_clip_sound(tl: dict, it: dict) -> bool:
    if it["kind"] != "video" or not it.get("has_audio"):
        return False
    forced = it.get("keep_audio")
    return tl["sound"]["keep_video_sound"] if forced is None else bool(forced)


def audio_graph(
    tl: dict, month_dir: Path, items: list[dict], video: Path, total: float
) -> tuple[list[str], str, int]:
    """Inputs + filter graph: every music clip (all layers) mixed, plus kept clip sounds underneath.

    Returns (inputs, graph, number of inputs used) so the caller can add more inputs after them.
    """
    snd = tl["sound"]
    inputs, chains = ["-i", str(video)], []
    idx = 1
    clips = tl.get("audio_clips", [])
    for k, c in enumerate(clips):
        dur = c["src_out"] - c["src_in"]
        inputs += ["-ss", f"{c['src_in']:.3f}", "-t", f"{dur:.3f}", "-i", str(song_path(month_dir, c["file"]))]
        f = "aformat=channel_layouts=stereo,"
        if c.get("fade_in", 0) > 0:
            f += f"afade=t=in:st=0:d={min(c['fade_in'], dur / 2):.3f},"
        if c.get("fade_out", 0) > 0:
            d = min(c["fade_out"], dur / 2)
            f += f"afade=t=out:st={dur - d:.3f}:d={d:.3f},"
        chains.append(f"[{idx}:a]{f}volume={c.get('volume', 1.0)},adelay={round(c['start'] * 1000)}:all=1[m{k}]")
        idx += 1
    if clips:
        labels = "".join(f"[m{k}]" for k in range(len(clips)))
        mixed = f"{labels}amix=inputs={len(clips)}:normalize=0:duration=longest," if len(clips) > 1 else f"{labels}"
        chains.append(f"{mixed}volume={snd['music_volume']}[mus]")
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
        png = make_caption_image(c, size, work / f"cap_{k}.png")
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{total:.3f}", "-i", str(png)]
        f = max(0.0, min(c.get("fade", 0.3), (e - s) / 2))
        if f:
            fi, fo = f"fade=t=in:st={s:.3f}:d={f:.3f}:alpha=1,", f"fade=t=out:st={e - f:.3f}:d={f:.3f}:alpha=1"
        else:  # hard on/off: blank the stream outside the time range
            fi, fo = f"fade=t=in:st={s:.3f}:d=0.001:alpha=1,", f"fade=t=out:st={e:.3f}:d=0.001:alpha=1"
        parts.append(f"[{first_input + k}:v]format=rgba,{fi}{fo}[cap{k}]")
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
            """Input args + padding filter giving `dur` seconds of item n starting `offset` into it.

            Offsets before the start of a clip repeat its first frame; offsets past its end repeat the last.
            """
            kind, path = source(n)
            if kind == "img":
                return ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(path)], ""
            it = items[n]
            raw = it["src_in"] + offset
            lead, ss = max(0.0, -raw), max(0.0, raw)
            limit = it.get("source_duration")
            need = dur - lead
            take = min(need, limit - ss) if limit else need
            if take <= 0.05:  # asked for something beyond the clip: hold its final frame
                ss, take = max(0.0, (limit or 0) - 0.1), 0.1
            tail = max(0.0, need - take)
            pads = []
            if lead > 0:
                pads.append(f"start_mode=clone:start_duration={lead:.3f}")
            if tail > 0:
                pads.append(f"stop_mode=clone:stop_duration={tail + 0.1:.3f}")
            extra = f",tpad={':'.join(pads)}" if pads else ""
            return ["-ss", f"{ss:.3f}", "-t", f"{take:.3f}", "-i", str(path)], extra

        clips: list[Path] = []
        for n, it in enumerate(items):
            t_in, a_in = (trans[n - 1][1], trans[n - 1][2]) if n > 0 else (0.0, 0.5)
            t_out, a_out = (trans[n][1], trans[n][2]) if n < count - 1 else (0.0, 0.5)
            u0 = (1 - a_in) * t_in  # the first part of this item is already on screen in the incoming blend
            body = it["duration"] - u0 - a_out * t_out
            inp, extra = inputs_for(n, u0, body)
            vf = _fit(w, h, fps) + extra
            f_in, f_out = month_fades(tl, it, n, count)
            f_in = 0.0 if t_in else min(f_in, body)
            f_out = 0.0 if t_out else min(f_out, body)
            if f_in:
                vf += f",fade=t=in:st=0:d={f_in:.3f}:color={color}"
            if f_out:
                vf += f",fade=t=out:st={body - f_out:.3f}:d={f_out:.3f}:color={color}"
            clip = work / f"clip_{n:04d}.mp4"
            _run([ff, "-y", "-v", "error", *inp, "-vf", vf, "-an", "-t", f"{body:.3f}", *enc, str(clip)])
            clips.append(clip)
            if n < count - 1 and t_out > 0:  # blend the end of this item into the start of the next
                typ, secs, a = trans[n]
                a_in, a_x = inputs_for(n, it["duration"] - a * secs, secs)
                b_in, b_x = inputs_for(n + 1, -a * secs, secs)
                graph = (
                    f"[0:v]{_fit(w, h, fps)}{a_x},setpts=PTS-STARTPTS[a];"
                    f"[1:v]{_fit(w, h, fps)}{b_x},setpts=PTS-STARTPTS[b];"
                    f"[a][b]xfade=transition={typ}:duration={secs:.3f}:offset=0,format=yuv420p[v]"
                )
                tclip = work / f"trans_{n:04d}.mp4"
                _run([ff, "-y", "-v", "error", *a_in, *b_in, "-filter_complex", graph, "-map", "[v]",
                      "-an", "-t", f"{secs:.3f}", *enc, str(tclip)])  # fmt: skip
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
              "-t", f"{total:.3f}", str(out)])  # fmt: skip
    return out
