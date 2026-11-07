---
description: Iteratively tune a mix toward a goal (e.g. "brighter choir, keep the centre solid") on a cloned preset, then line it up in the GUI for A/B listening
argument-hint: "<goal in plain words> [base preset]"
---

Use the `concert-mixlab` skill. Goal: **$ARGUMENTS**

1. `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" list`. The base preset is the one named in the goal, else the GUI's `edit` preset, else `current`.
2. Clone it with `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" clone <base> <short-name> --description "<goal>"`. Never modify the user's own presets.
3. Loop at most 5 times:
   - Change one idea with `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" set --preset <clone> …`
   - Run `metrics --presets <base>,<clone>`
   - Keep the change if it moves the measured numbers toward the goal without breaking anything else: correlation 0.3–0.7, |L−R| < 1.5 dB, no extra limiter squashing, no hiss-level shelves above +12 dB.
4. Put the result in front of the user's ears with `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" state --compare <base>,<clone> --edit <clone>`. Start the GUI with `mixlab-gui --no-open` if it isn't running.
5. Report a before/after table of the key numbers, the final chain (`show --preset <clone> --short`), and what to listen for. Remind the user they press 1/2 to A/B.
