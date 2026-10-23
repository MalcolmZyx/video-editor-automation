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
