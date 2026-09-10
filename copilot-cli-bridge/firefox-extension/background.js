// Background page: the only part of the extension allowed to reach the local
// bridge server. Content scripts can't — the Copilot page's CSP blocks
// connections to 127.0.0.1 from the content-script context.
//
// The token and port come from bridge-config.js, which
// launch-firefox-bridge.sh generates each time it starts the server — the port
// is whichever one it actually managed to bind, so a busy port on one machine
// doesn't need anything edited here.
//
// The token is deliberately never hard-coded: this file is committed, and a
// token in it would be a published secret.

const api = typeof browser !== "undefined" ? browser : chrome;
const TOKEN = (typeof BRIDGE_TOKEN !== "undefined" && BRIDGE_TOKEN) || "";
const PORT = (typeof BRIDGE_PORT !== "undefined" && BRIDGE_PORT) || 18765;
const BASE = "http://127.0.0.1:" + PORT;

// !claude can run for minutes; !shot waits on a drag-select. Be generous.
const TIMEOUTS = { ping: 5000, cmd: 630000, run: 130000 };

async function call(pathname, body, timeoutMs) {
  if (!TOKEN) {
    return {
      ok: false,
      kind: "none",
      error: "no bridge token — bridge-config.js wasn't generated. Run ./launch-firefox-bridge.sh"
    };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(BASE + pathname, {
      method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json", "X-Bridge-Token": TOKEN },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal
    });
    if (res.status === 401) {
      return { ok: false, kind: "none", error: "bridge rejected the token — restart via ./launch-firefox-bridge.sh" };
    }
    if (!res.ok) return { ok: false, kind: "none", error: `bridge returned HTTP ${res.status}` };
    return await res.json();
  } catch (e) {
    if (e.name === "AbortError") return { ok: false, kind: "none", error: "bridge timed out" };
    return {
      ok: false,
      kind: "none",
      error:
        "can't reach the bridge server on 127.0.0.1:" + PORT +
        " — start it with ./launch-firefox-bridge.sh"
    };
  } finally {
    clearTimeout(timer);
  }
}

api.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== "object") return Promise.resolve({ ok: false, error: "bad message" });
  switch (msg.type) {
    case "ping":
      return call("/ping", null, TIMEOUTS.ping);
    case "cmd":
      return call("/cmd", { line: msg.line, payload: msg.payload }, TIMEOUTS.cmd);
    case "run":
      return call("/run", { command: msg.command }, TIMEOUTS.run);
    default:
      // Legacy shape: { command } with no type.
      if (typeof msg.command === "string") return call("/run", { command: msg.command }, TIMEOUTS.run);
      return Promise.resolve({ ok: false, error: "unknown message type" });
  }
});
