"""Printing both sides of the conversation to the console."""
from __future__ import annotations

import re

from .console import BOLD, CYAN, DIM, MAGENTA, YELLOW, paint
from .highlight import Highlighter
from .protocol import MARKER, RULES, markers


# "PROJECT FILES:" / "REQUESTED FILES:" followed by FILE: + fenced-block pairs, and the layout listing.
FILES_SECTION = re.compile(r"^(PROJECT|REQUESTED) FILES:\n\n((?:FILE: \S+\n(`{3,})[^\n]*\n.*?\n\3(?:\n\n|\n?\Z))+)",
                           re.S | re.M)
LAYOUT_SECTION = re.compile(r"^PROJECT LAYOUT[^\n]*\n((?:[ *] [^\n]*(?:\n|\Z))+)", re.M)


class Transcript:
    """verbose prints code and outputs in full instead of shortening them. preview: how many lines of a
    long code block to show (0 = just a one-line summary, for files that get shown in full elsewhere)."""

    def __init__(self, verbose: bool = False, highlighter: Highlighter | None = None, preview: int = 12):
        self.verbose = verbose
        self.hl = highlighter or Highlighter()
        self.preview = preview

    def outgoing(self, msg: str) -> None:
        """Print our message: rules dropped, attached files listed by name, long outputs shortened."""
        msg = msg.replace(RULES, "").strip()
        msg = LAYOUT_SECTION.sub(lambda m: f"[project layout: {len(m.group(1).splitlines())} files listed]", msg)
        msg = FILES_SECTION.sub(self._files_summary, msg)
        if not self.verbose:  # the test output was just printed; show only the ends of long fenced blocks
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
                print(self.hl.output(part))

    @staticmethod
    def _files_summary(m: re.Match) -> str:
        names = re.findall(r"^FILE: (\S+)\n`{3,}", m.group(2), re.M)
        label = "files attached" if m.group(1) == "PROJECT" else "requested files sent"
        return f"[{len(names)} {label}: {', '.join(names)}]\n\n"

    def reply(self, blocks: list[dict]) -> None:
        print(paint("\n── Gemini " + "─" * 50, MAGENTA, BOLD))
        pending = None
        for b in blocks:
            if b["kind"] == "text":
                for ln in b["text"].splitlines():
                    print(paint(ln, YELLOW, BOLD) if MARKER.match(ln) else paint(ln, MAGENTA))
                for marker in markers(b["text"]):
                    if marker[0] != "READ":
                        pending = marker
            else:
                self._code_block(b, *(pending or (None, None)))
                pending = None

    def _code_block(self, block: dict, kind: str | None, path: str | None) -> None:
        code = block["text"].rstrip("\n")
        lexer = self.hl.shell_lexer() if kind == "COMMAND" else \
            self.hl.lexer_for(code, path if kind == "FILE" else None, block.get("lang"))
        lines = code.splitlines()
        if self.preview == 0 and not self.verbose and kind == "FILE":
            print(paint(f"  │ ({len(lines)} lines)", DIM))
            return
        more = len(lines) - self.preview if not self.verbose and len(lines) > self.preview + 8 else 0
        if more:
            lines = lines[:self.preview]
        print("\n".join(paint("  │ ", DIM) + ln for ln in self.hl.code("\n".join(lines), lexer).splitlines()))
        if more:
            print(paint(f"  │ ... {more} more lines (-v shows all)", DIM))
