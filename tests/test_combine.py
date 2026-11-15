import json
import subprocess
import time
import urllib.error

import pytest

from test_server_year import Client, start
from yearvideo.combine import combine, combined_sources, uncombine
from yearvideo.fakedata import generate_month
from yearvideo.ingest import find_tool
from yearvideo.pipeline import build_timeline, load_config, save_timeline
from yearvideo.year import build_year, month_folders


def duration(p):
    out = subprocess.run([find_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return float(out)


@pytest.fixture
def year(tmp_path):
    cfg = load_config()
    cfg["output"] = {"resolution": [320, 180], "fps": 10}
    cfg["title"] = {"enabled": False, "seconds": 2}
    for name, seed in (("01-Jan", 1), ("02-Feb", 2), ("03-Mar", 3)):
        generate_month(tmp_path / name, n_photos=4, n_dupes=0, n_videos=0, with_video=False, seed=seed)
        save_timeline(build_timeline(tmp_path / name, cfg), tmp_path / name / "timeline.json")
    return tmp_path


def test_combine_points_at_the_original_files_and_hides_the_sources(year):
    before = {p.name for p in (year / "01-Jan" / "photos").iterdir()}
    dest = combine(year, ["01-Jan", "02-Feb"])
    assert dest.name == "01-Jan+Feb"
    tl = json.loads((dest / "timeline.json").read_text())
    photos = [i for i in tl["items"] if i["kind"] == "photo"]
    assert len(photos) == 8 and all(i["src"].startswith("@0") for i in photos)
    assert {i["from_month"] for i in photos} == {"01-Jan", "02-Feb"}
    assert {p.name for p in (year / "01-Jan" / "photos").iterdir()} == before  # nothing moved
    assert not list(dest.glob("*.jpg")) and not (dest / "photos").exists()
    assert combined_sources(year) == {"01-Jan": dest.name, "02-Feb": dest.name}
    with pytest.raises(ValueError):
        combine(year, ["01-Jan", "03-Mar"])  # 01-Jan already belongs to a combined month


def test_combined_month_renders_with_both_sources_and_year_replaces_them(year):
    dest = combine(year, ["01-Jan", "02-Feb"])
    out = build_year(year)
    assert out.exists()
    assert (dest / "month.mp4").exists()
    assert not (year / "01-Jan" / "month.mp4").exists() and not (year / "02-Feb" / "month.mp4").exists()
    assert (year / "03-Mar" / "month.mp4").exists()
    assert duration(out) == pytest.approx(duration(dest / "month.mp4") + duration(year / "03-Mar" / "month.mp4"), abs=1.0)


def test_uncombine_brings_the_months_back(year):
    dest = combine(year, ["01-Jan", "02-Feb"])
    assert uncombine(year, dest.name) == ["01-Jan", "02-Feb"]
    assert not dest.exists() and combined_sources(year) == {}
    assert [d.name for d in month_folders(year)] == ["01-Jan", "02-Feb", "03-Mar"]


def test_server_combine_split_thumbs_and_stale_guard(year):
    c = Client(start(year))
    out = c.post("/api/combine?m=01-Jan", {"months": ["01-Jan", "02-Feb"]})
    assert out["name"] == "01-Jan+Feb"
    byname = {m["name"]: m for m in out["months"]}
    assert byname["01-Jan"]["hidden_by"] == "01-Jan+Feb" and byname["01-Jan+Feb"]["combined"]
    tl = c.json("/api/timeline?m=01-Jan%2BFeb")
    it = next(i for i in tl["items"] if i["kind"] == "photo" and i["from_month"] == "02-Feb")
    from urllib.parse import quote

    data, headers, _ = c.get(f"/thumb/{quote('01-Jan+Feb')}/{quote(it['src'], safe='/@')}")
    assert headers["Content-Type"].startswith("image/") and len(data) > 100
    # no way to read arbitrary files through the @ form
    with pytest.raises(urllib.error.HTTPError):
        c.get(f"/media/{quote('01-Jan+Feb')}/@../outside.txt")
    # stale window: a second save with the old revision is refused
    first = c.post("/api/timeline?m=01-Jan%2BFeb", tl)
    with pytest.raises(urllib.error.HTTPError) as e:
        c.post("/api/timeline?m=01-Jan%2BFeb", tl)
    assert e.value.code == 409
    assert c.post("/api/timeline?m=01-Jan%2BFeb", first)["rev"] == first["rev"] + 1
    back = c.post("/api/combine?m=01-Jan", {"action": "split", "name": "01-Jan+Feb"})
    assert back["back"] == ["01-Jan", "02-Feb"]


def test_year_build_runs_in_the_server_and_reports_progress(year):
    c = Client(start(year))
    c.post("/api/combine?m=01-Jan", {"months": ["01-Jan", "02-Feb"]})
    c.post("/api/year?m=01-Jan", {})
    for _ in range(240):
        st = c.json("/api/year_status?m=01-Jan")
        if st["status"] != "running":
            break
        time.sleep(0.5)
    assert st["status"] == "done", st
    assert st["year_mtime"] and (year / "year.mp4").exists()
