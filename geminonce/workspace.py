"""The local project: which files we send, writing Gemini's edits, and running commands."""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import safety
from .console import BOLD, DIM, GREEN, RED, YELLOW, ask_user, edit_text, paint, paint_findings
from .highlight import Highlighter
from .merge import looks_partial, merge_partial
from .protocol import fenced

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".tox", ".mypy_cache"}
MAX_FILE_BYTES = 200_000
MAX_OUTPUT_CHARS = 8_000
MAX_LAYOUT_FILES = 300


def read_text(p: Path) -> str | None:
    """File contents, or None if it's too big, binary or unreadable."""
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


class Workspace:
    """Everything that touches the project under `root`.

    backup_dir keeps the pre-session original of every file we overwrite; `sandbox` confines the
    test command (see safety.sandbox_argv); `allow_new_files` skips asking before creating files.
    """

    def __init__(self, root: Path, backup_dir: Path, timeout: int = 300, sandbox: bool = True,
                 allow_new_files: bool = False, highlighter: Highlighter | None = None, network: bool = False):
        self.root = root
        self.backup_dir = backup_dir
        self.timeout = timeout
        self.sandbox = sandbox
        self.network = network  # whether the sandboxed test command may use the internet
        self.allow_new_files = allow_new_files
        self.hl = highlighter or Highlighter()
        self.known: set[str] = set()  # files the user chose to send; Gemini may re-read these freely
        self.written: list[str] = []  # files we changed, in order
        # Optional write filter: guard(rel) returns why `rel` may not be written, or None to allow it.
        # The build pipeline uses it to keep each stage to its own files (e.g. tests locked for coding).
        self.guard = None

    # --- files --------------------------------------------------------------------------------
    def collect(self, targets: list[str]) -> dict[str, str]:
        """{relative path: text} for the given files/directories, minus secrets, binaries and huge files."""
        files = {}
        for t in targets:
            p = Path(t).resolve()
            if not p.exists():
                sys.exit(f"{t}: no such file or directory (cwd is {Path.cwd()})")
            for f in sorted(list_dir(p) if p.is_dir() else [p]):
                if not f.resolve().is_relative_to(self.root):
                    sys.exit(f"{f} is outside --root {self.root}")
                rel = str(f.resolve().relative_to(self.root))
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
        self.known = set(files)
        return files

    def layout(self) -> str:
        """The project's file names (git-tracked, minus secrets), marking the ones we sent."""
        paths = sorted(str(f.relative_to(self.root)) for f in list_dir(self.root) if f.is_file())
        paths = [rel for rel in paths if not safety.PROTECTED.search(rel)]
        if not paths:
            return ""
        more = len(paths) - MAX_LAYOUT_FILES
        lines = [("* " if rel in self.known else "  ") + rel for rel in paths[:MAX_LAYOUT_FILES]]
        if more > 0:
            lines.append(f"  ... and {more} more")
        return "PROJECT LAYOUT (* = included below; ask for others with READ:)\n" + "\n".join(lines)

    def read_files(self, paths: list[str]) -> tuple[dict[str, str], list[str]]:
        """Contents of files Gemini asked for, plus notes about any we won't send. Files the user
        chose to send go out freely; anything else needs the user's OK; secrets never do."""
        files, notes = {}, []
        for rel in paths:
            p = (self.root / rel).resolve()
            if not p.is_relative_to(self.root):
                notes.append(f"`{rel}` is outside the project, so it wasn't sent.")
                continue
            rel = str(p.relative_to(self.root))
            if not p.is_file():
                notes.append(f"`{rel}` doesn't exist. Check the PROJECT LAYOUT for the real file names.")
                continue
            if safety.PROTECTED.search(rel):
                notes.append(f"`{rel}` holds secrets or credentials, so it won't be sent.")
                continue
            text = read_text(p)
            if text is None:
                notes.append(f"`{rel}` is binary or too large to send.")
                continue
            if safety.find_secrets(text):
                notes.append(f"`{rel}` contains what looks like a secret, so it won't be sent.")
                continue
            if rel not in self.known and ask_user(
                    f"  Gemini asks to read {rel}, which you didn't include. Send it? [y/N] ").strip().lower() != "y":
                print(paint(f"  NOT SENT {rel}", RED))
                notes.append(f"The user chose not to share `{rel}`. Work with the files you have.")
                continue
            print(paint(f"  sending {rel}", GREEN))
            self.known.add(rel)
            files[rel] = text
        return files, notes

    def current_files(self) -> dict[str, str]:
        """Current contents of every file the user chose to send (for starting a fresh conversation)."""
        return {rel: text for rel in sorted(self.known) if (text := read_text(self.root / rel)) is not None}

    def apply(self, edits: list[tuple[str, str]]) -> list[str]:
        """Write Gemini's edits after the checks; returns notes for Gemini about anything rejected."""
        notes = []
        for rel, content in edits:
            if (note := self._apply_one(rel, content)):
                notes.append(note)
        return notes

    def _apply_one(self, rel: str, content: str) -> str | None:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            print(paint(f"  REJECTED {rel}: outside project root", RED))
            return f"`{rel}` is outside the project; edit rejected."
        if self.guard and (why := self.guard(str(p.relative_to(self.root)))):
            print(paint(f"  REJECTED {rel}: {why}", RED))
            return f"`{rel}` was not written: {why}."
        if not content.endswith("\n"):
            content += "\n"
        old = p.read_text() if p.exists() else None
        if old is None and not self.allow_new_files and \
                ask_user(f"  Gemini wants to create a NEW file {rel}. Create it? [y/N] ").strip().lower() != "y":
            print(paint(f"  REJECTED {rel}: new file not approved", RED))
            return (f"`{rel}` does not exist, so it was not created. Edit the existing files instead: "
                    + ", ".join(sorted(self.known)) + ". Only create a new file if it's truly needed.")
        if old is not None and looks_partial(old, content):
            merged = merge_partial(old, content)
            if merged and p.suffix == ".py":
                try:
                    compile(merged[0], rel, "exec")
                except SyntaxError:
                    merged = None
            if not merged:
                print(paint(f"  REJECTED {rel}: partial edit that couldn't be merged", RED))
                return f"Your `{rel}` was partial and couldn't be merged. Send the COMPLETE file."
            content = merged[0]
            print(f"  merged partial edit into {rel}: {', '.join(merged[1])}")
        if old == content:
            return None
        findings = safety.scan_edit(rel, old, content)
        if findings:
            high = [x for x in findings if x[0] == "high"]
            print(f"  Safety check on {rel}:\n{paint_findings(safety.format_findings(findings))}")
            self._show_flagged_lines(rel, content, high or findings)
            while high:
                answer = ask_user(f"  Write {rel} anyway? [y/N/v = view the whole file] ").strip().lower()
                if answer == "v":
                    print(self.hl.code(content.rstrip("\n"), self.hl.lexer_for(content, rel)))
                    continue
                if answer == "y":
                    break
                print(paint(f"  REJECTED {rel}: failed safety check", RED))
                return (f"Your change to `{rel}` was rejected by a safety check: "
                        + "; ".join(sorted({x[1] for x in high}))
                        + ". Do not do that; solve the problem without it.")
        self._write(rel, p, old, content)
        return None

    def _show_flagged_lines(self, rel: str, content: str, findings, context: int = 4) -> None:
        """The code around each flagged line (numbered, the flagged line marked), so it's clear what it
        does and where its values come from."""
        lines = content.splitlines()
        flagged = sorted({n for _, _, n, _ in findings if 0 < n <= len(lines)})
        shown_to = 0
        for n in flagged:
            start, end = max(n - context, shown_to + 1), min(n + context, len(lines))
            if start > end:
                continue
            print(paint(f"    {rel}, lines {start}-{end}:", DIM))
            code = self.hl.code("\n".join(lines[start - 1:end]), self.hl.lexer_for(content, rel)).splitlines()
            for i, ln in zip(range(start, end + 1), code):
                mark = paint(">", RED, BOLD) if i in flagged else " "
                print(f"    {mark} {paint(f'{i:4}', DIM)}  {ln}")
            shown_to = end

    def _write(self, rel: str, p: Path, old: str | None, content: str) -> None:
        if old is not None:
            backup = self.backup_dir / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():  # keep the pre-session original
                shutil.copy2(p, backup)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        if rel not in self.written:
            self.written.append(rel)
        self.known.add(rel)  # Gemini wrote it, so it may read it back
        diff = list(difflib.unified_diff((old or "").splitlines(), content.splitlines(), lineterm="", n=0))
        plus = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        minus = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        print(paint(f"  {'M' if old is not None else 'A'} {rel}", GREEN) + f"  (+{plus} -{minus})")

    # --- commands -----------------------------------------------------------------------------
    def run(self, cmd: str, sandbox: bool = False, show: bool = True) -> tuple[int, str]:
        """Run cmd from the project root; show=False runs it quietly (the caller presents the result)."""
        argv = safety.sandbox_argv(cmd, self.root, self.network) if sandbox else None
        label = ("   [sandboxed" + (", network allowed]" if self.network else "]")) if argv else ""
        if show:
            print(paint(f"$ {cmd}", BOLD) + paint(label, DIM))
        try:
            r = subprocess.run(argv or cmd, shell=argv is None, cwd=self.root, capture_output=True, text=True,
                               timeout=self.timeout)
            code, out = r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            code, out = -1, f"{e.stdout or ''}{e.stderr or ''}\n[timed out after {self.timeout}s]"
        if len(out) > MAX_OUTPUT_CHARS:
            out = "[...truncated...]\n" + out[-MAX_OUTPUT_CHARS:]
        if show:
            print(self.hl.output("\n".join(out.splitlines()[-40:])))
            print(paint(f"[exit {code}]", GREEN if code == 0 else RED, BOLD))
        return code, out

    def run_test(self, cmd: str) -> tuple[int, str]:
        """The test command runs Gemini's code, so it's sandboxed unless that was turned off."""
        return self.run(cmd, sandbox=self.sandbox)

    def review_commands(self, commands: list[str], test_cmd: str | None) -> list[str]:
        """Show each suggested command for approve/modify/skip; returns what happened, for Gemini."""
        results = []
        for cmd in commands:
            if cmd == test_cmd:
                continue  # the loop runs the test itself
            print(paint("\nGemini suggests running:", YELLOW, BOLD) + "\n"
                  + "\n".join(paint("    " + line, BOLD) for line in cmd.splitlines()))
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
                    print(paint("  WARNING, edited command:", RED) + "\n"
                          + paint_findings(safety.format_findings(findings)))
            if a == "s" or not cmd:
                results.append(f"User skipped command:\n{fenced(cmd)}")
                continue
            code, out = self.run(cmd)
            results.append(f"User ran:\n{fenced(cmd)}\nExit code {code}, output:\n{fenced(out)}")
        return results
