"""Command line: options, setup checks, and wiring the pieces together."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import HOME, chrome_profiles, env, protocol, safety
from .browser import Browser
from .checks import choose_test_command
from .console import BOLD, DIM, GREEN, YELLOW, ask_user, paint
from .gemini import GeminiChat
from .highlight import Highlighter
from .loop import FixLoop
from .transcript import Transcript
from .workspace import Workspace

ENV_VARS = ("GEMINONCE_ACCOUNT", "GEMINONCE_HOME", "GEMINONCE_CHROME_PROFILE", "GEMINONCE_MODEL",
            "GEMINONCE_PRICE")


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show '(default: X)' only when there's a meaningful default."""

    def _get_help_string(self, action):
        if action.default in (None, False, "", argparse.SUPPRESS) or "default" in (action.help or ""):
            return action.help
        return super()._get_help_string(action)


def add_session_options(ap: argparse.ArgumentParser) -> None:
    """Options shared by every command: the loop's limits, and how we reach Gemini."""
    ap.add_argument("-n", "--max-iters", type=int, default=20, help="hard cap on rounds")
    ap.add_argument("--price", default=env("PRICE"), metavar="IN,OUT",
                    help="API price in USD per 1M input,output tokens for the cost estimate "
                         "(built-in table used if omitted)")
    ap.add_argument("--retries", type=int, default=3, help="re-asks when Gemini replies without code")
    ap.add_argument("--patience", type=int, default=3, help="stop after N rounds with unchanged test output")
    ap.add_argument("--timeout", type=int, default=300, help="timeout for test/commands (seconds)")
    ap.add_argument("--profile", nargs="?", const=str(HOME / "profile"), metavar="DIR",
                    help="sign in with geminonce's own browser profile, where you log in once and it's remembered "
                         f"(default dir: {HOME / 'profile'})")
    ap.add_argument("--account", default=env("ACCOUNT"),
                    help="refuse to send unless the Gemini account contains this, e.g. @corp.com")
    ap.add_argument("--chrome-profile", default=env("CHROME_PROFILE"),
                    help="use a copy of your own Chrome profile (dir, name or email, e.g. 'Profile 2'); "
                         "'list' shows them")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="run the test command unsandboxed (allows network and writes outside the project)")
    ap.add_argument("--network", action=argparse.BooleanOptionalAction, default=None,
                    help="let the sandboxed test command use the internet (default: on for `build`, whose "
                         "e2e tests hit real services; off otherwise)")
    ap.add_argument("--model", default=env("MODEL"),
                    help="Gemini model to force: flash, pro, flash-lite, ...; 'any' leaves it alone "
                         "(default: flash, or flash-lite with --anonymous)")
    ap.add_argument("--anonymous", action="store_true",
                    help="signed-out Gemini (free tier) in a throwaway profile; treat what's sent as public. This is "
                         "the default unless --chrome-profile, --account, --profile or --cdp is given")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the one-time anonymous-mode confirmation")
    ap.add_argument("--show", action="store_true", help="show the browser window (hidden by default)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print full code and outputs in the transcript")
    ap.add_argument("--cdp", help="attach to an already-running Chrome, e.g. http://127.0.0.1:9222")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="geminonce", description="Fix code with Gemini web in a test loop. "
                                 "(Also: `geminonce build IDEA` writes a spec, tests, then the code.)",
                                 formatter_class=HelpFormatter)
    ap.add_argument("paths", nargs="+", help="files and/or directories to send to Gemini")
    ap.add_argument("-t", "--test", help="command that must pass (exit 0), e.g. 'pytest -x'")
    ap.add_argument("--check", metavar="TEXT",
                    help="describe in words how to tell it works (e.g. 'the output contains asdf'); Gemini "
                         "proposes test commands, you try them and pick one")
    ap.add_argument("-m", "--message", default="", help="what you want done (optional if --test fails)")
    ap.add_argument("--root", default=".", help="project root; paths in replies are relative to it")
    ap.add_argument("--allow-new-files", action="store_true",
                    help="let Gemini create files without asking (they're often hallucinated paths)")
    add_session_options(ap)
    return ap


def warn_unknown_env_vars() -> None:
    for var in os.environ:
        if var.startswith(("GEMINONCE_", "GEMININONCE_")) and var.replace("GEMININONCE_", "GEMINONCE_") not in ENV_VARS:
            print(f"warning: unknown environment variable {var} (did you mean GEMINONCE_ACCOUNT?)")


ANONYMOUS_OK = HOME / "anonymous-ok"  # created once the user has confirmed anonymous mode


def choose_session(args) -> None:
    """Anonymous (signed-out, free tier) unless an account is asked for in some way: --chrome-profile or
    --account (or their environment variables), --profile, or --cdp."""
    if args.anonymous:
        explicit = [f for f in ("--chrome-profile", "--account", "--cdp", "--profile")
                    if any(a == f or a.startswith(f + "=") for a in sys.argv[1:])]
        if explicit:
            sys.exit(f"--anonymous can't be combined with {', '.join(explicit)}")
        for var in ("GEMINONCE_CHROME_PROFILE", "GEMINONCE_ACCOUNT", "GEMININONCE_CHROME_PROFILE",
                    "GEMININONCE_ACCOUNT"):
            if os.environ.get(var):
                print(paint(f"--anonymous: ignoring {var}", DIM))
        args.chrome_profile = args.account = None
    elif not (args.chrome_profile or args.account or args.profile or args.cdp):
        args.anonymous = True
    if not args.anonymous and not args.profile:
        args.profile = str(HOME / ("chrome" if args.chrome_profile else "profile"))


def confirm_anonymous(args, files: dict[str, str]) -> bool:
    """Anonymous mode: the full warning and a typed 'yes' the first time (remembered), a one-line notice
    after that. False means abort."""
    if ANONYMOUS_OK.exists():
        print(paint("Signed-out Gemini (free tier): treat what's sent as public. To use your account: "
                    "--chrome-profile NAME (see --chrome-profile list x), or --profile.", YELLOW))
        return True
    print(paint("\n⚠  ANONYMOUS MODE: signed-out Gemini, free tier", YELLOW, BOLD))
    print(paint("   Fresh throwaway browser profile: no Google account, no cookies, nothing copied\n"
                "   from Chrome; deleted afterwards. Free-tier chats may be kept by Google, used to\n"
                "   improve its products and read by human reviewers. Treat everything sent as PUBLIC:\n"
                "   the task, test output, the list of file names in the project, and these\n"
                f"   {len(files)} file(s) (Gemini may ask for others; you'll be asked before any is sent):",
                YELLOW))
    print("\n".join(paint(f"     {rel}", YELLOW) for rel in files))
    print(paint("   This is the default when no account is given. To use your account instead:\n"
                "   --chrome-profile NAME (your Chrome login; see --chrome-profile list x) or --profile.\n"
                "   You'll only be asked this once.", YELLOW))
    if args.yes:  # skipped for this run (e.g. a script), but not remembered: the user hasn't seen it yet
        return True
    if ask_user(paint("   Type 'yes' to continue: ", YELLOW, BOLD)).strip().lower() != "yes":
        print("Aborted; nothing was sent.")
        return False
    try:
        ANONYMOUS_OK.write_text("confirmed\n")
    except OSError:
        pass
    return True


def open_chat(args, profile_dir: Path) -> GeminiChat:
    """Start the browser and connect to Gemini (closing the browser again if that fails)."""
    chrome_profile = None
    if args.chrome_profile and not args.anonymous:
        chrome_profile = chrome_profiles.resolve(args.chrome_profile)
    model = args.model or ("flash-lite" if args.anonymous else "flash")
    browser = Browser(profile_dir, chrome_profile, headless=not args.show, cdp=args.cdp)
    try:
        return GeminiChat(browser, args.account, None if model.lower() == "any" else model, args.anonymous)
    except BaseException:
        browser.close()  # don't leave Chrome running (or writing into a profile we're about to delete)
        raise


def profile_dir_for(args) -> Path:
    if args.anonymous:
        return Path(tempfile.mkdtemp(prefix="geminonce-anon-"))
    return Path(args.profile)


def main() -> int:
    if sys.argv[1:2] == ["build"]:
        from .pipeline import main as build_main
        return build_main(sys.argv[2:])
    args = build_parser().parse_args()
    choose_session(args)
    if args.chrome_profile == "list":
        for d, label in chrome_profiles.profiles().items():
            print(f"  {d!r}: {label}")
        return 0

    warn_unknown_env_vars()
    root = Path(args.root).resolve()
    HOME.mkdir(parents=True, exist_ok=True)
    backup_dir = HOME / "backups" / time.strftime("%Y%m%d-%H%M%S")
    ws = Workspace(root, backup_dir, args.timeout, sandbox=not args.no_sandbox,
                   allow_new_files=args.allow_new_files, network=bool(args.network))

    files = ws.collect(args.paths)
    if not files:
        if not all(Path(p).is_dir() for p in args.paths):
            sys.exit("No readable files to send.")
        # Only (empty) directories: a new project. Creating files is the point, so don't ask for each one.
        ws.allow_new_files = True
        print(paint("Empty project: Gemini will create the files (new files are allowed without asking).", DIM))
    ws.hl = Highlighter.for_files(files)
    size = sum(map(len, files.values()))
    if files:
        print(f"Sending {len(files)} file(s), {size:,} chars.")
    if size > 400_000 and ask_user("That's a lot for a chat message. Continue? [y/N] ").lower() != "y":
        return 1
    if args.anonymous and not confirm_anonymous(args, files):
        return 1

    if args.check and args.test:
        sys.exit("Give either -t (a command) or --check (a description), not both.")
    if not (args.test or args.check or args.message):
        sys.exit("Give --test, --check and/or --message.")
    if ws.sandbox and (args.test or args.check) and not safety.sandbox_argv("true", root):
        print("warning: no sandbox available here (macOS sandbox-exec / Linux bwrap); "
              "the test command will run Gemini's code unconfined.")

    profile_dir, chat, passed = profile_dir_for(args), None, False
    transcript = Transcript(args.verbose, ws.hl)
    try:
        if args.check:  # turn the description into a test command first
            chat = open_chat(args, profile_dir)
            chat.usage.price = args.price
            args.test = choose_test_command(chat, ws, transcript, files, ws.layout(), args.check)
            print(paint(f"\n  Test command: {args.test}", GREEN, BOLD))
            chat.new_chat()
        test_out, first = "", None
        if args.test:
            first = code, out = ws.run_test(args.test)
            if code == 0 and not args.message:
                print("Test already passes; nothing to do (pass -m to request a change anyway).")
                return 0
            test_out = protocol.test_result(args.test, code, out)
        message = args.message or (f"Make this true: {args.check}" if args.check else "")
        if chat is None:
            chat = open_chat(args, profile_dir)
            chat.usage.price = args.price
        loop = FixLoop(chat, ws, transcript, args.test, args.retries, args.patience, args.max_iters, message)
        passed = loop.run(protocol.initial_prompt(message, test_out, files, ws.layout()), first)
    finally:
        if backup_dir.exists():
            print(f"Originals of modified files backed up in {backup_dir}")
        if chat is not None:
            if chat.usage.turns:
                print(paint("\n" + chat.usage.report(chat.model), BOLD))
            chat.browser.close()
        if args.anonymous:
            shutil.rmtree(profile_dir, ignore_errors=True)
    return 0 if passed or not args.test else 1


def entry() -> None:
    from playwright.sync_api import Error as PWError
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except PWError as e:  # the browser, not the code: one clear line instead of a Playwright traceback
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        sys.exit(f"\nBrowser error: {first}\nIf Gemini's page looks different now, see docs/development.md "
                 f"(\"When it breaks\"); rerunning (or /new) usually helps.")
