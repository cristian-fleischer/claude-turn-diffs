#!/usr/bin/env python3
"""
turn-diffs.py — Review a Claude Code session's file changes grouped BY TURN.

For each of your prompts in a session, it shows the prompt followed by a single
consolidated diff of everything Claude changed in response — so you can see
exactly what each instruction produced. Outputs a self-contained HTML file
(colored diffs, a turn index, collapsible turns, optional live auto-reload).

USAGE
  python3 turn-diffs.py                       # newest session -> HTML
  python3 turn-diffs.py SESSION.jsonl         # a specific session
  python3 turn-diffs.py --list                # list recent sessions and exit
  python3 turn-diffs.py SESSION.jsonl -o out.html
  python3 turn-diffs.py --format md ...        # markdown instead of HTML

AUTO-UPDATE (two ways)
  1) Watch mode — regenerate whenever the session changes; the HTML reloads
     itself in the browser:
        python3 turn-diffs.py --watch -o ~/turn-diffs.html
     (no SESSION arg => follows the most recently active session)

  2) Stop hook — regenerate at the end of every turn with zero terminals.
     Add to ~/.claude/settings.json (see the README block printed by --hook-help):
        "Stop": [{ "hooks": [{ "type": "command", "async": true,
          "command": "python3 /ABS/turn-diffs.py --hook -o /ABS/turn-diffs.html" }]}]
     In --hook mode the script reads the hook JSON from stdin and uses its
     transcript_path automatically.

Reads only ~/.claude/projects/*. Nothing leaves your machine. No dependencies.
"""

import argparse
import difflib
import html
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
PROJECTS = CLAUDE_DIR / "projects"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_DIFF_LINES = 800
MAX_PROMPT_CHARS = 4000
MAX_AGENT_CHARS = 16000
MAX_THINK_CHARS = 8000   # per thinking block shown in the Process section
SPLIT_CONTEXT = 3   # unchanged lines kept around each change in the side-by-side view
_LINENO = re.compile(r"^\s*\d+[\t\u2192]")   # "   12<TAB>" / "   12->" prefix from Read output

# Where per-session reports and on/off flags live. Override with $TURN_DIFFS_DIR
# (the plugin build points this at ${CLAUDE_PLUGIN_DATA}); defaults under ~/.claude.
DATA_DIR = Path(os.environ.get("TURN_DIFFS_DIR", str(CLAUDE_DIR / "turn-diffs")))


# ---------------------------------------------------------------- per-session state
def reports_dir():
    return DATA_DIR / "reports"


def enabled_dir():
    return DATA_DIR / "enabled"


def report_path_for(session_id, fmt="html"):
    ext = "md" if fmt == "md" else "html"
    return reports_dir() / f"{session_id}.{ext}"


def enabled_flag(session_id):
    return enabled_dir() / session_id


def is_enabled(session_id):
    return bool(session_id) and enabled_flag(session_id).exists()


def session_id_of(path):
    return Path(str(path)).stem


def file_url(path):
    return "file://" + str(Path(path).resolve())


def session_title(entries):
    """The session's name: the user-set custom title if present, else the
    auto-generated ai-title, else ''. Uses the most recent of each."""
    custom = ai = ""
    for e in entries:
        t = e.get("type")
        if t == "custom-title" and e.get("customTitle"):
            custom = e["customTitle"]
        elif t == "ai-title" and e.get("aiTitle"):
            ai = e["aiTitle"]
    return custom or ai or ""


def current_session():
    """Best-effort 'the session running right now': the most recently written
    transcript. Invoked mid-turn (from the /turn-diffs command) the active
    session's .jsonl is the freshest, so this resolves to it. The Stop hook does
    NOT use this \u2014 it gets the authoritative session_id on stdin."""
    sessions = find_sessions()
    return sessions[0] if sessions else None


# ---------------------------------------------------------------- vendored assets
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _asset(name):
    try:
        return (ASSETS_DIR / name).read_text(encoding="utf-8")
    except Exception:
        return ""


EXT_LANG = {
    "py": "python", "pyw": "python", "js": "javascript", "mjs": "javascript",
    "cjs": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript",
    "json": "json", "sh": "bash", "bash": "bash", "zsh": "bash", "html": "xml",
    "htm": "xml", "xml": "xml", "svg": "xml", "vue": "xml", "svelte": "xml",
    "css": "css", "scss": "scss", "less": "less", "md": "markdown",
    "markdown": "markdown", "yml": "yaml", "yaml": "yaml", "toml": "ini", "ini": "ini",
    "cfg": "ini", "conf": "ini", "rs": "rust", "go": "go", "c": "c", "h": "c",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "java": "java",
    "kt": "kotlin", "rb": "ruby", "php": "php", "sql": "sql", "swift": "swift",
    "lua": "lua", "pl": "perl", "r": "r", "scala": "scala", "dart": "dart",
    "diff": "diff", "patch": "diff",
}


def lang_for(path):
    """Map a file path to a highlight.js language id ('' = let hljs auto-detect)."""
    name = Path(str(path)).name.lower()
    if name == "dockerfile":
        return "dockerfile"
    if name in ("makefile", "gnumakefile"):
        return "makefile"
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return EXT_LANG.get(ext, "")


# ---------------------------------------------------------------- parsing helpers
def find_sessions():
    if not PROJECTS.exists():
        return []
    files = list(PROJECTS.glob("*/*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def load(path):
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


_NOTIF_RE = re.compile(r"<task-notification>.*?</task-notification>", re.S)


def notification_text(e):
    """If this USER entry is a background-agent <task-notification>, return the
    block, else ''. Used only to keep notifications from becoming their own turn.
    A real block contains <task-id>; a user merely typing '<task-notification>'
    in prose does not, so it won't match."""
    if e.get("type") != "user" or e.get("isMeta"):
        return ""
    content = e.get("message", {}).get("content")
    txt = content if isinstance(content, str) else _text_of(content)
    if not txt or "<task-notification>" not in txt:
        return ""
    m = _NOTIF_RE.search(txt)
    return m.group(0) if (m and "<task-id>" in m.group(0)) else ""


def _find_notif_strings(obj, out):
    """Recursively collect any string in an entry that holds a full notification."""
    if isinstance(obj, str):
        if "<task-notification>" in obj and "</task-notification>" in obj:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _find_notif_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_notif_strings(v, out)


def collect_notifications(entries):
    """Index every <task-notification> by the Agent tool_use_id it reports, no
    matter how it was delivered (plain user text, queued/attachment entry, or
    wrapped with a [SYSTEM NOTIFICATION] prefix)."""
    out = {}
    for e in entries:
        strings = []
        _find_notif_strings(e, strings)
        for s in strings:
            for m in _NOTIF_RE.finditer(s):
                block = m.group(0)
                if "<task-id>" not in block:
                    continue
                info = parse_notification(block)
                tuid = info.get("tuid")
                if not tuid:
                    continue
                prev = out.get(tuid)
                if not prev or len(info.get("result", "")) > len(prev.get("result", "")):
                    out[tuid] = info
    return out


def parse_notification(txt):
    """Pull the agent's final answer (and a label) out of a <task-notification>."""
    def grab(tag):
        m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", txt, re.S)
        return m.group(1).strip() if m else ""
    result = grab("result")
    if len(result) > MAX_AGENT_CHARS:
        result = result[:MAX_AGENT_CHARS] + "\n…(truncated)"
    return {"label": grab("summary") or "Agent result",
            "status": grab("status"),
            "result": result,
            "tuid": grab("tool-use-id"),
            "order": [], "files": {}}


def is_user_prompt(e):
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    if notification_text(e):
        return False
    content = e.get("message", {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_tr = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        has_tx = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
        return has_tx and not has_tr
    return False


def clean_prompt(text):
    if not isinstance(text, str):
        text = _text_of(text)
    text = (text or "").strip()
    m = re.search(r"<command-name>\s*(.*?)\s*</command-name>", text, re.S)
    if m:
        arg = re.search(r"<command-args>\s*(.*?)\s*</command-args>", text, re.S)
        label = m.group(1).strip()
        if arg and arg.group(1).strip():
            label += " " + arg.group(1).strip()
        return f"(command) {label}"
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S).strip()
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS] + " ...(truncated)"
    return text


def _tool_summary(name, inp):
    """One-line summary of a tool call for the Process timeline."""
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "notebook_path", "pattern", "path",
              "description", "url", "query", "prompt", "old_string"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            s = " ".join(v.split())
            return (s[:160] + "…") if len(s) > 160 else s
    try:
        s = json.dumps(inp)
    except Exception:
        s = str(inp)
    return (s[:160] + "…") if len(s) > 160 else s


def entries_cwd(entries):
    """The session's working directory (from the transcript's cwd field)."""
    for e in entries:
        c = e.get("cwd")
        if isinstance(c, str) and c:
            return c
    return ""


def assistant_tool_uses(e):
    if e.get("type") != "assistant":
        return []
    content = e.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def tool_results(e):
    if e.get("type") != "user":
        return []
    content = e.get("message", {}).get("content")
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                inner = b.get("content")
                if isinstance(inner, list):
                    txt = "\n".join(
                        x.get("text", "")
                        for x in inner
                        if isinstance(x, dict) and x.get("type") == "text"
                    )
                elif isinstance(inner, str):
                    txt = inner
                else:
                    txt = ""
                out.append((b.get("tool_use_id"), txt))
    return out


def strip_linenos(txt):
    return "\n".join(_LINENO.sub("", ln, count=1) for ln in txt.split("\n"))


def apply_edit(content, old, new, replace_all=False):
    if old == "":
        return new + content, True
    if old not in content:
        return content, False
    if replace_all:
        return content.replace(old, new), True
    return content.replace(old, new, 1), True


def _apply_edit_tool(name, inp, rec, file_state, path):
    """Replay one edit/write tool call into rec + file_state. Shared by the main
    turn-builder and the subagent edit collector."""
    if name == "Write":
        rec["ops"].append(("write", None, inp.get("content", "")))
        file_state[path] = inp.get("content", "")
    elif name == "Edit":
        old, new = inp.get("old_string", ""), inp.get("new_string", "")
        rec["ops"].append(("edit", old, new))
        base = file_state.get(path)
        if base is None:
            rec["applied"] = False
        else:
            after, ok = apply_edit(base, old, new, inp.get("replace_all", False))
            file_state[path] = after
            rec["applied"] = rec["applied"] and ok
    elif name == "MultiEdit":
        base = file_state.get(path)
        for ed in inp.get("edits", []):
            old, new = ed.get("old_string", ""), ed.get("new_string", "")
            rec["ops"].append(("edit", old, new))
            if base is None:
                rec["applied"] = False
            else:
                base, ok = apply_edit(base, old, new, ed.get("replace_all", False))
                rec["applied"] = rec["applied"] and ok
        if base is not None:
            file_state[path] = base
    elif name == "NotebookEdit":
        rec["ops"].append(("edit", "", inp.get("new_source", "")))
        rec["applied"] = False
    rec["after"] = file_state.get(path)


def _new_rec(file_state, path):
    return {"before": file_state.get(path), "applied": True,
            "is_new": path not in file_state, "ops": [],
            "after": file_state.get(path)}


# ---------------------------------------------------------------- core: build turns
def build_turns(entries):
    file_state = {}        # path -> best-known current full content (or None)
    pending_reads = {}     # Read tool_use_id -> path
    turns = []
    cur = None

    def start_turn(prompt, ts):
        nonlocal cur
        cur = {"prompt": prompt, "ts": ts, "files": {}, "order": [],
               "agents": [], "agent_tuids": [], "answer": "", "process": [], "_pending": []}
        turns.append(cur)

    def touch_file(path):
        if path not in cur["files"]:
            cur["files"][path] = _new_rec(file_state, path)
            cur["order"].append(path)
        return cur["files"][path]

    for e in entries:
        for tid, txt in tool_results(e):
            if tid in pending_reads:
                p = pending_reads.pop(tid)
                if p not in file_state and txt:
                    file_state[p] = strip_linenos(txt)

        # a prompt the user queued while Claude was working is stored only as a
        # queued_command attachment (never a normal user message) — make it a turn
        if e.get("type") == "attachment":
            att = e.get("attachment")
            if isinstance(att, dict) and att.get("type") == "queued_command":
                qp = att.get("prompt", "")
                qp = qp if isinstance(qp, str) else _text_of(qp)
                if qp and "<task-notification>" not in qp:
                    start_turn(clean_prompt(qp), e.get("timestamp"))
            continue

        if notification_text(e):
            continue  # background-agent notification: not a turn; results gathered later

        if is_user_prompt(e):
            start_turn(clean_prompt(_text_of(e.get("message", {}).get("content"))),
                       e.get("timestamp"))
            continue

        # Split the turn's assistant text: text written BEFORE a tool call is
        # intermediate narration (-> Process); the trailing text after the LAST tool
        # call is the real Answer. Thinking + tool calls also go to the Process timeline.
        if e.get("type") == "assistant" and cur is not None:
            content = e.get("message", {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    typ = b.get("type")
                    if typ == "thinking":
                        # Claude Code stores thinking blocks with an EMPTY thinking field
                        # (signature only) — so usually a marker, occasionally real text.
                        cur["process"].append({"kind": "think",
                                               "text": (b.get("thinking") or "")[:MAX_THINK_CHARS]})
                    elif typ == "text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            cur["_pending"].append(txt)
                    elif typ == "tool_use":
                        if cur["_pending"]:   # text before this tool = narration
                            cur["process"].append({"kind": "narr", "text": "\n\n".join(cur["_pending"])})
                            cur["_pending"] = []
                        cur["process"].append({"kind": "tool", "name": b.get("name", ""),
                                               "summary": _tool_summary(b.get("name", ""),
                                                                        b.get("input", {}) or {})})
            cur["answer"] = "\n\n".join(cur["_pending"])   # trailing text after the last tool

        for tu in assistant_tool_uses(e):
            name = tu.get("name")
            inp = tu.get("input", {}) or {}
            if name in ("Agent", "Task") and cur is not None:
                tid = tu.get("id")
                if tid:
                    cur["agent_tuids"].append(tid)
                continue
            if name == "Read":
                fp = inp.get("file_path")
                if fp:
                    pending_reads[tu.get("id")] = fp
                continue
            if name not in EDIT_TOOLS or cur is None:
                continue
            path = inp.get("file_path") or inp.get("notebook_path")
            if not path:
                continue
            rec = touch_file(path)
            _apply_edit_tool(name, inp, rec, file_state, path)
    return turns


def collect_edits(entries):
    """Aggregate net file changes across an entire transcript (no turn boundaries).
    Used to summarise what a subagent changed during its whole run. Returns
    (order, files) shaped like a single turn's file map."""
    file_state = {}
    pending_reads = {}
    files = {}
    order = []
    for e in entries:
        for tid, txt in tool_results(e):
            if tid in pending_reads:
                p = pending_reads.pop(tid)
                if p not in file_state and txt:
                    file_state[p] = strip_linenos(txt)
        for tu in assistant_tool_uses(e):
            name = tu.get("name")
            inp = tu.get("input", {}) or {}
            if name == "Read":
                fp = inp.get("file_path")
                if fp:
                    pending_reads[tu.get("id")] = fp
                continue
            if name not in EDIT_TOOLS:
                continue
            path = inp.get("file_path") or inp.get("notebook_path")
            if not path:
                continue
            if path not in files:
                files[path] = _new_rec(file_state, path)
                order.append(path)
            _apply_edit_tool(name, inp, files[path], file_state, path)
    return order, files


def subagent_dir(session_path):
    sp = Path(str(session_path))
    return sp.parent / sp.stem / "subagents"


def scan_subagents(session_path):
    """Map each main-transcript Agent tool_use_id -> the edits its subagent made.
    Correlates via agent-<id>.meta.json's toolUseId. Returns {tuid: info}."""
    d = subagent_dir(session_path)
    out = {}
    if not d.exists():
        return out
    for meta in d.glob("agent-*.meta.json"):
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        tuid = info.get("toolUseId")
        if not tuid:
            continue
        jf = d / (meta.name[:-len(".meta.json")] + ".jsonl")
        order, files = ([], {})
        if jf.exists():
            try:
                order, files = collect_edits(load(jf))
            except Exception:
                order, files = ([], {})
        out[tuid] = {"agentType": info.get("agentType", ""),
                     "description": info.get("description", ""),
                     "spawnDepth": info.get("spawnDepth"),
                     "order": order, "files": files}
    return out


def attach_agents(turns, session_path, entries):
    """Build each turn's agent panels from the agents it spawned. Placement comes
    from the main transcript (which turn issued the Agent call), file diffs from the
    subagent transcript, and result text from the notification (delivered in any
    form). This catches agents whose notification was queued/attached, not just
    plain-text ones."""
    subs = scan_subagents(session_path)
    notifs = collect_notifications(entries)
    for t in turns:
        for tuid in t.get("agent_tuids", []):
            info = subs.get(tuid, {})
            notif = notifs.get(tuid, {})
            atype = info.get("agentType", "")
            desc = info.get("description", "")
            name = desc
            if not name:  # fall back to the quoted name in the notification summary
                m = re.search(r'"([^"]+)"', notif.get("label", ""))
                name = m.group(1) if m else (notif.get("label", "") or "")
            entry = {
                "label": name, "agentType": atype, "status": notif.get("status", ""),
                "result": notif.get("result", ""), "order": info.get("order", []),
                "files": info.get("files", {}), "tuid": tuid,
            }
            if entry["result"] or entry["order"] or atype:
                t["agents"].append(entry)


def file_diff_lines(rec):
    """Return (unified_diff_lines, mode) where mode is 'net' or 'hunks'."""
    before, after = rec.get("before"), rec.get("after")
    clean = rec["applied"] and after is not None and not (before is None and not rec["is_new"])
    if clean:
        diff = list(difflib.unified_diff(
            (before or "").splitlines(), after.splitlines(),
            fromfile=("(new file)" if before is None else "before this turn"),
            tofile="after this turn", lineterm=""))
        return diff, "net"
    # fallback: per-edit hunks
    blocks = []
    for kind, old, new in rec["ops"]:
        if kind == "write":
            blocks += ["+" + ln for ln in new.splitlines()]
        else:
            d = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                          fromfile="old", tofile="new", lineterm=""))
            blocks += d if d else ["(no textual change)"]
        blocks.append("")
    return blocks, "hunks"


# ---------------------------------------------------------------- markdown render
def render_md(turns, session_path, title=""):
    head = f"# {title}" if title else "# Turn-by-turn changes"
    sub = "Turn-by-turn changes · " if title else ""
    out = [head, "", f"{sub}Session: `{session_path}`", "",
           f"{len(turns)} turn(s).", "", "---", ""]
    for i, t in enumerate(turns, 1):
        ts = (t["ts"] or "")[:19].replace("T", " ")
        out.append(f"## Turn {i}" + (f"  ·  {ts}" if ts else ""))
        out.append("")
        out.append("**Prompt:**")
        for ln in (t["prompt"] or "(empty)").split("\n"):
            out.append("> " + ln if ln else ">")
        out.append("")
        if not t["order"] and not t.get("agents"):
            out += ["_No file edits in this turn._", ""]
        for path in t["order"]:
            rec = t["files"][path]
            out += [f"### `{path}`", ""]
            lines, mode = file_diff_lines(rec)
            if mode == "hunks":
                out.append("> _Prior full content not in transcript; showing each edit's change._")
                out.append("")
            if len(lines) > MAX_DIFF_LINES:
                lines = lines[:MAX_DIFF_LINES] + [f"... (truncated at {MAX_DIFF_LINES} lines)"]
            out += ["```diff"] + lines + ["```", ""]
        for ag in t.get("agents", []):
            lbl = (ag["label"] + " " if ag.get("label") else "") + (f"[{ag['agentType']}]" if ag.get("agentType") else "")
            out += [f"<details><summary>Subagent {lbl}</summary>", ""]
            if ag.get("result"):
                out += ag["result"].split("\n") + [""]
            for path in ag.get("order", []):
                rec = ag["files"][path]
                out += [f"#### `{path}` _(changed by subagent)_", ""]
                lines, _mode = file_diff_lines(rec)
                if len(lines) > MAX_DIFF_LINES:
                    lines = lines[:MAX_DIFF_LINES] + [f"... (truncated at {MAX_DIFF_LINES} lines)"]
                out += ["```diff"] + lines + ["```", ""]
            out += ["</details>", ""]
        if t.get("answer"):
            out += ["<details open><summary>Answer</summary>", "", t["answer"], "", "</details>", ""]
        out += ["---", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- HTML render
CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--card:#f6f8fa;
--add-bg:#c4f0cf;--add-fg:#116329;--del-bg:#ffd2cf;--del-fg:#82071e;
--hunk-bg:#ddf4ff;--hunk-fg:#0550ae;--accent:#0969da}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;
--line:#30363d;--card:#161b22;--add-bg:#1c4530;--add-fg:#3fb950;--del-bg:#4a2025;
--del-fg:#f85149;--hunk-bg:#121d2f;--hunk-fg:#58a6ff;--accent:#58a6ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 80px}
header{position:sticky;top:0;z-index:20;background:var(--bg);
padding:10px 0 8px;border-bottom:1px solid var(--line)}
header h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13.5px;word-break:break-all}
.badge{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:999px;
font-size:11px;background:var(--card);border:1px solid var(--line);color:var(--muted)}
.badge.live{color:#fff;background:var(--accent);border-color:var(--accent)}
nav.toc{margin:18px 0 8px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
nav.toc summary{cursor:pointer;padding:10px 14px;font-weight:600;font-size:13px}
nav.toc ol{margin:0;padding:4px 14px 12px 34px}
nav.toc li{margin:3px 0}
nav.toc a{color:var(--accent);text-decoration:none}
nav.toc a:hover{text-decoration:underline}
nav.toc .fc{color:var(--muted);font-size:11.5px;margin-left:6px}
details.turn{border:1px solid var(--line);border-radius:8px;margin:14px 0;overflow:hidden;scroll-margin-top:96px}
details.turn>summary{cursor:pointer;list-style:none;padding:12px 14px;background:var(--card);
display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
details.turn>summary::-webkit-details-marker{display:none}
.tn{font-weight:700}
.ts{color:var(--muted);font-size:11.5px}
.pin{color:var(--fg);opacity:.8;font-size:13px}
.body{padding:6px 14px 14px}
blockquote.prompt{margin:10px 0 14px;padding:10px 12px;border-left:3px solid var(--accent);
background:var(--card);border-radius:4px;white-space:pre-wrap}
.file h3{font:600 13.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
margin:16px 0 6px;color:var(--fg)}
details.file{margin:12px 0 0}
details.file>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:7px;padding:2px 0}
details.file>summary::-webkit-details-marker{display:none}
details.file>summary::before{content:'▾';color:var(--muted);font-size:11px;width:11px;text-align:center}
details.file:not([open])>summary::before{content:'▸'}
details.file>summary h3{margin:6px 0}
details.file>summary:hover h3{text-decoration:underline}
details.file:not([open])>summary{opacity:.65}
details.file:not([open])>summary::after{content:'— collapsed';color:var(--muted);font-size:11px}
.note{color:var(--muted);font-size:12px;margin:0 0 6px}
.noedit{color:var(--muted);font-style:italic;margin:8px 0}
details.answer{border:1px solid var(--line);border-radius:6px;margin:10px 0 14px;background:var(--card)}
details.answer>summary{cursor:pointer;list-style:none;padding:8px 12px;font-size:12px;
font-weight:600;color:var(--muted);display:flex;align-items:center}
details.answer>summary::-webkit-details-marker{display:none}
details.answer>summary::before{content:'▾';font-size:11px;margin-right:7px}
details.answer:not([open])>summary::before{content:'▸'}
details.answer[open]>summary{border-bottom:1px solid var(--line)}
details.answer .agent-result{padding:10px 12px}
details.agent{border:1px solid var(--line);border-radius:6px;margin:10px 0;background:var(--card)}
details.agent>summary{cursor:pointer;list-style:none;padding:8px 12px;font-size:12.5px;
display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
details.agent>summary::-webkit-details-marker{display:none}
details.agent .agk{font-weight:700;color:var(--accent)}
details.agent[open]>summary{border-bottom:1px solid var(--line)}
.agent-body{padding:10px 12px;color:var(--fg)}
.agent-result{color:var(--fg);font:14.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.agent-result p{margin:8px 0}
.agent-result a{color:var(--accent)}
.agent-result .md-h{font-weight:700;margin:13px 0 6px;line-height:1.3}
.agent-result .md-h1,.agent-result .md-h2{font-size:15px}
.agent-result .md-h3{font-size:14px}
.agent-result .md-h4,.agent-result .md-h5,.agent-result .md-h6{font-size:13px;color:var(--muted)}
.agent-result code{background:var(--card);border:1px solid var(--line);border-radius:4px;
padding:.5px 4px;font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.agent-result pre{margin:8px 0;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.agent-result pre code{display:block;padding:10px 12px;overflow:auto;border:0;background:none;
font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.agent-result ul,.agent-result ol{margin:8px 0;padding-left:22px}
.agent-result li{margin:3px 0}
.agent-result blockquote{margin:8px 0;padding:4px 12px;border-left:3px solid var(--line);color:var(--muted)}
.agent-result hr{border:0;border-top:1px solid var(--line);margin:12px 0}
.agent-result table.md-table{border-collapse:collapse;margin:8px 0;font-size:12.5px}
.agent-result table.md-table th,.agent-result table.md-table td{border:1px solid var(--line);
padding:4px 8px;text-align:left}
.agent-result table.md-table th{background:var(--card)}
/* when highlighting is on, let token colors show: drop the solid add/del text color, keep tint */
body.hl pre.diff .ln.add,body.hl pre.diff .ln.del,
body.hl table.split td.cell.add,body.hl table.split td.cell.del{color:var(--fg)}
pre.diff{margin:0;border:1px solid var(--line);border-radius:6px;overflow:auto;
font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--bg)}
pre.diff .ln{display:block;padding:0 10px;min-height:1.5em;white-space:pre;
border-left:3px solid transparent}
.ln.add{background:var(--add-bg);color:var(--add-fg);border-left-color:var(--add-fg)}
.ln.del{background:var(--del-bg);color:var(--del-fg);border-left-color:var(--del-fg)}
.ln.hunk{background:var(--hunk-bg);color:var(--hunk-fg)}
.ln.meta{color:var(--muted)}
.foot{color:var(--muted);font-size:12px;margin-top:24px}
/* side-by-side (split) view */
table.split{width:100%;border-collapse:collapse;border:1px solid var(--line);
border-radius:6px;table-layout:fixed;background:var(--bg)}
table.split col.cn{width:46px}
table.split col.cc{width:calc(50% - 46px)}
table.split td{padding:0 8px;vertical-align:top;white-space:pre-wrap;
overflow-wrap:anywhere;word-break:break-word;
font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
table.split td.lno,table.split td.rno{width:46px;text-align:right;color:var(--muted);
user-select:none;background:var(--card);border-right:1px solid var(--line)}
table.split td.cell.del{background:var(--del-bg);color:var(--del-fg)}
table.split td.cell.add{background:var(--add-bg);color:var(--add-fg)}
table.split td.cell.blank{background:var(--card);opacity:.5}
table.split td.meta{color:var(--muted)}
table.split tr.hid{display:none}
table.split td.exp-cell{cursor:pointer;color:var(--hunk-fg);background:var(--hunk-bg);
font-size:11.5px;user-select:none;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
table.split td.exp-cell:hover{text-decoration:underline}
/* view switch: unified by default; .view-split flips it and widens the page */
body:not(.view-split) table.split{display:none}
body.view-split pre.diff:not(.keep){display:none}
body.view-split .wrap{max-width:min(1800px,96vw)}
.vt{cursor:pointer;font:600 11.5px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:var(--accent);background:var(--card);border:1px solid var(--line);
border-radius:6px;padding:4px 10px;margin-left:10px}
.vt:hover{border-color:var(--accent)}
.fchip{opacity:.45;margin-left:6px}
h1 .fchip:first-of-type{margin-left:16px}
.fchip.active{opacity:1;border-color:var(--accent)}
.tbtn{cursor:pointer;font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:var(--muted);background:none;border:1px solid transparent;border-radius:5px;padding:1px 8px}
.tbtn:hover{border-color:var(--line);color:var(--fg)}
details.turn>summary .tbtn.star{margin-left:auto;font-size:14px}
details.turn.starred>summary .tbtn.star{color:#e3b341}
details.turn.starred{border-color:#b08a2e}
details.turn.hiddenmark{opacity:.55}
.working{display:inline-flex;align-items:center;gap:7px;color:var(--accent);
font:600 12px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.working.wblock{display:flex;margin:10px 0}
.working .dot{width:8px;height:8px;border-radius:50%;background:var(--accent);
animation:tdpulse 1.1s ease-in-out infinite}
@keyframes tdpulse{0%,100%{opacity:.25;transform:scale(.8)}50%{opacity:1;transform:scale(1.15)}}
.composer{position:fixed;left:0;right:0;bottom:0;z-index:50;background:var(--card);
border-top:1px solid var(--line);box-shadow:0 -6px 20px rgba(0,0,0,.18)}
.composer .cinner{max-width:min(1080px,96vw);margin:0 auto;padding:9px 20px}
body.view-split .composer .cinner{max-width:min(1800px,96vw)}
.composer .crow{display:flex;align-items:center;gap:10px;margin-top:6px}
.composer .cstatus{color:var(--muted);font-size:12px;flex:1;word-break:break-word}
.composer .EasyMDEContainer .CodeMirror{height:auto;min-height:60px;font-size:14px;border-radius:6px}
.composer .CodeMirror-scroll{min-height:60px;max-height:46vh}
.composer #td-send{padding:6px 16px;font-size:12.5px}
.composer .ctarget{font-size:11.5px;color:var(--muted);white-space:nowrap;margin-right:auto}
.composer .ctarget b{color:var(--accent)}
.composer .cstatus{flex:0 1 auto}
.cbanner{display:none;align-items:center;gap:8px;color:var(--accent);font-size:12.5px;
font-weight:600;padding:2px 0 8px}
.cbanner.show{display:flex}
/* dark-friendly EasyMDE — theme via our vars, override the vendored light css */
.composer .CodeMirror{background:var(--bg)!important;color:var(--fg)!important;border-color:var(--line)!important}
.composer .CodeMirror-cursor{border-left-color:var(--fg)!important}
.composer .CodeMirror-selected{background:var(--line)!important}
.composer .CodeMirror-placeholder{color:var(--muted)!important}
.composer .cm-formatting,.composer .cm-comment{color:var(--muted)!important}
/* toolbar hidden until toggled via the Formatting button */
.composer .editor-toolbar{max-height:0;opacity:0;padding:0!important;border:0!important;
overflow:hidden;border-radius:6px 6px 0 0;transition:max-height .13s ease,opacity .13s ease}
.composer.show-fmt .editor-toolbar{max-height:46px;opacity:1;
padding:6px 8px!important;border-bottom:1px solid var(--line)!important;background:var(--card)!important}
.composer #td-fmt.active{border-color:var(--accent);background:var(--line)}
.composer .editor-toolbar button{color:var(--fg)!important;border-color:transparent!important}
.composer .editor-toolbar button:hover,.composer .editor-toolbar button.active{
background:var(--line)!important;border-color:var(--line)!important}
.composer .editor-toolbar i.separator{border-color:var(--line)!important}
.composer .editor-preview,.composer .editor-preview-side{background:var(--card)!important;color:var(--fg)!important}
/* Process (thinking + tool calls), collapsed by default */
details.process{border:1px solid var(--line);border-radius:6px;margin:10px 0;background:var(--card)}
details.process>summary{cursor:pointer;list-style:none;padding:7px 12px;font-size:12px;
color:var(--muted);font-weight:600}
details.process>summary::-webkit-details-marker{display:none}
details.process>summary::before{content:'▸ '}
details.process[open]>summary::before{content:'▾ '}
details.process[open]>summary{border-bottom:1px solid var(--line)}
.pbody{padding:8px 12px;display:flex;flex-direction:column;gap:6px}
.pthink{white-space:pre-wrap;word-break:break-word;color:var(--muted);
font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
border-left:2px solid var(--line);padding:2px 0 2px 10px}
.ptool{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
display:flex;gap:8px;align-items:baseline}
.ptool .ptn{color:var(--accent);font-weight:700;flex:0 0 auto}
.ptool .pts{color:var(--fg);opacity:.85;word-break:break-word;overflow-wrap:anywhere}
.pmark{color:var(--muted);font-size:11.5px;font-style:italic;opacity:.75}
.pnarr{color:var(--fg);opacity:.92;border-left:2px solid var(--accent);padding:1px 0 1px 10px}
.pnarr p{margin:3px 0}.pnarr>*:first-child{margin-top:0}.pnarr>*:last-child{margin-bottom:0}
"""

JS = """
var REFRESH = __REFRESH__;
var SID = "__SID__";
(function(){
  var httpLive = location.protocol.lastIndexOf('http',0)===0;
  var lastKey = 0;
  function q(s,r){ return (r||document).querySelectorAll(s); }

  var LS; try{ localStorage.setItem('__t','1'); localStorage.removeItem('__t'); LS=localStorage; }catch(e){ LS=sessionStorage; }
  function mkey(k,n){ return 'td:'+SID+':'+k+':'+n; }
  function mark(k,n){ try{ return LS.getItem(mkey(k,n))==='1'; }catch(e){ return false; } }
  function setMark(k,n,v){ try{ if(v){LS.setItem(mkey(k,n),'1');}else{LS.removeItem(mkey(k,n));} }catch(e){} }
  var filter=null; try{ filter=JSON.parse(LS.getItem('td:'+SID+':filter')); }catch(e){}
  if(!filter) filter={regular:true,starred:true,hidden:false};
  function saveFilter(){ try{ LS.setItem('td:'+SID+':filter',JSON.stringify(filter)); }catch(e){} }
  function numOf(d){ return (d.id||'').replace('turn-',''); }
  function applyMarks(){
    q('details.turn').forEach(function(d){
      var n=numOf(d), st=mark('star',n), hi=mark('hide',n), cat=hi?'hidden':(st?'starred':'regular');
      d.classList.toggle('starred',st); d.classList.toggle('hiddenmark',hi);
      d.style.display=filter[cat]?'':'none';
      var sb=d.querySelector('.tbtn.star'); if(sb) sb.textContent=st?'★':'☆';
      var hb=d.querySelector('.tbtn.hidebtn'); if(hb) hb.textContent=hi?'unhide':'hide';
    });
    q('.fchip').forEach(function(ch){ ch.classList.toggle('active',!!filter[ch.getAttribute('data-cat')]); });
  }

  function wireTurns(){
    q('details.turn').forEach(function(d){
      if(d.dataset.wt) return; d.dataset.wt='1';
      var n=numOf(d), sb=d.querySelector('.tbtn.star'), hb=d.querySelector('.tbtn.hidebtn'), s=d.querySelector('summary');
      if(sb) sb.addEventListener('click',function(ev){ ev.preventDefault(); ev.stopPropagation(); setMark('star',n,!mark('star',n)); applyMarks(); });
      if(hb) hb.addEventListener('click',function(ev){ ev.preventDefault(); ev.stopPropagation(); setMark('hide',n,!mark('hide',n)); applyMarks(); });
      if(s) s.addEventListener('click',function(ev){ if(ev.ctrlKey||ev.metaKey) return; q('details.turn').forEach(function(o){ if(o!==d) o.open=false; }); });
    });
  }
  function wireDetails(){
    q('details[id]').forEach(function(d){
      if(d.dataset.wd) return; d.dataset.wd='1';
      var k='open:'+d.id, v; try{v=sessionStorage.getItem(k);}catch(e){}
      if(v==='0') d.open=false; if(v==='1') d.open=true;
      d.addEventListener('toggle',function(){ try{sessionStorage.setItem(k,d.open?'1':'0');}catch(e){} });
    });
  }
  function wireFilter(){
    q('.fchip').forEach(function(ch){
      if(ch.dataset.wf) return; ch.dataset.wf='1';
      ch.addEventListener('click',function(){ var c=ch.getAttribute('data-cat'); filter[c]=!filter[c]; saveFilter(); applyMarks(); });
    });
  }
  function highlight(){
    if(typeof hljs==='undefined') return;
    function hl(el,lang){ if(el.dataset.hl) return; var t=el.textContent; el.dataset.hl='1'; if(!t||t===' ') return;
      try{ el.innerHTML=(lang?hljs.highlight(t,{language:lang,ignoreIllegals:true}):hljs.highlightAuto(t)).value; }
      catch(e){ try{ el.innerHTML=hljs.highlightAuto(t).value; }catch(e2){} } }
    q('.file[data-lang]').forEach(function(f){ var lang=f.getAttribute('data-lang')||'';
      f.querySelectorAll('pre.diff span.ln.add, pre.diff span.ln.del, pre.diff span.ln.ctx').forEach(function(el){ hl(el,lang); });
      f.querySelectorAll('table.split td.cell:not(.blank)').forEach(function(el){ hl(el,lang); }); });
    q('.agent-result pre code').forEach(function(el){ if(el.dataset.hl) return; el.dataset.hl='1'; try{ hljs.highlightElement(el); }catch(e){} });
  }
  function wireExpanders(){
    var map={};
    q('table.split tr.hid').forEach(function(r){ var g=r.getAttribute('data-grp'); (map[g]=map[g]||[]).push(r); });
    q('table.split tr.exp').forEach(function(ex){
      if(ex.dataset.we) return; ex.dataset.we='1';
      var gid=ex.getAttribute('data-grp'), key='exp:'+gid, cell=ex.querySelector('.exp-cell'), rows=map[gid]||[];
      function setOpen(open){ rows.forEach(function(r){ r.style.display=open?'table-row':'none'; });
        ex.classList.toggle('open',open); if(cell) cell.textContent=(open?'▴ hide ':'⋯ show ')+rows.length+' unchanged line'+(rows.length!==1?'s':''); }
      var saved; try{ saved=sessionStorage.getItem(key); }catch(e){} setOpen(saved==='1');
      ex.addEventListener('click',function(){ var open=!ex.classList.contains('open'); try{ sessionStorage.setItem(key,open?'1':'0'); }catch(e){} setOpen(open); });
    });
  }
  function setupContent(){ wireDetails(); wireTurns(); wireFilter(); wireExpanders(); highlight(); applyMarks(); }

  (function(){
    var btn=document.getElementById('vt');
    function apply(v){ document.body.classList.toggle('view-split',v==='split');
      if(btn) btn.textContent=(v==='split')?'≡ Unified':'◧ Side-by-side'; }
    var v='unified'; try{ v=sessionStorage.getItem('view')||'unified'; }catch(e){}
    apply(v);
    if(btn) btn.addEventListener('click',function(){ var nv=document.body.classList.contains('view-split')?'unified':'split';
      try{ sessionStorage.setItem('view',nv); }catch(e){} apply(nv); });
  })();

  if(/^#turn-\\d+$/.test(location.hash)){ var tt=document.getElementById(location.hash.slice(1)); if(tt) tt.open=true; }
  try{ var yy=sessionStorage.getItem('scrollY'); if(yy) window.scrollTo(0,parseInt(yy,10));
    window.addEventListener('beforeunload',function(){ try{sessionStorage.setItem('scrollY',window.scrollY);}catch(e){} }); }catch(e){}
  setupContent();

  (function(){
    var box=document.getElementById('composer'); var tok=window.__TD_TOKEN__;
    if(!box||!httpLive||!tok||typeof EasyMDE==='undefined') return;
    box.hidden=false;
    var mde=new EasyMDE({element:document.getElementById('td-prompt'), spellChecker:false, status:false,
      autoDownloadFontAwesome:true, minHeight:'70px',
      placeholder:'Prompt this session…  (Enter to send · Shift+Enter for newline)',
      toolbar:['bold','italic','heading','code','quote','unordered-list','ordered-list','link','|','preview']});
    var status=document.getElementById('td-status'), btn=document.getElementById('td-send'),
        tgt=document.getElementById('td-target'), banner=document.getElementById('td-banner');
    function fit(){ document.body.style.paddingBottom=(box.offsetHeight+20)+'px'; }
    setTimeout(fit,60); window.addEventListener('resize',fit);
    fetch('/target/'+SID).then(function(r){return r.json();}).then(function(j){
      if(j&&j.ok){ tgt.innerHTML='⚡ target: <b>'+j.backend+' '+j.target+'</b>'+(j.status?(' · '+j.status):''); }
      else{ tgt.textContent='⚠ '+((j&&j.error)||'no pane found'); } fit(); }).catch(function(){});
    function send(){ var text=mde.value(); if(!text.trim()) return; btn.disabled=true; status.textContent='sending…';
      fetch('/prompt/'+SID,{method:'POST',headers:{'Content-Type':'application/json','X-TD-Token':tok},body:JSON.stringify({text:text})})
        .then(function(r){return r.json();}).then(function(j){ if(j.ok){ status.textContent=''; mde.value(''); if(banner) banner.classList.add('show'); fit(); } else { status.textContent='✗ '+(j.error||'failed'); } })
        .catch(function(e){ status.textContent='✗ '+e; }).then(function(){ btn.disabled=false; }); }
    btn.addEventListener('click',send);
    mde.codemirror.setOption('extraKeys', Object.assign(mde.codemirror.getOption('extraKeys')||{}, {
      'Enter': function(){ send(); }, 'Shift-Enter': function(cm){ cm.replaceSelection('\\n'); } }));
    // Formatting toggle (persisted) — show/hide the toolbar on demand
    var fmtBtn=document.getElementById('td-fmt');
    function setFmt(on){ box.classList.toggle('show-fmt',on); if(fmtBtn) fmtBtn.classList.toggle('active',on); setTimeout(fit,150); }
    var fmtOn=false; try{ fmtOn=sessionStorage.getItem('td-fmt')==='1'; }catch(e){}
    setFmt(fmtOn);
    if(fmtBtn) fmtBtn.addEventListener('click',function(){ var on=!box.classList.contains('show-fmt');
      try{ sessionStorage.setItem('td-fmt',on?'1':'0'); }catch(e){} setFmt(on); });
    // auto-grow (debounced) + note typing time so live morphs can defer while you type
    var fitT;
    mde.codemirror.on('change', function(){ lastKey=Date.now(); clearTimeout(fitT); fitT=setTimeout(fit,120); });
    mde.codemirror.on('focus', function(){ setTimeout(fit,150); });
    mde.codemirror.on('blur', function(){ setTimeout(fit,150); });
    setTimeout(function(){ mde.codemirror.refresh(); fit(); }, 80);
  })();

  var morphing=false;
  function morphUpdate(){
    if(typeof morphdom==='undefined'){ location.reload(); return; }
    if(morphing) return; morphing=true;
    fetch(location.href,{cache:'no-store'}).then(function(r){return r.text();}).then(function(t){
      var doc=new DOMParser().parseFromString(t,'text/html');
      var neww=doc.querySelector('.wrap'), cur=document.querySelector('.wrap');
      if(neww&&cur){
        morphdom(cur,neww,{ onBeforeElUpdated:function(from,to){
          if(from.classList && from.classList.contains('turn')){
            if(from.getAttribute('data-sig')===to.getAttribute('data-sig')) return false;  // unchanged turn: skip whole subtree
            to.style.display=from.style.display;
            if(from.classList.contains('starred')) to.classList.add('starred');
            if(from.classList.contains('hiddenmark')) to.classList.add('hiddenmark');
          }
          if(from.nodeName==='DETAILS' && from.id){ to.open=from.open; }
          if(from.dataset && from.dataset.hl && from.textContent===to.textContent){ return false; }
          return true;
        }});
        setupContent();
      }
    }).catch(function(){}).then(function(){ morphing=false; });
  }
  var deferT;
  if(httpLive && window.EventSource){
    try{ new EventSource('/events/'+location.pathname.split('/').pop()).onmessage=function(){
      if(document.hidden) return;
      if(Date.now()-lastKey<1500){ clearTimeout(deferT); deferT=setTimeout(morphUpdate,1500); }  // defer while typing
      else morphUpdate();
    }; }catch(e){}
  }

  (function(){
    var btn=document.getElementById('ar');
    function isOn(){ try{return sessionStorage.getItem('autoreload')==='on';}catch(e){return false;} }
    function label(){ if(btn){ btn.textContent=isOn()?('⟳ Auto-reload: on ('+REFRESH+'s)'):'⏸ Auto-reload: off'; } }
    label();
    if(btn) btn.addEventListener('click',function(){ try{ sessionStorage.setItem('autoreload', isOn()?'off':'on'); }catch(e){} label(); });
    if(REFRESH>0 && !httpLive){
      function tick(){ if(isOn() && !document.hidden){ location.reload(); } else { setTimeout(tick,1000); } }
      setTimeout(tick, REFRESH*1000);
    }
  })();
})();
"""


def esc(s):
    return html.escape(s, quote=False)


# ---------------------------------------------------------------- mini markdown
def _md_inline(s):
    """Inline markdown -> HTML on a single text run. Escapes first, so embedded
    HTML in agent text is shown literally."""
    s = esc(s)
    codes = []
    s = re.sub(r"`([^`]+)`", lambda m: codes.append(m.group(1)) or f"\x00C{len(codes)-1}\x00", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{esc(m.group(2))}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"\x00C(\d+)\x00", lambda m: "<code>" + codes[int(m.group(1))] + "</code>", s)
    return s


def _render_markdown(text):
    lines = (text or "").split("\n")
    out, para, i, n = [], [], 0, len(lines)

    def flush():
        if para:
            joined = " ".join(para).strip()
            if joined:
                out.append("<p>" + _md_inline(joined) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        st = line.strip()
        if st.startswith("```"):
            flush()
            lang = st[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f" class=\"language-{esc(lang)}\"" if lang else ""
            out.append(f"<pre><code{cls}>" + esc("\n".join(buf)) + "</code></pre>")
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", st):
            flush(); out.append("<hr>"); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)$", st)
        if m:
            flush()
            out.append(f"<div class='md-h md-h{len(m.group(1))}'>" + _md_inline(m.group(2).strip()) + "</div>")
            i += 1; continue
        if st.startswith(">"):
            flush()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + _md_inline(" ".join(buf)) + "</blockquote>")
            continue
        if "|" in st and i + 1 < n and "|" in lines[i + 1] and re.match(r"^[\s:|*-]*-{1,}[\s:|*-]*$", lines[i + 1].strip()):
            flush()
            hdr = [c.strip() for c in st.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{_md_inline(c)}</th>" for c in hdr)
            tb = "".join("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table class='md-table'><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table>")
            continue
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            flush()
            ordered = bool(re.match(r"^\s*\d+\.", line))
            items = []
            while i < n:
                mm = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)$", lines[i])
                if not mm:
                    break
                items.append("<li>" + _md_inline(mm.group(2).strip()) + "</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if not st:
            flush(); i += 1; continue
        para.append(st)
        i += 1
    flush()
    return "".join(out)


def render_markdown(text):
    try:
        return _render_markdown(text)
    except Exception:
        return "<p>" + esc(text or "") + "</p>"


def diff_html(lines, keep=False):
    """Unified diff view. keep=True marks it to stay visible even in split mode
    (used for the hunks fallback, which has no clean before/after to split)."""
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... (diff truncated at {MAX_DIFF_LINES} lines)"]
    out = []
    for ln in lines:
        if ln.startswith("+++") or ln.startswith("---"):
            cls = "meta"
        elif ln.startswith("@@"):
            cls = "hunk"
        elif ln.startswith("+"):
            cls = "add"
        elif ln.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append(f'<span class="ln {cls}">{esc(ln) if ln else "&nbsp;"}</span>')
    pre_cls = "diff keep" if keep else "diff"
    return f'<pre class="{pre_cls}">' + "".join(out) + "</pre>"


def split_rows(before, after):
    """Aligned (left, right) rows for a side-by-side view, via SequenceMatcher.
    Each row: (cls, lno, ltext, rno, rtext); None text == padding on that side."""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(("equal", i1 + k + 1, a[i1 + k], j1 + k + 1, b[j1 + k]))
        elif tag == "replace":
            left, right = list(range(i1, i2)), list(range(j1, j2))
            for k in range(max(len(left), len(right))):
                li = left[k] if k < len(left) else None
                rj = right[k] if k < len(right) else None
                rows.append(("replace",
                             (li + 1 if li is not None else None),
                             (a[li] if li is not None else None),
                             (rj + 1 if rj is not None else None),
                             (b[rj] if rj is not None else None)))
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append(("delete", k + 1, a[k], None, None))
        elif tag == "insert":
            for k in range(j1, j2):
                rows.append(("insert", None, None, k + 1, b[k]))
    return rows


def _split_tr(row, hidden=False, gid=""):
    cls, lno, ltext, rno, rtext = row
    lc = {"delete": "del", "replace": "del"}.get(cls, "")
    rc = {"insert": "add", "replace": "add"}.get(cls, "")
    lblank = " blank" if ltext is None else ""
    rblank = " blank" if rtext is None else ""
    attrs = f" class='hid' data-grp='{esc(gid)}'" if hidden else ""
    return ("<tr" + attrs + ">"
            f"<td class='lno'>{lno if lno else ''}</td>"
            f"<td class='cell {lc}{lblank}'>{esc(ltext) if ltext else '&nbsp;'}</td>"
            f"<td class='rno'>{rno if rno else ''}</td>"
            f"<td class='cell {rc}{rblank}'>{esc(rtext) if rtext else '&nbsp;'}</td>"
            "</tr>")


def split_html(rec, key=""):
    rows = split_rows(rec.get("before"), rec.get("after"))
    truncated = len(rows) > MAX_DIFF_LINES
    if truncated:
        rows = rows[:MAX_DIFF_LINES]
    n = len(rows)
    changed = [r[0] != "equal" for r in rows]
    # distance of each row to the nearest change, so SPLIT_CONTEXT lines stay visible
    dist = [10 ** 9] * n
    last = None
    for i in range(n):
        if changed[i]:
            last = i
        if last is not None:
            dist[i] = i - last
    last = None
    for i in range(n - 1, -1, -1):
        if changed[i]:
            last = i
        if last is not None:
            dist[i] = min(dist[i], last - i)
    hide = [(not changed[i]) and dist[i] > SPLIT_CONTEXT for i in range(n)]

    out = ['<table class="split"><colgroup><col class="cn"><col class="cc">'
           '<col class="cn"><col class="cc"></colgroup><tbody>']
    i = grp = 0
    while i < n:
        if hide[i]:
            j = i
            while j < n and hide[j]:
                j += 1
            count = j - i
            gid = f"{key}-g{grp}"
            grp += 1
            plural = "s" if count != 1 else ""
            out.append(f"<tr class='exp' data-grp='{esc(gid)}'><td class='lno'></td>"
                       f"<td class='cell exp-cell' colspan='3'>⋯ show {count} unchanged line{plural}</td></tr>")
            for k in range(i, j):
                out.append(_split_tr(rows[k], hidden=True, gid=gid))
            i = j
        else:
            out.append(_split_tr(rows[i]))
            i += 1
    if truncated:
        out.append(f"<tr><td class='lno'></td><td class='cell meta' colspan='3'>"
                   f"… truncated at {MAX_DIFF_LINES} rows</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def split_html_ops(rec):
    """Side-by-side built from each edit's old/new strings — used when the file's
    full before-state isn't reconstructable (so a whole-file split isn't possible)."""
    out = ['<table class="split"><colgroup><col class="cn"><col class="cc">'
           '<col class="cn"><col class="cc"></colgroup><tbody>']
    total = 0
    first = True
    for kind, old, new in rec["ops"]:
        if total > MAX_DIFF_LINES:
            out.append(f"<tr><td class='lno'></td><td class='cell meta' colspan='3'>"
                       f"… truncated at {MAX_DIFF_LINES} rows</td></tr>")
            break
        if not first:
            out.append("<tr><td class='lno'></td>"
                       "<td class='cell exp-cell' colspan='3'>— next edit —</td></tr>")
        first = False
        rows = split_rows("" if kind == "write" else old, new)
        for r in rows:
            out.append(_split_tr(r))
            total += 1
    out.append("</tbody></table>")
    return "".join(out)


def snippet(text, n=90):
    s = " ".join((text or "").split())
    return (s[:n] + "…") if len(s) > n else (s or "(empty)")


def file_block_html(path, rec, key=""):
    fid = key or path
    P = [f"<details class='file' id='file:{esc(fid)}' data-lang='{esc(lang_for(path))}' open>"
         f"<summary><h3>{esc(path)}</h3></summary>"]
    lines, mode = file_diff_lines(rec)
    if mode == "hunks":
        P.append("<p class='note'>Prior full content of this file isn't in the "
                 "transcript; showing each edit's own change.</p>")
        P.append(diff_html(lines))
        P.append(split_html_ops(rec))
    else:
        P.append(diff_html(lines))
        P.append(split_html(rec, key))
    P.append("</details>")
    return "".join(P)


def render_html(turns, session_path, refresh=0, title="", in_progress=False, cwd=""):
    gen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    live = (f'<span class="badge live">live · reloads every {refresh}s</span>'
            if refresh > 0 else '<span class="badge">static snapshot</span>')
    P = []
    P.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    P.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    tab = f"{title} — turn diffs" if title else f"Turn diffs — {Path(str(session_path)).name}"
    P.append(f"<title>{esc(tab)}</title>")
    # metadata for the server's session index
    P.append(f"<meta name='td-name' content='{esc(title or Path(str(session_path)).stem)}'>")
    P.append(f"<meta name='td-cwd' content='{esc(cwd)}'>")
    P.append(f"<meta name='td-turns' content='{len(turns)}'>")
    hljs_js = _asset("highlight.min.js")
    hl_on = bool(hljs_js)
    P.append(f"<style>{CSS}</style>")
    if hl_on:
        light, dark = _asset("github.min.css"), _asset("github-dark.min.css")
        if light:
            P.append(f"<style>{light}</style>")
        if dark:
            P.append(f"<style>@media(prefers-color-scheme:dark){{{dark}}}</style>")
    # apply the saved view synchronously, before first paint, so split view doesn't
    # flash narrow→wide on every auto-reload
    early = ("<script>try{if((sessionStorage.getItem('view')||'unified')==='split')"
             "document.body.classList.add('view-split');}catch(e){}</script>")
    # composer editor styles — load only over http (served by --serve); harmless 404 on file://
    P.append("<link rel='stylesheet' href='/assets/easymde.min.css'>")
    P.append(f"</head><body class='{'hl' if hl_on else ''}'>{early}<div class='wrap'>")
    ar_btn = ("<button class='vt' id='ar' type='button'>⟳ Auto-reload</button>"
              if refresh > 0 else "")
    heading = esc(title) if title else "Turn-by-turn changes"
    chips = ("<button class='vt fchip' data-cat='regular' type='button'>Regular</button>"
             "<button class='vt fchip' data-cat='starred' type='button'>★ Starred</button>"
             "<button class='vt fchip' data-cat='hidden' type='button'>Hidden</button>")
    P.append(f"<header><h1>{heading} "
             "<button class='vt' id='vt' type='button'>◧ Side-by-side</button>"
             f"{ar_btn}{chips}</h1>")
    kicker = "Turn-by-turn changes · " if title else ""
    cwd_line = f"cwd: <code>{esc(cwd)}</code> · " if cwd else ""
    P.append(f"<div class='sub'>{kicker}{cwd_line}{len(turns)} turn(s) · generated {gen} {live}"
             f"<br>Session: <code>{esc(str(session_path))}</code></div></header>")

    # turns
    for i, t in enumerate(turns, 1):
        ts = (t["ts"] or "")[:19].replace("T", " ")
        op = " open" if i == len(turns) else ""   # fresh start: only the last turn open
        work = ("<span class='working'><span class='dot'></span>working</span>"
                if (in_progress and i == len(turns)) else "")
        # cheap content fingerprint so the live morph can skip unchanged turns entirely
        sig = "%d.%d.%d.%d.%d.%d" % (len(t["prompt"]), len(t.get("answer", "")),
              len(t.get("process", [])), len(t["order"]),
              sum(len(t["files"][p].get("ops", [])) for p in t["order"]),
              1 if (in_progress and i == len(turns)) else 0)
        P.append(f"<details class='turn' id='turn-{i}'{op} data-sig='{sig}'><summary>"
                 f"<span class='tn'>Turn {i}</span>"
                 + (f"<span class='ts'>{esc(ts)}</span>" if ts else "")
                 + f"<span class='pin'>{esc(snippet(t['prompt'], 110))}</span>{work}"
                 "<button class='tbtn star' type='button' title='Star this turn'>☆</button>"
                 "<button class='tbtn hidebtn' type='button' title='Hide this turn'>hide</button>"
                 "</summary>")
        P.append("<div class='body'>")
        P.append(f"<blockquote class='prompt'>{esc(t['prompt'] or '(empty)')}</blockquote>")
        proc = t.get("process", [])
        if proc:
            ntools = sum(1 for it in proc if it["kind"] == "tool")
            nthink = sum(1 for it in proc if it["kind"] == "think")
            label = f"{ntools} tool call{'s' if ntools != 1 else ''}"
            if nthink:
                label += f" · {nthink} thinking step{'s' if nthink != 1 else ''}"
            P.append(f"<details class='process' id='proc-{i}'><summary>Process — {label}</summary>"
                     "<div class='pbody'>")
            prev_mark = False
            for it in proc:
                if it["kind"] == "think":
                    if it.get("text", "").strip():          # real thinking text (rare)
                        P.append(f"<div class='pthink'>{esc(it['text'])}</div>")
                        prev_mark = False
                    elif not prev_mark:                     # collapse consecutive markers
                        P.append("<div class='pmark'>💭 thinking</div>")
                        prev_mark = True
                elif it["kind"] == "narr":                  # intermediate narration
                    prev_mark = False
                    P.append(f"<div class='pnarr'>{render_markdown(it['text'])}</div>")
                else:
                    prev_mark = False
                    P.append(f"<div class='ptool'><span class='ptn'>{esc(it['name'])}</span>"
                             f"<span class='pts'>{esc(it['summary'])}</span></div>")
            P.append("</div></details>")
        if not t["order"] and not t.get("agents"):
            P.append("<div class='noedit'>No file edits in this turn.</div>")
        for path in t["order"]:
            P.append(file_block_html(path, t["files"][path], key=f"t{i}:{path}"))
        for k, ag in enumerate(t.get("agents", [])):
            st = (f"<span class='ts'>{esc(ag['status'])}</span>" if ag.get("status") else "")
            atype = (f"<span class='ts'>[{esc(ag['agentType'])}]</span>"
                     if ag.get("agentType") else "")
            nfiles = len(ag.get("order", []))
            fc = (f"<span class='ts'>· {nfiles} file" + ("s" if nfiles != 1 else "") + "</span>"
                  if nfiles else "")
            nm = f" {esc(ag['label'])}" if ag.get("label") else ""
            P.append(f"<details class='agent' id='turn-{i}-agent-{k}'><summary>"
                     f"<span class='agk'>Subagent</span>{nm} {atype} {fc} {st}"
                     "</summary><div class='agent-body'>")
            if ag.get("result"):
                P.append(f"<div class='agent-result'>{render_markdown(ag['result'])}</div>")
            if ag.get("order"):
                P.append("<p class='note'>Files changed by this subagent:</p>")
                for path in ag["order"]:
                    P.append(file_block_html(path, ag["files"][path], key=f"t{i}a{k}:{path}"))
            P.append("</div></details>")
        if t.get("answer"):
            P.append(f"<details class='answer' id='ans-{i}' open><summary>Answer</summary>"
                     f"<div class='agent-result'>{render_markdown(t['answer'])}</div></details>")
        if i == len(turns) and in_progress:
            P.append("<div class='working wblock'><span class='dot'></span>"
                     "Turn in progress — the report updates as the agent works…</div>")
        elif i == len(turns) and not t.get("answer"):
            P.append("<div class='noedit'>⏳ Answer pending — it appears once the turn "
                     "finishes and the report regenerates.</div>")
        P.append("</div></details>")

    changed = sum(1 for t in turns if t["order"])
    P.append(f"<div class='foot'>Turns that changed files: {changed} / {len(turns)}.</div>")
    P.append("</div>")
    # composer (prompt this session's terminal) — only activates when served live
    P.append("<div id='composer' class='composer' hidden><div class='cinner'>"
             "<div id='td-banner' class='cbanner'><span class='dot'></span>"
             "Prompt sent — agent working…</div>"
             "<textarea id='td-prompt'></textarea>"
             "<div class='crow'><span id='td-target' class='ctarget'></span>"
             "<span id='td-status' class='cstatus'></span>"
             "<button id='td-fmt' class='vt' type='button'>Formatting</button>"
             "<button id='td-send' class='vt' type='button'>Send ▶</button></div></div></div>")
    P.append("<script src='/assets/morphdom-umd.min.js'></script>")
    P.append("<script src='/assets/easymde.min.js'></script>")
    if hl_on:
        P.append("<script>" + hljs_js + "</script>")
    P.append("<script>" + JS.replace("__REFRESH__", str(int(refresh)))
                            .replace("__SID__", Path(str(session_path)).stem) + "</script>")
    P.append("</body></html>")
    return "".join(P)


# ---------------------------------------------------------------- generation glue
def generate(session_path, out_path, fmt, refresh, in_progress=False):
    entries = load(session_path)
    turns = build_turns(entries)
    attach_agents(turns, session_path, entries)
    title = session_title(entries)
    cwd = entries_cwd(entries)
    if fmt == "md":
        content = render_md(turns, session_path, title)
    else:
        content = render_html(turns, session_path, refresh, title, in_progress, cwd)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out_path)   # atomic: viewers never see a half-written report
    return len(turns)


def default_out(session_path, fmt):
    ext = "md" if fmt == "md" else "html"
    return Path.cwd() / (Path(str(session_path)).stem + f"-turn-diffs.{ext}")


def watch(session_arg, out_path, fmt, refresh):
    follow_newest = session_arg is None
    cur = None if follow_newest else Path(session_arg)
    last_sig = None
    print(f"Watching for changes… writing {out_path}  (Ctrl-C to stop)", file=sys.stderr)
    try:
        while True:
            if follow_newest:
                s = find_sessions()
                cur = s[0] if s else None
            if cur and cur.exists():
                try:
                    sig = (str(cur), cur.stat().st_mtime)
                except OSError:
                    sig = None
                if sig and sig != last_sig:
                    last_sig = sig
                    n = generate(cur, out_path, fmt, refresh)
                    print(f"  {time.strftime('%H:%M:%S')}  {cur.name}  ({n} turns)", file=sys.stderr)
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)


def _transcript_for(sid):
    for p in PROJECTS.glob(f"*/{sid}.jsonl"):
        return p
    return None


def _session_cwd(sid):
    tp = _transcript_for(sid)
    if not tp:
        return ""
    cwd = ""
    try:
        with open(tp, encoding="utf-8") as fh:
            for line in fh:
                if '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd") or cwd
                    except Exception:
                        continue
    except OSError:
        pass
    return cwd


def _load_panes_map():
    """Optional manual session->pane pinning: DATA_DIR/panes.json =
    {"<session_id>": {"backend": "tmux|herdr|zellij", "target": "<pane-id or zellij session>"}}"""
    try:
        return json.loads((DATA_DIR / "panes.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_pane(sid):
    """Locate the terminal pane hosting a session. Herdr is exact (its panes
    advertise their claude session id); tmux uses a cwd heuristic; zellij only via
    a manual panes.json pin (its CLI can't target arbitrary panes)."""
    import subprocess
    m = _load_panes_map().get(sid)
    if isinstance(m, dict) and m.get("backend") and m.get("target"):
        return m["backend"], m["target"], {"source": "panes.json"}
    try:
        r = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for p in json.loads(r.stdout).get("result", {}).get("panes", []):
                if (p.get("agent_session") or {}).get("value") == sid:
                    return "herdr", p["pane_id"], {"status": p.get("agent_status", "")}
    except Exception:
        pass
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F",
                            "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            cwd = _session_cwd(sid)
            cands = []
            for ln in r.stdout.splitlines():
                parts = (ln.split("\t") + ["", ""])[:3]
                if parts[1] in ("claude", "node") and (not cwd or parts[2] == cwd):
                    cands.append(parts[0])
            if len(cands) == 1:
                return "tmux", cands[0], {}
            if cands:
                return None, None, {"error": "ambiguous tmux panes — pin one in panes.json",
                                    "candidates": cands}
    except Exception:
        pass
    return None, None, {"error": "no pane found for this session (checked herdr + tmux); "
                                 "pin it manually in " + str(DATA_DIR / "panes.json")}


_PASTE_OPEN = "\x1b[200~"
_PASTE_CLOSE = "\x1b[201~"


def inject_prompt(sid, text):
    """Type a prompt into the session's terminal pane and press Enter. Multiline
    text is delivered as one bracketed-paste block so the TUI doesn't submit on
    each newline (and control sequences in the text stay inert)."""
    import subprocess
    backend, target, info = _find_pane(sid)
    if not backend:
        return {"ok": False, **info}
    body = (text or "").rstrip("\n")
    if not body.strip():
        return {"ok": False, "error": "empty prompt"}
    multiline = "\n" in body
    wrapped = (_PASTE_OPEN + body + _PASTE_CLOSE) if multiline else body
    try:
        if backend == "herdr":
            subprocess.run(["herdr", "pane", "send-text", target, wrapped],
                           check=True, capture_output=True, timeout=10)
            time.sleep(0.15)
            subprocess.run(["herdr", "pane", "send-keys", target, "Enter"],
                           check=True, capture_output=True, timeout=10)
        elif backend == "tmux":
            subprocess.run(["tmux", "set-buffer", "-b", "tdprompt", "--", body],
                           check=True, capture_output=True, timeout=10)
            subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-b", "tdprompt", "-t", target],
                           check=True, capture_output=True, timeout=10)
            time.sleep(0.15)
            subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                           check=True, capture_output=True, timeout=10)
        elif backend == "zellij":
            subprocess.run(["zellij", "--session", target, "action", "write-chars", wrapped],
                           check=True, capture_output=True, timeout=10)
            time.sleep(0.15)
            subprocess.run(["zellij", "--session", target, "action", "write", "13"],
                           check=True, capture_output=True, timeout=10)
        else:
            return {"ok": False, "error": f"unknown backend {backend!r}"}
    except Exception as exc:
        return {"ok": False, "error": f"{backend} injection failed: {exc}"[:300]}
    return {"ok": True, "backend": backend, "target": target, **info}


def _serve_token():
    """Stable per-machine token, embedded into served pages so only pages the
    server itself handed out can POST /prompt. Localhost-only binding is the real
    boundary; the token blocks naive/drive-by local requests."""
    f = DATA_DIR / "serve-token"
    try:
        t = f.read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    import secrets
    t = secrets.token_urlsafe(24)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(t, encoding="utf-8")
        os.chmod(f, 0o600)
    except OSError:
        pass
    return t


def _index_html(rd):
    """Styled session index. Pulls name/cwd/turns from each report's <head> meta
    (cheap: reads only the first ~8KB) and shows last-updated."""
    def meta(head, name):
        m = re.search(r"<meta name='td-%s' content='([^']*)'>" % name, head)
        return html.unescape(m.group(1)) if m else ""
    rows = []
    for f in sorted(rd.glob("*.html"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            head = ""
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        rows.append((f.name, meta(head, "name") or f.stem, meta(head, "cwd"),
                     meta(head, "turns"), mtime))
    items = []
    for fname, name, cwd, turns, mtime in rows:
        tl = (f"{turns} turns" if turns else "")
        meta_line = " · ".join(x for x in [tl, ("updated " + mtime) if mtime else ""] if x)
        items.append(
            f"<a class='scard' href='/{esc(fname)}'>"
            f"<div class='sname'>{esc(name)}</div>"
            + (f"<div class='scwd'>{esc(cwd)}</div>" if cwd else "")
            + f"<div class='smeta'>{esc(meta_line)}</div></a>")
    body = f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>turn-diffs · sessions</title><style>{CSS}
.ix{{max-width:900px;margin:0 auto;padding:28px 20px 60px}}
.ix h1{{font-size:20px;margin:0 0 4px}}
.ix .sub{{margin-bottom:18px}}
.scard{{display:block;text-decoration:none;color:var(--fg);border:1px solid var(--line);
border-radius:9px;background:var(--card);padding:13px 16px;margin:10px 0;transition:border-color .1s}}
.scard:hover{{border-color:var(--accent)}}
.sname{{font-weight:700;font-size:15px}}
.scwd{{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--muted);
margin-top:3px;word-break:break-all}}
.smeta{{color:var(--muted);font-size:12px;margin-top:5px}}
</style></head><body class='hl'><div class='ix'>
<h1>turn-diffs</h1><div class='sub'>{len(rows)} session report(s) · newest first</div>
{''.join(items) or "<p class='sub'>No reports yet.</p>"}
</div></body></html>"""
    return body.encode()


_REGEN_LOCK = threading.Lock()
_REGEN_AT = {}


def _safe_regen(sid, tx, out, in_progress):
    """Regenerate a report from its transcript, rate-limited across SSE clients so
    the server can be the live-update engine even for sessions whose own hooks
    don't fire mid-turn."""
    now = time.time()
    with _REGEN_LOCK:
        if now - _REGEN_AT.get(sid, 0) < 1.5:
            return
        _REGEN_AT[sid] = now
    try:
        generate(tx, out, "html", 5, in_progress=in_progress)
    except Exception:
        pass


def serve(port):
    """Live mode: serve the reports dir on 127.0.0.1 with SSE push-on-change and a
    token-guarded POST /prompt/<sid> that types a prompt into the session's terminal
    pane. The same HTML still works from file:// with no server. Ctrl-C to stop."""
    import http.server
    rd = reports_dir().resolve()
    rd.mkdir(parents=True, exist_ok=True)
    assets = ASSETS_DIR.resolve()
    token = _serve_token()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _report(self, name):
            t = (rd / name).resolve()
            return t if (t.parent == rd and t.suffix == ".html" and t.exists()) else None

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                return self._send(200, "text/html; charset=utf-8", _index_html(rd))
            if p.startswith("/assets/"):
                a = (assets / p[len("/assets/"):]).resolve()
                if a.parent == assets and a.is_file():
                    ct = ("text/css" if a.suffix == ".css" else
                          "application/javascript" if a.suffix == ".js" else "application/octet-stream")
                    return self._send(200, ct, a.read_bytes())
                return self._send(404, "text/plain", b"no asset")
            if p.startswith("/target/"):
                sid = p[len("/target/"):]
                if sid.endswith(".html"):
                    sid = sid[:-5]
                backend, target, info = _find_pane(sid)
                res = ({"ok": True, "backend": backend, "target": target, **info}
                       if backend else {"ok": False, **info})
                return self._send(200, "application/json; charset=utf-8", json.dumps(res).encode())
            if p.startswith("/events/"):
                name = p[len("/events/"):]
                t = self._report(name)
                if not t:
                    return self._send(404, "text/plain", b"nope")
                sid = name[:-5] if name.endswith(".html") else name
                tx = _transcript_for(sid)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                last_report = t.stat().st_mtime if t.exists() else 0
                last_size = tx.stat().st_size if (tx and tx.exists()) else -1
                changed_at = 0.0
                pending_clear = False   # a turn is showing "working" and still needs a final clear
                IDLE_CLEAR = 45         # fallback only (the Stop hook is the real turn-end signal)
                try:
                    while True:
                        time.sleep(1.0)
                        now = time.time()
                        if tx and tx.exists():
                            try:
                                sz = tx.stat().st_size
                            except OSError:
                                sz = last_size
                            if sz != last_size:
                                last_size = sz
                                changed_at = now
                                pending_clear = True
                                _safe_regen(sid, tx, t, in_progress=True)   # turn is active; keep it ON
                            elif pending_clear and now - changed_at > IDLE_CLEAR:
                                # no Stop-hook clear arrived and it's been idle a long time -> clear
                                _safe_regen(sid, tx, t, in_progress=False)
                                pending_clear = False
                        m = t.stat().st_mtime if t.exists() else 0
                        if m != last_report:
                            last_report = m
                            self.wfile.write(b"data: reload\n\n")
                        else:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            t = self._report(p.lstrip("/"))
            if t:
                body = t.read_bytes()
                inj = ("<script>window.__TD_TOKEN__=%r;</script>" % token).encode()
                body = body.replace(b"</head>", inj + b"</head>", 1)
                return self._send(200, "text/html; charset=utf-8", body)
            return self._send(404, "text/plain", b"not found")

        def do_POST(self):
            p = self.path.split("?")[0]
            if not p.startswith("/prompt/"):
                return self._send(404, "text/plain", b"not found")
            sid = p[len("/prompt/"):]
            if sid.endswith(".html"):
                sid = sid[:-5]
            try:
                n = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, "application/json", b'{"ok":false,"error":"bad body"}')
            if (self.headers.get("X-TD-Token") or data.get("token")) != token:
                return self._send(403, "application/json", b'{"ok":false,"error":"bad token"}')
            res = inject_prompt(sid, data.get("text", ""))
            body = json.dumps(res).encode()
            return self._send(200 if res.get("ok") else 409, "application/json; charset=utf-8", body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"turn-diffs live server: http://127.0.0.1:{port}/   (Ctrl-C to stop)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    return 0


def _wait_stable(path, settle=0.8, timeout=10.0):
    """Wait until the transcript stops growing. The Stop hook can fire while the
    harness is still flushing the turn's final entries; reading too early truncates
    the last turn's answer. Async hook, so waiting here costs nothing."""
    end = time.time() + timeout
    try:
        last = path.stat().st_size
    except OSError:
        return
    time.sleep(settle)
    while time.time() < end:
        try:
            cur = path.stat().st_size
        except OSError:
            return
        if cur == last:
            return
        last = cur
        time.sleep(settle)


def run_hook(fmt, refresh):
    """Driven by a Claude Code Stop hook. Reads the hook JSON from stdin. Runs only
    when THIS session was enabled via --enable; otherwise returns immediately. Writes
    to a per-session report file and never raises (it runs async, off the turn's
    critical path)."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    sid = data.get("session_id")
    tp = data.get("transcript_path")
    session = Path(tp) if tp else None
    if not sid and session is not None:
        sid = session_id_of(session)
    if not is_enabled(sid):
        return 0  # off for this session -> do nothing, cheaply
    if session is None or not session.exists():
        return 0
    out = report_path_for(sid, fmt)
    event = data.get("hook_event_name", "Stop")
    if event == "Stop":
        _wait_stable(session)   # let the harness finish flushing the turn's final entries
    else:
        # mid-turn event (PostToolUse): a snapshot is fine, but rate-limit bursts
        try:
            if time.time() - out.stat().st_mtime < 3:
                return 0
        except OSError:
            pass
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        generate(session, out, fmt, refresh, in_progress=(event != "Stop"))
    except Exception:
        pass
    return 0


def _resolve_session():
    s = current_session()
    if not s:
        print(f"No Claude sessions found under {PROJECTS}.", file=sys.stderr)
        return None, None
    return s, session_id_of(s)


def _server_port():
    try:
        return int(os.environ.get("TURN_DIFFS_PORT", "8787"))
    except ValueError:
        return 8787


def _server_running(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_server(port=None):
    """Start the --serve live server as a detached singleton if it isn't already
    running. Returns the port on success, None if it couldn't be started."""
    port = port or _server_port()
    if _server_running(port):
        return port
    import subprocess
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "serve.log", "ab") as lf:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port)],
                stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return None
    for _ in range(20):
        if _server_running(port):
            return port
        time.sleep(0.1)
    return None


def cmd_enable(fmt, refresh):
    session, sid = _resolve_session()
    if not session:
        return 1
    enabled_dir().mkdir(parents=True, exist_ok=True)
    enabled_flag(sid).touch()
    out = report_path_for(sid, fmt)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        n = generate(session, out, fmt, refresh, in_progress=True)
    except Exception as exc:
        print(f"Enabled, but the initial render failed: {exc}", file=sys.stderr)
        n = 0
    print(f"turn-diffs: ON for session {sid} ({n} turn(s) so far)")
    port = ensure_server()
    if port:
        print(f"Live view: http://127.0.0.1:{port}/{out.name}")
        print(f"Static fallback: {file_url(out)}")
    else:
        print(f"Open: {file_url(out)}")
    return 0


def cmd_disable():
    session, sid = _resolve_session()
    if not session:
        return 1
    try:
        enabled_flag(sid).unlink()
    except FileNotFoundError:
        pass
    print(f"turn-diffs: OFF for session {sid}")
    return 0


def cmd_status(fmt):
    session, sid = _resolve_session()
    if not session:
        return 1
    on = is_enabled(sid)
    print(f"turn-diffs: {'ON' if on else 'off'} for session {sid}")
    if on:
        out = report_path_for(sid, fmt)
        port = _server_port()
        if _server_running(port):
            print(f"Live view: http://127.0.0.1:{port}/{out.name}")
        print(f"Static: {file_url(out)}")
    print(f"Reports dir: {reports_dir()}")
    return 0


def cmd_ensure(fmt, refresh):
    """No-arg default: enable for this session if it's off; either way make sure the
    live server is up, then show status/links."""
    session, sid = _resolve_session()
    if not session:
        return 1
    if is_enabled(sid):
        ensure_server()
        return cmd_status(fmt)
    return cmd_enable(fmt, refresh)


HOOK_HELP = """\
Per-session, on-demand setup. Add this Stop hook to ~/.claude/settings.json (merge
into an existing "hooks" block if present). Use an ABSOLUTE path to the script.
`async: true` keeps it off the turn's critical path.

{
  "hooks": {
    "Stop": [
      { "hooks": [
          { "type": "command", "async": true,
            "command": "python3 /ABS/PATH/turn-diffs.py --hook" }
      ]}
    ]
  }
}

The hook fires every turn in every session but does nothing unless that session is
enabled. To control a session, run from inside it:

    python3 /ABS/PATH/turn-diffs.py --enable     # turn on + print the report link
    python3 /ABS/PATH/turn-diffs.py --status     # is it on? what's the link?
    python3 /ABS/PATH/turn-diffs.py --disable    # turn off

Reports are written per-session to  $TURN_DIFFS_DIR/reports/<session_id>.html
(default ~/.claude/turn-diffs/reports). The HTML auto-reloads, so the tab refreshes
itself as the hook regenerates it each turn.
"""


def main():
    ap = argparse.ArgumentParser(description="Show Claude Code session changes grouped by turn.")
    ap.add_argument("session", nargs="?", help="Path to a session .jsonl (default: newest)")
    ap.add_argument("-o", "--output", help="Output file path")
    ap.add_argument("--format", choices=["html", "md"], default="html", help="Output format (default html)")
    ap.add_argument("--refresh", type=int, default=-1,
                    help="HTML auto-reload seconds (0=off). Default: auto (5 in watch/hook/enable, else 0)")
    ap.add_argument("--watch", action="store_true", help="Regenerate whenever the session changes")
    ap.add_argument("--hook", action="store_true", help="Run once from a Claude Code Stop hook (reads stdin)")
    ap.add_argument("--hook-help", action="store_true", help="Print settings.json hook config and exit")
    ap.add_argument("--enable", action="store_true",
                    help="Enable turn-diffs for the current session and print its report link")
    ap.add_argument("--disable", action="store_true", help="Disable turn-diffs for the current session")
    ap.add_argument("--status", action="store_true",
                    help="Show whether the current session is enabled, and its link")
    ap.add_argument("--ensure", action="store_true",
                    help="Enable for the current session if it's off, else just show status")
    ap.add_argument("--serve", action="store_true",
                    help="Serve reports on http://127.0.0.1 with live push-on-change (SSE)")
    ap.add_argument("--port", type=int, default=8787, help="Port for --serve (default 8787)")
    ap.add_argument("--dir", help="Base dir for reports/flags (default: $TURN_DIFFS_DIR or ~/.claude/turn-diffs)")
    ap.add_argument("--list", action="store_true", help="List recent sessions and exit")
    args = ap.parse_args()

    if args.hook_help:
        print(HOOK_HELP)
        return 0

    if args.dir:
        global DATA_DIR
        DATA_DIR = Path(args.dir).expanduser()

    refresh = args.refresh
    if refresh < 0:
        refresh = 5 if (args.watch or args.hook or args.enable or args.ensure) else 0

    if args.enable:
        return cmd_enable(args.format, refresh)
    if args.disable:
        return cmd_disable()
    if args.status:
        return cmd_status(args.format)
    if args.ensure:
        return cmd_ensure(args.format, refresh)
    if args.serve:
        return serve(args.port)

    if args.list:
        sessions = find_sessions()
        if not sessions:
            print(f"No sessions found under {PROJECTS}", file=sys.stderr)
            return 1
        for p in sessions[:25]:
            first = next((clean_prompt(_text_of(e.get("message", {}).get("content")))
                          for e in load(p) if is_user_prompt(e)), "(no prompt)")
            print(f"{p}\n    {snippet(first, 90)}\n")
        return 0

    if args.hook:
        return run_hook(args.format, refresh)

    out = Path(args.output) if args.output else None

    if args.watch:
        if out is None:
            base = Path(args.session) if args.session else (find_sessions() or [Path("session")])[0]
            out = default_out(base, args.format)
        watch(args.session, out, args.format, refresh)
        return 0

    # one-shot
    if args.session:
        session = Path(args.session)
    else:
        sessions = find_sessions()
        if not sessions:
            print(f"No sessions under {PROJECTS}. Pass a path explicitly.", file=sys.stderr)
            return 1
        session = sessions[0]
        print(f"Using newest session: {session}", file=sys.stderr)
    if not session.exists():
        print(f"Not found: {session}", file=sys.stderr)
        return 1
    if out is None:
        out = default_out(session, args.format)
    n = generate(session, out, args.format, refresh)
    print(f"Wrote {out}  ({n} turns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
