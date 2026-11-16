"""Dedupe: find near 1:1 duplicate photos and flag merely similar ones for review.

Nothing is deleted. Every dropped item carries a reason so the review UI can show it
and the user can restore it.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageOps

from .cache import FileCache
from .ingest import MediaItem

DUP_DISTANCE = 2  # phash bits (of 64): effectively the same picture
SIMILAR_DISTANCE = 10  # same scene / burst: shown to user, never auto-dropped


@dataclass
class DedupeResult:
    keep: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)  # id -> reason
    similar_groups: list[list[str]] = field(default_factory=list)  # for review UI


def _hash(path: Path) -> int:
    """64-bit perceptual hash as an integer. JPEGs are decoded at reduced size, which is much faster."""
    with Image.open(path) as im:
        if im.format == "JPEG":
            im.draft("L", (256, 256))
        return int(str(imagehash.phash(ImageOps.exif_transpose(im))), 16)


def _hashes(root: Path, photos: list[MediaItem], cache: FileCache) -> dict[str, int]:
    out: dict[str, int] = {}
    todo = []
    for i in photos:
        h = cache.get(i.id, root / i.id, "hash")
        if h is None:
            todo.append(i)
        else:
            out[i.id] = int(h, 16)

    def one(i: MediaItem):
        try:
            return i.id, _hash(root / i.id)
        except Exception:  # noqa: BLE001 - a picture we cannot hash is just never treated as a duplicate
            return i.id, None

    if todo:
        with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
            for rel, h in pool.map(one, todo):
                if h is not None:
                    out[rel] = h
                    cache.put(rel, root / rel, "hash", f"{h:016x}")
    return out


def _close_pairs(values: np.ndarray, limit: int) -> list[tuple[int, int]]:
    """All index pairs (a < b) whose hashes differ in at most `limit` bits (chunked, so memory stays small)."""
    n = len(values)
    pairs: list[tuple[int, int]] = []
    for lo in range(0, n, 256):
        x = values[lo : lo + 256, None] ^ values[None, :]
        bits = np.unpackbits(x.view(np.uint8).reshape(x.shape[0], n, 8), axis=-1).sum(axis=-1)
        for r, c in np.argwhere(bits <= limit):
            if lo + r < c:
                pairs.append((lo + int(r), int(c)))
    return pairs


def _rank(item: MediaItem) -> tuple:
    # keep the highest-resolution, then largest, then earliest-named copy
    return (-(item.width * item.height), -item.size_bytes, item.id)


def dedupe_photos(root: Path, items: list[MediaItem], cache: FileCache | None = None) -> DedupeResult:
    res = DedupeResult()
    cache = cache or FileCache(root)
    hashes = _hashes(root, [i for i in items if i.kind == "photo"], cache)
    cache.save()
    photos = [i for i in items if i.kind == "photo" and i.id in hashes]
    values = np.array([hashes[i.id] for i in photos], dtype=np.uint64)
    close = {lim: _close_pairs(values, lim) for lim in (DUP_DISTANCE, SIMILAR_DISTANCE)} if len(photos) > 1 else {DUP_DISTANCE: [], SIMILAR_DISTANCE: []}

    # union-find over "duplicate" and "similar" relations
    def groups(limit: int) -> list[list[MediaItem]]:
        parent = {i.id: i.id for i in photos}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a_idx, b_idx in close[limit]:
            parent[find(photos[a_idx].id)] = find(photos[b_idx].id)
        out: dict[str, list[MediaItem]] = {}
        for i in photos:
            out.setdefault(find(i.id), []).append(i)
        return [g for g in out.values() if len(g) > 1]

    dup_groups = groups(DUP_DISTANCE)
    for g in dup_groups:
        best = min(g, key=_rank)
        for i in g:
            if i is not best:
                res.dropped[i.id] = f"duplicate of {best.id}"
    for g in groups(SIMILAR_DISTANCE):
        ids = sorted(i.id for i in g)
        if len({i for i in ids if i not in res.dropped}) > 1:
            res.similar_groups.append(ids)

    res.keep = [i.id for i in items if i.id not in res.dropped]
    return res
