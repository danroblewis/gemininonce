"""--check: plain-language description -> candidate test commands -> the user's pick. Fake Gemini, no network."""
import contextlib
import io
import sys

import pytest

from geminonce.checks import choose_test_command, preview
from geminonce.highlight import Highlighter
from geminonce.transcript import Transcript
from geminonce.usage import Usage
from geminonce.workspace import Workspace


def commands_reply(*cmds):
    blocks = []
    for c in cmds:
        blocks += [{"kind": "text", "text": "Checks it.\nCOMMAND:"}, {"kind": "code", "text": c}]
    return blocks


class FakeChat:
    model = "3.5 Flash-Lite"

    def __init__(self, replies):
        self.replies, self.sent, self.usage = list(replies), [], Usage()

    def ask(self, text):
        self.sent.append(text)
        return self.replies.pop(0)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "app.py").write_text("print('hello')\n")  # doesn't print asdf yet
    ws = Workspace(root, root / ".bak", sandbox=False, highlighter=Highlighter())
    ws.collect([str(root / "app.py")])
    return ws


def run(ws, chat, *answers, tty=True, monkeypatch=None):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cmd = choose_test_command(chat, ws, Transcript(), {"app.py": "print('hello')\n"}, "", "the output contains asdf")
    return cmd, out.getvalue()


CANDIDATES = (f"{sys.executable} app.py | grep -q asdf", f"{sys.executable} app.py", "grep -q asdf app.py")


def test_user_tries_some_candidates_and_picks_one(project, monkeypatch):
    chat = FakeChat([commands_reply(*CANDIDATES)])
    cmd, out = run(project, chat, "1 2", "1", monkeypatch=monkeypatch)
    assert cmd == CANDIDATES[0]
    assert "the output contains asdf" in chat.sent[0] and "exit 0 when that holds" in chat.sent[0]
    assert "1. $ " in out and "fails now, so it detects the problem" in out  # grep finds no asdf -> exit 1
    assert "2. $ " in out and "passes already, so it won't detect the problem" in out and "hello" in out
    assert "3. $ " not in out  # not picked, not run


def test_user_can_type_their_own_command(project, monkeypatch):
    cmd, _ = run(project, FakeChat([commands_reply(*CANDIDATES)]), "", "my own check", monkeypatch=monkeypatch)
    assert cmd == "my own check"


def test_r_asks_gemini_for_other_candidates_with_the_results(project, monkeypatch):
    chat = FakeChat([commands_reply(CANDIDATES[1]), commands_reply(CANDIDATES[0])])
    cmd, _ = run(project, chat, "", "r it needs to look at the output", "", "1", monkeypatch=monkeypatch)
    assert cmd == CANDIDATES[0]
    assert "They say: it needs to look at the output" in chat.sent[1] and "exited 0" in chat.sent[1]


def test_without_a_terminal_the_first_failing_candidate_is_used(project, monkeypatch):
    chat = FakeChat([commands_reply(CANDIDATES[1], CANDIDATES[0])])
    cmd, out = run(project, chat, tty=False, monkeypatch=monkeypatch)
    assert cmd == CANDIDATES[0] and "the first that fails now" in out


def test_preview_keeps_head_and_tail():
    text = "\n".join(f"line {i}" for i in range(30))
    shown = preview(text).splitlines()
    assert shown[0] == "line 0" and shown[-1] == "line 29" and "18 more lines" in shown[6]
