
import pytest

from yearvideo.dedupe import dedupe_photos
from yearvideo.fakedata import generate_month
from yearvideo.ingest import scan_month, write_catalog


def test_ingest_and_dedupe_photos(tmp_path):
    truth = generate_month(tmp_path, n_photos=8, n_dupes=3, with_video=False)
    items = scan_month(tmp_path)
    assert len(items) == 11  # music folder ignored
    assert all(i.kind == "photo" and (i.width, i.height) == (1280, 720) for i in items)

    res = dedupe_photos(tmp_path, items)
    dropped_pairs = {(r.split("duplicate of ")[1], k.split("/")[-1]) for k, r in res.dropped.items()}
    assert len(res.dropped) == 3
    for orig, dup in truth["duplicates"]:
        assert any(orig in (d[0], d[1]) or dup in (d[0], d[1]) for d in dropped_pairs)
    assert len(res.keep) == 8  # every distinct photo survives
    write_catalog(items, tmp_path / "catalog.json")
    assert (tmp_path / "catalog.json").exists()


def test_ingest_videos(tmp_path):
    generate_month(tmp_path, n_photos=1, n_dupes=0, n_videos=2)
    vids = [i for i in scan_month(tmp_path) if i.kind == "video"]
    assert len(vids) == 2
    assert vids[0].duration == pytest.approx(3.0, abs=0.2)
