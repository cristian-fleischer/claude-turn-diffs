# turn-diffs

**Live, per-session turn-by-turn diff viewer for [Claude Code](https://claude.com/claude-code).**

Parses the session transcript and renders each turn — prompt, tool process, file diffs, subagent
work, final answer — as a self-contained HTML page that updates live as the session runs. An
optional composer types prompts back into your terminal. Off by default; opt in per session with
`/turn-diffs`.

![turn-diffs in action](docs/demo.gif)

---

## Features

- **Per-turn layout** — prompt → collapsed **Process** (tool calls, thinking markers, narration) →
  **file diffs** → subagent panels → **Answer**. A `◆ N` badge marks turns that changed files.
- **Unified & side-by-side diffs** — syntax highlighting, collapsed unchanged context, per-file collapse.
- **Live streaming** — SSE push, morphs only changed nodes (no flicker), mid-turn updates;
  auto-reloads itself when the tool is upgraded.
- **Sessions sidebar** — every session with a live status dot: working (pulsing) / finished (green) /
  seen (gray) / blocked (red).
- **Prompt composer** — markdown editor with slash-command autocomplete (project/user/plugin
  commands, skills, built-ins) that types into the session's terminal pane.
- **Review comments** — click any diff line or a whole file to comment; comments compile with
  `file:line` locations into a prompt shown live in the composer and sent to the agent on demand.
- **Filters** — Regular / Starred / Hidden and **Changes only**; star/hide per turn; accordion
  (Ctrl+Click). State persisted per session.
- **Mobile-friendly** — edge-to-edge on narrow screens; works over Tailscale from a phone.
- **Self-contained** — one stdlib-only Python file; reports inline everything; works from `file://`
  (the server adds live push, the sidebar, and the composer).

---

## Requirements

- Claude Code · Python 3.8+ (stdlib only) · a modern browser
- *Composer only:* a multiplexer — **Herdr** (exact), **tmux** (heuristic), or **Zellij** (pinned).
  Plain terminals: run one inside.

---

## Install

**Plugin (recommended):**

```
/plugin marketplace add cristian-fleischer/claude-turn-diffs
/plugin install turn-diffs@turn-diffs
```

Reload Claude Code — registers the `Stop` + `PostToolUse` hooks and `/turn-diffs`.

**Manual:** copy `turn-diffs.py` + `assets/` (e.g. to `~/.claude/turn-diffs/`), add a `Stop` hook
and a `/turn-diffs` command (see `hooks/hooks.json`, `commands/turn-diffs.md`). Or run directly:

```
python3 turn-diffs.py --enable   # enable for this session, print the link
python3 turn-diffs.py --serve    # live server on http://127.0.0.1:8787
```

---

## Usage

```
/turn-diffs          # enable + start server + print link
/turn-diffs off      # disable for this session
/turn-diffs status   # state + links
```

Open the printed `http://127.0.0.1:8787/<session>.html` (or the `file://` fallback). Reports live in
`~/.claude/turn-diffs/reports/<session>.html` (`$TURN_DIFFS_DIR` to override).

---

## Live mode & composer

`--serve` runs a loopback `http.server` that serves the reports + an index, pushes SSE on change,
serves `/sessions` and `/commands`, and exposes token-guarded `POST /prompt/<session>` (types a
prompt into the session pane). Over http a composer appears; `/` autocompletes commands. `Enter`
sends, `Shift`+`Enter` newline (reversed on touch; `Ctrl`/`Cmd`+`Enter` always sends).

### Multiplexer support

| Backend | Session → pane |
|---|---|
| **Herdr** | Exact — panes advertise the session id; target shown as `workspace › tab` |
| **tmux** | Heuristic — pane command + cwd |
| **Zellij** | Manual pin only |
| **Ghostty / plain** | Run a multiplexer inside |

Pin ambiguous/Zellij mappings in `~/.claude/turn-diffs/panes.json`:

```json
{ "<session-id>": { "backend": "zellij", "target": "<zellij-session>" } }
```

### Security

`POST /prompt` types into your terminal, so it is guarded by:

- **Host allowlisting** (anti DNS-rebinding): `127.0.0.1` / `localhost` / `[::1]` → local (no token);
  `*.ts.net` or `TURN_DIFFS_ALLOWED_HOSTS` → remote (**token required**); anything else → `403`.
- **Token auth for remote**: token from `~/.claude/turn-diffs/serve-token` (`0600`, 192-bit); the
  tokenised URL from `/turn-diffs status` sets an `HttpOnly; SameSite=Strict` cookie; checked with
  `hmac.compare_digest`.
- **Output escaping**: all rendered text is escaped; markdown links limited to `http(s):` / `mailto:`
  / relative; injected prompts are control-char-stripped and bracketed-paste wrapped.

Single-user local tooling. Expose only to a tailnet you control — never `tailscale funnel`.

---

## How it works

Reads only `~/.claude/projects/*/<session>.jsonl`, replays `Read`/`Edit`/`Write` to reconstruct each
file's before/after, groups by turn, renders HTML. `Stop` regenerates per turn; `PostToolUse` and
the server's transcript-tailing give mid-turn updates. Fully local.

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TURN_DIFFS_DIR` | `~/.claude/turn-diffs` | reports + runtime state (plugin: `${CLAUDE_PLUGIN_DATA}`) |
| `TURN_DIFFS_PORT` | `8787` | live server port |
| `TURN_DIFFS_ALLOWED_HOSTS` | `*.ts.net` | extra proxy `Host` values, comma-separated |
| `TURN_DIFFS_DEBUG` | unset | log hook failures to `<data dir>/hook.log` |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code config dir |

### Commands

| Flag | Purpose |
|---|---|
| `--enable` / `--disable` / `--status` / `--ensure` | per-session reporting (`/turn-diffs`) |
| `--serve [--port N]` / `--stop` | run / stop the live server |
| `--prune` | delete stale reports (also automatic per turn) |
| `--version` | print the version |

Auto-prune: reports older than 30 days or beyond the newest 40, except still-enabled sessions.

### Browser shortcuts

| Key | Action |
|---|---|
| `Ctrl`+`↑` / `↓` | previous / next turn |
| any letter (nothing focused) | type straight into the composer |
| `Enter` / `Shift`+`Enter` | send / newline (reversed on touch) |
| `Ctrl`+`Enter` | always send |

---

## Development

Stdlib `unittest`:

```
python3 -m unittest discover -s tests -v
```

---

## Credits

Bundled libraries (licenses under `assets/`, see `assets/THIRD-PARTY-NOTICES.md`):
[highlight.js](https://github.com/highlightjs/highlight.js) (BSD-3-Clause) ·
[EasyMDE](https://github.com/Ionaru/easy-markdown-editor) (MIT) ·
[morphdom](https://github.com/patrick-steele-idem/morphdom) (MIT)

## License

[MIT](LICENSE) © Cristian Fleischer
