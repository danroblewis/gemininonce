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
        "PROJECT FILES:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(text)}" for rel, text in files.items()),
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
exit codes, output format) if there is one. Give each item a one-line description. Anything a
test might call or check must be here, spelled exactly as it will be implemented.

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
"""


def _context(files: dict[str, str], layout: str) -> list[str]:
    return [layout, "EXISTING FILES:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(t)}" for rel, t in files.items())
            if files else ""]


def spec_prompt(idea: str, spec_path: str, test_cmd: str, files: dict[str, str], layout: str,
                may_ask: bool = True) -> str:
    return "\n\n".join(filter(None, [
        "You are a software architect writing a SPECIFICATION (not code) together with the user.",
        f"IDEA: {idea}",
        "Why the spec matters: the next step is a separate test writer that sees ONLY this spec. It has no "
        "other context and makes no design decisions. It must be able to write complete tests without "
        "guessing a single module name, class, method, signature, argument order, return type, data format "
        "or exception. After that, an implementer writes code to pass those tests. So the spec has to "
        "contain the whole design: architecture, interfaces and behavior. A thin spec produces thin tests "
        "and the wrong program.",
        f"Write it as Markdown to `FILE: {spec_path}`, following this template. Keep every section, and "
        f"be concrete and complete rather than brief:\n{fenced(SPEC_TEMPLATE, 'markdown')}",
        f"Tests will be run exactly as `{test_cmd}` from the project root, so choose module names and a "
        "layout the tests can import when run that way.",
        ("If the idea leaves decisions open that would change the design, you may first ask the user up to 5 "
         "short numbered questions, in a reply with no FILE: block. Otherwise write the spec, and list what "
         "you decided in section 10." if may_ask else
         "Don't ask questions: make reasonable decisions and list them in section 10."),
        *_context(files, layout),
        RULES,
    ]))


def spec_self_review_prompt(spec_path: str) -> str:
    """Have the spec writer read its own spec the way the test writer will."""
    return "\n\n".join([
        f"Now review {spec_path} as the test writer will: someone who sees ONLY this file and must write "
        "complete tests from it. First list, briefly, everything they'd have to guess or that's ambiguous: "
        "missing or vague names, signatures, argument order, types, return values, data formats, error "
        "behavior, ordering, and requirements without checkable acceptance criteria. Also list anything "
        "that contradicts itself.",
        f"Then send the complete revised {spec_path} that resolves every point (record the decisions you "
        "made in section 10).",
        RULES,
    ])


def discussion_prompt(what: str, paths: list[str], user_text: str) -> str:
    """The user's reply while reviewing a stage's files (spec or tests)."""
    files = ", ".join(paths) or f"the {what}"
    return "\n\n".join([
        f"The user says:\n{user_text}",
        f"Answer any questions briefly and ask your own if something is still unclear. If this changes the "
        f"{what}, send the complete updated {files} with FILE: blocks (the whole file, not just the changes).",
        RULES,
    ])


def tests_prompt(spec: str, spec_path: str, test_cmd: str, tests_dir: str, files: dict[str, str],
                 layout: str) -> str:
    return "\n\n".join(filter(None, [
        "You are writing TESTS for code that does not exist yet (test-first). The specification below "
        "was agreed with the user and is the only source of truth.",
        f"- Tests run exactly as `{test_cmd}` from the project root. Put every test file under `{tests_dir}/`.\n"
        "- Use the names, signatures, types and exceptions exactly as the spec's Interface section gives them.\n"
        "- Cover every numbered requirement's acceptance criteria, every example, every error and edge case, "
        "and any measurable non-functional requirement. Write one focused test per behavior, named after it "
        "(e.g. test_r3_rejects_empty_input).\n"
        "- Do NOT write the implementation, stubs, or mocks of the code under test: these tests must "
        "fail until the implementation exists.",
        f"{spec_path}:\n{fenced(spec, 'markdown')}",
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


def spec_problems_prompt(problems: list[str], notes, spec_path: str) -> str:
    return "\n\n".join(filter(None, [
        f"{spec_path} isn't complete yet:\n" + "\n".join(f"- {p}" for p in problems),
        *notes,
        f"Send the complete updated {spec_path}.",
        RULES,
    ]))


def implement_message(spec_path: str, tests_dir: str, test_cmd: str) -> str:
    return (f"Implement the project described in {spec_path} so that all tests pass when run exactly as "
            f"`{test_cmd}` from the project root. {spec_path} and the tests in {tests_dir}/ were agreed with the "
            f"user and are locked: don't change them. If the tests can't import the code when run that way, "
            f"that's yours to fix: lay out the code accordingly or add configuration at the project root (for "
            f"example a conftest.py or pyproject.toml). If a test looks wrong, say so in your reply instead of "
            f"working around it.")
