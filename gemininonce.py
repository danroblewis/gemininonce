#!/usr/bin/env python3
"""gemininonce: a tiny fix-and-test loop that drives Gemini *web* via browser automation.

No API, no tool calls. We send files + test output as a chat message, ask Gemini to reply in a
strict format (FILE: <path> + full contents, COMMAND: + shell command), scrape the rendered reply
from the DOM, write the files, and re-run the test command until it passes.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# --- Gemini web selectors. These are the most likely thing to break; adjust here. -------------
GEMINI_URL = "https://gemini.google.com/app"
SEL_INPUT = 'rich-textarea div[contenteditable="true"]'
SEL_SEND = 'button[aria-label*="Send"]'
SEL_STOP = 'button[aria-label*="Stop"]'
SEL_RESPONSE = "model-response"
SEL_RESPONSE_BODY = "message-content"

HOME = Path(os.environ.get("GEMININONCE_HOME", Path.home() / ".gemininonce"))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".tox", ".mypy_cache"}
MAX_FILE_BYTES = 200_000
MAX_OUTPUT_CHARS = 8_000

# Walk the rendered reply in document order, emitting text blocks and code blocks.
EXTRACT_JS = """
(root) => {
  const out = [];
  const isCode = el => el.tagName === 'PRE' || el.tagName === 'CODE-BLOCK';
  const walk = el => {
    for (const c of el.children) {
      if (isCode(c)) {
        const code = c.querySelector('code') || c.querySelector('pre') || c;
        out.push({kind: 'code', text: code.innerText});
      } else if (c.querySelector('pre, code-block')) {
        walk(c);
      } else {
        const t = c.innerText;
        if (t && t.trim()) out.push({kind: 'text', text: t});
      }
    }
  };
  walk(root);
  return out;
}
"""

RULES = """\
RULES FOR YOUR REPLY (a script applies your reply mechanically; you cannot run tools):
1. For every file you change or create, write a line `FILE: <relative/path>` and immediately after it
   ONE fenced code block containing the COMPLETE new contents of that file. Never send diffs,
   partial files, or placeholders like "... rest unchanged".
2. Only include files you actually change.
3. If a shell command must be run (e.g. install a dependency), write a line `COMMAND:` followed by a
   fenced code block containing only the command. The user will review it before it runs.
4. Keep any explanation short, and put nothing but file contents / commands inside code blocks.
"""

MARKER = re.compile(r"^[\s*_#>`-]*(FILE|COMMAND)[\s*_`]*:[\s*_`]*([^\s*`]*)", re.M)
LAZY = re.compile(r"\.\.\.\s*\(?\s*(rest|remaining|existing|unchanged|same as|other)", re.I)


# --- Gemini browser driver ------------------------------------------------------------------------
class Gemini:
    def __init__(self, profile: Path, cdp: str | None = None):
        self.pw = sync_playwright().start()
        if cdp:
            self.ctx = self.pw.chromium.connect_over_cdp(cdp).contexts[0]
        else:
            kw = dict(
                headless=False,
                viewport=None,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            try:  # real Chrome is much less likely to be blocked at Google sign-in
                self.ctx = self.pw.chromium.launch_persistent_context(str(profile), channel="chrome", **kw)
            except Exception:
                self.ctx = self.pw.chromium.launch_persistent_context(str(profile), **kw)
        self.page = self.ctx.new_page()
        self.page.goto(GEMINI_URL)
        try:
            self.page.wait_for_selector(SEL_INPUT, timeout=15_000)
        except PWTimeout:
            input("Log in to Gemini in the browser window, then press Enter here... ")
            self.page.goto(GEMINI_URL)
            self.page.wait_for_selector(SEL_INPUT, timeout=60_000)

    def ask(self, text: str, timeout: float = 600) -> list[dict]:
        page = self.page
        n = page.locator(SEL_RESPONSE).count()
        box = page.locator(SEL_INPUT).first
        box.click()
        box.fill(text)
        try:
            page.locator(SEL_SEND).first.click(timeout=5_000)
        except PWTimeout:
            box.press("Enter")
        page.wait_for_function(f"document.querySelectorAll('{SEL_RESPONSE}').length > {n}", timeout=60_000)

        # Done when the stop button is gone and the reply text has been stable for a few seconds.
        last = page.locator(SEL_RESPONSE).last
        prev, stable_since, deadline = None, time.time(), time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            cur = last.inner_text()
            if cur != prev:
                prev, stable_since = cur, time.time()
                print(f"\r  receiving... {len(cur)} chars", end="", flush=True)
            elif cur.strip() and not page.locator(SEL_STOP).first.is_visible() and time.time() - stable_since >= 3:
                break
        else:
            raise TimeoutError("Gemini did not finish responding in time")
        print()

        body = last.locator(SEL_RESPONSE_BODY).first
        (HOME / "last_response.html").write_text(body.evaluate("e => e.outerHTML"))
        return body.evaluate(EXTRACT_JS)

    def close(self):
        try:
            self.ctx.close()
        finally:
            self.pw.stop()


# --- Reply parsing ----------------------------------------------------------------------------
def parse_reply(blocks: list[dict]):
    edits, commands, prose, pending = [], [], [], None
    for b in blocks:
        if b["kind"] == "text":
            prose.append(b["text"])
            for m in MARKER.finditer(b["text"]):
                pending = (m.group(1), m.group(2).strip().strip("'\""))
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


# --- Files --------------------------------------------------------------------------------------
def read_text(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = p.read_bytes()
        return None if b"\0" in data[:8192] else data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def list_dir(d: Path) -> list[Path]:
    try:  # respect .gitignore when possible
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=d,
                             capture_output=True, check=True).stdout.decode()
        return [d / f for f in out.split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        found = []
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS and not x.startswith(".")]
            found += [Path(dirpath) / f for f in filenames]
        return found


def collect_files(root: Path, targets: list[str]) -> dict[str, str]:
    files = {}
    for t in targets:
        p = Path(t).resolve()
        for f in sorted(list_dir(p) if p.is_dir() else [p]):
            if not f.resolve().is_relative_to(root):
                sys.exit(f"{f} is outside --root {root}")
            text = read_text(f)
            if text is None:
                print(f"  skipping {f.relative_to(root)} (binary, unreadable, or too large)")
                continue
            files[str(f.resolve().relative_to(root))] = text
    return files


def fenced(text: str, info: str = "") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{info}\n{text.rstrip()}\n{fence}"


def apply_edits(root: Path, edits, backup_dir: Path) -> list[str]:
    notes = []
    for rel, content in edits:
        p = (root / rel).resolve()
        if not p.is_relative_to(root):
            print(f"  REJECTED {rel}: outside project root")
            notes.append(f"`{rel}` is outside the project; edit rejected.")
            continue
        if not content.endswith("\n"):
            content += "\n"
        if LAZY.search(content) and input(f"  {rel} looks like a partial file. Apply anyway? [y/N] ").lower() != "y":
            notes.append(f"Your `{rel}` looked partial (placeholder like '... rest unchanged'). Send the COMPLETE file.")
            continue
        old = p.read_text() if p.exists() else None
        if old == content:
            continue
        if old is not None:
            (backup_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            if not (backup_dir / rel).exists():  # keep the pre-session original
                shutil.copy2(p, backup_dir / rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        diff = list(difflib.unified_diff((old or "").splitlines(), content.splitlines(), lineterm="", n=0))
        plus = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        minus = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        print(f"  {'M' if old is not None else 'A'} {rel}  (+{plus} -{minus})")
    return notes


# --- Commands -----------------------------------------------------------------------------------
def run(cmd: str, root: Path, timeout: int) -> tuple[int, str]:
    print(f"$ {cmd}")
    try:
        r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout)
        code, out = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        code, out = -1, f"{e.stdout or ''}{e.stderr or ''}\n[timed out after {timeout}s]"
    if len(out) > MAX_OUTPUT_CHARS:
        out = "[...truncated...]\n" + out[-MAX_OUTPUT_CHARS:]
    print("\n".join(out.splitlines()[-40:]))
    print(f"[exit {code}]")
    return code, out


def edit_text(text: str) -> str:
    if "\n" in text:  # multi-line: use $EDITOR
        with tempfile.NamedTemporaryFile("w+", suffix=".sh", delete=False) as f:
            f.write(text)
        subprocess.call([os.environ.get("EDITOR", "vi"), f.name])
        return Path(f.name).read_text().strip()
    try:
        import readline
        readline.set_startup_hook(lambda: readline.insert_text(text))
        try:
            return input("  edit> ").strip()
        finally:
            readline.set_startup_hook()
    except ImportError:
        return input("  edit (empty keeps original)> ").strip() or text


def review_commands(commands: list[str], root: Path, timeout: int) -> list[str]:
    results = []
    for cmd in commands:
        print("\nGemini suggests running:\n" + "\n".join("    " + line for line in cmd.splitlines()))
        while True:
            a = input("  [a]pprove / [m]odify / [s]kip? ").strip().lower()
            if a in ("a", "m", "s"):
                break
        if a == "m":
            cmd = edit_text(cmd)
        if a == "s" or not cmd:
            results.append(f"User skipped command:\n{fenced(cmd)}")
            continue
        code, out = run(cmd, root, timeout)
        results.append(f"User ran:\n{fenced(cmd)}\nExit code {code}, output:\n{fenced(out)}")
    return results


# --- Main loop ----------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Fix code with Gemini web in a test loop.")
    ap.add_argument("paths", nargs="+", help="files and/or directories to send to Gemini")
    ap.add_argument("-t", "--test", help="command that must pass (exit 0), e.g. 'pytest -x'")
    ap.add_argument("-m", "--message", default="", help="what you want done (optional if --test fails)")
    ap.add_argument("--root", default=".", help="project root; paths in replies are relative to it")
    ap.add_argument("-n", "--max-iters", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=300, help="timeout for test/commands (seconds)")
    ap.add_argument("--profile", default=str(HOME / "profile"), help="browser profile dir (keeps login)")
    ap.add_argument("--cdp", help="attach to an already-running Chrome, e.g. http://127.0.0.1:9222")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    HOME.mkdir(parents=True, exist_ok=True)
    backup_dir = HOME / "backups" / time.strftime("%Y%m%d-%H%M%S")

    files = collect_files(root, args.paths)
    if not files:
        sys.exit("No readable files to send.")
    size = sum(map(len, files.values()))
    print(f"Sending {len(files)} file(s), {size:,} chars.")
    if size > 400_000 and input("That's a lot for a chat message. Continue? [y/N] ").lower() != "y":
        return

    test_out = ""
    if args.test:
        code, test_out = run(args.test, root, args.timeout)
        if code == 0 and not args.message:
            print("Test already passes; nothing to do (pass -m to request a change anyway).")
            return
        test_out = f"Test command `{args.test}` exited {code}. Output:\n{fenced(test_out)}"
    elif not args.message:
        sys.exit("Give --test and/or --message.")

    prompt = "\n\n".join(filter(None, [
        "You are fixing code in a local project.",
        f"TASK: {args.message}" if args.message else "TASK: make the test command pass.",
        test_out,
        "PROJECT FILES:\n\n" + "\n\n".join(f"FILE: {rel}\n{fenced(text)}" for rel, text in files.items()),
        RULES,
    ]))

    gemini = Gemini(Path(args.profile), args.cdp)
    try:
        for i in range(1, args.max_iters + 1):
            print(f"\n=== Round {i}: asking Gemini ===")
            edits, commands, prose = parse_reply(gemini.ask(prompt))
            if prose.strip():
                print("\n" + "\n".join("  | " + line for line in prose.strip().splitlines()[:30]))
            notes = apply_edits(root, edits, backup_dir)
            results = review_commands(commands, root, args.timeout)

            if not edits and not commands:
                reply = input("\nNo file changes or commands. Your reply (empty to stop): ").strip()
                if not reply:
                    break
                prompt = f"{reply}\n\n{RULES}"
                continue

            if not args.test:
                reply = input("\nApplied. Follow-up for Gemini (empty to finish): ").strip()
                if not reply:
                    break
                prompt = "\n\n".join([reply, *notes, *results, RULES])
                continue

            code, out = run(args.test, root, args.timeout)
            if code == 0:
                print(f"\nTests pass after {i} round(s).")
                break
            prompt = "\n\n".join([
                f"I applied your changes. `{args.test}` still fails (exit {code}). Output:\n{fenced(out)}",
                *notes, *results, "Fix it.", RULES,
            ])
        else:
            print(f"\nGave up after {args.max_iters} rounds.")
    finally:
        if backup_dir.exists():
            print(f"Originals of modified files backed up in {backup_dir}")
        gemini.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
