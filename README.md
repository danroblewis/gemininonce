# gemininonce

A small coding agent that drives **Gemini in your browser**, with no API key and no tool calls. It
sends your code and failing test output to Gemini, writes the files Gemini sends back, re-runs your
tests, and repeats until they pass. It can also build something new from an idea: spec, then tests,
then a test plan, then tests, then code.

## Install

```sh
uv tool install git+https://github.com/danroblewis/gemininonce     # or: pip install -e <clone>
```

It uses your installed Google Chrome. Without Chrome, run `playwright install chromium` once.

## Quickstart

```sh
# Fix code until the tests pass
gemininonce src/ tests/ -t "pytest -x"

# Say what you want, too
gemininonce app.py -t "npm test" -m "the date parser breaks on ISO week dates"

# Build something new: spec -> test plan -> unit + e2e tests -> code (you settle each step with Gemini)
gemininonce build "a constraint solver in python that can solve n-queens" --dir nqueens

# Use your work Google account (reuses your Chrome login: SSO, 2FA)
gemininonce src/ -t "pytest" --chrome-profile "Profile 2" --account @yourcompany.com

# Signed-out, free-tier Gemini in a throwaway browser (treat everything sent as public)
gemininonce src/ -t "pytest" --anonymous

# Unattended: the exit code says whether the tests pass
until gemininonce src/ tests/ -t "pytest -x"; do :; done
```

On the first run without `--chrome-profile` or `--anonymous`, a Chrome window opens so you can sign
in to Gemini once.

## What you'll see

- **A colored transcript:** your messages, Gemini's replies, syntax-highlighted code, and test results.
- **The cost:** after every reply, what it cost and the running total at API prices. The web chat
  itself is free or flat-rate.
- **Approvals:** a prompt to approve, modify or skip any shell command Gemini suggests, and a `y/N`
  before it creates new files or reads files you didn't give it.
- **When Gemini is stuck or out of rounds:** you choose: send a hint, `/more N` rounds, `/new` (a
  fresh conversation that starts over from the current files), or stop.

The test command runs in a sandbox, risky code is flagged before it's written, and secrets are never
sent. Still, review the result with `git diff`.

## Common options

| Option | |
|---|---|
| `-t CMD` | test command; done when it exits 0 |
| `-m TEXT` | what you want done |
| `--model flash\|pro\|flash-lite` | Gemini model (default `flash`) |
| `--chrome-profile NAME`, `--account TEXT` | use your Chrome login, and require an account (`--chrome-profile list x` lists profiles) |
| `--anonymous`, `-y` | signed-out free tier; `-y` skips its confirmation |
| `--show`, `-v` | show the browser; print everything in full |
| `-n N`, `--patience N` | round cap (20); rounds without progress before asking you (3) |
| `--no-sandbox` | let the test command use the network and write outside the project |

Settings can also come from `GEMININONCE_CHROME_PROFILE`, `GEMININONCE_ACCOUNT`, `GEMININONCE_MODEL`
and `GEMININONCE_PRICE`. See `gemininonce --help` and `gemininonce build --help` for everything.

## More

- [How the fix loop works](docs/how-it-works.md): rounds, file requests, retries, partial edits, output, models, cost
- [`gemininonce build`](docs/build.md): spec, then test plan, then tests, then code, with independent reviews and locked tests
- [Testing](docs/testing.md): what a good test suite looks like, and how `build` gets there
- [Accounts and anonymous mode](docs/accounts.md): Chrome profiles, account checks, the free tier
- [Safety](docs/safety.md): sandbox, code checks, secrets
- [Development](docs/development.md): installing from a clone, updating, uvx, tests, code layout
