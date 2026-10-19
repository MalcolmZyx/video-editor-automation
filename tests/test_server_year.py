import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from yearvideo.cli import main
from yearvideo.fakedata import generate_month, make_song
from yearvideo.ingest import find_tool
from yearvideo.pipeline import build_timeline, load_config, refresh_timeline, save_timeline
from yearvideo.server import serve
from yearvideo.year import build_year, month_folders


@pytest.fixture
def small_cfg():
    c = load_config()
    c["output"] = {"resolution": [320, 180], "fps": 10}
    return c


class Client:
    def __init__(self, server, month=None):
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        self.month = month

    def _url(self, path):
        if self.month and "?" not in path and not path.startswith(("/thumb", "/media", "/api/months")):
            path += f"?m={self.month}"
        return self.base + path

    def get(self, path, headers=None):
        req = urllib.request.Request(self._url(path), headers=headers or {})
        with urllib.request.urlopen(req) as r:
            return r.read(), r.headers, r.status

    def post(self, path, data, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self._url(path), data=json.dumps(data).encode(), method="POST", headers=h)
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def json(self, path):
        return json.loads(self.get(path)[0])


def start(root):
    server = serve(root, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_render(c, tries=120):
    for _ in range(tries):
        st = c.json("/api/render_status")
        if st["status"] != "running":
            return st
        time.sleep(0.5)
    raise AssertionError("render did not finish")


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
    month = tmp_path / "01-January"
    generate_month(month, n_photos=8, n_dupes=1, n_videos=1)
    lib = tmp_path / "lib"
    lib.mkdir()
    make_song(lib / "extra.wav", music=30.0, seed=3)
    save_timeline(build_timeline(month, small_cfg, library=str(lib)), month / "timeline.json")
    server = start(month)
    c = Client(server, month.name)
    try:
        assert b"Month Editor" in c.get("/")[0]
        assert c.json("/api/months")[0]["name"] == "01-January"
        tl = c.json("/api/timeline")
        photo = next(i for i in tl["items"] if i["kind"] == "photo" and i["keep"])
        photo["duration_override"] = 3.0
        tl2 = c.post("/api/timeline", tl)
        assert next(i for i in tl2["items"] if i["id"] == photo["id"])["duration"] == 3.0

        thumb, hdr, _ = c.get(f"/thumb/{month.name}/{photo['id']}")
        assert hdr["Content-Type"] == "image/jpeg" and len(thumb) > 100
        vid = next(i for i in tl["items"] if i["kind"] == "video")
        assert len(c.get(f"/thumb/{month.name}/{vid['id']}")[0]) > 100
        with pytest.raises(urllib.error.HTTPError):
            c.get(f"/media/{month.name}/../../etc/passwd")

        # colour card + caption
        tl3 = c.post("/api/card", {"after_id": photo["id"], "color": "#ff8800", "text": "Part two"})
        assert any(i["kind"] == "card" and i["color"] == "#ff8800" for i in tl3["items"])
        tl3["captions"].append({"id": "c1", "text": "Hi", "start": 1.0, "end": 3.0, **tl3["caption_style"]})
        assert c.post("/api/timeline", tl3)["captions"][0]["text"] == "Hi"

        # music: list, add from library, listen, remove
        songs = c.json("/api/songs")
        assert {s["source"] for s in songs} == {"month", "library"}
        extra = next(s for s in songs if s["name"] == "extra.wav")
        tl4 = c.post("/api/music", {"action": "add", "file": extra["file"]})
        assert extra["file"] in tl4["music"]["playlist"]
        audio, hdr, _ = c.get(f"/song?file={urllib.request.quote(extra['file'])}&m={month.name}")
        assert len(audio) > 1000
        with pytest.raises(urllib.error.HTTPError):
            c.get(f"/song?file={urllib.request.quote(str(tmp_path / 'secret.txt'))}&m={month.name}")
        first = tl4["music"]["playlist"][0]
        tl5 = c.post("/api/music", {"action": "remove", "file": first})
        assert first not in tl5["music"]["playlist"]

        # quick preview render, then range-request the video like a browser does
        c.post("/api/render", {"preview": True})
        st = wait_render(c)
        assert st["status"] == "done", st
        assert st["preview_mtime"] and not st["final_mtime"]
        body, hdr, code = c.get(f"/media/{month.name}/month_preview.mp4", {"Range": "bytes=0-99"})
        assert code == 206 and len(body) == 100 and hdr["Content-Range"].startswith("bytes 0-99/")
    finally:
        server.shutdown()


def test_server_rejects_cross_site_posts_and_foreign_hosts(tmp_path, small_cfg):
    month = tmp_path / "02-February"
    generate_month(month, n_photos=4, n_dupes=0, n_videos=0, with_video=False)
    save_timeline(build_timeline(month, small_cfg), month / "timeline.json")
    server = start(month)
    c = Client(server, month.name)
    try:
        req = urllib.request.Request(  # a web page on another site can only send "simple" content types
            c.base + f"/api/timeline?m={month.name}", data=b"{}", method="POST", headers={"Content-Type": "text/plain"}
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            c.get("/api/timeline", {"Host": "evil.example"})
        assert e.value.code == 403
    finally:
        server.shutdown()


def test_year_level_review_lists_months_and_builds_missing_timelines(tmp_path):
    for n, name in enumerate(["01-January", "02-February"]):
        generate_month(tmp_path / name, n_photos=4, n_dupes=0, n_videos=0, seed=n, with_video=False)
    server = start(tmp_path)  # the year folder, not a month
    c = Client(server)
    try:
        names = [m["name"] for m in c.json("/api/months")]
        assert names == ["01-January", "02-February"]
        tl = c.json("/api/timeline?m=02-February")  # built on first open
        assert tl["month"] == "02-February" and (tmp_path / "02-February" / "timeline.json").exists()
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
