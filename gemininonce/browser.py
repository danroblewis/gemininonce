"""Launching Chrome with Playwright: persistent profile, optional copy of your own Chrome profile,
hidden (headless) or visible."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from . import chrome_profiles


class Browser:
    """One Chrome window/context and its current page. Closes itself if it fails to start."""

    def __init__(self, profile_dir: Path, chrome_profile: str | None = None, headless: bool = True,
                 cdp: str | None = None):
        self.profile_dir = profile_dir
        self.headless = headless and not cdp
        self.ctx = None
        self.args = ["--disable-blink-features=AutomationControlled"]
        self.ignore_args = ["--enable-automation"]
        self.pw = sync_playwright().start()
        try:
            if cdp:
                self.ctx = self.pw.chromium.connect_over_cdp(cdp).contexts[0]
                self.page = self.ctx.new_page()
                return
            if chrome_profile:
                chrome_profiles.mirror(chrome_profile, profile_dir)
                self.args += [f"--profile-directory={chrome_profile}", "--no-first-run"]
                # Playwright's defaults hide the real macOS Keychain, so copied cookies wouldn't decrypt.
                self.ignore_args += ["--use-mock-keychain", "--password-store=basic"]
            self.launch()
        except BaseException:
            self.close()
            raise

    def launch(self) -> None:
        kw = dict(headless=self.headless, args=self.args, ignore_default_args=self.ignore_args,
                  viewport={"width": 1280, "height": 900} if self.headless else None)
        try:  # real Chrome is much less likely to be blocked at Google sign-in
            self.ctx = self.pw.chromium.launch_persistent_context(str(self.profile_dir), channel="chrome", **kw)
        except Exception:
            self.ctx = self.pw.chromium.launch_persistent_context(str(self.profile_dir), **kw)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._disguise(self.page)

    def _disguise(self, page) -> None:
        if self.headless:  # don't advertise "HeadlessChrome" to Google
            ua = page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            self.ctx.new_cdp_session(page).send("Emulation.setUserAgentOverride", {"userAgent": ua})

    def new_tab(self):
        """Another page in the same browser (same sign-in), e.g. for a separate reviewer conversation."""
        page = self.ctx.new_page()
        self._disguise(page)
        return page

    def show(self) -> None:
        """Reopen with a visible window (e.g. so the user can sign in)."""
        self.ctx.close()
        self.headless = False
        self.launch()

    def close(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            self.pw.stop()
