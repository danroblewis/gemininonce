"""`gemininonce build`: idea -> SPEC.md -> tests -> implementation, one Gemini conversation per stage.

Each stage may only write its own files (Workspace.guard): the spec writer writes SPEC.md, the test
writer only files under tests/, and the implementer anything *except* those, so it can't make the
tests pass by changing them. The user reviews the spec and the tests before moving on.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import HOME, protocol
from .cli import HelpFormatter, add_session_options, confirm_anonymous, open_chat, profile_dir_for
from .console import BLUE, BOLD, DIM, GREEN, RED, YELLOW, ask_user, paint
from .highlight import Highlighter
from .loop import FixLoop
from .transcript import Transcript
from .workspace import Workspace

STAGES = ("spec", "tests", "code")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gemininonce build", formatter_class=HelpFormatter,
                                 description="Turn an idea into a spec, then tests, then code that passes them.")
    ap.add_argument("idea", nargs="?", default="", help="what to build (not needed with --from tests/code)")
    ap.add_argument("--dir", default=".", help="project directory (created if missing)")
    ap.add_argument("-t", "--test", default="python -m pytest -q", help="test command")
    ap.add_argument("--spec", default="SPEC.md", help="spec file, relative to --dir")
    ap.add_argument("--tests-dir", default="tests", help="where the test writer puts tests, relative to --dir")
    ap.add_argument("--from", dest="start", choices=STAGES, default="spec",
                    help="stage to start at, reusing the files earlier stages wrote")
    ap.add_argument("--accept", action="store_true",
                    help="accept the spec and tests without review (needed without a terminal)")
    add_session_options(ap)
    return ap


def banner(text: str) -> None:
    print(paint(f"\n{'═' * 64}\n  {text}\n{'═' * 64}", BLUE, BOLD))


def review(ws: Workspace, paths: list[str], what: str, auto: bool, hl: Highlighter) -> str | None:
    """Show the files and let the user accept (None), edit them in $EDITOR (None), or ask for changes
    (the feedback text). 'q' quits."""
    for rel in paths:
        text = (ws.root / rel).read_text()
        print(paint(f"\n── {rel} " + "─" * max(4, 58 - len(rel)), YELLOW, BOLD))
        print(hl.code(text.rstrip("\n"), hl.lexer_for(text, rel)))
    if auto:
        print(paint(f"\n  {what} accepted (--accept)", DIM))
        return None
    if not sys.stdin.isatty():
        sys.exit(f"Reviewing the {what} needs a terminal; pass --accept to take it as is.")
    answer = ask_user(paint(f"\n  Enter = accept the {what},  e = edit it yourself,  q = quit,\n"
                            "  or type what Gemini should change:\n  > ", YELLOW)).strip()
    if answer.lower() == "q":
        sys.exit(1)
    if answer.lower() == "e":
        for rel in paths:
            subprocess.call([os.environ.get("EDITOR", "vi"), str(ws.root / rel)])
        return None
    return answer or None


def only(allowed, why: str):
    """A Workspace.guard that permits just the paths `allowed(rel)` says yes to."""
    return lambda rel: None if allowed(rel) else why


def under(directory: str, rel: str) -> bool:
    return rel == directory or rel.startswith(directory.rstrip("/") + "/")


class Build:
    def __init__(self, args, ws: Workspace, chat, transcript: Transcript):
        self.args, self.ws, self.chat, self.transcript = args, ws, chat, transcript
        self.spec = args.spec
        self.tests_dir = args.tests_dir.rstrip("/")
        self.last_test_out = ""

    def fresh_context(self) -> tuple[dict[str, str], str]:
        """Every readable file in the project (as it is now) and the layout listing."""
        files = self.ws.collect([str(self.ws.root)]) if any(self.ws.root.iterdir()) else {}
        return files, self.ws.layout()

    def converse(self, prompt: str, what: str) -> list[str]:
        """Ask for files until the user accepts them. Returns the paths written in this stage."""
        loop = FixLoop(self.chat, self.ws, self.transcript, retries=self.args.retries)
        written_before, attempts = len(self.ws.written), 0
        while True:
            edits, _, _ = loop.ask_for_code(prompt)
            notes = self.ws.apply(edits)
            written = list(dict.fromkeys(self.ws.written[written_before:]))
            problems = self.check(what, written)
            if problems:
                print(paint("  " + "\n  ".join(problems), YELLOW))
                attempts += 1
                if attempts > self.args.patience:
                    sys.exit(f"The {what} still had problems after {attempts} tries; stopping.")
                prompt = protocol.tests_problems_prompt(problems, notes, self.last_test_out) \
                    if what == "tests" else protocol.revise_prompt(what, "\n".join(problems + notes))
                continue
            feedback = review(self.ws, written, what, self.args.accept, self.ws.hl)
            if feedback is None:
                return written
            prompt = protocol.revise_prompt(what, feedback)

    def check(self, what: str, written: list[str]) -> list[str]:
        """Mechanical checks before the user sees a stage's output."""
        if what == "spec":
            if self.spec not in written:
                return [f"No {self.spec} was written. Reply with `FILE: {self.spec}` and the whole spec."]
            text = (self.ws.root / self.spec).read_text()
            return [] if re.search(r"\bR1\b|^\s*1\.", text, re.M) else \
                ["The spec needs a numbered list of testable requirements (R1, R2, ...)."]
        problems = []
        tests = [rel for rel in written if rel.endswith(".py")]
        if not tests:
            return [f"No test files were written under {self.tests_dir}/."]
        for rel in tests:
            try:
                compile((self.ws.root / rel).read_text(), rel, "exec")
            except SyntaxError as e:
                problems.append(f"{rel} has a syntax error: {e}")
        code, out = self.ws.run_test(self.args.test)
        self.last_test_out = out
        if code == 0:
            problems.append("The tests pass before anything is implemented, so they don't test the spec. "
                            "They must exercise the behavior in the spec and fail until it's implemented.")
        if not re.search(r"\bdef test_|\bit\(|\btest\(", "\n".join((self.ws.root / r).read_text() for r in tests)):
            problems.append("No test functions found.")
        return problems

    # --- stages -----------------------------------------------------------------------------------
    def write_spec(self) -> None:
        banner(f"1/3  Spec: Gemini writes {self.spec}")
        self.ws.guard = only(lambda rel: rel == self.spec, f"only {self.spec} may be written while writing the spec")
        files, layout = self.fresh_context()
        prompt = protocol.spec_prompt(self.args.idea, self.spec, self.args.test, files, layout)
        self.converse(prompt, "spec")

    def write_tests(self) -> None:
        banner(f"2/3  Tests: Gemini writes tests in {self.tests_dir}/ from {self.spec}")
        self.chat.new_chat()
        self.ws.guard = only(lambda rel: under(self.tests_dir, rel),
                             f"only files under {self.tests_dir}/ may be written while writing tests")
        spec = (self.ws.root / self.spec).read_text()
        files, layout = self.fresh_context()
        files.pop(self.spec, None)  # it's quoted in the prompt already
        self.converse(protocol.tests_prompt(spec, self.spec, self.args.test, self.tests_dir, files, layout), "tests")

    def write_code(self) -> bool:
        banner(f"3/3  Code: Gemini implements until `{self.args.test}` passes ({self.spec} and tests locked)")
        self.chat.new_chat()
        self.ws.guard = lambda rel: (f"{rel} is part of the agreed spec/tests and is locked; change the "
                                     f"implementation instead") if rel == self.spec or under(self.tests_dir, rel) else None
        files, layout = self.fresh_context()
        code, out = self.ws.run_test(self.args.test)
        if code == 0:
            print(paint("The tests already pass; nothing to implement.", GREEN))
            return True
        message = protocol.implement_message(self.spec, self.tests_dir)
        prompt = protocol.initial_prompt(message, protocol.test_result(self.args.test, code, out), files, layout)
        loop = FixLoop(self.chat, self.ws, self.transcript, self.args.test, self.args.retries, self.args.patience,
                       self.args.max_iters, message)
        return loop.run(prompt, (code, out))

    def run(self) -> bool:
        start = STAGES.index(self.args.start)
        if start <= 0:
            self.write_spec()
        if start <= 1:
            self.write_tests()
        return self.write_code()


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.dir).resolve()
    if args.start == "spec" and not args.idea:
        sys.exit("Say what to build: gemininonce build \"a CLI that ...\"")
    if args.start != "spec" and not (root / args.spec).exists():
        sys.exit(f"--from {args.start} needs an existing {root / args.spec}")
    root.mkdir(parents=True, exist_ok=True)
    HOME.mkdir(parents=True, exist_ok=True)
    ws = Workspace(root, HOME / "backups" / time.strftime("%Y%m%d-%H%M%S"), args.timeout,
                   sandbox=not args.no_sandbox, allow_new_files=True)  # each stage's guard limits what's written
    existing = ws.collect([str(root)]) if any(root.iterdir()) else {}
    ws.hl = Highlighter.for_files(list(existing) or ["x.py"])
    if args.anonymous and not confirm_anonymous(args, existing):
        return 1

    profile_dir = profile_dir_for(args)
    try:
        chat = open_chat(args, profile_dir)
    except BaseException:
        if args.anonymous:
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    chat.usage.price = args.price
    passed = False
    try:
        passed = Build(args, ws, chat, Transcript(args.verbose, ws.hl)).run()
    finally:
        if chat.usage.turns:
            print(paint("\n" + chat.usage.report(chat.model), BOLD))
        chat.browser.close()
        if args.anonymous:
            shutil.rmtree(profile_dir, ignore_errors=True)
    print(paint(f"\n{'✔ Built: tests pass.' if passed else '✘ Not done: tests still fail.'}  ({root})",
                GREEN if passed else RED, BOLD))
    return 0 if passed else 1
