"""`gemininonce build`: idea -> SPEC.md -> test plan -> tests -> implementation.

Each stage runs in its own Gemini conversation and may only write its own files (Workspace.guard): the
spec writer writes SPEC.md, the planner tests/TEST_PLAN.md, the test writer tests/unit/ and tests/e2e/,
and the implementer anything *except* those, so it can't make the tests pass by changing them. The spec,
the plan and the tests are each checked mechanically, reviewed by a fresh Gemini session that sees only
what it needs, and settled in a conversation with the user (answer Gemini's questions, ask your own,
request changes) until they accept.
"""
from __future__ import annotations

import argparse
import difflib
from dataclasses import dataclass
import re
import shutil
import sys
import time
from pathlib import Path

from . import HOME, protocol
from .cli import HelpFormatter, add_session_options, confirm_anonymous, open_chat, profile_dir_for
from .console import BLUE, BOLD, DIM, GREEN, RED, YELLOW, ask_user, paint
from .gemini import GeminiChat, GeminiTimeout
from .highlight import Highlighter
from .loop import FixLoop
from .transcript import Transcript
from .workspace import Workspace

STAGES = ("spec", "plan", "tests", "code")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gemininonce build", formatter_class=HelpFormatter,
                                 description="Turn an idea into a spec, then tests, then code that passes them.")
    ap.add_argument("idea", nargs="?", default="", help="what to build (not needed with --from tests/code)")
    ap.add_argument("--dir", default=".", help="project directory (created if missing)")
    ap.add_argument("-t", "--test", default="pytest -q",
                    help="test command, run from --dir; the build is done when it exits 0")
    ap.add_argument("--spec", default="SPEC.md", help="spec file, relative to --dir")
    ap.add_argument("--tests-dir", default="tests",
                    help="where tests go, relative to --dir (unit tests in unit/, end-to-end tests in e2e/)")
    ap.add_argument("--plan", default=None, help="test plan file (default: TEST_PLAN.md in --tests-dir)")
    ap.add_argument("--tests-per-requirement", type=int, default=3,
                    help="minimum test cases per numbered requirement, in the plan and in the tests")
    ap.add_argument("--from", dest="start", choices=STAGES, default="spec",
                    help="stage to start at, reusing the files earlier stages wrote")
    ap.add_argument("--spec-model", help="model for writing the spec (default: pro when signed in, since "
                    "thinking helps most here; signed out only flash-lite exists)")
    ap.add_argument("--plan-model", help="model for the test plan (default: like --spec-model)")
    ap.add_argument("--tests-model", help="model for writing tests (default: same as --model)")
    ap.add_argument("--spec-reviews", type=int, default=1,
                    help="independent reviews of the spec before you see it: a fresh Gemini session that sees only "
                         "the spec (as the test writer will) lists gaps, and the spec writer fixes them")
    ap.add_argument("--plan-reviews", type=int, default=1,
                    help="independent reviews of the test plan against the spec and the testing guide")
    ap.add_argument("--tests-reviews", type=int, default=1,
                    help="independent reviews of the tests: a fresh session compares them with the spec "
                         "(coverage, exact interface, correct expectations, no over-specifying)")
    ap.add_argument("--accept", action="store_true",
                    help="accept the spec, plan and tests without discussing them (needed without a terminal)")
    add_session_options(ap)
    return ap


def banner(text: str) -> None:
    print(paint(f"\n{'═' * 64}\n  {text}\n{'═' * 64}", BLUE, BOLD))


def only(allowed, why: str):
    """A Workspace.guard that permits just the paths `allowed(rel)` says yes to."""
    return lambda rel: None if allowed(rel) else why


def under(directory: str, rel: str) -> bool:
    return rel == directory or rel.startswith(directory.rstrip("/") + "/")


def requirement_ids(spec: str) -> list[int]:
    """The numbers of the spec's requirements (R1, R2, ...)."""
    return sorted({int(n) for n in re.findall(r"(?<![A-Za-z0-9])R(\d+)\b", spec)})


def mentions_requirement(text: str, n: int) -> bool:
    """R3 / r3 / test_r3_... but not R30 or HR3."""
    return re.search(rf"(?i)(?<![a-z0-9])r{n}(?![0-9])", text) is not None


def plan_problems(plan: str, spec: str, per_requirement: int) -> list[str]:
    """What a test plan is missing: both kinds of tests, every requirement, enough cases."""
    titles = " ".join(title.lower() for _, _, title in protocol.headings(plan.splitlines()))
    problems = []
    if "unit" not in titles:
        problems.append("It needs a Unit tests section (tests with external connections mocked).")
    if "e2e" not in titles and "end-to-end" not in titles and "end to end" not in titles:
        problems.append("It needs an End-to-end tests section (real entry points and services, no mocks).")
    reqs = requirement_ids(spec)
    missing = [f"R{n}" for n in reqs if not mentions_requirement(plan, n)]
    if missing:
        problems.append(f"No test cases for {', '.join(missing)}; every requirement needs its own cases.")
    cases = set(re.findall(r"\btest_\w+", plan))
    if reqs and len(cases) < per_requirement * len(reqs):
        problems.append(f"Only {len(cases)} named test cases for {len(reqs)} requirements; plan at least "
                        f"{per_requirement} per requirement (normal case, boundaries, errors, examples), named test_...")
    return problems


def spec_problems(text: str) -> list[str]:
    """What a spec is missing for the test writer to work from it alone."""
    lines = text.splitlines()
    found = protocol.headings(lines)

    def section(word: str) -> str | None:
        """Body of the first heading containing `word`, up to the next heading at the same or a higher level."""
        for n, (i, level, title) in enumerate(found):
            if word.lower() in title.lower():
                end = next((j for j, lv, _ in found[n + 1:] if lv <= level), len(lines))
                return "\n".join(lines[i + 1:end])
        return None

    problems = []
    if not re.search(r"^\W*R1\b", text, re.M):
        problems.append("It needs numbered, testable requirements (R1, R2, ...), each with acceptance criteria.")
    interface = section("Interface")
    if interface is None:
        problems.append("It needs an Interface section giving the complete public API.")
    elif not re.search(r"```[^\n]*\n.*?\(.*?```", interface, re.S):
        problems.append("The Interface section must spell out the API as code: every module, class (constructor, "
                        "attributes, methods), function and exception, with full signatures and types.")
    if section("Architecture") is None:
        problems.append("It needs an Architecture section: components, responsibilities and the file/module layout.")
    return problems


@dataclass
class Stage:
    name: str             # "spec", "plan" or "tests"
    kind: str             # how it's called in messages
    path: str | None      # the document, for stages whose reply IS the document; None for file stages
    reviews: int          # independent reviews before the user sees it


class Build:
    def __init__(self, args, ws: Workspace, chat, transcript: Transcript, reviewer_factory=None):
        self.args, self.ws, self.chat = args, ws, chat
        # Reviews run in a separate tab, a fresh conversation each time, so the writer's own conversation
        # (and the user's discussion with it) is untouched and the reviewer knows only what's in the files.
        self.reviewer_factory = reviewer_factory or self._open_reviewer
        self.reviewer = None
        self.transcript = transcript  # for the code stage
        # Stage files are shown in full (then as diffs) by the review, so the transcript only previews them.
        self.quiet_transcript = Transcript(transcript.verbose, transcript.hl, preview=0, prose_lines=8)
        self.spec = args.spec
        self.tests_dir = args.tests_dir.rstrip("/")
        self.plan = args.plan or f"{self.tests_dir}/TEST_PLAN.md"
        self.last_test_out = ""
        self.stages = {
            "spec": Stage("spec", "specification", self.spec, args.spec_reviews),
            "plan": Stage("plan", "test plan", self.plan, args.plan_reviews),
            "tests": Stage("tests", "tests", None, args.tests_reviews),
        }

    def read(self, rel: str) -> str:
        return (self.ws.root / rel).read_text()

    def fresh_context(self) -> tuple[dict[str, str], str]:
        """Every readable file in the project (as it is now) and the layout listing."""
        files = self.ws.collect([str(self.ws.root)]) if any(self.ws.root.iterdir()) else {}
        return files, self.ws.layout()

    # --- settling a stage: checks, independent reviews, then the user ----------------------------------
    def discuss(self, prompt: str, stage: Stage) -> list[str]:
        """Settle a stage's files with Gemini: mechanical problems go straight back to it; then a fresh
        reviewer session looks for gaps; then the user answers its questions, asks their own or requests
        changes, until they press Enter to accept. Returns the paths written in this stage."""
        loop = FixLoop(self.chat, self.ws, self.quiet_transcript, retries=self.args.retries)
        written_before, attempts, errors, shown = len(self.ws.written), 0, 0, {}
        reviews_left = stage.reviews
        document = stage.kind if stage.path else None
        if not self.args.accept and not sys.stdin.isatty():
            sys.exit(f"Settling the {stage.kind} needs a terminal to talk in; pass --accept to take Gemini's "
                     "first version.")
        while True:
            blocks = loop._exchange(prompt)
            edits, _, prose = protocol.parse_reply(blocks)
            reads = protocol.file_requests(blocks)
            if reads and not edits:  # it wants to look at project files first
                prompt = protocol.read_reply(*self.ws.read_files(reads))
                continue
            if stage.path and not edits:  # the document is the reply itself (a FILE: block can't hold its code blocks)
                markdown = getattr(self.chat, "last_markdown", "") or protocol.blocks_markdown(blocks)
                finder = protocol.spec_document if stage.name == "spec" else protocol.plan_document
                if (doc := finder(markdown)):
                    edits = [(stage.path, doc)]
                elif self.quiet_transcript.truncated:  # a long message for the user, not the document: show it all
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
            problems = self.check(stage, written) if written else []
            if problems and attempts < self.args.patience:  # fix the mechanical stuff before asking anyone
                attempts += 1
                print(paint("  " + "\n  ".join(problems), YELLOW))
                prompt = protocol.doc_problems_prompt(stage.kind, problems, notes) if document else \
                    protocol.tests_problems_prompt(problems, notes, self.last_test_out)
                continue
            if written and not problems and reviews_left:  # an independent review before the user reads it
                reviews_left -= 1
                if (issues := self.review(stage, written)):
                    prompt = protocol.doc_review_feedback_prompt(stage.kind, issues) if document else \
                        protocol.tests_review_feedback_prompt(issues)
                    continue
                reviews_left = 0  # nothing found: no need for more rounds
            self.show_changes(written, shown)
            if problems:
                print(paint(f"  Still not right after {attempts} tries:\n  " + "\n  ".join(problems), YELLOW))
            if self.args.accept:
                if written and not problems:
                    print(paint(f"  {stage.kind} accepted (--accept)", DIM))
                    return written
                if attempts >= self.args.patience:
                    sys.exit(f"The {stage.kind} still had problems after {attempts} tries; stopping.")
                attempts += 1
                prompt = protocol.discussion_prompt(stage.kind, written, "Don't ask questions: make reasonable "
                                                    "decisions, note them, and write it now.", document)
                continue
            answer = self.ask_user_about(stage.kind, written, problems)
            if answer is None:
                return written
            attempts = 0
            prompt = protocol.discussion_prompt(stage.kind, written, answer, document)

    def check(self, stage: Stage, written: list[str]) -> list[str]:
        """Mechanical checks before a reviewer or the user sees a stage's output."""
        if stage.path and stage.path not in written:
            return [f"No {stage.kind} was written. Reply with the whole {stage.kind}, starting with its `# ` title."]
        if stage.name == "spec":
            return spec_problems(self.read(self.spec))
        if stage.name == "plan":
            return plan_problems(self.read(self.plan), self.read(self.spec), self.args.tests_per_requirement)
        return self.tests_problems(written)

    def tests_problems(self, written: list[str]) -> list[str]:
        tests = [rel for rel in written if rel.endswith(".py")]
        if not tests:
            return [f"No test files were written under {self.tests_dir}/."]
        problems = []
        for rel in tests:
            try:
                compile(self.read(rel), rel, "exec")
            except SyntaxError as e:
                problems.append(f"{rel} has a syntax error: {e}")
        all_tests = [str(p.relative_to(self.ws.root)) for p in sorted((self.ws.root / self.tests_dir).rglob("*.py"))]
        source = "\n".join(self.read(rel) for rel in all_tests)
        for kind in ("unit", "e2e"):
            if not any(under(f"{self.tests_dir}/{kind}", rel) and "def test_" in self.read(rel) for rel in all_tests):
                problems.append(f"There are no tests in {self.tests_dir}/{kind}/; the suite needs both unit tests "
                                "(external connections mocked) and e2e tests (real services, no mocks).")
        reqs = requirement_ids(self.read(self.spec))
        missing = [f"R{n}" for n in reqs if not mentions_requirement(source, n)]
        if missing:
            problems.append(f"No tests mention {', '.join(missing)}; name tests after their requirement "
                            "(test_r3_...).")
        count = len(re.findall(r"\bdef test_\w+", source))
        if reqs and count < self.args.tests_per_requirement * len(reqs):
            problems.append(f"Only {count} test functions for {len(reqs)} requirements; the plan calls for at least "
                            f"{self.args.tests_per_requirement} per requirement. Write every test case in the plan.")
        if not count:
            problems.append("No test functions found.")
        code, out = self.ws.run_test(self.args.test)
        self.last_test_out = out
        if code == 0:
            problems.append("The tests pass before anything is implemented, so they don't test the spec. "
                            "They must exercise the behavior in the spec and fail until it's implemented.")
        return problems

    def _open_reviewer(self):
        return GeminiChat(self.chat.browser, self.args.account, None, self.args.anonymous,
                          page=self.chat.browser.new_tab(), usage=self.chat.usage)

    def review(self, stage: Stage, written: list[str]) -> str | None:
        """Have a fresh Gemini session review the stage's output; returns its issues, or None if it found
        nothing (or couldn't answer)."""
        if self.reviewer is None:
            self.reviewer = self.reviewer_factory()
        else:
            self.reviewer.new_chat()
        if self.chat.model:
            self.use_model(self.chat.model, fallback_ok=True, chat=self.reviewer)  # same model as the writer
        spec = self.read(self.spec)
        if stage.name == "spec":
            what = f"reads only {self.spec}, as the test writer will"
            msg = protocol.spec_review_prompt(spec, self.spec)
        elif stage.name == "plan":
            what = f"checks the test plan against {self.spec} and the testing guide"
            msg = protocol.plan_review_prompt(spec, self.spec, self.read(self.plan), self.args.tests_per_requirement,
                                              self.tests_dir)
        else:
            what = f"checks the tests against {self.spec} and the test plan"
            tests = {rel: self.read(rel) for rel in written if under(self.tests_dir, rel) and rel != self.plan}
            msg = protocol.tests_review_prompt(spec, self.spec, self.read(self.plan), self.plan, tests,
                                               self.args.test, self.tests_dir)
        print(paint(f"\n  ▶ Independent review: a fresh Gemini session {what}", BLUE, BOLD))
        self.quiet_transcript.outgoing(msg)
        try:
            blocks = self.reviewer.ask(msg)
        except GeminiTimeout as e:
            print(paint(f"  {e}; skipping this review", YELLOW))
            return None
        Transcript(self.transcript.verbose, self.ws.hl).reply(blocks)
        usage = self.reviewer.usage
        print(paint(f"  $ {usage.step(self.reviewer.model, len(usage.turns) - 1, 'review')}", GREEN))
        found = getattr(self.reviewer, "last_markdown", "") or protocol.blocks_markdown(blocks)
        if protocol.reviewer_found_nothing(found):
            print(paint("  Reviewer found no issues.", GREEN))
            return None
        return found.strip()

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
        text = self.read(rel)
        print(paint(f"\n── {rel} " + "─" * max(4, 58 - len(rel)), YELLOW, BOLD))
        print(self.ws.hl.code(text.rstrip("\n"), self.ws.hl.lexer_for(text, rel)))

    def show_changes(self, written: list[str], shown: dict[str, str]) -> None:
        """First time: each file in full. After that: just what changed, as a diff."""
        for rel in written:
            text = self.read(rel)
            if rel not in shown:
                self.print_file(rel)
            elif text != shown[rel]:
                diff = "\n".join(difflib.unified_diff(shown[rel].splitlines(), text.splitlines(),
                                                      f"{rel} (before)", f"{rel} (now)", lineterm="", n=1))
                print(paint(f"\n── changes to {rel} " + "─" * max(4, 47 - len(rel)), YELLOW, BOLD))
                print(self.ws.hl.code(diff, self.ws.hl.lexer_for(diff)))
            shown[rel] = text

    # --- models -----------------------------------------------------------------------------------
    def use_model(self, wanted: str | None, fallback_ok: bool = False, chat=None) -> None:
        """Switch a conversation's model (the writer's by default); 'any' or None leaves it alone."""
        chat = chat or self.chat
        if not wanted or wanted.lower() == "any":
            return
        try:
            chat.select_model(wanted)
        except SystemExit as e:
            if not fallback_ok:
                raise
            print(paint(f"  {e}\n  Continuing with {chat.model or chat.current_model()}.", YELLOW))

    def use_thinking_model(self, explicit: str | None) -> None:
        """For the stages that need the most thought (spec, plan): Pro when signed in."""
        if explicit:
            self.use_model(explicit)
        elif self.args.anonymous:
            print(paint("  Signed out, only Flash-Lite is available. For a stronger model (Pro) here, run "
                        "signed in, e.g. with --chrome-profile.", YELLOW))
        else:
            self.use_model("pro", fallback_ok=True)

    def code_model(self) -> str:
        return self.args.model or ("flash-lite" if self.args.anonymous else "flash")

    # --- stages -----------------------------------------------------------------------------------
    def write_spec(self) -> None:
        banner(f"1/4  Spec: Gemini writes {self.spec}")
        self.use_thinking_model(self.args.spec_model)
        self.ws.guard = only(lambda rel: rel == self.spec, f"only {self.spec} may be written while writing the spec")
        files, layout = self.fresh_context()
        prompt = protocol.spec_prompt(self.args.idea, self.spec, self.args.test, files, layout,
                                      may_ask=not self.args.accept)
        self.discuss(prompt, self.stages["spec"])

    def write_plan(self) -> None:
        banner(f"2/4  Test plan: Gemini plans the test suite in {self.plan}")
        self.chat.new_chat()
        self.use_thinking_model(self.args.plan_model or self.args.spec_model)
        self.ws.guard = only(lambda rel: rel == self.plan, f"only {self.plan} may be written while planning tests")
        files, layout = self.fresh_context()
        files.pop(self.spec, None)  # quoted in the prompt already
        prompt = protocol.plan_prompt(self.read(self.spec), self.spec, self.args.test, self.tests_dir,
                                      self.args.tests_per_requirement, files, layout, may_ask=not self.args.accept)
        self.discuss(prompt, self.stages["plan"])

    def write_tests(self) -> None:
        banner(f"3/4  Tests: Gemini writes {self.tests_dir}/unit/ and {self.tests_dir}/e2e/ from the plan")
        self.chat.new_chat()
        self.use_model(self.args.tests_model or self.code_model())
        self.ws.guard = only(lambda rel: under(self.tests_dir, rel) and rel != self.plan,
                             f"only test files under {self.tests_dir}/ may be written now (the plan is agreed)")
        files, layout = self.fresh_context()
        files.pop(self.spec, None)
        files.pop(self.plan, None)  # both quoted in the prompt already
        self.discuss(protocol.tests_prompt(self.read(self.spec), self.spec, self.read(self.plan), self.plan,
                                           self.args.test, self.tests_dir, files, layout), self.stages["tests"])

    def write_code(self) -> bool:
        banner(f"4/4  Code: Gemini implements until `{self.args.test}` passes ({self.spec} and tests locked)")
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
        for i, step in enumerate((self.write_spec, self.write_plan, self.write_tests)):
            if start <= i:
                step()
        return self.write_code()


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.dir).resolve()
    if args.start == "spec" and not args.idea:
        sys.exit("Say what to build: gemininonce build \"a CLI that ...\"")
    if args.start != "spec" and not (root / args.spec).exists():
        sys.exit(f"--from {args.start} needs an existing {root / args.spec}")
    plan = args.plan or f"{args.tests_dir.rstrip('/')}/TEST_PLAN.md"
    if args.start == "tests" and not (root / plan).exists():
        sys.exit(f"--from {args.start} needs an existing {root / plan} (or start with --from plan)")
    root.mkdir(parents=True, exist_ok=True)
    HOME.mkdir(parents=True, exist_ok=True)
    ws = Workspace(root, HOME / "backups" / time.strftime("%Y%m%d-%H%M%S"), args.timeout,
                   sandbox=not args.no_sandbox, allow_new_files=True,  # each stage's guard limits what's written
                   network=args.network is not False)  # e2e tests hit real services
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
