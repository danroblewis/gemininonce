# gemininonce

A small agent loop for Gemini **web**. It doesn't use an API or tool calls. It sends your files and
the test output as a chat message, then scrapes the reply out of the browser. Next it writes the
returned files and re-runs your test until it passes.

## Setup

```sh
uv tool install -e .          # or: pip install -e .
playwright install chromium   # only needed if Google Chrome isn't installed
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

Options: `-n` sets the maximum number of rounds (default 5), `--root` sets the project root that
paths are relative to, and `--cdp http://127.0.0.1:9222` attaches to a Chrome you started with
`--remote-debugging-port=9222`.

## When it breaks

Gemini's DOM changes from time to time. The selectors are constants at the top of `gemininonce.py`. The
last reply's HTML is saved to `~/.gemininonce/last_response.html` so you can see what changed.
