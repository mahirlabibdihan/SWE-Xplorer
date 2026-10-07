"""Read-only view of the workspace's git history for the GUI's git graph.

SWE-Xplorer keeps its search state in git: every edit node is a commit, backtracking is `git checkout`, and each
request starts from a pseudo-root commit. The agent works on a detached HEAD, so commits it has moved away from are
not reachable from any ref; we therefore list history starting from the commits referenced by tree nodes.

All commands use `--no-optional-locks` so the GUI never takes the index lock while the agent is running git.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_FMT = "%x1e%H%x1f%P%x1f%s%x1f%ct%x1f%D"
_HEX = re.compile(r"^[0-9a-f]{7,40}$")


def _git(ws: Path, *args: str, stdin: str | None = None, check: bool = True) -> str:
    res = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.quotepath=off", *args],
        cwd=str(ws), input=stdin.encode() if stdin is not None else None,
        capture_output=True, creationflags=_NO_WINDOW,
    )
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return res.stdout.decode("utf-8", errors="replace")


def _parse_log(out: str) -> list[dict]:
    commits = []
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        head, _, rest = rec.partition("\n")
        h, parents, subject, ts, refs = (head.split("\x1f") + [""] * 5)[:5]
        stat = []
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d, f = parts
                stat.append([f, int(a) if a.isdigit() else None, int(d) if d.isdigit() else None])
        commits.append({
            "h": h, "p": parents.split() if parents else [], "s": subject, "t": int(ts or 0),
            "refs": [r.strip() for r in refs.split(",") if r.strip()], "stat": stat,
        })
    return commits


def _clip(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


def git_graph(workspace: str | Path | None, nodes: Iterable, base: str | None, limit: int = 600) -> dict:
    """Commits created during the session (plus a little prior history), annotated with the tree nodes."""
    if not workspace or not Path(workspace).exists():
        return {"available": False, "reason": "No workspace yet: the graph appears once the first run starts."}
    ws = Path(workspace)
    nodes = list(nodes)
    by_commit: dict[str, list] = {}
    for n in nodes:
        if getattr(n, "commit", None):
            by_commit.setdefault(n.commit, []).append(n)
    try:
        head = _git(ws, "rev-parse", "HEAD").strip()
    except RuntimeError as e:
        return {"available": False, "reason": str(e)}
    starts = sorted(set(by_commit) | {head} | ({base} if base else set()))
    exclude = []
    if base:
        exclude = [f"^{p}" for p in _git(ws, "rev-parse", f"{base}^@", check=False).split()]
    out = _git(ws, "log", "--topo-order", "--numstat", f"-n{limit}", f"--format={_FMT}", "--stdin", *exclude,
               stdin="\n".join(starts) + "\n", check=False)
    commits = _parse_log(out)
    history = []
    if base and exclude:
        history = _parse_log(_git(ws, "log", "-n5", f"--format={_FMT}", f"{base}^", check=False))
    dirty = [l for l in _git(ws, "status", "--porcelain", check=False).splitlines() if l.strip()]

    for c in commits:
        ns = by_commit.get(c["h"], [])
        creator = next((n for n in ns if getattr(n, "modifies_code", False)), None)
        root = next((n for n in ns if not getattr(n, "last_action", None)), None)
        if c["h"] == base:
            c["kind"] = "baseline"
        elif creator is not None:
            c["kind"] = "edit"
        elif root is not None:
            c["kind"] = "root"
        else:
            c["kind"] = "other"
        pick = creator or root or (ns[0] if ns else None)
        if pick is not None:
            la = getattr(pick, "last_action", None) or {}
            c["node"] = {
                "id": pick.id, "type": la.get("type") or ("root" if not la else None),
                "cmd": _clip(la.get("command"), 160), "rq": getattr(pick, "gui_request", 1),
            }
        c["nodes"] = [n.id for n in ns]
        c["visible"] = any(getattr(n, "visible", True) or getattr(n, "executed", False) for n in ns) if ns else True
    return {"available": True, "head": head, "base": base, "dirty": len(dirty), "dirty_files": dirty[:30],
            "commits": commits, "history": history, "workspace": str(ws)}


def git_show(workspace: str | Path, h: str) -> str:
    if not _HEX.match(h or ""):
        raise ValueError("bad commit hash")
    out = _git(Path(workspace), "show", "--stat", "--patch", "--format=commit %H%nparents %P%n%n    %s%n", h)
    return out if len(out) < 80_000 else out[:80_000] + "\n… (truncated)"


def uncommitted_diff(workspace: str | Path) -> str:
    out = _git(Path(workspace), "diff", "HEAD", "--stat", "--patch", check=False)
    return out if len(out) < 80_000 else out[:80_000] + "\n… (truncated)"
