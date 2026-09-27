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
