/* SWE-Xplorer chat panel: requests in, reasoning trace + final response out, follow-ups continue the tree. */
"use strict";

S.session = null;
S.liveTrace = [];
S.traceTimer = null;
S.phaseText = "";

const PHASE_CLASS = {
  "Iteration boundary": "phase-prune",
  "Cross-path reconciliation": "phase-reconcile",
  "Solution augmentation": "phase-select",
  "Recursive tournament voting": "phase-select",
};

function onSession(sess) { S.session = sess; renderChat(); updateComposer(); }

function scheduleTrace() {
  if (S.traceTimer) return;
  S.traceTimer = setTimeout(async () => {
    S.traceTimer = null;
    try { const r = await api("/api/trace"); S.liveTrace = r.trace || []; renderChat(); } catch {}
  }, 700);
}

function mdLite(text) {
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
  return String(text || "").split("```").map((b, i) => i % 2
    ? `<pre>${esc(b.replace(/^[\w-]*\n/, ""))}</pre>`
    : b.split(/\n{2,}/).map((p) => (p.trim() ? `<p>${inline(p.trim())}</p>` : "")).join("")).join("");
}

function backtrackText(e) {
  const after = e.from_step === 0 ? "from the starting point" : e.from_step ? `after step ${e.from_step}` : "";
  if (e.alt_of) return `a path elsewhere in the tree scored higher, so the next step tries an <b>alternative to step ${e.alt_of}</b>`
    + (after ? ` (a different way to continue ${after})` : "");
  if (after) return `a path elsewhere in the tree scored higher, so the search continues on another branch <b>${after}</b>`;
  return "a path elsewhere in the tree scored higher, so the search switches branches";
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
  return `<details class="tobs" data-key="${key}"><summary${bad ? ' class="bad"' : ""}>output${bad ? ` · exit ${o.rc}` : ""}</summary>
    ${o.note ? `<div class="obsnote">${esc(o.note)}</div>` : ""}<pre class="${bad ? "bad" : ""}${o.text.trim() ? "" : " empty"}">${esc(text)}</pre></details>`;
}

function traceHtml(trace, turnN) {
  let html = "";
  let altOf = null;  // the step right after a backtrack gets a "↳ alternative to step k" tag
  trace.forEach((e, i) => {
    if (e.kind === "step") {
      const alt = altOf; altOf = null;
      const invalid = !e.command;
      const color = invalid ? "#f87171" : TYPE_COLORS[e.type] || "#64748b";
      html += `<div class="tstep${S.selected === e.id ? " sel" : ""}" data-node="${e.id}" style="--dot:${color}" title="Show this step in the tree">
        <div class="thead"><span class="typebadge" style="background:${color}">${invalid ? "NO ACTION" : esc((e.type || "?").toUpperCase())}</span>
          <span>step ${e.n ?? "–"}</span><span>· itr ${e.itr ?? "–"}</span>${alt ? `<span class="alt">↳ alternative to step ${alt}</span>` : ""}${e.sys ? "<span>· system</span>" : ""}<span class="spacer"></span><span>value ${fmt(e.v)}</span></div>
        ${e.thought ? `<div class="tthought clamp">${esc(e.thought)}</div>` : ""}
        ${e.command ? `<code class="tcmd">${esc(e.command)}</code>` : ""}
        ${e.obs ? obsHtml(e.obs, `o${turnN}-${i}`) : ""}
      </div>`;
    } else if (e.kind === "backtrack") {
      altOf = e.alt_of || null;
      html += `<div class="tmark backtrack">↩ <b>Backtrack</b> · ${backtrackText(e)}</div>`;
    } else if (e.kind === "phase") {
      html += `<div class="tmark ${PHASE_CLASS[e.label] || ""}">◆ <b>${esc(e.label)}</b> ${esc(e.detail || "")}</div>`;
    }
  });
  return html || '<div class="muted" style="padding:6px 0">Preparing the workspace and indexing the repository…</div>';
}

function decisionChip(t) {
  if (t.status !== "Submitted") return "";
  if (t.decision === "accepted") return `<span class="chip acc">✓ accepted${t.applied ? " · applied to repo" : ""}</span>`;
  if (t.decision === "rejected") return '<span class="chip rej">✗ rejected</span>';
  return '<span class="chip pend">awaiting review</span>';
}

function turnHtml(t, isLatest) {
  const running = t.status === "running";
  const trace = running ? S.liveTrace : t.trace || [];
  const nSteps = trace.filter((e) => e.kind === "step").length;
  const nBt = trace.filter((e) => e.kind === "backtrack").length;
  const status = running
    ? `<span class="chip live" id="live-chip">${esc(S.phaseText || "working…")}</span>`
    : t.status === "Submitted" ? decisionChip(t) : `<span class="chip bad">${esc(t.status || "?")}</span>`;
  const meta = [
    t.mode === "continue" ? `<span class="chip" title="The new request was attached under the previous winning submission node">↳ continued from the last submission</span>` : "",
    !running && t.cost != null ? `<span class="chip">$${Number(t.cost).toFixed(3)}</span>` : "",
  ].join("");
  const reasoning = `<details class="reasoning" data-key="r${t.n}-${t.status}"${running ? " open" : ""}>
      <summary>${running ? '<span class="spin"></span>' : ""}Reasoning · ${nSteps} step${nSteps === 1 ? "" : "s"}${nBt ? ` · ${nBt} backtrack${nBt === 1 ? "" : "s"}` : ""}${t.iterations ? ` · ${t.iterations} iteration${t.iterations === 1 ? "" : "s"}` : ""}</summary>
      <div class="trace">${traceHtml(trace, t.n)}</div></details>`;
  let body = "";
  if (!running && t.status === "Submitted") {
    body += `<div class="response">${mdLite(t.response || "Submitted a patch.")}</div>`;
    if (t.judge_summary) body += `<details class="reasoning" data-key="j${t.n}"><summary>Tournament judge's summary of this solution</summary><div class="trace response">${mdLite(t.judge_summary)}</div></details>`;
    const files = (t.files || []).map((f) => `<span class="file">${esc(f)}</span>`).join("") || '<span class="muted">no file changes</span>';
    body += `<div class="changes"><div class="row"><b>Changes</b><span class="spacer"></span>
        ${t.winner ? `<button data-act="tree" data-node="${t.winner}">Show in tree</button>` : ""}</div>
      <div class="files">${files}</div>
      <details data-key="d${t.n}"${isLatest && !t.decision ? " open" : ""}><summary class="muted">Diff (relative to your code when the request was made)</summary><pre class="box diff">${diffHtml(t.turn_patch || t.patch || "")}</pre></details></div>`;
    if (isLatest && !t.decision) {
      const clone = S.session?.mode === "clone";
      body += `<div class="review"><span class="q">${clone ? "Apply these changes to your repository?" : "The changes are in your files as uncommitted edits. Keep them?"}</span>
        <button class="accept" data-act="accept" data-n="${t.n}">✓ ${clone ? "Accept &amp; apply" : "Keep"}</button>
        <button class="reject" data-act="reject" data-n="${t.n}">✗ Reject</button></div>`;
    }
  } else if (!running) {
    body += `<div class="response err">${esc(t.error || t.status)}</div>`;
  }
  return `<div class="msg-user">${esc(t.user)}</div>
    <div class="msg-agent"><div class="agent-head"><span class="wordmark"><b>SWE-X</b>plorer</span>${status}${meta}</div>${reasoning}${body}</div>`;
}

// ============================================================================ start screen + history
S.sessions = [];

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function histItemsHtml(list) {
  if (!list.length) return '<div class="hist-empty">No earlier chats yet.</div>';
  return list.map((x) => `<div class="hist-item${x.current ? " cur" : ""}" data-dir="${esc(x.dir)}" title="${esc(x.repo)}\n${x.turns} request${x.turns === 1 ? "" : "s"}">
      <span class="ht">${esc(x.title)}</span><span class="hm">${esc((x.repo || "").split(/[\\/]/).pop())} · ${ago(x.updated)}</span></div>`).join("");
}

async function loadSessions() {
  try { S.sessions = (await api("/api/sessions")).sessions || []; } catch { S.sessions = []; }
  if (!S.session?.turns?.length) renderChat();
}

function startHtml() {
  const noRepo = !S.repo && !S.session;
  const recent = S.sessions.filter((x) => !x.current).slice(0, 5);
  return `<div class="start"><img class="mark" src="/static/logo.svg" alt=""><span class="wordmark"><b>SWE-X</b>plorer</span>
    ${noRepo
      ? `<p>Select a local git repository with <b>📁 Select repository</b> below to start.</p>
         <p class="muted">It edits your repository directly. When it finishes, your branch is as it was and the result is in your files as uncommitted changes, which you keep or reject.</p>`
      : `<p>Describe a bug or a feature. Each request runs an <b>explore → reconcile → prune</b> search; the tree grows live in the middle.</p>
         <p class="muted">Results wait for your review. <kbd>Enter</kbd> sends, <kbd>Shift</kbd>+<kbd>Enter</kbd> adds a line.</p>`}
    ${recent.length ? `<div class="recent"><div class="recent-title">Recent chats</div>${histItemsHtml(recent)}</div>` : ""}</div>`;
}

async function openChat(dir) {
  if (S.status?.state === "running") return toast("Wait for the run to finish.", true);
  try {
    const r = await api("/api/session/open", { dir });
    S.liveTrace = []; S.snapshots = []; S.lastSeq = 0; S.selected = null; S.drawnPos.clear(); S.pos.clear(); S.nodeCache.clear();
    await syncHistory();
    onSession(r.session);
    if (r.session?.repo) { $("#repo").value = r.session.repo; checkRepo(); }
    $("#history-pop").classList.add("hidden");
    setTimeout(fit, 120);
  } catch (e) { toast(e.message, true); }
}

// ============================================================================ rendering
function onRepoChanged() { renderChat(); updateComposer(); }

function renderChat() {
  const log = $("#chat-log");
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 90;
  const state = new Map($$("details[data-key]", log).map((d) => [d.dataset.key, d.open]));
  const turns = S.session?.turns || [];
  $("#chat-title").textContent = turns.length ? turns[0].user.split("\n")[0].slice(0, 80) : "New chat";
  if (!turns.length) { log.innerHTML = startHtml(); updateRepoChip(); return; }
  const last = turns[turns.length - 1];
  log.innerHTML = turns.map((t) => turnHtml(t, t === last)).join("");
  $$("details[data-key]", log).forEach((d) => { if (state.has(d.dataset.key)) d.open = state.get(d.dataset.key); });
  if (nearBottom) log.scrollTop = log.scrollHeight;
  updateRepoChip();
}

function updateLiveChip(p) {
  S.phaseText = p.phase + (p.detail ? ` · ${p.detail}` : "");
  const c = $("#live-chip"); if (c) c.textContent = S.phaseText;
}

function updateRepoChip() {
  const chip = $("#chat-repo");
  const r = S.session
    ? { name: S.session.repo.split(/[\\/]/).pop(), root: S.session.repo }
    : S.repo;
  chip.classList.toggle("need", !r);
  if (!r) { chip.innerHTML = "📁 Select repository"; chip.title = "Choose the local git repository the agent works on"; }
  else {
    chip.innerHTML = `📁 <span class="rname">${esc(r.name)}</span> <span class="caret">▾</span>`;
    chip.title = `${r.root}${S.session?.turns?.length ? "\nLocked to this chat; switching starts a new chat." : "\nClick to change"}`;
  }
  // Python used for the commands: fixed once a chat has run, otherwise what the selected repo resolves to
  const py = S.session?.python || S.repo?.python;
  const pyChip = $("#py-chip");
  pyChip.classList.toggle("hidden", !py);
  if (py) {
    pyChip.textContent = `🐍 ${py.label}`;
    pyChip.classList.toggle("warn-text", !!py.error);
    pyChip.title = py.error ? py.error : `${py.python}${py.venv ? `\nvirtualenv: ${py.venv}` : "\nNo virtualenv found in the repository (.venv / venv / env)"}` +
      "\nThe repository root is on PYTHONPATH, so imports load the code being edited.\nChange it in the Setup tab.";
    $("#python-info").textContent = py.error || `Using ${py.python}${py.venv ? "" : " (no virtualenv found in the repository)"}`;
  }
  const quick = $("#ws-quick");
  quick.value = S.session ? S.session.mode : $("input[name=wsmode]:checked").value;
  quick.disabled = !!S.session?.turns?.length;
  quick.title = quick.disabled ? "Fixed for this chat" : "Where the agent works";
}

async function pickRepo() {
  if (S.status?.state === "running") return toast("Wait for the run to finish.", true);
  if (S.session?.turns?.length) {
    if (!confirm("This chat is tied to its repository. Switch repository and start a new chat?")) return;
    await newChat(true);
  }
  openBrowse($("#repo").value.trim());
}

function autosize() {
  const ta = $("#chat-input");
  ta.style.height = "auto";
  ta.style.height = Math.min(220, ta.scrollHeight) + "px";
}

function updateComposer() {
  const running = S.status?.state === "running";
  const turns = S.session?.turns || [];
  const last = turns[turns.length - 1];
  const pending = !running && last?.status === "Submitted" && !last.decision;
  const followUp = turns.some((t) => t.decision === "accepted");
  const noRepo = !S.session && !S.repo;
  // start screen: only the chat until the first request of a chat; then the tree, git graph and details appear
  const startScreen = !turns.length;
  const wasStart = document.body.classList.contains("start-screen");
  document.body.classList.toggle("start-screen", startScreen);
  if (wasStart && !startScreen) setTimeout(() => { render(true); fit(); }, 60);  // the tree view just got its size
  const send = $("#btn-send");
  send.classList.toggle("stop", running);
  send.textContent = running ? "■" : "↑";
  send.title = running ? "Stop the run" : "Send (Enter)";
  send.disabled = !running && (pending || noRepo);
  $("#chat-input").disabled = running || pending || noRepo;
  $("#chat-input").placeholder = noRepo ? "Select a repository to start…"
    : running ? "SWE-Xplorer is working…"
    : pending ? "Accept or reject the result above to continue"
    : followUp ? "Ask for a follow-up change…" : "Describe a bug or a feature…";
  $("#composer-hint").textContent = pending ? "Review the last result to continue" : followUp ? "Follow-ups continue the tree" : "";
  $("#btn-auto").classList.toggle("on", $("#auto-accept").checked);
}

// ============================================================================ actions
async function sendMessage() {
  if (S.status?.state === "running") { if (confirm("Stop the run?")) control("stop"); return; }
  const msg = $("#chat-input").value.trim();
  if (!msg) return;
  if (!S.cfg.data) return toast("Load a config first", true);
  const first = !S.session;
  const mode = $("input[name=wsmode]:checked").value;
  if (first && !S.repo) { pickRepo(); return toast("Select a repository first", true); }
  try {
    if (Object.keys(pendingKeys()).length) await saveKeys();
    else await pushStoredKeys();  // the server may have restarted since the page loaded
    await api("/api/chat", {
      message: msg, repo: $("#repo").value.trim(), workspace_mode: mode,
      include_uncommitted: $("#include-uncommitted").checked, runs_dir: $("#runs-dir").value.trim(),
      config: S.cfg.data, config_path: S.cfg.path, reward_same_as_policy: $("#reward-same").checked,
      auto_accept: $("#auto-accept").checked, python: $("#python-path").value.trim(),
    });
    $("#chat-input").value = ""; autosize();
    S.liveTrace = []; S.snapshots = []; S.lastSeq = 0; S.nodeCache.clear(); S.result = null; S.live = true;
    S.logs = []; $("#log").textContent = "";
    if (first) { S.selected = null; S.drawnPos.clear(); S.pos.clear(); S.lastCurrent = null; }
    $("#result-view").innerHTML = '<div class="muted pad">Running…</div>';
  } catch (e) { toast(e.message, true); }
}

async function newChat(confirmed = false) {
  if (S.status?.state === "running") return toast("Stop the run first.", true);
  if (!confirmed && S.session?.turns?.length && !confirm("Start a new chat? This chat stays available in the history.")) return;
  try {
    await api("/api/session/new", {});
    S.session = null; S.liveTrace = []; S.snapshots = []; S.lastSeq = 0; S.selected = null;
    S.drawnPos.clear(); S.pos.clear(); S.nodeCache.clear(); S.result = null;
    render(); renderChat(); updateComposer(); loadSessions();
    $("#result-view").innerHTML = '<div class="muted pad">No result yet.</div>';
  } catch (e) { toast(e.message, true); }
}

function togglePop(id, show) {
  const el = $(id);
  const on = show ?? el.classList.contains("hidden");
  $$(".popover").forEach((p) => p.classList.add("hidden"));
  el.classList.toggle("hidden", !on);
  return on;
}

function initChat() {
  const ta = $("#chat-input");
  $("#btn-send").onclick = sendMessage;
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
  });
  ta.addEventListener("input", autosize);
  $("#btn-new-chat").onclick = () => newChat();
  $("#btn-history").onclick = async (e) => {
    e.stopPropagation();
    if (!togglePop("#history-pop")) return;
    $("#history-pop").innerHTML = '<div class="hist-empty">Loading…</div>';
    await loadSessions();
    $("#history-pop").innerHTML = histItemsHtml(S.sessions);
  };
  $("#history-pop").addEventListener("click", (e) => { const it = e.target.closest(".hist-item"); if (it) openChat(it.dataset.dir); });
  $("#auto-accept").checked = !!pref("autoAccept", false);
  $("#btn-auto").onclick = () => {
    const cb = $("#auto-accept"); cb.checked = !cb.checked;
    setPref("autoAccept", cb.checked);
    toast(cb.checked ? "Auto-accept on: results are applied to your repo as soon as they are submitted" : "Auto-accept off: results wait for your review");
    updateComposer();
  };
  $("#btn-model").onclick = (e) => { e.stopPropagation(); togglePop("#model-pop"); };
  $("#model-pop").addEventListener("click", (e) => {
    e.stopPropagation();
    const a = e.target.closest("a[data-goto]"); if (a) { e.preventDefault(); togglePop("#model-pop", false); showTab("#left", a.dataset.goto); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".popover, #btn-history, #btn-model")) $$(".popover").forEach((p) => p.classList.add("hidden")); });
  $("#chat-repo").onclick = pickRepo;
  $("#py-chip").onclick = () => showTab("#left", "task");
  $("#ws-quick").onchange = (e) => {
    if (e.target.value === "inplace" && !confirm("In place: the agent works directly in your repository (it creates commits, detaches HEAD and runs git reset/clean). Use a sandbox copy unless you know you want this.\n\nSwitch to in place?")) {
      e.target.value = "clone"; return;
    }
    $(`input[name=wsmode][value=${e.target.value}]`).checked = true; updateRepoChip();
  };
  $$("input[name=wsmode]").forEach((r) => r.addEventListener("change", updateRepoChip));
  $("#chat-log").addEventListener("click", async (e) => {
    const hist = e.target.closest(".hist-item"); if (hist) return openChat(hist.dataset.dir);
    const btn = e.target.closest("button[data-act]");
    if (btn) {
      if (btn.dataset.act === "tree") { if (!S.live) goLive(); selectNode(btn.dataset.node); }
      if (btn.dataset.act === "accept" || btn.dataset.act === "reject") {
        btn.disabled = true;
        try {
          const r = await api("/api/decide", { turn: +btn.dataset.n, decision: btn.dataset.act });
          toast(r.message);
        } catch (err) { toast(err.message, true); btn.disabled = false; }
      }
      return;
    }
    if (e.target.closest("details, summary, pre")) return;
    const step = e.target.closest(".tstep");
    if (step) {
      const snap = currentSnap();
      if (snap && snap.nodes.some((n) => n.id === step.dataset.node)) selectNode(step.dataset.node);
      else toast("That step is not in the tree currently shown");
    }
  });
  renderChat(); updateComposer(); loadSessions();
}
