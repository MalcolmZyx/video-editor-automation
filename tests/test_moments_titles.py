import subprocess

from yearvideo.ingest import find_tool
from yearvideo.moments import pick_start, score_video
from yearvideo.titlecard import make_title_card, month_title


def test_month_title_parsing():
    assert month_title("01-January") == "January"
    assert month_title("03") == "March"
    assert month_title("07_July") == "July"
    assert month_title("Summer Trip") == "Summer Trip"


def test_title_card_is_written(tmp_path):
    p = make_title_card("January", (640, 360), tmp_path / "t.png")
    assert p.exists() and p.stat().st_size > 0


def test_pick_start_prefers_high_scores():
    scores = [0.0] * 4 + [1.0] * 4 + [0.0] * 4
    assert pick_start(scores, 3, 12.0) == 4.0
    assert pick_start(scores, 20, 12.0) == 0.0  # clip shorter than the window


def test_dark_start_is_skipped(tmp_path):
    out = tmp_path / "v.mp4"
    graph = (
        "color=black:s=320x180:r=30:d=4[a];testsrc2=s=320x180:r=30:d=4[b];"
        "[a][b]concat=n=2:v=1:a=0,format=yuv420p"
    )
    subprocess.run(
        [find_tool("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", graph, str(out)], check=True
    )
    scores = score_video(out)
    assert len(scores) >= 7
    assert pick_start(scores, 3, 8.0) >= 3.0
