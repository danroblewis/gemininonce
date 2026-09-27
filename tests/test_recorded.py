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
from gemininonce.gemini import EXTRACT_JS
from gemininonce.loop import FixLoop
from gemininonce.merge import looks_partial
from gemininonce.transcript import Transcript
from gemininonce.workspace import Workspace

ALL_RECORDINGS = sorted(p.stem for p in RECORDED.glob("*.json") if p.stem != "meta")
PYTEST = f"{sys.executable} -m pytest -x -q -p no:cacheprovider"


class ReplayChat:
    """Stands in for GeminiChat: returns recorded replies in order and keeps what we sent."""

    def __init__(self, names):
        self.replies = [load_recording(n)["blocks"] for n in names]
        self.sent = []

    def ask(self, text):
        self.sent.append(text)
        return self.replies.pop(0)


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
    prompt = protocol.initial_prompt("", protocol.test_result(PYTEST, code, out), files)
    chat = ReplayChat(session_rounds())
    passed, printed = quiet(FixLoop(chat, ws, Transcript(), PYTEST).run, prompt, prompt)

    assert passed, printed
    assert chat.replies == [], "every recorded reply should have been used"
    assert "PROJECT FILES:" in chat.sent[0] and "still fails" in chat.sent[1]
    errored = [i for i, n in enumerate(session_rounds()) if "encountered an error" in str(load_recording(n)["blocks"])]
    for i in errored:  # after Gemini's error reply, the same message was sent again
        assert chat.sent[i + 1] == chat.sent[i]
    assert subprocess.run(PYTEST, shell=True, cwd=todo_project, capture_output=True).returncode == 0
    assert (tmp_path / "backups/todo/utils.py").exists()  # originals were backed up


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
