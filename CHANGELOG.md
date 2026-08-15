# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ordo poll` reports the tokens an executor actually spent, summed from its own Claude Code
  transcript and read incrementally rather than from the start on every poll. A
  transcript that cannot be found reports unknown, never zero: a `running` task showing zero
  tokens is a false measurement, and the difference is the whole point.

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
