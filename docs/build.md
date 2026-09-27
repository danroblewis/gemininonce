# `gemininonce build`: spec → test plan → tests → code → README

```sh
gemininonce build "A module roman.py with to_roman(n) and from_roman(s) for 1..3999, \
  raising ValueError on bad input" --dir roman
```

It works in five stages. Each one gets its own fresh Gemini conversation, and each is only allowed
to write its own files:

1. **Spec.** Gemini writes `SPEC.md` from a template: goal, scope, architecture and module layout,
   the **complete interface as code signatures** (classes with constructors, attributes and
   methods, functions, exceptions), data model, numbered requirements with acceptance criteria,
   examples, errors, measurable non-functional requirements, and its assumptions. It's told that
   the test writer will see *only* this file, so nothing may be left to guess.
   - **Web research first:** before writing, Gemini gets a short research question: existing
     libraries and prior art, current docs of any library, API or service, standards, algorithms,
     pitfalls. It's a separate short question because Gemini searches for questions like that, but
     rarely while writing a long document. The sources it cites are shown (`sources: 7 (…)`), and
     the spec's section 11 lists them with links and what was taken from each. If it cites nothing,
     it's asked once more to search. `--no-research` skips this. The test planner is also asked to
     check the current docs of any real services the e2e tests will call.
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
   - **Model:** signed in, the spec and the test plan are written with **Pro**, and later stages switch back. Signed
     out (`--anonymous`), Gemini only offers Flash-Lite, so sign in for better specs. Choose
     explicitly with `--spec-model` and `--tests-model`.
2. **Test plan.** A planner writes `tests/TEST_PLAN.md` from the spec: the approach and what gets
   mocked, unit test cases for each requirement, end-to-end scenarios, and a requirement-to-tests
   coverage table. It's checked mechanically, reviewed by a fresh session against the spec and the
   testing guide, and then you discuss it with Gemini like the spec. See [testing.md](testing.md).
3. **Tests.** From the spec and the plan, Gemini writes **unit tests** in `tests/unit/` (external
   connections mocked) and **end-to-end tests** in `tests/e2e/` (no mocks, real services). They must
   compile, cover both folders, name every requirement, include enough tests per requirement, and
   **fail** before any code exists. Then a fresh reviewer checks them against the spec, the plan
   and the guide, and you discuss them. `--tests-reviews N` (default 1).
4. **Code.** The normal fix loop runs until the tests pass. `SPEC.md` and `tests/` are **locked**:
   Gemini can't make the tests pass by changing them. An attempt is rejected, and Gemini is told to
   change the implementation instead.
5. **README.** Once the tests pass, Gemini writes `README.md` from the actual code: what the project
   is, how to install it (including extra steps like `playwright install chromium`), configure it,
   run it, use it (commands, endpoints, what to click), and run its tests. It's sent back if the
   install, run or usage section is missing, or if there are no commands. The build then **prints
   the install and run instructions** as its last output. `--no-readme` skips this stage, and
   `--from readme` writes just the README for an existing project.

**You settle the spec, the plan and the tests by talking to Gemini.** You see the file in full, and after that
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
- `--spec`, `--plan` and `--tests-dir` rename the spec, the test plan and the tests folder.
- `--tests-per-requirement N` sets the minimum number of test cases per requirement (default 3).
- `--plan-reviews`, `--tests-reviews` and `--spec-reviews` set the number of independent reviews (default 1 each).
- `--no-network` keeps the test command offline (e2e tests need the network, so it's on by default for `build`).
- `--from plan`, `--from tests` or `--from code` restarts at a later stage and reuses the earlier files.
- All the usual options work: `--anonymous`, `--chrome-profile`, `--model`, the cost lines, and so on.

In a live anonymous test, the example above went from an empty folder to a spec with 7 numbered
requirements, 7 matching tests, and a correct implementation (checked on all of 1–3999 and on
malformed numerals). That took 3 messages, about $0.005 at API prices.
