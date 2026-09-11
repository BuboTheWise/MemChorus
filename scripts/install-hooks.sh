#!/usr/bin/env sh
# install-hooks.sh — point core.hooksPath at ./.githooks for this repo.
#
# Usage:  sh scripts/install-hooks.sh
#
# Idempotent: re-running just confirms the existing value. Prints what it
# did so the operator has a visible confirmation (the acceptance criteria
# on IMPL #203 require an "idempotent + prints what it did" installer).
#
# Local only (operator's box); CI hosts don't need the gate.

set -eu

# Resolve the repo root: the script lives in <root>/scripts/, so root is
# one component up.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Sanity: the target hook must exist or we'd be installing a hooksPath
# that points at an empty directory (git would then silently skip every
# hook and the gate would vanish) — surface a loud error.
HOOK_DIR=".githooks"
if [ ! -f "$HOOK_DIR/pre-push" ]; then
    echo "install-hooks: ERROR: $HOOK_DIR/pre-push not found in $ROOT" >&2
    echo "install-hooks: expected a hook there (IMPL #203)" >&2
    exit 1
fi

# Ensure it's executable (git invokes it as `sh <hook>` so this is
# belt-and-suspenders, but some flows spawn it directly).
chmod +x "$HOOK_DIR/pre-push"

CURRENT="$(git config core.hooksPath || true)"
if [ "$CURRENT" = "$HOOK_DIR" ]; then
    echo "install-hooks: OK — core.hooksPath already set to '$HOOK_DIR' (no change)"
    exit 0
fi

git config core.hooksPath "$HOOK_DIR"
echo "install-hooks: set core.hooksPath='$HOOK_DIR' (was: '${CURRENT:-<unset>}')"
echo "install-hooks: verify with:  git config core.hooksPath"
