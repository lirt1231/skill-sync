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
from urllib.parse import parse_qs, urlparse

from skill_sync import core
from skill_sync.errors import SkillSyncError


STATIC_DIR = Path(__file__).with_name("web_static")
STATE_VIEWS = ("summary", "inventory", "agents", "import-candidates")


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
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/state":
                try:
                    views = _query_views(parsed.query)
                except SkillSyncError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(_state(config_path, views=views))
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
                views = _body_views(body.get("views"))
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
                try:
                    next_state = _state(config_path, views=views)
                except (SkillSyncError, ValueError, OSError) as exc:
                    self._json(
                        {
                            "error": "operation completed but state refresh failed",
                            "result": result,
                            "state": None,
                            "state_error": str(exc),
                            "mutation_applied": True,
                        },
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._json({"result": result, "state": next_state})
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


def _query_views(query: str) -> tuple[str, ...] | None:
    requested = tuple(parse_qs(query).get("view", ()))
    return _validate_views(requested) if requested else None


def _body_views(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillSyncError("views must be a list of Web state view names")
    return _validate_views(tuple(value))


def _validate_views(views: tuple[str, ...]) -> tuple[str, ...]:
    unknown = set(views) - set(STATE_VIEWS)
    if unknown:
        raise SkillSyncError("unknown Web state view: " + ", ".join(sorted(unknown)))
    # Keep the caller's ordering for predictable incremental state merges while
    # ensuring a repeated query parameter cannot trigger duplicate core reads.
    return tuple(dict.fromkeys(views))


def _state(
    config_path: str | None,
    *,
    views: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    legacy_full_state = views is None
    requested = STATE_VIEWS if views is None else _validate_views(views)
    initialized = core.is_initialized(config_path)
    diagnosis = (
        core.doctor(config_path=config_path)
        if initialized and "agents" in requested
        else None
    )
    preview = None
    if "summary" in requested:
        preview = core.sync_preview(
            config_path=config_path,
            fetch_remote=False,
            **({"diagnosis": diagnosis} if diagnosis is not None else {}),
        )
        initialized = preview["initialized"]

    result: dict[str, Any] = {
        "schema_version": 1,
        "initialized": initialized,
    }
    if not legacy_full_state:
        result["loaded_views"] = list(requested)

    if not initialized:
        if "summary" in requested:
            result["preview"] = preview
        if "inventory" in requested:
            result["status"] = {"skills": []}
        if "agents" in requested:
            agents = [
                {
                    "name": agent.name,
                    "display_name": agent.display_name,
                    "detected": agent.detected,
                    "enabled": True,
                    "skills_dir": str(agent.skills_dir),
                    "skills_dirs": [str(path) for path in agent.skill_dirs],
                }
                for agent in core.detect_agents()
            ]
            result["doctor"] = {"agents": agents, "matrix": [], "issues": []}
        if "import-candidates" in requested:
            result["import_candidates"] = []
        return result

    if "summary" in requested:
        result["preview"] = preview
    if "agents" in requested:
        result["doctor"] = diagnosis
    if "inventory" in requested:
        result["status"] = _inventory(config_path, diagnosis=diagnosis)
    if "import-candidates" in requested:
        try:
            result["import_candidates"] = core.scan_import_candidates(
                config_path=config_path
            )
        except SkillSyncError:
            result["import_candidates"] = []
    return result


def _inventory(
    config_path: str | None,
    *,
    diagnosis: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    if diagnosis is not None:
        matrix = {
            (item["skill"], item["agent"]): item["state"]
            for item in diagnosis["matrix"]
        }
        for skill in skills.values():
            skill["agents"] = {
                agent["name"]: matrix.get(
                    (skill["name"], agent["name"]), "not-detected"
                )
                for agent in diagnosis["agents"]
            }
    sync_status["skills"] = [skills[name] for name in sorted(skills)]
    return sync_status
