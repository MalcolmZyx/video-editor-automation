"""Generate fake months of media (photos, clips, songs, duplicates) for testing without real files."""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .ingest import find_tool

SR = 44100


def make_photo(path: Path, seed: int, size=(1280, 720), quality=92) -> None:
    """Visually distinct synthetic photo: random gradient plus random shapes."""
    rng = random.Random(seed)
    w, h = size
    c1 = np.array([rng.randrange(256) for _ in range(3)], dtype=float)
    c2 = np.array([rng.randrange(256) for _ in range(3)], dtype=float)
    t = np.linspace(0, 1, w)[None, :, None]
    base = (c1 * (1 - t) + c2 * t) * np.ones((h, 1, 1))
    img = Image.fromarray(base.astype("uint8"))
    d = ImageDraw.Draw(img)
    for _ in range(rng.randrange(6, 14)):
        x, y = rng.randrange(w), rng.randrange(h)
        r = rng.randrange(40, 260)
        col = tuple(rng.randrange(256) for _ in range(3))
        box = (x, y, x + r, y + rng.randrange(40, 260))
        (d.ellipse if rng.random() < 0.5 else d.rectangle)(box, fill=col)
    img.save(path, quality=quality)


def make_song(
    path: Path, intro=4.0, music=40.0, outro=3.0, bpm=120, seed=0, chorus=(10.0, 30.0)
) -> dict:
    """WAV with silent intro, beat-clicked 'music', quiet outro. Returns ground-truth info."""
    rng = np.random.default_rng(seed)
    total = intro + music + outro
    n = int(total * SR)
    sig = np.zeros(n)
    tt = np.arange(n) / SR
    base = 220 * (1 + 0.25 * (seed % 4))
    in_music = (tt >= intro) & (tt < intro + music)
    sig += 0.2 * np.sin(2 * np.pi * base * tt) * in_music
    # louder tone during the chorus so section analysis has something to find
    in_chorus = (tt >= intro + chorus[0]) & (tt < intro + chorus[1])
    sig += 0.25 * np.sin(2 * np.pi * base * 2 * tt) * in_chorus
    step = 60.0 / bpm
    beats = []
    b = intro
    while b < intro + music:
        i = int(b * SR)
        k = min(2000, n - i)
        sig[i : i + k] += 0.6 * np.exp(-np.arange(k) / 300) * np.sin(2 * np.pi * 90 * np.arange(k) / SR)
        beats.append(round(b, 4))
        b += step
    sig += rng.normal(0, 0.0005, n) * (~in_music)  # near-silence, not digital zero
    pcm = (np.clip(sig, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return {
        "file": path.name,
        "music_start": intro,
        "music_end": intro + music,
        "beats": beats,
        "sections": [(intro + chorus[0], intro + chorus[1])],
    }


def make_video(path: Path, seconds: float, seed: int) -> None:
    cmd = [
        find_tool("ffmpeg"), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={300 + seed * 40}:duration={seconds}",
        "-vf", f"hue=h={seed * 37 % 360}", "-pix_fmt", "yuv420p", "-shortest", str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)


def generate_month(
    root: Path, n_photos=12, n_dupes=3, n_videos=2, n_songs=1, seed=0, with_video=True
) -> dict:
    """Create root/{photos,videos,music}. Returns ground truth for tests."""
    for sub in ("photos", "videos", "music"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    truth: dict = {"duplicates": [], "photos": [], "videos": [], "songs": []}
    for i in range(n_photos):
        p = root / "photos" / f"IMG_{seed:02d}{i:03d}.jpg"
        make_photo(p, seed * 1000 + i)
        truth["photos"].append(p.name)
    for i in range(n_dupes):
        src = root / "photos" / truth["photos"][i]
        dup = root / "photos" / f"IMG_{seed:02d}{n_photos + i:03d}.jpg"
        if i % 2 == 0:
            shutil.copy(src, dup)  # byte-identical
        else:
            Image.open(src).save(dup, quality=60)  # re-compressed, visually identical
        truth["duplicates"].append((src.name, dup.name))
    if with_video:
        for i in range(n_videos):
            v = root / "videos" / f"VID_{seed:02d}{i:03d}.mp4"
            make_video(v, 3.0 + 2 * i, seed * 10 + i)
            truth["videos"].append(v.name)
    for i in range(n_songs):
        truth["songs"].append(make_song(root / "music" / f"song{i + 1}.wav", seed=seed + i))
    return truth


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--months", type=int, default=1)
    args = ap.parse_args()
    for m in range(args.months):
        generate_month(args.out / f"{m + 1:02d}", seed=m)
    print(f"generated {args.months} fake month(s) in {args.out}")
