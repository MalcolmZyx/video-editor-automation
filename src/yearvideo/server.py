"""Local review UI for one month: a tiny standard-library web server (no extra dependencies).

Binds to 127.0.0.1 only. All edits go through timeline.json via replan().
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

from PIL import Image, ImageOps

from .ingest import find_tool
from .pipeline import load_timeline, refresh_timeline, replan, save_timeline
from .render import render_month

UI = Path(__file__).with_name("ui.html")
THUMB = (320, 180)


class Review:
    def __init__(self, month_dir: Path):
        self.dir = month_dir.resolve()
        self.path = self.dir / "timeline.json"
        self.lock = threading.Lock()
        self.render_state = {"status": "idle", "error": None}

    def safe(self, rel: str) -> Path | None:
        p = (self.dir / rel).resolve()
        return p if p.is_file() and self.dir in p.parents else None

    def thumb(self, rel: str, src_in: float = 0.0) -> Path | None:
        src = self.safe(rel)
        if not src:
            return None
        key = hashlib.sha1(f"{rel}{src.stat().st_mtime_ns}{src_in}".encode()).hexdigest()[:16]
        out = self.dir / ".thumbs" / f"{key}.jpg"
        if out.exists():
            return out
        out.parent.mkdir(exist_ok=True)
        if src.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv", ".avi"}:
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

    def render(self) -> None:
        def work():
            try:
                with self.lock:
                    tl = load_timeline(self.path)
                render_month(tl, self.dir, self.dir / "month.mp4")
                self.render_state = {"status": "done", "error": None}
            except Exception as e:  # noqa: BLE001 - surfaced in the UI
                self.render_state = {"status": "error", "error": str(e)}

        if self.render_state["status"] != "running":
            self.render_state = {"status": "running", "error": None}
            threading.Thread(target=work, daemon=True).start()


def make_handler(rv: Review):
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
            data = path.read_bytes()
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if rng and (rng.group(1) or rng.group(2)):
                a = int(rng.group(1) or 0)
                b = int(rng.group(2)) if rng.group(2) else len(data) - 1
                b = min(b, len(data) - 1)
                self._send(206, data[a : b + 1], ctype, {
                    "Content-Range": f"bytes {a}-{b}/{len(data)}", "Accept-Ranges": "bytes"})  # fmt: skip
            else:
                self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

        def do_GET(self):
            from urllib.parse import parse_qs, unquote, urlparse

            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/timeline":
                with rv.lock:
                    self._json(load_timeline(rv.path))
            elif u.path == "/api/render_status":
                self._json({**rv.render_state, "has_output": (rv.dir / "month.mp4").exists()})
            elif u.path.startswith("/thumb/"):
                t = rv.thumb(unquote(u.path[7:]), float(q.get("t", ["0"])[0]))
                self._file(t) if t else self._send(404, b"", "text/plain")
            elif u.path.startswith("/media/"):
                f = rv.safe(unquote(u.path[7:]))
                self._file(f) if f else self._send(404, b"", "text/plain")
            else:
                self._send(404, b"", "text/plain")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n) if n else b""
            if self.path == "/api/timeline":
                with rv.lock:
                    tl = replan(json.loads(body))
                    save_timeline(tl, rv.path)
                self._json(tl)
            elif self.path == "/api/refresh":
                with rv.lock:
                    tl = refresh_timeline(load_timeline(rv.path), rv.dir)
                    save_timeline(tl, rv.path)
                self._json(tl)
            elif self.path == "/api/render":
                rv.render()
                self._json(rv.render_state)
            else:
                self._send(404, b"", "text/plain")

    return H


def serve(month_dir: Path, port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Review(month_dir)))
    return server
