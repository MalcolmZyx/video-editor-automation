import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from yearvideo.fakedata import generate_month, make_song
from yearvideo.ingest import find_tool
from yearvideo.pipeline import (
    add_song,
    build_timeline,
    insert_card,
    list_songs,
    load_config,
    make_caption,
    remove_song,
    replan,
)
from yearvideo.render import render_month


@pytest.fixture
def cfg():
    c = load_config()
    c["output"] = {"resolution": [320, 180], "fps": 10}
    return c


def frame(video: Path, t: float, tmp_path: Path) -> np.ndarray:
    png = tmp_path / f"f_{t}.png"
    subprocess.run(
        [find_tool("ffmpeg"), "-y", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(png)],
        check=True,
    )
    return np.asarray(Image.open(png).convert("RGB")).astype(int)


def duration(video: Path) -> float:
    r = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(r.stdout)


def two_card_month(tmp_path, cfg):
    """A month with no media and no music: just a red card then a blue card, 3s each."""
    tl = build_timeline(tmp_path, cfg)
    tl["fade"].update(video_in=0.0, video_out=0.0)
    tl["transition"] = {"type": "cut", "seconds": 0.6, "align": "center"}
    insert_card(tl, color="#ff0000", seconds=3.0)
    insert_card(tl, color="#0000ff", seconds=3.0)
    return replan(tl)


def test_cards_are_blank_colour_items_anywhere_in_the_order(tmp_path, cfg):
    generate_month(tmp_path, n_photos=4, n_dupes=0, n_videos=0, with_video=False)
    tl = build_timeline(tmp_path, cfg)
    second = [i for i in tl["items"] if i["keep"]][1]["id"]
    tl = insert_card(tl, after_id=second, color="#00ff00", seconds=1.5)
    kinds = [i["kind"] for i in tl["items"]]
    assert kinds[2] == "card" and tl["items"][2]["duration"] == 1.5
    out = render_month(tl, tmp_path, tmp_path / "m.mp4", encoder="libx264")
    start = tl["items"][2]["start"]
    px = frame(out, start + 0.7, tmp_path)[90, 160]
    assert tuple(px) == pytest.approx((0, 255, 0), abs=25)


def test_crossfade_blends_neighbouring_items_without_changing_length(tmp_path, cfg):
    tl = two_card_month(tmp_path, cfg)
    cuts = render_month(tl, tmp_path, tmp_path / "cut.mp4", encoder="libx264")
    tl["transition"] = {"type": "fade", "seconds": 1.0, "align": "start"}
    fade = render_month(tl, tmp_path, tmp_path / "fade.mp4", encoder="libx264")
    assert duration(fade) == pytest.approx(duration(cuts), abs=0.25)
    # cut: pure red until the cut at 3s; fade: blue starts blending in from 3s to 4s
    assert tuple(frame(cuts, 2.5, tmp_path)[90, 160]) == pytest.approx((255, 0, 0), abs=25)
    assert tuple(frame(cuts, 3.5, tmp_path)[90, 160]) == pytest.approx((0, 0, 255), abs=25)
    mid = frame(fade, 3.5, tmp_path)[90, 160]
    assert 90 < mid[0] < 170 and 90 < mid[2] < 170  # roughly half red, half blue
    assert tuple(frame(fade, 1.0, tmp_path)[90, 160]) == pytest.approx((255, 0, 0), abs=25)
    assert tuple(frame(fade, 5.0, tmp_path)[90, 160]) == pytest.approx((0, 0, 255), abs=25)


def test_per_item_transition_override(tmp_path, cfg):
    tl = two_card_month(tmp_path, cfg)
    tl["transition"] = {"type": "fade", "seconds": 1.0, "align": "start"}
    tl["items"][0]["transition_out"] = 0  # this join stays a hard cut
    out = render_month(tl, tmp_path, tmp_path / "o.mp4", encoder="libx264")
    assert tuple(frame(out, 3.5, tmp_path)[90, 160]) == pytest.approx((0, 0, 255), abs=25)


def test_wipe_transition_type_works(tmp_path, cfg):
    tl = two_card_month(tmp_path, cfg)
    tl["transition"] = {"type": "wipeleft", "seconds": 1.0, "align": "start"}
    out = render_month(tl, tmp_path, tmp_path / "w.mp4", encoder="libx264")
    row = frame(out, 3.5, tmp_path)[90]
    assert row[:, 0].max() > 200 and row[:, 2].max() > 200  # both colours visible mid-wipe


def test_caption_shows_only_during_its_time_range(tmp_path, cfg):
    tl = two_card_month(tmp_path, cfg)
    tl["captions"].append(make_caption(tl, "Hello summer", 1.0, 2.5, fade=0.2))
    out = render_month(tl, tmp_path, tmp_path / "c.mp4", encoder="libx264")
    before, during, after = (frame(out, t, tmp_path) for t in (0.5, 1.8, 4.5))
    bottom = slice(120, 180)
    assert np.abs(during[bottom] - before[bottom]).sum() > 5000  # text appeared
    assert np.abs(after[:, :, :] - frame(out, 5.0, tmp_path)).max() < 20  # and is gone later
    assert np.abs(before[bottom] - frame(out, 0.9, tmp_path)[bottom]).max() < 20  # not before start


def test_photos_only_month_renders_with_silent_audio(tmp_path, cfg):
    generate_month(tmp_path, n_photos=4, n_dupes=0, n_videos=0, n_songs=0, with_video=False)
    tl = build_timeline(tmp_path, cfg)
    assert tl["audio"] == [] and tl["note"] == "no music selected"
    out = render_month(tl, tmp_path, tmp_path / "s.mp4", encoder="libx264")
    r = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert "audio" in r.stdout


def test_preview_render_is_small_and_fast_format(tmp_path, cfg):
    cfg["output"] = {"resolution": [1280, 720], "fps": 30}
    generate_month(tmp_path, n_photos=4, n_dupes=0, n_videos=0, with_video=False)
    tl = build_timeline(tmp_path, cfg)
    out = render_month(tl, tmp_path, tmp_path / "p.mp4", preview=True)
    assert frame(out, 1.0, tmp_path).shape[:2] == (360, 640)


def test_music_playlist_swap_and_library(tmp_path, cfg):
    month = tmp_path / "month"
    generate_month(month, n_photos=8, n_dupes=0, n_videos=0, n_songs=2, with_video=False)
    lib = tmp_path / "library"
    lib.mkdir()
    make_song(lib / "other.wav", music=30.0, seed=5)
    tl = build_timeline(month, cfg, library=str(lib))
    assert tl["music"]["playlist"] == ["music/song1.wav", "music/song2.wav"]
    names = {(s["name"], s["source"]) for s in list_songs(month, tl)}
    assert names == {("song1.wav", "month"), ("song2.wav", "month"), ("other.wav", "library")}

    tl = remove_song(tl, "music/song1.wav")  # swap: drop song1 ...
    assert [a["file"] for a in tl["audio"]] == ["music/song2.wav"]
    lib_ref = next(s["file"] for s in list_songs(month, tl) if s["name"] == "other.wav")
    tl = add_song(tl, month, lib_ref)  # ... and pick one from the library folder
    tl["music"]["playlist"] = [lib_ref]  # use only the library song
    tl = replan(tl)
    assert [a["file"] for a in tl["audio"]] == [lib_ref]
    out = render_month(tl, month, month / "m.mp4", encoder="libx264")
    assert duration(out) == pytest.approx(tl["total"], abs=0.5)


def test_blend_can_be_centered_on_or_end_at_the_cut(tmp_path, cfg):
    tl = two_card_month(tmp_path, cfg)  # red 0-3s, blue 3-6s, cut at 3s
    base = render_month(tl, tmp_path, tmp_path / "base.mp4", encoder="libx264")
    for align, pure_red_at, mix_at, pure_blue_at in [("center", 2.2, 3.0, 3.8), ("end", 1.8, 2.5, 3.2)]:
        tl["transition"] = {"type": "fade", "seconds": 1.0, "align": align}
        out = render_month(tl, tmp_path, tmp_path / f"{align}.mp4", encoder="libx264")
        assert duration(out) == pytest.approx(duration(base), abs=0.25)  # the month never gets longer
        assert tuple(frame(out, pure_red_at, tmp_path)[90, 160]) == pytest.approx((255, 0, 0), abs=25)
        mid = frame(out, mix_at, tmp_path)[90, 160]
        assert 80 < mid[0] < 180 and 80 < mid[2] < 180, (align, mid)
        assert tuple(frame(out, pure_blue_at, tmp_path)[90, 160]) == pytest.approx((0, 0, 255), abs=25)


def test_transition_never_swallows_a_short_item(tmp_path, cfg):
    tl = two_card_month(tmp_path, cfg)
    tl["items"][0]["duration"] = 0.6
    tl["transition"] = {"type": "fade", "seconds": 3.0, "align": "center"}
    from yearvideo.render import plan_transitions

    ((typ, secs, _),) = plan_transitions(tl, [i for i in tl["items"] if i["keep"]])
    assert typ == "fade" and secs <= 0.6 * 2  # clamped so each item keeps clear time


def test_every_join_can_have_its_own_style_length_and_alignment(tmp_path, cfg):
    tl = two_card_month(tmp_path, cfg)
    insert_card(tl, color="#00ff00", seconds=3.0)  # red 0-3, blue 3-6, green 6-9
    tl = replan(tl)
    red, blue, _green = [i for i in tl["items"] if i["kind"] == "card"]
    tl["transition"] = {"type": "cut", "seconds": 0.6, "align": "center"}  # month default: hard cuts
    red["transition_type"], red["transition_out"], red["transition_align"] = "fade", 1.0, "start"
    blue["transition_type"], blue["transition_out"], blue["transition_align"] = "fade", 1.0, "end"
    out = render_month(tl, tmp_path, tmp_path / "joins.mp4", encoder="libx264")
    # join 1 (3s) starts at the cut: red until 3s, mixed 3.5s, blue by 4s
    assert tuple(frame(out, 2.8, tmp_path)[90, 160]) == pytest.approx((255, 0, 0), abs=25)
    mid = frame(out, 3.5, tmp_path)[90, 160]
    assert 80 < mid[0] < 180 and 80 < mid[2] < 180
    # join 2 (6s) ends at the cut: blue until 5s, mixed 5.5s, green from 6s
    assert tuple(frame(out, 4.8, tmp_path)[90, 160]) == pytest.approx((0, 0, 255), abs=25)
    mid2 = frame(out, 5.5, tmp_path)[90, 160]
    assert 80 < mid2[1] < 180 and 80 < mid2[2] < 180
    assert tuple(frame(out, 6.3, tmp_path)[90, 160]) == pytest.approx((0, 255, 0), abs=30)
    assert duration(out) == pytest.approx(9.0, abs=0.3)
