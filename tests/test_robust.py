"""Failure handling and speed: nothing may be lost when things go wrong, and rescans must stay cheap."""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from test_server_year import Client, start
from yearvideo import ingest
from yearvideo.fakedata import generate_month
from yearvideo.ingest import scan_month
from yearvideo.pipeline import build_timeline, load_config, load_timeline, save_timeline
from yearvideo.server import Review


@pytest.fixture
def year(tmp_path):
    cfg = load_config()
    cfg["output"] = {"resolution": [320, 180], "fps": 10}
    for name, seed in (("01-Jan", 1), ("02-Feb", 2)):
        generate_month(tmp_path / name, n_photos=6, n_dupes=0, n_videos=0, with_video=False, seed=seed)
    save_timeline(build_timeline(tmp_path / "01-Jan", cfg), tmp_path / "01-Jan" / "timeline.json")
    return tmp_path


def test_a_crash_while_saving_never_damages_the_timeline(year, monkeypatch):
    path = year / "01-Jan" / "timeline.json"
    before = path.read_text(encoding="utf-8")
    tl = load_timeline(path)
    tl["items"][0]["duration_override"] = 9.0

    def boom(*a, **k):
        raise PermissionError("locked by antivirus")

    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        save_timeline(tl, path)
    assert path.read_text(encoding="utf-8") == before  # the old file is untouched and still valid
    json.loads(before)


def test_scanning_twice_only_reads_new_files(year, monkeypatch):
    d = year / "01-Jan"
    first = scan_month(d)
    calls = []
    real = ingest._photo_info
    monkeypatch.setattr(ingest, "_photo_info", lambda p: calls.append(p) or real(p))
    again = scan_month(d)
    assert [i.id for i in again] == [i.id for i in first] and calls == []
    generate_month(year / "02-Feb", n_photos=1, n_dupes=0, n_videos=0, with_video=False, seed=9)
    calls.clear()
    scan_month(year / "02-Feb")
    assert len(calls) <= 7  # that folder was never scanned before; the cache is per folder


def test_many_windows_saving_at_once_never_corrupt_the_file(year):
    c = Client(start(year))
    base = c.json("/api/timeline?m=01-Jan")
    results, errors = [], []

    def worker(k):
        try:
            tl = json.loads(json.dumps(base))
            tl["items"][0]["duration_override"] = 3 + k
            results.append(c.post("/api/timeline?m=01-Jan", tl)["rev"])
        except urllib.error.HTTPError as e:
            errors.append(e.code)

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 1 and errors == [409] * 9  # exactly one wins; the rest are told they are stale
    assert load_timeline(year / "01-Jan" / "timeline.json")["rev"] == results[0]


def test_a_failed_move_keeps_everything_consistent(year, monkeypatch):
    import shutil

    a, b = Review(year / "01-Jan"), Review(year / "02-Feb")
    ids = [m["id"] for m in b.media_list()][:3]
    real, n = shutil.move, {"n": 0}

    def flaky(src, dst):
        n["n"] += 1
        if n["n"] == 2:
            raise OSError("file is in use")
        return real(src, dst)

    monkeypatch.setattr(shutil, "move", flaky)
    with pytest.raises(OSError, match="moved 1 of 3"):
        a.take_from(b, ids)
    tl = a.timeline()
    assert len(tl["borrowed"]) == 1  # the one that did move is tracked, so it can still be sent back
    assert len([i for i in tl["items"] if i["kind"] == "photo"]) == 7
    assert len(b.media_list()) == 5


def test_bad_request_bodies_get_a_clean_error(year):
    c = Client(start(year))
    req = urllib.request.Request(c.base + "/api/timeline?m=01-Jan", data=b"{not json", method="POST",
                                 headers={"Content-Type": "application/json"})  # fmt: skip
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400
    assert c.json("/api/months")  # and the server is still fine


def test_a_bigger_month_stays_fast(tmp_path):
    cfg = load_config()
    cfg["output"] = {"resolution": [320, 180], "fps": 10}
    generate_month(tmp_path / "01-Big", n_photos=300, n_dupes=0, n_videos=0, with_video=False, seed=3)
    t = time.perf_counter()
    save_timeline(build_timeline(tmp_path / "01-Big", cfg), tmp_path / "01-Big" / "timeline.json")
    first = time.perf_counter() - t
    r = Review(tmp_path / "01-Big")
    t = time.perf_counter()
    r.rescan()
    again = time.perf_counter() - t
    tl = r.timeline()
    t = time.perf_counter()
    r.save(tl)
    save = time.perf_counter() - t
    assert len(tl["items"]) >= 300
    assert again < 3 and save < 3, (first, again, save)  # generous limits; real numbers are far lower


def test_the_editors_own_renders_are_never_scanned_as_clips(tmp_path):
    generate_month(tmp_path, n_photos=2, n_dupes=0, n_videos=0, with_video=False)
    for n in ("month.mp4", "month_preview.mp4", "month.part.mp4"):
        (tmp_path / n).write_bytes(b"not a real video")
    skipped = []
    assert len(scan_month(tmp_path, skipped=skipped)) == 2 and skipped == []
    assert [m["id"] for m in Review(tmp_path).media_list()] == ["photos/IMG_00000.jpg", "photos/IMG_00001.jpg"]


def test_a_finished_year_video_does_not_turn_the_year_folder_into_a_month(year):
    from yearvideo.server import App

    (year / "year.mp4").write_bytes(b"not a real video")
    app = App(year)
    assert not app.single and set(app.months) == {"01-Jan", "02-Feb"}


def test_nonsense_numbers_from_the_editor_are_cleaned_up(year):
    c = Client(start(year))
    tl = c.json("/api/timeline?m=01-Jan")
    a, b, cap = tl["items"][0], tl["items"][1], None
    a["duration_override"], a["fade_in"], a["audio_volume"] = -4, "abc", 99
    b["duration_override"], b["transition_out"] = 0, -2
    tl["captions"] = [{"id": "c1", "text": "hi", "start": -5, "end": -9}]
    out = c.post("/api/timeline?m=01-Jan", tl)
    a, b = out["items"][0], out["items"][1]
    assert a["duration_override"] == 0.2 and a["fade_in"] is None and a["audio_volume"] == 4
    assert b["duration_override"] == 0.2 and b["transition_out"] == 0
    cap = out["captions"][0]
    assert cap["start"] == 0 and cap["end"] >= 0.1


def test_the_restored_from_backup_notice_is_shown_once_not_saved_forever(year):
    c = Client(start(year))
    c.post("/api/timeline?m=01-Jan", c.json("/api/timeline?m=01-Jan"))  # makes a .bak
    (year / "01-Jan" / "timeline.json").write_text("{ broken", encoding="utf-8")
    tl = c.json("/api/timeline?m=01-Jan")
    assert tl["restored_from_backup"]
    c.post("/api/timeline?m=01-Jan", tl)  # the browser saves what it was given
    assert "restored_from_backup" not in json.loads((year / "01-Jan" / "timeline.json").read_text(encoding="utf-8"))
    assert "restored_from_backup" not in c.json("/api/timeline?m=01-Jan")
