# `gemininonce build`: spec → tests → code

```sh
gemininonce build "A module roman.py with to_roman(n) and from_roman(s) for 1..3999, \
  raising ValueError on bad input" --dir roman
```

It works in three stages. Each one gets its own fresh Gemini conversation, and each is only allowed
to write its own files:

1. **Spec.** Gemini writes `SPEC.md` from a template: goal, scope, architecture and module layout,
   the **complete interface as code signatures** (classes with constructors, attributes and
   methods, functions, exceptions), data model, numbered requirements with acceptance criteria,
   examples, errors, measurable non-functional requirements, and its assumptions. It's told that
   the test writer will see *only* this file, so nothing may be left to guess.
   - **The spec is Gemini's answer itself,** not a `FILE:` block. A spec has code blocks in it,
     and Gemini's page can't show a code block inside another one. The rendered answer is converted
     back to Markdown (headings, lists, code blocks, tables) and saved as `SPEC.md`, starting from
     its `# ` title. A reply that isn't a spec, such as questions for you, is treated as conversation.
   - **Automatic checks:** a spec without numbered requirements, an Interface section written as
     code, or an Architecture section goes straight back to Gemini.
   - **Independent review:** before you see it, a **fresh Gemini session in a second tab** reads
     *only* the spec, as the test writer will. It lists everything it would have to guess, or
     replies `NO ISSUES`. The writer revises from that list, and its conversation with you stays
     intact. `--spec-reviews N` sets how many reviews (default 1, 0 to skip).
   - **Model:** signed in, the spec is written with **Pro**, and later stages switch back. Signed
     out (`--anonymous`), Gemini only offers Flash-Lite, so sign in for better specs. Choose
     explicitly with `--spec-model` and `--tests-model`.
2. **Tests.** From the spec, Gemini writes tests under `tests/`. They must compile, and they must
   **fail**: tests that pass before any code exists don't test anything, so they're sent back.
   Then a fresh reviewer session compares the tests with the spec, checking:
   - every requirement, example and error case is covered
   - names and signatures match the Interface exactly
   - expected values are correct
   - nothing is asserted that the spec doesn't promise, like exact messages, ordering or internals

   The test writer fixes what it finds. `--tests-reviews N` (default 1).
3. **Code.** The normal fix loop runs until the tests pass. `SPEC.md` and `tests/` are **locked**:
   Gemini can't make the tests pass by changing them. An attempt is rejected, and Gemini is told to
   change the implementation instead.

**You settle the spec and tests by talking to Gemini.** You see the file in full, and after that
only a diff of what changed. Then you type your reply, and it goes to the same Gemini conversation.
You can answer its questions (it may ask some before writing anything), ask your own, or ask for
changes. Repeat until you're happy:
- Enter accepts.
- `/show` prints the whole file again.
- `/quit` stops.

`--accept` takes Gemini's version without discussion. It's required without a terminal, and then
Gemini is told to decide open questions itself rather than ask.

Other options:
- `-t` sets the test command (default `pytest -q`), run from `--dir`. Every stage is told exactly how
  tests will be run, and the build is only done when that command passes. Use the command you'll
  actually use: for example, `python -m pytest` can import from the project folder when plain
  `pytest` can't.
- `--spec` and `--tests-dir` rename the spec file and the tests folder.
- `--from tests` or `--from code` restarts at a later stage and reuses the earlier files.
- All the usual options work: `--anonymous`, `--chrome-profile`, `--model`, the cost lines, and so on.

In a live anonymous test, the example above went from an empty folder to a spec with 7 numbered
requirements, 7 matching tests, and a correct implementation (checked on all of 1–3999 and on
malformed numerals). That took 3 messages, about $0.005 at API prices.
