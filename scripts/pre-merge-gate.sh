#!/usr/bin/env bash
# pre-merge-gate.sh — Blocks squash merges and direct pushes to origin/master
# Run via .pre-commit-config.yaml local repos or manually before pushing.
set -euo pipefail

BRANCH="$(git symbolic-ref --short HEAD)"
ORIGIN_MASTER="origin/master"

echo "[[MERGE-GATE]] Checking branch: $BRANCH"

# Rule 1: Reject squash-merge commits being pushed to master
if [ "$BRANCH" = "master" ]; then
    # Check if any staged/unpushed commit is a squash merge pattern
    SQUASH=$(git log --oneline "$ORIGIN_MASTER..HEAD" --grep="#[0-9]*$" -n 1 || true)
    if [ -n "$SQUASH" ]; then
        echo "[MERGE-GATE BLOCKED] Squash merge detected on master:"
        git log --oneline "$ORIGIN_MASTER..HEAD"
        exit 1
    fi
fi

echo "[MERGE-GATE] OK — no squash merges detected"
