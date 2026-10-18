import numpy as np
import pytest

from yearvideo.fakedata import make_song
from yearvideo.music import analyze_track
from yearvideo.planner import Item, plan_month


@pytest.fixture(scope="module")
def analysed(tmp_path_factory):
    path = tmp_path_factory.mktemp("song") / "song.wav"
    truth = make_song(path, intro=4.0, music=40.0, outro=3.0, bpm=120, chorus=(10.0, 30.0))
    return truth, analyze_track(path)


def test_finds_real_music_bounds(analysed):
    truth, track = analysed
    assert track.music_start == pytest.approx(truth["music_start"], abs=0.3)
    assert track.music_end == pytest.approx(truth["music_end"], abs=0.3)


def test_beats_are_on_the_grid(analysed):
    truth, track = analysed
    assert len(track.beats) >= 20
    true_beats = np.array(truth["beats"])
    # every detected beat lands near a real click (tempo may be reported as half/double)
    errs = [np.min(np.abs(true_beats - b)) for b in track.beats]
    assert np.median(errs) < 0.1


def test_sections_cover_music_contiguously(analysed):
    _, track = analysed
    assert len(track.sections) >= 2
    assert track.sections[0][0] == track.music_start
    assert track.sections[-1][1] == track.music_end
    for (_, e1), (s2, _) in zip(track.sections, track.sections[1:], strict=False):
        assert e1 == s2


def test_analysed_track_feeds_planner(analysed):
    _, track = analysed
    items = [Item(f"p{i}", "photo") for i in range(15)]  # 30-75s, song is ~40s
    plan = plan_month(items, [track], "fit")
    assert plan.fits
    assert plan.total == pytest.approx(track.music_end - track.music_start, abs=0.05)


def test_section_boundaries_found_at_chorus_edges(analysed):
    _, track = analysed
    cuts = [s for s, _ in track.sections[1:]]
    # the fake chorus occupies 14s-34s of the file
    assert any(abs(c - 14.0) < 3.0 for c in cuts)
    assert any(abs(c - 34.0) < 3.0 for c in cuts)
