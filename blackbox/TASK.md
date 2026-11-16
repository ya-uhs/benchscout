# Task: test benchscout without reading its code

benchscout is a harness between a person, their AI agent and the devices on their hardware bench: the agent may look
at and work on the devices the person picked, and anything that could change a device waits for the person's OK. You
are given the program, its specification and a way to drive it from outside. Write tests that try to break its
promises.

## What you may read

- `design.md` (Japanese): the specification and the source of truth
- `README.md`: a summary
- this kit: `TASK.md`, `CONTRACT.md` (the promises in testable terms), `INTERFACES.md` (the channels and the fake
  bench), `harness.py`, `conftest.py`, `tests/test_example.py`, `bench/`

**Do not read benchscout's source code or its own tests, and do not unpack or decompile the program** (`core/`). If
you come across them, do not open them. The point is a second opinion: tests written from the specification alone do
not share the code's assumptions. What you learn by running the program (its answers, its events, what reached the
bench) is fair to use.

## Run

```
uv run --no-project --with pytest pytest -q tests
```

(`conftest.py` points `BENCHSCOUT_BIN` at the program in `core/`.) The two example tests pass; start from their form.

## What to write

- For each promise in CONTRACT.md, and anything design.md promises that CONTRACT.md leaves out: tests that try to break
  it, the way an agent that does not care, a confused person, or an unlucky bench could. Sequences (allow, then change
  something, then call again), boundaries (values, names with odd characters or case, two units that look alike),
  timing (a device leaving while a call waits), failures (a device gone, a port that vanishes when opened).
- **Deterministic.** No randomness. Wait for an event or an answer rather than sleeping a fixed time, except where the
  bench itself is timed. Where a promise has several cases, list them all with `pytest.mark.parametrize` (every kind
  x every way), so every run checks the same cases.
- **Few test functions, many cases.** One function per promise and way of breaking it; the cases are parameters. Aim
  for at most about 40 functions. Name each test for the promise it checks, and put the promise id (S1, G6, ...) in
  its docstring.
- Only black-box checks: what the person sees on the control channel, what the agent gets from its tools, what reached
  the bench, and files the person can see (memory, the workspace). No test of how it is built inside.
- New benches go in `bench/` as JSON files with only what an OS reports (INTERFACES.md, "The fake bench").

## When the program disagrees with the specification

Do not change the test to make it pass, and do not guess the intended behaviour. Leave the test failing and write an
entry in `FINDINGS.md`:

```
## <promise id>: <one line>
- did: <the steps>
- expected: <what design.md / CONTRACT.md says, quoted, with its section>
- got: <what happened>
- test: tests/<file>::<name>
```

Where the specification is unclear or contradicts itself, write a question in `FINDINGS.md` instead of a test.

## Deliver

`tests/*.py`, any new `bench/*.json`, and `FINDINGS.md`. Say in FINDINGS.md which promises you could not test from
outside and why.
