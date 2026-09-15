#!/usr/bin/env python3
#
# code-symbols.py
#
# Symbol-level access to a codebase, the way a language server sees it:
# ask for one class's outline, one method's body, or every reference to a
# name, instead of reading whole files.
#
# This is the retrieval half of what an LSP-backed assistant does. It exists
# so the Copilot bridge (`!sym`, `!body`, `!refs`) can put precisely the
# right 20 lines in the chat box instead of a 28 KB file.
#
# Parsing is tree-sitter when it is installed, which gives real parse trees
# and so real signatures, and identifiers that are not comments or strings:
#
#     pip3 install --user tree-sitter tree-sitter-language-pack
#
# Without it the script still works, using a brace-and-indent scanner that
# gets outlines right and references approximately. It says which it used.
#
# Symbols are found by walking the parse tree for nodes that declare a name
# — every grammar spells them <something>_declaration, _definition, _item or
# _specifier with a "name" field — so no per-language query files, and a
# language nobody thought about still mostly works.
#
# Usage:
#   code-symbols.py outline IssueService          # fields + every signature
#   code-symbols.py outline src/main/java/Foo.java
#   code-symbols.py body IssueService.transitionStatus
#   code-symbols.py refs transitionStatus         # call sites, file:line
#   code-symbols.py find issue                    # fuzzy symbol search
#   code-symbols.py index --rebuild               # refresh the cache
#
# Options:
#   --root <dir>     Project root (default: cwd, or its git top level)
#   --max <n>        Cap the results (default 60)
#   --context <n>    Lines of context around each reference (default 0)
#   --kind <k>       Only symbols of this kind: class, method, function...
#   --json           Machine-readable output, for the bridge
#   --no-cache       Parse everything fresh, ignoring the index
#   --rebuild        (with `index`) throw the cache away first
#
# Set CODE_SYMBOLS_NO_TREESITTER=1 to force the fallback scanner, which is
# how you check that the no-dependency path still works.

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path.home() / ".copilot-cli-bridge" / "symbols"

# Suffix -> tree-sitter grammar name. The pack calls C# "csharp", not
# "c_sharp", and TSX needs its own grammar to parse the angle brackets.
LANGS = {
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".py": "python", ".rb": "ruby", ".go": "go", ".rs": "rust",
    ".cs": "csharp", ".swift": "swift", ".php": "php", ".lua": "lua",
    ".dart": "dart", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".cxx": "cpp", ".hh": "cpp", ".sql": "sql",
    ".sh": "bash", ".bash": "bash",
}

# Node types that introduce a name, across grammars. The suffix test catches
# most of them; these are the ones that do not follow the pattern.
DECL_SUFFIXES = ("_declaration", "_definition", "_item", "_specifier")
EXTRA_DECLS = {
    "function_item", "impl_item", "trait_item", "mod_item",
    "method_definition", "public_field_definition", "field_declaration",
    "property_declaration", "variable_declarator", "type_alias_declaration",
    "create_table_statement", "create_function_statement",
}
# Kinds worth showing in an outline, mapped to something a human reads.
KIND_NAMES = {
    "class": "class", "interface": "interface", "enum": "enum",
    "struct": "struct", "trait": "trait", "impl": "impl", "protocol": "protocol",
    "method": "method", "function": "function", "constructor": "constructor",
    "field": "field", "property": "property", "type": "type", "table": "table",
}
CONTAINER_KINDS = {"class", "interface", "enum", "struct", "trait", "impl",
                   "protocol", "module", "namespace"}
FUNCTION_KINDS = {"method", "function", "constructor", "local_function"}
# Never symbols: an import is not something you look up, and a local is not
# visible outside the body it lives in.
NOISE_KINDS = {"import", "package", "using", "local_variable", "expression",
               "preproc_include", "comment", "export",
               # A parameter is part of the signature above it, not a symbol
               # you would ever look up on its own.
               "parameter", "optional_parameter", "type_parameter",
               "template_parameter", "argument", "attribute", "decorator",
               "label", "case"}


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# Which files are the project's own — reuse copilot-pack.py rather than grow
# a second opinion about what counts as source.
# --------------------------------------------------------------------------

def _load_packer():
    import importlib.util
    path = SCRIPT_DIR / "copilot-pack.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("copilot_pack", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:                                # noqa: BLE001
        warn(f"could not load copilot-pack.py ({e}); using a plain walk")
        return None


cp = _load_packer()
DENY_DIRS = cp.DENY_DIRS if cp else {"node_modules", ".git", "build", "dist",
                                     "target", ".venv", "__pycache__"}


def project_root(start):
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    return Path(start).resolve()


def source_files(root):
    if cp:
        rels = cp.git_files(root)
        if rels is not None:
            out = []
            for rel in rels:
                p = root / rel
                if any(d in Path(rel).parts for d in DENY_DIRS):
                    continue
                if p.suffix.lower() in LANGS and p.is_file():
                    out.append(p)
            return out
    out = []
    for p in root.rglob("*"):
        if p.suffix.lower() not in LANGS or not p.is_file():
            continue
        if any(d in p.relative_to(root).parts for d in DENY_DIRS):
            continue
        out.append(p)
    return out


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_parsers = {}
TS_OK = None


def parser_for(lang):
    """A tree-sitter parser, or None if tree-sitter is not installed."""
    global TS_OK
    if lang in _parsers:
        return _parsers[lang]
    try:
        if os.environ.get("CODE_SYMBOLS_NO_TREESITTER"):
            raise ImportError("disabled by CODE_SYMBOLS_NO_TREESITTER")
        from tree_sitter_language_pack import get_parser
        p = get_parser(lang)
        TS_OK = True
    except Exception:                                     # noqa: BLE001
        if TS_OK is None:
            TS_OK = False
        p = None
    _parsers[lang] = p
    return p


def node_kind(node_type):
    t = node_type.replace("_declaration", "").replace("_definition", "")
    t = t.replace("_specifier", "").replace("_item", "").replace("_statement", "")
    aliases = {"function": "function", "method": "method", "class": "class",
               "interface": "interface", "enum": "enum", "struct": "struct",
               "trait": "trait", "impl": "impl", "protocol": "protocol",
               "constructor": "constructor", "field": "field",
               "public_field": "field", "property": "property",
               "type_alias": "type", "create_table": "table",
               "variable_declarator": "variable", "local_function": "function",
               "module": "module", "namespace": "namespace"}
    return aliases.get(t, t or "symbol")


def name_of(node, src):
    """(name, line) of the declared identifier. The line is the identifier's
    own, not the node's: a Java method annotated on the line above starts
    two lines before its name, and a reference check needs the real one."""
    for field in ("name", "declarator", "pattern"):
        child = node.child_by_field_name(field)
        if child is None:
            continue
        text = src[child.start_byte:child.end_byte].decode("utf8", "replace")
        # A C/C++ declarator is the whole `foo(int a)` — keep the identifier.
        m = re.search(r"[A-Za-z_~][\w]*", text)
        if m:
            offset = text[:m.start()].count("\n")
            return m.group(0), child.start_point[0] + 1 + offset
    return None, 0


def signature_of(node, src):
    """Everything up to the body: `public Issue createIssue(UUID id)`."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", "replace")
    text = re.sub(r"\s*\n\s*", " ", text).strip().rstrip("{").strip()
    return re.sub(r"\s{2,}", " ", text)


def parse_symbols(path, src_bytes):
    """[{name, kind, line, end_line, signature, parent}] for one file."""
    lang = LANGS.get(path.suffix.lower())
    p = parser_for(lang) if lang else None
    if p is None:
        return scan_symbols(path, src_bytes)

    tree = p.parse(src_bytes)
    out = []

    seen_lines = set()

    def walk(node, parent, in_function):
        is_decl = (node.type.endswith(DECL_SUFFIXES) or node.type in EXTRA_DECLS)
        here, body = parent, in_function
        if is_decl:
            kind = node_kind(node.type)
            name, name_line = name_of(node, src_bytes)
            line = node.start_point[0] + 1
            # A field_declaration and the variable_declarator inside it are
            # the same symbol said twice; keep the outer one.
            dup = (line, name) in seen_lines
            skip = (kind in NOISE_KINDS or dup
                    or (in_function and kind not in CONTAINER_KINDS))
            if name and not skip:
                seen_lines.add((line, name))
                out.append({
                    "name": name,
                    "kind": kind,
                    "line": line,
                    "name_line": name_line or line,
                    "end_line": node.end_point[0] + 1,
                    "signature": signature_of(node, src_bytes),
                    "parent": parent,
                })
            if kind in CONTAINER_KINDS:
                here, body = (name or parent), False
            elif kind in FUNCTION_KINDS:
                # Everything below here is implementation detail.
                body = True
        for child in node.children:
            walk(child, here, body)

    walk(tree.root_node, None, False)
    return out


BRACE_DECL = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|internal|static|final|abstract|"
    r"async|export|default|const|func|fn|def|class|interface|struct|enum|"
    r"impl|trait|type|var|let|function|override|suspend|open|data)\s+)+"
    r"[\w<>\[\],.:\s*&]*?([A-Za-z_]\w*)\s*[({:=]")


def scan_symbols(path, src_bytes):
    """Fallback when tree-sitter is missing: indentation and keywords."""
    out = []
    text = src_bytes.decode("utf8", "replace")
    container = None
    for i, line in enumerate(text.splitlines(), 1):
        m = BRACE_DECL.match(line)
        if not m:
            continue
        name = m.group(1)
        kw = re.search(r"\b(class|interface|struct|enum|trait|impl)\b", line)
        kind = kw.group(1) if kw else ("function" if "(" in line else "field")
        out.append({"name": name, "kind": kind, "line": i,
                    "name_line": i, "end_line": i,
                    "signature": line.strip().rstrip("{").strip(),
                    "parent": None if kw else container})
        if kw:
            container = name
    return out


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------

def cache_path(root):
    h = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    return CACHE_DIR / f"{root.name}-{h}.json"


def build_index(root, use_cache=True, rebuild=False):
    """{rel: {mtime, symbols}} for every source file, reparsing only what
    changed since last time."""
    cache = {}
    path = cache_path(root)
    if use_cache and not rebuild and path.is_file():
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            cache = {}

    files = source_files(root)
    fresh, parsed, reused = {}, 0, 0
    for p in files:
        rel = str(p.relative_to(root))
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        prev = cache.get(rel)
        if prev and abs(prev.get("mtime", 0) - mtime) < 0.001:
            fresh[rel] = prev
            reused += 1
            continue
        try:
            src = p.read_bytes()
        except OSError:
            continue
        fresh[rel] = {"mtime": mtime, "symbols": parse_symbols(p, src)}
        parsed += 1

    if use_cache:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fresh))
        except OSError as e:
            warn(f"could not write the symbol cache: {e}")
    return fresh, {"files": len(fresh), "parsed": parsed, "reused": reused}


def all_symbols(index):
    for rel, entry in index.items():
        for s in entry["symbols"]:
            yield rel, s


def qualified(s):
    return f"{s['parent']}.{s['name']}" if s.get("parent") else s["name"]


def resolve(index, target, kind=None):
    """Find symbols matching `Class.method`, `method`, or a file path."""
    target = target.strip()
    want_parent, _, want_name = target.rpartition(".")
    hits = []
    for rel, s in all_symbols(index):
        if kind and s["kind"] != kind:
            continue
        if want_parent:
            if s["name"] == want_name and (s.get("parent") or "") == want_parent:
                hits.append((rel, s, 0))
            elif qualified(s).endswith(target):
                hits.append((rel, s, 1))
        elif s["name"] == target:
            hits.append((rel, s, 0 if s["kind"] in CONTAINER_KINDS else 1))
    hits.sort(key=lambda h: (h[2], h[0]))
    return [(rel, s) for rel, s, _ in hits]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_lines(root, rel):
    try:
        return (root / rel).read_text(encoding="utf8", errors="replace").splitlines()
    except OSError:
        return []


def cmd_outline(root, index, args):
    target = args.target
    rels = []
    if target:
        p = (root / target)
        if p.is_file():
            rels = [str(p.relative_to(root))]
        else:
            hits = resolve(index, target)
            seen = set()
            for rel, s in hits:
                if s["kind"] in CONTAINER_KINDS and rel not in seen:
                    seen.add(rel)
                    rels.append(rel)
            if not rels and hits:
                rels = [hits[0][0]]
    if not rels:
        return f"No file or container symbol matching {target!r}."

    out = []
    for rel in rels[:args.max]:
        syms = index.get(rel, {}).get("symbols", [])
        out.append(f"## {rel}")
        containers = [s for s in syms if s["kind"] in CONTAINER_KINDS]
        loose = [s for s in syms if s["kind"] not in CONTAINER_KINDS
                 and not s.get("parent")]
        for c in containers:
            out.append(f"\n{c['signature']}   [line {c['line']}]")
            members = [s for s in syms if s.get("parent") == c["name"]]
            for m in members:
                mark = "  " if m["kind"] in ("field", "property", "variable") else "  "
                out.append(f"{mark}{m['signature']}   [{m['line']}]")
        if loose:
            out.append("")
            for s in loose:
                out.append(f"{s['signature']}   [{s['line']}]")
    return "\n".join(out)


def cmd_body(root, index, args):
    hits = resolve(index, args.target, args.kind)
    if not hits:
        return f"No symbol named {args.target!r}. Try: code-symbols.py find {args.target}"
    out = []
    for rel, s in hits[:args.max]:
        lines = read_lines(root, rel)
        body = "\n".join(lines[s["line"] - 1:s["end_line"]])
        out.append(f"### {qualified(s)}  —  {rel}:{s['line']}-{s['end_line']}\n"
                   f"```\n{body}\n```")
    if len(hits) > args.max:
        out.append(f"({len(hits) - args.max} more match — narrow with Class.method)")
    return "\n\n".join(out)


def cmd_refs(root, index, args):
    """Every identifier equal to the name, with the symbol it sits inside.

    With tree-sitter this is identifier nodes only, so a name in a comment
    or a string does not count. Without it, it is a word-boundary search."""
    name = args.target
    word = re.compile(rf"\b{re.escape(name)}\b")
    defs = {(rel, s.get("name_line", s["line"]))
            for rel, s in all_symbols(index) if s["name"] == name}
    rows = []
    for rel in sorted(index):
        p = root / rel
        try:
            src = p.read_bytes()
        except OSError:
            continue
        if name.encode() not in src:
            continue
        lines = src.decode("utf8", "replace").splitlines()
        lang = LANGS.get(p.suffix.lower())
        parser = parser_for(lang) if lang else None
        wanted = set()
        if parser is not None:
            tree = parser.parse(src)

            def walk(n):
                if n.child_count == 0 and n.type in (
                        "identifier", "type_identifier", "field_identifier",
                        "property_identifier", "shorthand_property_identifier"):
                    if src[n.start_byte:n.end_byte].decode("utf8", "replace") == name:
                        wanted.add(n.start_point[0] + 1)
                for c in n.children:
                    walk(c)
            walk(tree.root_node)
        else:
            wanted = {i for i, l in enumerate(lines, 1) if word.search(l)}

        syms = sorted(index[rel]["symbols"], key=lambda s: s["line"])
        for ln in sorted(wanted):
            if (rel, ln) in defs:
                continue
            holder = ""
            for s in syms:
                if s["line"] <= ln <= s["end_line"] and s["kind"] not in CONTAINER_KINDS:
                    holder = qualified(s)
            text = lines[ln - 1].strip() if ln <= len(lines) else ""
            rows.append((rel, ln, holder, text))

    if not rows:
        return f"No references to {name!r} (outside its own definition)."
    out = [f"{len(rows)} reference(s) to `{name}`:"]
    by_file = defaultdict(list)
    for rel, ln, holder, text in rows[:args.max]:
        by_file[rel].append((ln, holder, text))
    for rel, items in by_file.items():
        out.append(f"\n{rel}")
        for ln, holder, text in items:
            where = f" in {holder}" if holder else ""
            out.append(f"  {ln}:{where}  {text}")
    if len(rows) > args.max:
        out.append(f"\n({len(rows) - args.max} more — raise --max)")
    return "\n".join(out)


def cmd_find(root, index, args):
    pat = args.target.lower()
    hits = []
    for rel, s in all_symbols(index):
        if args.kind and s["kind"] != args.kind:
            continue
        q = qualified(s)
        if pat in q.lower() or fnmatch.fnmatch(q.lower(), pat):
            hits.append((rel, s))
    if not hits:
        return f"No symbol matching {args.target!r}."
    hits.sort(key=lambda h: (len(h[1]["name"]), h[0]))
    out = [f"{len(hits)} symbol(s) matching {args.target!r}:"]
    for rel, s in hits[:args.max]:
        out.append(f"  {s['kind']:<11} {qualified(s):<45} {rel}:{s['line']}")
    if len(hits) > args.max:
        out.append(f"  ({len(hits) - args.max} more — raise --max)")
    return "\n".join(out)


def cmd_index(root, index, stats, args):
    kinds = defaultdict(int)
    for _rel, s in all_symbols(index):
        kinds[s["kind"]] += 1
    top = sorted(kinds.items(), key=lambda kv: -kv[1])[:12]
    out = [f"Indexed {stats['files']} file(s) under {root}",
           f"  parsed {stats['parsed']}, reused {stats['reused']} from cache",
           f"  parser: {'tree-sitter' if TS_OK else 'fallback scanner'}",
           f"  cache:  {cache_path(root)}",
           "  symbols: " + ", ".join(f"{n} {k}" for k, n in top)]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("command", nargs="?", default="index")
    ap.add_argument("target", nargs="?", default="")
    ap.add_argument("--root")
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--context", type=int, default=0)
    ap.add_argument("--kind")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.help or args.command in ("help", "--help"):
        doc = []
        for line in Path(__file__).read_text().splitlines()[1:]:
            if not line.startswith("#"):
                break
            doc.append(line[2:] if line.startswith("# ") else line[1:])
        print("\n".join(doc))
        return

    root = Path(args.root).expanduser().resolve() if args.root \
        else project_root(Path.cwd())
    if not root.is_dir():
        die(f"Not a directory: {root}")

    t0 = time.time()
    index, stats = build_index(root, use_cache=not args.no_cache,
                               rebuild=args.rebuild)
    took = time.time() - t0

    cmds = {"outline": cmd_outline, "body": cmd_body, "refs": cmd_refs,
            "find": cmd_find}
    if args.command == "index":
        text = cmd_index(root, index, stats, args) + f"\n  took {took:.1f}s"
    elif args.command in cmds:
        if not args.target:
            die(f"usage: code-symbols.py {args.command} <name>")
        text = cmds[args.command](root, index, args)
    else:
        die(f"Unknown command {args.command!r} — "
            f"expected outline, body, refs, find or index")

    if args.json:
        print(json.dumps({"ok": True, "command": args.command,
                          "root": str(root), "text": text}))
    else:
        print(text)


if __name__ == "__main__":
    main()
