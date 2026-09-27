import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Isolate tests from the user's ~/.geminonce and shell settings. HOME is read at import time,
# so this has to happen before anything imports geminonce.
_TEST_HOME = tempfile.mkdtemp(prefix="geminonce-test-home-")
os.environ["GEMINONCE_HOME"] = _TEST_HOME
for _var in ("ACCOUNT", "CHROME_PROFILE", "MODEL", "PRICE"):
    os.environ.pop(f"GEMINONCE_{_var}", None)
    os.environ.pop(f"GEMININONCE_{_var}", None)  # the old name still works, so clear it too

FIXTURES = Path(__file__).parent / "fixtures"
collect_ignore = ["fixtures"]  # the todo app's own tests fail on purpose
RECORDED = FIXTURES / "recorded"


def pytest_addoption(parser):
    parser.addoption("--e2e", action="store_true", help="also run tests against live anonymous Gemini")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: talks to live anonymous Gemini (run with --e2e)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--e2e"):
        return
    skip = pytest.mark.skip(reason="live Gemini test; run with --e2e")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


def pytest_unconfigure(config):
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


@pytest.fixture
def todo_project(tmp_path) -> Path:
    """A fresh copy of the buggy todo app (7 of its 10 tests fail)."""
    dest = tmp_path.resolve() / "todo_project"
    shutil.copytree(FIXTURES / "todo_project", dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def load_recording(name: str) -> dict:
    """A recorded reply: {"prompt": ..., "blocks": [...]} plus its HTML under "html"."""
    data = json.loads((RECORDED / f"{name}.json").read_text())
    data["html"] = (RECORDED / f"{name}.html").read_text()
    return data


def session_rounds() -> list[str]:
    return sorted(p.stem for p in RECORDED.glob("session-*.json"))
