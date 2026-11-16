import pytest

from test_server_year import Client, start
from yearvideo.fakedata import generate_month
from yearvideo.ingest import scan_month
from yearvideo.pipeline import build_timeline, load_config, save_timeline


@pytest.fixture
def year(tmp_path):
    cfg = load_config()
    cfg["output"] = {"resolution": [320, 180], "fps": 10}
    for name, seed in (("01-Jan", 1), ("02-Feb", 2)):
        generate_month(tmp_path / name, n_photos=4, n_dupes=0, n_videos=0, with_video=False, seed=seed)
    save_timeline(build_timeline(tmp_path / "01-Jan", cfg), tmp_path / "01-Jan" / "timeline.json")
    server = start(tmp_path)
    yield Client(server), tmp_path
    server.shutdown()


def test_hidden_cache_folders_are_never_scanned(tmp_path):
    generate_month(tmp_path, n_photos=2, n_dupes=0, n_videos=0, with_video=False)
    (tmp_path / ".thumbs").mkdir()
    (tmp_path / ".thumbs" / "x.jpg").write_bytes((next(tmp_path.glob("photos/*.jpg"))).read_bytes())
    assert all(not m.id.startswith(".") for m in scan_month(tmp_path))


def test_take_photos_from_another_month_and_send_them_back(year):
    c, root = year
    listing = c.json("/api/borrow?m=01-Jan")
    assert [m["name"] for m in listing] == ["02-Feb"]
    ids = [m["id"] for m in listing[0]["items"]][:2]
    out = c.post("/api/borrow?m=01-Jan", {"from": "02-Feb", "ids": ids})
    assert len([i for i in out["items"] if i["kind"] == "photo"]) == 6  # 4 + the 2 taken
    assert {b["from"] for b in out["borrowed"]} == {"02-Feb"}
    assert all(not (root / "02-Feb" / i).exists() for i in ids)  # really moved, not copied
    assert all((root / "01-Jan" / b["id"]).exists() for b in out["borrowed"])
    assert len(c.json("/api/borrow?m=01-Jan")[0]["items"]) == 2

    back = c.post("/api/borrow?m=01-Jan", {"action": "return", "ids": [b["id"] for b in out["borrowed"]]})
    assert len([i for i in back["items"] if i["kind"] == "photo"]) == 4 and back["borrowed"] == []
    assert all((root / "02-Feb" / i).exists() for i in ids)


def test_name_clashes_are_renamed_not_overwritten(year):
    c, root = year
    name = next((root / "02-Feb" / "photos").glob("*.jpg")).name
    mine = root / "01-Jan" / "photos" / name  # same name already in January
    mine.write_bytes(next((root / "01-Jan" / "photos").glob("*.jpg")).read_bytes())
    before = mine.read_bytes()
    c.post("/api/borrow?m=01-Jan", {"from": "02-Feb", "ids": [f"photos/{name}"]})
    assert mine.read_bytes() == before
    assert any("_from-02-Feb" in p.name for p in (root / "01-Jan" / "photos").iterdir())


def test_cannot_take_from_itself_or_outside_the_month(year):
    c, _ = year
    import urllib.error

    with pytest.raises(urllib.error.HTTPError):
        c.post("/api/borrow?m=01-Jan", {"from": "01-Jan", "ids": []})
    with pytest.raises(urllib.error.HTTPError):  # a path that climbs out of the month is refused
        c.post("/api/borrow?m=01-Jan", {"from": "02-Feb", "ids": ["../01-Jan/timeline.json"]})


def test_media_lists_every_month_and_marks_used_files(year):
    c, _ = year
    media = c.json("/api/media?m=01-Jan")
    assert [m["name"] for m in media] == ["01-Jan", "02-Feb"]
    assert len(media[0]["used"]) == 4 and media[1]["used"] == []
    assert len(media[0]["items"]) == 4 and len(media[1]["items"]) == 4


def test_taken_files_land_exactly_where_dropped(year):
    c, _ = year
    tl = c.json("/api/timeline?m=01-Jan")
    order = [i["id"] for i in tl["items"] if i["kind"] == "photo"]
    feb = [m["id"] for m in c.json("/api/media?m=01-Jan")[1]["items"]][:2]
    out = c.post("/api/borrow?m=01-Jan", {"from": "02-Feb", "ids": feb, "before_id": order[2]})
    ids = [i["id"] for i in out["items"] if i["kind"] == "photo"]
    assert ids[:2] == order[:2] and ids[4:] == order[2:]
    assert all(i["keep"] for i in out["items"] if i["id"] not in order)
    end = c.post("/api/borrow?m=01-Jan", {"from": "02-Feb", "ids": [m["id"] for m in c.json("/api/media?m=01-Jan")[1]["items"]][:1], "before_id": ""})
    assert [i["id"] for i in end["items"] if i["kind"] == "photo"][-1] in {b["id"] for b in end["borrowed"]}


def test_unassigned_music_can_be_moved_into_a_month_and_back(year):
    from yearvideo.fakedata import make_song

    c, root = year
    (root / "UNASSIGNED-MUSIC").mkdir()
    make_song(root / "UNASSIGNED-MUSIC" / "idea.wav", intro=1, music=12, outro=1)
    # a fresh server notices the folder
    c = Client(start(root))
    ua = c.json("/api/unassigned?m=01-Jan")["songs"]
    assert [u["name"] for u in ua] == ["idea.wav"]
    tl = c.post("/api/music?m=01-Jan", {"action": "claim", "file": ua[0]["file"]})
    assert (root / "01-Jan" / "music" / "idea.wav").exists() and not (root / "UNASSIGNED-MUSIC" / "idea.wav").exists()
    assert "music/idea.wav" in tl["music"]["playlist"] and any(t["file"] == "music/idea.wav" for t in tl["tracks"])
    assert c.json("/api/unassigned?m=01-Jan")["songs"] == []
    back = c.post("/api/music?m=01-Jan", {"action": "release", "file": "music/idea.wav"})
    assert (root / "UNASSIGNED-MUSIC" / "idea.wav").exists() and not (root / "01-Jan" / "music" / "idea.wav").exists()
    assert "music/idea.wav" not in back["music"]["playlist"] and all(t["file"] != "music/idea.wav" for t in back["tracks"])
    import urllib.error

    with pytest.raises(urllib.error.HTTPError):  # only songs that really sit in the unassigned folder
        c.post("/api/music?m=01-Jan", {"action": "claim", "file": str(root / "01-Jan" / "photos" / "x.jpg")})


def test_a_damaged_timeline_falls_back_to_the_backup_and_corrupt_photos_are_skipped(year):
    c, root = year
    c.post("/api/timeline?m=01-Jan", c.json("/api/timeline?m=01-Jan"))  # makes a .bak
    (root / "01-Jan" / "timeline.json").write_text("{ not json", encoding="utf-8")
    tl = c.json("/api/timeline?m=01-Jan")
    assert tl.get("restored_from_backup") and len(tl["items"]) >= 4
    assert (root / "01-Jan" / "timeline.json.corrupt").exists()
    (root / "02-Feb" / "photos" / "broken.jpg").write_bytes(b"this is not a picture")
    from yearvideo.ingest import scan_month

    skipped = []
    items = scan_month(root / "02-Feb", skipped=skipped)
    assert len(items) == 4 and [s["id"] for s in skipped] == ["photos/broken.jpg"]
