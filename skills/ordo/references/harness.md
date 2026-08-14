# The Claude Code harness in tmux orchestration

Reference sheet for a Claude Code session that drives other Claude Code sessions launched in tmux panes. Consult it when a `claude` command behaves strangely.

Source: `docs/harness.md`, measured 2026-07-22 on Claude Code 2.1.218, macOS 27.

---

## Identifiers and transcripts

| Notion | Detail |
|--------|--------|
| `id` | 8-character hexadecimal prefix |
| `sessionId` | Full UUID |
| **Transcript file** | Carries the full UUID: `~/.claude/projects/<encoded cwd>/<full sessionId>.jsonl` |
| Where to read the `sessionId` | The `session_id` field of the JSON output, or the 36 characters of the folder directory |
| Main trap | Storing the short prefix where the UUID is expected makes any transcript read and any cost calculation fail **silently** |

---

## Working directory encoding

The working directory is encoded into the transcript folder name by replacing **every non-alphanumeric character** with a dash, not only slashes.

Empirical examples across 33 real folders:

| Directory | Transcript folder |
|---|---|
| `/Users/x/Drive/School/Master_2/Annuel` | `-Users-x-Drive-School-Master-2-Annuel` |
| `.../com~apple~CloudDocs/Drive` | `...-com-apple-CloudDocs-Drive` |

**Warning:** the transcript folder carries the project root as the harness sees it. For a session that descends into a subfolder, that is the starting directory. For a session isolated in a worktree, that is the worktree path.

Always take the `cwd` reported by `claude agents --json`: it is the correct one.

---

## Paths and symlinks on macOS

macOS resolves `/var` to `/private/var`. Claude Code stores the unresolved path; `claude agents` reports the resolved path.

**Consequence:** any raw string comparison fails silently: session not found, transcript not found, no reusable session, cost never computed.

Canonicalize **on entry and on both sides of every comparison**.

---

## Variadic flags

`--allowedTools` and `--mcp-config` swallow everything that follows, including the positional prompt.

**Mandatory rule:** pass the prompt via stdin, never as a positional argument.

```bash
claude create -n task-name <<EOF
... prompt ...
EOF
```

A prompt starting with a dash would also be read as a flag.

---

## AskUserQuestion in headless mode

`AskUserQuestion` does not exist in non-interactive `-p` execution. A session that calls it receives:

> AskUserQuestion is not available as a tool.

Workaround: the session writes `state: "asking"` and **ends its turn**. The orchestrator relaunches it with the answer. No process waits.

Corollary: use a file-based question channel, never a blocking tool.

---

## Git isolation

### Background sessions
A session launched with `--bg` is automatically isolated in `.claude/worktrees/<name>` on a dedicated branch.

**Watch for deferred creation:** the worktree does not exist at +12 s; it is present at +40 s. Checking too early can wrongly conclude there is no isolation.

Cleanup: the harness does not clean up worktrees. That is Ordo's responsibility.

`.claude/worktrees/` must be in `.gitignore`.

### Interactive tmux sessions
**Interactive sessions are not isolated.** They all share the main checkout.

Direct consequence: several tmux executor sessions on the same repo collide with each other on git. This is an open risk of tmux mode, with no automatic mitigation.

---

## Cost and effort

| Measure | Result |
|--------|--------|
| `total_cost_usd` on the CLI | Unusable for comparing two configurations; recompute from tokens |
| System prompt | About 10 input tokens per 1785 characters; shortening it gains nothing |
| `--effort` | No measurable gain observed; variance between two identical runs reaches a factor of 4 |

---

## Useful options

| Option | Usage |
|--------|-------|
| `--output-format json` | Structured output, with `usage` and `session_id` |
| `--json-schema <schema>` | Forces conformant, validated output |
| `--bg -n <name>` | Named background session |
| `--fork-session` | Resume a session still held by the supervisor |
| `--exclude-dynamic-system-prompt-sections` | Lightens the system prompt |

---

## What SendMessage does not do

`SendMessage` only reaches agents launched **from the calling session**. It cannot address a peer session.

Consequence: an orchestrator that wants to drive background sessions must have launched them itself. Interactive sessions appear read-only.

---

## What tmux makes moot

### Session resumption (`--resume`)

In `--bg` mode, `--resume <sessionId>` refuses any session still held by the supervisor, even in a `done` state.

**In tmux:** the executor is a live interactive process living in a pane. There is neither resumption nor a supervisor holding the session.

### Session forking (`--fork-session`)

In `--bg` mode, forking was necessary to pull a session out of the supervisor's registry and relaunch it in the main checkout (a costly trap detailed in section 10 ter of the original guide).

**In tmux:** there is no supervisor registry. Launching a new session in a pane is intrinsically independent of it.

### `blocked` state

In `--bg` mode, `blocked` and `idle` are identical: the session has ended its turn. The only discriminant is the presence of a pending question.

**In tmux:** this disappears; the orchestrator drives the executor via `send-keys`, not by polling state.

### Background environment (`--bg`)

In `--bg` mode, the supervisor starts the MCP servers with its own environment, not the launcher's.

**In tmux:** there is no intermediary; the pane's environment is that of the tmux session itself.

---

**Persistent fact:** even though tmux eliminates the background constraints, it does not create git isolation. A tmux orchestrator must manage the risk of collision between several executor sessions on the same checkout.
