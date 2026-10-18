

from yearvideo.fakedata import generate_month


def test_generates_expected_files(tmp_path):
    truth = generate_month(tmp_path, n_photos=5, n_dupes=2, n_videos=0, with_video=False)
    assert len(list((tmp_path / "photos").glob("*.jpg"))) == 7
    assert len(truth["duplicates"]) == 2
    song = truth["songs"][0]
    assert song["music_start"] == 4.0 and song["music_end"] == 44.0
    assert (tmp_path / "music" / song["file"]).exists()


def test_generates_videos(tmp_path):
    truth = generate_month(tmp_path, n_photos=1, n_dupes=0, n_videos=1)
    assert (tmp_path / "videos" / truth["videos"][0]).stat().st_size > 0
