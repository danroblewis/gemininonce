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

Other options: `--root` sets the project root that paths are relative to, and
`--cdp http://127.0.0.1:9222` attaches to a Chrome you started with `--remote-debugging-port=9222`.

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

## When it breaks

Gemini's DOM changes from time to time. The selectors are constants at the top of `gemininonce.py`. The
last reply's HTML is saved to `~/.gemininonce/last_response.html` so you can see what changed.
