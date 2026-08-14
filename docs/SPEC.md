# Ordo, native Claude Code orchestration substrate

Implementation contract. This document is the reference: whatever it does not spell out is
for the implementer to decide; whatever it does spell out is not up for debate.

Origin: an earlier prototype produced a product spec and more than eight thousand lines of
Python, most of it working around `claude --bg`, `--resume` and the stdin daemon. tmux makes
those workarounds unnecessary. This substrate keeps the **proven invariants** and throws out
the plumbing.

---

## 1. What this is

An interactive Claude Code session, the one where the user talks, becomes the
**orchestrator** when the `ordo` skill loads. It holds the goal, the task graph and the
alignment. It does not produce code. It launches **executors**: real `claude` processes,
each in a tmux pane, that it reads and sends instructions to.

| Role | Holds | Does not do |
|---|---|---|
| Human | the will, the business calls, authorization for the irreversible | execution, follow-up |
| Orchestrator | the goal, the scope, the definition of done, the state, the guardrails | producing the work |
| Executors | the doing, against a precise contract | deciding intent, leaving scope |

Three invariants: alignment is captured **before** delegating; it is **maintained** during;
**proof** outweighs declaration.

**Out of scope:** no graphical interface, no dedicated daemon, no server, no external Python
dependency.

---

## 2. Directory layout

Code and data now live in two separate trees. The repository carries the code and the
skill; `ORDO_HOME` carries the state.

Repository:

```
ordo/
  README.md                # public entry point
  ARCHITECTURE.md          # module map, dependency direction, task lifecycle
  CONTRIBUTING.md          # how to work on Ordo without breaking its guarantees
  LICENSE                  # MIT
  CHANGELOG.md
  install.sh               # sets up the command and the skill for a bare git clone
  pyproject.toml
  .claude-plugin/
    plugin.json            # Claude Code plugin manifest
    marketplace.json       # the repository is its own plugin marketplace
  skills/ordo/
    SKILL.md               # orchestrator's role contract, loaded on trigger
    references/
      tmux.md              # tmux playbook, verified traps
      sensor.md            # sensor contract, examples
      harness.md           # claude CLI traps
  bin/ordo                 # executable entrypoint, python3 shebang
  ordo/
    __init__.py            # __version__
    store.py               # atomic state, lock, canon, sequences
    chantier.py            # campaigns, tasks, graph, dependencies, cycles, ready
    panes.py               # tmux topology, launch, injection, activity
    report.py              # file-based report channel, lenient parser
    plan.py                # raw dictated plan into a proposed graph
    capteur.py             # sensor contract, bounded execution, adoption
    controle.py            # scope drift, false completion, wake-up
    journal.py             # the campaign's journal, regenerated brief
    prompt.py              # composing executor briefs
    cli.py                 # verb dispatch
  tests/
    test_<module>.py       # one unit suite per module, no dependency
    e2e.py                 # full walkthrough against real tmux panes
    mutation_check.sh      # proves the tests are not decor
  docs/SPEC.md             # this file
  examples/sensors/        # example sensor, generic
```

`ORDO_HOME`, by default `~/.claude/ordo`, **one per project**:

```
  state.json                      # state, written atomically
  state.json.lock                 # lock, holder's pid
  reports/<campaign>/<task>.json  # written by executors, read by Ordo
  briefs/<campaign>/<task>.md     # brief sent to an executor, kept for audit
  sensors/<campaign>.*            # sensor script, outside the audited repo
  journal/<campaign>.md           # the campaign's journal
  archives/<campaign>/            # closed campaign: briefs, reports, journal, sensor
```

The Python package used to be called `lib/`; that was a private directory name with no
place in a public repository. Every test isolates itself via `ORDO_HOME`; no test writes to
the real directory.

---

## 3. State schema, frozen

`state.json`, a single file, written via `os.replace` from a temp file on the same volume.

```json
{
  "version": 1,
  "seq": {"chantier": 0, "tache": 0, "question": 0, "proposition": 0},
  "chantiers": {
    "c-01": {
      "id": "c-01",
      "slug": "myapp",
      "project": "/home/user/code/myapp",
      "objectif": "a sentence, the stopping condition",
      "perimetre": "what's inside",
      "horsScope": "what's explicitly outside",
      "state": "open",
      "tmuxSession": "ordo-myapp",
      "tmuxWindow": null,
      "permissions": "skip",
      "keepPanes": false,
      "createdAt": "2026-08-09T14:00:00Z",
      "closedAt": null,
      "capteur": {
        "path": null,
        "runs": [],
        "adopted": false,
        "adoptedAt": null,
        "lastSuccess": null,
        "lastError": null,
        "consecutiveFailures": 0,
        "identicalCycles": 0
      },
      "lastWake": null,
      "lastEvent": null
    }
  },
  "taches": {
    "t-01": {
      "id": "t-01",
      "chantier": "c-01",
      "titre": "schema migration",
      "prompt": "the substance, written by the orchestrator",
      "state": "queued",
      "dependsOn": ["t-00"],
      "touches": ["db", "app/src/api"],
      "checklist": [{"id": "c1", "label": "tests green", "done": false}],
      "priority": 0,
      "attempts": 0,
      "model": null,
      "paneId": null,
      "claudeSessionId": null,
      "reaped": false,
      "lastCapture": null,
      "cwd": null,
      "createdAt": "...", "startedAt": null, "finishedAt": null,
      "lastReportAt": null,
      "report": null,
      "error": null,
      "notes": []
    }
  },
  "questions": {
    "q-01": {
      "id": "q-01", "chantier": "c-01", "tache": "t-01",
      "question": "text", "options": ["a", "b"],
      "pourHumain": false,
      "answer": null,
      "askedAt": "...", "answeredAt": null
    }
  },
  "propositions": {
    "p-01": {
      "id": "p-01", "chantier": "c-01",
      "taches": [{"ref": "n1", "titre": "...", "prompt": "...",
                  "dependsOn": [], "touches": [], "checklist": []}],
      "state": "pending",
      "proposedAt": "...", "deadline": "...", "decidedAt": null,
      "refus": null
    }
  }
}
```

A task's states and transitions:

```
queued --> running --> done
   ^         |  |
   |         |  +--> waiting --(response)--> running
   |         |  +--> blocked
   |         +--> failed
   +--- (launch failure, 1 retry)
```

`done`, `failed`, `cancelled` are terminal. `blocked` is terminal for this round but
reversible by relaunching. A task whose dependency dies moves to `blocked` through
propagation.

States of a proposition: `pending`, `accepted`, `rejected`, `expired`.

**`tmuxSession` is unique by construction.** The name is `ordo-<slug>` as long as no
existing campaign, open or closed, already holds it; otherwise `ordo-<slug>-<chantier_id>`.
Two projects with the same basename used to share a session, and their executors landed in
the same window.

**`tmuxWindow` is a stable tmux window identifier**, of the form `@12`, filled in by the CLI
on first launch. It exists because a bare `-t <session>` targets the session's *current*
window, which changes as soon as an attached human switches tabs.

**`claudeSessionId` is imposed at launch, never guessed.** Ordo draws a UUID and passes it
to `claude --session-id`. Inferring it after the fact, by looking for the directory's most
recent transcript, would be ambiguous as soon as two executors of the same campaign run on
the same project, which is the nominal case. This is the identifier that `resume` replays.

**`reaped` and `lastCapture` go together.** When a task moves to `done`, `tick` keeps the
tail of its pane's screen in `lastCapture`, closes the pane, and sets `paneId` to `null`. A
campaign window then shows only live work. `capture` serves `lastCapture` for a reaped task:
closing the pane loses the live scrollback, never the record. Only the `done` state is
reaped; `blocked` and `failed` keep their pane, since those are the ones a human wants to
look at. `keepPanes`, set at `start`, disables reaping.

**`permissions` is `skip` or `normal`.** `skip`, the default, launches executors with
`--dangerously-skip-permissions`; `normal` lets permission requests surface through the
report channel. The value in force is shown at every launch, never withheld.

**One `ORDO_HOME` per project.** `chantier.start()` refuses to open a campaign if another
open campaign in the same home carries a different `project`. The `home_partage` escape
hatch lifts the refusal; it does not silently bypass it (I8).

---

## 4. Non-negotiable invariants

Each one comes from a real, measured bug in the original repository. Breaking them brings
the bug back. **Every invariant carries a test that fails if it is removed.**

**I1. A task's checklist is a POST-condition.** It gates the launch of its **dependents**,
never its own. The reverse deadlocks: the only actor able to check a box is the session the
checklist would prevent from starting.

**I2. The report is the signal, never the pane's state.** A silent pane has not succeeded.
Without `reports/<campaign>/<task>.json`, the task is never `done`. An unreadable report
**blocks** the task; it never lets it pass for a success.

**I3. Every path goes through `canon()`** before being stored or compared. macOS resolves
`/var` to `/private/var`; comparing raw strings breaks everything, silently.

**I4. The prompt goes out via stdin or via a file, never as a positional argument.** The
`claude` CLI's variadic flags swallow everything that follows.

**I5. A pane is targeted by its tmux `pane_id` (`%12`), never by
`session:window.index`.** Closing a pane renumbers all the following ones; an instruction
would then land in the wrong executor. `pane_id` is stable for the pane's whole life.

**I6. `tmux send-keys` for the text and for `C-m` are two separate calls**, the text with
`-l`. A single call swallows the end of the text. `Escape` **interrupts** a Claude Code turn:
never send it.

**I7. A cycle abandons the graph.** Flattening it produces a plan that looks valid but
executes in the wrong order.

**I8. No silent refusal.** Every refused operation says which one and why. A rearrangement
that does nothing without saying so is worse than a failure.

**I9. `measured` and `declared` never mix.** A figure written by the orchestrator and then
read back by a sensor is not a measurement. Confusing the two manufactures the false
completion this mode exists to detect.

**I10. No default value in a sensor.** Whatever could not be measured goes into `unknown`
with its reason.

**I11. The journal distinguishes its three authors**, `ORDO`, `ORCH`, `USER`. A call made by
the orchestrator and journaled under `USER` is read back on restart as an order from the
human.

**I12. `--json` on every read verb**, and what the CLI serves is the **filtered** signal,
never the raw state. Serving `capteur.lastSuccess` raw displays as a measurement whatever an
unadopted sensor produced.

**I13. Briefs and reports are filed by campaign**, `briefs/<campaign>/<task>.md` and
`reports/<campaign>/<task>.json`, never flat. Task identifiers are unique per `ORDO_HOME`,
but a home shared by several projects mixed their files together with nothing in the path
saying which campaign they came from.

---

## 5. Module contracts

Signatures are indicative; the names matter, the implementation details do not.

### `ordo/store.py`

```python
def home() -> Path                      # ORDO_HOME or ~/.claude/ordo, creates the tree
def canon(p: str | Path) -> str         # realpath + expanduser ; canon("") -> ""
def now() -> str                        # ISO 8601 UTC, Z suffix
def load() -> dict                      # state, creates the skeleton if absent
def save(state: dict) -> None           # atomic write, tmp + os.replace
@contextmanager
def locked() -> Iterator[dict]          # exclusive lock + load + save on exit
def next_id(state, kind: str) -> str    # "t-01", increments seq
```

Lock: a `state.json.lock` file containing the pid. A lock whose pid no longer exists is
stolen after 30s. Bounded wait of 10s, then an explicit exception.

### `ordo/chantier.py`

```python
def start(project, objectif, perimetre="", hors_scope="") -> dict
def close(chantier_id, force=False) -> dict     # refuses if panes are alive, unless force
def add_task(chantier_id, titre, prompt, depends_on=(), touches=(), checklist=()) -> dict
def depend(task_id, on_id) -> dict              # refuses on cycle (I7)
def cancel(task_id) -> dict
def prioritize(task_id, n) -> dict
def amend(task_id, prompt) -> dict              # refuses if the task is running (I8)
def check(task_id, item_id, done=True) -> dict
def has_cycle(tasks: dict) -> list[str] | None  # returns the cycle found
def ready(chantier_id) -> list[dict]            # deps done AND their checklists checked (I1)
def propagate_failures(state) -> list[str]      # dependents of a dead task -> blocked
def graph_ascii(chantier_id) -> str             # human-readable text rendering in a terminal
```

`ready()` is the heart of I1: a task is ready when **all** its dependencies are `done`
**and** **all** the boxes in their checklists are checked. Its own checklist never enters the
calculation.

### `ordo/panes.py`

Topology chosen: **one tmux session per campaign, one pane per executor, `tiled` layout.**
The choice of pane within the window belongs to the user; the truncation trap is neutralized
by manual geometry, verified on August 9, 2026.

```python
def ensure_session(session, label=None) -> tuple[str, str]  # (session, window_id): new-session -d,
                                                            # window-size manual, attach/detach hooks
def spawn(window, cwd, cmd, title=None) -> str   # reuses the seed pane, otherwise split-window
def relayout(window) -> None                     # resize-window + select-layout tiled, checks and grows
def capture(pane_id, lines=80, join=True, warn_floor=False) -> str
def is_degraded(pane_id) -> bool                 # pane currently under PANE_MIN_USABLE_COLS/ROWS
def wait_ready(pane_id, timeout=PANE_READY_TIMEOUT_S) -> str  # blocks until the TUI is ready,
                                                             # or the trust dialog appears
def send(pane_id, text) -> None                  # send-keys -l THEN C-m, two calls (I6)
def alive(pane_id) -> bool
def busy(pane_id) -> bool                        # robust activity detection
def kill(pane_id) -> bool                        # False when there was nothing left to kill
def kill_session(session) -> None                # the named session only, never kill-server
def panes(window) -> list[dict]                  # pane_id, titre, largeur, hauteur, vivant,
                                                 # actif, sousPlancher
```

Geometry, order matters: `split-window`, then `resize-window`, then `select-layout tiled`. A
`select-layout` before `resize-window` lets one pane absorb all the width gained.

First window estimate, recalculated on every pane added: `side = ceil(sqrt(n))`,
`colonnes = 130 * side`, `lignes = 40 * side`, floor `130x40`. **This is only a starting
point, not a guarantee**: measured on August 9, 2026 against real tmux 3.7b panes,
`select-layout tiled` for n=2 in a 260x36 window produced two panes stacked full-width at
17/18 lines tall, not two panes side by side full-height as a `ceil(n / side)`-line formula
would suggest. tmux's `tiled` algorithm picks its own strip layout from the window's
width/height ratio, unpredictable by a closed-form formula. `relayout()` therefore checks the
actual result afterward against two separately measured floors, **120 usable columns and 30
usable lines per pane**, and grows the window by a factor of 1.3 (up to 5 attempts) as long
as those floors are not met, rather than trusting the starting formula.

`window-size manual` on the window: verified, the geometry survives an attach from an 80x24
client IF NOTHING ELSE steps in. Product decision from August 9, 2026, ergonomics 1:
`ensure_session()` additionally sets two tmux hooks **on the session**, never `-g` (the tmux
server is shared with the user's own sessions):

- `client-attached`: resizes the window to the exact size of the attaching client
  (`#{client_width}`/`#{client_height}`, resolved by tmux at the moment the hook fires, never
  when the hook is set), then replays `select-layout tiled`. A human who runs `tmux attach`
  then sees their whole window, with no cropping or scrolling.
- `client-detached`: restores the wide geometry by rerunning `relayout()` (never a value
  frozen at the moment of attaching, since the number of panes may have changed in the
  meantime), but **only if no client is attached any more** (`#{session_attached} == 0`,
  checked by the hook itself): a second human who is still watching must not have the window
  yanked away by the first one leaving.

Measured on August 9, 2026, tmux 3.7b, client simulated with `tmux new-session -d -x 80 -y
24 "env -u TMUX tmux attach -t <session>"` (the `env -u TMUX` is necessary: every pane, even
inside a detached session, already carries `TMUX` in its environment, and `tmux attach`
otherwise refuses with "sessions should be nested with care"):

| Moment | Window geometry | Panes (n=2) |
|---|---|---|
| before attaching (1 pane) | 130x40 | - |
| during attach (80x24 client) | 80x24 | 78x24 / 1x24 |
| after detaching | 260x80 | 168x80 / 91x80 |
| two clients attached, only one detaches | unchanged (the other client's) | - |
| the last client detaches | restored (260x80) | 168x80 / 91x80 |

The hooks run under `run-shell -b`: `resize-window -x/-y` does not expand `#{...}` formats
itself (verified: `resize-window -x '#{client_width}'` fails with "width invalid" outside
`run-shell`), and `-b` avoids blocking the tmux server while the hook runs. The
`client-detached` hook relaunches this module itself as a subprocess
(`python3 ordo/panes.py --relayout <session>`) rather than duplicating `relayout()`'s formula
in shell: it is the only place that applies the measured floors and the growth loop, a
duplicate would silently drift the next time the formula changes.

**Associated guardrail, not optional: a pane can fall back under the floor while a client is
attached to it.** `is_degraded(pane_id)` queries the pane's actual geometry and returns
`True` under `PANE_MIN_USABLE_COLS`/`PANE_MIN_USABLE_ROWS`. `capture(pane_id,
warn_floor=True)` (the CLI's `capture` verb) then prefixes a warning line
(`FLOOR_WARNING_PREFIX`, `"WARNING ordo"`) before the content; `panes()` carries the same
signal as a structured field `sousPlancher`, served by the `poll` verb. `warn_floor` defaults to `False`:
`busy()` and `wait_ready()` call `capture()` in a tight loop and should not pay for an extra
tmux request on every poll.

`ensure_session(session, label=...)` also renames the window to the campaign's name (its
`slug`) instead of tmux's default name, and sets `pane-border-status top` /
`pane-border-format '#{pane_title}'` **on the window**, never `-g`: every pane permanently
shows the title `spawn(title=...)` gave it. `spawn()` reassigns this title on EVERY call,
whether it reuses the seed pane or splits a new one (see the reassignment note below), so a
recycled pane never shows a previous task's name. The agreed title is
`"<task-id> <task-title>"` (e.g. `t-03 schema migration`); it is text displayed by tmux,
never a targeting identifier; I5 still applies to `pane_id` exclusively.

`busy()`: the `esc to interrupt` pattern alone is truncated in a narrow pane and produces a
false `idle`. Search the union of: `esc to interrupt`, `· ↓`, `tokens`, `Running`, `Reading`,
`Capturing`, `Synthesizing`, and the TUI's waiting gerunds. A pane is `busy` if one of them
appears in the last 20 captured lines.

`spawn()` builds the executor's command:
`cd <cwd> && claude --dangerously-skip-permissions [--model <m>] < /dev/null`; no: the brief
goes out by **file**, not by stdin or as a positional argument (I4). The pane launches
`claude` in interactive mode; **before injecting anything at all**, the caller waits on
`wait_ready()`, then `send()` injects `Read <brief> and apply it.`

`wait_ready()` blocks until `claude`'s TUI is genuinely ready to receive text, or 30s at
most (polling every 0.5s): injecting before that landed in a pane where the TUI did not yet
exist and got lost. Two markers, searched across the pane's **entire** capture (the ready
banner is at the top, the input box at the bottom; a fixed-length tail misses the first one):
`"Claude Code v"` (TUI ready, returns `"pret"`) or `"Quick safety check"` / `"Yes, I trust
this folder"` (tmux trust dialog, returns `"confiance"`, checked first on every poll).
Measured on August 9, 2026, tmux 3.7b: the ready banner takes between 1.9s and 5.3s to appear
in an already-approved directory; the trust dialog, between 1.0s and 1.3s in a directory
never opened.

**Trust dialog: Ordo never approves a directory automatically, it escalates.** Product
decision from August 9, 2026. When `wait_ready()` returns `"confiance"`, `launch` sends **no
key at all**, neither Enter nor Escape: the task moves to `blocked` with a human-readable
reason, and `controle.wake_reasons()` surfaces it under the reason `trust-expected`, distinct
from a dead pane.

### `ordo/report.py`

The report channel is a file whose path is written into the brief. `AskUserQuestion` does
not exist outside interactive mode and the environment does not propagate; the file always
arrives.

```json
{"task": "t-01", "state": "done", "note": "a sentence",
 "checked": ["c1"], "question": null, "touched": ["db/schema.sql"]}
```

`state` is `done`, `blocked`, `asking` or `progress`.

```python
def path(task_id, chantier_id) -> Path   # reports/<campaign>/<task>.json, dir created (I13)
def read(task_id, chantier_id) -> dict | None    # None if absent
def parse(raw: str) -> dict              # lenient parser
def apply(state, task_id, report) -> list[str]   # returns the events
def clear(task_id, chantier_id) -> None
```

`parse()` absorbs what a model actually produces: prose around the JSON, a Markdown
` ```json ` block, uppercase keys, a string where a list is expected, a trailing comma. It
**raises** on what it does not understand; `apply()` translates a raise into `blocked` with
the reason (I2).

### `ordo/plan.py`

```python
def propose(chantier_id, pave: str, model="sonnet") -> dict   # -> proposition
def accept(prop_id) -> dict            # materializes, refuses on cycle (I7)
def refuse(prop_id, raison) -> dict
def expire_due(state) -> list[str]     # auto-acceptance after 45s
def waves(tasks) -> list[list[str]]    # disjoint zones AND same project AND parallelizable
```

Model call: `claude -p --output-format json --json-schema <schema> --model <m>`, prompt via
**stdin**. The schema forces `{"taches": [{"ref","titre","prompt","dependsOn","touches",
"checklist","parallelisable"}]}`.

`accept()` translates a **zone conflict into a real dependency**, otherwise the separation
is lost as soon as scheduling only looks at `dependsOn`. The model is systematically
optimistic about parallelism: `waves()` cross-checks `touches` and only puts two tasks in the
same wave if they declare themselves parallelizable **and** their zones are disjoint.

A zone is a **free-form string**, not necessarily a path: `db-test` and `staging` are valid
zones.

Auto-acceptance: 45s after the proposal, `expire_due()` accepts it. No exception for an
irreversible task; the barrier is the executor's brief, which forbids the irreversible act
without a question.

### `ordo/capteur.py`

Output contract imposed, method free. The script writes a JSON object to stdout and nothing
else.

```json
{"at": "...", "ok": true,
 "measured": [{"name": "stories", "value": 12, "unit": "files"}],
 "declared": [{"name": "proof", "value": "42/379", "source": "docs/mission.md"}],
 "drift":    [{"kind": "contradiction", "task": "t-04", "detail": "..."}],
 "unknown":  [{"name": "coverage", "why": "no report generated"}]}
```

```python
def install(chantier_id, script_path) -> dict   # copies into sensors/, chmod +x
def run(chantier_id, timeout=20) -> dict        # bounded execution, output capped at 256 KiB
def status(chantier_id) -> dict                 # FILTERED signal (I12)
def adopt(chantier_id) -> dict                  # 3 concordant runs + validation
def due(chantier_id, every=120) -> bool
```

Hard rules:
- **Read-only.** The sensor writes nothing, runs no build and no test suite.
- **Hard timeout** on Ordo's side, bounded output. A slow sensor never blocks the cycle.
- Adoption: **3 concordant runs plus a human validation.** Before that, `status()` returns
  `{"adopted": false, "signal": "unknown"}` and **serves no measurement** (I12).
- Failure: `unknown` with the reason and the time of the last success. **After 2 consecutive
  failures**, the orchestrator is woken to fix its script.
- The same output over N cycles counts as a **frozen sensor**, not a blocked task. The two
  are distinct, and confusing them is exactly the defect this contract exists to prevent.
- The script's shebang is honored: a Python sensor must run as Python.
- Location: `ORDO_HOME/sensors/`, **outside the audited repo**.
- Storage key: `chantier["id"]`, **never a project path**. A diverging key would make
  detection go silent with no signal at all.

### `ordo/controle.py`

```python
def scope_drift(chantier_id) -> list[dict]      # declared touches vs files written
def fausse_completion(chantier_id) -> list[dict]  # declared with no matching measured
def wake_reasons(chantier_id) -> list[dict]     # full state, pure function, no side effect
def wake_new(chantier_id) -> list[dict]         # only the reasons not yet served
def tick(chantier_id=None) -> dict              # full reconciliation, returns the events
```

`wake_reasons()` recomputes the entire current state on every call: served as-is by
`tick()`, it used to spit out one reason per finished task on every round, and the single new
reason drowned in it. `wake_new()` filters on a `wakeSeen` marker carried by the campaign,
and marks what it serves. A purely time-based reason like `control-round` is **never**
marked: it fires on elapsed time rather than on a new fact, marking it would snuff it out on
the very first round. Defect found in a real run, not by a unit suite.

`tick()` closes the loops, in this order:

1. `expire_due()`: proposals whose 45s have elapsed.
2. For every `running` task, **the report is read first**. If it is terminal, act on it
   without looking at the pane (I2).
3. Otherwise, a pane dead beyond the no-report delay: `blocked`, reason written.
4. A `waiting` task whose question is answered: the answer is injected into the pane.
5. `propagate_failures()`.
6. Sensor due: run, adoption, frozen, double failure.
7. `scope_drift()` and `fausse_completion()`.
8. All facts written to the journal under `ORDO`.

An exception raised by a broken campaign **must never kill `tick()`** for the others: a
campaign in error is caught, journaled, and the round continues. This exact defect already
killed the dispatch for every project in the original repository.

`wake_reasons()`: terminal report, question for the human, drift detected, sensor double
failure, dead pane, tmux trust dialog pending (`trust-expected`, see `ordo/panes.py` above),
plus a **control round if nothing has surfaced in 15 minutes**.

### `ordo/journal.py`

```python
def write(chantier_id, auteur, texte) -> None   # auteur in {"ORDO","ORCH","USER"} (I11)
def read(chantier_id, limit=None) -> list[dict]
def brief(chantier_id) -> str                   # regenerated brief
```

On-disk format, one timestamped fact per line:

```
14:02  ORDO  graph accepted, 8 tasks
14:19  ORCH  t-04 before t-03: t-03 is waiting on the migration, we don't block the rest
14:31  ORDO  drift t-04, correction filed
14:40  USER  don't touch prod
```

`ORDO` writes the observable facts, for free. `ORCH` writes the **why**, one line, only when
it decides something non-obvious. The why is exactly what a restart loses.

`brief()` composes, in this order: goal and scope, current graph with states and
dependencies, notes from finished reports, sensor output from the last N cycles, decision
journal, the human's last messages.

### `ordo/prompt.py`

```python
def brief_executante(task_id) -> str    # writes briefs/<campaign>/<task>.md, returns the path
def contrat_role() -> str               # role reminder, injected at a campaign's start
```

An executor's brief: **the orchestrator gives the substance** (goal, scope, traps), **Ordo
assembles** the rest (project rules, report protocol, report file path, checklist to check
off, prohibitions).

Two guardrails come from measured traps, not a style choice, and a test fails if they are
removed:
- **`AskUserQuestion` is forbidden** in an executor: the tool is absent outside interactive
  mode, the session looks for it then gives up without delivering anything. It writes
  `state: "asking"` in its report and **ends its turn**.
- **self-validation is forbidden**: an alignment guarantor that approves its own graph
  guarantees nothing anymore.

### `ordo/cli.py`

`ordo <verb> [args]`, `--json` on **every** read verb (I12). Human-readable output in
English; `--json` output stable and documented. Non-zero exit code on refusal,
with the reason on stderr.

| Group | Verbs |
|---|---|
| Campaign | `start`, `list`, `show`, `close`, `brief` |
| Graph | `add`, `dep`, `graph`, `ready`, `cancel`, `priority`, `amend`, `check` |
| Plan | `plan`, `proposals`, `accept`, `reject` |
| Execution | `launch`, `say`, `capture`, `poll`, `watch`, `kill`, `relaunch`, `resume`, `attach` |
| Signal | `tick`, `report`, `ask`, `answer`, `questions` |
| Sensor | `sensor install`, `sensor run`, `sensor status`, `sensor adopt` |
| Journal | `journal add`, `journal show` |
| Diagnostic | `doctor` |

`poll` is the verb the orchestrator calls the most: one line per live executor, with state,
activity, report presence and the pane's last useful line.

`tick` also serves the wake-up reasons, never in a separate verb: the orchestrator only
calls `tick` back when it takes the turn back, and a reason to escalate that it did not serve
would stay invisible. In `--json`, a `wakeups` key is added to the reconciliation result
(campaign to a list of reasons `{kind, task, detail}`); in human-readable output, each reason
prints a line prefixed `WAKEUP`.

By default `tick` goes through `wake_new()` and therefore serves only the new reasons. `tick
--all-wakeups` goes through `wake_reasons()`: full state, no marking.

---

## 6. Test discipline

- `tests/test_<module>.py`: unit-level, no third-party dependency, isolated via
  `ORDO_HOME`. One suite per module in the package, discovered by
  `python3 -m unittest discover tests/` from the repository root. The
  `discover -s tests -t .` form fails, `tests/` is not a package.
- No test touches the real `~/.claude/ordo`, nor a real `claude` process.
- The tmux tests use real panes with `bash`, never `claude`.
- **Every invariant I1 through I13 carries a named test that fails if the invariant is
  removed.**
- `tests/mutation_check.sh` applies one mutation per invariant and requires a red test. A
  test that passes under mutation is decor, and gets said so.
- `tests/e2e.py`: full walkthrough against real tmux panes, checks read back **from disk**,
  never from the return value.

---

## 7. What was verified before writing this spec

| Fact | Verification |
|---|---|
| `window-size manual` + `resize-window` survives a client attach at 80x24 | window stayed 400x60, panes 199/200/400 unchanged |
| A 204-character line is not wrapped in a 259-column pane | `capture-pane` renders one line of 204 |
| The same line wraps as 140 + 64 in a 140-column pane | hence the 120-column floor per pane |
| `select-layout tiled` before `resize-window` leaves one pane at 259 while two are at 140 | hence the imposed order |
| `tmux -V` = 3.5a during the first measurements, 3.7b after a Homebrew update that landed partway through the work | `/opt/homebrew/bin/tmux`, identical conclusions on both versions |
| Ergonomics 1, August 9, 2026: `client-attached`/`client-detached` hooks on the session, with an 80x24 simulated client | window 130x40 → 80x24 during attach → 260x80 after detach (see section 5, dedicated table) |
| `resize-window -x`/`-y` does not expand `#{client_width}`/`#{client_height}` itself | fails with "width invalid" outside `run-shell`; works inside `run-shell -b "tmux resize-window ..."` |
| A second attached client blocks restoration when the first detaches | window unchanged (the still-attached client's) as long as `#{session_attached} != 0` |
| A nested `tmux attach` refuses without `env -u TMUX`, even from a detached session | error "sessions should be nested with care", every pane already carries `TMUX` in its environment |
