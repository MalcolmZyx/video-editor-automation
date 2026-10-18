"""Pick the best part of a video clip: sharp, in motion, and not dark."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np

from .ingest import find_tool

W, H = 160, 90


def score_video(path: Path) -> list[float]:
    """One score per second of video (higher = more worth showing)."""
    out = subprocess.run(
        [find_tool("ffmpeg"), "-v", "error", "-i", str(path), "-vf",
         f"fps=1,scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    )  # fmt: skip
    frames = np.frombuffer(out.stdout, dtype=np.uint8).reshape(-1, H, W).astype(float)
    if len(frames) == 0:
        return []
    c = frames[:, 1:-1, 1:-1]
    lap = 4 * c - frames[:, :-2, 1:-1] - frames[:, 2:, 1:-1] - frames[:, 1:-1, :-2] - frames[:, 1:-1, 2:]
    sharp = lap.var(axis=(1, 2))
    motion = np.abs(np.diff(frames, axis=0, prepend=frames[:1])).mean(axis=(1, 2))
    sharp_n = sharp / (sharp.max() or 1.0)
    motion_n = np.minimum(motion / (np.percentile(motion, 90) or 1.0), 1.0)
    dark = np.where(frames.mean(axis=(1, 2)) < 15, 0.1, 1.0)  # lens-cap / black frames
    return [round(float(s), 4) for s in (0.6 * sharp_n + 0.4 * motion_n) * dark]


def pick_start(scores: list[float], duration: float, source_duration: float | None) -> float:
    """Start time (s) of the window of `duration` with the best total score."""
    w = math.ceil(duration)
    if not scores or w >= len(scores):
        return 0.0
    sums = [sum(scores[s : s + w]) for s in range(len(scores) - w + 1)]
    start = float(int(np.argmax(sums)))
    if source_duration is not None:
        start = min(start, max(0.0, source_duration - duration))
    return start
