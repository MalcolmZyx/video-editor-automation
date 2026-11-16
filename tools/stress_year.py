"""Build a big fake year folder and time the slow paths (scan, plan, borrow, send back, save).

    python tools/stress_year.py [photos_per_month] [months] [width]

Not part of the test suite (it is slow); run it by hand after changing scanning/planning code.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from yearvideo.pipeline import build_timeline, load_config, save_timeline
from yearvideo.server import Review


def make_photos(folder: Path, n: int, width: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    (folder / "photos").mkdir(parents=True, exist_ok=True)
    h = width * 3 // 4
    base = rng.integers(0, 255, (h // 16, width // 16, 3), dtype=np.uint8)
    for i in range(n):
        shift = rng.integers(0, 255, 3, dtype=np.uint8)
        small = (base.astype(np.uint16) + shift + rng.integers(0, 60, base.shape)).astype(np.uint8)
        im = Image.fromarray(small).resize((width, h), Image.BILINEAR)
        im.save(folder / "photos" / f"IMG_{seed}{i:04d}.jpg", quality=85)


def timed(label: str, fn):
    t = time.perf_counter()
    out = fn()
    print(f"{label:<34}{time.perf_counter() - t:8.2f}s")
    return out


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    months = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 4032
    root = Path(tempfile.mkdtemp(prefix="yv_stress_"))
    print("folder:", root)
    names = [f"{i + 1:02d}-M{i + 1}" for i in range(months)]
    timed(f"make {months}x{n} photos {width}px", lambda: [make_photos(root / m, n, width, k) for k, m in enumerate(names)])
    cfg = load_config()
    cfg["output"] = {"resolution": [320, 180], "fps": 10}
    reviews = {m: Review(root / m) for m in names}
    for m in names:
        timed(f"first plan {m}", lambda m=m: save_timeline(build_timeline(root / m, cfg), root / m / "timeline.json"))
    a, b = reviews[names[0]], reviews[names[1]]
    timed("timeline() load", a.timeline)
    tl = a.timeline()
    timed("save()/replan", lambda: a.save(tl))
    ids = [i["id"] for i in b.timeline()["items"] if i["kind"] == "photo"][:10]
    out = timed("take 10 from next month", lambda: a.take_from(b, ids))
    timed("send 10 back", lambda: a.send_back(reviews, [x["id"] for x in out["borrowed"]]))
    timed("rescan (nothing changed)", a.rescan)
    timed("thumbs for 20 photos", lambda: [a.thumb(i["id"]) for i in a.timeline()["items"][:20]])
    timed("media_list", a.media_list)
    print("items:", len(a.timeline()["items"]))
    if os.environ.get("KEEP") != "1":
        import shutil

        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
