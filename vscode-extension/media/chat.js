/* SWE-Xplorer sidebar chat (webview). Renders state pushed by the extension host; sends intents back. */
"use strict";

const vscode = acquireVsCodeApi();
const S = { st: null, trace: [], phase: null, status: null, sending: false, open: new Set(["live"]), editing: null };
const saved = vscode.getState() || {};

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TYPE = { search: "search", read: "read", edit: "edit", test: "test", submit: "submit" };

function mdLite(text) {
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
  return String(text || "").split("```").map((b, i) => (i % 2
    ? `<pre>${esc(b.replace(/^[\w-]*\n/, ""))}</pre>`
    : b.split(/\n{2,}/).map((p) => (p.trim() ? `<p>${inline(p.trim())}</p>` : "")).join(""))).join("");
}
const fmt = (v) => (v == null || Number.isNaN(+v) ? "–" : (+v).toFixed(2));

// ==================================================================================================================== layout
document.getElementById("app").innerHTML = `
  <div id="banner" class="banner hidden"></div>
  <div id="other" class="note hidden"></div>
  <div id="log" class="log"></div>
  <div class="composer">
    <div id="ctx" class="ctx hidden"></div>
    <textarea id="input" rows="1" placeholder="Describe a bug to fix or a change to make…"></textarea>
    <div class="bar">
      <select id="model" title="Model (providers whose API key is set)"></select>
      <button id="auto" class="autobtn" aria-pressed="false">⚡ Auto</button>
      <span class="sp"></span>
      <button id="send" class="send" title="Send (Enter)"><span class="codicon">➤</span></button>
    </div>
  </div>
  <div id="toast" class="toast hidden"></div>`;

const input = $("#input");
input.value = saved.draft || "";
autosize();

input.addEventListener("input", () => { autosize(); vscode.setState({ ...vscode.getState(), draft: input.value }); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  if (e.key === "Escape" && S.editing) { e.preventDefault(); cancelEdit(); }
});
$("#send").addEventListener("click", () => (running() ? vscode.postMessage({ type: "stop" }) : send()));
$("#model").addEventListener("change", (e) => {
  const [provider, ...rest] = e.target.value.split("|");
  if (provider === "__key") { vscode.postMessage({ type: "setApiKey" }); renderComposer(); return; }
  vscode.postMessage({ type: "setModel", provider, model: rest.join("|") });
});
$("#auto").addEventListener("click", () => {
  const on = !(S.st && S.st.autoAccept);
  if (S.st) S.st.autoAccept = on; // optimistic; the extension confirms with the shared preference
  renderComposer();
  vscode.postMessage({ type: "setAutoAccept", value: on });
});

document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-act]");
  if (!a) return;
  const n = a.dataset.n ? +a.dataset.n : undefined;
  switch (a.dataset.act) {
    case "keep": vscode.postMessage({ type: "decide", n, decision: "accept" }); break;
    case "reject": vscode.postMessage({ type: "decide", n, decision: "reject" }); break;
    case "review": vscode.postMessage({ type: "review", n }); break;
    case "diff": vscode.postMessage({ type: "openDiff", n, file: a.dataset.file }); break;
    case "tree": vscode.postMessage({ type: "openTree" }); break;
    case "key": vscode.postMessage({ type: "setApiKey" }); break;
    case "restartBackend": vscode.postMessage({ type: "restartBackend" }); break;
    case "log": vscode.postMessage({ type: "showLog" }); break;
    case "history": vscode.postMessage({ type: "history" }); break;
    case "open": vscode.postMessage({ type: "openSession", dir: a.dataset.dir }); break;
    case "clearctx": vscode.postMessage({ type: "clearContext" }); break;
    case "suggest": input.value = a.dataset.text; autosize(); input.focus(); break;
    case "expand": a.classList.toggle("clamp"); break;
    case "edit": startEdit(n); break;
    case "canceledit": cancelEdit(); break;
    case "retry": if (!S.sending && !running()) { S.sending = true; renderComposer(); vscode.postMessage({ type: "retry", n }); } break;
  }
});
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.tagName === "DETAILS" && d.dataset.key) (d.open ? S.open.add(d.dataset.key) : S.open.delete(d.dataset.key));
}, true);

function autosize() { input.style.height = "auto"; input.style.height = Math.min(220, input.scrollHeight) + "px"; }
function running() { return S.status && S.status.state === "running"; }

function send() {
  const text = input.value.trim();
  if (!text || S.sending || running()) return;
  S.sending = true;
  renderComposer();
  if (S.editing) {
    // an edited request keeps the code it was asked about
    vscode.postMessage({ type: "send", text: text + S.editing.suffix, attach: false, replaceLast: true });
  } else {
    vscode.postMessage({ type: "send", text, attach: !!(S.st && S.st.context) });
  }
}

function latestTurn() { const t = (S.st && S.st.session && S.st.session.turns) || []; return t[t.length - 1]; }

function startEdit(n) {
  const t = latestTurn();
  if (!t || t.n !== n || running()) return;
  const m = CTX_RE.exec(t.user || "");
  S.editing = { n, suffix: m ? m[0] : "", draft: input.value };
  input.value = stripContext(t.user);
  autosize(); renderComposer();
  input.focus(); input.setSelectionRange(input.value.length, input.value.length);
}

function cancelEdit() {
  if (!S.editing) return;
  input.value = S.editing.draft || "";
  S.editing = null;
  autosize(); renderComposer();
}

// ==================================================================================================================== messages
window.addEventListener("message", (e) => {
  const m = e.data;
  switch (m.type) {
    case "state": S.st = m; S.trace = m.trace || []; S.phase = m.phase; S.status = m.status; render(); break;
    case "trace": S.trace = m.trace || []; renderLog(); break;
    case "phase": S.phase = m.phase; updateLive(); break;
    case "status": S.status = m.status; renderComposer(); break;
    case "sent":
      S.sending = false;
      if (m.ok && !m.keepInput) { input.value = ""; autosize(); vscode.setState({ ...vscode.getState(), draft: "" }); }
      if (m.ok) { S.editing = null; S.open.add("live"); }
      renderComposer();
      break;
    case "error": toast(m.message); S.sending = false; renderComposer(); break;
    case "focusInput": input.focus(); break;
  }
});

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.add("hidden"), 6000);
}

// ==================================================================================================================== render
function render() { renderBanner(); renderLog(); renderComposer(); }

function renderBanner() {
  const st = S.st, b = $("#banner");
  if (!st) return;
  if (st.server === "starting" || (st.server === "stopped" && !st.session)) {
    b.className = "banner"; b.innerHTML = '<span class="spin"></span> Starting the SWE-Xplorer backend…';
  } else if (st.server === "failed") {
    b.className = "banner err";
    b.innerHTML = `<div>${esc(st.serverError || "The backend could not be started.")}</div>
      <div class="row"><button data-act="restartBackend">Restart backend</button><button class="ghost" data-act="log">Show log</button></div>`;
  } else b.className = "banner hidden";
  const o = $("#other");
  o.className = "note hidden";
}

function renderLog() {
  const log = $("#log");
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const st = S.st;
  const foreign = st && st.otherRepo && !running();
  const turns = (!foreign && st && st.session && st.session.turns) || [];
  if (!turns.length) { log.innerHTML = welcomeHtml(foreign ? st.sessionRepo : null); return; }
  log.innerHTML = turns.map((t, i) => turnHtml(t, i === turns.length - 1)).join("");
  for (const d of log.querySelectorAll("details[data-key]")) d.open = S.open.has(d.dataset.key);
  if (nearBottom || running()) log.scrollTop = log.scrollHeight;
}

function welcomeHtml(previousRepo) {
  const st = S.st || {};
  const ws = st.workspace;
  const noKey = st.server === "ready" && !(st.models || []).length;
  return `<div class="welcome">
    <img class="logo" src="${esc(document.body.dataset.logo || "")}" alt="">
    <div class="wordmark big"><b>SWE-X</b>plorer</div>
    <p>${ws ? `Ask for a fix or a feature in <b>${esc(ws)}</b>.` : "Open a folder (a git repository) to start."} It explores several solution paths, reconciles and prunes them, and picks the winning patch with a tournament. You review every change before keeping it.</p>
    ${noKey ? `<button data-act="key">Set an API key to start</button>` : ""}
    <div class="tips">
      <div><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>X</kbd> on a selection asks about that code (with its errors).</div>
    </div>
    ${recentHtml(st.recent || [], previousRepo)}
    ${ws ? `<div class="suggest">
      <button class="chip" data-act="suggest" data-text="Find and fix the failing tests in this repository.">Fix the failing tests</button>
      <button class="chip" data-act="suggest" data-text="Add type hints and docstrings to the public functions in ">Add type hints to…</button>
    </div>` : ""}
  </div>`;
}

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - (ts || 0));
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function recentHtml(list) {
  if (!list.length) return "";
  return `<div class="recent">
    <div class="rh"><span>Recent chats</span><a href="#" data-act="history">All chats…</a></div>
    ${list.map((x) => `<a href="#" class="ri" data-act="open" data-dir="${esc(x.dir)}" title="${esc(x.title)}">
      <span class="rt">${esc(x.title)}</span>
      <span class="rm">${esc(x.repo)} · ${x.turns} request${x.turns === 1 ? "" : "s"} · ${ago(x.updated)}</span></a>`).join("")}
  </div>`;
}

function backtrackText(e) {
  const after = e.from_step === 0 ? "from the start" : e.from_step ? `after step ${e.from_step}` : "";
  if (e.alt_of) return `tries an <b>alternative to step ${e.alt_of}</b>${after ? ` (another way to continue ${after})` : ""}`;
  return after ? `continues on another branch <b>${after}</b>` : "switches to a better-scoring branch";
}

/** The agent's observation (`<returncode>…</returncode><output>…</output>`, or the head/tail form for long
 *  outputs) as {rc, text, note}; anything else (errors, timeouts) is shown as it is. */
function parseObs(obs) {
  const s = String(obs || "");
  const rcm = /<returncode>\s*(-?\d+)\s*<\/returncode>/.exec(s);
  const rc = rcm ? Number(rcm[1]) : null;
  const tag = (name) => { const m = new RegExp(`<${name}>\\n?([\\s\\S]*?)(?:\\n?</${name}>|$)`).exec(s); return m ? m[1] : null; };
  const out = tag("output");
  if (out !== null) return { rc, text: out, note: "" };
  const head = tag("output_head"), tail = tag("output_tail");
  if (head !== null || tail !== null) {
    const el = /<elided_chars>\s*(\d+)/.exec(s);
    return { rc, text: `${head || ""}\n… ${el ? el[1] : "some"} characters elided …\n${tail || ""}`, note: "long output: the agent saw its start and end" };
  }
  return { rc, text: rcm ? s.replace(rcm[0], "").trim() : s, note: "" };
}

function obsHtml(obs, key) {
  const o = parseObs(obs);
  const bad = o.rc !== null && o.rc !== 0;
  const text = o.text.trim() ? o.text : "(no output)";
  return `<details data-key="${key}"><summary${bad ? ' class="bad"' : ""}>output${bad ? ` · exit ${o.rc}` : ""}</summary>
    ${o.note ? `<div class="obsnote">${esc(o.note)}</div>` : ""}<pre class="obs${bad ? " bad" : ""}${o.text.trim() ? "" : " empty"}">${esc(text)}</pre></details>`;
}

function traceHtml(trace, n) {
  let html = "", alt = null;
  trace.forEach((e, i) => {
    if (e.kind === "step") {
      const a = alt; alt = null;
      const invalid = !e.command;
      const ty = invalid ? "bad" : TYPE[e.type] || "other";
      html += `<div class="step ${ty}">
        <div class="sh"><span class="badge ${ty}">${invalid ? "NO ACTION" : esc((e.type || "?").toUpperCase())}</span>
          <span class="muted">step ${e.n ?? "–"}${e.itr ? ` · itr ${e.itr}` : ""}</span>${a ? `<span class="alt">↳ alt. to step ${a}</span>` : ""}
          <span class="sp"></span><span class="muted" title="node value">${fmt(e.v)}</span></div>
        ${e.thought ? `<div class="thought clamp" data-act="expand" title="Click to expand">${esc(e.thought)}</div>` : ""}
        ${e.command ? `<pre class="cmd">${esc(e.command)}</pre>` : ""}
        ${e.obs ? obsHtml(e.obs, `o${n}-${i}`) : ""}
      </div>`;
    } else if (e.kind === "backtrack") {
      alt = e.alt_of || null;
      html += `<div class="mark bt"><span class="bi">↩</span> <b>Backtrack</b> · ${backtrackText(e)}</div>`;
    } else if (e.kind === "phase") {
      html += `<div class="mark ph">◆ <b>${esc(e.label)}</b> ${esc(e.detail || "")}</div>`;
    }
  });
  return html || '<div class="muted pad">Preparing the workspace and indexing the repository…</div>';
}

function decisionChip(t) {
  if (t.decision === "accepted") return '<span class="chip ok">✓ kept</span>';
  if (t.decision === "rejected") return '<span class="chip bad">✗ rejected</span>';
  return '<span class="chip warn">awaiting review</span>';
}

function turnHtml(t, latest) {
  const live = t.status === "running";
  const trace = live ? S.trace : t.trace || [];
  const steps = trace.filter((e) => e.kind === "step").length;
  const bts = trace.filter((e) => e.kind === "backtrack").length;
  const phase = S.phase || {};
  const status = live ? `<span class="chip live" id="live">${esc(phase.phase || "working")}</span>`
    : t.status === "Submitted" ? decisionChip(t) : `<span class="chip bad">${esc(t.status || "?")}</span>`;
  const key = live ? "live" : `r${t.n}`;
  let body = "";
  if (!live && t.status === "Submitted") {
    body += `<div class="resp">${mdLite(t.response || "Submitted a patch.")}</div>`;
    const files = t.files || [];
    body += `<div class="changes"><div class="ch-head">${files.length} file${files.length === 1 ? "" : "s"} changed
        ${files.length ? `<a href="#" data-act="review" data-n="${t.n}">Review all</a>` : ""}</div>
      ${files.map((f) => `<a href="#" class="file" data-act="diff" data-n="${t.n}" data-file="${esc(f)}" title="Open the diff"><span class="fi">±</span>${esc(f)}</a>`).join("")}
    </div>`;
    if (latest && !t.decision) {
      body += `<div class="review">
        <button data-act="keep" data-n="${t.n}">✓ Keep</button>
        <button class="ghost" data-act="reject" data-n="${t.n}">✗ Reject</button>
        <button class="ghost" data-act="review" data-n="${t.n}">Review</button></div>`;
    }
  } else if (!live) {
    body += `<div class="resp err">${esc(t.error || t.status || "")}</div>`;
  }
  const meta = [
    t.mode === "continue" ? `<span class="chip" title="Continued from the last kept submission in the same tree">↳ continued</span>` : "",
    !live && t.cost != null && +t.cost > 0 ? `<span class="chip">$${(+t.cost).toFixed(3)}</span>` : "",
  ].join("");
  const pending = t.status === "Submitted" && !t.decision;
  const acts = latest && !live && !running() ? `<div class="uact">
      <a href="#" data-act="edit" data-n="${t.n}" title="Edit this request and run it again${pending ? " (rejects the current result)" : ""}">✎ Edit</a>
      <a href="#" data-act="retry" data-n="${t.n}" title="Run this request again${pending ? " (rejects the current result)" : ""}">↻ Retry</a></div>` : "";
  return `<div class="msg user${S.editing && S.editing.n === t.n ? " editing" : ""}">${mdLite(stripContext(t.user))}${contextBadge(t.user)}</div>${acts}
    <div class="msg agent">
      <div class="ah"><span class="wordmark"><b>SWE-X</b>plorer</span>${status}${meta}<span class="sp"></span>
        <a href="#" class="muted" data-act="tree" title="Open the search tree">tree ↗</a></div>
      <details class="reasoning" data-key="${key}">
        <summary>${live ? '<span class="spin"></span>' : ""}Reasoning · ${steps} step${steps === 1 ? "" : "s"}${bts ? ` · ${bts} backtrack${bts === 1 ? "" : "s"}` : ""}${t.iterations ? ` · ${t.iterations} iteration${t.iterations === 1 ? "" : "s"}` : ""}</summary>
        <div class="trace">${traceHtml(trace, t.n)}</div>
      </details>
      ${body}
    </div>`;
}

const CTX_RE = /\n\n<code_context file="([^"]*)" lines="([^"]*)">[\s\S]*$/;
function stripContext(s) { return String(s || "").replace(CTX_RE, ""); }
function contextBadge(s) {
  const m = CTX_RE.exec(String(s || ""));
  return m ? `<div class="ctxbadge">📎 ${esc(m[1])}:${esc(m[2])}</div>` : "";
}

function updateLive() {
  const chip = document.getElementById("live");
  if (chip && S.phase) { chip.textContent = S.phase.phase || "working"; chip.title = S.phase.detail || ""; }
}

function renderComposer() {
  const st = S.st || {};
  const sel = $("#model");
  const models = st.models || [];
  const cur = st.model ? `${st.model.provider}|${st.model.model}` : "";
  let opts = models.map((p) => `<optgroup label="${esc(p.label)}">${p.models.map((m) => `<option value="${esc(p.id + "|" + m)}">${esc(m)}</option>`).join("")}</optgroup>`).join("");
  if (st.model && !models.some((p) => p.id === st.model.provider && p.models.includes(st.model.model))) {
    opts = `<option value="${esc(cur)}">${esc(st.model.model)}</option>` + opts;
  }
  opts += `<option value="__key|">＋ Set an API key…</option>`;
  if (sel.innerHTML !== opts) sel.innerHTML = opts;
  sel.value = cur || "__key|";
  sel.disabled = running();
  const auto = $("#auto");
  auto.classList.toggle("on", !!st.autoAccept);
  auto.setAttribute("aria-pressed", String(!!st.autoAccept));
  auto.title = st.autoAccept ? "Auto-accept is on: every result is kept without review. Click to turn off."
    : "Auto-accept is off: results wait for your review. Click to keep every result automatically.";

  const ctx = $("#ctx");
  if (S.editing) {
    ctx.className = "ctx";
    ctx.innerHTML = `<span>✎ Editing request ${S.editing.n}</span><button class="x" data-act="canceledit" title="Cancel (Esc)">×</button>`;
  } else if (st.context) {
    const c = st.context;
    ctx.className = "ctx";
    ctx.innerHTML = `<span>📎 ${esc(c.file)}:${c.start}${c.end !== c.start ? "-" + c.end : ""}${c.diagnostics && c.diagnostics.length ? ` · ${c.diagnostics.length} problem${c.diagnostics.length === 1 ? "" : "s"}` : ""}</span>
      <button class="x" data-act="clearctx" title="Remove">×</button>`;
  } else ctx.className = "ctx hidden";

  const btn = $("#send");
  const r = running();
  btn.classList.toggle("stop", r);
  btn.title = r ? "Stop the run" : "Send (Enter)";
  btn.innerHTML = r ? "■" : S.sending ? '<span class="spin"></span>' : "➤";
  input.disabled = r || st.server === "failed";
  input.placeholder = r ? "SWE-Xplorer is working…" : (!st.otherRepo && st.session && st.session.turns && st.session.turns.length)
    ? "Ask a follow-up (continues the same search tree)…" : "Describe a bug to fix or a change to make…";
}

vscode.postMessage({ type: "ready" });
