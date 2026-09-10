"use strict";
// The command set behind `!` in the Copilot chat box.
//
// Every command returns the same shape so the browser side stays dumb:
//
//   { ok, kind: "text"|"files"|"none", text?, files?, note?, cwd? }
//
//   text  -> inserted into the Copilot composer (you press Enter)
//   files -> attached to the composer as real uploads
//   note  -> status line shown only in the local panel
//
// Because all the work happens here, every command is testable with curl.

const fs = require("fs");
const path = require("path");
const os = require("os");
const { exec, execFile } = require("child_process");
const U = require("./fsutil");

const STATE_DIR = path.join(os.homedir(), ".copilot-cli-bridge");
const STATE_PATH = path.join(STATE_DIR, "state.json");

const LIMITS = {
  inlineChars: 12000,      // beyond this, text is sent as an attachment instead
  maxAttachFiles: 10,      // Copilot's practical per-message attachment count
  maxFileBytes: 8 * 1024 * 1024,
  maxTotalBytes: 24 * 1024 * 1024,
  maxListFiles: 500,
  grepMatches: 200,
  shellTimeoutMs: 120000,
  claudeTimeoutMs: 600000
};

// ---- session state -------------------------------------------------------

const state = { cwd: os.homedir() };

function loadState() {
  try {
    const s = JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
    if (s.cwd && fs.existsSync(s.cwd)) state.cwd = s.cwd;
  } catch (_) {}
}

function saveState() {
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    fs.writeFileSync(STATE_PATH, JSON.stringify({ cwd: state.cwd }, null, 2));
  } catch (_) {}
}

// ---- argument parsing ----------------------------------------------------

// Flags that consume the following token as their value.
const VALUE_FLAGS = new Set([
  "ext", "max", "depth", "lines", "name", "context", "block", "bytes", "days",
  "out", "cwd", "C", "n", "L"
]);

function tokenize(line) {
  const out = [];
  let cur = "";
  let quote = null;
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quote) {
      if (c === quote) quote = null;
      else if (c === "\\" && quote === '"' && line[i + 1]) cur += line[++i];
      else cur += c;
      continue;
    }
    if (c === '"' || c === "'") {
      quote = c;
      quoted = true;
      continue;
    }
    if (/\s/.test(c)) {
      if (cur || quoted) out.push(cur);
      cur = "";
      quoted = false;
      continue;
    }
    cur += c;
  }
  if (cur || quoted) out.push(cur);
  return out;
}

function parseArgs(tokens) {
  const flags = Object.create(null);
  const positional = [];
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t === "--") {
      positional.push(...tokens.slice(i + 1));
      break;
    }
    let m = /^--([A-Za-z][\w-]*)(?:=([\s\S]*))?$/.exec(t);
    if (m) {
      const name = m[1];
      if (m[2] !== undefined) flags[name] = m[2];
      else if (VALUE_FLAGS.has(name) && tokens[i + 1] !== undefined && !/^--?[A-Za-z]/.test(tokens[i + 1]))
        flags[name] = tokens[++i];
      else flags[name] = true;
      continue;
    }
    m = /^-([A-Za-z])(.*)$/.exec(t);
    if (m) {
      const name = m[1];
      if (VALUE_FLAGS.has(name)) {
        flags[name] = m[2] !== "" ? m[2] : tokens[i + 1] !== undefined ? tokens[++i] : true;
      } else {
        flags[name] = true;
        for (const ch of m[2]) flags[ch] = true;
      }
      continue;
    }
    positional.push(t);
  }
  return { flags, positional };
}

function num(v, dflt) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : dflt;
}

// Pull an extension filter from either --ext or a bare positional like `cs,csproj`.
function extractExts(flags, positional) {
  if (flags.ext) return U.parseExts(flags.ext);
  for (let i = positional.length - 1; i >= 1; i--) {
    if (U.looksLikeExtList(positional[i])) {
      const exts = U.parseExts(positional[i]);
      positional.splice(i, 1);
      return exts;
    }
  }
  return null;
}

// ---- result helpers ------------------------------------------------------

function text(t, note) {
  return { ok: true, kind: "text", text: t, note: note, cwd: state.cwd };
}

function note(n) {
  return { ok: true, kind: "none", note: n, cwd: state.cwd };
}

function fail(msg) {
  return { ok: false, kind: "none", error: msg, cwd: state.cwd };
}

// Pick a fence longer than any backtick run inside the body.
function fence(body, lang) {
  let longest = 0;
  const re = /`+/g;
  let m;
  while ((m = re.exec(body))) longest = Math.max(longest, m[0].length);
  const bar = "`".repeat(Math.max(3, longest + 1));
  return bar + (lang || "") + "\n" + body.replace(/\s+$/, "") + "\n" + bar;
}

function b64File(absPath, displayName, mime) {
  const buf = fs.readFileSync(absPath);
  return {
    name: displayName || path.basename(absPath),
    path: absPath,
    mime: mime || U.mimeFor(absPath),
    size: buf.length,
    b64: buf.toString("base64")
  };
}

function b64Text(str, name, mime) {
  const buf = Buffer.from(str, "utf8");
  return { name, path: null, mime: mime || "text/plain", size: buf.length, b64: buf.toString("base64") };
}

// Long text becomes an attachment rather than a giant paste, unless the caller
// asked to keep it local (-q) or forced inline (--inline).
function textOrFile(body, opts) {
  const o = opts || {};
  const limit = num(o.limit, LIMITS.inlineChars);
  if (o.local) return { ok: true, kind: "none", note: body, cwd: state.cwd };
  if (o.forceFile || (body.length > limit && !o.inline)) {
    const name = o.fileName || "context.md";
    return {
      ok: true,
      kind: "files",
      files: [b64Text(body, name, "text/markdown")],
      text: o.caption || "",
      note: `${U.humanBytes(Buffer.byteLength(body))} — attached as ${name} (too long to paste).`,
      cwd: state.cwd
    };
  }
  return text(body, o.note);
}

// ---- shell ---------------------------------------------------------------

function runShell(command, opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    exec(
      command,
      {
        cwd: fs.existsSync(state.cwd) ? state.cwd : os.homedir(),
        timeout: o.timeoutMs || LIMITS.shellTimeoutMs,
        maxBuffer: 32 * 1024 * 1024,
        shell: process.platform === "win32" ? undefined : "/bin/bash",
        windowsHide: true,
        env: { ...process.env, PATH: enrichedPath() }
      },
      (err, stdout, stderr) => {
        resolve({
          code: err && typeof err.code === "number" ? err.code : err ? 1 : 0,
          stdout: String(stdout || ""),
          stderr: String(stderr || ""),
          killed: !!(err && err.killed)
        });
      }
    );
  });
}

// GUI-launched browsers hand us a minimal PATH; add the usual suspects so
// `claude`, `rg`, `git` and friends resolve.
function enrichedPath() {
  const extra = [
    path.join(os.homedir(), ".local/bin"),
    path.join(os.homedir(), "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin"
  ];
  const cur = (process.env.PATH || "").split(":").filter(Boolean);
  return [...new Set([...cur, ...extra])].join(":");
}

function shellResultToText(command, r) {
  const out = r.stdout.replace(/\s+$/, "");
  const err = r.stderr.replace(/\s+$/, "");
  let body = out;
  if (err) body += (body ? "\n" : "") + "[stderr]\n" + err;
  if (!body) body = "(no output)";
  if (r.killed) body += "\n[timed out]";
  return "`$ " + command + "`  → exit " + r.code + "\n\n" + fence(body);
}

// ---- commands ------------------------------------------------------------

const commands = {};

function define(names, meta, fn) {
  const list = Array.isArray(names) ? names : [names];
  const entry = { names: list, usage: meta.usage, help: meta.help, group: meta.group, fn };
  for (const n of list) commands[n] = entry;
}

// -- help ------------------------------------------------------------------

define(["help", "?", "h"], { group: "meta", usage: "!help [command]", help: "This cheat sheet." }, (ctx) => {
  const target = ctx.positional[0];
  if (target && commands[target]) {
    const c = commands[target];
    return note(`${c.usage}\n\n${c.help}\n\naliases: ${c.names.join(", ")}`);
  }
  const groups = {
    context: "Send files to Copilot",
    read: "Read & search locally",
    back: "Get things back out",
    shell: "Shell & tools",
    meta: "Session"
  };
  const seen = new Set();
  const lines = ["Copilot CLI Bridge — type these in the chat box, starting with !", ""];
  for (const [g, label] of Object.entries(groups)) {
    const rows = [];
    for (const key of Object.keys(commands)) {
      const c = commands[key];
      if (c.group !== g || seen.has(c)) continue;
      seen.add(c);
      rows.push([c.usage, c.help]);
    }
    if (!rows.length) continue;
    lines.push(`── ${label} ${"─".repeat(Math.max(0, 46 - label.length))}`);
    const w = Math.max(...rows.map((r) => r[0].length));
    for (const [u, h] of rows) lines.push("  " + U.pad(u, w + 2) + h);
    lines.push("");
  }
  lines.push("── Modifiers (any command) ────────────────────");
  lines.push("  -q            show here only; don't touch the chat box");
  lines.push("  --send        insert AND submit to Copilot immediately");
  lines.push("  --file        force the result to be an attachment");
  lines.push("  --inline      force inline text even if it's long");
  lines.push("  --ext ts,tsx  extension filter (or just: !ls ~/proj ts,tsx)");
  lines.push("  --max N       cap how many files are touched");
  lines.push("  --all         include node_modules/.git/build dirs");
  lines.push("");
  lines.push("Anything not listed runs as a shell command: !git status");
  return note(lines.join("\n"));
});

// -- session ---------------------------------------------------------------

define(["cd"], { group: "meta", usage: "!cd <dir>", help: "Set the working directory for every command." }, (ctx) => {
  const target = ctx.positional[0];
  if (!target) return note("cwd: " + U.tildify(state.cwd));
  const abs = U.resolvePath(state.cwd, target);
  const st = U.statSafe(abs);
  if (!st || !st.isDirectory()) return fail("not a directory: " + abs);
  state.cwd = abs;
  saveState();
  return note("cwd: " + U.tildify(abs));
});

define(["pwd"], { group: "meta", usage: "!pwd", help: "Show the working directory." }, () =>
  note("cwd: " + U.tildify(state.cwd))
);

// -- listing ---------------------------------------------------------------

define(["ls", "dir"], {
  group: "read",
  usage: "!ls [dir] [ext] [-r]",
  help: "List a directory. -r walks the whole tree."
}, (ctx) => {
  const exts = extractExts(ctx.flags, ctx.positional);
  const dir = U.resolvePath(state.cwd, ctx.positional[0]);
  const st = U.statSafe(dir);
  if (!st) return fail("no such path: " + dir);
  if (st.isFile()) return commands.cat.fn({ ...ctx, positional: [dir] });

  const recursive = !!(ctx.flags.r || ctx.flags.recursive);
  const max = num(ctx.flags.max, LIMITS.maxListFiles);
  const showHidden = !!(ctx.flags.a || ctx.flags.hidden || ctx.flags.all);

  const rows = [];
  let dirCount = 0;
  let fileCount = 0;
  let bytes = 0;

  if (recursive) {
    const res = U.collectFiles(state.cwd, dir, {
      exts, max, recursive: true, includeHidden: showHidden, all: !!ctx.flags.all
    });
    for (const f of res.files) {
      const s = U.statSafe(f);
      fileCount++;
      bytes += s ? s.size : 0;
      rows.push([path.relative(dir, f).split(path.sep).join("/"), s ? U.humanBytes(s.size) : "?", s ? s.mtime : null]);
    }
    if (res.truncated) rows.push(["… more (raise --max)", "", null]);
  } else {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (e) {
      return fail("cannot read " + dir + ": " + e.message);
    }
    entries.sort((a, b) => Number(b.isDirectory()) - Number(a.isDirectory()) || a.name.localeCompare(b.name));
    for (const e of entries) {
      if (!showHidden && e.name.startsWith(".")) continue;
      const full = path.join(dir, e.name);
      const s = U.statSafe(full);
      if (e.isDirectory()) {
        if (exts) continue; // a filter means "show me matching files"
        dirCount++;
        rows.push([e.name + "/", "", s ? s.mtime : null]);
      } else {
        if (!U.matchesExt(full, exts)) continue;
        fileCount++;
        bytes += s ? s.size : 0;
        rows.push([e.name, s ? U.humanBytes(s.size) : "?", s ? s.mtime : null]);
      }
      if (rows.length >= max) {
        rows.push(["… more (raise --max)", "", null]);
        break;
      }
    }
  }

  if (!rows.length) {
    return note(`${U.tildify(dir)} — nothing matched${exts ? " (." + exts.join(", .") + ")" : ""}.`);
  }

  const w = Math.min(70, Math.max(...rows.map((r) => r[0].length)));
  const body = rows
    .map((r) => U.pad(r[0], w + 2) + U.padLeft(r[1], 9) + (r[2] ? "  " + r[2].toISOString().slice(0, 10) : ""))
    .join("\n");

  const head =
    U.tildify(dir) +
    (exts ? "  [." + exts.join(", .") + "]" : "") +
    (recursive ? "  (recursive)" : "") +
    `\n${dirCount} dir${dirCount === 1 ? "" : "s"}, ${fileCount} file${fileCount === 1 ? "" : "s"}` +
    (bytes ? ", " + U.humanBytes(bytes) : "");

  return textOrFile(head + "\n\n" + fence(body), {
    local: ctx.flags.q,
    forceFile: ctx.flags.file,
    inline: ctx.flags.inline,
    fileName: "listing.md"
  });
});

define(["tree"], {
  group: "read",
  usage: "!tree [dir] [--depth 3]",
  help: "Directory structure, depth-limited."
}, (ctx) => {
  const exts = extractExts(ctx.flags, ctx.positional);
  const root = U.resolvePath(state.cwd, ctx.positional[0]);
  const st = U.statSafe(root);
  if (!st || !st.isDirectory()) return fail("not a directory: " + root);
  const maxDepth = num(ctx.flags.depth || ctx.flags.L, 3);
  const showHidden = !!(ctx.flags.a || ctx.flags.hidden);
  const skip = ctx.flags.all ? new Set() : U.DEFAULT_SKIP;
  const cap = num(ctx.flags.max, 600);

  const lines = [U.tildify(root)];
  let count = 0;
  let truncated = false;

  function render(dir, prefix, depth) {
    if (depth > maxDepth || truncated) return;
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    entries = entries.filter((e) => showHidden || !e.name.startsWith("."));
    entries = entries.filter((e) => !(e.isDirectory() && skip.has(e.name)));
    if (exts) {
      entries = entries.filter((e) => e.isDirectory() || U.matchesExt(e.name, exts));
    }
    entries.sort((a, b) => Number(b.isDirectory()) - Number(a.isDirectory()) || a.name.localeCompare(b.name));
    entries.forEach((e, i) => {
      if (truncated) return;
      if (count++ >= cap) {
        lines.push(prefix + "└── … truncated");
        truncated = true;
        return;
      }
      const last = i === entries.length - 1;
      lines.push(prefix + (last ? "└── " : "├── ") + e.name + (e.isDirectory() ? "/" : ""));
      if (e.isDirectory()) render(path.join(dir, e.name), prefix + (last ? "    " : "│   "), depth + 1);
    });
  }
  render(root, "", 1);

  return textOrFile(fence(lines.join("\n")), {
    local: ctx.flags.q,
    forceFile: ctx.flags.file,
    inline: ctx.flags.inline,
    fileName: "tree.md"
  });
});

define(["find", "ff"], {
  group: "read",
  usage: "!find <name-glob> [root]",
  help: "Find files by name, e.g. !find '*.csproj' ~/Projects"
}, (ctx) => {
  const pattern = ctx.positional[0];
  if (!pattern) return fail("usage: !find <name-glob> [root]");
  const root = U.resolvePath(state.cwd, ctx.positional[1]);
  const exts = U.parseExts(ctx.flags.ext);
  const max = num(ctx.flags.max, 200);
  const re = U.globToRegExp(pattern.includes("/") ? pattern : "**/" + pattern);
  const hits = [];
  let truncated = false;
  for (const f of U.walkFiles(root, {
    includeHidden: !!ctx.flags.a,
    skip: ctx.flags.all ? new Set() : undefined
  })) {
    const rel = path.relative(root, f).split(path.sep).join("/");
    if (!re.test(rel) && !re.test(path.basename(f))) continue;
    if (!U.matchesExt(f, exts)) continue;
    if (hits.length >= max) {
      truncated = true;
      break;
    }
    hits.push(rel);
  }
  if (!hits.length) return note(`no match for ${pattern} under ${U.tildify(root)}`);
  const body = hits.join("\n") + (truncated ? "\n… more (raise --max)" : "");
  return textOrFile(
    `${hits.length} match${hits.length === 1 ? "" : "es"} for \`${pattern}\` under ${U.tildify(root)}\n\n` + fence(body),
    { local: ctx.flags.q, forceFile: ctx.flags.file, inline: ctx.flags.inline, fileName: "found.md" }
  );
});

define(["recent"], {
  group: "read",
  usage: "!recent [dir] [--days 7]",
  help: "Files changed recently — good for 'what was I working on'."
}, (ctx) => {
  const exts = extractExts(ctx.flags, ctx.positional);
  const root = U.resolvePath(state.cwd, ctx.positional[0]);
  const days = num(ctx.flags.days, 7);
  const cutoff = Date.now() - days * 86400000;
  const max = num(ctx.flags.max, 100);
  const hits = [];
  for (const f of U.walkFiles(root, { skip: ctx.flags.all ? new Set() : undefined })) {
    if (!U.matchesExt(f, exts)) continue;
    const s = U.statSafe(f);
    if (!s || s.mtime.getTime() < cutoff) continue;
    hits.push([path.relative(root, f).split(path.sep).join("/"), s.mtime, s.size]);
  }
  hits.sort((a, b) => b[1] - a[1]);
  const shown = hits.slice(0, max);
  if (!shown.length) return note(`nothing modified in the last ${days} day(s) under ${U.tildify(root)}`);
  const w = Math.min(70, Math.max(...shown.map((h) => h[0].length)));
  const body = shown
    .map((h) => U.pad(h[0], w + 2) + h[1].toISOString().replace("T", " ").slice(0, 16) + "  " + U.padLeft(U.humanBytes(h[2]), 9))
    .join("\n");
  return textOrFile(
    `${shown.length} of ${hits.length} file(s) changed in the last ${days} day(s) under ${U.tildify(root)}\n\n` + fence(body),
    { local: ctx.flags.q, forceFile: ctx.flags.file, inline: ctx.flags.inline, fileName: "recent.md" }
  );
});

define(["stat", "info"], {
  group: "read",
  usage: "!stat <path>",
  help: "Size, dates, line count, type."
}, (ctx) => {
  const p = U.resolvePath(state.cwd, ctx.positional[0]);
  const s = U.statSafe(p);
  if (!s) return fail("no such path: " + p);
  const lines = [U.tildify(p)];
  lines.push("type    " + (s.isDirectory() ? "directory" : "file"));
  lines.push("size    " + U.humanBytes(s.size));
  lines.push("mtime   " + s.mtime.toISOString());
  if (s.isFile()) {
    const buf = fs.readFileSync(p, { encoding: null });
    const textual = U.looksTextual(p, buf);
    lines.push("kind    " + (textual ? "text" : "binary") + " (" + U.mimeFor(p) + ")");
    if (textual) lines.push("lines   " + buf.toString("utf8").split("\n").length);
  } else {
    let n = 0;
    let bytes = 0;
    for (const f of U.walkFiles(p, { skip: ctx.flags.all ? new Set() : undefined })) {
      n++;
      const fs2 = U.statSafe(f);
      bytes += fs2 ? fs2.size : 0;
      if (n > 20000) break;
    }
    lines.push("files   " + n + " (" + U.humanBytes(bytes) + ", excluding build dirs)");
  }
  return note(lines.join("\n"));
});

// -- reading ---------------------------------------------------------------

function readSlice(abs, flags) {
  const buf = fs.readFileSync(abs);
  if (!U.looksTextual(abs, buf)) return { binary: true, buf };
  let body = buf.toString("utf8");
  let range = null;
  const spec = flags.lines || flags.n;
  if (spec && spec !== true) {
    const m = /^(\d+)?\s*(?:[-:.]{1,2}\s*(\d+)?)?$/.exec(String(spec));
    if (m) {
      const all = body.split("\n");
      const from = Math.max(1, num(m[1], 1));
      const to = m[2] ? num(m[2], all.length) : m[1] && !/[-:.]/.test(String(spec)) ? from : all.length;
      body = all.slice(from - 1, to).join("\n");
      range = from + "-" + Math.min(to, from - 1 + body.split("\n").length);
    }
  }
  return { binary: false, body, range, buf };
}

define(["cat", "read", "show"], {
  group: "read",
  usage: "!cat <file> [--lines 40-90]",
  help: "Paste a file (or a line range) into the chat box."
}, (ctx) => {
  const target = ctx.positional[0];
  if (!target) return fail("usage: !cat <file> [--lines 40-90]");
  const abs = U.resolvePath(state.cwd, target);
  const st = U.statSafe(abs);
  if (!st) return fail("no such file: " + abs);
  if (st.isDirectory()) return fail(abs + " is a directory — try !ls, !tree, or !up");

  const r = readSlice(abs, ctx.flags);
  if (r.binary) {
    return {
      ok: true,
      kind: "files",
      files: [b64File(abs)],
      note: `${path.basename(abs)} is binary — attaching it instead.`,
      cwd: state.cwd
    };
  }
  const header = U.tildify(abs) + (r.range ? `  (lines ${r.range})` : "") + `  ${U.humanBytes(st.size)}`;
  return textOrFile(header + "\n" + fence(r.body, U.fenceLang(abs)), {
    local: ctx.flags.q,
    forceFile: ctx.flags.file,
    inline: ctx.flags.inline,
    fileName: path.basename(abs) + ".md"
  });
});

define(["head"], { group: "read", usage: "!head <file> [n]", help: "First N lines (default 40)." }, (ctx) => {
  const n = num(ctx.positional[1], 40);
  return commands.cat.fn({ ...ctx, positional: [ctx.positional[0]], flags: { ...ctx.flags, lines: "1-" + n } });
});

define(["tail"], { group: "read", usage: "!tail <file> [n]", help: "Last N lines (default 40)." }, (ctx) => {
  const abs = U.resolvePath(state.cwd, ctx.positional[0]);
  const st = U.statSafe(abs);
  if (!st || !st.isFile()) return fail("no such file: " + abs);
  const n = num(ctx.positional[1], 40);
  const all = fs.readFileSync(abs, "utf8").split("\n");
  const from = Math.max(1, all.length - n);
  return commands.cat.fn({ ...ctx, positional: [abs], flags: { ...ctx.flags, lines: from + "-" + all.length } });
});

define(["grep", "search"], {
  group: "read",
  usage: "!grep <pattern> [path] [-i] [-C 2]",
  help: "Search files. Regex unless --fixed. -C adds context lines."
}, (ctx) => {
  const pattern = ctx.positional[0];
  if (!pattern) return fail("usage: !grep <pattern> [path] [--ext ts] [-i] [-C 2]");
  const exts = extractExts(ctx.flags, ctx.positional);
  const target = ctx.positional[1] === undefined ? "." : ctx.positional[1];

  const ignoreCase = !!(ctx.flags.i || ctx.flags.ignorecase);
  const context = num(ctx.flags.C || ctx.flags.context, 0);
  const maxMatches = num(ctx.flags.max, LIMITS.grepMatches);

  let re;
  if (ctx.flags.fixed || ctx.flags.F) {
    re = new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), ignoreCase ? "gi" : "g");
  } else {
    try {
      re = new RegExp(pattern, ignoreCase ? "gi" : "g");
    } catch (_) {
      re = new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), ignoreCase ? "gi" : "g");
    }
  }

  const res = U.collectFiles(state.cwd, target, {
    exts,
    max: num(ctx.flags.maxfiles, 5000),
    recursive: ctx.flags.n ? false : true,
    all: !!ctx.flags.all
  });
  if (res.missing) return fail("no such path: " + res.missing);

  const root = res.root || state.cwd;
  const out = [];
  let matches = 0;
  let filesWith = 0;
  let truncated = false;

  for (const f of res.files) {
    if (truncated) break;
    let buf;
    try {
      buf = fs.readFileSync(f);
    } catch (_) {
      continue;
    }
    if (!U.looksTextual(f, buf)) continue;
    const lines = buf.toString("utf8").split("\n");
    let fileHits = 0;
    for (let i = 0; i < lines.length; i++) {
      re.lastIndex = 0;
      if (!re.test(lines[i])) continue;
      if (matches >= maxMatches) {
        truncated = true;
        break;
      }
      if (fileHits === 0) {
        out.push((out.length ? "\n" : "") + path.relative(root, f).split(path.sep).join("/"));
        filesWith++;
      }
      fileHits++;
      matches++;
      for (let c = Math.max(0, i - context); c < i; c++) out.push(U.padLeft(c + 1, 6) + "- " + lines[c]);
      out.push(U.padLeft(i + 1, 6) + ": " + lines[i]);
      for (let c = i + 1; c <= Math.min(lines.length - 1, i + context); c++)
        out.push(U.padLeft(c + 1, 6) + "- " + lines[c]);
    }
  }

  if (!matches) {
    return note(
      `no match for /${pattern}/ in ${res.files.length} file(s) under ${U.tildify(root)}` +
        (exts ? ` [.${exts.join(", .")}]` : "")
    );
  }
  if (truncated) out.push("\n… stopped at " + maxMatches + " matches (raise --max)");

  const head =
    `${matches} match${matches === 1 ? "" : "es"} for \`${pattern}\` in ${filesWith} file(s) — ` +
    `${U.tildify(root)}${exts ? " [." + exts.join(", .") + "]" : ""}`;

  return textOrFile(head + "\n\n" + fence(out.join("\n")), {
    local: ctx.flags.q,
    forceFile: ctx.flags.file,
    inline: ctx.flags.inline,
    fileName: "matches.md"
  });
});

// -- sending files ---------------------------------------------------------

function buildBundle(files, root, opts) {
  const o = opts || {};
  const parts = [];
  const included = [];
  const skipped = [];
  let bytes = 0;
  const budget = num(o.maxBytes, 4 * 1024 * 1024);

  for (const f of files) {
    const st = U.statSafe(f);
    if (!st) continue;
    let buf;
    try {
      buf = fs.readFileSync(f);
    } catch (e) {
      skipped.push([f, e.code || "unreadable"]);
      continue;
    }
    if (!U.looksTextual(f, buf)) {
      skipped.push([f, "binary"]);
      continue;
    }
    if (bytes + buf.length > budget) {
      skipped.push([f, "over size budget"]);
      continue;
    }
    bytes += buf.length;
    const rel = path.relative(root, f).split(path.sep).join("/") || path.basename(f);
    included.push([rel, st.size]);
    parts.push("\n### " + rel + "\n\n" + fence(buf.toString("utf8"), U.fenceLang(f)));
  }

  const head = [
    "# " + (o.title || U.tildify(root)),
    "",
    `${included.length} file(s), ${U.humanBytes(bytes)} — bundled from ${U.tildify(root)} on ${new Date().toISOString().slice(0, 16).replace("T", " ")}`,
    ""
  ];
  if (included.length) {
    head.push("## Contents", "");
    const w = Math.max(...included.map((i) => i[0].length));
    head.push("```");
    for (const [rel, size] of included) head.push(U.pad(rel, w + 2) + U.padLeft(U.humanBytes(size), 9));
    head.push("```");
  }
  if (skipped.length) {
    head.push("", "## Skipped", "");
    for (const [f, why] of skipped.slice(0, 50))
      head.push("- " + path.relative(root, f).split(path.sep).join("/") + " — " + why);
    if (skipped.length > 50) head.push(`- … ${skipped.length - 50} more`);
  }
  return { text: head.join("\n") + "\n" + parts.join("\n"), included, skipped, bytes };
}

define(["up", "upload", "attach", "add"], {
  group: "context",
  usage: "!up <path|glob> [ext] [-r]",
  help: "Attach real files to the message. A directory attaches what's inside."
}, (ctx) => {
  if (!ctx.positional.length) return fail("usage: !up <file|dir|glob> [--ext ts,tsx] [--max 10]");
  const exts = extractExts(ctx.flags, ctx.positional);
  const maxFiles = num(ctx.flags.max, LIMITS.maxAttachFiles);
  const recursive = ctx.flags.n || ctx.flags["no-recursive"] ? false : true;

  const all = [];
  let root = null;
  for (const target of ctx.positional) {
    const res = U.collectFiles(state.cwd, target, {
      exts,
      max: 2000,
      recursive,
      includeHidden: !!ctx.flags.a,
      all: !!ctx.flags.all
    });
    if (res.missing) return fail("no such path: " + res.missing);
    if (root === null) root = res.root || state.cwd;
    else if (res.root && !res.root.startsWith(root)) root = state.cwd;
    all.push(...res.files);
  }
  const files = [...new Set(all)];
  if (!files.length) {
    return fail("nothing matched" + (exts ? " for ." + exts.join(", .") : "") + " — try -r, or --ext");
  }

  // Too many for one message? Bundle them into a single attachment instead.
  if (files.length > maxFiles && !ctx.flags.raw) {
    const b = buildBundle(files, root, { title: path.basename(root) + " — " + files.length + " files" });
    const name = (path.basename(root) || "bundle").replace(/[^\w.-]+/g, "_") + ".md";
    return {
      ok: true,
      kind: "files",
      files: [b64Text(b.text, name, "text/markdown")],
      note:
        `${files.length} files exceeds the ${maxFiles}-attachment limit, so they were bundled into ${name} ` +
        `(${b.included.length} included, ${U.humanBytes(b.bytes)}${b.skipped.length ? ", " + b.skipped.length + " skipped" : ""}).\n` +
        `Use --max ${files.length} to attach individually, or --raw to force it.`,
      cwd: state.cwd
    };
  }

  const payload = [];
  const skipped = [];
  let total = 0;
  for (const f of files.slice(0, maxFiles)) {
    const st = U.statSafe(f);
    if (!st) continue;
    if (st.size > LIMITS.maxFileBytes) {
      skipped.push(path.basename(f) + " (" + U.humanBytes(st.size) + ", too big)");
      continue;
    }
    if (total + st.size > LIMITS.maxTotalBytes) {
      skipped.push(path.basename(f) + " (over total size budget)");
      continue;
    }
    total += st.size;
    const safe = U.uploadSafeName(f);
    if (safe.renamed) {
      // Copilot rejects unknown extensions; send as .txt with the real path on top.
      const buf = fs.readFileSync(f);
      const body = "// " + path.relative(root, f).split(path.sep).join("/") + "\n\n" + buf.toString("utf8");
      payload.push(b64Text(body, safe.name, "text/plain"));
    } else {
      payload.push(b64File(f, safe.name));
    }
  }

  if (!payload.length) return fail("nothing attachable: " + skipped.join(", "));

  return {
    ok: true,
    kind: "files",
    files: payload,
    note:
      `attaching ${payload.length} file(s), ${U.humanBytes(total)}` +
      (skipped.length ? "\nskipped: " + skipped.join(", ") : ""),
    cwd: state.cwd
  };
});

define(["pack", "bundle"], {
  group: "context",
  usage: "!pack <dir|glob> [ext]",
  help: "Concatenate many files into ONE attachment with a contents list."
}, (ctx) => {
  if (!ctx.positional.length) return fail("usage: !pack <dir|glob> [--ext ts,tsx] [--name foo.md]");
  const exts = extractExts(ctx.flags, ctx.positional);
  const max = num(ctx.flags.max, 400);

  const all = [];
  let root = null;
  for (const target of ctx.positional) {
    const res = U.collectFiles(state.cwd, target, {
      exts, max, recursive: ctx.flags.n ? false : true, includeHidden: !!ctx.flags.a, all: !!ctx.flags.all
    });
    if (res.missing) return fail("no such path: " + res.missing);
    if (root === null) root = res.root || state.cwd;
    all.push(...res.files);
  }
  const files = [...new Set(all)];
  if (!files.length) return fail("nothing matched" + (exts ? " for ." + exts.join(", .") : ""));

  const b = buildBundle(files, root, {
    title: path.basename(root) + (exts ? " (." + exts.join(", .") + ")" : ""),
    maxBytes: num(ctx.flags.bytes, 4 * 1024 * 1024)
  });
  const name = String(ctx.flags.name || (path.basename(root) || "bundle") + ".md").replace(/[^\w.-]+/g, "_");

  if (ctx.flags.q) return note(b.text.slice(0, 4000) + (b.text.length > 4000 ? "\n… (truncated preview)" : ""));
  if (ctx.flags.out) {
    const dest = U.resolvePath(state.cwd, ctx.flags.out);
    fs.writeFileSync(dest, b.text);
    return note(`wrote ${U.humanBytes(b.bytes)} to ${U.tildify(dest)}`);
  }

  return {
    ok: true,
    kind: "files",
    files: [b64Text(b.text, name.endsWith(".md") || name.endsWith(".txt") ? name : name + ".md", "text/markdown")],
    note:
      `packed ${b.included.length} file(s), ${U.humanBytes(b.bytes)} → ${name}` +
      (b.skipped.length ? `\nskipped ${b.skipped.length} (binary or over budget)` : ""),
    cwd: state.cwd
  };
});

// Files that tell you what a project *is*, in priority order.
const MANIFESTS = [
  "package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml",
  "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
  "Makefile", "docker-compose.yml", "Dockerfile", "CLAUDE.md", "AGENTS.md"
];

define(["ctx", "project", "brief"], {
  group: "context",
  usage: "!ctx [dir] [--depth 3]",
  help: "One-shot project briefing: tree + README + manifests + git state."
}, async (ctx) => {
  const root = U.resolvePath(state.cwd, ctx.positional[0]);
  const st = U.statSafe(root);
  if (!st || !st.isDirectory()) return fail("not a directory: " + root);

  const depth = num(ctx.flags.depth, 3);
  const readmeLines = ctx.flags.full ? 100000 : num(ctx.flags.lines, 120);
  const out = [`# ${path.basename(root)}`, "", "`" + U.tildify(root) + "`", ""];

  // Git state, if this is a repo.
  const prev = state.cwd;
  state.cwd = root;
  const isRepo = (await runShell("git rev-parse --is-inside-work-tree 2>/dev/null")).stdout.trim() === "true";
  if (isRepo) {
    const branch = (await runShell("git rev-parse --abbrev-ref HEAD")).stdout.trim();
    const status = (await runShell("git status --short")).stdout.replace(/\s+$/, "");
    const recent = (await runShell("git log --oneline -8")).stdout.replace(/\s+$/, "");
    out.push("## Git", "", "branch: **" + branch + "**", "");
    if (status) out.push("Uncommitted:", "", fence(status), "");
    out.push("Recent commits:", "", fence(recent), "");
  }
  state.cwd = prev;

  // Structure.
  const treeRes = commands.tree.fn({
    ...ctx,
    positional: [root],
    flags: { ...ctx.flags, depth, q: true, file: false, inline: true }
  });
  out.push("## Structure", "", (treeRes.text || treeRes.note || "").trim(), "");

  // Counts by extension — a fast read on what the project is made of.
  const byExt = new Map();
  let fileCount = 0;
  for (const f of U.walkFiles(root, { skip: ctx.flags.all ? new Set() : undefined })) {
    const e = path.extname(f).slice(1).toLowerCase() || "(none)";
    byExt.set(e, (byExt.get(e) || 0) + 1);
    if (++fileCount > 50000) break;
  }
  const top = [...byExt.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
  if (top.length) {
    out.push("## File types", "", fence(top.map(([e, n]) => U.pad("." + e, 14) + n).join("\n")), "");
  }

  // README + manifests.
  const readme = fs
    .readdirSync(root)
    .find((f) => /^readme(\.|$)/i.test(f));
  if (readme) {
    const body = fs.readFileSync(path.join(root, readme), "utf8").split("\n").slice(0, readmeLines).join("\n");
    // Push its headings down two levels so they nest under ours instead of
    // competing with them.
    out.push("## " + readme, "", body.trim().replace(/^(#{1,4})\s/gm, "$1## "), "");
  }
  for (const name of MANIFESTS) {
    const p = path.join(root, name);
    const s = U.statSafe(p);
    if (!s || !s.isFile() || s.size > 200000) continue;
    out.push("## " + name, "", fence(fs.readFileSync(p, "utf8"), U.fenceLang(p)), "");
  }
  // Project files that aren't in MANIFESTS (.csproj, .sln, …).
  for (const f of fs.readdirSync(root)) {
    if (!/\.(csproj|sln|fsproj|vbproj)$/i.test(f)) continue;
    const p = path.join(root, f);
    const s = U.statSafe(p);
    if (!s || s.size > 200000) continue;
    out.push("## " + f, "", fence(fs.readFileSync(p, "utf8"), "xml"), "");
  }

  const body = out.join("\n");
  if (ctx.flags.q) return note(body.slice(0, 6000) + (body.length > 6000 ? "\n… (preview truncated)" : ""));
  return {
    ok: true,
    kind: "files",
    files: [b64Text(body, (path.basename(root) || "project").replace(/[^\w.-]+/g, "_") + "-context.md", "text/markdown")],
    note: `project briefing for ${path.basename(root)} — ${U.humanBytes(Buffer.byteLength(body))}, ${fileCount} files scanned`,
    cwd: state.cwd
  };
});

define(["changed", "diff"], {
  group: "context",
  usage: "!changed [base] [--diff-only]",
  help: "The diff plus every changed file, bundled — 'review my work'."
}, async (ctx) => {
  const base = ctx.positional[0] || (ctx.flags.base === true ? "" : ctx.flags.base) || "";
  const isRepo = (await runShell("git rev-parse --is-inside-work-tree 2>/dev/null")).stdout.trim() === "true";
  if (!isRepo) return fail("not a git repository: " + U.tildify(state.cwd) + " (try !cd first)");

  const range = base ? JSON.stringify(base) + "..." : "";
  const diffCmd = base ? `git diff ${range}` : "git diff HEAD";
  const nameCmd = base ? `git diff --name-only ${range}` : "git diff --name-only HEAD";

  const diff = (await runShell(diffCmd)).stdout;
  let names = (await runShell(nameCmd)).stdout.split("\n").map((s) => s.trim()).filter(Boolean);

  // With no base, untracked files count as "changed" too.
  if (!base) {
    const untracked = (await runShell("git ls-files --others --exclude-standard")).stdout
      .split("\n").map((s) => s.trim()).filter(Boolean);
    names = [...new Set([...names, ...untracked])];
  }

  if (!diff.trim() && !names.length) return note("no changes" + (base ? " vs " + base : " in the working tree"));

  const repoRoot = (await runShell("git rev-parse --show-toplevel")).stdout.trim() || state.cwd;
  const out = [
    "# Changes" + (base ? " vs " + base : " (working tree)"),
    "",
    `${names.length} file(s) in ${U.tildify(repoRoot)}`,
    "",
    "## Diff",
    "",
    fence(diff.replace(/\s+$/, "") || "(no textual diff)", "diff"),
    ""
  ];

  if (!ctx.flags["diff-only"] && !ctx.flags.d) {
    const abs = names.map((n) => path.join(repoRoot, n)).filter((p) => {
      const s = U.statSafe(p);
      return s && s.isFile();
    });
    const b = buildBundle(abs, repoRoot, { title: "Changed files" });
    // buildBundle writes its own header; keep only the per-file sections.
    const idx = b.text.indexOf("\n### ");
    out.push("## Full contents of changed files", "", idx === -1 ? "(none readable)" : b.text.slice(idx + 1));
  }

  const body = out.join("\n");
  if (ctx.flags.q) return note(body.slice(0, 6000) + (body.length > 6000 ? "\n… (preview truncated)" : ""));
  return {
    ok: true,
    kind: "files",
    files: [b64Text(body, "changes.md", "text/markdown")],
    note: `${names.length} changed file(s)${base ? " vs " + base : ""} — ${U.humanBytes(Buffer.byteLength(body))}`,
    cwd: state.cwd
  };
});

define(["url", "fetch", "web"], {
  group: "context",
  usage: "!url <url>",
  help: "Fetch a page (or localhost API) and paste it as text."
}, async (ctx) => {
  let target = ctx.positional[0];
  if (!target) return fail("usage: !url <url>");
  if (!/^https?:\/\//i.test(target)) target = "http://" + target;

  let res;
  try {
    res = await fetch(target, {
      redirect: "follow",
      headers: { "User-Agent": "copilot-cli-bridge/2.0", Accept: "*/*" },
      signal: AbortSignal.timeout(num(ctx.flags.timeout, 30000))
    });
  } catch (e) {
    return fail("fetch failed: " + (e.message || e));
  }

  const type = res.headers.get("content-type") || "";
  const raw = await res.text();
  let body = raw;
  let lang = "";

  if (/json/.test(type)) {
    try {
      body = JSON.stringify(JSON.parse(raw), null, 2);
    } catch (_) {}
    lang = "json";
  } else if (/html/.test(type) && !ctx.flags.raw) {
    // Crude but effective: drop scripts/styles, unwrap tags, collapse blank lines.
    body = raw
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<!--[\s\S]*?-->/g, " ")
      .replace(/<\/(p|div|h[1-6]|li|tr|section|article|br)>/gi, "\n")
      .replace(/<li[^>]*>/gi, "- ")
      .replace(/<[^>]+>/g, "")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/[ \t]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  const head = `${target} → HTTP ${res.status} (${type.split(";")[0] || "unknown"}, ${U.humanBytes(Buffer.byteLength(raw))})`;
  return textOrFile(head + "\n\n" + fence(body, lang), {
    local: ctx.flags.q,
    forceFile: ctx.flags.file,
    inline: ctx.flags.inline,
    fileName: "fetched.md"
  });
});

define(["shot", "screenshot", "snap"], {
  group: "context",
  usage: "!shot [--full]",
  help: "Screenshot (drag to select, or --full) and attach it."
}, (ctx) => {
  if (process.platform !== "darwin") return fail("!shot is macOS-only for now");
  const dest = path.join(os.tmpdir(), "copilot-bridge-" + Date.now() + ".png");
  const args = ctx.flags.full ? ["-x", dest] : ["-i", "-x", dest];
  return new Promise((resolve) => {
    execFile("/usr/sbin/screencapture", args, { timeout: 120000 }, () => {
      if (!fs.existsSync(dest) || fs.statSync(dest).size === 0) {
        return resolve(fail("screenshot cancelled"));
      }
      const f = b64File(dest, "screenshot.png", "image/png");
      try {
        fs.unlinkSync(dest);
      } catch (_) {}
      resolve({
        ok: true,
        kind: "files",
        files: [f],
        note: "screenshot attached (" + U.humanBytes(f.size) + ")",
        cwd: state.cwd
      });
    });
  });
});

define(["clip", "paste"], {
  group: "context",
  usage: "!clip",
  help: "Paste whatever's on your clipboard into the chat box."
}, async (ctx) => {
  const cmd = process.platform === "darwin" ? "pbpaste" : "xclip -selection clipboard -o";
  const r = await runShell(cmd, { timeoutMs: 5000 });
  const body = r.stdout.replace(/\s+$/, "");
  if (!body) return note("clipboard is empty (or not text)");
  return textOrFile(fence(body), {
    local: ctx.flags.q, forceFile: ctx.flags.file, inline: ctx.flags.inline, fileName: "clipboard.md"
  });
});

// -- getting things back out ----------------------------------------------

define(["save", "write"], {
  group: "back",
  usage: "!save [path] [--block 2|--reply]",
  help: "Save Copilot's last code block (or --reply, or --block N) to a file."
}, (ctx) => {
  // The content script hands us { reply, blocks } scraped from the last answer.
  const p = ctx.payload && typeof ctx.payload === "object" ? ctx.payload : {};
  const blocks = Array.isArray(p.blocks) ? p.blocks.filter((b) => b && b.trim()) : [];
  const reply = typeof p.reply === "string" ? p.reply : "";

  let body;
  let source;
  if (ctx.flags.reply) {
    body = reply;
    source = "the full reply";
  } else if (ctx.flags.block !== undefined && ctx.flags.block !== false) {
    if (!blocks.length) return fail("no code block in Copilot's last reply");
    const idx = ctx.flags.block === true ? 1 : num(ctx.flags.block, 1);
    if (idx < 1 || idx > blocks.length) return fail(`--block ${idx} out of range (the reply has ${blocks.length})`);
    body = blocks[idx - 1];
    source = `code block ${idx} of ${blocks.length}`;
  } else if (blocks.length) {
    body = blocks[blocks.length - 1];
    source = blocks.length > 1 ? `the last of ${blocks.length} code blocks (--block N picks another)` : "the code block";
  } else {
    body = reply;
    source = "the full reply (no code block found)";
  }
  if (!body || !body.trim()) return fail("nothing captured from Copilot's last reply — try !probe");

  // No path? Name it from the content and drop it in the working directory.
  const target = ctx.positional[0] || "copilot-" + new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19) + guessExt(body);
  const abs = U.resolvePath(state.cwd, target);
  const st = U.statSafe(abs);
  const dest = st && st.isDirectory() ? path.join(abs, "copilot-" + Date.now() + guessExt(body)) : abs;
  try {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    if (ctx.flags.append || ctx.flags.a) fs.appendFileSync(dest, body.endsWith("\n") ? body : body + "\n");
    else fs.writeFileSync(dest, body);
  } catch (e) {
    return fail("write failed: " + e.message);
  }
  return note(
    `${ctx.flags.append || ctx.flags.a ? "appended" : "wrote"} ${U.humanBytes(Buffer.byteLength(body))} → ` +
      `${U.tildify(dest)}\nsource: ${source}`
  );
});

// Guess an extension from the shape of the code so `!save` with no path still
// produces a sensibly-named file.
function guessExt(body) {
  const s = String(body).slice(0, 4000);
  if (/^\s*[{[]/.test(s) && /["}\]]\s*$/.test(String(body).trim())) return ".json";
  if (/^\s*#!.*\b(bash|sh|zsh)\b/m.test(s) || /^\s*(sudo |brew |npm |git |echo |cd )/m.test(s)) return ".sh";
  if (/\b(interface|type)\s+\w+\s*[={]|:\s*(string|number|boolean)\b/.test(s)) return ".ts";
  if (/^\s*(import|export)\s|=>|\bconst\b|\bfunction\b/m.test(s)) return ".js";
  if (/^\s*(def|class)\s+\w+.*:\s*$/m.test(s) || /^\s*from\s+\w+\s+import\b/m.test(s)) return ".py";
  if (/\b(public|private|namespace)\b.*\b(class|record|interface)\b/.test(s)) return ".cs";
  if (/^\s*(SELECT|INSERT|UPDATE|CREATE TABLE)\b/im.test(s)) return ".sql";
  if (/^\s*</.test(s)) return ".html";
  if (/^#{1,6}\s|\n- |\n\d\. /.test(s)) return ".md";
  return ".txt";
}

define(["open", "reveal"], {
  group: "back",
  usage: "!open <path>",
  help: "Open a file/folder locally (Finder, default app)."
}, async (ctx) => {
  const abs = U.resolvePath(state.cwd, ctx.positional[0] || ".");
  if (!U.statSafe(abs)) return fail("no such path: " + abs);
  const cmd = process.platform === "darwin" ? "open" : process.platform === "win32" ? "start ''" : "xdg-open";
  const reveal = process.platform === "darwin" && ctx.flags.R ? "-R " : "";
  await runShell(`${cmd} ${reveal}${JSON.stringify(abs)}`, { timeoutMs: 10000 });
  return note("opened " + U.tildify(abs));
});

// -- shell & tools ---------------------------------------------------------

define(["sh", "run", "exec"], {
  group: "shell",
  usage: "!sh <command>",
  help: "Run a shell command (also the fallback for anything unknown)."
}, async (ctx) => {
  const command = ctx.rest;
  if (!command) return fail("usage: !sh <command>");
  const r = await runShell(command);
  return textOrFile(shellResultToText(command, r), {
    local: ctx.flags.q, forceFile: ctx.flags.file, inline: ctx.flags.inline, fileName: "output.md"
  });
});

define(["git"], {
  group: "shell",
  usage: "!git <args>",
  help: "Run git in the working directory: !git diff, !git log --oneline -20"
}, async (ctx) => {
  const args = ctx.rest || "status --short --branch";
  const r = await runShell("git " + args);
  const body = (r.stdout + (r.stderr ? "\n" + r.stderr : "")).replace(/\s+$/, "") || "(no output)";
  const lang = /^(diff|show|log -p|format-patch)/.test(args) ? "diff" : "";
  return textOrFile("`$ git " + args + "`\n\n" + fence(body, lang), {
    local: ctx.flags.q,
    forceFile: ctx.flags.file,
    inline: ctx.flags.inline,
    fileName: "git-" + args.split(/\s+/)[0].replace(/\W/g, "") + ".md"
  });
});

define(["claude", "cc"], {
  group: "shell",
  usage: "!claude <prompt>",
  help: "Ask local Claude Code (its MCP servers + skills) and paste the answer."
}, async (ctx) => {
  const prompt = ctx.rest;
  if (!prompt) return fail("usage: !claude <prompt>");
  const r = await runShell(
    "claude -p " + JSON.stringify(prompt) + " --output-format text 2>&1",
    { timeoutMs: LIMITS.claudeTimeoutMs }
  );
  const body = (r.stdout + r.stderr).replace(/\s+$/, "");
  if (!body) return fail("claude produced no output (is the `claude` CLI on PATH?)");
  return textOrFile("Claude Code answered:\n\n" + body, {
    local: ctx.flags.q, forceFile: ctx.flags.file, inline: ctx.flags.inline, fileName: "claude.md"
  });
});

// -- dispatch --------------------------------------------------------------

async function dispatch(line, opts) {
  const o = opts || {};
  const raw = String(line || "").trim();
  if (!raw) return note("type !help for the command list");

  const tokens = tokenize(raw);
  const name = tokens[0];
  const entry = commands[name];

  const restRaw = raw.slice(name.length).trim();
  const parsed = parseArgs(tokens.slice(1));

  const ctx = {
    name,
    raw,
    rest: restRaw,
    tokens: tokens.slice(1),
    positional: parsed.positional,
    flags: parsed.flags,
    payload: o.payload || ""
  };

  let result;
  if (entry) {
    try {
      result = await entry.fn(ctx);
    } catch (e) {
      result = fail((e && e.message) || String(e));
    }
  } else {
    // Unknown command → treat the whole line as a shell command.
    const r = await runShell(raw);
    result = textOrFile(shellResultToText(raw, r), {
      local: ctx.flags.q, forceFile: ctx.flags.file, inline: ctx.flags.inline, fileName: "output.md"
    });
  }

  // Global modifiers.
  if (result && result.ok) {
    if (ctx.flags.send) result.send = true;
    if (ctx.flags.q && result.kind === "text") {
      result.note = result.text;
      result.text = "";
      result.kind = "none";
    }
  }
  if (result && !result.cwd) result.cwd = state.cwd;
  return result;
}

loadState();

module.exports = { dispatch, state, commands, LIMITS, runShell, tokenize, parseArgs };
