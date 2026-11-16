"""Local review/editing UI: a tiny standard-library web server (no extra dependencies).

Binds to 127.0.0.1 only. Edits go through each month's timeline.json via replan(). POSTs must
be JSON (a browser page on another site cannot send that without our permission) and the Host
header must be local, which blocks cross-site and DNS-rebinding tricks.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageOps

from . import themes
from .combine import combine, combined_sources, is_combined, uncombine
from .fonts import list_fonts, resolve_font
from .ingest import PHOTO_EXT, VIDEO_EXT, find_tool, is_own_output
from .pipeline import (
    AUDIO_EXT,
    add_layer,
    add_song,
    build_timeline,
    insert_card,
    item_src,
    list_songs,
    load_config,
    load_timeline,
    refresh_timeline,
    remove_song,
    replan,
    save_timeline,
    set_manual_timing,
    song_path,
    split_item,
    to_manual,
)
from .render import render_month
from .year import build_year, month_folders

UI = Path(__file__).with_name("ui.html")
THUMB = (320, 180)
CHUNK = 1 << 20


def is_month_dir(d: Path) -> bool:
    """Is this one month's folder (not a year folder holding several months)?

    True when it has a timeline, media files directly inside, or the usual photos/videos/music
    sub-folders. Anything else with media further down is treated as a year of month folders.
    """
    if (d / "timeline.json").exists():
        return True
    if any(f.suffix.lower() in PHOTO_EXT | VIDEO_EXT and not is_own_output((f.name,)) for f in d.iterdir() if f.is_file()):
        return True
    return any((d / n).is_dir() for n in ("photos", "videos", "music", "Photos", "Videos", "Music"))


class StaleError(Exception):
    """The browser window is working on an older version of this month than the one on disk."""


RENDER_LOCK = threading.Lock()  # only one render at a time (the machine has one encoder); others wait


class Review:
    """One month: its timeline file, thumbnails and render state."""

    def __init__(self, month_dir: Path):
        self.dir = month_dir.resolve()
        self.name = self.dir.name
        self.path = self.dir / "timeline.json"
        self.lock = threading.Lock()
        self.proxy_lock = threading.Lock()
        self.warming = False
        self.render_state = {"status": "idle", "error": None, "preview": False}

    def timeline(self) -> dict:
        with self.lock:
            if not self.path.exists():
                save_timeline(build_timeline(self.dir, load_config()), self.path)
            try:
                tl = load_timeline(self.path)
            except (OSError, ValueError) as e:  # damaged file: fall back to the last backup, keep the bad one
                bak = self.path.with_suffix(".json.bak")
                if not bak.exists():
                    raise RuntimeError(f"{self.path.name} is damaged and there is no backup: {e}") from e
                self.path.replace(self.path.with_suffix(".json.corrupt"))
                shutil.copyfile(bak, self.path)
                tl = load_timeline(self.path)
                tl["restored_from_backup"] = True
            tl.setdefault("rev", 0)  # the browser sends this back so a stale window can be spotted
            return tl

    def save(self, tl: dict) -> dict:
        with self.lock:
            on_disk = 0
            if self.path.exists():
                try:
                    on_disk = load_timeline(self.path).get("rev", 0)
                except (OSError, ValueError):
                    on_disk = 0
            if "rev" in tl and tl["rev"] != on_disk:
                raise StaleError(
                    "stale: this month was changed in another window or tab. Reload the page to see the latest."
                )
            tl = replan(tl)
            tl["rev"] = on_disk + 1
            save_timeline(tl, self.path)
            return tl

    def _write(self, tl: dict) -> None:
        tl["rev"] = tl.get("rev", 0) + 1
        save_timeline(tl, self.path)

    def safe(self, rel: str) -> Path | None:
        if rel.startswith("@"):  # a file of another month that a combined month points at
            p = (self.dir.parent / rel[1:]).resolve()
            ok = p.suffix.lower() in PHOTO_EXT | VIDEO_EXT and self.dir.parent.resolve() in p.parents
            return p if ok and p.is_file() else None
        p = (self.dir / rel).resolve()
        return p if p.is_file() and self.dir in p.parents else None

    unassigned_root: Path | None = None  # the year's UNASSIGNED-MUSIC folder, set by App

    def unassigned_songs(self) -> list[dict]:
        """Songs waiting in the year's UNASSIGNED-MUSIC folder (not tied to any month yet)."""
        root = self.unassigned_root
        if not root or not root.is_dir():
            return []
        return [
            {"file": p.resolve().as_posix(), "name": p.name}
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix.lower() in AUDIO_EXT and not p.name.startswith(".")
        ]

    def claim_song(self, ref: str) -> dict:
        """Move a song from UNASSIGNED-MUSIC into this month's music folder and add it to the month."""
        if is_combined(self.dir):
            raise ValueError("a combined month has no music folder; add the song to one of its source months first")
        src = next((Path(u["file"]) for u in self.unassigned_songs() if u["file"] == ref), None)
        if src is None or not src.is_file():
            raise ValueError("that song is not in the UNASSIGNED-MUSIC folder")
        dest = self.dir / "music" / src.name
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{src.stem}_{n}{src.suffix}")
            n += 1
        dest.parent.mkdir(exist_ok=True)
        shutil.move(str(src), str(dest))
        self.timeline()
        with self.lock:
            tl = refresh_timeline(load_timeline(self.path), self.dir)  # analyses the new song, adds it to the playlist
            self._write(tl)
            return tl

    def release_song(self, ref: str) -> dict:
        """Take a song out of this month and put its file back into UNASSIGNED-MUSIC."""
        root = self.unassigned_root
        if not root:
            raise ValueError("there is no UNASSIGNED-MUSIC folder in the year folder")
        src = self.dir / ref
        if ref.startswith("music/") is False or not src.is_file() or self.dir not in src.resolve().parents:
            raise ValueError("only songs in this month's music folder can be sent back")
        dest = root / src.name
        n = 1
        while dest.exists():
            dest = root / f"{src.stem}_{n}{src.suffix}"
            n += 1
        with self.lock:
            tl = remove_song(load_timeline(self.path), ref)
            tl["tracks"] = [t for t in tl["tracks"] if t["file"] != ref]
            self._write(replan(tl))  # the month no longer mentions the song before its file leaves
        shutil.move(str(src), str(dest))
        return self.timeline()

    def song(self, ref: str) -> Path | None:
        """A song file, but only one this month actually lists (month folder or library)."""
        tl = self.timeline()
        allowed = {s["file"] for s in list_songs(self.dir, tl)} | {t["file"] for t in tl["tracks"]}
        allowed |= {u["file"] for u in self.unassigned_songs()}
        if ref not in allowed:
            return None
        p = song_path(self.dir, ref)
        return p if p.is_file() else None

    def thumb(self, rel: str, src_in: float = 0.0) -> Path | None:
        src = self.safe(rel)
        if not src:
            return None
        key = hashlib.sha1(f"{rel}{src.stat().st_mtime_ns}{src_in}".encode()).hexdigest()[:16]
        out = self.dir / ".thumbs" / f"{key}.jpg"
        if out.exists():
            return out
        out.parent.mkdir(exist_ok=True)
        if src.suffix.lower() in VIDEO_EXT:
            subprocess.run(
                [find_tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{src_in:.2f}", "-i", str(src),
                 "-frames:v", "1", "-vf", f"scale={THUMB[0]}:-2", str(out)],
                check=True,
            )  # fmt: skip
        else:
            with Image.open(src) as im:
                if im.format == "JPEG":
                    im.draft("RGB", (THUMB[0] * 2, THUMB[1] * 2))  # decode at reduced size: far faster
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail(THUMB)
                im.save(out, quality=80)
        return out

    def media_list(self) -> list[dict]:
        """Every photo/clip file in this month's folder (cheap: names only, no analysis)."""
        out = []
        for p in sorted(self.dir.rglob("*")):
            rel = p.relative_to(self.dir)
            if any(part.startswith(".") or part == "music" for part in rel.parts) or not p.is_file() or is_own_output(rel.parts):
                continue
            ext = p.suffix.lower()
            if ext in PHOTO_EXT or ext in VIDEO_EXT:
                out.append({"id": rel.as_posix(), "kind": "photo" if ext in PHOTO_EXT else "video"})
        return out

    def rescan(self) -> None:
        """Pick up files that were moved in/out of the folder, if this month has been planned."""
        with self.lock:
            if self.path.exists() and not is_combined(self.dir):
                self._write(refresh_timeline(load_timeline(self.path), self.dir))

    def used_ids(self) -> list[str]:
        """Files this month currently plays (cheap read of the saved timeline, empty if not planned yet)."""
        if not self.path.exists():
            return []
        try:
            return [item_src(i) for i in load_timeline(self.path)["items"] if i["keep"] and i["kind"] != "card"]
        except Exception:  # noqa: BLE001 - a damaged file just means "nothing marked as used"
            return []

    @staticmethod
    def place(tl: dict, ids: list[str], before_id: str | None) -> dict:
        """Put the items `ids` (kept) right before the item `before_id`, or at the end when there is none."""
        moving = [i for i in tl["items"] if i["id"] in ids]
        for it in moving:
            it["keep"] = True
        rest = [i for i in tl["items"] if i["id"] not in ids]
        at = next((n for n, i in enumerate(rest) if i["id"] == before_id), len(rest))
        tl["items"] = rest[:at] + moving + rest[at:]
        return tl

    def take_from(self, other: Review, ids: list[str], before_id: str | None = None) -> dict:
        """Move (cut + paste) files from another month's folder into this one; both months re-scan."""
        moved, failure = [], None
        for rel in ids:
            src = other.safe(rel)
            if not src or src.suffix.lower() not in PHOTO_EXT | VIDEO_EXT:
                raise ValueError(f"not a photo/clip of {other.name}: {rel}")
            sub = "photos" if src.suffix.lower() in PHOTO_EXT else "videos"
            dest = self.dir / sub / src.name
            n = 1
            while dest.exists():  # never overwrite: add a suffix instead
                dest = self.dir / sub / f"{src.stem}_from-{other.name}{'' if n == 1 else f'-{n}'}{src.suffix}"
                n += 1
            try:
                dest.parent.mkdir(exist_ok=True)
                shutil.move(str(src), str(dest))
            except OSError as e:  # keep what already moved consistent, report the rest
                failure = f"moved {len(moved)} of {len(ids)}; stopped at {src.name}: {e}"
                break
            moved.append({"id": dest.relative_to(self.dir).as_posix(), "from": other.name, "rel": rel})
        other.rescan()
        self.timeline()  # make sure this month has a timeline
        with self.lock:
            tl = refresh_timeline(load_timeline(self.path), self.dir)
            tl.setdefault("borrowed", []).extend(moved)
            if before_id is not None:
                tl = replan(self.place(tl, [m["id"] for m in moved], before_id or None))
            self._write(tl)
        if failure:
            raise OSError(failure)
        return tl

    def send_back(self, other_of: dict, ids: list[str]) -> dict:
        """Undo a borrow: move the files back to the month (and path) they came from."""
        tl = self.timeline()
        touched = []
        keep = []
        for b in tl.get("borrowed", []):
            if b["id"] not in ids:
                keep.append(b)
                continue
            src, home = self.safe(b["id"]), other_of.get(b["from"])
            if not src or not home:
                keep.append(b)
                continue
            dest = home.dir / b["rel"]
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}_returned{dest.suffix}")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            except OSError:
                keep.append(b)  # could not move it (file in use?): it stays borrowed, nothing is lost
                continue
            touched.append(home)
        for h in touched:
            h.rescan()
        with self.lock:
            tl = load_timeline(self.path)
            tl["borrowed"] = keep
            tl = refresh_timeline(tl, self.dir)
            self._write(tl)
            return tl

    def group_themes(self, group: bool, strictness: str = themes.DEFAULT_STRICTNESS) -> dict:
        """Group the KEPT photos/clips by what they show (duplicates were already dropped), or undo it."""
        tl = self.timeline()
        if not group:
            return self.save(themes.ungroup(tl))
        if not themes.available():
            raise RuntimeError('Theme grouping needs: pip install -e ".[themes]"')
        ids, entries = [], []
        for it in tl["items"]:
            if it["keep"] and it["kind"] in ("photo", "video"):
                t = self.thumb(item_src(it), it.get("src_in") or 0.0)
                if t:
                    ids.append(it["id"])
                    entries.append((t, ""))
        if len(entries) < 2:
            raise RuntimeError("Not enough kept photos/clips to group.")
        tl = themes.arrange(self.timeline(), themes.group_items(entries, strictness), ids)
        return self.save(tl)

    def proxy_image(self, rel: str, width: int = 1280) -> Path | None:
        """A web-sized JPEG of a photo for the live preview (originals can be huge or HEIC)."""
        src = self.safe(rel)
        if not src:
            return None
        key = hashlib.sha1(f"img{rel}{src.stat().st_mtime_ns}{width}".encode()).hexdigest()[:16]
        out = self.dir / ".thumbs" / f"img_{key}.jpg"
        if not out.exists():
            out.parent.mkdir(exist_ok=True)
            with Image.open(src) as im:
                if im.format == "JPEG":
                    im.draft("RGB", (width * 2, width * 2))
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((width, width))
                im.save(out, quality=85)
        return out

    def proxy_video(self, rel: str) -> Path | None:
        """A small H.264 copy of a clip so any phone codec plays (and seeks) in the browser."""
        src = self.safe(rel)
        if not src:
            return None
        key = hashlib.sha1(f"vid{rel}{src.stat().st_mtime_ns}".encode()).hexdigest()[:16]
        out = self.dir / ".proxy" / f"{key}.mp4"
        with self.proxy_lock:
            if not out.exists():
                out.parent.mkdir(exist_ok=True)
                tmp = out.with_suffix(".part.mp4")
                subprocess.run(
                    [find_tool("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vf", "scale=-2:480,fps=30",
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-g", "15", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(tmp)],
                    check=True, capture_output=True,
                )  # fmt: skip
                tmp.replace(out)
        return out

    def warm_proxies(self) -> None:
        """Build the clip proxies in the background so the editor never waits on them."""
        if self.warming:
            return
        self.warming = True

        def work():
            for it in self.timeline()["items"]:
                if it["kind"] == "video":
                    try:
                        self.proxy_video(item_src(it))
                    except (subprocess.CalledProcessError, OSError):
                        pass  # that clip just plays from the original file

        threading.Thread(target=work, daemon=True).start()

    def render(self, preview: bool) -> None:
        def work():
            try:
                with RENDER_LOCK:
                    tl = self.timeline()
                    name = "month_preview.mp4" if preview else "month.mp4"
                    render_month(tl, self.dir, self.dir / name, preview=preview)
                self.render_state = {"status": "done", "error": None, "preview": preview}
            except Exception as e:  # noqa: BLE001 - surfaced in the UI
                self.render_state = {"status": "error", "error": str(e), "preview": preview}

        if self.render_state["status"] != "running":
            self.render_state = {"status": "running", "error": None, "preview": preview}
            threading.Thread(target=work, daemon=True).start()

    def status(self) -> dict:
        def mt(n: str) -> float | None:
            f = self.dir / n
            return f.stat().st_mtime if f.exists() else None

        return {
            **self.render_state,
            "timeline_mtime": mt("timeline.json"),
            "final_mtime": mt("month.mp4"),
            "preview_mtime": mt("month_preview.mp4"),
        }


class App:
    def __init__(self, root: Path):
        root = root.resolve()
        self.root = root
        self.single = is_month_dir(root)
        if self.single:
            self.months = {root.name: Review(root)}
        else:
            self.months = {d.name: Review(d) for d in month_folders(root)}
        if not self.months:
            raise ValueError(f"no month folders with photos/videos found in {root}")
        self.year_state = {"status": "idle", "error": None, "done": 0, "total": 0, "current": "", "phase": ""}
        self.unassigned = next(
            (d for d in sorted(root.iterdir()) if d.is_dir() and d.name.upper().startswith("UNASSIGNED")), None
        ) if not self.single else None
        for r in self.months.values():
            r.unassigned_root = self.unassigned

    def month_list(self) -> list[dict]:
        replaced = {} if self.single else combined_sources(self.root)
        return [
            {"name": n, "has_timeline": r.path.exists(), "has_final": (r.dir / "month.mp4").exists(),
             "combined": r.path.exists() and is_combined(r.dir), "hidden_by": replaced.get(n)}
            for n, r in self.months.items()
        ]  # fmt: skip

    def combine(self, names: list[str]) -> str:
        if self.single:
            raise ValueError("open the whole year folder to combine months")
        dest = combine(self.root, names)
        self.months = {d.name: self.months.get(d.name) or Review(d) for d in month_folders(self.root)}
        for r in self.months.values():
            r.unassigned_root = self.unassigned
        return dest.name

    def uncombine(self, name: str) -> list[str]:
        if self.single:
            raise ValueError("open the whole year folder to combine months")
        out = uncombine(self.root, name)
        self.months = {d.name: self.months.get(d.name) or Review(d) for d in month_folders(self.root)}
        return out

    def build_year(self) -> None:
        if self.single:
            raise ValueError("open the whole year folder to build the year video")
        if self.year_state["status"] == "running":
            return

        def progress(i, n, name, phase):
            self.year_state.update(done=i, total=n, current=name, phase=phase)

        def work():
            try:
                with RENDER_LOCK:
                    build_year(self.root, progress=progress)
                self.year_state.update(status="done", error=None, phase="")
            except Exception as e:  # noqa: BLE001 - surfaced in the UI
                self.year_state.update(status="error", error=str(e))

        self.year_state = {"status": "running", "error": None, "done": 0, "total": 0, "current": "", "phase": "starting"}
        threading.Thread(target=work, daemon=True).start()

    def get(self, name: str | None) -> Review | None:
        return self.months.get(name) if name else next(iter(self.months.values()))


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _file(self, path: Path):
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            a, b, code = 0, size - 1, 200
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    a = int(m.group(1))
                    b = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:  # suffix range: last N bytes
                    a = max(0, size - int(m.group(2)))
                code = 206
            if a > b or a >= size:
                self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(b - a + 1))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            self.end_headers()
            try:
                with path.open("rb") as f:
                    f.seek(a)
                    left = b - a + 1
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser stopped reading (seeking in a video); not an error

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._local():
                return self._send(403, b"", "text/plain")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            rv = app.get(q.get("m", [None])[0])
            try:
                if u.path == "/":
                    self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
                elif u.path == "/year.mp4":
                    f = app.root / "year.mp4"
                    self._file(f) if f.is_file() else self._send(404, b"", "text/plain")
                elif u.path == "/api/months":
                    self._json(app.month_list())
                elif u.path == "/api/year_status":
                    self._json({**app.year_state, "single": app.single,
                                "year_mtime": (app.root / "year.mp4").stat().st_mtime if (app.root / "year.mp4").exists() else None})  # fmt: skip
                elif rv is None:
                    self._send(404, b"", "text/plain")
                elif u.path == "/api/timeline":
                    tl = rv.timeline()
                    rv.warm_proxies()
                    self._json(tl)
                elif u.path == "/api/fonts":
                    self._json([{k: v for k, v in f.items() if k != "path"} for f in list_fonts()])
                elif u.path == "/api/render_status":
                    self._json(rv.status())
                elif u.path == "/api/borrow":  # photos/clips in the OTHER months, to pick from
                    self._json([
                        {"name": n, "items": r.media_list()} for n, r in app.months.items() if r is not rv
                    ])  # fmt: skip
                elif u.path == "/api/media":  # every photo/clip of every month, with which are already used
                    self._json([
                        {"name": n, "items": r.media_list(), "used": r.used_ids()} for n, r in app.months.items()
                    ])  # fmt: skip
                elif u.path == "/api/songs":
                    self._json(list_songs(rv.dir, rv.timeline()))
                elif u.path == "/api/unassigned":
                    self._json({"folder": rv.unassigned_root.name if rv.unassigned_root else None, "songs": rv.unassigned_songs()})
                elif u.path == "/song":
                    f = rv.song(q.get("file", [""])[0])
                    self._file(f) if f else self._send(404, b"", "text/plain")
                elif u.path.startswith("/font/"):
                    f = resolve_font(unquote(u.path[6:]))
                    self._file(f) if f else self._send(404, b"", "text/plain")
                elif u.path.startswith("/img/"):
                    name, _, rel = unquote(u.path[5:]).partition("/")
                    r = app.get(name)
                    f = r.proxy_image(rel) if r else None
                    self._file(f) if f else self._send(404, b"", "text/plain")
                elif u.path.startswith("/vproxy/"):
                    name, _, rel = unquote(u.path[8:]).partition("/")
                    r = app.get(name)
                    f = r.proxy_video(rel) if r else None
                    self._file(f) if f else self._send(404, b"", "text/plain")
                elif u.path.startswith("/thumb/"):
                    name, _, rel = unquote(u.path[7:]).partition("/")
                    r = app.get(name)
                    t = r.thumb(rel, float(q.get("t", ["0"])[0])) if r else None
                    self._file(t) if t else self._send(404, b"", "text/plain")
                elif u.path.startswith("/media/"):
                    name, _, rel = unquote(u.path[7:]).partition("/")
                    r = app.get(name)
                    f = r.safe(rel) if r else None
                    self._file(f) if f else self._send(404, b"", "text/plain")
                else:
                    self._send(404, b"", "text/plain")
            except Exception as e:  # noqa: BLE001 - report to the browser instead of dropping the connection
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            if not self._local() or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(403, b"", "text/plain")
            u = urlparse(self.path)
            rv = app.get(parse_qs(u.query).get("m", [None])[0])
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._json({"error": "bad request (not valid JSON)"}, 400)
            if rv is None:
                return self._send(404, b"", "text/plain")
            try:
                if u.path == "/api/timeline":
                    self._json(rv.save(body))
                elif u.path == "/api/refresh":
                    with rv.lock:
                        tl = refresh_timeline(load_timeline(rv.path), rv.dir)
                        rv._write(tl)
                    self._json(tl)
                elif u.path == "/api/card":
                    tl = rv.timeline()
                    self._json(rv.save(insert_card(tl, body.get("after_id"), color=body.get("color"),
                                                    text=body.get("text", ""), seconds=body.get("seconds"))))  # fmt: skip
                elif u.path == "/api/music":
                    tl = body["timeline"] if "timeline" in body else rv.timeline()
                    act = body["action"]
                    if act == "add":
                        ref = body["file"]
                        if ref not in {s["file"] for s in list_songs(rv.dir, tl)}:
                            return self._json({"error": "unknown song"}, 400)
                        tl = add_song(tl, rv.dir, ref)
                    elif act == "remove":
                        tl = remove_song(tl, body["file"])
                    elif act == "layer_add":
                        ref = body["file"]
                        if ref not in {s["file"] for s in list_songs(rv.dir, tl)}:
                            return self._json({"error": "unknown song"}, 400)
                        tl = add_layer(tl, rv.dir, ref, float(body.get("start", 0)))
                    elif act == "to_manual":
                        tl = to_manual(tl)
                    elif act == "library":
                        tl["music"]["library"] = body.get("library") or None
                    elif act == "claim":
                        return self._json(rv.claim_song(body["file"]))
                    elif act == "release":
                        return self._json(rv.release_song(body["file"]))
                    self._json(rv.save(tl))
                elif u.path == "/api/borrow":
                    if body.get("action") == "return":
                        self._json(rv.send_back(app.months, body.get("ids", [])))
                    else:
                        other = app.months.get(body.get("from"))
                        if other is None or other is rv:
                            return self._json({"error": "choose a different month to take from"}, 400)
                        self._json(rv.take_from(other, body.get("ids", []), body.get("before_id")))
                elif u.path == "/api/timing":
                    tl = body["timeline"] if "timeline" in body else rv.timeline()
                    act = body.get("action")
                    self._json(rv.save(set_manual_timing(tl, act == "on", release=act == "release")))
                elif u.path == "/api/split":
                    tl = body["timeline"] if "timeline" in body else rv.timeline()
                    self._json(rv.save(split_item(tl, float(body["at"]))))
                elif u.path == "/api/themes":
                    self._json(rv.group_themes(body.get("action") != "ungroup", body.get("strictness") or themes.DEFAULT_STRICTNESS))
                elif u.path == "/api/render":
                    rv.render(bool(body.get("preview")))
                    self._json(rv.status())
                elif u.path == "/api/combine":
                    if body.get("action") == "split":
                        back = app.uncombine(body["name"])
                        self._json({"months": app.month_list(), "back": back})
                    else:
                        name = app.combine(body.get("months", []))
                        self._json({"months": app.month_list(), "name": name})
                elif u.path == "/api/year":
                    app.build_year()
                    self._json(app.year_state)
                else:
                    self._send(404, b"", "text/plain")
            except StaleError as e:
                self._json({"error": str(e), "stale": True}, 409)
            except Exception as e:  # noqa: BLE001 - report to the browser instead of dropping the connection
                self._json({"error": str(e)}, 500)

    return H


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # a browser tab that reloads/closes mid-response is normal; don't spam tracebacks for it
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve(root: Path, port: int = 8765) -> ThreadingHTTPServer:
    return QuietServer(("127.0.0.1", port), make_handler(App(root)))
