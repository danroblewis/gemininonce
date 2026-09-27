"""--check: plain-language description -> candidate test commands -> the user's pick. Fake Gemini, no network."""
import contextlib
import io
import sys

import pytest

from geminonce.checks import candidates, choose_test_command, preview, verdict
from geminonce.highlight import Highlighter
from geminonce.transcript import Transcript
from geminonce.usage import Usage
from geminonce.workspace import Workspace

PY = sys.executable
# (description, command) candidates for "the output contains asdf", against an app.py that prints "hello"
VERDICT = (f"{PY} -c \"import subprocess; o = subprocess.run(['{PY}', 'app.py'], capture_output=True, text=True).stdout; "
           f"ok = 'asdf' in o; print(('PASS: found asdf' if ok else 'FAIL: expected asdf in the output, got ' + repr(o))); "
           f"raise SystemExit(0 if ok else 1)\"")
CANDIDATES = [
    ("Pipes the output to grep.", f"{PY} app.py | grep -q asdf"),               # fails, but says nothing
    ("Asserts asdf is in the output, with a verdict.", VERDICT),                # FAIL: with a reason
    ("Just runs the program.", f"{PY} app.py"),                                  # passes already
    ("A broken check.", f"{PY} -c \"import no_such_module\""),                  # crashes: errored
]


def commands_reply(cands):
    blocks = []
    for desc, cmd in cands:
        blocks += [{"kind": "text", "text": f"{desc}\nCOMMAND:"}, {"kind": "code", "text": cmd}]
    return blocks


class FakeChat:
    model = "3.5 Flash-Lite"

    def __init__(self, replies):
        self.replies, self.sent, self.usage = list(replies), [], Usage()

    def ask(self, text):
        self.sent.append(text)
        self.usage.record(len(text), 100)
        return self.replies.pop(0)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path.resolve()
    (root / "app.py").write_text("print('hello')\n")  # doesn't print asdf yet
    w = Workspace(root, root / ".bak", sandbox=False, highlighter=Highlighter())
    w.collect([str(root / "app.py")])
    return w


def run(ws, chat, *answers, tty=True, monkeypatch=None):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cmd = choose_test_command(chat, ws, Transcript(), {"app.py": "print('hello')\n"}, "", "the output contains asdf")
    return cmd, out.getvalue()


def test_candidates_pair_each_command_with_its_description():
    assert candidates(commands_reply(CANDIDATES[:2])) == CANDIDATES[:2]


def test_verdicts_say_why_a_check_fails():
    assert verdict(1, "FAIL: expected 2 lines, got 5\n")[0] == "fails"
    assert verdict(0, "PASS: 2 lines\n")[0] == "passes"
    assert verdict(1, "Traceback (most recent call last):\n  ...\nNameError: x\n")[0] == "errored"
    assert verdict(1, "")[0] == "unclear"


def test_list_is_spaced_described_and_free_of_low_noise(ws, monkeypatch):
    chat = FakeChat([commands_reply(CANDIDATES)])
    _, out = run(ws, chat, "2", "2", monkeypatch=monkeypatch)
    listing = out.split("Candidate test commands:")[1].split("✗")[0]
    assert "\n\n  1. Pipes the output to grep.\n" in listing and "\n\n  2. Asserts asdf" in listing
    assert "[LOW]" not in out and "runs shell commands" not in out
    assert "Asking Gemini for test commands" in out and "── Gemini" not in out  # no raw reply dump


def test_each_tried_check_shows_its_own_verdict(ws, monkeypatch):
    cmd, out = run(ws, FakeChat([commands_reply(CANDIDATES)]), "", "2", monkeypatch=monkeypatch)
    assert cmd == VERDICT
    assert "✗ FAIL: expected asdf in the output, got 'hello\\n'" in out and "detects the problem" in out
    assert "✓ passes already" in out and "won't detect the problem" in out
    assert "⚠ errored without a verdict" in out and "may be broken" in out
    assert "fails (exit 1) without saying why" in out


def test_high_risk_candidates_are_flagged_above_the_list(ws, monkeypatch):
    risky = [("Cleans up first.", "rm -rf ~/ && true")] + CANDIDATES[1:2]
    _, out = run(ws, FakeChat([commands_reply(risky)]), "2", "2", monkeypatch=monkeypatch)
    assert out.index("[HIGH] candidate 1: recursive/forced delete") < out.index("Candidate test commands:")


def test_user_can_type_their_own_command(ws, monkeypatch):
    cmd, _ = run(ws, FakeChat([commands_reply(CANDIDATES)]), "1", "my own check", monkeypatch=monkeypatch)
    assert cmd == "my own check"


def test_r_asks_gemini_for_other_candidates_with_the_results(ws, monkeypatch):
    chat = FakeChat([commands_reply(CANDIDATES[2:3]), commands_reply(CANDIDATES[1:2])])
    cmd, _ = run(ws, chat, "", "r it needs to look at the output", "", "1", monkeypatch=monkeypatch)
    assert cmd == VERDICT
    assert "They say: it needs to look at the output" in chat.sent[1] and "exited 0" in chat.sent[1]
    assert "PASS:" in chat.sent[0] and "FAIL:" in chat.sent[0]  # candidates must report a verdict


def test_without_a_terminal_a_clear_fail_verdict_wins(ws, monkeypatch):
    cmd, out = run(ws, FakeChat([commands_reply(CANDIDATES)]), tty=False, monkeypatch=monkeypatch)
    assert cmd == VERDICT and "with a clear verdict" in out  # not the silent grep listed first


def test_preview_keeps_head_and_tail():
    text = "\n".join(f"line {i}" for i in range(30))
    shown = preview(text).splitlines()
    assert shown[0] == "line 0" and shown[-1] == "line 29" and "18 more lines" in shown[6]


def test_label_prefixes_are_dropped_from_descriptions():
    blocks = commands_reply([("What it checks: Passing --top 2 prints 2 lines.", "true")])
    assert candidates(blocks) == [("Passing --top 2 prints 2 lines.", "true")]
