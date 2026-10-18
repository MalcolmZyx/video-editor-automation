import subprocess

import numpy as np
import pytest
from PIL import Image

from yearvideo.fakedata import generate_month
from yearvideo.ingest import find_tool, scan_month
from yearvideo.music import SR, load_audio
from yearvideo.pipeline import build_timeline, load_config, replan
from yearvideo.render import audio_graph, render_month, wants_clip_sound


@pytest.fixture
def cfg():
    c = load_config()
    c["output"] = {"resolution": [320, 180], "fps": 10}
    return c


def rms(y, a, b):
    seg = y[int(a * SR) : int(b * SR)]
    return float(np.sqrt((seg**2).mean()))


def first_frame_brightness(video, tmp_path):
    png = tmp_path / "f0.png"
    subprocess.run(
        [find_tool("ffmpeg"), "-y", "-v", "error", "-i", str(video), "-frames:v", "1", str(png)],
        check=True,
    )
    return float(np.asarray(Image.open(png).convert("L")).mean())


def test_ingest_detects_video_audio(tmp_path):
    generate_month(tmp_path, n_photos=1, n_dupes=0, n_videos=1)
    vids = [i for i in scan_month(tmp_path) if i.kind == "video"]
    assert vids[0].has_audio


def test_clip_sound_is_off_by_default_and_per_clip_switchable(tmp_path, cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=0, n_videos=2)
    tl = build_timeline(tmp_path, cfg)
    vids = [i for i in tl["items"] if i["kind"] == "video"]
    assert not any(wants_clip_sound(tl, v) for v in vids)
    vids[0]["keep_audio"] = True
    assert wants_clip_sound(tl, vids[0]) and not wants_clip_sound(tl, vids[1])
    tl["video_sound"]["keep_default"] = True
    vids[1]["keep_audio"] = False
    assert wants_clip_sound(tl, vids[0]) and not wants_clip_sound(tl, vids[1])


def test_graph_places_clip_sound_and_ducks_music(tmp_path, cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=0, n_videos=1)
    tl = build_timeline(tmp_path, cfg)
    vid = next(i for i in tl["items"] if i["kind"] == "video")
    vid["keep_audio"] = True
    tl = replan(tl)
    items = [i for i in tl["items"] if i["keep"]]
    _, graph = audio_graph(tl, tmp_path, items, tmp_path / "v.mp4")
    assert "adelay=" in graph and "amix=inputs=2" in graph and "volume='1-(1-0.25)" in graph


def test_muted_clip_with_full_duck_is_silent_in_render(tmp_path, cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=0, n_videos=1)
    tl = build_timeline(tmp_path, cfg)
    vid = next(i for i in tl["items"] if i["kind"] == "video")
    base = render_month(tl, tmp_path, tmp_path / "base.mp4", encoder="libx264")
    vid["keep_audio"], vid["audio_volume"] = True, 0.0
    tl["video_sound"].update(duck=0.0, ramp=0.05)  # music fully off while the clip plays
    tl = replan(tl)
    vid = next(i for i in tl["items"] if i["id"] == vid["id"])
    out = render_month(tl, tmp_path, tmp_path / "ducked.mp4", encoder="libx264")
    a, b = vid["start"] + 0.3, vid["start"] + vid["duration"] - 0.3
    assert rms(load_audio(base), a, b) > 0.02  # music normally plays here
    assert rms(load_audio(out), a, b) < 0.005  # silent: clip volume 0 + music ducked to 0


def test_custom_fade_colour_and_length(tmp_path, cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=0, n_videos=0, with_video=False)
    tl = build_timeline(tmp_path, cfg)
    black = render_month(tl, tmp_path, tmp_path / "black.mp4", encoder="libx264")
    tl["fade"].update(video_in=1.0, color="white")
    white = render_month(tl, tmp_path, tmp_path / "white.mp4", encoder="libx264")
    assert first_frame_brightness(black, tmp_path) < 15
    assert first_frame_brightness(white, tmp_path) > 240


def test_per_item_fade_overrides(tmp_path, cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=0, n_videos=0, with_video=False)
    tl = build_timeline(tmp_path, cfg)
    mid = [i for i in tl["items"] if i["keep"]][3]
    mid["fade_in"], mid["fade_out"] = 0.7, 0.7
    out = render_month(tl, tmp_path, tmp_path / "m.mp4", encoder="libx264")
    assert out.exists()


def test_demo_command_builds_fake_year(tmp_path):
    from yearvideo.cli import main

    main(["demo", str(tmp_path / "d"), "--months", "2", "--no-review"])
    assert (tmp_path / "d" / "01-January" / "timeline.json").exists()
    assert (tmp_path / "d" / "02-February" / "timeline.json").exists()
