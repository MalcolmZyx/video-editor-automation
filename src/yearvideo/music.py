"""Music analysis: where the real music starts/ends, beats, and rough sections.

Uses only numpy + ffmpeg (no scipy/librosa) so it installs anywhere, including locked-down
Windows machines.
"""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import numpy as np

from .ingest import find_tool
from .planner import Track

SR = 22050
N_FFT = 1024
HOP = 512
HOP_S = HOP / SR
N_BANDS = 24
SILENCE_RATIO = 0.05  # frames below this fraction of the loud level are not music


def load_audio(path: Path) -> np.ndarray:
    """Decode any audio file (mp3, wav, m4a, ...) to mono float32 at SR via ffmpeg."""
    out = subprocess.run(
        [find_tool("ffmpeg"), "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
         "-ar", str(SR), "-"],
        check=True, capture_output=True,
    )  # fmt: skip
    return np.frombuffer(out.stdout, dtype="<f4")


def _frames(y: np.ndarray) -> np.ndarray:
    n = 1 + max(0, (len(y) - N_FFT) // HOP)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    return y[idx] * np.hanning(N_FFT)


def band_energies(y: np.ndarray) -> np.ndarray:
    """(frames, bands) log energies on a log-spaced frequency grid."""
    mag = np.abs(np.fft.rfft(_frames(y), axis=1))
    edges = np.unique(np.geomspace(2, mag.shape[1] - 1, N_BANDS + 1).astype(int))
    bands = np.stack([mag[:, a:b].sum(axis=1) for a, b in itertools.pairwise(edges)], axis=1)
    return np.log1p(bands)


def music_bounds(y: np.ndarray) -> tuple[float, float]:
    """First/last time the signal is clearly music (skips intro silence and trailing quiet)."""
    rms = np.sqrt((_frames(y) ** 2).mean(axis=1))
    loud = np.percentile(rms, 95)
    active = np.flatnonzero(rms > loud * SILENCE_RATIO)
    if active.size == 0:
        return 0.0, len(y) / SR
    return float(active[0] * HOP_S), float((active[-1] + 1) * HOP_S)


def onset_envelope(feat: np.ndarray) -> np.ndarray:
    flux = np.maximum(np.diff(feat, axis=0, prepend=feat[:1]), 0).sum(axis=1)
    return flux / (flux.max() or 1.0)


def detect_beats(env: np.ndarray, bpm_range=(70.0, 180.0)) -> np.ndarray:
    """Beat times (s, relative to env start): pick tempo by autocorrelation, then best phase."""
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1 :]
    lo, hi = (int(60 / b / HOP_S) for b in reversed(bpm_range))
    if hi <= lo or len(ac) <= hi:
        return np.array([])
    lags = np.arange(lo, hi + 1)
    p0 = lags[np.argmax(ac[lo : hi + 1] * (1 + 0.1 * np.log2(lags / lo)))]
    # refine to a fractional period (whole frames are ~23 ms and would drift over a song)
    best = (-1.0, float(p0), 0.0)
    for period in np.arange(p0 - 1.0, p0 + 1.0, 0.05):
        for phase in np.arange(0.0, period, 0.5):
            idx = np.round(np.arange(phase, len(env) - 1, period)).astype(int)
            score = float(env[idx].sum())
            if score > best[0]:
                best = (score, float(period), float(phase))
    _, period, phase = best
    pos = np.round(np.arange(phase, len(env) - 1, period)).astype(int)
    # let each beat settle on the local onset peak so slow drift doesn't accumulate
    snapped = [max(0, p - 2) + int(np.argmax(env[max(0, p - 2) : p + 3])) for p in pos]
    return np.array(snapped) * HOP_S


def find_sections(
    feat: np.ndarray, beats: np.ndarray, start: float, end: float, target_len: float = 15.0
) -> list[tuple[float, float]]:
    """Split music into contiguous sections at the biggest timbre changes, snapped to beats."""
    win = int(1.0 / HOP_S)  # ~1 s windows
    a, b = int(start / HOP_S), int(end / HOP_S)
    seg = feat[a:b]
    n = len(seg) // win
    if n < 8:
        return [(start, end)]
    w = seg[: n * win].reshape(n, win, -1).mean(axis=1)
    w = (w - w.mean(axis=0)) / (w.std(axis=0) + 1e-6)
    w /= np.linalg.norm(w, axis=1, keepdims=True) + 1e-9
    ssm = w @ w.T
    half = min(8, n // 2)
    sign = np.ones((2 * half, 2 * half))
    sign[:half, half:] = -1
    sign[half:, :half] = -1
    nov = np.zeros(n)
    for i in range(half, n - half):
        nov[i] = (ssm[i - half : i + half, i - half : i + half] * sign).sum()
    k = max(2, round((end - start) / target_len))
    cuts: list[float] = []
    order = np.argsort(nov)[::-1]
    for i in order:
        if len(cuts) >= k - 1 or nov[i] <= 0.15 * nov.max():
            break
        t = start + i * win * HOP_S
        if all(abs(t - c) > target_len / 3 for c in cuts):
            cuts.append(t)
    snapped = sorted({float(beats[np.argmin(np.abs(beats - c))]) if beats.size else c for c in cuts})
    edges = [start, *[c for c in snapped if start < c < end], end]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def analyze_track(path: Path, track_id: str | None = None) -> Track:
    y = load_audio(path)
    start, end = music_bounds(y)
    feat = band_energies(y)
    a, b = int(start / HOP_S), int(end / HOP_S)
    beats = detect_beats(onset_envelope(feat[a:b])) + a * HOP_S
    sections = find_sections(feat, beats, start, end)
    return Track(
        id=track_id or path.stem,
        music_start=round(start, 3),
        music_end=round(end, 3),
        beats=[round(float(t), 3) for t in beats],
        sections=[(round(s, 3), round(e, 3)) for s, e in sections],
    )
