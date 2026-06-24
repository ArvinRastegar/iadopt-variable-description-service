import os
import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.auth import AuthStore, utc_iso, utc_now
from app import main
from app.core import config as core_config
from app.core import dependencies as core_deps
from app.routers import decompose as decompose_router
from app.services import reranker as reranker_service


class ProviderConfigurationTests(unittest.TestCase):
    # Provider parsing moved to the app.core.config leaf in Phase 1 (refactor).
    def test_missing_allowlist_enables_both_providers(self):
        self.assertEqual(
            core_config._parse_enabled_model_providers(None),
            ["openrouter", "psnc"],
        )

    def test_single_provider_is_supported(self):
        self.assertEqual(core_config._parse_enabled_model_providers("psnc"), ["psnc"])
        self.assertEqual(core_config._parse_enabled_model_providers("openrouter"), ["openrouter"])

    def test_invalid_provider_is_rejected(self):
        with self.assertRaises(RuntimeError):
            core_config._parse_enabled_model_providers("psnc,unknown")


class PsncRerankerTests(unittest.TestCase):
    # The reranker moved to app.services.reranker in Phase 2; patch its session there.
    @patch.object(reranker_service, "get_http_session")
    def test_scores_are_returned_in_document_order(self, get_http_session):
        response = Mock()
        response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.2},
                {"index": 0, "relevance_score": 0.9},
            ]
        }
        response.raise_for_status.return_value = None
        get_http_session.return_value.post.return_value = response

        scores = reranker_service.call_psnc_reranker("query", ["first", "second"])

        self.assertEqual(scores, [0.9, 0.2])
        request = get_http_session.return_value.post.call_args
        self.assertEqual(request.kwargs["json"]["model"], core_config.settings.psnc_rerank_model)
        self.assertEqual(request.kwargs["json"]["documents"], ["first", "second"])


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict(
            os.environ,
            {
                "IADOPT_BOOTSTRAP_ADMIN_USERNAME": "admin",
                "IADOPT_BOOTSTRAP_ADMIN_PASSWORD": "admin-password",
                "IADOPT_BOOTSTRAP_ADMIN_DISPLAY_NAME": "Admin User",
            },
        )
        self.env_patcher.start()
        # Routers bind the canonical auth_store singleton (app.core.dependencies)
        # at import time, so we reconfigure that object in place rather than
        # swapping a module attribute, then restore it in tearDown.
        self.store = core_deps.auth_store
        self._saved = {
            "db_path": self.store.db_path,
            "enabled": self.store.enabled,
            "session_secret": self.store.session_secret,
            "cookie_secure": self.store.cookie_secure,
        }
        self.store.db_path = pathlib.Path(self.temp_dir.name) / "iadopt.sqlite3"
        self.store.enabled = True
        self.store.session_secret = "test-secret"
        self.store.cookie_secure = False
        self.store.init()
        self.client = TestClient(main.app)

    def tearDown(self):
        for attr, value in self._saved.items():
            setattr(self.store, attr, value)
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def login(self, username="admin", password="admin-password"):
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200)

    def test_protected_route_requires_login(self):
        response = self.client.get("/api/model-options")
        self.assertEqual(response.status_code, 401)

        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)

    def test_login_succeeds_and_fails(self):
        failed = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        self.assertEqual(failed.status_code, 401)

        success = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        self.assertEqual(success.status_code, 200)
        self.assertIn("iadopt_session", success.cookies)

    def test_inactive_user_cannot_login(self):
        user = self.store.create_user(username="disabled", password="password-123")
        self.store.update_user(user["id"], {"is_active": False})

        response = self.client.post(
            "/api/auth/login",
            json={"username": "disabled", "password": "password-123"},
        )

        self.assertEqual(response.status_code, 401)

    def test_admin_can_create_disable_and_reset_user(self):
        self.login()
        created = self.client.post(
            "/api/admin/users",
            json={
                "username": "alice",
                "password": "password-123",
                "display_name": "Alice",
                "email": "alice@example.org",
                "roles": ["user"],
                "is_active": True,
            },
        )
        self.assertEqual(created.status_code, 200)
        user_id = created.json()["user"]["id"]

        disabled = self.client.patch(
            f"/api/admin/users/{user_id}",
            json={"is_active": False, "password": "new-password-123"},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["user"]["is_active"])

        login = self.client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "new-password-123"},
        )
        self.assertEqual(login.status_code, 401)

    def test_normal_user_cannot_access_admin_endpoints(self):
        self.store.create_user(username="bob", password="password-123")
        self.login("bob", "password-123")

        response = self.client.get("/api/admin/users")

        self.assertEqual(response.status_code, 403)

    def test_last_active_admin_cannot_be_disabled(self):
        self.login()
        admin = next(user for user in self.store.list_users() if user["username"] == "admin")

        response = self.client.patch(
            f"/api/admin/users/{admin['id']}",
            json={"is_active": False},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("last active admin", response.json()["detail"])

    def test_decompose_and_nanopub_failures_are_audited(self):
        self.login()

        with patch.object(decompose_router, "run_pipeline") as run_pipeline:
            run_pipeline.return_value = {
                "raw_llm_output": "{}",
                "parsed_json": {"definition": "air temperature"},
                "schema_valid": True,
                "validation_errors": [],
                "enriched_json": {},
                "ttl": "@prefix ex: <http://example.org/> .",
            }
            response = self.client.post(
                "/api/decompose",
                json={"definition": "air temperature"},
            )
        self.assertEqual(response.status_code, 200)

        publish = self.client.post(
            "/api/nanopub/publish",
            json={"ttl": "not turtle"},
        )
        self.assertEqual(publish.status_code, 400)

        retract = self.client.post(
            "/api/nanopub/retract",
            json={"nanopub_uri": "not-a-nanopub"},
        )
        self.assertEqual(retract.status_code, 400)

        actions = [event["action"] for event in self.store.get_audit_events(limit=20)]
        self.assertIn("decompose", actions)
        self.assertIn("nanopub.publish", actions)
        self.assertIn("nanopub.retract", actions)

    def test_retention_cleanup_removes_old_audit_rows(self):
        self.store.audit_event(action="recent")
        with self.store._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (created_at, action)
                VALUES (?, ?)
                """,
                (utc_iso(utc_now().replace(year=2000)), "old"),
            )

        self.store.cleanup_old_audit(force=True)
        actions = [event["action"] for event in self.store.get_audit_events(limit=20)]

        self.assertIn("recent", actions)
        self.assertNotIn("old", actions)


if __name__ == "__main__":
    unittest.main()
