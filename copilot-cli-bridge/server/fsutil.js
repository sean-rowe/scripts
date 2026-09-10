"use strict";
// Filesystem helpers shared by the bridge commands: path resolution, globbing,
// directory walking, extension filters, binary sniffing, and output formatting.

const fs = require("fs");
const path = require("path");
const os = require("os");

// Directories that are almost never what you want to feed an assistant.
// `--all` on any command turns this off.
const DEFAULT_SKIP = new Set([
  "node_modules", ".git", ".hg", ".svn", "dist", "build", "out", ".next",
  ".nuxt", ".turbo", ".cache", ".parcel-cache", "coverage", "target", "bin",
  "obj", "vendor", "__pycache__", ".venv", "venv", ".tox", ".gradle",
  ".idea", "DerivedData", "Pods", ".terraform", ".pytest_cache", ".mypy_cache"
]);

// ---- paths ---------------------------------------------------------------

function expandHome(p) {
  const s = String(p);
  if (s === "~") return os.homedir();
  if (s.startsWith("~/")) return path.join(os.homedir(), s.slice(2));
  return s;
}

function resolvePath(cwd, p) {
  if (p === undefined || p === null || p === "") return cwd;
  return path.resolve(cwd, expandHome(p));
}

function tildify(p) {
  const home = os.homedir();
  return p === home || p.startsWith(home + path.sep) ? "~" + p.slice(home.length) : p;
}

// ---- globs ---------------------------------------------------------------

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Supports *, **, ?, {a,b}, and [abc]. `**/` crosses directory boundaries and
// also matches zero directories, so `**/*.ts` matches both `a.ts` and `x/a.ts`.
function globToRegExp(glob, opts) {
  const caseSensitive = !!(opts && opts.caseSensitive);
  const g = String(glob);
  let re = "";
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === "*") {
      if (g[i + 1] === "*") {
        if (g[i + 2] === "/") {
          re += "(?:[^/]*\\/)*";
          i += 2;
        } else {
          re += ".*";
          i += 1;
        }
      } else {
        re += "[^/]*";
      }
    } else if (c === "?") {
      re += "[^/]";
    } else if (c === "{") {
      const end = g.indexOf("}", i);
      if (end === -1) {
        re += "\\{";
      } else {
        re += "(?:" + g.slice(i + 1, end).split(",").map(escapeRe).join("|") + ")";
        i = end;
      }
    } else if (c === "[") {
      const end = g.indexOf("]", i);
      if (end === -1) {
        re += "\\[";
      } else {
        re += g.slice(i, end + 1);
        i = end;
      }
    } else {
      re += escapeRe(c);
    }
  }
  return new RegExp("^" + re + "$", caseSensitive ? "" : "i");
}

function hasGlobChars(s) {
  return /[*?[\]{}]/.test(String(s));
}

// ---- extension filters ---------------------------------------------------

// Accepts "ts,tsx", ".ts .tsx", "*.ts", "ts" — anything a human would type.
function parseExts(spec) {
  if (!spec) return null;
  const list = (Array.isArray(spec) ? spec : String(spec).split(/[,\s]+/))
    .map((e) => String(e).trim().toLowerCase())
    .filter(Boolean)
    .map((e) => e.replace(/^\*?\.?/, ""))
    .filter(Boolean);
  return list.length ? list : null;
}

// Does this look like a bare extension list rather than a path? Lets
// `!ls ~/proj cs,csproj` work without a --ext flag.
function looksLikeExtList(s) {
  return /^\*?\.?[A-Za-z0-9]{1,12}(\s*,\s*\*?\.?[A-Za-z0-9]{1,12})*$/.test(String(s).trim()) &&
    !String(s).includes("/");
}

function matchesExt(file, exts) {
  if (!exts) return true;
  const base = path.basename(file).toLowerCase();
  const ext = path.extname(base).replace(/^\./, "");
  // Also match extension-less names people filter on, e.g. `--ext dockerfile`.
  return exts.includes(ext) || exts.includes(base);
}

// ---- walking -------------------------------------------------------------

// Iterative depth-first walk. Yields absolute file paths.
function* walkFiles(root, opts) {
  const o = opts || {};
  const maxDepth = o.maxDepth === undefined ? Infinity : o.maxDepth;
  const includeHidden = !!o.includeHidden;
  const skip = o.skip === null ? new Set() : o.skip || DEFAULT_SKIP;
  const stack = [[root, 0]];
  while (stack.length) {
    const [dir, depth] = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      continue;
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const e of entries) {
      if (!includeHidden && e.name.startsWith(".")) continue;
      const full = path.join(dir, e.name);
      let isDir = e.isDirectory();
      let isFile = e.isFile();
      if (e.isSymbolicLink()) {
        // Follow symlinks for stat only; never descend (avoids cycles).
        try {
          const st = fs.statSync(full);
          isDir = false;
          isFile = st.isFile();
        } catch (_) {
          continue;
        }
      }
      if (isDir) {
        if (skip.has(e.name)) continue;
        if (depth < maxDepth) stack.push([full, depth + 1]);
      } else if (isFile) {
        yield full;
      }
    }
  }
}

function statSafe(p) {
  try {
    return fs.statSync(p);
  } catch (_) {
    return null;
  }
}

// Turn a user-supplied target into a concrete list of files.
//   - a file        -> [that file]
//   - a directory   -> its files (recursive unless recursive:false)
//   - a glob        -> everything under its non-glob prefix that matches
// Always applies the extension filter and the max cap.
function collectFiles(cwd, target, opts) {
  const o = opts || {};
  const exts = o.exts || null;
  const max = o.max === undefined ? 200 : o.max;
  const recursive = o.recursive !== false;
  const includeHidden = !!o.includeHidden;
  const skip = o.all ? null : undefined;

  const raw = expandHome(String(target === undefined ? "." : target));

  if (hasGlobChars(raw)) {
    // Split into a literal root and the glob tail.
    const parts = raw.split("/");
    const litParts = [];
    while (parts.length && !hasGlobChars(parts[0])) litParts.push(parts.shift());
    let root = path.resolve(cwd, litParts.join("/") || ".");
    // A trailing literal that is itself a file means the glob was really a path.
    const st = statSafe(root);
    if (st && st.isFile() && !parts.length) return { files: [root], truncated: false };
    const pattern = parts.join("/") || "*";
    const re = globToRegExp(pattern);
    const out = [];
    let truncated = false;
    for (const f of walkFiles(root, {
      maxDepth: recursive ? Infinity : 0,
      includeHidden: includeHidden || pattern.startsWith("."),
      skip
    })) {
      const rel = path.relative(root, f).split(path.sep).join("/");
      if (!re.test(rel)) continue;
      if (!matchesExt(f, exts)) continue;
      if (out.length >= max) {
        truncated = true;
        break;
      }
      out.push(f);
    }
    return { files: out, truncated, root };
  }

  const abs = path.resolve(cwd, raw);
  const st = statSafe(abs);
  if (!st) return { files: [], truncated: false, missing: abs };
  if (st.isFile()) return { files: [abs], truncated: false, root: path.dirname(abs) };

  const out = [];
  let truncated = false;
  for (const f of walkFiles(abs, {
    maxDepth: recursive ? Infinity : 0,
    includeHidden,
    skip
  })) {
    if (!matchesExt(f, exts)) continue;
    if (out.length >= max) {
      truncated = true;
      break;
    }
    out.push(f);
  }
  return { files: out, truncated, root: abs };
}

// ---- content sniffing ----------------------------------------------------

const TEXT_EXTS = new Set([
  "txt", "md", "markdown", "json", "jsonc", "json5", "yaml", "yml", "toml", "ini",
  "cfg", "conf", "env", "xml", "html", "htm", "css", "scss", "sass", "less",
  "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "vue", "svelte",
  "py", "rb", "go", "rs", "java", "kt", "kts", "swift", "m", "mm", "c", "h",
  "cc", "cpp", "hpp", "cs", "csproj", "sln", "fs", "fsx", "vb", "php", "pl",
  "sh", "bash", "zsh", "fish", "ps1", "psm1", "bat", "cmd", "sql", "graphql",
  "gql", "proto", "tf", "tfvars", "dockerfile", "makefile", "gradle", "properties",
  "lock", "gitignore", "editorconfig", "log", "csv", "tsv", "patch", "diff", "feature"
]);

function looksTextual(file, buf) {
  const ext = path.extname(file).slice(1).toLowerCase();
  const base = path.basename(file).toLowerCase();
  if (TEXT_EXTS.has(ext) || TEXT_EXTS.has(base)) return true;
  if (!buf || !buf.length) return true;
  const sample = buf.slice(0, 8000);
  let suspicious = 0;
  for (let i = 0; i < sample.length; i++) {
    const b = sample[i];
    if (b === 0) return false;
    if (b < 7 || (b > 13 && b < 32)) suspicious++;
  }
  return suspicious / sample.length < 0.1;
}

const MIME = {
  txt: "text/plain", md: "text/markdown", markdown: "text/markdown",
  json: "application/json", csv: "text/csv", tsv: "text/tab-separated-values",
  html: "text/html", htm: "text/html", css: "text/css", xml: "text/xml",
  js: "text/javascript", mjs: "text/javascript", ts: "text/plain",
  tsx: "text/plain", jsx: "text/plain", py: "text/x-python", rb: "text/x-ruby",
  go: "text/x-go", rs: "text/rust", java: "text/x-java", cs: "text/plain",
  sh: "text/x-shellscript", sql: "text/plain", yml: "text/yaml", yaml: "text/yaml",
  pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
  gif: "image/gif", webp: "image/webp", svg: "image/svg+xml", bmp: "image/bmp",
  heic: "image/heic", zip: "application/zip", docx:
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation"
};

function mimeFor(file) {
  const ext = path.extname(file).slice(1).toLowerCase();
  if (MIME[ext]) return MIME[ext];
  return looksTextual(file, null) ? "text/plain" : "application/octet-stream";
}

// Copilot rejects unknown extensions on upload; these are the ones it accepts.
// Source files get renamed to `.txt` with the original name preserved in a
// header so nothing is lost.
const UPLOAD_SAFE_EXTS = new Set([
  "txt", "md", "csv", "json", "pdf", "png", "jpg", "jpeg", "gif", "webp",
  "docx", "xlsx", "pptx", "html", "xml", "log", "yaml", "yml"
]);

function uploadSafeName(file) {
  const base = path.basename(file);
  const ext = path.extname(base).slice(1).toLowerCase();
  if (UPLOAD_SAFE_EXTS.has(ext)) return { name: base, renamed: false };
  return { name: base + ".txt", renamed: true };
}

// ---- language tag for fenced blocks --------------------------------------

const FENCE_LANG = {
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "jsx",
  ts: "typescript", tsx: "tsx", py: "python", rb: "ruby", go: "go", rs: "rust",
  java: "java", kt: "kotlin", swift: "swift", cs: "csharp", c: "c", h: "c",
  cpp: "cpp", hpp: "cpp", cc: "cpp", php: "php", sh: "bash", bash: "bash",
  zsh: "bash", ps1: "powershell", sql: "sql", json: "json", yml: "yaml",
  yaml: "yaml", toml: "toml", xml: "xml", html: "html", css: "css",
  scss: "scss", md: "markdown", diff: "diff", patch: "diff", feature: "gherkin"
};

function fenceLang(file) {
  return FENCE_LANG[path.extname(file).slice(1).toLowerCase()] || "";
}

// ---- formatting ----------------------------------------------------------

function humanBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(1) + " GB";
}

function pad(s, n) {
  s = String(s);
  return s.length >= n ? s : s + " ".repeat(n - s.length);
}

function padLeft(s, n) {
  s = String(s);
  return s.length >= n ? s : " ".repeat(n - s.length) + s;
}

module.exports = {
  DEFAULT_SKIP,
  expandHome,
  resolvePath,
  tildify,
  globToRegExp,
  hasGlobChars,
  parseExts,
  looksLikeExtList,
  matchesExt,
  walkFiles,
  statSafe,
  collectFiles,
  looksTextual,
  mimeFor,
  uploadSafeName,
  UPLOAD_SAFE_EXTS,
  fenceLang,
  humanBytes,
  pad,
  padLeft
};
