"""tmux orchestration primitives for Ordo executantes.

Pure subprocess wrapper around the tmux CLI. No other Ordo module is
imported here, on purpose: this file must be testable standalone, against
real tmux panes running plain bash. Every public function takes plain
strings (a tmux session name, a tmux window_id, a pane_id, a working
directory, a shell command) and returns strings, tuples or dicts.

Topology: one tmux session per chantier, one pane per executante, tiled
layout. See ~/.claude/skills/ordo/references/tmux.md for the measurements
behind every constant and ordering constraint below.

Window-scoped calls (split-window, resize-window, select-layout, list-panes,
the set-window-option calls, the hook bodies) target a window_id ('@12'),
never a bare session name: `-t <session>` alone resolves to that session's
CURRENT window, which drifts the moment an attached human opens or switches
windows, silently redirecting the call to the wrong place. ensure_session()
returns (session, window_id); every function below that used to take a
session name now takes that window_id as its nominal argument, with a
session name still accepted for reasonable backward compatibility (see
_resolve_target()). Session-name targets, wherever still used (has-session,
set-hook, kill-session, list-windows), are always matched exactly ('=name')
rather than tmux's default prefix matching, which was verified 2026-08-09 to
let a short target name silently match a longer, unrelated session name.
"""

import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

# Window size floor, and the unit each added pane grows the window by.
# colonnes = WINDOW_BASE_COLS * side, lignes = WINDOW_BASE_ROWS * side, avec
# side = ceil(sqrt(n)), recomputed on every pane addition (see _dimensions).
# Below roughly 120 usable columns per pane, the Claude Code TUI reflows and
# status markers vanish mid-word from the capture, which is exactly what
# busy() below depends on not happening.
#
# Both dimensions use the SAME multiplier `side`, not "side columns x
# ceil(n/side) rows" as an earlier version of this formula assumed. Measured
# 2026-08-09 against real tmux 3.7b panes: `select-layout tiled` for n=2 in a
# 260x36 window produced two panes stacked full-width at 17/18 rows tall, not
# two panes side by side at full height as the ceil(n/side) row count would
# imply. tmux's tiled algorithm balances its own row/column band count from
# the window's aspect ratio, which a closed-form guess cannot reliably
# predict (side=3 in particular was seen to flip between a 3x3-ish band grid
# and a lopsided 1-wide-column-plus-N-stacked-column layout for the exact
# same pane count, purely depending on the window's absolute height). Instead
# of chasing that algorithm analytically, `side` is used as a starting point
# for BOTH dimensions and relayout() below actively verifies and grows the
# window until the real, measured per-pane height also clears the floor.
WINDOW_BASE_COLS = 130
WINDOW_BASE_ROWS = 40

# The floors relayout() verifies its own result against, real panes measured
# each time rather than trusted from the formula alone (see relayout()).
# PANE_MIN_USABLE_COLS is not the same number as WINDOW_BASE_COLS by
# coincidence of the spec's own numbers, but a distinct guarantee:
# WINDOW_BASE_COLS sizes the window, this checks what each pane actually
# ended up with once tiled.
PANE_MIN_USABLE_COLS = 120

# Floor on USABLE rows per pane. Claude Code's TUI needs vertical room for its
# banner, scrollback and status line; 17-18 rows (observed 2026-08-09 in a
# 260x36 window tiled for 2 panes, before this fix) left barely more than the
# input box itself. 30 is comfortably above that failure and still leaves
# room to grow within a reasonably sized terminal window.
PANE_MIN_USABLE_ROWS = 30

# Substrings that indicate an executante is actively working, scanned as a
# union over the last 20 captured lines by busy(). "esc to interrupt" alone
# is not enough: it gets truncated away in a pane narrower than roughly 120
# columns and produces a false idle reading (see references/tmux.md).
# Claude Code's TUI also cycles through whimsical gerunds while it works
# that change between versions and are not enumerated here on purpose: add
# one to this tuple the day it is caught causing a false idle, that is
# exactly what this constant exists to absorb without touching busy().
BUSY_MARKERS = (
    "esc to interrupt",
    "· ↓",
    "tokens",
    "Running",
    "Reading",
    "Capturing",
    "Synthesizing",
)

# Marker for "the claude TUI has rendered its input box and is ready to be
# typed into". Measured 2026-08-09, tmux 3.7b: `claude --dangerously-skip-
# permissions < /dev/null` launched in an ALREADY-trusted directory
# (~/Documents/ordo), pane captured every 0.2-0.3 s from send() with nothing
# else sent afterwards. The banner line "Claude Code v2.1.226" appeared
# together with the bordered input box (a full-width "-" rule, then "> ")
# between 1.9 s and 5.3 s after send() across three separate runs in the same
# session -- close to a 3x spread, consistent with the variance already
# documented elsewhere in this project (CLAUDE.md: "la variance entre deux
# executions identiques atteint un facteur 4"). Matching only the "Claude
# Code v" prefix keeps this tolerant of the trailing version number changing.
READY_MARKERS = ("Claude Code v",)

# Trust-dialog marker, measured 2026-08-09 the same way but pointed at a
# directory Claude Code had never opened before (a fresh mkdtemp), same
# command, same polling. The dialog appeared 1.0-1.3 s after send(), reliably
# faster than the full ready banner since nothing else needs to load first.
# Exact captured text:
#   Accessing workspace:
#   <path>
#   Quick safety check: Is this a project you created or one you trust?
#   (Like your own code, a well-known open source project, or work from
#   your team). If not, take a moment to review what's in this folder
#   first.
#   Claude Code'll be able to read, edit, and execute files here.
#   Security guide
#   > 1. Yes, I trust this folder
#     2. No, exit
#   Enter to confirm . Esc to cancel
# Ordo's product decision is to never answer this dialog automatically (see
# wait_ready() and cli.py's launch): detecting it reliably matters more here
# than anywhere else in this module, hence two independent substrings rather
# than one.
TRUST_DIALOG_MARKERS = ("Quick safety check", "Yes, I trust this folder")

# Ceiling wait_ready() polls for before giving up. Generous on purpose: the
# measured ready banner alone ranged 1.9-5.3 s run to run under no load; a
# chantier launching several executantes back to back, or a slow disk, can
# easily push past that.
PANE_READY_TIMEOUT_S = 30.0
PANE_READY_POLL_INTERVAL_S = 0.5

# Prefix wait_ready() returns for each outcome. Not booleans: a caller that
# only checks truthiness could silently treat "trust" as success, which
# is exactly the injection-into-an-unhandled-dialog bug this exists to rule
# out.
PANE_ETAT_PRET = "ready"
PANE_ETAT_CONFIANCE = "trust"

# Prefix cli.py's launch writes into task["error"] when wait_ready() reports
# PANE_ETAT_CONFIANCE, and controle.py's wake_reasons() reads back to
# classify that block as its own kind, distinct from a dead pane or a
# generic terminal report. Owned here, next to the marker that triggers it,
# rather than in cli.py or controle.py: cli.py is the writer, controle.py
# the reader, and neither should have to import the other just to agree on
# one exact string (same reasoning as controle.py's PANE_MORT_RAISON).
TRUST_DIALOG_BLOCK_RAISON = "tmux trust dialog shown, human decision required"

# Prefix of the warning capture() prepends when warn_floor=True and the pane it just
# read is narrower or shorter than PANE_MIN_USABLE_COLS/ROWS at the moment of reading.
# A caller (cli.py's `capture` and `poll` verbs) matches on this exact prefix rather
# than re-deriving pane geometry itself, the same reasoning as TRUST_DIALOG_BLOCK_RAISON
# just above: one owner for the exact string, everyone else reads it back.
FLOOR_WARNING_PREFIX = "WARNING ordo"

# Absolute path to this file, embedded literally into the client-detached hook command
# (see _client_detached_hook_command). A hook body is stored by the tmux server and can
# fire long after the Python process that called ensure_session() has exited, so it
# cannot invoke relayout() as a live function call; it has to re-launch this very module
# as a fresh interpreter. Resolved once at import time rather than at hook-fire time:
# the file is not going to move while the process lives.
_MODULE_PATH = str(Path(__file__).resolve())

# Point M: the lowest tmux version this module relies on. window-size manual,
# resize-window -A and set-hook -t all require it; below that, the failure mode
# is not a clean error but a silently-ignored flag or an unrecognised option,
# which is worse than refusing to start.
TMUX_MIN_VERSION = (2, 9)

# Set once by _ensure_tmux_ready() the first time any tmux command actually
# runs, then left alone: every call after the first is a free no-op check
# instead of a second `tmux -V` subprocess. Reset only by tests.
_tmux_verified = False

# Point L: module-level trace hook. None by default -- the CLI's --verbose flag
# is the only intended caller of `panes.TRACE = ...`, and until it does, every
# code path below behaves exactly as if this constant did not exist.
TRACE = None


def _tmux_missing_message() -> str:
    return (
        "tmux not found: Ordo orchestrates its executors in tmux panes, "
        "nothing can run without it. Install it then relaunch (macOS: "
        "`brew install tmux`; Debian/Ubuntu: `apt install tmux`)."
    )


def _tmux_version_too_old_message(raw_version: str) -> str:
    minimum = ".".join(str(part) for part in TMUX_MIN_VERSION)
    return (
        f"tmux too old ({raw_version}), Ordo requires at least tmux {minimum}: "
        "window-size manual, resize-window -A and set-hook -t depend on it. "
        "Update tmux (macOS: `brew upgrade tmux`)."
    )


def _parse_tmux_version(raw: str) -> tuple[int, int] | None:
    """Best-effort major.minor out of `tmux -V`'s own output ("tmux 3.7b").

    Returns None on anything unparsable (an exotic fork, a distro patch
    scheme) rather than raising: a version this module cannot read is not
    proof it is too old, so it is let through instead of blocked on a guess.
    """
    match = re.search(r"(\d+)\.(\d+)", raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _ensure_tmux_ready() -> None:
    """Check the tmux binary and its version exactly once per process (Point M).

    Called from _tmux() below, itself the single choke point every tmux
    invocation in this module goes through -- so this runs before the very
    first real tmux command and never again, not once per command as a naive
    per-call check would. shutil.which() costs nothing (no subprocess); the
    one unavoidable `tmux -V` subprocess call is spent here a single time and
    cached in _tmux_verified for the rest of the process's life.
    """
    global _tmux_verified
    if _tmux_verified:
        return
    if shutil.which("tmux") is None:
        raise RuntimeError(_tmux_missing_message())
    result = subprocess.run(["tmux", "-V"], capture_output=True, text=True)
    raw_version = (result.stdout or result.stderr).strip()
    version = _parse_tmux_version(raw_version)
    if version is not None and version < TMUX_MIN_VERSION:
        raise RuntimeError(_tmux_version_too_old_message(raw_version or "unknown version"))
    _tmux_verified = True


def _tmux(args: list[str]) -> subprocess.CompletedProcess:
    """Single choke point for every tmux invocation in this module.

    Every function below that used to call subprocess.run(["tmux", ...])
    directly now goes through here instead, _run() included: that is what
    lets _ensure_tmux_ready() and TRACE apply uniformly, at zero added
    subprocess cost beyond the one real tmux call each caller already needed.
    """
    _ensure_tmux_ready()
    command = ["tmux", *args]
    if TRACE is not None:
        TRACE(command)
    return subprocess.run(command, capture_output=True, text=True)


def _run(args: list[str]) -> str:
    """Run a tmux subcommand, raising a French, command-naming error on failure.

    tmux fails quietly by default: non-zero exit, terse stderr, nothing on
    stdout. Every caller in this module depends on a failure being loud
    instead, never a swallowed None standing in for "something went wrong".
    """
    result = _tmux(args)
    if result.returncode != 0:
        commande = " ".join(args)
        detail = result.stderr.strip() or "no detail returned by tmux"
        raise RuntimeError(f"tmux command failed (tmux {commande}): {detail}")
    return result.stdout


def _session_exists(session: str) -> bool:
    """True if a session named EXACTLY `session` exists (Point C).

    The leading `=` forces tmux's exact-match rule. Without it, tmux falls
    back to prefix matching on session names: verified 2026-08-09 that
    `has-session -t ordo-api` returns success (wrongly) when only
    `ordo-api-docs` exists. That false positive is what sent a pane into the
    wrong chantier's window in the first place.
    """
    result = _tmux(["has-session", "-t", f"={session}"])
    return result.returncode == 0


def _window_id_of(session: str) -> str:
    """The stable window_id ('@12') of session's sole window (Point B).

    ensure_session() always leaves a session with exactly one window (see
    the module docstring's topology note); this reads back whichever window
    that is, right after creating or confirming the session, so every
    window-scoped tmux call from here on can target that id instead of the
    session's name. `=session` forces the same exact match as
    _session_exists() -- list-windows suffers the identical prefix-matching
    risk for the same reason.
    """
    output = _run(
        ["list-windows", "-t", f"={session}", "-F", "#{window_id} #{window_name}"]
    )
    lines = []
    for line in output.splitlines():
        if not line.strip():
            continue
        window_id, _, window_name = line.partition(" ")
        # Les fenetres de service (side_window) sont ecartees, jamais rendues comme
        # fenetre du chantier. Sans ce filtre, il suffit que la derniere pane
        # d'executante soit moissonnee pour que la fenetre du chantier disparaisse en
        # laissant la session vivante par la seule fenetre de la carte : ensure_session()
        # adopterait alors celle-ci, la renommerait au slug du chantier, y poserait ses
        # hooks, et l'executante suivante naitrait a cote du rafraichisseur de la carte.
        if window_name == MAP_WINDOW_NAME:
            continue
        lines.append(window_id)
    if not lines:
        raise RuntimeError(
            f"no window found for session {session}: cannot determine its "
            "stable identifier"
        )
    return lines[0]


def _work_window(session: str) -> str:
    """La fenetre de travail de la session, creee si toutes ses panes ont ete moissonnees.

    Une session ne survit normalement pas a la mort de sa derniere fenetre : moissonner la
    derniere executante emporte la session, et l'appel suivant a ensure_session() en
    recree une propre. Une fenetre de service (side_window) casse cette mecanique en
    maintenant la session en vie ; _window_id_of() ecarte cette fenetre-la, donc il ne
    reste plus RIEN a rendre, et le chantier se retrouverait bloque par le seul fait qu'on
    regardait sa carte. On refabrique donc la fenetre de travail au lieu d'echouer.
    """
    try:
        return _window_id_of(session)
    except RuntimeError:
        return _run(
            ["new-window", "-P", "-F", "#{window_id}", "-t", f"={session}:"]
        ).strip()


def _resolve_target(window: str) -> str:
    """The tmux -t value to use for a window-scoped command (Point B).

    The nominal argument every public function below now takes is already a
    tmux window_id ('@12'): those are unambiguous IDs, not names subject to
    prefix matching, so they are returned untouched. A caller still passing a
    legacy session name (anything not starting with '@') is honoured for
    reasonable backward compatibility, resolved with the same forced exact
    match ('=name') _session_exists() uses -- tmux then targets that
    session's CURRENT window, exactly the old behaviour, just no longer
    vulnerable to matching a differently-named session by prefix.

    The trailing ':' matters and is not decorative. Verified 2026-08-09: for
    commands whose -t is a target-PANE (split-window, select-layout, and
    set-hook on this tmux version -- see _install_attach_hooks()), a bare
    '=name' with no ':' fails outright ("can't find pane: =name"), even
    though the identical '=name' works fine as a target-SESSION (has-session)
    or target-WINDOW (resize-window). Appending ':' (an explicitly empty
    window component) makes tmux resolve "session name, exact match, current
    window" the same way for target-pane as it already does for the others --
    per FORMATS in tmux(1): "An empty window name ... [selects] the current
    window in session."
    """
    if window.startswith("@"):
        return window
    return f"={window}:"


def _reject_self(pane_id: str) -> None:
    """Refuse to target the caller's own pane.

    An orchestrator running inside tmux exposes its own pane_id through the
    TMUX_PANE environment variable. Sending keys to, capturing, or killing
    that exact pane would be the orchestrator acting on itself instead of
    on an executante.
    """
    if pane_id and pane_id == os.environ.get("TMUX_PANE"):
        raise ValueError(
            "refused: this pane belongs to the caller, self-injection refused"
        )


def _dimensions(pane_count: int) -> tuple[int, int]:
    """Starting-point window size for pane_count panes.

    Floored at WINDOW_BASE_COLSxWINDOW_BASE_ROWS, but this is a first guess,
    not a guarantee: see the module comment above WINDOW_BASE_ROWS for why a
    closed-form formula cannot promise the row floor by itself. relayout()
    is what actually enforces PANE_MIN_USABLE_ROWS, growing past this guess
    if real, measured panes come back too short.
    """
    n = max(pane_count, 1)
    side = math.ceil(math.sqrt(n))
    cols = WINDOW_BASE_COLS * side
    rows = WINDOW_BASE_ROWS * side
    return max(cols, WINDOW_BASE_COLS), max(rows, WINDOW_BASE_ROWS)


def _client_attached_hook_command(window_id: str) -> str:
    """tmux command run whenever a client attaches to the chantier's session.

    window-size manual (see ensure_session()) freezes the window at its wide,
    tiled-for-reading geometry so an attach never truncates the automated
    read path -- but left alone, that same freeze means a human who DOES
    attach from a normal terminal sees only a slice of a 400-column window
    and has to scroll (measured 2026-08-09: exactly this, an 80x24 client
    against a 400x60 window). This hook makes the window shrink to fit the
    attaching client instead, then re-tiles.

    Both resize-window and select-layout carry an explicit `-t window_id`
    (Point B) instead of running bare in whatever "current window" context
    the hook fires in. Verified 2026-08-09: with a second, human-created
    window in the same session made current, the bare form (no -t) retiles
    THAT window instead of the chantier's own -- exactly the wrong-window bug
    Point B exists to close, and it reaches here too since a hook body is
    just more tmux commands. -A means "size to the largest client attached to
    the window", needs no #{...} format expansion, so nothing to quote or
    break by embedding window_id directly.

    Measured 2026-08-09 with real clients: detached 260x80; one 100x30 client
    attaches, window becomes 100x29; a second 160x45 client attaches, window
    becomes 160x44 (the larger of the two, the smaller one scrolls); last
    client leaves, window returns to 260x80.

    THE TRAP, and it cost a wrong diagnosis: `tmux attach` launched from inside
    tmux refuses to nest unless $TMUX is unset. A test that forgets it attaches
    NO client at all, `tmux list-clients` stays empty, the hook never fires, and
    a hook that works is indistinguishable from a hook that does nothing. Any
    test of this path must force the attach with `env -u TMUX tmux attach` and
    assert `list-clients` is non-empty BEFORE asserting on any geometry.
    """
    return f"resize-window -A -t {window_id} ; select-layout -t {window_id} tiled"


def _client_detached_hook_command(session: str, window_id: str) -> str:
    """tmux command run whenever a client detaches from the chantier's session.

    Restores the wide, floor-respecting geometry relayout() computes for the
    window's CURRENT pane count -- not a size captured once at attach time,
    since panes can be added while someone is attached. relayout() already
    owns the only place PANE_MIN_USABLE_COLS/ROWS and the retry/growth loop
    are enforced (see relayout()); re-deriving that formula in shell here
    would silently drift from it the next time either changes, so this hook
    instead re-invokes this very module as a fresh interpreter (see the
    `__main__` block at the bottom of this file) rather than the formula --
    now with `--relayout <window_id>`, the module's own nominal argument
    (Point B), not a session name.

    The wide geometry is restored ONLY when no client remains: two humans can
    watch the same chantier, and the second one's detach must not yank the
    window out from under the first. `list-clients -t =session` uses the same
    forced exact match as _session_exists() (Point C): without it, this count
    could include clients attached to a differently-named session that merely
    shares `session` as a prefix, making the hook think a client is still
    watching when none is, and never restoring the wide geometry at all.

    When a client DOES remain, the window is resized to it rather than left
    alone, targeted at window_id (Point B) for the same reason as the
    attached hook above. Measured 2026-08-09 with two clients, 100x30 and
    160x45: without this branch, the 160x45 leaving left the window at
    160x44 and the remaining 100x30 client had to scroll a window sized for
    someone who was gone. `resize-window -A` on the survivors fixes it, and
    is a no-op when the single remaining client already matches the window
    size.
    """
    python = shlex.quote(sys.executable)
    module = shlex.quote(_MODULE_PATH)
    return (
        f'run-shell -b "if [ \\$(tmux list-clients -t ={session} 2>/dev/null | wc -l) '
        f'-eq 0 ]; then {python} {module} --relayout {window_id}; '
        f'else tmux resize-window -t {window_id} -A \\; select-layout -t {window_id} tiled; fi"'
    )


def _install_attach_hooks(session: str, window_id: str) -> None:
    """Pose les hooks client-attached / client-detached SUR LA SESSION d'Ordo, jamais -g.

    -g rendrait ce comportement global, deborderait sur toute autre session tmux du
    meme serveur (celles de l'utilisateur, partageant le meme serveur tmux) -- exactement
    ce que ce depot s'interdit. `set-hook -t =session` (Point C : correspondance exacte,
    verifie 2026-08-09 -- sans le prefixe `=`, `set-hook -t ordo-x` s'enregistre en
    silence sur `ordo-x-docs` quand `ordo-x` n'existe pas encore) scope le reglage a
    CETTE session precise, jamais -g. set-hook remplace l'entree d'index 0 au lieu d'en
    empiler une seconde : rappeler cette fonction depuis ensure_session() a chaque appel,
    meme sur une session deja existante, ne duplique donc jamais l'effet (meme
    raisonnement que le set-window-option window-size juste au-dessus). Les hooks
    eux-memes restent scopes a la session (un client s'attache a une session, pas a une
    fenetre -- verifie : `set-hook -w` sur client-attached est silencieusement ignore et
    l'entree atterrit quand meme dans la table de la session) ; c'est le CORPS de chaque
    hook, construit ci-dessus, qui vise desormais window_id (Point B).

    Le `:` final apres `={session}` n'est pas cosmetique : set-hook cible un
    target-PANE sur cette version de tmux, et verifie 2026-08-09, un `=nom` SANS `:`
    y echoue purement et simplement ("can't find pane: =nom") alors que le meme
    `=nom` fonctionne pour has-session (target-session). Voir _resolve_target()
    pour la meme correction et son explication complete.
    """
    _run(["set-hook", "-t", f"={session}:", "client-attached", _client_attached_hook_command(window_id)])
    _run(["set-hook", "-t", f"={session}:", "client-detached", _client_detached_hook_command(session, window_id)])


def ensure_session(session: str, label: str | None = None) -> tuple[str, str]:
    """Create the chantier's tmux session if absent, and pin its geometry.

    Returns (session, window_id): window_id is the stable tmux id ('@12') of
    the session's sole window (Point B), read back right after the session is
    confirmed to exist. Callers (chantier.py's start(), stored as
    chantier["tmuxWindow"]) are meant to keep this id and pass it, not the
    session name, to spawn()/relayout()/panes() from then on -- see
    _resolve_target()'s docstring for why: `-t <session>` alone targets that
    session's CURRENT window, which drifts the moment an attached human opens
    or switches to another window in the same session, silently redirecting
    every subsequent split-window/resize-window/select-layout/list-panes call
    to the wrong place.

    window-size manual detaches the window's geometry from whichever client
    later attaches to it. Without this, a user attaching from an 80x24
    terminal overwrites the layout and shrinks every executante pane in the
    chantier at once (verified 2026-08-09: the window stayed 400x60 and
    its panes kept 199/200/400 columns through an attach from an 80x24
    client). Applied unconditionally, even when the session already
    existed, so a session created outside this module still ends up safe.

    _install_attach_hooks() then layers the OTHER half of that same story on
    top: the freeze above is what keeps automated reading reliable while
    nobody is looking, and the hooks are what let the window still shrink to
    fit a human who does attach, then grow back once they leave (see both
    hook builders above for the measurements behind this).

    label, when given, renames the window to something a human recognizes
    instead of tmux's default (a shell's own name) and turns on a permanent
    per-pane title bar (pane-border-status/-format) showing whichever title
    spawn() gave each pane -- all three window-scoped options, applied with
    -t window_id (Point B), never -g. Re-applied unconditionally on every
    call for the same reason as window-size manual above: idempotent, and
    safe on a session this call did not itself create.
    """
    if not _session_exists(session):
        _run(
            [
                "new-session",
                "-d",
                "-s",
                session,
                "-x",
                str(WINDOW_BASE_COLS),
                "-y",
                str(WINDOW_BASE_ROWS),
            ]
        )
    window_id = _work_window(session)
    _run(["set-window-option", "-t", window_id, "window-size", "manual"])
    if label:
        _run(["rename-window", "-t", window_id, label])
    _run(["set-window-option", "-t", window_id, "pane-border-status", "top"])
    _run(["set-window-option", "-t", window_id, "pane-border-format", "#{pane_title}"])
    _install_attach_hooks(session, window_id)
    return session, window_id


# Pane-scoped user option stamped on every pane spawn() hands out (whether
# freshly split or reused), so a later spawn() on the same session can tell a
# pane it already gave away apart from ensure_session()'s still-untouched
# seed pane. Deliberately not stored in Ordo's own state: this module has no
# other module's state to consult (see the file docstring), and the pane
# itself is the one place this fact cannot go stale relative to tmux's own
# view of which panes exist.
_CLAIMED_PANE_OPTION = "@ordo_claimed"


def _reusable_seed_pane(window: str) -> str | None:
    """The chantier window's still-unclaimed seed pane, if there is one.

    `window` is the nominal identifier every function in this section now
    takes: a stable window_id ('@12', Point B) as its nominal argument, with
    a legacy session name still accepted for reasonable backward
    compatibility (see _resolve_target()). panes(window) below resolves
    either form the same way.

    ensure_session() always leaves a freshly created session with exactly
    one pane: an idle login shell nobody asked for. Without this, the first
    spawn() of every chantier left that pane sitting empty next to the
    executante's pane for the rest of the chantier's life (observed
    2026-08-09: session ordo-projet-test with %324 empty, %325 the
    executante, each getting half the window for one live executante).

    A pane only qualifies if it is the SOLE pane in the window, still alive,
    AND has never been claimed by an earlier spawn() (see
    _CLAIMED_PANE_OPTION): a chantier whose other panes were all killed,
    leaving one already-claimed executante pane behind, must never have that
    pane silently reused for an unrelated new task.
    """
    rows = panes(window)
    if len(rows) != 1 or not rows[0]["vivant"]:
        return None
    pane_id = rows[0]["pane_id"]
    result = _tmux(["show-options", "-p", "-t", pane_id, "-v", _CLAIMED_PANE_OPTION])
    if result.returncode == 0:
        return None  # option is set: already claimed by an earlier spawn()
    return pane_id


def spawn(window: str, cwd: str, cmd: str, title: str | None = None) -> str:
    """Start cmd in a pane of the chantier's window, returning its pane_id.

    `window` is the nominal argument (Point B): ensure_session()'s returned
    window_id ('@12'), targeted directly since it is already unambiguous. A
    legacy session name is still accepted (see _resolve_target()) and
    resolves to that session's CURRENT window exactly as before -- but the
    nominal, recommended path no longer goes through that ambiguity at all.
    Verified 2026-08-09: with a second window created in the same session and
    made current, splitting with a bare session-name target lands the new
    pane in that OTHER window instead of the chantier's own; targeting
    window_id keeps landing in the right one regardless.

    The chantier's first spawn() reuses ensure_session()'s idle seed pane
    (via respawn-pane -k, which replaces the pane's process without
    disturbing its pane_id) instead of splitting a second pane next to it.
    Every spawn() after that always splits, because the seed pane is marked
    claimed the moment it is handed out, by this call or an earlier one; see
    _reusable_seed_pane().

    When a split does happen, the caller must call relayout() right after,
    never before: running select-layout before resize-window lets the
    first-created pane keep the full width it inherited from the split,
    measured at 259 columns while two sibling panes stayed at 140.

    The returned id comes straight from tmux's own #{pane_id} format (I5):
    never session:window.pane, which silently points at the wrong pane the
    moment any pane in the window closes and indices shift. respawn-pane
    keeps the pane_id it is given, so the reuse path preserves this
    guarantee exactly like the split path does.

    title is stamped AFTER both branches, unconditionally whenever the
    caller passes one -- this is what keeps a reused pane from ever
    displaying a stale task's name: cli.py's _do_launch() passes a title on
    every single launch and relaunch, so whichever pane this call ends up
    handing back (the reused seed pane, or a freshly split one) always
    carries the CURRENT caller's title the instant it is claimed, never the
    previous occupant's. select-pane -T only changes what tmux DISPLAYS in
    pane-border-format (see ensure_session()); it is never read back to
    target a pane anywhere in this module (I5 stays pane_id-only).
    """
    seed = _reusable_seed_pane(window)
    if seed is not None:
        _run(["respawn-pane", "-k", "-t", seed, "-c", cwd, cmd])
        pane_id = seed
    else:
        target = _resolve_target(window)
        output = _run(
            ["split-window", "-t", target, "-c", cwd, "-P", "-F", "#{pane_id}", cmd]
        )
        pane_id = output.strip()
        if not pane_id:
            raise RuntimeError(
                "tmux command failed (tmux split-window): no pane_id returned"
            )
    _run(["set-option", "-p", "-t", pane_id, _CLAIMED_PANE_OPTION, "1"])
    if title:
        _run(["select-pane", "-t", pane_id, "-T", title])
    return pane_id


# relayout() retries this many times before giving up on a floor-respecting
# layout. See relayout()'s docstring for what this works around.
RELAYOUT_MAX_ATTEMPTS = 5
RELAYOUT_RETRY_DELAY_S = 0.15

# Growth applied to (cols, rows) before a retry that follows a SETTLED floor
# miss (see relayout()): tmux's own tiled algorithm was measured 2026-08-09
# to pick genuinely different, non-monotonic row/column band arrangements
# for the same pane count depending on the window's absolute size (side=3
# flipped between a roughly-square band grid and a lopsided one-wide-column
# layout purely from height changes, with no formula found that predicts
# which one it picks). Retrying the exact same numbers can only fix a
# transient race (see _layout_meets_floor); it can never fix a genuine
# too-small pick, so a genuine miss grows the window and tries again instead.
RELAYOUT_GROWTH_FACTOR = 1.3


def relayout(window: str) -> None:
    """Recompute the window size for the current pane count, then retile.

    `window` is the nominal argument (Point B): a window_id targeted
    directly, or a legacy session name resolved through _resolve_target()
    the same way spawn() and panes() do. This matters here specifically
    because the client-detached hook re-invokes this exact function (via
    `python3 panes.py --relayout <window_id>`, see the `__main__` block and
    _client_detached_hook_command()) to restore the wide geometry, and it now
    always passes the window_id it was given at hook-install time -- never a
    session name that could have grown a second, unrelated window since.

    Order is fixed: resize-window first, select-layout tiled second. See
    spawn() for the measured cost of swapping them. Both run as one
    chained tmux invocation (a literal ";" argument here, not a shell
    operator: no shell is involved in this call).

    Even chained, a resize immediately followed by a retile can still land
    on a stale, pre-resize geometry: tmux's own reply to resize-window is
    not a hard guarantee every internal part of the server has finished
    applying it before the next command in the same invocation runs.
    Measured 2026-08-09, over repeated live runs of ensure_session +
    spawn + relayout: most calls tiled cleanly (129/130 columns for 2
    panes in a 260-wide window), but roughly one run in three left a
    freshly split pane holding its pre-resize width (168/91, or one pane
    still spanning the full 260 while its sibling was already split).
    Every one of those bad layouts self-corrected on a second attempt.

    relayout() therefore verifies its own result against both floors this
    module exists to guarantee (columns AND rows, see PANE_MIN_USABLE_COLS
    and PANE_MIN_USABLE_ROWS), and retries until they hold or the attempt
    budget runs out. A miss that persists past the settle check in
    _layout_meets_floor is treated as genuine, not transient, and grows the
    window before the next attempt rather than reissuing the same numbers.
    """
    count = len(panes(window))
    if count == 0:
        return
    cols, rows = _dimensions(count)
    target = _resolve_target(window)
    for attempt in range(RELAYOUT_MAX_ATTEMPTS):
        _run(
            [
                "resize-window",
                "-t",
                target,
                "-x",
                str(cols),
                "-y",
                str(rows),
                ";",
                "select-layout",
                "-t",
                target,
                "tiled",
            ]
        )
        if _layout_meets_floor(window):
            return
        if attempt + 1 < RELAYOUT_MAX_ATTEMPTS:
            time.sleep(RELAYOUT_RETRY_DELAY_S)
            cols = math.ceil(cols * RELAYOUT_GROWTH_FACTOR)
            rows = math.ceil(rows * RELAYOUT_GROWTH_FACTOR)
    raise RuntimeError(
        "relayout: the tiled geometry did not settle above the floor of "
        f"{PANE_MIN_USABLE_COLS} columns x {PANE_MIN_USABLE_ROWS} rows "
        f"after {RELAYOUT_MAX_ATTEMPTS} attempts"
    )


def _layout_meets_floor(window: str) -> bool:
    """True if both floors hold now and still hold a moment later.

    A single passing check is not enough: a layout has been observed to
    read as good immediately after resize-window + select-layout, then
    read as the pre-resize, floor-violating geometry again a fraction of a
    second later, with no further command issued in between. Two checks
    spaced apart is what catches that in-flight settling instead of
    reporting success on a value that has not finished changing yet.
    """

    def dims_ok() -> bool:
        alive = [p for p in panes(window) if p["vivant"]]
        if not alive:
            return False
        widths = [p["largeur"] for p in alive]
        heights = [p["hauteur"] for p in alive]
        return (
            min(widths) >= PANE_MIN_USABLE_COLS
            and min(heights) >= PANE_MIN_USABLE_ROWS
        )

    if not dims_ok():
        return False
    time.sleep(RELAYOUT_RETRY_DELAY_S)
    return dims_ok()


def _pane_geometry(pane_id: str) -> tuple[int, int] | None:
    """pane_id's live width/height, or None if it cannot be queried.

    A single direct query keyed by pane_id (display-message resolves the
    owning session itself), rather than fetching the whole session's
    panes() and scanning for a match: capture()/is_degraded() only ever
    know a pane_id, never which session it lives in.
    """
    result = _tmux(["display-message", "-p", "-t", pane_id, "-F", "#{pane_width}x#{pane_height}"])
    if result.returncode != 0:
        return None
    try:
        w_str, h_str = result.stdout.strip().split("x", 1)
        return int(w_str), int(h_str)
    except ValueError:
        return None


def is_degraded(pane_id: str) -> bool:
    """True if pane_id is currently narrower or shorter than the read floor.

    The same PANE_MIN_USABLE_COLS/PANE_MIN_USABLE_ROWS relayout() enforces
    right after a launch -- but a pane that cleared the floor at launch time
    can fall back under it later, whenever a client attaches from a small
    terminal (see _client_attached_hook_command): window-size manual no
    longer protects a pane once someone is actively looking at it. A pane
    that cannot be found reads as NOT degraded: an unreadable pane is
    covered by capture()/alive() raising or reporting dead, not by this.
    """
    dims = _pane_geometry(pane_id)
    if dims is None:
        return False
    width, height = dims
    return width < PANE_MIN_USABLE_COLS or height < PANE_MIN_USABLE_ROWS


def _floor_warning(pane_id: str) -> str | None:
    """French warning line for capture(warn_floor=True), or None if not degraded."""
    dims = _pane_geometry(pane_id)
    if dims is None:
        return None
    width, height = dims
    if width >= PANE_MIN_USABLE_COLS and height >= PANE_MIN_USABLE_ROWS:
        return None
    return (
        f"{FLOOR_WARNING_PREFIX}: pane {pane_id} below the read floor "
        f"({width}x{height}, floor {PANE_MIN_USABLE_COLS}x{PANE_MIN_USABLE_ROWS}); "
        "capture potentially truncated while a client is attached to it"
    )


def capture(pane_id: str, lines: int = 80, join: bool = True, warn_floor: bool = False) -> str:
    """Return the last `lines` lines of pane_id, most recent last.

    -J rejoins tmux's own line-wrapping before returning text; without it a
    long line comes back cut mid-word, the exact failure mode that produces
    a false idle reading in busy(). Trailing blank lines are stripped:
    capture-pane always renders the pane's full height, blank rows and
    cursor line included, whether or not that many lines were ever written.

    tmux's own -S start-line counts from the top of the visible pane, not
    from the bottom: -S -20 pulls 20 extra lines of history on top of
    whatever the pane's current height already shows, not "the last 20
    lines" (measured: 55 non-blank lines back from a 36-row pane with -S
    -20, not 20). The full history is captured instead and sliced to the
    last `lines` entries here, in Python, where "last N lines" means what
    it says regardless of the pane's height.

    warn_floor=True additionally queries pane_id's live geometry and
    prepends a French warning line (see _floor_warning) when it is below
    PANE_MIN_USABLE_COLS/ROWS: a client attaching from a small terminal
    shrinks the whole window (see _client_attached_hook_command), and a
    reader treating that capture as fully reliable -- silently, without
    being told it might be truncated -- is exactly the failure this module
    exists to rule out. Left False by default: busy() and wait_ready() call
    capture() many times a second on their own, their marker scans never
    overlap this banner's text, and paying for an extra tmux query on every
    one of those internal calls would be pure overhead. cli.py's `capture`
    verb -- the human-facing read -- passes True.
    """
    _reject_self(pane_id)
    args = ["capture-pane", "-p", "-t", pane_id, "-S", "-"]
    if join:
        args.append("-J")
    output = _run(args)
    text_lines = output.split("\n")
    while text_lines and text_lines[-1].strip() == "":
        text_lines.pop()
    if lines > 0:
        text_lines = text_lines[-lines:]
    result = "\n".join(text_lines)
    if warn_floor:
        warning = _floor_warning(pane_id)
        if warning:
            result = f"{warning}\n{result}" if result else warning
    return result


def wait_ready(pane_id: str, timeout: float = PANE_READY_TIMEOUT_S) -> str:
    """Block until pane_id's TUI is ready for input, or flags the trust dialog.

    The very first bug this exists to close: launch() used to send the
    "Lis <brief> et applique-le." instruction the instant spawn() returned,
    before claude had even rendered its first frame. The instruction landed
    in an empty pane and was gone by the time the TUI existed to receive it
    (observed 2026-08-09: the pane showed the workspace-trust dialog, still
    unanswered, eight seconds after launch on a project never opened before).

    Polls capture() every PANE_READY_POLL_INTERVAL_S until either
    TRUST_DIALOG_MARKERS or READY_MARKERS appears ANYWHERE in the pane,
    whichever comes first. The trust dialog is checked FIRST on every poll,
    ahead of the ready markers: Ordo's product decision is to never approve a
    folder automatically (see cli.py's launch), so on the one poll where a
    capture could plausibly satisfy both patterns at once, treating it as the
    dialog is the only direction that cannot silently type into an unhandled
    security prompt.

    The whole pane is captured (lines=0, no slicing), not a fixed tail:
    checked 2026-08-09 against a real claude pane at the geometry relayout()
    now actually produces (260x80, two-panes-tall), the ready banner sits at
    the TOP of the screen and the input box at the BOTTOM, with blank rows
    filling the gap in between. capture() only strips TRAILING blanks, so
    that gap survives and pushes the banner out of any fixed "last N lines"
    window once the pane is taller than N -- a last-40-lines slice, tried
    first, missed the banner entirely in that same real capture. Since
    wait_ready() only ever runs on a freshly spawned pane, before any real
    scrollback has accumulated, capturing the whole thing costs nothing extra
    worth trading the correctness for.

    Returns PANE_ETAT_PRET or PANE_ETAT_CONFIANCE. Raises RuntimeError, in
    French, if neither marker shows up within `timeout` seconds: injecting
    text into a pane whose state nobody has confirmed is exactly what
    produced the original bug, so a caller that cannot tell must never
    proceed to send() on the strength of a guess.
    """
    _reject_self(pane_id)
    deadline = time.monotonic() + timeout
    while True:
        text = capture(pane_id, lines=0, join=True)
        if any(marker in text for marker in TRUST_DIALOG_MARKERS):
            return PANE_ETAT_CONFIANCE
        if any(marker in text for marker in READY_MARKERS):
            return PANE_ETAT_PRET
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"claude's TUI never appeared ready in pane {pane_id} after "
                f"{timeout:.0f}s of waiting; injection cancelled to avoid sending "
                "the instruction into the void"
            )
        time.sleep(PANE_READY_POLL_INTERVAL_S)


def _is_escape(text: str) -> bool:
    """True if text would deliver an actual Escape keypress to the pane."""
    return "\x1b" in text or text.strip() in {"Escape", "escape", "C-["}


# Character send() looks for, right after writing, to confirm the target pane still
# shows an interactive prompt instead of having swallowed the message whole (see
# send()). Measured 2026-08-16 on real claude panes (t-11, docs/diagnostic-envoi.md
# task D): "❯" (U+276F) frames the input line in every state a message actually lands
# in -- idle and bare, or mid-tool with our own message freshly queued behind an
# earlier one ("❯ Press up to edit queued messages", the delayed-not-lost state of the
# diagnostic's cas B, still healthy). It is absent from the one confirmed failure state
# beyond the trust dialog: a full-screen slash-command panel (measured on /cost) that
# replaces the box outright and fills the pane with unrelated content, none of it
# containing this character. /compact itself was re-measured for this task, twice, once
# against an empty session and once against a real one with turns to compact -- neither
# run opened a panel: /compact renders inline ("⎿ Compacted (ctrl+o to see full
# summary)"), the box never leaves. Deliberately distinct from the ASCII ">" the trust
# dialog's own menu uses ("> 1. Yes, I trust this folder", TRUST_DIALOG_MARKERS above):
# the two checks below can never be confused with one another, though TRUST_DIALOG_MARKERS
# is still checked first, for an error naming the actual cause instead of the generic one.
INPUT_BOX_MARKER = "❯"

# How many of the most recent captured lines send() scans for INPUT_BOX_MARKER /
# TRUST_DIALOG_MARKERS. Bounded on purpose, unlike wait_ready()'s lines=0 -- and here
# it is not a performance choice (capture()'s underlying tmux call is always -S -, the
# full scrollback, regardless of what `lines` is sliced down to afterwards): it is a
# correctness one. INPUT_BOX_MARKER is a single character that reappears throughout a
# session's entire prior transcript, once per message anyone ever sent ("❯ <that
# message>" stays in scrollback forever after being submitted). Scanning the full
# history the way wait_ready() does would make the check trivially always pass in any
# session with even one prior turn -- finding some OLD "❯" proves nothing about whether
# the box is there NOW. Bounding the tail is what keeps this check about the CURRENT
# state, but it must stay UNDER PANE_MIN_USABLE_ROWS (the floor every pane's real,
# CURRENTLY VISIBLE height already clears): a full-screen panel or dialog fills that
# whole visible height with fresh content the instant it takes over, so a window
# narrower than the floor is guaranteed to land entirely inside it, never reaching back
# into pre-panel scrollback, on even the most tightly tiled pane this module allows.
# Same value busy() already scans (its own tail window, same reasoning), comfortably
# above the trust dialog's own footprint (~15 lines end to end, TRUST_DIALOG_MARKERS
# above) and 10 lines under the floor for margin.
SEND_VERIFY_LINES = 20

# Delay send() waits after writing, before re-reading the pane (see send()). Must clear
# the slowest measured time for a full-screen panel to finish replacing the box: three
# back-to-back runs of /cost sent into an already-ready pane measured the border
# disappearing at 0.09s, 0.14s and 0.51s after send-keys (2026-08-16, tmux 3.7b) -- over
# a 5x spread between the fastest and slowest run alone, consistent with the variance
# already documented elsewhere in this project. 1.0s matches docs/diagnostic-envoi.md's
# own cas H, which used the same delay to catch the same failure.
#
# This alone is what t-53 and t-57 fell through (2026-08-16, t-59): both were freshly
# spawned panes whose brief injection landed while claude's TUI was still on its startup
# screen. A 1.0s post-write delay is correct for a pane that was ALREADY listening (t-26's
# case), but a pane that has JUST been spawned is a different situation entirely, and
# nothing waited for IT to be ready before writing at all -- see _wait_input_box() below,
# which is send()'s actual fix for that gap.
SEND_VERIFY_DELAY_S = 1.0

# Ceiling _do_launch() (cli.py) passes to send()'s ready_timeout for the ONE call that
# targets a pane it just spawned itself (cli.py:501) -- never send()'s own default (see
# send()'s ready_timeout parameter: None, unconditionally, for every other caller,
# because every other caller targets a pane already proven receptive by an earlier
# successful send(), not one merely assumed ready). Measured 2026-08-16 (t-59) against
# real freshly-spawned `claude` panes, `capture()`d every 0.2s from spawn() until
# INPUT_BOX_MARKER appeared and stayed: three isolated launches (nothing else running)
# reached it in 1.4s, 1.7s and 2.5s; four launched CONCURRENTLY -- approximating a
# chantier starting several exécutantes back to back, the exact situation t-53 and t-57
# were launched under -- pushed every one of the four to 4.9-5.8s. t-53 and t-57
# themselves are reported to have still shown their startup screen a full ten to fifteen
# real seconds in, under whatever load the machine carried that day. 30s matches
# wait_ready()'s own PANE_READY_TIMEOUT_S, on the same reasoning: generous enough to
# clear that worst case with real margin.
SEND_READY_TIMEOUT_S = 30.0

# How many (Ctrl-U, Backspace) pairs _clear_input() sends before every send() writes
# anything. Each pair erases one logical line of the box back into the previous one
# (see _clear_input() for why it takes a pair, not one keystroke). Bounded well above
# any real Ordo message's line count -- a chantier brief run through `ordo say` rarely
# reaches a few dozen lines -- and the bound costs nothing extra regardless: every
# repetition goes out in the SAME single tmux invocation (see _clear_input()), not one
# subprocess per keystroke.
CLEAR_FIELD_MAX_LINES = 80

# Footer hint Claude Code's TUI shows, measured 2026-08-16 (t-26), the moment a SINGLE
# paste crosses its own internal size threshold (confirmed between 711 and 2842
# characters on that run) and collapses pending confirmation instead of submitting on
# the Enter sent right after it: the box keeps the raw text, the footer status line is
# replaced by this string. Not itself matched by send()'s detection (_still_unsent()
# below reads the box's own content, not the footer, since the footer is cosmetic and
# was not verified to appear in every collapse) -- kept here only to name the cause
# precisely in the RuntimeError a caller sees.
PASTE_COLLAPSE_HINT = "paste again to expand"


def _wait_input_box(pane_id: str, timeout: float) -> None:
    """Bloque jusqu'à ce que pane_id montre une boîte de saisie, avant que send() n'écrive.

    wait_ready() (voir plus haut) attend la bannière de démarrage de claude
    (READY_MARKERS) avant que cli.py n'appelle send() -- mais c'est exactement ce que
    t-53 et t-57 ont mesuré insuffisant : la bannière peut apparaître avant que la boîte
    de saisie elle-même ne soit réellement rendue et prête à recevoir du texte, sous
    charge (voir SEND_READY_TIMEOUT_S pour la mesure). Seul send() lui-même, au moment
    précis où il écrit, peut vérifier que CETTE écriture-là a un destinataire -- d'où
    l'appel ICI, jamais chez un appelant.

    Appelée seulement quand `timeout` est fourni par send() (voir son paramètre
    ready_timeout) : PAS sur tout appel de send(), seulement sur celui de _do_launch()
    (cli.py:501), le seul qui vise un pane qu'il vient tout juste de créer lui-même. ordo
    say et ordo answer, qui appellent send() des dizaines de fois par chantier sur un
    pane déjà vivant depuis longtemps, ne passent jamais ici (c5 du brief t-59) --
    mesuré : un pane déjà vivant dont la boîte a été avalée par un panneau plein écran
    (InputBoxMissingTests) doit échouer aussi vite qu'avant cette fonction, pas attendre
    un plafond pensé pour un démarrage qui, pour lui, n'a pas lieu d'être.

    La même fenêtre bornée que la vérification post-écriture de send() (SEND_VERIFY_LINES),
    pas capture(lines=0) comme wait_ready() : contrairement à la bannière, la boîte de
    saisie vit toujours au bas du pane, jamais poussée hors d'une fenêtre récente par une
    scrollback longue -- borner écarte tout marqueur d'une boîte passée, déjà soumise et
    depuis longtemps remontée en scrollback, comme dans SEND_VERIFY_LINES.

    Lève RuntimeError, en français, nommant le pane, si la boîte n'apparaît jamais avant
    `timeout` : c'est le contrat du brief -- send() ne doit jamais écrire dans un pane
    dont personne ne sait s'il écoute (c3), et l'échec ne doit jamais survenir avant
    d'avoir réellement attendu le plafond, jamais sur un simple premier coup d'œil.
    """
    deadline = time.monotonic() + timeout
    while True:
        texte = capture(pane_id, lines=SEND_VERIFY_LINES, join=True)
        if INPUT_BOX_MARKER in texte:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"send failed: pane {pane_id} never showed an input box within "
                f"{timeout:.0f}s -- the message was never written; the pane either "
                "never started or is still stuck on its startup screen"
            )
        time.sleep(PANE_READY_POLL_INTERVAL_S)


def _clear_input(pane_id: str) -> None:
    """Erase whatever the pane's input box currently holds, before send() writes to it.

    Measured 2026-08-16 (t-26) against a real claude pane: Ctrl-U kills back to the
    START of the box's CURRENT line only -- ordinary readline semantics, confirmed by a
    footer hint that appears right after ("Ctrl+Y to paste deleted text") -- never the
    whole multi-line buffer in one keystroke. A Backspace sent right after merges that
    now-empty line into the previous one, moving the cursor to its end. Repeating the
    pair walks back through every line the box holds, one at a time, regardless of how
    many there are; repeating it CLEAR_FIELD_MAX_LINES times on an already-empty box
    was verified harmless the same day (15 repetitions against a bare box left it
    exactly as empty, no scrollback disturbed, nothing recalled into it).

    Sent as ONE tmux invocation, every keystroke of every pair inline, never
    CLEAR_FIELD_MAX_LINES separate subprocess calls: a single chained send-keys is
    exactly what relayout() already does for the same reason (see relayout()'s
    resize-window + select-layout call).

    Deliberately unconditional on every call to send(), never skipped on the assumption
    the box is already empty: this is c4 of t-26's brief, and the failure that brief
    reports is precisely a second, unrelated message concatenating with a first one
    that never submitted -- clearing first is what stops that regardless of whether the
    leftover came from Ordo's own previous send() or from a human who typed into the
    pane in between.
    """
    keys = ["C-u", "BSpace"] * CLEAR_FIELD_MAX_LINES
    _run(["send-keys", "-t", pane_id, *keys])


def _last_box_content(apres: str) -> str | None:
    """Text sitting on the same line as the LAST INPUT_BOX_MARKER in `apres`, stripped.

    The last marker inside SEND_VERIFY_LINES' bounded, recent tail is always the
    CURRENTLY OPEN box, never an earlier submitted turn's own echo (same reasoning
    send() already relies on for INPUT_BOX_MARKER, see below): a turn that actually
    submitted is followed by either the model's own reply or a busy indicator, and
    either one pushes a FRESH, later "❯" for the box underneath -- empty if idle, or
    the generic "Press up to edit queued messages" placeholder if the pane was busy
    and the message was genuinely accepted into its queue (see _still_unsent()).
    Returns None only when no marker exists at all in `apres`, a case send() already
    raises on before this is ever reached.
    """
    idx = apres.rfind(INPUT_BOX_MARKER)
    if idx == -1:
        return None
    line_end = apres.find("\n", idx)
    line = apres[idx:] if line_end == -1 else apres[idx:line_end]
    return line[len(INPUT_BOX_MARKER):].strip()


def _still_unsent(apres: str, text: str) -> bool:
    """True if `text` is still sitting, unconfirmed, in the pane's live input box.

    Compares only text's FIRST line against _last_box_content(). Two real, measured
    shapes of the same failure (t-26, 2026-08-16) both read as one whole line there:
    capture()'s -J already rejoins purely visual, terminal-width wrapping (see
    capture()'s own docstring), so a long SINGLE line Claude Code's TUI has collapsed
    pending confirmation (PASTE_COLLAPSE_HINT, measured above ~2800 characters on that
    run) comes back as one unbroken line here; a genuinely MULTI-line message that
    failed to submit shows its FIRST line alone directly after the marker, the rest on
    their own indented rows below it -- the first line is therefore enough to catch
    both shapes with the same test, and matching a PREFIX rather than the whole text
    keeps it correct even if the box has reflowed the tail differently.

    False on the healthy busy case, on purpose: a message accepted into Claude Code's
    own queue while the pane is mid-turn replaces the box with the generic "Press up to
    edit queued messages" placeholder, never the raw text (measured 2026-08-16,
    distinct from PASTE_COLLAPSE_HINT's collapsed-and-stuck state) -- that placeholder
    never starts with `text`'s own first line, so this reads it as delivered, not lost,
    exactly as it should.
    """
    first_line = text.split("\n", 1)[0].strip()
    if not first_line:
        return False
    contenu = _last_box_content(apres)
    return bool(contenu) and contenu.startswith(first_line)


def send(pane_id: str, text: str, ready_timeout: float | None = None) -> None:
    """Type text into pane_id, then press enter, as two separate tmux calls (I6).

    A single send-keys call carrying both the literal text and C-m eats the
    tail of the input: C-m gets sent as literal characters instead of being
    interpreted as enter, so the typed line never actually submits. The two
    calls must stay separate.

    Escape is refused outright. It interrupts the executante's current
    turn, and there is no legitimate reason to interrupt one from Ordo: if
    it is going the wrong way, it gets talked to, never cut off.

    _clear_input() runs first, unconditionally (c4 of t-26's brief): see its own
    docstring for why a leftover, never-submitted line -- Ordo's own or a human's --
    must never be given the chance to concatenate with what is about to be typed.

    After both keys are sent, the pane is re-read and checked before returning
    (task D of docs/diagnostic-envoi.md's découpage, extended by t-26). The two
    send-keys calls above succeeding only proves tmux delivered the bytes to the
    pane's pty -- not that anything on the other end was listening, or that the Enter
    was actually registered as "submit". Measured, real failures, all leaving the pane
    alive and busy() reading idle -- nothing else in this module would ever notice:

    - a full-screen slash-command panel (e.g. /cost) can silently replace the input
      box and swallow every message sent afterward without a trace;
    - the tmux trust dialog throws the typed text away outright and reads C-m as
      confirming "Yes, I trust this folder" instead;
    - measured 2026-08-16 for t-26: Claude Code's TUI can accept the typed text into
      its box but swallow the very next C-m -- confirmed twice, live, once for a
      multi-line message sent immediately after an earlier one had just been
      submitted, once for a single line long enough to cross the TUI's own paste-size
      threshold and collapse pending confirmation (PASTE_COLLAPSE_HINT). Both times a
      LATER, separately-timed Enter submitted the exact same unmodified text untouched
      -- proof the text itself always lands correctly, only its immediate validation
      is what gets lost.

    A caller that gets RuntimeError here knows its message did NOT land, instead of
    believing a write that went into the void.

    The proof is never on-screen text used as evidence of SUCCESS: the diagnostic that
    motivated the panel/dialog checks above found that check ("does my own message's
    text appear on screen?") to be pure decor -- text can sit visibly in a still-
    unsubmitted box, or get echoed by the terminal without ever being interpreted as a
    command, either way looking like success. _still_unsent() below uses on-screen text
    the OTHER way around, as evidence of FAILURE, which this diagnostic never ruled
    out: text that is STILL sitting in the box, unconfirmed, five real seconds after
    send() wrote it, is not decor, it is the message never having left.

    ready_timeout (t-59), when given, blocks BEFORE any of that -- before even
    _clear_input() -- until the pane actually shows an input box to write into, via
    _wait_input_box() (see its own docstring for why it lives here and not in a caller).
    None by default, meaning NO wait at all, byte-for-byte the same as before this
    parameter existed: every caller except cli.py's _do_launch() (cli.py:501, the one
    call that targets a pane it just spawned itself) leaves this at None, since ordo say
    and ordo answer only ever target a pane already proven receptive by an earlier
    successful send() -- measured 2026-08-16 (t-59): passing SEND_READY_TIMEOUT_S here
    unconditionally, for every caller, turned a pane whose box a full-screen panel had
    swallowed (already covered by InputBoxMissingTests) from a ~1s failure into a ~30s
    one, which is exactly the "ne casse pas l'envoi sur un pane vivant" c5 forbids.
    """
    _reject_self(pane_id)
    if _is_escape(text):
        raise ValueError(
            "refused: Escape would interrupt the executor's current turn"
        )
    if ready_timeout is not None:
        _wait_input_box(pane_id, ready_timeout)
    _clear_input(pane_id)
    _run(["send-keys", "-t", pane_id, "-l", text])
    _run(["send-keys", "-t", pane_id, "C-m"])
    time.sleep(SEND_VERIFY_DELAY_S)
    apres = capture(pane_id, lines=SEND_VERIFY_LINES, join=True)
    if any(marker in apres for marker in TRUST_DIALOG_MARKERS):
        raise RuntimeError(
            f"send failed: pane {pane_id} shows the tmux trust dialog after send() "
            "wrote to it -- the text was thrown away and C-m likely just confirmed "
            "'Yes, I trust this folder' instead of submitting the message"
        )
    if INPUT_BOX_MARKER not in apres:
        raise RuntimeError(
            f"send failed: pane {pane_id} has no input box after send() wrote to it "
            "(a full-screen panel is likely showing instead, e.g. a slash command); "
            "the message was written into the void, not delivered"
        )
    if _still_unsent(apres, text):
        # Recovery: measured 2026-08-16 (t-26) that a SECOND, separately-timed Enter
        # reliably submits the exact same unmodified text once the swallow above has
        # already happened -- confirmed live against both failure shapes (see
        # _still_unsent()'s docstring). Retried once, never looped: a pane that is
        # still stuck after this is treated as a genuine failure, not a race to keep
        # guessing at (c5 of t-26's brief).
        _run(["send-keys", "-t", pane_id, "C-m"])
        time.sleep(SEND_VERIFY_DELAY_S)
        apres = capture(pane_id, lines=SEND_VERIFY_LINES, join=True)
        if _still_unsent(apres, text):
            raise RuntimeError(
                f"send failed: pane {pane_id} still shows the just-sent text, "
                "unconfirmed, after a recovery Enter -- likely "
                f"'{PASTE_COLLAPSE_HINT}' (Claude Code's TUI can collapse a paste "
                "above its own size threshold, or swallow the Enter sent right after "
                "an earlier submission, and drop the very next C-m either way, "
                "measured 2026-08-16); the message was typed but never submitted"
            )


def alive(pane_id: str) -> bool:
    """True if pane_id still exists with its process running.

    No tmux server at all (list-panes -a itself failing) reads as "not
    alive" rather than raising: for this particular query, the absence of
    the pane IS the answer, not a hidden failure worth an exception.
    """
    _reject_self(pane_id)
    result = _tmux(["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"])
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        pid, _, dead = line.partition(" ")
        if pid == pane_id:
            return dead == "0"
    return False


def live_ids() -> set[str]:
    """Identifiants de TOUS les panes vivants du serveur, en un seul appel tmux.

    alive() repond la meme question pour un pane a la fois, et fait un list-panes complet
    a chaque appel. Une carte rafraichie toutes les cinq secondes sur un chantier de
    quarante taches paierait quarante appels par cycle pour la meme information. Absence
    de serveur tmux : ensemble vide, pas d'exception, exactement comme alive() -- pour
    cette question, l'absence de pane EST la reponse.
    """
    result = _tmux(["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"])
    if result.returncode != 0:
        return set()
    vivants = set()
    for line in result.stdout.splitlines():
        pane_id, _, dead = line.partition(" ")
        if pane_id and dead == "0":
            vivants.add(pane_id)
    return vivants


# Nom de la fenetre de service qu'ouvre side_window(). Fixe, et volontairement improbable
# pour une fenetre ouverte a la main : side_window() respawn ce qui porte deja ce nom.
MAP_WINDOW_NAME = "ordo-map"


def side_window(session: str, name: str, cmd: str) -> str:
    """Fenetre de service dans la session d'un chantier, sans toucher a ses exécutantes.

    new-window et non split-window : un split redimensionne toutes les panes de la fenetre
    des exécutantes, ce qui force le TUI de chaque `claude` en cours a se redessiner. Une
    fenetre a part n'en touche aucune. -d pour ne pas voler la fenetre courante d'un humain
    deja attache.

    Les hooks client-attached / client-detached poses par ensure_session() visent un
    window_id explicite (Point B) et non "la fenetre courante" : c'est precisement ce qui
    rend cette seconde fenetre inoffensive pour la geometrie de la premiere.

    Une fenetre portant deja `name` est respawnee plutot que dupliquee, sinon chaque appel
    empilerait une fenetre morte de plus. Le nom doit donc rester un nom de service, jamais
    un nom qu'un humain donnerait a sa propre fenetre.

    L'index de la nouvelle fenetre est calcule et impose, jamais laisse a tmux. Ce que la
    fenetre de service ne doit JAMAIS faire, c'est passer devant celle des exécutantes :
    _window_id_of() rend la PREMIERE fenetre listee, ensure_session() la rappelle a chaque
    launch et _record_window() la persiste, donc une fenetre de carte arrivee en tete ferait
    prendre au chantier la carte pour sa propre fenetre, et l'executante suivante serait
    splittee dedans.

    Mesure du 15 aout 2026 sur tmux 3.x : `new-window -t =session:` place bien la fenetre a
    max(index) + 1, meme avec base-index a 0 et la seule fenetre existante a l'index 1. La
    forme paresseuse serait donc correcte aujourd'hui. max(index) + 1 est ecrit quand meme
    parce que la garantie ci-dessus ne doit dependre ni de base-index ni de la version de
    tmux, et parce que le cout de l'ecrire est une ligne.
    """
    if not _session_exists(session):
        raise RuntimeError(f"tmux session {session} does not exist")
    listing = _run(
        ["list-windows", "-t", f"={session}", "-F", "#{window_index} #{window_id} #{window_name}"]
    )
    indices = []
    for line in listing.splitlines():
        index, _, reste = line.partition(" ")
        window_id, _, window_name = reste.partition(" ")
        if window_name == name:
            _run(["respawn-window", "-k", "-t", window_id, cmd])
            return window_id
        with suppress(ValueError):
            indices.append(int(index))
    cible = (max(indices) + 1) if indices else 1
    return _run(
        [
            "new-window", "-d", "-P", "-F", "#{window_id}",
            "-n", name, "-t", f"={session}:{cible}", cmd,
        ]
    ).strip()


def busy(pane_id: str) -> bool:
    """True if any BUSY_MARKERS substring appears in the last 20 lines.

    esc to interrupt alone is truncated away in a narrow pane; scanning the
    union of markers is the fix (see references/tmux.md).
    """
    _reject_self(pane_id)
    tail = capture(pane_id, lines=20, join=True)
    return any(marker in tail for marker in BUSY_MARKERS)


def kill(pane_id: str) -> bool:
    """Destroy pane_id outright; its process is gone immediately.

    Returns True when a pane was actually destroyed, False when it was already
    gone. Idempotent for the same reason as kill_session(): cmd_close walks
    pane ids stored in the state, most of which point at panes that died long
    ago, and a raw tmux error there aborted a close that had already archived
    everything. The boolean exists so the caller never announces a process it
    did not kill. Every other tmux failure still raises.
    """
    _reject_self(pane_id)
    result = _tmux(["kill-pane", "-t", pane_id])
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").strip()
    # "no server running" est le meme fait vu de plus loin : la derniere session morte
    # emporte le serveur avec elle, et tout ce qu'il contenait avec.
    if "can't find pane" in stderr or "no server running" in stderr:
        return False
    raise RuntimeError(f"tmux command failed (tmux kill-pane -t {pane_id}): {stderr}")


def kill_session(session: str) -> None:
    """Destroy the named tmux session, and nothing else.

    Exact match only ('=session', Point C): without it, tmux resolves a
    kill-session target by prefix, verified 2026-08-09 to destroy a
    DIFFERENTLY-named session that merely starts with the same characters
    when the intended one does not exist yet -- exactly the kind of accident
    this module exists to rule out. `kill-session -t =X` acts on X alone,
    closes every window and pane linked to it, and detaches any client
    attached to it; every other session on the server, X's own server
    process, is left untouched.

    `tmux kill-server` and `pkill tmux` are never used anywhere in this
    module, deliberately: the server is shared with the human's own tmux
    sessions, and destroying it destroys their unrelated work along with
    Ordo's. cli.py's cmd_close is the only intended caller, and only after
    confirming (via panes(window)) that the session's chantier-managed panes
    are already gone.

    Idempotent: a session that is already gone is the intended end state, not
    a failure. Found by a real smoke run, where closing a campaign whose
    session had already died archived everything and then exited non-zero on
    this call. Checking existence first would not fix it either, the session
    can die between the check and the call. Every other tmux failure still
    raises.
    """
    result = _tmux(["kill-session", "-t", f"={session}"])
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").strip()
    if "can't find session" in stderr or "no server running" in stderr:
        return
    raise RuntimeError(f"tmux command failed (tmux kill-session -t ={session}): {stderr}")


def panes(window: str) -> list[dict]:
    """List every pane of the chantier's window.

    `window` is the nominal argument (Point B): a window_id targeted
    directly and unambiguously, or a legacy session name resolved through
    _resolve_target() to that session's current window, same as spawn() and
    relayout() above. A missing window or session (never launched, or
    already closed) yields an empty list: that is the correct answer for a
    chantier with no running panes, not a hidden failure.

    sousPlancher is derived from the SAME width/height this one query
    already returns, no second tmux call: True whenever a pane currently
    reads narrower or shorter than PANE_MIN_USABLE_COLS/ROWS (see
    is_degraded(), the pane_id-only twin of this field for callers that
    only have one pane_id, not a whole window listing).
    """
    target = _resolve_target(window)
    result = _tmux(
        [
            "list-panes",
            "-t",
            target,
            "-F",
            "#{pane_id}\t#{pane_title}\t#{pane_width}\t#{pane_height}\t"
            "#{pane_dead}\t#{pane_active}",
        ]
    )
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        pane_id, title, width, height, dead, active = line.split("\t")
        width_i, height_i = int(width), int(height)
        rows.append(
            {
                "pane_id": pane_id,
                "titre": title,
                "largeur": width_i,
                "hauteur": height_i,
                "vivant": dead == "0",
                "actif": active == "1",
                "sousPlancher": width_i < PANE_MIN_USABLE_COLS or height_i < PANE_MIN_USABLE_ROWS,
            }
        )
    return rows


if __name__ == "__main__":
    # The only place this module invokes itself: the client-detached hook installed by
    # _install_attach_hooks() runs as a plain tmux/shell command, potentially long after
    # the Python process that called ensure_session() has exited, so restoring the wide
    # geometry cannot call relayout() as a live function -- it has to re-launch this file
    # in a fresh interpreter. Every other function above is used strictly as a library
    # import; this block is dead code for every caller except that one hook.
    #
    # The argument is a window_id ('@12', Point B): _client_detached_hook_command()
    # always embeds the window_id it was given at hook-install time. relayout() also
    # still accepts a legacy session name here for reasonable backward compatibility
    # (see _resolve_target()), so this entry point needs no branching of its own.
    if len(sys.argv) == 3 and sys.argv[1] == "--relayout":
        # Best effort, silencieux, delibere. Ce process tourne en tache de fond via
        # `run-shell -b`, declenche par un detachement humain, potentiellement longtemps
        # apres : la fenetre peut avoir disparu entre-temps, et c'est un cas NORMAL, pas
        # une panne. Le laisser echouer bruyamment ne se contente pas de polluer un log :
        # mesure du 11 aout 2026, tmux n'a alors aucun client a qui montrer l'erreur, la
        # met en attente, et la recrache sur la stderr de la PROCHAINE commande qui parle
        # au serveur, laquelle echoue alors qu'elle a parfaitement reussi. C'est ce qui
        # faisait echouer test_marker_scrolled_out_of_last_20_lines_goes_idle_again quatre
        # fois sur cinq dans la suite complete, et qui pouvait faire lever un `ordo say`
        # apres qu'un humain se soit detache de la session.
        #
        # Le retablissement de la geometrie large est un confort de lecture, jamais une
        # operation dont depend la correction d'un chantier : echouer sans rien dire est
        # ici le bon comportement, et c'est la seule exception au principe "aucun refus
        # silencieux" (I8) de tout ce depot, parce qu'il n'y a personne a informer.
        try:
            relayout(sys.argv[2])
        except Exception:
            pass
        sys.exit(0)
    else:
        print("usage: panes.py --relayout <window_id|session>", file=sys.stderr)
        sys.exit(2)
