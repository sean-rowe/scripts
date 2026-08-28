#!/usr/bin/env python3
#
# pod-audit.py
#
# Daily progress audit for a pod. For every dev named in the config it pulls
# their Rally stories for the current sprint (computed from a config anchor
# date; sprints are length_days long) and the previous N sprints, then for
# each story reports:
#
#   - acceptance criteria, with a completion % per AC (scored by the `claude`
#     CLI against the story branch's diff when [ai].enabled, otherwise a
#     story-level heuristic from task ToDo hours / PR / branch state)
#   - the story branch, found by searching configured repos for the story id,
#     with commit count, files changed, and last-commit age
#   - pull requests raised from that branch (GitHub via `gh`, Bitbucket via
#     its REST API): state, review decision, comment count, age
#   - blockers and detected risks (blocked flag, no estimate, no branch late
#     in the sprint, stale PRs/stories, comment churn, spillover from earlier
#     sprints, ToDo not moving since the last run)
#
# Each run appends a snapshot of remaining points to a history file; those
# snapshots drive a pod burndown chart and one per dev in the HTML report.
# A PDF copy of the report is produced alongside the HTML (rendered with a
# headless Chromium-based browser so it keeps the exact same look).
#
# Usage:
#   pod-audit.py [--config pod-audit.toml] [options]
#
# Options:
#   --config <path>     Config file (default: pod-audit.toml next to script)
#   --date <YYYY-MM-DD> Pretend today is this date (testing / backfill)
#   --no-ai             Skip claude-based AC scoring even if enabled in config
#   --no-git            Skip branch/PR discovery (Rally data only)
#   --no-pdf            Skip the PDF copy of the report
#   --show-sprints      Print the computed sprint windows and exit
#   --check             Diagnose the config against Rally: verifies the API
#                       key, that the computed iteration exists under that
#                       name, that each dev matches a Rally user (with
#                       suggestions), and that the git repos are reachable.
#                       Run this first if a report comes back empty.
#   --open              Open the HTML report in the default browser when done
#
# Run it daily from cron, e.g. weekdays at 7:30:
#   30 7 * * 1-5 cd /path/to/scripts && ./pod-audit.py --config pod-audit.toml
#
# Secrets can live in the config or in env vars: RALLY_API_KEY,
# BITBUCKET_TOKEN (or BITBUCKET_USER + BITBUCKET_APP_PASSWORD).

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"==> {msg}")


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULTS = {
    "pod": {"name": "Pod", "devs": []},
    "sprint": {"length_days": 14, "previous": 2},
    "rally": {
        "server": "https://rally1.rallydev.com",
        "api_key": "",
        "workspace": "",
        "project": "",
        "iteration_name_format": "Sprint {n}",
        "acceptance_criteria_field": "c_AcceptanceCriteria",
    },
    "git": {"repos": [], "remote": "origin"},
    "bitbucket": {"token": "", "user": "", "app_password": ""},
    "ai": {"enabled": False, "max_diff_chars": 12000},
    "risks": {
        "stale_pr_days": 3,
        "max_pr_comments": 10,
        "no_branch_after_pct": 50,
        "stale_story_days": 3,
    },
    "report": {"output_dir": "reports", "history_file": "reports/history.json", "pdf": True},
}


def load_config(path):
    p = Path(path).expanduser()
    if not p.is_file():
        die(
            f"Config file not found: {p}\n"
            f"       Copy pod-audit.example.toml to {p.name} and fill it in."
        )
    try:
        with open(p, "rb") as f:
            user_cfg = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(
            f"Cannot parse {p}: {e}\n"
            "       Note: TOML paths need no shell escaping — write spaces as-is\n"
            '       ("/Users/me/POD 6/repo", not "/Users/me/POD\\ 6/repo").'
        )
    cfg = {}
    for section, defaults in DEFAULTS.items():
        cfg[section] = {**defaults, **user_cfg.get(section, {})}

    devs = cfg["pod"]["devs"]
    if not isinstance(devs, list) or not devs or not all(
        isinstance(d, str) and d.strip() for d in devs
    ):
        die("[pod].devs must be a non-empty list of names")
    for key in ("current", "anchor_start"):
        if key not in cfg["sprint"]:
            die(f"[sprint].{key} is required")
    s = cfg["sprint"]
    if not isinstance(s["current"], int):
        die(f"[sprint].current must be a whole number, got {s['current']!r}")
    if not isinstance(s["length_days"], int) or s["length_days"] < 2:
        die(f"[sprint].length_days must be a whole number >= 2, got {s['length_days']!r}")
    if not isinstance(s["previous"], int) or s["previous"] < 0:
        die(f"[sprint].previous must be a whole number >= 0, got {s['previous']!r}")
    anchor = s["anchor_start"]
    if isinstance(anchor, str):
        try:
            anchor = dt.date.fromisoformat(anchor)
        except ValueError:
            die(f"[sprint].anchor_start is not a date: {anchor!r} (write 2026-08-24, unquoted)")
    elif isinstance(anchor, dt.datetime):
        anchor = anchor.date()
    if not isinstance(anchor, dt.date):
        die(f"[sprint].anchor_start is not a date: {anchor!r} (write 2026-08-24, unquoted)")
    cfg["sprint"]["anchor_start"] = anchor
    repos = cfg["git"]["repos"]
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        die("[git].repos must be a list of directory paths")
    try:
        cfg["rally"]["iteration_name_format"].format(n=1)
    except (KeyError, IndexError, ValueError):
        die(
            f"[rally].iteration_name_format is invalid: "
            f"{cfg['rally']['iteration_name_format']!r} (use {{n}} for the sprint number)"
        )
    cfg["rally"]["api_key"] = cfg["rally"]["api_key"] or os.environ.get("RALLY_API_KEY", "")
    bb = cfg["bitbucket"]
    bb["token"] = bb["token"] or os.environ.get("BITBUCKET_TOKEN", "")
    bb["user"] = bb["user"] or os.environ.get("BITBUCKET_USER", "")
    bb["app_password"] = bb["app_password"] or os.environ.get("BITBUCKET_APP_PASSWORD", "")
    cfg["_dir"] = p.parent
    return cfg


# --------------------------------------------------------------------------
# Sprint math
# --------------------------------------------------------------------------

def sprint_for_date(cfg, day):
    s = cfg["sprint"]
    return s["current"] + (day - s["anchor_start"]).days // s["length_days"]


def sprint_window(cfg, n):
    s = cfg["sprint"]
    start = s["anchor_start"] + dt.timedelta(days=(n - s["current"]) * s["length_days"])
    end = start + dt.timedelta(days=s["length_days"] - 1)
    return start, end


def iteration_name(cfg, n):
    return cfg["rally"]["iteration_name_format"].format(n=n)


# --------------------------------------------------------------------------
# Rally API (WSAPI v2.0)
# --------------------------------------------------------------------------

STORY_FETCH = (
    "FormattedID,Name,ObjectID,ScheduleState,PlanEstimate,ToDo,"
    "TaskEstimateTotal,TaskRemainingTotal,Blocked,BlockedReason,Description,"
    "Discussion,LastUpdateDate,Iteration,Owner,DisplayName"
)


class Rally:
    def __init__(self, cfg):
        r = cfg["rally"]
        if not r["api_key"]:
            die("No Rally API key: set [rally].api_key or export RALLY_API_KEY")
        self.server = r["server"].rstrip("/")
        self.base = f"{self.server}/slm/webservice/v2.0"
        self.key = r["api_key"]
        self.scope = {}
        if r["workspace"]:
            self.scope["workspace"] = self._ref("workspace", r["workspace"])
        if r["project"]:
            self.scope["project"] = self._ref("project", r["project"])
            self.scope["projectScopeDown"] = "true"
        self.ac_field = r["acceptance_criteria_field"]

    def _ref(self, kind, value):
        value = str(value)
        return f"/{kind}/{value}" if value.isdigit() else value

    def _get(self, endpoint, params):
        url = f"{self.base}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"ZSESSIONID": self.key})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode(errors="replace")[:300]
            except OSError:
                body = ""
            die(f"Rally API error {e.code} for {endpoint}: {body}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            die(f"Cannot reach Rally at {self.server}: {e}")
        except json.JSONDecodeError as e:
            die(f"Rally returned invalid JSON for {endpoint}: {e}")
        if not isinstance(data, dict):
            die(f"Rally returned an unexpected response for {endpoint}")
        return data

    def query(self, entity, query, fetch):
        results, start = [], 1
        while True:
            params = {
                "query": query,
                "fetch": fetch,
                "pagesize": "100",
                "start": str(start),
                **self.scope,
            }
            qr = self._get(entity, params).get("QueryResult")
            if not isinstance(qr, dict):
                die(f"Rally response for {entity} has no QueryResult")
            errors = qr.get("Errors") or []
            if errors:
                die(f"Rally query failed for {entity}: {'; '.join(errors)}")
            results.extend(qr.get("Results", []))
            total = qr.get("TotalResultCount", 0)
            if len(results) >= total:
                return results
            start += 100

    def whoami(self):
        data = self._get("user", {"fetch": "UserName,DisplayName,EmailAddress"})
        user = data.get("User")
        return user if isinstance(user, dict) else {}

    def stories_for(self, dev, sprint_name):
        owner_attr = "Owner.UserName" if "@" in dev else "Owner.DisplayName"
        q = f'(({owner_attr} = "{dev}") AND (Iteration.Name = "{sprint_name}"))'
        fetch = STORY_FETCH + "," + self.ac_field
        items = []
        for entity, kind in (("hierarchicalrequirement", "Story"), ("defect", "Defect")):
            for raw in self.query(entity, q, fetch):
                items.append((kind, raw))
        return items

    def story_url(self, kind, oid):
        page = "defect" if kind == "Defect" else "userstory"
        return f"{self.server}/#/detail/{page}/{oid}"


# --------------------------------------------------------------------------
# Acceptance criteria extraction
# --------------------------------------------------------------------------

def html_to_lines(text):
    if not text:
        return []
    text = re.sub(r"(?i)</\s*(li|p|div|h[1-6]|tr)\s*>|<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_acceptance_criteria(raw, ac_field):
    lines = html_to_lines(raw.get(ac_field) or "")
    if not lines:
        # Fall back to an "Acceptance Criteria" section in the Description.
        desc = html_to_lines(raw.get("Description") or "")
        in_section = False
        for ln in desc:
            if re.match(r"(?i)^acceptance\s+criteria\b", ln):
                in_section = True
                continue
            if in_section:
                if re.match(r"(?i)^(notes?|design|background|out of scope)\b.{0,20}:?$", ln):
                    break
                lines.append(ln)
    # Drop bullet/number prefixes and obvious non-criteria.
    acs = []
    for ln in lines:
        ln = re.sub(r"^\s*(?:[-*•]|\d+[.)]|[a-z][.)])\s+", "", ln).strip()
        if len(ln) > 3:
            acs.append(ln)
    return acs[:15]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class PR:
    title: str
    state: str          # OPEN / MERGED / DECLINED / CLOSED
    url: str
    comments: int
    review: str         # APPROVED / CHANGES_REQUESTED / '' etc.
    draft: bool
    created: str
    updated: str


@dataclass
class Branch:
    repo: str
    name: str
    commits: int = 0
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    last_commit_age_days: int | None = None
    last_commit_subject: str = ""
    prs: list = field(default_factory=list)


@dataclass
class Story:
    kind: str
    sid: str
    name: str
    url: str
    state: str
    points: float | None
    todo_hours: float | None
    task_est: float | None
    blocked: bool
    blocked_reason: str
    discussions: int
    last_update: dt.date | None
    acs: list
    ac_scores: list = field(default_factory=list)   # [(percent, note)] per AC
    branches: list = field(default_factory=list)
    risks: list = field(default_factory=list)

    @property
    def done(self):
        return self.state in ("Accepted", "Released")

    @property
    def completion(self):
        if self.ac_scores:
            return round(sum(p for p, _ in self.ac_scores) / len(self.ac_scores))
        return heuristic_percent(self)

    @property
    def all_prs(self):
        return [pr for b in self.branches for pr in b.prs]


def _num(v):
    """A float, or None for anything that isn't a real number."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_story(rally, kind, raw):
    lu = None
    raw_lu = raw.get("LastUpdateDate")
    if isinstance(raw_lu, str):
        try:
            lu = dt.datetime.fromisoformat(raw_lu.replace("Z", "+00:00")).date()
        except ValueError:
            lu = None
    disc = raw.get("Discussion")
    return Story(
        kind=kind,
        sid=str(raw.get("FormattedID") or "?"),
        name=str(raw.get("Name") or ""),
        url=rally.story_url(kind, raw.get("ObjectID", "")),
        state=str(raw.get("ScheduleState") or "?"),
        points=_num(raw.get("PlanEstimate")),
        todo_hours=_num(raw.get("ToDo")),
        task_est=_num(raw.get("TaskEstimateTotal")),
        blocked=bool(raw.get("Blocked")),
        blocked_reason=str(raw.get("BlockedReason") or ""),
        discussions=disc.get("Count", 0) if isinstance(disc, dict) else 0,
        last_update=lu,
        acs=parse_acceptance_criteria(raw, rally.ac_field),
    )


# --------------------------------------------------------------------------
# Git: find story branches and stats
# --------------------------------------------------------------------------

def run(cmd, cwd=None, timeout=120, input_=None):
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input_
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", str(e)


class RepoIndex:
    """ls-remote each configured repo once, then answer branch searches."""

    def __init__(self, cfg):
        self.remote = cfg["git"]["remote"]
        self.repos = {}  # path -> {"heads": [...], "default": str, "url": str, "fetched": bool}
        for raw_path in cfg["git"]["repos"]:
            path = Path(raw_path).expanduser()
            if not (path / ".git").exists() and not (path / "HEAD").exists():
                warn(f"Skipping [git].repos entry (not a git repo): {path}")
                continue
            rc, out, err = run(
                ["git", "ls-remote", "--symref", "--heads", self.remote], cwd=path, timeout=90
            )
            if rc != 0:
                # --symref on --heads won't show HEAD; get heads and HEAD separately
                warn(f"ls-remote failed for {path}: {err}")
                continue
            heads = [
                line.split("refs/heads/", 1)[1]
                for line in out.splitlines()
                if "refs/heads/" in line
            ]
            _, head_out, _ = run(["git", "ls-remote", "--symref", self.remote, "HEAD"], cwd=path)
            m = re.search(r"ref:\s+refs/heads/(\S+)\s+HEAD", head_out)
            default = m.group(1) if m else "main"
            _, url, _ = run(["git", "remote", "get-url", self.remote], cwd=path)
            self.repos[path] = {"heads": heads, "default": default, "url": url, "fetched": False}

    def find(self, story_id):
        """Return [(repo_path, branch_name, default_branch, remote_url)]."""
        sid = story_id.lower()
        out = []
        for path, meta in self.repos.items():
            for b in meta["heads"]:
                if sid in b.lower():
                    out.append((path, b, meta["default"], meta["url"]))
        return out

    def ensure_fetched(self, path):
        meta = self.repos[path]
        if not meta["fetched"]:
            info(f"Fetching {path.name}...")
            run(["git", "fetch", "-q", "--prune", self.remote], cwd=path, timeout=300)
            meta["fetched"] = True


def branch_stats(index, path, branch, default):
    index.ensure_fetched(path)
    remote = index.remote
    ref, base = f"{remote}/{branch}", f"{remote}/{default}"
    b = Branch(repo=path.name, name=branch)
    rc, out, _ = run(["git", "rev-list", "--count", f"{base}..{ref}"], cwd=path)
    if rc == 0 and out.isdigit():
        b.commits = int(out)
    rc, out, _ = run(["git", "diff", "--shortstat", f"{base}...{ref}"], cwd=path)
    if rc == 0:
        m = re.search(r"(\d+) files? changed", out)
        b.files_changed = int(m.group(1)) if m else 0
        m = re.search(r"(\d+) insertions?", out)
        b.insertions = int(m.group(1)) if m else 0
        m = re.search(r"(\d+) deletions?", out)
        b.deletions = int(m.group(1)) if m else 0
    rc, out, _ = run(["git", "log", "-1", "--format=%ct%x09%s", ref], cwd=path)
    if rc == 0 and out:
        ts, _, subject = out.partition("\t")
        if ts.isdigit():
            age = dt.datetime.now() - dt.datetime.fromtimestamp(int(ts))
            b.last_commit_age_days = age.days
        b.last_commit_subject = subject
    return b


def branch_diff_text(index, path, branch, default, limit):
    remote = index.remote
    ref, base = f"{remote}/{branch}", f"{remote}/{default}"
    _, stat, _ = run(["git", "diff", "--stat", f"{base}...{ref}"], cwd=path)
    _, patch, _ = run(["git", "diff", f"{base}...{ref}"], cwd=path, timeout=180)
    text = stat + "\n\n" + patch
    return text[:limit] + ("\n...[diff truncated]" if len(text) > limit else "")


# --------------------------------------------------------------------------
# Pull requests
# --------------------------------------------------------------------------

def github_prs(repo_path, branch):
    rc, out, err = run(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "10",
         "--json", "number"],
        cwd=repo_path,
    )
    if rc != 0:
        warn(f"gh pr list failed in {repo_path.name}: {err.splitlines()[0] if err else rc}")
        return []
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        warn(f"Unexpected gh output in {repo_path.name}; skipping PR lookup")
        return []
    prs = []
    for item in items:
        number = item.get("number") if isinstance(item, dict) else None
        if number is None:
            continue
        rc, detail, _ = run(
            ["gh", "pr", "view", str(number), "--json",
             "title,state,url,isDraft,createdAt,updatedAt,reviewDecision,comments,reviews"],
            cwd=repo_path,
        )
        if rc != 0:
            continue
        try:
            d = json.loads(detail)
        except json.JSONDecodeError:
            continue
        comments = len(d.get("comments") or []) + len(
            [r for r in d.get("reviews") or [] if (r.get("body") or "").strip()]
        )
        prs.append(PR(
            title=d.get("title", ""),
            state=d.get("state", ""),
            url=d.get("url", ""),
            comments=comments,
            review=d.get("reviewDecision") or "",
            draft=bool(d.get("isDraft")),
            created=(d.get("createdAt") or "")[:10],
            updated=(d.get("updatedAt") or "")[:10],
        ))
    return prs


def bitbucket_prs(cfg, remote_url, branch):
    bb = cfg["bitbucket"]
    headers = {}
    if bb["token"]:
        headers["Authorization"] = f"Bearer {bb['token']}"
    elif bb["user"] and bb["app_password"]:
        import base64
        cred = base64.b64encode(f"{bb['user']}:{bb['app_password']}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    else:
        warn("Bitbucket repo found but no credentials configured; skipping PR lookup")
        return []
    slug = re.sub(
        r"^(git@bitbucket\.org:|https://([^@/]+@)?bitbucket\.org/)", "", remote_url
    ).removesuffix(".git").strip("/")
    q = urllib.parse.quote(f'source.branch.name = "{branch}"')
    url = (
        f"https://api.bitbucket.org/2.0/repositories/{slug}/pullrequests"
        f"?q={q}&state=OPEN&state=MERGED&state=DECLINED&state=SUPERSEDED&pagelen=10"
    )
    prs = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            listing = json.load(resp)
        for v in listing.get("values", []):
            if not isinstance(v, dict) or "id" not in v:
                continue
            detail_url = (
                f"https://api.bitbucket.org/2.0/repositories/{slug}/pullrequests/{v['id']}"
            )
            with urllib.request.urlopen(
                urllib.request.Request(detail_url, headers=headers), timeout=30
            ) as resp:
                d = json.load(resp)
            approvals = sum(1 for p in d.get("participants", []) if p.get("approved"))
            prs.append(PR(
                title=d.get("title", ""),
                state=d.get("state", ""),
                url=d.get("links", {}).get("html", {}).get("href", ""),
                comments=d.get("comment_count", 0),
                review=f"{approvals} approval(s)" if approvals else "",
                draft=bool(d.get("draft")),
                created=(d.get("created_on") or "")[:10],
                updated=(d.get("updated_on") or "")[:10],
            ))
    except (urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        warn(f"Bitbucket PR lookup failed for {slug}: {e.__class__.__name__}: {e}")
    return prs


def find_prs(cfg, repo_path, remote_url, branch):
    if "github.com" in remote_url:
        return github_prs(repo_path, branch)
    if "bitbucket.org" in remote_url:
        return bitbucket_prs(cfg, remote_url, branch)
    return []


# --------------------------------------------------------------------------
# AC completion scoring
# --------------------------------------------------------------------------

def heuristic_percent(story):
    if story.state in ("Accepted", "Released"):
        return 100
    if story.state == "Completed":
        return 90
    if story.task_est and story.todo_hours is not None:
        pct = round(100 * (1 - story.todo_hours / story.task_est))
        return max(5, min(pct, 89))
    pr_states = {pr.state for pr in story.all_prs}
    if "MERGED" in pr_states:
        return 85
    if "OPEN" in pr_states:
        return 60
    if story.branches:
        return 40
    if story.state == "In-Progress":
        return 25
    return 0


def ai_score_acs(story, diff_text, cfg):
    """Ask the claude CLI to score each AC against the branch diff. Returns
    [(percent, note)] aligned with story.acs, or None on any failure."""
    ac_list = "\n".join(f"{i + 1}. {ac}" for i, ac in enumerate(story.acs))
    prompt = f"""You are auditing progress on a user story. Based ONLY on the code diff below, estimate how complete each acceptance criterion is (0-100) with a short note (max 12 words) explaining the evidence.

Story: {story.sid} — {story.name}

Acceptance criteria:
{ac_list}

Code diff of the story branch vs the mainline:
```
{diff_text}
```

Respond with ONLY a JSON array, one object per criterion in order:
[{{"index": 1, "percent": 0, "note": "..."}}, ...]"""
    rc, out, err = run(["claude", "-p"], input_=prompt, timeout=240)
    if rc != 0:
        warn(f"claude CLI failed for {story.sid}: {err.splitlines()[0] if err else rc}")
        return None
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        return None
    try:
        items = json.loads(m.group(0))
        scores = {int(i["index"]): i for i in items}
        return [
            (
                max(0, min(100, int(scores.get(i + 1, {}).get("percent", 0)))),
                str(scores.get(i + 1, {}).get("note", "")),
            )
            for i in range(len(story.acs))
        ]
    except (ValueError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------
# Risk detection
# --------------------------------------------------------------------------

def assess_risks(story, sprint_n, current_sprint, elapsed_pct, cfg, prev_story_state, today):
    r = cfg["risks"]
    risks = []
    if story.blocked:
        reason = f": {story.blocked_reason}" if story.blocked_reason else ""
        risks.append(("blocker", f"Marked BLOCKED in Rally{reason}"))
    if sprint_n < current_sprint and not story.done:
        risks.append(("spillover", f"Carried over from sprint {sprint_n} and still {story.state}"))
    if story.points is None:
        risks.append(("estimate", "No point estimate on the story"))
    if (
        not story.done
        and sprint_n == current_sprint
        and not story.branches
        and elapsed_pct >= r["no_branch_after_pct"]
    ):
        risks.append(("no-branch", f"No branch found and sprint is {elapsed_pct:.0f}% elapsed"))
    for pr in story.all_prs:
        if pr.state == "OPEN":
            try:
                idle = (today - dt.date.fromisoformat(pr.updated)).days
            except ValueError:
                idle = 0
            if idle >= r["stale_pr_days"]:
                risks.append(("stale-pr", f"PR open with no activity for {idle} days ({pr.url})"))
            if pr.review == "CHANGES_REQUESTED":
                risks.append(("review", f"PR has changes requested ({pr.url})"))
        if pr.comments > r["max_pr_comments"]:
            risks.append(("churn", f"PR has {pr.comments} comments — possible churn ({pr.url})"))
    if (
        story.state == "In-Progress"
        and story.last_update
        and (today - story.last_update).days >= r["stale_story_days"]
        and not story.blocked
    ):
        risks.append(
            ("stale", f"In-Progress but not updated in Rally for {(today - story.last_update).days} days")
        )
    if (
        prev_story_state is not None
        and not story.done
        and story.todo_hours is not None
        and prev_story_state.get("todo") == story.todo_hours
        and prev_story_state.get("state") == story.state
        and story.state == "In-Progress"
    ):
        risks.append(("no-movement", "No change in ToDo hours or state since the last audit"))
    story.risks = risks
    return risks


# --------------------------------------------------------------------------
# History / snapshots (drives the burndown charts)
# --------------------------------------------------------------------------

def load_history(path):
    if path.is_file():
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("snapshots"), dict):
                return data
            warn(f"History file {path} has an unexpected shape; starting fresh")
        except (json.JSONDecodeError, OSError) as e:
            warn(f"Cannot read history file {path} ({e}); starting fresh")
        # Preserve the bad file so past burndown data isn't silently destroyed.
        backup = path.with_name(path.name + ".corrupt")
        try:
            path.replace(backup)
            warn(f"Old history preserved at {backup}")
        except OSError:
            pass
    return {"snapshots": {}}


def remaining_points(stories):
    return sum(s.points or 0 for s in stories if not s.done)


def total_points(stories):
    return sum(s.points or 0 for s in stories)


def record_snapshot(history, today, sprint_n, per_dev_current):
    snap = {"sprint": sprint_n, "devs": {}, "stories": {}}
    for dev, stories in per_dev_current.items():
        snap["devs"][dev] = {
            "remaining": remaining_points(stories),
            "total": total_points(stories),
        }
        for s in stories:
            snap["stories"][s.sid] = {"todo": s.todo_hours, "state": s.state}
    snap["pod"] = {
        "remaining": sum(d["remaining"] for d in snap["devs"].values()),
        "total": sum(d["total"] for d in snap["devs"].values()),
    }
    history["snapshots"][today.isoformat()] = snap
    return snap


def sprint_series(history, sprint_n, start, key, dev=None):
    """[(day_index, remaining)] for snapshots belonging to this sprint."""
    out = []
    for date_str, snap in sorted(history["snapshots"].items()):
        if not isinstance(snap, dict) or snap.get("sprint") != sprint_n:
            continue
        try:
            day = (dt.date.fromisoformat(date_str) - start).days
        except ValueError:
            continue
        if dev is None:
            val = snap.get("pod", {}).get(key)
        else:
            val = snap.get("devs", {}).get(dev, {}).get(key)
        if val is not None:
            out.append((day, val))
    return out


# --------------------------------------------------------------------------
# Charts (inline SVG)
# --------------------------------------------------------------------------

def burndown_svg(title, length_days, total, actual, width=560, height=260):
    """actual: [(day_index, remaining_points)]"""
    ml, mr, mt, mb = 42, 14, 30, 30
    pw, ph = width - ml - mr, height - mt - mb
    y_max = max([total] + [v for _, v in actual] + [1])
    last_day = length_days - 1

    def x(day):
        return ml + pw * min(max(day, 0), last_day) / last_day

    def y(val):
        return mt + ph * (1 - val / y_max)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{html.escape(title)}">',
        f'<text x="{ml}" y="18" class="ctitle">{html.escape(title)}</text>',
    ]
    # gridlines + y labels
    steps = 4
    for i in range(steps + 1):
        val = y_max * i / steps
        yy = y(val)
        parts.append(f'<line x1="{ml}" y1="{yy:.1f}" x2="{width - mr}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 6}" y="{yy + 4:.1f}" class="ylab">{val:g}</text>')
    # x labels: day 1..N (every other day to avoid crowding)
    for d in range(0, length_days, 2):
        parts.append(
            f'<text x="{x(d):.1f}" y="{height - 8}" class="xlab">{d + 1}</text>'
        )
    # ideal line
    parts.append(
        f'<line x1="{x(0):.1f}" y1="{y(total):.1f}" x2="{x(last_day):.1f}" y2="{y(0):.1f}" class="ideal"/>'
    )
    # actual line + points
    if actual:
        pts = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in actual)
        parts.append(f'<polyline points="{pts}" class="actual"/>')
        for d, v in actual:
            parts.append(f'<circle cx="{x(d):.1f}" cy="{y(v):.1f}" r="3" class="dot"/>')
        label_y = max(y(actual[-1][1]) - 6, mt + 10)
        parts.append(
            f'<text x="{x(actual[-1][0]) + 6:.1f}" y="{label_y:.1f}" class="now">{actual[-1][1]:g}</text>'
        )
    parts.append(
        f'<text x="{width - mr}" y="18" text-anchor="end" class="legend">'
        f'– – ideal &#160;&#160;● actual (pts remaining, by sprint day)</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

CSS = """
:root { --bg:#fff; --fg:#1a1d21; --muted:#667085; --line:#e4e7ec; --card:#f8fafc;
        --ok:#12805c; --warnc:#b54708; --bad:#b42318; --accent:#175cd3; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#101418; --fg:#e6e9ee; --muted:#98a2b3; --line:#2a313a; --card:#171d24;
          --ok:#3ccb7f; --warnc:#f7b267; --bad:#f97066; --accent:#7cb3ff; } }
* { box-sizing:border-box; }
body { margin:0 auto; max-width:1100px; padding:24px; font:15px/1.5 -apple-system,
       "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--fg); }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:19px; margin:32px 0 8px; }
h3 { font-size:16px; margin:20px 0 6px; }
.sub { color:var(--muted); margin-bottom:20px; }
table { border-collapse:collapse; width:100%; margin:8px 0 16px; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; font-size:13px; }
td.num, th.num { text-align:right; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px 18px 18px; margin:14px 0; }
.card .sec { margin-top:24px; }
.seclab { font-size:11px; font-weight:700; text-transform:uppercase;
          letter-spacing:.06em; color:var(--muted); margin-bottom:8px; }
.prline { margin:6px 0 0 16px; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
         font-weight:600; border:1px solid var(--line); }
.b-done { color:var(--ok); } .b-prog { color:var(--accent); } .b-blocked { color:var(--bad); }
.risk { color:var(--bad); } .warn { color:var(--warnc); }
.muted { color:var(--muted); } a { color:var(--accent); }
.bar { background:var(--line); border-radius:4px; height:8px; width:160px;
       display:inline-block; vertical-align:middle; }
.bar > span { display:block; height:8px; border-radius:4px; background:var(--ok); }
ul.acs { margin:0; padding-left:0; list-style:none; }
ul.acs li { margin:6px 0; }
ul.risks { margin:0; padding-left:18px; } ul.risks li { margin:5px 0; }
.chart { max-width:100%; height:auto; }
.chart .grid { stroke:var(--line); stroke-width:1; }
.chart .ideal { stroke:var(--muted); stroke-width:1.5; stroke-dasharray:5 4; }
.chart .actual { stroke:var(--accent); stroke-width:2.2; fill:none; }
.chart .dot { fill:var(--accent); }
.chart .ctitle { fill:var(--fg); font-size:13px; font-weight:600; }
.chart .ylab { fill:var(--muted); font-size:10px; text-anchor:end; }
.chart .xlab { fill:var(--muted); font-size:10px; text-anchor:middle; }
.chart .now { fill:var(--accent); font-size:11px; font-weight:700; }
.chart .legend { fill:var(--muted); font-size:10px; }
.charts { display:flex; flex-wrap:wrap; gap:16px; }
.charts > div { flex:1 1 460px; }
"""


def esc(s):
    return html.escape(str(s))


def state_badge(story):
    cls = "b-done" if story.done else ("b-blocked" if story.blocked else "b-prog")
    label = story.state + (" · BLOCKED" if story.blocked else "")
    return f'<span class="badge {cls}">{esc(label)}</span>'


def pct_bar(pct):
    color = "var(--ok)" if pct >= 70 else ("var(--warnc)" if pct >= 30 else "var(--bad)")
    return (
        f'<span class="bar"><span style="width:{pct}%;background:{color}"></span></span> '
        f"<strong>{pct}%</strong>"
    )


def render_story(story, sprint_n, current_sprint):
    h = [f'<div class="card">']
    pts = f"{story.points:g} pts" if story.points is not None else "unestimated"
    h.append(
        f'<div><a href="{esc(story.url)}"><strong>{esc(story.sid)}</strong></a> '
        f"{esc(story.name)} &nbsp;{state_badge(story)} "
        f'<span class="muted">· {esc(story.kind)} · {pts} · '
        f"{story.discussions} discussion(s)</span></div>"
    )
    h.append(f'<div class="sec">Overall completion: {pct_bar(story.completion)}</div>')

    h.append('<div class="sec"><div class="seclab">Acceptance criteria</div>')
    if story.acs:
        h.append("<ul class='acs'>")
        for i, ac in enumerate(story.acs):
            if story.ac_scores:
                pct, note = story.ac_scores[i]
                note_html = f' <span class="muted">— {esc(note)}</span>' if note else ""
            else:
                pct, note_html = story.completion, ' <span class="muted">— story-level estimate</span>'
            h.append(f"<li>{pct_bar(pct)} {esc(ac)}{note_html}</li>")
        h.append("</ul>")
    else:
        h.append('<div class="warn">None found on the story.</div>')
    h.append("</div>")

    h.append('<div class="sec"><div class="seclab">Branch &amp; pull requests</div>')
    if story.branches:
        for b in story.branches:
            age = (
                f"last commit {b.last_commit_age_days}d ago"
                if b.last_commit_age_days is not None
                else "no commits"
            )
            h.append(
                f'<div><code>{esc(b.repo)}:{esc(b.name)}</code> '
                f'<span class="muted">— {b.commits} commit(s), {b.files_changed} file(s) '
                f"changed (+{b.insertions}/−{b.deletions}), {age}"
                + (f" · “{esc(b.last_commit_subject)}”" if b.last_commit_subject else "")
                + "</span></div>"
            )
            for pr in b.prs:
                extra = " · DRAFT" if pr.draft else ""
                review = f" · {esc(pr.review)}" if pr.review else ""
                h.append(
                    f'<div class="prline"><strong>PR</strong> '
                    f'<a href="{esc(pr.url)}">{esc(pr.title)}</a> '
                    f'<span class="muted">— {esc(pr.state)}{extra}{review} · '
                    f"{pr.comments} comment(s) · opened {esc(pr.created)}, "
                    f"updated {esc(pr.updated)}</span></div>"
                )
        if not story.all_prs and not story.done:
            h.append('<div class="warn">Branch exists but no PR raised yet.</div>')
    elif story.done:
        h.append('<div class="muted">No branch found.</div>')
    else:
        h.append('<div class="warn">No branch found for this story.</div>')
    h.append("</div>")

    if story.risks:
        h.append('<div class="sec"><div class="seclab">Risks</div><ul class="risks">')
        for tag, msg in story.risks:
            h.append(f'<li class="risk">[{esc(tag)}] {esc(msg)}</li>')
        h.append("</ul></div>")
    h.append("</div>")
    return "".join(h)


def render_report(cfg, today, current_sprint, sprints, per_dev, history):
    pod = cfg["pod"]["name"]
    length = cfg["sprint"]["length_days"]
    start, end = sprint_window(cfg, current_sprint)
    day_no = (today - start).days + 1
    it_name = iteration_name(cfg, current_sprint)

    all_current = [s for dev in per_dev.values() for s in dev.get(current_sprint, [])]
    all_stories = [s for dev in per_dev.values() for sp in dev.values() for s in sp]
    all_risks = [(s, r) for s in all_stories for r in s.risks]
    blocked = [s for s in all_stories if s.blocked]

    h = [
        f"<title>{esc(pod)} audit {today.isoformat()}</title>",
        f"<style>{CSS}</style>",
        f"<h1>{esc(pod)} — daily audit</h1>",
        f'<div class="sub">{today.strftime("%A %d %B %Y")} · {esc(it_name)} '
        f"({start.isoformat()} → {end.isoformat()}) · day {day_no} of {length} · "
        f"includes {len(sprints) - 1} previous sprint(s)</div>",
    ]

    # --- Pod summary table
    h.append("<h2>Pod summary</h2><table><tr><th>Dev</th>"
             "<th class='num'>Stories</th><th class='num'>Points</th>"
             "<th class='num'>Accepted</th><th class='num'>Remaining</th>"
             "<th class='num'>PRs open</th><th class='num'>Blocked</th>"
             "<th class='num'>Risks</th></tr>")
    for dev, by_sprint in per_dev.items():
        cur = by_sprint.get(current_sprint, [])
        everything = [s for sp in by_sprint.values() for s in sp]
        open_prs = sum(1 for s in everything for pr in s.all_prs if pr.state == "OPEN")
        h.append(
            f"<tr><td>{esc(dev)}</td>"
            f"<td class='num'>{len(cur)}</td>"
            f"<td class='num'>{total_points(cur):g}</td>"
            f"<td class='num'>{total_points(cur) - remaining_points(cur):g}</td>"
            f"<td class='num'>{remaining_points(cur):g}</td>"
            f"<td class='num'>{open_prs}</td>"
            f"<td class='num'>{sum(1 for s in everything if s.blocked)}</td>"
            f"<td class='num'>{sum(len(s.risks) for s in everything)}</td></tr>"
        )
    h.append(
        f"<tr><th>Pod</th><th class='num'>{len(all_current)}</th>"
        f"<th class='num'>{total_points(all_current):g}</th>"
        f"<th class='num'>{total_points(all_current) - remaining_points(all_current):g}</th>"
        f"<th class='num'>{remaining_points(all_current):g}</th>"
        f"<th class='num'>{sum(1 for s in all_stories for pr in s.all_prs if pr.state == 'OPEN')}</th>"
        f"<th class='num'>{len(blocked)}</th>"
        f"<th class='num'>{len(all_risks)}</th></tr></table>"
    )

    # --- Pod burndown
    pod_actual = sprint_series(history, current_sprint, start, "remaining")
    pod_total = max(
        [total_points(all_current)]
        + [snap.get("pod", {}).get("total", 0)
           for snap in history["snapshots"].values() if snap.get("sprint") == current_sprint]
    )
    h.append("<h2>Burndown</h2><div class='charts'><div>")
    h.append(burndown_svg(f"Pod — {it_name}", length, pod_total, pod_actual))
    h.append("</div></div>")

    # --- Blockers & risks up front
    h.append("<h2>Blockers &amp; risks</h2>")
    if all_risks:
        h.append("<ul class='risks'>")
        for s, (tag, msg) in all_risks:
            h.append(f'<li class="risk"><strong>{esc(s.sid)}</strong> [{esc(tag)}] {esc(msg)}</li>')
        h.append("</ul>")
    else:
        h.append('<div class="muted">None detected today. 🎉</div>')

    # --- Per-dev sections
    for dev, by_sprint in per_dev.items():
        h.append(f"<h2>{esc(dev)}</h2>")
        cur = by_sprint.get(current_sprint, [])
        dev_total = max(
            [total_points(cur)]
            + [snap.get("devs", {}).get(dev, {}).get("total", 0)
               for snap in history["snapshots"].values()
               if snap.get("sprint") == current_sprint]
        )
        h.append("<div class='charts'><div>")
        h.append(burndown_svg(
            f"{dev} — {it_name}", length, dev_total,
            sprint_series(history, current_sprint, start, "remaining", dev=dev),
        ))
        h.append("</div></div>")
        for n in sprints:
            stories = by_sprint.get(n, [])
            if n != current_sprint:
                carry = [s for s in stories if not s.done]
                if not carry:
                    continue
                h.append(f"<h3>{esc(iteration_name(cfg, n))} — unfinished (spillover)</h3>")
                stories = carry
            else:
                h.append(f"<h3>{esc(iteration_name(cfg, n))} (current)</h3>")
                if not stories:
                    h.append('<div class="warn">No stories assigned in the current sprint.</div>')
            for s in stories:
                h.append(render_story(s, n, current_sprint))

    h.append(f'<div class="muted" style="margin-top:32px">Generated by pod-audit.py '
             f"on {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}</div>")
    return "\n".join(h)


# --------------------------------------------------------------------------
# PDF: print the HTML report via a headless Chromium-based browser, which
# renders the exact same CSS and SVG charts as the browser view.
# --------------------------------------------------------------------------

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser", "msedge",
]


def find_chrome():
    override = os.environ.get("POD_AUDIT_CHROME")
    candidates = ([override] if override else []) + CHROME_CANDIDATES
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_file():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


def html_to_pdf(html_path, pdf_path):
    chrome = find_chrome()
    if not chrome:
        warn("No Chromium-based browser found for PDF output; skipping.")
        warn("Install Chrome/Edge/Brave or set POD_AUDIT_CHROME=/path/to/browser.")
        return False
    rc, _, err = run(
        [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
        timeout=120,
    )
    if rc != 0 or not pdf_path.is_file():
        warn(f"PDF generation failed: {err.splitlines()[-1] if err else f'exit {rc}'}")
        return False
    return True


# --------------------------------------------------------------------------
# Terminal summary
# --------------------------------------------------------------------------

def print_summary(cfg, today, current_sprint, per_dev, report_path, pdf_path=None):
    start, _ = sprint_window(cfg, current_sprint)
    day_no = (today - start).days + 1
    print()
    print("=" * 62)
    print(f" {cfg['pod']['name']} — {iteration_name(cfg, current_sprint)}, "
          f"day {day_no} of {cfg['sprint']['length_days']}  ({today.isoformat()})")
    print("=" * 62)
    for dev, by_sprint in per_dev.items():
        cur = by_sprint.get(current_sprint, [])
        everything = [s for sp in by_sprint.values() for s in sp]
        done = total_points(cur) - remaining_points(cur)
        print(f"\n {dev}: {len(cur)} stories, {done:g}/{total_points(cur):g} pts accepted")
        for s in everything:
            flags = " ".join(f"[{t}]" for t, _ in s.risks)
            marker = "✔" if s.done else ("✖" if s.blocked else "·")
            print(f"   {marker} {s.sid} {s.state:<12} {s.completion:>3}%  {s.name[:48]}"
                  + (f"  {flags}" if flags else ""))
    risks = [(s, r) for sp in per_dev.values() for st in sp.values() for s in st for r in s.risks]
    print(f"\n Risks flagged: {len(risks)}")
    for s, (tag, msg) in risks:
        print(f"   ! {s.sid} [{tag}] {msg}")
    print(f"\n Report: {report_path}")
    if pdf_path:
        print(f" PDF:    {pdf_path}")
    print("=" * 62)


# --------------------------------------------------------------------------
# --check: diagnose why the audit might come back empty
# --------------------------------------------------------------------------

def run_check(cfg, today, current_sprint):
    problems = 0

    def ok(msg):
        print(f" [ok] {msg}")

    def bad(msg):
        nonlocal problems
        problems += 1
        print(f" [!!] {msg}")

    rally = Rally(cfg)
    me = rally.whoami()
    if me:
        ok(f"Rally auth works: you are {me.get('DisplayName')} "
           f"<{me.get('EmailAddress') or me.get('UserName')}>")
    else:
        bad("Rally accepted the API key but returned no user info")
    scope = ", ".join(f"{k}={v}" for k, v in rally.scope.items() if k != "projectScopeDown")
    print(f"      scope: {scope or 'default workspace/project for this API key'}")

    # Does the computed iteration actually exist under that name?
    it_name = iteration_name(cfg, current_sprint)
    its = rally.query("iteration", f'(Name = "{it_name}")', "Name,StartDate,EndDate,Project")
    if its:
        projs = sorted({
            (i.get("Project") or {}).get("_refObjectName", "?") for i in its
            if isinstance(i, dict)
        })
        ok(f"Iteration '{it_name}' exists in project(s): {', '.join(projs)}")
        w_start, _ = sprint_window(cfg, current_sprint)
        starts = sorted({str(i.get("StartDate"))[:10] for i in its})
        if w_start.isoformat() not in starts:
            bad(f"Rally says '{it_name}' starts {', '.join(starts)} but the config "
                f"computes {w_start} — adjust [sprint].anchor_start or [sprint].current")
    else:
        bad(f"No iteration named '{it_name}' — adjust [rally].iteration_name_format")
        around = rally.query(
            "iteration",
            f'((StartDate <= "{today}") AND (EndDate >= "{today}"))',
            "Name,StartDate,EndDate",
        )
        names = sorted({str(i.get("Name")) for i in around if isinstance(i, dict)})[:10]
        if names:
            print(f"      iterations covering today are named: {', '.join(repr(n) for n in names)}")
        else:
            print("      (no iteration in scope covers today at all — check workspace/project)")

    # Does each configured dev resolve to a Rally user, and do they have work?
    for dev in cfg["pod"]["devs"]:
        attr = "UserName" if "@" in dev else "DisplayName"
        users = rally.query("user", f'({attr} = "{dev}")', "UserName,DisplayName,EmailAddress")
        if users:
            u = users[0]
            count = len(rally.stories_for(dev, it_name))
            line = (f"{dev} -> {u.get('DisplayName')} "
                    f"<{u.get('EmailAddress') or u.get('UserName')}>: "
                    f"{count} item(s) in '{it_name}'")
            ok(line) if count else bad(line + " — owner matches but owns nothing there")
        else:
            bad(f"No Rally user with {attr} = '{dev}'")
            token = (dev.split("@")[0] if "@" in dev else dev.split()[-1]).strip()
            close = rally.query(
                "user", f'(DisplayName contains "{token}")',
                "UserName,DisplayName,EmailAddress",
            ) if token else []
            if close:
                sugg = "; ".join(
                    f"\"{c.get('DisplayName')}\" <{c.get('EmailAddress') or c.get('UserName')}>"
                    for c in close[:5] if isinstance(c, dict)
                )
                print(f"      did you mean: {sugg}")

    # Git repos
    if cfg["git"]["repos"]:
        index = RepoIndex(cfg)
        for path, meta in index.repos.items():
            ok(f"repo {path.name}: {len(meta['heads'])} branch(es) on "
               f"{index.remote} (default: {meta['default']})")
        if not index.repos:
            bad("None of the [git].repos entries is a usable git repo")
    else:
        print(" [--] No [git].repos configured; branch/PR reporting will be skipped")

    print()
    if problems:
        print(f"{problems} problem(s) found — fix the config and re-run --check.")
        sys.exit(1)
    print("All checks passed.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(add_help=True, description="Daily pod progress audit")
    ap.add_argument("--config", default=str(SCRIPT_DIR / "pod-audit.toml"))
    ap.add_argument("--date", help="Pretend today is YYYY-MM-DD")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--no-pdf", action="store_true", help="Skip the PDF copy of the report")
    ap.add_argument("--show-sprints", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="Diagnose the config against Rally (auth, iteration names, dev names)")
    ap.add_argument("--open", action="store_true", help="Open the report in a browser")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.date:
        try:
            today = dt.date.fromisoformat(args.date)
        except ValueError:
            die(f"--date must be YYYY-MM-DD, got {args.date!r}")
    else:
        today = dt.date.today()

    current_sprint = sprint_for_date(cfg, today)
    sprints = list(range(current_sprint - cfg["sprint"]["previous"], current_sprint + 1))

    if args.show_sprints:
        for n in sprints:
            s, e = sprint_window(cfg, n)
            tag = "  <- current" if n == current_sprint else ""
            print(f"{iteration_name(cfg, n)}: {s.isoformat()} .. {e.isoformat()}{tag}")
        return

    if args.check:
        run_check(cfg, today, current_sprint)
        return

    start, _ = sprint_window(cfg, current_sprint)
    elapsed_pct = 100 * (today - start).days / cfg["sprint"]["length_days"]

    rally = Rally(cfg)
    index = None
    if not args.no_git:
        info("Indexing branches in configured repos...")
        index = RepoIndex(cfg)

    history_path = (cfg["_dir"] / cfg["report"]["history_file"]).expanduser()
    history = load_history(history_path)
    prev_snap = None
    for date_str, snap in sorted(history["snapshots"].items(), reverse=True):
        if date_str < today.isoformat():
            prev_snap = snap
            break

    ai_enabled = cfg["ai"]["enabled"] and not args.no_ai

    per_dev = {}
    for dev in cfg["pod"]["devs"]:
        per_dev[dev] = {}
        for n in sprints:
            it = iteration_name(cfg, n)
            items = rally.stories_for(dev, it)
            info(f"Rally: {dev} in {it}: {len(items)} item(s)")
            stories = [build_story(rally, kind, raw) for kind, raw in items]
            stories.sort(key=lambda s: s.sid)
            for story in stories:
                # Enrichment failures (a broken repo, gh hiccup, AI parse) must
                # not sink the whole report — warn and carry on with less data.
                try:
                    if index:
                        for path, branch, default, url in index.find(story.sid):
                            b = branch_stats(index, path, branch, default)
                            b.prs = find_prs(cfg, path, url, branch)
                            story.branches.append(b)
                    if ai_enabled and story.acs and story.branches and not story.done:
                        path, branch, default, _ = index.find(story.sid)[0]
                        info(f"AI-scoring ACs for {story.sid}...")
                        diff = branch_diff_text(index, path, branch, default,
                                                cfg["ai"]["max_diff_chars"])
                        scores = ai_score_acs(story, diff, cfg)
                        if scores:
                            story.ac_scores = scores
                except Exception as e:
                    warn(f"Branch/PR enrichment failed for {story.sid}: "
                         f"{e.__class__.__name__}: {e}")
                prev_state = (prev_snap or {}).get("stories", {}).get(story.sid)
                if not isinstance(prev_state, dict):
                    prev_state = None
                assess_risks(story, n, current_sprint, elapsed_pct, cfg, prev_state, today)
            per_dev[dev][n] = stories

    if not any(st for sp in per_dev.values() for st in sp.values()):
        warn("No Rally stories or defects were found for ANY dev in ANY sprint.")
        warn("Likely causes: iteration names don't match [rally].iteration_name_format,")
        warn("dev names don't exactly match Rally display names, or wrong workspace/project.")
        warn(f"Run  {sys.argv[0]} --check  to diagnose.")

    # Snapshot for burndown, then render.
    record_snapshot(history, today, current_sprint,
                    {dev: sp.get(current_sprint, []) for dev, sp in per_dev.items()})
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "w") as f:
            json.dump(history, f, indent=1)
    except OSError as e:
        die(f"Cannot write history file {history_path}: {e}")

    html_out = render_report(cfg, today, current_sprint, sprints, per_dev, history)
    slug = re.sub(r"[^a-z0-9]+", "-", cfg["pod"]["name"].lower()).strip("-") or "pod"
    try:
        out_dir = (cfg["_dir"] / cfg["report"]["output_dir"]).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"{slug}-audit-{today.isoformat()}.html"
        report_path.write_text("<!doctype html>\n<meta charset='utf-8'>\n" + html_out)
    except OSError as e:
        die(f"Cannot write report to {cfg['report']['output_dir']}: {e}")

    pdf_path = None
    if cfg["report"]["pdf"] and not args.no_pdf:
        candidate = report_path.with_suffix(".pdf")
        if html_to_pdf(report_path, candidate):
            pdf_path = candidate

    print_summary(cfg, today, current_sprint, per_dev, report_path, pdf_path)
    if args.open:
        try:
            webbrowser.open(report_path.as_uri())
        except Exception as e:
            warn(f"Could not open the report in a browser: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        if os.environ.get("POD_AUDIT_DEBUG"):
            raise
        die(f"Unexpected error: {e.__class__.__name__}: {e} "
            f"(set POD_AUDIT_DEBUG=1 for a full traceback)")
