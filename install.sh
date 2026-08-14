#!/usr/bin/env bash
#
# Installs the ordo command and the ordo skill.
#
# Only needed for a plain git clone. Installed as a Claude Code plugin, bin/ is
# put on PATH and the skill is loaded from skills/ordo automatically, so this
# script has nothing left to do.
#
# Nothing is installed silently: every destination is printed, and an existing
# skill directory is never overwritten without saying so.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${ORDO_BIN_DIR:-$HOME/.local/bin}"
SKILL_DIR="${ORDO_SKILL_DIR:-$HOME/.claude/skills/ordo}"

fail() { printf 'ordo install: %s\n' "$1" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"

python3 - <<'PY' || fail "python 3.10 or later is required"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

if ! command -v tmux >/dev/null 2>&1; then
    printf 'ordo install: warning, tmux not found on PATH. Ordo cannot launch executor\n'
    printf '  sessions without it. Install it with your package manager, then run "ordo doctor".\n'
fi

if ! command -v claude >/dev/null 2>&1; then
    printf 'ordo install: warning, the claude binary was not found on PATH. Ordo launches\n'
    printf '  real Claude Code sessions and needs it. See https://claude.com/claude-code\n'
fi

# --- command -----------------------------------------------------------------

mkdir -p "$BIN_DIR"
ln -sf "$REPO/bin/ordo" "$BIN_DIR/ordo"
printf 'ordo install: linked %s -> %s\n' "$BIN_DIR/ordo" "$REPO/bin/ordo"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'ordo install: warning, %s is not on your PATH. Add it to your shell profile.\n' "$BIN_DIR" ;;
esac

# --- skill -------------------------------------------------------------------

if [ -e "$SKILL_DIR" ] && [ ! -L "$SKILL_DIR" ]; then
    printf 'ordo install: %s already exists and is not a symlink.\n' "$SKILL_DIR"
    printf '  Move it aside, then run this script again. Nothing was changed.\n'
    exit 1
fi

mkdir -p "$(dirname "$SKILL_DIR")"
ln -sfn "$REPO/skills/ordo" "$SKILL_DIR"
printf 'ordo install: linked %s -> %s\n' "$SKILL_DIR" "$REPO/skills/ordo"

# --- next steps --------------------------------------------------------------

cat <<'EOF'

Installed. Next:

  ordo doctor                     check python, tmux, claude and the active ORDO_HOME
  cd <your project>
  export ORDO_HOME=$PWD/.ordo     one state directory per project, see README
  ordo start $PWD --goal "..."    a PATH to an existing directory, not a project name

Read the "Read this before you install" section of README.md if you have not:
executor sessions run with permission prompts skipped by default.
EOF
