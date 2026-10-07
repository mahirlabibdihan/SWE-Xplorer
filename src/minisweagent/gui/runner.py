"""Run orchestration for the web GUI: config handling, API keys, the agent thread and an event bus."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

from minisweagent import global_config_dir
from minisweagent.config import builtin_config_dir
from minisweagent.gui.environment import (
    LocalBashEnvironment,
    _copy_uncommitted,
    apply_patch_to_repo,
    find_bash,
    git_run,
    git_toplevel,
    prepare_workspace,
    resolve_python,
    repo_is_dirty,
    split_patch,
    sync_workspace_to_source,
    to_posix_path,
)
from minisweagent.gui.observer import (
    MAX_REPLY_ATTEMPTS,
    ObservedTreeSearchAgent,
    RunController,
    StopRequested,
    expand_trace,
    final_response,
    light_node,
    node_details,
    reply_problem,
)

# Everything the app keeps lives in one folder, like other desktop tools (the app only runs on this machine).
APP_DIR = global_config_dir / "swe_xplorer"
SETTINGS_FILE = APP_DIR / "settings.json"  # UI preferences (non-secret)
KEYS_FILE = APP_DIR / "keys.env"  # API keys, KEY=value, loaded into the environment at start-up
DRAFT_FILE = APP_DIR / "config_draft.json"  # unsaved config edits
CACHE_DIR = APP_DIR / "cache"
DEFAULT_RUNS_DIR = APP_DIR / "runs"
LEGACY_SETTINGS_FILE = global_config_dir / "gui_settings.json"
LEGACY_RUNS_DIR = global_config_dir / "gui_runs"  # chats of earlier versions are still listed in the history
# the agent's per-commit retrieval index (defaults to the temp folder otherwise)
os.environ.setdefault("MSWEA_RETRIEVAL_CACHE_DIR", str(CACHE_DIR / "retrieval"))

KNOWN_KEYS = [
    ("OPENROUTER_API_KEY", "OpenRouter (model_class: openrouter)"),
    ("OPENAI_API_KEY", "OpenAI (litellm: openai/...)"),
    ("ANTHROPIC_API_KEY", "Anthropic (litellm / anthropic)"),
    ("GEMINI_API_KEY", "Google Gemini (litellm: gemini/...)"),
    ("DEEPSEEK_API_KEY", "DeepSeek (litellm: deepseek/...)"),
    ("HOSTED_VLLM_API_KEY", "vLLM / OpenAI-compatible server (litellm: hosted_vllm/...)"),
    ("SENTENCE_TRANSFORMER_SERVER", "Relevance server URL for reward shaping (optional)"),
]
SECRET_HINT = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.I)

MODEL_CLASSES = ["", "openrouter", "litellm", "litellm_response", "anthropic", "portkey", "portkey_response", "requesty"]


# ======================================================================================================================
# YAML helpers (multi-line strings as block scalars so saved configs stay readable)
# ======================================================================================================================


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_representer)


def dump_yaml(data: dict) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)


def list_configs() -> list[dict]:
    out = []
    for p in sorted((builtin_config_dir / "extra").glob("*.yaml")) + sorted(builtin_config_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        agent = data.get("agent", {}) if isinstance(data, dict) else {}
        # Tree-search configs need a retrieval template and a reward model
        tree = isinstance(agent, dict) and "retrieval_template" in agent
        out.append({"name": p.name, "path": str(p), "tree_search": tree, "builtin": True})
    return out


def agent_field_schema() -> dict[str, dict]:
    """Every field of the tree-search agent config with its default, so the UI can show knobs missing from a YAML."""
    from minisweagent.gui.observer import ObservedTreeSearchAgent  # noqa: F401  (ensures import works)
    from minisweagent.agents.tree_search_agent import TreeSearchAgentConfig

    schema = {}
    for name, f in TreeSearchAgentConfig.model_fields.items():
        default = None if f.is_required() else f.default
        try:
            json.dumps(default)
        except TypeError:
            default = None
        schema[name] = {"required": f.is_required(), "default": default, "doc": f.description or ""}
    return schema


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        for base in (builtin_config_dir / "extra", builtin_config_dir):
            if (base / p).exists():
                p = base / p
                break
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return {"path": str(p), "name": p.name, "text": text, "data": data}


# ======================================================================================================================
# Settings (non-secret by default)
# ======================================================================================================================


def load_settings() -> dict:
    for f in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def key_status(settings: dict) -> list[dict]:
    stored = read_keys_file()
    names = [k for k, _ in KNOWN_KEYS] + [k for k in settings.get("extra_env_names", []) if k not in dict(KNOWN_KEYS)]
    labels = dict(KNOWN_KEYS)
    out = []
    for name in names:
        val = os.environ.get(name, "")
        secret = bool(SECRET_HINT.search(name))
        out.append({
            "name": name,
            "label": labels.get(name, "custom"),
            "set": bool(val),
            "secret": secret,
            # never send secrets back to the browser, only a hint that one is configured
            "preview": ("…" + val[-4:] if len(val) > 8 else "set") if (val and secret) else (val if val else ""),
            "stored": name in stored,
        })
    return out


def read_keys_file() -> dict[str, str]:
    out = {}
    try:
        for line in KEYS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_keys_file(keys: dict[str, str]) -> None:
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = "# SWE-Xplorer API keys (plain text, like other CLI tools). Loaded at start-up; edit in the Keys tab.\n"
    body += "".join(f"{k}={v}\n" for k, v in sorted(keys.items()))
    KEYS_FILE.write_text(body, encoding="utf-8")
    try:
        os.chmod(KEYS_FILE, 0o600)  # owner-only where the OS supports it
    except OSError:
        pass


def load_keys_into_env() -> None:
    """Keys saved on this computer; an environment variable that is already set wins."""
    for k, v in read_keys_file().items():
        if v:
            os.environ.setdefault(k, v)


def apply_keys(settings: dict, keys: dict[str, str | None], remember: bool = True) -> None:
    """Set API keys as environment variables of this process and (if `remember`) save them in keys.env.
    An empty value unsets and forgets the key."""
    stored = read_keys_file()
    changed = False
    extra = settings.setdefault("extra_env_names", [])
    for name, value in keys.items():
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if name not in dict(KNOWN_KEYS) and name not in extra:
            extra.append(name)
        if value is None:
            continue
        if value == "":
            os.environ.pop(name, None)
            changed |= stored.pop(name, None) is not None
        else:
            os.environ[name] = value
            if remember and stored.get(name) != value:
                stored[name] = value
                changed = True
    if changed:
        write_keys_file(stored)
    save_settings(settings)  # only the (non-secret) list of custom variable names


# ======================================================================================================================
# Event bus
# ======================================================================================================================


class EventBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.seq = 0
        self.last_snapshot: dict | None = None
        self.snapshots: list[dict] = []  # full history for replay
        self.logs: list[dict] = []
        self.last_phase: dict | None = None
        self.status: dict = {"state": "idle"}

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, kind: str, data: dict):
        with self._lock:
            self.seq += 1
            evt = {"type": kind, "seq": self.seq, "data": data}
            if kind == "snapshot":
                data["seq"] = self.seq
                self.last_snapshot = data
                self.snapshots.append(data)
            elif kind == "log":
                self.logs.append(data)
                self.logs = self.logs[-3000:]
            elif kind == "phase":
                self.last_phase = data
            elif kind == "status":
                self.status = data
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass

    def reset(self):
        with self._lock:
            self.last_snapshot = None
            self.snapshots = []
            self.logs = []
            self.last_phase = None


class _BusLogHandler(logging.Handler):
    def __init__(self, bus: EventBus):
        super().__init__(logging.DEBUG)
        self.bus = bus
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.bus.emit("log", {"level": record.levelname.lower(), "msg": self.format(record)[:8000], "t": time.time()})
        except Exception:
            pass


# ======================================================================================================================
# Runs
# ======================================================================================================================


def _rewrite_testbed(obj: Any, cwd_posix: str) -> Any:
    """SWE-bench templates say the repo lives in /testbed. Point them at the local workspace instead."""
    if isinstance(obj, str):
        return obj.replace("/testbed", cwd_posix)
    if isinstance(obj, dict):
        return {k: _rewrite_testbed(v, cwd_posix) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_testbed(v, cwd_posix) for v in obj]
    return obj


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _changed_files(patch: str) -> list[str]:
    return re.findall(r"(?m)^diff --git a/\S+ b/(\S+)", patch or "")


def compose_task(turns: list[dict], message: str) -> str:
    """The task text used in the prompts for a chat turn.

    Follow-ups continue from the last *accepted* submission node, but the tree-search prompts only show the steps
    below the newest pseudo-root, so accepted requests (whose changes are in the code) and rejected attempts
    (whose changes are not) are summarised here.
    """
    accepted = [t for t in turns if t.get("decision") == "accepted"]
    rejected = [t for t in turns if t.get("decision") == "rejected"]
    if not accepted and not rejected:
        return message
    parts = []
    if accepted:
        parts += ["<previous_work>",
                  "Earlier requests in this session were implemented and accepted by the user (the current code is the user's repository as it is now, which includes them plus any manual edits the user made)."]
        for t in accepted:
            files = ", ".join(t.get("files") or []) or "no files"
            parts.append(f"- Request {t['n']}: {_clip(t['user'], 400)}\n  Result ({files}): {_clip(t.get('response') or '', 700)}")
        parts += ["</previous_work>", ""]
    if rejected:
        parts += ["<rejected_attempts>",
                  "The user rejected these earlier results; their changes are NOT in the current code. Do not repeat them."]
        for t in rejected:
            parts.append(f"- Request {t['n']}: {_clip(t['user'], 400)}\n  Rejected result: {_clip(t.get('response') or '', 500)}")
        parts += ["</rejected_attempts>", ""]
    parts += ["<new_request>", message, "</new_request>", "",
              "Focus on the new request. Keep the accepted changes unless the new request asks otherwise."]
    return "\n".join(parts)


PROVIDER_KEYS = {"openai/": "OPENAI_API_KEY", "anthropic/": "ANTHROPIC_API_KEY", "gemini/": "GEMINI_API_KEY",
                 "deepseek/": "DEEPSEEK_API_KEY"}


def missing_key(model_cfg: dict) -> str | None:
    """The environment variable a model needs but that is not set (None if fine or unknown)."""
    if model_cfg.get("model_kwargs", {}).get("api_key") or os.environ.get("MSWEA_MODEL_API_KEY"):
        return None
    cls, name = model_cfg.get("model_class") or "", model_cfg.get("model_name") or ""
    need = "OPENROUTER_API_KEY" if cls == "openrouter" else next(
        (k for p, k in PROVIDER_KEYS.items() if cls in ("", "litellm", "litellm_response") and name.startswith(p)), None)
    return need if need and not os.environ.get(need) else None


def _scratch_clone(src: Path, dst: Path, log=print) -> Path:
    """A throwaway copy of `src` as it is now (uncommitted changes included, committed), sharing its objects."""
    top = git_toplevel(src)
    _remove_tree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    git_run(dst.parent, "clone", "-q", "--shared", "--no-checkout", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", str(top), str(dst))
    git_run(dst, "checkout", "-q", "--detach", git_run(top, "rev-parse", "HEAD").strip())
    _copy_uncommitted(top, dst, log)
    git_run(dst, "add", "-A")
    git_run(dst, "commit", "-q", "--allow-empty", "--no-verify", "-m", "SWE-Xplorer: codebase for test reproduction")
    return dst


def _remove_tree(path: Path) -> None:
    import shutil
    import stat

    def onerror(fn, p, _exc):  # git marks object files read-only (Windows)
        try:
            os.chmod(p, stat.S_IWRITE)
            fn(p)
        except OSError:
            pass

    if Path(path).exists():
        shutil.rmtree(path, onerror=onerror)


def _agent_updates(agent_cfg: dict) -> dict:
    """Search parameters that can change between follow-ups (templates/models are fixed for a session)."""
    from minisweagent.agents.tree_search_agent import TreeSearchAgentConfig

    fields = TreeSearchAgentConfig.model_fields
    return {k: v for k, v in agent_cfg.items()
            if k in fields and not k.endswith("_template") and not (isinstance(v, str) and "\n" in v)}


class RunManager:
    """Owns the chat session (one workspace, one growing tree, many requests) and the agent thread."""

    def __init__(self):
        self.bus = EventBus()
        self.settings = load_settings()
        load_keys_into_env()
        self.thread: threading.Thread | None = None
        self.controller: RunController | None = None
        self.agent: ObservedTreeSearchAgent | None = None
        self.env: LocalBashEnvironment | None = None
        self.loaded_nodes: dict[str, dict] = {}  # node details when viewing a saved tree
        self.loaded_trace: list[dict] = []
        self.result: dict | None = None
        self.session: dict | None = None
        self._handler = _BusLogHandler(self.bus)
        self.viewing = False  # showing a saved tree instead of the live agent
        self._load_session()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def source_repo(self) -> Path | None:
        return Path(self.session["repo"]) if self.session else None

    # -- session persistence -----------------------------------------------------------------------------------
    def _load_session(self):
        p = self.settings.get("session_dir")
        try:
            if p and (Path(p) / "session.json").exists():
                self.session = json.loads((Path(p) / "session.json").read_text(encoding="utf-8"))
                for t in self.session.get("turns", []):
                    if t.get("status") == "running":  # the server stopped mid-run
                        t["status"] = "Interrupted"
                last = next((t for t in reversed(self.session["turns"]) if t.get("run_dir")
                             and (Path(t["run_dir"]) / "run.tree.json").exists()), None)
                if last:
                    self.load_tree(json.loads((Path(last["run_dir"]) / "run.tree.json").read_text(encoding="utf-8")),
                                   f"session {self.session['id']}", trace=last.get("trace"))
        except Exception:
            self.session = None

    def _save_session(self):
        if not self.session:
            return
        d = Path(self.session["dir"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_text(json.dumps(self.session, indent=1, default=str), encoding="utf-8")
        self.settings["session_dir"] = str(d)
        save_settings(self.settings)

    def session_view(self) -> dict | None:
        if not self.session:
            return None
        s = {k: v for k, v in self.session.items() if k != "turns"}
        s["live_agent"] = self.agent is not None
        s["turns"] = [{k: v for k, v in t.items() if k != "task"} for t in self.session["turns"]]
        return s

    def _runs_dir(self) -> Path:
        return Path(self.settings.get("runs_dir") or DEFAULT_RUNS_DIR).expanduser()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """Earlier chats (each a folder with session.json in the runs folder), newest first."""
        out = []
        dirs = {self._runs_dir(), DEFAULT_RUNS_DIR, LEGACY_RUNS_DIR}
        for f in (f for d in dirs for f in d.glob("*/session.json")):
            try:
                sess = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            turns = sess.get("turns") or []
            if not turns:
                continue
            updated = max((t.get("ended") or t.get("started") or 0) for t in turns)
            out.append({
                "dir": str(f.parent), "title": _clip(turns[0].get("user") or "(untitled)", 90),
                "repo": sess.get("repo"), "turns": len(turns), "updated": updated,
                "current": bool(self.session and Path(self.session["dir"]) == f.parent),
            })
        return sorted(out, key=lambda x: x["updated"], reverse=True)[:limit]

    def open_session(self, session_dir: str) -> dict | None:
        """Reopen an earlier chat: its transcript and last tree are shown; a follow-up starts from the repo's current
        state (the in-memory tree of that chat is gone, so a new tree is started)."""
        if self.running:
            raise RuntimeError("Stop the current run first.")
        d = Path(session_dir).expanduser()
        if not (d / "session.json").exists():
            raise ValueError("Not a SWE-Xplorer chat folder.")
        if self.env is not None:
            self.env.cleanup()
        self.agent, self.env, self.result, self.session = None, None, None, None
        self.loaded_nodes, self.loaded_trace = {}, []
        self.bus.reset()
        self.settings["session_dir"] = str(d)
        save_settings(self.settings)
        self._load_session()
        if self.session is None:
            raise ValueError("Could not read this chat.")
        self.bus.emit("status", {"state": "idle"})
        self.bus.emit("session", {"session": self.session_view()})
        return self.session_view()

    def new_session(self):
        if self.running:
            raise RuntimeError("Stop the current run first.")
        if self.env is not None:
            self.env.cleanup()
        self.session, self.agent, self.env, self.result = None, None, None, None
        self.loaded_nodes, self.loaded_trace = {}, []
        self.settings.pop("session_dir", None)
        save_settings(self.settings)
        self.bus.reset()
        self.bus.emit("status", {"state": "idle"})
        self.bus.emit("session", {"session": None})

    # -- controls ----------------------------------------------------------------------------------------------
    def control(self, action: str):
        if not self.controller:
            return
        {"pause": self.controller.pause, "resume": self.controller.resume, "step": self.controller.step_once,
         "stop": self.controller.stop}[action]()
        if action == "stop":
            self.bus.emit("phase", {"phase": "Stopping", "detail": "Finishing the current operation…"})

    def node(self, node_id: str) -> dict | None:
        if self.viewing and node_id in self.loaded_nodes:
            return self.loaded_nodes[node_id]
        if self.agent is not None and node_id in self.agent.gui_nodes:
            return node_details(self.agent.gui_nodes[node_id])
        return self.loaded_nodes.get(node_id)

    def trace(self) -> list[dict]:
        if self.agent is not None and not self.viewing:
            return expand_trace(self.agent.gui_trace, self.agent.gui_nodes.get)
        return self.loaded_trace

    # -- git graph ---------------------------------------------------------------------------------------------
    def _graph_nodes(self):
        if self.agent is not None and not self.viewing:
            return list(self.agent.gui_nodes.values())
        return [type("N", (), d)() for d in self.loaded_nodes.values()] if self.loaded_nodes else []

    def git_graph(self) -> dict:
        from minisweagent.gui.gitgraph import git_graph

        ws = self.session.get("workspace") if self.session else None
        base = self.session.get("base_commit") if self.session else None
        if not base and self.agent is not None and self.agent.tree_root is not None and self.agent.tree_root.children:
            base = self.agent.tree_root.children[0].commit  # in-place sessions: the first pseudo-root
        return git_graph(ws, self._graph_nodes(), base)

    def git_show(self, h: str) -> str:
        from minisweagent.gui.gitgraph import git_show, uncommitted_diff

        ws = self.session.get("workspace") if self.session else None
        if not ws:
            raise ValueError("No workspace")
        return uncommitted_diff(ws) if h == "WORKTREE" else git_show(ws, h)

    # -- start a chat turn -------------------------------------------------------------------------------------
    def start(self, req: dict) -> None:
        if self.running:
            raise RuntimeError("A run is already in progress.")
        message = (req.get("message") or req.get("task") or "").strip()
        if not message:
            raise ValueError("Please describe the task.")
        config = req["config"]
        if not isinstance(config, dict) or "agent" not in config:
            raise ValueError("Config must contain an 'agent' section.")
        if find_bash() is None:
            raise RuntimeError("bash not found. On Windows install Git for Windows (no WSL needed).")
        for label, mcfg in (("policy", config.get("model") or {}),
                            ("reward", (config.get("model") or {}) if req.get("reward_same_as_policy") else (config.get("reward_model") or config.get("model") or {}))):
            if key := missing_key(mcfg):
                # the OpenRouter client retries a 401 for minutes; fail fast with a useful message instead
                raise RuntimeError(f"{key} is not set (needed by the {label} model {mcfg.get('model_name')}). Add it in the Keys tab.")

        if self.session is None:
            repo = Path(req.get("repo") or "").expanduser()
            top = git_toplevel(repo) if repo.is_dir() else None
            if top is None:
                raise ValueError(f"Not a git repository: {repo}")
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            runs_dir = Path(req.get("runs_dir") or DEFAULT_RUNS_DIR).expanduser()
            self.session = {
                "id": stamp, "dir": str(runs_dir / f"{stamp}-{top.name}"), "repo": str(top),
                "mode": req.get("workspace_mode", "clone"), "include_uncommitted": bool(req.get("include_uncommitted", True)),
                "workspace": None, "base_commit": None, "turns": [], "created": time.time(),
            }
        elif self._pending_turn() is not None:
            raise RuntimeError("Accept or reject the last result first (or turn on auto-accept).")
        elif self.session["mode"] == "inplace" and self.agent is None and any(t.get("status") == "Submitted" for t in self.session["turns"]):
            raise RuntimeError("This in-place session can no longer be continued (the server restarted). Start a new chat.")

        turn = {"n": len(self.session["turns"]) + 1, "user": message, "status": "running", "started": time.time()}
        turn["task"] = compose_task(self.session["turns"], message)
        self.session["turns"].append(turn)
        self._save_session()

        self.settings.update({k: req.get(k) for k in ("repo", "workspace_mode", "include_uncommitted", "runs_dir", "config_path", "python")})
        self.settings["config_text"] = dump_yaml(config)
        save_settings(self.settings)

        self.bus.reset()
        self.loaded_nodes, self.loaded_trace = {}, []
        self.viewing = False
        self.result = None
        self.controller = RunController(self.bus.emit)
        self.bus.emit("session", {"session": self.session_view()})
        self.thread = threading.Thread(target=self._run, args=(req, copy.deepcopy(config), turn), daemon=True, name="swe-xplorer")
        self.thread.start()

    def _log(self, msg: str, level: str = "info"):
        self.bus.emit("log", {"level": level, "msg": msg, "t": time.time()})

    def _ensure_workspace(self) -> Path:
        s = self.session
        if s["workspace"] is None:
            ws = prepare_workspace(s["repo"], Path(s["dir"]), mode=s["mode"], include_uncommitted=s["include_uncommitted"], log=self._log)
            s["workspace"] = str(ws)
            if s["mode"] == "clone":
                # Freeze the starting point (incl. copied uncommitted changes): patches are then relative to what the
                # user's working tree looks like, and the session can always be reset to it.
                git_run(ws, "add", "-A")
                git_run(ws, "commit", "-q", "--no-verify", "--allow-empty", "-m", "SWE-Xplorer baseline")
                s["base_commit"] = git_run(ws, "rev-parse", "HEAD").strip()
            self._save_session()
        return Path(s["workspace"])

    # -- working directly in the user's repository ("in place") -------------------------------------------------
    def _inplace_begin(self, ws: Path, turn: dict):
        """Detach HEAD before the agent starts, so its commits (pseudo-root, one per edit) never move the user's
        branch. Remember where the user was, to put them back afterwards."""
        try:
            branch = git_run(ws, "symbolic-ref", "-q", "--short", "HEAD").strip()
        except Exception:
            branch = ""
        head = git_run(ws, "rev-parse", "HEAD").strip()
        turn["inplace"] = {"branch": branch, "head": head}
        git_run(ws, "checkout", "-q", "--detach")  # keeps the working tree and uncommitted changes as they are

    def _inplace_end(self, ws: Path, turn: dict, agent, patch: str):
        """Put the user back where they were: on their branch, at its original commit, with their own uncommitted
        changes; then apply the agent's patch as ordinary uncommitted edits (Reject reverts exactly that patch)."""
        info = turn.get("inplace") or {}
        if not info:
            return
        root = None
        if agent is not None:
            node = agent.gui_request_root or (agent.tree_root.children[0] if agent.tree_root is not None and agent.tree_root.children else None)
            root = node.commit if node is not None else None
        if root:  # the pseudo-root commit holds the user's state at the start of the request (incl. uncommitted work)
            git_run(ws, "reset", "-q", "--hard", root)
            git_run(ws, "clean", "-fdq")
        if info.get("branch"):
            git_run(ws, "symbolic-ref", "HEAD", f"refs/heads/{info['branch']}")
        git_run(ws, "reset", "-q", "--mixed", info["head"])  # HEAD back on the user's commit; files stay as they were
        if patch:
            ok, msg = apply_patch_to_repo(ws, patch)
            if not ok:
                self._log(f"Could not apply the result to your working tree: {msg}", "warning")
                turn["apply_error"] = msg.strip()
            turn["applied"] = ok
        self._log(f"Your repository is back on {info.get('branch') or info['head'][:10]}"
                  + (" with the agent's changes as uncommitted edits." if patch and turn.get("applied") else "."))

    def _last_good_turn(self) -> dict | None:
        """The last accepted submission: follow-ups continue from it."""
        return next((t for t in reversed(self.session["turns"])
                     if t.get("status") == "Submitted" and t.get("decision") == "accepted" and t.get("winner")), None)

    def _pending_turn(self) -> dict | None:
        t = self.session["turns"][-1] if self.session and self.session.get("turns") else None
        return t if t and t.get("status") == "Submitted" and t.get("decision") is None else None

    # -- review: accept / reject ---------------------------------------------------------------------------------
    def decide(self, n: int, decision: str, apply: bool = True) -> tuple[bool, str]:
        if self.running:
            raise RuntimeError("Wait for the run to finish.")
        t = self._pending_turn()
        if t is None or t["n"] != n:
            raise ValueError("Only the latest result can be reviewed.")
        if decision == "reject":
            if self.session["mode"] == "inplace" and t.get("applied"):
                # the result is in the user's files as uncommitted edits: revert exactly that patch
                text, _ = split_patch(t.get("turn_patch") or "")
                if text.strip():
                    chk = subprocess.run(["git", "apply", "-R", "--check", "-"], input=text.encode(), cwd=self.session["workspace"], capture_output=True)
                    if chk.returncode != 0:
                        return False, ("Could not revert the changes (were those lines edited meanwhile?):\n"
                                       + chk.stderr.decode(errors="replace").strip())
                    subprocess.run(["git", "apply", "-R", "-"], input=text.encode(), cwd=self.session["workspace"], capture_output=True)
                t["applied"] = False
            t["decision"] = "rejected"
            self._save_session()
            self.bus.emit("session", {"session": self.session_view()})
            return True, "Rejected. The next request starts from the last accepted state."
        if decision != "accept":
            raise ValueError("decision must be 'accept' or 'reject'")
        return self._accept(t, apply)

    def _accept(self, t: dict, apply: bool = True) -> tuple[bool, str]:
        """Accept a result. In sandbox mode its patch, which is relative to the codebase when the request was
        made, is applied to the user's repository (checked first; a conflict with manual edits is reported)."""
        msg = "Accepted."
        if self.session["mode"] == "clone" and not t.get("applied"):
            ok, msg = apply_patch_to_repo(self.session["repo"], t.get("turn_patch") or "")
            if not ok:
                return False, "Could not apply the patch to your repository (did you edit the same lines meanwhile?):\n" + msg.strip()
            t["applied"] = True
            msg = "Accepted and applied to your repository."
        t["decision"] = "accepted"
        self._save_session()
        self.bus.emit("session", {"session": self.session_view()})
        return True, msg

    def _env_cfg(self, config: dict, workspace: Path, req: dict) -> dict:
        """LocalBashEnvironment settings: the config's environment section, the workspace and the repo's Python."""
        env_cfg = {k: v for k, v in (config.get("environment") or {}).items()
                   if k not in ("environment_class", "image", "entrypoint", "executable", "container_timeout", "pull_timeout", "run_args", "forward_env")}
        env_cfg["cwd"] = str(workspace)
        py = resolve_python(self.session["repo"], req.get("python") or None)
        self.session["python"] = py
        env_cfg.update(python=py["python"], path_prepend=[py["bin"]] if py["venv"] else [], virtual_env=py["venv"])
        return env_cfg

    # -- test reproduction (config: reproduction) --------------------------------------------------------------
    def _reproduce(self, req: dict, config: dict, turn: dict, run_dir: Path, workspace: Path) -> str:
        """Before the search: a reproducer agent writes tests + run_test.sh for this request in a scratch clone.

        Returns its patch (the tree search applies it to measure test status at the root and after every edit, see
        RewardGuidedAgent._get_test_status), or "" if it did not produce a usable run_test.sh.
        """
        from minisweagent.agents.single_action_agent import SingleActionAgent
        from minisweagent.models import get_model
        from minisweagent.run.utils.save import save_traj

        bus, ctrl = self.bus, self.controller
        acfg = copy.deepcopy((config.get("reproduction") or {}).get("agent") or {})
        main = config.get("agent") or {}
        for k in ("action_observation_template", "format_error_template", "timeout_template", "action_regex"):
            if k not in acfg and k in main:
                acfg[k] = main[k]
        acfg.setdefault("agent_role", "reproducer")
        rdir = run_dir / "reproduction"
        ws = rdir / "workspace"
        bus.emit("phase", {"phase": "Reproducing", "detail": "Writing tests for the task before the search", "stage": "idle"})
        self._log("Test reproduction: a reproducer agent writes tests and run_test.sh in a scratch clone")
        env = agent = None
        exit_status, result, info = None, "", {"ok": False, "files": [], "cost": 0.0}
        turn["reproduction"] = info

        class Reproducer(SingleActionAgent):
            def step(inner):
                ctrl.checkpoint()  # Pause / Stop
                la = getattr(inner.tree_node, "last_action", None) or {}
                if la.get("command"):
                    self._log(f"Reproducer step {inner.n_expanded}: {la['command'].strip().splitlines()[0][:160]}")
                bus.emit("phase", {"phase": "Reproducing", "detail": f"Reproducer step {inner.n_expanded + 1}/{inner.config.step_limit}", "stage": "idle"})
                return super().step()

        try:
            _scratch_clone(workspace, ws, self._log)
            env = LocalBashEnvironment(**self._env_cfg(config, ws, req))
            ctrl.on_stop.append(env.abort)
            model = get_model(config=copy.deepcopy(config.get("model") or {}))
            agent = Reproducer(model, env, **_rewrite_testbed(acfg, to_posix_path(ws)))
            orig, regex = model.query, agent.config.action_regex

            def query(*a, **k):  # stoppable, and unusable replies are re-asked (as for the tree search)
                messages, r = (a[0] if a else k.get("messages", [])), None
                for attempt in range(1, MAX_REPLY_ATTEMPTS + 1):
                    r = ctrl.interruptible(orig)(*a, **k)
                    problem = reply_problem("policy", messages, (r or {}).get("content", ""), regex)
                    if problem is None:
                        return r
                    self._log(f"Discarded an unusable reproducer reply: {problem} (attempt {attempt}/{MAX_REPLY_ATTEMPTS})", "warning")
                return r

            model.query = query
            exit_status, result = agent.run(turn["task"])
        except StopRequested:
            raise
        except Exception as e:
            exit_status, result = type(e).__name__, str(e)
            self._log(f"Test reproduction failed: {e!r}", "warning")
        finally:
            if env is not None:
                if env.abort in ctrl.on_stop:
                    ctrl.on_stop.remove(env.abort)
                env.cleanup()
            if agent is not None:
                info["cost"] = round(agent.model.cost or 0, 4)
                try:
                    save_traj(agent, rdir / "reproduction.traj.json", exit_status=exit_status, result=result, print_path=False)
                except Exception:
                    pass
            _remove_tree(ws)
        patch = split_patch(result)[0] if exit_status == "Submitted" else ""
        info.update(status=exit_status, ok=patch.lstrip().startswith("diff --git") and "b/run_test.sh" in patch, files=_changed_files(patch))
        if not info["ok"]:
            self._log(f"No usable reproduction ({exit_status}); this request runs without test-based reward shaping", "warning")
            return ""
        (rdir / "reproduction.patch").write_text(patch, encoding="utf-8")
        self._log(f"Reproduction ready: {', '.join(info['files'])}")
        return patch

    def _run(self, req: dict, config: dict, turn: dict):
        from minisweagent.agents.reward_model import RewardModel
        from minisweagent.models import get_model
        from minisweagent.run.utils.save import save_traj

        for name in ("minisweagent_instance", "minisweagent"):
            logging.getLogger(name).addHandler(self._handler)
        bus, ctrl = self.bus, self.controller
        exit_status, result, error = None, None, None
        run_dir = Path(self.session["dir"]) / f"turn-{turn['n']}"
        turn["run_dir"] = str(run_dir)
        agent = self.agent
        try:
            bus.emit("status", {"state": "running", "run_dir": str(run_dir), "turn": turn["n"]})
            bus.emit("phase", {"phase": "Preparing workspace", "detail": self.session["mode"]})
            workspace = self._ensure_workspace()
            if self.session["mode"] == "inplace":
                self._inplace_begin(workspace, turn)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "config.yaml").write_text(dump_yaml(config), encoding="utf-8")
            (run_dir / "task.md").write_text(turn["task"], encoding="utf-8")
            self._log(f"Workspace: {workspace}")

            # Every request starts from the user's codebase as it is now (manual edits included), like the first one.
            if turn["n"] > 1 and self.session["mode"] == "clone":
                self._log("Syncing the sandbox with your repository's current state")
                bus.emit("phase", {"phase": "Syncing workspace", "detail": "Taking your repository's current state as the starting point"})
                sync_workspace_to_source(workspace, self.session["repo"], f"SWE-Xplorer: your codebase at request {turn['n']}",
                                         include_uncommitted=self.session["include_uncommitted"], log=self._log)
            repro_patch = None  # None: test reproduction is off (config: reproduction.enabled)
            if (config.get("reproduction") or {}).get("enabled"):
                if (config.get("agent") or {}).get("shape_reward", True):
                    repro_patch = self._reproduce(req, config, turn, run_dir, workspace)
                else:
                    self._log("Test reproduction is enabled but agent.shape_reward is false, so it is skipped", "warning")
            good = self._last_good_turn()
            if agent is not None and good and good["winner"] in agent.gui_nodes:
                agent.attach_to(agent.gui_nodes[good["winner"]])  # keep the tree connected: the new request hangs here
            if agent is not None and good and agent.can_continue():
                # ---- follow-up: continue the same tree from the last submission node
                turn["mode"] = "continue"
                agent.gui_controller = ctrl
                agent._make_stoppable()
                ctrl.emit("phase", {"phase": f"Request {agent.gui_request + 1}", "detail": "Continuing from the last submission node", "stage": "explore"})
                updates = _agent_updates(config.get("agent", {}))
                # this request's tests (none when reproduction is off: the previous request's tests do not apply)
                updates["reproduction_patch"] = repro_patch if repro_patch is not None else config.get("agent", {}).get("reproduction_patch", "")
                exit_status, result = agent.continue_run(turn["user"], turn["task"], updates)
            else:
                # ---- first request (or the previous agent is gone): a fresh tree
                turn["mode"] = "fresh"
                task = turn["task"]
                config["agent"] = _rewrite_testbed(config.get("agent", {}), to_posix_path(workspace))
                if repro_patch is not None:
                    config["agent"]["reproduction_patch"] = repro_patch
                env_cfg = self._env_cfg(config, workspace, req)
                py = self.session["python"]
                self._log(f"Python for commands: {py['python']} ({'virtualenv ' + py['venv'] if py['venv'] else py['source']})")
                if self.env is not None:
                    self.env.cleanup()
                self.env = LocalBashEnvironment(**env_cfg)
                model_cfg = config.get("model") or {}
                reward_cfg = model_cfg if req.get("reward_same_as_policy") else (config.get("reward_model") or model_cfg)
                model = get_model(config=model_cfg)
                reward = RewardModel(
                    get_model(config=reward_cfg),
                    use_combined_scoring=reward_cfg.get("use_combined_scoring", True),
                    max_retries=reward_cfg.get("max_retries", 3),
                )
                self._log(f"Policy model: {model_cfg.get('model_name')} ({model_cfg.get('model_class') or 'litellm'})")
                self._log(f"Reward model: {reward_cfg.get('model_name')} ({reward_cfg.get('model_class') or 'litellm'})")
                bus.emit("phase", {"phase": "Indexing repository", "detail": "Building BM25 retrieval index (cached per commit)"})
                agent = ObservedTreeSearchAgent(model, self.env, reward, gui_controller=ctrl, **config["agent"])
                self.agent = agent
                exit_status, result = agent.run(task)
        except StopRequested as e:
            exit_status, result = "Stopped", str(e)
        except Exception as e:
            exit_status, result = type(e).__name__, str(e)
            error = traceback.format_exc()
            self._log(error, "error")
        finally:
            submitted = result if exit_status == "Submitted" else ""
            turn.update({"status": exit_status, "ended": time.time(), "response": "", "judge_summary": "",
                         "patch": "", "turn_patch": "", "files": [], "winner": None})
            if agent is not None:
                try:
                    response, judge, winner = final_response(agent) if submitted else ("", "", None)
                    if submitted and agent.tree_node is not None and agent.tree_node.is_terminating:
                        agent.gui_best = agent.tree_node.id
                    root = agent.gui_request_root or (agent.tree_root.children[0] if agent.tree_root and agent.tree_root.children else None)
                    turn.update({"response": response, "judge_summary": judge, "winner": winner,
                                 "request_root": root.id if root is not None else None, "request": agent.gui_request,
                                 "trace": self.trace(), "steps": agent.n_expanded, "iterations": agent.itr,
                                 "cost": round((agent.model.cost or 0) + (agent.reward_model.model.cost or 0)
                                               + ((turn.get("reproduction") or {}).get("cost") or 0), 4)})
                    agent._phase("Finished", exit_status or "", "done")
                except Exception as e:
                    self._log(f"Could not build the final response: {e!r}", "warning")
                try:
                    (run_dir / "run.tree.json").write_text(json.dumps(agent.tree_root.to_tree(), indent=1, default=str), encoding="utf-8")
                    save_traj(agent, run_dir / "run.traj.json", exit_status=exit_status, result=result,
                              extra_info={"traceback": error} if error else None, print_path=False)
                except Exception as e:
                    self._log(f"Could not save outputs: {e!r}", "warning")
            if submitted:
                turn["turn_patch"] = turn["patch"] = submitted  # relative to the codebase when the request was made
                turn["files"] = _changed_files(turn["turn_patch"])
                turn["decision"] = None  # waits for the user's review
            if self.session["mode"] == "inplace":
                try:
                    self._inplace_end(Path(self.session["workspace"]), turn, agent, submitted)
                except Exception as e:
                    self._log(f"Could not restore your branch: {e!r}. Check `git status` in your repository.", "error")
            if submitted:
                (run_dir / "patch.diff").write_text(turn["patch"], encoding="utf-8")
            else:
                turn["error"] = result
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "run.log").write_text("\n".join(l["msg"] for l in bus.logs), encoding="utf-8", errors="replace")
            except Exception:
                pass
            self._save_session()
            if submitted and req.get("auto_accept"):
                ok, msg = self._accept(turn)
                self._log(("Auto-accepted: " if ok else "Auto-accept failed, please review: ") + msg, "info" if ok else "warning")
            self.result = {"exit_status": exit_status, "patch": turn.get("patch", ""), "turn_patch": turn.get("turn_patch", ""),
                           "message": "" if submitted else result, "run_dir": str(run_dir),
                           "source_repo": self.session["repo"], "workspace_mode": self.session["mode"], "turn": turn["n"]}
            bus.emit("finished", self.result)
            bus.emit("session", {"session": self.session_view()})
            bus.emit("status", {"state": "finished", **self.result})
            for name in ("minisweagent_instance", "minisweagent"):
                logging.getLogger(name).removeHandler(self._handler)

    # -- patch ---------------------------------------------------------------------------------------------------
    def apply_patch(self) -> tuple[bool, str]:
        if not self.session or not self.session.get("turns"):
            return False, "No patch available."
        if self.session["mode"] == "inplace":
            return False, "The session runs in place: the changes are already in the repository."
        t = next((t for t in reversed(self.session["turns"]) if t.get("decision") == "accepted" and not t.get("applied")), None)
        if t is None:
            return False, "Accepted results are applied automatically."
        return self._accept(t)

    # -- view a saved tree ------------------------------------------------------------------------------------
    def load_tree(self, tree: dict, name: str = "", trace: list | None = None) -> dict:
        if self.running:
            raise RuntimeError("Stop the current run first.")
        nodes, extra, order = {}, {}, [0]

        class N:  # minimal adapter so we can reuse light_node/node_details
            pass

        def build(d, parent):
            if set(d.keys()) <= {"id"}:
                if parent is not None:
                    extra.setdefault(d["id"], []).append(parent.id)
                return
            n = N()
            n.id = d["id"]
            n.parent = parent
            n.children = []
            n.level = d.get("level", 0)
            for a in ("value", "merged_value", "raw_value", "executed", "visible", "is_terminating", "is_submission",
                      "system_generated", "merged", "itr", "order", "modifies_code", "is_repeat", "is_timeout",
                      "cache_hit", "observation", "modified_files", "read_files", "diff_size", "commit", "state_hash",
                      "test_status", "score_calculation", "solution_summary", "last_action"):
                setattr(n, a, d.get(a))
            n.invalid_termination = False
            n.get_path_value = lambda *_a, **_k: None
            n._k = order[0]
            order[0] += 1
            nodes[n.id] = n
            for c in d.get("children", []) or []:
                if isinstance(c, dict):
                    build(c, n)

        build(tree, None)
        root = next(iter(nodes.values()))
        light = [light_node(n, extra, n._k) for n in nodes.values()]
        self.viewing = True
        self.loaded_nodes = {nid: node_details(n) for nid, n in nodes.items()}
        subs = [n for n in nodes.values() if n.is_submission]
        executed = sorted((n for n in nodes.values() if n.executed), key=lambda n: (n.order or 0))
        snap = {
            "t": time.time(), "phase": "Loaded", "detail": name, "root": root.id,
            "current": (subs[-1].id if subs else (executed[-1].id if executed else root.id)),
            "frontier": [], "active": [], "evaluating": [], "best": subs[-1].id if subs else None, "rtv": [],
            "stats": {"steps": max([n.order or 0 for n in nodes.values()] + [0]), "nodes": len(light),
                      "itr": max([n.itr or 0 for n in nodes.values()] + [0]), "submissions": len(subs)},
            "nodes": light,
        }
        self.loaded_trace = trace or expand_trace(
            [{"kind": "step", "id": n.id, "n": n.order, "itr": n.itr} for n in executed if n.last_action], nodes.get)
        self.bus.reset()
        self.bus.emit("status", {"state": "viewing", "name": name})
        self.bus.emit("snapshot", snap)
        return {"nodes": len(light)}


def repo_info(path: str | None, python: str | None = None) -> dict:
    """Resolve any folder inside a git repository to the repository root, with a few facts for the UI."""
    if not path or not Path(path).expanduser().is_dir():
        return {"ok": False, "error": "Folder not found."}
    top = git_toplevel(Path(path).expanduser())
    if top is None:
        return {"ok": False, "error": "Not inside a git repository."}
    try:
        branch = git_run(top, "rev-parse", "--abbrev-ref", "HEAD").strip()
        head = git_run(top, "log", "-1", "--format=%h %s").strip()
    except Exception:
        branch, head = "", "(no commits yet)"
    try:
        py = resolve_python(top, python)
    except ValueError as e:
        py = {"error": str(e), "label": "invalid Python setting"}
    return {"ok": True, "root": str(top), "name": top.name, "branch": "detached HEAD" if branch == "HEAD" else branch,
            "head": head, "dirty": repo_is_dirty(top), "subfolder": Path(path).expanduser().resolve() != top.resolve(),
            "python": py}


def browse(path: str | None) -> dict:
    """Directory listing for the repo picker (browsers cannot reveal absolute paths of chosen folders)."""
    if not path:
        if os.name == "nt":
            import string

            drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
            return {"path": "", "parent": None, "is_repo": False, "entries": [{"name": d, "path": d, "repo": False} for d in drives]}
        path = str(Path.home())
    p = Path(path).expanduser().resolve()
    entries = []
    try:
        for c in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if c.is_dir() and not c.name.startswith((".", "$")):
                entries.append({"name": c.name, "path": str(c), "repo": (c / ".git").exists()})
    except (PermissionError, OSError):
        pass
    parent = str(p.parent) if p.parent != p else ("" if os.name == "nt" else None)
    is_repo = (p / ".git").exists()
    info = {"path": str(p), "parent": parent, "is_repo": is_repo, "entries": entries[:500]}
    if not is_repo and p.is_dir():
        top = git_toplevel(p)
        if top is not None:
            info["inside_repo"] = str(top)
    if is_repo:
        try:
            info["dirty"] = repo_is_dirty(p)
        except Exception:
            pass
    return info


# ======================================================================================================================
# Unsaved config edits and UI preferences (server-side, instead of the browser's localStorage)
# ======================================================================================================================


def load_draft() -> dict | None:
    try:
        d = json.loads(DRAFT_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("data") else None
    except Exception:
        return None


def save_draft(draft: dict | None) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not draft or not draft.get("dirty"):
        DRAFT_FILE.unlink(missing_ok=True)  # nothing unsaved: the config is loaded from its file next time
        return
    DRAFT_FILE.write_text(json.dumps(draft, indent=1), encoding="utf-8")
