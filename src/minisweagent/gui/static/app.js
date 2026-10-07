/* SWE-Xplorer demo — vanilla JS front-end (no external dependencies, works offline). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) el.append(c.nodeType ? c : document.createTextNode(c));
  return el;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v, d = 2) => (v == null ? "–" : v < 0 ? "−∞" : Number(v).toFixed(d));

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.status = r.status; e.body = j; throw e; }
  return j;
}
let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3000);
}

// ============================================================================ state
const S = {
  info: null,
  cfg: { path: "", name: "", data: null },
  cfgDirty: false,
  snapshots: [], lastSeq: 0,
  live: true, viewIdx: -1,
  selected: null, nodeCache: new Map(),
  colorMode: "score", showPruned: true, follow: true,
  status: { state: "idle" }, paused: false, result: null,
  view: { x: 40, y: 40, z: 1 },
  pos: new Map(), drawnPos: new Map(), anim: null,
  lastCurrent: null,
  logs: [],
};

const TYPE_COLORS = { search: "#60a5fa", read: "#a78bfa", edit: "#fb923c", test: "#22d3ee", submit: "#facc15", root: "#94a3b8" };
const TYPE_GLYPH = { search: "S", read: "R", edit: "E", test: "T", submit: "✓", root: "◆" };
const ITR_COLORS = ["#94a3b8", "#60a5fa", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#f87171", "#22d3ee"];

function lerp(a, b, t) { return a + (b - a) * t; }
function mix(c1, c2, t) {
  const p = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const a = p(c1), b = p(c2);
  return "#" + a.map((x, i) => Math.round(lerp(x, b[i], t)).toString(16).padStart(2, "0")).join("");
}
function scoreColor(s) {
  if (s == null) return "#475569";
  if (s < 0) return "#7f1d1d";
  s = Math.max(0, Math.min(1, s));
  return s < 0.5 ? mix("#ef4444", "#f59e0b", s / 0.5) : mix("#f59e0b", "#22c55e", (s - 0.5) / 0.5);
}

// ============================================================================ tabs
function initTabs() {
  $$("nav.tabs").forEach((nav) => {
    const scope = nav.parentElement;
    nav.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tab]"); if (!b) return;
      $$("button", nav).forEach((x) => x.classList.toggle("active", x === b));
      $$(":scope > .tab-body", scope).forEach((x) => x.classList.toggle("active", x.dataset.body === b.dataset.tab));
      if (b.dataset.tab === "yaml") refreshYaml();
    });
  });
}
function showTab(scopeSel, name) { $(`${scopeSel} nav.tabs button[data-tab="${name}"]`)?.click(); }

// ============================================================================ config forms (driven by the YAML)
const RUN_FIELDS_IGNORED = new Set(["environment_class", "image", "entrypoint", "executable", "container_timeout", "pull_timeout", "run_args", "forward_env", "cwd"]);

function markDirty() { S.cfgDirty = true; $("#cfg-dirty").classList.remove("hidden"); saveLocalDraft(); }

function parseLoose(text, like) {
  if (typeof like === "number") { const n = Number(text); return Number.isFinite(n) ? n : like; }
  if (typeof like === "string") return text;
  const t = text.trim();
  if (t === "" || t === "null" || t === "~") return null;
  if (t === "true" || t === "false") return t === "true";
  try { return JSON.parse(t); } catch { return text; }
}

function isTemplate(key, val) { return typeof val === "string" && (val.includes("\n") || /_template$/.test(key) || val.length > 90); }

/** Render an editable form for `obj`. Unknown keys work too: every YAML key gets a widget based on its value type. */
function renderForm(el, obj, opts = {}) {
  el.innerHTML = "";
  const schema = opts.schema || {};
  const keys = [...new Set([...Object.keys(obj), ...Object.keys(schema)])];
  const groups = { scalar: [], template: [], nested: [] };
  for (const k of keys) {
    const inObj = Object.prototype.hasOwnProperty.call(obj, k);
    const val = inObj ? obj[k] : schema[k]?.default;
    if (!inObj && schema[k]?.required) continue;
    if (!inObj && isTemplate(k, val ?? "") && !val) continue; // empty default templates add noise
    const kind = val !== null && typeof val === "object" ? "nested" : isTemplate(k, val) ? "template" : "scalar";
    groups[kind].push([k, val, inObj]);
  }
  const titles = opts.titles || { scalar: "Parameters", template: "Prompt templates", nested: "Nested" };
  for (const kind of ["scalar", "nested", "template"]) {
    if (!groups[kind].length) continue;
    if (!opts.flat) el.append(h("div", { class: "group-title", text: titles[kind] }));
    for (const [k, val, inObj] of groups[kind]) el.append(fieldRow(obj, k, val, inObj, opts));
  }
  el.append(addFieldRow(obj, () => renderForm(el, obj, opts)));
}

function fieldRow(obj, k, val, inObj, opts) {
  const schema = opts.schema || {};
  const known = !opts.schema || k in schema;
  const tags = [];
  if (!inObj) tags.push(h("span", { class: "tag", text: "default" }));
  if (!known && opts.markUnused) tags.push(h("span", { class: "tag warn", title: "Not a field of the agent config class, so it is ignored", text: "unused" }));
  if (opts.ignored?.has(k)) tags.push(h("span", { class: "tag warn", title: "Replaced for local runs", text: "local override" }));
  const set = (v) => { obj[k] = v; row.classList.remove("is-default"); markDirty(); opts.onChange?.(k, v); };
  const del = h("button", { class: "x", title: inObj ? "Remove key (falls back to default)" : "", text: inObj ? "×" : "",
    onclick: () => { if (!inObj) return; delete obj[k]; markDirty(); opts.rerender?.(); } });
  const label = h("div", { class: "k", title: schema[k]?.doc || k }, k, " ", ...tags);
  let row;

  if (val !== null && typeof val === "object" && !Array.isArray(val)) {
    // nested mapping → recursive sub-form (e.g. model_kwargs, env)
    if (!inObj) obj[k] = val = {};
    const sub = h("div", { class: "subform" });
    row = h("div", {}, h("div", { class: "subform-title" }, k, " ", ...tags), sub);
    renderForm(sub, val, { flat: true, rerender: () => renderForm(sub, val, { flat: true }) });
    return row;
  }
  if (Array.isArray(val) || (typeof val === "string" && isTemplate(k, val))) {
    const isArr = Array.isArray(val);
    const ta = h("textarea", { spellcheck: "false", rows: isArr ? 3 : 14 });
    ta.value = isArr ? JSON.stringify(val, null, 1) : val;
    ta.addEventListener("change", () => set(isArr ? parseLoose(ta.value, null) : ta.value));
    const lines = isArr ? `${val.length} items` : `${val.split("\n").length} lines`;
    const d = h("details", { class: "tmpl" }, h("summary", {}, k, " ", h("small", { class: "muted", text: lines }), " ", ...tags), ta);
    row = h("div", { class: "frow wide" + (inObj ? "" : " is-default") }, h("div", { class: "v" }, d), del);
    return row;
  }
  let input;
  if (typeof val === "boolean") {
    input = h("input", { type: "checkbox" }); input.checked = val;
    input.addEventListener("change", () => set(input.checked));
  } else if (opts.choices?.[k]) {
    const choices = [...new Set([...(opts.choices[k]), val ?? ""])];
    input = h("select", {}, choices.map((c) => h("option", { value: c, text: c === "" ? "(auto: litellm)" : c })));
    input.value = val ?? "";
    input.addEventListener("change", () => set(input.value === "" ? undefined : input.value));
  } else {
    input = h("input", { type: typeof val === "number" ? "number" : "text", step: "any", spellcheck: "false", placeholder: val === null ? "null" : "" });
    input.value = val ?? "";
    if (opts.datalist?.[k]) input.setAttribute("list", opts.datalist[k]);
    input.addEventListener("change", () => set(parseLoose(input.value, val)));
  }
  row = h("div", { class: "frow" + (inObj ? "" : " is-default") + (!known && opts.markUnused ? " unused" : "") }, label, input, del);
  return row;
}

function addFieldRow(obj, rerender) {
  const kIn = h("input", { placeholder: "new key", spellcheck: "false" });
  const vIn = h("input", { placeholder: "value (text, number, true, {json})", spellcheck: "false" });
  const add = h("button", { text: "+ Add", onclick: () => {
    const k = kIn.value.trim(); if (!k) return;
    obj[k] = parseLoose(vIn.value, null) ?? ""; markDirty(); rerender();
  } });
  return h("div", { class: "addrow" }, kIn, vIn, add);
}

function renderAllForms() {
  const d = S.cfg.data;
  if (!d) return;
  d.agent ??= {}; d.environment ??= {}; d.model ??= {}; d.reward_model ??= {};
  const agentOpts = { schema: S.info.schema, markUnused: true, titles: { scalar: "Search parameters", template: "Prompt templates", nested: "Nested" } };
  agentOpts.rerender = () => renderForm($("#form-agent"), d.agent, agentOpts);
  agentOpts.rerender();
  const envOpts = { ignored: RUN_FIELDS_IGNORED };
  envOpts.rerender = () => renderForm($("#form-env"), d.environment, envOpts);
  envOpts.rerender();
  d.reproduction ??= { enabled: false };
  const reOpts = { titles: { scalar: "Switch", template: "Prompt templates", nested: "Reproducer agent" } };
  reOpts.rerender = () => renderForm($("#form-repro"), d.reproduction, reOpts);
  reOpts.rerender();
  const mOpts = { choices: { model_class: S.info.model_classes }, datalist: { model_name: "model-names" }, flat: true };
  mOpts.rerender = () => renderForm($("#form-model"), d.model, mOpts);
  mOpts.rerender();
  const rOpts = { ...mOpts }; rOpts.rerender = () => renderForm($("#form-reward"), d.reward_model, rOpts);
  rOpts.rerender();
  syncRewardSame();
  $("#cfg-name").textContent = S.cfg.name || "(unsaved)";
  $("#cfg-dirty").classList.toggle("hidden", !S.cfgDirty);
}
function syncRewardSame() {
  const same = $("#reward-same").checked;
  $("#form-reward").style.opacity = same ? 0.35 : 1;
  $("#form-reward").style.pointerEvents = same ? "none" : "";
}


function showDraftNotice(draft) {
  const t = $("#toast");
  t.className = "toast";
  t.innerHTML = "";
  t.append(`Restored your unsaved edits of ${draft.name || "the config"} (model: ${draft.data?.model?.model_name || "?"}). `,
    h("a", { href: "#", text: "Reset to the file", onclick: async (e) => { e.preventDefault(); t.classList.add("hidden"); await loadConfig(draft.path); } }),
    "  ", h("a", { href: "#", text: "Keep", onclick: (e) => { e.preventDefault(); t.classList.add("hidden"); } }));
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 12000);
}

async function loadConfig(path) {
  const c = await api(`/api/config?path=${encodeURIComponent(path)}`);
  S.cfg = { path: c.path, name: c.name, data: c.data };
  S.cfgDirty = false;
  $("#cfg-path").value = c.path;
  renderAllForms();
  saveLocalDraft();
  toast(`Loaded ${c.name}`);
}
async function refreshYaml() {
  if (!S.cfg.data) return;
  const r = await api("/api/config/dump", { data: S.cfg.data });
  $("#yaml-text").value = r.text;
}
// Everything is kept by the local server in its app folder (settings.json, keys.env, config_draft.json);
// nothing is stored in the browser.
let draftTimer = null;
function saveLocalDraft() {  // debounced: the config forms call this on every edit
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => api("/api/draft", {
    draft: { path: S.cfg.path, name: S.cfg.name, data: S.cfg.data, dirty: S.cfgDirty },
  }).catch(() => {}), 400);
}
function pref(key, dflt) { const v = S.info?.prefs?.[key]; return v === undefined ? dflt : v; }
function setPref(key, value) {
  if (S.info) (S.info.prefs ??= {})[key] = value;
  api("/api/prefs", { [key]: value }).catch(() => {});
}

// ============================================================================ keys
function renderKeys(keys) {
  const el = $("#keys-list"); el.innerHTML = "";
  for (const k of keys) {
    const hint = k.set ? (k.secret ? `${k.stored ? "saved on this computer" : "set for this session"} (${k.preview})` : k.preview) : "not set";
    const inp = h("input", { type: k.secret ? "password" : "text", "data-key": k.name, spellcheck: "false", autocomplete: "off", placeholder: hint });
    const eye = k.secret ? h("button", { text: "👁", title: "show what you type", onclick: () => { inp.type = inp.type === "password" ? "text" : "password"; } }) : null;
    const clear = k.set || k.stored ? h("button", { text: "Clear", title: "Unset and forget this variable", onclick: () => saveKeys({ [k.name]: "" }) }) : null;
    el.append(h("div", { class: "keyrow" },
      h("div", { class: "kname" }, k.name, k.set ? h("span", { class: "ok", text: "● set" }) : h("span", { class: "muted", text: "○" }), h("small", { class: "muted", text: k.label })),
      h("div", { class: "row" }, inp, eye, clear)));
  }
}
function pendingKeys() {
  const keys = {};
  $$("#keys-list input[data-key]").forEach((i) => { if (i.value) keys[i.dataset.key] = i.value; });
  return keys;
}
async function saveKeys(extra = {}) {
  const remember = $("#keys-remember").checked;
  const r = await api("/api/keys", { keys: { ...pendingKeys(), ...extra }, remember });
  renderKeys(r.keys);
  $("#keys-msg").textContent = remember ? `Saved in ${S.info?.keys_file || "keys.env"}.` : "Set for this server session only (not saved).";
}
async function pushStoredKeys() {
  // one-time migration: keys that earlier versions kept in this browser move to the app folder
  let old = null;
  try { old = JSON.parse(localStorage.getItem("swex.keys") || "null"); } catch {}
  try { ["swex.keys", "swex.cfg", "swex.cfg.v2", "swex.autoAccept", "swex.view"].forEach((k) => localStorage.removeItem(k)); } catch {}
  if (!old || !Object.keys(old).length) return null;
  return (await api("/api/keys", { keys: old, remember: true })).keys;
}

// ============================================================================ repo picker
let modalPath = "";
let modalInfo = null;
async function openBrowse(path) {
  $("#modal").classList.remove("hidden");
  try {
    const r = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
    modalPath = r.path; modalInfo = r; $("#modal-path").value = r.path; $("#modal-info").classList.remove("modal-err");
    $("#modal-up").disabled = r.parent == null; $("#modal-up").dataset.p = r.parent ?? "";
    $("#modal-info").textContent = r.is_repo ? `git repository${r.dirty ? " (has uncommitted changes)" : ""}`
      : r.inside_repo ? `inside the git repository ${r.inside_repo}` : r.path ? "not a git repository" : "";
    $("#modal-select").disabled = !r.path;
    const list = $("#modal-list"); list.innerHTML = "";
    for (const e of r.entries) list.append(h("div", { class: e.repo ? "repo" : "", onclick: () => openBrowse(e.path) }, e.repo ? "⎇" : "📁", e.name));
    if (!r.entries.length) list.append(h("div", { class: "muted", text: "(no sub-folders)" }));
  } catch (e) { toast(e.message, true); }
}
async function checkRepo() {
  const p = $("#repo").value.trim(); const info = $("#repo-info");
  S.repo = null;
  if (!p) { info.textContent = ""; if (typeof onRepoChanged === "function") onRepoChanged(); return; }
  try {
    const py = $("#python-path").value.trim();
    const r = await api(`/api/repo?path=${encodeURIComponent(p)}${py ? `&python=${encodeURIComponent(py)}` : ""}`);
    if (r.ok) {
      S.repo = r;
      if (r.root !== p) $("#repo").value = r.root;  // a sub-folder resolves to the repository root
      info.innerHTML = `✓ ${esc(r.name)} · ${esc(r.branch)} · <span class="muted">${esc(r.head)}</span>${r.dirty ? ' · <span class="warn-text">uncommitted changes (included)</span>' : ""}`;
    } else info.innerHTML = `<span class="warn-text">${esc(r.error)} SWE-Xplorer only works on git repositories.</span>`;
  } catch { info.innerHTML = '<span class="warn-text">folder not found</span>'; }
  if (typeof onRepoChanged === "function") onRepoChanged();
}

// ============================================================================ run controls
const control = (action) => api("/api/control", { action }).catch((e) => toast(e.message, true));

function setStatus(st) {
  S.status = st;
  const running = st.state === "running";
  $("#btn-pause").disabled = !running;
  $("#btn-step").disabled = !running;
  $("#btn-stop").disabled = !running;
  $("#btn-pause").textContent = S.paused ? "▶ Resume" : "⏸ Pause";
  const pill = $("#state-pill");
  const label = running ? (S.paused ? "paused" : "running") : st.state === "finished" ? (st.exit_status || "finished") : st.state;
  pill.textContent = label;
  pill.className = "pill " + (running ? (S.paused ? "paused" : "running") : st.state === "finished" ? (st.exit_status === "Submitted" ? "finished" : "error") : "idle");
  if (typeof updateComposer === "function") updateComposer();
}

// ============================================================================ events (SSE)
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = syncHistory;
  es.onmessage = (m) => {
    const evt = JSON.parse(m.data);
    const d = evt.data;
    switch (evt.type) {
      case "snapshot": onSnapshot(d); break;
      case "log": addLog(d); break;
      case "phase": setPhase(d); break;
      case "backtrack": if (S.live) drawBacktrack(d.from, d.to); addLog({ level: "info", msg: `↩ backtrack ${d.from.slice(0, 8)} → ${d.to.slice(0, 8)}` }); break;
      case "status": setStatus(d); break;
      case "control": S.paused = !!d.paused; setStatus(S.status); break;
      case "finished": onFinished(d); break;
      case "session": onSession(d.session); break;
      case "prefs": // changed elsewhere (another tab, the VS Code sidebar)
        if (S.info) S.info.prefs = d.prefs;
        if (d.prefs && "autoAccept" in d.prefs && typeof updateComposer === "function") { $("#auto-accept").checked = !!d.prefs.autoAccept; updateComposer(); }
        break;
    }
  };
  es.onerror = () => { /* EventSource reconnects automatically */ };
}
async function syncHistory() {
  // the server keeps keys in memory only: after a server restart it has none until the browser re-sends them
  try { const k = await pushStoredKeys(); if (k) renderKeys(k); } catch {}
  try {
    const r = await api("/api/history");
    S.snapshots = r.snapshots; S.lastSeq = r.snapshots.length ? r.snapshots[r.snapshots.length - 1].seq : 0;
    S.logs = []; $("#log").textContent = ""; r.logs.forEach(addLog);
    if (r.phase) setPhase(r.phase);
    setStatus(r.status || { state: "idle" });
    if (r.result) onFinished(r.result);
    onSession(r.session);
    if (r.status?.state === "running") scheduleTrace();
    updateReplay(); if (S.snapshots.length) { render(true); fit(); }
  } catch (e) { console.warn(e); }
}
function onSnapshot(snap) {
  if (snap.seq && snap.seq <= S.lastSeq) return;
  S.lastSeq = snap.seq || S.lastSeq;
  S.snapshots.push(snap);
  if (S.snapshots.length > 4000) S.snapshots.splice(0, S.snapshots.length - 4000);
  // node details can change (observation arrives after execution) → invalidate cache for live nodes
  S.nodeCache.clear();
  updateReplay();
  if (S.live) render();
  if (S.status.state === "running") scheduleTrace();
}
function setPhase(p) { $("#phase").innerHTML = `${esc(p.phase)}<small>${esc(p.detail || "")}</small>`; if (p.stage) setStage(p.stage); if (typeof updateLiveChip === "function") updateLiveChip(p); }
function setStage(stage) { $$("#cycle span").forEach((x) => x.classList.toggle("on", x.dataset.stage === stage)); }

function addLog(l) {
  S.logs.push(l);
  if (S.logs.length > 4000) S.logs.shift();
  const f = $("#log-filter").value.toLowerCase();
  if (f && !l.msg.toLowerCase().includes(f)) return;
  appendLogLine(l);
}
function appendLogLine(l) {
  const el = $("#log");
  el.append(h("span", { class: l.level || "info", text: l.msg + "\n" }));
  while (el.childNodes.length > 3000) el.firstChild.remove();
  if ($("#log-auto").checked) el.scrollTop = el.scrollHeight;
}
function refilterLog() { $("#log").textContent = ""; const f = $("#log-filter").value.toLowerCase(); S.logs.filter((l) => !f || l.msg.toLowerCase().includes(f)).forEach(appendLogLine); }

// ============================================================================ replay
function currentSnap() { return S.live ? S.snapshots[S.snapshots.length - 1] : S.snapshots[S.viewIdx]; }
function updateReplay() {
  const r = $("#replay"); r.max = Math.max(0, S.snapshots.length - 1);
  if (S.live) r.value = r.max;
  $("#replay-label").textContent = S.live ? "live" : `${S.viewIdx + 1} / ${S.snapshots.length}`;
  $("#btn-live").classList.toggle("hidden", S.live);
}
let playTimer = null;
function play() {
  if (playTimer) { clearInterval(playTimer); playTimer = null; $("#btn-play").textContent = "⟲ Replay"; return; }
  if (!S.snapshots.length) return;
  S.live = false; S.viewIdx = S.viewIdx < 0 || S.viewIdx >= S.snapshots.length - 1 ? 0 : S.viewIdx;
  $("#btn-play").textContent = "⏸ Replay";
  playTimer = setInterval(() => {
    if (S.viewIdx >= S.snapshots.length - 1) { play(); goLive(); return; }
    S.viewIdx++; $("#replay").value = S.viewIdx; updateReplay(); render();
  }, 220);
}
function goLive() { S.live = true; S.viewIdx = -1; updateReplay(); render(); }

// ============================================================================ tree layout + rendering
const DX = 46, DY = 84, R = 13;

function layout(snap) {
  const nodes = new Map(snap.nodes.map((n) => [n.id, n]));
  const kids = new Map();
  for (const n of snap.nodes) if (n.p && nodes.has(n.p)) { if (!kids.has(n.p)) kids.set(n.p, []); kids.get(n.p).push(n); }
  for (const a of kids.values()) a.sort((x, y) => x.k - y.k);
  const pos = new Map(); let x = 0; let maxDepth = 0;
  let root = nodes.get(snap.root) || snap.nodes[0];
  // The agent's tree_root only holds the task prompt; its single child, the pseudo-root at the user's commit, is
  // where the search starts. Draw from there, so the tree does not always begin with two identical-looking roots.
  const top = root && (kids.get(root.id) || []);
  if (root && top.length === 1 && top[0].ty === "root") root = top[0];
  const visit = (n, depth) => {
    maxDepth = Math.max(maxDepth, depth);
    let ch = kids.get(n.id) || [];
    if (!S.showPruned) ch = ch.filter((c) => c.vis || c.ex);
    if (!ch.length) { pos.set(n.id, { x: x++ * DX, y: depth * DY, depth }); return; }
    for (const c of ch) visit(c, depth + 1);
    const f = pos.get(ch[0].id), l = pos.get(ch[ch.length - 1].id);
    pos.set(n.id, { x: (f.x + l.x) / 2, y: depth * DY, depth });
  };
  if (root) visit(root, 0);
  return { nodes, kids, pos, maxDepth };
}

function nodeFill(n, snap, range) {
  const m = S.colorMode;
  if (m === "type") return TYPE_COLORS[n.ty] || "#64748b";
  if (m === "itr") return ITR_COLORS[(n.itr || 0) % ITR_COLORS.length];
  if (m === "rq") return ITR_COLORS[(n.rq || 1) % ITR_COLORS.length];
  let v = n.mv ?? n.v;
  if (m === "rel" && v != null && v >= 0 && range.max > range.min) v = (v - range.min) / (range.max - range.min);
  if (n.ty === "root") return "#94a3b8";
  return scoreColor(v);
}

function render(instant = false) {
  const snap = currentSnap();
  $("#empty").classList.toggle("hidden", !!snap);
  if (!snap) { $("#g-nodes").innerHTML = $("#g-edges").innerHTML = ""; return; }
  const L = layout(snap);
  const target = L.pos;
  // animate from the currently drawn positions to the new layout
  const from = new Map(S.drawnPos);
  const start = performance.now(), dur = instant ? 0 : 380;
  cancelAnimationFrame(S.anim);
  const frame = (t) => {
    const k = dur ? Math.min(1, (t - start) / dur) : 1;
    const e = 1 - Math.pow(1 - k, 3);
    const cur = new Map();
    for (const [id, p] of target) {
      const f = from.get(id) || (L.nodes.get(id)?.p && (from.get(L.nodes.get(id).p) || target.get(L.nodes.get(id).p))) || p;
      cur.set(id, { x: lerp(f.x, p.x, e), y: lerp(f.y, p.y, e) });
    }
    draw(snap, L, cur, k === 0 || !dur);
    S.drawnPos = cur;
    if (k < 1) S.anim = requestAnimationFrame(frame);
  };
  S.anim = requestAnimationFrame(frame);
  S.pos = target;
  renderStats(snap);
  renderLegend();
  if (S.live && snap.stage) setStage(snap.stage);
  else if (!S.live) setStage(snap.stage || "");
  if (S.follow && snap.current && snap.current !== S.lastCurrent) { S.lastCurrent = snap.current; ensureVisible(snap.current); }
  if (!S.selected || !L.nodes.has(S.selected)) renderPath(snap, snap.current);
  else refreshSelected();
}

function draw(snap, L, P, first) {
  const range = { min: Infinity, max: -Infinity };
  for (const n of snap.nodes) { const v = n.mv ?? n.v; if (v != null && v >= 0 && n.ty !== "root") { range.min = Math.min(range.min, v); range.max = Math.max(range.max, v); } }
  const onPath = new Set(); let c = L.nodes.get(snap.current);
  while (c) { onPath.add(c.id); c = L.nodes.get(c.p); }
  const frontier = new Set(snap.frontier || []), evaluating = new Set(snap.evaluating || []);

  // iteration bands (depth guides)
  let bands = "";
  for (let d = 0; d <= L.maxDepth; d++) if (d % 2) bands += `<rect class="band" x="-100000" y="${d * DY - DY / 2}" width="200000" height="${DY}"/>`;
  $("#g-bands").innerHTML = bands;

  let edges = "";
  const curve = (a, b) => { const my = (a.y + b.y) / 2; return `M${a.x},${a.y + R} C${a.x},${my} ${b.x},${my} ${b.x},${b.y - R}`; };
  for (const n of snap.nodes) {
    const b = P.get(n.id); if (!b) continue;
    const a = n.p && P.get(n.p);
    if (a) {
      const cls = ["edge", n.ex ? "ex" : "", onPath.has(n.id) ? "onpath" : "", !n.vis && !n.ex ? "pruned" : ""].join(" ");
      edges += `<path class="${cls}" d="${curve(a, b)}"/>`;
    }
    for (const xp of n.xp || []) { const a2 = P.get(xp); if (a2) edges += `<path class="edge merge" d="${curve(a2, b)}"/>`; }
  }
  $("#g-edges").innerHTML = edges;

  let out = "";
  for (const n of snap.nodes) {
    const p = P.get(n.id); if (!p) continue;
    const fill = n.cmd == null && n.ty !== "root" ? "#52525b" : nodeFill(n, snap, range);
    const cls = ["node", n.ex ? "ex" : "", !n.vis && !n.ex ? "pruned" : "", first && !S.drawnPos.has(n.id) ? "new" : ""].join(" ");
    const shape = n.term || n.ty === "submit"
      ? `<rect class="shape" x="${-R + 2}" y="${-R + 2}" width="${2 * R - 4}" height="${2 * R - 4}" rx="3" transform="rotate(45)" fill="${fill}"/>`
      : n.mod ? `<rect class="shape" x="${-R + 1}" y="${-R + 1}" width="${2 * R - 2}" height="${2 * R - 2}" rx="6" fill="${fill}"/>`
      : `<circle class="shape" r="${R}" fill="${fill}"/>`;
    let rings = "";
    if (n.mg) rings += `<circle class="ring merged" r="${R + 3}"/>`;
    if (frontier.has(n.id)) rings += `<circle class="ring frontier" r="${R + 5}"/>`;
    if (evaluating.has(n.id)) rings += `<circle class="ring eval" r="${R + 5}"/>`;
    if (snap.best === n.id) rings += `<circle class="ring best" r="${R + 7}"/>`;
    if (snap.current === n.id) rings += `<circle class="ring current" r="${R + 6}"/>`;
    if (S.selected === n.id) rings += `<circle class="ring selected" r="${R + 9}"/>`;
    const bad = n.inv || n.to ? `<path class="bad" d="M${R - 4},${-R - 2} l7,7 m0,-7 l-7,7"/>` : "";
    // a response without a command (format error, or a model that did not act) is typed "read" by the agent: show it as invalid
    const invalid = n.cmd == null && n.ty !== "root";
    const glyph = invalid ? "!" : TYPE_GLYPH[n.ty] || "·";
    const val = n.ty === "root" ? "" : `<text class="val" y="${R + 13}">${fmt(n.mv ?? n.v)}</text>`;
    const ord = n.ex && n.ord ? `<text class="ord" y="${-R - 6}">#${n.ord}</text>` : "";
    const star = snap.best === n.id ? `<text class="ord" y="${-R - 16}" style="fill:#facc15;font-size:11px">★ winner</text>` : "";
    out += `<g class="${cls}" data-id="${n.id}" transform="translate(${p.x},${p.y})">${rings}${shape}<text class="glyph">${glyph}</text>${bad}${val}${ord}${star}</g>`;
  }
  $("#g-nodes").innerHTML = out;
  applyView();
}

function drawBacktrack(fromId, toId) {
  const a = S.drawnPos.get(fromId), b = S.drawnPos.get(toId);
  if (!a || !b) return;
  const mx = Math.min(a.x, b.x) - 60, my = Math.min(a.y, b.y) - 50;
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("class", "bt");
  p.setAttribute("d", `M${a.x},${a.y} Q${mx},${my} ${b.x},${b.y - R - 4}`);
  $("#g-fx").append(p);
  setTimeout(() => p.remove(), 2300);
}

// -- pan / zoom
function applyView() { $("#vp").setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.z})`); }
function fit() {
  if (!S.pos.size) return;
  const xs = [...S.pos.values()].map((p) => p.x), ys = [...S.pos.values()].map((p) => p.y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const W = $("#tree-wrap").clientWidth, H = $("#tree-wrap").clientHeight - 60;
  const z = Math.min(1.6, (W - 80) / (x1 - x0 + DX), (H - 60) / (y1 - y0 + DY));
  S.view = { z, x: W / 2 - ((x0 + x1) / 2) * z, y: 50 - y0 * z };
  applyView();
}
function ensureVisible(id) {
  const p = S.pos.get(id); if (!p) return;
  const W = $("#tree-wrap").clientWidth, H = $("#tree-wrap").clientHeight;
  const sx = p.x * S.view.z + S.view.x, sy = p.y * S.view.z + S.view.y;
  if (sx > 60 && sx < W - 60 && sy > 60 && sy < H - 90) return;
  const tx = W / 2 - p.x * S.view.z, ty = H / 2 - p.y * S.view.z;
  const fx = S.view.x, fy = S.view.y, t0 = performance.now();
  const step = (t) => { const k = Math.min(1, (t - t0) / 450), e = 1 - Math.pow(1 - k, 3);
    S.view.x = lerp(fx, tx, e); S.view.y = lerp(fy, ty, e); applyView(); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
function zoomAt(f, cx, cy) {
  const z = Math.max(0.08, Math.min(4, S.view.z * f)); f = z / S.view.z;
  S.view.x = cx - (cx - S.view.x) * f; S.view.y = cy - (cy - S.view.y) * f; S.view.z = z; applyView();
}
function initPanZoom() {
  const svg = $("#tree"); let drag = null;
  svg.addEventListener("wheel", (e) => { e.preventDefault(); const r = svg.getBoundingClientRect(); zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  svg.addEventListener("pointerdown", (e) => { if (e.target.closest(".node")) return; drag = { x: e.clientX, y: e.clientY, vx: S.view.x, vy: S.view.y }; svg.classList.add("dragging"); svg.setPointerCapture(e.pointerId); });
  svg.addEventListener("pointermove", (e) => {
    if (drag) { S.view.x = drag.vx + e.clientX - drag.x; S.view.y = drag.vy + e.clientY - drag.y; applyView(); return; }
    const g = e.target.closest(".node"); const tip = $("#tooltip");
    if (!g) { tip.classList.add("hidden"); return; }
    const n = currentSnap()?.nodes.find((x) => x.id === g.dataset.id); if (!n) return;
    const flags = [n.ex && "executed", !n.vis && "pruned", n.term && "terminating candidate", n.mg && "merged node", n.mod && "edits code", n.ch && "cache hit", n.rep && "repeat", n.to && "timeout", n.inv && "invalid submit"].filter(Boolean).join(" · ");
    tip.innerHTML = `<b>${esc(n.ty || "?")}</b> · value ${fmt(n.mv ?? n.v, 3)} · request ${n.rq ?? 1} · itr ${n.itr ?? "–"}${n.ord ? " · step #" + n.ord : ""}<br><span class="muted">${esc(flags)}</span>${n.cmd ? `<div class="cmd">${esc(n.cmd)}</div>` : ""}`;
    const r = $("#tree-wrap").getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - r.left + 14, r.width - 430) + "px"; tip.style.top = e.clientY - r.top + 14 + "px";
    tip.classList.remove("hidden");
  });
  const end = () => { drag = null; svg.classList.remove("dragging"); };
  svg.addEventListener("pointerup", end); svg.addEventListener("pointercancel", end);
  svg.addEventListener("pointerleave", () => $("#tooltip").classList.add("hidden"));
  svg.addEventListener("click", (e) => { const g = e.target.closest(".node"); if (g) selectNode(g.dataset.id); });
}

// ============================================================================ details panels
function renderStats(snap) {
  const s = snap.stats || {};
  const items = [
    ["step", s.step_limit ? `${s.steps}/${s.step_limit}` : s.steps], ["iteration", s.itr_limit ? `${s.itr}/${s.itr_limit}` : s.itr],
    ["nodes", s.nodes], ["frontier", (snap.frontier || []).length], ["terminating candidates", s.submissions], ["unique solution states", s.unique_solutions],
    ["backtracks", s.backtracks], ["re-asked replies", s.discarded || null], ["policy $", s.cost != null ? s.cost.toFixed(3) : null], ["reward $", s.reward_cost != null ? s.reward_cost.toFixed(3) : null],
    ["elapsed", s.elapsed != null ? `${Math.floor(s.elapsed / 60)}m ${Math.round(s.elapsed % 60)}s` : null], ["mode", s.mode],
  ].filter(([, v]) => v != null && v !== undefined);
  $("#stats").innerHTML = items.map(([k, v]) => `<span class="stat">${k}<b>${esc(v)}</b></span>`).join("");
  if (s.cost != null) $("#cost").textContent = `$${(s.cost + (s.reward_cost || 0)).toFixed(3)}`;
}
function renderLegend() {
  const shape = (inner) => `<svg width="18" height="18" viewBox="-9 -9 18 18">${inner}</svg>`;
  let colors = "";
  if (S.colorMode === "score" || S.colorMode === "rel") colors = `<span class="li">${shape(`<circle r="6" fill="${scoreColor(0.1)}"/>`)}low</span><span class="li">${shape(`<circle r="6" fill="${scoreColor(0.5)}"/>`)}mid</span><span class="li">${shape(`<circle r="6" fill="${scoreColor(0.95)}"/>`)}high value</span>`;
  else if (S.colorMode === "type") colors = Object.entries(TYPE_COLORS).map(([k, c]) => `<span class="li">${shape(`<circle r="6" fill="${c}"/>`)}${k}</span>`).join("");
  else if (S.colorMode === "rq") colors = ITR_COLORS.slice(1, 6).map((c, i) => `<span class="li">${shape(`<circle r="6" fill="${c}"/>`)}request ${i + 1}</span>`).join("");
  else colors = ITR_COLORS.slice(0, 6).map((c, i) => `<span class="li">${shape(`<circle r="6" fill="${c}"/>`)}itr ${i}</span>`).join("");
  $("#legend").innerHTML = colors +
    `<span class="li">${shape('<circle r="6" fill="#475569" stroke="#fff" stroke-width="2"/>')}executed</span>` +
    `<span class="li">${shape('<rect x="-5" y="-5" width="10" height="10" rx="3" fill="#475569"/>')}edits code</span>` +
    `<span class="li">${shape('<rect x="-4" y="-4" width="8" height="8" transform="rotate(45)" fill="#475569"/>')}terminating candidate</span>` +
    `<span class="li">${shape('<circle r="7" fill="none" stroke="#60a5fa" stroke-width="2.5"/>')}current</span>` +
    `<span class="li">${shape('<circle r="7" fill="none" stroke="#22d3ee" stroke-dasharray="2 2" stroke-width="1.5"/>')}frontier</span>` +
    `<span class="li">${shape('<circle r="7" fill="none" stroke="#fb923c" stroke-dasharray="3 2" stroke-width="2"/>')}scoring</span>` +
    `<span class="li">${shape('<line x1="-8" y1="0" x2="8" y2="0" stroke="#a78bfa" stroke-dasharray="3 2" stroke-width="2"/>')}reconciliation (merged node)</span>` +
    `<span class="li">${shape('<line x1="-8" y1="0" x2="8" y2="0" stroke="#fb923c" stroke-dasharray="3 2" stroke-width="2"/>')}backtrack</span>` +
    `<span class="li">${shape('<circle r="7" fill="none" stroke="#facc15" stroke-width="2.5"/>')}tournament winner</span>`;
}

async function fetchNode(id) {
  if (S.nodeCache.has(id)) return S.nodeCache.get(id);
  const d = await api(`/api/node?id=${encodeURIComponent(id)}`);
  S.nodeCache.set(id, d); return d;
}
async function selectNode(id) {
  S.selected = id; render(true);
  showTab("#right", "node");
}
async function refreshSelected() {
  const id = S.selected; const snap = currentSnap(); if (!snap) return;
  const n = snap.nodes.find((x) => x.id === id); if (!n) return;
  let d; try { d = await fetchNode(id); } catch { d = null; }
  if (S.selected !== id) return;
  const v = $("#node-view"); v.className = "pad";
  const noAction = n.cmd == null && n.ty !== "root";
  const badge = noAction ? '<span class="typebadge" style="background:#f87171">NO ACTION</span>'
    : `<span class="typebadge" style="background:${TYPE_COLORS[n.ty] || "#64748b"}">${esc((n.ty || "?").toUpperCase())}</span>`;
  const flags = [n.cmd == null && n.ty !== "root" && "invalid response: no bash command (format error); the agent was asked to retry", n.ex ? "executed" : "candidate", !n.vis && "pruned", n.term && "terminating candidate", n.sub && "submitted", n.sys && n.term && "augmented", n.mg && "merged node", n.mod && "edits code", n.ch && "cache hit", n.rep && "repeat", n.to && "timeout", n.inv && "invalid submit", snap.best === id && "★ tournament winner"].filter(Boolean);
  v.innerHTML = `
    <div class="row gap">${badge}<code>${esc(id.slice(0, 8))}</code><span class="muted">${esc(flags.join(" · "))}</span></div>
    <div class="kv">
      <span class="k">value</span><span>${fmt(d?.value ?? n.v, 4)}</span>
      <span class="k">merged value</span><span>${fmt(d?.merged_value ?? n.mv, 4)}</span>
      <span class="k">raw reward</span><span>${fmt(d?.raw_value ?? n.rv, 4)}</span>
      <span class="k">path score V<sub>path</sub></span><span>${fmt(d?.path_value, 4)}</span>
      <span class="k">depth / itr / step</span><span>${n.lvl} / ${n.itr ?? "–"} / ${n.ord || "–"}</span>
      ${d?.modified_files?.length ? `<span class="k">modified</span><span>${d.modified_files.map(esc).join("<br>")}</span>` : ""}
      ${d?.read_files?.length ? `<span class="k">read</span><span>${d.read_files.map(esc).join("<br>")}</span>` : ""}
      ${d?.commit ? `<span class="k">commit</span><span><code>${esc(d.commit.slice(0, 10))}</code></span>` : ""}
    </div>
    ${d?.command ? `<div class="block-title">Command</div><pre class="box">${esc(d.command)}</pre>` : ""}
    ${d?.thought ? `<div class="block-title">Thought</div><pre class="box">${esc(d.thought)}</pre>` : ""}
    ${d?.observation ? `<div class="block-title">Observation</div><pre class="box ${d.observation.startsWith("diff --git") ? "diff" : ""}">${d.observation.startsWith("diff --git") ? diffHtml(d.observation) : esc(d.observation)}</pre>` : ""}
    ${d?.solution_summary ? `<div class="block-title">Trajectory summary (tournament voting)</div><pre class="box">${esc(d.solution_summary)}</pre>` : ""}
    ${d?.test_status?.length ? `<div class="block-title">Test status</div><pre class="box">${esc(d.test_status.map((t) => `${t.status}  ${t.name}`).join("\n"))}</pre>` : ""}
  `;
  renderPath(snap, id);
}
function renderPath(snap, id) {
  const byId = new Map(snap.nodes.map((n) => [n.id, n]));
  const chain = []; let c = byId.get(id);
  while (c) { if (c.ty !== "root") chain.push(c); c = byId.get(c.p); }
  chain.reverse();
  const el = $("#path-view"); el.className = ""; el.innerHTML = "";
  if (!chain.length) { el.innerHTML = '<div class="muted pad">No steps yet.</div>'; return; }
  el.append(h("div", { class: "pad muted", text: `${chain.length} steps from the root to ${id === snap.current ? "the current node" : "the selected node"}` }));
  chain.forEach((n, i) => el.append(h("div", { class: "step" + (n.id === S.selected ? " sel" : ""), onclick: () => selectNode(n.id) },
    h("div", { class: "row" }, h("span", { class: "typebadge", style: `background:${TYPE_COLORS[n.ty] || "#64748b"}`, text: (n.ty || "?").toUpperCase() }),
      h("span", { class: "muted", text: `#${i + 1}` }), h("span", { class: "spacer" }), h("span", { text: `value ${fmt(n.mv ?? n.v)}` })),
    h("div", { class: "cmd", text: n.cmd || "(invalid / no command)" }))));
}
function diffHtml(p) {
  return p.split("\n").map((l) => {
    const c = l.startsWith("diff --git") || l.startsWith("+++") || l.startsWith("---") ? "file" : l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
  }).join("\n");
}
function onFinished(r) {
  S.result = r;
  const v = $("#result-view"); v.className = "pad";
  const ok = r.exit_status === "Submitted" && r.patch;
  v.innerHTML = `
    <div class="kv"><span class="k">exit status</span><span>${esc(r.exit_status)}</span>
      <span class="k">outputs</span><span><code>${esc(r.run_dir)}</code></span></div>
    ${r.message ? `<pre class="box">${esc(r.message)}</pre>` : ""}
    ${ok ? `<div class="block-title">Patch of the last request <span class="spacer"></span><button id="btn-copy">Copy</button><button id="btn-dl">Download</button>
</div>
      <pre class="box diff" style="max-height:none">${diffHtml(r.patch)}</pre>` : ""}`;
  if (ok) {
    $("#btn-copy").onclick = () => navigator.clipboard.writeText(r.patch).then(() => toast("Copied"));
    $("#btn-dl").onclick = () => { const a = h("a", { href: URL.createObjectURL(new Blob([r.patch], { type: "text/x-diff" })), download: "patch.diff" }); a.click(); };
  }
}

// ============================================================================ boot
async function boot() {
  initTabs(); initPanZoom();
  const info = await api("/api/info"); S.info = info;
  $("#platform").textContent = `${info.platform} · ${info.bash ? (info.platform === "Windows" ? "Git Bash" : "bash") : "no bash found!"}`;
  $("#platform").title = info.bash || "Install Git for Windows";
  $("#bash-path").textContent = info.bash || "bash not found";
  // model name suggestions
  document.body.append(h("datalist", { id: "model-names" }, ["openai/gpt-5-mini", "openai/gpt-5", "anthropic/claude-sonnet-5", "anthropic/claude-haiku-4-5-20251001",
    "deepseek/deepseek-v4-flash", "qwen/qwen3.5-flash-02-23", "z-ai/glm-4.7-flash", "gemini/gemini-2.5-flash", "hosted_vllm/Qwen/Qwen2.5-7B-Instruct"].map((m) => h("option", { value: m }))));

  const st = info.settings || {};
  $("#repo").value = st.repo || "";
  $("#python-path").value = st.python || "";
  $("#runs-dir").value = st.runs_dir || info.default_runs_dir;
  // Always work in the local repository for now (the sandbox mode is kept in the code but not offered).
  $("input[name=wsmode][value=inplace]").checked = true;
  if (st.include_uncommitted === false) $("#include-uncommitted").checked = false;
  $("#reward-same").checked = st.reward_same_as_policy !== false;  // default: reward model = policy model
  renderKeys((await pushStoredKeys().catch(() => null)) || info.keys);
  $("#keys-file").textContent = `(${info.keys_file})`;
  setViewMode(pref("view", "tree"));  // saved in the app folder's settings.json
  checkRepo();

  const sel = $("#cfg-select");
  const tree = info.configs.filter((c) => c.tree_search), other = info.configs.filter((c) => !c.tree_search);
  sel.append(h("optgroup", { label: "Tree-search configs" }, tree.map((c) => h("option", { value: c.path, text: c.name }))));
  if (other.length) sel.append(h("optgroup", { label: "Other (not tree-search)" }, other.map((c) => h("option", { value: c.path, text: c.name }))));

  // restore the last edited config (browser draft) or load the default
  // Unsaved config edits survive a reload, but only drafts written by this version (older ones pointed at
  // swebench_ts.yaml / a LAN vLLM reward model), and restored edits are announced with a one-click reset.
  const draft = info.draft;  // unsaved edits, kept by the server in config_draft.json
  if (draft?.data && draft.dirty) {
    S.cfg = { path: draft.path, name: draft.name, data: draft.data }; S.cfgDirty = true; $("#cfg-path").value = draft.path || ""; renderAllForms();
    showDraftNotice(draft);
  } else {
    const def = tree.find((c) => c.name === "swe_xplorer.yaml") || tree.find((c) => c.name === "swebench_ts.yaml") || tree[0];
    if (def) await loadConfig(def.path).catch((e) => toast(e.message, true));
  }
  if (S.cfg.path) sel.value = S.cfg.path;

  // wiring
  $("#btn-cfg-load").onclick = () => (!S.cfgDirty || confirm("Discard unsaved config changes?")) && loadConfig(sel.value).catch((e) => toast(e.message, true));
  $("#btn-cfg-open").onclick = () => $("#cfg-path").value.trim() && loadConfig($("#cfg-path").value.trim()).catch((e) => toast(e.message, true));
  $("#btn-cfg-save").onclick = async () => {
    const def = S.cfg.path ? S.cfg.path.replace(/(\.ya?ml)$/i, "_custom$1") : "my_config.yaml";
    const p = prompt("Save config as (absolute path to a .yaml file):", def); if (!p) return;
    try {
      let r;
      try { r = await api("/api/config/save", { path: p, data: S.cfg.data }); }
      catch (e) { if (e.status === 409 && confirm(`${p} exists. Overwrite?`)) r = await api("/api/config/save", { path: p, data: S.cfg.data, overwrite: true }); else throw e; }
      S.cfg.path = r.path; S.cfg.name = r.path.split(/[\\/]/).pop(); S.cfgDirty = false; renderAllForms(); saveLocalDraft(); toast("Saved " + r.path);
    } catch (e) { if (e.status !== 409) toast(e.message, true); }
  };
  $("#btn-yaml-apply").onclick = async () => {
    try { const r = await api("/api/config/parse", { text: $("#yaml-text").value }); S.cfg.data = r.data; markDirty(); renderAllForms(); toast("YAML applied"); }
    catch (e) { toast("YAML error: " + e.message, true); }
  };
  $("#btn-yaml-refresh").onclick = refreshYaml;
  $("#reward-same").onchange = syncRewardSame;
  $("#btn-keys-save").onclick = () => saveKeys().catch((e) => toast(e.message, true));
  $("#btn-key-add").onclick = async () => { const n = $("#key-new-name").value.trim(); if (!n) return; await saveKeys({ [n]: null }); $("#key-new-name").value = ""; };
  $("#repo").addEventListener("change", checkRepo);
  $("#python-path").addEventListener("change", checkRepo);
  $("#btn-browse").onclick = () => openBrowse($("#repo").value.trim());
  $("#modal-close").onclick = () => $("#modal").classList.add("hidden");
  $("#modal-up").onclick = () => openBrowse($("#modal-up").dataset.p);
  $("#modal-go").onclick = () => openBrowse($("#modal-path").value);
  $("#modal-select").onclick = () => {
    if (modalInfo && !modalInfo.is_repo && !modalInfo.inside_repo) {
      const msg = "This folder is not a git repository. SWE-Xplorer only works on git repositories: run `git init` there first, or pick another folder.";
      $("#modal-info").textContent = msg; $("#modal-info").classList.add("modal-err");
      return toast(msg, true);
    }
    $("#repo").value = modalPath; $("#modal").classList.add("hidden"); checkRepo();
  };
  $("#btn-load-tree").onclick = async () => {
    const f = $("#tree-file").files[0]; if (!f) return toast("Choose a .tree.json file first", true);
    try {
      const tree = JSON.parse(await f.text());
      S.snapshots = []; S.lastSeq = 0; S.nodeCache.clear(); S.selected = null; S.drawnPos.clear(); S.live = true;
      await api("/api/load_tree", { tree, name: f.name });
      setTimeout(fit, 150);
    } catch (e) { toast(e.message, true); }
  };
  $("#btn-pause").onclick = () => control(S.paused ? "resume" : "pause");
  $("#btn-step").onclick = () => control("step");
  $("#btn-stop").onclick = () => confirm("Stop the run?") && control("stop");
  $("#btn-fit").onclick = fit;
  $("#btn-zoom-in").onclick = () => zoomAt(1.25, $("#tree-wrap").clientWidth / 2, $("#tree-wrap").clientHeight / 2);
  $("#btn-zoom-out").onclick = () => zoomAt(0.8, $("#tree-wrap").clientWidth / 2, $("#tree-wrap").clientHeight / 2);
  $("#follow").onchange = (e) => { S.follow = e.target.checked; };
  $("#show-legend").onchange = (e) => $("#legend").classList.toggle("collapsed", !e.target.checked);
  if (window.innerWidth < 1100 || window.innerHeight < 700) { $("#show-legend").checked = false; $("#legend").classList.add("collapsed"); }
  $("#show-pruned").onchange = (e) => { S.showPruned = e.target.checked; render(); };
  $("#color-mode").onchange = (e) => { S.colorMode = e.target.value; render(true); };
  $("#replay").oninput = (e) => { S.live = false; S.viewIdx = +e.target.value; if (S.viewIdx >= S.snapshots.length - 1) goLive(); else { updateReplay(); render(); } };
  $("#btn-live").onclick = goLive;
  $("#btn-play").onclick = play;
  $("#log-filter").oninput = refilterLog;
  window.addEventListener("keydown", (e) => { if (e.target.matches("input,textarea,select")) return; if (e.key === "f") fit(); if (e.key === " ") { e.preventDefault(); S.status.state === "running" && control(S.paused ? "resume" : "pause"); } });
  renderLegend();
  initChat();
  connect();
}
boot().catch((e) => toast("Failed to start: " + e.message, true));
