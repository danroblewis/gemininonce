# gemininonce

A small agent loop for Gemini **web**. It doesn't use an API or tool calls. It sends your files and
the test output as a chat message, then scrapes the reply out of the browser. Next it writes the
returned files and re-runs your test until it passes.

## Setup

```sh
uv tool install -e .          # or: pip install -e .
playwright install chromium   # only needed if Google Chrome isn't installed
```

### One-liner with uvx (no install)

[uv](https://docs.astral.sh/uv/)'s `uvx` runs it in a temporary environment:

```sh
# from a local checkout
uvx --refresh-package gemininonce --from ~/gemininonce gemininonce src/ tests/ -t "pytest -x"

# from git, once the repo is pushed somewhere
uvx --refresh-package gemininonce --from git+https://github.com/<you>/gemininonce gemininonce src/ tests/ -t "pytest -x"

# with your own Chrome login and a required account
uvx --refresh-package gemininonce --from ~/gemininonce gemininonce src/ tests/ -t "pytest -x" \
  --chrome-profile Default --account @yourcompany.com
```

`--refresh-package gemininonce` makes uv rebuild from the current source. Without it, uv keeps
running the cached copy and ignores your edits. It re-checks only this package, so it's still fast.

It uses the Google Chrome you already have installed, so there's nothing else to download. Without
Chrome, run `uvx playwright install chromium` once.

For a short command, add an alias to `~/.zshrc`:

```sh
alias gn='uvx --refresh-package gemininonce --from ~/gemininonce gemininonce'
# gn src/ tests/ -t "pytest -x"
```

On the first run a Chrome window opens. Log in to Gemini there. The login is saved in
`~/.gemininonce/profile`.

## Usage

```sh
gemininonce src/ tests/ -t "pytest -x"
gemininonce app.py -t "npm test" -m "the date parser breaks on ISO week dates"
gemininonce lib/ -m "add a --verbose flag"      # no test: you review and send follow-ups
```

For each round:
1. Your files, the task, and the failing test output go to Gemini.
2. Gemini's reply must contain `FILE: path` followed by a code block with the **full** file, and
   `COMMAND:` followed by a code block for each shell command it suggests.
3. The returned files are written. Before a file is replaced, its original is backed up to `~/.gemininonce/backups/<timestamp>/`.
4. You're asked to **[a]pprove / [m]odify / [s]kip** each suggested command.
5. The test runs again. If it fails, the output goes back to the same chat.

It stops when the test passes (exit code 0), or exits 1 in either of these cases:
- the test output hasn't changed for `--patience` rounds in a row (default 3)
- it reaches the `-n` round cap (default 20)

Because the exit code reports the result, you can wrap it in a shell loop:

```sh
until gemininonce example/tests example/todo -t 'cd example && pytest -x'; do :; done
```

When Gemini replies without code (an error like "I encountered an error…", a refusal, or just an
explanation), it retries up to `--retries` times (default 3), and retries don't count as rounds.
For an error or a short reply it resends the prompt. For a longer reply it asks for the files.

If Gemini sends only the changed functions or classes instead of the whole file, including
`# ... existing code ...` placeholders, each definition is merged into the file by name. If a
snippet can't be placed unambiguously, it's rejected and Gemini is asked for the complete file.

**Browser and output:** Chrome runs hidden (headless). If you need to sign in, it opens a visible
window just for that. Use `--show` to watch the browser.

The console shows a colored transcript of the conversation:
- **Your messages** (cyan): the task, test output and a list of attached files, not their contents.
- **Gemini's replies** (magenta): `FILE:`/`COMMAND:` markers highlighted and code dimmed. Long
  files are shortened; `-v` prints everything.
- **Results:** test results in green or red, safety warnings in red or yellow.

Code is syntax-highlighted with [Pygments](https://pygments.org/). The language comes from the
`FILE:` path, or from the language label Gemini puts on the code block, or Pygments guesses from
the content. `COMMAND:` blocks are highlighted as shell, and diffs are recognized anywhere. In test
output, errors are red, passes green and `file:line` locations cyan. Quoted source lines (like
pytest's) are highlighted in the project's main language.

Colors turn off automatically when output isn't a terminal, or if you set `NO_COLOR`.

**Model:** it uses **Flash** by default. Choose a different model with `--model pro`,
`--model flash-lite`, or any unique part of a name shown in Gemini's model picker (e.g.
`--model 3.1`). Version numbers are ignored, so `flash` keeps working after an upgrade. Set
`GEMININONCE_MODEL` to change the default, or use `--model any` to leave the picker alone. The
model is checked before every message and switched back if it changed. If the requested model
isn't available, it exits and lists the models Gemini offers.

Other options: `--root` sets the project root that paths are relative to, and
`--cdp http://127.0.0.1:9222` attaches to a Chrome you started with `--remote-debugging-port=9222`.

## Cost estimate

Gemini web is flat-rate on a personal or Workspace plan, so a run costs nothing extra. At the end
of each run the tool prints what the same conversation **would cost on the Gemini API**:

```
Gemini usage: 7 message(s), ~37.0k tokens in (incl. re-sent history), ~2.6k tokens out
API-equivalent cost (3.6 Flash at $0.75/$3.75 per 1M in/out): ~$0.0375
```

- **Tokens** are estimated at about 4 characters per token, Google's rule of thumb.
- **The API has no memory,** so every message pays again for the whole conversation so far as
  input. The estimate counts it that way, with no caching discount.
- **Hidden "thinking" tokens can't be seen,** so they're left out.
- **Prices** come from a built-in table taken from
  [Google's pricing page](https://ai.google.dev/gemini-api/docs/pricing) (updated 2026-09-24).
  Flash prices double on 2027-01-01. Override them with `--price IN,OUT` (USD per 1M tokens) or
  `GEMININONCE_PRICE`.

## Anonymous mode (free tier)

```sh
gemininonce src/ -t "pytest -x" --anonymous
```

`--anonymous` guarantees a signed-out, free-tier session:
- **A fresh throwaway browser profile** each run. No Google account, no cookies, nothing copied
  from your Chrome. The profile is deleted afterwards.
- **It won't combine with account options.** `--chrome-profile`, `--account`, `--cdp` and
  `--profile` given on the command line are errors, and the matching environment variables are
  ignored.
- **Signed-out check:** before every message it checks that Gemini is signed out, and refuses to
  send if it isn't.
- **Model:** signed out, Gemini only offers **Flash-Lite**, so that's the default here. Asking for
  another model is an error, not a quiet switch.
- **Confirmation:** before anything is sent, it lists the files and warns that free-tier chats may
  be kept by Google, used to improve its products and read by human reviewers, so treat everything
  as public. You have to type `yes`. For scripts, `-y` skips the question; without a terminal and
  without `-y` it aborts.

## Which account is used

**Use your own Chrome login (SSO, 2FA, work account):**

```sh
gemininonce --chrome-profile list x              # show your Chrome profiles
export GEMININONCE_CHROME_PROFILE="Profile 2"     # dir, name, or email
export GEMININONCE_ACCOUNT=@yourcompany.com
```

Chrome 136 and later refuse to let automation control your real Chrome data directory. So at the
start of each run your profile is copied, without caches or history, into `~/.gemininonce/chrome`,
which only your user can read. Chrome then runs from that copy. The first copy is a few hundred MB,
and later runs copy only what changed. On macOS the cookies decrypt through the Keychain, so you're
already logged in. Your normal Chrome can stay open.

The copy trick works on macOS and Linux. On Windows, Chrome's app-bound cookie encryption means a
copied profile won't be logged in.

**Without `--chrome-profile`**, the tool uses its own empty profile (`~/.gemininonce/profile`),
where you'd sign in once.

**Account check:** Gemini lets you chat while signed out, and nothing would look wrong. So before
every message the tool reads the signed-in account from the page, and it refuses to send if
Gemini is signed out or doesn't match `GEMININONCE_ACCOUNT` / `--account`. If several Google
accounts are signed in, it switches to the matching one.

## Safety

These are rough guard rails, not a guarantee. You should still review what Gemini changed
(`git diff`).

- **The test command runs in a sandbox.** That command runs whatever code Gemini wrote. Inside the
  sandbox there's no internet access (localhost still works), files can only be written inside the
  project and temp folders, and credential locations (`~/.ssh`, `~/.aws`, `~/.gnupg`, Keychains,
  Chrome/Firefox profiles, `~/.netrc`, …) can't be read. It uses `sandbox-exec` on macOS and `bwrap`
  on Linux if installed, with a warning when neither is available. If your tests need the internet,
  use `--no-sandbox`.
- **Every added line is checked before a file is written.** Only lines Gemini added are checked, not
  ones that were already there. The checks look for:
  - recursive deletes, disk wipes, destructive SQL or git
  - system paths, your home folder or credentials
  - `curl | sh`
  - webhooks and pastebins, raw-IP URLs
  - obfuscated `exec`
  - startup hooks (`.zshrc`, cron, LaunchAgents), `sudo`
  - `git push`, `npm publish`, sending email
  - crypto miners
  - hard-coded secrets

  It also flags writes to `.git/`, CI workflows, `.env` and key files, install hooks
  (`postinstall`, `cmdclass`), and a file shrinking by more than 80%. For Python it adds new
  findings from [Bandit](https://github.com/PyCQA/bandit), via `bandit` or `uvx bandit`. Serious
  findings need your `y`; otherwise the file is rejected and Gemini is told why. Minor ones (plain
  `subprocess`, network calls) are just shown.
- **New files need your OK.** Gemini sometimes makes up paths (e.g. `src/todo.py` when the code is
  in `todo/service.py`). A file that doesn't exist yet is only created if you type `y`. Otherwise
  Gemini is told which files really exist. Use `--allow-new-files` to skip the question.
- **Suggested shell commands get the same checks,** shown with warnings before you approve or
  modify them. Commands you approve run outside the sandbox, because they may need the internet
  (e.g. `pip install`).
- **Secrets aren't sent to Gemini.** Files that look like credentials (`.env`, `*.pem`, …) or that
  contain keys or tokens (AWS, GitHub, Google, Slack, OpenAI/Anthropic, Stripe, private keys, JWTs,
  `password = "..."`) are never sent. Anything that looks like a secret in test output is replaced
  with `[REDACTED]`.

## When it breaks

Gemini's page structure changes from time to time. The selectors are constants at the top of
`gemininonce/gemini.py`. The last reply's HTML is saved to `~/.gemininonce/last_response.html`, so
you can see what changed.

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

## Tests

```sh
pytest tests          # offline, a few seconds
pytest tests --e2e    # also runs the live tests against anonymous Gemini (~1.5 min)
```

- **Unit tests** (`test_gemininonce.py`) cover merging, safety checks, parsing, applying edits and the
  cost estimate.
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

The tests use a temporary `GEMININONCE_HOME` and ignore your `GEMININONCE_*` settings.
