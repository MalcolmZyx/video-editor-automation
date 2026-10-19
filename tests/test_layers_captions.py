import numpy as np
import pytest
from PIL import Image

from yearvideo.cli import main
from yearvideo.fakedata import generate_month, make_song
from yearvideo.fonts import list_fonts, resolve_font
from yearvideo.music import SR, load_audio
from yearvideo.overlays import make_caption_image
from yearvideo.pipeline import (
    add_layer,
    build_timeline,
    load_config,
    load_timeline,
    make_caption,
    refresh_timeline,
    replan,
    save_timeline,
    to_manual,
)
from yearvideo.render import render_month


@pytest.fixture
def cfg():
    c = load_config()
    c["output"] = {"resolution": [320, 180], "fps": 10}
    return c


def rms(y, a, b):
    seg = y[int(a * SR) : int(b * SR)]
    return float(np.sqrt((seg**2).mean()))


def month_with_library(tmp_path, cfg):
    month = tmp_path / "month"
    generate_month(month, n_photos=10, n_dupes=0, n_videos=0, n_songs=1, with_video=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    make_song(lib / "second.wav", music=30.0, seed=7)
    tl = build_timeline(month, cfg, library=str(lib))
    return month, lib, tl


def test_second_song_plays_at_the_same_time(tmp_path, cfg):
    month, lib, tl = month_with_library(tmp_path, cfg)
    base = render_month(tl, month, month / "base.mp4", encoder="libx264")
    ref = str((lib / "second.wav").resolve().as_posix())
    tl = add_layer(tl, month, ref, start=10.0)
    layer = tl["music"]["layers"][0]
    assert layer["lane"] == 1 and layer["start"] == 10.0 and layer["file"] == ref
    assert [c["lane"] for c in tl["audio_clips"]] == [0, 1]
    both = render_month(tl, month, month / "both.mp4", encoder="libx264")
    # before the layer starts the mixes match; while it plays the mix is louder
    assert rms(load_audio(both), 2, 8) == pytest.approx(rms(load_audio(base), 2, 8), rel=0.1)
    assert rms(load_audio(both), 12, 25) > rms(load_audio(base), 12, 25) * 1.15


def test_overlapping_layers_get_their_own_lanes(tmp_path, cfg):
    month, lib, tl = month_with_library(tmp_path, cfg)
    ref = str((lib / "second.wav").resolve().as_posix())
    tl = add_layer(tl, month, ref, start=5.0)
    tl = add_layer(tl, month, ref, start=8.0)  # overlaps the first -> new lane
    tl = add_layer(tl, month, ref, start=39.0)  # after the first ends -> can reuse lane 1
    assert [layer["lane"] for layer in tl["music"]["layers"]] == [1, 2, 1]


def test_manual_music_keeps_the_month_length_until_you_change_it(tmp_path, cfg):
    month, lib, tl = month_with_library(tmp_path, cfg)
    ref = str((lib / "second.wav").resolve().as_posix())
    tl = add_layer(tl, month, ref, start=10.0)  # a layer exists before switching to manual
    before = tl["total"]
    tl = to_manual(tl)
    m = tl["music"]
    assert m["mode"] == "manual" and not any(c.get("auto") for c in tl["audio_clips"])
    assert m["layers"] == [] and len(m["clips"]) == 2  # layers were merged into the manual clips
    assert tl["total"] == pytest.approx(before, abs=0.05)

    main = next(c for c in m["clips"] if c["lane"] == 0)
    main["start"] = 5.0  # slide the song later: the MONTH does not stretch
    assert replan(tl)["total"] == pytest.approx(before, abs=0.05)
    main["src_out"] = main["src_in"] + 12.0  # shorten the song: still the same length
    assert replan(tl)["total"] == pytest.approx(before, abs=0.05)

    m["manual_length"] = None  # follow the music instead
    expected = max(c["start"] + c["src_out"] - c["src_in"] for c in m["clips"])
    assert replan(tl)["total"] == pytest.approx(min(expected, 50.0), abs=0.1) or tl["fits"] is False
    m["manual_length"] = 30.0
    assert replan(tl)["total"] == pytest.approx(30.0, abs=0.05)
    out = render_month(tl, month, month / "manual.mp4", encoder="libx264")
    assert out.exists()


def test_clips_can_move_between_lanes_in_manual_mode(tmp_path, cfg):
    month, lib, tl = month_with_library(tmp_path, cfg)
    tl = to_manual(tl)
    ref = str((lib / "second.wav").resolve().as_posix())
    tl = add_layer(tl, month, ref, start=0.0)  # new songs also go into the manual clips
    assert len(tl["music"]["clips"]) == 2 and tl["music"]["layers"] == []
    first, second = tl["music"]["clips"]
    assert (first["lane"], second["lane"]) == (0, 1)
    first["lane"], second["lane"] = 1, 0  # drag them past each other: any lane is fine
    lanes = {c["file"]: c["lane"] for c in replan(tl)["audio_clips"]}
    assert lanes[first["file"]] == 1 and lanes[second["file"]] == 0


def test_layers_never_sit_on_the_automatic_main_lane(tmp_path, cfg):
    month, lib, tl = month_with_library(tmp_path, cfg)
    ref = str((lib / "second.wav").resolve().as_posix())
    tl = add_layer(tl, month, ref, start=3.0)
    tl["music"]["layers"][0]["lane"] = 0  # e.g. dragged onto the main lane in the editor
    assert min(c["lane"] for c in replan(tl)["audio_clips"] if not c.get("auto")) == 1


def test_captions_have_no_background_box_by_default(tmp_path, cfg):
    assert load_config()["captions"]["background_opacity"] == 0.0
    _month, _, tl = month_with_library(tmp_path, cfg)
    assert make_caption(tl, "plain", 0, 2)["background_opacity"] == 0.0


def test_fonts_are_listed_and_only_by_file_name():
    assert resolve_font("../../Windows/system.ini") is None
    assert resolve_font("") is None
    fonts = list_fonts()
    if fonts:
        f = fonts[0]
        assert {"file", "family", "style", "path"} <= set(f)
        assert resolve_font(f["file"]).name == f["file"]


def bbox(path):
    a = np.asarray(Image.open(path).convert("RGBA"))[:, :, 3]
    ys, xs = np.nonzero(a > 10)
    return xs.min(), xs.max(), ys.min(), ys.max()


def test_caption_position_rotation_and_style(tmp_path):
    size = (640, 360)
    base = {"text": "Hello world", "size": 8.0}
    low = make_caption_image({**base, "x": 0.5, "y": 0.9}, size, tmp_path / "a.png")
    corner = make_caption_image({**base, "x": 0.2, "y": 0.2}, size, tmp_path / "b.png")
    x0, x1, y0, y1 = bbox(low)
    assert abs((x0 + x1) / 2 - 320) < 8 and (y0 + y1) / 2 > 280
    x0, x1, y0, y1 = bbox(corner)
    assert abs((x0 + x1) / 2 - 128) < 8 and abs((y0 + y1) / 2 - 72) < 12

    flat = bbox(make_caption_image({**base, "rotation": 0}, size, tmp_path / "c.png"))
    turned = bbox(make_caption_image({**base, "rotation": 90}, size, tmp_path / "d.png"))
    assert (flat[1] - flat[0]) > (flat[3] - flat[2])  # wide
    assert (turned[3] - turned[2]) > (turned[1] - turned[0])  # a quarter turn makes it tall

    styled = make_caption_image(
        {**base, "outline_width": 6, "outline_color": "#ff0000", "shadow": True, "align": "left",
         "background_opacity": 0, "font": (list_fonts() or [{"file": ""}])[0]["file"]},
        size, tmp_path / "e.png",
    )  # fmt: skip
    assert bbox(styled)[1] > bbox(styled)[0]


def test_captions_with_new_options_render(tmp_path, cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=0, n_videos=0, with_video=False)
    tl = build_timeline(tmp_path, cfg)
    tl["captions"].append(make_caption(tl, "Turned", 1.0, 4.0, x=0.3, y=0.3, rotation=-15, outline_width=4))
    out = render_month(tl, tmp_path, tmp_path / "c.mp4", encoder="libx264")
    assert out.exists()


def test_old_style_captions_still_work(tmp_path, cfg):
    """A caption saved before x/y/rotation existed (only `position`) keeps its place."""
    old = {"text": "Old", "size": 6.0, "position": "top"}
    top = bbox(make_caption_image(old, (640, 360), tmp_path / "o.png"))
    assert top[3] < 120


def test_plan_will_not_overwrite_your_edits_and_backups_exist(tmp_path):
    generate_month(tmp_path, n_photos=4, n_dupes=0, n_videos=0, with_video=False)
    main(["plan", str(tmp_path)])
    tl = load_timeline(tmp_path / "timeline.json")
    tl["fade"]["color"] = "white"
    save_timeline(tl, tmp_path / "timeline.json")
    with pytest.raises(SystemExit) as e:
        main(["plan", str(tmp_path)])
    assert "rescan" in str(e.value)
    assert load_timeline(tmp_path / "timeline.json")["fade"]["color"] == "white"
    main(["plan", str(tmp_path), "--force"])
    assert load_timeline(tmp_path / "timeline.json")["fade"]["color"] == "black"
    assert load_timeline(tmp_path / "timeline.json.bak")["fade"]["color"] == "white"  # old edits kept


def test_edits_survive_rescan_and_reload(tmp_path, cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=2, n_videos=0, with_video=False)
    tl = build_timeline(tmp_path, cfg)
    dropped = next(i for i in tl["items"] if not i["keep"])
    dropped["keep"] = True  # put a removed duplicate back
    tl["fade"]["color"] = "#ffffff"
    tl["transition"].update(type="dissolve", seconds=0.9)
    tl["captions"].append(make_caption(tl, "Keep me", 1.0, 3.0))
    save_timeline(replan(tl), tmp_path / "timeline.json")

    generate_month(tmp_path, n_photos=2, n_dupes=0, n_videos=0, seed=9, with_video=False)  # new files
    tl2 = refresh_timeline(load_timeline(tmp_path / "timeline.json"), tmp_path)
    save_timeline(tl2, tmp_path / "timeline.json")
    tl3 = load_timeline(tmp_path / "timeline.json")
    assert next(i for i in tl3["items"] if i["id"] == dropped["id"])["keep"] is True
    assert tl3["fade"]["color"] == "#ffffff"
    assert tl3["transition"]["type"] == "dissolve" and tl3["transition"]["seconds"] == 0.9
    assert tl3["captions"][0]["text"] == "Keep me"


def test_lane_gaps_close_up(tmp_path, cfg):
    month, lib, tl = month_with_library(tmp_path, cfg)
    ref = str((lib / "second.wav").resolve().as_posix())
    tl = add_layer(tl, month, ref, start=3.0)
    tl = add_layer(tl, month, ref, start=6.0)
    tl["music"]["layers"][0]["lane"] = 1
    tl["music"]["layers"][1]["lane"] = 5  # dragged far away
    assert sorted(c["lane"] for c in replan(tl)["audio_clips"] if not c.get("auto")) == [1, 2]
