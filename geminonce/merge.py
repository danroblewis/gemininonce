"""Partial-edit merging.

When Gemini sends only the changed functions/classes instead of a whole file, splice each
definition into the old file by name. Block extents come from indentation (plus closing
brackets), so this works for Python and most brace languages without real parsing.
"""
from __future__ import annotations

import re

from .protocol import LAZY

DEF_RE = re.compile(
    r"^\s*(?:(?:export|default|public|private|protected|internal|static|async|override|final|abstract|"
    r"inline|virtual|unsafe|extern|pub(?:\([^)]*\))?)\s+)*"
    r"(?:(?:def|class|function\*?|func|fn|struct|enum|interface|trait|impl|type|module)\s+(?:\([^)]*\)\s*)?"
    r"(?P<a>[A-Za-z_$][\w$]*)|(?:const|let|var)\s+(?P<b>[A-Za-z_$][\w$]*)\s*=)")
PLACEHOLDER = re.compile(r"^\s*(?:#|//|/\*+|<!--|--|\*)\s*(?:\.\.\.|…)|^\s*(?:\.\.\.|…)\s*$")
IMPORT_RE = re.compile(r"^\s*(?:import|from|#include|use|using|require)\b")
COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*|<!--|--)")


def indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def def_name(line: str) -> str | None:
    """Name defined on this line (function, class, const, ...), if any."""
    m = DEF_RE.match(line)
    return m and (m.group("a") or m.group("b"))


def is_placeholder(line: str) -> bool:
    return bool(PLACEHOLDER.match(line) or LAZY.search(line))


def reindent(lines: list[str], delta: int) -> list[str]:
    if delta >= 0:
        return [(" " * delta + ln) if ln.strip() else ln for ln in lines]
    return [ln[min(-delta, indent(ln)):] for ln in lines]


class Outline:
    """A file's lines, with its definitions found by indentation."""

    def __init__(self, lines: list[str]):
        self.lines = lines

    def block(self, i: int) -> tuple[int, int]:
        """(start, end) of the definition whose header is lines[i], including decorators above it."""
        lines = self.lines
        ind, j = indent(lines[i]), i + 1
        while j < len(lines):
            s = lines[j].strip()
            if not s or indent(lines[j]) > ind:
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
        while s > 0 and lines[s - 1].strip().startswith(("@", "#[")) and indent(lines[s - 1]) == ind:
            s -= 1
        return s, j

    def defs(self, lo: int, hi: int) -> list[tuple[str, int, int, int]]:
        """Direct child definitions in lines[lo:hi] as (name, start, header, end)."""
        out, i = [], lo
        while i < hi:
            name = def_name(self.lines[i])
            if name:
                s, e = self.block(i)
                out.append((name, s, i, min(e, hi)))
                i = e
            else:
                i += 1
        return out

    def kids(self, lo: int, hi: int) -> set[str]:
        """Names of nested functions/classes (not local variables) directly inside lines[lo:hi]."""
        return {d[0] for d in self.defs(lo, hi) if DEF_RE.match(self.lines[d[2]]).group("a")}

    def find_anywhere(self, name: str) -> list[tuple[str, int, int, int]]:
        """Every definition of `name` at any depth, as (name, start, header, end)."""
        return [(name, bs, k, be) for k, ln in enumerate(self.lines) if def_name(ln) == name
                for bs, be in [self.block(k)]]

    def names(self) -> set[str]:
        return {n for ln in self.lines if (n := def_name(ln))}

    def has_placeholder(self) -> bool:
        return any(is_placeholder(ln) for ln in self.lines)


def looks_partial(old: str, new: str) -> bool:
    old_o, new_o = Outline(old.splitlines()), Outline(new.splitlines())
    if new_o.has_placeholder() and not old_o.has_placeholder():
        return True
    return bool(old_o.names() - new_o.names()) and len(new_o.lines) < 0.8 * len(old_o.lines)


def merge_partial(old_text: str, new_text: str) -> tuple[str, list[str]] | None:
    """Splice definitions from a snippet into old_text. None if it can't be done unambiguously."""
    old, new = Outline(old_text.splitlines()), Outline(new_text.splitlines())
    old_stripped = {ln.strip() for ln in old.lines}
    splices, log, imports = [], [], []

    def walk(nlo, nhi, olo, ohi) -> bool:
        i = nlo
        while i < nhi:
            line, name = new.lines[i], def_name(new.lines[i])
            if not name:
                s = line.strip()
                if IMPORT_RE.match(line) and s not in old_stripped:
                    imports.append(s)
                elif s and s not in old_stripped and not is_placeholder(line) and not COMMENT_RE.match(line) \
                        and not line.lstrip().startswith("@"):
                    return False  # a changed line outside any definition: can't place it safely
                i += 1
                continue
            s, e = new.block(i)
            e = min(e, nhi)
            cands = [d for d in old.defs(olo, ohi) if d[0] == name] or old.find_anywhere(name)  # maybe a method sent without its class
            if len(cands) > 1:
                return False
            if not cands:  # brand-new definition: add at the end of the enclosing scope
                pos = ohi
                if olo > 0 and pos > olo and old.lines[pos - 1].strip()[:1] in ("}", ")", "]") \
                        and indent(old.lines[pos - 1]) == indent(old.lines[olo - 1]):  # container's own closer
                    pos -= 1
                kids = old.defs(olo, ohi)
                target = indent(old.lines[kids[0][2]]) if kids else (0 if olo == 0 else indent(old.lines[olo - 1]) + 4)
                splices.append((pos, pos, [""] + reindent(new.lines[s:e], target - indent(new.lines[i]))))
                log.append(f"added {name}")
            else:
                _, os_, oh, oe = cands[0]
                block = new.lines[s:e]
                missing = old.kids(oh + 1, oe) - new.kids(i + 1, e)
                if any(is_placeholder(ln) for ln in block) or missing:
                    if not walk(i + 1, e, oh + 1, oe):  # container sent partially: recurse into it
                        return False
                else:
                    splices.append((os_, oe, reindent(block, indent(old.lines[oh]) - indent(new.lines[i]))))
                    log.append(f"replaced {name}")
            i = e
        return True

    if not walk(0, len(new.lines), 0, len(old.lines)) or not (splices or imports):
        return None
    splices.sort(key=lambda x: (x[0], x[1]))
    if any(a[1] > b[0] for a, b in zip(splices, splices[1:])):
        return None  # overlapping replacements
    result = old.lines
    for s, e, repl in reversed(splices):
        result[s:e] = repl
    if imports:
        last = max((k for k, ln in enumerate(result) if IMPORT_RE.match(ln) and not indent(ln)), default=-1)
        result[last + 1:last + 1] = imports
        log.append(f"added {len(imports)} import(s)")
    return "\n".join(result) + "\n", log
