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
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageOps

from .fonts import list_fonts, resolve_font
from .ingest import PHOTO_EXT, VIDEO_EXT, find_tool
from .pipeline import (
    add_layer,
    add_song,
    build_timeline,
    insert_card,
    list_songs,
    load_config,
    load_timeline,
    refresh_timeline,
    remove_song,
    replan,
    save_timeline,
    song_path,
    to_manual,
)
from .render import render_month
from .year import month_folders

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
    if any(f.suffix.lower() in PHOTO_EXT | VIDEO_EXT for f in d.iterdir() if f.is_file()):
        return True
    return any((d / n).is_dir() for n in ("photos", "videos", "music", "Photos", "Videos", "Music"))


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
            return load_timeline(self.path)

    def save(self, tl: dict) -> dict:
        with self.lock:
            tl = replan(tl)
            save_timeline(tl, self.path)
            return tl

    def safe(self, rel: str) -> Path | None:
        p = (self.dir / rel).resolve()
        return p if p.is_file() and self.dir in p.parents else None

    def song(self, ref: str) -> Path | None:
        """A song file, but only one this month actually lists (month folder or library)."""
        tl = self.timeline()
        allowed = {s["file"] for s in list_songs(self.dir, tl)} | {t["file"] for t in tl["tracks"]}
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
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail(THUMB)
                im.save(out, quality=80)
        return out

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
                        self.proxy_video(it["id"])
                    except (subprocess.CalledProcessError, OSError):
                        pass  # that clip just plays from the original file

        threading.Thread(target=work, daemon=True).start()

    def render(self, preview: bool) -> None:
        def work():
            try:
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
        if is_month_dir(root):
            self.months = {root.name: Review(root)}
        else:
            self.months = {d.name: Review(d) for d in month_folders(root)}
        if not self.months:
            raise ValueError(f"no month folders with photos/videos found in {root}")

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
                elif u.path == "/api/months":
                    self._json([
                        {"name": n, "has_timeline": r.path.exists(), "has_final": (r.dir / "month.mp4").exists()}
                        for n, r in app.months.items()
                    ])  # fmt: skip
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
                elif u.path == "/api/songs":
                    self._json(list_songs(rv.dir, rv.timeline()))
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
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if rv is None:
                return self._send(404, b"", "text/plain")
            try:
                if u.path == "/api/timeline":
                    self._json(rv.save(body))
                elif u.path == "/api/refresh":
                    with rv.lock:
                        tl = refresh_timeline(load_timeline(rv.path), rv.dir)
                        save_timeline(tl, rv.path)
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
                    self._json(rv.save(tl))
                elif u.path == "/api/render":
                    rv.render(bool(body.get("preview")))
                    self._json(rv.status())
                else:
                    self._send(404, b"", "text/plain")
            except Exception as e:  # noqa: BLE001 - report to the browser instead of dropping the connection
                self._json({"error": str(e)}, 500)

    return H


def serve(root: Path, port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(App(root)))
