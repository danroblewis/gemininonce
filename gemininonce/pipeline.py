"""`gemininonce build`: idea -> SPEC.md -> tests -> implementation, one Gemini conversation per stage.

Each stage may only write its own files (Workspace.guard): the spec writer writes SPEC.md, the test
writer only files under tests/, and the implementer anything *except* those, so it can't make the
tests pass by changing them. The spec and the tests are each settled in a conversation: the user
answers Gemini's questions, asks their own and requests changes, until they accept.
"""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
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
    ap.add_argument("-t", "--test", default="pytest -q",
                    help="test command, run from --dir; the build is done when it exits 0")
    ap.add_argument("--spec", default="SPEC.md", help="spec file, relative to --dir")
    ap.add_argument("--tests-dir", default="tests", help="where the test writer puts tests, relative to --dir")
    ap.add_argument("--from", dest="start", choices=STAGES, default="spec",
                    help="stage to start at, reusing the files earlier stages wrote")
    ap.add_argument("--spec-model", help="model for writing the spec (default: pro when signed in, since "
                    "thinking helps most here; signed out only flash-lite exists)")
    ap.add_argument("--tests-model", help="model for writing tests (default: same as --model)")
    ap.add_argument("--spec-reviews", type=int, default=1,
                    help="times Gemini re-reads its spec as the test writer would and fixes gaps, before you see it")
    ap.add_argument("--accept", action="store_true",
                    help="accept the spec and tests without discussing them (needed without a terminal)")
    add_session_options(ap)
    return ap


def banner(text: str) -> None:
    print(paint(f"\n{'═' * 64}\n  {text}\n{'═' * 64}", BLUE, BOLD))


def only(allowed, why: str):
    """A Workspace.guard that permits just the paths `allowed(rel)` says yes to."""
    return lambda rel: None if allowed(rel) else why


def under(directory: str, rel: str) -> bool:
    return rel == directory or rel.startswith(directory.rstrip("/") + "/")


def spec_problems(text: str) -> list[str]:
    """What a spec is missing for the test writer to work from it alone."""
    problems = []
    if not re.search(r"^\W*R1\b", text, re.M):
        problems.append("It needs numbered, testable requirements (R1, R2, ...), each with acceptance criteria.")
    interface = re.search(r"^#+[^\n]*Interface[^\n]*\n(.*?)(?=^#{1,2} |\Z)", text, re.S | re.M)
    if not interface:
        problems.append("It needs an Interface section giving the complete public API.")
    elif not re.search(r"```[^\n]*\n.*?\(.*?```", interface.group(1), re.S):
        problems.append("The Interface section must spell out the API as code: every module, class (constructor, "
                        "attributes, methods), function and exception, with full signatures and types.")
    if not re.search(r"^#+[^\n]*Architecture", text, re.M):
        problems.append("It needs an Architecture section: components, responsibilities and the file/module layout.")
    return problems


class Build:
    def __init__(self, args, ws: Workspace, chat, transcript: Transcript):
        self.args, self.ws, self.chat = args, ws, chat
        self.transcript = transcript  # for the code stage
        # Stage files are shown in full (then as diffs) by the review, so the transcript only previews them.
        self.quiet_transcript = Transcript(transcript.verbose, transcript.hl, preview=0, prose_lines=8)
        self.spec = args.spec
        self.tests_dir = args.tests_dir.rstrip("/")
        self.last_test_out = ""

    def fresh_context(self) -> tuple[dict[str, str], str]:
        """Every readable file in the project (as it is now) and the layout listing."""
        files = self.ws.collect([str(self.ws.root)]) if any(self.ws.root.iterdir()) else {}
        return files, self.ws.layout()

    def discuss(self, prompt: str, what: str) -> list[str]:
        """Settle this stage's files in a conversation with Gemini: mechanical problems go straight back
        to Gemini; then the user answers its questions, asks their own or requests changes, until they
        press Enter to accept. Returns the paths written in this stage."""
        loop = FixLoop(self.chat, self.ws, self.quiet_transcript, retries=self.args.retries)
        written_before, attempts, errors, shown = len(self.ws.written), 0, 0, {}
        reviews_left = self.args.spec_reviews if what == "spec" else 0
        if not self.args.accept and not sys.stdin.isatty():
            sys.exit(f"Settling the {what} needs a terminal to talk in; pass --accept to take Gemini's first version.")
        while True:
            blocks = loop._exchange(prompt)
            edits, _, prose = protocol.parse_reply(blocks)
            reads = protocol.file_requests(blocks)
            if reads and not edits:  # it wants to look at project files first
                prompt = protocol.read_reply(*self.ws.read_files(reads))
                continue
            if what == "spec" and not edits:  # the spec is the reply itself (a FILE: block can't hold its code blocks)
                markdown = getattr(self.chat, "last_markdown", "") or protocol.blocks_markdown(blocks)
                if (doc := protocol.spec_document(markdown)):
                    edits = [(self.spec, doc)]
                elif self.quiet_transcript.truncated:  # a long message for the user, not a spec: show all of it
                    print(paint("\n── Gemini's full message " + "─" * 35, YELLOW, BOLD))
                    print(self.ws.hl.code(markdown.rstrip(), self.ws.hl.lexer_for(markdown, "reply.md")))
            if not edits and len(prose.strip()) < 300 and protocol.GEMINI_ERROR.search(prose) \
                    and errors < self.args.retries:  # a Gemini error, not a question for the user
                errors += 1
                print(paint(f"  Gemini replied with an error; resending ({errors}/{self.args.retries})", YELLOW))
                time.sleep(5)
                continue
            notes = self.ws.apply(edits)
            written = list(dict.fromkeys(self.ws.written[written_before:]))
            problems = self.check(what, written) if written else []
            if problems and attempts < self.args.patience:  # fix the mechanical stuff before asking the user
                attempts += 1
                print(paint("  " + "\n  ".join(problems), YELLOW))
                prompt = protocol.tests_problems_prompt(problems, notes, self.last_test_out) if what == "tests" \
                    else protocol.spec_problems_prompt(problems, notes, self.spec)
                continue
            if written and not problems and reviews_left:  # let it catch its own gaps before the user reads it
                reviews_left -= 1
                print(paint(f"  Asking Gemini to re-read {self.spec} as the test writer would...", DIM))
                prompt = protocol.spec_self_review_prompt(self.spec)
                continue
            self.show_changes(written, shown)
            if problems:
                print(paint(f"  Still not right after {attempts} tries:\n  " + "\n  ".join(problems), YELLOW))
            if self.args.accept:
                if written and not problems:
                    print(paint(f"  {what} accepted (--accept)", DIM))
                    return written
                if attempts >= self.args.patience:
                    sys.exit(f"The {what} still had problems after {attempts} tries; stopping.")
                attempts += 1
                prompt = protocol.discussion_prompt(what, written, "Don't ask questions: make reasonable decisions, "
                                                    "note them in the file, and write it now.", what == "spec")
                continue
            answer = self.ask_user_about(what, written, problems)
            if answer is None:
                return written
            attempts = 0
            prompt = protocol.discussion_prompt(what, written, answer, what == "spec")

    def ask_user_about(self, what: str, written: list[str], problems: list[str]) -> str | None:
        """The user's reply for Gemini, or None once they accept."""
        while True:
            answer = ask_user(paint(
                f"\n  Reply to Gemini: answer its questions, ask your own, or say what to change.\n"
                f"  /show prints the whole {what}, /quit stops, Enter accepts it.\n  > ", YELLOW)).strip()
            if answer == "/quit":
                sys.exit(1)
            if answer == "/show":
                for rel in written:
                    self.print_file(rel)
                continue
            if answer:
                return answer
            if written and not problems:
                return None
            print(paint(f"  There's no finished {what} yet; tell Gemini what you want.", YELLOW))

    def print_file(self, rel: str) -> None:
        text = (self.ws.root / rel).read_text()
        print(paint(f"\n── {rel} " + "─" * max(4, 58 - len(rel)), YELLOW, BOLD))
        print(self.ws.hl.code(text.rstrip("\n"), self.ws.hl.lexer_for(text, rel)))

    def show_changes(self, written: list[str], shown: dict[str, str]) -> None:
        """First time: each file in full. After that: just what changed, as a diff."""
        for rel in written:
            text = (self.ws.root / rel).read_text()
            if rel not in shown:
                self.print_file(rel)
            elif text != shown[rel]:
                diff = "\n".join(difflib.unified_diff(shown[rel].splitlines(), text.splitlines(),
                                                      f"{rel} (before)", f"{rel} (now)", lineterm="", n=1))
                print(paint(f"\n── changes to {rel} " + "─" * max(4, 47 - len(rel)), YELLOW, BOLD))
                print(self.ws.hl.code(diff, self.ws.hl.lexer_for(diff)))
            shown[rel] = text

    def check(self, what: str, written: list[str]) -> list[str]:
        """Mechanical checks before the user sees a stage's output."""
        if what == "spec":
            if self.spec not in written:
                return [f"No {self.spec} was written. Reply with `FILE: {self.spec}` and the whole spec."]
            return spec_problems((self.ws.root / self.spec).read_text())
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
    def use_model(self, wanted: str | None, fallback_ok: bool = False) -> None:
        """Switch the conversation's model for this stage ('any' or None leaves it alone)."""
        if not wanted or wanted.lower() == "any":
            return
        try:
            self.chat.select_model(wanted)
        except SystemExit as e:
            if not fallback_ok:
                raise
            print(paint(f"  {e}\n  Continuing with {self.chat.model or self.chat.current_model()}.", YELLOW))

    def code_model(self) -> str:
        return self.args.model or ("flash-lite" if self.args.anonymous else "flash")

    def write_spec(self) -> None:
        banner(f"1/3  Spec: Gemini writes {self.spec}")
        if self.args.spec_model:
            self.use_model(self.args.spec_model)
        elif self.args.anonymous:
            print(paint("  Signed out, only Flash-Lite is available. For a stronger spec writer (Pro), run "
                        "signed in, e.g. with --chrome-profile.", YELLOW))
        else:
            self.use_model("pro", fallback_ok=True)
        self.ws.guard = only(lambda rel: rel == self.spec, f"only {self.spec} may be written while writing the spec")
        files, layout = self.fresh_context()
        prompt = protocol.spec_prompt(self.args.idea, self.spec, self.args.test, files, layout,
                                      may_ask=not self.args.accept)
        self.discuss(prompt, "spec")

    def write_tests(self) -> None:
        banner(f"2/3  Tests: Gemini writes tests in {self.tests_dir}/ from {self.spec}")
        self.chat.new_chat()
        self.use_model(self.args.tests_model or self.code_model())
        self.ws.guard = only(lambda rel: under(self.tests_dir, rel),
                             f"only files under {self.tests_dir}/ may be written while writing tests")
        spec = (self.ws.root / self.spec).read_text()
        files, layout = self.fresh_context()
        files.pop(self.spec, None)  # it's quoted in the prompt already
        self.discuss(protocol.tests_prompt(spec, self.spec, self.args.test, self.tests_dir, files, layout), "tests")

    def write_code(self) -> bool:
        banner(f"3/3  Code: Gemini implements until `{self.args.test}` passes ({self.spec} and tests locked)")
        self.chat.new_chat()
        self.use_model(self.code_model())
        self.ws.guard = lambda rel: (f"{rel} is part of the agreed spec/tests and is locked; change the "
                                     f"implementation instead") if rel == self.spec or under(self.tests_dir, rel) else None
        files, layout = self.fresh_context()
        code, out = self.ws.run_test(self.args.test)
        if code == 0:
            print(paint("The tests already pass; nothing to implement.", GREEN))
            return True
        message = protocol.implement_message(self.spec, self.tests_dir, self.args.test)
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
    print(paint(f"\n{'✔ Built: tests pass' if passed else '✘ Not done: tests still fail'} "
                f"with `{args.test}` in {root}", GREEN if passed else RED, BOLD))
    return 0 if passed else 1
