"""End-to-end tests against live, signed-out (anonymous, free-tier) Gemini.

Skipped unless you pass --e2e:   pytest tests --e2e
They send only the bundled todo app and short test prompts; treat everything sent as public.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from geminonce import protocol
from geminonce.browser import Browser
from geminonce.gemini import GeminiChat

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def chat():
    profile = Path(tempfile.mkdtemp(prefix="geminonce-anon-"))
    browser = Browser(profile, headless=True)
    try:
        yield GeminiChat(browser, model="flash-lite", anonymous=True)
    finally:
        browser.close()
        shutil.rmtree(profile, ignore_errors=True)


def test_session_is_signed_out_on_flash_lite(chat):
    assert chat.account() is None
    chat.check_signed_out()  # would raise if signed in
    assert "flash-lite" in chat.current_model().lower()


def test_reply_round_trip(chat):
    chat.new_chat()
    blocks = chat.ask("Reply with exactly this and nothing else: a line `FILE: hello.txt` followed by a "
                      "fenced code block containing only the word hi")
    edits, commands, _ = protocol.parse_reply(blocks)
    assert [(path, text.strip()) for path, text in edits] == [("hello.txt", "hi")]
    assert chat.usage.turns and chat.usage.turns[-1][1] > 0


def test_models_unavailable_when_signed_out_are_refused(chat):
    with pytest.raises(SystemExit, match="isn't available here"):
        chat.select_model("flash")
    assert "flash-lite" in chat.current_model().lower()  # still usable afterwards


def test_cli_fixes_the_todo_app(todo_project):
    """The real command, start to finish: exit code 0 and the todo app's tests pass."""
    test_cmd = f"{sys.executable} -m pytest -x -q -p no:cacheprovider"
    env = {**os.environ, "NO_COLOR": "1"}
    run = subprocess.run([sys.executable, "-m", "geminonce", "todo", "tests", "-t", test_cmd, "--anonymous", "-y"],
                         cwd=todo_project, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         timeout=900)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    assert "Tests pass after" in run.stdout and "API-equivalent cost" in run.stdout
    assert subprocess.run(test_cmd, shell=True, cwd=todo_project, capture_output=True).returncode == 0
