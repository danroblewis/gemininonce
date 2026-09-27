"""The fix-and-test loop: ask Gemini, apply its edits, run the test, repeat."""
from __future__ import annotations

import re
import sys
import time

from . import protocol
from .console import BLUE, BOLD, CYAN, DIM, GREEN, RED, YELLOW, ask_user, paint
from .gemini import GeminiChat, GeminiTimeout
from .transcript import Transcript, sources_line
from .workspace import Workspace

MAX_READS_PER_ROUND = 5  # replies that only ask for files, answered before we count it as "no code"
MORE_ROUNDS = 5  # rounds added when the user says to keep going


def signature(out: str) -> str:
    """Test output with timings, addresses and temp paths removed, to detect 'no progress'."""
    out = re.sub(r"\d+(\.\d+)?\s*(s|ms|sec|seconds)\b", "", out)
    out = re.sub(r"0x[0-9a-fA-F]+", "", out)
    return re.sub(r"/(?:private/)?(?:var|tmp)/\S+", "", out)


class FixLoop:
    """Rounds of Gemini edits until `test` passes (or, with no test, until the user stops).

    retries: re-asks per round when a reply has no code. patience: rounds without progress (a test
    failure we've already seen, or no edits) before asking the user for help, or stopping if there's
    no terminal to ask on. max_iters: hard cap on rounds. message: the user's task, for fresh starts.
    """

    def __init__(self, chat: GeminiChat, workspace: Workspace, transcript: Transcript, test: str | None = None,
                 retries: int = 3, patience: int = 3, max_iters: int = 20, message: str = ""):
        self.chat = chat
        self.ws = workspace
        self.transcript = transcript
        self.test = test
        self.retries = retries
        self.patience = patience
        self.max_iters = max_iters
        self.message = message
        self.unanswered_reads: list[str] = []  # READ: requests that came with edits; answered next message
        self.last_prose = ""

    def _exchange(self, msg: str) -> list[dict]:
        """Send msg and return the reply's blocks. A Gemini timeout counts as a reply with no code,
        so it gets retried like one."""
        self.transcript.outgoing(msg)
        try:
            blocks = self.chat.ask(msg)
        except GeminiTimeout as e:
            print(paint(f"\n  {e}", YELLOW))
            return []
        self.transcript.reply(blocks)
        if (sources := getattr(self.chat, "last_sources", None)):  # it searched the web
            print(paint(f"  {sources_line(sources)}", CYAN))
        usage = self.chat.usage
        print(paint(f"  $ {usage.step(self.chat.model, len(usage.turns) - 1, 'this reply')}", GREEN))
        return blocks

    def ask_for_code(self, prompt: str):
        """Ask until the reply has edits or commands: serve READ: requests, and retry replies with no code
        (Gemini errors, refusals, chatter). Returns (edits, commands, prose)."""
        msg, attempt, reads_left = prompt, 0, MAX_READS_PER_ROUND
        while True:
            blocks = self._exchange(msg)
            edits, commands, prose = protocol.parse_reply(blocks)
            reads = protocol.file_requests(blocks)
            self.last_prose = prose
            if reads and not edits and not commands and reads_left:
                reads_left -= 1
                files, notes = self.ws.read_files(reads)
                msg = protocol.read_reply(files, notes)
                continue
            self.unanswered_reads = reads
            if edits or commands or attempt == self.retries:
                return edits, commands, prose
            attempt += 1
            text = prose.strip()
            if len(text) < 300 or protocol.GEMINI_ERROR.search(text):
                print(paint(f"  Gemini replied without code; resending ({attempt}/{self.retries})", YELLOW))
                time.sleep(5)
                msg = prompt
            else:
                print(paint(f"  Gemini replied without code; asking for files ({attempt}/{self.retries})", YELLOW))
                msg = protocol.NO_CODE_NUDGE

    def _requested_files_notes(self) -> list[str]:
        """Answer READ: requests that arrived alongside edits, as extra sections of the next message."""
        if not self.unanswered_reads:
            return []
        files, notes = self.ws.read_files(self.unanswered_reads)
        self.unanswered_reads = []
        return [s for s in [protocol.requested_files(files)] if s] + notes

    def ask_user_for_help(self, reason: str, rounds: int, code: int | None, out: str | None,
                          next_prompt: str) -> tuple[str, int, bool] | None:
        """Gemini is stuck (or used up its rounds): show where things stand and let the user steer.
        Returns (next message for Gemini, rounds to add, whether to hold off asking again for those rounds),
        or None to stop. `next_prompt` is what would
        be sent if the user just lets it keep going. Without a terminal (scripts, CI) this just stops."""
        print(paint(f"\n✋ {reason}.", YELLOW, BOLD))
        if not sys.stdin.isatty():
            return None
        print(f"   Rounds so far: {rounds}; files changed: {', '.join(self.ws.written) or 'none'}")
        if out:
            print("   Last test output:")
            print("\n".join(paint("     " + ln, DIM) for ln in out.strip().splitlines()[-6:]))
        if self.last_prose.strip():
            print("   Gemini's last message:")
            print("\n".join(paint("     " + ln, DIM) for ln in self.last_prose.strip().splitlines()[:4]))
        print(paint("   What next?\n"
                    "     type a message   sent to Gemini as a hint, then it keeps going\n"
                    f"     /more [N]        keep going as is for N more rounds (default {MORE_ROUNDS})\n"
                    "     /new             fresh Gemini conversation: it forgets this chat and starts over with\n"
                    "                      the current files and test output (helps when it's going in circles)\n"
                    "     Enter or /quit   stop here", YELLOW))
        while True:
            answer = ask_user(paint("   > ", YELLOW)).strip()
            if answer in ("", "/quit"):
                return None
            if answer.startswith("/more"):
                n = answer[len("/more"):].strip()
                if n and not n.isdigit():
                    print(paint("   /more takes a number of rounds, e.g. /more 10", YELLOW))
                    continue
                return next_prompt, int(n or MORE_ROUNDS), True
            if answer == "/new":
                print(paint("   Starting a fresh conversation...", DIM))
                self.chat.new_chat()
                test_out = protocol.test_result(self.test, code, out) if self.test and out else ""
                return protocol.initial_prompt(self.message, test_out, self.ws.current_files(),
                                               self.ws.layout()), MORE_ROUNDS, False
            if answer.startswith("/"):
                print(paint(f"   Unknown command {answer.split()[0]}; see the choices above.", YELLOW))
                continue
            return protocol.hint_prompt(answer, self.test, code, out), MORE_ROUNDS, False

    def run(self, prompt: str, first: tuple[int, str] | None = None) -> bool:
        """first: (exit code, output) of the test run before round 1. Returns True if the test passed."""
        code, out = first or (None, None)
        seen = {signature(out)} if out else set()
        stalled, i, limit = 0, 0, self.max_iters
        ask_after = 0  # after "/more N", don't interrupt again before round i + N
        while True:
            if i >= limit:  # out of rounds: ask (at a terminal) whether to keep going
                choice = self.ask_user_for_help(f"Used all {limit} rounds (-n) without the tests passing",
                                                i, code, out, prompt)
                if choice is None:
                    print(paint(f"\n✘ Gave up after {i} rounds.", RED, BOLD))
                    return False
                prompt, more, hold = choice
                limit, stalled, ask_after = i + more, 0, i + more if hold else 0
            i += 1
            print(paint(f"\n━━━ Round {i} " + "━" * 50, BLUE, BOLD))
            round_start = len(self.chat.usage.turns)
            edits, commands, _ = self.ask_for_code(prompt)
            if len(self.chat.usage.turns) - round_start > 1:  # several messages this round: subtotal
                print(paint(f"  $ {self.chat.usage.step(self.chat.model, round_start, f'round {i}')}", GREEN, BOLD))
            print()
            notes = self.ws.apply(edits)
            results = self.ws.review_commands(commands, self.test)
            notes += self._requested_files_notes()

            if not edits and not commands and i < ask_after:
                continue  # the user said to keep going; resend the same message
            if not edits and not commands:
                choice = self.ask_user_for_help("Gemini seems stuck: its replies had no file changes", i, code, out,
                                                prompt)
                if choice is None:
                    return False
                prompt, more, hold = choice
                limit, stalled, ask_after = max(limit, i + more), 0, i + more if hold else 0
                continue

            if not self.test:
                reply = ask_user("\nApplied. Follow-up for Gemini (empty to finish): ").strip()
                if not reply:
                    return False
                prompt = protocol.reply_prompt(reply, notes, results)
                continue

            code, out = self.ws.run_test(self.test)
            if code == 0:
                print(paint(f"\n✔ Tests pass after {i} round(s).", GREEN, BOLD))
                return True
            sig = signature(out)
            stalled = stalled + 1 if sig in seen else 0  # same failure again, or back to an earlier one
            seen.add(sig)
            prompt = protocol.failure_prompt(self.test, code, out, notes, results, bool(stalled))
            if stalled >= self.patience and i >= ask_after:
                choice = self.ask_user_for_help(
                    f"Gemini seems stuck: no progress for {stalled} rounds (the same test failures)",
                    i, code, out, prompt)
                if choice is None:
                    return False
                prompt, more, hold = choice
                limit, stalled, ask_after = max(limit, i + more), 0, i + more if hold else 0
