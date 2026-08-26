#!/usr/bin/env bash
#
# cherry-pick-story.sh
#
# Cherry-pick a user story's commits from one release into another.
#
# Finds the branch containing the story number, computes the commits it has
# on top of the from-release branch, creates a new branch off the to-release
# branch, cherry-picks those commits onto it, and pushes the result.
#
# Usage:
#   cherry-pick-story.sh --story US123456 --from-release 42 --to-release 43
#
# Options:
#   --story <id>            Story id embedded in the branch name (required)
#   --from-release <n>      Release the story branch was based on (required)
#   --to-release <n>        Release to carry the story into (required)
#   --remote <name>         Git remote (default: origin)
#   --release-prefix <p>    Release branch prefix (default: release-)
#   --dry-run               Show what would happen without changing anything
#
# On a cherry-pick conflict the script stops and leaves the conflict in place
# so you can resolve it manually, then continue with:
#   git cherry-pick --continue   (repeat until done)
#   git push -u <remote> <new-branch>

set -euo pipefail

STORY=""
FROM_RELEASE=""
TO_RELEASE=""
REMOTE="origin"
RELEASE_PREFIX="release-"
DRY_RUN=false

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | tail -n +2
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --story)          STORY="${2:-}"; shift 2 ;;
    --from-release)   FROM_RELEASE="${2:-}"; shift 2 ;;
    --to-release)     TO_RELEASE="${2:-}"; shift 2 ;;
    --remote)         REMOTE="${2:-}"; shift 2 ;;
    --release-prefix) RELEASE_PREFIX="${2:-}"; shift 2 ;;
    --dry-run)        DRY_RUN=true; shift ;;
    -h|--help)        usage 0 ;;
    *)                die "Unknown argument: $1 (use --help)" ;;
  esac
done

[[ -n "$STORY" ]]        || die "--story is required"
[[ -n "$FROM_RELEASE" ]] || die "--from-release is required"
[[ -n "$TO_RELEASE" ]]   || die "--to-release is required"
[[ "$FROM_RELEASE" != "$TO_RELEASE" ]] || die "--from-release and --to-release are the same"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside a git repository"

FROM_BRANCH="${RELEASE_PREFIX}${FROM_RELEASE}"
TO_BRANCH="${RELEASE_PREFIX}${TO_RELEASE}"

if [[ -n "$(git status --porcelain)" ]]; then
  die "Working tree is not clean. Commit or stash your changes first."
fi

info "Fetching from $REMOTE..."
git fetch --prune "$REMOTE"

# --- Locate the story branch on the remote -----------------------------------
MATCHES=()
while IFS= read -r line; do
  MATCHES+=("$line")
done < <(git branch -r --list "${REMOTE}/*${STORY}*" --format='%(refname:short)')

if [[ ${#MATCHES[@]} -eq 0 ]]; then
  die "No branch on '$REMOTE' matches story '$STORY'"
elif [[ ${#MATCHES[@]} -gt 1 ]]; then
  echo "Multiple branches match story '$STORY':" >&2
  printf '  %s\n' "${MATCHES[@]}" >&2
  die "Ambiguous story branch. Rename or delete the extras, or narrow the story id."
fi

STORY_REF="${MATCHES[0]}"                      # e.g. origin/feature/US123456-fix-thing
STORY_BRANCH="${STORY_REF#"${REMOTE}"/}"       # e.g. feature/US123456-fix-thing
info "Found story branch: $STORY_BRANCH"

# --- Verify release branches exist on the remote -----------------------------
git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${FROM_BRANCH}" >/dev/null \
  || die "Branch '$FROM_BRANCH' does not exist on '$REMOTE'"
git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${TO_BRANCH}" >/dev/null \
  || die "Branch '$TO_BRANCH' does not exist on '$REMOTE'"

# --- Work out which commits to cherry-pick -----------------------------------
COMMITS=()
while IFS= read -r line; do
  COMMITS+=("$line")
done < <(git rev-list --reverse --no-merges "${REMOTE}/${FROM_BRANCH}..${STORY_REF}")

MERGE_COUNT=$(git rev-list --merges --count "${REMOTE}/${FROM_BRANCH}..${STORY_REF}")
if [[ "$MERGE_COUNT" -gt 0 ]]; then
  echo "WARNING: $MERGE_COUNT merge commit(s) on $STORY_BRANCH will be skipped" >&2
  echo "         (only non-merge commits are cherry-picked)." >&2
fi

[[ ${#COMMITS[@]} -gt 0 ]] \
  || die "No commits found on '$STORY_BRANCH' that aren't already on '$FROM_BRANCH'"

info "Commits to cherry-pick (${#COMMITS[@]}):"
git log --oneline --no-merges --reverse "${REMOTE}/${FROM_BRANCH}..${STORY_REF}" | sed 's/^/    /'

# --- Create the new branch off the to-release branch -------------------------
NEW_BRANCH="${STORY_BRANCH}-${TO_BRANCH}"      # e.g. feature/US123456-fix-thing-release-43

if git rev-parse --verify --quiet "refs/heads/${NEW_BRANCH}" >/dev/null; then
  die "Local branch '$NEW_BRANCH' already exists. Delete it or handle it manually."
fi
if git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${NEW_BRANCH}" >/dev/null; then
  die "Branch '$NEW_BRANCH' already exists on '$REMOTE'."
fi

if $DRY_RUN; then
  info "[dry-run] Would create '$NEW_BRANCH' from '${REMOTE}/${TO_BRANCH}',"
  info "[dry-run] cherry-pick the ${#COMMITS[@]} commit(s) above, and push to '$REMOTE'."
  exit 0
fi

info "Creating '$NEW_BRANCH' from '${REMOTE}/${TO_BRANCH}'..."
git switch --create "$NEW_BRANCH" "${REMOTE}/${TO_BRANCH}"

# --- Cherry-pick -------------------------------------------------------------
for COMMIT in "${COMMITS[@]}"; do
  SUBJECT=$(git log -1 --format='%h %s' "$COMMIT")
  info "Cherry-picking: $SUBJECT"
  if ! git cherry-pick -x "$COMMIT"; then
    cat >&2 <<EOF

CONFLICT while cherry-picking $SUBJECT

You are on branch '$NEW_BRANCH'. Resolve the conflict, then:

    git add <resolved files>
    git cherry-pick --continue      # repeat for any further conflicts
    git push -u $REMOTE $NEW_BRANCH

To give up instead:

    git cherry-pick --abort
    git switch -
    git branch -D $NEW_BRANCH
EOF
    exit 1
  fi
done

# --- Push --------------------------------------------------------------------
info "Pushing '$NEW_BRANCH' to '$REMOTE'..."
git push -u "$REMOTE" "$NEW_BRANCH"

info "Done. '$STORY_BRANCH' has been carried from $FROM_BRANCH into $TO_BRANCH as '$NEW_BRANCH'."
