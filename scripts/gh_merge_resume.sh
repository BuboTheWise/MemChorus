#!/usr/bin/env bash
#
# gh_merge_resume.sh — idempotent PR merge + issue-close resume script.
#
# Usage:
#   ./scripts/gh_merge_resume.sh <PR_NUMBER> [ISSUE_1 ISSUE_2 ...]
#
# Context: the 2026-08-28 PR #145 cycle (t_cf72f9ce) hit the iteration ceiling
# with `gh pr merge ... --disable-auto-delete` printed-usage to stderr as a
# silent failure — the flag does not exist on gh 2.87.2. This script encodes
# the `gh-pr-merge-verification-gate` skill's rules in code:
#
#   1. Every `gh` call's exit code is checked; a non-zero exit aborts before
#      the next state-changing call.
#   2. Every state-changing call (merge / issue close) is preceded by a state
#      read, making the whole sequence re-runnable after any interruption.
#   3. Only flags verified in `gh 2.87.2` are used:
#        gh pr merge:    -s/--squash, -d/--delete-branch
#        gh issue close: -c/--comment, -r/--reason {completed|not planned}
#   4. A structured JSON summary goes to stdout for tooling to capture;
#      human diagnostics go to stderr so they don't pollute the JSON.
#
# Exit codes:
#   0  success (PR MERGED, all listed issues CLOSED — either pre-existing or closed now)
#   1  failure (merge failed, state unexpected, or an issue could not be closed)
#
set -euo pipefail

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <PR_NUMBER> [ISSUE_1 ISSUE_2 ...]" >&2
  exit 1
fi

PR_NUM="$1"
shift
ISSUES=("$@")

if ! [[ "$PR_NUM" =~ ^[0-9]+$ ]]; then
  echo "ERROR: PR number must be numeric, got: $PR_NUM" >&2
  exit 1
fi
for i in ${ISSUES[@]+"${ISSUES[@]}"}; do
  if ! [[ "$i" =~ ^[0-9]+$ ]]; then
    echo "ERROR: issue number must be numeric, got: $i" >&2
    exit 1
  fi
done

# --- Step 1: check PR state; merge only if OPEN --------------------------------
PR_STATE=$(gh pr view "$PR_NUM" --json state --jq .state) || {
  echo "ERROR: cannot read PR #$PR_NUM state" >&2
  exit 1
}

if [ "$PR_STATE" = "MERGED" ]; then
  log "PR #$PR_NUM already MERGED — skipping merge"
elif [ "$PR_STATE" = "OPEN" ]; then
  log "PR #$PR_NUM is OPEN — running gh pr merge -s --delete-branch"
  if ! MERGE_ERR=$(gh pr merge "$PR_NUM" --squash --delete-branch 2>&1); then
    echo "ERROR: gh pr merge #$PR_NUM failed:" >&2
    echo "$MERGE_ERR" | sed 's/^/    /' >&2
    exit 1
  fi
  PR_STATE=$(gh pr view "$PR_NUM" --json state --jq .state)
  if [ "$PR_STATE" != "MERGED" ]; then
    echo "ERROR: merge ran but PR #$PR_NUM state is '$PR_STATE' (expected MERGED)" >&2
    exit 1
  fi
  log "PR #$PR_NUM now MERGED"
else
  echo "ERROR: PR #$PR_NUM is in state '$PR_STATE' (expected OPEN or MERGED)" >&2
  exit 1
fi

# --- Step 2: final SHA ---------------------------------------------------------
# NOTE (gh 2.87.2): the dotted field path `mergeCommit.oid` is NOT a valid
# `--json` value — gh rejects it with "Unknown JSON field". Use the object
# field `mergeCommit` and let `--jq` do the second-level dereference.
MERGE_SHA=$(gh pr view "$PR_NUM" --json mergeCommit --jq .mergeCommit.oid) || {
  echo "ERROR: cannot read merge commit SHA for PR #$PR_NUM" >&2
  exit 1
}
log "merge commit SHA: $MERGE_SHA"

# --- Step 3: close issues idempotently -----------------------------------------
CLOSED_NOW=()
SKIPPED=()
for ISSUE_NUM in ${ISSUES[@]+"${ISSUES[@]}"}; do
  ISSUE_STATE=$(gh issue view "$ISSUE_NUM" --json state --jq .state) || {
    echo "ERROR: cannot read issue #$ISSUE_NUM state" >&2
    exit 1
  }
  if [ "$ISSUE_STATE" = "CLOSED" ]; then
    log "issue #$ISSUE_NUM already CLOSED — skipping close"
    SKIPPED+=("$ISSUE_NUM")
    continue
  fi
  log "closing issue #$ISSUE_NUM (-c 'Fixed in $MERGE_SHA via PR #$PR_NUM' -r completed)"
  if ! CLOSE_ERR=$(gh issue close "$ISSUE_NUM" --comment "Fixed in ${MERGE_SHA} via PR #${PR_NUM}" --reason completed 2>&1); then
    echo "ERROR: gh issue close #$ISSUE_NUM failed:" >&2
    echo "$CLOSE_ERR" | sed 's/^/    /' >&2
    exit 1
  fi
  log "issue #$ISSUE_NUM closed"
  CLOSED_NOW+=("$ISSUE_NUM")
done

# --- Step 4: JSON summary to stdout ---------------------------------------------
PR_STATE_FINAL=$(gh pr view "$PR_NUM" --json state --jq .state)

# NOTE: ${arr[@]+"${arr[@]}"} keeps `set -u` happy for empty arrays (bash < 4.4).
printf '%s\n' ${CLOSED_NOW[@]+"${CLOSED_NOW[@]}"} | sort -n | awk 'NF' > /tmp/_closed.$$
printf '%s\n' ${SKIPPED[@]+"${SKIPPED[@]}"}   | sort -n | awk 'NF' > /tmp/_skipped.$$

python3 - "$PR_NUM" "$PR_STATE_FINAL" "$MERGE_SHA" /tmp/_closed.$$ /tmp/_skipped.$$ <<'PY'
import json, sys
pr_num, state, sha, closed_path, skipped_path = sys.argv[1:6]
def load(p):
    with open(p) as f:
        return [int(x) for x in f.read().split() if x]
print(json.dumps({
    "pr": f"#{pr_num}",
    "state": state,
    "sha": sha,
    "issues_closed": load(closed_path),
    "skipped": load(skipped_path),
}))
PY

rm -f /tmp/_closed.$$ /tmp/_skipped.$$
