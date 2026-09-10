// Content script injected into Copilot surfaces.
//
// Flow:
//   1. Watch the page for code blocks whose FIRST line is the trigger marker
//      (default "#!run"). Everything after that line is the command.
//   2. Send the command to the background worker -> native host -> shell.
//   3. When the result returns, show a floating panel, copy it to the clipboard,
//      and (optionally) type it back into the Copilot chat box so Copilot can
//      read the output and continue.
//
// Copilot is prompted (see README) to emit blocks like:
//
//     ```
//     #!run
//     git status
//     ```
//
// Nothing runs unless the marker is present AND the native host's local
// allowlist permits it — ordinary code samples Copilot shows are never run.

(() => {
  // Guard against double injection (static match + "all sites" dynamic match
  // can both fire on the same Copilot page).
  if (window.__clibridgeLoaded) return;
  window.__clibridgeLoaded = true;

  const DEFAULTS = {
    marker: "#!run",   // code blocks starting with this line are run as commands
    autoType: true,    // type result back into the chat input
    autoSubmit: false, // press Enter after typing the result
    enabled: true
  };
  let cfg = { ...DEFAULTS };
  chrome.storage.sync.get(DEFAULTS, (v) => (cfg = { ...DEFAULTS, ...v }));
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "sync") return;
    for (const k of Object.keys(changes)) cfg[k] = changes[k].newValue;
  });

  const processed = new WeakSet();
  const idToCommand = new Map(); // queued id -> command, for labeling results

  // ---- detection ---------------------------------------------------------
  // Returns the command string, or null. First non-blank line must be the marker.
  function extractCommand(codeEl) {
    const text = codeEl.innerText || codeEl.textContent || "";
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length && lines[i].trim() === "") i++;
    if (i >= lines.length || lines[i].trim() !== cfg.marker) return null;
    return lines.slice(i + 1).join("\n").trim() || null;
  }

  function scan(root) {
    if (!cfg.enabled) return;
    const scope = root && root.querySelectorAll ? root : document;
    for (const el of scope.querySelectorAll("pre code, pre, code")) {
      if (processed.has(el)) continue;
      const cmd = extractCommand(el);
      if (!cmd) continue;
      processed.add(el);
      badge(el, "running…");
      run(el, cmd);
    }
  }

  function run(el, command) {
    chrome.runtime.sendMessage({ type: "exec", command }, (ack) => {
      if (chrome.runtime.lastError) {
        badge(el, "bridge error");
        showPanel(command, { ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      if (ack && ack.ok && ack.queued) idToCommand.set(ack.queued, command);
      if (!ack || !ack.ok) {
        badge(el, "blocked");
        showPanel(command, { ok: false, error: (ack && ack.error) || "no acknowledgement" });
      }
      // Successful result arrives asynchronously via the listener below.
    });
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== "exec-result") return;
    const r = message.payload || {};
    const command = idToCommand.get(r.id) || "";
    idToCommand.delete(r.id);
    showPanel(command, r);
    if (cfg.autoType) typeIntoChat(renderResult(command, r), cfg.autoSubmit);
  });

  // ---- result rendering --------------------------------------------------
  function renderResult(command, r) {
    if (!r.ok) return `\`\`\`\n⛔ CLI bridge: ${r.error || "unknown error"}\n\`\`\``;
    const parts = ["```", `$ ${command}   (exit ${r.code})`];
    if (r.stdout) parts.push(r.stdout.trimEnd());
    if (r.stderr) parts.push("[stderr]\n" + r.stderr.trimEnd());
    parts.push("```");
    return parts.join("\n");
  }

  // ---- inject result into the chat input ---------------------------------
  function findChatInput() {
    const selectors = [
      'textarea[data-testid="chat-input-textarea"]',
      'textarea[aria-label*="Ask" i]',
      'textarea[placeholder*="Message" i]',
      'textarea[placeholder*="Ask" i]',
      'div[contenteditable="true"][role="textbox"]',
      'div[contenteditable="true"]',
      "textarea"
    ];
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (el && el.offsetParent !== null) return el;
    }
    return null;
  }

  function typeIntoChat(text, submit) {
    const el = findChatInput();
    if (!el) return;
    el.focus();
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      const proto = el.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      const existing = el.value ? el.value + "\n\n" : "";
      setter.call(el, existing + text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      document.execCommand("insertText", false, (el.textContent ? "\n\n" : "") + text);
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
    if (submit) {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
    }
  }

  // ---- floating panel (always shown; clipboard fallback) -----------------
  let panel;
  function showPanel(command, r) {
    if (!panel) {
      panel = document.createElement("div");
      panel.style.cssText =
        "position:fixed;right:16px;bottom:16px;z-index:2147483647;width:min(520px,90vw);" +
        "max-height:60vh;overflow:auto;background:#111;color:#e6e6e6;" +
        "font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid #333;" +
        "border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.45);padding:10px 12px";
      document.documentElement.appendChild(panel);
    }
    const ok = r.ok;
    const head = document.createElement("div");
    head.style.cssText =
      "display:flex;justify-content:space-between;gap:8px;margin-bottom:6px;color:" +
      (ok ? "#7ee787" : "#ff7b72");
    head.innerHTML = `<strong>Copilot CLI Bridge</strong><span>${
      ok ? "exit " + (r.code ?? "?") : "error"
    }</span>`;
    const body = document.createElement("pre");
    body.style.cssText = "white-space:pre-wrap;margin:0";
    body.textContent = ok
      ? `$ ${command}\n${(r.stdout || "").trimEnd()}${
          r.stderr ? "\n[stderr]\n" + r.stderr.trimEnd() : ""
        }`
      : `$ ${command}\n${r.error || "unknown error"}`;
    panel.replaceChildren(head, body);
    try {
      navigator.clipboard.writeText(renderResult(command, r));
    } catch (_) {}
    clearTimeout(panel._t);
    panel._t = setTimeout(() => {
      if (panel) panel.remove();
      panel = null;
    }, 20000);
  }

  function badge(el, label) {
    try {
      el.setAttribute("data-clibridge", label);
      el.style.outline = "2px solid #58a6ff";
    } catch (_) {}
  }

  // ---- observe the page --------------------------------------------------
  new MutationObserver((muts) => {
    for (const m of muts) {
      for (const n of m.addedNodes) if (n.nodeType === 1) scan(n);
      if (m.type === "characterData" && m.target.parentElement) scan(m.target.parentElement);
    }
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });

  scan(document);
})();
