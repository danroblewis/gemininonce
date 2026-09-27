"""The chat protocol: what we ask Gemini to do, how its replies are read, and the prompts we send."""
from __future__ import annotations

import re

RULES = """\
RULES FOR YOUR REPLY (a script applies your reply mechanically; you cannot run tools):
1. For every file you change or create, write a line `FILE: <relative/path>` and immediately after it
   ONE fenced code block containing the COMPLETE new contents of that file. Never send diffs,
   partial files, or placeholders like "... rest unchanged".
2. Only include files you actually change.
3. If a shell command must be run (e.g. install a dependency), write a line `COMMAND:` followed by a
   fenced code block containing only the command. The user will review it before it runs.
4. If you need to see a file you don't have (or the current version of one you changed earlier),
   write a line `READ: <relative/path>` for each file you need, all in one reply, with no FILE: blocks.
   Their contents come back in the next message. Don't guess at code you haven't seen.
5. Keep any explanation short, and put nothing but file contents / commands inside code blocks.
"""

MARKER = re.compile(r"^[\s*_#>`-]*(FILE|COMMAND|READ)[\s*_`]*:[\s*_`]*([^\s*`]*)", re.M)
LAZY = re.compile(r"\.\.\.\s*\(?\s*(rest|remaining|existing|unchanged|same as|other)", re.I)
GEMINI_ERROR = re.compile(r"encounter(?:ed|ing) an error|something went wrong|try again|can't help with that|"
                          r"having trouble|unable to (?:process|respond)", re.I)
NO_CODE_NUDGE = ("Your reply contained no FILE: blocks, so nothing was applied. Reply with the complete "
                 "contents of every file that needs to change.\n\n" + RULES)


def fenced(text: str, info: str = "") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{info}\n{text.rstrip()}\n{fence}"


def markers(text: str):
    """(kind, argument) for each FILE:/COMMAND: marker in a block of reply text."""
    for m in MARKER.finditer(text):
        yield m.group(1), m.group(2).strip().strip("'\"")


def parse_reply(blocks: list[dict]):
    """Pair each FILE:/COMMAND: marker with the code block after it -> (edits, commands, prose).
    READ: requests stand alone; see file_requests()."""
    edits, commands, prose, pending = [], [], [], None
    for b in blocks:
        if b["kind"] == "text":
            prose.append(b["text"])
            for marker in markers(b["text"]):
                if marker[0] != "READ":
                    pending = marker  # the last marker wins
        elif pending:
            kind, arg = pending
            if kind == "FILE" and arg:
                edits.append((arg, b["text"]))
            elif kind == "COMMAND" and b["text"].strip():
                commands.append(b["text"].strip())
            pending = None
        else:
            prose.append("[unlabeled code block ignored]")
    return edits, commands, "\n".join(prose)


def file_requests(blocks: list[dict]) -> list[str]:
    """Paths Gemini asked to see with `READ: <path>` lines, in order, without duplicates."""
    paths = [arg for b in blocks if b["kind"] == "text" for kind, arg in markers(b["text"]) if kind == "READ" and arg]
    return list(dict.fromkeys(paths))


def requested_files(files: dict[str, str]) -> str:
    """The files Gemini asked for, as a message section."""
    if not files:
        return ""
    return "REQUESTED FILES:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(text)}" for rel, text in files.items())


def read_reply(files: dict[str, str], notes) -> str:
    """Answer to a reply that only asked for files."""
    return "\n\n".join(filter(None, [requested_files(files), *notes, RULES]))


def test_result(test_cmd: str, code: int, out: str) -> str:
    return f"Test command `{test_cmd}` exited {code}. Output:\n{fenced(out)}"


def initial_prompt(message: str, test_out: str, files: dict[str, str], layout: str = "") -> str:
    return "\n\n".join(filter(None, [
        "You are fixing code in a local project.",
        f"TASK: {message}" if message else "TASK: make the test command pass.",
        test_out,
        layout,
        "PROJECT FILES:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(text)}" for rel, text in files.items()) if files
        else "The project directory is empty: create every file it needs, each with a FILE: block.",
        RULES,
    ]))


def reply_prompt(reply: str, notes=(), results=()) -> str:
    """A message typed by the user, plus what happened to Gemini's last edits and commands."""
    return "\n\n".join([reply, *notes, *results, RULES])


def hint_prompt(hint: str, test_cmd: str | None, code: int | None, out: str | None, notes=(), results=()) -> str:
    """The user's own guidance when Gemini is stuck, with the current test result for context."""
    state = [f"Current state: `{test_cmd}` fails (exit {code}). Output:\n{fenced(out)}"] if test_cmd and out else []
    return "\n\n".join([f"A message from the user: {hint}", *state, *notes, *results, RULES])


def failure_prompt(test_cmd: str, code: int, out: str, notes, results, stalled: bool) -> str:
    return "\n\n".join([
        f"I applied your changes. `{test_cmd}` still fails (exit {code}). Output:\n{fenced(out)}",
        *notes, *results,
        "Fix it." if not stalled else "That did not change the failure at all. Try a different approach.",
        RULES,
    ])


# --- build pipeline: spec -> tests -> code ----------------------------------------------------
SPEC_TEMPLATE = """\
# <Name>: Specification

## 1. Goal
What this is for and who uses it, in two or three sentences.

## 2. Scope
In scope: bullet list. Out of scope: bullet list (things someone might expect but that won't be built).

## 3. Architecture
The components and what each one is responsible for, how they interact, and the data flow between
them. Include the file/module layout as a tree, with one line per file saying what it contains.

## 4. Interface
The complete public API, as code in the project's language, with full signatures and type hints:
every module; every class with its constructor arguments, public attributes and methods; every
function; every custom exception class and what raises it; constants; the CLI (commands, arguments,
exit codes, output format) if there is one. For a web app, also every HTTP endpoint (method, path,
request and response JSON, status codes) and every UI component (props, state, user interactions and
what they do). Cover every source file in the Architecture layout. Give each item a one-line
description. Anything a test might call or check must be here, spelled exactly as it will be implemented.

## 5. Data model
Inputs, outputs, file formats and data structures, with their types and invariants.

## 6. Requirements
Numbered R1, R2, ... Each one: a single testable behavior, then "Acceptance:" with concrete,
checkable criteria (given / when / then, or exact inputs and expected results).

## 7. Examples
Worked examples written as calls with their exact results (for instance `>>> f(x)` followed by the
output), including a realistic end-to-end use.

## 8. Errors and edge cases
Every invalid input or unusual situation and exactly what happens (which exception, message, or
return value).

## 9. Non-functional requirements
Only measurable ones (e.g. "solves n=8 in under 1 second", "deterministic output order").

## 10. Assumptions and open questions
Decisions you made that the user didn't specify, so they can confirm or change them.

## 11. References
The sources you researched, as links, and what you took from each (an API, an algorithm, a standard,
a library version, a pitfall to avoid).
"""

def research_prompt(idea: str) -> str:
    """A short question, which makes Gemini actually search (a long document request usually doesn't)."""
    return (f"Research this on the web before we design it: {idea}. Look up existing libraries and prior art, "
            "the current documentation of any library, API or service it would use (exact endpoints, parameters, "
            "formats, versions), relevant standards, well-known algorithms, and common pitfalls. Give me a short "
            "research summary with a link for every source.")


RESEARCH_RETRY = ("Please use Google Search now to check current sources, and give the research summary again "
                  "with a link for every source.")

SPEC_RESEARCH = (
    "Base the spec on your research above rather than on memory alone, search again if you need more, and list "
    "every source you used, with its link, in section 11.")

PLAN_RESEARCH = (
    "If the project talks to real external services, use Google Search to check their current documentation "
    "before planning the e2e tests: endpoints, authentication, rate limits, and any sandbox or test mode.")


def doc_rules(kind: str) -> str:
    """How to reply when the reply itself is the document (a spec or a test plan)."""
    return (f"HOW TO REPLY: the {kind} is your reply itself, written as a normal Markdown answer (headings, "
            "lists, tables, code blocks). Start it with its `# ` title heading and put nothing after it. Don't use "
            "a FILE: block and don't wrap it in a code block. To look at a project file first, reply with only "
            "`READ: <relative/path>` lines. To ask the user questions, reply with only the questions.\n")


SPEC_RULES = doc_rules("specification")
PLAN_RULES = doc_rules("test plan")


def headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) of Markdown headings, ignoring `#` lines inside code blocks (such as
    Python comments in an Interface section)."""
    out, fence = [], None
    for i, ln in enumerate(lines):
        m = re.match(r"\s*(`{3,}|~{3,})", ln)
        if m and (fence is None or m.group(1).startswith(fence)):
            fence = None if fence else m.group(1)
        elif fence is None and (h := re.match(r"(#{1,6}) (.*)", ln)):
            out.append((i, len(h.group(1)), h.group(2)))
    return out


def document(markdown: str, looks_like: str) -> str | None:
    """The document inside a reply: from its first heading (preferably the `# ` title) to the end, if it
    matches `looks_like` (a regex); else None (the reply is conversation, e.g. questions)."""
    lines = markdown.splitlines()
    found = headings(lines)
    starts = [i for i, level, _ in found if level == 1] or [i for i, _, _ in found]
    if not starts:
        return None
    doc = "\n".join(lines[starts[0]:]).strip() + "\n"
    return doc if re.search(looks_like, doc, re.I | re.M) else None


def spec_document(markdown: str) -> str | None:
    return document(markdown, r"requirement|interface|^\W*R1\b")


def plan_document(markdown: str) -> str | None:
    return document(markdown, r"unit|e2e|end-to-end|test_")


def blocks_markdown(blocks: list[dict]) -> str:
    """Rough Markdown from extracted blocks (when the rendered-DOM version isn't available)."""
    return "\n\n".join(b["text"].strip() if b["kind"] == "text" else fenced(b["text"], b.get("lang", "").lower())
                        for b in blocks) + "\n"


def _context(files: dict[str, str], layout: str) -> list[str]:
    return [layout, "EXISTING FILES:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(t)}" for rel, t in files.items())
            if files else ""]


def spec_prompt(idea: str, spec_path: str, test_cmd: str, files: dict[str, str], layout: str,
                may_ask: bool = True, research: bool = True) -> str:
    return "\n\n".join(filter(None, [
        "You are a software architect writing a SPECIFICATION (not code) together with the user.",
        f"IDEA: {idea}",
        "Why the spec matters: the next step is a separate test writer that sees ONLY this spec. It has no "
        "other context and makes no design decisions. It must be able to write complete tests without "
        "guessing a single module name, class, method, signature, argument order, return type, data format "
        "or exception. After that, an implementer writes code to pass those tests. So the spec has to "
        "contain the whole design: architecture, interfaces and behavior. A thin spec produces thin tests "
        "and the wrong program.",
        f"Follow this template (it's saved as {spec_path}). Keep every section, and be concrete and complete "
        f"rather than brief:\n{fenced(SPEC_TEMPLATE, 'markdown')}",
        SPEC_RESEARCH if research else "",
        f"Tests will be run exactly as `{test_cmd}` from the project root, so choose module names and a "
        "layout the tests can import when run that way.",
        ("If the idea leaves decisions open that would change the design, you may first ask the user up to 5 "
         "short numbered questions, in a reply with only the questions. Otherwise write the spec, and list "
         "what you decided in section 10." if may_ask else
         "Don't ask questions: make reasonable decisions and list them in section 10."),
        *_context(files, layout),
        SPEC_RULES,
    ]))


NO_ISSUES = "NO ISSUES"

REVIEW_FORMAT = (f"Reply with a numbered list of issues. For each one: where it is, what's wrong, and what "
                 f"should change. Don't rewrite the document or send files. If nothing matters enough to "
                 f"change, reply with exactly: {NO_ISSUES}")


def spec_review_prompt(spec: str, spec_path: str) -> str:
    """For a fresh reviewer session that sees only the spec, the way the test writer will."""
    return "\n\n".join([
        "You are reviewing a software specification before tests are written from it. Review it as the TEST "
        "WRITER: you will see only this document and must write complete tests from it, with no other context "
        "and no design decisions of your own.",
        "Look for everything you'd have to guess or that's ambiguous: missing or vague module/class/function "
        "names, signatures, argument order, types, return values, data formats, error behavior (which "
        "exception, when), ordering and determinism, requirements without checkable acceptance criteria, "
        "examples whose results are missing or wrong, and contradictions between sections.",
        f"{spec_path}:\n{fenced(spec, 'markdown')}",
        REVIEW_FORMAT,
    ])


def tests_review_prompt(spec: str, spec_path: str, plan: str, plan_path: str, tests: dict[str, str],
                        test_cmd: str, tests_dir: str) -> str:
    """For a fresh reviewer session that sees only the spec, the test plan and the tests."""
    return "\n\n".join([
        "You are reviewing TESTS written from a specification and a test plan, before any implementation exists. "
        f"They run as `{test_cmd}` and are meant to fail until the code is written.",
        guide(tests_dir),
        "Check that:\n"
        "- every test case in the plan exists, and every requirement, example, error and boundary in the spec is "
        "tested (list what's missing by name);\n"
        "- names, signatures, types and exceptions match the spec's Interface exactly;\n"
        "- expected values are actually correct;\n"
        "- unit tests mock every external connection with autospec (a mock of something that doesn't exist must "
        "fail) and test its failures; e2e tests use no mocks;\n"
        "- every e2e test asserts that its real integration produced real results (not just that nothing crashed: "
        "it must fail if the external call returns nothing or errors);\n"
        "- the tests don't demand things the spec doesn't promise;\n"
        "- tests are independent and deterministic, and would fail only for a missing or wrong implementation.",
        f"{spec_path}:\n{fenced(spec, 'markdown')}",
        f"{plan_path}:\n{fenced(plan, 'markdown')}",
        "TESTS:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(text)}" for rel, text in tests.items()),
        REVIEW_FORMAT + " Name the test (or the missing case) in each issue.",
    ])


def tests_review_feedback_prompt(issues: str) -> str:
    """The fresh reviewer's findings, for the test writer."""
    return "\n\n".join([
        "An independent reviewer compared your tests with the specification and raised these points:"
        f"\n\n{issues}",
        "Fix the tests accordingly. If you disagree with a point, say why in your reply. Send every changed "
        "test file in full.",
        RULES,
    ])


def reviewer_found_nothing(reply: str) -> bool:
    return NO_ISSUES in reply.upper() and len(reply.strip()) < 200


def discussion_prompt(what: str, paths: list[str], user_text: str, document: str | None = None) -> str:
    """The user's reply while reviewing a stage's files. document: the kind of document ("specification",
    "test plan") when the stage's file is the reply itself rather than FILE: blocks."""
    files = ", ".join(paths) or f"the {what}"
    how = (f"reply with the complete updated {document}, starting with its `# ` title heading" if document else
           f"send the complete updated {files} with FILE: blocks (the whole file, not just the changes)")
    return "\n\n".join([
        f"The user says:\n{user_text}",
        f"Answer any questions briefly and ask your own if something is still unclear. If this changes the "
        f"{what}, {how}.",
        doc_rules(document) if document else RULES,
    ])


TESTING_GUIDE = """\
WHAT A GOOD TEST SUITE LOOKS LIKE
- MANY small, focused tests. More is better. For every requirement, test the normal case, every acceptance
  criterion, the boundaries (smallest, largest, just outside), every error case, and edge cases (empty, one
  item, many, unusual but valid input). Use every example in the spec verbatim. Use parametrized tables
  (e.g. pytest.mark.parametrize) to cover many inputs cheaply.
- Check properties, not only examples: invariants that must always hold (round trips, a returned solution
  actually satisfies the rules, sizes and ordering guarantees), checked across ranges of inputs.
- Test behavior through the public interface in the spec, not private helpers or internal state. Assert
  everything the spec promises, and nothing it doesn't (exact messages, ordering or algorithms it leaves open).
- Precise assertions: exact values where the spec defines them, the exact exception type, the whole
  output structure.
- Independent and deterministic: no shared state or order dependence between tests; seed randomness;
  control time.
- Name each test after its requirement and behavior (test_r3_rejects_empty_input), with a one-line docstring.

TWO KINDS OF TESTS, IN TWO FOLDERS
- {tests_dir}/unit/: fast, isolated tests of each component. Mock every external connection (network/HTTP,
  databases, other services, subprocesses, the clock) at the boundary. Test how the code talks to them (called
  with the right arguments) and how it handles their failures (errors, timeouts, bad data). Mock with
  autospec (patch(..., autospec=True), create_autospec, or spec=), so a mock of a function, method or
  attribute that doesn't really exist fails instead of silently passing.
- {tests_dir}/e2e/: the whole system through its real entry points (public API, CLI) with NO mocks. If it
  uses real services or the network, call them for real: network access is available. Each e2e test must
  PROVE its real integration works: assert on the real results (data came back, the fields that matter are
  filled in, files were actually produced), never just that nothing crashed. A test that passes when every
  external call fails proves nothing; graceful failure belongs in the mocked unit tests. Where real data
  varies, check its shape and invariants rather than exact values. Skip only when a required credential is
  truly missing, and say why.
"""

TEST_PLAN_TEMPLATE = """\
# Test plan

## Approach
What's unit-tested and what's end-to-end; what unit tests mock and how; which real services or network the
e2e tests use; shared fixtures and test data.

## Unit tests ({tests_dir}/unit/)
For each requirement (R1, R2, ...): a list of test cases, each with its test name, what it checks
(input -> expected result or exception) and what's mocked.

## End-to-end tests ({tests_dir}/e2e/)
Scenarios through the real entry points, without mocks, and what each one asserts.

## Coverage
A table: requirement -> test names. Every requirement gets at least {per_requirement} tests, including its
errors and boundaries.

## Not tested
Anything deliberately left out, and why.
"""


def guide(tests_dir: str) -> str:
    return TESTING_GUIDE.format(tests_dir=tests_dir)


def plan_prompt(spec: str, spec_path: str, test_cmd: str, tests_dir: str, per_requirement: int,
                files: dict[str, str], layout: str, may_ask: bool = True, research: bool = True) -> str:
    return "\n\n".join(filter(None, [
        "You are a test engineer planning the TEST SUITE for software that doesn't exist yet (test-first). The "
        "specification below was agreed with the user. Plan first: the plan is reviewed, then the tests are "
        "written from it, then an implementer makes them pass.",
        guide(tests_dir),
        PLAN_RESEARCH if research else "",
        f"Tests will run exactly as `{test_cmd}` from the project root.",
        f"Follow this template, keep every section, and be thorough: list every test case by name.\n"
        f"{fenced(TEST_PLAN_TEMPLATE.format(tests_dir=tests_dir, per_requirement=per_requirement), 'markdown')}",
        f"{spec_path}:\n{fenced(spec, 'markdown')}",
        ("If something in the spec makes the tests impossible to plan, you may first ask the user short numbered "
         "questions, in a reply with only the questions." if may_ask else
         "Don't ask questions: make reasonable decisions and note them in the plan."),
        *_context(files, layout),
        PLAN_RULES,
    ]))


def plan_review_prompt(spec: str, spec_path: str, plan: str, per_requirement: int, tests_dir: str) -> str:
    """For a fresh reviewer session that sees only the spec and the test plan."""
    return "\n\n".join([
        "You are reviewing a TEST PLAN against its specification, before any tests or code are written.",
        guide(tests_dir),
        "Look for: requirements, acceptance criteria, examples, errors or boundaries with no test case; fewer than "
        f"{per_requirement} test cases for a requirement; expected results that are wrong or vague; unit tests "
        "that would touch real external services instead of mocks, or mock without autospec; e2e tests that use "
        "mocks, or that would still pass if the real integration returned nothing (they must assert real results); "
        "missing failure handling (errors, timeouts, bad data) for external connections; tests of things the spec "
        "doesn't promise.",
        f"{spec_path}:\n{fenced(spec, 'markdown')}",
        f"TEST PLAN:\n{fenced(plan, 'markdown')}",
        REVIEW_FORMAT,
    ])


def doc_review_feedback_prompt(kind: str, issues: str) -> str:
    """A fresh reviewer's findings, for the writer of a document (spec or test plan)."""
    return "\n\n".join([
        f"An independent reviewer read the {kind} (with only what it needed) and raised these points:\n\n{issues}",
        f"Revise the {kind} to resolve them. If you disagree with a point, leave it and say why. Reply with the "
        f"complete updated {kind}, starting with its `# ` title heading.",
        doc_rules(kind),
    ])


def tests_prompt(spec: str, spec_path: str, plan: str, plan_path: str, test_cmd: str, tests_dir: str,
                 files: dict[str, str], layout: str) -> str:
    return "\n\n".join(filter(None, [
        "You are writing TESTS for code that does not exist yet (test-first), implementing the agreed test plan "
        "for the agreed specification. Both are below.",
        guide(tests_dir),
        f"- Tests run exactly as `{test_cmd}` from the project root. Put unit tests under `{tests_dir}/unit/` "
        f"and end-to-end tests under `{tests_dir}/e2e/`.\n"
        "- Write EVERY test case in the plan (add more if you see gaps; never fewer).\n"
        "- Use the names, signatures, types and exceptions exactly as the spec's Interface section gives them.\n"
        "- Do NOT write the implementation or stubs of the code under test (mocking EXTERNAL connections in unit "
        "tests is expected): these tests must fail until the implementation exists.",
        f"{spec_path}:\n{fenced(spec, 'markdown')}",
        f"{plan_path}:\n{fenced(plan, 'markdown')}",
        *_context(files, layout),
        RULES,
    ]))


def tests_problems_prompt(problems: list[str], notes, test_out: str) -> str:
    return "\n\n".join(filter(None, [
        "The tests need another pass:\n" + "\n".join(f"- {p}" for p in problems),
        *notes,
        f"Test output:\n{fenced(test_out)}" if test_out else "",
        "Send the corrected test files in full.",
        RULES,
    ]))


def doc_problems_prompt(kind: str, problems: list[str], notes) -> str:
    return "\n\n".join(filter(None, [
        f"The {kind} isn't complete yet:\n" + "\n".join(f"- {p}" for p in problems),
        *notes,
        f"Reply with the complete updated {kind}, starting with its `# ` title heading.",
        doc_rules(kind),
    ]))


def implement_message(spec_path: str, tests_dir: str, test_cmd: str) -> str:
    return (f"Implement the project described in {spec_path} so that all tests pass when run exactly as "
            f"`{test_cmd}` from the project root. {spec_path} and the tests in {tests_dir}/ were agreed with the "
            f"user and are locked: don't change them. If the tests can't import the code when run that way, "
            f"that's yours to fix: lay out the code accordingly or add configuration at the project root (for "
            f"example a conftest.py or pyproject.toml). The unit tests mock external connections; the e2e tests "
            f"call real services, and network access is available. If a test looks wrong, say so in your reply "
            f"instead of working around it.")


README_RULES = doc_rules("README")

README_TEMPLATE = """\
# <Project name>

One or two sentences: what it is and who it's for.

## Install
Every step from a fresh checkout, as commands: runtime version, creating a virtual environment, installing
dependencies, and any extra setup (e.g. `playwright install chromium`, `npm install` in a frontend folder).

## Configure
Environment variables, config files and API keys, with defaults. Say "Nothing to configure." if so.

## Run
The exact commands to start it (servers with host and port, background workers, the frontend dev server),
and what you should see when it's working (e.g. which URL to open).

## Usage
How to use it, with real examples: CLI commands and their output, HTTP endpoints with example requests
and responses, or what to click in the UI.

## Tests
How to run the unit tests and the end-to-end tests, and what the e2e tests need (e.g. network access).
"""


def readme_prompt(spec: str, spec_path: str, test_cmd: str, files: dict[str, str], layout: str) -> str:
    return "\n\n".join(filter(None, [
        "The project below is implemented and its tests pass. Write its README.md: the document someone reads to "
        "install it, run it and use it. Base every command, name, port, endpoint and option on the actual code "
        "below (not just the spec), so the instructions work when followed exactly.",
        f"Follow this template and keep every section:\n{fenced(README_TEMPLATE, 'markdown')}",
        f"The tests run as `{test_cmd}` from the project root.",
        f"{spec_path}:\n{fenced(spec, 'markdown')}",
        *_context(files, layout),
        README_RULES,
    ]))


def readme_document(markdown: str) -> str | None:
    return document(markdown, r"install|usage|run")


# --- --check: from a plain-language description to a test command ----------------------------------
CHECK_FORMAT = ("For each one: a short line saying what it checks, then a line `COMMAND:` and a code block containing "
                "only the command. Don't change or create any files.")

CHECK_VERDICT = ("Each command must report its own verdict: print exactly one line starting with `PASS:` or `FAIL:` "
                 "that says what was expected and what actually happened (e.g. `FAIL: expected 2 lines, got 5`), and "
                 "exit 0 for PASS and 1 for FAIL. If the program can't run at all, that's a FAIL too: catch it and "
                 "report it (e.g. `FAIL: the program exited 2: unrecognized arguments: --top`), don't let the check "
                 "crash. A short `python3 -c` script is usually the easiest way to do this.")


def check_prompt(check: str, files: dict[str, str], layout: str) -> str:
    return "\n\n".join(filter(None, [
        f"I need a shell command to use as a pass/fail test for this: {check}",
        "Propose 3 to 5 different candidate commands, from the simplest to the most thorough. Each must:\n"
        "- exit 0 when that holds and non-zero when it doesn't (e.g. `grep -q`, `test`, or a short `python3 -c` "
        "script that asserts), so the exit code IS the result;\n"
        "- run from the project root using what's already installed, finish within a few seconds, and not change "
        "any files;\n"
        "- not use the network unless the check is about something online.",
        CHECK_VERDICT,
        CHECK_FORMAT,
        layout,
        "PROJECT FILES:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(t)}" for rel, t in files.items()) if files else "",
    ]))


def check_retry_prompt(feedback: str, tried: list[tuple[str, int, str]]) -> str:
    """tried: (command, exit code, output) for the candidates that were run."""
    results = "\n\n".join(f"`{cmd}` exited {code}. Output:\n{fenced(out[-1500:] or '(none)')}" for cmd, code, out in tried)
    return "\n\n".join(filter(None, [
        f"Those didn't work for the user. {('They say: ' + feedback) if feedback else ''}".strip(),
        results,
        "Propose 3 to 5 different candidate commands, with the same requirements (exit code is the result, no file "
        "changes, fast).",
        CHECK_VERDICT,
        CHECK_FORMAT,
    ]))


CHECK_NUDGE = "Please propose the candidate commands now. " + CHECK_FORMAT
