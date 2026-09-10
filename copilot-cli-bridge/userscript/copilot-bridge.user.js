// ==UserScript==
// @name         Copilot CLI Bridge
// @namespace    pinyridgelabs
// @version      1.0.0
// @description  Run #!run command blocks from Microsoft Copilot on your machine via a local server.
// @match        https://copilot.microsoft.com/*
// @match        https://copilot.fun/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";
  if (window.__clibridgeUS) return;
  window.__clibridgeUS = true;

  // ---- config (server on localhost) --------------------------------------
  // Paste the contents of .bridge-token here after installing the script. It is
  // left blank on purpose: this file is committed, and a token in it would be a
  // published secret.
  const SERVER = "http://127.0.0.1:8765/run";
  const TOKEN = "";
  const MARKER = "#!run";
  const AUTOTYPE = true;   // type the result back into the chat box
  const AUTOSUBMIT = false;

  const processed = new WeakSet();

  // GM_xmlhttpRequest may be exposed as GM_xmlhttpRequest or GM.xmlHttpRequest.
  const gmx =
    typeof GM_xmlhttpRequest !== "undefined"
      ? GM_xmlhttpRequest
      : typeof GM !== "undefined" && GM.xmlHttpRequest
      ? GM.xmlHttpRequest.bind(GM)
      : null;

  function runCommand(command) {
    return new Promise((resolve) => {
      if (!gmx) return resolve({ ok: false, error: "GM_xmlhttpRequest unavailable" });
      gmx({
        method: "POST",
        url: SERVER,
        headers: { "Content-Type": "application/json", "X-Bridge-Token": TOKEN },
        data: JSON.stringify({ command }),
        timeout: 130000,
        onload: (r) => {
          try {
            resolve(JSON.parse(r.responseText));
          } catch (e) {
            resolve({ ok: false, error: "bad server response: " + r.status });
          }
        },
        onerror: () =>
          resolve({ ok: false, error: "cannot reach local server — is it running? (node local-server.js)" }),
        ontimeout: () => resolve({ ok: false, error: "server timeout" })
      });
    });
  }

  // ---- detection ----------------------------------------------------------
  function extractCommand(el) {
    const text = el.innerText || el.textContent || "";
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length && lines[i].trim() === "") i++;
    if (i >= lines.length || lines[i].trim() !== MARKER) return null;
    return lines.slice(i + 1).join("\n").trim() || null;
  }

  function scan(root) {
    const scope = root && root.querySelectorAll ? root : document;
    for (const el of scope.querySelectorAll("pre code, pre, code")) {
      if (processed.has(el)) continue;
      const cmd = extractCommand(el);
      if (!cmd) continue;
      processed.add(el);
      badge(el);
      runCommand(cmd).then((r) => {
        showPanel(cmd, r);
        if (AUTOTYPE) typeIntoChat(renderResult(cmd, r), AUTOSUBMIT);
      });
    }
  }

  // ---- result rendering / injection --------------------------------------
  function renderResult(command, r) {
    if (!r.ok) return "```\n⛔ " + (r.error || "error") + "\n```";
    const parts = ["```", "$ " + command + "   (exit " + r.code + ")"];
    if (r.stdout) parts.push(r.stdout.replace(/\s+$/, ""));
    if (r.stderr) parts.push("[stderr]\n" + r.stderr.replace(/\s+$/, ""));
    parts.push("```");
    return parts.join("\n");
  }

  function findChatInput() {
    const sels = [
      'textarea[data-testid="chat-input-textarea"]',
      'textarea[aria-label*="Message" i]',
      'textarea[placeholder*="Message" i]',
      'div[contenteditable="true"][role="textbox"]',
      'div[contenteditable="true"]',
      "textarea"
    ];
    for (const s of sels) {
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
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, (el.value ? el.value + "\n\n" : "") + text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      document.execCommand("insertText", false, (el.textContent ? "\n\n" : "") + text);
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
    if (submit) el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
  }

  // ---- floating panel -----------------------------------------------------
  let panel;
  function showPanel(command, r) {
    if (!panel) {
      panel = document.createElement("div");
      panel.style.cssText =
        "position:fixed;right:16px;bottom:16px;z-index:2147483647;width:min(520px,90vw);" +
        "max-height:60vh;overflow:auto;background:#111;color:#e6e6e6;" +
        "font:12px/1.45 ui-monospace,Menlo,monospace;border:1px solid #333;border-radius:10px;" +
        "box-shadow:0 8px 30px rgba(0,0,0,.45);padding:10px 12px";
      document.documentElement.appendChild(panel);
    }
    const ok = r.ok;
    const head = document.createElement("div");
    head.style.cssText =
      "display:flex;justify-content:space-between;margin-bottom:6px;color:" + (ok ? "#7ee787" : "#ff7b72");
    head.innerHTML =
      "<strong>Copilot CLI Bridge</strong><span>" + (ok ? "exit " + r.code : "error") + "</span>";
    const body = document.createElement("pre");
    body.style.cssText = "white-space:pre-wrap;margin:0";
    body.textContent = ok
      ? "$ " + command + "\n" + (r.stdout || "").replace(/\s+$/, "") +
        (r.stderr ? "\n[stderr]\n" + r.stderr.replace(/\s+$/, "") : "")
      : "$ " + command + "\n" + (r.error || "error");
    panel.replaceChildren(head, body);
    clearTimeout(panel._t);
    panel._t = setTimeout(() => {
      if (panel) panel.remove();
      panel = null;
    }, 20000);
  }

  function badge(el) {
    try {
      el.style.outline = "2px solid #58a6ff";
    } catch (_) {}
  }

  // ---- observe ------------------------------------------------------------
  new MutationObserver((muts) => {
    for (const m of muts) {
      for (const n of m.addedNodes) if (n.nodeType === 1) scan(n);
      if (m.type === "characterData" && m.target.parentElement) scan(m.target.parentElement);
    }
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  scan(document);

  // ---- "!" direct-command escape (like Claude Code) -----------------------
  // Type "!ls ~/Projects" in the chat box + Enter: runs locally, Copilot never
  // receives it.
  function isChatInput(el) {
    return el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable);
  }
  function clearInput(el) {
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, "");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.textContent = "";
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
  }
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
      const el = e.target;
      if (!isChatInput(el)) return;
      const text = ((el.value !== undefined && el.value !== null ? el.value : el.textContent) || "").trimStart();
      if (!text.startsWith("!")) return;
      const command = text.slice(1).trim();
      e.preventDefault();
      e.stopImmediatePropagation();
      if (!command) return;
      clearInput(el);
      runCommand(command).then((r) => showPanel(command, r));
    },
    true
  );
})();
