#!/usr/bin/env python3
#
# weekly-digest.py
#
# Scrape everything each dev demonstrably worked on over the last N days and
# write it to one plain-text file, headed by a prompt telling Copilot (or any
# LLM) to turn it into a weekly report: highlights, accomplishments, and
# planned activities for next week.
#
# This script does NOT write the report. It gathers evidence and hands it
# over. Nothing here is inferred or scored — every line is something that
# actually happened, with a date and a source, so the model writing the
# report has facts rather than vibes.
#
# What it collects, per dev, for the window:
#   - Rally    stories/defects they own that changed in the window: what was
#              accepted, what moved, what is blocked, what is still open
#   - Git      commits they authored across every configured repo, with churn,
#              the branches touched, and the story ids referenced
#   - PRs      pull requests they opened, merged or had closed in the window,
#              plus (GitHub only) reviews they gave someone else
#   - Comms    snippets naming them in the configured sqlite transcript /
#              chat / email databases
#   - Ahead    what they carry into next week: open PRs, in-progress and
#              blocked stories
#
# It reuses pod-audit.toml — same devs, same Rally, same repos, same sources.
#
# Usage:
#   weekly-digest.py                          # last 7 days, all devs
#   weekly-digest.py --days 14
#   weekly-digest.py --date 2026-09-05        # pretend today is this date
#   weekly-digest.py --split                  # one file per dev
#   weekly-digest.py --list-authors           # see git authors, to map names
#
# Options:
#   --config <path>     Config file (default: pod-audit.toml next to script)
#   --date <YYYY-MM-DD> Treat this as "today" (default: today)
#   --days <n>          Window length in days (default: 7)
#   --dev <name>        Only this dev (repeatable; default: every [pod].devs)
#   --out <path>        Output file (default: reports/weekly-digest-<date>.txt)
#   --split             Write one file per dev instead of one combined file
#   --no-rally          Skip Rally
#   --no-git            Skip git commits
#   --no-prs            Skip pull requests
#   --no-comms          Skip the sqlite sources
#   --list-authors      List every git author seen in the window and exit.
#                       Use it to fill in [pod.aliases] when a dev's commits
#                       are not being found.
#
# Matching a Rally name to a git author:
#   Rally knows "Jane Developer"; git knows "jane.developer@corp.com". The
#   script derives the obvious candidates (full name, first.last, flast,
#   first) and matches case-insensitively. When that misses, map it
#   explicitly in the config and it will stop guessing:
#
#       [pod.aliases]
#       "Jane Developer" = ["jane.developer@corp.com", "jane-dev"]
#
#   Run --list-authors to see exactly what git has.

import argparse
import datetime as dt
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_pod_audit():
    """pod-audit.py is not importable by name (hyphen), so load it by path.
    Everything this script needs to talk to Rally, git and the sqlite sources
    already lives there; duplicating it would mean two things to keep right."""
    path = SCRIPT_DIR / "pod-audit.py"
    if not path.is_file():
        print(f"ERROR: pod-audit.py not found next to {Path(__file__).name}", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("pod_audit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pa = _load_pod_audit()
die, info, warn, run = pa.die, pa.info, pa.warn, pa.run


# --------------------------------------------------------------------------
# Identity: Rally display name -> git author / forge handle candidates
# --------------------------------------------------------------------------

def author_patterns(cfg, dev):
    """Candidate git author strings for a Rally dev name. Explicit config
    wins; otherwise derive the usual shapes an org uses."""
    aliases = cfg["pod"].get("aliases") or {}
    if dev in aliases:
        return [a for a in aliases[dev] if a]

    out = [dev]
    if "@" in dev:                      # config already used an email
        out.append(dev.split("@", 1)[0])
        return out
    parts = [p for p in re.split(r"\s+", dev.strip()) if p]
    if len(parts) >= 2:
        first, last = parts[0].lower(), parts[-1].lower()
        out += [f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}",
                f"{first}-{last}", f"{last}.{first}", first, last]
    elif parts:
        out.append(parts[0].lower())
    return out


def all_git_authors(index, since, until):
    """Every author seen in the window, with commit counts, across all repos."""
    seen = {}
    for path in index.repos:
        index.ensure_fetched(path)
        rc, out, _ = run(["git", "log", "--all", f"--since={since}", f"--until={until}",
                          "--pretty=format:%an|%ae"], cwd=path, timeout=180)
        if rc != 0:
            continue
        for line in out.splitlines():
            if line.strip():
                seen[line] = seen.get(line, 0) + 1
    return sorted(seen.items(), key=lambda kv: -kv[1])


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------

STORY_ID_RE = re.compile(r"\b(US|DE|TA|TS)\d{4,}\b", re.I)


def rally_activity(rally, dev, since, until):
    """Stories/defects owned by dev that changed in the window. No iteration
    filter: a week is not a sprint, and work slips across both."""
    owner_attr = "Owner.UserName" if "@" in dev else "Owner.DisplayName"
    q = (f'(({owner_attr} = "{dev}") AND '
         f'((LastUpdateDate >= "{since}") AND (LastUpdateDate <= "{until}T23:59:59.000Z")))')
    fetch = pa.STORY_FETCH + "," + rally.ac_field
    items = []
    for entity, kind in (("hierarchicalrequirement", "Story"), ("defect", "Defect")):
        try:
            for raw in rally.query(entity, q, fetch):
                items.append((kind, raw))
        except Exception as e:                       # noqa: BLE001 - never sink the run
            warn(f"Rally {entity} query failed for {dev}: {e}")
    out = []
    for kind, raw in items:
        try:
            out.append(pa.build_story(rally, kind, raw))
        except Exception as e:                       # noqa: BLE001
            warn(f"Could not read a Rally {kind} for {dev}: {e}")
    out.sort(key=lambda s: s.sid)
    return out


def git_commits(index, patterns, since, until):
    """Commits authored by any of `patterns`, deduped by sha across repos."""
    # \x1f (UNIT SEPARATOR), not \x1e: str.splitlines() treats \x1e as a line
    # boundary and would eat the field separator. For the same reason the
    # output is split on "\n" only — splitlines() also breaks on \x0b, \x0c,
    # \x1c, \x1d and \u2028, any of which can appear in a commit subject.
    sep = "\x1f"
    fmt = sep.join(["%H", "%an", "%ae", "%ad", "%s"])
    commits, seen = [], set()
    for path in index.repos:
        index.ensure_fetched(path)
        for pat in patterns:
            rc, out, _ = run(
                ["git", "log", "--all", "--no-merges", "-i", f"--author={pat}",
                 f"--since={since}", f"--until={until} 23:59:59",
                 "--date=short", f"--pretty=format:{fmt}", "--numstat"],
                cwd=path, timeout=240)
            if rc != 0 or not out.strip():
                continue
            cur = None
            for line in out.split("\n"):
                if sep in line:
                    sha, an, ae, ad, subj = line.split(sep, 4)
                    if sha in seen:
                        cur = None
                        continue
                    seen.add(sha)
                    cur = {"repo": path.name, "sha": sha[:9], "author": an, "email": ae,
                           "date": ad, "subject": subj, "files": 0, "ins": 0, "dels": 0,
                           "paths": []}
                    commits.append(cur)
                elif cur is not None and line.strip():
                    bits = line.split("\t")
                    if len(bits) == 3:
                        ins, dels, p = bits
                        cur["files"] += 1
                        cur["ins"] += int(ins) if ins.isdigit() else 0
                        cur["dels"] += int(dels) if dels.isdigit() else 0
                        if len(cur["paths"]) < 12:
                            cur["paths"].append(p)
    commits.sort(key=lambda c: (c["date"], c["repo"]))
    return commits


def branches_touched(index, patterns, since, until):
    """Remote branches whose tip commit is by this dev inside the window.

    Deliberately one `for-each-ref` per repo rather than a `git log` per
    branch: a real repo has hundreds of branches and the per-branch form
    turned this into thousands of subprocesses. The trade-off is that a
    branch someone else has since committed on top of will not show here —
    the commit list below is the complete record either way."""
    lowered = {p.lower() for p in patterns}
    out = []
    for path in index.repos:
        index.ensure_fetched(path)
        rc, res, _ = run(
            ["git", "for-each-ref", "--sort=-committerdate",
             "--format=%(refname:short)\t%(committerdate:short)\t%(authorname)\t%(authoremail)",
             f"refs/remotes/{index.remote}"], cwd=path, timeout=120)
        if rc != 0:
            continue
        for line in res.splitlines():
            bits = line.split("\t")
            if len(bits) != 4:
                continue
            ref, cdate, aname, aemail = bits
            # git shortens refs/remotes/origin/HEAD to just "origin"
            if ref == index.remote or ref.endswith("/HEAD"):
                continue
            short = ref.split("/", 1)[1] if "/" in ref else ref
            if not (since <= cdate <= until):
                continue
            hay = f"{aname} {aemail}".lower()
            if any(p in hay for p in lowered):
                out.append((path.name, short, cdate))
    return sorted(set(out))


def github_activity(cfg, index, patterns, since, until):
    """PRs authored, and reviews given, per repo. Best-effort: gh may not be
    installed, or a repo may not be on GitHub."""
    authored, reviewed = [], []
    if not run(["which", "gh"])[1]:
        return authored, reviewed, "gh CLI not installed"
    lowered = {p.lower() for p in patterns}
    note = ""
    for path, meta in index.repos.items():
        if "github.com" not in (meta.get("url") or ""):
            continue
        rc, out, err = run(
            ["gh", "pr", "list", "--state", "all", "--limit", "150", "--json",
             "number,title,url,state,author,createdAt,updatedAt,mergedAt,"
             "baseRefName,headRefName,additions,deletions,isDraft"],
            cwd=path, timeout=180)
        if rc != 0:
            note = f"gh pr list failed in {path.name}: {err[:120]}"
            continue
        try:
            prs = json.loads(out or "[]")
        except json.JSONDecodeError:
            continue
        for pr in prs:
            login = ((pr.get("author") or {}).get("login") or "")
            name = ((pr.get("author") or {}).get("name") or "")
            touched = (pr.get("mergedAt") or pr.get("updatedAt") or "")[:10]
            created = (pr.get("createdAt") or "")[:10]
            if not (since <= touched <= until or since <= created <= until):
                continue
            if login.lower() in lowered or name.lower() in lowered or any(
                    p in login.lower() or p in name.lower() for p in lowered if len(p) > 3):
                pr["_repo"] = path.name
                authored.append(pr)
        # Reviews given — a collaboration signal the commit log never shows.
        for pat in patterns:
            rc, out, _ = run(
                ["gh", "pr", "list", "--state", "all", "--limit", "60", "--search",
                 f"reviewed-by:{pat} updated:>={since}", "--json",
                 "number,title,url,state,author,updatedAt"], cwd=path, timeout=120)
            if rc != 0:
                continue
            try:
                for pr in json.loads(out or "[]"):
                    pr["_repo"] = path.name
                    if not any(r["url"] == pr["url"] for r in reviewed):
                        reviewed.append(pr)
            except json.JSONDecodeError:
                pass
    return authored, reviewed, note


def bitbucket_note(cfg, index):
    """Bitbucket PR collection is not implemented here; say so rather than
    silently reporting zero PRs for a Bitbucket-hosted pod."""
    hosted = [p.name for p, m in index.repos.items()
              if "bitbucket.org" in (m.get("url") or "")]
    if not hosted:
        return ""
    return (f"PRs not collected for Bitbucket repo(s) {', '.join(hosted)} — this "
            "script reads PRs from GitHub via gh only; their commits ARE included")


# --------------------------------------------------------------------------
# The prompt handed to Copilot
# --------------------------------------------------------------------------

PROMPT = """\
You are writing the weekly report for a software delivery pod. Everything
below the line marked EVIDENCE is raw, factual data scraped from Rally, git,
the pull-request host and team chat for the reporting period shown. Each item
carries a date and a source.

Write a report with exactly these three sections:

1. HIGHLIGHTS OF THE WEEK
   The three to six things a manager outside the pod should know. Lead with
   outcomes that shipped or unblocked someone, not with activity counts.
   Write them as short prose, one bullet each, naming the dev and the story.

2. ACCOMPLISHMENTS
   Grouped per developer. For each dev, list what they actually completed —
   stories accepted, PRs merged, defects fixed, reviews given. Each bullet
   must trace to at least one item in the evidence; cite the story id or PR
   number in parentheses. If a dev has little evidence, say so plainly rather
   than padding it.

3. PLANNED ACTIVITIES FOR NEXT WEEK
   Grouped per developer, derived from what is open: in-progress stories,
   open PRs, blocked items, and anything carried over. Call out explicitly
   anything that is blocked and who needs to unblock it.

Rules:
- Use only the evidence below. Do not invent stories, numbers, or dates.
- Where the evidence is thin or contradictory, say that in one short line
  rather than guessing or smoothing it over.
- Do not editorialise about individual performance; describe work, not
  people's character.
- Prefer plain language over agile jargon. No emoji.
- Keep the whole report under roughly 700 words.
"""


# --------------------------------------------------------------------------
# Rendering the evidence file
# --------------------------------------------------------------------------

def rule(ch="=", n=78):
    return ch * n


def section(title):
    return f"\n{rule('-')}\n{title}\n{rule('-')}"


def fmt_story(s):
    bits = [f"  [{s.sid}] {s.name}",
            f"      type={s.kind} state={s.state}"]
    if s.points is not None:
        bits[-1] += f" points={s.points:g}"
    if s.blocked:
        bits.append(f"      BLOCKED: {s.blocked_reason or 'no reason recorded'}")
    dates = []
    for label, val in (("in-progress", s.in_progress), ("accepted", s.accepted),
                       ("last-update", s.last_update)):
        if val:
            dates.append(f"{label}={val.isoformat()}")
    if dates:
        bits.append("      " + " ".join(dates))
    if s.acs:
        bits.append(f"      acceptance criteria ({len(s.acs)}):")
        for ac in s.acs[:6]:
            bits.append(f"        - {ac}")
    bits.append(f"      url={s.url}")
    return "\n".join(bits)


def render_dev(dev, data, cfg, since, until):
    L = [f"\n\n{rule('#')}",
         f"DEVELOPER: {dev}",
         rule("#")]

    stories = data["stories"]
    accepted = [s for s in stories if s.accepted and since <= s.accepted.isoformat() <= until]
    acc_ids = {s.sid for s in accepted}
    blocked = [s for s in stories if s.blocked and s.sid not in acc_ids]
    bl_ids = {s.sid for s in blocked}
    in_prog = [s for s in stories
               if not s.done and s.sid not in acc_ids and s.sid not in bl_ids]
    done_ids = acc_ids | bl_ids | {s.sid for s in in_prog}
    other = [s for s in stories if s.sid not in done_ids]

    L.append(section(f"RALLY — ACCEPTED IN THIS WINDOW ({len(accepted)})"))
    L.append("\n".join(fmt_story(s) for s in accepted) if accepted
             else "  (nothing reached Accepted in this window)")

    L.append(section(f"RALLY — IN PROGRESS AT END OF WINDOW ({len(in_prog)})"))
    L.append("\n".join(fmt_story(s) for s in in_prog) if in_prog
             else "  (nothing in progress)")

    L.append(section(f"RALLY — BLOCKED ({len(blocked)})"))
    L.append("\n".join(fmt_story(s) for s in blocked) if blocked
             else "  (nothing blocked)")

    if other:
        L.append(section(f"RALLY — OTHER STORIES TOUCHED ({len(other)})"))
        L.append("\n".join(fmt_story(s) for s in other))

    commits = data["commits"]
    tot_i = sum(c["ins"] for c in commits)
    tot_d = sum(c["dels"] for c in commits)
    ids = sorted({m.group(0).upper() for c in commits
                  for m in [STORY_ID_RE.search(c["subject"])] if m})
    L.append(section(f"GIT COMMITS ({len(commits)} commits, +{tot_i}/-{tot_d} lines)"))
    if commits:
        if ids:
            L.append(f"  story ids referenced in commit messages: {', '.join(ids)}")
        for c in commits:
            L.append(f"  {c['date']} {c['repo']}@{c['sha']} +{c['ins']}/-{c['dels']} "
                     f"({c['files']} files)")
            L.append(f"      {c['subject']}")
            if c["paths"]:
                L.append(f"      touched: {', '.join(c['paths'][:8])}"
                         + (" ..." if c["files"] > 8 else ""))
    else:
        L.append("  (no commits found — if this looks wrong, the git author name may not "
                 "match; run --list-authors and set [pod.aliases])")

    br = data["branches"]
    L.append(section(f"BRANCHES WORKED ON ({len(br)})"))
    L.append("\n".join(f"  {repo}: {name}  (tip {d})" for repo, name, d in br) if br
             else "  (none)")

    prs = data["prs"]
    L.append(section(f"PULL REQUESTS AUTHORED ({len(prs)})"))
    if prs:
        for pr in prs:
            merged = (pr.get("mergedAt") or "")[:10]
            state = "MERGED " + merged if merged else pr.get("state", "?")
            L.append(f"  #{pr['number']} [{state}] {pr['title']}")
            L.append(f"      {pr['_repo']}: {pr.get('headRefName','?')} -> "
                     f"{pr.get('baseRefName','?')} "
                     f"+{pr.get('additions',0)}/-{pr.get('deletions',0)}"
                     + ("  (draft)" if pr.get("isDraft") else ""))
            L.append(f"      opened={(pr.get('createdAt') or '')[:10]} "
                     f"updated={(pr.get('updatedAt') or '')[:10]}")
            L.append(f"      url={pr['url']}")
    else:
        L.append("  (none found)")

    rev = data["reviews"]
    L.append(section(f"CODE REVIEWS GIVEN TO OTHERS ({len(rev)})"))
    if rev:
        for pr in rev:
            author = (pr.get("author") or {}).get("login", "?")
            L.append(f"  #{pr['number']} [{pr.get('state','?')}] {pr['title']}")
            L.append(f"      {pr['_repo']}, author={author}, "
                     f"updated={(pr.get('updatedAt') or '')[:10]}")
            L.append(f"      url={pr['url']}")
    else:
        L.append("  (none found)")

    comms = data["comms"]
    L.append(section(f"MENTIONS IN TRANSCRIPTS / CHAT / EMAIL ({len(comms)})"))
    if comms:
        for sn in comms:
            src = f"{sn['source']}.{sn['table']}" + (f" {sn['date']}" if sn.get("date") else "")
            L.append(f"  [{src}] {sn['snippet']}")
    else:
        L.append("  (no sources configured, or nothing matched)")

    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    L.append(section("CARRYING INTO NEXT WEEK"))
    carry = []
    for s in in_prog:
        carry.append(f"  story {s.sid} ({s.state}) — {s.name}")
    for s in blocked:
        carry.append(f"  BLOCKED {s.sid} — {s.name} "
                     f"[{s.blocked_reason or 'no reason recorded'}]")
    for p in open_prs:
        carry.append(f"  open PR #{p['number']} — {p['title']} ({p['_repo']})")
    L.append("\n".join(carry) if carry else "  (nothing outstanding)")
    return "\n".join(L)


def render_header(cfg, today, since, until, devs, notes):
    L = [PROMPT, rule(), "EVIDENCE", rule(),
         f"Pod:               {cfg['pod']['name']}",
         f"Reporting period:  {since} to {until} (inclusive)",
         f"Generated:         {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
         f"Developers:        {', '.join(devs)}"]
    try:
        n = pa.sprint_for_date(cfg, today)
        s, e = pa.sprint_window(cfg, n)
        L.append(f"Sprint:            {pa.iteration_name(cfg, n)} "
                 f"({s.isoformat()} to {e.isoformat()})")
    except Exception:                                # noqa: BLE001
        pass
    for note in notes:
        if note:
            L.append(f"Note:              {note}")
    return "\n".join(L)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--config", default=str(SCRIPT_DIR / "pod-audit.toml"))
    ap.add_argument("--date")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dev", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--split", action="store_true")
    ap.add_argument("--no-rally", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--no-prs", action="store_true")
    ap.add_argument("--no-comms", action="store_true")
    ap.add_argument("--list-authors", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.help:
        doc = []
        for line in Path(__file__).read_text().splitlines()[1:]:
            if not line.startswith("#"):
                break
            doc.append(line[2:] if line.startswith("# ") else line[1:])
        print("\n".join(doc))
        return

    cfg = pa.load_config(args.config)
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    since_d = today - dt.timedelta(days=args.days - 1)
    since, until = since_d.isoformat(), today.isoformat()

    devs = args.dev or cfg["pod"]["devs"]
    if not devs:
        die("No devs: set [pod].devs in the config or pass --dev")

    index = None
    if not args.no_git or not args.no_prs or args.list_authors:
        index = pa.RepoIndex(cfg)
        if not index.repos:
            warn("No usable repos in [git].repos — git and PR sections will be empty")

    if args.list_authors:
        if not index or not index.repos:
            die("No usable repos in [git].repos")
        info(f"Git authors with commits {since}..{until}:")
        rows = all_git_authors(index, since, until)
        if not rows:
            print("  (none — is the window right, and are the repos fetched?)")
        for who, n in rows:
            name, email = who.split("|", 1)
            print(f"  {n:4d}  {name}  <{email}>")
        print("\nMap any of these to a Rally dev name in the config:\n")
        print("  [pod.aliases]")
        print('  "Jane Developer" = ["jane.developer@corp.com", "jane-dev"]')
        return

    rally = None
    if not args.no_rally:
        try:
            rally = pa.Rally(cfg)
        except SystemExit:
            warn("No Rally API key (set [rally].api_key or RALLY_API_KEY) — "
                 "continuing with git and PR evidence only")
        except Exception as e:                       # noqa: BLE001
            warn(f"Rally unavailable, continuing without it: {e}")

    notes = []
    per_dev = {}
    for dev in devs:
        info(f"Collecting {dev}...")
        pats = author_patterns(cfg, dev)
        data = {"stories": [], "commits": [], "branches": [], "prs": [],
                "reviews": [], "comms": [], "patterns": pats}
        if rally is not None:
            data["stories"] = rally_activity(rally, dev, since, until)
        if index and not args.no_git:
            data["commits"] = git_commits(index, pats, since, until)
            data["branches"] = branches_touched(index, pats, since, until)
        if index and not args.no_prs:
            authored, reviewed, note = github_activity(cfg, index, pats, since, until)
            data["prs"], data["reviews"] = authored, reviewed
            if note and note not in notes:
                notes.append(note)
            bnote = bitbucket_note(cfg, index)
            if bnote and bnote not in notes:
                notes.append(bnote)
        if not args.no_comms:
            try:
                data["comms"] = pa.search_comms(
                    cfg, [dev] + [p for p in pats if len(p) > 4],
                    cfg["sources"].get("max_snippets_per_dev", 6))
            except Exception as e:                   # noqa: BLE001
                warn(f"Comms search failed for {dev}: {e}")
        per_dev[dev] = data

    out_dir = (cfg["_dir"] / cfg["report"]["output_dir"]).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        die(f"Cannot create {out_dir}: {e}")

    header = render_header(cfg, today, since, until, list(per_dev), notes)
    written = []
    if args.split:
        for dev, data in per_dev.items():
            slug = re.sub(r"[^a-z0-9]+", "-", dev.lower()).strip("-")
            p = Path(args.out).parent / f"weekly-{slug}-{until}.txt" if args.out \
                else out_dir / f"weekly-{slug}-{until}.txt"
            p.write_text(header + render_dev(dev, data, cfg, since, until) + "\n")
            written.append(p)
    else:
        body = "".join(render_dev(d, x, cfg, since, until) for d, x in per_dev.items())
        p = Path(args.out) if args.out else out_dir / f"weekly-digest-{until}.txt"
        p.write_text(header + body + "\n")
        written.append(p)

    tot_c = sum(len(d["commits"]) for d in per_dev.values())
    tot_s = sum(len(d["stories"]) for d in per_dev.values())
    tot_p = sum(len(d["prs"]) for d in per_dev.values())
    print()
    print(rule())
    print(" WEEKLY DIGEST READY")
    print(rule())
    print(f" Period:   {since} to {until}")
    print(f" Devs:     {len(per_dev)}")
    print(f" Scraped:  {tot_s} Rally item(s), {tot_c} commit(s), {tot_p} PR(s)")
    for p in written:
        print(f" File:     {p}")
    print(rule())
    print(" Paste the file into Copilot — the prompt is already at the top.")
    if tot_c == 0 and not args.no_git:
        print(" No commits found: run --list-authors and set [pod.aliases].")


if __name__ == "__main__":
    main()
