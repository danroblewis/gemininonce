"""Re-record the Gemini reply fixtures from a live *anonymous* session (no account involved).

    python tests/record_fixtures.py

Records into tests/fixtures/recorded/:
  session-NN.{json,html}  every round of a real fix loop on tests/fixtures/todo_project
  command / no_code / partial.{json,html}  single replies for specific behaviors
Each .json has the prompt we sent and the blocks EXTRACT_JS pulled out; each .html is the reply's
DOM (Angular noise and Google tracking attributes stripped). Fixtures are only replaced if the
recorded session ends with the tests passing.
"""
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
os.environ["GEMININONCE_HOME"] = tempfile.mkdtemp(prefix="gemininonce-record-")

from gemininonce import HOME, protocol  # noqa: E402
from gemininonce.browser import Browser  # noqa: E402
from gemininonce.gemini import GeminiChat  # noqa: E402
from gemininonce.loop import FixLoop  # noqa: E402
from gemininonce.transcript import Transcript  # noqa: E402
from gemininonce.workspace import Workspace  # noqa: E402

# Plain `python` (the venv's, via PATH) and a fixed pytest temp dir, so no personal paths (home
# directory, username) end up in the prompts we send or in the recorded fixtures.
os.environ["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
TEST_CMD = "python -m pytest -x -q -p no:cacheprovider --basetemp=/tmp/gemininonce-pytest"
PERSONAL = [str(Path.home()), Path.home().name]
NOISE_ATTRS = re.compile(r'\s(?:jslog|data-ved|decode-data-ved|data-hveid|_ngcontent-[\w-]+|_nghost-[\w-]+)="[^"]*"')

SINGLE_PROMPTS = {
    "command": "Running my script fails with `ModuleNotFoundError: No module named 'requests'`. "
               "I don't need any file changes, just the command to fix it.\n\n" + protocol.RULES,
    "no_code": "In one short sentence and without any code: what does the word 'nonce' mean?",
    "partial": "Fix `_next_id` in this file so ids stay unique after a todo is removed. Reply with "
               "`FILE: todo/service.py` and a code block containing ONLY the class line and the changed "
               "method, with a `# ... existing code ...` comment standing in for everything else.\n\n"
               "FILE: todo/service.py\n" + protocol.fenced((HERE / "fixtures/todo_project/todo/service.py").read_text()),
}


def clean_html(html: str) -> str:
    return NOISE_ATTRS.sub("", html.replace("<!---->", ""))


class Recorder:
    """Stands in for GeminiChat, saving every reply under name-NN (or `name` for single prompts)."""

    def __init__(self, chat: GeminiChat, outdir: Path):
        self.chat, self.outdir, self.name, self.count = chat, outdir, "session", 0

    def ask(self, text: str) -> list[dict]:
        blocks = self.chat.ask(text)
        self.count += 1
        stem = f"{self.name}-{self.count:02}" if self.name == "session" else self.name
        (self.outdir / f"{stem}.html").write_text(clean_html((HOME / "last_response.html").read_text()))
        (self.outdir / f"{stem}.json").write_text(json.dumps({"prompt": text, "blocks": blocks}, indent=1))
        return blocks

    def __getattr__(self, attr):
        return getattr(self.chat, attr)


def main() -> int:
    sys.stdin = open(os.devnull)  # answer every prompt "no", exactly like the replay test does
    outdir = Path(tempfile.mkdtemp(prefix="gemininonce-recorded-"))
    project = Path(tempfile.mkdtemp(prefix="gemininonce-proj-", dir="/tmp")).resolve() / "todo_project"
    shutil.copytree(HERE / "fixtures/todo_project", project, ignore=shutil.ignore_patterns("__pycache__"))
    profile = Path(tempfile.mkdtemp(prefix="gemininonce-anon-"))
    browser = Browser(profile, headless=True)
    try:
        chat = GeminiChat(browser, model="flash-lite", anonymous=True)
        rec = Recorder(chat, outdir)
        ws = Workspace(project, HOME / "backups")
        files = ws.collect([str(project / "todo"), str(project / "tests")])
        code, out = ws.run_test(TEST_CMD)
        prompt = protocol.initial_prompt("", protocol.test_result(TEST_CMD, code, out), files, ws.layout())
        passed = FixLoop(rec, ws, Transcript(), TEST_CMD).run(prompt, (code, out))
        if not passed:
            print("Recorded session did not end with passing tests; fixtures left unchanged.")
            return 1
        for name, text in SINGLE_PROMPTS.items():
            chat.new_chat()
            rec.name = name
            Transcript().reply(rec.ask(text))
        (outdir / "meta.json").write_text(json.dumps({
            "recorded": datetime.date.today().isoformat(), "model": chat.model,
            "session_replies": rec.count - len(SINGLE_PROMPTS), "test_command": TEST_CMD}, indent=1))
    finally:
        browser.close()
        shutil.rmtree(profile, ignore_errors=True)
    leaks = [(p.name, s) for p in outdir.iterdir() for s in PERSONAL if s in p.read_text()]
    if leaks:
        print(f"Refusing to save fixtures containing personal paths: {leaks}")
        return 1
    dest = HERE / "fixtures/recorded"
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(outdir, dest)
    print(f"Recorded {len(list(dest.glob('*.json'))) - 1} replies into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
