// Copilot CLI Bridge — content script.
//
// Type `!` in the Copilot chat box to run a local command instead of sending a
// message. The bridge server does the work and answers with one of:
//
//   text   -> inserted into the composer (you review, then press Enter)
//   files  -> attached to the message as real uploads
//   note   -> shown only in the local panel
//
// Nothing is sent to Copilot automatically unless you add --send.

(function () {
  "use strict";
  if (window.__clibridge) return;
  window.__clibridge = true;

  const api = typeof browser !== "undefined" ? browser : chrome;
  const MARKER = "#!run";
  const HISTORY_KEY = "clibridge.history";
  const AUTORUN_KEY = "clibridge.autorun";

  // Auto-running #!run blocks is off by default: anything that lands in the
  // page (including text Copilot quotes from a web page) could contain one.
  let autoRun = false;
  try {
    autoRun = sessionStorage.getItem(AUTORUN_KEY) === "1";
  } catch (_) {}

  let history = [];
  let historyIdx = -1;
  try {
    history = JSON.parse(sessionStorage.getItem(HISTORY_KEY) || "[]");
  } catch (_) {}

  const send = (msg) => Promise.resolve(api.runtime.sendMessage(msg)).catch((e) => ({
    ok: false,
    error: "extension messaging failed: " + (e && e.message)
  }));

  // ---- composer ----------------------------------------------------------

  const COMPOSER_SELECTORS = [
    'textarea[data-testid="chat-input-textarea"]',
    "textarea#userInput",
    'textarea[aria-label*="Message" i]',
    'textarea[placeholder*="Message" i]',
    'textarea[placeholder*="Ask" i]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    "textarea"
  ];

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.offsetParent !== null;
  }

  function findComposer() {
    for (const sel of COMPOSER_SELECTORS) {
      for (const el of document.querySelectorAll(sel)) if (visible(el)) return el;
    }
    return null;
  }

  function isChatInput(el) {
    return !!el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable);
  }

  function readInput(el) {
    if (!el) return "";
    return (el.value !== undefined && el.value !== null ? el.value : el.textContent) || "";
  }

  // React owns the value, so poke the native setter and fire `input`.
  function setInput(el, value) {
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.textContent = value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
  }

  function appendToComposer(text) {
    const el = findComposer();
    if (!el) return false;
    el.focus();
    const existing = readInput(el);
    const joined = existing.trim() ? existing.replace(/\s+$/, "") + "\n\n" + text : text;
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      setInput(el, joined);
      try {
        el.selectionStart = el.selectionEnd = joined.length;
      } catch (_) {}
    } else {
      // insertText keeps contenteditable editors (and their undo stack) happy.
      const ok = document.execCommand("insertText", false, (existing ? "\n\n" : "") + text);
      if (!ok) setInput(el, joined);
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
    el.scrollTop = el.scrollHeight;
    return true;
  }

  function submitComposer() {
    const el = findComposer();
    if (!el) return false;
    el.focus();
    for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(
        new KeyboardEvent(type, {
          key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true
        })
      );
    }
    return true;
  }

  // ---- attachments -------------------------------------------------------

  function b64ToFile(desc) {
    const bin = atob(desc.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], desc.name, { type: desc.mime || "text/plain", lastModified: Date.now() });
  }

  function findFileInputs() {
    const inputs = [...document.querySelectorAll('input[type="file"]')].filter((i) => !i.disabled);
    // Prefer the most recently rendered one; Copilot re-creates it per composer.
    return inputs.reverse();
  }

  function findDropTarget() {
    const composer = findComposer();
    if (!composer) return null;
    // Walk up a few levels — the drop handler usually sits on the composer
    // shell — but never past <body>, or the event misses the app entirely.
    let el = composer;
    for (let i = 0; i < 4; i++) {
      const parent = el.parentElement;
      if (!parent || parent === document.body || parent === document.documentElement) break;
      el = parent;
    }
    return el;
  }

  function countOccurrences(haystack, needle) {
    if (!needle) return 0;
    let n = 0;
    let i = 0;
    while ((i = haystack.indexOf(needle, i)) !== -1) {
      n++;
      i += needle.length;
    }
    return n;
  }

  // Copilot renders a chip with the filename once an upload is accepted, so
  // watch for the name appearing more often than it did before we tried.
  function attachmentProbe(names) {
    const probes = names.map((n) => n.slice(0, 24));
    const before = probes.map((p) => countOccurrences(document.body.innerText || "", p));
    return function landed() {
      const text = document.body.innerText || "";
      return probes.some((p, i) => countOccurrences(text, p) > before[i]);
    };
  }

  function wait(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  async function attachFiles(descs) {
    let files;
    try {
      files = descs.map(b64ToFile);
    } catch (e) {
      return { ok: false, error: "could not decode files: " + e.message };
    }

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    const landed = attachmentProbe(files.map((f) => f.name));

    const attempts = [];
    for (const input of findFileInputs()) {
      attempts.push([
        "file input" + (input.id ? " #" + input.id : ""),
        () => {
          input.files = dt.files;
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
        }
      ]);
    }
    const target = findDropTarget();
    const composer = findComposer();
    if (composer) {
      attempts.push([
        "paste",
        () => {
          composer.focus();
          composer.dispatchEvent(
            new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true })
          );
        }
      ]);
    }
    if (target) {
      attempts.push([
        "drop",
        () => {
          for (const type of ["dragenter", "dragover"]) {
            target.dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
          }
          target.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
        }
      ]);
    }

    if (!attempts.length) return { ok: false, error: "no file input, composer, or drop target found on this page" };

    const tried = [];
    for (const [label, run] of attempts) {
      tried.push(label);
      try {
        run();
      } catch (e) {
        continue;
      }
      // Give the app a moment to render the attachment chip.
      for (let i = 0; i < 20; i++) {
        await wait(200);
        if (landed()) return { ok: true, how: label };
      }
    }
    return { ok: false, error: "tried " + tried.join(", ") + " — no attachment appeared", tried };
  }

  // ---- reading Copilot's reply (for !save) --------------------------------

  const REPLY_SELECTORS = [
    '[data-content="ai-message"]',
    '[data-testid*="ai-message"]',
    '[data-testid*="assistant"]',
    '[data-message-author-role="assistant"]',
    "div[role=article]"
  ];

  function lastReplyElement() {
    for (const sel of REPLY_SELECTORS) {
      const nodes = document.querySelectorAll(sel);
      if (nodes.length) return nodes[nodes.length - 1];
    }
    // Fall back to whatever contains the last code block on the page.
    const pres = document.querySelectorAll("pre");
    if (pres.length) {
      let el = pres[pres.length - 1];
      for (let i = 0; i < 6 && el.parentElement && el.parentElement !== document.body; i++) el = el.parentElement;
      return el;
    }
    return null;
  }

  function capturePayload() {
    const el = lastReplyElement();
    if (!el) return { reply: "", blocks: [] };
    const blocks = [...el.querySelectorAll("pre")].map((p) => {
      const code = p.querySelector("code");
      return (code || p).innerText.replace(/\s+$/, "");
    });
    // No <pre> inside the detected reply? Take the page's trailing code blocks.
    if (!blocks.length) {
      const pres = [...document.querySelectorAll("pre")].slice(-5);
      for (const p of pres) {
        const code = p.querySelector("code");
        blocks.push((code || p).innerText.replace(/\s+$/, ""));
      }
    }
    return { reply: (el.innerText || "").replace(/\s+$/, ""), blocks };
  }

  // ---- panel -------------------------------------------------------------

  let panel = null;

  function ensurePanel() {
    if (panel && document.documentElement.contains(panel.root)) return panel;
    const root = document.createElement("div");
    root.style.cssText = [
      "position:fixed", "right:16px", "bottom:16px", "z-index:2147483647",
      "width:min(560px,92vw)", "max-height:64vh", "display:flex", "flex-direction:column",
      "background:#0f1115", "color:#e6e6e6", "border:1px solid #2a2f3a", "border-radius:12px",
      "box-shadow:0 12px 40px rgba(0,0,0,.55)",
      "font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace", "overflow:hidden"
    ].join(";");

    const head = document.createElement("div");
    head.style.cssText =
      "display:flex;align-items:center;gap:8px;padding:8px 10px;background:#161a22;" +
      "border-bottom:1px solid #2a2f3a;flex:0 0 auto";

    const dot = document.createElement("span");
    dot.style.cssText = "width:8px;height:8px;border-radius:50%;background:#ff5c8a;flex:0 0 auto";

    const title = document.createElement("strong");
    title.textContent = "CLI Bridge";
    title.style.cssText = "font-weight:600;letter-spacing:.02em";

    const cwd = document.createElement("span");
    cwd.style.cssText =
      "margin-left:auto;color:#7d8590;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%";

    const close = document.createElement("button");
    close.textContent = "✕";
    close.title = "close (Esc)";
    close.style.cssText =
      "background:none;border:none;color:#7d8590;cursor:pointer;font-size:13px;padding:0 2px;flex:0 0 auto";
    close.onclick = hidePanel;

    head.append(dot, title, cwd, close);

    const body = document.createElement("pre");
    body.style.cssText =
      "margin:0;padding:10px 12px;overflow:auto;white-space:pre-wrap;word-break:break-word;flex:1 1 auto";

    root.append(head, body);
    document.documentElement.appendChild(root);
    panel = { root, body, cwd, dot, title };
    return panel;
  }

  function hidePanel() {
    if (panel) {
      panel.root.remove();
      panel = null;
    }
  }

  function showPanel(text, opts) {
    const o = opts || {};
    const p = ensurePanel();
    p.body.textContent = text;
    p.body.scrollTop = 0;
    p.dot.style.background = o.error ? "#ff7b72" : o.busy ? "#d29922" : "#7ee787";
    p.title.textContent = o.title || "CLI Bridge";
    if (o.cwd) p.cwd.textContent = o.cwd;
    clearTimeout(p.root._timer);
    if (o.autoHide) p.root._timer = setTimeout(hidePanel, o.autoHide);
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && panel) hidePanel();
  });

  // ---- the ! command line ------------------------------------------------

  function pushHistory(line) {
    history = [line, ...history.filter((h) => h !== line)].slice(0, 100);
    historyIdx = -1;
    try {
      sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history));
    } catch (_) {}
  }

  function localCommand(line) {
    const [name, ...rest] = line.split(/\s+/);
    switch (name) {
      case "probe":
        return probe();
      case "auto":
        if (rest[0] === "on" || rest[0] === "off") {
          autoRun = rest[0] === "on";
          try {
            sessionStorage.setItem(AUTORUN_KEY, autoRun ? "1" : "0");
          } catch (_) {}
        }
        return (
          "auto-run of #!run code blocks: " + (autoRun ? "ON" : "OFF") +
          "\n\nWhen ON, any fenced block starting with #!run executes immediately." +
          "\nAnything that reaches the page can contain one, so leave it off unless" +
          "\nyou're actively driving Copilot that way.  !auto on   !auto off"
        );
      case "panel":
        return "panel stays until you press Esc or click ✕";
      default:
        return null;
    }
  }

  function probe() {
    const composer = findComposer();
    const inputs = findFileInputs();
    const reply = lastReplyElement();
    const payload = capturePayload();
    const describe = (el) =>
      !el
        ? "none"
        : el.tagName.toLowerCase() +
          (el.id ? "#" + el.id : "") +
          (el.getAttribute("data-testid") ? "[data-testid=" + el.getAttribute("data-testid") + "]" : "") +
          (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".") : "");
    return [
      "DOM probe — what the bridge can see on this page",
      "",
      "composer:      " + describe(composer),
      "file inputs:   " + (inputs.length ? inputs.map(describe).join("\n               ") : "none found"),
      "drop target:   " + describe(findDropTarget()),
      "last reply:    " + describe(reply),
      "code blocks:   " + payload.blocks.length + " in the last reply",
      "reply length:  " + payload.reply.length + " chars",
      "",
      "If attachments fail, send me this output and I'll fix the selectors."
    ].join("\n");
  }

  async function runBridgeCommand(line) {
    const local = localCommand(line);
    if (local !== null) {
      showPanel(local);
      return;
    }

    showPanel("$ " + line + "\n\nworking…", { busy: true });
    const payload = /^(save|write)\b/.test(line) ? capturePayload() : undefined;
    const r = (await send({ type: "cmd", line, payload })) || {};

    if (!r.ok) {
      showPanel("$ " + line + "\n\n⛔ " + (r.error || "unknown error"), { error: true, cwd: r.cwd });
      return;
    }

    const lines = ["$ " + line];
    if (r.note) lines.push("", r.note);

    if (r.kind === "files" && r.files && r.files.length) {
      const res = await attachFiles(r.files);
      if (res.ok) {
        lines.push("", "✔ attached via " + res.how + ":");
        for (const f of r.files) lines.push("   " + f.name + "  (" + f.size + " bytes)");
        if (r.text) appendToComposer(r.text);
      } else {
        lines.push("", "⛔ attach failed: " + res.error);
        lines.push("", "Falling back to !probe so you can see what's on the page:", "", probe());
      }
      showPanel(lines.join("\n"), { error: !res.ok, cwd: r.cwd });
      if (res.ok && r.send) submitComposer();
      return;
    }

    if (r.kind === "text" && r.text) {
      const ok = appendToComposer(r.text);
      lines.push("", ok ? "✔ inserted into the chat box (" + r.text.length + " chars) — press Enter to send" : "⛔ couldn't find the chat box");
      showPanel(lines.join("\n"), { error: !ok, cwd: r.cwd, autoHide: ok ? 6000 : 0 });
      if (ok && r.send) submitComposer();
      return;
    }

    showPanel(lines.join("\n"), { cwd: r.cwd });
  }

  // ---- key handling ------------------------------------------------------

  document.addEventListener(
    "keydown",
    (e) => {
      const el = e.target;
      if (!isChatInput(el)) return;
      const raw = readInput(el);
      const bang = raw.trimStart().startsWith("!");

      // History recall while in ! mode.
      if (bang && (e.key === "ArrowUp" || e.key === "ArrowDown") && history.length) {
        const single = !raw.includes("\n");
        if (single) {
          e.preventDefault();
          e.stopImmediatePropagation();
          historyIdx = e.key === "ArrowUp"
            ? Math.min(historyIdx + 1, history.length - 1)
            : Math.max(historyIdx - 1, -1);
          setInput(el, historyIdx === -1 ? "!" : "!" + history[historyIdx]);
          showHint(el);
          return;
        }
      }

      if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
      if (!bang) return;

      const line = raw.trimStart().slice(1).trim();
      // Beat Copilot's own Enter handler; this message is ours.
      e.preventDefault();
      e.stopImmediatePropagation();
      if (!line) return;
      setInput(el, "");
      setBangStyle(el, false);
      pushHistory(line);
      runBridgeCommand(line);
    },
    true
  );

  // ---- pink ! indicator (like Claude Code's bash mode) --------------------

  function setBangStyle(el, on) {
    try {
      if (on) {
        el.style.setProperty("background-color", "rgba(255,92,138,0.10)", "important");
        el.style.setProperty("outline", "2px solid #ff5c8a", "important");
        el.style.setProperty("outline-offset", "1px", "important");
        el.style.setProperty("border-radius", "10px", "important");
        el.dataset.clibridgeBang = "1";
        showHint(el);
      } else if (el.dataset && el.dataset.clibridgeBang) {
        el.style.removeProperty("background-color");
        el.style.removeProperty("outline");
        el.style.removeProperty("outline-offset");
        el.style.removeProperty("border-radius");
        delete el.dataset.clibridgeBang;
        hideHint();
      }
    } catch (_) {}
  }

  let hint = null;
  function showHint(el) {
    if (!hint) {
      hint = document.createElement("div");
      hint.style.cssText =
        "position:fixed;z-index:2147483646;background:#ff5c8a;color:#12060b;padding:2px 8px;" +
        "border-radius:6px 6px 0 0;font:11px/1.6 ui-monospace,Menlo,monospace;font-weight:600;pointer-events:none";
      document.documentElement.appendChild(hint);
    }
    const text = readInput(el).trimStart().slice(1).trim().split(/\s+/)[0];
    hint.textContent = "bridge" + (text ? " · " + text : "  —  !help for commands");
    const r = el.getBoundingClientRect();
    hint.style.left = r.left + "px";
    hint.style.top = Math.max(0, r.top - 20) + "px";
  }

  function hideHint() {
    if (hint) {
      hint.remove();
      hint = null;
    }
  }

  function updateBangMode(e) {
    const el = e.target;
    if (!isChatInput(el)) return;
    setBangStyle(el, readInput(el).trimStart().startsWith("!"));
  }
  document.addEventListener("input", updateBangMode, true);
  document.addEventListener("keyup", updateBangMode, true);
  document.addEventListener("focusout", (e) => {
    if (isChatInput(e.target) && !readInput(e.target).trimStart().startsWith("!")) hideHint();
  }, true);

  // ---- legacy: auto-run #!run blocks (opt-in via !auto on) ----------------

  const processed = new WeakSet();

  function extractCommand(el) {
    // Only look at the outermost code element so a <pre><code> pair fires once.
    if (el.tagName === "CODE" && el.parentElement && el.parentElement.tagName === "PRE") return null;
    const text = el.innerText || el.textContent || "";
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length && lines[i].trim() === "") i++;
    if (i >= lines.length || lines[i].trim() !== MARKER) return null;
    return lines.slice(i + 1).join("\n").trim() || null;
  }

  function scan(root) {
    if (!autoRun) return;
    const scope = root && root.querySelectorAll ? root : document;
    for (const el of scope.querySelectorAll("pre, code")) {
      if (processed.has(el)) continue;
      const cmd = extractCommand(el);
      if (!cmd) continue;
      processed.add(el);
      el.style.outline = "2px solid #58a6ff";
      send({ type: "run", command: cmd }).then((r) => {
        r = r || { ok: false, error: "no response" };
        const out = (r.stdout || "") + (r.stderr ? "\n[stderr]\n" + r.stderr : "");
        showPanel("$ " + cmd + "\n\n" + (r.ok ? out || "(no output)" : "⛔ " + r.error), { error: !r.ok });
        if (r.ok) {
          appendToComposer(
            "I ran `" + cmd + "` on my machine (exit " + r.code + "):\n\n```\n" +
              (out.replace(/\s+$/, "") || "(no output)") + "\n```"
          );
        }
      });
    }
  }

  new MutationObserver((muts) => {
    if (!autoRun) return;
    for (const m of muts) {
      for (const n of m.addedNodes) if (n.nodeType === 1) scan(n);
      if (m.type === "characterData" && m.target.parentElement) scan(m.target.parentElement);
    }
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });

  // ---- greeting ----------------------------------------------------------

  send({ type: "ping" }).then((r) => {
    if (r && r.ok) {
      showPanel(
        "connected — bridge v" + r.version + ", " + r.commands + " commands\n\n" +
          "Type ! in the chat box, then:\n" +
          "  !help              every command\n" +
          "  !cd ~/Projects/x   set the working directory\n" +
          "  !up src ts,tsx     attach matching files to the message\n" +
          "  !grep TODO src     search and paste the hits\n" +
          "  !save out.ts       save Copilot's last code block to disk",
        { cwd: r.cwd, autoHide: 12000 }
      );
    } else {
      showPanel(
        "⛔ " + ((r && r.error) || "bridge unreachable") +
          "\n\nStart it with:\n  cd ~/Projects/pinyridgelabs/scripts/copilot-cli-bridge\n  ./launch-firefox-bridge.sh",
        { error: true }
      );
    }
  });
})();
