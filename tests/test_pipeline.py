"""The build pipeline (spec -> tests -> code) with a scripted fake Gemini. No network."""
import contextlib
import io
import sys

import pytest

from gemininonce import loop as loop_module
from gemininonce.highlight import Highlighter
from gemininonce.pipeline import Build, build_parser
from gemininonce.transcript import Transcript
from gemininonce.usage import Usage
from gemininonce.workspace import Workspace

SPEC = "# Spec\n\n## Requirements\n- R1: add(a, b) returns a + b\n\n## Interface\n- add.py: add(a, b)\n"
TESTS = "from add import add\n\n\ndef test_r1_adds():\n    assert add(2, 3) == 5\n"
VACUOUS = "def test_nothing():\n    assert True\n"
CODE = "def add(a, b):\n    return a + b\n"


def reply(*files):
    blocks = []
    for path, text in files:
        blocks += [{"kind": "text", "text": f"FILE: {path}"}, {"kind": "code", "text": text}]
    return blocks


class ScriptedChat:
    model = "3.5 Flash-Lite"

    def __init__(self, replies):
        self.replies, self.sent, self.usage, self.new_chats = list(replies), [], Usage(), 0

    def ask(self, text):
        self.sent.append(text)
        r = self.replies.pop(0)
        self.usage.record(len(text), 100)
        return r

    def new_chat(self):
        self.new_chats += 1


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(loop_module.time, "sleep", lambda s: None)

    def make(replies, *extra):
        args = build_parser().parse_args(["add numbers", "--dir", str(tmp_path), "--accept",
                                          "-t", f"{sys.executable} -m pytest -q -p no:cacheprovider", *extra])
        ws = Workspace(tmp_path.resolve(), tmp_path / ".bak", sandbox=False, allow_new_files=True,
                       highlighter=Highlighter())
        chat = ScriptedChat(replies)
        return Build(args, ws, chat, Transcript()), chat, ws
    return make


def run_quiet(fn):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        return fn(), out.getvalue()


def test_idea_to_passing_code_with_locked_tests(build, tmp_path):
    b, chat, ws = build([
        reply(("SPEC.md", SPEC), ("add.py", "sneaky early code\n")),  # spec stage may only write SPEC.md
        reply(("tests/test_add.py", TESTS)),
        reply(("tests/test_add.py", "def test_r1_adds():\n    pass\n"), ("add.py", CODE)),  # can't touch tests
    ])
    passed, printed = run_quiet(b.run)
    assert passed
    assert (tmp_path / "SPEC.md").read_text() == SPEC
    assert (tmp_path / "tests/test_add.py").read_text() == TESTS  # the implementer's rewrite was refused
    assert (tmp_path / "add.py").read_text() == CODE
    assert "only SPEC.md may be written" in printed and "is locked" in printed
    assert chat.new_chats == 2  # a fresh conversation for tests, and again for code
    assert "IDEA: add numbers" in chat.sent[0] and SPEC in chat.sent[1]
    assert "locked" in chat.sent[2] and "was not written" not in chat.sent[2]


def test_tests_that_pass_without_code_are_sent_back(build, tmp_path):
    (tmp_path / "SPEC.md").write_text(SPEC)
    b, chat, _ = build([reply(("tests/test_add.py", VACUOUS)), reply(("tests/test_add.py", TESTS)),
                        reply(("add.py", CODE))], "--from", "tests")
    passed, printed = run_quiet(b.run)
    assert passed
    assert "pass before anything is implemented" in chat.sent[1]
    assert (tmp_path / "tests/test_add.py").read_text() == TESTS


def test_spec_without_numbered_requirements_is_sent_back(build, tmp_path):
    b, chat, _ = build([reply(("SPEC.md", "# Spec\n\nIt adds numbers.\n")), reply(("SPEC.md", SPEC))])
    run_quiet(b.write_spec)
    assert "numbered list of testable requirements" in chat.sent[1]
    assert (tmp_path / "SPEC.md").read_text() == SPEC


def test_review_needs_a_terminal_unless_accepted(build, tmp_path):
    b, _, _ = build([reply(("SPEC.md", SPEC))])
    b.args.accept = False
    with pytest.raises(SystemExit, match="needs a terminal"):
        run_quiet(b.write_spec)
