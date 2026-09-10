// Background service worker.
//
// Responsibilities:
//   - Own the single connection to the native messaging host.
//   - Relay "exec" requests from content scripts to the host and route the
//     host's reply back to the tab/frame that asked.
//   - Answer "ping-host" health checks from the popup.
//   - Refuse anything but exec/ping from content scripts, so an AI-controlled
//     page cannot reconfigure the bridge. The allow/confirm/deny decision lives
//     in the native host, which reads a local file the page cannot touch.

const HOST_NAME = "com.pinyridgelabs.copilot_clibridge";
const ALL_SITES_ID = "clibridge-all-sites";

// Keep the "run on all sites" dynamic content script in sync with the toggle
// and the granted <all_urls> permission — so the user grants access ONCE and
// never sees a per-site prompt again. The static content_scripts in the
// manifest still cover the known Copilot domains with zero setup.
async function syncAllSitesRegistration() {
  let runOnAllSites = false;
  try {
    ({ runOnAllSites } = await chrome.storage.sync.get({ runOnAllSites: false }));
  } catch (_) {}
  const hasPerm = await chrome.permissions.contains({ origins: ["<all_urls>"] }).catch(() => false);

  let registered = [];
  try {
    registered = await chrome.scripting.getRegisteredContentScripts({ ids: [ALL_SITES_ID] });
  } catch (_) {}
  const isRegistered = registered.length > 0;

  if (runOnAllSites && hasPerm) {
    if (!isRegistered) {
      try {
        await chrome.scripting.registerContentScripts([
          {
            id: ALL_SITES_ID,
            js: ["content.js"],
            matches: ["<all_urls>"],
            runAt: "document_idle",
            allFrames: true
          }
        ]);
      } catch (e) {
        console.warn("[clibridge] registerContentScripts failed:", e);
      }
    }
  } else if (isRegistered) {
    try {
      await chrome.scripting.unregisterContentScripts({ ids: [ALL_SITES_ID] });
    } catch (_) {}
  }
}

chrome.runtime.onInstalled.addListener(syncAllSitesRegistration);
chrome.runtime.onStartup.addListener(syncAllSitesRegistration);
chrome.permissions.onAdded.addListener(syncAllSitesRegistration);
chrome.permissions.onRemoved.addListener(syncAllSitesRegistration);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.runOnAllSites) syncAllSitesRegistration();
});

// requestId -> handler. Two shapes:
//   { kind: "tab", tabId, frameId }  -> forward to content script
//   { kind: "cb",  cb: fn }          -> invoke callback (used by ping)
const pending = new Map();
let port = null;

function connectHost() {
  if (port) return port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (e) {
    console.error("[clibridge] connectNative threw:", e);
    port = null;
    return null;
  }

  port.onMessage.addListener((msg) => {
    const target = pending.get(msg.id);
    if (!target) return;
    pending.delete(msg.id);
    if (target.kind === "cb") {
      target.cb(msg);
      return;
    }
    chrome.tabs
      .sendMessage(target.tabId, { type: "exec-result", payload: msg }, { frameId: target.frameId })
      .catch(() => {/* tab may be gone */});
  });

  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    const emsg = err && err.message ? err.message : "unknown";
    console.warn("[clibridge] native host disconnected:", emsg);
    for (const [id, target] of pending.entries()) {
      const payload = {
        id,
        ok: false,
        error: "Native host disconnected — is it installed? " + emsg
      };
      if (target.kind === "cb") target.cb(payload);
      else
        chrome.tabs
          .sendMessage(target.tabId, { type: "exec-result", payload }, { frameId: target.frameId })
          .catch(() => {});
    }
    pending.clear();
    port = null;
  });

  return port;
}

let counter = 0;
const nextId = () => `${Date.now()}-${(counter = (counter + 1) % 1e9)}`;

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message) return;

  // ---- exec: from content scripts only -----------------------------------
  if (message.type === "exec") {
    if (!sender.tab) {
      sendResponse({ ok: false, error: "exec must originate from a tab" });
      return;
    }
    const command = String(message.command || "").trim();
    if (!command) {
      sendResponse({ ok: false, error: "empty command" });
      return;
    }
    const p = connectHost();
    if (!p) {
      sendResponse({ ok: false, error: "Could not start native host. See README." });
      return;
    }
    const id = nextId();
    pending.set(id, { kind: "tab", tabId: sender.tab.id, frameId: sender.frameId });
    try {
      p.postMessage({ id, command, origin: sender.origin || sender.url || "" });
      sendResponse({ ok: true, queued: id });
    } catch (e) {
      pending.delete(id);
      sendResponse({ ok: false, error: String(e) });
    }
    return; // real result arrives async via chrome.tabs.sendMessage
  }

  // ---- ping-host: from popup/options -------------------------------------
  if (message.type === "ping-host") {
    const p = connectHost();
    if (!p) {
      sendResponse({ ok: false, error: "connectNative failed" });
      return true;
    }
    const id = "ping-" + nextId();
    const timer = setTimeout(() => {
      pending.delete(id);
      sendResponse({ ok: false, error: "timeout waiting for host" });
    }, 3000);
    pending.set(id, {
      kind: "cb",
      cb: (msg) => {
        clearTimeout(timer);
        sendResponse({ ok: !!msg.ok, host: msg });
      }
    });
    try {
      p.postMessage({ id, command: "__ping__", origin: "popup" });
    } catch (e) {
      clearTimeout(timer);
      pending.delete(id);
      sendResponse({ ok: false, error: String(e) });
    }
    return true; // async response
  }
});
