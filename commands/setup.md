---
description: Point concert-mixlab at a folder of recordings and map its files to mic layers
argument-hint: "<recordings folder>"
---

Set up concert-mixlab for the recordings in **$ARGUMENTS**:

1. Run `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" source "$ARGUMENTS"`. This sets `source_dir` and lists the audio files with channel count, sample rate and duration.
2. Run `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" list` to find the config path, usually `~/.concert-mixlab/presets.json`.
3. Compare the files with the layers in the `raw` preset (`show --preset raw`).
   - If the file names or channel counts differ, ask the user which files are the close/spot mics and which is the main or ambient pair.
   - Then edit the `inputs` of each layer in **all** presets with `mixlab set`, e.g. `layers.close.inputs.0.file=MyLeft.wav`.
4. Optionally save named excerpts: `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" source --excerpt "Kyrie" 12:30 150`.
5. Check the chain with `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" verify --preset current --start <a music moment> --dur 20`. It should PASS.
