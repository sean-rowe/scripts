#!/usr/bin/env node
// Local HTTP bridge server for the Copilot CLI userscript.
//
// The userscript POSTs { command } here; this runs it in the shell and returns
// { code, stdout, stderr }. A shared secret token gates every request so no
// other web page can drive your machine. Binds to 127.0.0.1 only.
//
// Run:  node local-server.js
// Token is read from ../.bridge-token (created by setup) or the BRIDGE_TOKEN env.

const http = require("http");
const { exec } = require("child_process");
const os = require("os");
const fs = require("fs");
const path = require("path");

const PORT = Number(process.env.BRIDGE_PORT || 8765);
const TOKEN =
  process.env.BRIDGE_TOKEN ||
  (() => {
    try {
      return fs.readFileSync(path.join(__dirname, "..", ".bridge-token"), "utf8").trim();
    } catch (_) {
      return "";
    }
  })();

if (!TOKEN) {
  console.error("No token. Create ../.bridge-token or set BRIDGE_TOKEN.");
  process.exit(1);
}

const CONFIG = {
  cwd: process.env.BRIDGE_CWD || os.homedir(),
  timeoutMs: 120000,
  maxOutputBytes: 1000000,
  shell: process.platform === "win32" ? undefined : "/bin/bash"
};

function json(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json",
    // GM_xmlhttpRequest bypasses CORS, but allow it anyway for flexibility.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, X-Bridge-Token",
    "Access-Control-Allow-Methods": "POST, OPTIONS"
  });
  res.end(body);
}

const server = http.createServer((req, res) => {
  if (req.method === "OPTIONS") return json(res, 204, {});
  if (req.method === "GET" && req.url === "/ping") {
    // Health check still requires the token via header.
    if (req.headers["x-bridge-token"] !== TOKEN) return json(res, 401, { error: "bad token" });
    return json(res, 200, { ok: true, version: "1.0.0" });
  }
  if (req.method !== "POST" || req.url !== "/run") return json(res, 404, { error: "not found" });

  let body = "";
  req.on("data", (c) => {
    body += c;
    if (body.length > 100000) req.destroy();
  });
  req.on("end", () => {
    let data;
    try {
      data = JSON.parse(body);
    } catch (_) {
      return json(res, 400, { error: "bad json" });
    }
    if ((req.headers["x-bridge-token"] || data.token) !== TOKEN) {
      return json(res, 401, { error: "bad token" });
    }
    const command = String(data.command || "").trim();
    if (!command) return json(res, 400, { error: "empty command" });

    console.log(`[${new Date().toISOString()}] RUN: ${command}`);
    exec(
      command,
      {
        cwd: fs.existsSync(CONFIG.cwd) ? CONFIG.cwd : os.homedir(),
        timeout: CONFIG.timeoutMs,
        maxBuffer: CONFIG.maxOutputBytes,
        shell: CONFIG.shell,
        windowsHide: true
      },
      (err, stdout, stderr) => {
        const code = err && typeof err.code === "number" ? err.code : err ? 1 : 0;
        json(res, 200, {
          ok: true,
          code,
          stdout: String(stdout || "").slice(0, CONFIG.maxOutputBytes),
          stderr: String(stderr || "").slice(0, CONFIG.maxOutputBytes),
          killed: !!(err && err.killed)
        });
      }
    );
  });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Copilot CLI bridge server on http://127.0.0.1:${PORT}`);
  console.log(`cwd: ${CONFIG.cwd}`);
  console.log(`token loaded (${TOKEN.slice(0, 6)}…). Waiting for the userscript.`);
});
