# Architecture

Ordo turns one interactive Claude Code session into an orchestrator: it holds the goal,
the task graph and the alignment, and delegates the actual work to executor sessions,
real `claude` processes launched one per tmux pane, that it reads, redirects and
reconciles. It ships as a skill plus a small Python CLI, no daemon, no server, no GUI, no
third-party Python dependency. The system lives in two separate trees: the repository
holds the code (the `ordo` package, the CLI entrypoint, the skill) and never holds state;
`ORDO_HOME`, `~/.claude/ordo` by default, holds every campaign's live state, one
directory per project.

## Modules

| Module | Owns | Must never do |
|---|---|---|
| `store.py` | `state.json`'s on-disk format, atomic write (tmp file + `os.replace`), the lock (`state.json.lock`, stale-stealable after 30s), sequential ids | be bypassed: it is the only place that knows the on-disk format and the locking strategy, every other module that touches `state.json` goes through it |
| `chantier.py` | campaigns, tasks, the dependency graph, cycle detection (`has_cycle`), `ready()` | import `panes.py`; take the lock inside `propagate_failures()` - it mutates the state dict in place, the caller already holds `store.locked()` |
| `panes.py` | tmux topology: session, window, pane; spawn, capture, busy/ready detection, geometry | import any other `ordo` module - the isolation is deliberate, it keeps this file testable standalone against real tmux; run `tmux kill-server` or `pkill tmux`; target a window-scoped call by bare session name instead of the stable window id |
| `report.py` | the file-based report channel: path, read, parse, apply, clear; lenient parsing of what a model actually produces | let an unreadable report pass as success (I2: `apply()` turns a parse failure into `blocked`, never `done`); take the lock inside `apply()` - same reasoning as `propagate_failures()` |
| `plan.py` | turning a dictated plan into a proposed graph via one `claude -p` call, zone-conflict to dependency translation, auto-accept after 45s | pass the raw plan as a CLI argument instead of stdin (I4); hold `store.locked()` during the model round trip (up to `MODEL_TIMEOUT` = 300s) |
| `capteur.py` | the sensor contract: install, bounded run (hard timeout, 256 KiB output cap), filtered status, adoption (3 concordant runs plus an explicit human call) | serve any measurement before adoption (I12); fill a missing output key with a default instead of raising (I10); invoke the script through a shell instead of respecting its own shebang |
| `controle.py` | the reconciliation loop: `scope_drift`, `fausse_completion`, `wake_reasons`/`wake_new`, `tick()` | let an exception raised while reconciling one campaign stop `tick()` for the others - each campaign is wrapped in its own try/except |
| `journal.py` | the per-campaign journal file, three authors only, the regenerated `brief()` | accept a fourth author (I11: `write()` raises on anything outside `ORDO`/`ORCH`/`USER`) |
| `carte.py` | the read-only picture of a campaign: dependency levels, phases read from title prefixes, blocking reasons, warnings, plus `vue()` (the flat shape the page consumes) and `html()` (a self-contained page carrying that shape as JSON) | write anything, to `state.json` or elsewhere (`cmd_map` owns the file write, this module only returns strings); talk to tmux - pane liveness is injected through `alive`, so a map can be drawn with no tmux server at all; invent a missing `why` - what nobody explained is reported as unexplained, never filled in; compute layout - positions do not exist before the browser reflows the rows, so Python serves data and the page places it; concatenate campaign content into markup - it travels as JSON and reaches the DOM only through textContent |
| `usage.py` | tokens an executor actually spent, summed from its own Claude Code transcript (`~/.claude/projects/*/<claudeSessionId>.jsonl`), read incrementally | report zero for a transcript it cannot find - absent is absent, and a `running` task showing 0 tokens is a false measurement; re-read a multi-megabyte transcript from the start on every poll; derive the transcript's directory name from the project path - that encoding is not ours, the session id is unique, so it searches |
| `serveur.py` | the single local map server on port 9123: the registry of known `ORDO_HOME`s, liveness by signature, detached start, and the read-only HTTP routes (`/`, `/api/state`, `/api/map`, `/health`) | write anything - there is no POST and no route mutates state; listen anywhere but `127.0.0.1`; trust the `home` query parameter (it is checked against the registry) or the `Host` header (checked against loopback names, which is what closes DNS rebinding); let its own failure break the watch that started it |
| `prompt.py` | executor brief composition (`brief_executante`), the orchestrator role-contract text (`contrat_role`) | import `report.py` or `capteur.py` - the report path is derived directly from `store.home()`, not from `report.py`'s own logic |
| `cli.py` | argparse dispatch, `--json` on every read verb (I12), the question registry (`state["questions"]`), task-to-campaign resolution | carry business rules that belong to another module - by its own docstring, this file has none of its own |

## Dependency direction

Verified from the actual `from . import ...` lines, not assumed:

| Module | Imports (within `ordo/`) |
|---|---|
| `store.py` | none |
| `panes.py` | none - stdlib only, deliberately standalone |
| `chantier.py` | `store` |
| `report.py` | `store` |
| `capteur.py` | `store` |
| `plan.py` | `chantier`, `store` |
| `journal.py` | `chantier`, `store` |
| `prompt.py` | `chantier`, `store` |
| `usage.py` | none - stdlib only |
| `carte.py` | `chantier`, `journal`, `store`, `usage` |
| `serveur.py` | `carte`, `chantier`, `panes`, `store`, `usage` |
| `controle.py` | `capteur`, `chantier`, `journal`, `panes`, `plan`, `report`, `store` |
| `cli.py` | `capteur`, `chantier`, `journal`, `panes`, `plan`, `prompt`, `report`, `store`, plus `controle`, `carte` and `serveur` imported lazily, inside `cmd_tick`, `_map_write` and `cmd_serve`/`cmd_watch` only, so the rest of the CLI keeps working if any of them is broken or absent |

`store.py` is the only module that knows the on-disk format of `state.json`; every mutation
in every other module goes through `store.locked()` or `store.load()`/`store.save()`.
`panes.py` is the only module that shells out to tmux; `controle.py` and `cli.py` are the
only two callers of it elsewhere in the package.

## `ORDO_HOME` layout, today

```
ORDO_HOME/
  state.json                        # single source of truth, written atomically
  state.json.lock                   # lock file, holds the pid of its holder
  briefs/<campaign>/<task>.md       # brief sent to an executor, kept for audit
  reports/<campaign>/<task>.json    # written by the executor, read by Ordo
  journal/<campaign>.md             # one journal file per campaign
  sensors/<campaign>.*              # the sensor script, outside the audited repo
  archives/<campaign>/              # a closed campaign's briefs, reports, journal, sensor
  map/<campaign>.html               # the map page, rewritten by `ordo map`, never read back
```

One file lives outside every `ORDO_HOME`, and has to: `~/.claude/ordo-serve.json`, the
registry of homes the map server knows about. A registry kept inside a home would be
invisible to the others, and navigating between campaigns of different projects is the
whole point of the server. `ORDO_REGISTRY` overrides its path; `ORDO_NO_SERVE` stops the
server from ever being started, which is what the test suite sets.

Briefs and reports are scoped by campaign (I13), never flat: `reports/<campaign>/<task>.json`,
not `reports/<task>.json`. Task ids are unique within one `ORDO_HOME`, but a home shared
between two projects mixed their reports at a flat path with nothing in it saying which
campaign a file belonged to. Scoping by campaign at the path level means two campaigns
sharing one home cannot collide, independent of id uniqueness.

## Lifecycle of a task

| Step | What happens | Module doing the work |
|---|---|---|
| `add` | task created in state, `queued` | `chantier.add_task()` |
| `ready` | eligibility computed: every dependency `done` **and** its checklist fully checked (I1); the task's own checklist never counts | `chantier.ready()` |
| `launch` | brief written to `briefs/<campaign>/<task>.md`; tmux session/pane created, `wait_ready()` blocks until the `claude` TUI is ready or the trust dialog appears; `send()` injects "Read \<brief\> and apply it."; task state becomes `running` | `prompt.brief_executante()`, `panes.ensure_session/spawn/wait_ready/send`, orchestrated by `cli._do_launch()` |
| executor runs | the real `claude` process reads its brief and, at the end of its turn or when blocked, writes `reports/<campaign>/<task>.json` itself | the executor process, outside Ordo's control |
| `tick` reconciles | for every `running` task, the report is read first, before the pane is even looked at (I2); a terminal report (`done`/`blocked`/`asking`) drives the state transition | `controle.tick()` -> `_tick_one()` -> `report.apply()` |
| `done` | task state becomes `done` once `report.apply()` reads `state: "done"` from the report | `report.apply()` |
| pane reaped | after `tick`, every `done` task's pane is captured (last 60 lines kept in `task["lastCapture"]`), then killed; `paneId` is cleared | `cli._moissonner_les_finies()`, called from `cmd_tick` |

Only `done` panes are reaped; `blocked` and `failed` panes stay open because those are
exactly the ones a human wants to look at. `--keep-panes` on `start` turns reaping off
entirely for a campaign.

## The report channel, and why it is the signal

A pane going idle proves nothing: the executor could be thinking, could have crashed,
could have finished with nothing written. The only channel Ordo trusts is the file whose
absolute path was handed to the executor in its own brief (`reports/<campaign>/<task>.json`).
This is invariant I2, the one the README calls out by name: "an executor that says it is
done is not done... the signal is the report file it wrote, never the state of its pane."

Concretely: a `running` task whose pane died without a report only becomes `blocked` after
`PANE_DEAD_GRACE_S` (30s) of grace, to give a report written just before the process died a
chance to reach disk. A report that exists but fails to parse (`report.parse()` raises
`RapportError`) is never read as success either: `report.apply()` catches the exception and
sets the task to `blocked` with the parse error as the reason. There is no code path in this
package that marks a task `done` without having read a `state: "done"` report.

## Concurrency

`store.locked()` is the single lock around `state.json`: an exclusive file lock
(`state.json.lock`, holding the holder's pid), waited on for up to `LOCK_WAIT_TIMEOUT`
(10s) and stealable once its holder's pid is dead and the lock is older than
`LOCK_STALE_AFTER` (30s). The state dict is (re)loaded *inside* the lock, mutated, and
saved automatically when the `with` block exits normally.

File reads and every tmux call happen **outside** the lock. `controle._tick_one()`
collects report contents and pane liveness before acquiring the lock, and sends any queued
tmux messages after releasing it; `capteur.run()` executes the sensor script entirely
outside the lock, only taking it briefly to read the script's path beforehand and to record
the result afterward; `plan.propose()` calls the planning model (up to 300s) outside the
lock too. Holding `state.json.lock` for the duration of a subprocess call or a model round
trip would stall every other Ordo command, in this campaign or any other sharing the same
`ORDO_HOME`, for the entire wall-clock duration of that call.

This is also why `report.apply()` and `chantier.propagate_failures()` never call
`store.locked()` themselves: both mutate the state dict they are handed, in place, and
expect the caller to already hold the lock, so several related effects land in one atomic
write instead of several partial ones. Calling `store.locked()` from inside a function
already running under it would deadlock, since the lock is not reentrant.
