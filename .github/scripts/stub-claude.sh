#!/usr/bin/env bash
#
# Puts a stub `claude` on the runner's PATH.
#
# No test in this repository ever invokes claude: the launch and resume tests
# mock ordo.panes entirely, and the tmux tests spawn plain bash panes. But the
# CLI refuses to launch or resume when `claude` is absent from PATH, and that
# refusal is itself a tested behaviour, so `shutil.which("claude")` has to
# return something for those tests to reach the code they exercise.
#
# The stub is deliberately loud and useless: if anything in CI ever really
# invokes it, the run fails on the spot instead of quietly pretending a model
# answered. No token is spent, no model is called, ever.

set -euo pipefail

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

cat >"$BIN_DIR/claude" <<'STUB'
#!/bin/sh
echo "stub claude (CI): no model is available here, and none should be needed." >&2
echo "A test that actually invoked claude is a test that must be rewritten." >&2
exit 127
STUB

chmod +x "$BIN_DIR/claude"
echo "$BIN_DIR" >>"$GITHUB_PATH"

printf 'stub installed at %s\n' "$BIN_DIR/claude"
