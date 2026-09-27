"""Terminal helpers: ANSI colors and prompts that survive a non-interactive stdin."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
BOLD, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "1", "2", "31", "32", "33", "34", "95", "36"


def paint(text: str, *codes: str) -> str:
    return f"\033[{';'.join(codes)}m{text}\033[0m" if COLOR and codes else text


def paint_findings(text: str) -> str:
    return "\n".join(paint(ln, RED if "[HIGH]" in ln else YELLOW) for ln in text.splitlines())


def ask_user(msg: str) -> str:
    """input() that treats a closed stdin (non-interactive run) as an empty answer."""
    try:
        return input(msg)
    except EOFError:
        print("(no input available)")
        return ""


def edit_text(text: str) -> str:
    """Let the user edit text: inline with readline for one line, $EDITOR for several."""
    if "\n" in text:
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
