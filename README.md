# Yearly video automation

Turns a year of photos, videos and songs into one recap video. Free/open-source tools only
(Python, ffmpeg, numpy, Pillow). You review and edit everything before anything is final.

## Try it with fake data first

```powershell
.venv\Scripts\yearvideo demo                 # fake photos/videos/songs in output\demo, then opens the editor
.venv\Scripts\yearvideo year output\demo     # renders every fake month and joins them into output\demo\year.mp4
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
.venv\Scripts\yearvideo review my-year      # editor for all months (month picker at the top)
.venv\Scripts\yearvideo review my-year\01-January   # or just one month
.venv\Scripts\yearvideo year   my-year      # plan + render all months, join into year.mp4
```
`plan` / `replan` / `render` do the same steps from the command line. A month's first edit creates its
`timeline.json`; `year` never re-plans a month that already has one, so your edits are safe, and it only
re-renders months you changed. Added files to a month later? Click **Rescan folder** in the editor.

## The editor
Every setting has an **(i)** button that explains it. Layout:
- **Month settings** (top): music picker (swap songs, listen with ▶, choose a library folder), lengths, beat sync,
  transitions, start/end fades, sound levels, caption style.
- **Storyboard**: every photo/clip in play order. Keep/drop, reorder with ▲▼, fixed length (empty = automatic, shows
  the current automatic value), per-item fades, per-join transition, colour cards (**+ card**), clip **Trim…**
  (watch the clip, drag start/end or capture them from the playhead) and per-clip sound.
- **Dock** (bottom): preview player + timeline (clips, captions, music with beat ticks). **Quick preview** renders a
  small fast version; click the timeline to jump; add captions at the playhead and drag them / their edges.

## What the pieces do
- **Beat sync**: `off`, `snap` (nudge cuts to the nearest beat when lengths allow), `on_beat` (every cut on a beat).
- **Transitions**: cut, fade (cross-dissolve), wipes, slides... between photos/clips; override any single join.
- **Month fades**: picture fades from/to a colour at the very start/end of a month; music fade in / fade-out when a
  song is cut early.
- **Cards**: blank colour cards (optional centred text) anywhere in the order.
- **Captions**: text over the video for any time range, with fade in/out.
- **Sound**: clip sound is off by default; when on, it is mixed quietly *under* the music. Music and clip levels
  are separate (month-wide, plus a multiplier per clip).
- **Music modes** (Advanced in the music panel): `fit`, `loop_cut`, `multi_song`, `shared_song`.

## Settings
`config/default.yaml` holds the defaults for new months (each month keeps its own copy in `timeline.json`).

## Development
```powershell
python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"; .venv\Scripts\pytest
python -m yearvideo.fakedata out\fake --months 3      # fake photos/videos/songs, no real media needed
```
