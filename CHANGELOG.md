# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ordo map <campaign>` writes a self-contained HTML page of the campaign: one band per
  phase, every phase on screen at once, tiles ordered by dependency depth inside each band.
  Clicking a task opens its prompt, checklist, blocking dependencies, report and the
  orchestrator journal lines that name it. No server, no third-party asset, no write to
  `state.json`. `--watch` refreshes it in a loop, `--pane` runs that loop in a dedicated
  tmux window of the campaign session, `--open` opens it in the browser, `--json` serves the
  same model as data (I12).
- Hovering a task marks its whole transitive chain, green upstream and blue downstream, and
  dims the rest; clicking opens the detail in place. Arc coordinates are computed by the
  browser from the real position of the rows, since the layout reflows with the window.
  Search box, `reste`/`tout` filter and `graphe`/`liste` toggle; `reste` folds finished
  phases and dims settled work rather than hiding anything, because a task that disappears
  is a bearing lost.
- The page carries the campaign as JSON and puts it in the DOM through `textContent` only.
  A task title is text a model wrote; concatenating it into markup is how it ends up
  executing.
- `ordo group <campaign> <key> <label> --why "..."` names a phase and says what it serves.
  A phase named with no task yet is drawn as announced-but-not-cut, which is what shows that
  a six-phase campaign has only cut one. Membership stays derived from the numeric prefix of
  task titles; only the label and the why are stored, under a `groupes` key that older
  states simply do not have. Renaming a phase never erases its why.
- `ordo add --why` and `ordo why <task> "..."` record why a task exists and why there in the
  split. The title says what, the prompt says how, and neither says why - the one thing
  nobody can reconstruct afterwards. The map counts what nobody explained and never fills it
  in from the title.
- The map header answers what a graph alone cannot: which tasks exhaust the graph as it
  stands, which phases are announced but not cut, and which phases the goal names that
  nobody has even announced.
- The map calls out what makes a graph lie: a `done` task whose checklist is not ticked and
  which therefore still blocks its dependants (I1), a `running` task whose pane is gone, two
  same-depth tasks sharing a declared zone, a dangling dependency id, a cycle, and a closed
  campaign that still holds unfinished tasks.
- `ordo serve` runs one local map server on `127.0.0.1:9123` for every campaign of every
  project, registered through `~/.claude/ordo-serve.json`. `ordo watch` starts it if nothing
  answers, so the first campaign of the day lights it up and the next ones only register;
  `--no-serve` and `ORDO_NO_SERVE` turn that off. The page polls its own server and redraws
  only what changed, so scroll position, open task and filters survive a refresh. Read-only:
  there is no POST, the `home` parameter is checked against the registry and the `Host`
  header against loopback names, which is what closes DNS rebinding.
- `ordo poll` reports the tokens an executor actually spent, summed from its own Claude Code
  transcript and read incrementally rather than from the start on every poll. A
  transcript that cannot be found reports unknown, never zero: a `running` task showing zero
  tokens is a false measurement, and the difference is the whole point.

### Fixed

- `launch` targeted the campaign's tmux **session** where it meant its **window**, so it
  acted on whichever window was current. Harmless while a campaign session had exactly one
  window; `ordo map --pane` adds a second, and an executor could then be spawned into the
  map's window, recycle the refresher's pane, and leave `relayout` resizing the wrong
  window while the executors kept a stale geometry. `spawn`/`relayout` now take the
  window id, as `resume` already did. A service window is also skipped when resolving a
  campaign's window, and the work window is recreated if reaping left the session holding
  nothing but the map.

## [0.1.0] - 2026-08-15

First public release. Ordo ran privately before this; nothing below is an upgrade path from
a published version, it is what the first one contains.

### The orchestration substrate

- One tmux session per campaign, one pane per executor session, tiled layout with a pinned
  geometry so that a human attaching from a small terminal never truncates what Ordo reads
  back.
- A task graph with dependencies, declared zones, post-condition checklists, cycle
  detection, and failure propagation confined to its own campaign.
- File-based report channel. The report is the signal, never the state of the pane: a silent
  pane is a blocked task, not a finished one.
- Questions travel from executors to the orchestrator, not to the human. Only architecture,
  business calls, money, irreversible actions, scope drift and human-only information
  escalate further.
- Optional sensor: what is measured, kept strictly apart from what executors declare. Three
  concordant runs plus a human validation before its signal counts at all.
- Reconciliation round (`tick`) with wake-up reasons served once, so a new reason never
  drowns in a list of already-seen ones.
- Thirteen invariants, each from a real measured bug, each carrying a test that fails if the
  invariant is removed. `tests/mutation_check.sh` proves the suite is not decor, and reports
  16 of 16 cases proven.
- Briefs and reports are filed per campaign, `briefs/<campaign>/<task>.md` and
  `reports/<campaign>/<task>.json`. Task identifiers are unique per `ORDO_HOME`, but the two
  directories used to be flat: nothing in `reports/t-01.json` said which campaign it came
  from, and a home shared by two projects mixed their files. Invariant I13 covers it;
  removing the campaign segment turns the test red.

### Transparency, deliberately

- `launch` prints the tmux session, the attach command, the pane and its title, the brief
  path and the permission mode in force.
- **A finished executor closes its own pane.** When `tick` sees a task reach `done`, it
  keeps that pane's tail in the state and closes it, so attaching to a campaign shows live
  work and nothing else instead of a wall of finished sessions. Only `done` is reaped;
  `blocked` and `failed` panes stay open, they are the ones worth looking at. `ordo capture`
  serves the kept tail for a reaped task, and `--keep-panes` on `start` turns it off.
- `ordo resume <task>` reopens a reaped executor on its own claude session, with its context
  intact. Executors are launched with a pinned `--session-id`, so the conversation to resume
  is known rather than guessed from the newest transcript in the directory, which is
  ambiguous as soon as two executors share a project. Resuming reloads an entire context and
  is the most expensive move in the system: Ordo never does it on its own, and both the CLI
  and the skill say so at the point of use.
- `ordo watch <campaign>`, a read-only event stream, one line per new fact: a report landed,
  a pane died, a turn ended with no report, a question was asked, a pane went quiet for too
  long. It closes the loop that made campaigns stall. An orchestrator that launches an
  executor and ends its turn has nothing to bring it back, so drift was caught at the final
  report and the next ready task waited until a human thought to ask. Armed under the
  harness's watcher, every line wakes the orchestrator, which then reconciles with `tick`.
  It never writes to the state and never consumes a wake-up reason, so it cannot starve the
  reconciliation it exists to trigger.
- `ordo attach` prints the exact command to watch an executor work, and never attaches for
  you, which would hijack the orchestrator's own terminal.
- `ordo doctor` reports Python, tmux, the `claude` binary, the active `ORDO_HOME` and every
  open campaign sharing it.
- `--verbose` traces every tmux command on stderr. `--version` exists.
- `cancel` says the pane and the `claude` process keep running. `kill` says they are gone,
  and says "already dead" when there was nothing left to kill. `close` lists what it killed
  and what it archived, or says plainly that nothing was killed.
- The brief an executor receives states that it is a real Claude Code session in a tmux
  pane, and carries the project, the working directory, the session and its report path.

### Safety

- Executors start with `--dangerously-skip-permissions` by default, and that default is now
  printed at every launch instead of being silent. `--permissions normal` turns prompts back
  on.
- Ordo never answers Claude Code's folder-trust dialog for you. The pane is left untouched,
  the task is reported blocked, and the attach command is printed so a human can decide.
- `tmux kill-server` and `pkill tmux` appear nowhere. Only named objects Ordo created are
  destroyed, one at a time.

### Isolation

- One `ORDO_HOME` per project. Opening a campaign in a home that already holds an open
  campaign on a different repository is refused and names the conflict; `--shared-home`
  lifts it explicitly.
- tmux session names are unique by construction, so two projects with the same directory
  basename no longer share a session.
- Every tmux call that acts on a window targets a stable window identifier, never the bare
  session name, which resolves to whatever window a human is currently looking at.
- `tmux has-session` and its siblings use exact-match targets; prefix matching let a short
  name silently resolve to a longer, unrelated session.
- Closing a campaign archives its briefs, reports, journal and sensor under
  `archives/<campaign>/`.

### Known limitations

- Executor sessions in tmux panes are not isolated in git worktrees. Two executors on the
  same repository can overwrite each other. Declare disjoint zones per task, or run one at a
  time.
- Unix only. Windows is not supported.
- Pane activity detection matches strings from the Claude Code TUI, measured on v2.1.226. A
  wording change upstream can turn a busy pane into an idle-looking one.

### Distribution

- `.claude-plugin/marketplace.json` makes the repository its own plugin marketplace, so
  `/plugin marketplace add` followed by `/plugin install ordo@ordo` works against the clone
  URL directly. The plugin route puts `bin/` on the PATH of Claude Code's Bash tool, not on
  the user's shell PATH; `install.sh` is still what makes `ordo` callable from a terminal.

### Fixed, and worth the detail

- **A background relayout could make an unrelated command fail.** When a human detaches
  from a campaign's session, a tmux hook re-runs this package in the background to restore
  the wide reading geometry. If the window had gone away in the meantime, that background
  process died loudly; tmux had no client to show the error to, held it, and handed it to
  the stderr of the *next* command talking to the server, which then failed despite having
  fully succeeded. Measured 2026-08-11: the full test suite passed 1 run out of 5 before
  the fix and 7 out of 7 after. In real use, the same mechanism could make an `ordo say`
  or `ordo capture` raise right after a human closed their terminal. The background
  relayout is now best-effort and silent, the single deliberate exception to this project's
  no-silent-failure rule: there is no one to inform, and nothing depends on it.
