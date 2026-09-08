#!/usr/bin/env python3
#
# sqlite-schemas.py
#
# Walk a directory tree, find every SQLite database under it, and write all
# of their schemas into a single text file in the directory you ran from.
#
# Databases are found by content, not by name: every regular file is sniffed
# for the 16-byte "SQLite format 3" header, so .db, .sqlite, .sqlite3, .s3db,
# extensionless files and oddly-named ones are all caught, and a .db file
# that is really a Berkeley DB or a LevelDB is not. Write-ahead-log sidecars
# (-wal, -shm, -journal) are skipped: they belong to a database that is
# already in the list.
#
# Nothing is ever opened for writing. Each database is opened read-only, and
# if that fails (a database in WAL mode with an unrecovered log cannot be
# opened read-only) it falls back to an immutable read, then to schema-
# reading a temporary copy.
#
# For each database the report gives the file's own header facts (page size,
# encoding, user_version, journal mode) and then, per table: columns with
# type/nullability/default/primary key, foreign keys shown on the column
# they leave from, indexes, triggers, and the original CREATE statement.
# Views, standalone triggers and virtual tables get their DDL too.
#
# Usage:
#   sqlite-schemas.py                       # walk the current directory
#   sqlite-schemas.py ~/Projects            # walk somewhere else
#   sqlite-schemas.py .. --counts           # include row counts
#   sqlite-schemas.py --path .. --ddl-only  # just the CREATE statements
#
# Options:
#   --path <dir>        Directory to spider (default: current directory).
#                       May also be given as a bare first argument.
#   --out <file>        Output file (default: sqlite-schemas.txt in the
#                       directory you ran from)
#   --counts            Add a row count per table. Off by default: it is a
#                       full scan of every table in every database found.
#   --ddl-only          Emit only the CREATE statements, no column tables
#   --internal          Include SQLite's own sqlite_* tables
#   --max-depth <n>     Stop descending after n levels below --path
#   --exclude <glob>    Skip paths matching this glob (repeatable)
#   --all-dirs          Descend into node_modules, .git, venvs and friends,
#                       which are skipped by default
#   --follow-symlinks   Follow symlinked directories (off: symlink loops)
#   --max-copy-mb <n>   Largest database to copy when it can only be read
#                       from a copy (default 512)
#   --stdout            Write the report to stdout instead of a file

import argparse
import datetime as dt
import fnmatch
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

MAGIC = b"SQLite format 3\x00"

# The smallest legal SQLite database is one 512-byte page.
MIN_DB_BYTES = 512

# Sidecars of a database that is already being reported on.
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# Directories that are somebody else's business. Hidden directories in
# general are *not* skipped: plenty of real databases live in ~/.config,
# ~/.local and the like.
DENY_DIRS = {
    "node_modules", "bower_components", "jspm_packages", "vendor",
    ".git", ".svn", ".hg",
    ".venv", "venv", "virtualenv", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".gradle", ".m2", ".nuget",
    "target", "build", "dist", "out", "obj",
    ".next", ".nuxt", ".svelte-kit", ".angular", ".parcel-cache", ".turbo",
    "site-packages", "dist-packages", "Pods", "DerivedData",
}

RULE = "=" * 78
THIN = "-" * 78


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"==> {msg}")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# --------------------------------------------------------------------------
# Finding the databases
# --------------------------------------------------------------------------

def looks_like_sqlite(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == MAGIC
    except OSError:
        return False


def find_databases(root, args):
    """Walk the tree and return the databases found, deduplicated by inode so
    a hard link or a symlinked file is not reported twice."""
    found, seen = [], set()
    stats = {"files": 0, "pruned": 0, "excluded": 0, "sidecars": 0,
             "unreadable": 0, "duplicates": 0}
    root_depth = len(root.parts)

    def on_error(e):
        stats["unreadable"] += 1
        warn(f"cannot read directory: {e}")

    for dirpath, dirnames, filenames in os.walk(
            root, onerror=on_error, followlinks=args.follow_symlinks):
        here = Path(dirpath)
        depth = len(here.parts) - root_depth

        if args.max_depth is not None and depth >= args.max_depth:
            stats["pruned"] += len(dirnames)
            dirnames[:] = []
        elif not args.all_dirs:
            keep = [d for d in dirnames if d not in DENY_DIRS]
            stats["pruned"] += len(dirnames) - len(keep)
            dirnames[:] = keep
        dirnames.sort()

        for name in sorted(filenames):
            p = here / name
            if name.endswith(SIDECAR_SUFFIXES):
                stats["sidecars"] += 1
                continue
            if args.exclude and any(fnmatch.fnmatch(str(p), g) or
                                    fnmatch.fnmatch(name, g)
                                    for g in args.exclude):
                stats["excluded"] += 1
                continue
            try:
                st = p.stat()          # stat, not lstat: follow file symlinks
            except OSError:
                stats["unreadable"] += 1
                continue
            if not os.path.isfile(p) or st.st_size < MIN_DB_BYTES:
                continue
            stats["files"] += 1
            if not looks_like_sqlite(p):
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                stats["duplicates"] += 1
                continue
            seen.add(key)
            found.append((p, st.st_size))

    found.sort(key=lambda f: str(f[0]).lower())
    return found, stats


# --------------------------------------------------------------------------
# Opening, without ever writing
# --------------------------------------------------------------------------

def open_readonly(path, args):
    """Open a database for reading. Returns (connection, note, tempdir).

    A database left in WAL mode with a live -wal file cannot be opened
    read-only, because recovering the log needs a write. So: plain read-only
    first, then an immutable read (which ignores the log, so the schema may
    be one checkpoint stale), then a copy of the file and its sidecars into a
    temporary directory, which always works but costs the disk space."""
    def try_uri(uri):
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con

    errors = []
    for query, note in (("mode=ro", ""),
                        ("mode=ro&immutable=1",
                         "opened immutable — any un-checkpointed WAL was ignored")):
        try:
            return try_uri(f"{path.as_uri()}?{query}"), note, None
        except sqlite3.Error as e:
            errors.append(str(e))

    # A file that is corrupt or encrypted will not read any better from a
    # copy, so only pay for the copy when the failure looks like a lock.
    if all("not a database" in e or "encrypted" in e for e in errors):
        raise sqlite3.Error(errors[0])

    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > args.max_copy_mb:
        raise sqlite3.Error(f"{errors[0]} (too big to copy: {size_mb:.0f} MB "
                            f"> --max-copy-mb {args.max_copy_mb})")

    tmp = Path(tempfile.mkdtemp(prefix="sqlite-schemas-"))
    try:
        shutil.copy2(path, tmp / path.name)
        for suffix in SIDECAR_SUFFIXES:
            side = path.with_name(path.name + suffix)
            if side.is_file():
                shutil.copy2(side, tmp / side.name)
        con = try_uri(f"{(tmp / path.name).as_uri()}?mode=ro")
        return con, "read from a temporary copy (the original is locked)", tmp
    except (OSError, sqlite3.Error) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise sqlite3.Error(f"{errors[0]}; copy also failed: {e}")


# --------------------------------------------------------------------------
# Reading a schema
# --------------------------------------------------------------------------

def pragma(con, name, arg=None):
    try:
        sql = f"PRAGMA {name}" + (f"({arg})" if arg is not None else "")
        return con.execute(sql).fetchall()
    except sqlite3.Error:
        return []


def scalar(con, name):
    rows = pragma(con, name)
    return rows[0][0] if rows and rows[0] else None


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def indent(text, pad="    "):
    return "\n".join(pad + line for line in text.strip().splitlines())


def column_line(col, fks):
    """col is a PRAGMA table_info row: cid, name, type, notnull, default, pk."""
    _cid, name, ctype, notnull, default, pk = col[:6]
    bits = []
    if pk:
        bits.append("PK" if pk == 1 else f"PK{pk}")
    if notnull:
        bits.append("NOT NULL")
    if default is not None:
        bits.append(f"DEFAULT {default}")
    for fk in fks:
        # id, seq, table, from, to, on_update, on_delete, match
        if fk[3] == name:
            ref = f"-> {fk[2]}({fk[4] or 'rowid'})"
            if fk[6] and fk[6] != "NO ACTION":
                ref += f" ON DELETE {fk[6]}"
            if fk[5] and fk[5] != "NO ACTION":
                ref += f" ON UPDATE {fk[5]}"
            bits.append(ref)
    return f"    {name:<26} {(ctype or '—'):<14} {', '.join(bits)}".rstrip()


def index_lines(con, table):
    out = []
    for seq, name, unique, origin, partial in (
            r[:5] for r in pragma(con, "index_list", quoted(table))):
        cols = [r[2] for r in pragma(con, "index_info", quoted(name))]
        tags = []
        if unique:
            tags.append("UNIQUE")
        if partial:
            tags.append("PARTIAL")
        if origin != "c":
            # 'pk' and 'u' indexes are created by the table's own constraints.
            tags.append("implicit")
        label = " ".join(tags)
        cols = ", ".join(c if c is not None else "<expr>" for c in cols)
        out.append(f"    {name:<26} {label + ' ' if label else ''}({cols})")
    return out


def row_count(con, table):
    try:
        return con.execute(f"SELECT count(*) FROM {quoted(table)}").fetchone()[0]
    except sqlite3.Error:
        return None


def describe(path, rel, size, args):
    """Return the report section for one database, as a list of lines."""
    L = [RULE, f"DATABASE: {rel}", f"FILE:     {path}"]
    try:
        con, note, tmp = open_readonly(path, args)
    except sqlite3.Error as e:
        L += [f"SIZE:     {human(size)}",
              RULE, "",
              f"    !! could not be read: {e}", ""]
        return L, None

    try:
        objects = con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name IS NOT NULL ORDER BY type, name").fetchall()
        if not args.internal:
            objects = [o for o in objects if not o[1].startswith("sqlite_")]

        tables = [o for o in objects if o[0] == "table"]
        views = [o for o in objects if o[0] == "view"]
        indexes = [o for o in objects if o[0] == "index"]
        triggers = [o for o in objects if o[0] == "trigger"]

        L.append(f"SIZE:     {human(size)}    "
                 f"tables: {len(tables)}  views: {len(views)}  "
                 f"indexes: {len(indexes)}  triggers: {len(triggers)}")
        L.append(f"HEADER:   page_size {scalar(con, 'page_size')}  "
                 f"encoding {scalar(con, 'encoding')}  "
                 f"user_version {scalar(con, 'user_version')}  "
                 f"application_id {scalar(con, 'application_id')}  "
                 f"journal {scalar(con, 'journal_mode')}")
        if note:
            L.append(f"NOTE:     {note}")
        L.append(RULE)
        L.append("")

        if not objects:
            L += ["    (no schema — the database is empty)", ""]

        for _type, name, _tbl, sql in sorted(tables, key=lambda o: o[1].lower()):
            header = f"TABLE  {name}"
            if args.counts:
                n = row_count(con, name)
                header += f"    ({n:,} rows)" if n is not None else "    (rows: ?)"
            L += [THIN, header, THIN]

            if not args.ddl_only:
                cols = pragma(con, "table_info", quoted(name))
                fks = pragma(con, "foreign_key_list", quoted(name))
                if cols:
                    L.append("  Columns:")
                    L += [column_line(c, fks) for c in cols]
                else:
                    L.append("  Columns: (none reported — virtual table?)")
                idx = index_lines(con, name)
                if idx:
                    L.append("  Indexes:")
                    L += idx
                mine = [t for t in triggers if t[2] == name]
                if mine:
                    L.append("  Triggers:")
                    L += [f"    {t[1]}" for t in mine]

            if sql:
                L += ["  DDL:", indent(sql), ""]
            else:
                L.append("")

        # Triggers are named under their table above; their bodies live here.
        for kind, group in (("VIEW", views), ("TRIGGER", triggers)):
            for _type, name, tbl, sql in sorted(group, key=lambda o: o[1].lower()):
                on = f"  (on {tbl})" if tbl and tbl != name else ""
                L += [THIN, f"{kind}  {name}{on}", THIN]
                L += ["  DDL:", indent(sql), ""] if sql else [""]

        # Indexes that CREATE INDEX made are shown per table above; anything
        # left without a table of its own would be a SQLite oddity, so only
        # report the count.
        counts = {"tables": len(tables), "views": len(views),
                  "indexes": len(indexes), "triggers": len(triggers)}
        return L, counts
    finally:
        con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("positional", nargs="?")
    ap.add_argument("--path")
    ap.add_argument("--out")
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--ddl-only", action="store_true")
    ap.add_argument("--internal", action="store_true")
    ap.add_argument("--max-depth", type=int)
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--all-dirs", action="store_true")
    ap.add_argument("--follow-symlinks", action="store_true")
    ap.add_argument("--max-copy-mb", type=float, default=512.0)
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

    if args.path and args.positional and args.path != args.positional:
        die("Give the directory once — either as an argument or as --path")
    root = Path(args.path or args.positional or ".").expanduser().resolve()
    if not root.is_dir():
        die(f"Not a directory: {root}")

    info(f"Spidering {root}...")
    found, stats = find_databases(root, args)
    if not found:
        die(f"No SQLite databases found under {root}. "
            f"Sniffed {stats['files']} file(s) — try --all-dirs, or a deeper "
            f"--path.")
    info(f"Found {len(found)} database(s); reading schemas...")

    body = [
        "#" * 78,
        "# SQLITE SCHEMAS",
        f"# Root: {root}",
        f"# Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} "
        f"by sqlite-schemas.py",
        f"# {len(found)} database(s)",
        "#" * 78,
        "",
        "CONTENTS",
    ]
    for p, size in found:
        body.append(f"  {p.relative_to(root) if root in p.parents else p}"
                    f"  ({human(size)})")
    body.append("")

    totals = {"tables": 0, "views": 0, "indexes": 0, "triggers": 0}
    failed = []
    for p, size in found:
        rel = p.relative_to(root) if root in p.parents else p
        info(f"  {rel}")
        lines, counts = describe(p, rel, size, args)
        body += lines
        if counts is None:
            failed.append(rel)
        else:
            for k in totals:
                totals[k] += counts[k]

    text = "\n".join(body) + "\n"

    if args.stdout:
        sys.stdout.write(text)
        return

    out = Path(args.out).expanduser() if args.out else Path.cwd() / "sqlite-schemas.txt"
    try:
        out.write_text(text)
    except OSError as e:
        die(f"Cannot write {out}: {e}")

    print()
    print("=" * 62)
    print(" SQLITE SCHEMAS WRITTEN")
    print("=" * 62)
    print(f" Root:      {root}")
    print(f" Databases: {len(found)} found, {len(found) - len(failed)} read")
    print(f" Schema:    {totals['tables']} tables, {totals['views']} views, "
          f"{totals['indexes']} indexes, {totals['triggers']} triggers")
    skipped = ", ".join(f"{v} {k}" for k, v in stats.items()
                        if v and k != "files")
    print(f" Sniffed:   {stats['files']} file(s)")
    if skipped:
        print(f" Skipped:   {skipped}")
    if failed:
        print(f" Unreadable: {', '.join(str(f) for f in failed)}")
    print(f" Written:   {out}  ({human(len(text))})")
    print("=" * 62)


if __name__ == "__main__":
    main()
