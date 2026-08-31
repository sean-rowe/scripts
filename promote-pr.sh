#!/usr/bin/env bash
#
# promote-pr.sh
#
# Cherry-pick the commits of one or more PRs into a release branch.
#
# PRs can be given explicitly (1 to many), or discovered from a Rally
# story/defect id via find-rally-prs.sh (every PR whose branch, title/body,
# or commits reference the id).
#
# Usage:
#   promote-pr.sh --to 45 --pr 123 [--pr 456 ...]
#   promote-pr.sh --to release-45 --pr 123,456
#   promote-pr.sh --to main --story US833008
#
# The target (--to) accepts "main", "release-45", a bare number (45 ->
# release-45), or any other branch name verbatim.
#
# What it does:
#   1. Resolves the PR list (explicit --pr, or find-rally-prs.sh <story>)
#      and shows it for confirmation
#   2. Orders the PRs by merge time (unmerged ones last), fetches each PR's
#      commits from GitHub (via refs/pull/N/head, so this works even after
#      squash-merges and deleted branches) and collects the hashes
#      oldest-first, deduped, skipping merge commits and commits already on
#      the target branch
#   3. Creates a branch off the target branch (named
#      <source-branch>-to-<target> for a single PR, promote/<story>-<target>
#      otherwise), cherry-picks the commits onto it, pushes, opens a PR into
#      the target — for a single PR the new PR reuses the original title
#      (release numbers rewritten) and body — and opens it in the browser
#
# If a cherry-pick hits a conflict the run stops with progress saved.
# Edit the conflicted files to resolve them, then:
#   promote-pr.sh --continue     # stages your fixes, finishes this commit, does the rest
#   promote-pr.sh --abort        # discard the interrupted run
#
# Options:
#   --pr <n[,n...]>       PR number(s) to promote (repeatable)
#   --story <id>          Rally story/defect id -> PRs via find-rally-prs.sh
#   --to <target>         Target branch: main | release-45 | 45 (required)
#   --to-release <n>      Alias for --to
#   --remote <name>       Git remote (default: origin)
#   --release-prefix <p>  Release branch prefix (default: release-)
#   --branch-name <name>  Override the generated work branch name
#   --no-pr               Skip opening the pull request
#   --no-open             Don't open the created PR in the browser
#   --keep-foreign-commits  With --story: keep commits whose message
#                         references a different Rally id (they are
#                         excluded by default, with a warning)
#   --yes                 Skip confirmation of the resolved PR list
#   --dry-run             Show what would happen without changing anything
#   --continue            Resume an interrupted run after fixing conflicts
#   --abort               Abandon an interrupted run and delete its branch
#
# Requires: gh (authenticated) and jq. GitHub repos only.

set -euo pipefail

PRS=()
STORY=""
TARGET_SPEC=""
REMOTE="origin"
RELEASE_PREFIX="release-"
BRANCH_OVERRIDE=""
CREATE_PR=true
OPEN_PR=true
KEEP_FOREIGN=false
ASSUME_YES=false
DRY_RUN=false
CONTINUE_RUN=false
ABORT_RUN=false
FORCE_PUSH=false

SCRIPT_NAME=$(basename "$0")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | tail -n +2
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)             IFS=',' read -ra _prs <<< "${2:-}"; PRS+=("${_prs[@]}"); shift 2 ;;
    --story)          STORY="${2:-}"; shift 2 ;;
    --to|--to-release) TARGET_SPEC="${2:-}"; shift 2 ;;
    --remote)         REMOTE="${2:-}"; shift 2 ;;
    --release-prefix) RELEASE_PREFIX="${2:-}"; shift 2 ;;
    --branch-name)    BRANCH_OVERRIDE="${2:-}"; shift 2 ;;
    --no-pr)          CREATE_PR=false; shift ;;
    --no-open)        OPEN_PR=false; shift ;;
    --keep-foreign-commits) KEEP_FOREIGN=true; shift ;;
    --yes)            ASSUME_YES=true; shift ;;
    --dry-run)        DRY_RUN=true; shift ;;
    --continue)       CONTINUE_RUN=true; shift ;;
    --abort)          ABORT_RUN=true; shift ;;
    -h|--help)        usage 0 ;;
    *)                die "Unknown argument: $1 (use --help)" ;;
  esac
done

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside a git repository"
STATE_FILE="$(git rev-parse --git-dir)/promote-pr.state"

command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install with: brew install gh"
command -v jq >/dev/null 2>&1 || die "jq not found. Install with: brew install jq"

# Progress is saved before each cherry-pick so --continue can resume. The
# saved NEXT_INDEX points past the commit being attempted, because
# 'git cherry-pick --continue' completes that one.
save_state() {
  cat > "$STATE_FILE" <<EOF
STORY='$STORY'
TO_BRANCH='$TO_BRANCH'
REMOTE='$REMOTE'
RELEASE_PREFIX='$RELEASE_PREFIX'
CREATE_PR=$CREATE_PR
OPEN_PR=$OPEN_PR
PR_LIST='${PRS[*]}'
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
      git switch -q --detach "${REMOTE}/${TO_BRANCH}"
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
  # shellcheck disable=SC2206
  PRS=($PR_LIST)
  START_INDEX=$NEXT_INDEX
  info "Resuming onto ${TO_BRANCH} (commit $NEXT_INDEX of ${#COMMITS[@]} was in progress)"
else
  [[ -n "$TARGET_SPEC" ]] || die "--to is required (main, release-45, or 45)"
  [[ ${#PRS[@]} -gt 0 || -n "$STORY" ]] || die "Give --pr <n> (repeatable) or --story <id>"
  if [[ -f "$STATE_FILE" ]] && ! $DRY_RUN; then
    die "An interrupted run exists. Re-run with --continue to resume it, or --abort to discard it."
  fi

  # Resolve the target: main | release-45 | bare number | any branch name
  if [[ "$TARGET_SPEC" =~ ^[0-9]+$ ]]; then
    TO_BRANCH="${RELEASE_PREFIX}${TARGET_SPEC}"
  else
    TO_BRANCH="$TARGET_SPEC"
  fi
fi

if ! $CONTINUE_RUN; then
  if [[ -n "$(git status --porcelain)" ]]; then
    die "Working tree is not clean. Commit or stash your changes first."
  fi

  # --- Resolve the PR list from the story if needed --------------------------
  if [[ ${#PRS[@]} -eq 0 ]]; then
    FINDER="$SCRIPT_DIR/find-rally-prs.sh"
    [[ -x "$FINDER" ]] || die "find-rally-prs.sh not found next to $SCRIPT_NAME"
    info "Finding PRs for $STORY..."
    FOUND=$("$FINDER" "$STORY" --numbers) || die "No PRs found for $STORY"
    while IFS= read -r n; do
      [[ -n "$n" ]] && PRS+=("$n")
    done <<< "$FOUND"
    [[ ${#PRS[@]} -gt 0 ]] || die "No PRs found for $STORY"
  fi

  for n in "${PRS[@]}"; do
    [[ "$n" =~ ^[0-9]+$ ]] || die "'$n' is not a PR number"
  done

  # --- Fetch PR details and order by merge time (unmerged last) --------------
  info "Fetching PR details..."
  PR_JSON=$(for n in "${PRS[@]}"; do
    gh pr view "$n" --json number,title,state,mergedAt,headRefName,commits \
      || die "Cannot read PR #$n (does it exist in this repo?)"
  done | jq -s 'unique_by(.number) | sort_by(.mergedAt // "9999-99-99", .number)')

  echo ""
  echo "PRs to promote into $TO_BRANCH (in this order):"
  echo "$PR_JSON" | jq -r '.[] | "  #\(.number) [\(.state)] \(.title)  (\(.commits | length) commit(s), branch: \(.headRefName))"'
  echo ""

  OPEN_COUNT=$(echo "$PR_JSON" | jq '[.[] | select(.state != "MERGED")] | length')
  if [[ "$OPEN_COUNT" -gt 0 ]]; then
    echo "WARNING: $OPEN_COUNT of these PR(s) are not merged; their commits may still change." >&2
  fi

  if ! $ASSUME_YES && ! $DRY_RUN; then
    if [[ -t 0 ]]; then
      printf 'Proceed with these %d PR(s)? [y/N] ' "${#PRS[@]}"
      read -r REPLY
      [[ "$REPLY" =~ ^[Yy] ]] || { echo "Cancelled."; exit 1; }
    else
      die "Not a terminal; re-run with --yes to skip confirmation."
    fi
  fi

  info "Fetching from $REMOTE..."
  git fetch --prune "$REMOTE"
  git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${TO_BRANCH}" >/dev/null \
    || die "Branch '$TO_BRANCH' does not exist on '$REMOTE'"

  # refs/pull/N/head keeps every PR's original commits available even after
  # a squash-merge or branch deletion.
  for n in "${PRS[@]}"; do
    git fetch -q "$REMOTE" "pull/$n/head" 2>/dev/null \
      || echo "WARNING: could not fetch pull/$n/head (relying on already-fetched objects)" >&2
  done

  # --- Collect commit hashes: per-PR oldest-first, deduped -------------------
  COMMITS=()
  SKIPPED_MERGES=0
  SKIPPED_PRESENT=0
  while IFS= read -r sha; do
    git cat-file -e "${sha}^{commit}" 2>/dev/null \
      || die "Commit $sha from the PRs is not available locally even after fetching"
    # skip merge commits
    if [[ -n "$(git rev-list --merges --no-walk "$sha")" ]]; then
      SKIPPED_MERGES=$((SKIPPED_MERGES + 1))
      continue
    fi
    # skip commits already reachable from the target release branch
    if git merge-base --is-ancestor "$sha" "${REMOTE}/${TO_BRANCH}"; then
      SKIPPED_PRESENT=$((SKIPPED_PRESENT + 1))
      continue
    fi
    COMMITS+=("$sha")
  done < <(echo "$PR_JSON" | jq -r '.[].commits[].oid' | awk '!seen[$0]++')

  [[ "$SKIPPED_MERGES" -gt 0 ]] && echo "WARNING: skipping $SKIPPED_MERGES merge commit(s)." >&2
  [[ "$SKIPPED_PRESENT" -gt 0 ]] && info "Skipping $SKIPPED_PRESENT commit(s) already on $TO_BRANCH."

  # --- Foreign-story guard (only when promoting by --story) ------------------
  # A story's PR can contain another story's commits (branch cut off another
  # feature branch). Exclude commits whose message references a different
  # Rally id and never this story's, unless --keep-foreign-commits.
  if [[ -n "$STORY" && ${#COMMITS[@]} -gt 0 ]]; then
    STORY_UP=$(echo "$STORY" | tr '[:lower:]' '[:upper:]')
    FOREIGN=()
    KEEP=()
    for h in "${COMMITS[@]}"; do
      IDS=$(git show -s --format=%B "$h" \
              | grep -ioE '(US|DE|TA|TS)[0-9]{4,}' | tr '[:lower:]' '[:upper:]' \
              | sort -u || true)
      if [[ -n "$IDS" ]] && ! grep -qx "$STORY_UP" <<< "$IDS"; then
        FOREIGN+=("$h")
      else
        KEEP+=("$h")
      fi
    done
    if [[ ${#FOREIGN[@]} -gt 0 ]]; then
      if $KEEP_FOREIGN; then
        echo "WARNING: keeping ${#FOREIGN[@]} commit(s) that reference a different story (--keep-foreign-commits):" >&2
        for h in "${FOREIGN[@]}"; do git show -s --format='    %h %s' "$h" >&2; done
      else
        echo "WARNING: excluding ${#FOREIGN[@]} commit(s) that reference a different story id:" >&2
        for h in "${FOREIGN[@]}"; do git show -s --format='    %h %s' "$h" >&2; done
        echo "         Re-run with --keep-foreign-commits if they really belong to $STORY." >&2
        COMMITS=("${KEEP[@]:+${KEEP[@]}}")
      fi
    fi
  fi

  [[ ${#COMMITS[@]} -gt 0 ]] || die "Nothing to promote: no cherry-pickable commits left."

  info "Commits to cherry-pick (${#COMMITS[@]}):"
  for h in "${COMMITS[@]}"; do
    git show -s --format='    %h %s' "$h"
  done

  # --- Work branch off the target ---------------------------------------------
  # Single PR keeps the promote-pr convention: <source-branch>-to-<target>.
  if [[ -n "$BRANCH_OVERRIDE" ]]; then
    NEW_BRANCH="$BRANCH_OVERRIDE"
  elif [[ ${#PRS[@]} -eq 1 && -z "$STORY" ]]; then
    SRC=$(echo "$PR_JSON" | jq -r '.[0].headRefName')
    NEW_BRANCH="${SRC}-to-${TO_BRANCH}"
  elif [[ -n "$STORY" ]]; then
    NEW_BRANCH="promote/${STORY}-${TO_BRANCH}"
  else
    JOINED=$(IFS=-; echo "${PRS[*]}")
    NEW_BRANCH="promote/pr-${JOINED:0:40}-${TO_BRANCH}"
  fi

  REUSE_LOCAL=false
  FORCE_PUSH=false
  git rev-parse --verify --quiet "refs/heads/${NEW_BRANCH}" >/dev/null && REUSE_LOCAL=true
  git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${NEW_BRANCH}" >/dev/null && FORCE_PUSH=true

  if $DRY_RUN; then
    if $REUSE_LOCAL; then
      info "[dry-run] Would reset existing branch '$NEW_BRANCH' to '${REMOTE}/${TO_BRANCH}',"
    else
      info "[dry-run] Would create '$NEW_BRANCH' from '${REMOTE}/${TO_BRANCH}',"
    fi
    info "[dry-run] cherry-pick the ${#COMMITS[@]} commit(s) above, push to '$REMOTE',"
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
    # Stage the user's resolutions automatically, refusing only if a file
    # still contains conflict markers (i.e. wasn't actually resolved).
    UNMERGED=()
    while IFS= read -r f; do
      UNMERGED+=("$f")
    done < <(git diff --name-only --diff-filter=U)
    if [[ ${#UNMERGED[@]} -gt 0 ]]; then
      STILL_CONFLICTED=()
      for f in "${UNMERGED[@]}"; do
        if [[ -f "$f" ]] && grep -qE '^(<{7}|>{7})( |$)' "$f"; then
          STILL_CONFLICTED+=("$f")
        fi
      done
      if [[ ${#STILL_CONFLICTED[@]} -gt 0 ]]; then
        echo "These files still contain conflict markers (<<<<<<< / >>>>>>>):" >&2
        printf '    %s\n' "${STILL_CONFLICTED[@]}" >&2
        die "Finish resolving them, then re-run --continue."
      fi
      info "Staging resolved files: ${UNMERGED[*]}"
      git add -A -- "${UNMERGED[@]}"
    fi
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

Progress has been saved. Edit the conflicted files to resolve them, then:

    $SCRIPT_NAME --continue     # stages your fixes, finishes this commit, does the rest

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

# --- Create the pull request -------------------------------------------------
PR_URL=""
if $CREATE_PR; then
  # Single PR: reuse the original title (release numbers rewritten to the
  # target) and body, like promote-pr. Multi/story: composed title.
  PR_TITLE=""
  ORIG_BODY=""
  if [[ ${#PRS[@]} -eq 1 ]]; then
    PR_TITLE=$(gh pr view "${PRS[0]}" --json title --jq '.title' 2>/dev/null) || PR_TITLE=""
    ORIG_BODY=$(gh pr view "${PRS[0]}" --json body --jq '.body' 2>/dev/null) || ORIG_BODY=""
    if [[ -n "$PR_TITLE" && "$TO_BRANCH" =~ ^${RELEASE_PREFIX}[0-9]+$ ]]; then
      PR_TITLE=$(echo "$PR_TITLE" | sed -E "s/${RELEASE_PREFIX}[0-9]+/${TO_BRANCH}/g")
    fi
  fi
  if [[ -z "$PR_TITLE" ]]; then
    if [[ -n "$STORY" ]]; then
      PR_TITLE="$STORY: promote to $TO_BRANCH"
    else
      PR_TITLE="Promote PR(s) ${PRS[*]} to $TO_BRANCH"
    fi
  fi
  PR_BODY="${ORIG_BODY}${ORIG_BODY:+

}---
Cherry-picked $TOTAL commit(s) from PR(s) ${PRS[*]} onto ${TO_BRANCH} by $SCRIPT_NAME."
  info "Creating pull request via gh..."
  PR_URL=$(gh pr create --base "$TO_BRANCH" --head "$NEW_BRANCH" \
             --title "$PR_TITLE" --body "$PR_BODY" 2>&1 \
           | grep -Eo 'https?://[^[:space:]]+' | tail -1) || PR_URL=""
  [[ -n "$PR_URL" ]] || echo "WARNING: 'gh pr create' failed; create the PR manually." >&2
  if [[ -n "$PR_URL" ]] && $OPEN_PR && command -v open >/dev/null 2>&1; then
    info "Opening PR in browser..."
    open "$PR_URL" 2>/dev/null || true
  fi
fi

rm -f "$STATE_FILE"

echo
echo "=============================================================="
echo " SUCCESS"
echo "=============================================================="
echo " Promoted:      ${#PRS[@]} PR(s): ${PRS[*]}"
echo " Cherry-picked: $TOTAL commit(s) onto $TO_BRANCH"
echo " New branch:    $NEW_BRANCH  (pushed to $REMOTE)"
if [[ -n "$PR_URL" ]]; then
  echo " Pull request:  $PR_URL"
else
  echo " Create a PR from '$NEW_BRANCH' into '$TO_BRANCH' on GitHub."
fi
echo "=============================================================="
