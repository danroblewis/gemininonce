# Development

## Installing from a clone

```sh
git clone https://github.com/danroblewis/geminonce
cd geminonce && uv venv && uv pip install -e . pytest
```

Use an **editable** install (`-e`). A plain `pip install .` copies the code, so later changes (and branch switches) won't take effect until you reinstall.

## Updating

```sh
uv tool install --reinstall git+https://github.com/danroblewis/geminonce   # installed from GitHub
git pull                                                                      # editable install from a clone
```

`--reinstall` makes uv fetch the latest commit instead of reusing the copy it already has. With
uvx, keep `--refresh-package geminonce` in the command (see below).

## Running with uvx (no install)

[uv](https://docs.astral.sh/uv/)'s `uvx` runs it in a temporary environment:

```sh
# from a local checkout
uvx --refresh-package geminonce --from ~/geminonce geminonce src/ tests/ -t "pytest -x"

# from GitHub
uvx --refresh-package geminonce --from git+https://github.com/danroblewis/geminonce geminonce src/ tests/ -t "pytest -x"

# with your own Chrome login and a required account
uvx --refresh-package geminonce --from ~/geminonce geminonce src/ tests/ -t "pytest -x" \
  --chrome-profile Default --account @yourcompany.com
```

`--refresh-package geminonce` makes uv rebuild from the current source. Without it, uv keeps
running the cached copy and ignores your edits. It re-checks only this package, so it's still fast.

It uses the Google Chrome you already have installed, so there's nothing else to download. Without
Chrome, run `uvx playwright install chromium` once.

For a short command, add an alias to `~/.zshrc`:

```sh
alias gn='uvx --refresh-package geminonce --from ~/geminonce geminonce'
# gn src/ tests/ -t "pytest -x"
```

On the first run a Chrome window opens. Log in to Gemini there. The login is saved in
`~/.geminonce/profile`.

## Tests

```sh
pytest tests          # offline, a few seconds
pytest tests --e2e    # also runs the live tests against anonymous Gemini (~1.5 min)
```

- **Unit tests** (`test_geminonce.py`) cover merging, safety checks, parsing, applying edits and the
  cost estimate. `test_pipeline.py` runs `geminonce build` against a scripted fake Gemini.
- **Recorded-reply tests** (`test_recorded.py`) use real replies saved from an anonymous Gemini
  session in `tests/fixtures/recorded/`. They check that:
  - the page-reading code gets the same blocks from each saved reply's HTML (in a local browser,
    no network)
  - the recorded 7-reply session, replayed through the real fix loop, leaves the bundled todo app
    (`tests/fixtures/todo_project`, 6 bugs) passing its tests. The replay includes Gemini's real
    "I encountered an error" reply and the retry.
  - partial-edit merging and command handling work on real replies
- **End-to-end tests** (`test_e2e.py`, only with `--e2e`) use live signed-out Gemini: the sign-out
  check, a message round trip, refusing Flash, and the full command fixing the todo app.

To re-record the fixtures after Gemini's page changes, run `python tests/record_fixtures.py`. It
uses an anonymous session, keeps the old fixtures unless the recorded session ends with passing
tests, and refuses to save anything containing your home folder path or username.

The tests use a temporary `GEMINONCE_HOME` and ignore your `GEMINONCE_*` settings.

## Code layout

| Module | What's in it |
|---|---|
| `cli.py` | Command-line options and setup checks (anonymous confirmation, environment variables); connects the pieces |
| `loop.py` | `FixLoop`: rounds of ask, apply, test; retries on replies without code; stops when the test output stops changing |
| `gemini.py` | `GeminiChat`: the Gemini page (selectors, account checks, model picker, sending a message and reading the reply) |
| `browser.py` | `Browser`: launching Chrome with Playwright (profile, hidden or visible, user-agent) |
| `chrome_profiles.py` | Listing and copying your own Chrome profiles |
| `workspace.py` | `Workspace`: the project on disk (collecting files, applying edits with backups, running commands) |
| `merge.py` | `Outline` and `merge_partial`: merging partial edits into files by definition name |
| `protocol.py` | The reply format rules, parsing replies, building prompts |
| `safety.py` | Sandbox, risky-code patterns, secret detection |
| `transcript.py`, `highlight.py`, `console.py` | Console output: transcript, syntax highlighting, colors and prompts |
| `usage.py` | `Usage`: token counts and the API-equivalent cost estimate |
| `pipeline.py` | `geminonce build`: the spec, tests and code stages, reviews and locking |

## When it breaks

Gemini's page structure changes from time to time. The selectors are constants at the top of
`geminonce/gemini.py`. The last reply's HTML is saved to `~/.geminonce/last_response.html`, so
you can see what changed.
