/* Claude Code GUI - renderer + event pump */
const $ = (s) => document.querySelector(s);
const el = (t, c, txt) => { const n = document.createElement(t); if (c) n.className = c; if (txt != null) n.textContent = txt; return n; };
const api = () => window.pywebview && window.pywebview.api;

const state = {
  busy: false, started: false, sessionId: null, cwd: "",
  pending: [],            // permission requests
  tools: new Map(),       // tool_use_id -> card refs
  stream: new Map(),      // messageId -> {node, kind, buf}
  streamed: new Set(),    // messageIds already rendered from deltas
  currentAI: null,
  pollFails: 0,
  tick: 0,
  commands: [],        // slash command catalogue from the backend
  slashList: [],       // currently filtered suggestions
  slashSel: 0,
  turnChars: 0,        // assistant text rendered this turn (safety net)
  lastUserMsgId: null,
  attachments: [],     // {id,name,kind,mime,bytes,data?} staged for the next send
  attWired: false,     // wireAttachments() registration guard
};

/* ------------------------------------------------------------------ utils */
/* Layout guard: if the composer ever lands below the fold (grid/viewport quirks,
   zoom, window restore), pin it to the bottom so the chat box is always reachable. */
function layoutCheck(report) {
  const c = $("#composer");
  if (!c) return false;
  const r = c.getBoundingClientRect();
  const vh = window.innerHeight, vw = window.innerWidth;
  const off = r.bottom > vh + 2 || r.top >= vh || r.height === 0;
  if (off) {
    document.body.classList.add("composer-pinned");
    const side = $("#sidebar") ? $("#sidebar").getBoundingClientRect().width : 0;
    const rail = $("#rail") && !$("#rail").classList.contains("hidden")
      ? $("#rail").getBoundingClientRect().width : 0;
    document.documentElement.style.setProperty("--pin-left", side + "px");
    document.documentElement.style.setProperty("--pin-right", rail + "px");
  } else if (document.body.classList.contains("composer-pinned")) {
    document.body.classList.remove("composer-pinned");
  }
  if (report) {
    const t = $("#transcript").getBoundingClientRect();
    call("layout", JSON.stringify({
      vh: vh, vw: vw,
      composer: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      inView: r.bottom <= vh + 2 && r.height > 0,
      pinned: document.body.classList.contains("composer-pinned"),
      transcript: [Math.round(t.top), Math.round(t.height)],
    }));
  }
  return off;
}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), 3800);
}
async function call(fn) {
  const a = api(); if (!a || typeof a[fn] !== "function") return null;
  const args = Array.prototype.slice.call(arguments, 1);
  try {
    const r = await a[fn].apply(a, args);
    return typeof r === "string" ? JSON.parse(r) : r;
  } catch (e) { console.warn(fn, e); return null; }
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

/* minimal markdown: fenced code blocks, inline code, bold */
function renderText(node, raw) {
  node.innerHTML = "";
  const parts = String(raw).split(/```(\w*)\n?([\s\S]*?)```/g);
  for (let i = 0; i < parts.length; i++) {
    if (i % 3 === 2) { const pre = el("pre", "code"); pre.textContent = parts[i]; node.appendChild(pre); }
    else if (!parts[i]) continue;
    else {
      const p = el("div", "ai-text");
      p.innerHTML = esc(parts[i]).replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
      node.appendChild(p);
    }
  }
}

/* ------------------------------------------------------------------ transcript */
function wrap() {
  let w = $("#transcript .wrap");
  if (!w) { w = el("div", "wrap"); $("#transcript").appendChild(w); }
  return w;
}
function scrollDown() { const t = $("#transcript"); t.scrollTop = t.scrollHeight; }

/* Render a resumed session's stored transcript so the window is not blank. */
function renderHistory(items) {
  $("#transcript").innerHTML = "";
  state.stream.clear(); state.tools.clear(); state.streamed.clear(); state.currentAI = null;
  const w = wrap();
  notice("Restored transcript from the saved session \u2014 Claude also has this context.");
  (items || []).forEach((it) => {
    if (it.role === "user") {
      const txt = it.blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n");
      if (txt.trim()) addUser(txt, it.uuid);
      return;
    }
    const box = aiShell(null);
    it.blocks.forEach((b) => {
      if (b.type === "text") { const n = el("div", "ai-text"); renderText(n, b.text); box.appendChild(n); }
      else if (b.type === "thinking") addThinking(b.text, null);
      else if (b.type === "tool_use" && !state.tools.has(b.id)) addToolStart(b.id, b.name, null, b.input);
      else if (b.type === "tool_result") addToolResult(b.toolUseId, b.text, b.isError);
    });
    state.currentAI = null;
  });
  scrollDown();
}

function notice(text) {
  const n = el("div", "notice"); n.textContent = text;
  wrap().appendChild(n); state.currentAI = null; scrollDown();
}

function addUser(text, uuid, atts) {
  if (uuid) state.lastUserMsgId = uuid;
  const m = el("div", "msg-user");
  m.appendChild(el("div", null, text));
  if (atts && atts.length) m.appendChild(attachBarHtml(atts, true));
  if (uuid) {
    const b = el("button", "rewind", "\u21BA restore files to this point");
    b.onclick = () => {
      if (!confirm("Restore tracked files to their state before this message?")) return;
      call("rewind", uuid);
    };
    m.appendChild(b);
  }
  wrap().appendChild(m);
  state.currentAI = null;
  scrollDown();
}

function aiShell(parent) {
  const box = el("div", "msg-ai");
  const head = el("div", "ai-head");
  head.append(el("span", "dot"), el("span", null, parent ? "subagent" : "Claude"));
  if (parent) head.appendChild(el("span", "sub-tag", "Agent tool"));
  box.appendChild(head); wrap().appendChild(box);
  return box;
}
function ensureAI(parent) {
  if (!state.currentAI) state.currentAI = aiShell(parent);
  return state.currentAI;
}

function addThinking(text, parent) {
  const box = ensureAI(parent);
  const d = el("details", "think");
  d.appendChild(el("summary", null, "thinking"));
  const body = el("div", "body", text || "");
  d.appendChild(body); box.appendChild(d); scrollDown();
  return body;
}

function toolArg(input) {
  try {
    const o = JSON.parse(input);
    for (const k of ["command", "file_path", "path", "pattern", "prompt", "url", "query", "description", "content"])
      if (o[k]) return String(o[k]).replace(/\s+/g, " ").slice(0, 170);
    return JSON.stringify(o).slice(0, 160);
  } catch (e) { return String(input).slice(0, 150); }
}

function addToolStart(id, name, parent, inputJson) {
  const box = ensureAI(parent);
  const card = el("div", "tool");
  const head = el("div", "tool-head");
  head.appendChild(el("span", "t-name", name));
  const arg = el("span", "t-arg", inputJson ? toolArg(inputJson) : "");
  head.appendChild(arg);
  const st = el("span", "t-state run", "running");
  head.appendChild(st);
  card.appendChild(head);
  let body = null;
  if (inputJson) { body = el("pre", null); body.textContent = inputJson; card.appendChild(body); }
  const out = el("pre", "out hidden"); card.appendChild(out);
  head.onclick = () => { if (body) body.classList.toggle("hidden"); out.classList.toggle("hidden"); };
  box.appendChild(card); scrollDown();
  state.tools.set(id, { st, arg, out });
}

function addToolResult(id, text, isErr) {
  const t = state.tools.get(id);
  if (!t) return;
  t.st.className = "t-state " + (isErr ? "err" : "ok");
  t.st.textContent = isErr ? "failed" : "done";
  t.out.classList.remove("hidden");
  t.out.textContent = text || "(no output)";
  scrollDown();
}

function addResult(d) {
  /* Safety net: a turn that produced no visible prose (no text block and no
     streamed delta) still has the CLI's `result` string - surface it. */
  if (d.fallbackText && !state.turnChars) {
    const box = ensureAI(null);
    const n = el("div", "ai-text");
    renderText(n, d.fallbackText);
    box.appendChild(n);
    state.turnChars += d.fallbackText.length;
  }
  const box = el("div", "result");
  const bits = [];
  if (d.subtype === "error" || d.isError) bits.push(["status", "turn ended with an error"]);
  if (d.costUsd) bits.push(["cost", "$" + Number(d.costUsd).toFixed(4)]);
  if (d.turns) bits.push(["turns", d.turns]);
  if (d.durationMs) bits.push(["time", (d.durationMs / 1000).toFixed(1) + "s"]);
  const u = d.usage || {};
  if (u.input_tokens) bits.push(["in", u.input_tokens + (u.cache_read_input_tokens ? " (+" + u.cache_read_input_tokens + " cache)" : "")]);
  if (u.output_tokens) bits.push(["out", u.output_tokens]);
  for (const pair of bits) {
    const s = el("span");
    s.append(el("b", null, pair[0] + " "), document.createTextNode(String(pair[1])));
    box.appendChild(s);
  }
  if (d.denials && d.denials.length) {
    const s = el("span"); s.append(el("b", null, "denied "), document.createTextNode(d.denials.map((x) => x.tool).join(", ")));
    box.appendChild(s);
  }
  wrap().appendChild(box);
  state.currentAI = null;
  scrollDown();
}

/* ------------------------------------------------------------------ streaming */
function applyDelta(messageId, kind, text, parent) {
  let entry = state.stream.get(messageId);
  if (!entry) {
    let node;
    if (kind === "thinking") node = addThinking("", parent);
    else { const box = ensureAI(parent); node = el("div", "ai-text cursor"); box.appendChild(node); }
    entry = { node, kind, buf: "" };
    state.stream.set(messageId, entry);
  }
  state.streamed.add(messageId);
  entry.buf += text;
  if (kind === "text") state.turnChars += text.length;
  if (entry.kind === "thinking") entry.node.textContent = entry.buf;
  else renderText(entry.node, entry.buf);
  scrollDown();
}

/* ------------------------------------------------------------------ events */
function handle(ev) {
  const k = ev.kind;

  if (k === "commands") {
    state.commands = Array.isArray(ev.payload) ? ev.payload : [];
    const n = state.commands.filter((c) => c.kind === "skill").length;
    $("#brandSub").textContent = "connected \u00B7 " + n + " skills";
    return;
  }
  if (k === "history") {
    const p = ev.payload || {};
    if (p.error) { notice("Could not load history: " + p.error); return; }
    renderHistory(p.items);
    return;
  }
  if (k === "usage") { renderUsage(ev.payload); return; }
  if (k === "sessions") { renderSessions(ev.payload); return; }
  if (k === "git") { renderGit(ev.payload); return; }
  if (k === "auth") { renderAuth(ev.payload); return; }

  if (k === "delta") { applyDelta(ev.messageId, ev.kind, ev.text, ev.parent); return; }

  if (k === "assistant") {
    const mid = ev.messageId;
    const blocks = (ev.blocks || []).filter((b) => b.type !== "tool_result");
    const streamedText = state.streamed.has(mid);
    if (streamedText) {
      const e = state.stream.get(mid);
      if (e && e.kind === "text") e.node.classList.remove("cursor");
      state.stream.delete(mid);
    }
    if (!blocks.length) { state.currentAI = null; return; }
    if (!streamedText) {
      const box = ensureAI(ev.parentToolUseId);
      for (const b of blocks) {
        if (b.type === "text" && b.text.trim()) {
          const n = el("div", "ai-text"); renderText(n, b.text); box.appendChild(n);
          state.turnChars += b.text.length;
        } else if (b.type === "thinking") addThinking(b.text, ev.parentToolUseId);
      }
    }
    for (const b of blocks) {
      if (b.type === "tool_use" && !state.tools.has(b.id)) addToolStart(b.id, b.name, ev.parentToolUseId, b.input);
    }
    state.currentAI = null;
    scrollDown();
    return;
  }

  if (k === "tool_start") { if (!state.tools.has(ev.id)) addToolStart(ev.id, ev.name, ev.parent, ""); return; }
  if (k === "tool_result") { addToolResult(ev.id, ev.text, ev.isError); return; }

  if (k === "user_echo") {
    // subagent prompts are worth showing; main-conversation echoes we drew locally
    if (ev.parent) addUser("[subagent prompt] " + ev.text, null);
    return;
  }
  if (k === "result") { addResult(ev); setBusy(false); typing(false); fetchUsage(); refreshGit(); return; }
  if (k === "busy") { setBusy(!!ev.busy); typing(!!ev.busy); return; }

  if (k === "notice") { notice(ev.text); return; }
  if (k === "task") { $("#pathLine").textContent = (ev.tool ? ev.tool + " \u00B7 " : "") + (ev.description || ""); return; }

  if (k === "permission") { state.pending.push(ev); renderPermission(); return; }
  if (k === "permission_resolved") {
    state.pending = state.pending.filter((p) => p.id !== ev.id);
    if (!state.pending.length) $("#permOverlay").classList.add("hidden");
    else renderPermission();
    return;
  }

  if (k === "init") {
    $("#brandSub").textContent = "connected";
    const chip = $("#authChip"); chip.textContent = "session live"; chip.className = "chip ok";
    if (ev.model) { const sel = $("#modelSel"); if ([].some.call(sel.options, (o) => o.value === ev.model)) sel.value = ev.model; }
    if (ev.permissionMode) $("#modeSel").value = ev.permissionMode;
    const n = el("div", "notice");
    n.textContent = "Session ready \u00B7 " + (ev.model || "") + " \u00B7 " + (ev.tools ? ev.tools.length : 0) + " tools \u00B7 " + (ev.cwd || "");
    wrap().appendChild(n);
    state.currentAI = null;
    fetchUsage();
    return;
  }
  if (k === "system") { const n = el("div", "notice"); n.textContent = "system \u00B7 " + ev.subtype; wrap().appendChild(n); return; }

  if (k === "meta") {
    if (ev.quiet) return;                       // bookkeeping counters: ignore
    if (ev.sessionId) { state.sessionId = ev.sessionId; loadSessions(true); }
    if (ev.model) $("#modelSel").value = ev.model;
    if (ev.mode) $("#modeSel").value = ev.mode;
    return;
  }
  if (k === "status") {
    if (ev.state === "ready") {
      state.started = true;
      const n = state.commands.filter((c) => c.kind === "skill").length;
      $("#brandSub").textContent = "connected" + (n ? " \u00B7 " + n + " skills" : "");
      flushPendingSend();
    }
    if (ev.state === "error" && ev.message) toast("\u26A0 " + ev.message);
    if (ev.state === "idle") { setBusy(false); typing(false); }
    return;
  }
}

function typing(on) {
  const t = $("#typingRow");
  if (on) {
    if (!t) {
      const n = el("div", "typing"); n.id = "typingRow";
      n.innerHTML = "<i></i><i></i><i></i> Claude is working\u2026";
      wrap().appendChild(n); scrollDown();
    }
  } else if (t) t.remove();
}
function setBusy(b) {
  state.busy = b;
  $("#stopBtn").disabled = !b;
  $("#sendBtn").textContent = b ? "Working\u2026" : "Send \u21B5";
}

/* ------------------------------------------------------------------ permission modal */
function renderPermission() {
  const p = state.pending[0];
  if (!p) return;
  $("#permOverlay").classList.remove("hidden");
  $("#permTool").textContent = p.tool;
  $("#permTitle").textContent = p.displayName || p.title || ("Claude Code wants to use " + p.tool);
  $("#permDesc").textContent = p.description || "";
  $("#permInput").textContent = p.input || "{}";
  const r = $("#permReason");
  const bits = [p.reason, p.blockedPath ? "path: " + p.blockedPath : null].filter(Boolean);
  if (bits.length) { r.textContent = bits.join(" \u00B7 "); r.classList.remove("hidden"); }
  else r.classList.add("hidden");

  const always = $("#permAlways");
  const sugg = (p.suggestions && p.suggestions[0]) || null;
  if (sugg) {
    let label = "Always allow";
    if (sugg.type === "setMode") label = "Switch to " + sugg.mode + " mode";
    else if (sugg.type === "addDirectories") label = "Always allow this directory";
    else if (sugg.rules && sugg.rules[0]) {
      const r0 = sugg.rules[0];
      const name = r0.tool_name || r0.toolName;
      const content = r0.rule_content != null ? r0.rule_content : r0.ruleContent;
      label = "Always allow " + (content ? name + "(" + content.replace(/^\^|\$$/g, "") + ")" : name);
    }
    always.textContent = label;
    always.disabled = false;
    always.dataset.sugg = JSON.stringify([sugg]);
  } else {
    always.textContent = "Always allow";
    always.disabled = true;
    always.dataset.sugg = "";
  }
}

async function resolve(decision) {
  const p = state.pending[0];
  if (!p) return;
  $("#permOverlay").classList.add("hidden");
  await call("resolve_permission", p.id, JSON.stringify(decision));
}
$("#permAllow").onclick = () => resolve({ behavior: "allow" });
$("#permAlways").onclick = (e) => resolve({
  behavior: "allow",
  updated_permissions: e.target.dataset.sugg ? JSON.parse(e.target.dataset.sugg) : null,
});
$("#permDeny").onclick = async () => {
  const fb = $("#permFeedback");
  if (fb.classList.contains("hidden")) {
    fb.classList.remove("hidden");
    fb.placeholder = "Optional: tell Claude what to do instead, then click Deny again.";
    fb.focus();
    return;
  }
  await resolve({ behavior: "deny", message: fb.value || "User denied this action.", interrupt: true });
  fb.value = "";
};

/* ------------------------------------------------------------------ usage panel */
function hex(c) { return c ? (String(c).charAt(0) === "#" ? c : "#" + c) : "#8a7f72"; }
function fetchUsage() { call("context_usage"); }   // result arrives as a 'usage' event

function renderUsage(d) {
  if (!d || d.error) return;
  const body = $("#usageBody");
  body.innerHTML = "";
  const total = d.totalTokens || 1;
  const bar = el("div", "bar");
  (d.categories || []).forEach((c) => {
    const s = el("span");
    s.style.width = ((c.tokens / total) * 100).toFixed(2) + "%";
    s.style.background = hex(c.color);
    bar.appendChild(s);
  });
  body.appendChild(bar);
  body.appendChild(el("div", "muted",
    (d.totalTokens / 1000).toFixed(1) + "k / " + ((d.maxTokens || 0) / 1000).toFixed(0) + "k \u00B7 "
    + (d.percentage || 0) + "% \u00B7 " + (d.model || "")));
  (d.categories || []).forEach((c) => {
    const row = el("div", "cat");
    const l = el("span"); const i = el("i"); i.style.background = hex(c.color);
    l.append(i, document.createTextNode(c.name || "other"));
    row.appendChild(l);
    row.appendChild(el("span", null, (Math.round((c.tokens || 0) / 100) / 10) + "k"));
    body.appendChild(row);
  });
}
$("#usageBtn").onclick = () => { $("#usagePanel").classList.toggle("hidden"); fetchUsage(); };
$("#closeUsage").onclick = () => $("#usagePanel").classList.add("hidden");

/* ------------------------------------------------------------------ sessions / git */
function loadSessions(refresh) { call("sessions", state.cwd || null, !!refresh); }
function refreshGit(refresh) { call("git", state.cwd || null, !!refresh); }

function renderSessions(list) {
  const el2 = $("#sessionList");
  el2.innerHTML = "";
  if (!Array.isArray(list)) { el2.appendChild(el("div", "muted", (list && list.error) || "no sessions yet")); return; }
  list.slice(0, 25).forEach((s) => {
    const active = s.id === state.sessionId;
    const n = el("div", "s-item" + (active ? " active" : ""));
    const head = el("div", "s-head");
    head.appendChild(el("div", "s-title", s.title || "(untitled)"));
    const del = el("button", "s-del", "\u00D7");
    del.title = "Delete this session (removes its transcript from disk)";
    del.onclick = (e) => { e.stopPropagation(); deleteSession(s); };
    head.appendChild(del);
    n.appendChild(head);

    const m = el("div", "s-meta");
    if (s.branch) m.appendChild(el("span", null, "\u2387 " + s.branch));
    if (s.cwd) m.appendChild(el("span", null, String(s.cwd).split(/[\\/]/).pop()));
    m.appendChild(el("span", null, new Date(s.modified).toLocaleDateString()));
    n.appendChild(m);
    n.onclick = () => startSession(state.cwd || s.cwd || "", s.id, s.title);
    el2.appendChild(n);
  });
}

async function deleteSession(s) {
  const label = s.title || s.id.slice(0, 8);
  const live = s.id === state.sessionId;
  const msg = "Delete session \u201C" + label + "\u201D?\n\n"
    + "This permanently removes its transcript from ~/.claude/projects and cannot be undone."
    + (live ? "\n\nIt is the session currently open in this window." : "");
  if (!confirm(msg)) return;
  const r = await call("delete_session", s.id, s.cwd || state.cwd || null);
  if (r && !r.ok) { toast("Delete failed: " + (r.error || "unknown error")); return; }
  if (live) {
    state.started = false; state.sessionId = null;
    $("#brandSub").textContent = "connected";
  }
  loadSessions(true);
}

function renderGit(d) {
  const b = $("#gitBody");
  b.innerHTML = "";
  if (!d) return;
  if (d.error) { b.appendChild(el("div", "muted", d.error)); return; }
  if (d.note) b.appendChild(el("div", "rail-warn", d.note));
  if (d.branch) b.appendChild(el("div", "muted", "\u2387 " + d.branch + (d.root ? " \u00B7 " + d.root : "")));
  (d.files || []).forEach((f) => {
    const row = el("div", "g-file");
    row.appendChild(el("span", "g-xy g-" + (f.xy[0] || f.xy), f.xy || "?"));
    row.appendChild(el("span", null, f.path));
    b.appendChild(row);
  });
  if (!d.files || !d.files.length) b.appendChild(el("div", "muted", "working tree clean"));
  if (d.stat) { const p = el("pre", "g-stat"); p.textContent = d.stat; b.appendChild(p); }
}

function renderAuth(a) {
  const chip = $("#authChip");
  if (!a) return;
  if (chip.textContent === "session live") return;
  if (!a.installed) { chip.textContent = "Claude Code not installed"; chip.className = "chip bad"; toast("Install the CLI: npm install -g @anthropic-ai/claude-code"); }
  else if (a.loggedIn) { chip.textContent = "signed in \u00B7 " + (a.authMethod || ""); chip.className = "chip ok"; }
  else { chip.textContent = "not signed in"; chip.className = "chip bad"; toast("Sign in first: run  claude auth login  in a terminal"); }
}

/* ------------------------------------------------------------------ start / send */
async function startSession(cwd, resume, title) {
  if (!cwd) { toast("Pick a working directory first"); return; }
  if (resume) call("history", resume, cwd);   // replay the stored transcript
  const model = $("#modelSel").value, mode = $("#modeSel").value, effort = $("#effortSel").value;
  $("#transcript").innerHTML = "";
  state.stream.clear(); state.tools.clear(); state.streamed.clear(); state.currentAI = null;
  wrap().appendChild(el("div", "notice", resume
    ? "Resuming session \u00B7 " + (title || "")
    : "Starting a fresh Claude Code session in " + cwd));
  $("#sessionTitle").textContent = title || "New session";
  $("#pathLine").textContent = cwd;
  state.cwd = cwd;
  $("#cwdInput").value = cwd;
  await call("start", cwd, model, mode, effort, resume || null);
  loadSessions(true); refreshGit(true);
}

/* ------------------------------------------------------------------ attachments */
const MAX_ATTACH = 8;

function needSession() {
  toast(state.attachments.length
    ? "Attachments need a live session - press + New session, then send again."
    : "Start a session first (+ New session).");
}

function fileToB64(file) {
  return new Promise((resolve) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => resolve("");
    r.readAsDataURL(file);
  });
}

function attachBarHtml(list, compact) {
  const bar = el("div", "attach-bar");
  list.forEach((a) => {
    const chip = el("div", "att-chip" + (a.kind === "error" ? " err" : ""));
    if (a.kind === "image" && (a.preview || a.data)) {
      const im = el("img");
      im.src = "data:" + (a.mime || "image/png") + ";base64," + (a.preview || a.data);
      im.title = a.name || "";
      chip.appendChild(im);
    } else if (a.kind === "image") {
      chip.appendChild(el("div", "att-ico", "IMG"));
    } else {
      const ext = String(a.name || "?").split(".").pop().slice(0, 4).toUpperCase();
      chip.appendChild(el("div", "att-ico", ext));
    }
    chip.appendChild(el("span", "att-name", a.name || "attachment"));
    if (!compact) chip.appendChild(el("span", "att-kind", a.kind + (a.bytes ? " \u00B7 " + Math.round(a.bytes / 1024) + "k" : "")));
    bar.appendChild(chip);
  });
  return bar;
}

function renderAttachBar() {
  const bar = $("#attachBar");
  bar.innerHTML = "";
  bar.classList.toggle("hidden", !state.attachments.length);
  state.attachments.forEach((a, i) => {
    const chip = attachBarHtml([a], false).firstChild;
    const x = el("button", "att-x", "\u00D7");
    x.title = "Remove attachment";
    x.onclick = () => { state.attachments.splice(i, 1); renderAttachBar(); };
    chip.appendChild(x);
    bar.appendChild(chip);
  });
}

function clearAttachments() {
  state.attachments = [];
  renderAttachBar();
}

function addAttachment(a) {
  if (!a) return;
  if (a.kind === "error") { toast("Cannot attach " + (a.name || "file") + ": " + a.error); return; }
  if (state.attachments.length >= MAX_ATTACH) { toast("Max " + MAX_ATTACH + " attachments per message."); return; }
  state.attachments.push(a);
  renderAttachBar();
}

/* Base64 travels through the JS bridge as a string, so cap what we encode: huge
   payloads fail silently there. Big files should be attached by path instead. */
const MAX_BLOB_BYTES = 5 * 1024 * 1024;

async function attachFiles(files) {
  const room = Math.max(0, MAX_ATTACH - state.attachments.length);
  const list = Array.from(files || []).slice(0, room);
  if (!list.length) return;
  for (const f of list) {
    if (f.size > MAX_BLOB_BYTES) {
      toast(f.name + " is " + Math.round(f.size / 1048576) + " MB - too large to paste. "
        + "Use the Files button to attach it by path.");
      continue;
    }
    const b64 = await fileToB64(f);
    if (!b64) { toast("Could not read " + f.name); continue; }
    const r = await call("attach_blob", f.name || "file.bin", f.type || "application/octet-stream", b64);
    addAttachment(r);
  }
}

/* A pasted image appears in clipboardData.items AND clipboardData.files, so the
   same File can arrive twice. Collect from one source and de-duplicate by
   name+size+type before attaching. */
function fileKey(f) { return (f.name || "") + "|" + f.size + "|" + (f.type || ""); }

function collectPastedFiles(cd) {
  const out = [];
  const seen = new Set();
  const push = (f) => {
    if (!f) return;
    const k = fileKey(f);
    if (seen.has(k)) return;      // same file reported by items and files
    seen.add(k);
    out.push(f);
  };
  for (const it of Array.from(cd.items || [])) {
    if (it.kind === "file") push(it.getAsFile());
  }
  if (!out.length) {             // no item entries: fall back to .files
    for (const f of Array.from(cd.files || [])) push(f);
  }
  return out;
}

let pasteBusy = false;
async function onPaste(e) {
  const cd = e.clipboardData;
  if (!cd) return;
  const files = collectPastedFiles(cd).filter((f) => f && f.size > 0);

  if (files.length) {
    e.preventDefault();          // we own image/file pastes; plain text stays native
    if (pasteBusy) return;
    pasteBusy = true;
    try { await attachFiles(files); }
    finally { pasteBusy = false; }
    return;
  }

  // Plain-text paste belongs to the textarea; only raw-bitmap screenshots land here.
  const text = cd.getData ? cd.getData("text/plain") : "";
  if (text) return;             // let the textarea paste normally

  // WebView2 exposes no File for raw bitmaps: read the Win32 clipboard in Python.
  if (pasteBusy) return;
  pasteBusy = true;
  try {
    const r = await call("paste_clipboard_image");
    if (r && r.items && r.items.length) {
      e.preventDefault();
      addAttachment(r.items[0]);
      toast("Image added from the clipboard.");
    } else if (r && r.kind === "error") {
      toast("Clipboard image failed: " + (r.error || "unknown error"));
    }
  } finally {
    pasteBusy = false;
  }
}

let veil = null;
function showVeil(on) {
  if (on && !veil) {
    veil = el("div", "drop-veil", "Drop files to attach them");
    document.body.appendChild(veil);
  } else if (!on && veil) {
    veil.remove();
    veil = null;
  }
}

function wireAttachments() {
  if (state.attWired) return;   // single registration: wiring twice = double paste
  state.attWired = true;

  $("#attachFile").onclick = async () => {
    const btn = $("#attachFile");
    btn.disabled = true;
    try {
      const r = await call("pick_files");
      if (!r) { toast("File dialog failed to run."); return; }
      const items = (r.items || []);
      items.forEach(addAttachment);
      if (!items.length && r.source !== "cancelled") {
        toast("File dialog returned nothing (" + (r.source || "unknown") + ").");
      }
    } finally {
      btn.disabled = false;
    }
  };

  $("#attachClip").onclick = async () => {
    const r = await call("paste_clipboard_image");
    if (r && r.items && r.items.length) addAttachment(r.items[0]);
    else toast(r && r.kind === "error" ? "Clipboard failed: " + r.error : "No image on the clipboard.");
  };

  // Paste on the whole document, so Ctrl+V works whatever has focus. Plain-text
  // pastes are left to the textarea (onPaste returns without preventDefault).
  document.addEventListener("paste", onPaste);

  document.addEventListener("dragover", (e) => { e.preventDefault(); showVeil(true); });
  document.addEventListener("dragenter", (e) => { e.preventDefault(); showVeil(true); });
  document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) showVeil(false); });
  document.addEventListener("drop", async (e) => {
    e.preventDefault();
    showVeil(false);
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (files.length) await attachFiles(files);
  });
}

/* ------------------------------------------------------------------ slash commands */
function parseSlash(text) {
  const m = /^\/([\w-]+)\s*([\s\S]*)$/.exec(text.trim());
  if (!m) return null;
  return { name: m[1].toLowerCase(), args: (m[2] || "").trim() };
}
function findCommand(name) {
  return state.commands.find((c) => c.name === name) || null;
}

async function localCommand(c, args) {
  switch (c.name) {
    case "model": {
      const alias = args.toLowerCase();
      const opt = [...$("#modelSel").options].find((o) => o.value === alias);
      if (!opt && !alias) { notice("Usage: /model <opus|sonnet|haiku|fable|default>"); break; }
      $("#modelSel").value = opt ? alias : "default";
      await call("set_model", $("#modelSel").value);
      notice("Model set to " + $("#modelSel").value + ".");
      break;
    }
    case "effort": {
      const lvl = args.toLowerCase();
      if (!["low","medium","high","xhigh","max"].includes(lvl)) { notice("Usage: /effort <low|medium|high|xhigh|max>"); break; }
      $("#effortSel").value = lvl;
      notice("Effort \u2192 " + lvl + ". Restarting the session to apply it.");
      await startSession($("#cwdInput").value.trim(), null, "New session \u00B7 effort " + lvl);
      break;
    }
    case "plan":
      $("#modeSel").value = "plan"; await call("set_mode", "plan");
      notice("Plan mode on: Claude reads, does not write."); break;
    case "permissions": {
      const mode = args.toLowerCase();
      const known = ["default","acceptEdits","plan","bypassPermissions"];
      if (!known.includes(mode)) {
        notice("Usage: /permissions <" + known.join("|") + ">. Current: " + $("#modeSel").value);
        break;
      }
      $("#modeSel").value = mode; await call("set_mode", mode);
      notice("Permission mode \u2192 " + mode + "."); break;
    }
    case "context":
    case "usage":
      $("#usagePanel").classList.remove("hidden"); fetchUsage(); break;
    case "clear":
    case "new":
    case "reset":
      notice("Starting a fresh conversation in this project.");
      await call("restart");
      $("#transcript").innerHTML = ""; state.stream.clear(); state.tools.clear();
      state.streamed.clear(); state.currentAI = null; wrap(); break;
    case "resume":
    case "sessions":
      loadSessions(true); notice("Session list refreshed \u2014 click one in the sidebar to resume."); break;
    case "rewind":
      if (!state.lastUserMsgId) { notice("No prompt to rewind to yet."); break; }
      if (confirm("Restore tracked files to their state before your last prompt?")) await call("rewind", state.lastUserMsgId);
      break;
    case "stop":
      await call("interrupt"); notice("Interrupt requested."); break;
    case "help":
      notice("Slash commands: " + state.commands.map((x) => "/" + x.name + (x.args ? " " + x.args : "")).join("  \u00B7  "));
      break;
    case "skills": {
      const sk = state.commands.filter((x) => x.kind === "skill");
      if (!sk.length) { notice("No skills discovered in this project or ~/.claude/skills."); break; }
      notice(sk.length + " skills available \u2014 invoke one by typing it, e.g. /design-taste-frontend <your task>:\n"
        + sk.map((x) => "/" + x.name + "  (" + x.source + ")").join("\n"));
      break;
    }
    default:
      notice("No handler for /" + c.name);
  }
}

async function send() {
  const t = $("#prompt");
  const text = t.value.trim();
  if (!text && !state.attachments.length) return;
  hideSlash();

  const slash = parseSlash(text);
  if (slash) {
    const c = findCommand(slash.name);
    addUser(text, "local-" + Date.now(), state.attachments.slice());
    t.value = "";

    if (!c) {
      notice("Unknown command /" + slash.name + ". Type / to see what's available (built-ins plus your .claude/commands/*.md).");
      return;
    }
    if (c.kind === "local") { await localCommand(c, slash.args); return; }

    // custom command file: expand it into a real prompt
    let payload = text;
    if (c.kind === "expand") {
      const r = await call("expand", "/" + c.name, slash.args, state.cwd || null);
      if (!r || r.error) { notice(r && r.error ? r.error : "Command expansion failed."); return; }
      payload = r.text;
      notice("Expanded /" + c.name + " (" + (c.source === "project" ? "project" : "user") + " command) into a prompt.");
    }
    if (!state.started) { needSession(); return; }
    state.turnChars = 0;
    setBusy(true); typing(true);
    const ids = state.attachments.map((a) => a.id);
    clearAttachments();
    await call("send", payload, JSON.stringify(ids));
    return;
  }

  if (!state.started) {
    // Restored transcript with no live process: resume that session, then send.
    if (state.sessionId) { queueAfterResume(text); return; }
    needSession();
    return;
  }
  const ids = state.attachments.map((a) => a.id);
  addUser(text, "local-" + Date.now(), state.attachments.slice());
  t.value = "";
  clearAttachments();
  state.turnChars = 0;
  setBusy(true); typing(true);
  await call("send", text, JSON.stringify(ids));
}

async function queueAfterResume(text) {
  state.pendingSend = text;
  $("#prompt").value = "";
  notice("Resuming your last conversation, then sending…");
  await startSession(state.cwd, state.sessionId, "Resumed session");
}

function flushPendingSend() {
  const text = state.pendingSend;
  if (!text) return;
  state.pendingSend = null;
  const ids = state.attachments.map((a) => a.id);
  addUser(text, "local-" + Date.now(), state.attachments.slice());
  clearAttachments();
  state.turnChars = 0;
  setBusy(true); typing(true);
  call("send", text, JSON.stringify(ids));
}

/* autocomplete menu */
function slashQuery(v) {
  const m = /^\/([\w-]*)$/.exec(v.trim());
  return m ? m[1].toLowerCase() : null;
}
function hideSlash() {
  state.slashList = [];
  $("#slashMenu").classList.add("hidden");
}
function renderSlash(q) {
  const menu = $("#slashMenu");
  const list = state.commands.filter((c) => c.name.startsWith(q));
  if (!list.length) { hideSlash(); return; }
  state.slashList = list;
  if (state.slashSel >= list.length) state.slashSel = 0;
  menu.innerHTML = "";
  list.forEach((c, i) => {
    const row = el("div", "srow" + (i === state.slashSel ? " sel" : ""));
    row.appendChild(el("span", "sname", "/" + c.name));
    if (c.args) row.appendChild(el("span", "sargs", c.args));
    row.appendChild(el("span", "sdesc", c.desc));
    const tag = el("span", "stag " + c.kind, c.source === "built-in" ? c.kind : c.source);
    row.appendChild(tag);
    row.onmousedown = (e) => { e.preventDefault(); acceptSlash(i); };
    menu.appendChild(row);
  });
  menu.classList.remove("hidden");
}
function acceptSlash(i) {
  const c = state.slashList[i];
  if (!c) return;
  const t = $("#prompt");
  t.value = "/" + c.name + (c.args ? " " : "");
  hideSlash();
  t.focus();
}

function onPromptKey(e) {
  const menuOpen = !$("#slashMenu").classList.contains("hidden");
  if (menuOpen) {
    if (e.key === "ArrowDown") { e.preventDefault(); state.slashSel = (state.slashSel + 1) % state.slashList.length; renderSlash(slashQuery($("#prompt").value)); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); state.slashSel = (state.slashSel - 1 + state.slashList.length) % state.slashList.length; renderSlash(slashQuery($("#prompt").value)); return; }
    if (e.key === "Tab") { e.preventDefault(); acceptSlash(state.slashSel); return; }
    if (e.key === "Enter" && !e.shiftKey && state.slashList.length) {
      const q = slashQuery($("#prompt").value);
      if (q !== null) { e.preventDefault(); acceptSlash(state.slashSel); return; }
    }
    if (e.key === "Escape") { e.preventDefault(); hideSlash(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); return; }
  if (e.key === "Escape") { call("interrupt"); }
}

/* ------------------------------------------------------------------ wiring */
function fillSelect(sel, items, value) {
  sel.innerHTML = "";
  items.forEach((pair) => { const o = el("option", null, pair[1] || pair[0]); o.value = pair[0]; sel.appendChild(o); });
  if (value) sel.value = value;
}

async function boot() {
  const d = await call("defaults");
  if (!d) { $("#brandSub").textContent = "no backend"; return; }
  fillSelect($("#modelSel"), d.models.map((m) => [m, m === "default" ? "Model: auto" : "Model: " + m]), d.model);
  fillSelect($("#modeSel"), d.modes, d.mode);
  fillSelect($("#effortSel"), d.efforts.map((e) => [e, "Effort: " + e]), d.effort);
  const homeInfo = await call("home");
  // Restore the previous project + conversation so history survives a relaunch.
  state.cwd = d.lastCwd || state.cwd || d.cwd || (homeInfo && homeInfo.home) || "";
  $("#cwdInput").value = state.cwd;
  if (d.lastSession) {
    state.sessionId = d.lastSession;
    call("history", d.lastSession, state.cwd);
  }

  // discovery is fully async: these queue background work, answers arrive as events
  call("auth_status", false);
  loadSessions(false);
  refreshGit(false);
  call("commands", state.cwd || null);
  setInterval(pump, 120);

  if (!state.started) {
    const n = el("div", "notice");
    n.textContent = "Pick a working directory, then press \u21B5 in the box below to start. "
      + "Sign in first with: claude auth login";
    wrap().appendChild(n);
  }

  // verify the chat box is on screen; re-check on resize and after fonts settle
  layoutCheck(true);
  setTimeout(() => layoutCheck(true), 400);
  setTimeout(() => layoutCheck(true), 1500);
  window.addEventListener("resize", () => layoutCheck(false));
}

async function pump() {
  const a = api();
  if (!a) return;
  if (++state.tick % 40 === 0) layoutCheck(false);   // periodic self-check
  let batch;
  try {
    batch = JSON.parse(await a.poll());
    state.pollFails = 0;
  } catch (e) {
    if (++state.pollFails > 6) $("#brandSub").textContent = "backend unreachable";
    return;
  }
  for (const ev of batch) { try { handle(ev); } catch (e) { console.warn("handle", ev.kind, e); } }
}

$("#newSession").onclick = () => startSession($("#cwdInput").value.trim(), null, "New session");
$("#pickFolder").onclick = async () => {
  const btn = $("#pickFolder");
  btn.textContent = "\u2026";
  const d = await call("pick_folder");
  btn.textContent = "\u22EF";
  if (d && d.path) {
    $("#cwdInput").value = d.path;
    state.cwd = d.path;
    loadSessions(true); refreshGit(true); call("commands", state.cwd || null);
    return;
  }
  const why = d && d.source && String(d.source).startsWith("error") ? String(d.source).slice(7) : "";
  toast(why ? "Folder picker failed \u2014 " + why + ". Type the path in the box instead."
            : "No folder chosen. You can also type a path directly.");
};
$("#refreshSessions").onclick = () => loadSessions(true);
$("#refreshGit").onclick = () => refreshGit(true);
$("#sendBtn").onclick = send;
$("#stopBtn").onclick = () => call("interrupt");
$("#modelSel").onchange = (e) => call("set_model", e.target.value);
$("#modeSel").onchange = (e) => call("set_mode", e.target.value);
$("#effortSel").onchange = () => startSession($("#cwdInput").value.trim(), null, "New session \u00B7 effort " + $("#effortSel").value);
$("#prompt").addEventListener("keydown", onPromptKey);
$("#prompt").addEventListener("input", () => {
  const q = slashQuery($("#prompt").value);
  if (q !== null) { state.slashSel = 0; renderSlash(q); } else hideSlash();
});
$("#quickRow").addEventListener("click", (e) => {
  const q = e.target.dataset && e.target.dataset.q;
  if (!q) return;
  $("#prompt").value = q;
  $("#prompt").focus();
});
$("#cwdInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#cwdInput").dispatchEvent(new Event("change")); }
});
$("#cwdInput").addEventListener("change", () => {
  const v = $("#cwdInput").value.trim();
  if (!v) return;
  state.cwd = v; loadSessions(true); refreshGit(true); call("commands", state.cwd || null);
});

/* Attach handlers are wired at load: they must work before boot() completes,
   and boot() returns early when the backend is not ready yet. */
wireAttachments();
if (window.pywebview) boot();
else window.addEventListener("pywebviewready", boot);
