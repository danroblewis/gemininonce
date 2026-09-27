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

try:
    from pygments import highlight
    from pygments.formatters import Terminal256Formatter
    from pygments.lexers import (BashLexer, DiffLexer, get_lexer_by_name, get_lexer_for_filename,
                                 guess_lexer)
    from pygments.util import ClassNotFound
except ImportError:  # highlighting is optional
    highlight = None

import gemininonce_safety as safety

# --- Gemini web selectors. These are the most likely thing to break; adjust here. -------------
GEMINI_URL = "https://gemini.google.com/app"
SEL_INPUT = 'rich-textarea div[contenteditable="true"]'
SEL_SEND = 'button[aria-label*="Send"]'
SEL_STOP = 'button[aria-label*="Stop"]'
SEL_RESPONSE = "model-response"
SEL_RESPONSE_BODY = "message-content"
SEL_MODEL_BUTTON = '[data-test-id="bard-mode-menu-button"]'
SEL_MODEL_OPTION = '[data-test-id^="bard-mode-option"]'
SEL_SIGNED_OUT = 'a[href*="accounts.google.com/ServiceLogin"]'
SEL_ACCOUNT = 'a[href*="accounts.google.com/SignOutOptions"], [aria-label^="Google Account"]'
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

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
        const label = c.querySelector('.code-block-decoration span');
        out.push({kind: 'code', text: code.innerText, lang: label ? label.innerText.trim() : ''});
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


COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
BOLD, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "1", "2", "31", "32", "33", "34", "95", "36"


def paint(text: str, *codes: str) -> str:
    return f"\033[{';'.join(codes)}m{text}\033[0m" if COLOR and codes else text


def paint_findings(text: str) -> str:
    return "\n".join(paint(ln, RED if "[HIGH]" in ln else YELLOW) for ln in text.splitlines())


# --- Syntax highlighting -------------------------------------------------------------------
PROJECT_LEXER = None  # main language of the files we sent; used for code lines inside test output
DIFF_RE = re.compile(r"^(@@ .* @@|--- \S|\+\+\+ \S)", re.M)


def lexer_for(code: str, path: str | None = None, label: str | None = None):
    """Pick a Pygments lexer from a file path, Gemini's code-block label, or by content."""
    if not highlight:
        return None
    if DIFF_RE.search(code) and re.search(r"^[+-]", code, re.M):
        return DiffLexer()
    for attempt in (lambda: get_lexer_for_filename(path, code) if path else None,
                    lambda: get_lexer_by_name(label.lower()) if label else None):
        try:
            if (lx := attempt()):
                return lx
        except ClassNotFound:
            pass
    try:
        lx = guess_lexer(code)
        return lx if lx.name != "Text only" and lx.analyse_text(code) >= 0.1 else None
    except ClassNotFound:
        return None


def colorize(code: str, lexer) -> str:
    if not (COLOR and highlight and lexer):
        return code
    return highlight(code, lexer, Terminal256Formatter(style="monokai")).rstrip("\n")


def colorize_output(text: str) -> str:
    """Color test/command output: diffs as diffs, otherwise line by line (errors, passes, locations,
    and quoted source lines in the project's language)."""
    if not COLOR:
        return text
    if highlight and DIFF_RE.search(text) and re.search(r"^[+-]", text, re.M):
        return colorize(text, DiffLexer())
    out = []
    for ln in text.splitlines():
        s = ln.lstrip()
        if re.search(r"\b\d+ (failed|errors?)\b", ln) or re.match(r"(E\s|FAILED\b|ERROR\b)", ln) \
                or re.match(r"[\w.]*(Error|Exception)\b.*:", s):
            out.append(paint(ln, RED))
        elif re.match(r"[=_-]{5,} .* [=_-]{5,}$", ln):  # pytest section headers
            out.append(paint(ln, BOLD))
        elif re.search(r"\b\d+ passed\b|\bPASSED\b|^ok\b", ln):
            out.append(paint(ln, GREEN))
        elif re.match(r"[\w./\\-]+\.\w+:\d+", s) or s.startswith(("File \"", "at ")):
            out.append(paint(ln, CYAN))
        elif PROJECT_LEXER and re.match(r"(>|\s{4,}|\t)\s*\S", ln):
            lead = re.match(r"(>\s*|\s*)", ln).group(1)
            out.append(paint(lead, DIM) + colorize(ln[len(lead):], PROJECT_LEXER))
        else:
            out.append(paint(ln, DIM))
    return "\n".join(out)


def ask_user(msg: str) -> str:
    """input() that treats a closed stdin (non-interactive run) as an empty answer."""
    try:
        return input(msg)
    except EOFError:
        print("(no input available)")
        return ""


# --- Using your real Chrome profile ---------------------------------------------------------------
# Chrome 136+ refuses automation of its *default* data dir, but a copy elsewhere works, and the
# copied cookies still decrypt (on macOS the key lives in the Keychain, not the profile). So we
# mirror your profile (minus caches) into ~/.gemininonce/chrome and launch that: same SSO sessions.
CHROME_DATA = {
    "darwin": Path.home() / "Library/Application Support/Google/Chrome",
    "linux": Path.home() / ".config/google-chrome",
    "win32": Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
}.get(sys.platform)
PROFILE_SKIP = ["Service Worker", "File System", "History*", "Favicons*", "Top Sites*", "Visited Links",
                "Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs", "*Cache*",
                "Shared Dictionary", "Crashpad", "Singleton*", "BrowserMetrics*"]


def chrome_profiles() -> dict[str, str]:
    """{profile dir: 'Name <email>'} from Chrome's Local State."""
    import json
    try:
        info = json.loads((CHROME_DATA / "Local State").read_text())["profile"]["info_cache"]
    except (OSError, KeyError, ValueError, TypeError):
        return {}
    return {d: f"{v.get('name', '')} <{v.get('user_name') or 'not signed in'}>" for d, v in info.items()}


def resolve_profile(name: str) -> str:
    """Accept a profile dir ('Profile 2'), display name, or email substring."""
    profiles = chrome_profiles()
    if name in profiles:
        return name
    hits = [d for d, label in profiles.items() if name.lower() in label.lower()]
    if len(hits) != 1:
        listing = "\n".join(f"  {d!r}: {label}" for d, label in profiles.items()) or "  (none found)"
        sys.exit(f"Chrome profile {name!r} is {'ambiguous' if hits else 'not found'}. Profiles:\n{listing}")
    return hits[0]


def mirror_chrome_profile(profile: str, dest: Path) -> None:
    src = CHROME_DATA / profile
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)  # holds live session cookies
    print(f"Copying Chrome profile {profile!r} ({chrome_profiles().get(profile, '')}) ...")
    shutil.copy2(CHROME_DATA / "Local State", dest / "Local State")
    if shutil.which("rsync"):
        cmd = ["rsync", "-a", "--delete", *[f"--exclude={p}" for p in PROFILE_SKIP], f"{src}/", str(dest / profile)]
        subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)  # files changing mid-copy are fine
    else:
        import fnmatch
        shutil.rmtree(dest / profile, ignore_errors=True)
        shutil.copytree(src, dest / profile, ignore_dangling_symlinks=True,
                        ignore=lambda d, names: [n for n in names if any(fnmatch.fnmatch(n, p) for p in PROFILE_SKIP)])


# --- Gemini browser driver ------------------------------------------------------------------------
class Gemini:
    def __init__(self, profile: Path, cdp: str | None = None, account: str | None = None,
                 chrome_profile: str | None = None, model: str | None = None, show: bool = False,
                 anonymous: bool = False):
        self.required_account = account
        self.anonymous = anonymous
        self.model = model  # resolved to the picker's exact name by select_model()
        self.turns: list[tuple[int, int]] = []  # (chars sent, chars received) per message
        self.pw = sync_playwright().start()
        self.headless = not show and not cdp
        try:
            self._start(profile, cdp, chrome_profile, anonymous)
        except BaseException:
            self.close()  # don't leave Chrome running (or writing into a profile we're about to delete)
            raise

    def _start(self, profile: Path, cdp: str | None, chrome_profile: str | None, anonymous: bool) -> None:
        if cdp:
            self.ctx = self.pw.chromium.connect_over_cdp(cdp).contexts[0]
            self.page = self.ctx.new_page()
        else:
            self._profile = profile
            self._args = ["--disable-blink-features=AutomationControlled"]
            self._ignore = ["--enable-automation"]
            if chrome_profile:
                mirror_chrome_profile(chrome_profile, profile)
                self._args += [f"--profile-directory={chrome_profile}", "--no-first-run"]
                # Playwright's defaults hide the real macOS Keychain, so copied cookies wouldn't decrypt.
                self._ignore += ["--use-mock-keychain", "--password-store=basic"]
            self._launch()
        self._load()
        if anonymous:
            self.check_signed_out()
            print(paint("Gemini: signed out (anonymous, free tier)", YELLOW))
        elif self.headless:  # a hidden browser can't be signed into: show a window only if we need one
            email = self.account()
            if not (self._ok(email) or (email and self._find_account())):
                print(paint("Opening a browser window so you can sign in...", YELLOW))
                self.ctx.close()
                self.headless = False
                self._launch()
                self._load()
        if not anonymous:
            self.check_account(wait=True)
        if self.model:
            self.select_model(self.model)

    def _launch(self) -> None:
        kw = dict(headless=self.headless, args=self._args, ignore_default_args=self._ignore,
                  viewport={"width": 1280, "height": 900} if self.headless else None)
        try:  # real Chrome is much less likely to be blocked at Google sign-in
            self.ctx = self.pw.chromium.launch_persistent_context(str(self._profile), channel="chrome", **kw)
        except Exception:
            self.ctx = self.pw.chromium.launch_persistent_context(str(self._profile), **kw)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if self.headless:  # don't advertise "HeadlessChrome" to Google
            ua = self.page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            self.ctx.new_cdp_session(self.page).send("Emulation.setUserAgentOverride", {"userAgent": ua})

    def _load(self) -> None:
        self.page.goto(GEMINI_URL)
        try:
            self.page.wait_for_selector(SEL_INPUT, timeout=15_000)
        except PWTimeout:
            if self.headless:
                return  # the headless sign-in check will reopen visibly
            print("Log in to Gemini in the browser window (waiting up to 5 minutes)...")
            self.page.wait_for_url("https://gemini.google.com/**", timeout=300_000)
            self.page.wait_for_selector(SEL_INPUT, timeout=300_000)

    def check_signed_out(self) -> None:
        """Anonymous mode: refuse to send unless the page is definitely signed out."""
        if not self.page.locator(SEL_SIGNED_OUT).count() or self.account():
            raise SystemExit("Refusing to send: --anonymous but Gemini appears to be signed in.")

    def account(self) -> str | None:
        """Signed-in email, or None when signed out / unknown. Waits briefly for the header to render."""
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.page.locator(SEL_SIGNED_OUT).count():
                return None
            for el in self.page.locator(SEL_ACCOUNT).all():
                m = EMAIL_RE.search(el.get_attribute("aria-label") or "")
                if m:
                    return m.group(0)
            time.sleep(0.5)
        return None

    def _ok(self, email: str | None) -> bool:
        want = self.required_account
        return bool(email) and (not want or want.lower() in email.lower())

    def _open(self, url: str) -> str | None:
        self.page.goto(url)
        try:
            self.page.wait_for_selector(SEL_INPUT, timeout=15_000)
        except PWTimeout:
            return None
        return self.account()

    def _find_account(self) -> str | None:
        """With several Google accounts signed in, each has its own /u/N/ URL. Find the right one."""
        for n in range(6):
            email = self._open(f"https://gemini.google.com/u/{n}/app")
            if self._ok(email):
                return email
            if email is None:
                break
        return None

    def check_account(self, wait: bool = False) -> None:
        """Refuse to talk to Gemini unless signed in (as the required account, if one is set)."""
        want = self.required_account
        email = self.account()
        if self._ok(email):
            if wait:
                print(f"Gemini account: {email}")
            return
        if not wait:
            raise SystemExit(f"Refusing to send: Gemini is signed in as {email or 'nobody (signed out)'}"
                             + (f", expected {want}" if want else ""))
        if email and (found := self._find_account()):
            print(f"Gemini account: {found}")
            return
        print(f"Gemini is signed in as {email or 'nobody'}. Sign in{' as ' + want if want else ''} "
              "in the browser window (waiting up to 5 minutes)...")
        self.page.goto("https://accounts.google.com/ServiceLogin?continue=" + GEMINI_URL)
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2)
            if not self.page.url.startswith("https://gemini.google.com"):
                continue  # still on the Google sign-in pages
            if (found := self._find_account()):
                print(f"Gemini account: {found}")
                return
            print(f"Still not signed in{' as ' + want if want else ''}; opening sign-in again...")
            self.page.goto("https://accounts.google.com/ServiceLogin?continue=" + GEMINI_URL)
        raise SystemExit("Timed out waiting for Gemini sign-in; nothing was sent.")

    @staticmethod
    def _model_key(name: str) -> str:
        """'3.6 Flash' -> 'flash', so a request survives version bumps."""
        return re.sub(r"^\d+(\.\d+)*\s*", "", name.strip().lower())

    def current_model(self) -> str:
        btn = self.page.locator(SEL_MODEL_BUTTON).first
        for _ in range(20):  # the label fills in shortly after page load
            m = re.search(r"currently (.+)", btn.get_attribute("aria-label") or "")
            name = (m.group(1) if m else btn.inner_text()).strip()
            if name:
                return name
            time.sleep(0.25)
        return ""

    def select_model(self, want: str) -> None:
        """Pick a model from Gemini's mode picker by name ('flash', 'pro', 'flash-lite', '3.1', ...)."""
        self.page.locator(SEL_MODEL_BUTTON).first.click()
        self.page.locator(SEL_MODEL_OPTION).first.wait_for(timeout=10_000)
        options, disabled = {}, []
        for el in self.page.locator(SEL_MODEL_OPTION).all():
            name = el.inner_text().split("\n")[0].strip()
            if el.get_attribute("aria-disabled") == "true":
                disabled.append(name)  # e.g. Flash/Pro when signed out
            else:
                options[name] = el
        w = want.strip().lower()
        names = [*options, *disabled]  # match against all, so 'flash' can't fall through to Flash-Lite
        hits = [n for n in names if w in (n.lower(), self._model_key(n))] \
            or [n for n in names if w in n.lower()]
        if len(hits) == 1 and hits[0] in disabled:
            self.page.keyboard.press("Escape")
            raise SystemExit(f"Model {hits[0]!r} isn't available here"
                             + (" (signed out: only " + ", ".join(options) + ")" if self.anonymous else "")
                             + ". Pick another with --model.")
        if len(hits) != 1:
            self.page.keyboard.press("Escape")
            raise SystemExit(f"Model {want!r} is {'ambiguous' if hits else 'not available'}. "
                             f"Gemini offers: {', '.join(options)}"
                             + (f" (unavailable here: {', '.join(disabled)})" if disabled else ""))
        options[hits[0]].click()
        time.sleep(1)
        if self._model_key(self.current_model()) != self._model_key(hits[0]):
            raise SystemExit(f"Tried to select {hits[0]!r} but Gemini shows {self.current_model()!r}")
        self.model = hits[0]
        print(paint(f"Gemini model: {hits[0]}", DIM))

    def ask(self, text: str, timeout: float = 600) -> list[dict]:
        page = self.page
        # re-check every time: a session can expire or switch mid-run
        self.check_signed_out() if self.anonymous else self.check_account()
        if self.model and self._model_key(self.current_model()) != self._model_key(self.model):
            print(f"  model changed to {self.current_model()!r}; switching back to {self.model!r}")
            self.select_model(self.model)
        n = page.locator(SEL_RESPONSE).count()
        box = page.locator(SEL_INPUT).first
        box.click()
        text = safety.redact(text)  # last line of defense: never send secrets
        box.fill(text)
        try:
            page.locator(SEL_SEND).first.click(timeout=5_000)
        except PWTimeout:
            box.press("Enter")
        # Poll from Python: Gemini's Trusted Types CSP blocks string-eval'd wait_for_function.
        start = time.time()
        while page.locator(SEL_RESPONSE).count() <= n:
            if time.time() - start > 60:
                raise TimeoutError("Gemini never started a response (message not sent?)")
            time.sleep(0.5)

        # Done when the stop button is gone and the reply text has been stable for a few seconds.
        last = page.locator(SEL_RESPONSE).last
        prev, stable_since, deadline = None, time.time(), time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            cur = last.inner_text()
            if cur != prev:
                prev, stable_since = cur, time.time()
                print(paint(f"\r  receiving... {len(cur)} chars", DIM), end="", flush=True)
            elif cur.strip() and not page.locator(SEL_STOP).first.is_visible() and time.time() - stable_since >= 3:
                break
        else:
            raise TimeoutError("Gemini did not finish responding in time")
        print()
        self.turns.append((len(text), len(prev or "")))

        body = last.locator(SEL_RESPONSE_BODY).first
        (HOME / "last_response.html").write_text(body.evaluate("e => e.outerHTML"))
        return body.evaluate(EXTRACT_JS)

    def close(self):
        try:
            if getattr(self, "ctx", None):
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
        if not p.exists():
            sys.exit(f"{t}: no such file or directory (cwd is {Path.cwd()})")
        for f in sorted(list_dir(p) if p.is_dir() else [p]):
            if not f.resolve().is_relative_to(root):
                sys.exit(f"{f} is outside --root {root}")
            rel = str(f.resolve().relative_to(root))
            if safety.PROTECTED.search(rel):
                print(f"  skipping {rel} (secrets/credentials/CI file; never sent)")
                continue
            text = read_text(f)
            if text is None:
                print(f"  skipping {rel} (binary, unreadable, or too large)")
                continue
            if (hits := safety.find_secrets(text)):
                print(f"  skipping {rel} (contains what looks like a secret: {', '.join(hits[:3])})")
                continue
            files[rel] = text
    return files


def fenced(text: str, info: str = "") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{info}\n{text.rstrip()}\n{fence}"


# --- Partial-edit merging ---------------------------------------------------------------------
# When Gemini sends only the changed functions/classes instead of a whole file, splice each
# definition into the old file by name. Block extents come from indentation (plus closing
# brackets), so this works for Python and most brace languages without real parsing.
DEF_RE = re.compile(
    r"^\s*(?:(?:export|default|public|private|protected|internal|static|async|override|final|abstract|"
    r"inline|virtual|unsafe|extern|pub(?:\([^)]*\))?)\s+)*"
    r"(?:(?:def|class|function\*?|func|fn|struct|enum|interface|trait|impl|type|module)\s+(?:\([^)]*\)\s*)?"
    r"(?P<a>[A-Za-z_$][\w$]*)|(?:const|let|var)\s+(?P<b>[A-Za-z_$][\w$]*)\s*=)")
PLACEHOLDER = re.compile(r"^\s*(?:#|//|/\*+|<!--|--|\*)\s*(?:\.\.\.|…)|^\s*(?:\.\.\.|…)\s*$")
IMPORT_RE = re.compile(r"^\s*(?:import|from|#include|use|using|require)\b")
COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*|<!--|--)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _name(line: str) -> str | None:
    m = DEF_RE.match(line)
    return m and (m.group("a") or m.group("b"))


def _is_placeholder(line: str) -> bool:
    return bool(PLACEHOLDER.match(line) or LAZY.search(line))


def _block(lines: list[str], i: int) -> tuple[int, int]:
    """(start, end) of the definition whose header is lines[i], including decorators above it."""
    ind, j = _indent(lines[i]), i + 1
    while j < len(lines):
        s = lines[j].strip()
        if not s or _indent(lines[j]) > ind:
            j += 1
            continue
        if s[0] in "})]" or s == "end":
            j += 1
            if s.endswith((":", "{")):  # `) -> int:` or `} else {`: header/body continues
                continue
        break
    while j > i + 1 and not lines[j - 1].strip():
        j -= 1
    s = i
    while s > 0 and lines[s - 1].strip().startswith(("@", "#[")) and _indent(lines[s - 1]) == ind:
        s -= 1
    return s, j


def _defs(lines: list[str], lo: int, hi: int) -> list[tuple[str, int, int, int]]:
    """Direct child definitions in lines[lo:hi] as (name, start, header, end)."""
    out, i = [], lo
    while i < hi:
        name = _name(lines[i])
        if name:
            s, e = _block(lines, i)
            out.append((name, s, i, min(e, hi)))
            i = e
        else:
            i += 1
    return out


def _kids(lines: list[str], lo: int, hi: int) -> set[str]:
    """Names of nested functions/classes (not local variables) directly inside lines[lo:hi]."""
    return {d[0] for d in _defs(lines, lo, hi) if DEF_RE.match(lines[d[2]]).group("a")}


def _reindent(lines: list[str], delta: int) -> list[str]:
    if delta >= 0:
        return [(" " * delta + ln) if ln.strip() else ln for ln in lines]
    return [ln[min(-delta, _indent(ln)):] for ln in lines]


def looks_partial(old: str, new: str) -> bool:
    new_lines = new.splitlines()
    if any(_is_placeholder(ln) for ln in new_lines) and not any(_is_placeholder(ln) for ln in old.splitlines()):
        return True
    old_names = {n for ln in old.splitlines() if (n := _name(ln))}
    new_names = {n for ln in new_lines if (n := _name(ln))}
    return bool(old_names - new_names) and len(new_lines) < 0.8 * len(old.splitlines())


def merge_partial(old_text: str, new_text: str) -> tuple[str, list[str]] | None:
    """Splice definitions from a snippet into old_text. None if it can't be done unambiguously."""
    old, new = old_text.splitlines(), new_text.splitlines()
    old_stripped = {ln.strip() for ln in old}
    splices, log, imports = [], [], []

    def walk(nlo, nhi, olo, ohi) -> bool:
        i = nlo
        while i < nhi:
            line, name = new[i], _name(new[i])
            if not name:
                s = line.strip()
                if IMPORT_RE.match(line) and s not in old_stripped:
                    imports.append(s)
                elif s and s not in old_stripped and not _is_placeholder(line) and not COMMENT_RE.match(line) \
                        and not line.lstrip().startswith("@"):
                    return False  # a changed line outside any definition: can't place it safely
                i += 1
                continue
            s, e = _block(new, i)
            e = min(e, nhi)
            cands = [d for d in _defs(old, olo, ohi) if d[0] == name]
            if not cands:  # maybe a method sent without its class
                cands = [(name, bs, k, be) for k, ln in enumerate(old) if _name(ln) == name
                         for bs, be in [_block(old, k)]]
            if len(cands) > 1:
                return False
            if not cands:  # brand-new definition: add at the end of the enclosing scope
                pos = ohi
                if olo > 0 and pos > olo and old[pos - 1].strip()[:1] in ("}", ")", "]") \
                        and _indent(old[pos - 1]) == _indent(old[olo - 1]):  # container's own closer
                    pos -= 1
                kids = _defs(old, olo, ohi)
                target = _indent(old[kids[0][2]]) if kids else (0 if olo == 0 else _indent(old[olo - 1]) + 4)
                splices.append((pos, pos, [""] + _reindent(new[s:e], target - _indent(new[i]))))
                log.append(f"added {name}")
            else:
                _, os_, oh, oe = cands[0]
                block = new[s:e]
                missing = _kids(old, oh + 1, oe) - _kids(new, i + 1, e)
                if any(_is_placeholder(ln) for ln in block) or missing:
                    if not walk(i + 1, e, oh + 1, oe):  # container sent partially: recurse into it
                        return False
                else:
                    splices.append((os_, oe, _reindent(block, _indent(old[oh]) - _indent(new[i]))))
                    log.append(f"replaced {name}")
            i = e
        return True

    if not walk(0, len(new), 0, len(old)) or not (splices or imports):
        return None
    splices.sort(key=lambda x: (x[0], x[1]))
    if any(a[1] > b[0] for a, b in zip(splices, splices[1:])):
        return None  # overlapping replacements
    for s, e, repl in reversed(splices):
        old[s:e] = repl
    if imports:
        last = max((k for k, ln in enumerate(old) if IMPORT_RE.match(ln) and not _indent(ln)), default=-1)
        old[last + 1:last + 1] = imports
        log.append(f"added {len(imports)} import(s)")
    return "\n".join(old) + "\n", log


def apply_edits(root: Path, edits, backup_dir: Path, known: set[str] = frozenset(),
                allow_new: bool = False) -> list[str]:
    notes = []
    for rel, content in edits:
        p = (root / rel).resolve()
        if not p.is_relative_to(root):
            print(paint(f"  REJECTED {rel}: outside project root", RED))
            notes.append(f"`{rel}` is outside the project; edit rejected.")
            continue
        if not content.endswith("\n"):
            content += "\n"
        old = p.read_text() if p.exists() else None
        if old is None and not allow_new and \
                ask_user(f"  Gemini wants to create a NEW file {rel}. Create it? [y/N] ").strip().lower() != "y":
            print(paint(f"  REJECTED {rel}: new file not approved", RED))
            notes.append(f"`{rel}` does not exist, so it was not created. Edit the existing files instead: "
                         + ", ".join(sorted(known)) + ". Only create a new file if it's truly needed.")
            continue
        if old is not None and looks_partial(old, content):
            merged = merge_partial(old, content)
            if merged and p.suffix == ".py":
                try:
                    compile(merged[0], rel, "exec")
                except SyntaxError:
                    merged = None
            if not merged:
                print(paint(f"  REJECTED {rel}: partial edit that couldn't be merged", RED))
                notes.append(f"Your `{rel}` was partial and couldn't be merged. Send the COMPLETE file.")
                continue
            content = merged[0]
            print(f"  merged partial edit into {rel}: {', '.join(merged[1])}")
        if old == content:
            continue
        findings = safety.scan_edit(rel, old, content)
        if findings:
            high = [x for x in findings if x[0] == "high"]
            print(f"  Safety check on {rel}:\n{paint_findings(safety.format_findings(findings))}")
            if high and ask_user(f"  Write {rel} anyway? [y/N] ").strip().lower() != "y":
                print(paint(f"  REJECTED {rel}: failed safety check", RED))
                notes.append(f"Your change to `{rel}` was rejected by a safety check: "
                             + "; ".join(sorted({x[1] for x in high}))
                             + ". Do not do that; solve the problem without it.")
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
        print(paint(f"  {'M' if old is not None else 'A'} {rel}", GREEN) + f"  (+{plus} -{minus})")
    return notes


# --- Commands -----------------------------------------------------------------------------------
def run(cmd: str, root: Path, timeout: int, sandbox: bool = False) -> tuple[int, str]:
    argv = safety.sandbox_argv(cmd, root) if sandbox else None
    print(paint(f"$ {cmd}", BOLD) + paint("   [sandboxed]" if argv else "", DIM))
    try:
        r = subprocess.run(argv or cmd, shell=argv is None, cwd=root, capture_output=True, text=True,
                           timeout=timeout)
        code, out = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        code, out = -1, f"{e.stdout or ''}{e.stderr or ''}\n[timed out after {timeout}s]"
    if len(out) > MAX_OUTPUT_CHARS:
        out = "[...truncated...]\n" + out[-MAX_OUTPUT_CHARS:]
    print(colorize_output("\n".join(out.splitlines()[-40:])))
    print(paint(f"[exit {code}]", GREEN if code == 0 else RED, BOLD))
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


def review_commands(commands: list[str], root: Path, timeout: int, test_cmd: str | None) -> list[str]:
    results = []
    for cmd in commands:
        if cmd == test_cmd:
            continue  # the loop runs the test itself
        print(paint("\nGemini suggests running:", YELLOW, BOLD) + "\n" + "\n".join(paint("    " + line, BOLD) for line in cmd.splitlines()))
        if (findings := safety.scan_command(cmd)):
            print(paint("  WARNING, this command:", RED) + "\n" + paint_findings(safety.format_findings(findings)))
        while True:
            a = ask_user("  [a]pprove / [m]odify / [s]kip? ").strip().lower()
            if a in ("a", "m", "s"):
                break
            if not a and not sys.stdin.isatty():
                a = "s"
                break
        if a == "m":
            cmd = edit_text(cmd)
            if (findings := safety.scan_command(cmd)):
                print(paint("  WARNING, edited command:", RED) + "\n" + paint_findings(safety.format_findings(findings)))
        if a == "s" or not cmd:
            results.append(f"User skipped command:\n{fenced(cmd)}")
            continue
        code, out = run(cmd, root, timeout)
        results.append(f"User ran:\n{fenced(cmd)}\nExit code {code}, output:\n{fenced(out)}")
    return results


GEMINI_ERROR = re.compile(r"encountered an error|something went wrong|try again|can't help with that|"
                          r"having trouble|unable to (?:process|respond)", re.I)


def show_outgoing(msg: str, verbose: bool) -> None:
    """Print our message: rules dropped, attached files listed by name, long outputs shortened."""
    msg = msg.replace(RULES, "").strip()
    if "PROJECT FILES:" in msg:
        head, files_part = msg.split("PROJECT FILES:", 1)
        names = re.findall(r"^FILE: (\S+)$", files_part, re.M)
        msg = f"{head.rstrip()}\n\n[{len(names)} files attached: {', '.join(names)}]"
    if not verbose:  # the test output was just printed; show only the ends of long fenced blocks
        def shorten(m):
            lines = m.group(2).splitlines()
            if len(lines) <= 10:
                return m.group(0)
            return "\n".join([m.group(1), *lines[:3], f"... {len(lines) - 6} lines ...", *lines[-3:], m.group(3)])
        msg = re.sub(r"^(`{3,})\n(.*?)\n^(\1)$", shorten, msg, flags=re.S | re.M)
    print(paint("\n── You → Gemini " + "─" * 44, CYAN, BOLD))
    parts = re.split(r"^(`{3,})[^\n]*\n(.*?)\n\1$", msg, flags=re.S | re.M)
    for k, part in enumerate(parts):  # re.split yields: text, fence, body, text, fence, body, ...
        if k % 3 == 0:
            print("\n".join(paint(ln, CYAN) for ln in part.strip("\n").splitlines()))
        elif k % 3 == 2:
            print(colorize_output(part))


def show_reply(blocks: list[dict], verbose: bool) -> None:
    print(paint("\n── Gemini " + "─" * 50, MAGENTA, BOLD))
    pending = None
    for b in blocks:
        if b["kind"] == "text":
            for ln in b["text"].splitlines():
                print(paint(ln, YELLOW, BOLD) if MARKER.match(ln) else paint(ln, MAGENTA))
            for m in MARKER.finditer(b["text"]):
                pending = (m.group(1), m.group(2).strip().strip("'\""))
        else:
            code = b["text"].rstrip("\n")
            kind, path = pending or (None, None)
            pending = None
            lexer = BashLexer() if highlight and kind == "COMMAND" else \
                lexer_for(code, path if kind == "FILE" else None, b.get("lang"))
            lines = code.splitlines()
            more = len(lines) - 12 if not verbose and len(lines) > 20 else 0
            if more:
                lines = lines[:12]
            print("\n".join(paint("  │ ", DIM) + ln for ln in colorize("\n".join(lines), lexer).splitlines()))
            if more:
                print(paint(f"  │ ... {more} more lines (-v shows all)", DIM))


def ask_for_code(gemini: Gemini, prompt: str, retries: int, verbose: bool = False):
    """Ask, retrying when the reply has no files or commands (Gemini errors, refusals, chatter)."""
    msg = prompt
    for attempt in range(retries + 1):
        show_outgoing(msg, verbose)
        blocks = gemini.ask(msg)
        show_reply(blocks, verbose)
        edits, commands, prose = parse_reply(blocks)
        if edits or commands or attempt == retries:
            return edits, commands, prose
        text = prose.strip()
        if len(text) < 300 or GEMINI_ERROR.search(text):
            print(paint(f"  Gemini replied without code; resending ({attempt + 1}/{retries})", YELLOW))
            time.sleep(5)
            msg = prompt
        else:
            print(paint(f"  Gemini replied without code; asking for files ({attempt + 1}/{retries})", YELLOW))
            msg = ("Your reply contained no FILE: blocks, so nothing was applied. Reply with the complete "
                   "contents of every file that needs to change.\n\n" + RULES)
    return edits, commands, prose


# --- API-equivalent cost estimate --------------------------------------------------------------
# Gemini web on a Workspace/personal plan is flat-rate; this estimates what the same conversation
# would cost on the Gemini API. USD per 1M tokens: (input, output, input >200k, output >200k).
# Source: https://ai.google.dev/gemini-api/docs/pricing (page updated 2026-09-24). Override: --price.
PRICES = {
    "3.8 flash": (0.75, 3.75, 0.75, 3.75),   # $1.50 / $7.50 from 2027-01-01
    "3.7 flash": (0.75, 3.75, 0.75, 3.75),   # $1.50 / $7.50 from 2027-01-01
    "3.6 flash": (0.75, 3.75, 0.75, 3.75),   # $1.50 / $7.50 from 2027-01-01
    "3.5 flash": (1.50, 9.00, 1.50, 9.00),
    "3.5 flash-lite": (0.30, 2.50, 0.30, 2.50),
    "3.1 flash-lite": (0.25, 1.50, 0.25, 1.50),
    "3.1 pro": (2.00, 12.00, 4.00, 18.00),
    "2.5 pro": (1.25, 10.00, 2.50, 15.00),
    "2.5 flash": (0.30, 2.50, 0.30, 2.50),
    "2.5 flash-lite": (0.10, 0.40, 0.10, 0.40),
}
CHARS_PER_TOKEN = 4  # Google's rule of thumb; real tokenization varies (code is often denser)


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def usage_report(turns: list[tuple[int, int]], model: str | None, price: str | None) -> str:
    """Tokens and API-equivalent cost. The API is stateless, so each message re-sends the whole
    conversation so far as input; that's counted here (without context caching discounts)."""
    history, tok_in, tok_out, cost = 0, 0.0, 0.0, 0.0
    if price:
        rates = tuple(float(x) for x in price.split(","))
        rates = (rates[0], rates[1], rates[0], rates[1])
    else:
        rates = PRICES.get((model or "").lower())
    for sent, received in turns:
        i, o = (history + sent) / CHARS_PER_TOKEN, received / CHARS_PER_TOKEN
        tok_in, tok_out = tok_in + i, tok_out + o
        if rates:
            long = i > 200_000
            cost += (i * rates[2 if long else 0] + o * rates[3 if long else 1]) / 1e6
        history += sent + received
    lines = [f"Gemini usage: {len(turns)} message(s), ~{_k(tok_in)} tokens in (incl. re-sent history), "
             f"~{_k(tok_out)} tokens out"]
    if rates:
        lines.append(f"API-equivalent cost ({price and 'custom price' or model} at ${rates[0]:g}/${rates[1]:g} "
                     f"per 1M in/out): ~${cost:.4f}  (excludes hidden thinking tokens; "
                     f"Gemini web itself is free or flat-rate)")
    else:
        lines.append(f"No API price known for model {model or 'unknown'!r}; pass --price IN,OUT (USD per 1M tokens)")
    return "\n".join(lines)


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show '(default: X)' only when there's a meaningful default."""
    def _get_help_string(self, action):
        if action.default in (None, False, "", argparse.SUPPRESS) or "default" in (action.help or ""):
            return action.help
        return super()._get_help_string(action)


# --- Main loop ----------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Fix code with Gemini web in a test loop.",
                                 formatter_class=_HelpFormatter)
    ap.add_argument("paths", nargs="+", help="files and/or directories to send to Gemini")
    ap.add_argument("-t", "--test", help="command that must pass (exit 0), e.g. 'pytest -x'")
    ap.add_argument("-m", "--message", default="", help="what you want done (optional if --test fails)")
    ap.add_argument("--root", default=".", help="project root; paths in replies are relative to it")
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
    ap.add_argument("--allow-new-files", action="store_true",
                    help="let Gemini create files without asking (they're often hallucinated paths)")
    ap.add_argument("--show", action="store_true", help="show the browser window (hidden by default)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print full code and outputs in the transcript")
    ap.add_argument("--cdp", help="attach to an already-running Chrome, e.g. http://127.0.0.1:9222")
    args = ap.parse_args()
    if args.chrome_profile == "list":
        for d, label in chrome_profiles().items():
            print(f"  {d!r}: {label}")
        return 0

    for var in os.environ:
        if var.startswith("GEMININONCE_") and var not in ("GEMININONCE_ACCOUNT", "GEMININONCE_HOME", "GEMININONCE_CHROME_PROFILE",
                                                                    "GEMININONCE_MODEL", "GEMININONCE_PRICE"):
            print(f"warning: unknown environment variable {var} (did you mean GEMININONCE_ACCOUNT?)")
    root = Path(args.root).resolve()
    HOME.mkdir(parents=True, exist_ok=True)
    backup_dir = HOME / "backups" / time.strftime("%Y%m%d-%H%M%S")

    files = collect_files(root, args.paths)
    if not files:
        sys.exit("No readable files to send.")
    global PROJECT_LEXER
    exts = [Path(rel).suffix for rel in files if Path(rel).suffix]
    if exts:
        top = max(set(exts), key=exts.count)
        PROJECT_LEXER = lexer_for("", f"x{top}")
    size = sum(map(len, files.values()))
    print(f"Sending {len(files)} file(s), {size:,} chars.")
    if size > 400_000 and ask_user("That's a lot for a chat message. Continue? [y/N] ").lower() != "y":
        return 1
    if args.anonymous:
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
                    f"   the task, test output and these {len(files)} file(s):", YELLOW))
        print("\n".join(paint(f"     {rel}", YELLOW) for rel in files))
        if not args.yes and ask_user(paint("   Type 'yes' to continue: ", YELLOW, BOLD)).strip().lower() != "yes":
            print("Aborted; nothing was sent.")
            return 1

    test_out = ""
    if args.test:
        if not args.no_sandbox and not safety.sandbox_argv("true", root):
            print("warning: no sandbox available here (macOS sandbox-exec / Linux bwrap); "
                  "the test command will run Gemini's code unconfined.")
        code, test_out = run(args.test, root, args.timeout, sandbox=not args.no_sandbox)
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

    profile_dir, chrome_profile = Path(args.profile), None
    if args.anonymous:
        profile_dir = Path(tempfile.mkdtemp(prefix="gemininonce-anon-"))
    elif args.chrome_profile:
        chrome_profile = resolve_profile(args.chrome_profile)
        if args.profile == str(HOME / "profile"):
            profile_dir = HOME / "chrome"
    model = args.model or ("flash-lite" if args.anonymous else "flash")
    try:
        gemini = Gemini(profile_dir, args.cdp, args.account, chrome_profile,
                        None if model.lower() == "any" else model, args.show, args.anonymous)
    except BaseException:
        if args.anonymous:
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    passed, stalled, last_sig = False, 0, _signature(test_out)
    try:
        for i in range(1, args.max_iters + 1):
            print(paint(f"\n━━━ Round {i} " + "━" * 50, BLUE, BOLD))
            edits, commands, prose = ask_for_code(gemini, prompt, args.retries, args.verbose)
            print()
            notes = apply_edits(root, edits, backup_dir, set(files), args.allow_new_files)
            results = review_commands(commands, root, args.timeout, args.test)

            if not edits and not commands:
                reply = ask_user("\nNo file changes or commands. Your reply (empty to stop): ").strip()
                if not reply:
                    break
                prompt = f"{reply}\n\n{RULES}"
                continue

            if not args.test:
                reply = ask_user("\nApplied. Follow-up for Gemini (empty to finish): ").strip()
                if not reply:
                    break
                prompt = "\n\n".join([reply, *notes, *results, RULES])
                continue

            code, out = run(args.test, root, args.timeout, sandbox=not args.no_sandbox)
            if code == 0:
                print(paint(f"\n✔ Tests pass after {i} round(s).", GREEN, BOLD))
                passed = True
                break
            sig = _signature(out)
            stalled = stalled + 1 if sig == last_sig else 0
            last_sig = sig
            if stalled >= args.patience:
                print(paint(f"\n✘ No progress for {stalled} rounds (same test output); stopping.", RED, BOLD))
                break
            prompt = "\n\n".join([
                f"I applied your changes. `{args.test}` still fails (exit {code}). Output:\n{fenced(out)}",
                *notes, *results,
                "Fix it." if not stalled else "That did not change the failure at all. Try a different approach.",
                RULES,
            ])
        else:
            print(paint(f"\n✘ Gave up after {args.max_iters} rounds.", RED, BOLD))
    finally:
        if backup_dir.exists():
            print(f"Originals of modified files backed up in {backup_dir}")
        if gemini.turns:
            print(paint(usage_report(gemini.turns, gemini.model, args.price), DIM))
        gemini.close()
        if args.anonymous:
            shutil.rmtree(profile_dir, ignore_errors=True)
    return 0 if passed or not args.test else 1


def _signature(out: str) -> str:
    """Test output with timings, addresses and temp paths removed, to detect 'no progress'."""
    out = re.sub(r"\d+(\.\d+)?\s*(s|ms|sec|seconds)\b", "", out)
    out = re.sub(r"0x[0-9a-fA-F]+", "", out)
    return re.sub(r"/(?:private/)?(?:var|tmp)/\S+", "", out)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
