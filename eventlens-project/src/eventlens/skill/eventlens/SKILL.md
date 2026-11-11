---
name: eventlens
description: Count how many people stopped near a spot (stand, booth, poster, standee, display) in a fixed-camera or time-lapse video, with the local `eventlens` tool. Covers project setup, fast person detection, camera moves and blackouts, the "nearby" zone, dwell rules, evidence review, accuracy checks and the final report. Use when the user gives event or street footage and asks how many people stopped, visited, gathered or lingered, or wants such a count checked, reviewed or re-run with other rules. Not for identifying who someone is, face recognition, or editing videos.
---

# EventLens

EventLens turns fixed-camera or time-lapse footage into a count of **stays near a spot**
(人次, visits), with every number traceable to evidence frames and a clear status:
machine estimate, spot-checked, or fully reviewed. The heavy work runs once per video
(about 60 frames/s on an Apple-silicon Mac); everything after that recounts in seconds.

Talk to the user in their language. Run commands yourself; never ask the user to type
commands or edit JSON. Show them images, not coordinates.

## Ground rules

- **Local only.** Never upload videos, frames or reports to any outside service or model
  unless the user explicitly asks for that specific upload. Downloading the detection
  model (~36 MB, official YOLOX release) or the demo sample needs the user's OK first.
- **Counts are visits, not unique people.** Someone who leaves for longer than
  `merge_gap_s` (default 30 s of real time) and comes back counts again. Say so.
- **Visible behaviour only.** Report that people stopped nearby; do not claim interest,
  intent, scans or website visits. Scan or visit numbers need the website's own logs.
- **Status words are not optional.** Never present an unreviewed number as "the count".
  Use the report's `headline` and `state` verbatim in meaning:
  - `machine_estimate`: "machine estimate, not reviewed"
  - `spot_checked`: "estimate corrected by partial review (N confirmed, M pending)"
  - `fully_reviewed`: "every record reviewed"
- **Your judgement is a suggestion.** Record your own review decisions with
  `--by agent`. Use `--by human` only for a decision the user stated. Never convert one
  into the other. Agent suggestions never change confirmed counts.
- **No invented accuracy.** Accuracy exists only after `eventlens eval` on labelled
  windows. Without that, say it has not been measured.

## 0. Check the tool

```bash
eventlens doctor
```

- `eventlens` missing: if the user has the EventLens package, run its `install.ps1`
  (Windows) or `install.sh` (macOS/Linux); otherwise install the wheel or source folder
  they point to: `uv tool install <eventlens-*.whl | path>`.
- FFmpeg missing: Windows `winget install Gyan.FFmpeg`, macOS `brew install ffmpeg`;
  or set `EVENTLENS_FFMPEG` / `EVENTLENS_FFPROBE` to the binaries.
- Model missing: ask the user, then `eventlens models fetch` (sha256 is verified).
- Every command takes `--project DIR` (or `EVENTLENS_PROJECT`), and `--json` for parsing.

## 1. Ask only what the video cannot tell you

Ask in one short message; offer the default for each:

1. **Time-lapse or normal video?** For time-lapse: seconds between photos (often 2).
   This is critical: real time = raw frames × interval, never the playback duration.
2. **Where is "nearby"?** You will propose a zone on an image; they only confirm.
3. **Minimum stay?** Default 4 s of real time (3 photos at 2 s). Anything shorter is
   treated as walking past.
4. **Anyone to exclude?** Organisers, staff, vendors standing there all day.
5. **Known clock time of the first frame?** Optional; marked unverified unless confirmed.

Record their answers in the project with the commands below, and repeat the
real-time coverage back to them after adding each video.

## 2. Set up and analyse

```bash
eventlens init --name "<event name>"
eventlens video add "<file>" --timelapse-interval 2      # time-lapse, 2 s per photo
eventlens video add "<file>" --realtime --sample-every 2 # normal video, sample every 2 s
eventlens analyze                                         # one pass per video; cached
```

`analyze` prints progress and ETA. It is resumable and keyed to the video content,
model bytes and detector settings, so re-running is instant and a moved video only
needs `eventlens video relink <video> <new path>`.

## 3. Camera views, blackouts, moves

```bash
eventlens segments suggest <video>
eventlens frames <video> --at <b-2>:<b+3>    # for every suggested boundary b
```

Look at each boundary sheet yourself, then show the user the ones that matter and
confirm: blackouts (camera fell over, lens covered), camera moves, temporary views.
Then `eventlens segments suggest <video> --apply`, or set them by hand:

```bash
eventlens segments add <video> --kind camera --camera cam1 --start 0 --end 3090
eventlens segments add <video> --kind blackout --start 200 --end 212
eventlens segments add <video> --kind skip --start 3090 --end 3101 --note "moving camera"
```

Frames are raw indices from 0; intervals are `[start, end)`. Uncovered frames,
blackouts and skips are unobservable and never back-filled. Identities are never
matched across segment boundaries. If two videos show the same camera position,
`eventlens camera rename v2-cam1 v1-cam1` merges them so they share one zone.

## 4. Person-like objects (do this before trusting any count)

```bash
eventlens statics
```

Standees, posters, mannequins and dark doorways get detected as people and become
one long fake visit. Open the image it writes. For each candidate the user agrees is
an object, run the `exclude_command` it prints (`eventlens zone exclude <cam> --box ...`).
If unsure, ask; a vendor may really stand there all day (then they are staff, see §1).

## 5. The "nearby" zone

```bash
eventlens zone show <cam>                                   # frame with a 0-1 grid
eventlens zone show <cam> --points "x,y x,y x,y x,y"        # preview, not saved
eventlens zone set <cam> --points "x,y x,y x,y x,y"
```

Read coordinates off the grid (x right, y down, 0-1). The zone is where **feet** are,
so draw it on the ground around the spot. Show the preview image and get a yes.
Do this for every camera; without a zone the whole frame counts and the report warns.

## 6. Count and explain the rules

```bash
eventlens count
eventlens rules show
eventlens rules set min_dwell_s=6 merge_gap_s=60
```

Explain rules in real seconds (`rules show` does the conversion). The report includes
a sensitivity table where every row is a full recount; use it to show how much the
number depends on the minimum stay, and say it is not an error bar.

## 7. Review

```bash
eventlens visits list --sort priority --limit 30
eventlens visits show <key> [<key> ...]          # evidence strip: context + crops
eventlens review set <key> --decision confirm|reject|duplicate|unsure --by human|agent [--of <key>] [--note "..."]
eventlens review add --video <v> --camera <c> --start <f> --end <f> --x <0-1> --y <0-1> --note "missed"
eventlens review undo <key> --by human
eventlens review status
```

Flags explain why a record is worth checking: `long_static` (object?), `merged`
(two pieces joined: same person?), `crowded`, `short`, `near_boundary` (may be
double-counted across a cut), `truncated_start/end`, `low_score`.

A good loop: pre-screen strips yourself and record `--by agent` suggestions, then
show the user the doubtful ones in batches of 5-10 and record their answers
`--by human`. Reviews survive any recount; rule changes never erase them.

## 8. Measure accuracy (before promising a number)

```bash
eventlens gt propose --debug 2 --test 1 --length 60 --apply   # per camera: typical, crowded, held-out
eventlens gt sheet <window>                                    # numbered boxes per frame
eventlens gt add <window> --start <f> --end <f> --frame <f> --det <n>   # or --x --y for missed people
eventlens gt done <window>
eventlens eval --role debug      # tune rules on these
eventlens eval --role test       # report these; never tune on them
```

Labels must come from the user (or be confirmed by them). Report misses, duplicates,
false merges and false positives separately, never just "totals are close". One hour of
labels does not prove all-day de-duplication; say what was and was not checked.

## 9. Report

```bash
eventlens report            # outputs/<time>-<rules>/report.html, visits.csv, unresolved.csv, time_bins.csv, report.json
```

Give the user the headline with its status, per-video and per-camera numbers,
unobservable time, rules used, what is not verified, and the file paths. `visits.csv`
has one row per record: video, camera, raw frames, real relative time, evidence
frames, merge basis, flags and review status.

## Speed and troubleshooting

- `eventlens bench <file>` measures decode and inference speed on this machine and
  estimates the job time. Apple silicon uses CoreML automatically; NVIDIA uses CUDA if
  `onnxruntime-gpu` is installed; Windows GPUs can use `onnxruntime-directml` with
  `eventlens detector set provider=dml`.
- Changing `detector` settings (conf, model) creates a new analysis cache; old caches
  stay until deleted. Rules, zones, segments and exclusions never need re-analysis.
- `EVENTLENS_DEBUG=1` shows full tracebacks.
- Demo on public footage: `eventlens demo --dir eventlens-demo --yes` (downloads a
  24 MB CC BY 3.0 clip; ask first).
