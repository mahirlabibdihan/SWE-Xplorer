"""Local bash environment + workspace preparation for the GUI.

On Windows the agent's commands (heredocs, ``sed -i``, ``nl``, ``python3 - << EOF`` ...) need a POSIX shell.
We use Git for Windows' bash (no WSL needed). On Linux/macOS the system bash is used.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

IS_WINDOWS = os.name == "nt"


def to_posix_path(path: str | Path) -> str:
    """``D:\\a\\b`` -> ``/d/a/b`` (Git Bash / MSYS style). No-op on POSIX systems."""
    path = str(path)
    if not IS_WINDOWS:
        return path
    path = path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/?(.*)$", path)
    if m:
        return f"/{m.group(1).lower()}/{m.group(2)}".rstrip("/") or "/"
    return path


def find_bash() -> str | None:
    """Locate a usable bash. On Windows prefer Git for Windows (never the WSL launcher in System32)."""
    if not IS_WINDOWS:
        return shutil.which("bash") or ("/bin/bash" if Path("/bin/bash").exists() else None)
    candidates: list[Path] = []
    if git := shutil.which("git"):
        git_root = Path(git).resolve().parent.parent  # .../Git/cmd/git.exe -> .../Git
        candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
    for base in [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")]:
        if base:
            candidates += [Path(base) / "Git" / "bin" / "bash.exe", Path(base) / "Programs" / "Git" / "bin" / "bash.exe"]
    for c in candidates:
        if c.exists():
            return str(c)
    which = shutil.which("bash")
    if which and "system32" not in which.lower():
        return which
    return None


class LocalBashEnvironmentConfig(BaseModel):
    cwd: str = ""
    env: dict[str, str] = {}
    timeout: int = 120
    clean_start: bool = True
    """Read by the tree-search agent (`_get_root_commit`)."""
    checkpoint: str | None = None
    """Read by the tree-search agent (`_reset`); a saved tree to replay in simulation mode."""
    bash_path: str | None = None
    python: str | None = None
    """Interpreter behind `python`/`python3` in commands (default: the one running the GUI)."""
    path_prepend: list[str] = []
    """Extra directories put first on PATH, e.g. a virtualenv's Scripts/bin folder."""
    virtual_env: str | None = None
    pythonpath_workspace: bool = True
    """Put the workspace root on PYTHONPATH so `import <package>` loads the sandbox copy the agent is editing,
    even for scripts in sub-folders (e.g. Django's tests/runtests.py)."""
    rewrite_global_git_config: bool = True
    """The agent runs `git config --global user.name ...` at start-up. On the user's machine that must not change
    their global (or repo) git config, so it becomes a no-op; the agent's internal commits get a neutral identity
    through the GIT_AUTHOR_* / GIT_COMMITTER_* environment variables instead."""


class LocalBashEnvironment:
    """Execute commands with bash directly on this machine, inside the chosen working directory."""

    def __init__(self, *, config_class: type = LocalBashEnvironmentConfig, **kwargs):
        self.config = config_class(**kwargs)
        self.bash = self.config.bash_path or find_bash()
        if not self.bash:
            msg = "Could not find bash. On Windows install Git for Windows (https://git-scm.com/download/win)."
            raise RuntimeError(msg)
        self._tmp = Path(tempfile.mkdtemp(prefix="mswea-gui-env-"))
        self._shim_dir = self._make_python_shim()
        self._proc: subprocess.Popen | None = None

    # -- setup -------------------------------------------------------------------------------------------------
    def _make_python_shim(self) -> Path | None:
        """Make `python3`/`python` inside bash point at the interpreter running the GUI.

        On Windows `python3` usually resolves to the Microsoft Store stub, which breaks the agent's
        `python3 - << 'EOF'` indexing script and any reproduction scripts it writes.
        """
        shim = self._tmp / "bin"
        shim.mkdir(parents=True, exist_ok=True)
        exe = to_posix_path(self.config.python or sys.executable)
        for name in ("python3", "python"):
            p = shim / name
            p.write_text(f'#!/bin/sh\nexec "{exe}" "$@"\n', encoding="utf-8", newline="\n")
            p.chmod(0o755)
        return shim

    def _script(self, command: str, cwd: str) -> str:
        lines = ["#!/bin/bash"]
        front = [to_posix_path(p) for p in ([str(self._shim_dir)] if self._shim_dir else []) + list(self.config.path_prepend)]
        if front:
            lines.append(f'export PATH="{":".join(front)}:$PATH"')
        lines.append(f'cd "{to_posix_path(cwd)}" || exit 1')
        lines.append(command)
        return "\n".join(lines) + "\n"

    def _rewrite(self, command: str) -> str:
        if self.config.rewrite_global_git_config:
            command = re.sub(r"\bgit\s+config\s+--global\s+[^&;|]*", "true ", command)
        return command

    # -- protocol ----------------------------------------------------------------------------------------------
    def execute(self, command: str, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        cwd = cwd or self.config.cwd or os.getcwd()
        timeout = timeout or self.config.timeout
        stamp = time.time_ns()
        script = self._tmp / f"cmd_{stamp}.sh"
        launcher = self._tmp / f"run_{stamp}.sh"
        script.write_text(self._script(self._rewrite(command), cwd), encoding="utf-8", newline="\n")
        # coreutils `timeout` kills the whole process group, including MSYS children on Windows that
        # taskkill cannot see; the Python-side timeout below is only a fallback.
        launcher.write_text(
            f'timeout -k 3 {int(timeout)} bash --noprofile --norc "{to_posix_path(script)}"\n', encoding="utf-8", newline="\n"
        )
        # No __pycache__ in the repo: keeps `git add -A` patches free of .pyc files (config `env` can override).
        env = os.environ | {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"} | {
            f"GIT_{who}_{what}": val for who in ("AUTHOR", "COMMITTER")
            for what, val in (("NAME", "SWE-Xplorer"), ("EMAIL", "swe-xplorer@localhost"))
        }
        if self.config.virtual_env:
            env["VIRTUAL_ENV"] = self.config.virtual_env
            env.pop("PYTHONHOME", None)
        if self.config.pythonpath_workspace:
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(cwd), os.environ.get("PYTHONPATH", "")) if p)
        env |= self.config.env
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        chunks: list[bytes] = []
        t0 = time.time()
        try:
            proc = subprocess.Popen(
                [self.bash, "--noprofile", "--norc", to_posix_path(launcher)],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
            reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: proc.stdout.read(65536), b"")), daemon=True)
            reader.start()
            self._proc = proc
            try:
                proc.wait(timeout=timeout + 10)
                reader.join(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)
                reader.join(timeout=3)
                raise subprocess.TimeoutExpired(command, timeout, output=b"".join(chunks))
            out = b"".join(chunks)
            if proc.returncode in (124, 137) and time.time() - t0 >= timeout - 0.5:
                raise subprocess.TimeoutExpired(command, timeout, output=out)  # agents decode e.output as bytes
            return {"output": out.decode("utf-8", errors="replace").replace("\r\n", "\n"), "returncode": proc.returncode}
        finally:
            script.unlink(missing_ok=True)
            launcher.unlink(missing_ok=True)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()

    def get_template_vars(self) -> dict[str, Any]:
        return self.config.model_dump() | platform.uname()._asdict() | dict(os.environ)

    def abort(self) -> None:
        """Kill the command that is running right now (used by Stop); the agent sees it as a failed command."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill_tree(proc)  # execute() then stops waiting and returns what was printed so far

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


# ======================================================================================================================
# Workspace preparation
# ======================================================================================================================


def _git(*args: str, cwd: str | Path, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=check, **kw,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0,
    )


def git_run(repo: str | Path, *args: str) -> str:
    """Run a git command in `repo` and return stdout (raises CalledProcessError with stderr on failure)."""
    return _git("-c", "user.name=SWE-Xplorer", "-c", "user.email=swe-xplorer@localhost", *args, cwd=repo).stdout.decode("utf-8", errors="replace")


def git_toplevel(path: str | Path) -> Path | None:
    try:
        out = _git("rev-parse", "--show-toplevel", cwd=path).stdout.decode().strip()
        return Path(out) if out else None
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return None


def repo_is_dirty(repo: str | Path) -> bool:
    return bool(_git("status", "--porcelain", cwd=repo).stdout.strip())


def prepare_workspace(
    source: str | Path,
    run_dir: Path,
    *,
    mode: str = "clone",
    include_uncommitted: bool = True,
    log=print,
) -> Path:
    """Return the directory the agent will work in.

    mode="clone":   `git clone` the repo into `run_dir/workspace` (the original repo is never modified).
    mode="inplace": work directly in the repo (the agent will create commits, detach HEAD and run
                    `git reset --hard` / `git clean -fd` there).
    """
    top = git_toplevel(source)
    if top is None:
        msg = f"{source} is not inside a git repository."
        raise ValueError(msg)
    if mode == "inplace":
        return top

    dst = run_dir / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {top} -> {dst}")
    # core.autocrlf=false: keep files byte-identical to the repo (LF), which is what the agent's sed/diff expect.
    _git("clone", "--no-hardlinks", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", str(top), str(dst), cwd=run_dir)
    head = _git("rev-parse", "HEAD", cwd=top).stdout.decode().strip()
    _git("checkout", "--detach", head, cwd=dst)  # clone checks out the default branch; match the source HEAD
    _git("config", "user.name", "mini-swe-agent-gui", cwd=dst)
    _git("config", "user.email", "mini-swe-agent-gui@localhost", cwd=dst)

    if include_uncommitted:
        _copy_uncommitted(top, dst, log)
    return dst


def _copy_uncommitted(top: Path, dst: Path, log=print) -> None:
    """Bring `top`'s uncommitted changes (tracked diffs + untracked, non-ignored files) into `dst`."""
    if not repo_is_dirty(top):
        return
    log("Copying uncommitted changes into the workspace")
    diff = _git("diff", "HEAD", "--binary", cwd=top).stdout
    if diff.strip():
        res = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", "-"], input=diff, cwd=str(dst), capture_output=True)
        if res.returncode != 0:
            log(f"Warning: could not apply uncommitted changes: {res.stderr.decode(errors='replace')}")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z", cwd=top).stdout.decode(errors="replace")
    for rel in filter(None, untracked.split("\0")):
        src_f, dst_f = top / rel, dst / rel
        if src_f.is_file():
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, dst_f)


def sync_workspace_to_source(ws: str | Path, source: str | Path, message: str, *, include_uncommitted: bool = True, log=print) -> str:
    """Make the sandbox match the user's repository *as it is now* and commit that state.

    Takes the source's current HEAD (fetched, so new user commits are included) plus its uncommitted and untracked
    changes, i.e. exactly what a first request would start from. Returns the new commit.
    """
    ws, top = Path(ws), git_toplevel(source)
    if top is None:
        raise ValueError(f"{source} is not a git repository")
    _git("fetch", "-q", "--no-tags", str(top), "HEAD", cwd=ws)
    _git("reset", "-q", "--hard", cwd=ws)
    _git("clean", "-fdq", cwd=ws)
    _git("checkout", "-q", "--detach", "-f", "FETCH_HEAD", cwd=ws)
    if include_uncommitted:
        _copy_uncommitted(top, ws, log)
    git_run(ws, "add", "-A")
    git_run(ws, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
    return git_run(ws, "rev-parse", "HEAD").strip()


def split_patch(patch: str) -> tuple[str, list[str]]:
    """Drop per-file sections that are binary (e.g. stray .pyc files). Returns (text patch, skipped paths)."""
    sections = re.split(r"(?m)^(?=diff --git )", patch)
    keep, skipped = [], []
    for sec in sections:
        if not sec.strip():
            continue
        if "\nGIT binary patch" in sec or "\nBinary files " in sec:
            m = re.match(r"diff --git a/(\S+)", sec)
            skipped.append(m.group(1) if m else "?")
        else:
            keep.append(sec)
    return "".join(keep), skipped


def apply_patch_to_repo(repo: str | Path, patch: str) -> tuple[bool, str]:
    """Apply a unified diff (as produced by `git diff --cached`) to `repo`'s working tree. Checks first."""
    text, skipped = split_patch(patch)
    if not text.strip():
        return False, "The patch only contains binary files."
    data = text if text.endswith("\n") else text + "\n"
    check = subprocess.run(["git", "apply", "--check", "-"], input=data.encode(), cwd=str(repo), capture_output=True)
    if check.returncode != 0:
        return False, check.stderr.decode(errors="replace")
    res = subprocess.run(["git", "apply", "-"], input=data.encode(), cwd=str(repo), capture_output=True)
    msg = (res.stderr or res.stdout).decode(errors="replace")
    if skipped:
        msg += f"Skipped binary files: {', '.join(skipped)}"
    return res.returncode == 0, msg



# ======================================================================================================================
# Python environment of the repository
# ======================================================================================================================

VENV_NAMES = (".venv", "venv", "env")


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def _venv_version(venv: Path) -> str:
    try:
        for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("version", "version_info"):
                return v.strip()
    except OSError:
        pass
    return ""


def resolve_python(repo: str | Path, override: str | None = None) -> dict:
    """Which Python the agent's commands use: `override` (a venv folder or an interpreter), else a virtualenv inside
    the repository (.venv / venv / env), else the Python running the GUI."""
    venv = None
    if override:
        p = Path(override).expanduser()
        if p.is_dir() and (p / "pyvenv.cfg").exists():
            venv = p
        elif p.is_file():
            if (p.parent.parent / "pyvenv.cfg").exists():
                venv = p.parent.parent
            else:
                return {"python": str(p), "venv": None, "bin": str(p.parent), "version": "", "source": "custom", "label": p.name}
        else:
            raise ValueError(f"Python not found: {override} (give a virtualenv folder or a python executable)")
    else:
        for name in VENV_NAMES:
            cand = Path(repo) / name
            if (cand / "pyvenv.cfg").exists() and _venv_python(cand).exists():
                venv = cand
                break
    if venv is not None:
        exe = _venv_python(venv)
        if not exe.exists():
            raise ValueError(f"No interpreter in {venv}")
        ver = _venv_version(venv)
        return {"python": str(exe), "venv": str(venv), "bin": str(exe.parent), "version": ver,
                "source": "custom" if override else "repo", "label": f"{venv.name}{' · ' + ver if ver else ''}"}
    return {"python": sys.executable, "venv": None, "bin": None, "version": platform.python_version(), "source": "gui",
            "label": f"GUI Python {platform.python_version()}"}
