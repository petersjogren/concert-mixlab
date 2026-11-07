# concert-mixlab

Quantify how mix/EQ settings change a multi-mic concert master: spectrum, EQ, mic usage, stereo image, loudness/limiter.

## GUI
`./gui.sh` → http://127.0.0.1:8765. Excerpt picker, compare presets, live editor (sliders save to presets.json),
all analysis plots, gapless loudness-matched A/B playback (space = play, 1–9 = switch). Follows external edits
to presets.json/state.json (e.g. from Claude Code via the `concert-mixlab` skill in `skill/SKILL.md`).

## Agent / CLI control
`mixlab.py list | show | clone | set | delete | state | metrics` — see `skill/SKILL.md`.

```bash
cd ~/GitHubProjects/concert-mixlab
P=.venv/bin/python

# compare all presets on the Lacrimosa (HTML report + metrics.json, + WAVs with --render)
$P mixlab.py report --start 39:08 --dur 180 --out out/lacrimosa --render
$P mixlab.py report --start 7:36 --dur 530 --presets raw,current --out out/brahms

$P mixlab.py render --preset current --start 39:08 --dur 180 --fade 3 --out lacrimosa.wav
$P mixlab.py ffmpeg --preset current --start 0 --dur 0 --out full_concert.wav   # equivalent ffmpeg command
$P mixlab.py verify --preset current --start 39:08 --dur 30                    # python DSP == ffmpeg?
```

## Presets (`presets.json`)
- `extends` inherits another preset; layers are merged **by name**, so you can override just one field.
- Layer: `inputs` (file, ch, pan −1..1, gain_db), `width` (M/S, 1 = unchanged, 0 = mono), `delay_ms`,
  `filters` (`highpass`/`lowpass` f,q · `peak` f,oct,g · `highshelf`/`lowshelf` f,g,slope; `"bypass": true`), `gain_db`, `mute`.
- Master: `gain_db`, `limiter_ceiling` (linear, null = off), `attack_ms`, `release_ms`.

## Report sections
1. Settings per preset  2. Summary (LUFS, peak, crest, centroid, L/R corr, S/M, balance, limiter GR, mic energy share)
3. EQ curves, tonal table vs rough guide, spectra, delta vs first preset  4. Mic usage per octave + coherent-sum gain + time alignment (GCC-PHAT)
5. Stereo: correlation & S/M per octave, vectorscope  6. Short-term loudness + limiter GR timeline  7. Equivalent ffmpeg commands

Biquads are the RBJ formulas ffmpeg uses (verified null ≈ −77 dB). The limiter is a look-ahead approximation of
ffmpeg `alimiter`, not bit-exact.
