#!/usr/bin/env bash
#
# cherry-pick-story.sh
#
# Cherry-pick a user story's commits from one release into another.
#
# Finds the story's commits, creates a new branch off the to-release branch,
# cherry-picks them onto it, pushes the result, and opens a PR.
#
# The story's commits are resolved in this order:
#   1. --branch, if given: everything on that branch that isn't on from-release
#   2. the most recent PR into the from-release branch whose source branch
#      name contains the story id (GitHub/Bitbucket) — the PR supplies both
#      the branch name and the exact commit hashes, so this works even after
#      the story branch was deleted on merge
#   3. remote branch names containing the story id (case-insensitive)
#
# Usage:
#   cherry-pick-story.sh --story US123456 --from-release 42 --to-release 43
#
# If a cherry-pick hits a conflict, the script stops and saves its progress.
# Resolve the conflict, 'git add' the resolved files, then run:
#   cherry-pick-story.sh --continue
# It finishes the interrupted commit, applies the rest, pushes, and opens
# the PR. To discard the run instead:
#   cherry-pick-story.sh --abort
#
# Options:
#   --story <id>            Story id embedded in the branch name (required)
#   --branch <name>         Use this exact branch instead of resolving it
#   --from-release <n>      Release the story branch was based on (required)
#   --to-release <n>        Release to carry the story into (required)
#   --remote <name>         Git remote (default: origin)
#   --release-prefix <p>    Release branch prefix (default: release-)
#   --no-pr                 Skip automatic pull request creation
#   --dry-run               Show what would happen without changing anything
#   --continue              Resume an interrupted run after fixing conflicts
#   --abort                 Abandon an interrupted run and delete its branch
#
# After a successful push the script opens a pull request into the to-release
# branch automatically:
#   - GitHub remotes:    uses the 'gh' CLI (must be installed and logged in)
#   - Bitbucket Cloud:   uses the REST API; export BITBUCKET_TOKEN, or
#                        BITBUCKET_USER and BITBUCKET_APP_PASSWORD
# The same credentials are used to resolve the story branch from its PR.
# If neither is possible it prints the link to create the PR manually.

set -euo pipefail

STORY=""
BRANCH_OVERRIDE=""
FROM_RELEASE=""
TO_RELEASE=""
REMOTE="origin"
RELEASE_PREFIX="release-"
CREATE_PR=true
DRY_RUN=false
CONTINUE_RUN=false
ABORT_RUN=false
FORCE_PUSH=false

SCRIPT_NAME=$(basename "$0")

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | tail -n +2
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --story)          STORY="${2:-}"; shift 2 ;;
    --branch)         BRANCH_OVERRIDE="${2:-}"; shift 2 ;;
    --from-release)   FROM_RELEASE="${2:-}"; shift 2 ;;
    --to-release)     TO_RELEASE="${2:-}"; shift 2 ;;
    --remote)         REMOTE="${2:-}"; shift 2 ;;
    --release-prefix) RELEASE_PREFIX="${2:-}"; shift 2 ;;
    --no-pr)          CREATE_PR=false; shift ;;
    --dry-run)        DRY_RUN=true; shift ;;
    --continue)       CONTINUE_RUN=true; shift ;;
    --abort)          ABORT_RUN=true; shift ;;
    -h|--help)        usage 0 ;;
    *)                die "Unknown argument: $1 (use --help)" ;;
  esac
done

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside a git repository"
STATE_FILE="$(git rev-parse --git-dir)/cherry-pick-story.state"

# Progress is saved before each cherry-pick so --continue can resume. The
# saved NEXT_INDEX points past the commit being attempted, because
# 'git cherry-pick --continue' completes that one.
save_state() {
  cat > "$STATE_FILE" <<EOF
STORY='$STORY'
FROM_RELEASE='$FROM_RELEASE'
TO_RELEASE='$TO_RELEASE'
REMOTE='$REMOTE'
RELEASE_PREFIX='$RELEASE_PREFIX'
CREATE_PR=$CREATE_PR
STORY_BRANCH='$STORY_BRANCH'
NEW_BRANCH='$NEW_BRANCH'
FORCE_PUSH=$FORCE_PUSH
NEXT_INDEX=$1
COMMITS_STR='${COMMITS[*]}'
EOF
}

# --- --abort: discard an interrupted run -------------------------------------
if $ABORT_RUN; then
  [[ -f "$STATE_FILE" ]] || die "No interrupted $SCRIPT_NAME run to abort"
  # shellcheck disable=SC1090
  . "$STATE_FILE"
  git cherry-pick --abort >/dev/null 2>&1 || true
  if [[ "$(git rev-parse --abbrev-ref HEAD)" == "$NEW_BRANCH" ]]; then
    if git rev-parse --verify --quiet refs/heads/main >/dev/null; then
      git switch -q main
    elif git rev-parse --verify --quiet refs/heads/master >/dev/null; then
      git switch -q master
    else
      git switch -q --detach "${REMOTE}/${RELEASE_PREFIX}${TO_RELEASE}"
    fi
  fi
  git branch -D "$NEW_BRANCH" >/dev/null 2>&1 || true
  rm -f "$STATE_FILE"
  info "Aborted. Branch '$NEW_BRANCH' deleted; nothing was pushed."
  exit 0
fi

# --- --continue: load saved progress -----------------------------------------
START_INDEX=0
if $CONTINUE_RUN; then
  [[ -f "$STATE_FILE" ]] \
    || die "No interrupted $SCRIPT_NAME run found (nothing to --continue)"
  # shellcheck disable=SC1090
  . "$STATE_FILE"
  # shellcheck disable=SC2206
  COMMITS=($COMMITS_STR)
  START_INDEX=$NEXT_INDEX
  info "Resuming: $STORY onto ${RELEASE_PREFIX}${TO_RELEASE} (commit $NEXT_INDEX of ${#COMMITS[@]} was in progress)"
else
  [[ -n "$STORY" ]]        || die "--story is required"
  [[ -n "$FROM_RELEASE" ]] || die "--from-release is required"
  [[ -n "$TO_RELEASE" ]]   || die "--to-release is required"
  [[ "$FROM_RELEASE" != "$TO_RELEASE" ]] || die "--from-release and --to-release are the same"
  if [[ -f "$STATE_FILE" ]] && ! $DRY_RUN; then
    die "An interrupted run exists. Re-run with --continue to resume it, or --abort to discard it."
  fi
fi

FROM_BRANCH="${RELEASE_PREFIX}${FROM_RELEASE}"
TO_BRANCH="${RELEASE_PREFIX}${TO_RELEASE}"

# --- Detect the git host -----------------------------------------------------
REMOTE_URL=$(git remote get-url "$REMOTE")
HOST=""
WORKSPACE_REPO=""
BB_AUTH=()
case "$REMOTE_URL" in
  *github.com*)
    HOST="github"
    ;;
  *bitbucket.org*)
    HOST="bitbucket"
    WORKSPACE_REPO=$(echo "$REMOTE_URL" \
      | sed -E 's#^(git@bitbucket\.org:|https://([^@/]+@)?bitbucket\.org/)##; s#\.git$##; s#/*$##')
    if [[ -n "${BITBUCKET_TOKEN:-}" ]]; then
      BB_AUTH=(-H "Authorization: Bearer $BITBUCKET_TOKEN")
    elif [[ -n "${BITBUCKET_USER:-}" && -n "${BITBUCKET_APP_PASSWORD:-}" ]]; then
      BB_AUTH=(-u "$BITBUCKET_USER:$BITBUCKET_APP_PASSWORD")
    fi
    ;;
esac

# Print "<pr-id><TAB><source-branch>" for the most recent PR into FROM_BRANCH
# whose source branch name contains the story id; prints nothing if it can't
# be determined.
find_story_pr() {
  case "$HOST" in
    github)
      command -v gh >/dev/null 2>&1 || return 0
      gh pr list --base "$FROM_BRANCH" --state all --limit 100 \
          --json number,headRefName \
          --jq '.[] | "\(.number)\t\(.headRefName)"' 2>/dev/null \
        | grep -iF -- "$STORY" | head -1 || true
      ;;
    bitbucket)
      [[ ${#BB_AUTH[@]} -gt 0 ]] || return 0
      curl -sf "${BB_AUTH[@]}" \
          "https://api.bitbucket.org/2.0/repositories/${WORKSPACE_REPO}/pullrequests?state=MERGED&state=OPEN&state=DECLINED&sort=-updated_on&pagelen=50&q=destination.branch.name+%3D+%22${FROM_BRANCH}%22" \
          2>/dev/null \
        | python3 -c 'import sys, json
for pr in json.load(sys.stdin).get("values", []):
    print(str(pr["id"]) + "\t" + pr["source"]["branch"]["name"])' 2>/dev/null \
        | grep -iF -- "$STORY" | head -1 || true
      ;;
  esac
}

# Print the PR's commit hashes, oldest first; prints nothing on failure.
pr_commit_hashes() {
  local pr_id="$1"
  case "$HOST" in
    github)
      gh pr view "$pr_id" --json commits --jq '.commits[].oid' 2>/dev/null || true
      ;;
    bitbucket)
      # Bitbucket returns newest first; reverse to oldest first.
      curl -sf "${BB_AUTH[@]}" \
          "https://api.bitbucket.org/2.0/repositories/${WORKSPACE_REPO}/pullrequests/${pr_id}/commits?pagelen=100" \
          2>/dev/null \
        | python3 -c 'import sys, json
for c in reversed(json.load(sys.stdin).get("values", [])):
    print(c["hash"])' 2>/dev/null || true
      ;;
  esac
}

if ! $CONTINUE_RUN; then
  if [[ -n "$(git status --porcelain)" ]]; then
    die "Working tree is not clean. Commit or stash your changes first."
  fi

  info "Fetching from $REMOTE..."
  git fetch --prune "$REMOTE"

  # --- Verify release branches exist on the remote ---------------------------
  git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${FROM_BRANCH}" >/dev/null \
    || die "Branch '$FROM_BRANCH' does not exist on '$REMOTE'"
  git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${TO_BRANCH}" >/dev/null \
    || die "Branch '$TO_BRANCH' does not exist on '$REMOTE'"

  # --- Resolve the story branch and its commits ------------------------------
  # COMMITS is filled either from the story's PR (preferred: the PR records
  # the exact hashes) or from git history relative to the from-release branch.
  COMMITS=()
  STORY_BRANCH=""
  STORY_REF=""      # set only when resolving from a branch ref

  MATCHES=()
  if [[ -n "$BRANCH_OVERRIDE" ]]; then
    BRANCH_OVERRIDE="${BRANCH_OVERRIDE#"${REMOTE}"/}"
    git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${BRANCH_OVERRIDE}" >/dev/null \
      || die "Branch '$BRANCH_OVERRIDE' does not exist on '$REMOTE'"
    MATCHES=("${REMOTE}/${BRANCH_OVERRIDE}")
  fi

  if [[ ${#MATCHES[@]} -eq 0 ]]; then
    PR_MATCH=$(find_story_pr)
    if [[ -n "$PR_MATCH" ]]; then
      PR_ID="${PR_MATCH%%$'\t'*}"
      PR_BRANCH="${PR_MATCH#*$'\t'}"
      info "Found PR #$PR_ID into $FROM_BRANCH (source branch: $PR_BRANCH)"
      while IFS= read -r line; do
        COMMITS+=("$line")
      done < <(pr_commit_hashes "$PR_ID")

      # Every hash must exist locally (it will, if it's reachable from any
      # fetched branch — merged PRs are reachable from the from-release branch).
      for h in "${COMMITS[@]:+${COMMITS[@]}}"; do
        if ! git cat-file -e "${h}^{commit}" 2>/dev/null; then
          echo "WARNING: PR commit $h not found locally; falling back to branch history." >&2
          COMMITS=()
          break
        fi
      done

      if [[ ${#COMMITS[@]} -gt 0 ]]; then
        STORY_BRANCH="$PR_BRANCH"
      else
        MATCHES=()
        if git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${PR_BRANCH}" >/dev/null; then
          MATCHES=("${REMOTE}/${PR_BRANCH}")
        else
          echo "WARNING: could not read commits from PR #$PR_ID and branch '$PR_BRANCH'" >&2
          echo "         no longer exists on '$REMOTE'; falling back to branch-name search." >&2
        fi
      fi
    fi
  fi

  if [[ ${#COMMITS[@]} -eq 0 && ${#MATCHES[@]} -eq 0 ]]; then
    while IFS= read -r line; do
      MATCHES+=("$line")
    done < <(git branch -r --list "${REMOTE}/*" --format='%(refname:short)' \
             | grep -iF -- "$STORY" || true)

    # Branches this script generated earlier end in "-<release-prefix><n>" and
    # also contain the story number. When several branches match, prefer the
    # one made for the from-release (chaining, e.g. 42->43 then 43->44);
    # otherwise ignore generated branches and keep the original(s).
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
      echo "Multiple branches match story '$STORY':"
      for i in "${!MATCHES[@]}"; do
        printf '  %2d) %s\n' "$((i + 1))" "${MATCHES[$i]}"
      done
      if [[ -t 0 ]]; then
        CHOICE=""
        while :; do
          printf 'Pick the branch to cherry-pick from [1-%d]: ' "${#MATCHES[@]}"
          read -r CHOICE
          [[ "$CHOICE" =~ ^[0-9]+$ ]] && (( CHOICE >= 1 && CHOICE <= ${#MATCHES[@]} )) && break
          echo "Invalid choice."
        done
        MATCHES=("${MATCHES[$((CHOICE - 1))]}")
      else
        die "Ambiguous story branch. Re-run with --branch <name> to choose one."
      fi
    fi
  fi

  # --- Work out which commits to cherry-pick ---------------------------------
  if [[ ${#COMMITS[@]} -eq 0 ]]; then
    # Branch-based: everything the story branch has on top of the from-release.
    STORY_REF="${MATCHES[0]}"                  # e.g. origin/feature/US123456-fix-thing
    STORY_BRANCH="${STORY_REF#"${REMOTE}"/}"   # e.g. feature/US123456-fix-thing
    info "Found story branch: $STORY_BRANCH"

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
  else
    # PR-based: the PR listed the hashes; drop merge commits (not cherry-pickable).
    NONMERGE=()
    for h in "${COMMITS[@]}"; do
      if [[ -z "$(git rev-list --merges --no-walk "$h")" ]]; then
        NONMERGE+=("$h")
      else
        echo "WARNING: skipping merge commit $h from the PR." >&2
      fi
    done
    COMMITS=("${NONMERGE[@]:+${NONMERGE[@]}}")
    [[ ${#COMMITS[@]} -gt 0 ]] || die "PR #$PR_ID contains no cherry-pickable commits"
  fi

  info "Commits to cherry-pick (${#COMMITS[@]}):"
  for h in "${COMMITS[@]}"; do
    git show -s --format='    %h %s' "$h"
  done

  # --- Create the new branch off the to-release branch -----------------------
  # e.g. feature/US123456-fix-thing -> feature/US123456-fix-thing-release-43
  # (a "-release-42" suffix from a previous run is replaced, not stacked)
  BASE_NAME=$(echo "$STORY_BRANCH" | sed -E "s/-${RELEASE_PREFIX}[0-9]+\$//")
  NEW_BRANCH="${BASE_NAME}-${TO_BRANCH}"

  # A leftover branch from an earlier run is reset and reused, not an error.
  REUSE_LOCAL=false
  FORCE_PUSH=false
  if git rev-parse --verify --quiet "refs/heads/${NEW_BRANCH}" >/dev/null; then
    REUSE_LOCAL=true
  fi
  if git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${NEW_BRANCH}" >/dev/null; then
    FORCE_PUSH=true
  fi

  if $DRY_RUN; then
    if $REUSE_LOCAL; then
      info "[dry-run] Would reset existing branch '$NEW_BRANCH' to '${REMOTE}/${TO_BRANCH}',"
    else
      info "[dry-run] Would create '$NEW_BRANCH' from '${REMOTE}/${TO_BRANCH}',"
    fi
    if $FORCE_PUSH; then
      info "[dry-run] cherry-pick the ${#COMMITS[@]} commit(s) above, force-push to '$REMOTE',"
    else
      info "[dry-run] cherry-pick the ${#COMMITS[@]} commit(s) above, push to '$REMOTE',"
    fi
    if $CREATE_PR; then
      info "[dry-run] and open a PR from '$NEW_BRANCH' into '$TO_BRANCH'."
    else
      info "[dry-run] and skip PR creation (--no-pr)."
    fi
    exit 0
  fi

  if $REUSE_LOCAL; then
    info "Branch '$NEW_BRANCH' already exists; resetting it to '${REMOTE}/${TO_BRANCH}'..."
    git switch -q "$NEW_BRANCH"
    git reset --hard -q "${REMOTE}/${TO_BRANCH}"
  else
    if $FORCE_PUSH; then
      info "Branch '$NEW_BRANCH' exists on '$REMOTE'; recreating it from '${REMOTE}/${TO_BRANCH}'..."
    else
      info "Creating '$NEW_BRANCH' from '${REMOTE}/${TO_BRANCH}'..."
    fi
    git switch --create "$NEW_BRANCH" "${REMOTE}/${TO_BRANCH}"
  fi
else
  # --- Resume: finish whatever was interrupted -------------------------------
  if [[ "$(git rev-parse --abbrev-ref HEAD)" != "$NEW_BRANCH" ]]; then
    git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null \
      && die "A cherry-pick is in progress on a different branch; resolve or abort it first."
    git switch "$NEW_BRANCH"
  fi

  if git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null; then
    [[ -z "$(git diff --name-only --diff-filter=U)" ]] \
      || die "Unresolved conflicts remain. Fix them, 'git add' the files, then re-run --continue."
    info "Finishing the interrupted cherry-pick..."
    if ! GIT_EDITOR=true git cherry-pick --continue >/dev/null 2>&1; then
      if [[ -z "$(git diff --cached --name-only)" ]]; then
        info "Resolution left nothing to apply; skipping that commit."
        git cherry-pick --skip >/dev/null 2>&1 || git cherry-pick --quit >/dev/null 2>&1 || true
      else
        die "'git cherry-pick --continue' failed. Resolve the problem and re-run --continue."
      fi
    fi
  fi
fi

# --- Cherry-pick -------------------------------------------------------------
TOTAL=${#COMMITS[@]}
i=$START_INDEX
while (( i < TOTAL )); do
  COMMIT="${COMMITS[$i]}"
  save_state "$((i + 1))"
  SUBJECT=$(git show -s --format='%h %s' "$COMMIT")
  info "Cherry-picking ($((i + 1))/$TOTAL): $SUBJECT"
  if ! PICK_OUT=$(git cherry-pick -x "$COMMIT" 2>&1); then
    if [[ -z "$(git diff --name-only --diff-filter=U)" && -z "$(git diff --cached --name-only)" ]]; then
      info "Nothing to apply (already on $TO_BRANCH); skipping."
      git cherry-pick --skip >/dev/null 2>&1 || git cherry-pick --quit >/dev/null 2>&1 || true
    else
      echo "$PICK_OUT" >&2
      cat >&2 <<EOF

CONFLICT while cherry-picking $SUBJECT  (commit $((i + 1)) of $TOTAL)

Progress has been saved. Resolve the conflict, then:

    git add <resolved files>
    $SCRIPT_NAME --continue     # finishes this commit and does the rest

To give up instead:

    $SCRIPT_NAME --abort
EOF
      exit 1
    fi
  fi
  i=$((i + 1))
done

# --- Push --------------------------------------------------------------------
PUSH_ARGS=(-u)
if $FORCE_PUSH; then
  PUSH_ARGS+=(--force-with-lease)
  info "Force-pushing '$NEW_BRANCH' to '$REMOTE' (branch already existed there)..."
else
  info "Pushing '$NEW_BRANCH' to '$REMOTE'..."
fi
PUSH_OUTPUT=$(git push "${PUSH_ARGS[@]}" "$REMOTE" "$NEW_BRANCH" 2>&1) \
  || { echo "$PUSH_OUTPUT" >&2; die "Push failed. Fix the problem and re-run with --continue."; }

# GitHub/Bitbucket/GitLab print a create-pull-request URL in the push response.
PR_CREATE_HINT=$(echo "$PUSH_OUTPUT" | grep -Eo 'https?://[^[:space:]]+' | grep -Ei 'pull|merge|compare' | head -1 || true)

# --- Create the pull request -------------------------------------------------
PR_URL=""
if $CREATE_PR; then
  PR_TITLE="$STORY: cherry-pick $FROM_BRANCH -> $TO_BRANCH"
  PR_BODY="Automated cherry-pick of $TOTAL commit(s) from '$STORY_BRANCH' (based on $FROM_BRANCH) onto '$TO_BRANCH'."

  case "$HOST" in
    github)
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
    bitbucket)
      if [[ ${#BB_AUTH[@]} -gt 0 ]]; then
        info "Creating pull request via Bitbucket API..."
        RESPONSE=$(curl -sf -X POST "${BB_AUTH[@]}" -H "Content-Type: application/json" \
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
        if [[ -z "$PR_URL" ]]; then
          # Creation fails if a PR is already open for this branch; report it.
          ENC_BRANCH=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$NEW_BRANCH")
          PR_URL=$(curl -sf "${BB_AUTH[@]}" \
            "https://api.bitbucket.org/2.0/repositories/${WORKSPACE_REPO}/pullrequests?state=OPEN&q=source.branch.name+%3D+%22${ENC_BRANCH}%22" \
            2>/dev/null \
            | python3 -c 'import sys, json
v = json.load(sys.stdin).get("values", [])
print(v[0]["links"]["html"]["href"] if v else "")' 2>/dev/null) || PR_URL=""
        fi
        [[ -n "$PR_URL" ]] || echo "WARNING: Bitbucket PR creation failed; create the PR manually." >&2
      else
        echo "WARNING: export BITBUCKET_TOKEN (or BITBUCKET_USER + BITBUCKET_APP_PASSWORD)" >&2
        echo "         to create the PR automatically; skipping." >&2
      fi
      ;;
    *)
      echo "WARNING: unrecognized git host for '$REMOTE_URL'; skipping automatic PR creation." >&2
      ;;
  esac
fi

rm -f "$STATE_FILE"

echo
echo "=============================================================="
echo " SUCCESS"
echo "=============================================================="
echo " Story branch:  $STORY_BRANCH"
echo " Cherry-picked: $TOTAL commit(s)  ($FROM_BRANCH -> $TO_BRANCH)"
echo " New branch:    $NEW_BRANCH  (pushed to $REMOTE)"
if [[ -n "$PR_URL" ]]; then
  echo " Pull request:  $PR_URL"
elif [[ -n "$PR_CREATE_HINT" ]]; then
  echo " Create a PR:   $PR_CREATE_HINT"
else
  echo " Create a PR from '$NEW_BRANCH' into '$TO_BRANCH' on your git host."
fi
echo "=============================================================="
