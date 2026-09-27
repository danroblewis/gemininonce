"""geminonce: a tiny fix-and-test loop that drives Gemini *web* via browser automation.

No API, no tool calls. We send files + test output as a chat message, ask Gemini to reply in a
strict format (FILE: <path> + full contents, COMMAND: + shell command), scrape the rendered reply
from the DOM, write the files, and re-run the test command until it passes.
"""
import os
import sys
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    """GEMINONCE_<name>, or the old GEMININONCE_<name> (from before the rename to geminonce)."""
    if (value := os.environ.get(f"GEMINONCE_{name}")) is not None:
        return value
    if (value := os.environ.get(f"GEMININONCE_{name}")) is not None:
        print(f"note: GEMININONCE_{name} is now GEMINONCE_{name}; please rename it", file=sys.stderr)
        return value
    return default


def _home() -> Path:
    """~/.geminonce, moving ~/.gemininonce (saved logins, the copied Chrome profile, backups) over once."""
    if (configured := env("HOME")):
        return Path(configured)
    new, old = Path.home() / ".geminonce", Path.home() / ".gemininonce"
    if not new.exists() and old.is_dir():
        try:
            old.rename(new)
            print(f"note: moved {old} to {new} (the project is now called geminonce)", file=sys.stderr)
        except OSError:
            return old
    return new


HOME = _home()
