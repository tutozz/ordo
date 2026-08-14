# Contributing

## Setup

| Requirement | Detail |
|---|---|
| Python | 3.10 or later, standard library only, no third-party dependency |
| tmux | 2.9 or later (`window-size manual`, `resize-window -A`, `set-hook -t`) |
| Claude Code | the `claude` binary on your PATH |
| OS | Unix; macOS and Linux are supported, Windows is not |

```bash
git clone https://github.com/tutozz/ordo.git
cd ordo
./install.sh
```

`install.sh` links `bin/ordo` into `~/.local/bin` and `skills/ordo` into
`~/.claude/skills/ordo`. Nothing else is written until a campaign actually starts.

Run `ordo doctor` after installing: it checks Python, tmux, the `claude` binary, the
active `ORDO_HOME` and every open campaign already sharing it, and exits non-zero if
tmux or `claude` is missing.

## Tests, three layers

| Layer | Command | Proves |
|---|---|---|
| Unit suite | `ORDO_HOME=$(mktemp -d) python3 -m unittest discover tests/` | each module's behaviour in isolation; 517 tests as measured on this checkout |
| End-to-end | `ORDO_HOME=$(mktemp -d) python3 tests/e2e.py` | the full CLI flow against real tmux panes; every check re-reads the actual on-disk artifact (`state.json`, a report file, the journal, a brief) instead of trusting a return value or stdout alone |
| Mutation check | `bash tests/mutation_check.sh` | the two suites above are not decor - see below |

Run `discover` from the repository root as `discover tests/`; the form
`discover -s tests -t .` fails, `tests/` is not a package. tmux-driven tests run against a
private tmux server (`tests/tmux_isolation.py` points `TMUX_TMPDIR` at a scratch
directory) so they never compete with, or get slowed down by, your own tmux sessions.

## The invariant rule

This is the load-bearing rule of the project. Read it before touching anything else.

Ordo names 13 invariants, `I1` through `I13`, each traced back to a real, measured bug.
**Every invariant carries a test named after it, that fails red the moment the invariant
is removed from the code** - and `tests/mutation_check.sh` proves that it actually does:
for each invariant it applies one exact, unique textual mutation to the file that carries
it, runs that invariant's test, and requires the test to go red. It does the same for
three prohibitions written into every executor's brief (no `AskUserQuestion`, no
self-validation, nothing irreversible without asking). 16 cases in total, currently.

When you add a behaviour worth protecting:

1. Name the invariant, or say which existing one it strengthens.
2. Write a test that fails when the invariant is violated, not a test that merely
   exercises the code path once.
3. Add a mutation case to `tests/mutation_check.sh`: one exact, unique search/replace
   against the file that carries the invariant.
4. Run the script and confirm the total case count goes up, and that your new case
   reports `PROUVE`, not `NON COUVERT`.

The failure mode this whole mechanism exists to prevent: a test whose assertion is so
weak, or whose path is so narrow, that it stays green no matter what the mutation just
broke. A test that would pass identically with the bug present is not a test, it is decor
shaped like one - and this project's stated position is that it proves nothing and must
not be cited as proof.

## Test discipline

- Every test isolates itself via `ORDO_HOME`; none is allowed to touch the real
  `~/.claude/ordo`.
- tmux-driven tests spawn real panes running plain `bash`, never `claude`: no token is
  ever spent, no model is ever called, by any test in this repository.

## Things you must never do here

| Never | Why |
|---|---|
| Run `tmux kill-server` or `pkill tmux` | destroys the whole tmux server, shared with your own unrelated sessions. A stray `kill-server` once destroyed a user's working session and the process inside it. `panes.kill()` and `panes.kill_session()` only ever destroy named objects Ordo itself created, one at a time, matched exactly. |
| Answer Claude Code's folder-trust dialog on the user's behalf | deliberate product decision: Ordo never approves a directory automatically. `wait_ready()` detects the dialog, sends no key at all (not even Escape), blocks the task, and prints the exact attach command so a human decides. |
| Add a silent failure | I8: every refusal names itself and says why. The one deliberate exception is the background relayout hook fired when a human detaches from a session (`panes.py --relayout`, see `CHANGELOG.md`): it now fails best-effort and silent, because it can run long after the triggering process has exited, with no one left to report to. It is a documented, singular exception, not a precedent to extend. |
| Make a pane's state the success signal | I2: an idle or silent pane proves nothing. The only signal `tick()` trusts is the report file the executor writes to `reports/<campaign>/<task>.json`; without it, a task is never `done`, no matter what the pane looks like. |
| Target a tmux window by bare session name for a window-scoped call | `-t <session>` alone resolves to that session's *current* window, which silently drifts the moment an attached human opens or switches windows. Target the stable window id (`@12`) that `ensure_session()` returns instead. |

## Commit convention

`type(scope): description` - types: `feat`, `fix`, `refactor`, `test`, `chore`, `perf`,
`docs`.

Commit messages and code comments in this repository are written in French. Everything a
user or an orchestrating session reads back - README, the skill files, CLI output, the
text sent to an executor - is written in English. Match whichever side of that line
you're writing on; do not mix them.

## Where to start reading

1. `ARCHITECTURE.md` - the module map, the dependency direction, the lifecycle of a task.
2. `docs/SPEC.md` - the full implementation contract.
3. `skills/ordo/SKILL.md` - the orchestrator's role contract, what an orchestrating
   session actually loads and follows.
