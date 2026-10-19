import pytest

from yearvideo.planner import Item, Limits, Track, plan_month


def photos(n):
    return [Item(f"p{i}", "photo") for i in range(n)]


def track(tid="t", start=4.0, end=124.0, step=0.5, sections=None):
    beats = [start + k * step for k in range(int((end - start) / step) + 1)]
    return Track(tid, start, end, beats, sections or [])


def test_fit_uses_real_music_only_and_sums_to_length():
    plan = plan_month(photos(40), [track()], "fit")  # 120s of music, 40 photos
    assert plan.fits
    assert plan.total == pytest.approx(120.0, abs=0.01)
    assert plan.audio[0].start == 4.0 and plan.audio[0].end == 124.0
    assert all(2.0 <= p.duration <= 5.0 for p in plan.items)


def test_fit_flags_when_song_does_not_fit():
    plan = plan_month(photos(5), [track()], "fit")  # 5 photos max 25s vs 120s song
    assert not plan.fits
    assert "too long" in plan.note


def test_loop_cut_ends_early_with_fade():
    t = track(sections=[(4.0, 64.0), (64.0, 124.0)])
    plan = plan_month(photos(15), [t], "loop_cut")  # 15 photos: 30-75s
    assert plan.fits
    assert plan.audio[0].end == 64.0 and plan.audio[0].fade_out
    assert plan.total == pytest.approx(60.0, abs=0.01)


def test_loop_cut_tops_up_with_second_song():
    a = track("a", sections=[(4.0, 34.0)])
    b = track("b", start=0.0, end=60.0)
    plan = plan_month(photos(30), [a, b], "loop_cut")  # needs 60-150s
    assert plan.fits
    assert [s.track_id for s in plan.audio] == ["a", "b"]


def test_multi_song_fills_long_month():
    tracks = [track("a", end=64.0), track("b", end=64.0), track("c", end=64.0)]  # 60s each
    plan = plan_month(photos(60), tracks, "multi_song")  # 120-300s
    assert plan.fits
    assert plan.total >= 120.0
    assert len(plan.audio) >= 2


def test_shared_song_returns_resume_offset():
    t = track(end=124.0)
    first = plan_month(photos(20), [t], "shared_song", offset=0.0)  # 40-100s
    assert first.fits and first.next_offset is not None
    second = plan_month(photos(20), [t], "shared_song", offset=first.next_offset)
    assert second.audio[0].start >= first.audio[0].end


def test_overrides_and_dropped_items_respected():
    items = photos(10)
    items[0].duration_override = 7.0
    items[1].keep = False
    plan = plan_month(items, [track(end=64.0)], "fit")
    assert plan.items[0].id == "p0"
    assert "p1" not in [p.id for p in plan.items]
    assert plan.items[0].duration == 7.0


def test_limits_configurable():
    lim = Limits(photo_min=1.0, photo_max=1.0)
    plan = plan_month(photos(30), [track(end=34.0)], "fit", lim)  # 30s song, 30 x 1s
    assert plan.fits
    assert all(p.duration == 1.0 for p in plan.items)


def test_video_never_longer_than_source():
    items = [Item("v", "video", source_duration=3.0), *photos(4)]
    plan = plan_month(items, [track(end=19.0)], "fit")  # 15s
    v = plan.items[0]
    assert v.duration <= 3.0


def test_on_beat_puts_every_cut_on_a_beat():
    t = track(end=64.0, step=0.75)  # 60s of music, beats every 0.75s
    plan = plan_month(photos(20), [t], "fit", beat_sync="on_beat")
    assert plan.fits
    beats = [b - 4.0 for b in t.beats]
    for p in plan.items[1:]:
        assert min(abs(p.start - b) for b in beats) < 0.002
    assert plan.items[-1].start + plan.items[-1].duration == pytest.approx(60.0, abs=0.01)
    assert all(2.0 - 1e-6 <= p.duration <= 5.0 + 1e-6 for p in plan.items[:-1])


def test_on_beat_resyncs_after_a_fixed_length_item():
    items = photos(12)
    items[2].duration_override = 3.1  # not a multiple of the beat
    t = track(end=44.0, step=0.5)  # 40s of music for 12 photos: plenty of slack
    plan = plan_month(items, [t], "fit", beat_sync="on_beat")
    beats = [b - 4.0 for b in t.beats]
    after = plan.items[4]  # a later cut is back on the grid
    assert min(abs(after.start - b) for b in beats) < 0.002


def test_photos_only_month_has_no_music_but_plans():
    plan = plan_month(photos(10), [], "fit")
    assert plan.fits and plan.audio == []
    assert plan.total == pytest.approx(35.0)  # midpoint 3.5s each
