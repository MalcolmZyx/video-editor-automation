import pytest

from yearvideo.fakedata import generate_month
from yearvideo.pipeline import (
    build_timeline,
    item_src,
    load_config,
    refresh_timeline,
    replan,
    set_manual_timing,
    split_item,
)
from yearvideo.render import render_month


@pytest.fixture
def cfg():
    c = load_config()
    c["output"] = {"resolution": [320, 180], "fps": 10}
    return c


def month_with_video(tmp_path, cfg):
    generate_month(tmp_path, n_photos=3, n_dupes=0, n_videos=1, n_songs=0)
    return build_timeline(tmp_path, cfg)


def test_split_a_video_into_two_pieces_that_play_the_same_file(tmp_path, cfg):
    tl = month_with_video(tmp_path, cfg)
    vid = next(i for i in tl["items"] if i["kind"] == "video" and i["keep"])
    total, vid_id, vid_dur = tl["total"], vid["id"], vid["duration"]
    at = vid["start"] + vid_dur / 2
    tl = split_item(tl, at)
    a = next(i for i in tl["items"] if i["id"] == vid["id"])
    b = tl["items"][tl["items"].index(a) + 1]
    assert b["id"] != a["id"] and item_src(b) == item_src(a) == vid_id
    assert a["duration"] + b["duration"] == pytest.approx(vid_dur, abs=0.01)
    assert b["src_in"] == pytest.approx(a["src_in"] + a["duration"], abs=0.01)
    assert (a["transition_type"], a["transition_out"]) == ("cut", 0)  # hard cut between the halves
    assert tl["total"] == pytest.approx(total, abs=0.3)  # splitting never changes the month length
    out = render_month(tl, tmp_path, tmp_path / "split.mp4", encoder="libx264")
    assert out.exists()


def test_both_halves_survive_a_rescan_and_the_file_is_not_added_twice(tmp_path, cfg):
    tl = month_with_video(tmp_path, cfg)
    vid = next(i for i in tl["items"] if i["kind"] == "video" and i["keep"])
    tl = split_item(tl, vid["start"] + 1.0)
    n = len(tl["items"])
    tl = refresh_timeline(tl, tmp_path)
    assert len(tl["items"]) == n


def test_split_refuses_edges_and_gaps(tmp_path, cfg):
    tl = replan(month_with_video(tmp_path, cfg))
    first = next(i for i in tl["items"] if i["keep"] and i.get("duration"))
    with pytest.raises(ValueError):
        split_item(tl, first["start"] + 0.05)
    with pytest.raises(ValueError):
        split_item(tl, tl["total"] + 5)


def test_split_a_photo_and_a_card(tmp_path, cfg):
    tl = month_with_video(tmp_path, cfg)
    photo = next(i for i in tl["items"] if i["kind"] == "photo" and i["keep"])
    pid, dur, mid = photo["id"], photo["duration"], photo["start"] + photo["duration"] / 2
    tl = split_item(tl, mid)
    halves = [i for i in tl["items"] if item_src(i) == pid]
    assert len(halves) == 2
    assert sum(h["duration"] for h in halves) == pytest.approx(dur, abs=0.01)


def month_with_song(tmp_path, cfg):
    generate_month(tmp_path, n_photos=10, n_dupes=0, n_videos=0, n_songs=1, with_video=False)
    return build_timeline(tmp_path, cfg)


def test_manual_timing_pins_lengths_so_nothing_moves_and_stretching_does_not_refit_others(tmp_path, cfg):
    tl = month_with_song(tmp_path, cfg)
    before = [(i["id"], round(i["duration"], 2)) for i in tl["items"] if i["keep"]]
    total = tl["total"]
    tl = set_manual_timing(tl, True)
    assert [(i["id"], round(i["duration"], 2)) for i in tl["items"] if i["keep"]] == before  # nothing visibly moved
    assert tl["total"] == pytest.approx(total, abs=0.05) and tl["manual_timing"] is True
    first = next(i for i in tl["items"] if i["keep"])
    first["duration_override"] = first["duration"] + 2.0  # stretch one picture
    tl = replan(tl)
    others = [(i["id"], round(i["duration"], 2)) for i in tl["items"] if i["keep"]][1:]
    assert others == before[1:]  # the rest keep their lengths
    assert tl["total"] == pytest.approx(total + 2.0, abs=0.05)  # the month simply gets longer
    assert "music" in tl["note"]  # and says how it compares with the music


def test_without_manual_timing_stretching_one_picture_refits_the_rest(tmp_path, cfg):
    tl = month_with_song(tmp_path, cfg)
    total = tl["total"]
    first = next(i for i in tl["items"] if i["keep"])
    first["duration_override"] = first["duration"] + 2.0
    tl = replan(tl)
    assert tl["total"] == pytest.approx(total, abs=0.3)  # automatic: month stays the length of the music


def test_new_items_get_a_natural_length_and_release_goes_back_to_automatic(tmp_path, cfg):
    tl = set_manual_timing(month_with_song(tmp_path, cfg), True)
    victim = next(i for i in tl["items"] if i["keep"])
    victim["duration_override"] = None  # e.g. a brand-new photo from a rescan
    tl = replan(tl)
    lim = tl["limits"]
    assert next(i for i in tl["items"] if i["id"] == victim["id"])["duration"] == pytest.approx(
        (lim["photo_min"] + lim["photo_max"]) / 2, abs=0.01
    )
    tl = set_manual_timing(tl, False, release=True)
    assert tl["manual_timing"] is False
    assert all(i["duration_override"] is None for i in tl["items"] if i["kind"] != "card")
    assert tl["fits"] is True


@pytest.mark.parametrize("delta", [+12.0, -12.0])
def test_render_follows_the_pictures_not_the_music_in_manual_timing(tmp_path, cfg, delta):
    from test_features import duration

    tl = set_manual_timing(month_with_song(tmp_path, cfg), True)
    music = tl["audio_clips"][0]["src_out"] - tl["audio_clips"][0]["src_in"]
    items = [i for i in tl["items"] if i["keep"]]
    share = delta / len(items)
    for i in items:
        i["duration_override"] = max(0.5, i["duration"] + share)
    tl = replan(tl)
    out = render_month(tl, tmp_path, tmp_path / "manual_timing.mp4", encoder="libx264")
    assert duration(out) == pytest.approx(tl["total"], abs=0.4)  # longer than the song: silence; shorter: song is cut
    assert abs(tl["total"] - music) > 5
