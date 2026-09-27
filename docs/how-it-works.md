# How the fix loop works

```sh
geminonce src/ tests/ -t "pytest -x"
geminonce app.py -t "npm test" -m "the date parser breaks on ISO week dates"
geminonce lib/ -m "add a --verbose flag"      # no test: you review and send follow-ups
```

## Starting a new project in an empty folder

```sh
mkdir myapi && cd myapi
geminonce . -m "set up a basic FastAPI project ..." --check "GET /health returns {\"status\": \"ok\"}"
```

If every path you give is a directory with no files in it yet, Gemini is told the project is empty and to
create the files it needs. New files are then allowed without asking for each one. For a bigger project where
you want a spec, a test plan and tests first, use [`geminonce build`](build.md).

## No test command yet? `--check`

```sh
geminonce 070_pd.py --check "the output should contain the word asdf"
```

Describe in words how to tell it works, and Gemini turns that into a test command:
1. **Candidates.** It proposes 3–5 shell commands, each shown with a one-line description. Every candidate
   reports its own verdict: one line starting `PASS:` or `FAIL:` that says what was expected and what
   happened, with exit 0 or 1 to match. Commands the safety checks flag as risky are marked above the list.
2. **You choose which to try:** one number (`2`), several (`1 3`), or Enter for all. Or **type what you'd
   like instead**, e.g. `use bash, not python` or `check the exact counts`, and Gemini proposes a new set.
3. **Each one runs in the sandbox** and shows its verdict:
   - **✗ FAIL: expected 2 lines, got 3:** it detects the problem, with a reason. That's what you want.
   - **✓ passes already:** it wouldn't detect the problem.
   - **⚠ errored without a verdict:** the check itself crashed, so it's probably broken.
   - **fails without saying why:** it failed but didn't say why, so it's weak evidence.
4. **You pick:**
   - a number
   - `$ <your own command>`
   - text for Gemini (what to change), which gets you a new set; Gemini sees how the tried ones did
   - `q` to quit

The chosen command becomes the test (`-t`), your description becomes the task, and the normal loop runs in a
fresh conversation. Without a terminal, it uses the first candidate with a clear `FAIL:` verdict.

A self-reported verdict makes a broken check visible, but it can't prove the check will pass once the code is
right. Read the `FAIL:` reasons: a sensible one ("expected 2 lines, got 3") is a good sign.

## Each round

1. The first message has your files, the task, the failing test output, and a **list of every file
   in the project** (from `git ls-files`, with secret and credential files left out). Later messages
   only carry what changed: test output, notes about rejected edits, and command results.
2. Gemini's reply must contain `FILE: path` followed by a code block with the **full** file, and
   `COMMAND:` followed by a code block for each shell command it suggests. If it needs a file it
   doesn't have, or the current version of one it changed earlier, it writes `READ: path`, and the
   file comes back in the next message.
3. The returned files are written. Before a file is replaced, its original is backed up to `~/.geminonce/backups/<timestamp>/`.
4. You're asked to **[a]pprove / [m]odify / [s]kip** each suggested command.
5. The test runs again. If it fails, the output goes back to the same chat.

It stops when the test passes (exit code 0), or exits 1 when it reaches the `-n` round cap
(default 20).

## When Gemini is stuck, or out of rounds

Instead of just giving up, the tool stops and asks you. That happens in two cases:
- **No progress:** `--patience` rounds (default 3) in a row where the test keeps failing the same
  way or goes back to a failure it already had, or replies with no changes even after retries.
- **Out of rounds:** the `-n` round limit (default 20) is used up.

It shows the rounds so far, the files changed, the end of the last test output and Gemini's last
message, then:

```
  What next?
    type a message   sent to Gemini as a hint, then it keeps going
    /more [N]        keep going as is for N more rounds (default 5)
    /new             fresh Gemini conversation: it forgets this chat and starts over with the
                     current files and test output (helps when it's going in circles)
    Enter or /quit   stop here
```

- **`/more N`** means "don't ask me again for N rounds". It sends exactly what would have been sent
  anyway, and raises the round limit if needed.
- **A hint or `/new`** also adds rounds if you're at the limit, but you're asked again as soon as it
  gets stuck.
- **`/new` restarts the conversation.** Gemini loses the whole conversation (its earlier attempts,
  your hints) and gets the current state of the files, which includes its own changes so far. Long
  chats tend to go in circles, and a fresh one is also cheaper, because each message no longer
  re-sends the long history.

Without a terminal (scripts, `until` loops), it stops with exit code 1 instead of asking.

## Files Gemini asks for (`READ: path`)

- **Files you gave:** sent without asking.
- **Any other file in the project:** you're asked `Send it? [y/N]`. Without a terminal the answer
  is no.
- **Never sent:** files outside the project, and secret or credential files.
- **Made-up paths:** Gemini is told the file doesn't exist and pointed to the list.
- Up to 5 replies per round can be just file requests before it counts as "no changes".

## Exit codes and retries

Because the exit code reports the result, you can wrap it in a shell loop:

```sh
until geminonce example/tests example/todo -t 'cd example && pytest -x'; do :; done
```

When Gemini replies without code (an error like "I encountered an error…", a refusal, or just an
explanation), it retries up to `--retries` times (default 3), and retries don't count as rounds.
For an error or a short reply it resends the prompt. For a longer reply it asks for the files. If
Gemini doesn't reply at all within the time limit, that's retried the same way, and a screenshot
of the page is saved to `~/.geminonce/last_error.png`.

## Partial edits

If Gemini sends only the changed functions or classes instead of the whole file, including
`# ... existing code ...` placeholders, each definition is merged into the file by name. If a
snippet can't be placed unambiguously, it's rejected and Gemini is asked for the complete file.

## Browser and console output

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

## Model

It uses **Flash** by default. Choose a different model with `--model pro`,
`--model flash-lite`, or any unique part of a name shown in Gemini's model picker (e.g.
`--model 3.1`). Version numbers are ignored, so `flash` keeps working after an upgrade. Set
`GEMINONCE_MODEL` to change the default, or use `--model any` to leave the picker alone. The
model is checked before every message and switched back if it changed. If the requested model
isn't available, it exits and lists the models Gemini offers.

## Other options

`--root` sets the project root that paths are relative to, and
`--cdp http://127.0.0.1:9222` attaches to a Chrome you started with `--remote-debugging-port=9222`.

## Cost estimate

Gemini web is flat-rate on a personal or Workspace plan, so a run costs nothing extra. The tool
shows what the same conversation **would cost on the Gemini API**. After every Gemini reply, a
green line shows what that reply cost and the running total:

```
  $ this reply: ~4.7k in, ~442 out, $0.0025  |  total: ~11.5k in, ~1.0k out, $0.0060
```

If a round took several messages (file requests, retries), a round subtotal follows, e.g.
`$ round 3: …  |  total: …`.

and a summary at the end:

```
Gemini usage: 7 message(s), ~37.0k tokens in (incl. re-sent history), ~2.6k tokens out
API-equivalent cost (3.6 Flash at $0.75/$3.75 per 1M in/out): ~$0.0375
```

If you don't see these, check that you're running the current version (see [development.md](development.md#updating)).
Also, when the test already passes at the start, Gemini is never contacted, so there's nothing to
price.

- **Tokens** are estimated at about 4 characters per token, Google's rule of thumb.
- **The API has no memory,** so every message pays again for the whole conversation so far as
  input. The estimate counts it that way, with no caching discount.
- **Hidden "thinking" tokens can't be seen,** so they're left out.
- **Prices** come from a built-in table taken from
  [Google's pricing page](https://ai.google.dev/gemini-api/docs/pricing) (updated 2026-09-24).
  Flash prices double on 2027-01-01. Override them with `--price IN,OUT` (USD per 1M tokens) or
  `GEMINONCE_PRICE`.
