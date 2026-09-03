#!/usr/bin/env python3
#
# copilot-pack.py
#
# Two things, one command:
#   1. Concatenate this project's own source code into a single .txt file —
#      only code you wrote, never dependencies, build output or anything
#      installed or compiled.
#   2. Pull a Rally story/defect's description, notes and acceptance
#      criteria, wrap them in a prompt, and put that on the Mac clipboard.
#
# You then attach the .txt to Copilot and paste (Cmd-V) the prompt, and ask
# it how to fix the thing the story is asking for.
#
# What counts as "local code":
#   In a git repo the file list comes from `git ls-files` plus untracked
#   files git would keep — so anything .gitignore excludes (node_modules,
#   target/, dist/, .venv, packages/) is already gone, which is exactly the
#   line you want. Outside a repo it falls back to a directory walk with the
#   usual vendor directories denied. On top of that: a source-extension
#   allowlist, lock/minified/generated files dropped, binaries sniffed out,
#   and per-file plus total size caps so the pack stays inside a model's
#   context window.
#
# Usage:
#   copilot-pack.py --story US847435
#   copilot-pack.py --story DE9911 --path ~/Projects/backend
#   copilot-pack.py --path . --no-story            # just pack the code
#
# Options:
#   --story <id>        Rally FormattedID (US123456 / DE9911). Its
#                       description + notes go to the clipboard.
#   --path <dir>        Project root to pack (default: current directory)
#   --out <file>        Output file (default: <project>-code.txt beside you)
#   --config <path>     pod-audit.toml, for the Rally credentials
#   --max-file-kb <n>   Skip any single file bigger than this (default 200)
#   --max-total-mb <n>  Stop packing past this much code (default 8)
#   --ext <list>        Extra comma-separated extensions to include
#   --exclude <glob>    Skip paths matching this glob (repeatable)
#   --tests             Include test files (excluded by default: they crowd
#                       out the code under discussion)
#   --no-story          Don't touch Rally, just pack the code
#   --no-clipboard      Print the prompt instead of copying it
#   --stdout            Write the pack to stdout instead of a file

import argparse
import datetime as dt
import fnmatch
import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

SOURCE_EXT = {
    # languages
    ".java", ".kt", ".kts", ".scala", ".groovy",
    ".cs", ".fs", ".vb",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".rb", ".go", ".rs", ".php", ".pl", ".lua", ".dart", ".swift", ".m",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".hh",
    ".sh", ".bash", ".zsh", ".ps1",
    ".sql", ".graphql", ".gql", ".proto", ".thrift",
    # markup / style that carries behaviour
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # build & config that explains wiring
    ".gradle", ".properties", ".toml", ".tf", ".tfvars",
    ".yaml", ".yml", ".xml", ".json", ".ini", ".cfg", ".env.example",
    ".feature", ".md",
}

# Directories that are never your code, even if someone committed them.
DENY_DIRS = {
    "node_modules", "bower_components", "jspm_packages", "vendor", "packages",
    "target", "build", "dist", "out", "bin", "obj", "output", "release",
    ".venv", "venv", "env", "virtualenv", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".gradle", ".m2", ".nuget",
    ".git", ".svn", ".hg", ".idea", ".vs", ".vscode", ".settings",
    "coverage", "htmlcov", ".nyc_output", "test-results", "allure-results",
    ".next", ".nuxt", ".svelte-kit", ".angular", ".parcel-cache", ".turbo",
    "site-packages", "dist-packages", "Pods", "DerivedData",
}

DENY_FILE_GLOBS = [
    "*.min.js", "*.min.css", "*.bundle.js", "*.bundle.css", "*.map",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "Gemfile.lock", "poetry.lock", "Pipfile.lock", "composer.lock",
    "Cargo.lock", "go.sum", "*.lock",
    "*.generated.*", "*_pb2.py", "*_pb.go", "*.pb.go", "*.g.dart",
    "*.designer.cs", "*.Designer.cs", "*.feature.cs",
    "*.snap", "*.pyc", "*.class", "*.jar", "*.war", "*.dll", "*.exe", "*.so",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.pdf", "*.zip",
    "*.woff", "*.woff2", "*.ttf", "*.eot", "*.mp4", "*.mp3",
]

TEST_PATTERNS = [
    "*/test/*", "*/tests/*", "*/__tests__/*", "*/spec/*", "*/e2e/*",
    "*Test.java", "*Tests.java", "*IT.java", "*.test.ts", "*.test.tsx",
    "*.test.js", "*.test.jsx", "*.spec.ts", "*.spec.tsx", "*.spec.js",
    "test_*.py", "*_test.py", "*_test.go", "*Tests.cs", "*Test.cs",
]


def _load_pod_audit():
    path = SCRIPT_DIR / "pod-audit.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("pod_audit", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:                            # noqa: BLE001
        print(f"WARNING: could not load pod-audit.py ({e}); "
              "Rally lookup unavailable", file=sys.stderr)
        return None


pa = _load_pod_audit()


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"==> {msg}")


# --------------------------------------------------------------------------
# Finding the project's own files
# --------------------------------------------------------------------------

def git_files(root):
    """Tracked files plus untracked ones git would keep. Returns None when
    this is not a git repo, so the caller can fall back to walking."""
    def g(*args):
        r = subprocess.run(["git", *args], cwd=root, capture_output=True,
                           text=True, timeout=120)
        return r.stdout.splitlines() if r.returncode == 0 else None

    if g("rev-parse", "--is-inside-work-tree") is None:
        return None
    tracked = g("ls-files", "-z") or []
    if tracked and "\x00" in "".join(tracked):
        tracked = "\n".join(tracked).replace("\x00", "\n").split("\n")
    untracked = g("ls-files", "--others", "--exclude-standard") or []
    seen, out = set(), []
    for rel in [*tracked, *untracked]:
        rel = rel.strip()
        if rel and rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def walk_files(root):
    out = []
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in DENY_DIRS for part in p.relative_to(root).parts):
            continue
        out.append(str(p.relative_to(root)))
    return out


def is_test(rel):
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/" + rel, pat)
               for pat in TEST_PATTERNS)


def looks_binary(path, probe=4096):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(probe)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    if not chunk:
        return False
    # A high proportion of bytes outside printable ASCII means binary.
    text = bytes(range(32, 127)) + b"\n\r\t\f\b"
    return sum(b not in text for b in chunk) / len(chunk) > 0.30


def collect(root, args):
    rels = git_files(root)
    mode = "git ls-files"
    if rels is None:
        rels = walk_files(root)
        mode = "directory walk (not a git repo)"

    kept, skipped = [], {"ext": 0, "test": 0, "deny": 0, "big": 0,
                         "binary": 0, "excluded": 0, "missing": 0}
    extra = {e if e.startswith(".") else "." + e
             for e in (args.ext.split(",") if args.ext else []) if e.strip()}
    allow = SOURCE_EXT | extra
    max_file = args.max_file_kb * 1024

    for rel in sorted(rels):
        p = root / rel
        parts = Path(rel).parts
        if any(part in DENY_DIRS for part in parts):
            skipped["deny"] += 1
            continue
        name = Path(rel).name
        if any(fnmatch.fnmatch(name, g) for g in DENY_FILE_GLOBS):
            skipped["deny"] += 1
            continue
        if args.exclude and any(fnmatch.fnmatch(rel, g) for g in args.exclude):
            skipped["excluded"] += 1
            continue
        if p.suffix.lower() not in allow:
            skipped["ext"] += 1
            continue
        if not args.tests and is_test(rel):
            skipped["test"] += 1
            continue
        if not p.is_file():
            skipped["missing"] += 1
            continue
        try:
            size = p.stat().st_size
        except OSError:
            skipped["missing"] += 1
            continue
        if size > max_file:
            skipped["big"] += 1
            continue
        if looks_binary(p):
            skipped["binary"] += 1
            continue
        kept.append((rel, size))
    return kept, skipped, mode


def build_pack(root, kept, budget):
    """Concatenate, newest-shallowest first so the most navigable files land
    before the budget runs out."""
    chunks, packed, used, dropped = [], [], 0, []
    for rel, size in sorted(kept, key=lambda k: (Path(k[0]).parts.__len__(), k[0])):
        p = root / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            warn(f"unreadable, skipping: {rel} ({e})")
            continue
        lines = text.count("\n") + 1
        head = (f"{'=' * 78}\nFILE: {rel}\nLINES: {lines}  SIZE: {size / 1024:.1f} KB\n"
                f"{'=' * 78}\n")
        block = head + text + ("\n" if not text.endswith("\n") else "")
        if used + len(block) > budget:
            dropped.append(rel)
            continue
        chunks.append(block)
        packed.append((rel, lines, size))
        used += len(block)
    return chunks, packed, dropped


# --------------------------------------------------------------------------
# Rally
# --------------------------------------------------------------------------

STORY_FETCH = ("FormattedID,Name,ObjectID,ScheduleState,Description,Notes,"
               "PlanEstimate,Blocked,BlockedReason,Owner,Iteration,Project")


def fetch_story(cfg, sid):
    """Description, notes and acceptance criteria for one FormattedID."""
    rally = pa.Rally(cfg)
    fetch = STORY_FETCH + "," + rally.ac_field
    q = f'(FormattedID = "{sid}")'
    kinds = (("defect", "Defect"), ("hierarchicalrequirement", "Story"))
    if sid.upper().startswith("US"):
        kinds = tuple(reversed(kinds))
    for entity, kind in kinds:
        try:
            rows = rally.query(entity, q, fetch)
        except Exception as e:                        # noqa: BLE001
            warn(f"Rally {entity} query failed: {e}")
            continue
        if rows:
            raw = rows[0]
            return {
                "kind": kind,
                "sid": raw.get("FormattedID") or sid,
                "name": raw.get("Name") or "",
                "state": raw.get("ScheduleState") or "",
                "owner": (raw.get("Owner") or {}).get("_refObjectName") or "",
                "blocked": bool(raw.get("Blocked")),
                "blocked_reason": raw.get("BlockedReason") or "",
                "description": "\n".join(pa.html_to_lines(raw.get("Description") or "")),
                "notes": "\n".join(pa.html_to_lines(raw.get("Notes") or "")),
                "acs": pa.parse_acceptance_criteria(raw, rally.ac_field),
                "url": rally.story_url(kind, raw.get("ObjectID", "")),
            }
    return None


def build_prompt(story, out_name, packed, dropped, root):
    L = []
    if story:
        L.append(f"I need to fix Rally {story['kind'].lower()} {story['sid']}"
                 f"{' — ' + story['name'] if story['name'] else ''}.")
        L.append("")
        L.append(f"{story['kind'].upper()} {story['sid']}")
        L.append(f"Title:  {story['name']}")
        if story["state"]:
            L.append(f"State:  {story['state']}")
        if story["owner"]:
            L.append(f"Owner:  {story['owner']}")
        if story["blocked"]:
            L.append(f"BLOCKED: {story['blocked_reason'] or 'no reason recorded'}")
        if story["url"]:
            L.append(f"Link:   {story['url']}")
        L.append("")
        L.append("DESCRIPTION")
        L.append("-" * 60)
        L.append(story["description"] or "(empty in Rally)")
        L.append("")
        L.append("NOTES")
        L.append("-" * 60)
        L.append(story["notes"] or "(empty in Rally)")
        if story["acs"]:
            L.append("")
            L.append("ACCEPTANCE CRITERIA")
            L.append("-" * 60)
            for ac in story["acs"]:
                L.append(f"- {ac}")
    else:
        L.append("I need to make a change to the codebase below.")

    total_lines = sum(l for _r, l, _s in packed)
    L += [
        "",
        "=" * 60,
        f"The complete source of {root.name} is in the attached file "
        f"{out_name} — {len(packed)} files, {total_lines:,} lines. It contains "
        "only first-party code; dependencies and build output are excluded.",
    ]
    if dropped:
        L.append(f"({len(dropped)} file(s) did not fit the size budget and are "
                 "not in the pack; say if you need one and I will send it.)")
    L += [
        "",
        "Working only from that code, tell me:",
        "1. Which files and functions have to change, and why each one.",
        "2. The exact change for each — a diff or the replacement code.",
        "3. Anything in the story that the code contradicts, or that is too "
        "ambiguous to implement without a decision from me.",
        "4. The tests to add or update, and what they should assert.",
        "",
        "Do not invent files, classes, methods or APIs that are not in the "
        "attached code. If the fix needs something that is not there, say so "
        "explicitly rather than assuming it exists.",
    ]
    return "\n".join(L)


def to_clipboard(text):
    try:
        p = subprocess.run(["pbcopy"], input=text, text=True, timeout=30)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        warn(f"pbcopy failed ({e})")
        return False


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--story")
    ap.add_argument("--path", default=".")
    ap.add_argument("--out")
    ap.add_argument("--config", default=str(SCRIPT_DIR / "pod-audit.toml"))
    ap.add_argument("--max-file-kb", type=int, default=200)
    ap.add_argument("--max-total-mb", type=float, default=8.0)
    ap.add_argument("--ext")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--tests", action="store_true")
    ap.add_argument("--no-story", action="store_true")
    ap.add_argument("--no-clipboard", action="store_true")
    ap.add_argument("--stdout", action="store_true")
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

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        die(f"Not a directory: {root}")

    info(f"Scanning {root}...")
    kept, skipped, mode = collect(root, args)
    if not kept:
        die("No source files found. Check --path, or widen --ext.")

    budget = int(args.max_total_mb * 1024 * 1024)
    chunks, packed, dropped = build_pack(root, kept, budget)

    manifest = "\n".join(f"  {rel}  ({lines} lines)" for rel, lines, _s in packed)
    header = (
        f"{'#' * 78}\n"
        f"# SOURCE PACK — {root.name}\n"
        f"# Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} "
        f"by copilot-pack.py\n"
        f"# {len(packed)} files, {sum(l for _r, l, _s in packed):,} lines. "
        f"First-party source only.\n"
        f"# File list built from: {mode}\n"
        f"{'#' * 78}\n\n"
        f"CONTENTS\n{manifest}\n\n"
    )
    body = header + "".join(chunks)

    if args.stdout:
        sys.stdout.write(body)
        out_name = "(stdout)"
    else:
        out = Path(args.out).expanduser() if args.out else Path.cwd() / f"{root.name}-code.txt"
        try:
            out.write_text(body)
        except OSError as e:
            die(f"Cannot write {out}: {e}")
        out_name = out.name

    story = None
    if args.story and not args.no_story:
        if pa is None:
            warn("pod-audit.py unavailable — skipping the Rally lookup")
        else:
            try:
                cfg = pa.load_config(args.config)
                info(f"Fetching {args.story} from Rally...")
                story = fetch_story(cfg, args.story)
                if story is None:
                    warn(f"No Rally story or defect found with id {args.story}")
            except SystemExit:
                warn("No Rally API key (set [rally].api_key or RALLY_API_KEY) — "
                     "packing the code without the story")
            except Exception as e:                    # noqa: BLE001
                warn(f"Rally lookup failed: {e}")

    prompt = build_prompt(story, out_name, packed, dropped, root)
    copied = False
    if not args.no_clipboard:
        copied = to_clipboard(prompt)

    if args.stdout:
        return
    total_kb = len(body) / 1024
    print()
    print("=" * 62)
    print(" SOURCE PACK READY")
    print("=" * 62)
    print(f" Project:   {root}")
    print(f" Files:     {len(packed)} packed, {sum(l for _r, l, _s in packed):,} lines "
          f"({total_kb / 1024:.1f} MB)")
    print(f" Source:    {mode}")
    excl = ", ".join(f"{v} {k}" for k, v in skipped.items() if v)
    if excl:
        print(f" Excluded:  {excl}")
    if dropped:
        print(f" Over budget: {len(dropped)} file(s) left out "
              f"(raise --max-total-mb to include them)")
    print(f" Written:   {out}")
    if story:
        print(f" Story:     {story['sid']} — {story['name']}")
    print("=" * 62)
    if copied:
        print(" Prompt is on the clipboard. In Copilot: attach the file above,")
        print(" then paste with Cmd-V.")
    else:
        print(" Prompt (not copied):\n")
        print(prompt)


if __name__ == "__main__":
    main()
