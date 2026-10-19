import urllib.error
import urllib.parse

import pytest

from test_server_year import Client, start
from yearvideo.fakedata import generate_month, make_song
from yearvideo.fonts import list_fonts
from yearvideo.pipeline import build_timeline, load_config, save_timeline


@pytest.fixture
def served(tmp_path):
    cfg = load_config()
    cfg["output"] = {"resolution": [320, 180], "fps": 10}
    month = tmp_path / "03-March"
    generate_month(month, n_photos=6, n_dupes=0, n_videos=1)
    lib = tmp_path / "lib"
    lib.mkdir()
    make_song(lib / "layer.wav", music=30.0, seed=4)
    save_timeline(build_timeline(month, cfg, library=str(lib)), month / "timeline.json")
    server = start(month)
    yield Client(server, month.name), month
    server.shutdown()


def test_fonts_endpoint_serves_listed_fonts_only(served):
    c, _ = served
    fonts = c.json("/api/fonts")
    assert all({"file", "family", "style"} <= set(f) and "path" not in f for f in fonts)
    if fonts:
        body, _hdr, code = c.get("/font/" + urllib.parse.quote(fonts[0]["file"]))
        assert code == 200 and body[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true")  # TrueType / OpenType header
    for bad in ("nope.ttf", "..%2F..%2Fsecret.txt"):
        with pytest.raises(urllib.error.HTTPError):
            c.get("/font/" + bad)


def test_proxies_for_the_live_preview(served):
    c, month = served
    tl = c.json("/api/timeline")
    photo = next(i for i in tl["items"] if i["kind"] == "photo")
    body, hdr, _ = c.get(f"/img/{month.name}/{photo['id']}")
    assert hdr["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
    vid = next(i for i in tl["items"] if i["kind"] == "video")
    body, hdr, _ = c.get(f"/vproxy/{month.name}/{vid['id']}")
    assert b"ftyp" in body[:64]
    with pytest.raises(urllib.error.HTTPError):
        c.get(f"/vproxy/{month.name}/../../etc/passwd")


def test_song_layers_and_manual_music_over_the_api(served):
    c, _ = served
    lib = next(s for s in c.json("/api/songs") if s["source"] == "library")
    tl = c.post("/api/music", {"action": "layer_add", "file": lib["file"], "start": 5})
    assert tl["music"]["layers"][0]["lane"] == 1 and any(a["lane"] == 1 for a in tl["audio_clips"])
    tl = c.post("/api/music", {"action": "to_manual"})
    assert tl["music"]["mode"] == "manual" and tl["music"]["clips"]
    with pytest.raises(urllib.error.HTTPError):
        c.post("/api/music", {"action": "layer_add", "file": "/etc/passwd"})
    assert isinstance(list_fonts(), list)
