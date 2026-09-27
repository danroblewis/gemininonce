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

SPEC = ("# Spec\n\n## 3. Architecture\nOne module, add.py.\n\n## 4. Interface\n```python\n"
        "def add(a: int, b: int) -> int: ...\n```\n\n## 6. Requirements\n- R1: add(a, b) returns a + b. "
        "Acceptance: add(2, 3) == 5\n")
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
        """A reply is either blocks, or a string: a whole Markdown answer (like a spec written as the reply)."""
        self.sent.append(text)
        r = self.replies.pop(0)
        self.usage.record(len(text), 100)
        self.last_markdown = r if isinstance(r, str) else ""
        return [{"kind": "text", "text": r}] if isinstance(r, str) else r

    def new_chat(self):
        self.new_chats += 1

    def select_model(self, name):
        if name == "pro" and getattr(self, "signed_out", False):
            raise SystemExit("Model '3.1 Pro' isn't available here (signed out: only 3.5 Flash-Lite).")
        self.models = getattr(self, "models", []) + [name]
        self.model = name

    def current_model(self):
        return self.model


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(loop_module.time, "sleep", lambda s: None)

    def make(replies, *extra):
        args = build_parser().parse_args(["add numbers", "--dir", str(tmp_path), "--accept",
                                          "-t", f"{sys.executable} -m pytest -q -p no:cacheprovider",
                                          *(extra or ("--spec-reviews", "0"))])
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
    assert "FILE: SPEC.md" in chat.sent[2] and SPEC.strip() in chat.sent[2]  # the implementer gets the spec


def test_tests_that_pass_without_code_are_sent_back(build, tmp_path):
    (tmp_path / "SPEC.md").write_text(SPEC)
    b, chat, _ = build([reply(("tests/test_add.py", VACUOUS)), reply(("tests/test_add.py", TESTS)),
                        reply(("add.py", CODE))], "--from", "tests")
    passed, printed = run_quiet(b.run)
    assert passed
    assert "pass before anything is implemented" in chat.sent[1]
    assert (tmp_path / "tests/test_add.py").read_text() == TESTS


def test_thin_spec_is_sent_back_before_the_user_sees_it(build, tmp_path):
    thin = "# Spec\n\n## Interface\n- add.py: add(a, b)\n\nIt adds numbers.\n"
    b, chat, _ = build([reply(("SPEC.md", thin)), reply(("SPEC.md", SPEC))])
    run_quiet(b.write_spec)
    assert "numbered, testable requirements" in chat.sent[1]
    assert "Interface section must spell out the API as code" in chat.sent[1]
    assert "Architecture section" in chat.sent[1]
    assert (tmp_path / "SPEC.md").read_text() == SPEC


def test_discussion_needs_a_terminal_unless_accepted(build, tmp_path):
    b, _, _ = build([reply(("SPEC.md", SPEC))])
    b.args.accept = False
    with pytest.raises(SystemExit, match="needs a terminal"):
        run_quiet(b.write_spec)


def test_user_settles_the_spec_by_talking_to_gemini(build, tmp_path, monkeypatch):
    """Gemini asks first, the user answers, asks for a change, looks at it, and accepts: all in one
    conversation, with the change shown as a diff."""
    questions = [{"kind": "text", "text": "Before I write it:\n1. Integers only, or floats too?"}]
    updated = SPEC.replace("- R1:", "- R2: add(a, b) accepts floats. Acceptance: add(0.5, 0.25) == 0.75\n- R1:")
    b, chat, _ = build([questions, reply(("SPEC.md", SPEC)), reply(("SPEC.md", updated))])
    b.args.accept = False
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["", "integers only", "please also support floats", "/show", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    _, printed = run_quiet(b.write_spec)
    assert "Before I write it" in printed and "no finished spec yet" in printed  # Enter before a spec exists
    assert chat.sent[1].startswith("The user says:\nintegers only")
    assert chat.sent[2].startswith("The user says:\nplease also support floats")
    assert "changes to SPEC.md" in printed and "+- R2: add(a, b) accepts floats" in printed
    assert printed.count("── SPEC.md") == 2  # shown in full once, then again only on /show
    assert (tmp_path / "SPEC.md").read_text() == updated and len(chat.sent) == 3


def test_spec_uses_pro_and_is_self_reviewed_then_stages_switch_back(build, tmp_path):
    thinner = SPEC.replace("Acceptance: add(2, 3) == 5", "")
    b, chat, _ = build([reply(("SPEC.md", thinner)), reply(("SPEC.md", SPEC)),
                        reply(("tests/test_add.py", TESTS)), reply(("add.py", CODE))], "--spec-reviews", "1")
    passed, printed = run_quiet(b.run)
    assert passed and chat.models == ["pro", "flash", "flash"]  # spec on Pro; tests and code on the default
    assert "as the test writer will" in chat.sent[1] and "re-read SPEC.md" in printed
    assert (tmp_path / "SPEC.md").read_text() == SPEC  # the self-reviewed version is what the user sees


def test_signed_out_keeps_flash_lite_and_explains(build, tmp_path):
    b, chat, _ = build([reply(("SPEC.md", SPEC))], "--anonymous", "--spec-reviews", "0")
    chat.signed_out = True
    _, printed = run_quiet(b.write_spec)
    assert "only Flash-Lite is available" in printed and not getattr(chat, "models", [])


def test_spec_written_as_the_reply_itself_is_saved_from_its_title(build, tmp_path):
    """Gemini can't put a spec with code blocks inside one FILE: code block, so it answers with the spec
    as its reply; anything before the `# ` title (e.g. its self-review notes) is dropped."""
    answer = "Issues I found:\n1. R1 had no acceptance criteria\n\n" + SPEC
    b, chat, _ = build([answer])
    run_quiet(b.write_spec)
    assert (tmp_path / "SPEC.md").read_text() == SPEC
    assert "the specification is your reply itself" in chat.sent[0]  # not a FILE: block


def test_long_questions_are_shown_in_full(build, tmp_path, monkeypatch):
    questions = "Before I write the spec I need to know:\n" + "\n".join(f"{i}. Question {i}?" for i in range(1, 16))
    b, chat, _ = build([questions, SPEC])
    b.args.accept = False
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["answers: yes to all", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    _, printed = run_quiet(b.write_spec)
    assert "Question 15?" in printed and "Gemini's full message" in printed
    assert chat.sent[1].startswith("The user says:\nanswers: yes to all")
    assert (tmp_path / "SPEC.md").read_text() == SPEC
