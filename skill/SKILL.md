---
name: concert-mixlab
description: Use when tuning or analysing the multi-mic concert recording mix (Mozart Requiem / Brahms, Zoom recorder). Drives ~/GitHubProjects/concert-mixlab presets via CLI; the open GUI follows live.
---

# concert-mixlab — agent control

Repo: `~/GitHubProjects/concert-mixlab` · Python: `.venv/bin/python mixlab.py …` (run from the repo).
Shared truth = `presets.json` (mix settings) + `state.json` (what the GUI shows). The GUI
(`./gui.sh`, http://127.0.0.1:8765) polls both and re-analyses within ~2 s, so every CLI edit is
immediately visible/audible to the user. Never edit the JSON by hand while the GUI is open; use the CLI.

## Signal model
Layer = inputs (file, ch, pan −1..1, gain_db) → width (M/S; 1 normal, 0 mono) → delay_ms → filters → gain_db.
Layers sum → master gain_db → look-ahead limiter (limiter_ceiling linear, null = off).
Filters: `highpass`/`lowpass` {f,q} · `peak` {f,oct,g} · `highshelf`/`lowshelf` {f,g,slope}; `"bypass": true`.
Layers here: `close` (ZOOM0001_INPUT1/2 mono spaced pair, hard L/R) and `camera` (ZOOM0001_MIC stereo).

## Commands
```bash
P=.venv/bin/python
$P mixlab.py list                                   # presets, excerpts, GUI state
$P mixlab.py show --preset current --short          # readable chain with filter indices
$P mixlab.py clone current try1 --description "why"  # ALWAYS experiment on a clone, never on user presets
$P mixlab.py set --preset try1 layers.close.width=0.7 layers.close.filters.3.g=12 layers.camera.gain_db=-8
$P mixlab.py set --preset try1 'layers.close.filters.4={"type":"peak","f":9000,"oct":1,"g":2}'   # append (index = len)
$P mixlab.py set --preset try1 layers.close.filters.1=delete          # remove; layers.camera.mute=true
$P mixlab.py state --excerpt Lacrimosa --compare raw,current,try1 --edit try1   # steer the GUI
$P mixlab.py metrics --presets current,try1 [--align]                  # compact JSON (uses GUI excerpt by default)
$P mixlab.py report --presets current,try1 --out out/x                 # static HTML report
$P mixlab.py render --preset try1 --start 39:08 --dur 180 --fade 3 --out x.wav
$P mixlab.py ffmpeg --preset try1 --start 0 --dur 0 --out full.wav     # whole-concert ffmpeg command
$P mixlab.py verify --preset try1 --dur 30                             # python vs ffmpeg null (< -60 dB = PASS)
```
`--start/--dur` default to the GUI's current excerpt. Excerpts: "Brahms - Warum (full)" 7:36+530,
"Lacrimosa" 39:08+180, "Sanctus" 49:40+92 (also 60 s variants).

## Reading metrics (targets are rules of thumb, not standards)
- `tilt_rel_0p5_2k` vs `guide`: 2–4k ≈ −7, 4–8k ≈ −14, 8–12k ≈ −20, 12–16k ≈ −26, 250–500 ≈ +1. Raw close pair is ~15–20 dB too dark above 4 kHz.
- `centroid_hz` ↑ = brighter. `corr` 0.3–0.7 natural image; ≈0 = hole-in-middle risk; <0 phasey.
- `balance_db` (L−R) ≠ 0 → image leans; INPUT2 is ~4.6 dB hotter than INPUT1 in the raw files.
- `layer_sum_gain_db_per_octave` < 0 → layers cancel (check delay); `gr_time_pct_over_1db` > ~5 → limiter squashing.
- Alignment: trust only IQR < 2 ms (sparse/a-cappella excerpts, e.g. Brahms). Measured camera lag ≈ 11.1–11.6 ms.

## Workflow
1. `list` + `state` to see what the user is looking at. 2. `clone` the user's preset. 3. Change one idea at a time with `set`.
4. `metrics --presets <base>,<clone>` and compare against the guide. 5. Point the GUI at it with `state --compare … --edit …`
so the user can A/B by ear (keys 1–9, loudness-matched). 6. Report numbers + what to listen for; let the user's ears decide.
Pitfalls: big shelves (> +12 dB) mostly add hiss; EQ on the spaced pair lowers correlation — counter with width 0.7–0.8 or more camera.
