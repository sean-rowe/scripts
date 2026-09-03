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
#   promote-pr.sh --to qa --from-release 45
#
# The target (--to) accepts "main", "release-45", a bare number (45 ->
# release-45), or any other branch name verbatim.
#
# With --from-release, no PRs are involved: the whole release branch is
# MERGED into the target instead (e.g. promote release-45 to qa). A work
# branch is created off the target, the release branch is merged into it,
# and a PR into the target is opened. --from-release accepts the same
# forms as --to and cannot be combined with --pr/--story.
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
#   --to <target>         Target branch: main | release-45 | 45 | qa (required)
#   --to-release <n>      Alias for --to
#   --from-release <n>    Merge this whole release branch into the target
#                         (e.g. --to qa --from-release 45); excludes --pr/--story
#   --remote <name>       Git remote (default: origin)
#   --release-prefix <p>  Release branch prefix (default: release-)
#   --branch-name <name>  Override the generated work branch name
#   --no-pr               Skip opening the pull request
#   --no-open             Don't open the created PR in the browser
#   --keep-foreign-commits  Keep commits whose message references a Rally
#                         id but never the expected one (the --story id,
#                         or the id in the commit's own PR branch name).
#                         Such commits are excluded by default, loudly.
#   --yes                 Skip confirmation of the resolved PR list
#   --dry-run             Show what would happen without changing anything
#   --continue            Resume an interrupted run after fixing conflicts
#   --no-verify           Skip commit hooks when finishing a conflicted
#                         commit (use with --continue when a hook fails it)
#   --abort               Abandon an interrupted run and delete its branch
#
# Requires: gh (authenticated) and jq. GitHub repos only.

set -euo pipefail

PRS=()
STORY=""
TARGET_SPEC=""
FROM_SPEC=""
FROM_BRANCH=""
MODE="pick"
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
NO_VERIFY=false

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
    --from-release)   FROM_SPEC="${2:-}"; shift 2 ;;
    --remote)         REMOTE="${2:-}"; shift 2 ;;
    --release-prefix) RELEASE_PREFIX="${2:-}"; shift 2 ;;
    --branch-name)    BRANCH_OVERRIDE="${2:-}"; shift 2 ;;
    --no-pr)          CREATE_PR=false; shift ;;
    --no-open)        OPEN_PR=false; shift ;;
    --keep-foreign-commits) KEEP_FOREIGN=true; shift ;;
    --yes)            ASSUME_YES=true; shift ;;
    --dry-run)        DRY_RUN=true; shift ;;
    --continue)       CONTINUE_RUN=true; shift ;;
    --no-verify)      NO_VERIFY=true; shift ;;
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
MODE='$MODE'
TO_BRANCH='$TO_BRANCH'
FROM_BRANCH='$FROM_BRANCH'
REMOTE='$REMOTE'
RELEASE_PREFIX='$RELEASE_PREFIX'
CREATE_PR=$CREATE_PR
OPEN_PR=$OPEN_PR
PR_LIST='${PRS[*]-}'
NEW_BRANCH='$NEW_BRANCH'
FORCE_PUSH=$FORCE_PUSH
NO_VERIFY=$NO_VERIFY
NEXT_INDEX=$1
COMMITS_STR='${COMMITS[*]-}'
EOF
}

# Stage the user's conflict resolutions automatically, refusing only if a
# file still contains conflict markers (i.e. wasn't actually resolved).
stage_resolved_files() {
  local f
  UNMERGED=()
  while IFS= read -r f; do
    UNMERGED+=("$f")
  done < <(git diff --name-only --diff-filter=U)
  [[ ${#UNMERGED[@]} -gt 0 ]] || return 0
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
}

# --- --abort: discard an interrupted run -------------------------------------
if $ABORT_RUN; then
  [[ -f "$STATE_FILE" ]] || die "No interrupted $SCRIPT_NAME run to abort"
  # shellcheck disable=SC1090
  . "$STATE_FILE"
  git cherry-pick --abort >/dev/null 2>&1 || true
  git merge --abort >/dev/null 2>&1 || true
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
  CLI_NO_VERIFY=$NO_VERIFY
  # shellcheck disable=SC1090
  . "$STATE_FILE"
  # a --no-verify given on this command line wins over the saved value
  if $CLI_NO_VERIFY; then NO_VERIFY=true; fi
  # shellcheck disable=SC2206
  COMMITS=($COMMITS_STR)
  # shellcheck disable=SC2206
  PRS=($PR_LIST)
  START_INDEX=$NEXT_INDEX
  if [[ "$MODE" == "merge" ]]; then
    info "Resuming the merge of ${FROM_BRANCH} into ${TO_BRANCH}"
  else
    info "Resuming onto ${TO_BRANCH} (commit $NEXT_INDEX of ${#COMMITS[@]} was in progress)"
  fi
else
  [[ -n "$TARGET_SPEC" ]] || die "--to is required (main, release-45, 45, or qa)"
  if [[ -n "$FROM_SPEC" ]]; then
    MODE="merge"
    [[ ${#PRS[@]} -eq 0 && -z "$STORY" ]] \
      || die "--from-release merges the whole release; it cannot be combined with --pr/--story"
  else
    [[ ${#PRS[@]} -gt 0 || -n "$STORY" ]] \
      || die "Give --pr <n> (repeatable), --story <id>, or --from-release <n>"
  fi
  if [[ -f "$STATE_FILE" ]] && ! $DRY_RUN; then
    die "An interrupted run exists. Re-run with --continue to resume it, or --abort to discard it."
  fi

  # Resolve target and source: main | release-45 | bare number | any branch name
  if [[ "$TARGET_SPEC" =~ ^[0-9]+$ ]]; then
    TO_BRANCH="${RELEASE_PREFIX}${TARGET_SPEC}"
  else
    TO_BRANCH="$TARGET_SPEC"
  fi
  if [[ -n "$FROM_SPEC" ]]; then
    if [[ "$FROM_SPEC" =~ ^[0-9]+$ ]]; then
      FROM_BRANCH="${RELEASE_PREFIX}${FROM_SPEC}"
    else
      FROM_BRANCH="$FROM_SPEC"
    fi
    [[ "$FROM_BRANCH" != "$TO_BRANCH" ]] || die "--from-release and --to are the same branch"
  fi
fi

if ! $CONTINUE_RUN; then
  if [[ -n "$(git status --porcelain)" ]]; then
    die "Working tree is not clean. Commit or stash your changes first."
  fi

  if [[ "$MODE" == "merge" ]]; then
    # --- Merge mode: promote a whole release branch into the target ----------
    info "Fetching from $REMOTE..."
    git fetch --prune "$REMOTE"
    git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${FROM_BRANCH}" >/dev/null \
      || die "Branch '$FROM_BRANCH' does not exist on '$REMOTE'"
    git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${TO_BRANCH}" >/dev/null \
      || die "Branch '$TO_BRANCH' does not exist on '$REMOTE'"

    AHEAD=$(git rev-list --count "${REMOTE}/${TO_BRANCH}..${REMOTE}/${FROM_BRANCH}")
    if [[ "$AHEAD" -eq 0 ]]; then
      echo "'$TO_BRANCH' already has everything on '$FROM_BRANCH'. Nothing to promote."
      exit 0
    fi
    info "$FROM_BRANCH is $AHEAD commit(s) ahead of $TO_BRANCH:"
    git log --oneline "${REMOTE}/${TO_BRANCH}..${REMOTE}/${FROM_BRANCH}" | head -30 | sed 's/^/    /'
    if [[ "$AHEAD" -gt 30 ]]; then
      echo "    ... and $((AHEAD - 30)) more"
    fi

    if ! $ASSUME_YES && ! $DRY_RUN; then
      if [[ -t 0 ]]; then
        printf 'Merge %s into %s? [y/N] ' "$FROM_BRANCH" "$TO_BRANCH"
        read -r REPLY
        [[ "$REPLY" =~ ^[Yy] ]] || { echo "Cancelled."; exit 1; }
      else
        die "Not a terminal; re-run with --yes to skip confirmation."
      fi
    fi

    COMMITS=()
    NEW_BRANCH="${BRANCH_OVERRIDE:-${FROM_BRANCH}-to-${TO_BRANCH}}"
  else

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
    gh pr view "$n" --json number,title,state,mergedAt,headRefName,commits,url \
      || die "Cannot read PR #$n (does it exist in this repo?)"
  done | jq -s 'unique_by(.number) | sort_by(.mergedAt // "9999-99-99", .number)')

  echo ""
  echo "PRs to promote into $TO_BRANCH (in this order):"
  echo "$PR_JSON" | jq -r '.[] | "  #\(.number) [\(.state)] \(.title)  (\(.commits | length) commit(s), branch: \(.headRefName))\n      \(.url)"'
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
  # Each commit also carries the Rally id from its own PR's branch name, so
  # the foreign-story guard below works even for explicit --pr runs.
  COMMITS=()
  EXPECTED=()
  SKIPPED_MERGES=0
  SKIPPED_PRESENT=0
  while IFS=$'\t' read -r sha bid; do
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
    EXPECTED+=("${bid:-}")
  done < <(echo "$PR_JSON" | jq -r '.[] as $pr
      | (($pr.headRefName | [match("(us|de|ta|ts)[0-9]{4,}"; "i").string] | first) // "" | ascii_upcase) as $bid
      | $pr.commits[] | .oid + "\t" + $bid' \
    | awk -F'\t' '!seen[$1]++')

  [[ "$SKIPPED_MERGES" -gt 0 ]] && echo "WARNING: skipping $SKIPPED_MERGES merge commit(s)." >&2
  [[ "$SKIPPED_PRESENT" -gt 0 ]] && info "Skipping $SKIPPED_PRESENT commit(s) already on $TO_BRANCH."

  # --- Foreign-story guard ---------------------------------------------------
  # A PR can contain another story's commits (branch cut off a different
  # feature branch, or a stray merge). A commit is foreign when its message
  # references a Rally id but never the expected one — the --story id when
  # given, otherwise the id from the commit's own PR branch name. Excluded
  # unless --keep-foreign-commits.
  if [[ ${#COMMITS[@]} -gt 0 ]]; then
    STORY_UP=$(echo "$STORY" | tr '[:lower:]' '[:upper:]')
    FOREIGN=()
    KEEP=()
    for idx in "${!COMMITS[@]}"; do
      h="${COMMITS[$idx]}"
      EXPECT="${STORY_UP:-${EXPECTED[$idx]}}"
      if [[ -z "$EXPECT" ]]; then
        KEEP+=("$h")   # no story context to compare against
        continue
      fi
      IDS=$(git show -s --format=%B "$h" \
              | grep -ioE '(US|DE|TA|TS)[0-9]{4,}' | tr '[:lower:]' '[:upper:]' \
              | sort -u || true)
      if [[ -n "$IDS" ]] && ! grep -qx "$EXPECT" <<< "$IDS"; then
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
        echo "         Re-run with --keep-foreign-commits if they really belong here." >&2
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

  fi  # end of pick-mode resolution

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
    if [[ "$MODE" == "merge" ]]; then
      info "[dry-run] merge '${REMOTE}/${FROM_BRANCH}' into it ($AHEAD commit(s)), push to '$REMOTE',"
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

  if [[ "$MODE" == "merge" ]]; then
    save_state 0
    info "Merging '${REMOTE}/${FROM_BRANCH}' into '$NEW_BRANCH'..."
    if ! MERGE_OUT=$(git merge --no-ff -m "Merge ${FROM_BRANCH} into ${TO_BRANCH}" "${REMOTE}/${FROM_BRANCH}" 2>&1); then
      echo "$MERGE_OUT" >&2
      cat >&2 <<EOF

CONFLICT while merging ${FROM_BRANCH} into ${TO_BRANCH}

Progress has been saved. Edit the conflicted files to resolve them, then:

    $SCRIPT_NAME --continue     # stages your fixes, finishes the merge, pushes, opens the PR

To give up instead:

    $SCRIPT_NAME --abort
EOF
      exit 1
    fi
  fi
else
  # --- Resume: finish whatever was interrupted -------------------------------
  if [[ "$(git rev-parse --abbrev-ref HEAD)" != "$NEW_BRANCH" ]]; then
    { git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null \
        || git rev-parse -q --verify MERGE_HEAD >/dev/null; } \
      && die "A cherry-pick or merge is in progress on a different branch; resolve or abort it first."
    git switch "$NEW_BRANCH"
  fi

  COMMIT_ARGS=(--no-edit)
  if $NO_VERIFY; then COMMIT_ARGS+=(--no-verify); fi

  if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
    stage_resolved_files
    info "Finishing the interrupted merge..."
    if ! FINISH_OUT=$(GIT_EDITOR=true git commit "${COMMIT_ARGS[@]}" 2>&1); then
      echo "$FINISH_OUT" >&2
      echo "" >&2
      echo "'git commit' failed to finish the merge (its output is above)." >&2
      echo "If a commit hook caused it, re-run with: $SCRIPT_NAME --continue --no-verify" >&2
      exit 1
    fi
  elif git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null; then
    stage_resolved_files
    info "Finishing the interrupted cherry-pick..."
    # With --no-verify, finish via 'git commit' directly: 'git cherry-pick
    # --continue' has no way to skip commit hooks.
    if $NO_VERIFY; then
      FINISH_CMD=(git commit "${COMMIT_ARGS[@]}")
    else
      FINISH_CMD=(git cherry-pick --continue)
    fi
    if ! FINISH_OUT=$(GIT_EDITOR=true "${FINISH_CMD[@]}" 2>&1); then
      if [[ -z "$(git diff --cached --name-only)" ]]; then
        info "Resolution left nothing to apply; skipping that commit."
        git cherry-pick --skip >/dev/null 2>&1 || git cherry-pick --quit >/dev/null 2>&1 || true
      else
        echo "$FINISH_OUT" >&2
        echo "" >&2
        echo "Finishing the cherry-pick failed (its output is above)." >&2
        echo "If a commit hook caused it, re-run with: $SCRIPT_NAME --continue --no-verify" >&2
        exit 1
      fi
    elif $NO_VERIFY; then
      git cherry-pick --quit >/dev/null 2>&1 || true
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

# --- Anything actually applied? ----------------------------------------------
# Commits can all turn out to be patch-identical to changes already on the
# target (promoted earlier under different hashes); git only detects that at
# cherry-pick time. With nothing applied there is nothing to push or PR.
APPLIED=$(git rev-list --count "${REMOTE}/${TO_BRANCH}..HEAD")
if [[ "$APPLIED" -eq 0 ]]; then
  rm -f "$STATE_FILE"
  if git rev-parse --verify --quiet refs/heads/main >/dev/null; then
    git switch -q main
  elif git rev-parse --verify --quiet refs/heads/master >/dev/null; then
    git switch -q master
  else
    git switch -q --detach "${REMOTE}/${TO_BRANCH}"
  fi
  git branch -D "$NEW_BRANCH" >/dev/null 2>&1 || true
  echo
  echo "=============================================================="
  echo " NOTHING TO PROMOTE"
  echo "=============================================================="
  if [[ "$MODE" == "merge" ]]; then
    echo " '$TO_BRANCH' already has everything on '$FROM_BRANCH'."
    echo " No branch pushed, no PR created."
  else
    echo " Every change in PR(s) ${PRS[*]-} is already on '$TO_BRANCH'"
    echo " (the same patches landed there previously under different"
    echo " commit hashes). No branch pushed, no PR created."
  fi
  echo "=============================================================="
  exit 0
fi

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
  if [[ "$MODE" == "merge" ]]; then
    PR_TITLE="Merge ${FROM_BRANCH} into ${TO_BRANCH}"
  elif [[ ${#PRS[@]} -eq 1 ]]; then
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
  if [[ "$MODE" == "merge" ]]; then
    PR_BODY="Merges $APPLIED commit(s) from ${FROM_BRANCH} into ${TO_BRANCH} by $SCRIPT_NAME."
  else
    PR_BODY="${ORIG_BODY}${ORIG_BODY:+

}---
Cherry-picked $APPLIED commit(s) from PR(s) ${PRS[*]-} onto ${TO_BRANCH} by $SCRIPT_NAME."
  fi
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
if [[ "$MODE" == "merge" ]]; then
  echo " Promoted:      $FROM_BRANCH -> $TO_BRANCH (merge)"
  echo " Merged:        $APPLIED commit(s) onto $TO_BRANCH"
else
  echo " Promoted:      ${#PRS[@]} PR(s): ${PRS[*]-}"
  if [[ "$APPLIED" -lt "$TOTAL" ]]; then
    echo " Cherry-picked: $APPLIED commit(s) onto $TO_BRANCH ($((TOTAL - APPLIED)) already there, skipped)"
  else
    echo " Cherry-picked: $APPLIED commit(s) onto $TO_BRANCH"
  fi
fi
echo " New branch:    $NEW_BRANCH  (pushed to $REMOTE)"
if [[ -n "$PR_URL" ]]; then
  echo " Pull request:  $PR_URL"
else
  echo " Create a PR from '$NEW_BRANCH' into '$TO_BRANCH' on GitHub."
fi
echo "=============================================================="
