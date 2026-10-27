"""Saved ORCID iDs: one account keeps several iDs and picks one when publishing or retracting.

The routes run against a temporary auth database, like ``test_configuration``. They take
names only from the in-process cache that publish and retract fill; the tests patch that
read, and one test proves the routes never ask ORCID themselves.
"""

import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import main
from app.auth import MAX_SAVED_ORCIDS
from app.core import dependencies as core_deps
from app.routers import auth as auth_router
from app.services import orcid as orcid_service
from app.services.orcid import canonical_orcid_uri

CARBERRY = "https://orcid.org/0000-0002-1825-0097"
HAAK = "https://orcid.org/0000-0001-5109-3700"


def orcid_with_check_digit(base15: str) -> str:
    """Build a valid ORCID URI from 15 digits (ISO 7064 MOD 11-2, written out independently)."""
    total = 0
    for char in base15:
        total = (total + int(char)) * 2
    check = (12 - total % 11) % 11
    digits = base15 + ("X" if check == 10 else str(check))
    return "https://orcid.org/" + "-".join(digits[i:i + 4] for i in range(0, 16, 4))


class CanonicalOrcidTests(unittest.TestCase):
    def test_bare_and_url_forms_become_one_canonical_uri(self):
        for value in (
            "0000-0002-1825-0097",
            " https://orcid.org/0000-0002-1825-0097 ",
            "http://ORCID.org/0000-0002-1825-0097",
        ):
            with self.subTest(value=value):
                self.assertEqual(canonical_orcid_uri(value), CARBERRY)
        self.assertEqual(canonical_orcid_uri("0000-0002-1694-233x"), "https://orcid.org/0000-0002-1694-233X")

    def test_malformed_iDs_and_wrong_check_digits_are_refused(self):
        cases = {
            "0000-0002-1825-0098": "check digit",
            "0000-0002-1825-009": "valid ORCID",
            "https://example.org/0000-0002-1825-0097": "valid ORCID",
            "": "valid ORCID",
            # Other scripts' digits are digits to Python's \d and int(), but not ORCID iDs.
            "\uff10\uff10\uff10\uff10-\uff10\uff10\uff10\uff12-\uff11\uff16\uff19\uff14-\uff12\uff13\uff13X": "valid ORCID",
            "\u0660\u0660\u0660\u0660-\u0660\u0660\u0660\u0662-\u0661\u0668\u0662\u0665-\u0660\u0660\u06697": "valid ORCID",
        }
        for value, message in cases.items():
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    canonical_orcid_uri(value)


class SavedOrcidRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict(
            os.environ,
            {
                "IADOPT_BOOTSTRAP_ADMIN_USERNAME": "admin",
                "IADOPT_BOOTSTRAP_ADMIN_PASSWORD": "admin-password",
            },
        )
        self.env_patcher.start()
        # Reconfigure the shared store in place, as test_configuration does, and restore it after.
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
        self.names = {CARBERRY: "Josiah Carberry", HAAK: "Laure Haak"}
        self.lookup_patcher = patch.object(auth_router, "cached_orcid_display_name", side_effect=lambda uri: self.names.get(uri))
        self.lookup = self.lookup_patcher.start()
        self.bob = self.store.create_user(username="bob", password="password-123")
        self.alice = self.store.create_user(username="alice", password="password-123")
        self.client = TestClient(main.app)

    def tearDown(self):
        self.lookup_patcher.stop()
        for attr, value in self._saved.items():
            setattr(self.store, attr, value)
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def login(self, username):
        response = self.client.post("/api/auth/login", json={"username": username, "password": "password-123"})
        self.assertEqual(response.status_code, 200)

    def saved(self):
        response = self.client.get("/api/auth/me/orcids")
        self.assertEqual(response.status_code, 200)
        return [(item["orcid"], item["name"]) for item in response.json()["orcids"]]

    def test_saved_orcids_need_a_session(self):
        self.assertEqual(self.client.get("/api/auth/me/orcids").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY}).status_code, 401)
        self.assertEqual(self.client.delete("/api/auth/me/orcids/0000-0002-1825-0097").status_code, 401)

    def test_an_account_saves_several_iDs_most_recently_used_first(self):
        self.login("bob")
        self.assertEqual(self.saved(), [])

        first = self.client.post("/api/auth/me/orcids", json={"orcid": "0000-0002-1825-0097"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual([item["orcid"] for item in first.json()["orcids"]], [CARBERRY])

        self.client.post("/api/auth/me/orcids", json={"orcid": HAAK})
        self.assertEqual(self.saved(), [(HAAK, "Laure Haak"), (CARBERRY, "Josiah Carberry")])

        # Saving a saved iD again only marks it as just used.
        self.client.post("/api/auth/me/orcids", json={"orcid": "https://orcid.org/0000-0002-1825-0097"})
        self.assertEqual(self.saved(), [(CARBERRY, "Josiah Carberry"), (HAAK, "Laure Haak")])

    def test_invalid_iDs_are_refused_and_nothing_is_stored(self):
        self.login("bob")
        for value in ("0000-0002-1825-0098", "https://example.org/0000-0002-1825-0097"):
            with self.subTest(value=value):
                response = self.client.post("/api/auth/me/orcids", json={"orcid": value})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post("/api/auth/me/orcids", json={"orcid": ""}).status_code, 422)
        self.assertEqual(self.saved(), [])
        self.lookup.assert_not_called()

    def test_a_removed_iD_disappears_and_unknown_ones_are_reported(self):
        self.login("bob")
        self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY})
        self.client.post("/api/auth/me/orcids", json={"orcid": HAAK})

        removed = self.client.delete("/api/auth/me/orcids/0000-0002-1825-0097")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual([item["orcid"] for item in removed.json()["orcids"]], [HAAK])
        self.assertEqual(self.client.delete("/api/auth/me/orcids/0000-0002-1825-0097").status_code, 404)
        self.assertEqual(self.client.delete("/api/auth/me/orcids/0000-0002-1825-0098").status_code, 400)
        self.assertEqual(self.saved(), [(HAAK, "Laure Haak")])

    def test_accounts_never_see_each_others_iDs(self):
        self.store.save_orcid(self.alice["id"], HAAK, "Laure Haak")
        self.login("bob")
        self.assertEqual(self.saved(), [])
        self.assertEqual(self.client.delete("/api/auth/me/orcids/0000-0001-5109-3700").status_code, 404)
        self.assertEqual([item["orcid"] for item in self.store.list_orcids(self.alice["id"])], [HAAK])

    def test_an_unresolved_name_never_replaces_a_known_one(self):
        self.login("bob")
        self.names = {}
        self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY})
        self.assertEqual(self.saved(), [(CARBERRY, None)])
        self.names = {CARBERRY: "Josiah Carberry"}
        self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY})
        self.names = {}
        self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY})
        self.assertEqual(self.saved(), [(CARBERRY, "Josiah Carberry")])

    def test_an_account_keeps_at_most_the_limit(self):
        for index in range(MAX_SAVED_ORCIDS):
            self.store.save_orcid(self.bob["id"], orcid_with_check_digit(f"0000000300000{index:02d}"))
        self.login("bob")
        full = self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY})
        self.assertEqual(full.status_code, 400)
        self.assertIn(str(MAX_SAVED_ORCIDS), full.json()["detail"])
        # A saved iD can still be marked as used when the list is full.
        again = self.client.post("/api/auth/me/orcids", json={"orcid": orcid_with_check_digit("000000030000000")})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(again.json()["orcids"]), MAX_SAVED_ORCIDS)

    def test_new_iDs_and_removals_are_audited_but_uses_are_not(self):
        self.login("bob")
        self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY})
        self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY})
        self.client.delete("/api/auth/me/orcids/0000-0002-1825-0097")
        events = [event for event in self.store.get_audit_events(user_id=self.bob["id"]) if event["action"].startswith("auth.orcid")]
        self.assertEqual([event["action"] for event in events], ["auth.orcid_removed", "auth.orcid_saved"])
        # The request is logged as sent; the canonical iD goes into the response payload.
        self.assertIn('"orcid_id": "0000-0002-1825-0097"', events[0]["request_payload"])
        self.assertIn(CARBERRY, events[0]["response_payload"])
        self.assertIn(CARBERRY, events[1]["request_payload"])

    def test_saving_never_asks_orcid_for_a_name(self):
        fresh = orcid_with_check_digit("000000040000001")
        self.login("bob")
        # A spy, not a raising stub: the ORCID lookup swallows exceptions, so only a recorded
        # call can show that a request was attempted.
        with patch.object(auth_router, "cached_orcid_display_name", side_effect=orcid_service.cached_orcid_display_name), \
                patch.object(orcid_service, "get_http_session", return_value=Mock(get=Mock(side_effect=ConnectionError))) as http_session:
            response = self.client.post("/api/auth/me/orcids", json={"orcid": fresh})
        self.assertEqual(response.status_code, 200)
        http_session.assert_not_called()
        self.assertEqual([(item["orcid"], item["name"]) for item in response.json()["orcids"]], [(fresh, None)])

    def test_deleting_an_account_deletes_its_iDs(self):
        self.store.save_orcid(self.bob["id"], CARBERRY)
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM users WHERE id = ?", (self.bob["id"],))
            remaining = conn.execute("SELECT COUNT(*) FROM saved_orcids").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_without_authentication_the_routes_refuse(self):
        self.store.enabled = False
        self.assertEqual(self.client.get("/api/auth/me/orcids").status_code, 409)
        self.assertEqual(self.client.post("/api/auth/me/orcids", json={"orcid": CARBERRY}).status_code, 409)
        self.assertEqual(self.client.delete("/api/auth/me/orcids/0000-0002-1825-0097").status_code, 409)
        self.lookup.assert_not_called()

    def test_the_store_applies_the_same_check(self):
        orcids, created = self.store.save_orcid(self.bob["id"], "0000-0002-1825-0097")
        self.assertTrue(created)
        self.assertEqual([item["orcid"] for item in orcids], [CARBERRY])
        for value in ("0000-0002-1825-0098", "https://example.org/0000-0002-1825-0097"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.save_orcid(self.bob["id"], value)
        self.assertEqual([item["orcid"] for item in self.store.list_orcids(self.bob["id"])], [CARBERRY])

if __name__ == "__main__":
    unittest.main()
