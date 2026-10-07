"""Localhost web server for the SWE-Xplorer demo GUI (standard library only).

Security without tokens: binds to 127.0.0.1 only, rejects requests whose Host header is not localhost
(DNS-rebinding protection) and rejects state-changing POSTs whose Origin is not this page (so another website
open in the browser cannot start runs). Responses carry no CORS headers, so other origins cannot read them.
"""

from __future__ import annotations

import json
import mimetypes
import platform
import queue
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from minisweagent import __version__
from minisweagent.gui import runner
from minisweagent.gui.environment import find_bash

STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.manager = runner.RunManager()


def make_handler(app: App):
    allowed_hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}", f"[::1]:{app.port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "swe-xplorer-gui"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -- helpers -----------------------------------------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, default=str).encode("utf-8"))

        def _error(self, msg: str, code: int = 400):
            self._json({"error": msg}, code)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 50 * 1024 * 1024:
                raise ValueError("Request too large")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _origin_ok(self) -> bool:
            # Browsers always send Origin on POST; non-browser local clients (curl, scripts) may omit it.
            origin = self.headers.get("Origin")
            return origin is None or origin in allowed_origins

        # -- routing ------------------------------------------------------------------------------------------
        def do_GET(self):
            if not self._host_ok():
                return self._error("bad host", 403)
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                return self._static("index.html")
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            try:
                return self._api_get(url.path, q)
            except Exception as e:
                return self._error(f"{type(e).__name__}: {e}", 500)

        def do_POST(self):
            if not self._host_ok() or not self._origin_ok():
                return self._error("forbidden", 403)
            url = urlparse(self.path)
            try:
                return self._api_post(url.path, self._body())
            except (ValueError, RuntimeError, yaml.YAMLError) as e:
                return self._error(str(e), 400)
            except Exception as e:
                return self._error(f"{type(e).__name__}: {e}", 500)

        def _static(self, rel: str):
            p = (STATIC / rel).resolve()
            if not str(p).startswith(str(STATIC.resolve())) or not p.is_file():
                return self._error("not found", 404)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, p.read_bytes(), ctype)

        # -- API ----------------------------------------------------------------------------------------------
        def _api_get(self, path: str, q: dict):
            m = app.manager
            if path == "/api/info":
                s = m.settings
                return self._json({
                    "version": __version__,
                    "platform": platform.system(),
                    "python": sys.version.split()[0],
                    "bash": find_bash(),
                    "configs": runner.list_configs(),
                    "schema": runner.agent_field_schema(),
                    "model_classes": runner.MODEL_CLASSES,
                    "keys": runner.key_status(s),
                    "settings": {k: s.get(k) for k in ("repo", "task", "workspace_mode", "include_uncommitted", "runs_dir",
                                                        "config_path", "config_text", "reward_same_as_policy", "python")},
                    "default_runs_dir": str(runner.DEFAULT_RUNS_DIR),
                    "app_dir": str(runner.APP_DIR),
                    "keys_file": str(runner.KEYS_FILE),
                    "prefs": s.get("prefs", {}),
                    "draft": runner.load_draft(),
                    "status": m.bus.status,
                    "running": m.running,
                    "session": m.session_view(),
                })
            if path == "/api/config":
                return self._json(runner.load_config(q["path"]))
            if path == "/api/repo":
                return self._json(runner.repo_info(q.get("path"), q.get("python") or None))
            if path == "/api/browse":
                return self._json(runner.browse(q.get("path")))
            if path == "/api/node":
                d = m.node(q.get("id", ""))
                return self._json(d) if d else self._error("unknown node", 404)
            if path == "/api/history":
                return self._json({"snapshots": m.bus.snapshots, "logs": m.bus.logs[-1500:], "status": m.bus.status,
                                   "phase": m.bus.last_phase, "result": m.result, "session": m.session_view()})
            if path == "/api/sessions":
                return self._json({"sessions": m.list_sessions()})
            if path == "/api/session":
                return self._json({"session": m.session_view()})
            if path == "/api/git":
                return self._json(m.git_graph())
            if path == "/api/git/show":
                return self._json({"text": m.git_show(q.get("h", ""))})
            if path == "/api/trace":
                return self._json({"trace": m.trace(), "viewing": m.viewing})
            if path == "/api/events":
                return self._sse()
            return self._error("not found", 404)

        def _api_post(self, path: str, body: dict):
            m = app.manager
            if path == "/api/config/parse":
                data = yaml.safe_load(body.get("text", "")) or {}
                if not isinstance(data, dict):
                    raise ValueError("Top level of the YAML must be a mapping")
                return self._json({"data": data})
            if path == "/api/config/dump":
                return self._json({"text": runner.dump_yaml(body.get("data") or {})})
            if path == "/api/config/save":
                target = Path(body["path"]).expanduser()
                if target.suffix not in (".yaml", ".yml"):
                    raise ValueError("Config file must end with .yaml or .yml")
                if target.exists() and not body.get("overwrite"):
                    return self._json({"exists": True, "path": str(target)}, 409)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(runner.dump_yaml(body["data"]), encoding="utf-8")
                return self._json({"ok": True, "path": str(target)})
            if path == "/api/keys":
                runner.apply_keys(m.settings, body.get("keys", {}), bool(body.get("remember", True)))
                return self._json({"keys": runner.key_status(m.settings)})
            if path == "/api/prefs":
                prefs = m.settings.setdefault("prefs", {})
                prefs.update({k: v for k, v in (body or {}).items() if isinstance(k, str) and len(k) < 64})
                runner.save_settings(m.settings)
                m.bus.emit("prefs", {"prefs": prefs})  # keep every open view in sync (web page, VS Code sidebar)
                return self._json({"prefs": prefs})
            if path == "/api/draft":
                runner.save_draft(body.get("draft"))
                return self._json({"ok": True})
            if path == "/api/session/open":
                return self._json({"session": m.open_session(body.get("dir", ""))})
            if path == "/api/session/new":
                m.new_session()
                return self._json({"ok": True})
            if path in ("/api/run", "/api/chat"):
                m.settings["reward_same_as_policy"] = bool(body.get("reward_same_as_policy"))
                m.start(body)
                return self._json({"ok": True})
            if path == "/api/control":
                m.control(body.get("action", ""))
                return self._json({"ok": True})
            if path == "/api/decide":
                ok, msg = m.decide(int(body.get("turn", 0)), body.get("decision", ""), bool(body.get("apply", True)))
                return self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            if path == "/api/apply_patch":
                ok, msg = m.apply_patch()
                return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            if path == "/api/load_tree":
                tree = body.get("tree")
                if tree is None and body.get("path"):
                    tree = json.loads(Path(body["path"]).expanduser().read_text(encoding="utf-8"))
                if not isinstance(tree, dict) or "id" not in tree:
                    raise ValueError("Not a tree JSON (expected the nested format written as *.tree.json)")
                return self._json(m.load_tree(tree, body.get("name", "")))
            return self._error("not found", 404)

        def _sse(self):
            bus = app.manager.bus
            q = bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        evt = q.get(timeout=15)
                        data = json.dumps(evt, default=str)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                bus.unsubscribe(q)
                self.close_connection = True

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    app = App(host, port)
    httpd = None
    for p in range(port, port + 20):
        try:
            app.port = p
            httpd = ThreadingHTTPServer((host, p), make_handler(app))
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"No free port in {port}-{port + 19}")
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{app.port}/"
    print(f"\n  SWE-Xplorer demo running at:\n  {url}\n\n  (Ctrl+C to quit)\n", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down…")
        if app.manager.controller:
            app.manager.controller.stop()
    finally:
        httpd.server_close()
