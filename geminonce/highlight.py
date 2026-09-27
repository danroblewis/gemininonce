"""Syntax highlighting with Pygments (optional): code by language, test output line by line."""
from __future__ import annotations

import re
from pathlib import Path

from . import console
from .console import BOLD, CYAN, DIM, GREEN, RED, paint

try:
    from pygments import highlight
    from pygments.formatters import Terminal256Formatter
    from pygments.lexers import (BashLexer, DiffLexer, get_lexer_by_name, get_lexer_for_filename,
                                 guess_lexer)
    from pygments.util import ClassNotFound
except ImportError:  # highlighting is optional
    highlight = None

DIFF_RE = re.compile(r"^(@@ .* @@|--- \S|\+\+\+ \S)", re.M)


def _is_diff(text: str) -> bool:
    return bool(DIFF_RE.search(text) and re.search(r"^[+-]", text, re.M))


class Highlighter:
    """project_lexer is the project's main language, used for source lines quoted in test output."""

    def __init__(self, project_lexer=None):
        self.project_lexer = project_lexer

    @classmethod
    def for_files(cls, paths) -> Highlighter:
        """Use the most common file extension among the files we send as the project's language."""
        exts = [Path(p).suffix for p in paths if Path(p).suffix]
        if not exts:
            return cls()
        return cls(cls.lexer_for("", f"x{max(set(exts), key=exts.count)}"))

    @staticmethod
    def lexer_for(code: str, path: str | None = None, label: str | None = None):
        """Pick a Pygments lexer from a file path, Gemini's code-block label, or by content."""
        if not highlight:
            return None
        if _is_diff(code):
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

    @staticmethod
    def shell_lexer():
        return BashLexer() if highlight else None

    @staticmethod
    def code(text: str, lexer) -> str:
        if not (console.COLOR and highlight and lexer):
            return text
        return highlight(text, lexer, Terminal256Formatter(style="monokai")).rstrip("\n")

    def output(self, text: str) -> str:
        """Color test/command output: diffs as diffs, otherwise line by line (errors, passes, locations,
        and quoted source lines in the project's language)."""
        if not console.COLOR:
            return text
        if highlight and _is_diff(text):
            return self.code(text, DiffLexer())
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
            elif self.project_lexer and re.match(r"(>|\s{4,}|\t)\s*\S", ln):
                lead = re.match(r"(>\s*|\s*)", ln).group(1)
                out.append(paint(lead, DIM) + self.code(ln[len(lead):], self.project_lexer))
            else:
                out.append(paint(ln, DIM))
        return "\n".join(out)
