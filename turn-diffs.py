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
import hashlib
import hmac
import html
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
PROJECTS = CLAUDE_DIR / "projects"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
__version__ = "1.2.0"
HEALTH_MAGIC = "turn-diffs"     # identifies our server on a shared port
MAX_REPORT_AGE_DAYS = 30        # reports older than this are pruned
MAX_REPORTS_KEPT = 40           # ...and only this many are kept regardless
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_DIFF_LINES = 800
MAX_PROMPT_CHARS = 4000
MAX_AGENT_CHARS = 16000
MAX_THINK_CHARS = 8000   # per thinking block shown in the Process section
SPLIT_CONTEXT = 3   # unchanged lines kept around each change in the side-by-side view
MAX_SPLIT_ROWS = 1500       # ceiling on rows emitted per side-by-side table, collapsed included
SPLIT_HIDDEN_RUN_MAX = 120  # a longer collapsed run is summarised instead of emitted
_LINENO = re.compile(r"^\s*\d+[\t\u2192]")   # "   12<TAB>" / "   12->" prefix from Read output

# Where per-session reports and on/off flags live. Defaults under ~/.claude so a
# manual install works with no setup; hooks/hooks.json sets $TURN_DIFFS_DIR to
# ${CLAUDE_PLUGIN_DATA} for plugin installs, which keeps the two from sharing a
# state directory (and lets Claude Code reclaim the space on uninstall).
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
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A line that parses to a non-dict (null / number / string) would
                # make every downstream .get() raise, and the hook swallows that
                # exception — the report would silently stop updating.
                if isinstance(obj, dict):
                    entries.append(obj)
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


# Text the harness echoes back into the transcript as a "user" entry. These are
# not prompts: treating them as turns splits a real turn in two and misattributes
# every file edit that follows them to the wrong prompt.
_ECHO_PREFIXES = ("<local-command-stdout>", "<local-command-caveat>",
                  "<command-message>", "[Request interrupted")


def _is_harness_echo(txt):
    return txt.lstrip().startswith(_ECHO_PREFIXES)


def is_user_prompt(e):
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    if notification_text(e):
        return False
    raw = e.get("message", {}).get("content")
    if _is_harness_echo(raw if isinstance(raw, str) else _text_of(raw)):
        return False
    content = e.get("message", {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_tr = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        has_tx = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
        return has_tx and not has_tr
    return False


def _local_ts(raw):
    """Transcript timestamps are ISO-8601 UTC ('...Z'); show them in local time."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(raw)[:19].replace("T", " ")


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


_SYSREM_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)


def _read_is_partial(inp):
    """A Read with offset/limit returns a slice, not the file. Seeding file_state
    from it would present that slice as the complete 'before' content."""
    return bool(inp.get("offset") or inp.get("limit"))


def _clean_read_result(txt):
    """Read results carry line-number gutters and sometimes an appended
    <system-reminder> block; neither is part of the file."""
    return strip_linenos(_SYSREM_RE.sub("", txt))


def errored_tool_ids(entries):
    """tool_use ids whose result came back as an error.

    Replaying a denied or failed Edit/Write as if it succeeded poisons
    file_state for every later turn, so the edit must be skipped."""
    bad = set()
    for e in entries:
        if e.get("type") != "user":
            continue
        content = e.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if (isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("is_error") and b.get("tool_use_id")):
                bad.add(b["tool_use_id"])
    return bad


# ---------------------------------------------------------------- core: build turns
def build_turns(entries, subs=None):
    """Group the transcript into turns and reconstruct each file's before/after.

    subs: optional {agent_tool_use_id: info} from scan_subagents(). When given,
    a subagent's resulting file contents are folded into file_state at the point
    its Agent call was issued — without that, the NEXT turn diffs against
    pre-subagent content and attributes the subagent's edits to itself.
    Defaults to None so the plain build_turns(entries) contract is unchanged.
    """
    file_state = {}        # path -> best-known current full content (or None)
    pending_reads = {}     # Read tool_use_id -> (path, is_partial)
    errored = errored_tool_ids(entries)
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
                p, partial = pending_reads.pop(tid)
                if p not in file_state and txt and not partial and tid not in errored:
                    file_state[p] = _clean_read_result(txt)

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
                    # The subagent edited real files; carry its results forward so
                    # the next turn diffs against what is actually on disk.
                    if subs:
                        for p, r in (subs.get(tid, {}).get("files") or {}).items():
                            if isinstance(r, dict) and r.get("after") is not None:
                                file_state[p] = r["after"]
                continue
            if name == "Read":
                fp = inp.get("file_path")
                if fp:
                    pending_reads[tu.get("id")] = (fp, _read_is_partial(inp))
                continue
            if name not in EDIT_TOOLS or cur is None:
                continue
            if tu.get("id") in errored:
                continue          # the tool call failed or was denied — never happened
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
    errored = errored_tool_ids(entries)
    files = {}
    order = []
    for e in entries:
        for tid, txt in tool_results(e):
            if tid in pending_reads:
                p, partial = pending_reads.pop(tid)
                if p not in file_state and txt and not partial and tid not in errored:
                    file_state[p] = _clean_read_result(txt)
        for tu in assistant_tool_uses(e):
            name = tu.get("name")
            inp = tu.get("input", {}) or {}
            if name == "Read":
                fp = inp.get("file_path")
                if fp:
                    pending_reads[tu.get("id")] = (fp, _read_is_partial(inp))
                continue
            if name not in EDIT_TOOLS:
                continue
            if tu.get("id") in errored:
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
        order, files, children = ([], {}, [])
        if jf.exists():
            try:
                sub_entries = load(jf)
                order, files = collect_edits(sub_entries)
                # Agent calls this subagent made itself. Their tool_use ids live
                # here, not in the main transcript, which is why nested agents
                # were previously dropped on the floor.
                children = [tu.get("id") for e in sub_entries
                            for tu in assistant_tool_uses(e)
                            if tu.get("name") in ("Agent", "Task") and tu.get("id")]
            except Exception:
                order, files, children = ([], {}, [])
        out[tuid] = {"agentType": info.get("agentType", ""),
                     "description": info.get("description", ""),
                     "spawnDepth": info.get("spawnDepth"),
                     "order": order, "files": files, "children": children}
    return out


def attach_agents(turns, session_path, entries, subs=None):
    """Build each turn's agent panels from the agents it spawned. Placement comes
    from the main transcript (which turn issued the Agent call), file diffs from the
    subagent transcript, and result text from the notification (delivered in any
    form). This catches agents whose notification was queued/attached, not just
    plain-text ones."""
    subs = subs if subs is not None else scan_subagents(session_path)
    notifs = collect_notifications(entries)
    MAX_DEPTH = 4          # backstop; real nesting is 1-2 levels

    def build_entry(tuid, depth):
        info = subs.get(tuid, {})
        notif = notifs.get(tuid, {})
        atype = info.get("agentType", "")
        name = info.get("description", "")
        if not name:  # fall back to the quoted name in the notification summary
            m = re.search(r'"([^"]+)"', notif.get("label", ""))
            name = m.group(1) if m else (notif.get("label", "") or "")
        entry = {
            "label": name, "agentType": atype, "status": notif.get("status", ""),
            "result": notif.get("result", ""), "order": info.get("order", []),
            "files": info.get("files", {}), "tuid": tuid, "depth": depth,
        }
        # Always keep the panel: the main transcript proves the agent was
        # spawned, so an interrupted or crashed one must leave a trace rather
        # than vanishing as if it never ran.
        if not (entry["result"] or entry["order"] or atype):
            entry["label"] = entry["label"] or "Agent"
            entry["result"] = "(no result recorded — the agent was interrupted or crashed)"
        if depth:
            entry["label"] = ("↳ " * depth) + (entry["label"] or "nested agent")
        return entry

    def walk(tuid, depth, seen, out):
        if tuid in seen or depth > MAX_DEPTH:
            return
        seen.add(tuid)
        out.append(build_entry(tuid, depth))
        for child in subs.get(tuid, {}).get("children", []):
            walk(child, depth + 1, seen, out)

    for t in turns:
        seen = set()
        for tuid in t.get("agent_tuids", []):
            walk(tuid, 0, seen, t["agents"])


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
        ts = _local_ts(t["ts"])
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
.htop{display:flex;align-items:center;gap:10px}
.htop h1{font-size:20px;margin:0 4px 0 0;flex:0 1 auto;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.sbtoggle{cursor:pointer;flex:0 0 auto;background:var(--card);border:1px solid var(--line);
border-radius:6px;color:var(--fg);font-size:15px;line-height:1;padding:5px 9px}
.sbtoggle:hover{border-color:var(--accent)}
.hmenu{margin-left:auto;position:relative;flex:0 0 auto}
.hmenu-sum{display:none}
.hmenu .hmenu-body{display:flex;flex-wrap:wrap;gap:6px;align-items:center;justify-content:flex-end}
.hmenu-body .vt{margin-left:0}
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
.chg{flex:0 0 auto;white-space:nowrap;font:600 11px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:var(--add-fg);background:var(--add-bg);border-radius:10px;padding:3px 8px}
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
.hmenu-body .fchip:first-of-type{margin-left:14px}
/* sessions sidebar */
#sb-backdrop{position:fixed;inset:0;z-index:99;background:rgba(0,0,0,.35);display:none}
#sb-backdrop.show{display:block}
#sidebar{position:fixed;top:0;left:0;bottom:0;width:310px;max-width:86vw;z-index:100;
background:var(--card);border-right:1px solid var(--line);transform:translateX(-102%);
transition:transform .18s ease;display:flex;flex-direction:column;box-shadow:2px 0 18px rgba(0,0,0,.25)}
#sidebar.open{transform:none}
.sb-head{padding:12px 14px;border-bottom:1px solid var(--line);display:flex;
align-items:center;justify-content:space-between}
.sb-head b{font-size:14px}
.sb-close{cursor:pointer;background:none;border:0;color:var(--muted);font-size:20px;line-height:1}
.sb-list{overflow-y:auto;flex:1;padding:6px}
.sb-item{display:flex;flex-direction:column;gap:3px;padding:8px 10px;border-radius:7px;
text-decoration:none;color:var(--fg);border:1px solid transparent;margin:2px 0}
.sb-item:hover{background:var(--bg);border-color:var(--line)}
.sb-item.current{border-color:var(--accent);background:var(--bg)}
.sb-r1{display:flex;align-items:center;gap:8px;min-width:0}
.sb-nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600}
.sb-mt{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:17px}
details.turn.pending>summary{opacity:.85}
.pending-note{margin:10px 0 4px;padding:9px 12px;border:1px dashed var(--line);
border-radius:8px;color:var(--muted);font-size:13px}
.td-empty{margin:24px 0;padding:16px;border:1px dashed var(--line);border-radius:10px;
color:var(--muted);text-align:center;display:flex;gap:10px;justify-content:center;align-items:center}
.td-empty button{cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--fg);
border-radius:6px;padding:4px 12px;font:inherit}
.sb-dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;background:var(--muted)}
.sb-dot.finished{background:#3fb950}
.sb-dot.seen{background:var(--muted);opacity:.55}
.sb-dot.blocked{background:#f85149}
.sb-dot.working{background:var(--accent);animation:tdpulse 1.1s ease-in-out infinite}
/* diff comments */
.fcmt{cursor:pointer;font-size:12px;margin-left:12px;opacity:.6;border:1px solid var(--line);
background:var(--card);color:var(--fg);border-radius:5px;padding:1px 8px;vertical-align:middle}
.fcmt:hover{opacity:1;border-color:var(--accent)}
.td-cbox{margin:6px 0;padding:8px;border:1px solid var(--accent);border-radius:7px;background:var(--card)}
.td-cbox .ctx{font-size:11.5px;color:var(--muted);margin-bottom:5px;word-break:break-all}
.td-cbox textarea{width:100%;box-sizing:border-box;min-height:52px;background:var(--bg);color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:6px;font:inherit;resize:vertical}
.td-cbox .row{display:flex;gap:8px;justify-content:flex-end;margin-top:6px}
.td-cbox button{cursor:pointer;border:1px solid var(--line);background:var(--bg);color:var(--fg);
border-radius:6px;padding:4px 12px;font-size:13px}
.td-cbox button.pri{background:var(--accent);border-color:var(--accent);color:#0b0f14}
/* inline review comment shown under the commented line (unified: block inside <pre>;
   split: a full-width row). Force a normal font/wrapping even inside <pre>. */
.td-cmark{display:flex;gap:8px;align-items:flex-start;margin:3px 0 7px;padding:7px 10px;
border:1px solid var(--line);border-left:3px solid var(--accent);background:var(--card);
border-radius:0 7px 7px 0;white-space:normal;
font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
pre.diff .td-cmark{margin:3px 6px 7px}
.td-cmark .cbody{flex:1;min-width:0}
.td-cmark .cloc{display:block;font:600 11px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
color:var(--accent);margin-bottom:2px}
.td-cmark .ctext{white-space:pre-wrap;word-break:break-word}
.td-cmark .x{cursor:pointer;color:var(--muted);flex:0 0 auto;font-size:12px;line-height:1.4}
.td-cmark .x:hover{color:#f85149}
tr.td-cmark-row>td,tr.td-ins-row>td{padding:0!important;border:0!important;background:transparent!important}
tr.td-cmark-row .td-cmark{margin:3px 0 7px}
pre.diff .td-cbox{white-space:normal;font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:6px}
/* highlight a line that has a comment */
pre.diff span.ln.commented{box-shadow:inset 3px 0 0 var(--accent)}
table.split tr.commented td.cell{box-shadow:inset 0 -1px 0 var(--accent)}
.td-cbar{position:fixed;top:64px;right:16px;z-index:80;display:none;gap:10px;align-items:center;
background:var(--card);border:1px solid var(--accent);border-radius:10px;padding:8px 12px;
box-shadow:0 6px 20px rgba(0,0,0,.35)}
.td-cbar.show{display:flex}
.td-cbar b{font-size:13px}
.td-cbar button{cursor:pointer;border:1px solid var(--line);background:var(--bg);color:var(--fg);
border-radius:6px;padding:5px 12px;font-size:13px}
.td-cbar button.pri{background:var(--accent);border-color:var(--accent);color:#0b0f14;font-weight:600}
pre.diff span.ln.add,pre.diff span.ln.del,pre.diff span.ln.ctx{cursor:text}
pre.diff span.ln.add:hover,pre.diff span.ln.del:hover,pre.diff span.ln.ctx:hover{background:rgba(88,166,255,.08)}
/* mobile / narrow screens */
@media(max-width:720px){
  .wrap{padding:14px 10px 80px}
  body.view-split .wrap{max-width:100%;padding:14px 6px 80px}
  .htop h1{font-size:17px}
  .sub{font-size:11.5px}
  .hmenu-sum{display:inline-block;cursor:pointer;background:var(--card);border:1px solid var(--line);
    border-radius:6px;padding:4px 11px;color:var(--fg);font-size:15px;line-height:1}
  .hmenu .hmenu-body{display:none;position:absolute;right:0;top:calc(100% + 6px);z-index:60;
    flex-direction:column;align-items:stretch;background:var(--card);border:1px solid var(--line);
    border-radius:8px;padding:8px;min-width:190px;box-shadow:0 8px 24px rgba(0,0,0,.3)}
  .hmenu.open .hmenu-body{display:flex}
  .hmenu-body .vt{margin:2px 0;text-align:left}
  .hmenu-body .fchip:first-of-type{margin:6px 0 2px;border-top:1px solid var(--line);padding-top:8px}
  .composer .cinner,body.view-split .composer .cinner{max-width:100%;padding:6px 7px}
  .composer .crow{gap:7px;margin-top:5px}
  /* collapsed turn cards: keep to one compact line */
  details.turn>summary{padding:9px 10px;flex-wrap:nowrap;gap:8px;align-items:center}
  details.turn>summary .pin{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  details.turn>summary .tn{flex:0 0 auto}
  details.turn>summary .tbtn{flex:0 0 auto;padding:1px 7px}
  .ts{display:none}
}
.fchip.active{opacity:1;border-color:var(--accent)}
.tbtn{cursor:pointer;font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:var(--muted);background:none;border:1px solid transparent;border-radius:5px;padding:1px 8px}
.tbtn:hover{border-color:var(--line);color:var(--fg)}
details.turn>summary .tbtn.star{margin-left:auto;font-size:14px}
details.turn>summary .tbtn.hidebtn{font-size:15px;line-height:1;padding:1px 6px}
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
.composer .cinner{max-width:min(1080px,96vw);margin:0 auto;padding:9px 20px;position:relative}
body.view-split .composer .cinner{max-width:min(1800px,96vw)}
.composer .crow{display:flex;align-items:center;gap:10px;margin-top:6px}
/* slash-command autocomplete */
.td-cac{position:absolute;left:12px;right:12px;bottom:100%;margin-bottom:8px;z-index:70;
background:var(--card);border:1px solid var(--line);border-radius:9px;overflow-y:auto;
max-height:min(320px,52vh);box-shadow:0 -8px 26px rgba(0,0,0,.4)}
.td-cac-row{display:flex;gap:12px;align-items:baseline;padding:8px 12px;cursor:pointer;
border-bottom:1px solid var(--line)}
.td-cac-row:last-child{border-bottom:0}
.td-cac-row.active,.td-cac-row:hover{background:var(--bg)}
.td-cac-row .nm{font:600 13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
color:var(--accent);flex:0 0 auto;white-space:nowrap}
.td-cac-row .ds{font-size:11.5px;color:var(--muted);flex:1;min-width:0;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.td-cac-row .sr{font-size:10px;color:var(--muted);opacity:.7;flex:0 0 auto}
.composer .cstatus{color:var(--muted);font-size:12px;flex:1;word-break:break-word}
.composer .EasyMDEContainer .CodeMirror{height:auto;min-height:60px;font-size:14px;border-radius:6px}
.composer .CodeMirror-scroll{min-height:60px;max-height:46vh}
.composer #td-send{padding:6px 16px;font-size:12.5px}
.composer .ctarget{font-size:11.5px;color:var(--muted);white-space:nowrap;margin-right:auto}
.composer .ctarget b{color:var(--accent)}
.composer .cstatus{flex:0 1 auto}
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
  // runtime-only "already wired" markers, preserved across morphdom updates
  var TDGUARDS=['wt','wd','we','wf','fcb','hl'];
  // session-scoped keys must be namespaced too, or two sessions share open/scroll state
  function skey(k){ return 'td:'+SID+':'+k; }
  var filter=null; try{ filter=JSON.parse(LS.getItem('td:'+SID+':filter')); }catch(e){}
  // Merge over defaults rather than replacing: a filter saved by an older build
  // has no `changes` key, and passing undefined to classList.toggle() *toggles*
  // instead of forcing, making the chip flip state on every re-render.
  filter=Object.assign({regular:true,starred:true,hidden:false,changes:false},
                       (filter&&typeof filter==='object')?filter:{});
  filter.changes=!!filter.changes;
  function saveFilter(){ try{ LS.setItem('td:'+SID+':filter',JSON.stringify(filter)); }catch(e){} }
  function numOf(d){ return (d.id||'').replace('turn-',''); }
  function applyMarks(){
    q('details.turn').forEach(function(d){
      var n=numOf(d), st=mark('star',n), hi=mark('hide',n), cat=hi?'hidden':(st?'starred':'regular');
      var hasChg=(d.dataset.changes && d.dataset.changes!=='0');
      d.classList.toggle('starred',st); d.classList.toggle('hiddenmark',hi);
      // a just-sent placeholder has no file changes yet — never filter it out
      var show=filter[cat] && (!filter.changes || hasChg || d.classList.contains('pending'));
      d.style.display=show?'':'none';
      var sb=d.querySelector('.tbtn.star'); if(sb) sb.textContent=st?'★':'☆';
      var hb=d.querySelector('.tbtn.hidebtn'); if(hb){ hb.textContent=hi?'⊙':'⊘'; hb.title=hi?'Show this turn':'Hide this turn'; }
    });
    q('.fchip').forEach(function(ch){
      var on = ch.getAttribute('data-chg') ? !!filter.changes : !!filter[ch.getAttribute('data-cat')];
      ch.classList.toggle('active',on); });
    // A filter combination can hide every turn, leaving a blank page that looks
    // broken and persists across reloads — always offer a way back.
    var anyVisible=false;
    q('details.turn').forEach(function(d){ if(d.style.display!=='none') anyVisible=true; });
    var er=document.getElementById('td-empty');
    if(!anyVisible && q('details.turn').length){
      if(!er){
        er=document.createElement('div'); er.id='td-empty'; er.className='td-empty';
        er.innerHTML="No turns match the current filters. <button type='button'>Reset filters</button>";
        er.querySelector('button').addEventListener('click',function(){
          filter={regular:true,starred:true,hidden:false,changes:false};
          saveFilter(); applyMarks(); });
        var w=document.querySelector('.wrap'); if(w) w.appendChild(er);
      }
      er.style.display='';
    } else if(er){ er.style.display='none'; }
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
      var k=skey('open:'+d.id), v; try{v=sessionStorage.getItem(k);}catch(e){}
      if(v==='0') d.open=false; if(v==='1') d.open=true;
      d.addEventListener('toggle',function(){ try{sessionStorage.setItem(k,d.open?'1':'0');}catch(e){} });
    });
  }
  function wireFilter(){
    q('.fchip').forEach(function(ch){
      if(ch.dataset.wf) return; ch.dataset.wf='1';
      ch.addEventListener('click',function(){ var c=ch.getAttribute('data-chg')?'changes':ch.getAttribute('data-cat'); filter[c]=!filter[c]; saveFilter(); applyMarks(); });
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
      var saved; try{ saved=sessionStorage.getItem(skey(key)); }catch(e){} setOpen(saved==='1');
      ex.addEventListener('click',function(){ var open=!ex.classList.contains('open'); try{ sessionStorage.setItem(skey(key),open?'1':'0'); }catch(e){} setOpen(open); });
    });
  }
  function setupContent(){ wireDetails(); wireTurns(); wireFilter(); wireExpanders(); highlight(); applyMarks();
    if(window.__td_afterContent) window.__td_afterContent(); }

  (function(){
    var btn=document.getElementById('vt');
    function apply(v){ document.body.classList.toggle('view-split',v==='split');
      if(btn) btn.textContent=(v==='split')?'≡ Unified':'◧ Side-by-side'; }
    var v='unified'; try{ v=sessionStorage.getItem(skey('view'))||'unified'; }catch(e){}
    apply(v);
    if(btn) btn.addEventListener('click',function(){ var nv=document.body.classList.contains('view-split')?'unified':'split';
      try{ sessionStorage.setItem(skey('view'),nv); }catch(e){} apply(nv); });
  })();

  if(/^#turn-\\d+$/.test(location.hash)){ var tt=document.getElementById(location.hash.slice(1)); if(tt) tt.open=true; }
  try{ var yy=sessionStorage.getItem(skey('scrollY')); if(yy) window.scrollTo(0,parseInt(yy,10));
    window.addEventListener('beforeunload',function(){ try{sessionStorage.setItem(skey('scrollY'),window.scrollY);}catch(e){} }); }catch(e){}
  setupContent();

  (function(){
    var box=document.getElementById('composer'); var tok=window.__TD_TOKEN__;
    if(!box||!httpLive||!tok||typeof EasyMDE==='undefined') return;
    box.hidden=false;
    var isTouch=false; try{ isTouch=(window.matchMedia&&window.matchMedia('(pointer:coarse)').matches)||('ontouchstart' in window); }catch(e){}
    var mde=new EasyMDE({element:document.getElementById('td-prompt'), spellChecker:false, status:false,
      autoDownloadFontAwesome:true, minHeight:'70px',
      placeholder:(isTouch?'Prompt this session…  (Enter = newline · tap Send to submit)'
                          :'Prompt this session…  (Enter to send · Shift+Enter for newline)'),
      toolbar:['bold','italic','heading','code','quote','unordered-list','ordered-list','link','|','preview']});
    var status=document.getElementById('td-status'), btn=document.getElementById('td-send'),
        tgt=document.getElementById('td-target');
    function fit(){ document.body.style.paddingBottom=(box.offsetHeight+20)+'px'; }
    setTimeout(fit,60); window.addEventListener('resize',fit);
    fetch('/target/'+SID).then(function(r){return r.json();}).then(function(j){
      if(j&&j.ok){ var name=j.label?(j.backend+' · '+j.label):(j.backend+' '+j.target);
        tgt.innerHTML='⚡ target: <b></b><span class="tstat"></span>';
        tgt.querySelector('b').textContent=name;
        if(j.status) tgt.querySelector('.tstat').textContent=' · '+j.status;
        tgt.title=(j.title?(j.title+'  '):'')+'['+j.backend+' '+j.target+']'; }
      else{ tgt.textContent='⚠ '+((j&&j.error)||'no pane found'); } fit(); }).catch(function(){});
    var sending=false;
    function send(){ var text=mde.value(); if(!text.trim()||sending) return;
      sending=true; btn.disabled=true; status.textContent='sending…';
      fetch('/prompt/'+SID,{method:'POST',headers:{'Content-Type':'application/json','X-TD-Token':tok},body:JSON.stringify({text:text})})
        .then(function(r){return r.json();}).then(function(j){ if(j.ok){ status.textContent=''; mde.value(''); fit();
            if(window.__tdComposer && window.__tdComposer.onAfterSend) window.__tdComposer.onAfterSend(text); }
          else { status.textContent='✗ '+(j.error||'failed'); } })
        .catch(function(e){ status.textContent='✗ '+e; })
        .then(function(){ sending=false; btn.disabled=false; }); }
    btn.addEventListener('click',send);
    // bridge so the diff-comments feature can live-preview + send through this composer
    window.__tdComposer={present:true, onAfterSend:null,
      value:function(){ return mde.value(); },
      set:function(t){ mde.value(t); setTimeout(fit,20); },
      focus:function(){ try{ mde.codemirror.focus(); }catch(e){} },
      // used by type-to-compose: put the keystroke that triggered the focus
      // into the editor, so the first character typed isn't lost
      insert:function(ch){ try{ mde.codemirror.focus(); mde.codemirror.replaceSelection(ch);
                                lastKey=Date.now(); setTimeout(fit,20); }catch(e){} },
      send:function(){ send(); }};
    // ---- slash-command autocomplete (list from /commands/<sid>) ----
    var CM=mde.codemirror.constructor;
    var cac=document.createElement('div'); cac.className='td-cac'; cac.style.display='none';
    box.querySelector('.cinner').appendChild(cac);
    var CMDS=[], cacItems=[], cacIdx=-1;
    fetch('/commands/'+SID,{cache:'no-store'}).then(function(r){return r.json();})
      .then(function(j){ CMDS=Array.isArray(j)?j:[]; }).catch(function(){});
    function cacOpen(){ return cac.style.display!=='none'; }
    function cacHide(){ cac.style.display='none'; cacItems=[]; cacIdx=-1; }
    function cacRender(){
      if(!cacItems.length){ cacHide(); return; }
      cac.innerHTML='';
      cacItems.forEach(function(c,i){
        var row=document.createElement('div'); row.className='td-cac-row'+(i===cacIdx?' active':'');
        row.innerHTML='<span class="nm"></span><span class="ds"></span><span class="sr"></span>';
        row.querySelector('.nm').textContent='/'+c.name;
        row.querySelector('.ds').textContent=c.desc||'';
        row.querySelector('.sr').textContent=c.source||'';
        row.addEventListener('mousedown',function(e){ e.preventDefault(); cacAccept(i); });
        cac.appendChild(row);
      });
      cac.style.display='';
      var a=cac.querySelector('.active'); if(a&&a.scrollIntoView) a.scrollIntoView({block:'nearest'});
    }
    function cacUpdate(){
      var m=/^\\s*\\/([\\w:.-]*)$/.exec(mde.value());   // whole input is one "/token"
      if(!m){ cacHide(); return; }
      var q=m[1].toLowerCase();
      cacItems=CMDS.filter(function(c){ return c.name.toLowerCase().indexOf(q)>=0; }).slice(0,12);
      cacIdx=cacItems.length?0:-1; cacRender();
    }
    function cacMove(d){ if(!cacItems.length) return; cacIdx=(cacIdx+d+cacItems.length)%cacItems.length; cacRender(); }
    function cacAccept(i){
      if(i==null) i=cacIdx; if(i<0||i>=cacItems.length){ cacHide(); return; }
      mde.value('/'+cacItems[i].name+' '); cacHide();
      var cm=mde.codemirror, d=cm.getDoc(); cm.focus(); d.setCursor({line:0,ch:d.getLine(0).length}); setTimeout(fit,20);
    }
    mde.codemirror.on('changes', cacUpdate);
    mde.codemirror.on('cursorActivity', cacUpdate);
    mde.codemirror.on('blur', function(){ setTimeout(cacHide,160); });

    // Desktop: Enter sends, Shift+Enter = newline. Touch (Gboard has no easy Shift+Enter):
    // Enter = newline and you submit with the Send button. Ctrl/Cmd+Enter always sends.
    // When the command list is open, Enter/Tab accept and Up/Down navigate.
    var keymap={ 'Shift-Enter': function(cm){ cm.replaceSelection('\\n'); },
                 'Ctrl-Enter': function(){ send(); }, 'Cmd-Enter': function(){ send(); },
                 'Up': function(){ if(cacOpen()){ cacMove(-1); return; } return CM.Pass; },
                 'Down': function(){ if(cacOpen()){ cacMove(1); return; } return CM.Pass; },
                 'Tab': function(){ if(cacOpen()){ cacAccept(); return; } return CM.Pass; },
                 'Esc': function(){ if(cacOpen()){ cacHide(); return; } return CM.Pass; } };
    keymap['Enter'] = isTouch
      ? function(cm){ if(cacOpen()){ cacAccept(); return; } cm.execCommand('newlineAndIndent'); }
      : function(){ if(cacOpen()){ cacAccept(); return; } send(); };
    mde.codemirror.setOption('extraKeys', Object.assign(mde.codemirror.getOption('extraKeys')||{}, keymap));
    // Formatting toggle (persisted) — show/hide the toolbar on demand
    var fmtBtn=document.getElementById('td-fmt');
    function setFmt(on){ box.classList.toggle('show-fmt',on); if(fmtBtn) fmtBtn.classList.toggle('active',on); setTimeout(fit,150); }
    var fmtOn=false; try{ fmtOn=sessionStorage.getItem(skey('td-fmt'))==='1'; }catch(e){}
    setFmt(fmtOn);
    if(fmtBtn) fmtBtn.addEventListener('click',function(){ var on=!box.classList.contains('show-fmt');
      try{ sessionStorage.setItem(skey('td-fmt'),on?'1':'0'); }catch(e){} setFmt(on); });
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
      // if the page template (CSS/JS/markup) changed, a morph can't apply it — full reload once
      var cb=document.querySelector('meta[name="td-build"]'), nb=doc.querySelector('meta[name="td-build"]');
      if(cb&&nb&&cb.getAttribute('content')!==nb.getAttribute('content')){ location.reload(); return; }
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
          // Carry over the runtime-only "already wired" markers. morphdom removes
          // any attribute missing from the incoming markup, and the server never
          // emits these — losing them makes setupContent() bind a SECOND listener
          // to the same button, so ★/⊘ toggle twice and cancel out.
          if(from.dataset && to.dataset){
            TDGUARDS.forEach(function(k){ if(from.dataset[k]) to.dataset[k]=from.dataset[k]; });
          }
          if(from.dataset && from.dataset.hl && from.textContent===to.textContent){ return false; }
          return true;
        }});
        setupContent();
      }
    }).catch(function(){}).then(function(){ morphing=false; });
  }
  var deferT;
  var pendingUpdate=false;
  if(httpLive && window.EventSource){
    try{ new EventSource('/events/'+location.pathname.split('/').pop()).onmessage=function(){
      // A hidden tab must remember it missed an update, not discard it: once the
      // session goes idle no further events arrive, so the page would stay stale
      // until a manual reload.
      if(document.hidden){ pendingUpdate=true; return; }
      if(Date.now()-lastKey<1500){ clearTimeout(deferT); deferT=setTimeout(morphUpdate,1500); }  // defer while typing
      else morphUpdate();
    }; }catch(e){}
    document.addEventListener('visibilitychange',function(){
      if(!document.hidden && pendingUpdate){ pendingUpdate=false; morphUpdate(); } });
  }

  (function(){
    var btn=document.getElementById('ar');
    function isOn(){ try{return sessionStorage.getItem(skey('autoreload'))==='on';}catch(e){return false;} }
    function label(){ if(btn){ btn.textContent=isOn()?('⟳ Auto-reload: on ('+REFRESH+'s)'):'⏸ Auto-reload: off'; } }
    label();
    if(btn) btn.addEventListener('click',function(){ try{ sessionStorage.setItem(skey('autoreload'), isOn()?'off':'on'); }catch(e){} label(); });
    if(REFRESH>0 && !httpLive){
      function tick(){ if(isOn() && !document.hidden){ location.reload(); } else { setTimeout(tick,1000); } }
      setTimeout(tick, REFRESH*1000);
    }
  })();

  // ---- header options menu (mobile popover) ----
  (function(){
    var hm=document.querySelector('.hmenu'), sum=document.getElementById('hmenu-sum');
    if(!hm||!sum) return;
    sum.addEventListener('click',function(e){ e.stopPropagation(); hm.classList.toggle('open'); });
    document.addEventListener('click',function(e){ if(hm.classList.contains('open') && !hm.contains(e.target)) hm.classList.remove('open'); });
  })();

  // ---- sessions sidebar ----
  (function(){
    var sb=document.getElementById('sidebar'), bd=document.getElementById('sb-backdrop'),
        tgl=document.getElementById('sbtoggle'), cls=document.getElementById('sb-close'),
        list=document.getElementById('sb-list');
    if(!sb||!tgl) return;
    function seenKey(sid){ return 'td:seen:'+sid; }
    function markSeen(sid,mtime){ try{ LS.setItem(seenKey(sid),String(mtime)); }catch(e){} }
    function open(){ sb.classList.add('open'); if(bd) bd.classList.add('show'); load(); }
    function close(){ sb.classList.remove('open'); if(bd) bd.classList.remove('show'); }
    tgl.addEventListener('click',open);
    if(cls) cls.addEventListener('click',close);
    if(bd) bd.addEventListener('click',close);
    document.addEventListener('keydown',function(e){ if(e.key==='Escape') close(); });
    function load(){
      if(!httpLive){ list.innerHTML='<div style="padding:14px;color:var(--muted);font-size:13px">'
        +'The session list needs the live server (open the http:// link).</div>'; return; }
      fetch('/sessions',{cache:'no-store'}).then(function(r){return r.json();}).then(function(rows){
        list.innerHTML='';
        rows.forEach(function(s){
          var seenAt=0; try{ seenAt=parseFloat(LS.getItem(seenKey(s.sid))||'0'); }catch(e){}
          var st=s.status;
          if(st==='finished' && seenAt && seenAt>=s.mtime) st='seen';
          if(s.sid===SID) st=(s.status==='working'||s.status==='blocked')?s.status:'seen';
          var a=document.createElement('a');
          a.className='sb-item'+(s.sid===SID?' current':'');
          a.href='/'+s.file;
          // status comes from a JSON endpoint that ultimately reads strings out
          // of `herdr pane list`, so it is whitelisted rather than interpolated
          a.innerHTML='<span class="sb-r1"><span class="sb-dot"></span><span class="sb-nm"></span></span>'
            +'<span class="sb-mt"></span>';
          if(['working','blocked','finished','seen'].indexOf(st)>=0)
            a.querySelector('.sb-dot').classList.add(st);
          a.querySelector('.sb-nm').textContent=s.name||s.sid;
          var base=s.cwd?s.cwd.replace(/\\/+$/,'').replace(/^.*\\//,''):'';
          a.querySelector('.sb-mt').textContent=[base,(s.turns?(s.turns+' turn(s)'):'')].filter(Boolean).join(' · ');
          a.addEventListener('click',function(){ markSeen(s.sid,s.mtime); });
          list.appendChild(a);
        });
      }).catch(function(){ list.innerHTML='<div style="padding:14px;color:var(--muted)">failed to load</div>'; });
    }
    // keep THIS session marked seen (using its server-reported mtime) so it never nags itself
    if(httpLive){ fetch('/sessions',{cache:'no-store'}).then(function(r){return r.json();})
      .then(function(rows){ rows.forEach(function(s){ if(s.sid===SID) markSeen(s.sid,s.mtime); }); }).catch(function(){}); }
  })();

  // ---- diff comments: line/file-level review comments -> live-composed prompt ----
  (function(){
    var CKEY='td:'+SID+':comments';
    var comments=[]; try{ comments=JSON.parse(LS.getItem(CKEY))||[]; }catch(e){}
    var C=window.__tdComposer||null, lastAuto='';
    var drafts={};   // in-progress comment editors, keyed by fid|side|line
    var isTouch=false; try{ isTouch=(window.matchMedia&&window.matchMedia('(pointer:coarse)').matches)||('ontouchstart' in window); }catch(e){}
    function save(){ try{ LS.setItem(CKEY,JSON.stringify(comments)); }catch(e){} }

    var bar=document.createElement('div'); bar.className='td-cbar';
    bar.innerHTML='<b class="n"></b><button class="pri send" type="button">Send</button>'
      +'<button class="clr" type="button">Clear</button>';
    document.body.appendChild(bar);
    var barN=bar.querySelector('.n'), sendB=bar.querySelector('.send'), clrB=bar.querySelector('.clr');

    function shortSnip(s){ s=(s||'').replace(/^[+\\- ]/,'').trim(); return s.length>100?s.slice(0,100)+'…':s; }
    function base(p){ return (p||'').replace(/^.*\\//,''); }

    // ---- compile the review comments into a prompt ----
    function compile(){
      if(!comments.length) return '';
      var out=['I reviewed the changes and left the following review comments:',''];
      comments.forEach(function(c,i){
        var loc = c.line ? ('`'+c.file+':'+c.line+'`') : ('`'+c.file+'` (file-level)');
        out.push((i+1)+'. '+loc+' — '+c.text);
        if(c.snip) out.push('   > '+c.snip);
      });
      out.push(''); out.push('Please address these comments.');
      return out.join('\\n');
    }
    function syncComposer(){   // mirror the compiled prompt into the composer (don't clobber custom edits)
      if(!C) return;
      var compiled=compile(), cur=C.value();
      if(cur===lastAuto || cur.trim()===''){ C.set(compiled); lastAuto=compiled; }
    }
    function updateBar(){ bar.classList.toggle('show',comments.length>0);
      barN.textContent=comments.length+' comment'+(comments.length!==1?'s':''); }
    function changed(){ save(); renderMarks(); updateBar(); syncComposer(); }
    clrB.addEventListener('click',function(){ comments=[]; changed(); });

    // ---- capture what/where the user is commenting on ----
    function fileOf(el){ var f=el.closest?el.closest('.file'):null; if(!f) return '';
      var h=f.querySelector('summary h3'); return h?h.textContent:''; }
    // The SAME path can appear in several turns. Remember which block the comment
    // was written in (each .file carries a per-turn id) so it re-anchors there
    // instead of jumping to the earliest turn that happened to touch the file.
    function fidOf(el){ var f=el.closest?el.closest('.file'):null; return f&&f.id?f.id:''; }
    function lineInfo(el){
      if(el.classList.contains('ln')){   // unified
        return {side:el.getAttribute('data-side')||'', line:parseInt(el.getAttribute('data-ln'),10)||0,
                snip:(el.textContent||'')};
      }
      var tr=el.closest('tr'), del=el.classList.contains('del'), side=del?'old':'new';  // split cell
      var nc=tr?tr.querySelector(del?'.lno':'.rno'):null;
      return {side:side, line:nc?(parseInt(nc.textContent,10)||0):0, snip:(el.textContent||'')};
    }

    function placeAfter(anchor, el){   // insert a node right after a diff line, in either view
      if(anchor.tagName==='TD'){
        var tr=anchor.closest('tr'), w=document.createElement('tr'); w.className='td-ins-row';
        var td=document.createElement('td'); td.colSpan=4; td.appendChild(el); w.appendChild(td);
        tr.parentNode.insertBefore(w, tr.nextSibling); return w;
      }
      anchor.parentNode.insertBefore(el, anchor.nextSibling); return el;
    }

    function openEditor(anchor, ctx){
      var nxt = anchor.tagName==='TD' ? (anchor.closest('tr')||{}).nextElementSibling : anchor.nextElementSibling;
      if(nxt){ var ex = (nxt.classList&&nxt.classList.contains('td-cbox')) ? nxt : (nxt.querySelector?nxt.querySelector('.td-cbox'):null);
        if(ex){ ex.querySelector('textarea').focus(); return; } }
      var box=document.createElement('div'); box.className='td-cbox';
      var ph=isTouch?'Leave a review comment…  (Enter = newline · tap Add)'
                    :'Leave a review comment…  (Enter to add · Shift+Enter for newline)';
      box.innerHTML='<div class="ctx"></div>'
        +'<textarea placeholder="'+ph+'"></textarea>'
        +'<div class="row"><button class="cancel" type="button">Cancel</button>'
        +'<button class="pri add" type="button">Add comment</button></div>';
      box.querySelector('.ctx').textContent=(ctx.line?(base(ctx.file)+':'+ctx.line):(base(ctx.file)+' · file-level'))
        +(ctx.snip?('   ·   '+shortSnip(ctx.snip)):'');
      var wrap=placeAfter(anchor, box);
      var ta=box.querySelector('textarea'); ta.focus();
      // Remember the draft: a live morph can replace this subtree at any moment,
      // and losing a half-written comment is worse than any staleness.
      var dkey=[ctx.fid||'',ctx.side||'',ctx.line||0].join('|');
      if(drafts[dkey]){ ta.value=drafts[dkey]; }
      ta.addEventListener('input',function(){ drafts[dkey]=ta.value; lastKey=Date.now(); });
      function close(){ delete drafts[dkey]; wrap.remove(); }
      box.querySelector('.cancel').addEventListener('click',close);
      box.querySelector('.add').addEventListener('click',function(){
        var v=ta.value.trim(); if(!v){ ta.focus(); return; }
        comments.push({file:ctx.file,fid:ctx.fid||'',side:ctx.side||'',line:ctx.line||0,
                       snip:shortSnip(ctx.snip),snipFull:(ctx.snip||'').replace(/^[+\\- ]/,''),
                       text:v});
        close(); changed(); });
      // Desktop: Enter adds, Shift+Enter = newline. Touch: Enter = newline, tap Add. Ctrl/Cmd+Enter always adds.
      ta.addEventListener('keydown',function(e){
        if(e.key!=='Enter') return;
        if(e.ctrlKey||e.metaKey){ e.preventDefault(); box.querySelector('.add').click(); return; }
        if(!isTouch && !e.shiftKey){ e.preventDefault(); box.querySelector('.add').click(); }
      });
      return box;
    }

    // Re-open editors for drafts the morph destroyed, so typing survives updates.
    function restoreDrafts(){
      Object.keys(drafts).forEach(function(k){
        if(!drafts[k]) return;
        var parts=k.split('|'), fid=parts[0], side=parts[1], line=parseInt(parts[2],10)||0;
        var f=fid?document.getElementById(fid):null; if(!f) return;
        if(f.querySelector('.td-cbox')) return;                // already open
        var anchor=line
          ? f.querySelector('pre.diff span.ln[data-ln="'+line+'"][data-side="'+(side||'new')+'"]')
          : f.querySelector('summary');
        if(anchor) openEditor(anchor,{file:fileOf(anchor)||'',fid:fid,side:side,line:line,snip:''});
      });
    }

    // ---- render the inline comment markers (both views) ----
    function mkMark(c, idx){
      var mk=document.createElement('div'); mk.className='td-cmark'; mk.title=c.text;
      mk.innerHTML='<span class="cbody"><span class="cloc"></span><span class="ctext"></span></span>'
        +'<span class="x" title="remove comment">✕</span>';
      mk.querySelector('.cloc').textContent=c.line?(base(c.file)+':'+c.line):'file-level';
      mk.querySelector('.ctext').textContent=c.text;
      mk.querySelector('.x').addEventListener('click',function(){ comments.splice(idx,1); changed(); });
      return mk;
    }
    function findFile(c){
      // Prefer the exact block the comment was written in; fall back to the path
      // for comments stored before fid was recorded.
      if(c.fid){ var byId=document.getElementById(c.fid); if(byId) return byId; }
      var out=null;
      q('.file').forEach(function(f){ if(out) return; var h=f.querySelector('summary h3');
        if(h&&h.textContent===c.file) out=f; }); return out; }
    // The stored snippet is truncated with an ellipsis for display; matching must
    // use the untruncated text, or any line over 100 chars can never re-anchor.
    function matchText(c){ return (c.snipFull||c.snip||'').replace(/…$/,''); }
    // insert after the previous mark on the same anchor, so several comments on
    // one line keep the order they were written in
    function insertAfter(anchor, node, lastMap, key){
      var ref=lastMap[key]||anchor;
      ref.parentNode.insertBefore(node, ref.nextSibling);
      lastMap[key]=node;
    }
    function renderMarks(){
      q('tr.td-cmark-row').forEach(function(m){ m.remove(); });
      q('.td-cmark').forEach(function(m){ m.remove(); });
      q('.commented').forEach(function(el){ el.classList.remove('commented'); });
      var lastU={}, lastS={};
      comments.forEach(function(c,idx){
        var f=findFile(c); if(!f) return;
        if(!c.line){ var s=f.querySelector('summary');
          if(s) insertAfter(s, mkMark(c,idx), lastU, 'sum:'+(c.fid||c.file)); return; }
        var txt=matchText(c);
        // unified
        var span=f.querySelector('pre.diff span.ln[data-ln="'+c.line+'"][data-side="'+(c.side||'new')+'"]');
        if(!span && txt){ f.querySelectorAll('pre.diff span.ln').forEach(function(el){
          if(!span && (el.textContent||'').indexOf(txt)>=0) span=el; }); }
        if(span){ span.classList.add('commented');
          insertAfter(span, mkMark(c,idx), lastU, 'u:'+(c.fid||c.file)+':'+c.side+':'+c.line); }
        // split
        var row=null, numSel=(c.side==='old')?'.lno':'.rno';
        f.querySelectorAll('table.split tr').forEach(function(tr){ if(row) return;
          var nc=tr.querySelector(numSel); if(nc&&(parseInt(nc.textContent,10)||0)===c.line) row=tr; });
        if(!row && txt){ f.querySelectorAll('table.split tr').forEach(function(tr){ if(row) return;
          if((tr.textContent||'').indexOf(txt)>=0) row=tr; }); }
        if(row){ row.classList.add('commented');
          // a comment on a collapsed context line would otherwise render inside a
          // hidden region — open that run so the card is actually visible
          if(row.classList.contains('hid')){
            var g=row.getAttribute('data-grp');
            if(g) q('tr.exp[data-grp="'+g+'"]').forEach(function(ex){ if(!ex.classList.contains('open')) ex.click(); });
          }
          var w=document.createElement('tr'); w.className='td-cmark-row';
          var td=document.createElement('td'); td.colSpan=4; td.appendChild(mkMark(c,idx)); w.appendChild(td);
          insertAfter(row, w, lastS, 's:'+(c.fid||c.file)+':'+c.side+':'+c.line); }
      });
    }

    document.addEventListener('click',function(e){
      if(!e.target.closest) return;
      var ln=e.target.closest('pre.diff span.ln.add, pre.diff span.ln.del, pre.diff span.ln.ctx, table.split td.cell');
      if(!ln || ln.classList.contains('blank')) return;
      var sel=window.getSelection&&window.getSelection(); if(sel&&!sel.isCollapsed) return;
      var file=fileOf(ln); if(!file) return;
      var info=lineInfo(ln);
      openEditor(ln, {file:file, fid:fidOf(ln), side:info.side, line:info.line, snip:info.snip});
    });

    function addFileButtons(){
      q('.file > summary').forEach(function(s){
        if(s.dataset.fcb) return; s.dataset.fcb='1';
        var b=document.createElement('button'); b.className='fcmt'; b.type='button'; b.textContent='💬 comment';
        b.addEventListener('click',function(ev){ ev.preventDefault(); ev.stopPropagation();
          var f=s.closest('.file'); if(f&&!f.open) f.open=true;
          var h=s.querySelector('h3');
          openEditor(s, {file:h?h.textContent:'', fid:(f&&f.id)||'', side:'', line:0, snip:''}); });
        s.appendChild(b);
      });
    }

    sendB.addEventListener('click',function(){
      if(!comments.length) return;
      if(C){
        var cur=C.value(), block=compile();
        if(cur.trim()==='' || cur===lastAuto){ C.set(block); lastAuto=block; }
        else {
          // The composer holds text the user typed. Appending keeps BOTH — the
          // previous behaviour silently sent their text and left the comments
          // stuck in the bar, looking like Send had failed.
          var merged=cur.replace(/\\s+$/,'')+'\\n\\n'+block;
          C.set(merged); lastAuto=merged;
        }
        C.focus(); C.send(); }
      else {   // file:// — no terminal target; copy the compiled prompt
        var text=compile();
        try{ navigator.clipboard.writeText(text).then(function(){ barN.textContent='copied to clipboard'; },
          function(){ barN.textContent='copy failed'; }); }
        catch(e){ barN.textContent='clipboard unavailable'; }
      }
    });
    // once the composer sends the comment prompt, consume the comments
    if(C){ C.onAfterSend=function(sent){
      if(comments.length && sent===lastAuto){ comments=[]; lastAuto=''; changed(); } }; }

    window.__td_afterContent=function(){ addFileButtons(); renderMarks(); restoreDrafts(); };
    addFileButtons(); renderMarks(); updateBar(); syncComposer();
  })();

  // ---- optimistic turn: show the prompt immediately on send -------------
  // The Stop/PostToolUse hooks can take many seconds to produce the first
  // rendering of a new turn, during which the page looked like nothing had
  // happened. Insert a placeholder straight away; the next morph replaces it
  // with the server-rendered turn (same id, so morphdom reconciles in place).
  (function(){
    var C=window.__tdComposer;
    if(!C||!C.present) return;
    function addPending(text){
      var wrap=document.querySelector('.wrap'); if(!wrap) return;
      var n=q('details.turn').length+1;
      var d=document.createElement('details');
      d.className='turn pending'; d.id='turn-'+n; d.open=true;
      d.setAttribute('data-sig','pending'); d.setAttribute('data-changes','0');
      var s=document.createElement('summary');
      s.innerHTML="<span class='tn'></span><span class='ts'></span><span class='pin'></span>"
        +"<span class='working'><span class='dot'></span>working</span>";
      s.querySelector('.tn').textContent='#'+n;
      s.querySelector('.ts').textContent='just now';
      s.querySelector('.pin').textContent=text.replace(/\\s+/g,' ').slice(0,110);
      d.appendChild(s);
      var b=document.createElement('div'); b.className='body';
      var bq=document.createElement('blockquote'); bq.className='prompt'; bq.textContent=text;
      var note=document.createElement('div'); note.className='pending-note';
      note.textContent='Prompt sent — waiting for the first update from the session…';
      b.appendChild(bq); b.appendChild(note); d.appendChild(b);
      wrap.appendChild(d);
      try{ applyMarks(); }catch(e){}
      d.scrollIntoView({behavior:'smooth',block:'start'});
    }
    var prev=C.onAfterSend;
    C.onAfterSend=function(sent){ try{ if(prev) prev(sent); }catch(e){} addPending(sent); };
  })();

  // ---- keyboard navigation ---------------------------------------------
  (function(){
    function inEditor(el){
      if(!el) return false;
      if(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.isContentEditable) return true;
      return !!(el.closest && el.closest('.CodeMirror'));
    }
    function visible(){
      return q('details.turn').filter(function(d){ return d.style.display!=='none'; });
    }
    function currentIdx(ts){
      var y=window.scrollY+90, best=0;
      ts.forEach(function(d,i){
        if(d.getBoundingClientRect().top+window.scrollY<=y) best=i; });
      return best;
    }
    function go(delta){
      var ts=visible(); if(!ts.length) return;
      var i=currentIdx(ts)+delta;
      if(i<0) i=0; if(i>ts.length-1) i=ts.length-1;
      var d=ts[i];
      if(!d.open) d.open=true;
      d.scrollIntoView({behavior:'smooth',block:'start'});
    }
    document.addEventListener('keydown',function(e){
      if(e.ctrlKey && !e.altKey && !e.metaKey &&
         (e.key==='ArrowUp'||e.key==='ArrowDown')){
        e.preventDefault(); go(e.key==='ArrowUp'?-1:1); return;
      }
      // Type-to-compose: a bare printable key with nothing focused starts a
      // prompt instead of being swallowed by the document.
      if(e.ctrlKey||e.metaKey||e.altKey) return;
      if(inEditor(document.activeElement)) return;
      if(e.key==null||e.key.length!==1) return;
      var C=window.__tdComposer;
      if(C&&C.present&&C.insert){ e.preventDefault(); C.insert(e.key); }
    });
  })();
})();
"""


def esc(s):
    """Escape for HTML. Quotes included: much of this file interpolates esc()
    output into single-quoted attributes (id=, data-*, <meta content=>), so
    leaving quotes raw allowed a file path or session title to break out of the
    attribute and inject an event handler."""
    return html.escape(s)


# Only these schemes may appear in a rendered link. Agent answers can quote a
# malicious README / web page / MCP result verbatim, and the report is served
# from the same origin as the prompt-injection endpoint, so a javascript: or
# data: href is a straight path to driving the user's terminal.
_SAFE_URL_RE = re.compile(r"^(?:https?://|mailto:|/|\#|\./|\.\./)", re.I)


# ---------------------------------------------------------------- mini markdown
def _md_inline(s):
    """Inline markdown -> HTML on a single text run. Escapes first, so embedded
    HTML in agent text is shown literally."""
    s = esc(s)
    codes = []
    s = re.sub(r"`([^`]+)`", lambda m: codes.append(m.group(1)) or f"\x00C{len(codes)-1}\x00", s)

    def _link(m):
        label, url = m.group(1), m.group(2)
        # url is already HTML-escaped by the esc() above, so unescape before
        # testing the scheme (&#x27; etc. must not hide a javascript: prefix).
        raw = html.unescape(url)
        if not _SAFE_URL_RE.match(raw.strip()):
            return f"{label} ({url})"      # render inert, keep the text visible
        return f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'

    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, s)
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


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def diff_html(lines, keep=False, anchors=True):
    """Unified diff view. keep=True marks it to stay visible even in split mode
    (used for the hunks fallback, which has no clean before/after to split).

    anchors=True emits data-ln/data-side so review comments can bind to a real
    file line. Pass anchors=False for the hunks fallback: there the lines are a
    concatenation of independent per-edit diffs whose numbering restarts at 1
    each time, so the values would be duplicated *and* wrong — a comment would
    land on the wrong edit and the prompt would cite a bogus file:line."""
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... (diff truncated at {MAX_DIFF_LINES} lines)"]
    out = []
    old_no = new_no = None   # 1-based counters, tracked from @@ hunk headers
    in_hunk = False
    for idx, ln in enumerate(lines):
        attrs = ""
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        # A ---/+++ pair starts a new file header block (the hunks fallback
        # concatenates several unified_diff outputs, so this can happen mid-list).
        if ln.startswith("--- ") and nxt.startswith("+++ "):
            in_hunk = False
        # Only treat ---/+++ as a header OUTSIDE a hunk. Inside one they are
        # ordinary content: deleting a markdown "---" rule yields the diff line
        # "----", and mistaking it for a header desyncs every later line number.
        if not in_hunk and (ln.startswith("+++") or ln.startswith("---")):
            cls = "meta"
        elif ln.startswith("@@"):
            cls = "hunk"
            m = _HUNK_RE.match(ln)
            if m:
                old_no, new_no = int(m.group(1)), int(m.group(2))
                in_hunk = True
        elif ln.startswith("+"):
            cls = "add"
            if new_no is not None and anchors:
                attrs = f' data-ln="{new_no}" data-side="new"'
                new_no += 1
        elif ln.startswith("-"):
            cls = "del"
            if old_no is not None and anchors:
                attrs = f' data-ln="{old_no}" data-side="old"'
                old_no += 1
        else:
            cls = "ctx"
            if new_no is not None and anchors:
                attrs = f' data-ln="{new_no}" data-side="new"'
                new_no += 1
            if old_no is not None:
                old_no += 1
        out.append(f'<span class="ln {cls}"{attrs}>{esc(ln) if ln else "&nbsp;"}</span>')
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
    # NOTE: the cap is applied to *visible* rows further down, after the
    # hide/context pass. Truncating the raw row list here instead would drop any
    # change past row MAX_DIFF_LINES entirely — in a long file the side-by-side
    # view would show nothing but collapsed context.
    rows = split_rows(rec.get("before"), rec.get("after"))
    n = len(rows)
    truncated = False
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
    shown = 0        # visible (non-collapsed) rows emitted so far
    emitted = 0      # every <tr> emitted, collapsed ones included
    while i < n:
        if hide[i]:
            j = i
            while j < n and hide[j]:
                j += 1
            count = j - i
            gid = f"{key}-g{grp}"
            grp += 1
            plural = "s" if count != 1 else ""
            # Keep the total DOM bounded on very large files: past the ceiling,
            # collapsed runs are summarised instead of being emitted row by row.
            expandable = (count <= SPLIT_HIDDEN_RUN_MAX
                          and emitted + count <= MAX_SPLIT_ROWS)
            label = (f"⋯ show {count} unchanged line{plural}" if expandable
                     else f"⋯ {count} unchanged line{plural} (not loaded)")
            out.append(f"<tr class='exp' data-grp='{esc(gid)}'><td class='lno'></td>"
                       f"<td class='cell exp-cell' colspan='3'>{label}</td></tr>")
            if expandable:
                for k in range(i, j):
                    out.append(_split_tr(rows[k], hidden=True, gid=gid))
                emitted += count
            i = j
        else:
            if shown >= MAX_DIFF_LINES:
                truncated = True
                break
            out.append(_split_tr(rows[i]))
            shown += 1
            emitted += 1
            i += 1
    if truncated:
        out.append(f"<tr><td class='lno'></td><td class='cell meta' colspan='3'>"
                   f"… truncated at {MAX_DIFF_LINES} changed/context rows</td></tr>")
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
        P.append(diff_html(lines, anchors=False))
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
    # signature of the page template (CSS+JS): if it changes, open pages full-reload
    # instead of morphing, so code changes actually show up without a manual refresh
    build = hashlib.md5((CSS + JS).encode("utf-8")).hexdigest()[:10]
    P.append(f"<meta name='td-build' content='{build}'>")
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
    # NOTE: this key must match skey('view') in the main script — session-scoped,
    # so two sessions open side by side don't share the view toggle.
    _sid_js = json.dumps(session_id_of(session_path))
    early = ("<script>try{var _k='td:'+%s+':view';"
             "if((sessionStorage.getItem(_k)||'unified')==='split')"
             "document.body.classList.add('view-split');}catch(e){}</script>" % _sid_js)
    # composer editor styles — load only over http (served by --serve); harmless 404 on file://
    P.append("<link rel='stylesheet' href='/assets/easymde.min.css'>")
    sidebar = ("<div id='sb-backdrop'></div>"
               "<aside id='sidebar'><div class='sb-head'><b>Sessions</b>"
               "<button class='sb-close' id='sb-close' type='button' aria-label='Close'>×</button></div>"
               "<div class='sb-list' id='sb-list'></div></aside>")
    P.append(f"</head><body class='{'hl' if hl_on else ''}'>{early}{sidebar}<div class='wrap'>")
    ar_btn = ("<button class='vt' id='ar' type='button'>⟳ Auto-reload</button>"
              if refresh > 0 else "")
    heading = esc(title) if title else "Turn-by-turn changes"
    chips = ("<button class='vt fchip' data-cat='regular' type='button'>Regular</button>"
             "<button class='vt fchip' data-cat='starred' type='button'>★ Starred</button>"
             "<button class='vt fchip' data-cat='hidden' type='button'>Hidden</button>"
             "<button class='vt fchip fchg' data-chg='1' type='button'>◆ Changes only</button>")
    controls = ("<button class='vt' id='vt' type='button'>◧ Side-by-side</button>"
                f"{ar_btn}{chips}")
    P.append("<header><div class='htop'>"
             "<button class='sbtoggle' id='sbtoggle' type='button' title='Sessions' aria-label='Sessions'>☰</button>"
             f"<h1>{heading}</h1>"
             "<div class='hmenu'><button class='hmenu-sum' id='hmenu-sum' type='button' title='Options'>⋯</button>"
             f"<div class='hmenu-body'>{controls}</div></div>"
             "</div>")
    cwd_line = f"cwd: <code>{esc(cwd)}</code> · " if cwd else ""
    P.append(f"<div class='sub'>{cwd_line}{len(turns)} turn(s) · generated {gen} {live}</div></header>")

    # turns
    for i, t in enumerate(turns, 1):
        ts = _local_ts(t["ts"])
        op = " open" if i == len(turns) else ""   # fresh start: only the last turn open
        work = ("<span class='working'><span class='dot'></span>working</span>"
                if (in_progress and i == len(turns)) else "")
        # cheap content fingerprint so the live morph can skip unchanged turns entirely
        # Subagent panels are grafted on by attach_agents AFTER the process
        # timeline is built, so agent state must be part of the fingerprint —
        # otherwise a finished subagent's result and file diffs never reach an
        # open page (morphdom skips the subtree as "unchanged").
        agents = t.get("agents", [])
        sig = "%d.%d.%d.%d.%d.%d.%d.%d.%d" % (
              len(t["prompt"]), len(t.get("answer", "")),
              len(t.get("process", [])), len(t["order"]),
              sum(len(t["files"][p].get("ops", [])) for p in t["order"]),
              1 if (in_progress and i == len(turns)) else 0,
              len(agents),
              sum(len(a.get("result", "")) for a in agents),
              sum(len(a.get("order", [])) for a in agents))
        nchg = len(t["order"]) + sum(len(ag.get("order", [])) for ag in t.get("agents", []))
        chg = (f"<span class='chg' title='{nchg} file(s) changed'>◆ {nchg}</span>" if nchg else "")
        P.append(f"<details class='turn{' has-changes' if nchg else ''}' id='turn-{i}'{op} "
                 f"data-sig='{sig}' data-changes='{nchg}'><summary>"
                 f"<span class='tn'>#{i}</span>{chg}"
                 + (f"<span class='ts'>{esc(ts)}</span>" if ts else "")
                 + f"<span class='pin'>{esc(snippet(t['prompt'], 110))}</span>{work}"
                 "<button class='tbtn star' type='button' title='Star this turn'>☆</button>"
                 "<button class='tbtn hidebtn' type='button' title='Hide this turn' aria-label='Hide'>⊘</button>"
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
    subs = scan_subagents(session_path)
    turns = build_turns(entries, subs=subs)
    attach_agents(turns, session_path, entries, subs=subs)
    title = session_title(entries)
    cwd = entries_cwd(entries)
    if fmt == "md":
        content = render_md(turns, session_path, title)
    else:
        content = render_html(turns, session_path, refresh, title, in_progress, cwd)
    # Unique temp name per writer: the Stop hook, the PostToolUse hook and the
    # server's SSE regen all target the same report concurrently, and a shared
    # "<sid>.html.tmp" let one truncate the file another was still writing —
    # publishing a half-written report through the os.replace below.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out_path.parent),
                                    prefix=out_path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, out_path)   # atomic: viewers never see a half-written report
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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


def _herdr_names(ws_id, tab_id):
    """Resolve a herdr workspace_id/tab_id to their human labels (best effort)."""
    import subprocess
    ws = tab = ""
    try:
        if ws_id:
            r = subprocess.run(["herdr", "workspace", "list"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                for w in json.loads(r.stdout).get("result", {}).get("workspaces", []):
                    if w.get("workspace_id") == ws_id:
                        ws = w.get("label") or ""
                        break
    except Exception:
        pass
    try:
        if tab_id:
            r = subprocess.run(["herdr", "tab", "list"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                for t in json.loads(r.stdout).get("result", {}).get("tabs", []):
                    if t.get("tab_id") == tab_id:
                        tab = t.get("label") or ""
                        break
    except Exception:
        pass
    return ws, tab


def _find_pane(sid, names=False):
    """Locate the terminal pane hosting a session. Herdr is exact (its panes
    advertise their claude session id); tmux uses a cwd heuristic; zellij only via
    a manual panes.json pin (its CLI can't target arbitrary panes). With names=True,
    a herdr match also resolves the human workspace/tab labels (for display only —
    injection always uses the internal pane_id)."""
    import subprocess
    m = _load_panes_map().get(sid)
    if isinstance(m, dict) and m.get("backend") and m.get("target"):
        return m["backend"], m["target"], {"source": "panes.json"}
    try:
        r = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for p in json.loads(r.stdout).get("result", {}).get("panes", []):
                if (p.get("agent_session") or {}).get("value") == sid:
                    info = {"status": p.get("agent_status", "")}
                    if names:
                        ws, tab = _herdr_names(p.get("workspace_id"), p.get("tab_id"))
                        title = p.get("terminal_title_stripped") or ""
                        parts = [x for x in (ws, tab) if x]
                        info["label"] = " › ".join(parts) if parts else p["pane_id"]
                        if ws:
                            info["ws"] = ws
                        if tab:
                            info["tab"] = tab
                        if title:
                            info["title"] = title
                    return "herdr", p["pane_id"], info
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
# Everything in C0/C1 except \n and \t. \r matters most: it reaches the TUI as a
# literal Enter, and ESC would let the text close its own bracketed paste and
# then drive the interface as keystrokes (answering permission prompts, etc.).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize_prompt(text):
    """Make text safe to hand a terminal: no escape sequences, no bare carriage
    returns, no way to terminate the bracketed paste early."""
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL_RE.sub("", body)


def inject_prompt(sid, text):
    """Type a prompt into the session's terminal pane and press Enter. The text
    is always delivered as one bracketed-paste block so the TUI doesn't submit on
    each newline, and is stripped of control characters first so it cannot leave
    paste mode and act as keystrokes."""
    import subprocess
    backend, target, info = _find_pane(sid)
    if not backend:
        return {"ok": False, **info}
    body = _sanitize_prompt(text).rstrip("\n")
    if not body.strip():
        return {"ok": False, "error": "empty prompt"}
    wrapped = _PASTE_OPEN + body + _PASTE_CLOSE
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
    """Stable per-machine token gating POST /prompt (which types into the user's
    terminal) and every request that did not arrive over loopback.

    Loopback binding is only part of the trust boundary — see host_kind() — so
    this token is what authenticates remote (proxied) clients."""
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
        # Create with the restrictive mode already applied: write_text() + a
        # later chmod leaves a window where the credential is world-readable.
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(t)
    except FileExistsError:
        try:
            return f.read_text(encoding="utf-8").strip() or t
        except OSError:
            pass
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


def _herdr_statuses():
    """Map claude session_id -> herdr agent_status ('working'/'idle'/…) if herdr runs."""
    import subprocess
    try:
        r = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, timeout=4)
        if r.returncode != 0:
            return {}
        out = {}
        for p in json.loads(r.stdout).get("result", {}).get("panes", []):
            s = (p.get("agent_session") or {}).get("value")
            if s:
                out[s] = p.get("agent_status", "")
        return out
    except Exception:
        return {}


def _tail_status(tx, max_bytes=20000):
    """Peek at the end of a transcript: 'blocked' if it ends on an unanswered
    AskUserQuestion/ExitPlanMode or a trailing question, else None."""
    try:
        size = tx.stat().st_size
        with open(tx, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    answered, pending_ask, last_text = set(), None, ""
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        for tid, _t in tool_results(e):
            answered.add(tid)
        if e.get("type") == "assistant":
            c = e.get("message", {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") in ("AskUserQuestion", "ExitPlanMode"):
                        pending_ask = b.get("id")
                    elif b.get("type") == "text" and (b.get("text") or "").strip():
                        last_text = b["text"]
    if pending_ask and pending_ask not in answered:
        return "blocked"
    if last_text.strip().endswith("?"):
        return "blocked"
    return None


def _session_status(sid, tx, herdr_map):
    hs = herdr_map.get(sid)
    if hs == "working":
        return "working"
    if hs is None and tx is not None:   # no herdr info -> recency heuristic
        try:
            if time.time() - tx.stat().st_mtime < 8:
                return "working"
        except OSError:
            pass
    if tx is not None and _tail_status(tx) == "blocked":
        return "blocked"
    return "finished"


def _sessions_json(rd):
    herdr_map = _herdr_statuses()
    out = []
    for f in rd.glob("*.html"):
        sid = f.stem
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            head = ""

        def meta(n):
            m = re.search(r"<meta name='td-%s' content='([^']*)'>" % n, head)
            return html.unescape(m.group(1)) if m else ""

        tx = _transcript_for(sid)
        out.append({"sid": sid, "file": f.name, "name": meta("name") or sid,
                    "cwd": meta("cwd"), "turns": meta("turns"),
                    "status": _session_status(sid, tx, herdr_map),
                    "mtime": f.stat().st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ---------------------------------------------------------------- slash commands
_BUILTIN_CMDS = [
    ("help", "List available commands"),
    ("clear", "Clear conversation history and free the context"),
    ("compact", "Summarize and compact the conversation"),
    ("context", "Show what's using the context window"),
    ("review", "Review a pull request / code changes"),
    ("cost", "Show token usage and cost for this session"),
    ("model", "Switch the active model"),
    ("config", "Open the settings panel"),
    ("resume", "Resume a previous session"),
    ("init", "Bootstrap a CLAUDE.md for the project"),
    ("memory", "Edit Claude memory files"),
    ("agents", "Manage subagents"),
    ("mcp", "Manage MCP servers"),
    ("status", "Show session / account status"),
    ("pr-comments", "Fetch and show PR comments"),
    ("vim", "Toggle vim editing mode"),
    ("terminal-setup", "Configure terminal key bindings"),
    ("doctor", "Diagnose installation health"),
]


def _parse_frontmatter(path):
    """(name, description) from a markdown file's leading YAML-ish frontmatter."""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    if not txt.startswith("---"):
        return None, ""
    end = txt.find("\n---", 3)
    fm = txt[3:end] if end != -1 else ""
    name, desc = None, ""
    for line in fm.splitlines():
        m = re.match(r"\s*([A-Za-z][\w-]*)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        k, v = m.group(1).lower(), m.group(2).strip().strip('"').strip("'")
        if k == "description" and not desc:
            desc = v
        elif k == "name" and name is None:
            name = v
    return name, desc


def _short(d, n=150):
    d = (d or "").strip()
    return d[:n].rstrip() + "…" if len(d) > n else d


def _scan_commands(base, source, out, seen):
    if not base.is_dir():
        return
    for f in sorted(base.rglob("*.md")):
        name = ":".join(f.relative_to(base).with_suffix("").parts)
        if not name or name in seen:
            continue
        _n, desc = _parse_frontmatter(f)
        seen.add(name)
        out.append({"name": name, "desc": _short(desc), "source": source})


def _scan_skills(base, source, out, seen):
    if not base.is_dir():
        return
    for sk in sorted(base.glob("*/SKILL.md")):
        name_ov, desc = _parse_frontmatter(sk)
        name = name_ov or sk.parent.name
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "desc": _short(desc), "source": source})


def _scan_plugins(cwd, out, seen):
    reg = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except Exception:
        return
    for key, entries in (data.get("plugins") or {}).items():
        plug = key.split("@", 1)[0]
        for e in entries if isinstance(entries, list) else []:
            if e.get("scope") == "project" and cwd and e.get("projectPath") not in (cwd, str(cwd)):
                continue
            ip = e.get("installPath")
            if not ip:
                continue
            cdir = Path(ip) / "commands"
            if not cdir.is_dir():
                continue
            for f in sorted(cdir.rglob("*.md")):
                cn = ":".join(f.relative_to(cdir).with_suffix("").parts)
                name = f"{plug}:{cn}"
                if name in seen:
                    continue
                _n, desc = _parse_frontmatter(f)
                seen.add(name)
                out.append({"name": name, "desc": _short(desc), "source": "plugin:" + plug})


def _slash_commands(cwd):
    """Discover slash commands available to a session: project + user commands and
    skills, enabled plugin commands, plus a curated set of built-ins."""
    out, seen = [], set()
    cwdp = Path(cwd) if cwd else None
    # only treat cwd/.claude as a distinct "project" source if it isn't the user config dir
    proj_distinct = False
    if cwdp:
        try:
            proj_distinct = (cwdp / ".claude").resolve() != CLAUDE_DIR.resolve()
        except OSError:
            proj_distinct = True
    if proj_distinct:
        _scan_commands(cwdp / ".claude" / "commands", "project", out, seen)
        _scan_skills(cwdp / ".claude" / "skills", "skill·project", out, seen)
    _scan_commands(CLAUDE_DIR / "commands", "user", out, seen)
    _scan_skills(CLAUDE_DIR / "skills", "skill", out, seen)
    try:
        _scan_plugins(cwd, out, seen)
    except Exception:
        pass
    for nm, desc in _BUILTIN_CMDS:
        if nm in seen:
            continue
        seen.add(nm)
        out.append({"name": nm, "desc": desc, "source": "built-in"})
    prio = {"built-in": 3}
    out.sort(key=lambda c: (prio.get(c["source"], 0), c["name"]))
    return out


def _report_cwd(sid):
    try:
        head = report_path_for(sid).read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return ""
    m = re.search(r"<meta name='td-cwd' content='([^']*)'>", head)
    return html.unescape(m.group(1)) if m else ""


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


# ---------------------------------------------------------------- server auth
# The server binds loopback, but that is NOT the whole trust boundary:
#   * a reverse proxy (tailscale serve) can forward remote traffic to it, and
#   * any web page the user visits can DNS-rebind its own hostname to 127.0.0.1
#     and become same-origin with us.
# Both are distinguishable by the Host header the client sends, so every request
# is classified before it is served.
_LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}
MAX_POST_BYTES = 64 * 1024      # a prompt; anything larger is abuse
MAX_SSE_STREAMS = 32            # each live stream holds a thread for its lifetime
_SID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SSE_LOCK = threading.Lock()
_SSE_COUNT = [0]


def _allowed_remote_hosts():
    """Extra hostnames allowed to reach the server through a proxy.

    Defaults to Tailscale MagicDNS names so `tailscale serve` keeps working;
    override/extend with TURN_DIFFS_ALLOWED_HOSTS='host1,*.example.ts.net'.
    """
    raw = os.environ.get("TURN_DIFFS_ALLOWED_HOSTS", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def host_kind(host_header):
    """'local' (loopback, trusted by the OS), 'remote' (proxied in, must
    authenticate), or None (reject — almost certainly DNS rebinding)."""
    h = (host_header or "").strip().lower()
    name = (h.split("]")[0] + "]") if h.startswith("[") else h.split(":")[0]
    if name in _LOOPBACK_HOSTS:
        return "local"
    for pat in _allowed_remote_hosts():
        base = pat.split(":")[0]
        if name == base or (base.startswith("*.") and name.endswith(base[1:])):
            return "remote"
    if name.endswith(".ts.net"):        # Tailscale MagicDNS
        return "remote"
    return None


def safe_sid(sid):
    """Session ids come straight off the URL and are used to build filesystem
    paths, so they are allowlisted rather than sanitised."""
    return bool(sid) and ".." not in sid and bool(_SID_RE.match(sid))


def token_ok(supplied, token):
    return bool(supplied) and hmac.compare_digest(str(supplied), str(token))


def serve(port, ready=None):
    """Live mode: serve the reports dir on 127.0.0.1 with SSE push-on-change and a
    token-guarded POST /prompt/<sid> that types a prompt into the session's terminal
    pane. The same HTML still works from file:// with no server. Ctrl-C to stop.

    ready(name, obj) — optional hook called with ('srv', server) once bound, so
    callers (and tests) can shut the server down cleanly."""
    import http.server
    rd = reports_dir().resolve()
    rd.mkdir(parents=True, exist_ok=True)
    assets = ASSETS_DIR.resolve()
    token = _serve_token()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code, ctype, body, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _report(self, name):
            t = (rd / name).resolve()
            return t if (t.parent == rd and t.suffix == ".html" and t.exists()) else None

        # ---- auth -------------------------------------------------------
        def _query_token(self):
            q = self.path.split("?", 1)
            if len(q) < 2:
                return ""
            from urllib.parse import parse_qs
            return (parse_qs(q[1]).get("t") or [""])[0]

        def _cookie_token(self):
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "td_token":
                    return v
            return ""

        def _auth(self):
            """Classify the request. Returns 'local'/'remote', or None after
            having already sent the rejection."""
            kind = host_kind(self.headers.get("Host"))
            if kind is None:
                # Unknown Host: a browser that rebound a hostile hostname to
                # 127.0.0.1, not a real client of ours.
                self._send(403, "text/plain; charset=utf-8", b"forbidden host")
                return None
            if kind == "local":
                return kind
            supplied = (self.headers.get("X-TD-Token")
                        or self._query_token() or self._cookie_token())
            if not token_ok(supplied, token):
                self._send(401, "text/plain; charset=utf-8",
                           b"authentication required: append ?t=<token> once "
                           b"(see `turn-diffs --status`)")
                return None
            return kind

        def _cookie_header(self):
            """Trade a ?t=<token> URL for an HttpOnly cookie so the token stops
            travelling in URLs (and out of reach of page scripts)."""
            if self._query_token() and not self._cookie_token():
                return [("Set-Cookie",
                         f"td_token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800")]
            return []

        def do_GET(self):
            p = self.path.split("?")[0]
            # Identity probe: answered before auth so a caller can tell "turn-diffs
            # is on this port" from "some other app is on this port" without a
            # token. Reveals nothing but the magic string and the version.
            if p == "/healthz":
                return self._send(200, "application/json; charset=utf-8",
                                  json.dumps({"app": HEALTH_MAGIC, "version": __version__}).encode())
            if self._auth() is None:
                return
            extra = self._cookie_header()
            if p == "/":
                return self._send(200, "text/html; charset=utf-8", _index_html(rd), extra)
            if p == "/sessions":
                body = json.dumps(_sessions_json(rd)).encode()
                return self._send(200, "application/json; charset=utf-8", body, extra)
            if p.startswith("/commands/"):
                sid = p[len("/commands/"):]
                if sid.endswith(".html"):
                    sid = sid[:-5]
                if not safe_sid(sid):
                    return self._send(400, "application/json", b'{"error":"bad session id"}')
                body = json.dumps(_slash_commands(_report_cwd(sid))).encode()
                return self._send(200, "application/json; charset=utf-8", body, extra)
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
                if not safe_sid(sid):
                    return self._send(400, "application/json", b'{"ok":false,"error":"bad session id"}')
                backend, target, info = _find_pane(sid, names=True)
                res = ({"ok": True, "backend": backend, "target": target, **info}
                       if backend else {"ok": False, **info})
                return self._send(200, "application/json; charset=utf-8", json.dumps(res).encode())
            if p.startswith("/events/"):
                name = p[len("/events/"):]
                t = self._report(name)
                if not t:
                    return self._send(404, "text/plain", b"nope")
                sid = name[:-5] if name.endswith(".html") else name
                if not safe_sid(sid):
                    return self._send(400, "text/plain", b"bad session id")
                # Each stream parks a thread for its whole lifetime, so the
                # number of them has to be bounded.
                with _SSE_LOCK:
                    if _SSE_COUNT[0] >= MAX_SSE_STREAMS:
                        return self._send(503, "text/plain", b"too many live streams")
                    _SSE_COUNT[0] += 1
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
                finally:
                    with _SSE_LOCK:
                        _SSE_COUNT[0] -= 1
            t = self._report(p.lstrip("/"))
            if t:
                body = t.read_bytes()
                inj = ("<script>window.__TD_TOKEN__=%r;</script>" % token).encode()
                body = body.replace(b"</head>", inj + b"</head>", 1)
                return self._send(200, "text/html; charset=utf-8", body, extra)
            return self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if self._auth() is None:
                return
            p = self.path.split("?")[0]
            if not p.startswith("/prompt/"):
                return self._send(404, "text/plain", b"not found")
            sid = p[len("/prompt/"):]
            if sid.endswith(".html"):
                sid = sid[:-5]
            if not safe_sid(sid):
                return self._send(400, "application/json", b'{"ok":false,"error":"bad session id"}')
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, "application/json", b'{"ok":false,"error":"bad length"}')
            # A prompt is small; an unbounded read lets any client balloon the process.
            if n <= 0 or n > MAX_POST_BYTES:
                return self._send(413, "application/json", b'{"ok":false,"error":"body too large"}')
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except Exception:
                return self._send(400, "application/json", b'{"ok":false,"error":"bad body"}')
            # POST types into the user's terminal, so it is token-gated on every
            # host, loopback included — a local browser page is not trusted.
            supplied = (self.headers.get("X-TD-Token") or data.get("token")
                        or self._cookie_token())
            if not token_ok(supplied, token):
                return self._send(401, "application/json", b'{"ok":false,"error":"bad token"}')
            res = inject_prompt(sid, data.get("text", ""))
            body = json.dumps(res).encode()
            return self._send(200 if res.get("ok") else 409, "application/json; charset=utf-8", body)

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        # Two sessions can race to start the singleton; the loser should exit
        # quietly rather than dumping a traceback into serve.log.
        print(f"turn-diffs: cannot bind port {port}: {exc}", file=sys.stderr)
        return 0 if _port_busy(port) else 1
    try:
        (DATA_DIR / "serve.pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    if ready:
        ready("srv", srv)
    print(f"turn-diffs live server: http://127.0.0.1:{port}/   (Ctrl-C to stop)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        srv.server_close()
        try:
            (DATA_DIR / "serve.pid").unlink(missing_ok=True)
        except OSError:
            pass
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


def _hook_debug(msg):
    """Hooks are silent by design (async, off the critical path), which makes a
    misbehaving one undiagnosable. TURN_DIFFS_DEBUG=1 leaves a breadcrumb."""
    if not os.environ.get("TURN_DIFFS_DEBUG"):
        return
    try:
        with open(DATA_DIR / "hook.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def run_hook(fmt, refresh, out_override=None):
    """Driven by a Claude Code Stop hook. Reads the hook JSON from stdin. Runs only
    when THIS session was enabled via --enable; otherwise returns immediately. Writes
    to a per-session report file and never raises (it runs async, off the turn's
    critical path)."""
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        _hook_debug(f"bad stdin payload: {exc}")
        return 0
    sid = data.get("session_id")
    tp = data.get("transcript_path")
    session = Path(tp) if tp else None
    if not sid and session is not None:
        sid = session_id_of(session)
    if not is_enabled(sid):
        return 0  # off for this session -> do nothing, cheaply
    if session is None or not session.exists():
        _hook_debug(f"{sid}: transcript missing ({tp!r})")
        return 0
    out = Path(out_override) if out_override else report_path_for(sid, fmt)
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
        if event == "Stop":
            prune_reports()
    except Exception as exc:
        _hook_debug(f"{sid}: generate failed: {type(exc).__name__}: {exc}")
    return 0


def _rotate_log(path):
    """Keep serve.log from growing without bound across restarts."""
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def prune_reports():
    """Drop stale reports. Every report inlines ~170 KB of vendored assets and a
    long session can reach tens of MB, so without this the data dir grows forever
    (nothing else in the tool ever deletes one)."""
    rd = reports_dir()
    if not rd.exists():
        return 0
    cutoff = time.time() - MAX_REPORT_AGE_DAYS * 86400
    removed = 0
    try:
        files = sorted(rd.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    for i, f in enumerate(files):
        try:
            sid = f.stem
            if enabled_flag(sid).exists():
                continue          # never prune a session that is still reporting
            if i >= MAX_REPORTS_KEPT or f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    # also drop leftover temp files from interrupted writes
    for t in rd.glob("*.tmp"):
        try:
            if t.stat().st_mtime < time.time() - 3600:
                t.unlink()
        except OSError:
            pass
    return removed


def _resolve_session():
    """Identify the session this command belongs to.

    Claude Code exports CLAUDE_CODE_SESSION_ID into the tool environment; use it
    when present. The newest-transcript heuristic below is only a fallback — with
    two sessions open it can enable/disable the wrong one.
    """
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if safe_sid(sid):
        tx = _transcript_for(sid)
        if tx:
            return tx, sid
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
    """True only if *turn-diffs* is on this port.

    A bare TCP connect isn't enough: 8787 is a common dev port, and mistaking
    someone else's server for ours makes /turn-diffs print a link to their app
    while live updates silently never start."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as r:
            return json.loads(r.read() or b"{}").get("app") == HEALTH_MAGIC
    except Exception:
        return False


def _port_busy(port):
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
    if _port_busy(port):
        print(f"turn-diffs: port {port} is in use by another application; "
              f"set TURN_DIFFS_PORT to a free port.", file=sys.stderr)
        return None
    import subprocess
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_log(DATA_DIR / "serve.log")
        with open(DATA_DIR / "serve.log", "ab") as lf:
            # --dir must be propagated, or a custom DATA_DIR run serves reports
            # from the default location and every printed link 404s.
            cmd = [sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port)]
            if str(DATA_DIR) != str(CLAUDE_DIR / "turn-diffs"):
                cmd += ["--dir", str(DATA_DIR)]
            subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception:
        return None
    for _ in range(20):
        if _server_running(port):
            return port
        time.sleep(0.1)
    return None


def cmd_stop(port):
    """Shut down the detached live server. It is started with start_new_session=True
    so it outlives the session that spawned it; without this there is no supported
    way to stop it short of hunting the PID."""
    if not _server_running(port):
        print(f"turn-diffs: no live server on port {port}.")
        return 0
    pidfile = DATA_DIR / "serve.pid"
    pid = None
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        pass
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not _server_running(port):
                    pidfile.unlink(missing_ok=True)
                    print(f"turn-diffs: stopped live server (pid {pid}).")
                    return 0
                time.sleep(0.1)
        except (OSError, ProcessLookupError):
            pass
    print(f"turn-diffs: could not stop the server on port {port}; "
          f"no usable pid file at {pidfile}.", file=sys.stderr)
    return 1


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
    # The live server only serves .html (and only regenerates html), so a
    # markdown report has no live view — printing one would 404.
    port = ensure_server() if fmt == "html" else None
    if port:
        print(f"Live view: http://127.0.0.1:{port}/{out.name}")
        for url in _remote_urls(out.name):
            print(f"Remote:    {url}")
        print(f"Static fallback: {file_url(out)}")
    else:
        if fmt != "html":
            print("(markdown reports have no live view — use --format html for that)")
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
            for url in _remote_urls(out.name):
                print(f"Remote:    {url}")
        print(f"Static: {file_url(out)}")
    print(f"Reports dir: {reports_dir()}")
    return 0


def _remote_urls(name):
    """Tokenised URLs for reaching the report from another device.

    Remote (proxied) requests must authenticate — see host_kind() — so the link
    carries ?t=<token> once; the server then sets an HttpOnly cookie and later
    visits need no token in the URL."""
    out = []
    try:
        import subprocess
        r = subprocess.run(["tailscale", "serve", "status"],
                           capture_output=True, text=True, timeout=4)
        if r.returncode != 0:
            return out
        token = _serve_token()
        for m in re.finditer(r"^(https://\S+)", r.stdout, re.M):
            out.append(f"{m.group(1).rstrip('/')}/{name}?t={token}")
    except Exception:
        pass
    return out


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
    ap.add_argument("--port", type=int, default=None,
                    help="Port for --serve (default: $TURN_DIFFS_PORT or 8787)")
    ap.add_argument("--stop", action="store_true", help="Stop the background live server")
    ap.add_argument("--prune", action="store_true", help="Delete stale reports and exit")
    ap.add_argument("--version", action="version", version=f"turn-diffs {__version__}")
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
    if args.stop:
        return cmd_stop(args.port or _server_port())
    if args.prune:
        n = prune_reports()
        print(f"turn-diffs: pruned {n} stale report(s) from {reports_dir()}")
        return 0
    if args.serve:
        return serve(args.port or _server_port())

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
        return run_hook(args.format, refresh, args.output)

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
