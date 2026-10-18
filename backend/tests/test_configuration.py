import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.auth import AuthStore, utc_iso, utc_now
from app import main
from app.core import config as core_config
from app.core import dependencies as core_deps
from app.routers import decompose as decompose_router
from app.routers import nanopub as nanopub_router


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
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)

        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)

    def test_visualizer_metadata_is_available_without_login(self):
        expected_keys = {
            "/api/model-options": {"default_model_provider", "default_model_name", "model_names", "providers"},
            "/api/nanopub/preparation-options": {
                "conforms_to_uri", "created_with_label",
            },
        }
        for path, keys in expected_keys.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.json()), keys)
                self.assertNotIn("iadopt_session", response.cookies)

    def test_public_metadata_exemption_only_allows_get(self):
        for path in ("/api/model-options", "/api/nanopub/preparation-options"):
            for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD"):
                with self.subTest(path=path, method=method):
                    response = self.client.request(method, path)
                    self.assertEqual(response.status_code, 401)

    def test_private_account_admin_and_docs_still_require_login(self):
        for path in (
            "/api/auth/me", "/api/auth/verify", "/api/admin/users", "/api/admin/stats",
            "/api/admin/audit", "/api/docs", "/api/redoc", "/api/openapi.json",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

    def test_anonymous_protected_operations_cannot_reach_services(self):
        operations = (
            ("POST", "/api/decompose", {"definition": "air temperature"}),
            ("POST", "/api/decompose/stream", {"definition": "air temperature"}),
            ("POST", "/api/nanopub/publish", {"ttl": "<https://example.org/v> a <https://w3id.org/iadopt/ont/Variable> .", "creator_orcid_id": "0000-0002-1825-0097"}),
            ("POST", "/api/nanopub/retract", {"nanopub_uri": "https://example.org/nanopub", "creator_orcid_id": "0000-0002-1825-0097"}),
            ("POST", "/api/events", {"action": "visualize", "payload": {"ttl": "private input"}}),
            ("POST", "/api/admin/users", {"username": "guest", "password": "password-123"}),
            ("PATCH", "/api/admin/users/1", {"display_name": "Changed"}),
            ("POST", "/api/auth/logout", {}),
        )
        # Stop any accidental regression before LLM calls, signing-key access, or
        # registry calls; the assertion below also requires these guards stay unused.
        with (
            patch.object(decompose_router, "run_pipeline", side_effect=AssertionError("Unexpected LLM call")) as pipeline,
            patch.object(decompose_router, "stream_pipeline_events", side_effect=AssertionError("Unexpected LLM call")) as stream,
            patch.object(nanopub_router, "get_nanopub_profile", side_effect=AssertionError("Unexpected signing access")) as profile,
            patch.object(self.store, "audit_event") as audit,
        ):
            for method, path, body in operations:
                with self.subTest(path=path, method=method):
                    response = self.client.request(method, path, json=body)
                    self.assertEqual(response.status_code, 401)
            pipeline.assert_not_called()
            stream.assert_not_called()
            profile.assert_not_called()
            audit.assert_not_called()

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
            json={"ttl": "not turtle", "creator_orcid_id": "0000-0002-1825-0097"},
        )
        self.assertEqual(publish.status_code, 400)

        retract = self.client.post(
            "/api/nanopub/retract",
            json={"nanopub_uri": "not-a-nanopub", "creator_orcid_id": "0000-0002-1825-0097"},
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


class CreatorOrcidRequiredTests(unittest.TestCase):
    """Publishing identity comes from the person publishing: there is no server default."""

    def test_orcid_resolution_has_no_server_default(self):
        from app.services import orcid as orcid_service

        with patch.object(orcid_service.settings, "nanopub_orcid_id", "0009-0006-1978-4302", create=True):
            for missing in (None, ""):
                with self.subTest(value=missing), self.assertRaisesRegex(RuntimeError, "ORCID is required"):
                    orcid_service.resolve_creator_metadata(missing)

    def test_supplied_orcid_is_normalized_and_resolved(self):
        from app.services import orcid as orcid_service

        with patch.object(orcid_service, "lookup_orcid_display_name", return_value="Test Creator"):
            self.assertEqual(
                orcid_service.resolve_creator_metadata("0000-0002-1825-0097"),
                ("https://orcid.org/0000-0002-1825-0097", "Test Creator"),
            )

    def test_publish_and_retract_requests_require_an_orcid(self):
        from pydantic import ValidationError
        from app.schemas.requests import PublishNanopubRequest, RetractNanopubRequest

        for model, fields in (
            (PublishNanopubRequest, {"ttl": "x"}),
            (RetractNanopubRequest, {"nanopub_uri": "x"}),
        ):
            for extra in ({}, {"creator_orcid_id": ""}):
                with self.subTest(model=model.__name__, extra=extra), self.assertRaises(ValidationError):
                    model(**fields, **extra)

    def test_preparation_options_expose_no_default_orcid(self):
        from app.schemas.responses import NanopubPreparationOptionsResponse

        self.assertNotIn("default_creator_orcid_id", NanopubPreparationOptionsResponse.model_fields)

    def test_decompose_request_has_no_orcid_field(self):
        from app.schemas.requests import DecomposeRequest

        self.assertNotIn("creator_orcid_id", DecomposeRequest.model_fields)
