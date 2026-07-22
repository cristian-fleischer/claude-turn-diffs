---
description: Per-session turn-by-turn diff reports (no arg = enable if off; on/off/status)
argument-hint: "[on|off|status]"
allowed-tools: Bash(python3:*)
---

Control the turn-diffs reporter for THIS Claude Code session. The argument is
`$ARGUMENTS` (one of `on`, `off`, `status`, or empty).

Run exactly ONE of these based on the argument, then show the user the command's
output verbatim and render any link — prefer showing the `http://127.0.0.1:…`
live link first, with the `file://…` static link as fallback:

- `on`  → `python3 "${CLAUDE_PLUGIN_ROOT}/turn-diffs.py" --enable`
- `off` → `python3 "${CLAUDE_PLUGIN_ROOT}/turn-diffs.py" --disable`
- `status` → `python3 "${CLAUDE_PLUGIN_ROOT}/turn-diffs.py" --status`
- empty (no argument) → `python3 "${CLAUDE_PLUGIN_ROOT}/turn-diffs.py" --ensure`
  (enables if currently off, starts the live server if needed, otherwise shows status)

Do nothing else. Keep the reply to the result and the links.
