/* SWE-Xplorer git graph: the workspace's commits (one per edit node), HEAD moving as the search backtracks. */
"use strict";

const GIT = { data: null, timer: null, lastHead: null, open: null, mode: "tree", fetching: false };
const G_ROW = 30, G_LANE = 14, G_X0 = 12;
const G_COLORS = ["#60a5fa", "#34d399", "#f472b6", "#fbbf24", "#a78bfa", "#22d3ee", "#fb923c", "#f87171"];

function setViewMode(mode) {
  GIT.mode = mode;
  $("#views").className = `views mode-${mode}`;
  $$("#view-mode button").forEach((b) => b.classList.toggle("on", b.dataset.view === mode));
  if (S.info) setPref("view", mode);
  if (mode !== "git") setTimeout(fit, 30);
  if (mode !== "tree") refreshGit(true);
}

function refreshGit(now = false) {
  if (GIT.mode === "tree") return;
  if (GIT.timer && !now) return;
  clearTimeout(GIT.timer);
  GIT.timer = setTimeout(async () => {
    GIT.timer = null;
    if (GIT.fetching) return;
    GIT.fetching = true;
    try { GIT.data = await api("/api/git"); renderGit(); } catch (e) { console.warn(e); } finally { GIT.fetching = false; }
  }, now ? 0 : 1200);
}

function layoutGit(commits) {
  const known = new Set(commits.map((c) => c.h));
  const lanes = [], pos = new Map();
  let width = 1;
  commits.forEach((c, row) => {
    let lane = lanes.indexOf(c.h);
    if (lane < 0) { lane = lanes.indexOf(null); if (lane < 0) { lane = lanes.length; lanes.push(null); } }
    for (let i = 0; i < lanes.length; i++) if (i !== lane && lanes[i] === c.h) lanes[i] = null; // converging branches end here
    pos.set(c.h, { row, lane });
    const parents = c.p.filter((p) => known.has(p));
    lanes[lane] = parents[0] ?? null;
    for (const p of parents.slice(1)) {
      if (lanes.includes(p)) continue;
      let l = lanes.indexOf(null); if (l < 0) { l = lanes.length; lanes.push(null); }
      lanes[l] = p;
    }
    while (lanes.length && lanes[lanes.length - 1] === null) lanes.pop();
    width = Math.max(width, lanes.length, lane + 1);
  });
  return { pos, width };
}

const gx = (lane) => G_X0 + lane * G_LANE;
const gy = (row) => row * G_ROW + G_ROW / 2;

function renderGit() {
  const d = GIT.data;
  const empty = $("#git-empty"), graph = $("#git-graph");
  if (!d || !d.available) { empty.textContent = d?.reason || "The graph appears once the first run starts."; empty.classList.remove("hidden"); graph.classList.add("hidden"); $("#git-sub").textContent = "workspace"; return; }
  empty.classList.add("hidden"); graph.classList.remove("hidden");
  $("#git-sub").textContent = `${d.commits.length} commit${d.commits.length === 1 ? "" : "s"} · HEAD ${d.head.slice(0, 7)}${d.dirty ? ` · ${d.dirty} uncommitted` : ""}`;

  const snap = currentSnap();
  const byId = new Map((snap?.nodes || []).map((n) => [n.id, n]));
  const onPath = new Set();
  for (let c = byId.get(snap?.current); c; c = byId.get(c.p)) if (c.cm) onPath.add(c.cm);
  const winners = new Map();
  for (const t of S.session?.turns || []) { const w = t.winner && byId.get(t.winner); if (w?.cm) winners.set(w.cm, t.n); }
  const selCommit = S.selected && byId.get(S.selected)?.cm;
  const showPruned = $("#git-pruned").checked;

  let commits = d.commits.filter((c) => showPruned || c.visible || c.h === d.head || c.kind !== "edit");
  if (d.dirty) commits = [{ h: "WORKTREE", p: [d.head], virtual: true, s: `Uncommitted changes (${d.dirty} file${d.dirty === 1 ? "" : "s"})`, stat: [], refs: [] }, ...commits];
  const { pos, width } = layoutGit(commits);
  const W = G_X0 + width * G_LANE;

  // edges then dots
  let svg = "";
  for (const c of commits) {
    const a = pos.get(c.h);
    for (const p of c.p) {
      const b = pos.get(p); if (!b) continue;
      const col = G_COLORS[a.lane % G_COLORS.length];
      const x1 = gx(a.lane), y1 = gy(a.row), x2 = gx(b.lane), y2 = gy(b.row);
      const dash = c.virtual ? ' stroke-dasharray="3 3"' : "";
      const dpath = x1 === x2 ? `M${x1},${y1} L${x2},${y2}` : `M${x1},${y1} L${x1},${y2 - G_ROW * 0.6} C${x1},${y2 - G_ROW * 0.2} ${x2},${y2 - G_ROW * 0.35} ${x2},${y2}`;
      svg += `<path d="${dpath}" stroke="${col}" stroke-width="2" fill="none"${dash}/>`;
    }
  }
  for (const c of commits) {
    const a = pos.get(c.h), col = G_COLORS[a.lane % G_COLORS.length];
    const x = gx(a.lane), y = gy(a.row);
    if (c.virtual) svg += `<circle cx="${x}" cy="${y}" r="4.5" fill="none" stroke="${col}" stroke-width="2" stroke-dasharray="2 2"/>`;
    else if (c.h === d.head) svg += `<circle cx="${x}" cy="${y}" r="6.5" fill="#0f1117" stroke="${col}" stroke-width="3"/><circle cx="${x}" cy="${y}" r="2.5" fill="${col}"/>`;
    else if (c.kind === "root" || c.kind === "baseline") svg += `<rect x="${x - 4.5}" y="${y - 4.5}" width="9" height="9" rx="2" fill="${col}"/>`;
    else svg += `<circle cx="${x}" cy="${y}" r="4.5" fill="${col}"/>`;
    if (winners.has(c.h)) svg += `<circle cx="${x}" cy="${y}" r="9" fill="none" stroke="#facc15" stroke-width="2"/>`;
  }
  const svgEl = $("#git-svg");
  svgEl.setAttribute("width", W); svgEl.setAttribute("height", commits.length * G_ROW);
  svgEl.innerHTML = svg;

  const rows = commits.map((c) => {
    const refs = [];
    if (c.h === d.head) refs.push('<span class="ref head">HEAD</span>');
    if (c.kind === "baseline") refs.push('<span class="ref base">baseline</span>');
    if (winners.has(c.h)) refs.push(`<span class="ref win">★ request ${winners.get(c.h)}</span>`);
    for (const r of c.refs) { const name = r.replace(/^HEAD -> /, "").replace(/^HEAD$/, ""); if (name && !name.startsWith("tag:")) refs.push(`<span class="ref br">${esc(name)}</span>`); }
    let label;
    if (c.virtual) label = `<span class="glabel">${esc(c.s)}</span>`;
    else if (c.kind === "edit" && c.node) label = `<span class="typebadge" style="background:${TYPE_COLORS[c.node.type] || "#64748b"}">${esc((c.node.type || "edit").toUpperCase())}</span><span class="glabel"><code>${esc(c.node.cmd || "")}</code></span>`;
    else if (c.kind === "root") label = `<span class="glabel">pseudo-root · request ${c.node?.rq ?? 1}</span><span class="gsub">${esc(c.s)}</span>`;
    else if (c.kind === "baseline") label = `<span class="glabel">pseudo-root · request 1</span><span class="gsub">${esc(c.s)}${c.s === "Committing changes before starting tree search" ? " (your uncommitted work)" : " (your repo had no uncommitted changes, so no extra commit)"}</span>`;
    else label = `<span class="glabel">${esc(c.s)}</span>`;
    const adds = c.stat.reduce((s, x) => s + (x[1] || 0), 0), dels = c.stat.reduce((s, x) => s + (x[2] || 0), 0);
    const stat = c.stat.length ? `<span class="gstat" title="${esc(c.stat.map((x) => x[0]).join("\n"))}">${esc(c.stat[0][0].split("/").pop())}${c.stat.length > 1 ? ` +${c.stat.length - 1}` : ""} <span class="a">+${adds}</span> <span class="d">−${dels}</span></span>` : "";
    const cls = ["grow", onPath.has(c.h) ? "onpath" : "", (selCommit === c.h || GIT.open === c.h) ? "sel" : "", c.visible === false ? "faded" : "", c.h === d.head && GIT.lastHead && GIT.lastHead !== d.head ? "flash" : ""].join(" ");
    const title = c.virtual ? "Changes in the working tree that are not committed yet" : `${c.h}\n${c.s}${c.nodes?.length ? `\n${c.nodes.length} tree node(s) at this state` : ""}`;
    const row = `<div class="${cls}" data-h="${c.h}" data-node="${c.node?.id || ""}" title="${esc(title)}">${refs.join("")}${label}${stat}<span class="ghash">${c.virtual ? "" : c.h.slice(0, 7)}</span></div>`;
    return row;
  });
  let hist = "";
  if (d.history?.length) hist = `<div class="git-sep">earlier history of the repository</div>` + d.history.map((c) => `<div class="grow hist" title="${esc(c.h)}"><span class="glabel">${esc(c.s)}</span><span class="ghash">${c.h.slice(0, 7)}</span></div>`).join("");
  if (!d.commits.some((c) => c.kind === "edit" || c.kind === "root") && S.status?.state === "running") {
    hist = `<div class="git-sep">no edits yet: the agent commits every EDIT candidate; READ / SEARCH / TEST steps don't change the code</div>` + hist;
  }
  const rowsEl = $("#git-rows");
  rowsEl.style.paddingLeft = W + 8 + "px";
  rowsEl.innerHTML = rows.join("") + hist;
  GIT.lastHead = d.head;
}

async function openGitDetail(h) {
  GIT.open = h;
  const pane = $("#git-detail"), body = $("#git-detail-body");
  if (!h) { pane.classList.add("hidden"); return; }
  pane.classList.remove("hidden");
  $("#git-detail-title").textContent = h === "WORKTREE" ? "Uncommitted changes" : `commit ${h.slice(0, 10)}`;
  body.innerHTML = '<span class="muted">loading…</span>';
  try {
    const r = await api(`/api/git/show?h=${encodeURIComponent(h)}`);
    if (GIT.open === h) body.innerHTML = `<pre class="box diff">${diffHtml(r.text || "(no changes)")}</pre>`;
  } catch (e) { body.innerHTML = `<span class="warn-text">${esc(e.message)}</span>`; }
}

function scrollGitToSelected() {
  if (GIT.mode === "tree") return;
  const row = $("#git-rows .grow.sel");
  if (!row) return;
  const box = $("#git-scroll"), r = row.getBoundingClientRect(), b = box.getBoundingClientRect();
  if (r.top < b.top || r.bottom > b.bottom) row.scrollIntoView({ block: "center", behavior: "smooth" });
}

function initGit() {
  $("#view-mode").addEventListener("click", (e) => { const b = e.target.closest("button[data-view]"); if (b) setViewMode(b.dataset.view); });
  $("#btn-git-refresh").onclick = () => refreshGit(true);
  $("#git-pruned").onchange = renderGit;
  $("#git-detail-close").onclick = () => { openGitDetail(null); renderGit(); };
  $("#git-rows").addEventListener("click", (e) => {
    if (e.target.closest(".gdetail")) return;
    const row = e.target.closest(".grow[data-h]"); if (!row) return;
    const h = row.dataset.h;
    openGitDetail(GIT.open === h ? null : h);
    const nodeId = row.dataset.node;
    const snap = currentSnap();
    if (nodeId && snap?.nodes.some((n) => n.id === nodeId)) { S.selected = nodeId; render(true); showTab("#right", "node"); }
    renderGit();
  });
  // piggy-back on the tree renderer: every tree update may have moved HEAD or created commits
  const treeRender = render;
  render = function (...args) { treeRender(...args); if (GIT.mode !== "tree") { renderGit(); refreshGit(); scrollGitToSelected(); } };
  setViewMode("tree");  // the saved view is applied once the server's preferences are loaded (see boot)
}
initGit();
