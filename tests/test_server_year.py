import json
import subprocess
import threading
import time
import urllib.request

import pytest

from yearvideo.cli import main
from yearvideo.fakedata import generate_month
from yearvideo.ingest import find_tool
from yearvideo.pipeline import build_timeline, load_config, refresh_timeline, save_timeline
from yearvideo.server import serve
from yearvideo.year import build_year, month_folders


@pytest.fixture
def small_cfg():
    c = load_config()
    c["output"] = {"resolution": [320, 180], "fps": 10}
    return c


def call(base, path, data=None):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                 method="POST" if data is not None else "GET")  # fmt: skip
    with urllib.request.urlopen(req) as r:
        return r.read(), r.headers


def test_refresh_adds_new_files_and_keeps_decisions(tmp_path, small_cfg):
    generate_month(tmp_path, n_photos=6, n_dupes=1, n_videos=0, with_video=False)
    tl = build_timeline(tmp_path, small_cfg)
    victim = next(i for i in tl["items"] if i["kind"] == "photo" and i["keep"])
    victim["keep"] = False  # reviewer drops one
    generate_month(tmp_path, n_photos=3, n_dupes=0, n_videos=0, seed=7, with_video=False)  # new files
    tl = refresh_timeline(tl, tmp_path)
    assert next(i for i in tl["items"] if i["id"] == victim["id"])["keep"] is False
    assert sum(1 for i in tl["items"] if i["kind"] == "photo") == 7 + 3


def test_review_server_roundtrip(tmp_path, small_cfg):
    generate_month(tmp_path, n_photos=8, n_dupes=1, n_videos=1)
    save_timeline(build_timeline(tmp_path, small_cfg), tmp_path / "timeline.json")
    server = serve(tmp_path, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        body, _ = call(base, "/")
        assert b"Month Review" in body
        tl = json.loads(call(base, "/api/timeline")[0])
        photo = next(i for i in tl["items"] if i["kind"] == "photo" and i["keep"])
        photo["duration_override"] = 3.0
        tl2 = json.loads(call(base, "/api/timeline", tl)[0])
        assert next(i for i in tl2["items"] if i["id"] == photo["id"])["duration"] == 3.0
        thumb, hdr = call(base, "/thumb/" + photo["id"])
        assert hdr["Content-Type"] == "image/jpeg" and len(thumb) > 100
        vid = next(i for i in tl["items"] if i["kind"] == "video")
        assert len(call(base, "/thumb/" + vid["id"])[0]) > 100
        with pytest.raises(urllib.error.HTTPError):
            call(base, "/media/../../etc/passwd")
        call(base, "/api/render", {})
        for _ in range(60):
            st = json.loads(call(base, "/api/render_status")[0])
            if st["status"] != "running":
                break
            time.sleep(0.5)
        assert st["status"] == "done", st
        assert st["has_output"]
    finally:
        server.shutdown()


def test_build_year_joins_months(tmp_path, small_cfg):
    for n, name in enumerate(["01-January", "02-February"]):
        generate_month(tmp_path / name, n_photos=6, n_dupes=0, n_videos=1, seed=n)
    (tmp_path / "notes").mkdir()
    assert [d.name for d in month_folders(tmp_path)] == ["01-January", "02-February"]
    out = build_year(tmp_path)
    assert out.exists()
    durs = []
    for p in [tmp_path / "01-January" / "month.mp4", tmp_path / "02-February" / "month.mp4", out]:
        r = subprocess.run([find_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True)  # fmt: skip
        durs.append(float(r.stdout))
    assert durs[2] == pytest.approx(durs[0] + durs[1], abs=0.5)


def test_cli_year_command_exists():
    with pytest.raises(SystemExit):
        main(["year"])
