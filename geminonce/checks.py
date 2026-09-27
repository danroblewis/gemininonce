"""--check: turn a plain-language description of "working" into a test command.

Gemini proposes a few candidate commands that each report their own verdict (a `PASS:` or `FAIL:` line,
exit 0 or 1). The user picks which to try, sees each one's verdict and output, and chooses one to use as
the test (or types their own, or asks for other suggestions).
"""
from __future__ import annotations

import re
import sys

from . import protocol, safety
from .console import BOLD, DIM, GREEN, RED, YELLOW, ask_user, paint, paint_findings
from .gemini import GeminiTimeout

ERROR_SIGNS = re.compile(r"Traceback \(most recent call last\)|SyntaxError|NameError|command not found|"
                         r"syntax error|No such file or directory|unexpected EOF|Permission denied", re.I)


def preview(out: str, head: int = 6, tail: int = 6) -> str:
    """The first and last lines of some output, with '...' in between."""
    lines = out.rstrip("\n").splitlines()
    if len(lines) <= head + tail + 1:
        return "\n".join(lines)
    return "\n".join(lines[:head] + [f"... ({len(lines) - head - tail} more lines) ..."] + lines[-tail:])


def candidates(blocks: list[dict]) -> list[tuple[str, str]]:
    """(description, command) for each COMMAND: in Gemini's reply; the description is the text line just
    before the marker."""
    found, pending, last_text = [], False, ""
    for b in blocks:
        if b["kind"] == "text":
            for line in b["text"].splitlines():
                if protocol.MARKER.match(line):
                    pending = line.strip().upper().lstrip("*_`# ").startswith("COMMAND")
                elif line.strip():
                    last_text, pending = line.strip(), False
        elif pending and b["text"].strip():
            desc = re.sub(r"^\W*(what it checks|checks?|description)\W*:\s*", "", last_text, flags=re.I)
            found.append((desc, b["text"].strip()))
            pending, last_text = False, ""
    unique = {}
    for desc, cmd in found:
        unique.setdefault(cmd, desc)
    return [(desc, cmd) for cmd, desc in unique.items()]


def verdict(code: int, out: str) -> tuple[str, str, str]:
    """(kind, text, color) for a candidate's result before anything is fixed. kind: 'fails' (a real FAIL:
    verdict), 'passes', 'errored' (the check crashed: it may be broken), or 'unclear'."""
    said = [ln.strip() for ln in out.splitlines() if re.match(r"\s*(PASS|FAIL)\s*:", ln)]
    if code != 0 and any(s.upper().startswith("FAIL") for s in said):
        reason = next(s for s in said if s.upper().startswith("FAIL"))
        return "fails", f"✗ {reason}   (it detects the problem: good)", GREEN
    if code == 0:
        return "passes", "✓ passes already" + (f": {said[0]}" if said else "") + \
            "   (it won't detect the problem)", YELLOW
    last = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "no output")
    if ERROR_SIGNS.search(out):
        return "errored", f"⚠ errored without a verdict (exit {code}): {last}   (the check itself may be broken)", RED
    return "unclear", f"fails (exit {code}) without saying why: {last}", YELLOW


def show_candidates(found: list[tuple[str, str]], hl) -> None:
    warnings = [(i, f) for i, (_, cmd) in enumerate(found, 1) for f in safety.scan_command(cmd) if f[0] == "high"]
    if warnings:
        print()
        for i, f in warnings:
            print(paint_findings(f"    [HIGH] candidate {i}: {f[1]}"))
    print(paint("\n  Candidate test commands:", BOLD))
    shell = hl.shell_lexer()
    for i, (desc, cmd) in enumerate(found, 1):
        print(f"\n  {paint(f'{i}.', BOLD)} {desc}")
        print("\n".join("        " + ln for ln in hl.code(cmd, shell).splitlines()))


def choose_test_command(chat, ws, transcript, files: dict[str, str], layout: str, check: str) -> str:
    """Ask Gemini for candidate test commands for `check`, try the ones the user picks, and return the one
    they choose. Without a terminal, the first candidate with a real FAIL: verdict is used."""
    prompt, tries = protocol.check_prompt(check, files, layout), 0
    while True:
        print(paint("\n  Asking Gemini for test commands...", DIM))
        try:
            blocks = chat.ask(prompt)
        except GeminiTimeout as e:
            blocks = []
            print(paint(f"  {e}", YELLOW))
        if chat.usage.turns:
            print(paint(f"  $ {chat.usage.step(chat.model, len(chat.usage.turns) - 1, 'this reply')}", GREEN))
        found = candidates(blocks)
        if not found:
            tries += 1
            if tries > 3:
                sys.exit("Gemini didn't propose any test commands; give one with -t instead.")
            transcript.reply(blocks)
            prompt = protocol.CHECK_NUDGE
            continue

        show_candidates(found, ws.hl)
        interactive = sys.stdin.isatty()
        picks = list(range(1, len(found) + 1))
        if interactive:
            answer = ask_user(paint("\n  Try which? One number (2), several (1 3), or Enter for all: ", YELLOW)).strip()
            chosen = [int(x) for x in answer.replace(",", " ").split() if x.isdigit() and 1 <= int(x) <= len(found)]
            picks = chosen or picks

        results = {}
        for i in picks:
            desc, cmd = found[i - 1]
            code, out = ws.run(cmd, sandbox=ws.sandbox, show=False)
            kind, text, color = verdict(code, out)
            results[i] = (kind, code, out)
            print(f"\n  {paint(f'{i}.', BOLD)} {desc}")
            print(paint(f"     {text}", color))
            extra = "\n".join(ln for ln in out.splitlines() if not re.match(r"\s*(PASS|FAIL)\s*:", ln))
            if extra.strip():
                print("\n".join("       " + ln for ln in ws.hl.output(preview(extra)).splitlines()))

        if not interactive:
            for wanted in ("fails", "unclear"):
                good = [i for i in picks if results[i][0] == wanted]
                if good:
                    print(paint(f"\n  Using candidate {good[0]}: the first that fails now "
                                f"{'with a clear verdict' if wanted == 'fails' else ''} (no terminal to ask).", DIM))
                    return found[good[0] - 1][1]
            sys.exit("None of the proposed checks fails now for a clear reason; give one with -t.")

        while True:
            answer = ask_user(paint("\n  Use which one as the test? A number, your own command, "
                                    "r <what's wrong> for other suggestions, or q to quit: ", YELLOW)).strip()
            if answer in ("q", "/quit"):
                sys.exit(1)
            if answer.isdigit() and 1 <= int(answer) <= len(found):
                return found[int(answer) - 1][1]
            if answer == "r" or answer.startswith("r "):
                tried = [(found[i - 1][1], results[i][1], results[i][2]) for i in results]
                prompt = protocol.check_retry_prompt(answer[1:].strip(), tried)
                break
            if answer:
                return answer  # their own command
