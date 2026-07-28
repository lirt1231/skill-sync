import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

from skill_sync.web import _edit_inspection, _handler_factory


class WebEditSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths_patcher = mock.patch(
            "skill_sync.web.core.edit_session_paths",
            return_value={
                "baseline_path": "/tmp/baseline",
                "workspace_path": "/tmp/workspace",
            },
        )
        self.paths_patcher.start()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_factory("/tmp/config.json", "test-token")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.paths_patcher.stop()

    def post(self, path: str, body: dict, *, token: str | None = "test-token"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Skill-Sync-Token"] = token
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read())

    @staticmethod
    def checks(*, changed: bool = True, valid: bool = True, blocked: bool = False):
        identity = {
            "session_id": "session-1",
            "skill": "alpha",
            "scope": "client",
            "target": "codex",
        }
        session = {
            "session_id": "session-1",
            "logical_skill": "alpha",
            "status": "active",
            "target_scope": {"kind": "client", "target": "codex"},
            "workspace_path": "/tmp/workspace",
        }
        diff = {
            **identity,
            "status": "active",
            "changed": changed,
            "summary": {"added": 0, "modified": int(changed), "deleted": 0, "total": int(changed)},
            "files": [],
            "resolved_diffs": [],
        }
        validation = {
            **identity,
            "status": "active",
            "valid": valid,
            "changed": changed,
            "stale_baseline": blocked,
            "issues": [] if valid else [{"code": "invalid", "path": "SKILL.md", "message": "bad"}],
        }
        impact = {
            **identity,
            "status": "active",
            "stale_baseline": blocked,
            "blocked": blocked,
            "blocked_reason": "canonical-layer-changed-since-begin" if blocked else None,
            "has_workspace_changes": changed,
            "clients": [],
            "summary": {"affected": 0, "requires_rebuild": 0, "disabled": 0, "undetected": 0},
        }
        return session, diff, validation, impact

    def test_edit_posts_require_csrf_token(self):
        for path in (
            "/api/edit/begin",
            "/api/edit/inspect",
            "/api/edit/apply",
            "/api/edit/abort",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                self.post(path, {}, token=None)
            self.assertEqual(raised.exception.code, 403)

    def test_begin_validates_request_and_refreshes_managed_state(self):
        result = {
            "session_id": "session-1",
            "skill": "alpha",
            "scope": "family",
            "target": "kimi",
            "status": "active",
        }
        next_state = {"initialized": True, "loaded_views": ["inventory", "managed"]}
        with mock.patch("skill_sync.web.core.edit_begin", return_value=result) as begin, mock.patch(
            "skill_sync.web._state", return_value=next_state
        ) as state:
            status, body = self.post(
                "/api/edit/begin",
                {
                    "skill": "alpha",
                    "scope": "family",
                    "target": "kimi",
                    "actor": "codex",
                    "views": ["inventory", "managed"],
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(body, {"result": result, "state": next_state})
        begin.assert_called_once_with(
            "alpha",
            scope="family",
            target="kimi",
            actor="codex",
            config_path="/tmp/config.json",
        )
        state.assert_called_once_with(
            "/tmp/config.json", views=("inventory", "managed")
        )

    def test_begin_rejects_unknown_fields_and_invalid_scope_before_mutation(self):
        for request_body in (
            {"skill": "alpha", "scope": "all"},
            {"skill": "alpha", "scope": "family"},
            {"skill": "alpha", "scope": "base", "target": "codex"},
            {"skill": "alpha", "scope": "base", "unexpected": True},
        ):
            with self.subTest(body=request_body), mock.patch(
                "skill_sync.web.core.edit_begin"
            ) as begin, self.assertRaises(HTTPError) as raised:
                self.post("/api/edit/begin", request_body)
            self.assertEqual(raised.exception.code, 400)
            begin.assert_not_called()

    def test_inspect_is_read_only_and_aggregates_all_core_checks(self):
        session, diff, validation, impact = self.checks()
        with mock.patch(
            "skill_sync.web.core.edit_session_status", return_value=session
        ) as status, mock.patch(
            "skill_sync.web.core.edit_session_paths",
            return_value={"baseline_path": "/tmp/baseline", "workspace_path": "/tmp/workspace"},
        ) as paths, mock.patch(
            "skill_sync.web.core.edit_diff", return_value=diff
        ) as edit_diff, mock.patch(
            "skill_sync.web.core.edit_validate", return_value=validation
        ) as validate, mock.patch(
            "skill_sync.web.core.edit_impact", return_value=impact
        ) as edit_impact, mock.patch("skill_sync.web.core.edit_apply") as apply:
            response_status, body = self.post(
                "/api/edit/inspect", {"session_id": "session-1"}
            )

        self.assertEqual(response_status, 200)
        self.assertTrue(body["inspection"]["can_apply"])
        self.assertRegex(body["inspection"]["inspection_id"], r"^sha256:[0-9a-f]{64}$")
        status.assert_called_once_with("session-1", config_path="/tmp/config.json")
        paths.assert_called_once_with("session-1", config_path="/tmp/config.json")
        edit_diff.assert_called_once_with("session-1", config_path="/tmp/config.json")
        validate.assert_called_once_with("session-1", config_path="/tmp/config.json")
        edit_impact.assert_called_once_with("session-1", config_path="/tmp/config.json")
        apply.assert_not_called()

    def test_apply_reinspects_and_rejects_changed_or_blocked_snapshot(self):
        session, diff, validation, impact = self.checks()
        with mock.patch("skill_sync.web.core.edit_session_status", return_value=session), mock.patch(
            "skill_sync.web.core.edit_diff", return_value=diff
        ), mock.patch("skill_sync.web.core.edit_validate", return_value=validation), mock.patch(
            "skill_sync.web.core.edit_impact", return_value=impact
        ):
            inspection_id = _edit_inspection("session-1", "/tmp/config.json")["inspection_id"]

        blocked_session, blocked_diff, blocked_validation, blocked_impact = self.checks(blocked=True)
        for sent_id, checks, expected_code in (
            ("sha256:" + "0" * 64, (session, diff, validation, impact), "edit_inspection_changed"),
            (inspection_id, (blocked_session, blocked_diff, blocked_validation, blocked_impact), "edit_inspection_changed"),
        ):
            with self.subTest(expected_code=expected_code), mock.patch(
                "skill_sync.web.core.edit_session_status", return_value=checks[0]
            ), mock.patch("skill_sync.web.core.edit_diff", return_value=checks[1]), mock.patch(
                "skill_sync.web.core.edit_validate", return_value=checks[2]
            ), mock.patch("skill_sync.web.core.edit_impact", return_value=checks[3]), mock.patch(
                "skill_sync.web.core.edit_apply"
            ) as apply, self.assertRaises(HTTPError) as raised:
                self.post(
                    "/api/edit/apply",
                    {"session_id": "session-1", "inspection_id": sent_id},
                )
            payload = json.loads(raised.exception.read())
            self.assertEqual(payload["code"], expected_code)
            apply.assert_not_called()

        _, unchanged_diff, unchanged_validation, unchanged_impact = self.checks(changed=False)
        with mock.patch("skill_sync.web.core.edit_session_status", return_value=session), mock.patch(
            "skill_sync.web.core.edit_diff", return_value=unchanged_diff
        ), mock.patch(
            "skill_sync.web.core.edit_validate", return_value=unchanged_validation
        ), mock.patch("skill_sync.web.core.edit_impact", return_value=unchanged_impact):
            unchanged = _edit_inspection("session-1", "/tmp/config.json")
        with mock.patch("skill_sync.web._edit_inspection", return_value=unchanged), mock.patch(
            "skill_sync.web.core.edit_apply"
        ) as apply, self.assertRaises(HTTPError) as raised:
            self.post(
                "/api/edit/apply",
                {"session_id": "session-1", "inspection_id": unchanged["inspection_id"]},
            )
        self.assertEqual(json.loads(raised.exception.read())["code"], "edit_apply_blocked")
        apply.assert_not_called()

    def test_apply_and_abort_refresh_state_only_after_core_success(self):
        session, diff, validation, impact = self.checks()
        inspection = None
        with mock.patch("skill_sync.web.core.edit_session_status", return_value=session), mock.patch(
            "skill_sync.web.core.edit_diff", return_value=diff
        ), mock.patch("skill_sync.web.core.edit_validate", return_value=validation), mock.patch(
            "skill_sync.web.core.edit_impact", return_value=impact
        ):
            inspection = _edit_inspection("session-1", "/tmp/config.json")

        state_value = {"initialized": True, "loaded_views": ["inventory", "managed"]}
        with mock.patch("skill_sync.web._edit_inspection", return_value=inspection), mock.patch(
            "skill_sync.web.core.edit_apply", return_value={"status": "applied"}
        ) as apply, mock.patch("skill_sync.web._state", return_value=state_value) as state:
            _, body = self.post(
                "/api/edit/apply",
                {
                    "session_id": "session-1",
                    "inspection_id": inspection["inspection_id"],
                    "views": ["inventory", "managed"],
                },
            )
        self.assertEqual(body["result"]["status"], "applied")
        apply.assert_called_once_with("session-1", config_path="/tmp/config.json")
        state.assert_called_once_with("/tmp/config.json", views=("inventory", "managed"))

        with mock.patch("skill_sync.web.core.edit_session_status", return_value=session), mock.patch(
            "skill_sync.web.core.edit_abort", return_value={"status": "aborted"}
        ) as abort, mock.patch("skill_sync.web._state", return_value=state_value):
            _, body = self.post(
                "/api/edit/abort",
                {"session_id": "session-1", "views": ["inventory", "managed"]},
            )
        self.assertEqual(body["result"]["status"], "aborted")
        abort.assert_called_once_with("session-1", config_path="/tmp/config.json")


if __name__ == "__main__":
    unittest.main()
