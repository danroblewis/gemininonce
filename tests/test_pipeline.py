"""The build pipeline (spec -> test plan -> tests -> code) with a scripted fake Gemini. No network."""
import contextlib
import io
import sys

import pytest

from gemininonce import loop as loop_module
from gemininonce import protocol
from gemininonce.highlight import Highlighter
from gemininonce.pipeline import Build, build_parser
from gemininonce.transcript import Transcript
from gemininonce.usage import Usage
from gemininonce.workspace import Workspace

SPEC = ("# Spec\n\n## 3. Architecture\nOne module, add.py.\n\n## 4. Interface\n```python\n"
        "def add(a: int, b: int) -> int: ...\n```\n\n## 6. Requirements\n- R1: add(a, b) returns a + b. "
        "Acceptance: add(2, 3) == 5\n")
PLAN = ("# Test plan\n\n## Approach\nNo external connections.\n\n## Unit tests (tests/unit/)\n"
        "- R1: test_r1_adds: add(2, 3) == 5\n\n## End-to-end tests (tests/e2e/)\n"
        "- R1: test_r1_e2e_adds: imported as a user would, add(2, 3) == 5\n")
UNIT = "from add import add\n\n\ndef test_r1_adds():\n    assert add(2, 3) == 5\n"
E2E = "from add import add\n\n\ndef test_r1_e2e_adds():\n    assert add(2, 3) == 5\n"
TESTS = (("tests/unit/test_add.py", UNIT), ("tests/e2e/test_add_e2e.py", E2E))
VACUOUS = (("tests/unit/test_add.py", "def test_r1_nothing():\n    assert True\n"),
           ("tests/e2e/test_add_e2e.py", "def test_r1_e2e_nothing():\n    assert True\n"))
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
        self.last_sources = ["https://example.org/doc"] if isinstance(r, str) and "(https://" in r else []
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

    def make(replies, *extra, reviews=(), research=False):
        """replies: the writer's; reviews: the independent reviewer's (reviews and web research are off unless
        asked for)."""
        args = build_parser().parse_args(["add numbers", "--dir", str(tmp_path), "--accept",
                                          *([] if research else ["--no-research"]),
                                          "-t", f"{sys.executable} -m pytest -q -p no:cacheprovider",
                                          "--spec-reviews", "0", "--plan-reviews", "0", "--tests-reviews", "0",
                                          "--tests-per-requirement", "1", *extra])
        ws = Workspace(tmp_path.resolve(), tmp_path / ".bak", sandbox=False, allow_new_files=True,
                       highlighter=Highlighter())
        chat = ScriptedChat(replies)
        b = Build(args, ws, chat, Transcript(), reviewer_factory=lambda: make.reviewer)
        make.reviewer = ScriptedChat(reviews)
        return b, chat, ws
    return make


def run_quiet(fn):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        return fn(), out.getvalue()


def test_idea_to_passing_code_with_locked_tests(build, tmp_path):
    b, chat, ws = build([
        reply(("SPEC.md", SPEC), ("add.py", "sneaky early code\n")),  # spec stage may only write SPEC.md
        PLAN,
        reply(*TESTS),
        reply(("tests/unit/test_add.py", "def test_r1_adds():\n    pass\n"), ("add.py", CODE)),  # can't touch tests
    ])
    passed, printed = run_quiet(b.run)
    assert passed
    assert (tmp_path / "SPEC.md").read_text() == SPEC and (tmp_path / "tests/TEST_PLAN.md").read_text() == PLAN
    assert (tmp_path / "tests/unit/test_add.py").read_text() == UNIT  # the implementer's rewrite was refused
    assert (tmp_path / "add.py").read_text() == CODE
    assert "only SPEC.md may be written" in printed and "is locked" in printed
    assert chat.new_chats == 3  # a fresh conversation for the plan, the tests, and the code
    assert "IDEA: add numbers" in chat.sent[0]
    assert SPEC in chat.sent[1] and "WHAT A GOOD TEST SUITE LOOKS LIKE" in chat.sent[1]  # the planner
    assert SPEC in chat.sent[2] and PLAN in chat.sent[2] and "tests/e2e/" in chat.sent[2]  # the test writer
    assert "FILE: SPEC.md" in chat.sent[3] and SPEC.strip() in chat.sent[3]  # the implementer gets the spec


def test_tests_that_pass_without_code_are_sent_back(build, tmp_path):
    (tmp_path / "SPEC.md").write_text(SPEC)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/TEST_PLAN.md").write_text(PLAN)
    b, chat, _ = build([reply(*VACUOUS), reply(*TESTS), reply(("add.py", CODE))], "--from", "tests")
    passed, printed = run_quiet(b.run)
    assert passed
    assert "pass before anything is implemented" in chat.sent[1]
    assert (tmp_path / "tests/unit/test_add.py").read_text() == UNIT


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
    assert "Before I write it" in printed and "no finished specification yet" in printed  # Enter before a spec exists
    assert chat.sent[1].startswith("The user says:\nintegers only")
    assert chat.sent[2].startswith("The user says:\nplease also support floats")
    assert "changes to SPEC.md" in printed and "+- R2: add(a, b) accepts floats" in printed
    assert printed.count("── SPEC.md") == 2  # shown in full once, then again only on /show
    assert (tmp_path / "SPEC.md").read_text() == updated and len(chat.sent) == 3


def test_spec_and_plan_use_pro_and_later_stages_switch_back(build, tmp_path):
    b, chat, _ = build([SPEC, PLAN, reply(*TESTS), reply(("add.py", CODE))])
    passed, _ = run_quiet(b.run)
    assert passed and chat.models == ["pro", "pro", "flash", "flash"]


def test_spec_is_reviewed_by_a_fresh_session_that_sees_only_the_spec(build, tmp_path):
    thinner = SPEC.replace("Acceptance: add(2, 3) == 5", "")
    b, chat, _ = build([thinner, SPEC], "--spec-reviews", "2",
                       reviews=["1. R1 has no acceptance criteria: what does add(2, 3) return?", "NO ISSUES"])
    _, printed = run_quiet(b.write_spec)
    reviewer = b.reviewer
    assert len(reviewer.sent) == 2 and reviewer.new_chats == 1  # a fresh conversation for each review
    first_review = reviewer.sent[0]
    assert thinner.strip() in first_review and "IDEA" not in first_review  # only the spec, none of the conversation
    assert chat.sent[1].startswith("An independent reviewer read the specification")
    assert "R1 has no acceptance criteria" in chat.sent[1]
    assert (tmp_path / "SPEC.md").read_text() == SPEC and "Reviewer found no issues" in printed
    assert len(chat.sent) == 2  # "NO ISSUES" ends the reviewing


def test_tests_are_reviewed_against_the_spec_and_plan_by_a_fresh_session(build, tmp_path):
    (tmp_path / "SPEC.md").write_text(SPEC)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/TEST_PLAN.md").write_text(PLAN)
    sloppy = UNIT.replace("== 5", "== 5\n    assert add.__doc__ == 'Adds.'")
    b, chat, _ = build([reply(("tests/unit/test_add.py", sloppy), TESTS[1]), reply(*TESTS),
                        reply(("add.py", CODE))], "--from", "tests", "--tests-reviews", "1",
                       reviews=["1. test_r1_adds checks add.__doc__, which the spec never promises."])
    passed, _ = run_quiet(b.run)
    assert passed
    review = b.reviewer.sent[0]
    assert SPEC.strip() in review and PLAN.strip() in review and "FILE: tests/unit/test_add.py" in review
    assert "__doc__" in review and "e2e tests use no mocks" in review
    assert "An independent reviewer compared your tests" in chat.sent[1] and "never promises" in chat.sent[1]
    assert (tmp_path / "tests/unit/test_add.py").read_text() == UNIT


def test_signed_out_keeps_flash_lite_and_explains(build, tmp_path):
    b, chat, _ = build([reply(("SPEC.md", SPEC))], "--anonymous")
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


def test_python_comments_in_the_interface_are_not_mistaken_for_headings():
    """`# pkg/core.py` inside an Interface code block is a comment, not the end of the section."""
    from gemininonce.pipeline import spec_problems
    spec = ("# Spec\n\n## 3. Architecture\npkg/\n\n## 4. Interface\n\n```python\n# pkg/core.py\n\n"
            "class Solver:\n    def solve(self, n: int) -> list[int]: ...\n```\n\n## 6. Requirements\n- R1: works\n")
    assert spec_problems(spec) == []


def test_thin_test_plan_is_sent_back_then_reviewed_by_a_fresh_session(build, tmp_path):
    (tmp_path / "SPEC.md").write_text(SPEC)
    thin = "# Test plan\n\n## Unit tests\n- check that add works\n"
    b, chat, _ = build([thin, PLAN, PLAN], "--from", "plan", "--plan-reviews", "1",
                       reviews=["1. No boundary cases for R1 (negative numbers, zero)."])
    run_quiet(b.write_plan)
    problems = chat.sent[1]
    assert "End-to-end tests section" in problems and "No test cases for R1" in problems
    assert "named test cases" in problems
    review = b.reviewer.sent[0]
    assert SPEC.strip() in review and PLAN.strip() in review and "WHAT A GOOD TEST SUITE" in review
    assert chat.sent[2].startswith("An independent reviewer read the test plan") and "boundary" in chat.sent[2]
    assert (tmp_path / "tests/TEST_PLAN.md").read_text() == PLAN


def test_tests_need_unit_and_e2e_every_requirement_and_enough_cases(build, tmp_path):
    (tmp_path / "SPEC.md").write_text(SPEC.replace("Acceptance: add(2, 3) == 5\n",
                                                   "Acceptance: add(2, 3) == 5\n- R2: add(0, 0) == 0\n"))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/TEST_PLAN.md").write_text(PLAN)
    only_unit = reply(("tests/unit/test_add.py", UNIT))
    b, chat, _ = build([only_unit, reply(*TESTS)], "--from", "tests", "--tests-per-requirement", "2",
                       "--patience", "1")
    with pytest.raises(SystemExit, match="still had problems"):  # still not enough tests after one more try
        run_quiet(b.write_tests)
    problems = chat.sent[1]
    assert "no tests in tests/e2e/" in problems
    assert "No tests mention R2" in problems
    assert "Only 1 test functions for 2 requirements" in problems


def test_spec_starts_with_a_short_research_question_then_builds_on_it(build, tmp_path):
    """A short question makes Gemini search; if it cites nothing, it's asked once more to search."""
    b, chat, _ = build(["From memory: backtracking.", "Found [the docs](https://example.org/doc).", SPEC, PLAN],
                       research=True)
    _, printed = run_quiet(b.write_spec)
    run_quiet(b.write_plan)
    assert chat.sent[0].startswith("Research this on the web before we design it: add numbers")
    assert chat.sent[1] == protocol.RESEARCH_RETRY and "No sources cited" in printed
    assert "Base the spec on your research above" in chat.sent[2] and "## 11. References" in chat.sent[2]
    assert "sources: 1 (example.org)" in printed
    assert "use Google Search to check their current documentation" in chat.sent[3]  # the planner
    assert (tmp_path / "SPEC.md").read_text() == SPEC


def test_no_research_skips_the_research_step(build, tmp_path):
    b, chat, _ = build([SPEC])
    run_quiet(b.write_spec)
    assert chat.sent[0].startswith("You are a software architect") and "research above" not in chat.sent[0]


def test_sources_line_names_the_sites():
    from gemininonce.transcript import sources_line
    urls = ["https://docs.python.org/3/a", "https://www.pypi.org/p", "https://docs.python.org/3/b",
            "https://github.com/x", "https://en.wikipedia.org/w", "https://stackoverflow.com/q"]
    assert sources_line(urls) == ("sources: 6 (docs.python.org, pypi.org, github.com, en.wikipedia.org, "
                                  "+1 more)")
