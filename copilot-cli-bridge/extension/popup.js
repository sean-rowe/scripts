const DEFAULTS = { marker: "#!run", autoType: true, autoSubmit: false, enabled: true };
const $ = (id) => document.getElementById(id);

chrome.storage.sync.get(DEFAULTS, (v) => {
  const c = { ...DEFAULTS, ...v };
  $("marker").value = c.marker;
  $("autoType").checked = c.autoType;
  $("autoSubmit").checked = c.autoSubmit;
  $("enabled").checked = c.enabled;
});

function save() {
  chrome.storage.sync.set({
    marker: $("marker").value.trim() || "#!run",
    autoType: $("autoType").checked,
    autoSubmit: $("autoSubmit").checked,
    enabled: $("enabled").checked
  });
}
for (const id of ["marker", "autoType", "autoSubmit", "enabled"]) {
  $(id).addEventListener("change", save);
  $(id).addEventListener("input", save);
}

// "Run on all sites" toggle: request the <all_urls> permission once (this click
// is the required user gesture), then background.js registers the content script
// everywhere. Unchecking removes the permission.
function initAllSites() {
  chrome.permissions.contains({ origins: ["<all_urls>"] }, (has) => {
    chrome.storage.sync.get({ runOnAllSites: false }, ({ runOnAllSites }) => {
      $("allSites").checked = !!(has && runOnAllSites);
    });
  });
}
$("allSites").addEventListener("change", () => {
  if ($("allSites").checked) {
    chrome.permissions.request({ origins: ["<all_urls>"] }, (granted) => {
      if (!granted) {
        $("allSites").checked = false;
        return;
      }
      chrome.storage.sync.set({ runOnAllSites: true });
    });
  } else {
    chrome.storage.sync.set({ runOnAllSites: false });
    chrome.permissions.remove({ origins: ["<all_urls>"] });
  }
});
initAllSites();

function ping() {
  const el = $("status");
  el.textContent = "checking host…";
  el.className = "";
  chrome.runtime.sendMessage({ type: "ping-host" }, (res) => {
    if (chrome.runtime.lastError) {
      el.textContent = "extension error: " + chrome.runtime.lastError.message;
      el.className = "bad";
      return;
    }
    if (res && res.ok) {
      const v = res.host && res.host.version ? " v" + res.host.version : "";
      el.textContent = "native host connected" + v;
      el.className = "ok";
    } else {
      el.textContent = "host not reachable: " + ((res && res.error) || "unknown");
      el.className = "bad";
    }
  });
}
$("ping").addEventListener("click", ping);
$("opts").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});
ping();
