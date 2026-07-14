"""Local-only Web UI for skill-sync."""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from skill_sync import core
from skill_sync.errors import SkillSyncError


STATIC_DIR = Path(__file__).with_name("web_static")


def serve(host: str = "127.0.0.1", port: int = 8765, config_path: str | None = None, open_browser: bool = True) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SkillSyncError("Web UI may only bind to a loopback address")
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), _handler_factory(config_path, token))
    url = f"http://{host}:{server.server_port}"
    print(f"Skill Sync Web UI: {url}")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_factory(config_path: str | None, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/state":
                self._json(_state(config_path))
                return
            if path == "/api/preview":
                self._json(core.sync_preview(config_path=config_path, fetch_remote=False))
                return
            if path == "/api/token":
                self._json({"token": token})
                return
            filename = "index.html" if path == "/" else path.lstrip("/")
            if filename not in {"index.html", "app.js", "style.css", "remixicon.css", "remixicon.woff2"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content = (STATIC_DIR / filename).read_bytes()
            content_type = {
                ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                ".woff2": "font/woff2",
            }[Path(filename).suffix]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            if self.headers.get("X-Skill-Sync-Token") != token:
                self._json({"error": "invalid request token"}, HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                path = urlparse(self.path).path
                kwargs = {"skill_names": body.get("skills"), "config_path": config_path}
                if path == "/api/init":
                    result = core.init_sync(
                        body.get("repo", ""),
                        sync_dir=body.get("sync_dir") or None,
                        branch=body.get("branch") or "main",
                        platform=None,
                        skills_root=body.get("skills_root") or None,
                        config_path=config_path,
                    )
                elif path == "/api/sync":
                    result = core.sync(**kwargs)
                elif path == "/api/link":
                    result = core.link_skills(agent_names=body.get("agents"), **kwargs)
                elif path == "/api/unlink":
                    result = core.unlink_skills(agent_names=body.get("agents"), **kwargs)
                elif path == "/api/select":
                    result = core.select_skills(body.get("skills", []), platform=None, config_path=config_path)
                elif path == "/api/deselect":
                    result = core.deselect_skills(body.get("skills", []), config_path=config_path)
                elif path == "/api/import":
                    result = core.import_agent_skills(
                        body.get("skills", []), body.get("agent", ""), config_path=config_path
                    )
                elif path == "/api/copy":
                    result = core.copy_global_skills_to_agents(
                        body.get("skills", []), body.get("agents", []), config_path=config_path
                    )
                elif path == "/api/delete":
                    result = core.delete_global_skills(
                        body.get("skills", []), config_path=config_path
                    )
                elif path == "/api/agent":
                    if body.get("enabled"):
                        result = core.enable_agent_sync(body.get("agent", ""), config_path=config_path)
                    else:
                        result = core.disable_agent_sync(body.get("agent", ""), config_path=config_path)
                elif path == "/api/backup":
                    result = core.backup_global_skill(body.get("skill", ""), config_path=config_path)
                else:
                    self._json({"error": "unknown action"}, HTTPStatus.NOT_FOUND)
                    return
                self._json({"result": result, "state": _state(config_path)})
            except (SkillSyncError, ValueError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            content = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _state(config_path: str | None) -> dict[str, Any]:
    preview = core.sync_preview(config_path=config_path, fetch_remote=False)
    if not preview["initialized"]:
        agents = [
            {"name": agent.name, "display_name": agent.display_name, "detected": agent.detected,
             "enabled": True, "skills_dir": str(agent.skills_dir), "skills_dirs": [str(path) for path in agent.skill_dirs]}
            for agent in core.detect_agents()
        ]
        return {
            "schema_version": 1,
            "initialized": False,
            "preview": preview,
            "status": {"skills": []},
            "doctor": {"agents": agents, "matrix": [], "issues": []},
            "import_candidates": [],
        }
    diagnosis = core.doctor(config_path=config_path)
    try:
        sync_status = core.status(config_path=config_path, fetch_remote=False)
    except SkillSyncError as exc:
        sync_status = {"error": str(exc), "skills": []}
    selected = {item["name"]: item for item in sync_status.get("skills", [])}
    try:
        candidates = core.scan_skills(platform=None, config_path=config_path)
    except SkillSyncError:
        candidates = []
    skills: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        skills[candidate["name"]] = {
            "name": candidate["name"],
            "local_path": candidate["path"],
            "description": candidate.get("description", ""),
            "selected": candidate["selected"],
            "changed_local": False,
        }
    for name, item in selected.items():
        skills[name] = {**skills.get(name, {}), **item}
    matrix = {(item["skill"], item["agent"]): item["state"] for item in diagnosis["matrix"]}
    for skill in skills.values():
        skill["agents"] = {agent["name"]: matrix.get((skill["name"], agent["name"]), "not-detected") for agent in diagnosis["agents"]}
    sync_status["skills"] = [skills[name] for name in sorted(skills)]
    try:
        import_candidates = core.scan_import_candidates(config_path=config_path)
    except SkillSyncError:
        import_candidates = []
    return {
        "schema_version": 1,
        "initialized": True,
        "preview": preview,
        "status": sync_status,
        "doctor": diagnosis,
        "import_candidates": import_candidates,
    }
