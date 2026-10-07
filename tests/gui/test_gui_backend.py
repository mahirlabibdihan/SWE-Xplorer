"""Tests for the web GUI backend: local bash environment, sandbox workspace and an end-to-end tree search run."""

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from minisweagent.config import builtin_config_dir
from minisweagent.gui.environment import LocalBashEnvironment, find_bash, prepare_workspace, repo_is_dirty

pytestmark = pytest.mark.skipif(find_bash() is None, reason="bash not available")


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    """Never touch the real app folder (settings, keys.env, config draft) from tests."""
    from minisweagent.gui import runner

    app = tmp_path / "app_dir"
    for name, path in {"APP_DIR": app, "SETTINGS_FILE": app / "settings.json", "KEYS_FILE": app / "keys.env",
                       "DRAFT_FILE": app / "config_draft.json", "LEGACY_SETTINGS_FILE": app / "legacy.json",
                       "DEFAULT_RUNS_DIR": app / "runs", "LEGACY_RUNS_DIR": app / "legacy_runs"}.items():
        monkeypatch.setattr(runner, name, path)


REPRO_CMD = """cat <<'EOF' > run_test.sh
python3 - <<'PY'
import json, calc
st = "PASSED" if calc.add(2, 3) == 5 else "FAILED"
json.dump({"tests": [{"name": "test_add", "status": st}]}, open("test_status.json", "w"))
PY
EOF
bash run_test.sh && cat test_status.json"""


class ScriptedModel:
    """Offline stand-in for both the policy and the reward model (selected with `role`)."""

    def __init__(self, role: str = "policy", model_name: str = "scripted", flaky: bool = False, **kwargs):
        self.role = role
        self.flaky = flaky  # every 3rd call answers like a content-safety classifier (as openrouter/free may)
        self.config = type("Cfg", (), {"model_name": model_name})()
        self.cost, self.n_calls, self.input_tokens, self.output_tokens = 0.0, 0, 0, 0

    def query(self, messages: list[dict], **kwargs) -> dict:
        self.n_calls += 1
        if self.flaky and self.n_calls % 3 == 0:
            return {"content": "User Safety: safe\nResponse Safety: safe", "extra": {"response": {"model": "nvidia/content-safety"}}}
        text = "\n".join(str(m.get("content", "")) for m in messages)
        if self.role == "reward":
            return {"content": f"<score>{60 + (self.n_calls * 7) % 35}</score>"}
        if "<solution_1>" in text:
            return {"content": 'Both fine.\n```json\n{"verdict": 1}\n```'}
        if "<candidate_solution>" in text:
            return {"content": "Changes subtraction to addition in calc.add."}
        depth = sum(1 for m in messages if m["role"] == "assistant")
        if "Test Reproduction Instructions" in text:  # the reproducer (config: reproduction)
            if depth == 0:
                cmd, kind = REPRO_CMD, "TEST"
            else:
                cmd, kind = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "SUBMIT"
            return {"content": f"THOUGHT: reproduce, step {depth}.\nCOMMAND_TYPE: [{kind}]\n\n```bash\n{cmd}\n```", "extra": {}}
        follow_up = "<new_request>" in text
        edit = "echo 'def sub(a, b):\n    return a - b' >> calc.py" if follow_up else "sed -i 's/a - b/a + b/' calc.py"
        if depth == 0:
            cmd, kind = ("cat calc.py", "READ") if self.n_calls % 2 else ("grep -n 'def add' calc.py", "SEARCH")
        elif depth == 1:
            cmd, kind = edit, "EDIT"
        elif depth == 2:
            cmd, kind = "python3 -c 'import calc; print(calc.add(2, 3))'", "TEST"
        else:
            cmd, kind = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "SUBMIT"
        thought = f"step {depth}: {'the follow-up asks for sub()' if follow_up else 'add() must return the sum'}"
        return {"content": f"THOUGHT: {thought}, so I run this.\nCOMMAND_TYPE: [{kind}]\n\n```bash\n{cmd}\n```", "extra": {}}

    def get_template_vars(self) -> dict[str, Any]:
        return {"model_name": "scripted"}


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "demo_repo"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", newline="\n")
    _git("init", "-q", cwd=r)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=r)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=r)
    return r


def test_bash_environment_basics(tmp_path):
    env = LocalBashEnvironment(cwd=str(tmp_path), timeout=20)
    out = env.execute("echo hello && pwd")
    assert out["returncode"] == 0 and out["output"].startswith("hello")
    out = env.execute("python3 - << 'EOF'\nimport sys\nprint('py', sys.version_info[0])\nEOF")
    assert out["output"].strip() == "py 3"
    out = env.execute("cat <<'EOF' > f.txt\nline1\nEOF\nsed -i 's/line1/LINE/' f.txt && nl -ba f.txt")
    assert "LINE" in out["output"]
    assert env.execute("exit 3")["returncode"] == 3
    env.cleanup()


def test_bash_environment_timeout_returns_bytes(tmp_path):
    env = LocalBashEnvironment(cwd=str(tmp_path), timeout=2)
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired) as e:
        env.execute("echo started; sleep 30")
    assert time.time() - t0 < 20
    assert isinstance(e.value.output, bytes)  # the agents call e.output.decode()
    env.cleanup()


def test_global_git_config_is_rewritten(repo):
    env = LocalBashEnvironment(cwd=str(repo))
    cmd = env._rewrite('git config --global user.name "x" && git config --global user.email "y" && ls')
    assert "git config" not in cmd and cmd.rstrip().endswith("&& ls")  # global identity writes become no-ops
    env.cleanup()


def test_clone_workspace_leaves_source_untouched(repo, tmp_path):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # wip\n", newline="\n")
    (repo / "notes.txt").write_text("untracked\n")
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    ws = prepare_workspace(repo, tmp_path / "run", mode="clone", include_uncommitted=True, log=lambda *_: None)
    assert ws != repo
    assert "# wip" in (ws / "calc.py").read_text()
    assert (ws / "notes.txt").exists()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == before
    assert repo_is_dirty(ws)


def test_end_to_end_run(repo, tmp_path, monkeypatch):
    from minisweagent.gui.runner import RunManager

    monkeypatch.setattr("minisweagent.gui.runner.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("MSWEA_RETRIEVAL_CACHE_DIR", str(tmp_path / "retrieval"))
    monkeypatch.delenv("SENTENCE_TRANSFORMER_SERVER", raising=False)
    config = yaml.safe_load((builtin_config_dir / "extra" / "swe_xplorer.yaml").read_text(encoding="utf-8"))
    config["agent"].update(step_limit=12, itr_limit=2, branching_factor=2, sub_thres=1)
    config["model"] = {"model_class": "tests.gui.test_gui_backend.ScriptedModel", "model_name": "scripted"}
    config["reward_model"] = {"model_class": "tests.gui.test_gui_backend.ScriptedModel", "model_name": "scripted", "role": "reward"}

    m = RunManager()
    m.start({"repo": str(repo), "task": "add() subtracts instead of adding", "config": config,
             "workspace_mode": "clone", "runs_dir": str(tmp_path / "runs")})
    m.thread.join(timeout=300)
    assert not m.running, "run did not finish"
    assert m.result["exit_status"] == "Submitted", m.result
    assert "+    return a + b" in m.result["patch"]
    assert "return a - b" in (repo / "calc.py").read_text()  # source repo untouched
    run_dir = Path(m.result["run_dir"])
    assert (run_dir / "run.tree.json").exists() and (run_dir / "patch.diff").exists()

    snaps = m.bus.snapshots
    assert len(snaps) > 5
    last = snaps[-1]
    assert last["phase"] == "Finished"
    assert any(n["ex"] for n in last["nodes"]) and any(n["term"] for n in last["nodes"])
    assert m.node(last["current"])["command"]

    turn1 = m.session["turns"][0]
    assert turn1["response"] and "THOUGHT" not in turn1["response"] and "```" not in turn1["response"]
    steps = [e for e in m.trace() if e["kind"] == "step"]
    assert steps and steps[0]["thought"] and steps[-1]["term"]
    winner1 = turn1["winner"]

    # ---- review: a result must be accepted or rejected before the next request
    assert turn1["decision"] is None
    with pytest.raises(RuntimeError, match="Accept or reject"):
        m.start({"message": "next", "config": config})
    ok, msg = m.decide(1, "accept")
    assert ok, msg
    assert "return a + b" in (repo / "calc.py").read_text()  # accepted = applied to the repo

    # ---- the user edits the code by hand before the next request
    (repo / "calc.py").write_text("# edited by hand\n" + (repo / "calc.py").read_text(), newline="\n")
    (repo / "notes.txt").write_text("untracked user file\n")

    # ---- follow-up: starts from the user's *current* codebase, hangs under the accepted submission node
    m.start({"message": "Also add a sub(a, b) function", "config": config})
    m.thread.join(timeout=300)
    assert m.result["exit_status"] == "Submitted", m.result
    turn2 = m.session["turns"][1]
    assert turn2["mode"] == "continue" and turn2["request"] == 2
    request_root = m.agent.gui_nodes[turn2["request_root"]]
    assert request_root.parent.id == winner1  # new pseudo-root hangs under the accepted submission
    ws = m.session["workspace"]
    from minisweagent.gui.environment import git_run

    start_code = git_run(ws, "show", f"{request_root.commit}:calc.py")
    assert start_code.startswith("# edited by hand") and "return a + b" in start_code  # manual edit included
    assert git_run(ws, "show", f"{request_root.commit}:notes.txt").strip() == "untracked user file"
    assert "def sub" in turn2["turn_patch"] and "+    return a + b" not in turn2["turn_patch"]  # relative to request start
    assert "<new_request>" in (Path(turn2["run_dir"]) / "task.md").read_text(encoding="utf-8")
    assert m.agent.gui_nodes[winner1].observation.startswith("The user added a new task")

    # git graph: every commit referenced by the tree is listed, even those not reachable from a ref
    g = m.git_graph()
    assert g["available"]
    hashes = {c["h"] for c in g["commits"]}
    node_commits = {n.commit for n in m.agent.gui_nodes.values() if n.commit}
    assert node_commits <= hashes and g["head"] in hashes
    kinds = [c["kind"] for c in g["commits"]]
    assert "baseline" in kinds and "edit" in kinds and kinds.count("root") == 1  # request 1 starts at the baseline, request 2 at its sync commit
    edit = next(c for c in g["commits"] if c["kind"] == "edit")
    assert edit["node"]["type"] == "edit" and edit["stat"] and "calc.py" in m.git_show(edit["h"])

    ok, msg = m.decide(2, "accept")  # only request 2's own patch, on top of the user's edited code
    assert ok, msg
    code = (repo / "calc.py").read_text()
    assert code.startswith("# edited by hand") and code.count("return a + b") == 1 and code.count("def sub") == 1

    # ---- reject: nothing is applied; the next request again starts from the repo and hangs under turn 2
    m.start({"message": "Rename sub to subtract", "config": config})
    m.thread.join(timeout=300)
    assert m.result["exit_status"] == "Submitted", m.result
    ok, _ = m.decide(3, "reject")
    assert ok and (repo / "calc.py").read_text() == code
    m.start({"message": "Add a docstring to add()", "config": config})
    m.thread.join(timeout=300)
    turn4 = m.session["turns"][3]
    assert turn4["mode"] == "continue"
    assert m.agent.gui_nodes[turn4["request_root"]].parent.id == turn2["winner"]
    assert "<rejected_attempts>" in (Path(turn4["run_dir"]) / "task.md").read_text(encoding="utf-8")

    # ---- auto-accept: applied as soon as the request finishes
    m.decide(4, "reject")
    m.start({"message": "One more change", "config": config, "auto_accept": True})
    m.thread.join(timeout=300)
    turn5 = m.session["turns"][4]
    assert turn5["decision"] == "accepted" and turn5["applied"]

    # the saved tree (both requests) can be re-opened in the viewer, without ending the session
    import json

    m.load_tree(json.loads((Path(turn5["run_dir"]) / "run.tree.json").read_text(encoding="utf-8")), "run.tree.json")
    assert len(m.bus.last_snapshot["nodes"]) == len(m.agent.gui_nodes)
    assert m.agent is not None and m.agent.can_continue()


def test_server_security_without_token(tmp_path, monkeypatch):
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer

    from minisweagent.gui import server

    monkeypatch.setattr("minisweagent.gui.runner.SETTINGS_FILE", tmp_path / "settings.json")
    app = server.App("127.0.0.1", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()

    try:
        assert req("GET", "/")[0] == 200  # no token needed
        assert req("GET", "/api/info")[0] == 200
        assert req("GET", "/", {"Host": "evil.example:80"})[0] == 403  # DNS rebinding
        keys = {"keys": {"MY_TEST_API_KEY": "sk-secret-abc123"}}
        assert req("POST", "/api/keys", {"Origin": "https://evil.example", "Content-Type": "application/json"}, keys)[0] == 403
        status, body = req("POST", "/api/keys", {"Origin": f"http://127.0.0.1:{app.port}", "Content-Type": "application/json"}, keys)
        assert status == 200
        assert b"sk-secret-abc123" not in body  # values are never echoed back
    finally:
        httpd.shutdown()
        import os

        os.environ.pop("MY_TEST_API_KEY", None)



def test_non_git_folder_is_rejected(tmp_path):
    from minisweagent.gui.runner import repo_info

    (tmp_path / "plain").mkdir()
    info = repo_info(str(tmp_path / "plain"))
    assert not info["ok"] and "Not inside a git repository" in info["error"]


def test_swe_xplorer_config_is_valid():
    from jinja2 import StrictUndefined, Template

    from minisweagent.agents.tree_search_agent import TreeSearchAgentConfig

    cfg = yaml.safe_load((builtin_config_dir / "extra" / "swe_xplorer.yaml").read_text(encoding="utf-8"))
    agent = TreeSearchAgentConfig(**cfg["agent"])
    unknown = set(cfg["agent"]) - set(TreeSearchAgentConfig.model_fields)
    assert not unknown, f"keys the agent would ignore: {unknown}"
    assert "/testbed" not in yaml.safe_dump(cfg) and "SWE-bench" not in agent.instance_template
    assert cfg["reward_model"]["model_name"] == cfg["model"]["model_name"] == "openrouter/free"
    # free models report cost 0: without this the OpenRouter client raises on every call
    assert cfg["model"]["cost_tracking"] == cfg["reward_model"]["cost_tracking"] == "ignore_errors"
    from minisweagent.models.openrouter_model import OpenRouterModelConfig

    OpenRouterModelConfig(**{k: v for k, v in cfg["model"].items() if k != "model_class"})
    Template(agent.instance_template, undefined=StrictUndefined).render(task="demo task")
    Template(agent.voting_user_template, undefined=StrictUndefined).render(task="t", solution_1="a", solution_2="b")
    # ~10 expansions per iteration: the budget must cover all iterations plus the convergence phase
    assert agent.step_limit >= 10 * agent.itr_limit + 10


def test_inplace_keeps_the_users_branch_and_work(repo, tmp_path, monkeypatch):
    """Working directly in the user's repository: branch, history and uncommitted work are left as they were."""
    from minisweagent.gui.runner import RunManager

    monkeypatch.setattr("minisweagent.gui.runner.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("MSWEA_RETRIEVAL_CACHE_DIR", str(tmp_path / "retrieval"))
    monkeypatch.delenv("SENTENCE_TRANSFORMER_SERVER", raising=False)

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout.strip()

    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "notes.md").write_text("committed notes\n", newline="\n")
    _git("add", "-A", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "notes", cwd=repo)
    (repo / "notes.md").write_text("committed notes\nmy uncommitted line\n", newline="\n")  # user's own work
    (repo / "todo.txt").write_text("untracked user file\n")
    head, n_commits = git("rev-parse", "HEAD"), git("rev-list", "--count", "HEAD")

    config = yaml.safe_load((builtin_config_dir / "extra" / "swe_xplorer.yaml").read_text(encoding="utf-8"))
    config["agent"].update(step_limit=12, itr_limit=2, branching_factor=2, sub_thres=1)
    config["model"] = {"model_class": "tests.gui.test_gui_backend.ScriptedModel", "model_name": "scripted"}
    config["reward_model"] = {"model_class": "tests.gui.test_gui_backend.ScriptedModel", "model_name": "scripted", "role": "reward"}

    m = RunManager()
    m.start({"repo": str(repo), "task": "add() subtracts instead of adding", "config": config, "workspace_mode": "inplace",
             "runs_dir": str(tmp_path / "runs")})
    m.thread.join(timeout=300)
    assert m.result["exit_status"] == "Submitted", m.result

    def user_state_intact():
        assert git("symbolic-ref", "--short", "HEAD") == "feature"  # back on the branch, not a detached HEAD
        assert git("rev-parse", "HEAD") == head and git("rev-list", "--count", "HEAD") == n_commits  # no commits added
        assert (repo / "notes.md").read_text() == "committed notes\nmy uncommitted line\n"
        assert (repo / "todo.txt").read_text() == "untracked user file\n"
        assert "user" not in (repo / ".git" / "config").read_text()  # the agent's `git config` never wrote here

    user_state_intact()
    assert "return a + b" in (repo / "calc.py").read_text()  # the result, as an uncommitted edit
    assert "M calc.py" in [line.strip() for line in git("status", "--porcelain").splitlines()]

    ok, msg = m.decide(1, "reject")
    assert ok, msg
    user_state_intact()
    assert "return a - b" in (repo / "calc.py").read_text()  # exactly the agent's patch was reverted

    (repo / "newmod.py").write_text("def brand_new_function():\n    return 1\n", newline="\n")  # user adds a file
    m.start({"message": "Also add a sub(a, b) function", "config": config})
    m.thread.join(timeout=300)
    assert m.result["exit_status"] == "Submitted", m.result
    assert "newmod.py" in m.agent.file_ids  # the retrieval index was refreshed at the start of the request
    assert any("Retrieval index refreshed" in l["msg"] for l in m.bus.logs)
    (repo / "newmod.py").unlink()
    user_state_intact()
    assert "def sub" in (repo / "calc.py").read_text()
    ok, _ = m.decide(2, "accept")
    assert ok and "def sub" in (repo / "calc.py").read_text()


def test_stop_interrupts_a_slow_model_call_and_a_running_command(tmp_path):
    import threading

    from minisweagent.gui.observer import RunController, StopRequested

    ctrl = RunController(lambda *a: None)
    slow = ctrl.interruptible(lambda: time.sleep(30) or "late")
    threading.Timer(0.5, ctrl.stop).start()
    t0 = time.time()
    with pytest.raises(StopRequested):
        slow()  # e.g. an OpenRouter call stuck in its retry/backoff loop
    assert time.time() - t0 < 5

    env = LocalBashEnvironment(cwd=str(tmp_path), timeout=120)
    ctrl2 = RunController(lambda *a: None)
    ctrl2.on_stop.append(env.abort)
    threading.Timer(1.0, ctrl2.stop).start()
    t0 = time.time()
    try:
        env.execute("echo started; sleep 60")
    except subprocess.TimeoutExpired:
        pass
    assert time.time() - t0 < 20  # the command was killed, not waited for
    env.cleanup()


def test_run_refuses_to_start_without_the_provider_key(repo, tmp_path, monkeypatch):
    from minisweagent.gui.runner import RunManager, missing_key

    monkeypatch.setattr("minisweagent.gui.runner.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MSWEA_MODEL_API_KEY", raising=False)
    assert missing_key({"model_class": "openrouter", "model_name": "openrouter/free"}) == "OPENROUTER_API_KEY"
    assert missing_key({"model_class": "litellm", "model_name": "anthropic/claude-sonnet-5"}) in ("ANTHROPIC_API_KEY", None)
    assert missing_key({"model_class": "tests.gui.test_gui_backend.ScriptedModel", "model_name": "x"}) is None
    config = yaml.safe_load((builtin_config_dir / "extra" / "swe_xplorer.yaml").read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        RunManager().start({"repo": str(repo), "task": "t", "config": config})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert missing_key(config["model"]) is None


def test_app_folder_keeps_keys_draft_and_prefs_across_restarts(monkeypatch):
    import os

    from minisweagent.gui import runner

    monkeypatch.delenv("SWEX_TEST_API_KEY", raising=False)
    m = runner.RunManager()
    runner.apply_keys(m.settings, {"SWEX_TEST_API_KEY": "sk-persist-123"}, remember=True)
    assert "SWEX_TEST_API_KEY=sk-persist-123" in runner.KEYS_FILE.read_text(encoding="utf-8")

    os.environ.pop("SWEX_TEST_API_KEY")  # simulate a server restart: the process environment is gone
    runner.RunManager()
    assert os.environ.get("SWEX_TEST_API_KEY") == "sk-persist-123"  # loaded back from keys.env

    runner.apply_keys(m.settings, {"SWEX_TEST_API_KEY": ""})  # Clear = unset and forget
    assert "SWEX_TEST_API_KEY" not in os.environ and "SWEX_TEST_API_KEY" not in runner.read_keys_file()
    runner.apply_keys(m.settings, {"SWEX_TEST_API_KEY": "sk-session"}, remember=False)
    assert "SWEX_TEST_API_KEY" not in runner.read_keys_file()  # not remembered
    os.environ.pop("SWEX_TEST_API_KEY", None)

    runner.save_draft({"path": "x.yaml", "name": "x.yaml", "data": {"agent": {"step_limit": 7}}, "dirty": True})
    assert runner.load_draft()["data"]["agent"]["step_limit"] == 7
    runner.save_draft({"path": "x.yaml", "data": {"agent": {}}, "dirty": False})  # saved/reset: no draft any more
    assert runner.load_draft() is None and runner.APP_DIR.exists()


def test_code_fingerprint_tracks_content_not_commits(repo):
    from minisweagent.gui.observer import code_fingerprint

    fp0, clean = code_fingerprint(repo)
    assert clean
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", newline="\n")
    fp1, clean = code_fingerprint(repo)
    assert not clean and fp1 != fp0
    (repo / "extra.py").write_text("x = 1\n")
    fp2, _ = code_fingerprint(repo)
    assert fp2 != fp1  # a new untracked file counts
    (repo / "extra.py").unlink()
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fix", cwd=repo)
    fp3, clean = code_fingerprint(repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "same code", cwd=repo)
    assert clean and code_fingerprint(repo)[0] == fp3  # a new commit with identical content is not a change


def test_unusable_replies_are_discarded_and_reasked(repo, tmp_path, monkeypatch):
    from minisweagent.gui.observer import reply_problem
    from minisweagent.gui.runner import RunManager

    rx = r"```bash\s*\n(.*?)\n```"
    act = [{"role": "system", "content": "... COMMAND_TYPE ..."}, {"role": "user", "content": "task"}]
    assert reply_problem("policy", act, "User Safety: safe\nResponse Safety: safe", rx)
    assert reply_problem("policy", act, "THOUGHT: hmm, no command here", rx)
    assert reply_problem("policy", act, "THOUGHT: ok\n```bash\nls\n```", rx) is None
    assert reply_problem("reward", [], "User Safety: safe", rx) and reply_problem("reward", [], "<score>70</score>", rx) is None
    vote = [{"role": "user", "content": "<solution_1>a</solution_1>"}]
    assert reply_problem("policy", vote, "I prefer 1", rx)
    assert reply_problem("policy", vote, '```json\n{"verdict": 1}\n```', rx) is None
    assert reply_problem("policy", act, "", rx) == "empty reply"

    monkeypatch.setenv("MSWEA_RETRIEVAL_CACHE_DIR", str(tmp_path / "retrieval"))
    monkeypatch.delenv("SENTENCE_TRANSFORMER_SERVER", raising=False)
    config = yaml.safe_load((builtin_config_dir / "extra" / "swe_xplorer.yaml").read_text(encoding="utf-8"))
    config["agent"].update(step_limit=12, itr_limit=2, branching_factor=2, sub_thres=1)
    M = "tests.gui.test_gui_backend.ScriptedModel"
    config["model"] = {"model_class": M, "model_name": "flaky", "flaky": True}
    config["reward_model"] = {"model_class": M, "model_name": "flaky", "role": "reward", "flaky": True}
    m = RunManager()
    m.start({"repo": str(repo), "task": "add() subtracts instead of adding", "config": config, "workspace_mode": "clone",
             "runs_dir": str(tmp_path / "runs")})
    m.thread.join(timeout=300)
    assert m.result["exit_status"] == "Submitted", m.result
    assert m.agent.gui_discarded > 0
    assert any("content-safety classifier" in l["msg"] and "nvidia/content-safety" in l["msg"] for l in m.bus.logs)
    # nothing the classifier said reached the tree
    assert not any("User Safety" in ((n.last_action or {}).get("thought") or "") for n in m.agent.gui_nodes.values())


def test_test_reproduction_shapes_rewards_and_stays_out_of_the_repo(repo, tmp_path, monkeypatch):
    from minisweagent.gui.runner import RunManager

    monkeypatch.setenv("MSWEA_RETRIEVAL_CACHE_DIR", str(tmp_path / "retrieval"))
    monkeypatch.delenv("SENTENCE_TRANSFORMER_SERVER", raising=False)
    config = yaml.safe_load((builtin_config_dir / "extra" / "swe_xplorer.yaml").read_text(encoding="utf-8"))
    assert config["reproduction"]["enabled"] is False  # off by default
    config["reproduction"]["enabled"] = True
    config["agent"].update(step_limit=12, itr_limit=2, branching_factor=2, sub_thres=1)
    M = "tests.gui.test_gui_backend.ScriptedModel"
    config["model"] = {"model_class": M, "model_name": "scripted"}
    config["reward_model"] = {"model_class": M, "model_name": "scripted", "role": "reward"}
    m = RunManager()
    m.start({"repo": str(repo), "task": "add() subtracts instead of adding", "config": config, "workspace_mode": "inplace",
             "runs_dir": str(tmp_path / "runs")})
    m.thread.join(timeout=300)
    assert m.result["exit_status"] == "Submitted", m.result
    turn = m.session["turns"][-1]
    assert turn["reproduction"]["ok"] and "run_test.sh" in turn["reproduction"]["files"]
    agent = m.agent
    assert "run_test.sh" in agent.config.reproduction_patch
    root = agent.tree_root.children[0]
    assert root.test_status == [{"name": "test_add", "status": "FAILED"}]  # measured on the unfixed code
    fixed = [n for n in agent.gui_nodes.values() if n.test_status and n.test_status[0]["status"] == "PASSED"]
    assert fixed, "the edit that fixes add() should pass the reproduction test"
    # the tests are only a measuring device: not in the result, not left in the repository, scratch clone removed
    assert "run_test.sh" not in turn["patch"] and "a + b" in (repo / "calc.py").read_text()
    assert not (repo / "run_test.sh").exists() and not (repo / "test_status.json").exists()
    assert not (Path(turn["run_dir"]) / "reproduction" / "workspace").exists()
    assert (Path(turn["run_dir"]) / "reproduction" / "reproduction.patch").exists()
    assert any("Reproduction tests at the root: 1 test(s), 1 failing" in l["msg"] for l in m.bus.logs)
