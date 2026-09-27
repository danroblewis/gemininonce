# Testing in `gemininonce build`

`gemininonce build` aims for a large test suite in two parts: **unit tests** with every external
connection mocked, and **end-to-end tests** that use no mocks and hit the real services. It gets
there in steps, each one checked:

1. **Plan.** A planner writes `tests/TEST_PLAN.md` from the spec, following the guide and template
   below. It's sent back automatically if it's missing a unit or e2e section, leaves a requirement
   (R1, R2, …) without cases, or has fewer than `--tests-per-requirement` (default 3) named cases
   per requirement. Next, a **fresh reviewer session** checks the plan against the spec and the
   guide. Finally you discuss it with Gemini until you accept it.
2. **Tests.** A test writer implements every case in the plan, in `tests/unit/` and `tests/e2e/`.
   The tests are sent back automatically if:
   - an e2e test mocks anything, or the unit tests use `patch(...)` without any `autospec`/`spec`
   - they don't compile
   - either folder is empty
   - a requirement isn't named in any test (`test_r3_…`)
   - there are too few test functions
   - they **pass before any code exists**, since then they test nothing

   Then a **fresh reviewer session** checks the tests against the spec, the plan and the guide:
   missing cases, wrong expected values, mocks in e2e tests, real connections in unit tests, and
   assertions the spec doesn't promise. You review them last.
3. **Code.** The implementer makes them pass, and `SPEC.md` and `tests/` are locked.

**Network:** e2e tests call real services, so in `build` the sandboxed test command is **allowed to
use the network**. File writes stay confined to the project, and credentials stay unreadable.
`--no-network` turns it off. The plain fix loop stays offline unless you pass `--network`.

## The guide the planner, test writer and reviewers get

```text
WHAT A GOOD TEST SUITE LOOKS LIKE
- MANY small, focused tests. More is better. For every requirement, test the normal case, every acceptance
  criterion, the boundaries (smallest, largest, just outside), every error case, and edge cases (empty, one
  item, many, unusual but valid input). Use every example in the spec verbatim. Use parametrized tables
  (e.g. pytest.mark.parametrize) to cover many inputs cheaply.
- Check properties, not only examples: invariants that must always hold (round trips, a returned solution
  actually satisfies the rules, sizes and ordering guarantees), checked across ranges of inputs.
- Test behavior through the public interface in the spec, not private helpers or internal state. Assert
  everything the spec promises, and nothing it doesn't (exact messages, ordering or algorithms it leaves open).
- Precise assertions: exact values where the spec defines them, the exact exception type, the whole
  output structure.
- Independent and deterministic: no shared state or order dependence between tests; seed randomness;
  control time.
- Name each test after its requirement and behavior (test_r3_rejects_empty_input), with a one-line docstring.

TWO KINDS OF TESTS, IN TWO FOLDERS
- tests/unit/: fast, isolated tests of each component. Mock every external connection (network/HTTP,
  databases, other services, subprocesses, the clock) at the boundary. Test how the code talks to them (called
  with the right arguments) and how it handles their failures (errors, timeouts, bad data). Mock with
  autospec (patch(..., autospec=True), create_autospec, or spec=), so a mock of a function, method or
  attribute that doesn't really exist fails instead of silently passing.
- tests/e2e/: the whole system through its real entry points (public API, CLI) with NO mocks. If it
  uses real services or the network, call them for real: network access is available. Each e2e test must
  PROVE its real integration works: assert on the real results (data came back, the fields that matter are
  filled in, files were actually produced), never just that nothing crashed. A test that passes when every
  external call fails proves nothing; graceful failure belongs in the mocked unit tests. Where real data
  varies, check its shape and invariants rather than exact values. Skip only when a required credential is
  truly missing, and say why.
```

## The test plan template

```markdown
# Test plan

## Approach
What's unit-tested and what's end-to-end; what unit tests mock and how; which real services or network the
e2e tests use; shared fixtures and test data.

## Unit tests (tests/unit/)
For each requirement (R1, R2, ...): a list of test cases, each with its test name, what it checks
(input -> expected result or exception) and what's mocked.

## End-to-end tests (tests/e2e/)
Scenarios through the real entry points, without mocks, and what each one asserts.

## Coverage
A table: requirement -> test names. Every requirement gets at least 3 tests, including its
errors and boundaries.

## Not tested
Anything deliberately left out, and why.
```

To change what counts as a good test suite, edit `TESTING_GUIDE` and `TEST_PLAN_TEMPLATE` in
`gemininonce/protocol.py`.
