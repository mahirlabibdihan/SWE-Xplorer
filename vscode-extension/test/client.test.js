// End-to-end check of the extension's backend client against the real Python server, with the offline scripted
// model from tests/gui. Run from vscode-extension/:  npm test   (needs Python with SWE-Xplorer + git)
"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");
const { Backend, splitCommand } = require("../lib/backend");
const { beforeContent, patchFiles } = require("../lib/review");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const ORIGINAL = "def add(a, b):\n    return a - b\n";
const lf = (s) => s.replace(/\r\n/g, "\n"); // line endings follow the user's core.autocrlf

async function main() {
  // unit: command splitting
  assert.deepStrictEqual(splitCommand("py -3.12"), ["py", "-3.12"]);
  assert.deepStrictEqual(splitCommand('"C:\\Program Files\\Python312\\python.exe"'), ["C:\\Program Files\\Python312\\python.exe"]);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "swex-ext-"));
  process.env.MSWEA_GLOBAL_CONFIG_DIR = path.join(tmp, "app"); // keep the user's real app folder untouched
  const repo = path.join(tmp, "demo_repo");
  fs.mkdirSync(repo);
  fs.writeFileSync(path.join(repo, "calc.py"), ORIGINAL);
  const git = (...a) => execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", ...a], { cwd: repo });
  git("init", "-q"); git("add", "-A"); git("commit", "-qm", "init");

  const b = new Backend({ python: process.env.SWEX_PYTHON || "", port: 8830, cwd: REPO_ROOT, log: () => {} });
  const seen = {};
  let finished = null;
  b.on("event", (e) => { seen[e.type] = (seen[e.type] || 0) + 1; if (e.type === "finished") finished = e.data; });
  try {
    await b.start();
    assert.strictEqual(b.state, "ready");
    const info = await b.get("/api/info");
    assert.ok(Array.isArray(info.keys));

    // auto-accept is one shared preference: a change reaches every client as a "prefs" event
    const prefsEvt = new Promise((resolve) => b.on("event", (e) => e.type === "prefs" && resolve(e.data.prefs)));
    await new Promise((r) => setTimeout(r, 300)); // let the event stream connect
    await b.post("/api/prefs", { autoAccept: true });
    assert.strictEqual((await prefsEvt).autoAccept, true, "prefs event");
    assert.strictEqual((await b.get("/api/info")).prefs.autoAccept, true);
    await b.post("/api/prefs", { autoAccept: false });

    const cfg = (await b.get("/api/config?path=swe_xplorer.yaml")).data;
    const M = "tests.gui.test_gui_backend.ScriptedModel";
    Object.assign(cfg.agent, { step_limit: 12, itr_limit: 2, branching_factor: 2, sub_thres: 1 });
    cfg.model = { model_class: M, model_name: "scripted" };
    cfg.reward_model = { model_class: M, model_name: "scripted", role: "reward" };

    await b.post("/api/chat", {
      message: "add() subtracts instead of adding", repo, workspace_mode: "inplace", include_uncommitted: true,
      runs_dir: path.join(tmp, "runs"), config: cfg, reward_same_as_policy: false, auto_accept: false, python: "",
    });
    const t0 = Date.now();
    let sawTrace = false;
    while (!finished && Date.now() - t0 < 300000) {
      await new Promise((r) => setTimeout(r, 400));
      if (!sawTrace) sawTrace = ((await b.get("/api/trace")).trace || []).some((e) => e.kind === "step");
    }
    assert.ok(finished, "run did not finish");
    assert.strictEqual(finished.exit_status, "Submitted", JSON.stringify(finished));
    assert.ok(seen.snapshot > 0 && seen.phase > 0, `events: ${JSON.stringify(seen)}`);
    assert.ok(sawTrace, "live trace was never available");

    const { session } = await b.get("/api/session");
    const turn = session.turns[0];
    assert.deepStrictEqual(turn.files, ["calc.py"]);
    assert.deepStrictEqual(patchFiles(turn.turn_patch), ["calc.py"]);
    const now = fs.readFileSync(path.join(repo, "calc.py"), "utf8");
    assert.ok(now.includes("a + b"), "the fix is in the working tree");
    const before = await beforeContent(turn.turn_patch, "calc.py", now);
    assert.strictEqual(lf(before), ORIGINAL, "review: before side");
    assert.strictEqual(before.includes("\r\n"), now.includes("\r\n"), "review: keeps the file's line endings");

    const r = await b.post("/api/decide", { turn: turn.n, decision: "reject" });
    assert.ok(r.ok, JSON.stringify(r));
    assert.strictEqual(lf(fs.readFileSync(path.join(repo, "calc.py"), "utf8")), ORIGINAL, "reject restores the file");
    const branch = execFileSync("git", ["symbolic-ref", "--short", "HEAD"], { cwd: repo }).toString().trim();
    assert.ok(branch, "back on a branch");

    // retry / edit (what the sidebar does): the rejected result stays in the chat, the request runs again
    finished = null;
    await b.post("/api/chat", {
      message: "add() subtracts instead of adding", repo, workspace_mode: "inplace", include_uncommitted: true,
      runs_dir: path.join(tmp, "runs"), config: cfg, reward_same_as_policy: false, auto_accept: false, python: "",
    });
    const t1 = Date.now();
    while (!finished && Date.now() - t1 < 300000) await new Promise((res) => setTimeout(res, 400));
    assert.strictEqual(finished && finished.exit_status, "Submitted", "retry finished");
    const s2 = (await b.get("/api/session")).session;
    assert.deepStrictEqual(s2.turns.map((t) => [t.n, t.decision || null]), [[1, "rejected"], [2, null]]);
    assert.deepStrictEqual(s2.turns[1].files, ["calc.py"], "the retry produced a new result");
    assert.ok((await b.post("/api/decide", { turn: 2, decision: "reject" })).ok);
    console.log(`ok - events ${JSON.stringify(seen)}, ${Math.round((Date.now() - t0) / 1000)}s`);
  } finally {
    await b.dispose();
    await new Promise((r) => setTimeout(r, 800));
    fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 3 });
  }
}

main().catch((e) => { console.error("FAIL", e); process.exit(1); });
