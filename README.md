# Yearly video automation

Turns a year of photos, videos and songs into one recap video. Free/open-source tools only
(Python, ffmpeg, numpy, Pillow). You review and edit everything before anything is final.

## Try it with fake data first

```powershell
.venv\Scripts\python -m yearvideo demo                 # fake photos/videos/songs in output\demo, then opens the editor
.venv\Scripts\python -m yearvideo year output\demo     # renders every fake month and joins them into output\demo\year.mp4
```

## Folder layout

```
my-year/
  01-January/
    photos/  videos/  music/        # any sub-folders work; songs for this month go in music/
  02-February/
  ...
```
Optional: a separate folder of songs (e.g. `D:\Music\Videos`) that you can pick from inside the editor.

## Workflow

```powershell
.venv\Scripts\python -m yearvideo review my-year             # editor for all months (month picker at the top)
.venv\Scripts\python -m yearvideo review my-year\01-January  # or just one month
.venv\Scripts\python -m yearvideo year   my-year             # plan + render all months, join into year.mp4
.venv\Scripts\python -m yearvideo rescan my-year\01-January  # pick up added/removed files, keeping every edit
```
Your edits live in each month's `timeline.json` (the previous save is kept as `timeline.json.bak`).
`plan` refuses to overwrite an existing timeline (use `rescan`, or `plan --force` to start over), and `year`
never re-plans a planned month, so edits are safe. Restored photos, settings, captions and music choices all
survive a rescan.

## The editor
Every setting has an **(i)** button that explains it.
- **Month settings** (top): music picker (swap songs, listen with ▶, library folder, "Edit music by hand"), lengths,
  beat sync, transitions, start/end fades, sound levels, new-caption style.
- **Storyboard**: every photo/clip in play order. Keep/drop, reorder with ▲▼, fixed length (empty = automatic,
  shows the current automatic value), per-item fades, per-join transition, colour cards (**+ card**), clip
  **Trim…** and per-clip sound.
- **Dock** (bottom): **Live preview** (plays your edits instantly - photos, clips, transitions, fades, cards,
  captions, music), or a rendered preview / final video. The timeline has lanes for clips, captions, the main
  music and any extra song layers. The right-hand panel edits whatever is selected.

## Features
- **Transitions**: cut, fade (cross-dissolve), wipes, slides... The month has a default; every individual join
  (the diamonds on the Clips row: photo -> photo, photo -> video, video -> card, ...) can have its own style, length
  and blend position (starts at / centred on / ends at the cut). Backspace on a diamond makes it a hard cut.
- **Beat sync**: `off`, `snap` (cuts pulled toward beats by a strength you choose; ~60% looks natural), or
  `on_beat` (exact). "Every Nth beat" gives calmer pacing.
- **Music**: automatic fit (`fit`, `loop_cut`, `multi_song`, `shared_song`), or **manual** ("Edit music by hand"):
  drag songs in the timeline - sideways to move, edges to trim, up/down to another lane. In manual mode the month
  length stays fixed (editable) while you slide songs around. **Song layers** play a second (third...) song at the
  same time, each with its own volume/fades.
- **Sound**: clip sound is off by default; when on it is mixed quietly *under* the music. Music and clip levels are
  separate (month-wide, plus a multiplier per clip).
- **Cards**: blank colour cards (optional centred text) anywhere in the order.
- **Keyboard**: Backspace/Delete removes the selected caption, song, clip (it goes to Dropped), card or transition;
  Ctrl+Z undoes; Space plays/pauses the live preview.
- **Captions** (transparent by default; background box optional): any installed font (with a live preview of each), drag it anywhere on the preview, rotate and resize
  with handles, outline, shadow, background box, alignment, fades. Extra fonts: drop `.ttf`/`.otf` files into `fonts/`.
- **Month fades**: picture fades from/to a colour at the very start/end of a month; music fade-in and
  fade-out-when-cut-early.

## Settings
`config/default.yaml` holds the defaults for NEW months (each month keeps its own copy in `timeline.json`).

## Development
```powershell
python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"; .venv\Scripts\pytest
python -m yearvideo.fakedata out\fake --months 3      # fake photos/videos/songs, no real media needed
```

## Group by theme (optional)

`.venv\Scripts\pip install -e ".[themes]"` adds a small picture-recognition model (OpenAI CLIP via ONNX Runtime, runs
on your CPU, nothing is uploaded). In the storyboard, **Group same moments** finds photos/clips that show the *same moment* (e.g. one event shot by
different people at different times) and puts them together. Time stays in charge: a group sits where its earliest
photo was taken and everything else keeps its time position. Strictness: very strict / strict (default) / loose; **Ungroup (by time)** restores capture order.
It only looks at items that are still *kept*, so duplicates you dropped are ignored. The model (~150 MB) downloads on
first use into `~/.yearvideo/models`.
