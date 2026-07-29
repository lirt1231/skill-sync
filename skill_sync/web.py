"""Local-only Web UI for skill-sync."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from skill_sync import core, variant_source
from skill_sync.errors import SkillSyncError


STATIC_DIR = Path(__file__).with_name("web_static")
STATE_VIEWS = (
    "summary",
    "inventory",
    "agents",
    "managed",
    "import-candidates",
)


class _EditDeleteTasks:
    """Run local edit-session deletion without holding an HTTP response open."""

    def __init__(self, config_path: str | None) -> None:
        self.config_path = config_path
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._by_session: dict[str, str] = {}

    def enqueue(self, session_id: str) -> dict[str, Any]:
        session = core.edit_session_status(session_id, config_path=self.config_path)
        if session.get("status") in {"applying", "needs-recovery"}:
            raise SkillSyncError(
                f"edit session cannot be deleted while {session.get('status')}",
                code="edit_delete_blocked",
                details={"session_id": session_id, "status": session.get("status")},
            )
        with self._lock:
            existing_id = self._by_session.get(session_id)
            existing = self._tasks.get(existing_id or "")
            if existing and existing["status"] in {"queued", "running"}:
                return dict(existing)
            task_id = secrets.token_urlsafe(18)
            task = {
                "task_id": task_id,
                "session_id": session_id,
                "status": "queued",
                "result": None,
                "error": None,
                "code": None,
            }
            self._tasks[task_id] = task
            self._by_session[session_id] = task_id
        threading.Thread(
            target=self._run,
            args=(task_id, session_id),
            name=f"skill-sync-edit-delete-{session_id[:8]}",
            daemon=True,
        ).start()
        return dict(task)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def _run(self, task_id: str, session_id: str) -> None:
        self._update(task_id, status="running")
        try:
            result = core.edit_delete(session_id, config_path=self.config_path)
        except Exception as exc:  # Background failures must remain observable.
            self._update(
                task_id,
                status="failed",
                error=str(exc),
                code=exc.code if isinstance(exc, SkillSyncError) else "edit_delete_failed",
            )
            return
        self._update(task_id, status="completed", result=result)

    def _update(self, task_id: str, **values: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.update(values)


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
    edit_delete_tasks = _EditDeleteTasks(config_path)

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
            if path == "/api/edit/delete-status":
                task_ids = parse_qs(parsed.query).get("task_id", [])
                if len(task_ids) != 1 or not task_ids[0]:
                    self._json({"error": "task_id is required"}, HTTPStatus.BAD_REQUEST)
                    return
                task = edit_delete_tasks.get(task_ids[0])
                if task is None:
                    self._json({"error": "delete task not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._json({"result": task})
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
                if not isinstance(body, dict):
                    raise SkillSyncError("request body must be a JSON object")
                path = urlparse(self.path).path
                if path == "/api/plan":
                    self._json(
                        core.preview_mutation(
                            body.get("operation"),
                            body.get("request"),
                            config_path=config_path,
                        )
                    )
                    return
                if path == "/api/edit/inspect":
                    request = _edit_request(
                        body,
                        allowed={"session_id"},
                        required={"session_id"},
                    )
                    self._json(
                        {"inspection": _edit_inspection(request["session_id"], config_path)}
                    )
                    return
                if path == "/api/edit/launch":
                    request = _edit_request(
                        body,
                        allowed={"session_id", "agent"},
                        required={"session_id", "agent"},
                    )
                    self._json(
                        {
                            "result": core.launch_edit_agent(
                                request["session_id"],
                                request["agent"],
                                config_path=config_path,
                            )
                        }
                    )
                    return
                if path == "/api/edit/delete":
                    request = _edit_request(
                        body,
                        allowed={"session_id"},
                        required={"session_id"},
                    )
                    self._json(
                        {"result": edit_delete_tasks.enqueue(request["session_id"])},
                        HTTPStatus.ACCEPTED,
                    )
                    return
                views = _body_views(body.get("views"))
                if path == "/api/edit/begin":
                    request = _edit_request(
                        body,
                        allowed={"skill", "scope", "target", "actor", "views"},
                        required={"skill", "scope"},
                    )
                    result = core.edit_begin(
                        request["skill"],
                        scope=request["scope"],
                        target=request.get("target"),
                        actor=request.get("actor"),
                        config_path=config_path,
                    )
                elif path == "/api/edit/apply":
                    request = _edit_request(
                        body,
                        allowed={"session_id", "inspection_id", "views"},
                        required={"session_id", "inspection_id"},
                    )
                    inspection = _edit_inspection(request["session_id"], config_path)
                    if inspection["inspection_id"] != request["inspection_id"]:
                        raise SkillSyncError(
                            "edit session inspection changed; review it and confirm again",
                            code="edit_inspection_changed",
                            details={"inspection": inspection},
                        )
                    if not inspection["can_apply"]:
                        raise SkillSyncError(
                            "edit session cannot be applied safely",
                            code="edit_apply_blocked",
                            details={
                                "session_id": request["session_id"],
                                "blockers": inspection["blockers"],
                            },
                        )
                    result = core.edit_apply(
                        request["session_id"], config_path=config_path
                    )
                elif path == "/api/edit/abort":
                    request = _edit_request(
                        body,
                        allowed={"session_id", "views"},
                        required={"session_id"},
                    )
                    session = core.edit_session_status(
                        request["session_id"], config_path=config_path
                    )
                    if session.get("status") != "active":
                        raise SkillSyncError(
                            "only an active edit session can be aborted",
                            code="edit_abort_blocked",
                            details={
                                "session_id": request["session_id"],
                                "status": session.get("status"),
                            },
                        )
                    result = core.edit_abort(
                        request["session_id"], config_path=config_path
                    )
                else:
                    kwargs = {"skill_names": body.get("skills"), "config_path": config_path}
                    result = None
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
                elif path in {
                    "/api/edit/begin",
                    "/api/edit/apply",
                    "/api/edit/abort",
                }:
                    pass
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
                payload = {"error": str(exc)}
                if isinstance(exc, SkillSyncError):
                    payload.update({"code": exc.code, "details": exc.details})
                self._json(payload, HTTPStatus.BAD_REQUEST)

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


def _edit_request(
    body: dict[str, Any],
    *,
    allowed: set[str],
    required: set[str],
) -> dict[str, Any]:
    unknown = set(body) - allowed
    if unknown:
        raise SkillSyncError(
            "unknown edit request field: " + ", ".join(sorted(unknown)),
            code="edit_request_invalid",
        )
    missing = required - set(body)
    if missing:
        raise SkillSyncError(
            "missing edit request field: " + ", ".join(sorted(missing)),
            code="edit_request_invalid",
        )
    for field in required:
        if not isinstance(body[field], str) or not body[field].strip():
            raise SkillSyncError(
                f"edit request field must be a non-empty string: {field}",
                code="edit_request_invalid",
            )
    if "scope" in body and body["scope"] not in {"base", "family", "client"}:
        raise SkillSyncError(
            "edit scope must be base, family, or client",
            code="edit_request_invalid",
        )
    if body.get("scope") == "base" and body.get("target") is not None:
        raise SkillSyncError(
            "Base edit scope must not have a target",
            code="edit_request_invalid",
        )
    if body.get("scope") in {"family", "client"}:
        target = body.get("target")
        if not isinstance(target, str) or not target.strip():
            raise SkillSyncError(
                f"{body['scope']} edit scope requires a target",
                code="edit_request_invalid",
            )
    if "actor" in body and body["actor"] is not None and not isinstance(body["actor"], str):
        raise SkillSyncError(
            "edit request actor must be a string",
            code="edit_request_invalid",
        )
    return body


def _edit_inspection(session_id: str, config_path: str | None) -> dict[str, Any]:
    """Aggregate the three read-only edit checks into one confirmable snapshot."""

    session = core.edit_session_status(session_id, config_path=config_path)
    session.update(core.edit_session_paths(session_id, config_path=config_path))
    if session.get("status") != "active":
        raise SkillSyncError(
            "only an active edit session can be inspected",
            code="edit_inspection_blocked",
            details={"session_id": session_id, "status": session.get("status")},
        )

    results: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    checks = (
        ("diff", core.edit_diff),
        ("validation", core.edit_validate),
        ("impact", core.edit_impact),
    )
    for stage, operation in checks:
        try:
            results[stage] = operation(session_id, config_path=config_path)
        except SkillSyncError as exc:
            results[stage] = None
            errors.append({"stage": stage, "code": exc.code, "message": str(exc)})

    blockers = list(errors)
    diff = results["diff"]
    validation = results["validation"]
    impact = results["impact"]
    expected_identity = {
        "session_id": session_id,
        "skill": session.get("logical_skill"),
        "scope": (session.get("target_scope") or {}).get("kind", "base"),
        "target": (session.get("target_scope") or {}).get("target"),
    }
    for stage, value in results.items():
        if value is None:
            continue
        actual = {key: value.get(key) for key in expected_identity}
        if actual != expected_identity:
            blockers.append(
                {
                    "stage": stage,
                    "code": "edit_inspection_scope_mismatch",
                    "message": "edit inspection scope changed unexpectedly",
                }
            )
    if validation is not None:
        if not validation.get("valid"):
            blockers.append(
                {"stage": "validation", "code": "invalid", "message": "workspace validation failed"}
            )
        if not validation.get("changed"):
            blockers.append(
                {"stage": "validation", "code": "unchanged", "message": "workspace has no authored changes"}
            )
        if validation.get("stale_baseline"):
            blockers.append(
                {"stage": "validation", "code": "stale-baseline", "message": "authored layer changed since begin"}
            )
    if diff is not None and not diff.get("changed"):
        blockers.append(
            {"stage": "diff", "code": "unchanged", "message": "workspace has no authored changes"}
        )
    if impact is not None:
        if impact.get("blocked") or impact.get("stale_baseline"):
            blockers.append(
                {
                    "stage": "impact",
                    "code": impact.get("blocked_reason") or "blocked",
                    "message": "edit impact is blocked",
                }
            )
        if not impact.get("has_workspace_changes"):
            blockers.append(
                {"stage": "impact", "code": "unchanged", "message": "workspace has no authored changes"}
            )

    snapshot = {
        "schema_version": 1,
        "session": session,
        **results,
        "errors": errors,
        "blockers": blockers,
        "can_apply": not blockers,
    }
    fingerprint_source = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    snapshot["inspection_id"] = "sha256:" + hashlib.sha256(fingerprint_source).hexdigest()
    return snapshot


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
        if "managed" in requested:
            result["managed"] = {
                "variants": {
                    "variant_count": 0,
                    "valid": True,
                    "variants": [],
                    "issues": [],
                },
                "deployments": {
                    "skills": [],
                    "operations": [],
                    "recovery_required": False,
                },
                "sessions": {"sessions": []},
                "edit_agents": core.edit_agent_capabilities(),
            }
        if "import-candidates" in requested:
            result["import_candidates"] = []
        return result

    if "summary" in requested:
        result["preview"] = preview
    if "agents" in requested:
        result["doctor"] = diagnosis
    if "inventory" in requested:
        result["status"] = _inventory(config_path, diagnosis=diagnosis)
    if "managed" in requested:
        result["managed"] = _managed_state(config_path)
    if "import-candidates" in requested:
        try:
            result["import_candidates"] = core.scan_import_candidates(
                config_path=config_path
            )
        except SkillSyncError:
            result["import_candidates"] = []
    return result


def _managed_state(config_path: str | None) -> dict[str, Any]:
    """Return CLI-identical Variant, deployment, and session read models."""

    return {
        "variants": variant_source.list_variants(config_path=config_path),
        "deployments": core.deploy_status(config_path=config_path),
        "sessions": core.list_edit_sessions(config_path=config_path),
        "edit_agents": core.edit_agent_capabilities(),
    }


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
