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
#   --no-pr                 Skip automatic pull request creation
#   --dry-run               Show what would happen without changing anything
#
# After a successful push the script opens a pull request into the to-release
# branch automatically:
#   - GitHub remotes:    uses the 'gh' CLI (must be installed and logged in)
#   - Bitbucket Cloud:   uses the REST API; export BITBUCKET_TOKEN, or
#                        BITBUCKET_USER and BITBUCKET_APP_PASSWORD
# If neither is possible it prints the link to create the PR manually.
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
CREATE_PR=true
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
    --no-pr)          CREATE_PR=false; shift ;;
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
done < <(git branch -r --list "${REMOTE}/*" --format='%(refname:short)' \
         | grep -iF -- "$STORY" || true)

# Branches this script generated earlier end in "-<release-prefix><n>" and also
# contain the story number. When several branches match, prefer the one made
# for the from-release (chaining, e.g. 42->43 then 43->44); otherwise ignore
# generated branches and keep the original(s).
if [[ ${#MATCHES[@]} -gt 1 ]]; then
  FILTERED=()
  while IFS= read -r line; do
    FILTERED+=("$line")
  done < <(printf '%s\n' "${MATCHES[@]}" | grep -E -- "-${RELEASE_PREFIX}${FROM_RELEASE}\$" || true)
  if [[ ${#FILTERED[@]} -eq 0 ]]; then
    while IFS= read -r line; do
      FILTERED+=("$line")
    done < <(printf '%s\n' "${MATCHES[@]}" | grep -Ev -- "-${RELEASE_PREFIX}[0-9]+\$" || true)
  fi
  if [[ ${#FILTERED[@]} -gt 0 ]]; then
    MATCHES=("${FILTERED[@]}")
  fi
fi

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
# e.g. feature/US123456-fix-thing -> feature/US123456-fix-thing-release-43
# (a "-release-42" suffix from a previous run is replaced, not stacked)
BASE_NAME=$(echo "$STORY_BRANCH" | sed -E "s/-${RELEASE_PREFIX}[0-9]+\$//")
NEW_BRANCH="${BASE_NAME}-${TO_BRANCH}"

if git rev-parse --verify --quiet "refs/heads/${NEW_BRANCH}" >/dev/null; then
  die "Local branch '$NEW_BRANCH' already exists. Delete it or handle it manually."
fi
if git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${NEW_BRANCH}" >/dev/null; then
  die "Branch '$NEW_BRANCH' already exists on '$REMOTE'."
fi

if $DRY_RUN; then
  info "[dry-run] Would create '$NEW_BRANCH' from '${REMOTE}/${TO_BRANCH}',"
  info "[dry-run] cherry-pick the ${#COMMITS[@]} commit(s) above, push to '$REMOTE',"
  if $CREATE_PR; then
    info "[dry-run] and open a PR from '$NEW_BRANCH' into '$TO_BRANCH'."
  else
    info "[dry-run] and skip PR creation (--no-pr)."
  fi
  exit 0
fi

info "Creating '$NEW_BRANCH' from '${REMOTE}/${TO_BRANCH}'..."
git switch --create "$NEW_BRANCH" "${REMOTE}/${TO_BRANCH}"

# --- Cherry-pick -------------------------------------------------------------
for COMMIT in "${COMMITS[@]}"; do
  SUBJECT=$(git log -1 --format='%h %s' "$COMMIT")
  info "Cherry-picking: $SUBJECT"
  if ! git cherry-pick -x "$COMMIT" >/dev/null; then
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
PUSH_OUTPUT=$(git push -u "$REMOTE" "$NEW_BRANCH" 2>&1) \
  || { echo "$PUSH_OUTPUT" >&2; die "Push failed"; }

# GitHub/Bitbucket/GitLab print a create-pull-request URL in the push response.
PR_CREATE_HINT=$(echo "$PUSH_OUTPUT" | grep -Eo 'https?://[^[:space:]]+' | grep -Ei 'pull|merge|compare' | head -1 || true)

# --- Create the pull request -------------------------------------------------
PR_URL=""
if $CREATE_PR; then
  PR_TITLE="$STORY: cherry-pick $FROM_BRANCH -> $TO_BRANCH"
  PR_BODY="Automated cherry-pick of ${#COMMITS[@]} commit(s) from '$STORY_BRANCH' (based on $FROM_BRANCH) onto '$TO_BRANCH'."
  REMOTE_URL=$(git remote get-url "$REMOTE")

  case "$REMOTE_URL" in
    *github.com*)
      if command -v gh >/dev/null 2>&1; then
        info "Creating pull request via gh..."
        PR_URL=$(gh pr create --base "$TO_BRANCH" --head "$NEW_BRANCH" \
                   --title "$PR_TITLE" --body "$PR_BODY" 2>&1 \
                 | grep -Eo 'https?://[^[:space:]]+' | tail -1) || PR_URL=""
        [[ -n "$PR_URL" ]] || echo "WARNING: 'gh pr create' failed; create the PR manually." >&2
      else
        echo "WARNING: 'gh' CLI not found; skipping automatic PR creation." >&2
      fi
      ;;
    *bitbucket.org*)
      WORKSPACE_REPO=$(echo "$REMOTE_URL" \
        | sed -E 's#^(git@bitbucket\.org:|https://([^@/]+@)?bitbucket\.org/)##; s#\.git$##; s#/*$##')
      AUTH=()
      if [[ -n "${BITBUCKET_TOKEN:-}" ]]; then
        AUTH=(-H "Authorization: Bearer $BITBUCKET_TOKEN")
      elif [[ -n "${BITBUCKET_USER:-}" && -n "${BITBUCKET_APP_PASSWORD:-}" ]]; then
        AUTH=(-u "$BITBUCKET_USER:$BITBUCKET_APP_PASSWORD")
      else
        echo "WARNING: export BITBUCKET_TOKEN (or BITBUCKET_USER + BITBUCKET_APP_PASSWORD)" >&2
        echo "         to create the PR automatically; skipping." >&2
      fi
      if [[ ${#AUTH[@]} -gt 0 ]]; then
        info "Creating pull request via Bitbucket API..."
        RESPONSE=$(curl -sf -X POST "${AUTH[@]}" -H "Content-Type: application/json" \
          "https://api.bitbucket.org/2.0/repositories/${WORKSPACE_REPO}/pullrequests" \
          -d "{\"title\": \"$PR_TITLE\", \"description\": \"$PR_BODY\",
               \"source\": {\"branch\": {\"name\": \"$NEW_BRANCH\"}},
               \"destination\": {\"branch\": {\"name\": \"$TO_BRANCH\"}},
               \"close_source_branch\": true}") || RESPONSE=""
        if [[ -n "$RESPONSE" ]]; then
          PR_URL=$(printf '%s' "$RESPONSE" | python3 -c \
            'import sys, json; print(json.load(sys.stdin)["links"]["html"]["href"])' \
            2>/dev/null) || PR_URL=""
        fi
        [[ -n "$PR_URL" ]] || echo "WARNING: Bitbucket PR creation failed; create the PR manually." >&2
      fi
      ;;
    *)
      echo "WARNING: unrecognized git host for '$REMOTE_URL'; skipping automatic PR creation." >&2
      ;;
  esac
fi

echo
echo "=============================================================="
echo " SUCCESS"
echo "=============================================================="
echo " Story branch:  $STORY_BRANCH"
echo " Cherry-picked: ${#COMMITS[@]} commit(s)  ($FROM_BRANCH -> $TO_BRANCH)"
echo " New branch:    $NEW_BRANCH  (pushed to $REMOTE)"
if [[ -n "$PR_URL" ]]; then
  echo " Pull request:  $PR_URL"
elif [[ -n "$PR_CREATE_HINT" ]]; then
  echo " Create a PR:   $PR_CREATE_HINT"
else
  echo " Create a PR from '$NEW_BRANCH' into '$TO_BRANCH' on your git host."
fi
echo "=============================================================="
