# Yearly video automation

Turns a year of photos, videos and songs into one recap video. Free/open-source tools only
(Python, ffmpeg, numpy, Pillow). You review and edit everything before anything is final.

## Try it with fake data first

```powershell
.venv\Scripts\yearvideo demo          # makes fake photos/videos/songs in output\demo, plans them, opens the review UI
.venv\Scripts\yearvideo year output\demo   # renders every fake month and joins them into output\demo\year.mp4
```

## Folder layout

```
my-year/
  01-January/
    photos/  videos/  music/        # any sub-folders work; songs go in music/
  02-February/
  ...
```

## Workflow

```powershell
.venv\Scripts\yearvideo plan   my-year\01-January     # scan, drop duplicates, analyse songs -> timeline.json
.venv\Scripts\yearvideo review my-year\01-January     # open the review UI (browser)
.venv\Scripts\yearvideo render my-year\01-January      # month.mp4 (or use the Render button in the UI)
.venv\Scripts\yearvideo year   my-year                 # plan+render all months, join into year.mp4
```

`year` never re-plans a month that already has a `timeline.json`, so your edits are safe, and it only
re-renders months you changed.

## Review UI
- Untick **keep** to drop an item; dropped items (e.g. duplicates, with the reason) stay listed so you can restore them.
- **length** pins how long a photo/clip shows (blank = automatic). **start at** picks where a video clip begins.
- ▲▼ reorder. **Rescan folder** picks up files you added or removed without losing your edits.
- Music mode: `fit` (whole song, real music only), `loop_cut` (stop after a chorus/section, fade out, top up with
  another song if needed), `multi_song` (chain songs), `shared_song` (one song across months).

## Video sound
Clips' own sound is off by default. Turn it on for everything (Month settings) or per clip (the **sound**
dropdown: default / keep / mute, plus a volume box). While a clip's sound plays the music dips to the
configured level and recovers smoothly.

## Fades
`config/default.yaml` -> `fade:` sets picture fade in/out length and colour at the start/end of each month and
the music fade in / fade-out-when-cut-early. Each month copies these into its `timeline.json` and the
**Month settings** panel edits them. Any single photo/clip can also have its own **fade in/out** seconds.

## Settings
`config/default.yaml`: photo/video min and max seconds, resolution, fps, title card, fade.

## Development
```powershell
python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"; .venv\Scripts\pytest
python -m yearvideo.fakedata out\fake --months 3      # fake photos/videos/songs, no real media needed
```
