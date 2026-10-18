"""Dedupe: find near 1:1 duplicate photos and flag merely similar ones for review.

Nothing is deleted. Every dropped item carries a reason so the review UI can show it
and the user can restore it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import imagehash
from PIL import Image, ImageOps

from .ingest import MediaItem

DUP_DISTANCE = 2  # phash bits (of 64): effectively the same picture
SIMILAR_DISTANCE = 10  # same scene / burst: shown to user, never auto-dropped


@dataclass
class DedupeResult:
    keep: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)  # id -> reason
    similar_groups: list[list[str]] = field(default_factory=list)  # for review UI


def _hash(path: Path) -> imagehash.ImageHash:
    with Image.open(path) as im:
        return imagehash.phash(ImageOps.exif_transpose(im))


def _rank(item: MediaItem) -> tuple:
    # keep the highest-resolution, then largest, then earliest-named copy
    return (-(item.width * item.height), -item.size_bytes, item.id)


def dedupe_photos(root: Path, items: list[MediaItem]) -> DedupeResult:
    res = DedupeResult()
    photos = [i for i in items if i.kind == "photo"]
    hashes = {i.id: _hash(root / i.id) for i in photos}

    # union-find over "duplicate" and "similar" relations
    def groups(limit: int) -> list[list[MediaItem]]:
        parent = {i.id: i.id for i in photos}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a_idx, a in enumerate(photos):
            for b in photos[a_idx + 1 :]:
                if hashes[a.id] - hashes[b.id] <= limit:
                    parent[find(a.id)] = find(b.id)
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
