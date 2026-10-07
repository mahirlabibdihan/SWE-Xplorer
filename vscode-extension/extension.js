// SWE-Xplorer for VS Code: a sidebar chat backed by the SWE-Xplorer GUI server (python -m minisweagent.gui).
// The extension host owns the backend process and its API; the sidebar webview only renders state and sends intents.
"use strict";

const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const { Backend } = require("./lib/backend");
const { beforeContent } = require("./lib/review");

/** Providers offered in the model picker (same list as the web GUI's). */
const PROVIDERS = [
  { id: "openrouter", label: "OpenRouter", key: "OPENROUTER_API_KEY", cls: "openrouter", prefix: "",
    models: ["openrouter/free", "poolside/laguna-s-2.1:free", "nvidia/nemotron-3-super-120b-a12b:free", "cohere/north-mini-code:free",
      "deepseek/deepseek-v4-flash", "openai/gpt-5-mini", "openai/gpt-5", "anthropic/claude-sonnet-5", "anthropic/claude-opus-5-5",
      "google/gemini-2.5-flash", "qwen/qwen3.5-flash-02-23", "z-ai/glm-4.7-flash"] },
  { id: "openai", label: "OpenAI", key: "OPENAI_API_KEY", cls: "litellm", prefix: "openai/", models: ["openai/gpt-5-mini", "openai/gpt-5", "openai/gpt-4.1-mini"] },
  { id: "anthropic", label: "Anthropic", key: "ANTHROPIC_API_KEY", cls: "litellm", prefix: "anthropic/",
    models: ["anthropic/claude-sonnet-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-4-5-20251001"] },
  { id: "gemini", label: "Google Gemini", key: "GEMINI_API_KEY", cls: "litellm", prefix: "gemini/", models: ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"] },
  { id: "deepseek", label: "DeepSeek", key: "DEEPSEEK_API_KEY", cls: "litellm", prefix: "deepseek/", models: ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"] },
];
const BEFORE_SCHEME = "swexplorer-before";

let ctl = null;

// ======================================================================================================================
// Controller: backend lifecycle + state shared by the sidebar, the status bar and the commands
// ======================================================================================================================
class Controller {
  constructor(context) {
    this.context = context;
    this.output = vscode.window.createOutputChannel("SWE-Xplorer");
    this.status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 40);
    this.status.command = "sweXplorer.focus";
    this.backend = null;
    this.info = null;      // /api/info
    this.config = null;    // {path, name, data} of the agent config
    this.session = null;   // current chat (server side)
    this.runStatus = { state: "idle" };
    this.phase = null;
    this.liveTrace = [];
    this.recent = [];      // earlier chats (/api/sessions), for the welcome screen
    this.context_ = null;  // code attached with "Ask SWE-Xplorer about this code"
    this.views = new Set(); // ChatViewProviders (secondary sidebar, or the activity bar on older VS Code)
    this._traceTimer = null;
    this._starting = null;
    this.renderStatus();
    this.status.show();
  }

  settings() { return vscode.workspace.getConfiguration("sweXplorer"); }
  log(s) { if (s) this.output.appendLine(s); }

  workspaceRoot() {
    const folders = vscode.workspace.workspaceFolders || [];
    if (!folders.length) return null;
    const active = vscode.window.activeTextEditor && vscode.workspace.getWorkspaceFolder(vscode.window.activeTextEditor.document.uri);
    return (active || folders[0]).uri.fsPath;
  }

  // -- backend -------------------------------------------------------------------------------------------------------
  async ensureBackend() {
    if (this.backend && this.backend.state === "ready") return this.backend;
    if (this._starting) return this._starting;
    this._starting = (async () => {
      const s = this.settings();
      if (this.backend) this.backend.stopProcess();
      this.backend = new Backend({
        python: s.get("pythonPath"), port: s.get("port"), serverUrl: s.get("serverUrl"),
        cwd: this.workspaceRoot() || undefined, log: (x) => this.log(x),
      });
      this.backend.on("event", (e) => this.onEvent(e));
      this.backend.on("connected", () => this.refresh().catch(() => {}));
      this.backend.on("state", () => { this.renderStatus(); this.push(); });
      this.push();
      try {
        await this.backend.start();
        await this.refresh();
        await this.loadConfig();
      } catch (e) {
        this.log(`Backend failed: ${e.message}`);
        this.push();
        throw e;
      }
      return this.backend;
    })().finally(() => { this._starting = null; });
    return this._starting;
  }

  async loadConfig() {
    const p = this.settings().get("config") || "swe_xplorer.yaml";
    this.config = await this.backend.get(`/api/config?path=${encodeURIComponent(p)}`);
    this.push();
  }

  async refresh() {
    if (!this.backend || this.backend.state !== "ready") return;
    this.info = await this.backend.get("/api/info");
    this.session = this.info.session;
    this.runStatus = this.info.status || { state: this.info.running ? "running" : "idle" };
    if (this.info.running) { const r = await this.backend.get("/api/trace"); this.liveTrace = r.trace || []; }
    await this.loadRecent();
    this.renderStatus();
    this.push();
  }

  async loadRecent() {
    try { this.recent = (await this.backend.get("/api/sessions")).sessions || []; } catch { this.recent = []; }
  }

  /** The 3 latest chats, this workspace's first; the open chat is left out. */
  recentChats() {
    const root = this.workspaceRoot();
    const open = this.session && this.session.dir;
    const list = this.recent.filter((x) => x.dir !== open || (root && norm(x.repo) !== norm(root)));
    const here = list.filter((x) => root && norm(x.repo) === norm(root));
    const other = list.filter((x) => !(root && norm(x.repo) === norm(root)));
    return [...here, ...other].slice(0, 3).map((x) => ({ dir: x.dir, title: x.title, repo: path.basename(x.repo || ""), turns: x.turns, updated: x.updated }));
  }

  async refreshSession() {
    const r = await this.backend.get("/api/session");
    this.session = r.session;
    this.push();
  }

  onEvent(evt) {
    const d = evt.data || {};
    switch (evt.type) {
      case "snapshot": if (this.isRunning()) this.scheduleTrace(); break;
      case "phase": this.phase = d; this.renderStatus(); this.post({ type: "phase", phase: d }); break;
      case "status": this.runStatus = d; this.renderStatus(); this.post({ type: "status", status: d }); break;
      case "session": this.session = d.session; this.push(); break;
      case "log": if (d.level === "warning" || d.level === "error") this.log(`[${d.level}] ${d.msg}`); break;
      case "prefs": if (this.info) { this.info.prefs = d.prefs || {}; this.push(); } break;
      case "finished": this.onFinished(d); break;
    }
  }

  async onFinished(result) {
    this.phase = null;
    try { await this.loadRecent(); await this.refreshSession(); } catch {}
    this.renderStatus();
    const turn = this.lastTurn();
    if (!turn) return;
    if (turn.status === "Submitted" && !turn.decision) {
      const pick = await vscode.window.showInformationMessage(
        `SWE-Xplorer changed ${turn.files.length} file${turn.files.length === 1 ? "" : "s"}. Review the result?`, "Review changes", "Keep", "Reject");
      if (pick === "Review changes") this.review(turn.n);
      else if (pick === "Keep") this.decide(turn.n, "accept");
      else if (pick === "Reject") this.decide(turn.n, "reject");
    } else if (turn.status !== "Submitted" && turn.status !== "Stopped") {
      vscode.window.showWarningMessage(`SWE-Xplorer: ${turn.status}. ${(result && result.message || "").slice(0, 200)}`);
    }
  }

  scheduleTrace() {
    if (this._traceTimer) return;
    this._traceTimer = setTimeout(async () => {
      this._traceTimer = null;
      try { const r = await this.backend.get("/api/trace"); this.liveTrace = r.trace || []; this.post({ type: "trace", trace: this.liveTrace }); } catch {}
    }, 500);
  }

  isRunning() { return this.runStatus && this.runStatus.state === "running"; }
  lastTurn() { const t = (this.session && this.session.turns) || []; return t[t.length - 1] || null; }

  renderStatus() {
    const st = this.status;
    if (this.backend && this.backend.state === "starting") { st.text = "$(loading~spin) SWE-X: starting"; st.tooltip = "Starting the SWE-Xplorer backend"; return; }
    if (this.backend && this.backend.state === "failed") { st.text = "$(error) SWE-X"; st.tooltip = this.backend.error; return; }
    if (this.isRunning()) {
      const p = this.phase || {};
      st.text = `$(loading~spin) SWE-X: ${p.phase || "working"}`;
      st.tooltip = `${p.phase || ""}${p.detail ? " · " + p.detail : ""}\nClick to open the chat`;
      return;
    }
    const t = this.lastTurn();
    if (t && t.status === "Submitted" && !t.decision) { st.text = "$(diff) SWE-X: review result"; st.tooltip = "A result is waiting for your review"; return; }
    st.text = "$(type-hierarchy) SWE-X"; st.tooltip = "SWE-Xplorer";
  }

  // -- state for the webview ------------------------------------------------------------------------------------------
  models() {
    const keys = ((this.info && this.info.keys) || []).filter((k) => k.set).map((k) => k.name);
    return PROVIDERS.filter((p) => keys.includes(p.key)).map((p) => ({ id: p.id, label: p.label, models: p.models }));
  }

  selectedModel() {
    const saved = this.context.globalState.get("sweXplorer.model");
    const avail = this.models();
    if (saved && avail.some((p) => p.id === saved.provider)) return saved;
    const m = (this.config && this.config.data && this.config.data.model) || {};
    const p = PROVIDERS.find((x) => (m.model_class === "openrouter" ? x.id === "openrouter" : x.prefix && (m.model_name || "").startsWith(x.prefix)));
    if (p && avail.some((a) => a.id === p.id)) return { provider: p.id, model: m.model_name };
    if (avail.length) return { provider: avail[0].id, model: avail[0].models[0] };
    return null;
  }

  snapshot() {
    const root = this.workspaceRoot();
    const b = this.backend;
    return {
      type: "state",
      server: b ? b.state : "stopped",
      serverError: b ? b.error : "",
      workspace: root ? path.basename(root) : null,
      sessionRepo: this.session ? path.basename(this.session.repo || "") : null,
      otherRepo: !!(this.session && root && norm(this.session.repo) !== norm(root)),
      session: this.session,
      status: this.runStatus,
      phase: this.phase,
      trace: this.liveTrace,
      models: this.models(),
      model: this.selectedModel(),
      autoAccept: this.autoAccept(),
      configName: this.config ? this.config.name : null,
      context: this.context_,
      recent: this.recentChats(),
    };
  }
  push() { this.post(this.snapshot()); }
  post(msg) { for (const v of this.views) v.post(msg); }

  // -- actions --------------------------------------------------------------------------------------------------------
  /**
   * @param {{replaceLast?: boolean}} opts  replaceLast: an edit or retry of the latest request. A result of it that is
   *   still waiting for review is rejected first (its changes are reverted), so the new attempt starts from your code.
   */
  async send(text, attachContext, opts = {}) {
    try {
      await this.ensureBackend();
      const root = this.workspaceRoot();
      if (!root) throw new Error("Open a folder (a git repository) to work in.");
      if (this.isRunning()) throw new Error("A run is in progress. Stop it first.");
      const last = this.lastTurn();
      if (opts.replaceLast && last && last.status === "Submitted" && !last.decision) {
        const r = await this.backend.post("/api/decide", { turn: last.n, decision: "reject" });
        if (!r.ok) throw new Error(r.message || "Could not reject the previous result.");
      }
      if (this.session && norm(this.session.repo) !== norm(root)) await this.backend.post("/api/session/new");
      const sel = this.selectedModel();
      if (!sel) throw new Error("No model is available: set an API key first.");
      const prov = PROVIDERS.find((p) => p.id === sel.provider);
      const config = JSON.parse(JSON.stringify(this.config.data));
      config.model = { ...(config.model || {}), model_class: prov.cls, model_name: sel.model };
      if (config.model.model_kwargs && config.model.model_kwargs.api_base) delete config.model.model_kwargs.api_base;
      const message = attachContext && this.context_ ? withContext(text, this.context_) : text;
      await this.backend.post("/api/chat", {
        message, repo: root, workspace_mode: "inplace", include_uncommitted: true, runs_dir: "",
        config, config_path: this.config.path, reward_same_as_policy: !!this.settings().get("rewardSameAsPolicy"),
        auto_accept: this.autoAccept(), python: "",
      });
      this.context_ = null;
      this.liveTrace = [];
      this.runStatus = { state: "running" };
      await this.refreshSession();
      this.renderStatus();
      return true;
    } catch (e) {
      this.error(e);
      return false;
    }
  }

  /** Run the latest request again (rejecting its result first if it is still waiting for review). */
  async retry(n) {
    const t = this.lastTurn();
    if (!t || t.n !== n) return this.error(new Error("Only the latest request can be retried."));
    return this.send(t.user, false, { replaceLast: true });
  }

  async stop() { try { await this.backend.post("/api/control", { action: "stop" }); } catch (e) { this.error(e); } }

  async decide(n, decision) {
    try {
      const r = await this.backend.post("/api/decide", { turn: n, decision });
      if (r.message) vscode.window.setStatusBarMessage(`SWE-Xplorer: ${r.message}`, 5000);
      await this.refreshSession();
      this.renderStatus();
    } catch (e) { this.error(e); }
  }

  async newChat() {
    try {
      await this.ensureBackend();
      if (this.isRunning()) throw new Error("Stop the current run first.");
      await this.backend.post("/api/session/new");
      this.liveTrace = []; this.phase = null; this.context_ = null;
      await this.loadRecent();
      await this.refreshSession();
      this.renderStatus();
    } catch (e) { this.error(e); }
  }

  async history() {
    try {
      await this.ensureBackend();
      const { sessions } = await this.backend.get("/api/sessions");
      if (!sessions.length) return vscode.window.showInformationMessage("No earlier SWE-Xplorer chats yet.");
      const pick = await vscode.window.showQuickPick(sessions.map((s) => ({
        label: s.title, description: `${path.basename(s.repo || "")} · ${s.turns} request${s.turns === 1 ? "" : "s"}`,
        detail: new Date(s.updated * 1000).toLocaleString(), dir: s.dir, picked: s.current,
      })), { placeHolder: "Open an earlier chat" });
      if (pick) await this.openSession(pick.dir);
    } catch (e) { this.error(e); }
  }

  async openSession(dir) {
    try {
      await this.ensureBackend();
      if (this.isRunning()) throw new Error("Stop the current run first.");
      const r = await this.backend.post("/api/session/open", { dir });
      this.session = r.session; this.liveTrace = []; this.phase = null;
      this.push();
    } catch (e) { this.error(e); }
  }

  async setApiKey() {
    try {
      await this.ensureBackend();
      const keys = (this.info && this.info.keys) || [];
      const items = PROVIDERS.map((p) => {
        const k = keys.find((x) => x.name === p.key);
        return { label: p.label, description: p.key, detail: k && k.set ? `set (${k.preview})` : "not set", key: p.key };
      });
      const pick = await vscode.window.showQuickPick(items, { placeHolder: "Which provider's API key?" });
      if (!pick) return;
      const value = await vscode.window.showInputBox({ prompt: `${pick.key} (saved in the SWE-Xplorer app folder; empty to remove)`, password: true, ignoreFocusOut: true });
      if (value === undefined) return;
      await this.backend.post("/api/keys", { keys: { [pick.key]: value.trim() }, remember: true });
      await this.refresh();
    } catch (e) { this.error(e); }
  }

  setModel(provider, model) {
    this.context.globalState.update("sweXplorer.model", { provider, model });
    this.push();
  }

  /** Auto-accept is the backend's preference, shared with the web GUI's "Auto" toggle. */
  autoAccept() { return !!(this.info && this.info.prefs && this.info.prefs.autoAccept); }
  async setAutoAccept(v) {
    try {
      await this.ensureBackend();
      const r = await this.backend.post("/api/prefs", { autoAccept: !!v });
      if (this.info) this.info.prefs = r.prefs || {};
      this.push();
    } catch (e) { this.error(e); }
  }

  attachSelection() {
    const ed = vscode.window.activeTextEditor;
    if (!ed) return vscode.window.showInformationMessage("Open a file and select some code first.");
    const doc = ed.document;
    const range = ed.selection.isEmpty ? doc.lineAt(ed.selection.active.line).range : ed.selection;
    const folder = vscode.workspace.getWorkspaceFolder(doc.uri);
    const rel = folder ? path.relative(folder.uri.fsPath, doc.uri.fsPath).split(path.sep).join("/") : path.basename(doc.uri.fsPath);
    let text = doc.getText(range);
    if (text.length > 6000) text = text.slice(0, 6000) + "\n… (truncated)";
    const diags = vscode.languages.getDiagnostics(doc.uri).filter((d) => d.range.intersection(range))
      .map((d) => `${["error", "warning", "info", "hint"][d.severity]}: ${d.message} (line ${d.range.start.line + 1})`);
    this.context_ = { file: rel, start: range.start.line + 1, end: range.end.line + 1, text, lang: doc.languageId, diagnostics: diags };
    this.push();
    focusChat().then(() => this.post({ type: "focusInput" }));
  }
  clearContext() { this.context_ = null; this.push(); }

  // -- review ---------------------------------------------------------------------------------------------------------
  turn(n) { return ((this.session && this.session.turns) || []).find((t) => t.n === n); }

  beforeUri(n, rel) { return vscode.Uri.from({ scheme: BEFORE_SCHEME, path: "/" + rel, query: `turn=${n}` }); }
  fileUri(rel) { return vscode.Uri.file(path.join(this.session.repo, ...rel.split("/"))); }

  async review(n) {
    const t = this.turn(n);
    if (!t || !(t.files || []).length) return vscode.window.showInformationMessage("This result has no file changes.");
    const resources = t.files.map((f) => [this.fileUri(f), this.beforeUri(n, f), this.fileUri(f)]);
    try {
      await vscode.commands.executeCommand("vscode.changes", `SWE-Xplorer · request ${n}`, resources);
    } catch {
      await this.openFileDiff(n, t.files[0]);
    }
  }

  async openFileDiff(n, rel) {
    await vscode.commands.executeCommand("vscode.diff", this.beforeUri(n, rel), this.fileUri(rel), `${rel} (SWE-Xplorer · request ${n})`);
  }

  async provideBefore(uri) {
    const n = Number(new URLSearchParams(uri.query).get("turn"));
    const rel = uri.path.replace(/^\//, "");
    const t = this.turn(n);
    if (!t || !this.session) return "";
    const file = path.join(this.session.repo, ...rel.split("/"));
    const current = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : null;
    return beforeContent(t.turn_patch || t.patch || "", rel, current);
  }

  // -- search tree (the full web GUI in an editor tab) -----------------------------------------------------------------
  async openTree() {
    try {
      await this.ensureBackend();
      if (this.treePanel) { this.treePanel.reveal(); return; }
      const p = vscode.window.createWebviewPanel("sweXplorer.tree", "SWE-Xplorer · Search Tree", vscode.ViewColumn.Active, { enableScripts: true, retainContextWhenHidden: true });
      p.iconPath = vscode.Uri.joinPath(this.context.extensionUri, "media", "logo.svg");
      const url = this.backend.baseUrl + "/";
      p.webview.html = `<!DOCTYPE html><html><head><meta charset="utf-8">
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src http://127.0.0.1:* http://localhost:*; style-src 'unsafe-inline';">
        <style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100%;overflow:hidden;background:#0f1117}</style></head>
        <body><iframe src="${url}" allow="clipboard-read; clipboard-write"></iframe></body></html>`;
      p.onDidDispose(() => { this.treePanel = null; });
      this.treePanel = p;
    } catch (e) { this.error(e); }
  }

  error(e) {
    const msg = (e && e.message) || String(e);
    this.log(`Error: ${msg}`);
    this.post({ type: "error", message: msg });
    vscode.window.showErrorMessage(`SWE-Xplorer: ${msg}`);
  }

  async restart() {
    if (this.backend) await this.backend.dispose();
    this.backend = null;
    try { await this.ensureBackend(); } catch (e) { this.error(e); }
  }

  async dispose() {
    if (this.backend) await this.backend.dispose();
  }
}

function norm(p) { return path.resolve(p || "").replace(/[\\/]+$/, "").toLowerCase(); }

function withContext(text, c) {
  let s = `${text}\n\n<code_context file="${c.file}" lines="${c.start}-${c.end}">\n${c.text}\n</code_context>`;
  if (c.diagnostics && c.diagnostics.length) s += `\n<diagnostics>\n${c.diagnostics.join("\n")}\n</diagnostics>`;
  return s;
}

// ======================================================================================================================
// Sidebar webview
// ======================================================================================================================
class ChatViewProvider {
  constructor(context, controller) { this.context = context; this.ctl = controller; this.webview = null; }

  resolveWebviewView(view) {
    this.webview = view.webview;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    const nonce = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
    const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    const js = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    const logo = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "logo.svg"));
    view.webview.html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}'; img-src ${view.webview.cspSource} data:;">
      <meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="${css}"></head>
      <body data-logo="${logo}"><div id="app"></div><script nonce="${nonce}" src="${js}"></script></body></html>`;
    view.webview.onDidReceiveMessage((m) => this.onMessage(m));
    view.onDidChangeVisibility(() => { if (view.visible) this.ctl.push(); });
    this.ctl.views.add(this);
  }

  post(msg) { if (this.webview) this.webview.postMessage(msg); }

  async onMessage(m) {
    const c = this.ctl;
    switch (m.type) {
      case "ready": c.push(); c.ensureBackend().catch(() => {}); break;
      case "send": { const ok = await c.send(m.text, m.attach, { replaceLast: !!m.replaceLast }); this.post({ type: "sent", ok }); break; }
      case "retry": { const ok = await c.retry(m.n); this.post({ type: "sent", ok, keepInput: true }); break; }
      case "stop": c.stop(); break;
      case "decide": c.decide(m.n, m.decision); break;
      case "review": c.review(m.n); break;
      case "openDiff": c.openFileDiff(m.n, m.file); break;
      case "openFile": vscode.window.showTextDocument(c.fileUri(m.file)); break;
      case "newChat": c.newChat(); break;
      case "history": c.history(); break;
      case "openSession": c.openSession(m.dir); break;
      case "openTree": c.openTree(); break;
      case "setModel": c.setModel(m.provider, m.model); break;
      case "setAutoAccept": c.setAutoAccept(m.value); break;
      case "setApiKey": c.setApiKey(); break;
      case "clearContext": c.clearContext(); break;
      case "restartBackend": c.restart(); break;
      case "showLog": c.output.show(); break;
    }
  }
}

// ======================================================================================================================
/** VS Code 1.106+ lets extensions contribute to the secondary sidebar (next to Chat, Codex, Claude Code). */
function supportsSecondarySidebar() {
  const [major, minor] = vscode.version.split(".").map(Number);
  return major > 1 || (major === 1 && minor >= 106);
}
const chatViewId = () => (supportsSecondarySidebar() ? "sweXplorer.chatSecondary" : "sweXplorer.chat");
const focusChat = () => vscode.commands.executeCommand(`${chatViewId()}.focus`);

function activate(context) {
  vscode.commands.executeCommand("setContext", "sweXplorer.noSecondarySidebar", !supportsSecondarySidebar());
  ctl = new Controller(context);
  const reg = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  context.subscriptions.push(
    ctl.output, ctl.status,
    ...["sweXplorer.chat", "sweXplorer.chatSecondary"].map((id) =>
      vscode.window.registerWebviewViewProvider(id, new ChatViewProvider(context, ctl), { webviewOptions: { retainContextWhenHidden: true } })),
    vscode.workspace.registerTextDocumentContentProvider(BEFORE_SCHEME, { provideTextDocumentContent: (uri) => ctl.provideBefore(uri) }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("sweXplorer.config") && ctl.backend && ctl.backend.state === "ready") ctl.loadConfig().catch((x) => ctl.error(x));
    }),
  );
  reg("sweXplorer.focus", () => focusChat());
  reg("sweXplorer.newChat", () => ctl.newChat());
  reg("sweXplorer.history", () => ctl.history());
  reg("sweXplorer.openTree", () => ctl.openTree());
  reg("sweXplorer.askAboutSelection", () => ctl.attachSelection());
  reg("sweXplorer.stop", () => ctl.stop());
  reg("sweXplorer.setApiKey", () => ctl.setApiKey());
  reg("sweXplorer.restartServer", () => ctl.restart());
  reg("sweXplorer.showLog", () => ctl.output.show());
}

async function deactivate() {
  if (ctl) await ctl.dispose();
}

module.exports = { activate, deactivate, withContext, PROVIDERS };
