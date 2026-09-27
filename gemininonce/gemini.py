"""Driving the Gemini web page: sign-in checks, the model picker, and sending messages."""
from __future__ import annotations

import re
import time

from playwright.sync_api import TimeoutError as PWTimeout

from . import HOME, safety
from .browser import Browser
from .console import DIM, YELLOW, paint
from .usage import Usage

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
SIGN_IN_URL = "https://accounts.google.com/ServiceLogin?continue=" + GEMINI_URL

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


class GeminiTimeout(TimeoutError):
    """Gemini didn't start or finish a reply in time (a screenshot is saved as last_error.png)."""


def model_key(name: str) -> str:
    """'3.6 Flash' -> 'flash', so a request survives version bumps."""
    return re.sub(r"^\d+(\.\d+)*\s*", "", name.strip().lower())


class GeminiChat:
    """One Gemini conversation in `browser`.

    account: required account (substring of the email), if any. anonymous: require signed out.
    model: picker name to force ('flash', 'pro', ...), or None to leave it alone.
    """

    def __init__(self, browser: Browser, account: str | None = None, model: str | None = None,
                 anonymous: bool = False):
        self.browser = browser
        self.required_account = account
        self.anonymous = anonymous
        self.model = model  # resolved to the picker's exact name by select_model()
        self.usage = Usage()
        self._connect()

    @property
    def page(self):
        return self.browser.page

    def _connect(self) -> None:
        self._load()
        if self.anonymous:
            self.check_signed_out()
            print(paint("Gemini: signed out (anonymous, free tier)", YELLOW))
        else:
            if self.browser.headless:  # a hidden browser can't be signed into: show a window only if needed
                email = self.account()
                if not (self._ok(email) or (email and self._find_account())):
                    print(paint("Opening a browser window so you can sign in...", YELLOW))
                    self.browser.show()
                    self._load()
            self.check_account(wait=True)
        if self.model:
            self.select_model(self.model)

    def new_chat(self) -> None:
        """Start a fresh conversation (no earlier messages as context)."""
        self._load()

    def _load(self) -> None:
        self.page.goto(GEMINI_URL)
        try:
            self.page.wait_for_selector(SEL_INPUT, timeout=15_000)
        except PWTimeout:
            if self.browser.headless:
                return  # the headless sign-in check will reopen visibly
            print("Log in to Gemini in the browser window (waiting up to 5 minutes)...")
            self.page.wait_for_url("https://gemini.google.com/**", timeout=300_000)
            self.page.wait_for_selector(SEL_INPUT, timeout=300_000)

    # --- account ------------------------------------------------------------------------------
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
        self.page.goto(SIGN_IN_URL)
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2)
            if not self.page.url.startswith("https://gemini.google.com"):
                continue  # still on the Google sign-in pages
            if (found := self._find_account()):
                print(f"Gemini account: {found}")
                return
            print(f"Still not signed in{' as ' + want if want else ''}; opening sign-in again...")
            self.page.goto(SIGN_IN_URL)
        raise SystemExit("Timed out waiting for Gemini sign-in; nothing was sent.")

    # --- model --------------------------------------------------------------------------------
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
        hits = [n for n in names if w in (n.lower(), model_key(n))] \
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
        if model_key(self.current_model()) != model_key(hits[0]):
            raise SystemExit(f"Tried to select {hits[0]!r} but Gemini shows {self.current_model()!r}")
        self.model = hits[0]
        print(paint(f"Gemini model: {hits[0]}", DIM))

    # --- messages -----------------------------------------------------------------------------
    def ask(self, text: str, timeout: float = 600) -> list[dict]:
        """Send a message and return the reply as text/code blocks (see EXTRACT_JS)."""
        page = self.page
        # re-check every time: a session can expire or switch mid-run
        self.check_signed_out() if self.anonymous else self.check_account()
        if self.model and model_key(self.current_model()) != model_key(self.model):
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
                self._timeout("Gemini never started a response (message not sent?)")
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
            self._timeout("Gemini did not finish responding in time")
        print()
        self.usage.record(len(text), len(prev or ""))

        body = last.locator(SEL_RESPONSE_BODY).first
        (HOME / "last_response.html").write_text(body.evaluate("e => e.outerHTML"))
        return body.evaluate(EXTRACT_JS)

    def _timeout(self, what: str):
        shot = HOME / "last_error.png"
        try:
            self.page.screenshot(path=str(shot))
            what += f" (screenshot: {shot})"
        except Exception:
            pass
        raise GeminiTimeout(what)
