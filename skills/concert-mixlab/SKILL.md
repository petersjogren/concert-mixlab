---
name: concert-mixlab
description: Use when mixing, analysing or tuning multi-mic concert recordings (orchestra/choir/soloists, e.g. a Zoom recorder with a close pair + camera/ambient pair): EQ, mic balance, time alignment, stereo image, loudness. Drives concert-mixlab presets via CLI; the open GUI follows live so the user can A/B by ear.
---

# concert-mixlab: agent control

CLI: `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" <command>`. Below, `M` means that command. It sets up its own venv on first run (needs ffmpeg).
GUI: `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab-gui"` (http://127.0.0.1:8765). Use `--no-open` to start it without opening a browser and `--stop` to stop it.

Shared truth is in `~/.concert-mixlab/` (override with `MIXLAB_HOME`):
- `presets.json` holds the mix settings and excerpts.
- `state.json` holds what the GUI shows: excerpt, compared presets and the edited preset.

The GUI polls both and re-analyses within about 2 s. Every CLI edit is therefore immediately visible and audible to the user. Never hand-edit the JSON while the GUI is open; use the CLI.

## Signal model
- A layer is: inputs (file, ch, pan −1..1, gain_db) → width (M/S; 1 = normal, 0 = mono) → delay_ms → filters → gain_db.
- Layers are summed, then master gain_db, then a look-ahead limiter (`limiter_ceiling` is linear; null = off).
- Filters:
  - `highpass`/`lowpass` {f, q}
  - `peak` {f, oct, g}
  - `highshelf`/`lowshelf` {f, g, slope}
  - any filter can carry `"bypass": true`
- Biquads are the RBJ formulas ffmpeg uses, so `M ffmpeg` reproduces a preset exactly (`M verify` should report a null below −60 dB).
- Typical layers are `close` (spaced spot pair, hard L/R) and `camera` (stereo recorder/camera mic, used as ambience). Run `M show --preset raw --short` to see the real ones.

## Commands
```bash
M list                                   # config path, source_dir, presets, excerpts, GUI state
M source [DIR] [--excerpt NAME START DUR] # set recordings folder / save excerpt / list files
M show --preset current --short          # readable chain with filter indices
M clone current try1 --description "why" # ALWAYS experiment on a clone, never on user presets
M set --preset try1 layers.close.width=0.7 layers.close.filters.3.g=12 layers.camera.gain_db=-8
M set --preset try1 'layers.close.filters.4={"type":"peak","f":9000,"oct":1,"g":2}'   # append (index = len)
M set --preset try1 layers.close.filters.1=delete                                       # remove; layers.camera.mute=true
M state --excerpt NAME --compare raw,current,try1 --edit try1   # steer the GUI
M metrics --presets current,try1 [--align]                      # compact JSON (GUI excerpt by default)
M report --presets current,try1 [--out DIR] [--render]          # static HTML report (+ WAVs)
M render --preset try1 --start 39:08 --dur 180 --fade 3 --out x.wav
M ffmpeg --preset try1 --start 0 --dur 0 --out full.wav         # whole-recording ffmpeg command
M verify --preset try1 --dur 30                                 # python vs ffmpeg null test
M set --preset try1 layers.camera.invert=true                    # polarity flip (layer or input)
M daw --preset try1 [--json]                                    # plain DAW recipe: track fader/pan, bus delay/EQ(Q)/width/fader, limiter
```
`--start/--dur` default to the GUI's current excerpt. Times accept `mm:ss`. Edited presets are saved standalone (their `extends` is dropped).

## Reading metrics (targets are rules of thumb, not standards)
- Compare `tilt_rel_0p5_2k` with `guide`:
  - 250–500 ≈ +1
  - 2–4k ≈ −7
  - 4–8k ≈ −14
  - 8–12k ≈ −20
  - 12–16k ≈ −26
- `centroid_hz` going up means brighter.
- `corr`: 0.3–0.7 is a natural image; ≈0 risks a hole in the middle; <0 sounds phasey.
- `balance_db` (L−R) ≠ 0 means the image leans. Fix it with per-input `gain_db`.
- `layer_sum_gain_db_per_octave` < 0 means the layers cancel; check the delay.
- `gr_time_pct_over_1db` above about 5 means the limiter is squashing.
- `layer_alignment` (raw lag per layer, falls back to the last trustworthy measurement in `~/.concert-mixlab/alignment.json`) and per-preset `alignment_after_delays.residual_ms` (raw lag − applied delay; aim for |residual| < 0.1 ms; `first_notch_hz` = comb notch). This recording: camera +11.33 ms after close, so the close delay should be ≈11.33 ms (current 11 ms leaves +0.33 ms → notch ≈1.5 kHz).
- `layer_alignment.polarity` = −1 / `alignment_after_delays.polarity_ok` = false → the layers are polarity-inverted relative to each other: set `layers.<layer>.invert=true` (this recording: camera is Ø-inverted vs close in ~90% of windows; fixing it turns −0.3…−0.8 dB LF cancellation into +0.3…+0.6 dB summation, ≈ +1 dB bass 31–250 Hz). Inputs also accept `invert`.
- `clipped_pct` > 0 with the limiter off = audible clipping; lower master gain.
- Alignment: trust only rows with IQR < 2 ms. Measure on sparse or a-cappella excerpts; dense tutti gives garbage. Delay the closer layer by the measured lag.

## Workflow
1. Run `list` and `state` to see what the user is looking at.
2. `clone` their preset.
3. Change one idea at a time with `set`.
4. Run `metrics --presets <base>,<clone>` and compare against the guide.
5. Run `state --compare <base>,<clone> --edit <clone>` so the user can A/B by ear: keys 1–9, loudness-matched.
6. Report the numbers and what to listen for. The user's ears decide.
7. When the user wants to rebuild a mix in their DAW, give them `M daw --preset <name>` output (the GUI shows the same panel for whatever preset is playing).

## Pitfalls
- Very large shelves (> +12 dB) mostly add hiss; dull close mics can't be fully fixed by EQ.
- EQ on a spaced pair lowers L/R correlation. Counter it with width 0.7–0.8 or more of the ambient pair.
- The ambient/camera layer masks the close-mic correction. Keep it about 4–8 dB down and EQ it too, including the ~330 Hz mud cut.
- Check for clipped sources (peak 0 dBFS) before boosting.
