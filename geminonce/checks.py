"""--check: turn a plain-language description of "working" into a test command.

Gemini proposes a few candidate commands (exit 0 = the check holds); the user picks which to try, sees
each one's exit code and output, and chooses one to use as the test (or types their own, or asks for
other suggestions).
"""
from __future__ import annotations

import sys

from . import protocol, safety
from .console import BOLD, DIM, GREEN, YELLOW, ask_user, paint, paint_findings
from .gemini import GeminiTimeout


def preview(out: str, head: int = 6, tail: int = 6) -> str:
    """The first and last lines of some output, with '...' in between."""
    lines = out.rstrip("\n").splitlines()
    if len(lines) <= head + tail + 1:
        return "\n".join(lines)
    return "\n".join(lines[:head] + [f"... ({len(lines) - head - tail} more lines) ..."] + lines[-tail:])


def verdict(code: int) -> tuple[str, str]:
    """What an exit code means for a candidate test, before anything is fixed."""
    if code != 0:
        return f"exit {code}: fails now, so it detects the problem", GREEN
    return "exit 0: passes already, so it won't detect the problem", YELLOW


def choose_test_command(chat, ws, transcript, files: dict[str, str], layout: str, check: str) -> str:
    """Ask Gemini for candidate test commands for `check`, try the ones the user picks, and return the one
    they choose. Without a terminal, the first candidate that fails now is used."""
    prompt, tries = protocol.check_prompt(check, files, layout), 0
    while True:
        transcript.outgoing(prompt)
        try:
            blocks = chat.ask(prompt)
        except GeminiTimeout as e:
            blocks = []
            print(paint(f"  {e}", YELLOW))
        transcript.reply(blocks)
        _, commands, _ = protocol.parse_reply(blocks)
        commands = list(dict.fromkeys(commands))
        if not commands:
            tries += 1
            if tries > 3:
                sys.exit("Gemini didn't propose any test commands; give one with -t instead.")
            prompt = protocol.CHECK_NUDGE
            continue

        print(paint("\n  Candidate test commands:", BOLD))
        for i, cmd in enumerate(commands, 1):
            print(f"  {i}. " + paint(cmd, BOLD))
            if (findings := safety.scan_command(cmd)):
                print(paint_findings(safety.format_findings(findings)))
        interactive = sys.stdin.isatty()
        picks = list(range(1, len(commands) + 1))
        if interactive:
            answer = ask_user(paint("  Which should I try? Numbers like 1 3, or Enter for all: ", YELLOW)).strip()
            chosen = [int(x) for x in answer.replace(",", " ").split() if x.isdigit() and 1 <= int(x) <= len(commands)]
            picks = chosen or picks

        results = {}
        for i in picks:
            cmd = commands[i - 1]
            code, out = ws.run(cmd, sandbox=ws.sandbox, show=False)
            results[i] = (code, out)
            text, color = verdict(code)
            print(paint(f"\n  {i}. $ {cmd}", BOLD))
            print(paint(f"     {text}", color))
            if out.strip():
                print("\n".join("     " + ln for ln in ws.hl.output(preview(out)).splitlines()))

        if not interactive:
            failing = [i for i in picks if results[i][0] != 0]
            if not failing:
                sys.exit("None of the proposed checks fails now, so none would detect the problem; give one with -t.")
            print(paint(f"\n  Using candidate {failing[0]}: the first that fails now (no terminal to ask).", DIM))
            return commands[failing[0] - 1]

        while True:
            answer = ask_user(paint("\n  Use which one as the test? A number, your own command, "
                                    "r <what's wrong> for other suggestions, or q to quit: ", YELLOW)).strip()
            if answer in ("q", "/quit"):
                sys.exit(1)
            if answer.isdigit() and 1 <= int(answer) <= len(commands):
                return commands[int(answer) - 1]
            if answer == "r" or answer.startswith("r "):
                tried = [(commands[i - 1], *results[i]) for i in results]
                prompt = protocol.check_retry_prompt(answer[1:].strip(), tried)
                break
            if answer:
                return answer  # their own command
