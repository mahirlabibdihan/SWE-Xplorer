"""Observe a running SWE-Xplorer (TreeSearchAgent) without changing its search logic.

`ObservedTreeSearchAgent` only wraps existing methods: it calls the original implementation and reports
what happened (phases, snapshots of the tree, backtracks, evaluations) to a `RunController`.
The controller also implements pause / single-step / stop at safe points (between steps and before LLM calls).
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from minisweagent.agents.default import NonTerminatingException, TerminatingException
from minisweagent.agents.tree_search_agent import TreeSearchAgent


class StopRequested(Exception):
    """Raised at a safe point when the user presses Stop. Deliberately *not* an agent exception type,
    so it propagates out of `agent.run()` instead of being treated as a submission/format error."""


class RunController:
    def __init__(self, emit: Callable[[str, dict], None]):
        self.emit = emit
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self.on_stop: list = []
        self._pause_after_step = False
        self.paused = False
        self.started_at = time.time()

    # -- user controls -----------------------------------------------------------------------------------------
    def pause(self):
        self.paused = True
        self._resume.clear()
        self.emit("control", {"paused": True})

    def resume(self):
        self._pause_after_step = False
        self.paused = False
        self._resume.set()
        self.emit("control", {"paused": False})

    def step_once(self):
        self._pause_after_step = True
        self.paused = False
        self._resume.set()
        self.emit("control", {"paused": False, "stepping": True})

    def stop(self):
        self._stop.set()
        self._resume.set()
        for fn in list(self.on_stop):  # e.g. kill the shell command that is running right now
            try:
                fn()
            except Exception:
                pass

    def interruptible(self, fn):
        """Wrap a blocking call (an LLM request with its retries) so that Stop returns at once.

        The call runs on a helper thread; we wait for it while watching the stop flag. On Stop we raise
        StopRequested and the abandoned request's result is discarded when it eventually returns.
        """
        def wrapper(*args, **kwargs):
            if self._stop.is_set():
                raise StopRequested("Stopped by user")
            box: dict = {}

            def run():
                try:
                    box["result"] = fn(*args, **kwargs)
                except BaseException as e:  # re-raised in the caller's thread
                    box["error"] = e

            t = threading.Thread(target=run, daemon=True, name="llm-call")
            t.start()
            while t.is_alive():
                t.join(0.2)
                if self._stop.is_set():
                    raise StopRequested("Stopped by user")
            if "error" in box:
                raise box["error"]
            return box["result"]

        return wrapper

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- called from the agent thread ------------------------------------------------------------------------
    def checkpoint(self):
        if self._stop.is_set():
            raise StopRequested("Stopped by user")
        if not self._resume.is_set():
            self.emit("phase", {"phase": "Paused", "detail": "Press Resume or Step"})
            self._resume.wait()
            if self._stop.is_set():
                raise StopRequested("Stopped by user")

    def end_of_step(self):
        if self._pause_after_step:
            self._pause_after_step = False
            self.pause()


def _short(s: Any, n: int) -> str | None:
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(x):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):  # NaN / inf are not valid JSON
        return -1.0 if x == float("-inf") else None
    return round(x, 4)


# ======================================================================================================================
# Retrieval index freshness
# ======================================================================================================================
# The agent builds its BM25 index once, cached per commit, when it is created. In a chat the code changes between
# requests (accepted results, the user's own uncommitted edits, new untracked files) while the commit may not. So at
# the start of every request we fingerprint the code and rebuild the index when it no longer matches. Never during
# the search: then the agent checks out half-finished candidate states on different branches of the tree.

_INDEX_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".cs", ".rb", ".php"}


def _is_test_path(rel: str) -> bool:  # same rule as the agent's extraction script
    return bool({"test", "tests", "testing"} & set(re.split(r"[ _/.]", rel.lower())))


def _git_out(ws: Path, *args: str, stdin: str | None = None) -> str:
    res = subprocess.run(["git", "--no-optional-locks", "-c", "core.quotepath=off", *args], cwd=str(ws),
                         input=stdin.encode() if stdin is not None else None, capture_output=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    return res.stdout.decode("utf-8", errors="replace")


def code_fingerprint(ws: Path) -> tuple[str, bool]:
    """Content fingerprint of the working tree (read-only): HEAD's tree + a hash of every changed/untracked file.
    Returns (fingerprint, clean). Using the tree, not the commit, means re-committing the same code (pseudo-roots,
    sandbox syncs) does not count as a change."""
    tree = _git_out(ws, "rev-parse", "HEAD^{tree}").strip()
    entries = [e for e in _git_out(ws, "status", "--porcelain", "-z", "--untracked-files=all").split("\0") if e]
    paths = sorted({e[3:] for e in entries if len(e) > 3})
    present = [p for p in paths if (ws / p).is_file()]
    blobs = _git_out(ws, "hash-object", "--stdin-paths", stdin="\n".join(present) + "\n").split() if present else []
    h = hashlib.sha1(tree.encode())
    for p in paths:
        h.update(f"\0{p}\0".encode())
    for b in blobs:
        h.update(b.encode())
    return h.hexdigest(), not paths

# ======================================================================================================================
# Unusable model replies
# ======================================================================================================================
# Routers such as openrouter/free may serve a call with a model that cannot do the job, e.g. a content-safety
# classifier that answers "User Safety: safe". Such replies are discarded and the call is simply made again (the
# router then picks another model), instead of becoming a NO ACTION node, a failed vote or a random reward score.

_SAFETY_VERDICT = re.compile(r"(?im)^\s*(?:user|response|prompt)\s+safety\s*:\s*(?:safe|unsafe)\b")
_SCORE = re.compile(r"<score>\s*\d{1,3}\s*</score>")
MAX_REPLY_ATTEMPTS = 4


def reply_problem(kind: str, messages: list[dict], content: str, action_regex: str) -> str | None:
    """Why a reply cannot serve this call (None if it is usable)."""
    text = (content or "").strip()
    if not text:
        return "empty reply"
    if _SAFETY_VERDICT.search(text) and len(text) < 400:
        return "content-safety classifier verdict, not an answer"
    if kind == "reward":
        return None if _SCORE.search(text) else "no <score> in the reward reply"
    prompt = "\n".join(str(m.get("content", "")) for m in messages)
    if "<solution_1>" in prompt:  # tournament vote
        return None if '"verdict"' in text else "no verdict in the vote"
    if "<candidate_solution>" in prompt:  # trajectory summary for voting
        return None
    if messages and "COMMAND_TYPE" in str(messages[0].get("content", "")):  # an action of the agent
        n = len(re.findall(action_regex, text, re.DOTALL))
        return None if n >= 1 else "no bash command in the action reply"
    return None


def light_node(n, extra_parents: dict[str, list[str]], order_idx: int) -> dict:
    la = n.last_action or {}
    return {
        "id": n.id,
        "p": n.parent.id if n.parent is not None else None,
        "xp": extra_parents.get(n.id, []),
        "k": order_idx,  # creation order, used for stable child ordering
        "lvl": n.level,
        "v": _num(n.value),
        "mv": _num(n.merged_value),
        "rv": _num(n.raw_value),
        "ty": la.get("type") or ("root" if not la else None),
        "cmd": _short(la.get("command"), 140),
        "ex": bool(n.executed),
        "vis": bool(n.visible),
        "term": bool(n.is_terminating),
        "sub": bool(n.is_submission),
        "inv": bool(getattr(n, "invalid_termination", False)),
        "sys": bool(n.system_generated),
        "mg": bool(n.merged),
        "itr": n.itr,
        "ord": n.order,
        "mod": bool(n.modifies_code),
        "rep": bool(getattr(n, "is_repeat", False)),
        "to": bool(getattr(n, "is_timeout", False)),
        "ch": bool(getattr(n, "cache_hit", None)),
        "rq": getattr(n, "gui_request", 1),
        "cm": n.commit,
    }


def node_details(n) -> dict:
    la = n.last_action or {}
    path = []
    cur = n
    while cur is not None:
        path.append(cur.id)
        cur = cur.parent
    return {
        "id": n.id,
        "command": la.get("command"),
        "thought": la.get("thought"),
        "type": la.get("type"),
        "observation": _short(n.observation, 60_000),
        "modified_files": list(n.modified_files or []),
        "read_files": list(n.read_files or []),
        "diff_size": n.diff_size,
        "commit": n.commit,
        "modifies_code": bool(n.modifies_code),
        "visible": bool(n.visible),
        "executed": bool(n.executed),
        "last_action": (n.last_action or None) and {k: n.last_action.get(k) for k in ("command", "type")},
        "gui_request": getattr(n, "gui_request", 1),
        "state_hash": n.state_hash,
        "test_status": n.test_status or [],
        "value": _num(n.value),
        "merged_value": _num(n.merged_value),
        "raw_value": _num(n.raw_value),
        "path_value": _num(_safe(lambda: n.get_path_value(0.85))),
        "score_calculation": n.score_calculation,
        "solution_summary": n.solution_summary,
        "path": path[::-1],
    }


def clean_thought(text: str | None) -> str:
    """Agent messages look like 'THOUGHT: ...\nCOMMAND_TYPE: [EDIT]\n```bash ...```'. Keep only the reasoning."""
    if not text:
        return ""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"(?im)^\s*COMMAND_TYPE:.*$", "", text)
    text = re.sub(r"^\s*THOUGHT:\s*", "", text.strip())
    return text.strip()


def expand_trace(events: list[dict], get_node) -> list[dict]:
    """Attach node content to recorded trace events (steps) for the chat's reasoning view."""
    out = []
    for e in list(events):
        if e.get("kind") != "step":
            out.append(e)
            continue
        n = get_node(e["id"])
        if n is None:
            continue
        la = n.last_action or {}
        out.append({
            **e,
            "type": la.get("type"),
            "thought": _short(clean_thought(la.get("thought")), 2500),
            "command": _short(la.get("command"), 800),
            "obs": _short(n.observation, 1500),
            "v": _num(n.merged_value if n.merged_value is not None else n.value),
            "sys": bool(n.system_generated),
            "term": bool(n.is_terminating),
        })
    return out


def final_response(agent) -> tuple[str, str, str | None]:
    """(response, judge_summary, winner_id): the winning submission's reasoning is the chat reply.
    Augmented (system-generated) submissions have no reasoning of their own, so fall back to the tournament
    judge's trajectory summary, then to the last substantive thought on the winning path."""
    node = agent.tree_node if agent.tree_node is not None and agent.tree_node.is_terminating else agent.all_node_map.get(agent.gui_best or "")
    if node is None:
        return "", "", None
    judge = (node.solution_summary or "").split("\n\nFinal Patch:")[0].strip()
    thought = "" if node.system_generated else clean_thought((node.last_action or {}).get("thought"))
    if len(thought) < 40:
        if judge:
            thought = judge
        else:
            cur = node.parent
            while cur is not None and cur.last_action is not None:
                t = clean_thought(cur.last_action.get("thought"))
                if not cur.system_generated and len(t) >= 40:
                    thought = t
                    break
                cur = cur.parent
    return thought, (judge if judge and judge != thought else ""), node.id


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def snapshot_agent(agent: "ObservedTreeSearchAgent", phase: str, detail: str = "") -> dict:
    nodes = list(agent.gui_nodes.values())  # every request of the session; list() is atomic under the GIL
    extra: dict[str, list[str]] = {}
    for p in nodes:
        for c in list(p.children):
            if c.parent is not None and c.parent is not p:
                extra.setdefault(c.id, []).append(p.id)
    root_id = agent.tree_root.id if agent.tree_root is not None else None
    out = []
    for i, n in enumerate(nodes):
        if n.parent is None and n.id != root_id:
            continue  # stubs that are not attached to the tree
        out.append(light_node(n, extra, i))
    frontier = [item[1].id for item in list(agent.frontier.queue)]
    ctrl = agent.gui_controller
    model_cost = getattr(agent.model, "cost", 0.0) or 0.0
    reward_cost = getattr(getattr(agent, "reward_model", None), "model", None)
    reward_cost = getattr(reward_cost, "cost", 0.0) or 0.0
    return {
        "t": time.time(),
        "phase": phase,
        "detail": detail,
        "stage": agent.gui_stage,
        "root": root_id,
        "current": agent.tree_node.id if agent.tree_node is not None else None,
        "frontier": frontier,
        "active": list(agent.node_map.keys()),
        "evaluating": sorted(agent.gui_evaluating),
        "best": agent.gui_best,
        "rtv": agent.rtv[-6:],
        "stats": {
            "steps": agent.n_expanded,
            "step_limit": agent.config.step_limit,
            "itr": agent.itr,
            "itr_limit": agent.config.itr_limit,
            "nodes": len(out),
            "submissions": agent.n_submissions,
            "unique_solutions": len(agent.terminating_nodes),
            "backtracks": agent.n_backtracks,
            "cost": round(model_cost, 4),
            "reward_cost": round(reward_cost, 4),
            "elapsed": round(time.time() - ctrl.started_at, 1),
            "mode": agent.mode,
            "request": agent.gui_request,
            "discarded": agent.gui_discarded,
        },
        "nodes": out,
    }


class ObservedTreeSearchAgent(TreeSearchAgent):
    """TreeSearchAgent + progress reporting, plus follow-up requests.

    Reporting overrides call the original methods unchanged. Follow-ups (`continue_run`) use the agent's own
    continuation mechanism: after a submission, the next `step()` calls `_create_pseudo_root()`, which commits the
    submitted code and attaches a new pseudo-root *under the submission node*, so the search continues in the same
    tree. This subclass adds what that path lacks: the new instruction in the prompts, fresh per-request search
    state, and a per-request root commit.
    """

    def __init__(self, *args, gui_controller: RunController, **kwargs):
        self.gui_controller = gui_controller
        self.gui_evaluating: set[str] = set()
        self.gui_best: str | None = None
        self.gui_stage = "idle"
        self.gui_trace: list[dict] = []
        self._gui_phase = "Starting"
        self._gui_lock = threading.Lock()
        self._gui_last_snap = 0.0
        self.gui_nodes: dict = {}  # all nodes of all requests (all_node_map is scoped to the current request)
        self.gui_request = 1
        self.gui_request_root = None  # pseudo-root of the current follow-up request
        self._gui_pending_request = False
        self.gui_discarded = 0  # unusable model replies that were re-asked
        super().__init__(*args, **kwargs)
        self._gui_cost_budget = self.config.cost_limit
        self._make_stoppable()

    def _make_stoppable(self):
        """Wrap both models' `query`: (1) Stop must not wait for a slow or retrying LLM call, (2) unusable replies are
        discarded and the call is made again. Also let Stop kill a long shell command."""
        ctrl = self.gui_controller
        models = [(self.model, "policy"), (getattr(self.reward_model, "model", None), "reward")]
        for model, kind in models:
            if model is None or getattr(model, "_gui_wrapped", False):
                continue
            orig = model.query
            model.query = lambda *a, _orig=orig, _kind=kind, **k: self._validated_query(_orig, _kind, *a, **k)
            model._gui_wrapped = True
        abort = getattr(self.env, "abort", None)
        if callable(abort) and abort not in ctrl.on_stop:
            ctrl.on_stop.append(abort)

    def _validated_query(self, orig, kind: str, *args, **kwargs):
        messages = args[0] if args else kwargs.get("messages", [])
        response = None
        for attempt in range(1, MAX_REPLY_ATTEMPTS + 1):
            response = self.gui_controller.interruptible(orig)(*args, **kwargs)  # each attempt is stoppable
            problem = reply_problem(kind, messages, (response or {}).get("content", ""), self.config.action_regex)
            if problem is None:
                return response
            served = ((response or {}).get("extra") or {}).get("response", {}) or {}
            served = served.get("model") if isinstance(served, dict) else None
            self.gui_discarded += 1
            self.gui_controller.emit("log", {"level": "warning", "t": time.time(), "msg":
                f"Discarded an unusable {kind} reply{f' from {served}' if served else ''}: {problem}"
                + (f" (retrying, attempt {attempt + 1}/{MAX_REPLY_ATTEMPTS})" if attempt < MAX_REPLY_ATTEMPTS else " (giving up; the agent handles it as usual)")})
        return response

    # -- retrieval index freshness -------------------------------------------------------------------------------
    def refresh_index_if_changed(self) -> bool:
        """Rebuild the BM25 index if the code differs from what it was built from. Returns True if rebuilt.

        The index the agent built in __init__ is keyed by commit only, so it is trusted as-is only when the working
        tree is clean at the first request; any uncommitted change, and any later change, triggers a rebuild."""
        ws = Path(self.env.config.cwd)
        try:
            fp, clean = code_fingerprint(ws)
        except Exception as e:
            self.gui_controller.emit("log", {"level": "warning", "msg": f"index fingerprint failed: {e!r}"})
            return False
        if getattr(self, "_gui_index_fp", None) is None and clean:
            self._gui_index_fp = fp  # the per-commit index built in __init__ matches a clean checkout
            return False
        if fp == getattr(self, "_gui_index_fp", None):
            return False
        self._phase("Refreshing index", "The code changed since the retrieval index was built; re-indexing")
        t0 = time.time()
        self._gui_build_index(ws, fp)
        self._gui_index_fp = fp
        self.gui_controller.emit("log", {"level": "info", "msg": f"Retrieval index refreshed ({len(self.file_ids)} files, {time.time() - t0:.1f}s)"})
        return True

    def _gui_build_index(self, ws: Path, fp: str):
        """Same documents and indexes as RewardGuidedAgent.__init__, from the files as they are now (tracked and
        untracked, non-ignored), cached under a key of the code's content."""
        from rank_bm25 import BM25Okapi

        from minisweagent.agents.repo_tree import collect_rankable_nodes, dict_to_tree, remove_redundancy, result_to_structure

        root = Path(os.getenv("MSWEA_RETRIEVAL_CACHE_DIR", str(Path(tempfile.gettempdir()) / "mini-swe-agent-retrieval")))
        cache = root / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', ws.name)}-code-{fp[:12]}"
        cache.mkdir(parents=True, exist_ok=True)
        docs_path, pk = cache / "documents.jsonl", cache / "indexes.pkl"
        if docs_path.exists() and pk.exists():
            with pk.open("rb") as f:
                self.repo_root, self.rank_nodes, self.bm25_h, self.bm25, self.file_ids = pickle.load(f)
            return
        files = _git_out(ws, "ls-files", "-z").split("\0") + _git_out(ws, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        docs = []
        for rel in dict.fromkeys(f for f in files if f):
            p = ws / rel
            if p.suffix not in _INDEX_EXT or _is_test_path(rel) or not p.is_file():
                continue
            try:
                docs.append({"id": rel, "content": rel + "\n" + p.read_text(encoding="utf-8", errors="replace")})
            except OSError:
                continue
        docs_path.write_text("".join(json.dumps(d) + "\n" for d in docs), encoding="utf-8")
        structure = result_to_structure(docs)
        remove_redundancy(structure)
        self.repo_root = dict_to_tree(None, structure)
        self.rank_nodes = collect_rankable_nodes(self.repo_root)
        self.bm25_h = BM25Okapi(["\n".join([n.qualified_name()] + n.text).split() for n in self.rank_nodes] or [[""]])
        self.file_ids = [d["id"] for d in docs]
        self.bm25 = BM25Okapi([d["content"].split() for d in docs] or [[""]])
        with pk.open("wb") as f:
            pickle.dump((self.repo_root, self.rank_nodes, self.bm25_h, self.bm25, self.file_ids), f)

    # -- follow-up requests ------------------------------------------------------------------------------------
    def _create_node(self, last_action: dict = None):
        node = super()._create_node(last_action)
        node.gui_request = self.gui_request
        self.gui_nodes[node.id] = node
        return node

    def _get_root_commit(self) -> str:
        # Each request's patches, edit detection and augmentation are relative to where that request started.
        if self.gui_request_root is not None:
            return self.gui_request_root.commit
        return super()._get_root_commit()

    def _create_pseudo_root(self):
        super()._create_pseudo_root()
        if self._gui_pending_request:
            self._gui_pending_request = False
            root = self.tree_node
            root.state_hash = "empty"  # like the first request's root: no changes relative to itself
            # each request may bring its own reproduction tests (config: reproduction), so measure this root afresh
            root.test_status = (self._get_test_status() if self.config.shape_reward else None) or []
            self.gui_request_root = root
            self._gui_log_baseline(root)

    def _baseline_failure_count(self) -> int:
        # the core uses the first pseudo-root; in a chat each request's tests are measured at its own root
        if self.gui_request_root is None:
            return super()._baseline_failure_count()
        tests = self._normalize_test_status_entries(self.gui_request_root.test_status or [])
        return max(1, sum(1 for st in tests.values() if st in {"FAILED", "ERROR"}))

    def _gui_log_baseline(self, root):
        if not (self.config.shape_reward and self.config.reproduction_patch):
            return
        tests = self._normalize_test_status_entries(root.test_status or [])
        failing = sum(1 for st in tests.values() if st in {"FAILED", "ERROR"})
        self.gui_controller.emit("log", {"level": "info" if tests else "warning", "t": time.time(), "msg":
            f"Reproduction tests at the root: {len(tests)} test(s), {failing} failing" if tests
            else "Reproduction tests could not be run at the root; rewards are not shaped by tests"})

    def _set_task(self, task: str):
        """Re-render the prompts for a new task (mirrors what `_reset` does for the first one)."""
        self.task = task
        self.extra_template_vars["task"] = task
        scores = self.bm25.get_scores(task.split())
        rng = scores.max() - scores.min()
        scores = (scores - scores.min()) / rng if rng > 0 else scores * 0.0
        self.relevance_dict = dict(zip(self.file_ids, scores))
        top = sorted(self.relevance_dict.items(), key=lambda x: x[1], reverse=True)[:10]
        retrieved = [{"file_path": f, "score": f"{v:.4f}"} for f, v in top]
        system = self.render_template(self.config.system_template)
        user = self.render_template(self.config.instance_template)
        retrieval = self.render_template(self.config.retrieval_template, retrieved_docs=retrieved)
        self.SYSTEM_PROMPT, self.USER_PROMPT = system, user
        self.candidates = [
            {"SYSTEM_PROMPT": system, "USER_PROMPT": user},
            {"SYSTEM_PROMPT": system, "USER_PROMPT": user + "\n\n" + retrieval},
        ]

    def can_continue(self) -> bool:
        return self.tree_node is not None and self.tree_node.is_terminating and self.tree_node.is_submission

    def attach_to(self, submission_node):
        """Hang the next request under `submission_node` without touching the files.

        The caller prepares the working tree first (the user's current codebase); the next `step()` then commits it
        as the new pseudo-root under this node.
        """
        self.tree_node = submission_node

    def continue_run(self, message: str, task: str, agent_updates: dict | None = None) -> tuple[str, str]:
        """Handle a new user request, starting from the last submission node."""
        if not self.can_continue():
            raise RuntimeError("There is no submission to continue from.")
        sub = self.tree_node
        # Same convention as the interactive agents ("The user added a new task: ...").
        sub.observation = f"The user added a new task: {message}"
        self.add_message("user", sub.observation)
        if agent_updates:
            self.config = self.config.model_copy(update=agent_updates)
            self._gui_cost_budget = self.config.cost_limit
        self.gui_request += 1
        self.refresh_index_if_changed()  # the code may have changed since the last request (accepted results, own edits)
        self._set_task(task)
        # Fresh per-request search state; the tree itself (gui_nodes) is kept.
        self.frontier.reset()
        self.itr, self.n_expanded, self.n_submissions, self.n_backtracks = 1, 0, 0, 0
        self.terminating_nodes, self.rtv, self.action_cache = {}, [], {}
        self.node_map_itr = [{} for _ in range(self.config.itr_limit + 2)]
        self.node_map, self.all_node_map = {}, {}
        self.gui_best, self.gui_trace = None, []
        if self._gui_cost_budget and self._gui_cost_budget > 0:  # the cost limit applies per request
            self.config.cost_limit = self.model.cost + self._gui_cost_budget
        self._gui_pending_request = True
        self._mark(f"Request {self.gui_request}", "Continuing from the last submission node")
        return self._gui_loop()

    def _gui_loop(self) -> tuple[str, str]:
        """The run loop of SingleActionAgent.run, without `_reset()` (the tree is kept)."""
        while True:
            try:
                self.step()
            except NonTerminatingException as e:
                self.add_message("user", str(e))
                self.tree_node.observation = str(e)
            except TerminatingException as e:
                self.add_message("user", str(e))
                self.tree_node.observation = str(e)
                return type(e).__name__, str(e)

    # -- reporting helpers -----------------------------------------------------------------------------------
    def _mark(self, label: str, detail: str = ""):
        """Record a phase marker in the chat trace (consecutive duplicates collapse)."""
        last = self.gui_trace[-1] if self.gui_trace else None
        if last and last.get("kind") == "phase" and last.get("label") == label:
            last["detail"] = detail
            return
        self.gui_trace.append({"kind": "phase", "label": label, "detail": detail, "itr": self.itr})

    def _act(self):
        n = self.tree_node
        self.gui_trace.append({"kind": "step", "id": n.id, "n": self.n_expanded + 1, "itr": self.itr})
        return super()._act()

    def _phase(self, phase: str, detail: str = "", stage: str | None = None):
        self._gui_phase = phase
        if stage is not None:
            self.gui_stage = stage
        self.gui_controller.emit("phase", {"phase": phase, "detail": detail, "stage": self.gui_stage})
        self._snap(force=True, detail=detail)

    def _snap(self, force: bool = False, detail: str = ""):
        now = time.time()
        with self._gui_lock:
            if not force and now - self._gui_last_snap < 0.25:
                return
            self._gui_last_snap = now
        try:
            snap = snapshot_agent(self, self._gui_phase, detail)
        except Exception as e:  # never let visualisation break the search
            self.gui_controller.emit("log", {"level": "warning", "msg": f"snapshot failed: {e!r}"})
            return
        self.gui_controller.emit("snapshot", snap)

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def _reset(self):
        self._phase("Creating root", "Committing the baseline codebase state", "idle")
        self.refresh_index_if_changed()  # before the pseudo-root commit, on the code as the user has it
        super()._reset()  # ranks files for the task with the (possibly refreshed) index
        if self.tree_root.children:
            self._gui_log_baseline(self.tree_root.children[0])
        # BM25 scores are min-max normalised in _reset; when every file scores the same (tiny demo repos)
        # that divides by zero and every reward becomes NaN. Neutralise those entries.
        self.relevance_dict = {k: (0.0 if v != v else v) for k, v in self.relevance_dict.items()}
        self._phase("Ready", "Root created, starting path-level best-first search", "explore")

    def step(self):
        self.gui_controller.checkpoint()
        self._phase("Explore", f"Expansion {self.n_expanded + 1}: expand → score → select best path")
        try:
            return super().step()
        finally:
            self._snap(force=True)
            self.gui_controller.end_of_step()

    def query(self, messages):
        self.gui_controller.checkpoint()
        return super().query(messages)

    # -- expansion / scoring -----------------------------------------------------------------------------------
    def _generate_new_nodes(self, n_actions):
        self._phase("Explore · expanding", f"Sampling {n_actions} candidate actions at depth {self.tree_node.level}", "explore")
        nodes = super()._generate_new_nodes(n_actions)
        self._phase("Explore · scored", f"{len(nodes)} candidates scored by the reward model")
        return nodes

    def _generate_new_node(self, i):
        self.gui_controller.checkpoint()
        node = super()._generate_new_node(i)
        self._snap(force=True, detail=f"Candidate {i + 1} executed, scoring…")
        return node

    def _evaluate_node(self, node):
        self.gui_evaluating.add(node.id)
        self._snap()
        try:
            return super()._evaluate_node(node)
        finally:
            self.gui_evaluating.discard(node.id)
            self.gui_controller.emit("value", {"id": node.id})
            self._snap()

    def _calculate_relevance(self, action, observation) -> float:
        # Without a sentence-transformer server the original retries 3x with sleeps and returns 0.0.
        # Return the same value immediately so local demos are not slowed down.
        if not os.environ.get("SENTENCE_TRANSFORMER_SERVER"):
            return 0.0
        return super()._calculate_relevance(action, observation)

    # -- search control ----------------------------------------------------------------------------------------
    def _backtrack(self, target_node):
        src_node = self.tree_node
        src = src_node.id if src_node is not None else None
        super()._backtrack(target_node)
        if src and src != target_node.id and target_node.parent is not None and src != target_node.parent.id:
            self.gui_controller.emit("backtrack", {"from": src, "to": target_node.id})
            if self._gui_phase.startswith("Explore"):  # other phases backtrack internally (merging, augmentation)
                self.gui_trace.append({"kind": "backtrack", "from": src, "to": target_node.id, "itr": self.itr,
                                       **self._gui_backtrack_context(src_node, target_node)})

    def _gui_step_no(self, node_id) -> int | None:
        for e in reversed(self.gui_trace):
            if e.get("kind") == "step" and e.get("id") == node_id:
                return e.get("n")
        return None

    def _gui_backtrack_context(self, src_node, target_node) -> dict:
        """Where the search jumps to, in terms of the steps shown in the chat: the next step is an alternative to
        `alt_of` (the step of the abandoned path that has the same parent) and continues after `from_step`
        (0 = straight from this request's starting point)."""
        parent = target_node.parent
        start = self.gui_request_root or (self.tree_root.children[0] if self.tree_root and self.tree_root.children else None)
        ctx = {"from_step": 0 if parent is start else self._gui_step_no(parent.id)}
        cur = src_node
        while cur is not None and cur.parent is not None and cur.parent is not parent:
            cur = cur.parent
        if cur is not None and cur.parent is parent and cur is not target_node:
            ctx["alt_of"] = self._gui_step_no(cur.id)
        return ctx

    def _update_iteration(self):
        self._phase("Iteration boundary", f"End of iteration {self.itr}: reconcile, then prune the frontier to top-{self.config.top_k_tree_pruning}", "prune")
        itr = self.itr
        result = super()._update_iteration()
        self._mark("Iteration boundary", f"Iteration {itr} ended; frontier pruned to top-{self.config.top_k_tree_pruning}")
        self._phase("Prune · done", f"Iteration {self.itr} starts from the pruned frontier", "explore")
        return result

    def _coalesce_dual_nodes(self, nodes, k):
        self._phase("Cross-path reconciliation", "Pairing paths that reached the same codebase state into merged nodes", "reconcile")
        before = sum(1 for n in list(self.all_node_map.values()) if n.merged)
        result = super()._coalesce_dual_nodes(nodes, k)
        merged = sum(1 for n in list(self.all_node_map.values()) if n.merged) - before
        if merged:
            self._mark("Cross-path reconciliation", f"{merged} merged node(s) created from compatible paths")
        self._snap(force=True)
        return result

    def _generate_terminating_nodes(self):
        self._phase("Solution augmentation", "Generating a Submit candidate for every unique edited codebase state", "select")
        before = sum(1 for n in list(self.all_node_map.values()) if n.is_terminating)
        result = super()._generate_terminating_nodes()
        added = sum(1 for n in list(self.all_node_map.values()) if n.is_terminating) - before
        if added:
            self._mark("Solution augmentation", f"{added} Submit candidate(s) generated for unsubmitted edit states")
        self._snap(force=True)
        return result

    def _recursive_tournament_voting(self, terminating_nodes):
        self._phase("Recursive tournament voting", f"LLM judge compares {len(terminating_nodes)} candidates head-to-head", "select")
        best = super()._recursive_tournament_voting(terminating_nodes)
        self.gui_best = best.id if best is not None else None
        self._mark("Recursive tournament voting", f"{len(terminating_nodes)} candidates, {len(self.rtv)} round(s)")
        self._phase("Selected", "Tournament winner chosen", "select")
        return best
