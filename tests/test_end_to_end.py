import json
import subprocess

import pytest

from yearvideo.cli import main
from yearvideo.fakedata import generate_month
from yearvideo.ingest import find_tool
from yearvideo.pipeline import build_timeline, load_config, replan, save_timeline
from yearvideo.render import render_month


def probe(path):
    out = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return json.loads(out.stdout)


@pytest.fixture
def cfg():
    c = load_config()
    c["output"] = {"resolution": [640, 360], "fps": 15}  # small and quick for tests
    return c


def test_plan_review_replan_render(tmp_path, cfg):
    generate_month(tmp_path, n_photos=10, n_dupes=2, n_videos=2)
    tl = build_timeline(tmp_path, cfg)
    assert tl["fits"], tl["note"]
    dropped = [i for i in tl["items"] if not i["keep"]]
    assert len(dropped) == 2 and all(d["drop_reason"] for d in dropped)

    # reviewer restores a dropped duplicate and pins one photo to 4s
    dropped[0]["keep"] = True
    photo = next(i for i in tl["items"] if i["kind"] == "photo" and i["keep"])
    photo["duration_override"] = 4.0
    tl = replan(tl)
    assert next(i for i in tl["items"] if i["id"] == photo["id"])["duration"] == 4.0
    assert sum(1 for i in tl["items"] if i["keep"]) == 13
    save_timeline(tl, tmp_path / "timeline.json")

    out = render_month(tl, tmp_path, tmp_path / "month.mp4", encoder="libx264")
    info = probe(out)
    kinds = {s["codec_type"] for s in info["streams"]}
    assert kinds == {"video", "audio"}
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (640, 360)
    assert float(info["format"]["duration"]) == pytest.approx(tl["total"], abs=1.0)


def test_cli_plan_and_render(tmp_path):
    generate_month(tmp_path, n_photos=8, n_dupes=1, n_videos=1)
    main(["plan", str(tmp_path), "--mode", "loop_cut"])
    tl = json.loads((tmp_path / "timeline.json").read_text())
    assert tl["music"]["mode"] == "loop_cut"
    assert tl["audio"][0]["fade_out"]
