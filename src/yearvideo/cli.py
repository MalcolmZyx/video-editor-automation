"""Command line: plan a month, then render it after review."""

from __future__ import annotations

import argparse
from pathlib import Path

from .fakedata import generate_month
from .pipeline import (
    build_timeline,
    load_config,
    load_timeline,
    replan,
    save_timeline,
)
from .render import render_month
from .server import serve
from .year import build_year


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="yearvideo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="scan a month folder and write timeline.json for review")
    p.add_argument("month_dir", type=Path)
    p.add_argument("--mode", choices=["fit", "loop_cut", "multi_song", "shared_song"])
    p.add_argument("--config", type=Path)
    r = sub.add_parser("replan", help="recompute after editing keep/duration/mode in timeline.json")
    r.add_argument("month_dir", type=Path)
    v = sub.add_parser("review", help="open the local review UI for a month")
    v.add_argument("month_dir", type=Path)
    v.add_argument("--port", type=int, default=8765)
    y = sub.add_parser("year", help="plan/render every month folder and join into year.mp4")
    y.add_argument("year_dir", type=Path)
    y.add_argument("-o", "--out", type=Path)
    m = sub.add_parser("demo", help="make fake photos/videos/songs, plan them, and open the review UI")
    m.add_argument("dir", type=Path, nargs="?", default=Path("output/demo"))
    m.add_argument("--months", type=int, default=3)
    m.add_argument("--no-review", action="store_true")
    m.add_argument("--port", type=int, default=8765)
    d = sub.add_parser("render", help="render timeline.json to mp4")
    d.add_argument("month_dir", type=Path)
    d.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "year":
        print(f"rendered {build_year(a.year_dir, a.out)}")
        return
    if a.cmd == "demo":
        names = ["01-January", "02-February", "03-March", "04-April", "05-May", "06-June"]
        cfg = load_config()
        for n in range(a.months):
            month = a.dir / names[n % len(names)]
            generate_month(month, n_photos=14, n_dupes=3, n_videos=3, seed=n)
            save_timeline(build_timeline(month, cfg), month / "timeline.json")
        print(f"fake year created in {a.dir}")
        print(f"  render it all:  yearvideo year {a.dir}")
        print(f"  review a month: yearvideo review {a.dir / names[0]}")
        if a.no_review:
            return
        a.cmd, a.month_dir = "review", a.dir / names[0]
    if a.cmd == "review":
        import webbrowser

        server = serve(a.month_dir, a.port)
        url = f"http://127.0.0.1:{a.port}/"
        print(f"review UI at {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    tl_path = a.month_dir / "timeline.json"
    if a.cmd == "plan":
        tl = build_timeline(a.month_dir, load_config(a.config), a.mode)
        save_timeline(tl, tl_path)
    elif a.cmd == "replan":
        tl = replan(load_timeline(tl_path))
        save_timeline(tl, tl_path)
    else:
        tl = load_timeline(tl_path)
        out = render_month(tl, a.month_dir, a.out or a.month_dir / "month.mp4")
        print(f"rendered {out}")
        return
    kept = sum(1 for i in tl["items"] if i["keep"])
    print(f"{tl['month']}: kept {kept}/{len(tl['items'])} items, {tl['total']:.1f}s, fits={tl['fits']}")
    if tl["note"]:
        print("note:", tl["note"])


if __name__ == "__main__":
    main()
