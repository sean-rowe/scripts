#!/usr/bin/env node
"use strict";
// Local HTTP bridge for the Copilot CLI Bridge extension.
//
// Binds to 127.0.0.1 only; every request must carry the shared token from
// ../.bridge-token (or $BRIDGE_TOKEN), so no other page can drive your machine.
//
//   GET  /ping                      health + version + cwd
//   POST /cmd   {line, payload?}    run a `!` command  -> {kind, text, files, note}
//   POST /run   {command}           raw shell (legacy #!run path)
//
// Run:  node server/bridge-server.js

const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const C = require("./commands");

const VERSION = "2.0.0";
const PORT = Number(process.env.BRIDGE_PORT || 8765);
const LOG_PATH = path.join(os.homedir(), ".copilot-cli-bridge", "bridge.log");
const MAX_BODY = 16 * 1024 * 1024;

const TOKEN =
  process.env.BRIDGE_TOKEN ||
  (() => {
    for (const p of [
      path.join(__dirname, "..", ".bridge-token"),
      path.join(os.homedir(), ".copilot-cli-bridge", "token")
    ]) {
      try {
        const t = fs.readFileSync(p, "utf8").trim();
        if (t) return t;
      } catch (_) {}
    }
    return "";
  })();

if (!TOKEN) {
  console.error("No token. Create .bridge-token in the repo root or set BRIDGE_TOKEN.");
  process.exit(1);
}

function log(line) {
  const stamped = `[${new Date().toISOString()}] ${line}`;
  console.log(stamped);
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    fs.appendFileSync(LOG_PATH, stamped + "\n");
  } catch (_) {}
}

function json(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, X-Bridge-Token",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS"
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        req.destroy();
        reject(new Error("body too large"));
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (e) {
        reject(new Error("bad json"));
      }
    });
    req.on("error", reject);
  });
}

// Constant-time-ish compare so the token isn't guessable by timing.
function tokenOk(given) {
  const a = String(given || "");
  if (a.length !== TOKEN.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ TOKEN.charCodeAt(i);
  return diff === 0;
}

// Keep the log readable: files are base64 blobs, so summarise them.
function summarize(result) {
  if (!result) return "no result";
  const bits = [result.kind];
  if (result.files) bits.push(result.files.map((f) => `${f.name} ${f.size}B`).join(", "));
  if (result.text) bits.push(result.text.length + " chars");
  if (!result.ok) bits.push("ERROR " + result.error);
  return bits.filter(Boolean).join(" | ");
}

const server = http.createServer(async (req, res) => {
  if (req.method === "OPTIONS") return json(res, 204, {});

  if (!tokenOk(req.headers["x-bridge-token"])) {
    // Don't leak whether the route exists to an unauthenticated caller.
    return json(res, 401, { ok: false, error: "bad token" });
  }

  if (req.method === "GET" && req.url === "/ping") {
    // Logged so you can confirm the content script is alive on a given page.
    log("PING from " + (req.headers.origin || req.headers.referer || "unknown origin"));
    return json(res, 200, {
      ok: true,
      version: VERSION,
      cwd: C.state.cwd,
      platform: process.platform,
      commands: Object.keys(C.commands).length
    });
  }

  let body;
  try {
    body = await readBody(req);
  } catch (e) {
    return json(res, 400, { ok: false, error: e.message });
  }

  if (req.method === "POST" && req.url === "/cmd") {
    const line = String(body.line || "").trim();
    log("CMD: " + line.slice(0, 300));
    try {
      const result = await C.dispatch(line, { payload: body.payload });
      log("  -> " + summarize(result));
      return json(res, 200, result);
    } catch (e) {
      log("  -> THREW " + e.message);
      return json(res, 200, { ok: false, kind: "none", error: e.message, cwd: C.state.cwd });
    }
  }

  // Legacy: the #!run code-block path.
  if (req.method === "POST" && req.url === "/run") {
    const command = String(body.command || "").trim();
    if (!command) return json(res, 400, { ok: false, error: "empty command" });
    log("RUN: " + command.slice(0, 300));
    const r = await C.runShell(command);
    return json(res, 200, { ok: true, ...r });
  }

  return json(res, 404, { ok: false, error: "not found" });
});

server.on("clientError", (err, socket) => socket.destroy());

server.listen(PORT, "127.0.0.1", () => {
  log(`bridge server v${VERSION} on http://127.0.0.1:${PORT}`);
  log(`cwd: ${C.state.cwd}`);
  log(`token loaded (${TOKEN.slice(0, 6)}…), ${Object.keys(C.commands).length} command names registered`);
});
