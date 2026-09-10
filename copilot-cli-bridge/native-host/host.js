#!/usr/bin/env node
// Native messaging host for Copilot CLI Bridge.
//
// A dumb bridge: reads a command from the browser extension, runs it in the
// shell, returns stdout/stderr/exit code. No allowlist, no filtering — it runs
// whatever it's given. Config only sets the working directory and limits.

const { exec } = require("child_process");
const os = require("os");
const fs = require("fs");
const path = require("path");

const VERSION = "0.2.0";
const CONFIG_DIR = path.join(os.homedir(), ".copilot-cli-bridge");
const CONFIG_PATH = path.join(CONFIG_DIR, "config.json");
const LOG_PATH = path.join(CONFIG_DIR, "bridge.log");

const DEFAULT_CONFIG = {
  cwd: os.homedir(),
  timeoutMs: 120000,
  maxOutputBytes: 1000000,
  shell: process.platform === "win32" ? undefined : "/bin/bash",
  // Optional: substrings that, if present, block a command. Empty = run anything.
  deny: []
};

function ensureConfig() {
  try {
    if (!fs.existsSync(CONFIG_DIR)) fs.mkdirSync(CONFIG_DIR, { recursive: true });
    if (!fs.existsSync(CONFIG_PATH)) {
      fs.writeFileSync(CONFIG_PATH, JSON.stringify(DEFAULT_CONFIG, null, 2));
    }
  } catch (_) {}
}

function loadConfig() {
  try {
    return { ...DEFAULT_CONFIG, ...JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")) };
  } catch (_) {
    return { ...DEFAULT_CONFIG };
  }
}

function log(line) {
  try {
    fs.appendFileSync(LOG_PATH, `[${new Date().toISOString()}] ${line}\n`);
  } catch (_) {}
}

// ---- native messaging framing -------------------------------------------
function send(obj) {
  const buf = Buffer.from(JSON.stringify(obj), "utf8");
  const len = Buffer.alloc(4);
  len.writeUInt32LE(buf.length, 0);
  process.stdout.write(len);
  process.stdout.write(buf);
}

let stdinBuf = Buffer.alloc(0);
process.stdin.on("data", (chunk) => {
  stdinBuf = Buffer.concat([stdinBuf, chunk]);
  while (stdinBuf.length >= 4) {
    const len = stdinBuf.readUInt32LE(0);
    if (stdinBuf.length < 4 + len) break;
    const body = stdinBuf.slice(4, 4 + len);
    stdinBuf = stdinBuf.slice(4 + len);
    try {
      handle(JSON.parse(body.toString("utf8")));
    } catch (_) {}
  }
});
process.stdin.on("end", () => process.exit(0));

// ---- run a command -------------------------------------------------------
function handle(msg) {
  const id = msg && msg.id;
  const command = msg && typeof msg.command === "string" ? msg.command : "";

  if (command === "__ping__") {
    send({ id, ok: true, version: VERSION, config: CONFIG_PATH });
    return;
  }
  if (!command.trim()) {
    send({ id, ok: false, error: "empty command" });
    return;
  }

  const cfg = loadConfig();

  // Optional deny list — empty by default, so nothing is blocked.
  const blocked = (cfg.deny || []).find((p) => p && command.includes(p));
  if (blocked) {
    log(`DENIED (${blocked}): ${command}`);
    send({ id, ok: false, error: `blocked by deny rule: ${blocked}` });
    return;
  }

  log(`RUN: ${command}`);
  exec(
    command,
    {
      cwd: fs.existsSync(cfg.cwd) ? cfg.cwd : os.homedir(),
      timeout: cfg.timeoutMs,
      maxBuffer: cfg.maxOutputBytes,
      shell: cfg.shell,
      windowsHide: true
    },
    (err, stdout, stderr) => {
      const code = err && typeof err.code === "number" ? err.code : err ? 1 : 0;
      send({
        id,
        ok: true,
        code,
        stdout: String(stdout || "").slice(0, cfg.maxOutputBytes),
        stderr: String(stderr || "").slice(0, cfg.maxOutputBytes),
        killed: !!(err && err.killed)
      });
    }
  );
}

ensureConfig();
log(`host started v${VERSION} pid=${process.pid}`);
