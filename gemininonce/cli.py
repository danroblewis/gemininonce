"""Command line: options, setup checks, and wiring the pieces together."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import HOME, chrome_profiles, protocol, safety
from .browser import Browser
from .console import BOLD, DIM, YELLOW, ask_user, paint
from .gemini import GeminiChat
from .highlight import Highlighter
from .loop import FixLoop
from .transcript import Transcript
from .workspace import Workspace

ENV_VARS = ("GEMININONCE_ACCOUNT", "GEMININONCE_HOME", "GEMININONCE_CHROME_PROFILE", "GEMININONCE_MODEL",
            "GEMININONCE_PRICE")


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show '(default: X)' only when there's a meaningful default."""

    def _get_help_string(self, action):
        if action.default in (None, False, "", argparse.SUPPRESS) or "default" in (action.help or ""):
            return action.help
        return super()._get_help_string(action)


def add_session_options(ap: argparse.ArgumentParser) -> None:
    """Options shared by every command: the loop's limits, and how we reach Gemini."""
    ap.add_argument("-n", "--max-iters", type=int, default=20, help="hard cap on rounds")
    ap.add_argument("--price", default=os.environ.get("GEMININONCE_PRICE"), metavar="IN,OUT",
                    help="API price in USD per 1M input,output tokens for the cost estimate "
                         "(built-in table used if omitted)")
    ap.add_argument("--retries", type=int, default=3, help="re-asks when Gemini replies without code")
    ap.add_argument("--patience", type=int, default=3, help="stop after N rounds with unchanged test output")
    ap.add_argument("--timeout", type=int, default=300, help="timeout for test/commands (seconds)")
    ap.add_argument("--profile", default=str(HOME / "profile"), help="browser profile dir (keeps login)")
    ap.add_argument("--account", default=os.environ.get("GEMININONCE_ACCOUNT"),
                    help="refuse to send unless the Gemini account contains this, e.g. @corp.com")
    ap.add_argument("--chrome-profile", default=os.environ.get("GEMININONCE_CHROME_PROFILE"),
                    help="use a copy of your own Chrome profile (dir, name or email, e.g. 'Profile 2'); "
                         "'list' shows them")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="run the test command unsandboxed (allows network and writes outside the project)")
    ap.add_argument("--model", default=os.environ.get("GEMININONCE_MODEL"),
                    help="Gemini model to force: flash, pro, flash-lite, ...; 'any' leaves it alone "
                         "(default: flash, or flash-lite with --anonymous)")
    ap.add_argument("--anonymous", action="store_true",
                    help="use signed-out Gemini (free tier) in a throwaway profile: no account, no copied "
                         "Chrome data; conversations should be treated as public")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the --anonymous confirmation")
    ap.add_argument("--show", action="store_true", help="show the browser window (hidden by default)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print full code and outputs in the transcript")
    ap.add_argument("--cdp", help="attach to an already-running Chrome, e.g. http://127.0.0.1:9222")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gemininonce", description="Fix code with Gemini web in a test loop. "
                                 "(Also: `gemininonce build IDEA` writes a spec, tests, then the code.)",
                                 formatter_class=HelpFormatter)
    ap.add_argument("paths", nargs="+", help="files and/or directories to send to Gemini")
    ap.add_argument("-t", "--test", help="command that must pass (exit 0), e.g. 'pytest -x'")
    ap.add_argument("-m", "--message", default="", help="what you want done (optional if --test fails)")
    ap.add_argument("--root", default=".", help="project root; paths in replies are relative to it")
    ap.add_argument("--allow-new-files", action="store_true",
                    help="let Gemini create files without asking (they're often hallucinated paths)")
    add_session_options(ap)
    return ap


def warn_unknown_env_vars() -> None:
    for var in os.environ:
        if var.startswith("GEMININONCE_") and var not in ENV_VARS:
            print(f"warning: unknown environment variable {var} (did you mean GEMININONCE_ACCOUNT?)")


def confirm_anonymous(args, files: dict[str, str]) -> bool:
    """Enforce --anonymous's restrictions and get the user's OK; False means abort."""
    explicit = [f for f in ("--chrome-profile", "--account", "--cdp", "--profile")
                if any(a == f or a.startswith(f + "=") for a in sys.argv[1:])]
    if explicit:
        sys.exit(f"--anonymous can't be combined with {', '.join(explicit)}")
    for var in ("GEMININONCE_CHROME_PROFILE", "GEMININONCE_ACCOUNT"):
        if os.environ.get(var):
            print(paint(f"--anonymous: ignoring {var}", DIM))
    args.chrome_profile = args.account = None
    print(paint("\n⚠  ANONYMOUS MODE: signed-out Gemini, free tier", YELLOW, BOLD))
    print(paint("   Fresh throwaway browser profile: no Google account, no cookies, nothing copied\n"
                "   from Chrome; deleted afterwards. Free-tier chats may be kept by Google, used to\n"
                "   improve its products and read by human reviewers. Treat everything sent as PUBLIC:\n"
                "   the task, test output, the list of file names in the project, and these\n"
                f"   {len(files)} file(s) (Gemini may ask for others; you'll be asked before any is sent):",
                YELLOW))
    print("\n".join(paint(f"     {rel}", YELLOW) for rel in files))
    if not args.yes and ask_user(paint("   Type 'yes' to continue: ", YELLOW, BOLD)).strip().lower() != "yes":
        print("Aborted; nothing was sent.")
        return False
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
        return Path(tempfile.mkdtemp(prefix="gemininonce-anon-"))
    if args.chrome_profile and args.profile == str(HOME / "profile"):
        return HOME / "chrome"
    return Path(args.profile)


def main() -> int:
    if sys.argv[1:2] == ["build"]:
        from .pipeline import main as build_main
        return build_main(sys.argv[2:])
    args = build_parser().parse_args()
    if args.chrome_profile == "list":
        for d, label in chrome_profiles.profiles().items():
            print(f"  {d!r}: {label}")
        return 0

    warn_unknown_env_vars()
    root = Path(args.root).resolve()
    HOME.mkdir(parents=True, exist_ok=True)
    backup_dir = HOME / "backups" / time.strftime("%Y%m%d-%H%M%S")
    ws = Workspace(root, backup_dir, args.timeout, sandbox=not args.no_sandbox,
                   allow_new_files=args.allow_new_files)

    files = ws.collect(args.paths)
    if not files:
        sys.exit("No readable files to send.")
    ws.hl = Highlighter.for_files(files)
    size = sum(map(len, files.values()))
    print(f"Sending {len(files)} file(s), {size:,} chars.")
    if size > 400_000 and ask_user("That's a lot for a chat message. Continue? [y/N] ").lower() != "y":
        return 1
    if args.anonymous and not confirm_anonymous(args, files):
        return 1

    test_out, first = "", None
    if args.test:
        if ws.sandbox and not safety.sandbox_argv("true", root):
            print("warning: no sandbox available here (macOS sandbox-exec / Linux bwrap); "
                  "the test command will run Gemini's code unconfined.")
        first = code, out = ws.run_test(args.test)
        if code == 0 and not args.message:
            print("Test already passes; nothing to do (pass -m to request a change anyway).")
            return 0
        test_out = protocol.test_result(args.test, code, out)
    elif not args.message:
        sys.exit("Give --test and/or --message.")

    profile_dir = profile_dir_for(args)
    try:
        chat = open_chat(args, profile_dir)
    except BaseException:
        if args.anonymous:
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    chat.usage.price = args.price
    loop = FixLoop(chat, ws, Transcript(args.verbose, ws.hl), args.test, args.retries, args.patience,
                   args.max_iters, args.message)
    passed = False
    try:
        passed = loop.run(protocol.initial_prompt(args.message, test_out, files, ws.layout()), first)
    finally:
        if backup_dir.exists():
            print(f"Originals of modified files backed up in {backup_dir}")
        if chat.usage.turns:
            print(paint("\n" + chat.usage.report(chat.model), BOLD))
        chat.browser.close()
        if args.anonymous:
            shutil.rmtree(profile_dir, ignore_errors=True)
    return 0 if passed or not args.test else 1


def entry() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
