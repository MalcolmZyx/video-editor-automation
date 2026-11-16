"""Per-folder cache of what scanning learned about each file, so a rescan only looks at new/changed files.

Stored as a hidden `.yv_cache.json` in the month folder (hidden files are never scanned as media). Entries
are keyed by the path inside the folder and are valid only while the file's size and modified time match.
A damaged cache is simply ignored and rebuilt: it can never lose anything, it only saves time.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

NAME = ".yv_cache.json"


class FileCache:
    def __init__(self, folder: Path | None):
        self.path = folder / NAME if folder else None
        self.data: dict[str, dict] = {}
        self.dirty = False
        self.lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
            except (OSError, ValueError):
                self.data = {}

    @staticmethod
    def stamp(path: Path) -> list[int]:
        st = path.stat()
        return [st.st_mtime_ns, st.st_size]

    def get(self, rel: str, path: Path, field: str):
        """The cached value of `field` for this file, or None if unknown or the file changed."""
        e = self.data.get(rel)
        if not e or e.get("stamp") != self.stamp(path):
            return None
        return e.get(field)

    def put(self, rel: str, path: Path, field: str, value) -> None:
        with self.lock:
            stamp = self.stamp(path)
            e = self.data.get(rel)
            if not e or e.get("stamp") != stamp:
                e = self.data[rel] = {"stamp": stamp}
            e[field] = value
            self.dirty = True

    def forget_missing(self, rels: set[str]) -> None:
        with self.lock:
            for k in [k for k in self.data if k not in rels]:
                del self.data[k]
                self.dirty = True

    def save(self) -> None:
        if not (self.path and self.dirty):
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass  # a cache that cannot be written just means the next scan is slower
