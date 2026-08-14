# Ordo

[![tests](https://github.com/tutozz/ordo/actions/workflows/tests.yml/badge.svg)](https://github.com/tutozz/ordo/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Ordo turns one interactive Claude Code session into an **orchestrator**. It holds the goal,
the task graph and the alignment; it does not write code. The work is done by **executor
sessions**: real `claude` processes, each running in its own tmux pane, that Ordo launches,
reads, redirects and reconciles.

It is a skill plus a small Python CLI. No daemon, no server, no GUI, no third-party Python
dependency.

## Read this before you install

Ordo starts **real Claude Code sessions that spend real tokens**, in your repository, and
by default it starts them with `--dangerously-skip-permissions`. That default exists
because an executor stopped by a permission prompt in a detached pane blocks forever with
nobody to answer it. It is still a default that hands autonomous write access to a model.

- Set `--permissions normal` on `ordo start` if you want prompts back. Executor sessions
  then ask, and their questions reach you through the report channel.
- Executor sessions running in tmux panes are **not isolated in git worktrees**. Two
  executors on the same repository can overwrite each other's work. Declare disjoint zones
  per task, or run one executor at a time.
- Ordo never runs `tmux kill-server` and never runs `pkill tmux`. It only destroys objects
  it created, one by one, by name. Do not add code that does otherwise: a stray
  `kill-server` once destroyed a user's unrelated work session and the process inside it.

## Requirements

| | |
|---|---|
| Python | 3.10 or later, standard library only |
| tmux | 2.9 or later (`window-size manual`, `resize-window -A`, `set-hook -t`) |
| Claude Code | the `claude` binary on your PATH |
| OS | Unix. macOS and Linux are supported, Windows is not |

Run `ordo doctor` after installing; it checks all of the above and tells you what is
missing.

## Install

As a Claude Code plugin, which is the shortest path:

```
/plugin marketplace add https://github.com/tutozz/ordo.git
/plugin install ordo@ordo
```

The plugin loads the skill and puts `bin/` on the PATH of Claude Code's own Bash tool, so
an orchestrator session can call `ordo` by name. It does **not** put `ordo` on your shell's
PATH: to run `ordo doctor` or `ordo watch` in your own terminal, install from a clone as
well, or call `bin/ordo` by its path.

From a plain git clone, which is what you want if you also drive Ordo by hand:

```bash
git clone https://github.com/tutozz/ordo.git
cd ordo
./install.sh
```

`install.sh` links the `ordo` command into `~/.local/bin` and the skill into
`~/.claude/skills/ordo`. Nothing else is written until you start a campaign.

## One ORDO_HOME per project

A **campaign** is one long piece of work on one repository: a goal, a scope, a task graph,
a journal. Ordo keeps every campaign in `ORDO_HOME`, which defaults to `~/.claude/ordo`.
That directory holds `state.json`, the briefs sent to executors, the reports they write
back, and one journal per campaign. Briefs and reports are filed per campaign, under
`briefs/<campaign>/<task>.md` and `reports/<campaign>/<task>.json`, so two campaigns in one
home never write to the same file.

**Still, use one `ORDO_HOME` per project.** A single home shared by several projects
serializes their state writes behind one lock and lets `ordo tick` in project A consume the
wake-up signals of project B.

```bash
cd ~/code/myapp
export ORDO_HOME=$PWD/.ordo    # add .ordo/ to .gitignore
```

Opening a campaign in a home that already holds an open campaign on a different repository
is refused, and the conflict is named. `--shared-home` lifts the refusal when you mean it.

## Quickstart

```bash
cd ~/code/myapp
export ORDO_HOME=$PWD/.ordo

# the first argument is a PATH to an existing directory, not a project name;
# the campaign takes its name from that directory's basename
ordo start $PWD --goal "integration tests pass on /stock" \
                --scope "server/stock, its tests" \
                --out-of-scope "the client, the schema migration"

ordo add c-01 --title "fix the stock reducer" \
              --prompt "..." --touches server/stock --check "tests green"

ordo ready c-01          # what can start now
ordo launch t-01         # creates the pane, starts claude, injects the brief
ordo attach c-01         # prints the exact command to watch it work
ordo watch c-01          # read-only event stream, one line per new fact
ordo tick                # reconcile: reports, dependencies, drift, wake-ups
```

When a task reaches `done`, its pane is closed and the tail of its screen is kept in the
state, so a campaign window shows live work and nothing else. Blocked panes stay open,
`ordo capture` still serves a finished task's last screen, and `--keep-panes` on `start`
turns reaping off. Reopening a finished executor with `ordo resume <task>` reloads its whole
context and is the most expensive move available; a new task with a clean brief is almost
always cheaper.

`ordo watch` is what keeps a campaign moving. An orchestrator that launches an executor and
ends its turn has nothing to bring it back: the executor finishes, or drifts, or dies, and
nobody reads its report until a human notices that hours went by. `watch` prints one line
whenever something happens, so the orchestrator can be woken by its own harness instead of
having to remember. It reads only, never touches the state, and exits on its own once
nothing of that campaign is alive.

Launching prints the tmux session, the pane, the brief path and the permission mode, plus
the exact `tmux attach` command. Nothing about the tmux layer is hidden from you.

**The first launch in a directory Claude Code has never opened stops on its trust dialog**,
even with permissions skipped. Ordo refuses to answer that dialog for you, ever: it leaves
the pane untouched, reports the task blocked, and prints the attach command so a human can
go and choose "Yes, I trust this folder" themselves. Relaunch the task once they have.

## How it works

| Role | Holds | Does not do |
|---|---|---|
| You | intent, business calls, authorization for anything irreversible | execution, monitoring |
| Orchestrator | goal, scope, definition of done, state, guardrails | producing the work |
| Executors | the doing, against a precise contract | deciding intent, leaving scope |

Three invariants drive the whole design:

1. **Alignment is captured before delegating.** An executor launched on a fuzzy contract
   produces out-of-scope work, and nobody sees it before the end.
2. **It is maintained during.** The orchestrator reads every report as it lands; drift is
   not discovered at the final report.
3. **Proof beats declaration.** An executor that says it is done is not done. The signal is
   the report file it wrote, never the state of its pane. A silent pane is a blocked task,
   not a finished one.

A **sensor** (`ordo sensor`) is the optional counterweight to the graph: the graph says what
executors *declare*, the sensor says what is *measured*. Confusing the two manufactures
false completion. A sensor needs three concordant runs and a human validation before its
signal counts for anything.

## Watching an executor

```bash
ordo attach c-01                 # prints the command, does not attach for you
tmux attach -t ordo-myapp        # from another terminal
```

Panes carry a permanent title with the task id and title. The window geometry is pinned so
that attaching from a small terminal does not truncate what Ordo reads back; see
`skills/ordo/references/tmux.md` for the measurements behind that.

## Tests

```bash
ORDO_HOME=$(mktemp -d) python3 -m unittest discover tests/   # unit suite
ORDO_HOME=$(mktemp -d) python3 tests/e2e.py                  # against real tmux panes
bash tests/mutation_check.sh                                 # proves the tests are not decor
```

`mutation_check.sh` mutates the package, checks that a test fails for each mutation, then
restores. It is the only check that proves the suite has teeth. Run it after any fix.

## Documentation

| File | What it is |
|---|---|
| `ARCHITECTURE.md` | the module map: who owns what, who may call whom |
| `CONTRIBUTING.md` | how to work on Ordo without breaking its guarantees |
| `skills/ordo/SKILL.md` | the orchestrator's role contract, loaded by Claude Code |
| `skills/ordo/references/tmux.md` | tmux playbook: measured traps, verified counter-examples |
| `skills/ordo/references/sensor.md` | the sensor contract and its hard rules |
| `skills/ordo/references/harness.md` | Claude Code CLI behaviour under tmux orchestration |
| `docs/SPEC.md` | full implementation contract |

Every document is in English. Code comments and commit messages are in French; see
`CONTRIBUTING.md`.

## License

MIT. See `LICENSE`.
