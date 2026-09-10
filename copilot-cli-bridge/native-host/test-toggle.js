// Headless test of background.js syncAllSitesRegistration() using a mocked
// chrome API. Verifies the toggle state machine without needing a live browser.
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "../extension/background.js"),
  "utf8"
);

// ---- mock chrome ---------------------------------------------------------
function makeChrome() {
  const state = {
    runOnAllSites: false,
    hasPerm: false,
    registry: [] // registered content scripts
  };
  const noop = { addListener() {} };
  const chrome = {
    _state: state,
    runtime: {
      onInstalled: noop,
      onStartup: noop,
      onMessage: { addListener() {} },
      connectNative() {
        throw new Error("connectNative should not be called at load");
      },
      lastError: null
    },
    permissions: {
      onAdded: noop,
      onRemoved: noop,
      contains: async ({ origins }) =>
        origins.includes("<all_urls>") ? state.hasPerm : false
    },
    storage: {
      sync: {
        get: async (defaults) => ({ runOnAllSites: state.runOnAllSites, ...{} }),
      },
      onChanged: { addListener() {} }
    },
    scripting: {
      getRegisteredContentScripts: async ({ ids } = {}) =>
        ids ? state.registry.filter((s) => ids.includes(s.id)) : state.registry.slice(),
      registerContentScripts: async (scripts) => {
        state.registry.push(...scripts);
      },
      unregisterContentScripts: async ({ ids }) => {
        state.registry = state.registry.filter((s) => !ids.includes(s.id));
      }
    },
    tabs: { sendMessage: async () => {} }
  };
  return chrome;
}

// storage.sync.get needs to honor the live state each call:
function wireStorage(chrome) {
  chrome.storage.sync.get = async () => ({ runOnAllSites: chrome._state.runOnAllSites });
}

// Load background.js in an isolated function scope, return the function.
const factory = new Function(
  "chrome",
  "console",
  src + "\nreturn { syncAllSitesRegistration };"
);

// ---- assertions ----------------------------------------------------------
let failures = 0;
function assert(cond, label) {
  if (cond) {
    console.log("  PASS  " + label);
  } else {
    failures++;
    console.log("  FAIL  " + label);
  }
}
const hasAllSites = (chrome) =>
  chrome._state.registry.some((s) => s.id === "clibridge-all-sites");

(async () => {
  const chrome = makeChrome();
  wireStorage(chrome);
  const { syncAllSitesRegistration } = factory(chrome, console);
  const s = chrome._state;

  console.log("scenario 1: toggle off, no permission");
  s.runOnAllSites = false;
  s.hasPerm = false;
  await syncAllSitesRegistration();
  assert(!hasAllSites(chrome), "not registered");

  console.log("scenario 2: toggle on but permission NOT granted");
  s.runOnAllSites = true;
  s.hasPerm = false;
  await syncAllSitesRegistration();
  assert(!hasAllSites(chrome), "still not registered (permission required)");

  console.log("scenario 3: toggle on AND permission granted");
  s.runOnAllSites = true;
  s.hasPerm = true;
  await syncAllSitesRegistration();
  assert(hasAllSites(chrome), "registered clibridge-all-sites");
  const entry = s.registry.find((x) => x.id === "clibridge-all-sites");
  assert(entry && entry.matches.includes("<all_urls>"), "matches <all_urls>");
  assert(entry && entry.js.includes("content.js"), "injects content.js");

  console.log("scenario 4: idempotent — calling again does not double-register");
  await syncAllSitesRegistration();
  assert(
    s.registry.filter((x) => x.id === "clibridge-all-sites").length === 1,
    "exactly one registration"
  );

  console.log("scenario 5: toggle off again");
  s.runOnAllSites = false;
  await syncAllSitesRegistration();
  assert(!hasAllSites(chrome), "unregistered");

  console.log("scenario 6: permission revoked while toggle still on");
  s.runOnAllSites = true;
  s.hasPerm = true;
  await syncAllSitesRegistration();
  assert(hasAllSites(chrome), "registered again");
  s.hasPerm = false; // user removed the permission
  await syncAllSitesRegistration();
  assert(!hasAllSites(chrome), "unregistered after permission revoked");

  console.log("");
  console.log(failures === 0 ? "ALL PASS" : failures + " FAILURE(S)");
  process.exit(failures === 0 ? 0 : 1);
})();
