#!/usr/bin/env python3
"""Test suite for turn-diffs.

Stdlib only (unittest), matching the tool itself. Run with:

    python3 -m unittest discover -s tests -v
    python3 tests/test_turn_diffs.py

The suite is deliberately split into two kinds of test:

  * REGRESSION tests pin behaviour that already works, so the security and
    correctness fixes can't quietly break the tool.
  * DEFECT tests encode the behaviour the code *should* have. They fail against
    the unfixed tree on purpose; that is the point of writing them first.

Every test isolates state through $TURN_DIFFS_DIR, so running the suite never
touches the user's real reports.
"""

import os
import tempfile

# Must be set before the module is loaded: DATA_DIR is resolved at import time.
_TMPDIR = tempfile.mkdtemp(prefix="turn-diffs-tests-")
os.environ["TURN_DIFFS_DIR"] = _TMPDIR

import http.client                                              # noqa: E402
import importlib.util                                           # noqa: E402
import json                                                     # noqa: E402
import pathlib                                                  # noqa: E402
import re                                                       # noqa: E402
import socket                                                   # noqa: E402
import threading                                                # noqa: E402
import time                                                     # noqa: E402
import unittest                                                 # noqa: E402
from concurrent.futures import ThreadPoolExecutor               # noqa: E402

_HERE = pathlib.Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "turn-diffs.py"


def _load():
    spec = importlib.util.spec_from_file_location("turn_diffs", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


td = _load()


# ---------------------------------------------------------------- helpers
def jsonl(*entries):
    """Write entries to a temp .jsonl transcript and return its Path."""
    p = pathlib.Path(tempfile.mkstemp(suffix=".jsonl", dir=_TMPDIR)[1])
    p.write_text("\n".join(json.dumps(e) if isinstance(e, dict) else str(e)
                           for e in entries), encoding="utf-8")
    return p


def user(text, ts="2026-01-01T10:00:00.000Z", **kw):
    e = {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts}
    e.update(kw)
    return e


def assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def tool_use(name, tid, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def out_path(suffix=".html"):
    return pathlib.Path(tempfile.mkstemp(suffix=suffix, dir=_TMPDIR)[1])


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ================================================================ pure helpers
class TestLocalTimestamps(unittest.TestCase):
    """REGRESSION: transcript timestamps are UTC and must render in local time."""

    def test_utc_z_converts_to_local(self):
        from datetime import datetime, timezone
        got = td._local_ts("2026-07-23T20:07:26.019Z")
        expect = (datetime(2026, 7, 23, 20, 7, 26, tzinfo=timezone.utc)
                  .astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(got, expect)

    def test_naive_timestamp_treated_as_utc(self):
        self.assertEqual(td._local_ts("2026-07-23T20:07:26"),
                         td._local_ts("2026-07-23T20:07:26Z"))

    def test_explicit_offset_is_honoured(self):
        self.assertEqual(td._local_ts("2026-07-23T20:07:26+00:00"),
                         td._local_ts("2026-07-23T20:07:26Z"))

    def test_empty_and_none(self):
        self.assertEqual(td._local_ts(""), "")
        self.assertEqual(td._local_ts(None), "")

    def test_unparseable_falls_back_without_raising(self):
        self.assertEqual(td._local_ts("not-a-date"), "not-a-date")


class TestEscaping(unittest.TestCase):
    """DEFECT: esc() output lands in single-quoted HTML attributes, so it must
    escape quotes. quote=False allows attribute injection."""

    def test_escapes_angle_brackets_and_amp(self):
        self.assertEqual(td.esc("<b>&</b>"), "&lt;b&gt;&amp;&lt;/b&gt;")

    def test_escapes_single_quote(self):
        self.assertNotIn("'", td.esc("it's"))

    def test_escapes_double_quote(self):
        self.assertNotIn('"', td.esc('say "hi"'))

    def test_file_path_with_quote_cannot_break_attribute(self):
        rec = td._new_rec({}, "/tmp/x.py")
        rec.update(applied=True, ops=[], before=None, after="x\n")
        html = td.file_block_html("/tmp/a'/onmouseover='alert(1).py", rec, "t1")
        self.assertNotIn("onmouseover='alert(1)", html)
        self.assertNotIn('onmouseover="alert(1)', html)

    def test_title_with_apostrophe_survives_meta_roundtrip(self):
        """_index_html/_sessions_json parse these metas with content='([^']*)'."""
        p = jsonl(user("hello"), assistant({"type": "text", "text": "hi"}))
        o = out_path()
        td.generate(p, o, "html", 0, in_progress=False)
        html = o.read_text(encoding="utf-8")
        for meta in ("td-name", "td-cwd"):
            m = re.search(r"<meta name='%s' content='([^']*)'" % meta, html)
            self.assertIsNotNone(m, f"{meta} meta must remain parseable")


def parsed_attrs(html_text):
    """Return every (tag, attr, value) actually parsed out of the markup.

    Substring checks are the wrong tool here: escaped text may legitimately
    contain the word "onmouseover". What matters is whether a browser would
    see an *attribute*.
    """
    from html.parser import HTMLParser

    found = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                found.append((tag, k.lower(), (v or "")))

    p = P()
    p.feed(html_text)
    return found


class TestMarkdownLinkSafety(unittest.TestCase):
    """DEFECT: agent answers are attacker-influenceable (a README, a scraped
    page, an MCP result). Links must not become script."""

    def assertNoEventHandlers(self, out):
        handlers = [a for _, a, _ in parsed_attrs(out) if a.startswith("on")]
        self.assertEqual(handlers, [], f"event handler injected: {out!r}")

    def assertNoDangerousHref(self, out):
        for tag, attr, val in parsed_attrs(out):
            if attr == "href":
                self.assertRegex(val.strip().lower(),
                                 r"^(https?://|mailto:|/|#|\./|\.\./)",
                                 f"unsafe href rendered: {val!r}")

    def test_javascript_scheme_is_not_emitted_as_href(self):
        self.assertNoDangerousHref(td._md_inline("[click](javascript:alert(1))"))

    def test_attribute_breakout_double_quote(self):
        out = td._md_inline('[go](a"/onmouseover="location=name)')
        self.assertNoEventHandlers(out)

    def test_attribute_breakout_single_quote(self):
        out = td._md_inline("[go](a'/onmouseover='location=name)")
        self.assertNoEventHandlers(out)

    def test_data_scheme_is_not_emitted_as_href(self):
        self.assertNoDangerousHref(td._md_inline("[x](data:text/html;base64,PHNjcmlwdD4=)"))

    def test_end_to_end_report_has_no_event_handlers(self):
        """The full chain: malicious answer text -> rendered report."""
        payload = '[go](a"/onmouseover="location=name) and [x](javascript:alert(1))'
        p = jsonl(user("read that file"), assistant({"type": "text", "text": payload}))
        o = out_path()
        td.generate(p, o, "html", 0)
        body = o.read_text(encoding="utf-8")
        body = body.split("<body", 1)[-1]      # skip inlined vendor JS/CSS
        handlers = [(t, a) for t, a, _ in parsed_attrs(body)
                    if a.startswith("on") and t == "a"]
        self.assertEqual(handlers, [])

    # REGRESSION: ordinary links must keep working.
    def test_http_link_still_renders(self):
        out = td._md_inline("[docs](https://example.com/a?b=1)")
        self.assertIn('href="https://example.com/a?b=1"', out)

    def test_relative_and_anchor_links_still_render(self):
        self.assertIn('href="/reports"', td._md_inline("[r](/reports)"))
        self.assertIn('href="#turn-3"', td._md_inline("[t](#turn-3)"))

    def test_inline_code_and_bold_still_render(self):
        out = td._md_inline("**bold** and `code`")
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<code>code</code>", out)


# ================================================================ turn building
class TestTurnBoundaries(unittest.TestCase):
    def test_plain_prompts_become_turns_in_order(self):
        turns = td.build_turns([user("first"), assistant({"type": "text", "text": "a"}),
                                user("second")])
        self.assertEqual([t["prompt"] for t in turns], ["first", "second"])

    def test_meta_entries_are_not_turns(self):
        turns = td.build_turns([user("real"), user("meta thing", isMeta=True)])
        self.assertEqual(len(turns), 1)

    def test_task_notification_is_not_a_turn(self):
        notif = ("<task-notification><task-id>abc</task-id>"
                 "<result>done</result></task-notification>")
        turns = td.build_turns([user("real"), user(notif)])
        self.assertEqual(len(turns), 1)

    def test_queued_command_attachment_becomes_a_turn(self):
        att = {"type": "attachment",
               "attachment": {"type": "queued_command", "prompt": "queued one"},
               "timestamp": "2026-01-01T10:05:00.000Z"}
        turns = td.build_turns([user("first"), att])
        self.assertEqual([t["prompt"] for t in turns], ["first", "queued one"])

    # DEFECT: harness echoes must not become turns — they split a real turn in
    # two and misattribute every edit that follows them.
    def test_local_command_stdout_is_not_a_turn(self):
        turns = td.build_turns([user("real"),
                                user("<local-command-stdout>Set model to X</local-command-stdout>")])
        self.assertEqual(len(turns), 1)

    def test_local_command_caveat_is_not_a_turn(self):
        turns = td.build_turns([user("real"),
                                user("<local-command-caveat>Caveat: ...</local-command-caveat>")])
        self.assertEqual(len(turns), 1)

    def test_request_interrupted_is_not_a_turn(self):
        turns = td.build_turns([user("real"), user("[Request interrupted by user]")])
        self.assertEqual(len(turns), 1)

    def test_edits_after_a_harness_echo_stay_in_the_real_turn(self):
        entries = [
            user("edit the file"),
            assistant(tool_use("Write", "w1", file_path="/f.txt", content="hello\n")),
            user("<local-command-stdout>noise</local-command-stdout>"),
            assistant(tool_use("Write", "w2", file_path="/g.txt", content="bye\n")),
        ]
        turns = td.build_turns(entries)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["order"], ["/f.txt", "/g.txt"])


class TestLoadRobustness(unittest.TestCase):
    def test_malformed_lines_are_skipped(self):
        p = jsonl(user("a"), "not json", user("b"))
        self.assertEqual(len(td.load(p)), 2)

    def test_truncated_final_line_is_skipped(self):
        p = jsonl(user("a"))
        p.write_text(p.read_text() + '\n{"type":"user","message":{"conte',
                     encoding="utf-8")
        self.assertEqual(len(td.load(p)), 1)

    # DEFECT: a line parsing to a non-dict makes every downstream .get() raise,
    # and the hook swallows it, so the report silently stops updating.
    def test_non_dict_json_lines_are_filtered(self):
        p = jsonl(user("a"), "null", "123", '"a string"', "[1,2]", user("b"))
        entries = td.load(p)
        self.assertTrue(all(isinstance(e, dict) for e in entries))
        self.assertEqual(len(td.build_turns(entries)), 2)


# ================================================================ file state
class TestFileStateReconstruction(unittest.TestCase):
    def test_full_read_seeds_state_so_edit_applies_cleanly(self):
        entries = [
            user("fix it"),
            assistant(tool_use("Read", "r1", file_path="/f.txt")),
            tool_result("r1", "alpha\nbravo\n"),
            assistant(tool_use("Edit", "e1", file_path="/f.txt",
                               old_string="bravo", new_string="charlie")),
        ]
        rec = td.build_turns(entries)[0]["files"]["/f.txt"]
        self.assertTrue(rec["applied"])
        self.assertEqual(rec["before"], "alpha\nbravo\n")
        self.assertEqual(rec["after"], "alpha\ncharlie\n")

    # DEFECT: 45% of real Read calls are partial. Seeding from them fabricates
    # a "before" state showing lines that never existed.
    def test_partial_read_does_not_seed_file_state(self):
        entries = [
            user("fix it"),
            assistant(tool_use("Read", "r1", file_path="/big.txt", offset=930, limit=2)),
            tool_result("r1", "line930\nline931\n"),
            assistant(tool_use("Edit", "e1", file_path="/big.txt",
                               old_string="line930", new_string="CHANGED")),
        ]
        rec = td.build_turns(entries)[0]["files"]["/big.txt"]
        self.assertIsNone(rec["before"],
                          "a partial read must not be recorded as the whole file")

    def test_limit_only_read_also_does_not_seed(self):
        entries = [
            user("x"),
            assistant(tool_use("Read", "r1", file_path="/big.txt", limit=50)),
            tool_result("r1", "some head\n"),
            assistant(tool_use("Edit", "e1", file_path="/big.txt",
                               old_string="some", new_string="other")),
        ]
        rec = td.build_turns(entries)[0]["files"]["/big.txt"]
        self.assertIsNone(rec["before"])

    def test_full_read_after_partial_read_wins(self):
        entries = [
            user("x"),
            assistant(tool_use("Read", "r1", file_path="/f.txt", offset=2, limit=1)),
            tool_result("r1", "bravo\n"),
            assistant(tool_use("Read", "r2", file_path="/f.txt")),
            tool_result("r2", "alpha\nbravo\n"),
            assistant(tool_use("Edit", "e1", file_path="/f.txt",
                               old_string="bravo", new_string="charlie")),
        ]
        rec = td.build_turns(entries)[0]["files"]["/f.txt"]
        self.assertEqual(rec["before"], "alpha\nbravo\n")

    # DEFECT: 556 errored tool results exist in the real corpus; replaying them
    # as successes poisons every later turn's diff.
    def test_errored_write_is_not_applied(self):
        entries = [
            user("write it"),
            assistant(tool_use("Write", "w1", file_path="/f.txt", content="NEVER\n")),
            tool_result("w1", "<tool_use_error>File has not been read yet.</tool_use_error>",
                        is_error=True),
        ]
        turns = td.build_turns(entries)
        rec = turns[0]["files"].get("/f.txt")
        if rec is not None:
            self.assertNotEqual(rec["after"], "NEVER\n",
                                "a rejected Write must not be recorded as applied")

    def test_errored_edit_does_not_corrupt_later_turns(self):
        entries = [
            user("one"),
            assistant(tool_use("Write", "w1", file_path="/f.txt", content="base\n")),
            tool_result("w1", "ok"),
            user("two"),
            assistant(tool_use("Edit", "e1", file_path="/f.txt",
                               old_string="base", new_string="POISON")),
            tool_result("e1", "<tool_use_error>denied</tool_use_error>", is_error=True),
            user("three"),
            assistant(tool_use("Edit", "e2", file_path="/f.txt",
                               old_string="base", new_string="real")),
            tool_result("e2", "ok"),
        ]
        turns = td.build_turns(entries)
        self.assertEqual(turns[2]["files"]["/f.txt"]["before"], "base\n")
        self.assertEqual(turns[2]["files"]["/f.txt"]["after"], "real\n")

    def test_successful_result_still_applies(self):
        """REGRESSION: the is_error check must not reject successful ops."""
        entries = [
            user("x"),
            assistant(tool_use("Write", "w1", file_path="/f.txt", content="yes\n")),
            tool_result("w1", "File created successfully"),
        ]
        rec = td.build_turns(entries)[0]["files"]["/f.txt"]
        self.assertEqual(rec["after"], "yes\n")

    def test_write_then_edit_within_one_turn(self):
        entries = [
            user("x"),
            assistant(tool_use("Write", "w1", file_path="/f.txt", content="a\nb\n")),
            tool_result("w1", "ok"),
            assistant(tool_use("Edit", "e1", file_path="/f.txt",
                               old_string="b", new_string="c")),
            tool_result("e1", "ok"),
        ]
        rec = td.build_turns(entries)[0]["files"]["/f.txt"]
        self.assertTrue(rec["is_new"])
        self.assertEqual(rec["after"], "a\nc\n")

    def test_multiedit_applies_all_edits(self):
        entries = [
            user("x"),
            assistant(tool_use("Write", "w1", file_path="/f.txt", content="1\n2\n3\n")),
            tool_result("w1", "ok"),
            assistant(tool_use("MultiEdit", "m1", file_path="/f.txt", edits=[
                {"old_string": "1", "new_string": "one"},
                {"old_string": "3", "new_string": "three"},
            ])),
            tool_result("m1", "ok"),
        ]
        rec = td.build_turns(entries)[0]["files"]["/f.txt"]
        self.assertEqual(rec["after"], "one\n2\nthree\n")


# ================================================================ diffing
class TestDiffLineNumbers(unittest.TestCase):
    """The review-comment feature anchors on data-ln/data-side and sends the
    resulting file:line back to Claude, so wrong numbers mean wrong fixes."""

    @staticmethod
    def spans(html):
        return re.findall(r'<span class="ln (\w+)"([^>]*)>([^<]*)</span>', html)

    @staticmethod
    def attrs(a):
        ln = re.search(r'data-ln="(\d+)"', a)
        side = re.search(r'data-side="(\w+)"', a)
        return (int(ln.group(1)) if ln else None, side.group(1) if side else None)

    def _check_invariant(self, before, after):
        """Every add line must carry its true 1-based index in `after`, and every
        del line its true index in `before`."""
        import difflib
        import html as _html
        bl, al = before.splitlines(), after.splitlines()
        lines = list(difflib.unified_diff(bl, al, lineterm=""))
        markup = td.diff_html(lines)
        for cls, a, raw in self.spans(markup):
            text = _html.unescape(raw)          # diff_html escapes cell content
            ln, side = self.attrs(a)
            if cls == "add":
                self.assertIsNotNone(ln, f"add line missing data-ln: {text!r}")
                self.assertEqual(side, "new")
                self.assertEqual(al[ln - 1], text[1:],
                                 f"add line {ln} should be {al[ln-1]!r}, diff says {text[1:]!r}")
            elif cls == "del":
                self.assertIsNotNone(ln, f"del line missing data-ln: {text!r}")
                self.assertEqual(side, "old")
                self.assertEqual(bl[ln - 1], text[1:],
                                 f"del line {ln} should be {bl[ln-1]!r}, diff says {text[1:]!r}")

    def test_simple_change(self):
        self._check_invariant("a\nb\nc\n", "a\nB\nc\n")

    def test_insertion_and_deletion(self):
        self._check_invariant("a\nb\nc\nd\n", "a\nc\nx\nd\ne\n")

    # DEFECT: content lines beginning ---/+++ are treated as diff headers, which
    # desyncs the counters for the rest of the hunk.
    def test_markdown_horizontal_rule_deleted(self):
        self._check_invariant("title\n---\nbody1\nbody2\n", "title\nbody1\nbody2\n")

    def test_hugo_front_matter_added(self):
        self._check_invariant("body\n", "+++\ntitle = 'x'\n+++\nbody\n")

    def test_dashed_line_is_classified_as_deletion_not_meta(self):
        import difflib
        lines = list(difflib.unified_diff(["title", "---", "body"],
                                          ["title", "body"], lineterm=""))
        html = td.diff_html(lines)
        self.assertIn('class="ln del"', html)
        self.assertNotIn('class="ln meta">----', html)

    def test_real_diff_headers_are_still_meta(self):
        """REGRESSION: genuine ---/+++ headers must stay meta."""
        lines = ["--- before", "+++ after", "@@ -1 +1 @@", "-a", "+b"]
        html = td.diff_html(lines)
        self.assertEqual(html.count('class="ln meta"'), 2)

    def test_property_random_diffs(self):
        import random
        rng = random.Random(1234)
        for _ in range(40):
            n = rng.randint(1, 25)
            before = [f"line{i}" for i in range(n)]
            after = list(before)
            for _ in range(rng.randint(1, 5)):
                op = rng.choice(("ins", "del", "chg"))
                if not after:
                    op = "ins"
                i = rng.randrange(len(after) + 1) if op == "ins" else rng.randrange(len(after))
                if op == "ins":
                    after.insert(i, rng.choice(["---", "+++", "new", "@@ x", "  "]))
                elif op == "del":
                    after.pop(i)
                else:
                    after[i] = after[i] + "!"
            self._check_invariant("\n".join(before) + "\n", "\n".join(after) + "\n")


class TestHunksModeAnchors(unittest.TestCase):
    """DEFECT: in the per-edit fallback the diff is a concatenation of separate
    unified_diffs, each numbered from 1 — anchors would be duplicated and wrong,
    so a review comment would cite a bogus file:line back to the agent."""

    @staticmethod
    def _hunks_rec():
        # two edits with no reconstructable base -> file_diff_lines picks 'hunks'
        return {"before": None, "is_new": False, "applied": False,
                "after": None,
                "ops": [("edit", "alpha\nbeta", "alpha\nBETA"),
                        ("edit", "gamma\ndelta", "gamma\nDELTA")]}

    def test_mode_is_hunks(self):
        _, mode = td.file_diff_lines(self._hunks_rec())
        self.assertEqual(mode, "hunks")

    def test_no_line_anchors_emitted_in_hunks_mode(self):
        html = td.file_block_html("/f.py", self._hunks_rec(), "t1")
        self.assertNotIn("data-ln=", html,
                         "hunks mode must not emit per-fragment line anchors")

    def test_net_mode_still_emits_anchors(self):
        """REGRESSION: the normal path must keep its anchors."""
        rec = {"before": "a\nb\n", "after": "a\nB\n", "is_new": False,
               "applied": True, "ops": [("edit", "b", "B")]}
        html = td.file_block_html("/f.py", rec, "t1")
        self.assertIn("data-ln=", html)


class TestSplitView(unittest.TestCase):
    # DEFECT: split view truncates by absolute row before collapsing unchanged
    # runs, so changes past MAX_DIFF_LINES vanish entirely.
    def test_change_beyond_cap_is_visible(self):
        before = "".join(f"line{i}\n" for i in range(1, 2001))
        after = before.replace("line1500\n", "line1500 CHANGED\n")
        rec = {"before": before, "after": after, "ops": [], "applied": True, "is_new": False}
        html = td.split_html(rec)
        self.assertIn("CHANGED", html)

    def test_small_file_unaffected(self):
        """REGRESSION: ordinary files must render as before."""
        rec = {"before": "a\nb\n", "after": "a\nB\n", "ops": [], "applied": True,
               "is_new": False}
        html = td.split_html(rec)
        self.assertIn("B", html)


# ================================================================ report writing
class TestGenerate(unittest.TestCase):
    def test_empty_transcript_does_not_crash(self):
        for fmt in ("html", "md"):
            o = out_path("." + fmt)
            td.generate(jsonl(), o, fmt, 0)
            self.assertTrue(o.read_text(encoding="utf-8"))

    def test_malformed_transcript_does_not_crash(self):
        p = jsonl(user("a"), "garbage", assistant({"type": "text", "text": "b"}))
        for fmt in ("html", "md"):
            o = out_path("." + fmt)
            td.generate(p, o, fmt, 0)
            self.assertTrue(o.read_text(encoding="utf-8"))

    def test_html_is_well_formed(self):
        p = jsonl(user("a"), assistant({"type": "text", "text": "b"}))
        o = out_path()
        td.generate(p, o, "html", 0)
        html = o.read_text(encoding="utf-8")
        self.assertTrue(html.rstrip().endswith("</html>"))

    # DEFECT: a single fixed temp filename is shared by every concurrent writer
    # (Stop hook, PostToolUse hook, SSE regen), so reports can be published
    # half-written.
    def test_concurrent_generate_never_publishes_partial_html(self):
        p = jsonl(*([user("a"), assistant({"type": "text", "text": "b" * 500})] * 40))
        o = out_path()
        errors = []

        def worker():
            try:
                for _ in range(6):
                    td.generate(p, o, "html", 0)
                    txt = o.read_text(encoding="utf-8")
                    if not txt.rstrip().endswith("</html>"):
                        errors.append("published a truncated report")
            except Exception as exc:                        # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda _: worker(), range(6)))
        self.assertEqual(errors, [])

    def test_no_temp_files_left_behind(self):
        p = jsonl(user("a"))
        o = out_path()
        td.generate(p, o, "html", 0)
        self.assertEqual(list(o.parent.glob(o.name + "*.tmp")), [])


# ================================================================ morph fingerprint
class TestMorphSignature(unittest.TestCase):
    """DEFECT: data-sig omits agents, so a finished subagent's panel and file
    diffs never appear until a manual reload."""

    def _sig(self, html, turn=1):
        m = re.search(r"id='turn-%d'[^>]*data-sig='([^']*)'" % turn, html)
        return m.group(1) if m else None

    def test_signature_changes_when_agent_result_arrives(self):
        base = [user("go"), assistant(tool_use("Task", "a1", prompt="do it"))]
        notif = ("<task-notification><task-id>t</task-id><tool-use-id>a1</tool-use-id>"
                 "<result>the agent answer</result></task-notification>")
        p1, p2 = jsonl(*base), jsonl(*base, user(notif))
        o1, o2 = out_path(), out_path()
        td.generate(p1, o1, "html", 0)
        td.generate(p2, o2, "html", 0)
        self.assertNotEqual(self._sig(o1.read_text(encoding="utf-8")),
                            self._sig(o2.read_text(encoding="utf-8")),
                            "sig must change when a subagent result lands")


# ================================================================ terminal injection
class TestPromptSanitising(unittest.TestCase):
    """DEFECT: text is sent to the terminal without stripping the bracketed-paste
    terminator, so injected text can leave paste mode and act as keystrokes."""

    def test_paste_terminator_is_removed(self):
        cleaned = td._sanitize_prompt("review this\x1b[201~\ry\r")
        self.assertNotIn("\x1b[201~", cleaned)
        self.assertNotIn("\x1b", cleaned)

    def test_carriage_returns_do_not_survive_as_enter(self):
        self.assertNotIn("\r", td._sanitize_prompt("line1\rline2"))

    def test_ordinary_text_is_untouched(self):
        text = "please fix `foo.py` — line 3\n\nsecond paragraph"
        self.assertEqual(td._sanitize_prompt(text), text)

    def test_newlines_are_preserved(self):
        self.assertEqual(td._sanitize_prompt("a\nb"), "a\nb")


# ================================================================ HTTP server
class ServerTestBase(unittest.TestCase):
    """Drives a real server instance on an ephemeral loopback port."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.rd = td.reports_dir()
        cls.rd.mkdir(parents=True, exist_ok=True)
        cls.token = td._serve_token()
        cls.sid = "11111111-2222-3333-4444-555555555555"
        p = jsonl(user("hello"), assistant({"type": "text", "text": "hi"}))
        td.generate(p, cls.rd / f"{cls.sid}.html", "html", 0)

        cls.box = {}
        cls.thread = threading.Thread(
            target=td.serve, args=(cls.port,), kwargs={"ready": cls.box.setdefault},
            daemon=True)
        cls.thread.start()
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", cls.port), 0.1):
                    break
            except OSError:
                threading.Event().wait(0.05)

    @classmethod
    def tearDownClass(cls):
        srv = cls.box.get("srv")
        if srv is not None:
            srv.shutdown()
            srv.server_close()

    def req(self, method, path, host=None, token=None, body=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-TD-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data


class TestServerLocalAccess(ServerTestBase):
    """REGRESSION: loopback use must keep working with zero friction."""

    def test_index_served_on_loopback(self):
        status, _ = self.req("GET", "/")
        self.assertEqual(status, 200)

    def test_report_served_on_loopback(self):
        status, body = self.req("GET", f"/{self.sid}.html")
        self.assertEqual(status, 200)
        self.assertIn(b"</html>", body)

    def test_sessions_json_served_on_loopback(self):
        status, body = self.req("GET", "/sessions")
        self.assertEqual(status, 200)
        json.loads(body)

    def test_localhost_hostname_also_allowed(self):
        status, _ = self.req("GET", "/", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)


class TestServerHostValidation(ServerTestBase):
    """DEFECT: without Host validation, any website can DNS-rebind to
    127.0.0.1 and drive the POST /prompt endpoint."""

    def test_foreign_host_is_rejected(self):
        status, _ = self.req("GET", "/", host="evil.example.com")
        self.assertIn(status, (400, 403))

    def test_foreign_host_rejected_on_report(self):
        status, _ = self.req("GET", f"/{self.sid}.html", host="attacker.tld:8787")
        self.assertIn(status, (400, 403))

    def test_foreign_host_cannot_post_prompt(self):
        status, _ = self.req("POST", f"/prompt/{self.sid}", host="evil.example.com",
                             token=self.token, body=json.dumps({"text": "hi"}))
        self.assertIn(status, (400, 403))

    def test_tailnet_host_is_allowed_but_requires_token(self):
        """The user's remote workflow must keep working — with authentication."""
        host = "cachyos.weasel-pirate.ts.net:8443"
        status, _ = self.req("GET", "/", host=host)
        self.assertEqual(status, 401, "remote access must require the token")
        status, _ = self.req("GET", "/", host=host, token=self.token)
        self.assertEqual(status, 200, "remote access with a token must work")

    def test_tailnet_report_with_token(self):
        host = "cachyos.weasel-pirate.ts.net:8443"
        status, body = self.req("GET", f"/{self.sid}.html", host=host, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"</html>", body)

    def test_tailnet_query_token_sets_cookie(self):
        host = "cachyos.weasel-pirate.ts.net:8443"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/?t={self.token}", headers={"Host": host})
        r = conn.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie") or ""
        conn.close()
        self.assertEqual(r.status, 200)
        self.assertIn("td_token=", cookie)
        self.assertIn("HttpOnly", cookie)


class TestServerPathTraversal(ServerTestBase):
    def test_report_traversal_blocked(self):
        status, _ = self.req("GET", "/../../../../etc/passwd")
        self.assertEqual(status, 404)

    def test_assets_traversal_blocked(self):
        status, _ = self.req("GET", "/assets/../../turn-diffs.py")
        self.assertEqual(status, 404)

    # DEFECT: /commands/<sid> used the unvalidated report_path_for().
    def test_commands_traversal_blocked(self):
        status, body = self.req("GET", "/commands/../../../../home/dizzyc/somefile")
        self.assertIn(status, (400, 404))

    def test_commands_rejects_non_session_ids(self):
        status, _ = self.req("GET", "/commands/..%2f..%2fetc")
        self.assertIn(status, (400, 404))


class TestServerPostHardening(ServerTestBase):
    def test_prompt_requires_token_even_on_loopback(self):
        """REGRESSION: the POST gate predates this work and must stay."""
        status, _ = self.req("POST", f"/prompt/{self.sid}",
                             body=json.dumps({"text": "hi"}))
        self.assertIn(status, (401, 403))

    def test_wrong_token_rejected(self):
        status, _ = self.req("POST", f"/prompt/{self.sid}", token="wrong",
                             body=json.dumps({"text": "hi"}))
        self.assertIn(status, (401, 403))

    # DEFECT: Content-Length was read unbounded.
    def test_oversized_body_rejected(self):
        big = json.dumps({"text": "x" * (256 * 1024)})
        status, _ = self.req("POST", f"/prompt/{self.sid}", token=self.token, body=big)
        self.assertIn(status, (400, 413))

    def test_negative_content_length_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", f"/prompt/{self.sid}")
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("X-TD-Token", self.token)
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        try:
            r = conn.getresponse()
            self.assertIn(r.status, (400, 413))
        except Exception:
            pass                                    # connection refused is fine too
        finally:
            conn.close()


class TestHostClassification(unittest.TestCase):
    """Unit-level checks for the DNS-rebinding guard."""

    def test_loopback_variants_are_local(self):
        for h in ("127.0.0.1:8787", "localhost:8787", "127.0.0.1", "[::1]:8787", ""):
            self.assertEqual(td.host_kind(h), "local", h)

    def test_missing_host_is_local(self):
        self.assertEqual(td.host_kind(None), "local")

    def test_tailnet_names_are_remote(self):
        self.assertEqual(td.host_kind("cachyos.weasel-pirate.ts.net:8443"), "remote")

    def test_arbitrary_hosts_are_rejected(self):
        for h in ("evil.example.com", "attacker.tld:8787", "127.0.0.1.evil.com",
                  "notts.net.evil.com"):
            self.assertIsNone(td.host_kind(h), h)

    def test_env_allowlist_is_honoured(self):
        os.environ["TURN_DIFFS_ALLOWED_HOSTS"] = "reports.example.com,*.corp.internal"
        try:
            self.assertEqual(td.host_kind("reports.example.com:8787"), "remote")
            self.assertEqual(td.host_kind("a.corp.internal"), "remote")
            self.assertIsNone(td.host_kind("other.example.com"))
        finally:
            del os.environ["TURN_DIFFS_ALLOWED_HOSTS"]


class TestSidValidation(unittest.TestCase):
    def test_accepts_real_session_ids(self):
        self.assertTrue(td.safe_sid("97c2d9d3-a1d1-44f0-a3f2-13bd5a7fbbc5"))

    def test_rejects_traversal_and_separators(self):
        for bad in ("../../etc/passwd", "a/b", "", "..", "x" * 200, "a\x00b"):
            self.assertFalse(td.safe_sid(bad), bad)


class TestTokenComparison(unittest.TestCase):
    def test_exact_match_only(self):
        self.assertTrue(td.token_ok("abc", "abc"))
        self.assertFalse(td.token_ok("abcd", "abc"))
        self.assertFalse(td.token_ok("", "abc"))
        self.assertFalse(td.token_ok(None, "abc"))


class TestPruning(unittest.TestCase):
    def test_enabled_sessions_are_never_pruned(self):
        rd = td.reports_dir()
        rd.mkdir(parents=True, exist_ok=True)
        td.enabled_dir().mkdir(parents=True, exist_ok=True)
        keep = rd / "keepme.html"
        keep.write_text("<html></html>", encoding="utf-8")
        td.enabled_flag("keepme").touch()
        old = rd / "ancient.html"
        old.write_text("<html></html>", encoding="utf-8")
        ancient = time.time() - (td.MAX_REPORT_AGE_DAYS + 5) * 86400
        os.utime(old, (ancient, ancient))
        try:
            td.prune_reports()
            self.assertTrue(keep.exists(), "an enabled session's report must survive")
            self.assertFalse(old.exists(), "a stale report should be removed")
        finally:
            td.enabled_flag("keepme").unlink(missing_ok=True)
            keep.unlink(missing_ok=True)

    def test_stale_temp_files_are_cleaned(self):
        rd = td.reports_dir()
        rd.mkdir(parents=True, exist_ok=True)
        t = rd / "leftover.html.abc.tmp"
        t.write_text("partial", encoding="utf-8")
        os.utime(t, (time.time() - 7200, time.time() - 7200))
        td.prune_reports()
        self.assertFalse(t.exists())


class TestHealthEndpoint(ServerTestBase):
    def test_healthz_identifies_the_app_without_a_token(self):
        status, body = self.req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body).get("app"), td.HEALTH_MAGIC)

    def test_server_running_probe_accepts_our_server(self):
        self.assertTrue(td._server_running(self.port))

    def test_server_running_probe_rejects_a_foreign_server(self):
        """A bare TCP connect would call any app on the port 'turn-diffs'."""
        import http.server
        port = free_port()
        srv = http.server.HTTPServer(("127.0.0.1", port),
                                     http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertTrue(td._port_busy(port))
            self.assertFalse(td._server_running(port))
        finally:
            srv.shutdown()
            srv.server_close()


def make_session_with_subagents(main_entries, agents):
    """Build a transcript plus its subagents/ sidecar directory.

    agents: {tool_use_id: {"entries": [...], "meta": {...}, "name": "agent-N"}}
    Mirrors Claude Code's real layout: <session>.jsonl next to
    <session>/subagents/agent-<id>.jsonl + agent-<id>.meta.json
    """
    root = pathlib.Path(tempfile.mkdtemp(dir=_TMPDIR))
    sid = "sess"
    main = root / f"{sid}.jsonl"
    main.write_text("\n".join(json.dumps(e) for e in main_entries), encoding="utf-8")
    sub = root / sid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    for tuid, spec in agents.items():
        name = spec.get("name") or f"agent-{tuid}"
        (sub / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in spec["entries"]), encoding="utf-8")
        meta = {"toolUseId": tuid, "agentType": spec.get("agentType", "general-purpose")}
        meta.update(spec.get("meta", {}))
        (sub / f"{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return main


class TestSubagentFileState(unittest.TestCase):
    """DEFECT: a subagent's edits never reached the parent's file_state, so the
    NEXT turn diffed against pre-subagent content and falsely showed the
    subagent's changes as its own."""

    def setUp(self):
        self.main = make_session_with_subagents(
            [
                user("delegate it"),
                assistant(tool_use("Task", "a1", prompt="write the file")),
                user("now tweak it"),
                assistant(tool_use("Edit", "e1", file_path="/f.txt",
                                   old_string="from-agent", new_string="tweaked")),
                tool_result("e1", "ok"),
            ],
            {"a1": {"entries": [
                user("write it"),
                assistant(tool_use("Write", "w1", file_path="/f.txt",
                                   content="from-agent\n")),
                tool_result("w1", "ok"),
            ]}},
        )

    def test_next_turn_sees_the_subagents_result_as_its_before(self):
        entries = td.load(self.main)
        subs = td.scan_subagents(self.main)
        turns = td.build_turns(entries, subs=subs)
        rec = turns[1]["files"]["/f.txt"]
        self.assertEqual(rec["before"], "from-agent\n",
                         "turn after a subagent must start from the subagent's result")
        self.assertEqual(rec["after"], "tweaked\n")
        self.assertTrue(rec["applied"])

    def test_generate_uses_the_merged_state(self):
        o = out_path()
        td.generate(self.main, o, "html", 0)
        html = o.read_text(encoding="utf-8")
        self.assertIn("tweaked", html)

    def test_without_subs_behaviour_is_unchanged(self):
        """REGRESSION: the default call path must not change."""
        turns = td.build_turns(td.load(self.main))
        self.assertIsNone(turns[1]["files"]["/f.txt"]["before"])


class TestNestedSubagents(unittest.TestCase):
    """DEFECT: a depth-2 agent's toolUseId lives in its PARENT subagent's
    transcript, which was never scanned — so its edits vanished."""

    def test_depth_two_agent_is_attached(self):
        main = make_session_with_subagents(
            [user("go"), assistant(tool_use("Task", "a1", prompt="outer"))],
            {
                "a1": {"name": "agent-outer", "entries": [
                    user("outer work"),
                    assistant(tool_use("Task", "a2", prompt="inner")),
                ]},
                "a2": {"name": "agent-inner", "meta": {"spawnDepth": 2}, "entries": [
                    user("inner work"),
                    assistant(tool_use("Write", "w9", file_path="/deep.txt",
                                       content="written by the nested agent\n")),
                    tool_result("w9", "ok"),
                ]},
            },
        )
        entries = td.load(main)
        turns = td.build_turns(entries, subs=td.scan_subagents(main))
        td.attach_agents(turns, main, entries)
        files = [f for a in turns[0]["agents"] for f in a.get("order", [])]
        self.assertIn("/deep.txt", files,
                      "a nested agent's file changes must still be reported")


class TestAgentPanels(unittest.TestCase):
    def test_agent_without_result_still_renders(self):
        """An interrupted subagent must leave evidence it ran."""
        entries = [user("go"), assistant(tool_use("Task", "a1", prompt="do it"))]
        p = jsonl(*entries)
        o = out_path()
        td.generate(p, o, "html", 0)
        html = o.read_text(encoding="utf-8")
        self.assertIn("no result recorded", html)


class TestTokenFile(unittest.TestCase):
    def test_token_file_is_owner_only(self):
        td._serve_token()
        f = td.DATA_DIR / "serve-token"
        self.assertTrue(f.exists())
        self.assertEqual(f.stat().st_mode & 0o077, 0,
                         "token file must not be group/world readable")

    def test_token_is_high_entropy(self):
        self.assertGreaterEqual(len(td._serve_token()), 24)


class TestEmbeddedFrontend(unittest.TestCase):
    """The CSS/JS live inside Python string literals, so a stray backslash
    silently becomes a real newline and breaks the page with no Python error.
    These checks catch that class of bug in CI rather than in the browser."""

    def test_js_parses(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        f = pathlib.Path(tempfile.mkstemp(suffix=".js", dir=_TMPDIR)[1])
        f.write_text(td.JS.replace("__SID__", "sid"), encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_raw_newline_inside_js_string_literals(self):
        """A JS single-quoted string can never span a line — if one does, a
        '\\n' in the Python source was interpreted instead of emitted."""
        for i, line in enumerate(td.JS.splitlines(), 1):
            if line.count("'") % 2:
                self.assertNotIn("+'", line + "'",
                                 f"unbalanced quote suggests an escaping bug at JS line {i}: {line!r}")

    def test_placeholders_are_substituted(self):
        p = jsonl(user("hi"))
        o = out_path()
        td.generate(p, o, "html", 0)
        html = o.read_text(encoding="utf-8")
        self.assertNotIn("__SID__", html)
        self.assertNotIn("__REFRESH__", html)

    def test_build_hash_is_present_and_stable(self):
        p = jsonl(user("hi"))
        a, b = out_path(), out_path()
        td.generate(p, a, "html", 0)
        td.generate(p, b, "html", 0)
        pat = r"<meta name='td-build' content='([^']+)'"
        ha = re.search(pat, a.read_text(encoding="utf-8"))
        hb = re.search(pat, b.read_text(encoding="utf-8"))
        self.assertIsNotNone(ha)
        self.assertEqual(ha.group(1), hb.group(1))


# ================================================================ smoke
class TestRealTranscriptSmoke(unittest.TestCase):
    """Cheap end-to-end guard: render real transcripts and assert nothing raises.
    Skipped automatically when the machine has no sessions."""

    def test_render_real_transcripts(self):
        sessions = td.find_sessions()[:12]
        if not sessions:
            self.skipTest("no real transcripts available")
        rendered = 0
        for p in sessions:
            if p.stat().st_size > 8_000_000:        # keep the suite quick
                continue
            o = out_path()
            with self.subTest(session=p.name):
                td.generate(p, o, "html", 0)
                html = o.read_text(encoding="utf-8")
                self.assertTrue(html.rstrip().endswith("</html>"))
                rendered += 1
        # Guard against the loop silently skipping everything and passing.
        self.assertGreaterEqual(rendered, 3, "smoke test rendered almost nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
