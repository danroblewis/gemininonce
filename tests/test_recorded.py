"""Tests driven by replies recorded from a real anonymous Gemini session (tests/fixtures/recorded,
re-record with `python tests/record_fixtures.py`). No network needed."""
import contextlib
import io
import subprocess
import sys

import pytest
from conftest import RECORDED, load_recording, session_rounds

from gemininonce import loop as loop_module
from gemininonce import protocol
from gemininonce.gemini import EXTRACT_JS, EXTRACT_MD_JS
from gemininonce.loop import FixLoop
from gemininonce.merge import looks_partial
from gemininonce.transcript import Transcript
from gemininonce.usage import Usage
from gemininonce.workspace import Workspace

ALL_RECORDINGS = sorted(p.stem for p in RECORDED.glob("*.json") if p.stem != "meta")
PYTEST = f"{sys.executable} -m pytest -x -q -p no:cacheprovider"


class ReplayChat:
    """Stands in for GeminiChat: returns recorded replies in order and keeps what we sent."""

    model = "3.5 Flash-Lite"

    def __init__(self, names):
        self.replies = [load_recording(n)["blocks"] if isinstance(n, str) else n for n in names]
        self.sent = []
        self.usage = Usage()
        self.new_chats = 0

    def ask(self, text):
        self.sent.append(text)
        reply = self.replies.pop(0)
        self.usage.record(len(text), sum(len(b["text"]) for b in reply))
        return reply

    def new_chat(self):
        self.new_chats += 1


@pytest.fixture
def no_input(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # every prompt answered "no"
    monkeypatch.setattr(loop_module.time, "sleep", lambda s: None)  # skip the retry pause


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = fn(*args)
    return result, out.getvalue()


# --- reading Gemini's DOM -------------------------------------------------------------------
@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Exception:
            try:
                browser = pw.chromium.launch()
            except Exception as e:
                pytest.skip(f"no browser for Playwright: {e}")
        yield browser.new_page()
        browser.close()


@pytest.mark.parametrize("name", ALL_RECORDINGS)
def test_extract_js_reads_recorded_reply_html(page, name):
    """Our DOM walker gets the same text/code blocks from the saved HTML as it did live."""
    rec = load_recording(name)
    page.set_content(rec["html"])
    assert page.locator("message-content").first.evaluate(EXTRACT_JS) == rec["blocks"]


@pytest.mark.parametrize("name", ALL_RECORDINGS)
def test_markdown_extraction_keeps_text_and_code_of_recorded_replies(page, name):
    rec = load_recording(name)
    page.set_content(rec["html"])
    md = page.locator("message-content").first.evaluate(EXTRACT_MD_JS)
    code_blocks = [b for b in rec["blocks"] if b["kind"] == "code"]
    assert md.count("```") >= 2 * len(code_blocks)
    for b in code_blocks:
        assert b["text"].strip().splitlines()[0] in md
    for path, _ in protocol.parse_reply(rec["blocks"])[0]:
        assert f"FILE: {path}" in md.replace("`", "")


def test_markdown_extraction_of_a_formatted_spec(page):
    page.set_content("""<message-content><div class="markdown"><h1>Roman: Specification</h1>
        <p>Uses <strong>strict</strong> parsing via <code>from_roman()</code>.</p><h2>4. Interface</h2>
        <code-block><div class="code-block-decoration"><span>Python</span><button>Copy</button></div>
        <pre><code>def to_roman(n: int) -&gt; str: ...\n</code></pre></code-block>
        <h2>6. Requirements</h2><ol><li><p><strong>R1</strong>: converts 1..3999</p>
        <ul><li>Acceptance: <code>to_roman(4) == "IV"</code></li></ul></li></ol>
        <table><tr><th>in</th><th>out</th></tr><tr><td>4</td><td>IV</td></tr></table>
        <p>Done.<source-inline-chip>[1] wikipedia</source-inline-chip></p></div></message-content>""")
    md = page.locator("message-content").first.evaluate(EXTRACT_MD_JS)
    assert md == ("# Roman: Specification\n\nUses **strict** parsing via `from_roman()`.\n\n## 4. Interface\n\n"
                  "```python\ndef to_roman(n: int) -> str: ...\n```\n\n## 6. Requirements\n\n"
                  "1. **R1**: converts 1..3999\n   - Acceptance: `to_roman(4) == \"IV\"`\n\n"
                  "| in | out |\n| --- | --- |\n| 4 | IV |\n\nDone.\n")
    assert protocol.spec_document("Some preface.\n\n" + md).startswith("# Roman: Specification")


# --- parsing real replies -------------------------------------------------------------------
@pytest.mark.parametrize("name", session_rounds())
def test_session_replies_parse(name):
    blocks = load_recording(name)["blocks"]
    edits, commands, prose = protocol.parse_reply(blocks)
    if "encountered an error" in prose:
        assert not edits and not commands and protocol.GEMINI_ERROR.search(prose)
    else:
        assert edits and all(path.startswith("todo/") for path, _ in edits)


def test_command_reply():
    edits, commands, _ = protocol.parse_reply(load_recording("command")["blocks"])
    assert edits == [] and commands == ["pip install requests"]


def test_no_code_reply():
    edits, commands, prose = protocol.parse_reply(load_recording("no_code")["blocks"])
    assert edits == [] and commands == [] and "nonce" in prose.lower()


# --- the whole loop, replayed ---------------------------------------------------------------
def test_recorded_session_replays_to_passing_tests(todo_project, no_input, tmp_path):
    """Feed the recorded replies through the real FixLoop: the todo app's tests end up passing,
    including the round where Gemini answered with an error and we resent the prompt."""
    ws = Workspace(todo_project, tmp_path / "backups", sandbox=False)
    files = ws.collect([str(todo_project / "todo"), str(todo_project / "tests")])
    (code, out), _ = quiet(ws.run_test, PYTEST)
    assert code != 0
    prompt = protocol.initial_prompt("", protocol.test_result(PYTEST, code, out), files, ws.layout())
    chat = ReplayChat(session_rounds())
    passed, printed = quiet(FixLoop(chat, ws, Transcript(), PYTEST).run, prompt, (code, out))

    assert passed, printed
    assert chat.replies == [], "every recorded reply should have been used"
    assert "PROJECT FILES:" in chat.sent[0] and "still fails" in chat.sent[1]
    errored = [i for i, n in enumerate(session_rounds()) if "encountered an error" in str(load_recording(n)["blocks"])]
    for i in errored:  # after Gemini's error reply, the same message was sent again
        assert chat.sent[i + 1] == chat.sent[i]
    assert subprocess.run(PYTEST, shell=True, cwd=todo_project, capture_output=True).returncode == 0
    assert (tmp_path / "backups/todo/utils.py").exists()  # originals were backed up
    assert printed.count("$ this reply: ~") == len(session_rounds())  # cost of every reply...
    assert "  |  total: ~" in printed and "$ round 6: ~" in printed  # ...the running total, and round subtotals


def test_error_reply_is_retried_with_same_prompt(no_input):
    chat = ReplayChat(["no_code", "session-01"])
    (edits, _, _), printed = quiet(FixLoop(chat, None, Transcript()).ask_for_code, "PROMPT")
    assert edits and chat.sent == ["PROMPT", "PROMPT"] and "resending" in printed


def test_long_reply_without_code_gets_nudged(no_input):
    chat = ReplayChat(["session-01"])
    chat.replies.insert(0, [{"kind": "text", "text": "Here is a long explanation. " * 20}])
    (edits, _, _), _ = quiet(FixLoop(chat, None, Transcript()).ask_for_code, "PROMPT")
    assert edits and chat.sent[1] == protocol.NO_CODE_NUDGE


def test_recorded_partial_edit_merges_and_fixes_the_bug(todo_project, no_input, tmp_path):
    (path, snippet), = protocol.parse_reply(load_recording("partial")["blocks"])[0]
    old = (todo_project / path).read_text()
    assert looks_partial(old, snippet)
    ws = Workspace(todo_project, tmp_path / "backups", sandbox=False)
    ws.known = {path}
    notes, printed = quiet(ws.apply, [(path, snippet)])
    assert notes == [] and "merged partial edit" in printed
    new = (todo_project / path).read_text()
    assert "def pending" in new and "def with_tag" in new  # the rest of the class survived
    one_test = f"{PYTEST} tests/test_todo.py::test_ids_unique_after_remove"
    assert subprocess.run(one_test, shell=True, cwd=todo_project, capture_output=True).returncode == 0


def test_recorded_command_is_skipped_without_approval(no_input, tmp_path):
    _, commands, _ = protocol.parse_reply(load_recording("command")["blocks"])
    ws = Workspace(tmp_path, tmp_path / "backups", sandbox=False)
    results, printed = quiet(ws.review_commands, commands, None)
    assert results == [f"User skipped command:\n{protocol.fenced('pip install requests')}"]
    assert "Gemini suggests running" in printed


@pytest.mark.parametrize("name", ALL_RECORDINGS)
def test_transcript_renders_recorded_reply(name):
    blocks = load_recording(name)["blocks"]
    _, printed = quiet(Transcript().reply, blocks)
    assert "── Gemini" in printed
    for path, _ in protocol.parse_reply(blocks)[0]:
        assert f"FILE: {path}" in printed


# --- asking for files (READ:) ---------------------------------------------------------------
def read_request(*paths):
    return [{"kind": "text", "text": "I need to see these first:\n" + "\n".join(f"READ: {p}" for p in paths)}]


def test_read_requests_are_answered_before_the_round_continues(todo_project, no_input, tmp_path):
    """A reply that only asks for files gets them (known files freely, others refused without an OK,
    made-up paths explained), and doesn't use up a retry."""
    (todo_project / "notes.txt").write_text("private notes\n")
    ws = Workspace(todo_project, tmp_path / "backups", sandbox=False)
    ws.collect([str(todo_project / "todo" / "utils.py")])
    chat = ReplayChat([read_request("todo/utils.py", "notes.txt", "todo/nope.py", "../../etc/passwd"), "session-01"])
    (edits, _, _), printed = quiet(FixLoop(chat, ws, Transcript(), retries=0).ask_for_code, "PROMPT")
    assert edits  # session-01's fix, after the files were served
    answer = chat.sent[1]
    assert "REQUESTED FILES:" in answer and "FILE: todo/utils.py" in answer
    assert "private notes" not in answer and "chose not to share `notes.txt`" in answer
    assert "`todo/nope.py` doesn't exist" in answer and "outside the project" in answer
    assert "Gemini asks to read notes.txt" in printed


def test_read_requests_alongside_edits_are_answered_in_the_next_message(todo_project, no_input, tmp_path):
    ws = Workspace(todo_project, tmp_path / "backups", sandbox=False)
    files = ws.collect([str(todo_project / "todo"), str(todo_project / "tests")])
    both = load_recording("session-01")["blocks"] + read_request("todo/models.py")
    chat = ReplayChat([both, "session-02"])
    loop = FixLoop(chat, ws, Transcript(), PYTEST, max_iters=2)
    quiet(loop.run, protocol.initial_prompt("", "", files), None)
    assert "REQUESTED FILES:" in chat.sent[1] and "FILE: todo/models.py" in chat.sent[1]


def test_layout_lists_project_files_and_marks_the_sent_ones(todo_project, tmp_path):
    (todo_project / ".env").write_text("SECRET=1\n")
    ws = Workspace(todo_project, tmp_path / "backups", sandbox=False)
    ws.collect([str(todo_project / "todo")])
    layout = ws.layout().splitlines()
    assert layout[0].startswith("PROJECT LAYOUT")
    assert "* todo/models.py" in layout and "  tests/test_todo.py" in layout
    assert not any(".env" in line for line in layout)  # secrets aren't even named


# --- when Gemini is stuck -------------------------------------------------------------------
# A full-file edit that doesn't fix anything: the same failure every round.
STUCK = [{"kind": "text", "text": "FILE: todo/utils.py"},
         {"kind": "code", "text": (RECORDED.parent / "todo_project/todo/utils.py").read_text() + "# no real change\n"}]


def stuck_loop(todo_project, tmp_path, replies, patience=2):
    ws = Workspace(todo_project, tmp_path / "backups", sandbox=False)
    files = ws.collect([str(todo_project / "todo"), str(todo_project / "tests")])
    chat = ReplayChat(replies)
    return FixLoop(chat, ws, Transcript(), PYTEST, patience=patience, max_iters=6), chat, files


def test_stuck_without_a_terminal_stops(todo_project, no_input, tmp_path):
    loop, chat, files = stuck_loop(todo_project, tmp_path, [STUCK, STUCK, STUCK])
    passed, printed = quiet(loop.run, protocol.initial_prompt("", "", files), None)
    assert not passed and "Gemini seems stuck: no progress" in printed and len(chat.sent) == 3


def test_stuck_user_hint_is_sent_and_new_starts_fresh(todo_project, tmp_path, monkeypatch):
    monkeypatch.setattr(loop_module.time, "sleep", lambda s: None)
    answers = iter(["look at utils.normalize_tag", "/new", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    loop, chat, files = stuck_loop(todo_project, tmp_path, [STUCK] * 3 + [STUCK] * 3 + [STUCK] * 3, patience=2)
    passed, printed = quiet(loop.run, protocol.initial_prompt("", "", files), None)
    assert not passed
    hint = next(m for m in chat.sent if m.startswith("A message from the user:"))
    assert "look at utils.normalize_tag" in hint and "still fails" not in hint and "Output:" in hint
    assert chat.new_chats == 1
    fresh = chat.sent[chat.sent.index(hint) + 2]  # two more stalled rounds later, /new re-sends the project
    assert "PROJECT FILES:" in fresh and "PROJECT LAYOUT" in fresh and "# no real change" in fresh
    assert printed.count("Gemini seems stuck") == 2


def test_gemini_timeout_is_retried_not_fatal(no_input):
    from gemininonce.gemini import GeminiTimeout
    chat = ReplayChat(["session-01"])
    real_ask = chat.ask
    calls = []

    def flaky_ask(text):
        calls.append(text)
        if len(calls) == 1:
            raise GeminiTimeout("Gemini never started a response (message not sent?)")
        return real_ask(text)

    chat.ask = flaky_ask
    (edits, _, _), printed = quiet(FixLoop(chat, None, Transcript()).ask_for_code, "PROMPT")
    assert edits and calls == ["PROMPT", "PROMPT"] and "never started a response" in printed


def test_dunder_file_names_survive_markdown_bold(page):
    """`__init__.py` in Gemini's markdown renders as <strong>init</strong>.py; we must still read the name."""
    page.set_content("""<message-content><div class="markdown">
        <p><strong>FILE:</strong> pkg/<strong>init</strong>.py</p>
        <code-block><pre><code>x = 1\n</code></pre></code-block>
        <p>FILE: pkg/<em>main</em>.py</p><code-block><pre><code>y = 2\n</code></pre></code-block>
        <p>This is <strong>important</strong>.</p></div></message-content>""")
    blocks = page.locator("message-content").first.evaluate(EXTRACT_JS)
    edits, _, _ = protocol.parse_reply(blocks)
    assert [path for path, _ in edits] == ["pkg/__init__.py", "pkg/_main_.py"]
    md = page.locator("message-content").first.evaluate(EXTRACT_MD_JS)
    assert "pkg/__init__.py" in md and "This is **important**." in md


def test_code_block_inside_a_list_item_stays_a_code_block(page):
    page.set_content("""<message-content><div class="markdown"><ul><li><p><strong>Layout</strong>:</p>
        <response-element><code-block><div class="code-block-decoration"><span>Plaintext</span></div>
        <pre><code>pkg/\n└── core.py\n</code></pre></code-block></response-element></li><li>Next</li></ul>
        </div></message-content>""")
    md = page.locator("message-content").first.evaluate(EXTRACT_MD_JS)
    assert md == "- **Layout**:\n   ```plaintext\n   pkg/\n   └── core.py\n   ```\n- Next\n"
