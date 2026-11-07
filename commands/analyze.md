---
description: Analyse and compare mix presets on the current excerpt (tone vs guide, mic usage, stereo image, loudness)
argument-hint: "[preset1,preset2,...]"
---

Use the `concert-mixlab` skill.

1. `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" list` shows the presets and the excerpt the GUI is on.
2. `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" metrics --presets ${ARGUMENTS:-<the presets in GUI state "compare">} --align`
3. Summarise in at most about 10 lines:
   - tonal balance per band against the guide
   - brightness (centroid)
   - stereo correlation and L−R balance
   - mic energy share
   - limiter activity
   - trustworthy alignment rows only (IQR < 2 ms)
4. End with 1–3 concrete, testable suggestions written as `mixlab set` commands, but do not apply them unless asked.
