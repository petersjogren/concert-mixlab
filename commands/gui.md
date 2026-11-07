---
description: Start the concert-mixlab GUI (live analysis graphs + loudness-matched A/B playback) and open it in the browser
argument-hint: "[excerpt name]"
---

Start the concert-mixlab GUI:

1. Run `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab-gui"`. The first run creates the Python environment, which takes about a minute.
2. Run `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" list`. If `source_dir` is marked MISSING, ask the user where the recordings are and run `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" source <dir>`.
3. If the user named an excerpt ("$ARGUMENTS"), switch the GUI to it with `"${CLAUDE_PLUGIN_ROOT}/bin/mixlab" state --excerpt "$ARGUMENTS"`.
4. Tell the user the URL (http://127.0.0.1:8765). Mention: space = play/stop, 1–9 = switch preset, and that you can tune presets for them, which the GUI shows live.
