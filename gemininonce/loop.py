"""The fix-and-test loop: ask Gemini, apply its edits, run the test, repeat."""
from __future__ import annotations

import re
import time

from . import protocol
from .console import BLUE, BOLD, GREEN, RED, YELLOW, ask_user, paint
from .gemini import GeminiChat
from .transcript import Transcript
from .workspace import Workspace


def signature(out: str) -> str:
    """Test output with timings, addresses and temp paths removed, to detect 'no progress'."""
    out = re.sub(r"\d+(\.\d+)?\s*(s|ms|sec|seconds)\b", "", out)
    out = re.sub(r"0x[0-9a-fA-F]+", "", out)
    return re.sub(r"/(?:private/)?(?:var|tmp)/\S+", "", out)


class FixLoop:
    """Rounds of Gemini edits until `test` passes (or, with no test, until the user stops).

    retries: re-asks per round when a reply has no code. patience: rounds with unchanged test
    output before giving up. max_iters: hard cap on rounds.
    """

    def __init__(self, chat: GeminiChat, workspace: Workspace, transcript: Transcript, test: str | None = None,
                 retries: int = 3, patience: int = 3, max_iters: int = 20):
        self.chat = chat
        self.ws = workspace
        self.transcript = transcript
        self.test = test
        self.retries = retries
        self.patience = patience
        self.max_iters = max_iters

    def ask_for_code(self, prompt: str):
        """Ask, retrying when the reply has no files or commands (Gemini errors, refusals, chatter)."""
        msg = prompt
        for attempt in range(self.retries + 1):
            self.transcript.outgoing(msg)
            blocks = self.chat.ask(msg)
            self.transcript.reply(blocks)
            edits, commands, prose = protocol.parse_reply(blocks)
            if edits or commands or attempt == self.retries:
                return edits, commands, prose
            text = prose.strip()
            if len(text) < 300 or protocol.GEMINI_ERROR.search(text):
                print(paint(f"  Gemini replied without code; resending ({attempt + 1}/{self.retries})", YELLOW))
                time.sleep(5)
                msg = prompt
            else:
                print(paint(f"  Gemini replied without code; asking for files ({attempt + 1}/{self.retries})", YELLOW))
                msg = protocol.NO_CODE_NUDGE
        return edits, commands, prose

    def run(self, prompt: str, first_test_output: str = "") -> bool:
        """Returns True if the test passed."""
        stalled, last_sig = 0, signature(first_test_output)
        for i in range(1, self.max_iters + 1):
            print(paint(f"\n━━━ Round {i} " + "━" * 50, BLUE, BOLD))
            edits, commands, _ = self.ask_for_code(prompt)
            print()
            notes = self.ws.apply(edits)
            results = self.ws.review_commands(commands, self.test)

            if not edits and not commands:
                reply = ask_user("\nNo file changes or commands. Your reply (empty to stop): ").strip()
                if not reply:
                    return False
                prompt = protocol.reply_prompt(reply)
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
            stalled = stalled + 1 if sig == last_sig else 0
            last_sig = sig
            if stalled >= self.patience:
                print(paint(f"\n✘ No progress for {stalled} rounds (same test output); stopping.", RED, BOLD))
                return False
            prompt = protocol.failure_prompt(self.test, code, out, notes, results, bool(stalled))
        print(paint(f"\n✘ Gave up after {self.max_iters} rounds.", RED, BOLD))
        return False
