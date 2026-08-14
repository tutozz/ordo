# tmux topology for executor sessions

This entire document is empirical. Measurements marked "verified" were run on 2026-08-09,
`/opt/homebrew/bin/tmux`, macOS 27. The first measurements of the day were taken on tmux
3.5a; those taken after a Homebrew upgrade mid-campaign were taken on tmux 3.7b; each
section states which. The conclusions do not diverge between the two.

Under normal circumstances you do not need this file: `ordo launch`, `ordo say`,
`ordo capture`, and `ordo poll` do the right thing. Read it when something misbehaves.

---

## 1. The layout chosen

**One tmux session per campaign, one pane per executor, `tiled` layout.**

```
tmux attach -t ordo-myapp
+---------------------+---------------------+
| t-01 migration      | t-02 api            |
+---------------------+---------------------+
| t-03 tests          | t-04 doc            |
+---------------------+---------------------+
```

The user can attach at any time to watch the executors work. That is the whole point of
this layout, and it comes with a cost that must be neutralized: a narrow pane breaks
readability.

`ensure_session()` creates the session with a single pane, an unused login shell. The
first executor of a campaign **reuses this seed pane** instead of opening a second one
next to it: `spawn()` runs `respawn-pane -k` on it, which replaces its process without
changing its `pane_id`. Every subsequent executor of the same campaign, by contrast, gets
a real `split-window`. Without this reuse, the seed pane would stay an empty shell taking
up half the window for the entire life of the campaign, next to the single live executor.

---

## 2. The width trap, and its fix

The Claude Code TUI reflows its display to the pane width. Below about 120 columns, status
lines get cut off, and the `esc to interrupt` pattern disappears from the capture. A naive
activity detector then concludes there is a **false idle**, and the orchestrator reins in
an executor that was working perfectly well.

Measured: a 204-character line comes out whole from a 259-column pane, and comes out split
into 140 + 64 from a 140-column pane.

The fix comes down to two settings:

```bash
tmux new-session -d -s ordo-myapp -x 260 -y 72
tmux set-window-option -t ordo-myapp window-size manual
```

`window-size manual` decouples the window geometry from that of the attached client.
**Verified**: after attaching from an 80x24 client, the window stayed at 400x60 and the
panes kept 199, 200, and 400 columns. The user sees a portion and scrolls; the executors
do not shrink.

Without this setting, the user's attach would overwrite the geometry and break readability
for every executor in the campaign at once.

This setting alone has a cost for the human who attaches: they see a portion of a wide
window and must scroll, all the time, even while actively watching. See section 11 for the
fix (tmux hooks that make the window follow the client during attachment, and restore it
on detach) and section 12 for the guardrail that goes with it (a pane can drop below the
floor while someone is attached; the read must say so).

---

## 3. The order of the three commands

**`split-window`, then `resize-window`, then `select-layout tiled`.** This order is not a
preference.

Measured by reversing it: a `select-layout tiled` applied before the enlargement leaves one
pane at 259 columns while two others sit at 140. The pane created first absorbs all the
width gained, and the layout is not replayed to redistribute it.

First estimate of the window, recalculated on every pane added:

```
side     = ceil(sqrt(n))
columns  = 130 * side
rows     = 40 * side
```

Floor: `130x40`. **This formula is only a starting point, not a guarantee**: the same
multiplication `side` serves both dimensions; it is not "side columns x ceil(n / side)
rows" as an earlier version assumed. Measured 2026-08-09, tmux 3.7b: for n=2 in a 260x36
window, `select-layout tiled` produced two panes **stacked full-width at 17 and 18 lines
tall**, not two panes side by side at full height. tmux's `tiled` algorithm picks its own
strip layout from the window's width/height ratio; no closed-form formula predicted it
reliably (for `side=3` in particular, the same pane count flipped between a roughly square
grid and a narrow column plus N stacked panes, based solely on window height).

`relayout()` therefore does not trust this formula: after `resize-window` +
`select-layout tiled`, it **actually measures** each pane and checks two distinct floors,
**120 usable columns and 30 usable rows**. If a live pane misses either one, the window
grows by a factor of 1.3 and the cycle restarts, up to 5 attempts before giving up with an
explicit error. A given geometry is also re-checked a few hundred milliseconds apart before
being accepted: a layout was seen correct right after `select-layout`, then fell back to
the pre-resize geometry the very next instant, with no additional command issued between
the two reads.

---

## 4. Waiting for the TUI to be ready

`claude` renders nothing at the instant `spawn()` returns: the process starts, but its
interface does not exist yet. Injecting the instruction right away sends it into the void.
`wait_ready(pane_id)` polls the pane every 0.5s (`PANE_READY_POLL_INTERVAL_S`), up to 30s
(`PANE_READY_TIMEOUT_S`), and only returns once one of the two following markers is found
in the **entire** capture of the pane (never a fixed tail: the ready banner is at the top
of the screen, the input box at the bottom, with blank lines between them that push the
banner out of a last-40-lines window):

| Marker | Meaning | Value returned |
|---|---|---|
| `Claude Code v` | the TUI has rendered its banner and input box | `"pret"` |
| `Quick safety check` or `Yes, I trust this folder` | trust dialog displayed | `"confiance"` |

The trust marker is checked **first** on every poll: in the one case where a capture would
satisfy both patterns at once, treating it as the dialog is the only direction that never
risks injecting text into an unhandled security prompt.

Measured 2026-08-09, tmux 3.7b: in an already-approved folder, the ready banner appears
between **1.9s and 5.3s** after launch, across three distinct launches. In a folder never
opened by Claude Code, the trust dialog appears between **1.0s and 1.3s**, faster since
nothing else needs to load first.

If neither marker appears before the timeout, `wait_ready()` raises: an explicit launch
failure is better than a blind injection into a pane whose state nobody confirmed.

---

## 5. The Claude Code trust dialog

An executor launched in a folder Claude Code has never opened displays:

```
Accessing workspace:
<path>
Quick safety check: Is this a project you created or one you trust?
(Like your own code, a well-known open source project, or work from
your team). If not, take a moment to review what's in this folder
first.
Claude Code'll be able to read, edit, and execute files here.
Security guide
> 1. Yes, I trust this folder
  2. No, exit
Enter to confirm . Esc to cancel
```

**Product decision, made explicitly on 2026-08-09: Ordo never approves a folder
automatically; it escalates.** When `wait_ready()` returns `"confiance"`, `launch` sends
**no keystroke**, neither Enter nor Escape; the pane stays intact, exactly in the state the
dialog left it in. The task moves to `blocked` with a human-readable reason, and
`controle.wake_reasons()` surfaces it under the wake-up reason `confiance-attendue`,
distinct from a dead pane (the pane is alive, it is waiting on a decision) and from scope
drift (nothing was written).

On this wake-up reason, the only correct action is human: attach to the tmux session
(`tmux attach -t <session>`) and choose "Yes, I trust this folder" or "No, exit" yourself.
An orchestrator that receives this reason does not relaunch anything and does not decide on
the executor's behalf; it surfaces it.

---

## 6. Targeting a pane

**By `pane_id`, the form `%12`. Never by `session:window.index`.**

Indexes get renumbered when a pane closes: closing pane 1 slides the former pane 2 into
position 1. An instruction meant for `t-03` would end up in `t-04`. The `pane_id` is stable
for the entire life of the pane.

```bash
tmux list-panes -t ordo-myapp -F '#{pane_id} #{pane_width}x#{pane_height} #{pane_dead} #{pane_title}'
tmux capture-pane -p -J -t %12
```

Syntax note: `-t ordo-myapp.0` does not designate pane 0 of the session. The full form is
`session:window.pane`. This is a source of silent error; `pane_id` avoids it entirely.

---

## 7. Injecting text

```bash
tmux send-keys -t %12 -l "the text"    # call 1, -l for literal
tmux send-keys -t %12 C-m              # call 2, separate
```

**Two separate calls, always.** A single call swallows the end of the text.

Text sent while the executor is working **gets queued** without interrupting it; Claude
Code displays "Press up to edit queued messages". This is the intended behavior: the
instruction is received and will be processed at the end of the current turn.

**`Escape` interrupts a turn.** Never send it. There is no legitimate reason to interrupt
an executor mid-work from Ordo; if it is heading off the rails, talk to it, do not cut it
off.

**Never inject into yourself.** Check that the targeted pane is not the orchestrator's own.

---

## 8. Detecting activity

The `esc to interrupt` pattern alone is not enough, even at correct width: it does not
appear at every phase. Look for the union of several markers in the last twenty lines:

```
esc to interrupt · ↓ tokens Running Reading Capturing Synthesizing
```

plus the TUI's waiting gerunds, which vary from one version to another.

Three distinct states, not to be confused:

| Observation | Meaning | What to do |
|---|---|---|
| markers present | the executor is working | nothing, leave it be |
| pane alive, no marker, no report | turn finished with no signal | ask it for its report |
| pane dead | process exited | task blocked, reason recorded |

**A silent pane is never a success.** The success signal is `reports/<campaign>/<task>.json`,
nothing
else. This exact confusion let a finished session go unnoticed for fourteen days in the
original project.

---

## 9. Open risk of tmux mode

An executor launched with `claude --bg` is automatically isolated in a git worktree. An
executor **interactive in a tmux pane is not**: it shares the main checkout with all the
others.

Consequence: two executors on the same repo step on each other in git. This is not solved
by the core. The workarounds, in order of preference:

1. Split the graph so that two simultaneous tasks do not share a zone. This is what
   `touches` and the wave computation exist to guarantee.
2. Launch an executor in a manually created worktree, passing its path as `cwd`.
3. Serialize the tasks that touch the same repo, through a real dependency.

Never rely on automatic isolation inside a pane: it does not exist.

---

## 10. Quick diagnostics

```bash
tmux ls                                                    # the live campaigns
tmux list-panes -a -F '#{session_name} #{pane_id} #{pane_width} #{pane_dead}'
tmux capture-pane -p -J -t %12 | grep -v '^[[:space:]]*$' | tail -40
tmux kill-session -t ordo-myapp                            # kills the whole campaign
```

**`tmux kill-server` and `pkill tmux` are forbidden, no exceptions.** The server is shared
with the user's own work. On 2026-08-09, a server crash during a measurement led to a
repair `kill-server` that destroyed a working session and the process living inside it; a
real, unrecoverable loss. Only named sessions get destroyed, one at a time, and only
Ordo's own. A misbehaving server gets reported, not repaired.

`capture-pane` without `-J` renders wrapped lines split; with `-J` it rejoins them. To read
text, always `-J`. To measure a pane's real width, never.

A capture full of blank lines at the end of the output is normal: `capture-pane` renders
the pane's full height, cursor included. Filter blank lines before running `tail`.

---

## 11. The window follows the human attachment

Added 2026-08-09, in direct response to the cost described in section 2: `window-size
manual` protects automated reading, but leaves the user seeing a portion of a 400-column
window in their 200-column terminal and having to scroll the whole time they are watching.

The fix is set by `ensure_session()` as two hooks **on the session**, never `-g` (the tmux
server is shared with the user's own sessions; a global hook would spill onto them):

```bash
tmux set-hook -t ordo-myapp client-attached \
  'run-shell -b "tmux resize-window -t ordo-myapp -x #{client_width} -y #{client_height} \; select-layout -t ordo-myapp tiled"'

tmux set-hook -t ordo-myapp client-detached \
  'run-shell -b "if [ \$(tmux list-clients -t ordo-myapp 2>/dev/null | wc -l) -eq 0 ]; then python3 ordo/panes.py --relayout ordo-myapp; fi"'
```

Two pitfalls hit while writing them:

**`resize-window -x`/`-y` does not expand `#{...}` itself.** Verified: passing
`'#{client_width}'` literally to `resize-window -x` fails with "width invalid". Only the
`shell-command` argument of `run-shell` performs format expansion before launching the
command; hence the systematic wrapping of the actual resize inside a `run-shell`. `-b`
additionally avoids blocking the tmux server for the duration of the hook's execution.

**Detachment must check that no other client is still attached.** Two humans can watch the
same campaign; without the `#{session_attached} == 0` guard, the second one leaving would
rip the window away from the first mid-read. Verified with two simulated clients:
detaching the second leaves the window at the size the first had given it; detaching the
first (the last one remaining) then triggers the restoration.

**The restoration reruns `relayout()` in a fresh interpreter, never a frozen value.** The
pane count may have changed while someone was attached; replaying `relayout()`'s formula
in shell inside the hook would have duplicated the logic (floors, growth loop) in two
places that would eventually have diverged. The hook therefore runs
`python3 ordo/panes.py --relayout <session>`, the only place where this module invokes
itself.

Measured 2026-08-09, client simulated with the procedure from section 1
(`tmux new-session -d -x 80 -y 24 "..."`), with one indispensable addition:

```bash
tmux new-session -d -s client-test -x 80 -y 24 \
  "env -u TMUX tmux attach -t ordo-myapp"
```

**`env -u TMUX` is mandatory.** Without it: "sessions should be nested with care, unset
$TMUX to force", and no hook fires. Every pane, even inside a detached session, already
carries `TMUX` in its environment (this is how tmux lets a process know which session it
is running in); `tmux attach` detects it and refuses by default.

| Moment | Window (n=2 panes) | Panes |
|---|---|---|
| before attach | 260x80 | 168x80 / 91x80 |
| during attach (80x24 client) | 80x24 | 78x24 / 1x24 |
| after detach | 260x80 | 168x80 / 91x80 |

---

## 12. Pane titles, borders, and the floor that silently degrades

Added 2026-08-09. Two related requirements: see in tmux which task is running in which
pane without having to decode a `%991`, and never let a shrunken read (section 11) pass
itself off as reliable.

**Titles.** `ensure_session(session, label=...)` sets `pane-border-status top` and
`pane-border-format '#{pane_title}'` **on the window**, never `-g`, and renames the window
itself to the campaign name (`rename-window`) instead of leaving tmux's default name.
`spawn(..., title=...)` stores each pane's title via `select-pane -T`; the agreed format is
`"<task-id> <task-title>"` (`t-03 schema migration`), enough to recognize a task without
opening `ordo show`.

`spawn()` reassigns this title on **every** call, whether it reuses `ensure_session()`'s
seed pane or splits a new one: this is what stops a recycled pane from displaying the name
of a task no longer running inside it. In the current core, the only entry point that
assigns a pane to a task is `_do_launch()` (`ordo/cli.py`), shared by `launch` and
`relaunch`; both systematically go through this same up-to-date title.

**The title stays displayed text, never a targeting identifier.** I5 does not change:
every command in this module keeps targeting a pane by its tmux `pane_id` (`%12`), never
by its title or its index.

**The readability floor can break during an attach (section 11).** A human attached from a
small terminal drives the geometry below `PANE_MIN_USABLE_COLS`/`PANE_MIN_USABLE_ROWS`
(120x30); an automated read that lands in that window must not present itself as reliable.
`is_degraded(pane_id)` queries the pane's real geometry at call time and evaluates to
`True` below the floor. `capture(pane_id, warn_floor=True)` (the CLI's `capture` verb,
never used by default inside `busy()`/`wait_ready()`, which call `capture()` in a tight
loop) then prefixes the rendered content with a warning line. `panes()` carries the same
signal as a `sousPlancher` field, served by the `poll` verb.
