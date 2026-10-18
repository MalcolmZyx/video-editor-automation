"""Command line: plan a month, then render it after review."""

from __future__ import annotations

import argparse
from pathlib import Path

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
    d = sub.add_parser("render", help="render timeline.json to mp4")
    d.add_argument("month_dir", type=Path)
    d.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "year":
        print(f"rendered {build_year(a.year_dir, a.out)}")
        return
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
