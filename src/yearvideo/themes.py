"""Group photos/clips by what they show (CLIP image embeddings, run locally with ONNX Runtime).

Optional: needs `pip install -e ".[themes]"` (onnxruntime + tokenizers). The model (~150 MB, OpenAI CLIP
ViT-B/32 as ONNX by Xenova) is downloaded once into the cache folder; nothing is sent anywhere after that.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

REPO = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
FILES = [
    "onnx/vision_model_quantized.onnx",
    "onnx/text_model_quantized.onnx",
    "tokenizer.json",
]
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], "f")
STD = np.array([0.26862954, 0.26130258, 0.27577711], "f")

# what a group can be called; the closest wording to a group's average picture wins
LABELS = [
    "food", "a drink", "a selfie", "friends hanging out", "a group photo", "a family", "a pet", "a dog", "a cat",
    "a car", "a night scene", "a sunset", "a beach", "a mountain", "a forest", "a city street", "a building",
    "a party", "a concert", "a sports game", "a gym workout", "a video game", "a computer screen",
    "a phone screenshot", "a meme", "a sign or text", "a document", "a kitchen", "a bedroom", "a living room",
    "a restaurant", "a shop", "a road trip", "a flight or airport", "a hike", "a campfire", "a birthday",
    "a holiday decoration", "a costume", "a baby", "a wedding", "a school or office", "a garden or flowers",
    "the sky", "water", "snow", "art or a painting", "a close-up of an object",
]  # fmt: skip

MIN_SIM = 0.74  # two pictures this alike (average over a group) belong together
LOOSE_SIM = 0.62  # a lone picture joins the closest group if at least this alike


def model_dir() -> Path:
    base = os.environ.get("YEARVIDEO_CACHE") or str(Path.home() / ".yearvideo")
    return Path(base) / "models" / "clip"


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except Exception:  # noqa: BLE001 - missing package or blocked DLL
        return False
    return True


def ensure_model() -> Path:
    d = model_dir()
    for f in FILES:
        p = d / f
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".part")
            urllib.request.urlretrieve(REPO + f, tmp)
            tmp.replace(p)
    return d


class Clip:
    def __init__(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = ensure_model()
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.vision = ort.InferenceSession(str(d / FILES[0]), opts, providers=["CPUExecutionProvider"])
        self.text = ort.InferenceSession(str(d / FILES[1]), opts, providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(str(d / FILES[2]))
        self.tok.enable_padding(length=77)
        self.tok.enable_truncation(77)

    @staticmethod
    def _prep(path: Path) -> np.ndarray:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            s = 224 / min(w, h)
            im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
            w, h = im.size
            left, top = (w - 224) // 2, (h - 224) // 2
            im = im.crop((left, top, left + 224, top + 224))
            return ((np.asarray(im, "f") / 255 - MEAN) / STD).transpose(2, 0, 1)

    def images(self, paths: list[Path]) -> np.ndarray:
        out = []
        for i in range(0, len(paths), 16):
            x = np.stack([self._prep(p) for p in paths[i : i + 16]])
            out.append(self.vision.run(None, {"pixel_values": x})[0])
        e = np.concatenate(out)
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def labels(self, words: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(["a photo of " + w for w in words])
        ids = np.array([e.ids for e in enc], "int64")
        e = self.text.run(None, {"input_ids": ids})[0]
        return e / np.linalg.norm(e, axis=1, keepdims=True)


def cluster(emb: np.ndarray, min_sim: float = MIN_SIM, loose: float = LOOSE_SIM) -> tuple[list[list[int]], list[int]]:
    """Average-linkage grouping on cosine similarity -> (groups, leftovers).

    Stray single pictures join the nearest group if alike enough; the rest are the leftovers.
    """
    n = len(emb)
    groups: list[list[int]] = [[i] for i in range(n)]
    sums = [emb[i].copy() for i in range(n)]

    def sim(a: int, b: int) -> float:  # average pairwise cosine between two groups
        return float(sums[a] @ sums[b]) / (len(groups[a]) * len(groups[b]))

    while len(groups) > 1:
        best, pair = -1.0, (0, 0)
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                s = sim(a, b)
                if s > best:
                    best, pair = s, (a, b)
        if best < min_sim:
            break
        a, b = pair
        groups[a] += groups[b]
        sums[a] = sums[a] + sums[b]
        del groups[b], sums[b]

    # lone pictures: attach to the closest bigger group if reasonably alike, else they stay "other"
    big = [g for g in groups if len(g) > 1]
    loners = [g[0] for g in groups if len(g) == 1]
    if big:
        cents = [emb[g].mean(0) for g in big]
        for i in loners[:]:
            sims = [float(emb[i] @ c / np.linalg.norm(c)) for c in cents]
            j = int(np.argmax(sims))
            if sims[j] >= loose:
                big[j].append(i)
                loners.remove(i)
    return [sorted(g) for g in big], sorted(loners)


def embed_cached(clip: Clip, entries: list[tuple[Path, str]]) -> np.ndarray:
    """Embeddings for (image path, extra key) pairs; reuses earlier results stored next to the model."""
    cache_file = model_dir().parent / "embeddings.npz"
    store: dict[str, np.ndarray] = {}
    if cache_file.exists():
        try:
            with np.load(cache_file) as z:
                store = {k: z[k] for k in z.files}
        except Exception:  # noqa: BLE001 - a damaged cache is just rebuilt
            store = {}
    keys = []
    for p, extra in entries:
        st = p.stat()
        keys.append(hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}|{extra}".encode()).hexdigest())
    todo = [i for i, k in enumerate(keys) if k not in store]
    if todo:
        new = clip.images([entries[i][0] for i in todo])
        for i, v in zip(todo, new):
            store[keys[i]] = v
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache_file, **store)
    return np.stack([store[k] for k in keys])


def group_items(entries: list[tuple[Path, str]]) -> list[dict]:
    """[{label, members: [index into entries]}] for the given (image path, key) pairs, biggest first."""
    clip = Clip()
    emb = embed_cached(clip, entries)
    groups, leftovers = cluster(emb)
    lab = clip.labels(LABELS)
    merged: dict[str, list[int]] = {}
    for g in groups:  # groups the model would call the same thing are one theme
        label = LABELS[int(np.argmax(lab @ emb[g].mean(0)))]
        merged.setdefault(label.removeprefix("a ").removeprefix("an "), []).extend(g)
    out = [{"label": k, "members": sorted(v)} for k, v in merged.items()]
    out.sort(key=lambda g: -len(g["members"]))
    if leftovers:
        out.append({"label": "other", "members": leftovers})
    return out


def _slots(tl: dict) -> list[int]:
    """Positions of the kept photos/clips; colour cards and dropped items keep their places."""
    return [n for n, it in enumerate(tl["items"]) if it["keep"] and it["kind"] in ("photo", "video")]


def _when(it: dict) -> tuple:
    return (it.get("taken") is None, it.get("taken") or 0, it["id"])


def arrange(tl: dict, groups: list[dict], ids: list[str]) -> dict:
    """Put each theme's items next to each other. `groups` members index into `ids` (kept items only).

    Themes play in order of their earliest item; inside a theme items stay in time order, and the
    leftover "other" group goes last.
    """
    by_id = {it["id"]: it for it in tl["items"]}
    placed = []
    for n, g in enumerate(groups):
        members = sorted((by_id[ids[i]] for i in g["members"]), key=_when)
        for it in members:
            it["theme"], it["theme_n"] = g["label"], n
        placed.append((g["label"] == "other", _when(members[0]), members))
    placed.sort(key=lambda t: t[:2])
    order = [it for _, _, members in placed for it in members]
    slots = _slots(tl)
    for n, it in zip(slots, order):
        tl["items"][n] = it
    return tl


def ungroup(tl: dict) -> dict:
    """Back to plain time order, forgetting the themes."""
    slots = _slots(tl)
    ordered = sorted((tl["items"][n] for n in slots), key=_when)
    for n, it in zip(slots, ordered):
        tl["items"][n] = it
    for it in tl["items"]:
        it.pop("theme", None)
        it.pop("theme_n", None)
    return tl
