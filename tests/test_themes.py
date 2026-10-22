import numpy as np
import pytest

from test_server_extras import served  # noqa: F401 - fixture
from yearvideo import themes


def unit(*v):
    a = np.array(v, "f")
    return a / np.linalg.norm(a)


def test_cluster_groups_alike_pictures_and_leaves_odd_ones_out():
    rng = np.random.default_rng(1)
    base = [unit(1, 0, 0, 0), unit(0, 1, 0, 0)]
    emb = np.stack([unit(*(b + rng.normal(0, 0.05, 4))) for b in base for _ in range(4)] + [unit(0, 0, 0, 1)])
    groups, leftovers = themes.cluster(emb)
    assert sorted(map(len, groups)) == [4, 4]
    assert leftovers == [8]


def make_tl():
    def it(i, kind="photo", keep=True, taken=0.0):
        return {"id": i, "kind": kind, "keep": keep, "taken": taken}

    return {"items": [it("a", taken=1), it("b", taken=2), it("card", "card", taken=None), it("c", taken=3),
                      it("gone", keep=False, taken=4), it("d", taken=5)]}  # fmt: skip


def test_arrange_keeps_themes_together_and_leaves_cards_and_dropped_alone():
    tl = make_tl()
    ids = ["a", "b", "c", "d"]
    groups = [{"label": "food", "members": [0, 2]}, {"label": "beach", "members": [1, 3]}, {"label": "other", "members": []}]
    groups = groups[:2]
    themes.arrange(tl, groups, ids)
    assert [i["id"] for i in tl["items"]] == ["a", "c", "card", "b", "gone", "d"]
    assert {i["id"]: i["theme"] for i in tl["items"] if "theme" in i} == {"a": "food", "c": "food", "b": "beach", "d": "beach"}
    themes.ungroup(tl)
    assert [i["id"] for i in tl["items"]] == ["a", "b", "card", "c", "gone", "d"]
    assert not any("theme" in i for i in tl["items"])


def test_endpoint_only_groups_kept_items_and_ungroups(served, monkeypatch):  # noqa: F811
    c, _month = served
    tl = c.json("/api/timeline")
    drop = next(i for i in tl["items"] if i["kind"] == "photo")
    drop["keep"] = False
    c.post("/api/timeline", tl)
    seen = {}

    def fake(entries):
        seen["n"] = len(entries)
        half = len(entries) // 2
        return [{"label": "x", "members": list(range(half))}, {"label": "y", "members": list(range(half, len(entries)))}]

    monkeypatch.setattr(themes, "group_items", fake)
    monkeypatch.setattr(themes, "available", lambda: True)
    out = c.post("/api/themes", {"action": "group"})
    kept = [i for i in out["items"] if i["keep"] and i["kind"] != "card"]
    assert seen["n"] == len(kept)  # the dropped photo was not sent to the model
    assert {i["theme"] for i in kept} == {"x", "y"}
    assert "theme" not in next(i for i in out["items"] if i["id"] == drop["id"])
    back = c.post("/api/themes", {"action": "ungroup"})
    assert not any("theme" in i for i in back["items"])


@pytest.mark.skipif(not (themes.available() and (themes.model_dir() / "tokenizer.json").exists()), reason="model not installed")
def test_real_model_separates_a_dark_and_a_bright_picture(tmp_path):
    from PIL import Image

    paths = []
    for n, colour in enumerate([(250, 250, 250), (245, 245, 250), (5, 5, 10), (10, 5, 5)]):
        p = tmp_path / f"{n}.jpg"
        Image.new("RGB", (300, 200), colour).save(p)
        paths.append((p, ""))
    got = themes.group_items(paths)
    assert sum(len(g["members"]) for g in got) == 4
