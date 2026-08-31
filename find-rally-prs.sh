#!/bin/bash
# find-rally-prs.sh - Find PRs associated with a Rally story/defect ID
#
# Searches the current repo's PRs three ways (case insensitive):
#   1. Branch name partial match
#   2. PR title/body search
#   3. Commit messages (mapped back to their containing PRs)
#
# Usage: ./find-rally-prs.sh <RALLY_ID> [--numbers]   (e.g. US123456 or DE45678)
# Run from inside the git repo you want to search.
#
# --numbers: machine-readable mode for scripting — prints only the unique
#            PR numbers (one per line) on stdout; progress goes to stderr.
#
# Exit codes: 0 = PRs found, 1 = none found, 2 = usage/environment error

set -euo pipefail

RALLY_ID=""
NUMBERS_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --numbers) NUMBERS_ONLY=true ;;
        -*) echo "Unknown option: $arg" >&2; exit 2 ;;
        *) RALLY_ID="$arg" ;;
    esac
done
[[ -n "$RALLY_ID" ]] || { echo "Usage: ./find-rally-prs.sh <RALLY_ID> [--numbers]  (e.g. US123456 or DE45678)" >&2; exit 2; }
PR_SCAN_LIMIT=${PR_SCAN_LIMIT:-1000}

fail() {
    echo "❌ $1" >&2
    exit 2
}

command -v gh >/dev/null 2>&1 || fail "gh CLI not found. Install with: brew install gh"
command -v jq >/dev/null 2>&1 || fail "jq not found. Install with: brew install jq"
gh auth status >/dev/null 2>&1 || fail "gh is not authenticated. Run: gh auth login"

REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null) \
    || fail "Not inside a GitHub repo (or no remote configured). cd into the repo first."

ID_LOWER=$(echo "$RALLY_ID" | tr '[:upper:]' '[:lower:]')

echo "🔍 Searching $REPO for PRs associated with $RALLY_ID ..." >&2
$NUMBERS_ONLY || echo ""

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
MATCHES="$TMP_DIR/matches.jsonl"   # one JSON object per match: {number, source}
PR_DETAILS="$TMP_DIR/prs.json"

# --- Strategy 1: branch name partial match (client-side, case insensitive) ---
gh pr list --state all --limit "$PR_SCAN_LIMIT" --json number,headRefName 2>/dev/null \
    | jq --arg id "$ID_LOWER" \
        '.[] | select(.headRefName | ascii_downcase | contains($id)) | {number, source: "branch"}' \
    >> "$MATCHES" \
    || echo "⚠️  Branch-name scan failed (continuing with other strategies)" >&2

# --- Strategy 2: title/body search (server-side, case insensitive) ---
# in:title,body is required: without it GitHub also matches PR comments,
# which returns PRs where the ID was merely mentioned in a discussion.
gh pr list --state all --search "$RALLY_ID in:title,body" --limit 100 \
    --json number \
    --jq '.[] | {number, source: "title/body"}' \
    >> "$MATCHES" 2>/dev/null \
    || echo "⚠️  Title/body search failed (continuing with other strategies)" >&2

# --- Strategy 3: commit messages -> containing PRs ---
COMMIT_SHAS=$(gh search commits "$RALLY_ID" --repo "$REPO" --limit 50 \
    --json sha --jq '.[].sha' 2>/dev/null) \
    || { echo "⚠️  Commit search failed (continuing with other strategies)" >&2; COMMIT_SHAS=""; }

for sha in $COMMIT_SHAS; do
    gh api "repos/$REPO/commits/$sha/pulls" \
        --jq '.[] | {number, source: "commit"}' \
        >> "$MATCHES" 2>/dev/null || true
done

if [[ ! -s "$MATCHES" ]]; then
    echo "No PRs found for $RALLY_ID in $REPO." >&2
    exit 1
fi

# --- Dedupe: collapse to unique PR numbers, keeping the set of match sources ---
UNIQUE=$(jq -s 'group_by(.number) | map({number: .[0].number, sources: (map(.source) | unique | join(", "))})' "$MATCHES")

if $NUMBERS_ONLY; then
    echo "$UNIQUE" | jq -r '.[].number'
    exit 0
fi

# --- Fetch details for each unique PR ---
echo "$UNIQUE" | jq -r '.[].number' | while read -r num; do
    gh pr view "$num" --json number,title,state,headRefName,url 2>/dev/null || true
done | jq -s '.' > "$PR_DETAILS"

# --- Merge details with match sources and print ---
COUNT=$(echo "$UNIQUE" | jq 'length')
echo "✅ Found $COUNT PR(s) for $RALLY_ID:"
echo ""

jq -nr --argjson unique "$UNIQUE" --slurpfile prs "$PR_DETAILS" '
    ($unique | map({(.number | tostring): .sources}) | add) as $srcmap
    | $prs[0][]
    | "  #\(.number) [\(.state)] \(.title)\n      branch:  \(.headRefName)\n      matched: \($srcmap[.number | tostring])\n      \(.url)\n"
'

exit 0
