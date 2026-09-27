"""Driving the Gemini web page: sign-in checks, the model picker, and sending messages."""
from __future__ import annotations

import re
import time

from playwright.sync_api import Error as PWError
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
  // Markdown turns `__init__.py` into <strong>init</strong>.py, so innerText loses the underscores.
  // In lines naming files (FILE:/READ:), put them back for emphasized words followed by a dot.
  const underscored = n => {
    const t = n.textContent, next = n.nextSibling ? n.nextSibling.textContent || '' : '';
    if (!/^\\w+$/.test(t) || !/^\\.\\w/.test(next)) return t;  // a file extension, not the end of a sentence
    return (n.tagName === 'STRONG' || n.tagName === 'B') ? '__' + t + '__' : '_' + t + '_';
  };
  const markerText = el => Array.from(el.childNodes).map(n => {
    if (n.nodeType === 3) return n.textContent;
    if (n.nodeType !== 1) return '';
    if (/^(STRONG|B|EM|I)$/.test(n.tagName)) return underscored(n);
    if (n.tagName === 'BR') return '\\n';
    return markerText(n) + (/^(P|LI|DIV)$/.test(n.tagName) ? '\\n' : '');
  }).join('');
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
        if (t && t.trim()) out.push({kind: 'text',
                                     text: /\\b(FILE|READ)\\b/.test(t) && c.querySelector('strong, b, em, i')
                                           ? markerText(c).trim() : t});
      }
    }
  };
  walk(root);
  return out;
}
"""


class GeminiTimeout(TimeoutError):
    """Gemini didn't start or finish a reply in time (a screenshot is saved as last_error.png)."""

# The rendered reply converted back to Markdown (headings, lists, code blocks, tables, inline code),
# for replies that ARE the document (the build's spec) rather than FILE: blocks. Citation chips are
# dropped.
EXTRACT_MD_JS = """
(root) => {
  const SKIP = new Set(['SOURCE-FOOTNOTE', 'SOURCE-INLINE-CHIP', 'SOURCES-CAROUSEL-INLINE', 'BUTTON', 'MAT-ICON']);
  const BLOCK = 'p, h1, h2, h3, h4, h5, h6, ul, ol, pre, code-block, table, blockquote, hr';
  const inlineNode = n => {
    if (n.nodeType === 3) return n.textContent;
    if (n.nodeType !== 1 || SKIP.has(n.tagName)) return '';
    const t = n.tagName;
    if (t === 'CODE') return '`' + n.textContent + '`';
    const next = n.nextSibling ? n.nextSibling.textContent || '' : '';
    const dunder = /^\\w+$/.test(n.textContent) && /^\\.\\w/.test(next);  // `__init__.py` rendered as bold
    if (t === 'STRONG' || t === 'B') return dunder ? '__' + n.textContent + '__' : '**' + inline(n) + '**';
    if (t === 'EM' || t === 'I') return dunder ? '_' + n.textContent + '_' : '*' + inline(n) + '*';
    if (t === 'BR') return '\\n';
    if (t === 'A' && /^https?:/.test(n.href || '')) {  // keep the address (references need it)
      const url = cleanUrl(n.href), text = inline(n).trim();
      return !text || text === url ? '<' + url + '>' : '[' + text + '](' + url + ')';
    }
    return inline(n);
  };
  const cleanUrl = h => {
    // Gemini sometimes links a source through a Google search for it: use the real address.
    const m = h.match(/^https?:\\/\\/(www\\.)?google\\.com\\/search\\?q=(https?[^&]+)/);
    if (m) h = decodeURIComponent(m[2]);
    return h.replace(/[?&]utm_source=gemini$/, '').replace(/([?&])utm_source=gemini&/, '$1');
  };
  const inline = el => Array.from(el.childNodes).map(inlineNode).join('');
  const fenced = el => {
    const code = el.querySelector('code') || el.querySelector('pre') || el;
    const label = el.querySelector('.code-block-decoration span');
    const lang = label ? label.innerText.trim().toLowerCase().replace(/\\s+/g, '') : '';
    const body = code.innerText.replace(/\\n$/, '');
    let fence = '```';
    while (body.includes(fence)) fence += '`';
    return fence + lang + '\\n' + body + '\\n' + fence;
  };
  const list = (ul, depth) => {
    let n = +(ul.getAttribute('start') || 1);
    const pad = '   '.repeat(depth), lines = [];
    for (const li of ul.children) {
      if (li.tagName !== 'LI') continue;
      let text = '';
      const extra = [];
      for (const c of li.childNodes) {
        if (c.nodeType === 1 && (c.tagName === 'UL' || c.tagName === 'OL')) extra.push(list(c, depth + 1));
        else if (c.nodeType === 1 && (c.tagName === 'PRE' || c.tagName === 'CODE-BLOCK' || c.querySelector('pre, code-block'))) {
          const inner = [];  // block content (e.g. a code block, maybe wrapped in other elements) under the item
          walk(c, inner);
          extra.push(inner.join('\\n\\n').split('\\n').map(l => pad + '   ' + l).join('\\n'));
        }
        else text += inlineNode(c) + (c.nodeType === 1 && c.tagName === 'P' ? ' ' : '');
      }
      const bullet = ul.tagName === 'OL' ? (n++) + '.' : '-';
      lines.push(pad + bullet + ' ' + text.trim().replace(/\\s*\\n\\s*/g, ' '), ...extra);
    }
    return lines.join('\\n');
  };
  const table = tb => {
    const rows = Array.from(tb.querySelectorAll('tr')).map(tr =>
      Array.from(tr.children).map(td => inline(td).trim().replace(/\\|/g, '\\\\|')));
    if (!rows.length) return '';
    const out = ['| ' + rows[0].join(' | ') + ' |', '|' + rows[0].map(() => ' --- |').join('')];
    for (const r of rows.slice(1)) out.push('| ' + r.join(' | ') + ' |');
    return out.join('\\n');
  };
  const walk = (el, out) => {
    if (el.tagName === 'PRE' || el.tagName === 'CODE-BLOCK') { out.push(fenced(el)); return; }
    for (const c of el.children) {
      const t = c.tagName;
      if (SKIP.has(t)) continue;
      if (/^H[1-6]$/.test(t)) out.push('#'.repeat(+t[1]) + ' ' + inline(c).trim());
      else if (t === 'P') { const s = inline(c).trim(); if (s) out.push(s); }
      else if (t === 'UL' || t === 'OL') out.push(list(c, 0));
      else if (t === 'PRE' || t === 'CODE-BLOCK') out.push(fenced(c));
      else if (t === 'TABLE') out.push(table(c));
      else if (t === 'BLOCKQUOTE') out.push(inline(c).trim().split('\\n').map(l => '> ' + l).join('\\n'));
      else if (t === 'HR') out.push('---');
      else if (c.querySelector(BLOCK)) walk(c, out);
      else { const s = inline(c).trim(); if (s) out.push(s); }
    }
  };
  const out = [];
  walk(root, out);
  // "[https://x](https://x)" (a link to itself, even inside inline code) reads better as just the address.
  return out.join('\\n\\n').replace(/\\[(https?:[^\\]\\s]+)\\]\\(\\1\\)/g, '$1') + '\\n';
}
"""


# External links in a reply (Gemini's citations when it searched the web), without Google's own links.
SOURCES_JS = """
(root) => {
  // Google's own search, sign-in and Gemini links, and its asset hosts; real docs (developers.google.com) count.
  const NOT_SOURCES = /^https?:\\/\\/(gemini\\.google\\.com|accounts\\.google\\.com|(www\\.)?google\\.com\\/(search|url)|[^/]*\\.gstatic\\.com|[^/]*\\.googleusercontent\\.com)/;
  return Array.from(new Set(Array.from(root.querySelectorAll('a[href]'))
  .map(a => a.href.replace(/[?&]utm_source=gemini$/, '').replace(/([?&])utm_source=gemini&/, '$1'))
  .filter(h => /^https?:/.test(h) && !NOT_SOURCES.test(h))));
}
"""


def model_key(name: str) -> str:
    """'3.6 Flash' -> 'flash', so a request survives version bumps."""
    return re.sub(r"^\d+(\.\d+)*\s*", "", name.strip().lower())


class GeminiChat:
    """One Gemini conversation in `browser`.

    account: required account (substring of the email), if any. anonymous: require signed out.
    model: picker name to force ('flash', 'pro', ...), or None to leave it alone.
    """

    def __init__(self, browser: Browser, account: str | None = None, model: str | None = None,
                 anonymous: bool = False, page=None, usage: Usage | None = None):
        self.browser = browser
        self.required_account = account
        self.anonymous = anonymous
        self.model = model  # resolved to the picker's exact name by select_model()
        self._page = page  # its own tab (e.g. a reviewer), or None for the browser's main page
        self.usage = usage or Usage()  # shared with other conversations for one cost total
        self.conversation = self.usage.new_conversation()
        self.last_markdown = ""  # the latest reply as Markdown (see EXTRACT_MD_JS)
        self.last_sources: list[str] = []  # web sources the latest reply cited (see SOURCES_JS)
        self._connect()

    @property
    def page(self):
        return self._page or self.browser.page

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
        self.conversation = self.usage.new_conversation()
        self._to_front()
        self._load()

    def _to_front(self) -> None:
        """With a visible browser (--show), show the tab of the conversation that's working right now. The
        reviewer's tab opens on top, so without this the writer's and implementer's tabs stay hidden."""
        if self.browser is not None and not self.browser.headless:
            try:
                self.page.bring_to_front()
            except PWError:
                pass

    def _load(self) -> None:
        for attempt in range(3):  # ride out a dropped connection
            try:
                self.page.goto(GEMINI_URL)
                break
            except PWError as e:
                if attempt == 2 or "net::" not in str(e):
                    raise
                print(paint(f"  network error loading Gemini ({str(e).splitlines()[0][:80]}); retrying...", YELLOW))
                time.sleep(3)
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
        """Send a message and return the reply as text/code blocks (see EXTRACT_JS). The same reply as
        Markdown is left in self.last_markdown."""
        page = self.page
        self._to_front()
        self.last_markdown, self.last_sources = "", []
        # re-check every time: a session can expire or switch mid-run
        self.check_signed_out() if self.anonymous else self.check_account()
        if self.model and model_key(self.current_model()) != model_key(self.model):
            print(f"  model changed to {self.current_model()!r}; switching back to {self.model!r}")
            self.select_model(self.model)
        text = safety.redact(text)  # last line of defense: never send secrets
        box = self._type_message(text)
        n = page.locator(SEL_RESPONSE).count()
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
        self.usage.record(len(text), len(prev or ""), self.conversation)

        body = last.locator(SEL_RESPONSE_BODY).first
        (HOME / "last_response.html").write_text(body.evaluate("e => e.outerHTML"))
        self.last_markdown = body.evaluate(EXTRACT_MD_JS)
        self.last_sources = body.evaluate(SOURCES_JS)
        return body.evaluate(EXTRACT_JS)

    TYPE_TIMEOUT_MS = 20_000

    def _type_message(self, text: str):
        """Put the message in Gemini's input box. If the box can't be typed into (something covering it,
        or the page stuck), reload the conversation once and try again; then give up with what the page shows."""
        blocker = ""
        for attempt in (1, 2):
            box = self.page.locator(SEL_INPUT).first
            try:
                box.click(timeout=self.TYPE_TIMEOUT_MS)
                box.fill(text, timeout=self.TYPE_TIMEOUT_MS)
                return box
            except PWError:  # includes timeouts
                if attempt == 2:
                    break
                blocker = self._page_blocker()
                print(paint(f"\n  Can't type into Gemini's message box{f' (page shows: {blocker})' if blocker else ''};"
                            " reloading the conversation...", YELLOW))
                try:
                    self.page.reload()
                    self.page.wait_for_selector(SEL_INPUT, timeout=self.TYPE_TIMEOUT_MS)
                except PWError:
                    break
        self._timeout("Couldn't type into Gemini's message box", blocker)

    def _page_blocker(self) -> str:
        """Text of whatever might be in the way: open dialogs, or notices about limits or signing in."""
        found = []
        try:
            for el in self.page.locator("[role=dialog], [role=alertdialog], mat-dialog-container, .cdk-overlay-pane").all():
                if el.is_visible() and (t := " ".join(el.inner_text().split())):
                    found.append(t)
            body = " ".join(self.page.locator("body").inner_text(timeout=5_000).split())
            found += re.findall(r"[^.!?]*\b(?:limit|sign in to continue|try again later|unavailable)\b[^.!?]*[.!?]?", body,
                                re.I)[:2]
        except PWError:
            pass
        return "; ".join(dict.fromkeys(f.strip() for f in found if f.strip()))[:300]

    def _timeout(self, what: str, seen_before: str = ""):
        """Raise GeminiTimeout, saying what the page shows (or showed before a reload), with a screenshot and
        the page's HTML saved."""
        if (blocker := self._page_blocker() or seen_before):
            what += f"; the page shows: {blocker}"
        saved = []
        try:
            self.page.screenshot(path=str(HOME / "last_error.png"))
            saved.append(str(HOME / "last_error.png"))
            (HOME / "last_error.html").write_text(self.page.content())
            saved.append(str(HOME / "last_error.html"))
        except Exception:
            pass
        raise GeminiTimeout(what + (f" (saved: {', '.join(saved)})" if saved else ""))
