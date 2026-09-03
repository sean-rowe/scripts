#!/usr/bin/env python3
#
# pod-audit.py
#
# Daily morning report for pod leadership (tech lead / PM / PO / scrum
# master — not the devs). For every dev in the config it pulls their Rally
# stories for the current sprint (computed from a config anchor date) and
# the previous N sprints, cross-references git branches and PRs, and builds
# an HTML + PDF report containing:
#
#   - what changed since the last report, and a "conversations to have
#     today" agenda routed to the right owner (TL / PO / SM)
#   - a pod summary with green/yellow/red lateness per story, and a
#     promotion matrix per story across [targets].branches
#     (develop / release-N / qa / ...): merged, PR open, or missing
#   - reviews & CI: PRs waiting on first review (and on whom), failing and
#     pending checks, oversized PRs, review-load distribution
#   - blocked stories with age, the PO acceptance queue with age, scope
#     added after sprint start, WIP-limit breaches, data-hygiene problems
#   - burndown charts (reconstructed from Rally AcceptedDate) for the pod
#     and per dev, for the current and previous sprints
#   - per story: acceptance criteria with completion % each and whether
#     each is tested behaviorally, test style (behavioral vs
#     implementation), coverage adequacy, and an architecture note — all
#     judged by the `claude` CLI against the branch diff when [ai].enabled,
#     with a task/PR-based heuristic otherwise
#   - per dev: a coaching scorecard (delivery/quality/communication/
#     collaboration, early at-risk flag, strengths, suggestions),
#     outstanding questions/requests mined from configured sqlite
#     transcript/chat/email databases, and their recent mentions
#   - open action items from a configured markdown checklist
#
# Every optional source degrades gracefully: an unconfigured section
# reports itself as such instead of failing the run. Each run snapshots
# state into a history file, which powers the aging, churn, and
# no-movement detections and keeps coaching scores stable day to day.
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
#   --lookup <id>       Show how Rally names one known story's owner,
#                       iteration, project and workspace — paste-ready values
#                       for the config when --check alone doesn't explain an
#                       empty report.
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
import sqlite3
import subprocess
import sys
import tempfile
import time
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
    "pod": {"name": "Pod", "devs": [], "wip_limit": 2},
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
    "ai": {"enabled": False, "max_diff_chars": 12000, "coaching": True},
    "risks": {
        "stale_pr_days": 3,
        "max_pr_comments": 10,
        "no_branch_after_pct": 50,
        "stale_story_days": 3,
        "review_wait_days": 1,
        "blocked_escalate_days": 3,
        "acceptance_wait_days": 2,
        "big_pr_lines": 400,
        "yellow_gap_pct": 10,
        "red_gap_pct": 30,
    },
    # Promotion targets checked per story; {n} = current sprint, {prev} = n-1.
    "targets": {"branches": ["develop", "release-{prev}", "release-{n}", "qa", "qa1"]},
    # Optional extra data sources; sections degrade gracefully when empty.
    "sources": {
        "sqlite": [],                    # transcript/chat/email DBs to search
        "action_items": "",              # markdown checklist file ('- [ ] item')
        "max_snippets_per_dev": 6,
    },
    "capacity": {"out": []},             # devs out today (names as in [pod].devs)
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
        cfg["rally"]["iteration_name_format"].format(n=1, year=2026)
    except (KeyError, IndexError, ValueError):
        die(
            f"[rally].iteration_name_format is invalid: "
            f"{cfg['rally']['iteration_name_format']!r} "
            "(use {n} for the sprint number, {year} for the sprint's start year)"
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
    start, _ = sprint_window(cfg, n)
    return cfg["rally"]["iteration_name_format"].format(n=n, year=start.year)


# --------------------------------------------------------------------------
# Rally API (WSAPI v2.0)
# --------------------------------------------------------------------------

STORY_FETCH = (
    "FormattedID,Name,ObjectID,ScheduleState,PlanEstimate,ToDo,"
    "TaskEstimateTotal,TaskRemainingTotal,Blocked,BlockedReason,Description,"
    "Discussion,LastUpdateDate,AcceptedDate,InProgressDate,Iteration,Owner,DisplayName"
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
    base: str = ""                       # branch the PR targets
    author: str = ""
    merged: str = ""                     # date merged, "" if not
    additions: int = 0
    deletions: int = 0
    files: int = 0
    checks_total: int = 0
    checks_failed: int = 0
    checks_pending: int = 0
    reviewers: list = field(default_factory=list)   # people who reviewed
    awaiting: list = field(default_factory=list)    # requested, not yet reviewed


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
    accepted: dt.date | None
    in_progress: dt.date | None
    acs: list
    owner: str = ""     # Rally Owner display name ("" = unassigned in Rally)
    ac_scores: list = field(default_factory=list)   # [(percent, note)] per AC
    branches: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    # AI analysis (filled when [ai].enabled)
    ac_tested: list = field(default_factory=list)   # bool per AC: tested behaviorally
    tests_style: str = ""       # behavioral / implementation / mixed / none
    coverage_note: str = ""
    arch_note: str = ""
    ai_concerns: list = field(default_factory=list)
    lateness: str = ""          # green / yellow / red

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


def _rally_date(v):
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return None


def build_story(rally, kind, raw):
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
        last_update=_rally_date(raw.get("LastUpdateDate")),
        accepted=_rally_date(raw.get("AcceptedDate")),
        in_progress=_rally_date(raw.get("InProgressDate")),
        acs=parse_acceptance_criteria(raw, rally.ac_field),
        owner=str((raw.get("Owner") or {}).get("_refObjectName") or ""),
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
             "title,state,url,isDraft,createdAt,updatedAt,reviewDecision,comments,"
             "reviews,baseRefName,author,mergedAt,additions,deletions,changedFiles,"
             "statusCheckRollup,reviewRequests"],
            cwd=repo_path,
        )
        if rc != 0:
            continue
        try:
            d = json.loads(detail)
        except json.JSONDecodeError:
            continue
        reviews = d.get("reviews") or []
        comments = len(d.get("comments") or []) + len(
            [r for r in reviews if (r.get("body") or "").strip()]
        )
        reviewers = []
        for r in reviews:
            login = ((r.get("author") or {}).get("login") or "").strip()
            if login and login not in reviewers:
                reviewers.append(login)
        awaiting = []
        for rr in d.get("reviewRequests") or []:
            who = rr.get("login") or rr.get("slug") or rr.get("name") or ""
            if who and who not in awaiting:
                awaiting.append(who)
        ok_states = {"SUCCESS", "NEUTRAL", "SKIPPED"}
        bad_states = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
        total = failed = pending = 0
        for c in d.get("statusCheckRollup") or []:
            if not isinstance(c, dict):
                continue
            total += 1
            val = (c.get("conclusion") or c.get("state") or "").upper()
            if val in bad_states:
                failed += 1
            elif val not in ok_states:
                pending += 1
        prs.append(PR(
            title=d.get("title", ""),
            state=d.get("state", ""),
            url=d.get("url", ""),
            comments=comments,
            review=d.get("reviewDecision") or "",
            draft=bool(d.get("isDraft")),
            created=(d.get("createdAt") or "")[:10],
            updated=(d.get("updatedAt") or "")[:10],
            base=d.get("baseRefName") or "",
            author=((d.get("author") or {}).get("login") or ""),
            merged=(d.get("mergedAt") or "")[:10],
            additions=d.get("additions") or 0,
            deletions=d.get("deletions") or 0,
            files=d.get("changedFiles") or 0,
            checks_total=total,
            checks_failed=failed,
            checks_pending=pending,
            reviewers=reviewers,
            awaiting=awaiting,
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
            participants = d.get("participants", [])
            approvals = sum(1 for p in participants if p.get("approved"))
            reviewers = [
                ((p.get("user") or {}).get("display_name") or "")
                for p in participants if p.get("approved")
            ]
            awaiting = [
                ((p.get("user") or {}).get("display_name") or "")
                for p in participants
                if p.get("role") == "REVIEWER" and not p.get("approved")
            ]
            prs.append(PR(
                title=d.get("title", ""),
                state=d.get("state", ""),
                url=d.get("links", {}).get("html", {}).get("href", ""),
                comments=d.get("comment_count", 0),
                review=f"{approvals} approval(s)" if approvals else "",
                draft=bool(d.get("draft")),
                created=(d.get("created_on") or "")[:10],
                updated=(d.get("updated_on") or "")[:10],
                base=((d.get("destination") or {}).get("branch") or {}).get("name", ""),
                author=((d.get("author") or {}).get("display_name") or ""),
                merged=(d.get("updated_on") or "")[:10] if d.get("state") == "MERGED" else "",
                reviewers=[r for r in reviewers if r],
                awaiting=[a for a in awaiting if a],
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


def _ai_json(prompt, label, timeout=300):
    """Run `claude -p` and extract the first JSON object/array from the reply."""
    rc, out, err = run(["claude", "-p"], input_=prompt, timeout=timeout)
    if rc != 0:
        warn(f"claude CLI failed for {label}: {err.splitlines()[0] if err else rc}")
        return None
    m = re.search(r"\{.*\}|\[.*\]", out, re.DOTALL)
    if not m:
        warn(f"claude returned no JSON for {label}")
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        warn(f"claude returned unparseable JSON for {label}")
        return None


def ai_story_analysis(story, diff_text, cfg):
    """One claude pass per story: per-AC coded %, whether each AC is tested
    behaviorally, test style, coverage adequacy, and an architecture note.
    Fills the story's AI fields in place; silently leaves heuristics on failure."""
    ac_list = "\n".join(f"{i + 1}. {ac}" for i, ac in enumerate(story.acs))
    prompt = f"""You are a tech lead auditing progress on a story. Judge ONLY from the code diff below.

Story: {story.sid} — {story.name}

Acceptance criteria:
{ac_list}

Code diff of the story branch vs the mainline (includes test changes):
```
{diff_text}
```

Respond with ONLY this JSON object:
{{
 "acs": [{{"index": 1, "percent": 0-100, "note": "evidence, max 12 words", "tested_behaviorally": true/false}}, ...],
 "tests_style": "behavioral" | "implementation" | "mixed" | "none",
 "coverage_adequate": "yes" | "no" | "partial",
 "coverage_note": "max 20 words",
 "architecture": "how the changes look architecturally, max 40 words; name concrete issues",
 "concerns": ["specific risk or smell, max 15 words each"]
}}
"tested_behaviorally" means a test exercises the AC through observable behavior (inputs/outputs, API, UI), not internals/mocks of the unit under test."""
    data = _ai_json(prompt, story.sid)
    if not isinstance(data, dict):
        return
    try:
        scores = {int(i["index"]): i for i in data.get("acs") or [] if isinstance(i, dict)}
        story.ac_scores = [
            (max(0, min(100, int(scores.get(i + 1, {}).get("percent", 0)))),
             str(scores.get(i + 1, {}).get("note", "")))
            for i in range(len(story.acs))
        ]
        story.ac_tested = [
            bool(scores.get(i + 1, {}).get("tested_behaviorally", False))
            for i in range(len(story.acs))
        ]
    except (ValueError, KeyError, TypeError):
        pass
    story.tests_style = str(data.get("tests_style") or "")
    cov = str(data.get("coverage_adequate") or "")
    note = str(data.get("coverage_note") or "")
    story.coverage_note = f"{cov}{' — ' + note if note else ''}" if cov else ""
    story.arch_note = str(data.get("architecture") or "")
    story.ai_concerns = [str(c) for c in data.get("concerns") or [] if str(c).strip()][:5]


def ai_dev_coaching(dev, facts, prev_card, cfg):
    """One claude pass per dev: scorecard, strengths, coaching, early risk
    call. prev_card (yesterday's) is included so scores stay stable unless
    the evidence moves. Returns the scorecard dict or None."""
    prev = json.dumps(prev_card, indent=1) if prev_card else "none (first run)"
    prompt = f"""You are coaching-notes assistant for a tech lead. This report is read by leadership only, never by the dev. Be direct and specific; praise what is genuinely good.

Developer: {dev}

Today's facts (stories, progress, PRs, reviews given, recent communications):
{json.dumps(facts, indent=1, default=str)}

Yesterday's scorecard (keep scores stable unless today's evidence justifies a change):
{prev}

Respond with ONLY this JSON object:
{{
 "scores": {{"delivery": 1-5, "quality": 1-5, "communication": 1-5, "collaboration": 1-5}},
 "strengths": ["what they are getting right, specific, max 20 words each"],
 "coaching": ["concrete suggestion the tech lead could raise, max 25 words each"],
 "risk": "none" | "watch" | "action",
 "risk_reason": "if watch/action: why they may not finish in time, max 30 words",
 "questions_requests": ["outstanding question or request FROM this dev found in the communications, max 20 words each"]
}}"""
    data = _ai_json(prompt, f"coaching:{dev}")
    if not isinstance(data, dict) or not isinstance(data.get("scores"), dict):
        return None
    return data


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
# Leadership analysis: promotion matrix, lateness, reviews, aging, agenda
# --------------------------------------------------------------------------

def resolve_targets(cfg, current_sprint):
    out = []
    for t in cfg["targets"]["branches"]:
        try:
            out.append(t.format(n=current_sprint, prev=current_sprint - 1))
        except (KeyError, IndexError, ValueError):
            warn(f"Bad [targets].branches entry {t!r}; skipping")
    return out


def promotion_matrix(story, targets):
    """{target: 'merged'|'open'|'declined'|'-'} judged from the story's PRs."""
    row = {}
    for t in targets:
        status = "-"
        for pr in story.all_prs:
            if pr.base != t:
                continue
            if pr.state == "MERGED":
                status = "merged"
                break
            if pr.state == "OPEN" and status == "-":
                status = "open"
            elif pr.state in ("DECLINED", "CLOSED") and status == "-":
                status = "declined"
        row[t] = status
    return row


def score_lateness(story, sprint_n, current_sprint, elapsed_pct, cfg):
    r = cfg["risks"]
    if story.done:
        story.lateness = "green"
    elif sprint_n < current_sprint:
        story.lateness = "red"          # spillover is late by definition
    else:
        gap = elapsed_pct - story.completion
        if gap >= r["red_gap_pct"]:
            story.lateness = "red"
        elif gap >= r["yellow_gap_pct"]:
            story.lateness = "yellow"
        else:
            story.lateness = "green"
    return story.lateness


def review_stats(all_stories, today, cfg):
    """(waiting, load): open PRs awaiting review with wait days, and how many
    reviews each person has given across the pod's PRs."""
    waiting = []
    load = {}
    seen = set()
    for s in all_stories:
        for pr in s.all_prs:
            if pr.url in seen:
                continue
            seen.add(pr.url)
            for who in pr.reviewers:
                load[who] = load.get(who, 0) + 1
            if pr.state == "OPEN" and not pr.draft and not pr.reviewers:
                try:
                    wait = (today - dt.date.fromisoformat(pr.created)).days
                except ValueError:
                    wait = 0
                waiting.append((s, pr, wait))
    waiting.sort(key=lambda x: -x[2])
    return waiting, load


def first_snapshot_date(history, sid, pred, before=None):
    """Earliest snapshot date whose per-story record satisfies pred."""
    for date_str, snap in sorted(history["snapshots"].items()):
        if before and date_str >= before:
            break
        rec = (snap.get("stories") or {}).get(sid)
        if isinstance(rec, dict) and pred(rec):
            try:
                return dt.date.fromisoformat(date_str)
            except ValueError:
                continue
    return None


def aging_queues(all_stories, history, today, cfg):
    """(blocked_aging, acceptance_queue) with day counts from history."""
    r = cfg["risks"]
    blocked, accept = [], []
    for s in all_stories:
        if s.blocked:
            since = first_snapshot_date(history, s.sid, lambda rec: rec.get("blocked"))
            days = (today - since).days if since else 0
            blocked.append((s, days))
        if s.state == "Completed":
            since = first_snapshot_date(history, s.sid, lambda rec: rec.get("state") == "Completed")
            days = (today - since).days if since else 0
            accept.append((s, days))
    blocked.sort(key=lambda x: -x[1])
    accept.sort(key=lambda x: -x[1])
    return blocked, accept


def scope_churn(history, current_sprint, per_dev_current, today):
    """Stories/points added after the sprint's first recorded day."""
    baseline_date = None
    baseline = {}
    for date_str, snap in sorted(history["snapshots"].items()):
        if snap.get("sprint") == current_sprint and snap.get("stories"):
            baseline_date = date_str
            baseline = snap["stories"]
            break
    # No baseline before today = first meaningful run; nothing to compare.
    if baseline_date is None or baseline_date >= today.isoformat():
        return None
    added = []
    for stories in per_dev_current.values():
        for s in stories:
            if s.sid not in baseline:
                added.append(s)
    base_points = sum((rec.get("points") or 0) for rec in baseline.values()
                     if isinstance(rec, dict))
    return {"baseline_date": baseline_date, "added": added, "base_points": base_points}


def yesterday_digest(history, all_stories, today):
    """What changed since the previous snapshot: state moves and PR activity."""
    prev = None
    for date_str, snap in sorted(history["snapshots"].items(), reverse=True):
        if date_str < today.isoformat():
            prev = snap
            break
    events = []
    prev_stories = (prev or {}).get("stories") or {}
    for s in all_stories:
        old = prev_stories.get(s.sid) or {}
        old_state = old.get("state")
        if old_state and old_state != s.state:
            events.append(f"{s.sid} moved {old_state} -> {s.state}")
        elif not old_state and prev is not None:
            events.append(f"{s.sid} appeared ({s.state})")
    cutoff = (today - dt.timedelta(days=3)).isoformat()
    for s in all_stories:
        for pr in s.all_prs:
            if pr.merged and pr.merged >= cutoff:
                events.append(f"PR merged into {pr.base}: {s.sid} — {pr.title}")
            elif pr.state == "OPEN" and pr.created >= cutoff:
                events.append(f"PR opened against {pr.base}: {s.sid} — {pr.title}")
    return events


def wip_and_hygiene(per_dev, current_sprint, cfg):
    """(wip_flags, hygiene): WIP-limit breaches and data-quality problems."""
    wip_flags = []
    hygiene = []
    for dev, by_sprint in per_dev.items():
        active = [s for sp in by_sprint.values() for s in sp if s.state == "In-Progress"]
        if len(active) > cfg["pod"]["wip_limit"]:
            wip_flags.append((dev, active))
        for sp_n, stories in by_sprint.items():
            for s in stories:
                if s.points is None:
                    hygiene.append((dev, s, "no point estimate"))
                if not s.acs:
                    hygiene.append((dev, s, "no acceptance criteria"))
                if s.state == "In-Progress" and not s.branches:
                    hygiene.append((dev, s, "In-Progress but no branch found"))
    return wip_flags, hygiene


def build_agenda(per_dev, blocked_aging, accept_queue, waiting_reviews, wip_flags, cfg, out_today):
    """The 'conversations to have today' list.

    Each item records who should lead it (role), who it is WITH (dev) and
    which story it is about (sid), because the report groups by developer
    then story — you have one conversation with someone, not one per flag.
    """
    r = cfg["risks"]
    agenda = []

    def item(role, dev, sid, text):
        agenda.append({"role": role, "dev": dev or "", "sid": sid or "", "text": text})

    for s, days in blocked_aging:
        if days >= r["blocked_escalate_days"]:
            item("Scrum master", s.owner, s.sid,
                 f"Escalate — blocked {days}d: {s.blocked_reason or 'no reason recorded'}")
    for s, pr, wait in waiting_reviews:
        if wait >= r["review_wait_days"]:
            who = ", ".join(pr.awaiting) or "no reviewer assigned"
            item("Tech lead", s.owner, s.sid,
                 f"Get it reviewed — PR open {wait}d ({who})")
    for dev, by_sprint in per_dev.items():
        if dev in out_today:
            continue
        reds = [s for sp in by_sprint.values() for s in sp
                if s.lateness == "red" and not s.done]
        for s in reds:
            item("Tech lead", dev, s.sid, "Flagged red — agree a plan or cut scope")
    for dev, active in wip_flags:
        item("Scrum master", dev, "",
             f"{len(active)} stories In-Progress at once — help them finish one first")
    return agenda


# --------------------------------------------------------------------------
# Comms mining: search sqlite transcript/chat/email pools for stories & devs
# --------------------------------------------------------------------------

def _text_columns(con, table):
    cols = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    text_cols = [c[1] for c in cols
                 if not c[2] or any(t in (c[2] or "").upper() for t in ("CHAR", "TEXT", "CLOB"))]
    date_cols = [c[1] for c in cols
                 if re.search(r"date|time|created|updated|ts", c[1], re.IGNORECASE)]
    return text_cols, (date_cols[0] if date_cols else None)


def search_comms(cfg, terms, limit):
    """Search every configured sqlite DB's text columns for the given terms.
    Returns [{source, table, date, snippet}] newest-ish first. Schema-agnostic:
    tables and text columns are discovered, not assumed."""
    results = []
    terms = [t for t in terms if t and len(t) >= 3]
    if not terms:
        return results
    for raw_path in cfg["sources"]["sqlite"]:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            warn(f"[sources].sqlite entry not found: {path}")
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            for table in tables:
                try:
                    text_cols, date_col = _text_columns(con, table)
                    if not text_cols:
                        continue
                    where = " OR ".join(
                        f'"{c}" LIKE ?' for c in text_cols for _ in terms
                    )
                    params = [f"%{t}%" for _ in text_cols for t in terms]
                    order = f'ORDER BY "{date_col}" DESC' if date_col else ""
                    rows = con.execute(
                        f'SELECT * FROM "{table}" WHERE {where} {order} LIMIT ?',
                        params + [limit],
                    ).fetchall()
                    for row in rows:
                        best = ""
                        for c in text_cols:
                            v = str(row[c] or "")
                            if any(t.lower() in v.lower() for t in terms) and len(v) > len(best):
                                best = v
                        if not best:
                            continue
                        for t in terms:
                            i = best.lower().find(t.lower())
                            if i >= 0:
                                best = best[max(0, i - 120):i + 240]
                                break
                        results.append({
                            "source": path.stem,
                            "table": table,
                            "date": str(row[date_col])[:16] if date_col else "",
                            "snippet": " ".join(best.split()),
                        })
                except sqlite3.Error:
                    continue
            con.close()
        except sqlite3.Error as e:
            warn(f"Cannot search {path}: {e}")
    results.sort(key=lambda r: r["date"], reverse=True)
    return results[:limit]


def load_action_items(cfg):
    """Open '- [ ]' items from the configured markdown checklist, or None if
    not configured."""
    path_cfg = cfg["sources"]["action_items"]
    if not path_cfg:
        return None
    path = Path(path_cfg)
    if not path.is_absolute():
        path = cfg["_dir"] / path
    path = path.expanduser()
    if not path.is_file():
        return {"path": path, "open": [], "missing": True}
    items = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            m = re.match(r"\s*[-*]\s*\[( |x|X)\]\s*(.+)", line)
            if m and m.group(1) == " ":
                items.append(m.group(2).strip())
    except OSError as e:
        warn(f"Cannot read action items file {path}: {e}")
    return {"path": path, "open": items, "missing": False}


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
            snap["stories"][s.sid] = {
                "todo": s.todo_hours, "state": s.state,
                "points": s.points, "blocked": s.blocked,
            }
    snap["pod"] = {
        "remaining": sum(d["remaining"] for d in snap["devs"].values()),
        "total": sum(d["total"] for d in snap["devs"].values()),
    }
    history["snapshots"][today.isoformat()] = snap
    return snap


def burndown_series(stories, start, cap):
    """[(day_index, remaining_points)] for each sprint day up to `cap`,
    reconstructed from Rally AcceptedDate: remaining on a day = planned
    total minus the points of everything accepted by that day. This gives
    full historical curves without needing the script to have run daily."""
    total = total_points(stories)
    out = []
    for d in range((cap - start).days + 1):
        day = start + dt.timedelta(days=d)
        done = sum(
            s.points or 0 for s in stories
            if (s.accepted and s.accepted <= day) or (s.done and not s.accepted)
        )
        out.append((d, round(total - done, 2)))
    return out


# --------------------------------------------------------------------------
# Charts (inline SVG)
# --------------------------------------------------------------------------

def burndown_svg(title, length_days, total, actual, today_day=None, width=640, height=300):
    """A wider viewBox so labels land at ~8pt printed instead of ~5pt; the
    remaining figure is labelled in words; the post-today region is shaded so
    a flat line reads as flat; the title lives in HTML, not inside the SVG,
    so it can wrap and stays selectable."""
    ml, mr, mt, mb = 56, 24, 40, 44
    pw, ph = width - ml - mr, height - mt - mb
    y_max = max([total] + [v for _, v in actual] + [1])
    last_day = max(length_days - 1, 1)

    def x(day):
        return ml + pw * min(max(day, 0), last_day) / last_day

    def y(val):
        return mt + ph * (1 - val / y_max)

    p = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
         f'aria-label="{esc(title)}">']

    steps = 4
    for i in range(steps + 1):
        val = y_max * i / steps
        yy = y(val)
        stroke = "#B7BCC2" if i == 0 else "#E3E7EB"
        p.append(f'<line x1="{ml}" y1="{yy:.1f}" x2="{width - mr}" y2="{yy:.1f}" '
                 f'stroke="{stroke}" stroke-width="1"/>')
        if i in (0, steps // 2, steps):
            p.append(f'<text x="{ml - 10}" y="{yy + 4:.1f}" text-anchor="end" '
                     f'fill="#6B6C68" font-family="Geist Mono, monospace" '
                     f'font-size="15">{val:g}</text>')

    for d in (0, 4, 8, length_days - 1):
        if 0 <= d <= last_day:
            p.append(f'<text x="{x(d):.1f}" y="{height - 14}" text-anchor="middle" '
                     f'fill="#6B6C68" font-family="Geist Mono, monospace" '
                     f'font-size="15">{d + 1}</text>')

    p.append(f'<line x1="{x(0):.1f}" y1="{y(total):.1f}" x2="{x(last_day):.1f}" '
             f'y2="{y(0):.1f}" stroke="#6B6C68" stroke-width="1.6" stroke-dasharray="6 5"/>')

    show_today = today_day is not None and 0 <= today_day <= last_day
    if show_today:
        tx = x(today_day)
        p.append(f'<rect x="{tx:.1f}" y="{mt - 6}" width="{width - mr - tx:.1f}" '
                 f'height="{ph + 12}" fill="#14181D" opacity="0.035"/>')
        p.append(f'<line x1="{tx:.1f}" y1="{mt - 6}" x2="{tx:.1f}" y2="{mt + ph + 6}" '
                 f'stroke="#8A5A12" stroke-width="1.4" stroke-dasharray="3 3"/>')
        p.append(f'<text x="{tx + 6:.1f}" y="{mt - 10}" fill="#8A5A12" '
                 f'font-family="Geist Mono, monospace" font-size="14" '
                 f'font-weight="600">today</text>')

    if actual:
        line = list(actual)
        if line[0][0] > 0:
            line.insert(0, (0, total))
        remaining = actual[-1][1]
        ideal_now = total * (1 - (today_day if today_day is not None else last_day) / last_day)
        colour = "#2F6B4F" if remaining <= ideal_now else \
                 ("#9B3327" if remaining > ideal_now + total * 0.3 else "#2B4E7E")
        pts = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in line)
        p.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="3"/>')
        dots = [pt for i, pt in enumerate(actual)
                if i in (0, len(actual) - 1) or actual[i][1] != actual[i - 1][1]]
        for d, v in dots:
            p.append(f'<circle cx="{x(d):.1f}" cy="{y(v):.1f}" r="4.5" fill="{colour}"/>')
        lx, ly = x(actual[-1][0]) + 12, max(y(remaining) - 6, mt + 12)
        anchor = "start"
        if lx > width - mr - 70:
            lx, anchor = x(actual[-1][0]) - 12, "end"
        p.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" fill="{colour}" '
                 f'font-family="Geist Mono, monospace" font-size="17" '
                 f'font-weight="600">{remaining:g} left</text>')

    p.append("</svg>")
    return "".join(p)


def _chart_block(title, caption, svg):
    return (f'<div><div class="ctitle">{title}</div>{svg}'
            + (f'<div class="ccap">{caption}</div>' if caption else "")
            + "</div>")


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

CSS = """
@page { size: A4; margin: 13mm 14mm 13mm; }

:root {
  --paper:#FBFAF7; --ink:#14181D; --ink2:#3E4148; --muted:#5C5F66;
  --faint:#6B6C68; --rule:#DCE0E5; --rule2:#B7BCC2; --panel:#F1F3F5;
  --ok:#2F6B4F; --warnc:#8A5A12; --bad:#9B3327; --accent:#2B4E7E;
  --navy:#1E3A5F; --gold:#F2B33D;
  --tintb:#E9EEF6; --tintg:#E5EFE8; --tinta:#F8EFDB; --tintr:#F9E8E3;
  --serif:"Manrope","Helvetica Neue",Helvetica,sans-serif;
  --sans:"Instrument Sans","Helvetica Neue",Helvetica,Arial,sans-serif;
  --mono:"Geist Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
}

* { box-sizing:border-box; }
html { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
body {
  margin:0 auto; max-width:186mm; padding:14mm 0;
  background:var(--paper); color:var(--ink);
  font:400 9.5pt/1.5 var(--sans);
  text-wrap:pretty;
}
@media print { body { max-width:none; padding:0; } }

/* Running header / footer frame. thead and tfoot repeat on every printed
   page, which keeps body text from sliding under them. */
table.frame { width:100%; table-layout:fixed; border-collapse:collapse; }
.twocol > *, .flags > *, .charts > *, .tiles > * { min-width:0; }
.wrapscroll { max-width:100%; overflow:hidden; }
table.frame > thead > tr > td,
table.frame > tbody > tr > td,
table.frame > tfoot > tr > td { padding:0; border:0; }
.runhead {
  display:flex; justify-content:space-between; align-items:baseline;
  font:600 8pt/1 var(--mono); letter-spacing:.09em; text-transform:uppercase;
  color:var(--navy); border-bottom:2pt solid var(--accent);
  padding-bottom:5pt; margin-bottom:11pt;
}
.runfoot {
  display:flex; justify-content:space-between; align-items:baseline;
  font:400 7.5pt/1 var(--mono); color:var(--faint);
  border-top:.5pt solid var(--rule); padding-top:5pt; margin-top:11pt;
}

/* Headings ------------------------------------------------------------ */
.masthead { background:var(--navy); padding:16pt 18pt 17pt; margin:0 0 22pt; break-inside:avoid; }
.masthead h1 { color:#FFFFFF; margin:0 0 7pt; }
.masthead .sub { color:#B9C9DD; margin:0 0 12pt; font-size:9.5pt; }
.masthead .elapsed { margin:0; }
.masthead .elapsed .track { background:#3A5C86; height:5pt; border-radius:0; }
.masthead .elapsed .fill { background:var(--gold); height:5pt; }
.masthead .elapsed .pct { color:var(--gold); }
h1 { font:700 23pt/1.04 var(--serif); letter-spacing:-.02em; margin:0 0 6pt; }
h2 {
  font:700 12.5pt/1.2 var(--serif); letter-spacing:-.015em; margin:30pt 0 3pt;
  color:var(--navy); padding-bottom:5pt; border-bottom:2pt solid var(--accent);
  break-after:avoid;
}
h2:first-of-type { margin-top:10pt; }
h3 { font:800 18pt/1.08 var(--serif); letter-spacing:-.025em; margin:0; }
.seclab {
  font:600 8pt/1 var(--mono); letter-spacing:.09em; text-transform:uppercase;
  color:var(--navy); background:var(--tintb);
  padding:6pt 8pt; margin:26pt 0 14pt; break-after:avoid;
}
.fieldlab {
  font:600 7.5pt/1 var(--mono); letter-spacing:.07em; text-transform:uppercase;
  color:var(--muted); margin-bottom:5pt;
}
.sub { font:400 10pt/1.45 var(--sans); color:var(--muted); margin:0 0 4pt; }
.note { font:400 9pt/1.4 var(--sans); color:var(--faint); margin:7pt 0 10pt; }
.mono { font-family:var(--mono); }
.muted { color:var(--faint); } .risk { color:var(--bad); }
.warn { color:var(--warnc); } .good { color:var(--ok); }
a { color:var(--accent); text-decoration:none; }
a:hover { color:var(--bad); }

/* Owner attribution — every flagged line says whose it is. */
.who {
  display:inline-block; font:600 7pt/1 var(--mono); letter-spacing:.05em;
  text-transform:uppercase; color:var(--muted); background:#FFFFFF;
  border:.5pt solid var(--rule2); padding:2pt 4pt; margin-left:3pt;
  white-space:nowrap; vertical-align:1pt;
}
.who.unassigned { color:var(--bad); border-color:var(--bad); }

/* Page-1 furniture ---------------------------------------------------- */
.elapsed { display:flex; align-items:center; gap:8pt; margin:0 0 16pt; }
.elapsed .track { flex:1; height:4pt; background:#E3E7EB; border-radius:2pt; overflow:hidden; }
.elapsed .fill { height:4pt; background:var(--ink); }
.elapsed .pct { font:600 8pt/1 var(--mono); letter-spacing:.06em; color:var(--muted); }

.read {
  break-inside:avoid; background:var(--tintr); border-left:4pt solid var(--bad);
  padding:12pt 15pt 14pt; margin:0 0 26pt;
}
.read .kicker {
  font:600 8pt/1 var(--mono); letter-spacing:.11em; text-transform:uppercase;
  color:var(--bad); margin-bottom:6pt;
}
.read p { font:400 12pt/1.45 var(--serif); margin:0; orphans:3; widows:3; }
.read .mono { font-size:12pt; }

.tiles {
  display:grid; grid-template-columns:repeat(6,1fr); gap:5pt;
  margin:0 0 30pt; break-inside:avoid;
}
.tile { background:var(--tintb); padding:10pt 10pt 11pt; }
.tile.g { background:var(--tintg); } .tile.a { background:var(--tinta); }
.tile.r { background:var(--tintr); }
.tile .lbl {
  font:600 7.5pt/1 var(--mono); letter-spacing:.07em; text-transform:uppercase;
  color:#5F6B7A; margin-bottom:5pt;
}
.tile .num { font:700 19pt/1 var(--serif); letter-spacing:-.02em; }
.tile .foot { font:400 7.5pt/1.3 var(--sans); color:var(--faint); margin-top:3pt; }

.agenda { display:grid; gap:5pt; margin:11pt 0 30pt; }
.agenda .item {
  background:var(--tintb); display:grid; grid-template-columns:74pt 1fr;
  gap:12pt; padding:12pt 14pt; break-inside:avoid;
}
.agenda .owner {
  display:inline-block; font:600 7.5pt/1 var(--mono); letter-spacing:.08em;
  text-transform:uppercase; color:#FFFFFF; background:var(--accent); padding:4pt 6pt;
}
.agenda .who { font:400 7.5pt/1.3 var(--mono); color:var(--faint); margin-top:4pt;
               display:block; background:none; border:0; padding:0; margin-left:0;
               text-transform:none; letter-spacing:0; }
.agenda .body { font:400 10.5pt/1.42 var(--serif); orphans:3; widows:3; }
.agenda .body .mono { font-size:10pt; font-weight:600; }
.agenda .link { font:400 8.5pt/1.3 var(--mono); margin-top:5pt; }

/* Conversations: developer -> story -> detail. The person is the outer
   group because a conversation is with a person; their stories nest inside,
   and each story's problems nest inside that. */
.convo { display:grid; gap:9pt; margin:11pt 0 30pt; }
.convo-dev { break-inside:avoid; background:var(--panel); padding:0 0 9pt;
             border-left:3pt solid var(--rule2); }
.convo-dev.bad { border-left-color:var(--bad); background:var(--tintr); }
.convo-dev.warn { border-left-color:var(--warnc); background:var(--tinta); }
.convo-dev .dvhd {
  display:flex; align-items:baseline; gap:8pt;
  font:700 12pt/1.25 var(--serif); letter-spacing:-.015em; color:var(--navy);
  padding:9pt 13pt 7pt; border-bottom:.5pt solid var(--rule2); margin-bottom:2pt;
}
.convo-dev .dvhd .cnt { margin-left:auto; font:600 7.5pt/1 var(--mono);
                        letter-spacing:.06em; text-transform:uppercase;
                        color:var(--muted); }
.convo-dev .outtag { font:600 7.5pt/1 var(--mono); letter-spacing:.06em;
                     text-transform:uppercase; color:var(--warnc); }
.convo-story { padding:6pt 13pt 2pt; break-inside:avoid; }
.convo-story .sthd { font:400 10pt/1.35 var(--serif); margin-bottom:3pt; }
.convo-story .sthd .mono { font-size:10pt; font-weight:700; }
.convo-story .sthd .nm { font-weight:600; }
.convo-story .role {
  display:inline-block; margin-left:6pt; padding:2pt 5pt;
  font:600 6.5pt/1 var(--mono); letter-spacing:.07em; text-transform:uppercase;
  color:#FFFFFF; background:var(--accent); vertical-align:1.5pt;
}
.convo-story .row { display:grid; grid-template-columns:62pt 1fr; gap:7pt;
                    font:400 9pt/1.4 var(--sans); color:var(--ink2); margin-top:3pt; }
.convo-story .v { min-width:0; }
.convo-story .k { font:600 7pt/1.5 var(--mono); letter-spacing:.05em;
                  text-transform:uppercase; color:var(--muted); text-align:right;
                  white-space:nowrap; overflow:hidden; }

.flags { display:grid; grid-template-columns:1fr 1fr; gap:12pt; margin:11pt 0 30pt; }
.flag { break-inside:avoid; background:var(--panel); border-left:3pt solid var(--rule2); padding:9pt 11pt 10pt; }
.flag.bad { border-left-color:var(--bad); background:var(--tintr); }
.flag.warn { border-left-color:var(--warnc); background:var(--tinta); }
.flag .lbl {
  font:600 8pt/1 var(--mono); letter-spacing:.08em; text-transform:uppercase;
  margin-bottom:6pt;
}
.flag.bad .lbl { color:var(--bad); } .flag.warn .lbl { color:var(--warnc); }
.flag .body { font:400 9.5pt/1.4 var(--sans); }
.flag .row { margin-bottom:4pt; }
.flag .row:last-child { margin-bottom:0; }

.twocol { display:grid; grid-template-columns:1fr 1fr; gap:22pt; margin-bottom:30pt; }
.digest { list-style:none; margin:9pt 0 0; padding:0; color:var(--ink2); }
.digest li { display:grid; grid-template-columns:9pt 1fr; gap:5pt; margin-bottom:5pt; break-inside:avoid; }
.digest .g { color:var(--ok); font-weight:700; }
.digest .b { color:var(--accent); font-weight:700; }
.plainlist { list-style:none; margin:9pt 0 0; padding:0; color:var(--ink2); }
.plainlist li { font:400 9.5pt/1.45 var(--sans); margin-bottom:5pt; break-inside:avoid; }

.spread { display:flex; gap:2pt; margin:11pt 0 8pt; }
.spread > div { height:16pt; }
.key { display:flex; gap:14pt; font:400 8.5pt/1.4 var(--sans); color:var(--ink2); }
.sw { display:inline-block; width:7pt; height:7pt; margin-right:4pt; }

/* Tables -------------------------------------------------------------- */
table.data { width:100%; border-collapse:collapse; margin:10pt 0 30pt; font:400 9.5pt/1.35 var(--sans); }
table.data th {
  text-align:left; padding:5pt 8pt 5pt 6pt; background:var(--tintb);
  border-bottom:1.5pt solid var(--accent);
  font:600 7.5pt/1.2 var(--mono); letter-spacing:.06em; text-transform:uppercase;
  color:var(--navy);
}
table.data td { padding:7pt 8pt 7pt 0; border-bottom:.5pt solid var(--rule); }
table.data tr { break-inside:avoid; }
table.data .num, table.data th.num { text-align:right; padding-left:8pt; padding-right:0; font-family:var(--mono); }
table.data tfoot td, table.data tfoot th {
  background:var(--tintb); border-top:.5pt solid var(--accent);
  border-bottom:1.5pt solid var(--navy); font-weight:700;
}
table.matrix td, table.matrix th { text-align:center; font-family:var(--mono); font-size:9pt; }
table.matrix th { padding:5pt 2pt; font-size:6.5pt; letter-spacing:.02em; word-break:break-all; }
table.matrix td { padding:6pt 2pt; }
table.matrix td:first-child, table.matrix th:first-child { text-align:left; }
.m-merged { color:var(--ok); font-weight:700; }
.m-open { color:var(--accent); font-weight:600; }
.m-declined { color:var(--bad); font-weight:700; }
.m-none { color:var(--rule2); }
.lat { display:inline-block; width:7pt; height:7pt; margin-right:5pt; vertical-align:middle; }
.lat-green { background:var(--ok); } .lat-yellow { background:var(--warnc); }
.lat-red { background:var(--bad); }

/* Per-dev pages ------------------------------------------------------- */
.dev { break-before:page; margin-top:44pt; padding-top:4pt; }
.devhead {
  display:flex; justify-content:space-between; align-items:flex-end;
  border-bottom:2.5pt solid var(--accent); padding-bottom:6pt; margin-bottom:4pt;
  break-after:avoid;
}
.devhead .stat { font:600 8pt/1 var(--mono); letter-spacing:.07em; text-transform:uppercase; }
.quotes {
  background:var(--tinta); border-left:3pt solid var(--warnc);
  padding:11pt 14pt; margin-bottom:26pt; break-inside:avoid;
}
.quotes .q { font:400 9.5pt/1.5 var(--serif); margin-bottom:7pt; }
.quotes .q:last-child { margin-bottom:0; }
.quotes .src { font:400 8pt/1 var(--mono); color:var(--faint); }

.scorebox { display:flex; gap:8pt; flex-wrap:wrap; margin:10pt 0 12pt; break-inside:avoid; }
.scorebox > div { border:.5pt solid var(--rule); padding:6pt 12pt; text-align:center; }
.scorebox .num { font:700 16pt/1 var(--serif); letter-spacing:-.02em; }
.scorebox .lbl { font:600 7pt/1 var(--mono); letter-spacing:.06em; text-transform:uppercase; color:var(--muted); margin-top:3pt; }

.charts { display:grid; grid-template-columns:1fr 1fr; gap:16pt; margin-bottom:28pt; }
.charts > div { break-inside:avoid; background:var(--panel); padding:10pt 11pt 11pt; }
.charts .ctitle { font:600 9.5pt/1 var(--sans); color:var(--navy); margin-bottom:6pt; }
.charts .ccap { font:400 8.5pt/1.4 var(--sans); color:var(--faint); margin-top:4pt; }
.ccap { font:400 8.5pt/1.4 var(--sans); color:var(--faint); }
svg.chart { width:100%; height:auto; display:block; }

/* Story cards --------------------------------------------------------- */
.card {
  break-inside:avoid; margin-bottom:26pt; padding:11pt 13pt 13pt;
  background:var(--panel); border-left:3pt solid var(--rule2);
}
.card.green { border-left-color:var(--ok); background:var(--tintg); }
.card.yellow { border-left-color:var(--warnc); background:var(--tinta); }
.card.red { border-left-color:var(--bad); background:var(--tintr); }
.card .top { display:flex; justify-content:space-between; align-items:baseline; gap:10pt; }
.card .title { font:700 11pt/1.3 var(--serif); letter-spacing:-.012em; }
.card .title .mono { font-size:11pt; }
.card .state { font:600 7.5pt/1 var(--mono); letter-spacing:.06em; text-transform:uppercase; white-space:nowrap; }
.card .meta { font:400 8.5pt/1.4 var(--mono); color:var(--faint); margin:4pt 0 8pt; }
.card .cols { display:grid; grid-template-columns:1fr 1fr; gap:14pt; }
.card .line { font:400 9.5pt/1.45 var(--sans); color:var(--ink2); }
.card .tiny { font:400 8pt/1.4 var(--sans); color:var(--faint); margin-top:5pt; }
.acs { list-style:none; margin:0; padding:0; }
.acs li { font:400 9.5pt/1.4 var(--sans); color:var(--ink2); margin-bottom:4pt; break-inside:avoid; }
.acs .pct { display:inline-block; width:30pt; font:600 8.5pt/1 var(--mono); }
.pct { font:600 8.5pt/1 var(--mono); }
.branch { font:400 8.5pt/1.45 var(--mono); }

.colophon {
  margin-top:34pt; padding-top:10pt; border-top:2pt solid var(--accent);
  font:400 8pt/1.5 var(--mono); color:var(--faint); break-inside:avoid;
}
p, li { orphans:3; widows:3; }
"""


def esc(s):
    return html.escape(str(s))


FINISHED_STATES = ("Completed", "Accepted", "Released")


def is_finished(story):
    """True for work nobody needs to act on. This report is about what is
    NOT done yet, so Completed and Accepted stories are left out of the
    per-dev cards entirely rather than shown with a status badge."""
    return story.state in FINISHED_STATES or story.done


def _lateness_class(story):
    return story.lateness or "green"


def _pct_class(pct):
    return "good" if pct >= 70 else ("warn" if pct >= 30 else "risk")


def lat_dot(story):
    return f'<span class="lat lat-{_lateness_class(story)}" title="{esc(story.lateness)}"></span>'


def pct_bar(pct):
    """Percentages read as figures, not decorative bars: thirty of them down a
    printed page was noise, and a bar's fill is unreadable at 4pt."""
    return f'<span class="pct {_pct_class(pct)}">{pct}%</span>'


def state_badge(story):
    cls = "good" if story.done else ("risk" if story.blocked else "m-open")
    label = story.state + (" · blocked" if story.blocked else "")
    return f'<span class="state {cls}">{esc(label)}</span>'


def _tile(label, value, foot="", tone="", tint=""):
    """tone colours the figure; tint colours the card (g/a/r, default blue)."""
    cls = f" {tone}" if tone else ""
    tcls = f" {tint}" if tint else ""
    return (f'<div class="tile{tcls}"><div class="lbl">{esc(label)}</div>'
            f'<div class="num{cls}">{value}</div>'
            f'<div class="foot">{foot}</div></div>')


def _flag(label, count, rows, tone="bad"):
    body = "".join(f'<div class="row">{r}</div>' for r in rows)
    return (f'<div class="flag {tone}"><div class="lbl">{esc(label)} · {count}</div>'
            f'<div class="body">{body}</div></div>')


def _sid(sid):
    return f'<span class="mono" style="font-weight:600">{esc(sid)}</span>'


def _who(story):
    """The owner chip. Every flagged line must say whose story it is —
    an unowned story is a finding, not a blank."""
    who = story.owner or "unassigned"
    cls = "who" if story.owner else "who unassigned"
    return f'<span class="{cls}">{esc(who)}</span>'


def _sid_who(story):
    return f"{_sid(story.sid)} {_who(story)}"


def iteration_name_short(n):
    return f"Sprint {n}"


def render_story(story, sprint_n, current_sprint):
    """Two columns: what was promised (acceptance criteria) on the left, what
    exists (branch, PR, risks) on the right. The old single column of labelled
    stripes is why a story could split across a page break."""
    pts = f"{story.points:g} pts" if story.points is not None else "unestimated"
    h = [f'<div class="card {_lateness_class(story)}">']
    h.append(
        '<div class="top">'
        f'<div class="title"><a class="mono" href="{esc(story.url)}">{esc(story.sid)}</a> '
        f"&nbsp;{esc(story.name)}</div>"
        f'<div class="state {"risk" if story.blocked else ("good" if story.done else "m-open")}">'
        f'{esc(story.state)}{" · blocked" if story.blocked else ""}</div></div>'
    )
    h.append(f'<div class="meta">{esc(story.kind)} · {esc(story.owner or "unassigned")} · '
             f"{pts} · {story.discussions} discussion(s) · {story.completion}% complete · "
             f"{esc(iteration_name_short(sprint_n))}</div>")

    h.append('<div class="cols"><div>')
    h.append(f'<div class="fieldlab">Acceptance criteria — {len(story.acs)}</div>')
    if story.acs:
        h.append('<ul class="acs">')
        for i, ac in enumerate(story.acs):
            if story.ac_scores:
                pct, note = story.ac_scores[i]
            else:
                pct, note = story.completion, ""
            tested = ""
            if story.ac_tested:
                tested = (' <span class="good">✓ tested</span>' if story.ac_tested[i]
                          else ' <span class="risk">✗ untested</span>')
            note_html = f' <span class="muted">— {esc(note)}</span>' if note else ""
            h.append(f'<li><span class="pct {_pct_class(pct)}">{pct}%</span>'
                     f"{esc(ac)}{tested}{note_html}</li>")
        h.append("</ul>")
        if not story.ac_scores:
            h.append('<div class="tiny">Story-level estimate — enable '
                     '<span class="mono">[ai]</span> for per-criterion scoring.</div>')
    else:
        h.append('<div class="line warn">None recorded on the story.</div>')
    if story.tests_style or story.coverage_note or story.arch_note:
        h.append('<div class="tiny">')
        if story.tests_style:
            cls = _pct_class(100 if story.tests_style == "behavioral" else 0)
            h.append(f'Tests: <span class="{cls}">{esc(story.tests_style)}</span>')
            if story.coverage_note:
                h.append(f" · coverage: {esc(story.coverage_note)}")
        if story.arch_note:
            h.append(f"<br>Architecture: {esc(story.arch_note)}")
        h.append("</div>")
    h.append("</div><div>")

    h.append('<div class="fieldlab">Branch, PR &amp; risks</div>')
    if story.branches:
        for b in story.branches:
            age = (f"last commit {b.last_commit_age_days}d ago"
                   if b.last_commit_age_days is not None else "no commits")
            h.append(f'<div class="branch">{esc(b.repo)}:{esc(b.name)}</div>')
            h.append(f'<div class="tiny" style="margin-top:0">{b.commits} commit(s) · '
                     f"{b.files_changed} file(s) · +{b.insertions}/−{b.deletions} · {age}"
                     + (f" — “{esc(b.last_commit_subject)}”" if b.last_commit_subject else "")
                     + "</div>")
            for pr in b.prs:
                bits = [f'PR → <span class="mono">{esc(pr.base or "?")}</span>', esc(pr.state)]
                if pr.draft:
                    bits.append("draft")
                if pr.review:
                    bits.append(f'<span class="risk">{esc(pr.review.lower().replace("_", " "))}</span>')
                if pr.additions or pr.deletions:
                    bits.append(f"+{pr.additions}/−{pr.deletions}")
                bits.append(f"{pr.comments} comment(s)")
                if pr.checks_total:
                    if pr.checks_failed:
                        bits.append(f'<span class="risk">{pr.checks_failed} check(s) failing</span>')
                    elif pr.checks_pending:
                        bits.append(f'<span class="warn">{pr.checks_pending} check(s) pending</span>')
                    else:
                        bits.append('<span class="good">checks green</span>')
                h.append('<div class="line" style="margin-top:4pt">'
                         + " · ".join(bits) + "</div>")
                h.append(f'<div class="tiny" style="margin-top:0">'
                         f'<a href="{esc(pr.url)}">{esc(pr.title)}</a> — opened '
                         f"{esc(pr.created)}, updated {esc(pr.updated)}</div>")
        if not story.all_prs and not story.done:
            h.append('<div class="line warn">Branch exists but no PR raised yet.</div>')
    elif story.done:
        h.append('<div class="line">No branch found — accepted in Rally with nothing '
                 'in the configured repos. Worth checking the branch naming.</div>')
    else:
        h.append('<div class="line warn">No branch found for this story.</div>')

    for tag, msg in story.risks:
        h.append(f'<div class="line risk" style="margin-top:3pt">'
                 f'<span class="mono" style="font-size:8pt;font-weight:600">'
                 f'{esc(tag.upper())}</span> {esc(msg)}</div>')
    for c in story.ai_concerns:
        h.append(f'<div class="line warn" style="margin-top:3pt">⚠ {esc(c)}</div>')

    h.append("</div></div></div>")
    return "".join(h)


MATRIX_SYMBOL = {"merged": ("✓", "m-merged"), "open": ("●", "m-open"),
                 "declined": ("✗", "m-declined"), "-": ("—", "m-none")}


def headline(cfg, today, current_sprint, per_dev, analysis, all_current, all_stories):
    """The one-line read at the top of page 1: derived, not decorative."""
    start, end = sprint_window(cfg, current_sprint)
    length = cfg["sprint"]["length_days"]
    elapsed = max(0, min(1, ((today - start).days + 1) / length))
    total = total_points(all_current)
    remaining = remaining_points(all_current)
    ideal = total * (1 - elapsed)
    days_left = max(0, (end - today).days)

    worst = {}
    for s in all_stories:
        for tag, _ in s.risks:
            worst.setdefault(s.sid, 0)
            worst[s.sid] += 1
    hot = sorted(worst.items(), key=lambda kv: -kv[1])[:2]

    if remaining <= ideal:
        verdict = (f"The pod is on or ahead of the line with <strong>{remaining:g} of "
                   f"{total:g} points</strong> left and {days_left} days to go")
    else:
        verdict = (f"The pod is <strong>{remaining - ideal:.0f} points behind the line</strong> "
                   f"with {remaining:g} of {total:g} left and {days_left} days to go")

    if hot:
        names = " and ".join(f'<span class="mono">{esc(sid)}</span>' for sid, _ in hot)
        verdict += f", and today's risks concentrate on {names}"
    out = analysis["out_today"]
    if out:
        verdict += (f". {esc(', '.join(sorted(out)))} "
                    f"{'is' if len(out) == 1 else 'are'} out")
    return verdict + "."


def render_report(cfg, today, current_sprint, sprints, per_dev, analysis):
    pod = cfg["pod"]["name"]
    length = cfg["sprint"]["length_days"]
    risks_cfg = cfg["risks"]
    start, end = sprint_window(cfg, current_sprint)
    day_no = (today - start).days + 1
    it_name = iteration_name(cfg, current_sprint)

    all_current = [s for dev in per_dev.values() for s in dev.get(current_sprint, [])]
    all_stories = [s for dev in per_dev.values() for sp in dev.values() for s in sp]
    all_risks = [(s, r) for s in all_stories for r in s.risks]
    blocked = [s for s in all_stories if s.blocked]
    open_prs = [(s, pr) for s in all_stories for pr in s.all_prs if pr.state == "OPEN"]
    out_today = analysis["out_today"]
    total, remaining = total_points(all_current), remaining_points(all_current)

    green = sum(1 for s in all_stories if s.lateness == "green")
    yellow = sum(1 for s in all_stories if s.lateness == "yellow")
    red = sum(1 for s in all_stories if s.lateness == "red")

    body = []

    # ---- Page 1: the dashboard -------------------------------------------
    body.append('<div class="masthead">')
    body.append(f"<h1>{esc(pod)} — morning report</h1>")
    body.append(
        f'<div class="sub">{today.strftime("%A %d %B %Y")} · {esc(it_name)} '
        f"({start.isoformat()} → {end.isoformat()}) · day {day_no} of {length} · "
        f"{len(sprints) - 1} previous sprint(s) in scope</div>"
    )
    pct_elapsed = round(100 * day_no / length)
    body.append(f'<div class="elapsed"><div class="track">'
                f'<div class="fill" style="width:{pct_elapsed}%"></div></div>'
                f'<span class="pct">{pct_elapsed}% ELAPSED</span></div>')
    body.append("</div>")

    body.append('<div class="read"><div class="kicker">The one-line read</div><p>'
                + headline(cfg, today, current_sprint, per_dev, analysis,
                           all_current, all_stories)
                + "</p></div>")

    unreviewed = analysis["waiting_reviews"]
    failing = [(s, pr) for s, pr in open_prs if pr.checks_failed]
    body.append('<div class="tiles">')
    body.append(_tile("Remaining", f"{remaining:g}", f"of {total:g} pts"))
    n_open = sum(1 for s in all_current if not is_finished(s))
    body.append(_tile("Stories open", f"{n_open}",
                      f"of {len(all_current)}, {len(all_current) - n_open} finished",
                      tone="" if n_open else "good", tint="" if n_open else "g"))
    body.append(_tile("PRs open", f"{len(open_prs)}",
                      (f'<span class="risk">{len(unreviewed)} unreviewed</span>'
                       if unreviewed else "all reviewed")))
    body.append(_tile("Blocked", f"{len(blocked)}",
                      ", ".join(esc(s.sid) for s in blocked) or "nothing blocked",
                      tone="risk" if blocked else "",
                      tint="r" if blocked else "g"))
    body.append(_tile("Risks", f"{len(all_risks)}",
                      f"across {len({s.sid for s, _ in all_risks})} stories",
                      tone="risk" if all_risks else "",
                      tint="r" if all_risks else "g"))
    body.append(_tile("Capacity", f"{len(out_today)} out" if out_today else "full",
                      ", ".join(sorted(esc(d) for d in out_today)) or "everyone in",
                      tone="warn" if out_today else "",
                      tint="a" if out_today else "g"))
    body.append("</div>")

    # Conversations to have today — grouped developer, then story, then the
    # detail on that story. You have ONE conversation with a person covering
    # everything of theirs, so the person is the outer group; a flat list
    # routed by risk type made that impossible to see.
    SEV = {"ask": 0, "blocked": 1, "failing": 2, "review": 3, "oversized": 4,
           "acceptance": 5, "risk": 6, "hygiene": 7}
    ROLE_OF = {}          # sid -> role that should lead, from the agenda
    groups = {}           # dev -> {"stories": {sid: {...}}, "general": [...]}

    def group_for(dev):
        return groups.setdefault(dev or "Unassigned",
                                 {"stories": {}, "general": []})

    def add(story, kind, text, role=""):
        g = group_for(story.owner)
        e = g["stories"].setdefault(story.sid, {"story": story, "items": []})
        e["items"].append((SEV[kind], kind, text, role))

    for it in analysis["agenda"]:
        if it["sid"]:
            ROLE_OF.setdefault(it["sid"], it["role"])
        else:
            group_for(it["dev"])["general"].append((it["role"], it["text"]))

    for st, d in analysis["blocked_aging"]:
        add(st, "blocked", f'Blocked {d}d — {esc(st.blocked_reason or "no reason recorded")}'
                           f' <span class="muted">(escalate at '
                           f'{risks_cfg["blocked_escalate_days"]}d)</span>')
    for st, pr in failing:
        add(st, "failing", f"{pr.checks_failed} check(s) failing on the PR to "
                           f'<span class="mono">{esc(pr.base or "?")}</span>')
    for st, pr, w in unreviewed:
        add(st, "review", f"Waiting {w}d on a first review — "
                          f'{esc(", ".join(pr.awaiting) or "no reviewer assigned")}')
    big = [(st, pr) for st, pr in open_prs
           if (pr.additions + pr.deletions) > risks_cfg["big_pr_lines"]]
    for st, pr in big:
        add(st, "oversized", f"Oversized PR: +{pr.additions}/−{pr.deletions} against a "
                             f'{risks_cfg["big_pr_lines"]}-line threshold')
    for st, (tag, msg) in all_risks:
        if tag.lower() not in ("blocked", "stale-review"):
            add(st, "risk", f'<span class="mono" style="font-size:8pt;font-weight:600">'
                            f"{esc(tag.upper())}</span> {esc(msg)}")
    # The ask itself, from the agenda, sits at the top of its story.
    for it in analysis["agenda"]:
        if not it["sid"]:
            continue
        for g in groups.values():
            if it["sid"] in g["stories"]:
                g["stories"][it["sid"]]["items"].append(
                    (SEV["ask"], "ask", esc(it["text"]), it["role"]))
                break
    # Hygiene only joins a story already being discussed; a story whose only
    # problem is a missing estimate is a cleanup chore, not a conversation.
    hygiene_only = []
    for _dev, st, problem in analysis["hygiene"]:
        g = groups.get(st.owner or "Unassigned")
        if g and st.sid in g["stories"]:
            add(st, "hygiene", esc(problem))
        else:
            hygiene_only.append((st, problem))

    def worst_of(entry):
        return min(i[0] for i in entry["items"]) if entry["items"] else 99

    ranked_devs = sorted(
        groups.items(),
        key=lambda kv: (min([worst_of(e) for e in kv[1]["stories"].values()] or [99]),
                        -len(kv[1]["stories"]), kv[0]))

    body.append("<h2>Conversations to have today</h2>")
    if ranked_devs:
        n_items = sum(len(e["items"]) for _d, g in ranked_devs
                      for e in g["stories"].values()) \
                  + sum(len(g["general"]) for _d, g in ranked_devs)
        body.append(f'<div class="note">{len(ranked_devs)} '
                    f'{"person" if len(ranked_devs) == 1 else "people"} to talk to, '
                    f"{n_items} thing(s) to raise. One block per person: everything "
                    "of theirs is here, worst first. Roles mark who should lead.</div>")
        body.append('<div class="convo">')
        for dev, g in ranked_devs:
            worst = min([worst_of(e) for e in g["stories"].values()] or [99])
            tone = "bad" if worst <= SEV["failing"] else "warn"
            out_tag = (' <span class="outtag">out today</span>'
                       if dev in out_today else "")
            unassigned = (' <span class="outtag" style="color:var(--bad)">'
                          "nobody owns these</span>") if dev == "Unassigned" else ""
            body.append(f'<div class="convo-dev {tone}">')
            body.append(f'<div class="dvhd">{esc(dev)}{unassigned}{out_tag}'
                        f'<span class="cnt">{len(g["stories"])} '
                        f'stor{"y" if len(g["stories"]) == 1 else "ies"}</span></div>')
            for _sid, e in sorted(g["stories"].items(), key=lambda kv: (
                    worst_of(kv[1]), kv[0])):
                st = e["story"]
                role = ROLE_OF.get(st.sid, "")
                badge = f'<span class="role">{esc(role)}</span>' if role else ""
                body.append(f'<div class="convo-story"><div class="sthd">{lat_dot(st)}'
                            f'<a class="mono" href="{esc(st.url)}">{esc(st.sid)}</a> '
                            f'<span class="nm">{esc(st.name)}</span>{badge}</div>')
                for _sv, kind, text, _role in sorted(e["items"], key=lambda i: i[0]):
                    body.append(f'<div class="row"><span class="k">{kind}</span>'
                                f'<span class="v">{text}</span></div>')
                body.append("</div>")
            for role, text in g["general"]:
                body.append(f'<div class="convo-story"><div class="row">'
                            f'<span class="k">{esc(role)}</span>'
                            f'<span class="v">{esc(text)}</span></div></div>')
            body.append("</div>")
        body.append("</div>")
    else:
        body.append('<div class="note">Nothing needs an intervention this '
                    "morning.</div>")

    actions = analysis["actions"]
    if actions is not None and not actions.get("missing") and actions["open"]:
        body.append('<div class="flags"><div class="flag warn">'
                    f'<div class="lbl">Open action items · {len(actions["open"])}</div>'
                    '<div class="body">'
                    + "".join(f'<div class="row">{esc(i)}</div>'
                              for i in actions["open"][:6])
                    + "</div></div></div>")

    # Digest + lateness spread, side by side.
    body.append('<div class="twocol"><div>')
    body.append("<h2>Since the last report</h2>")
    if analysis["digest"]:
        body.append('<ul class="digest">')
        for e in analysis["digest"][:12]:
            mark = "g" if "appeared" in e else "b"
            glyph = "+" if mark == "g" else "→"
            body.append(f'<li><span class="{mark}">{glyph}</span><span>{esc(e)}</span></li>')
        body.append("</ul>")
    else:
        body.append('<div class="note">No recorded changes (first run, or a quiet day).</div>')
    body.append("</div><div>")
    body.append("<h2>Lateness spread</h2>")
    body.append('<div class="spread">'
                f'<div style="flex:{max(green, 0.01)};background:#2F6B4F"></div>'
                f'<div style="flex:{max(yellow, 0.01)};background:#8A5A12"></div>'
                f'<div style="flex:{max(red, 0.01)};background:#9B3327"></div></div>')
    body.append('<div class="key">'
                f'<span><span class="sw" style="background:#2F6B4F"></span>{green} on track</span>'
                f'<span><span class="sw" style="background:#8A5A12"></span>{yellow} yellow</span>'
                f'<span><span class="sw" style="background:#9B3327"></span>{red} red</span></div>')
    body.append(f'<div class="ccap" style="margin-top:8pt">Yellow at '
                f'{risks_cfg["yellow_gap_pct"]}% behind sprint elapsed, red at '
                f'{risks_cfg["red_gap_pct"]}%. Counted across every sprint in scope.</div>')
    body.append("</div></div>")

    # ---- Reference pages -------------------------------------------------
    if cfg["report"].get("burndown", True):
        body.append('<div style="break-before:page;margin-top:44pt;padding-top:4pt">')
        body.append("<h2>Pod burndown</h2>")
        body.append('<div class="note">Reconstructed from Rally acceptance dates. '
                    'Dots mark the days on which remaining points actually changed.</div>')
        body.append('<div class="charts">')
        for n in reversed(sprints):
            s_start, s_end = sprint_window(cfg, n)
            sprint_stories = [s for dev in per_dev.values() for s in dev.get(n, [])]
            if not sprint_stories and n != current_sprint:
                continue
            label = (f'{esc(iteration_name(cfg, n))} <span class="muted">· '
                     f'{"current" if n == current_sprint else "closed"}</span>')
            body.append(_chart_block(label, "", burndown_svg(
                f"Pod — {iteration_name(cfg, n)}", length,
                total_points(sprint_stories),
                burndown_series(sprint_stories, s_start, min(today, s_end)),
                today_day=(today - s_start).days if n == current_sprint else None)))
        body.append("</div></div>")

    body.append("<h2>Pod summary</h2>")
    body.append('<table class="data"><thead><tr><th>Dev</th>'
                '<th class="num">Stories</th><th class="num">Points</th>'
                '<th class="num">Accepted</th><th class="num">Remaining</th>'
                '<th class="num">PRs open</th><th class="num">Blocked</th>'
                '<th class="num">G / Y / R</th></tr></thead><tbody>')
    for dev, by_sprint in per_dev.items():
        cur = by_sprint.get(current_sprint, [])
        everything = [s for sp in by_sprint.values() for s in sp]
        dev_prs = sum(1 for s in everything for pr in s.all_prs if pr.state == "OPEN")
        g = sum(1 for s in everything if s.lateness == "green")
        y = sum(1 for s in everything if s.lateness == "yellow")
        r = sum(1 for s in everything if s.lateness == "red")
        tag = ' <span class="muted" style="font-size:8.5pt">(out)</span>' if dev in out_today else ""
        nblocked = sum(1 for s in everything if s.blocked)
        body.append(
            f"<tr><td>{esc(dev)}{tag}</td>"
            f'<td class="num">{len(cur)}</td>'
            f'<td class="num">{total_points(cur):g}</td>'
            f'<td class="num">{total_points(cur) - remaining_points(cur):g}</td>'
            f'<td class="num" style="font-weight:600">{remaining_points(cur):g}</td>'
            f'<td class="num">{dev_prs}</td>'
            f'<td class="num{" risk" if nblocked else ""}">{nblocked}</td>'
            f'<td class="num"><span class="good">{g}</span> / '
            f'<span class="warn">{y}</span> / <span class="risk">{r}</span></td></tr>'
        )
    body.append(
        f'</tbody><tfoot><tr><td>Pod</td><td class="num">{len(all_current)}</td>'
        f'<td class="num">{total:g}</td>'
        f'<td class="num">{total - remaining:g}</td>'
        f'<td class="num">{remaining:g}</td>'
        f'<td class="num">{len(open_prs)}</td>'
        f'<td class="num">{len(blocked)}</td>'
        f'<td class="num">{len(all_risks)} risks</td></tr></tfoot></table>'
    )

    targets = analysis["targets"]
    body.append('<div class="twocol"><div>')
    if targets:
        body.append("<h2>Promotion matrix</h2>")
        body.append('<table class="data matrix"><thead><tr><th>Story</th><th>Dev</th>'
                    + "".join(f"<th>{esc(t)}</th>" for t in targets)
                    + "</tr></thead><tbody>")
        for dev, by_sprint in per_dev.items():
            for s in sorted(by_sprint.get(current_sprint, []), key=lambda x: x.sid):
                row = promotion_matrix(s, targets)
                cells = "".join(
                    f'<td class="{MATRIX_SYMBOL[row[t]][1]}" title="{row[t]}">'
                    f"{MATRIX_SYMBOL[row[t]][0]}</td>" for t in targets)
                body.append(f'<tr><td>{lat_dot(s)}<a href="{esc(s.url)}">{esc(s.sid)}</a></td>'
                            f"<td>{esc((s.owner or dev).split()[0])}</td>{cells}</tr>")
        body.append("</tbody></table>")
        body.append('<div class="ccap"><span class="m-merged">✓</span> merged &nbsp; '
                    '<span class="m-open">●</span> PR open &nbsp; '
                    '<span class="m-declined">✗</span> declined &nbsp; '
                    '<span class="m-none">—</span> no PR</div>')
    body.append("</div><div>")
    body.append("<h2>Cleanup — not urgent</h2>")
    rows = []
    for st, problem in hygiene_only:
        rows.append(f"<li>{_sid_who(st)} — {esc(problem)}</li>")
    churn = analysis["churn"]
    if churn and churn["added"]:
        for s in churn["added"]:
            pts = f"{s.points:g} pts" if s.points is not None else "unestimated"
            rows.append(f'<li class="warn">{_sid_who(s)} ({pts}) added after sprint '
                        f'start (baseline {esc(churn["baseline_date"])})</li>')
    if rows:
        body.append('<ul class="plainlist">' + "".join(rows) + "</ul>")
    else:
        body.append('<div class="note">Nothing to clean up: every story has criteria '
                    "and an estimate, and no scope was added after the baseline.</div>")
    if analysis["review_load"]:
        load = sorted(analysis["review_load"].items(), key=lambda kv: -kv[1])
        body.append('<div class="ccap" style="margin-top:8pt">Review load — '
                    + ", ".join(f"{esc(k)} {v}" for k, v in load) + "</div>")
    body.append("</div></div>")

    # ---- One page per dev ------------------------------------------------
    # Ordered by how much attention the dev needs, not by dict order: the
    # person you should turn to first should not be on the last page.
    def dev_weight(item):
        _dev, by_sprint = item
        everything = [x for sp in by_sprint.values() for x in sp]
        return (-sum(1 for x in everything if x.blocked),
                -sum(1 for x in everything if x.lateness == "red"),
                -sum(1 for x in everything if x.lateness == "yellow"),
                _dev)

    for dev, by_sprint in sorted(per_dev.items(), key=dev_weight):
        everything = [s for sp in by_sprint.values() for s in sp]
        cur = by_sprint.get(current_sprint, [])
        nblocked = sum(1 for s in everything if s.blocked)
        tone = "risk" if nblocked or any(s.lateness == "red" for s in everything) else \
               ("good" if all(s.done for s in cur) and cur else "warn")
        out_tag = (' <span class="warn" style="font:400 11pt/1 var(--sans)">out today</span>'
                   if dev in out_today else "")
        body.append('<div class="dev">')
        body.append(f'<div class="devhead"><h3>{esc(dev)}{out_tag}</h3>'
                    f'<div class="stat {tone}">{len(cur)} stories · '
                    f'{total_points(cur):g} pts · {remaining_points(cur):g} remaining'
                    + (f" · {nblocked} blocked" if nblocked else "") + "</div></div>")

        if cfg["report"].get("burndown", True):
            body.append('<div class="charts">')
            for n in reversed(sprints):
                s_start, s_end = sprint_window(cfg, n)
                dev_stories = by_sprint.get(n, [])
                if not dev_stories and n != current_sprint:
                    continue
                label = (f'{esc(iteration_name(cfg, n))} <span class="muted">· '
                         f'{"current" if n == current_sprint else "closed"}</span>')
                body.append(_chart_block(label, "", burndown_svg(
                    f"{dev} — {iteration_name(cfg, n)}", length,
                    total_points(dev_stories),
                    burndown_series(dev_stories, s_start, min(today, s_end)),
                    today_day=(today - s_start).days if n == current_sprint else None)))
            body.append("</div>")

        for n in sprints:
            stories = by_sprint.get(n, [])
            if n != current_sprint:
                stories = [s for s in stories if not is_finished(s)]
                if not stories:
                    continue
                body.append(f'<div class="seclab">{esc(iteration_name(cfg, n))} — '
                            "unfinished spillover</div>")
            else:
                body.append(f'<div class="seclab">{esc(iteration_name(cfg, n))} — '
                            "current</div>")
                if not stories:
                    body.append('<div class="line warn">No stories assigned in the '
                                "current sprint.</div>")
            # Only work that is not done yet. Completed and Accepted
            # stories are dropped, not summarised: this page exists to show
            # what still needs something from someone.
            open_stories = [x for x in stories if not is_finished(x)]
            for s in sorted(open_stories, key=lambda x: (
                    {"red": 0, "yellow": 1}.get(x.lateness, 2), not x.blocked, x.sid)):
                body.append(render_story(s, n, current_sprint))
            if not open_stories and stories:
                body.append('<div class="line good">Nothing outstanding — every '
                            "story assigned here is finished.</div>")

        card = analysis["coaching"].get(dev)
        if card:
            risk = str(card.get("risk") or "none")
            risk_cls = {"none": "good", "watch": "warn", "action": "risk"}.get(risk, "muted")
            body.append('<div class="scorebox">')
            for k in ("delivery", "quality", "communication", "collaboration"):
                v = card.get("scores", {}).get(k, "?")
                body.append(f'<div><div class="num">{esc(v)}</div>'
                            f'<div class="lbl">{esc(k)}</div></div>')
            body.append(f'<div><div class="num {risk_cls}">{esc(risk)}</div>'
                        f'<div class="lbl">risk</div></div></div>')
            if risk != "none" and card.get("risk_reason"):
                body.append(f'<div class="line {risk_cls}">⚠ {esc(card["risk_reason"])}</div>')
            for s_txt in card.get("strengths") or []:
                body.append(f'<div class="line good">＋ {esc(s_txt)}</div>')
            for c_txt in card.get("coaching") or []:
                body.append(f'<div class="line">→ {esc(c_txt)}</div>')
            qs = card.get("questions_requests") or []
            if qs:
                body.append('<div class="fieldlab" style="margin-top:10pt">Outstanding '
                            "questions from them</div>")
                for q in qs:
                    body.append(f'<div class="line">? {esc(q)}</div>')

        snippets = analysis["comms"].get(dev) or []
        if snippets:
            body.append('<div class="quotes"><div class="fieldlab" '
                        'style="margin-bottom:8pt">In their own words — transcripts, '
                        "chat, email</div>")
            for sn in snippets:
                src = f"{sn['source']}.{sn['table']}" + (f" · {sn['date']}" if sn["date"] else "")
                body.append(f'<div class="q">“{esc(sn["snippet"])}”'
                            f'<span class="src"> &nbsp;{esc(src)}</span></div>')
            body.append("</div>")
        body.append("</div>")

    body.append(
        '<div class="colophon">Thresholds in force — stale PR '
        f'{risks_cfg["stale_pr_days"]}d · unreviewed {risks_cfg["review_wait_days"]}d · '
        f'blocked escalation {risks_cfg["blocked_escalate_days"]}d · acceptance wait '
        f'{risks_cfg["acceptance_wait_days"]}d · oversized PR {risks_cfg["big_pr_lines"]} '
        f'lines · WIP limit {cfg["pod"]["wip_limit"]} · yellow '
        f'{risks_cfg["yellow_gap_pct"]}% / red {risks_cfg["red_gap_pct"]}% behind elapsed. '
        + ("AI judgment layer on." if analysis["ai_enabled"]
           else "AI judgment layer off — acceptance criteria show story-level estimates.")
        + "</div>"
    )

    runhead = (f'<div class="runhead"><span>{esc(pod)} &nbsp;·&nbsp; Morning report</span>'
               f'<span>{today.strftime("%a %d %b %Y")} &nbsp;·&nbsp; {esc(it_name)}, '
               f"day {day_no} / {length}</span></div>")
    runfoot = (f'<div class="runfoot"><span>pod-audit.py &nbsp;·&nbsp; generated '
               f'{dt.datetime.now().strftime("%Y-%m-%d %H:%M")}</span>'
               f"<span>For pod leadership — TL / PM / PO / SM</span></div>")

    return "\n".join([
        f"<title>{esc(pod)} audit {today.isoformat()}</title>",
        f"<style>{CSS}</style>",
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        "family=Geist+Mono:wght@400..600&family=Instrument+Sans:wght@400..700&"
        'family=Manrope:wght@500..800&display=swap">',
        '<table class="frame"><thead><tr><td>' + runhead + "</td></tr></thead>",
        "<tbody><tr><td>",
        "\n".join(body),
        "</td></tr></tbody>",
        "<tfoot><tr><td>" + runfoot + "</td></tr></tfoot></table>",
    ])


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


def find_chromes():
    """All usable browser binaries, POD_AUDIT_CHROME first, deduped."""
    override = os.environ.get("POD_AUDIT_CHROME")
    found = []
    for c in ([override] if override else []) + CHROME_CANDIDATES:
        p = Path(c).expanduser()
        resolved = str(p) if p.is_file() else shutil.which(c)
        if resolved and resolved not in found:
            found.append(resolved)
    return found


def _print_pdf_attempt(chrome, html_path, pdf_path, timeout_s=60):
    """Try one browser. Returns '' on success, else a failure description.
    Waiting for the browser process to exit is unreliable: headless Chrome
    with a fresh profile writes the PDF within seconds but can then linger
    forever, and launching against the user's default profile can block on
    sign-in/policy dialogs or a profile lock (corporate Edge). So: run with
    a throwaway profile, watch for the PDF file to appear and stop growing,
    then reap the browser ourselves."""
    try:
        pdf_path.unlink(missing_ok=True)
    except OSError as e:
        return f"cannot replace {pdf_path}: {e}"
    detail = ""
    with tempfile.TemporaryDirectory(prefix="pod-audit-pdf-") as tmp:
        log = Path(tmp, "browser.log")
        proc = None
        try:
            with open(log, "wb") as lf:
                proc = subprocess.Popen(
                    [chrome, "--headless", "--disable-gpu",
                     f"--user-data-dir={tmp}/profile", "--no-first-run",
                     "--no-default-browser-check", "--disable-extensions",
                     "--disable-sync", "--no-pdf-header-footer",
                     f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
                    stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                )
            start = time.monotonic()
            last_size, stable_at = 0, 0.0
            while True:
                exited = proc.poll() is not None
                now = time.monotonic()
                size = pdf_path.stat().st_size if pdf_path.is_file() else 0
                if size > 0:
                    if size != last_size:
                        last_size, stable_at = size, now
                    elif exited or now - stable_at >= 2:
                        break  # written and settled
                elif exited:
                    detail = f"exited (code {proc.returncode}) without producing a PDF"
                    break
                if now - start > timeout_s:
                    detail = f"timed out after {timeout_s}s"
                    break
                time.sleep(0.3)
        except OSError as e:
            detail = str(e)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        pass
        if not detail and pdf_path.is_file() and pdf_path.stat().st_size > 0:
            return ""
        # Failed: keep the browser's own output next to the report so the
        # reason survives even if the terminal warning scrolls away.
        tail = ""
        kept = pdf_path.with_name(pdf_path.name + ".browser.log")
        try:
            text = log.read_text(errors="replace").strip()
            tail = text.splitlines()[-1][:200] if text else ""
            kept.write_text(text + "\n")
        except OSError:
            kept = None
        parts = [detail or "no PDF produced"]
        if tail:
            parts.append(f"browser said: {tail}")
        if kept:
            parts.append(f"full log: {kept}")
        return " — ".join(parts)


def html_to_pdf(html_path, pdf_path):
    browsers = find_chromes()
    if not browsers:
        warn("No Chromium-based browser found for PDF output; skipping.")
        warn("Install Chrome/Edge/Brave or set POD_AUDIT_CHROME=/path/to/browser.")
        return False
    for chrome in browsers:
        info(f"Rendering PDF with {Path(chrome).name}...")
        detail = _print_pdf_attempt(chrome, html_path, pdf_path)
        if not detail:
            return True
        warn(f"{Path(chrome).name}: {detail}")
    warn("Every browser failed to produce the PDF. The HTML report is unaffected.")
    warn("To debug by hand, run:")
    warn(f'  "{browsers[0]}" --headless --print-to-pdf=/tmp/test.pdf \\')
    warn(f"      {html_path.resolve().as_uri()}")
    warn("then set POD_AUDIT_CHROME to a browser that works, or [report].pdf = false.")
    return False


# --------------------------------------------------------------------------
# Terminal summary
# --------------------------------------------------------------------------

def print_summary(cfg, today, current_sprint, per_dev, report_path, pdf_path=None, analysis=None):
    start, _ = sprint_window(cfg, current_sprint)
    day_no = (today - start).days + 1
    lat_mark = {"green": "G", "yellow": "Y", "red": "R", "": "-"}
    print()
    print("=" * 62)
    print(f" {cfg['pod']['name']} — {iteration_name(cfg, current_sprint)}, "
          f"day {day_no} of {cfg['sprint']['length_days']}  ({today.isoformat()})")
    print("=" * 62)
    if analysis and analysis.get("agenda"):
        print("\n Conversations to have today:")
        for it in analysis["agenda"]:
            who = f" with {it['dev']}" if it["dev"] else ""
            sid = f" {it['sid']}" if it["sid"] else ""
            print(f"   [{it['role']}{who}]{sid} {it['text']}")
    for dev, by_sprint in per_dev.items():
        cur = by_sprint.get(current_sprint, [])
        everything = [s for sp in by_sprint.values() for s in sp]
        done = total_points(cur) - remaining_points(cur)
        card = (analysis or {}).get("coaching", {}).get(dev)
        risk_note = ""
        if card and card.get("risk") not in (None, "none"):
            risk_note = f"  [coach: {card['risk']}]"
        print(f"\n {dev}: {len(cur)} stories, {done:g}/{total_points(cur):g} pts accepted{risk_note}")
        for s in everything:
            flags = " ".join(f"[{t}]" for t, _ in s.risks)
            marker = "✔" if s.done else ("✖" if s.blocked else "·")
            print(f"   {marker} {lat_mark.get(s.lateness, '-')} {s.sid} {s.state:<12} "
                  f"{s.completion:>3}%  {s.name[:44]}"
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

def run_lookup(cfg, sid):
    """Show how Rally names everything about one known story, so the config
    can be matched to reality (owner display name, iteration name, project,
    workspace)."""
    sid = sid.strip()
    if not re.fullmatch(r"[A-Za-z]+[0-9]+", sid):
        die(f"--lookup expects a FormattedID like US123456 or DE9911, got {sid!r}")
    rally = Rally(cfg)
    fetch = ("FormattedID,Name,ScheduleState,PlanEstimate,Owner,Iteration,"
             "Project,Workspace,StartDate,EndDate")
    found = False
    for entity, kind in (("hierarchicalrequirement", "Story"), ("defect", "Defect")):
        for r in rally.query(entity, f"(FormattedID = {sid})", fetch):
            found = True
            owner = r.get("Owner") or {}
            it = r.get("Iteration") or {}
            proj = r.get("Project") or {}
            ws = r.get("Workspace") or {}
            print(f"{kind} {r.get('FormattedID')}: {r.get('Name')}")
            print(f"  ScheduleState:     {r.get('ScheduleState')}")
            print(f"  Owner.DisplayName: {owner.get('_refObjectName')!r}")
            print(f"  Iteration.Name:    {it.get('_refObjectName')!r}")
            print(f"  Project:           {proj.get('_refObjectName')!r}")
            print(f"  Workspace:         {ws.get('_refObjectName')!r}")
            print()
    if found:
        print("To make the audit find items like this one:")
        print("  - [pod].devs must contain the Owner.DisplayName string exactly as above")
        print("  - [rally].iteration_name_format must produce the Iteration.Name above")
        print("    (e.g. Iteration.Name 'Sprint 44' -> \"Sprint {n}\")")
        print("  - if the Workspace isn't your default, set [rally].workspace")
    else:
        print(f"No story or defect with FormattedID {sid} is visible to this API key")
        print("in the configured scope. If it exists, the workspace/project scope is")
        print("wrong — set [rally].workspace (and [rally].project) in the config.")
        sys.exit(1)


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
    ap.add_argument("--lookup", metavar="US123456",
                    help="Show how Rally names one known story's owner/iteration/workspace")
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

    if args.lookup:
        run_lookup(cfg, args.lookup)
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
                        info(f"AI analysis for {story.sid}...")
                        diff = branch_diff_text(index, path, branch, default,
                                                cfg["ai"]["max_diff_chars"])
                        ai_story_analysis(story, diff, cfg)
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

    # Snapshot for burndown/aging, then run the leadership analyses.
    per_dev_current = {dev: sp.get(current_sprint, []) for dev, sp in per_dev.items()}
    record_snapshot(history, today, current_sprint, per_dev_current)

    all_stories = [s for sp in per_dev.values() for st in sp.values() for s in st]
    for dev, by_sprint in per_dev.items():
        for n, stories in by_sprint.items():
            for s in stories:
                score_lateness(s, n, current_sprint, elapsed_pct, cfg)

    targets = resolve_targets(cfg, current_sprint)
    waiting_reviews, review_load = review_stats(all_stories, today, cfg)
    blocked_aging, accept_queue = aging_queues(all_stories, history, today, cfg)
    churn = scope_churn(history, current_sprint, per_dev_current, today)
    digest = yesterday_digest(history, all_stories, today)
    wip_flags, hygiene = wip_and_hygiene(per_dev, current_sprint, cfg)
    out_today = set(cfg["capacity"]["out"])
    agenda = build_agenda(per_dev, blocked_aging, accept_queue, waiting_reviews,
                          wip_flags, cfg, out_today)

    comms_by_dev = {}
    if cfg["sources"]["sqlite"]:
        info("Searching communications sources...")
        for dev, by_sprint in per_dev.items():
            terms = [dev] + ([dev.split()[0]] if " " in dev else []) \
                + [s.sid for sp in by_sprint.values() for s in sp]
            comms_by_dev[dev] = search_comms(cfg, terms, cfg["sources"]["max_snippets_per_dev"])

    coaching = {}
    if ai_enabled and cfg["ai"]["coaching"]:
        history.setdefault("coaching", {})
        for dev, by_sprint in per_dev.items():
            info(f"Coaching analysis for {dev}...")
            facts = {
                "sprint_elapsed_pct": round(elapsed_pct),
                "out_today": dev in out_today,
                "stories": [
                    {
                        "id": s.sid, "name": s.name, "state": s.state,
                        "points": s.points, "completion_pct": s.completion,
                        "lateness": s.lateness,
                        "risks": [m for _, m in s.risks],
                        "tests_style": s.tests_style,
                        "prs": [
                            {"state": pr.state, "base": pr.base,
                             "comments": pr.comments,
                             "size": pr.additions + pr.deletions,
                             "checks_failed": pr.checks_failed}
                            for pr in s.all_prs
                        ],
                    }
                    for sp in by_sprint.values() for s in sp
                ],
                "recent_communications": comms_by_dev.get(dev, []),
                "pod_review_counts": review_load,
            }
            prev_card = (history["coaching"].get(dev) or {}).get("card")
            card = ai_dev_coaching(dev, facts, prev_card, cfg)
            if card:
                coaching[dev] = card
                history["coaching"][dev] = {"date": today.isoformat(), "card": card}

    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "w") as f:
            json.dump(history, f, indent=1)
    except OSError as e:
        die(f"Cannot write history file {history_path}: {e}")

    analysis = {
        "targets": targets,
        "waiting_reviews": waiting_reviews,
        "review_load": review_load,
        "blocked_aging": blocked_aging,
        "accept_queue": accept_queue,
        "churn": churn,
        "digest": digest,
        "wip_flags": wip_flags,
        "hygiene": hygiene,
        "agenda": agenda,
        "actions": load_action_items(cfg),
        "comms": comms_by_dev,
        "coaching": coaching,
        "ai_enabled": ai_enabled,
        "out_today": out_today,
    }
    html_out = render_report(cfg, today, current_sprint, sprints, per_dev, analysis)
    slug = re.sub(r"[^a-z0-9]+", "-", cfg["pod"]["name"].lower()).strip("-") or "pod"
    try:
        out_dir = (cfg["_dir"] / cfg["report"]["output_dir"]).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"{slug}-audit-{today.isoformat()}.html"
        report_path.write_text(
            "<!doctype html>\n<html lang='en'>\n<meta charset='utf-8'>\n"
            + html_out + "\n</html>\n")
    except OSError as e:
        die(f"Cannot write report to {cfg['report']['output_dir']}: {e}")

    pdf_path = None
    if cfg["report"]["pdf"] and not args.no_pdf:
        candidate = report_path.with_suffix(".pdf")
        if html_to_pdf(report_path, candidate):
            pdf_path = candidate

    print_summary(cfg, today, current_sprint, per_dev, report_path, pdf_path, analysis)
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
