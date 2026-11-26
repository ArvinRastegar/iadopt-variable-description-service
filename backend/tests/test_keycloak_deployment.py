"""keycloak-deployment: the operator's check of the configured provider (increment 2).

Behavioural tests of ``components/keycloak-deployment.md`` (approved under G-020): ``python -m app.cli
oidc check`` against a fake provider, the example settings, what the deployment files do not name, the
files of the local Keycloak, the runbooks, and that the check of the local Keycloak stays on this machine.
Nothing reaches a real provider.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import socket
import sys
import unittest
from typing import Any, Dict, Iterator, List, Tuple
from urllib.parse import parse_qsl, urlencode
from unittest.mock import patch

import requests
import yaml
from dotenv import dotenv_values
from fake_provider import REMOVED
from increment1_support import ISSUER, run_cli, started_app
from increment2_support import PUBLIC_URL, OidcTestCase
from joserfc.jwk import RSAKey
from test_accounts_roles import AccountsTestCase

from app.core import config as core_config
from app.oidc import provider as provider_module
from app.oidc import settings as oidc_settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
FOLDER = ROOT / "deploy" / "keycloak"
TOOL = ROOT / "backend" / "tools" / "keycloak_check.py"
SETTINGS = (
    "IADOPT_ENVIRONMENT", "IADOPT_AUTH_MODE", "IADOPT_OIDC_ISSUER", "IADOPT_OIDC_CLIENT_ID", "IADOPT_PUBLIC_URL",
    "IADOPT_OIDC_POST_LOGOUT", "IADOPT_TRUSTED_PROXY_HOPS", "IADOPT_SIGN_IN_LIMIT",
)


class CheckTests(OidcTestCase):
    def check(self, *options: str) -> Any:
        return run_cli("oidc", "check", *options)

    def client(self, **kwargs: Any) -> Any:  # the provider's client, not a browser
        return super().client(**kwargs)

    def authorization(self, answers: Dict[str, Any], callbacks: tuple = ()) -> None:
        """Answer authorization requests by their PKCE method, and refuse the callbacks that are not registered."""

        def reply(handler: Any, request: Any) -> None:
            query = dict(parse_qsl(request.query, keep_blank_values=True))
            method = query.get("code_challenge_method", "none")
            if query["redirect_uri"] not in (f"{PUBLIC_URL}/api/auth/callback", *callbacks):
                handler.reply(400, b"Invalid parameter: redirect_uri")
                return
            status, location = answers[method]
            handler.reply(status, b"", {"Location": location} if location else {})

        self.provider.overrides[self.provider.paths("auth")] = reply

    def refusal(self) -> tuple:
        return 302, f"{PUBLIC_URL}/api/auth/callback?{urlencode({'error': 'invalid_request', 'state': 'x'})}"

    def test_a_good_provider(self):
        """Covers: AC-keycloak-deployment-003, AC-keycloak-deployment-006."""
        before = self.stored_text()
        for mode, environment, enabled in (("both", "development", True), ("local", "development", True), ("oidc", "development", True), ("nonsense", "development", True),
                                           ("both", "development", False)):
            with self.subTest(mode=mode, enabled=enabled), patch.object(core_config.settings, "auth_mode_raw", mode):
                self.store.enabled = enabled
                self.provider.received.clear()
                code, out = self.check()
                self.assertEqual(code, 0, out)
                self.assertNotIn("failed", out)
                self.assertEqual(out.count("ok "), 6, out)
                self.assertEqual([request.path for request in self.provider.received], [self.provider.paths("metadata"), self.provider.paths("certs")])
                self.assertNotIn("test-secret", out)
        self.assertEqual(self.stored_text(), before)
        self.assertIn("a sign-out page is offered", out)
        self.assertIn("back-channel sign-out is offered", out)
        self.provider.metadata_changes = {"end_session_endpoint": REMOVED, "backchannel_logout_supported": False}
        code, out = self.check()
        self.assertEqual((code, "a sign-out page is not offered" in out, "back-channel sign-out is not offered" in out), (0, True, True), out)

    def test_what_fails(self):
        """Covers: AC-keycloak-deployment-004."""
        self.replace(provider_module, "REQUEST_SECONDS", 0.5)
        for name, change, word in (
            ("another issuer", {"issuer": "https://login.example.org"}, "other_issuer"), ("no S256", {"code_challenge_methods_supported": ["plain"]}, "no_s256"),
            ("an endpoint that is not allowed", {"token_endpoint": "http://login.example.org"}, "endpoint_not_allowed"), ("no algorithm", {"id_token_signing_alg_values_supported": ["HS256"]}, "no_algorithm"),
        ):
            with self.subTest(name):
                self.provider.metadata_changes = change
                code, out = self.check()
                self.assertEqual((code, "failed" in out, word in out), (1, True, True), out)
        self.provider.metadata_changes = {}
        with self.subTest("no usable key"):
            self.provider.published = [RSAKey.generate_key(2048, {"kid": "enc", "use": "enc"})]
            code, out = self.check()
            self.assertEqual((code, "no_usable_key" in out), (1, True), out)
        with self.subTest("no answer"):
            self.provider.answer("metadata", 200, b"{}", wait=1.0)
            code, out = self.check()
            self.assertEqual((code, "failed" in out), (1, True), out)
        for field, value, name in (("oidc_issuer_raw", "", "IADOPT_OIDC_ISSUER"), ("oidc_client_id_raw", "a b", "IADOPT_OIDC_CLIENT_ID"), ("public_url_raw", "x", "IADOPT_PUBLIC_URL"),
                                   ("oidc_post_logout_raw", "x", "IADOPT_OIDC_POST_LOGOUT"), ("environment_raw", "x", "IADOPT_ENVIRONMENT")):
            with self.subTest(setting=name), patch.object(core_config.settings, field, value):
                self.provider.received.clear()
                code, out = self.check()
                self.assertEqual((code, "failed" in out, name in out, self.provider.received), (1, True, True, []), out)
        for options in (("--unknown",), ("--refused-callback",), ("--refused-callback", " ")):
            with self.subTest(options=options):
                self.assertEqual(self.check(*options)[0], 2)

    def test_a_refused_callback_that_is_no_address_is_a_mistake(self):
        """Covers: AC-keycloak-deployment-004 (a malformed setting, here an option, is not reported as ok)."""
        for value in ("localhost:5173/api/auth/callback", "/api/auth/callback", "callback", "ftp://localhost/x", "http://", "http://:5173/x", "https://[::1/x", " "):
            with self.subTest(value=value), patch.object(requests.Session, "request", side_effect=AssertionError("sent")):
                code, out = self.check("--pkce", "--refused-callback", value)
                self.assertEqual(code, 2, out)
                self.assertNotIn("ok ", out)

    def test_the_client_must_require_s256_and_refuse_other_callbacks(self):
        """Covers: AC-keycloak-deployment-005."""
        good = {"S256": (200, None), "plain": self.refusal(), "none": self.refusal()}
        callbacks = ("--refused-callback", f"{PUBLIC_URL}/", "--refused-callback", "https://evil.example/api/auth/callback")
        self.authorization(good)
        code, out = self.check("--pkce", *callbacks)
        self.assertEqual(code, 0, out)
        sent = [request for request in self.provider.received if request.path == self.provider.paths("auth")]
        self.assertEqual(len(sent), 5)
        self.assertTrue(all(request.method == "GET" and "cookie" not in {name.lower() for name in request.headers} for request in sent))
        self.assertEqual(self.provider.count("token"), 0)
        self.authorization({**good, "S256": (302, "https://login.example.org/next")})
        self.assertEqual(self.check("--pkce")[0], 0, "a redirect without an error is an acceptance")

        for name, answers, options, words in (
            ("plain accepted", {**good, "plain": (200, None)}, ("--pkce",), "with plain"), ("no challenge accepted", {**good, "none": (302, f"{PUBLIC_URL}/x")}, ("--pkce",), "without a challenge"),
            ("a client that is off", {"S256": (400, None), "plain": (400, None), "none": (400, None)}, ("--pkce",), "with S256"),
            ("a client that is off, callbacks only", {"S256": (400, None)}, callbacks, "with S256"),
            ("an unknown client", {"S256": (404, None), "plain": (404, None), "none": (404, None)}, ("--pkce",), "with S256"),
            ("403", {**good, "plain": (403, None)}, ("--pkce",), "with plain"), ("429", {**good, "none": (429, None)}, ("--pkce",), "without a challenge"), ("500", {**good, "plain": (500, None)}, ("--pkce",), "with plain"),
            ("an error elsewhere", {**good, "plain": (302, "https://login.example.org/?error=x")}, ("--pkce",), "with plain"),
        ):
            with self.subTest(name):
                self.authorization(answers)
                code, out = self.check(*options)
                failed = [line for line in out.splitlines() if line.startswith("failed")]
                self.assertEqual((code, any(words in line for line in failed)), (1, True), out)
        with self.subTest("a callback that is accepted"):
            self.authorization(good, callbacks=(f"{PUBLIC_URL}/",))
            code, out = self.check(*callbacks)
            self.assertEqual((code, f"failed  the callback {PUBLIC_URL}/ is refused" in out), (1, True), out)


class FilesTests(OidcTestCase):
    def test_the_settings_are_documented_and_the_example_starts_in_local(self):
        """Covers: AC-keycloak-deployment-007."""
        example, manual = (ROOT / ".env.example").read_text(), (ROOT / "docs" / "manual.md").read_text()
        for name in SETTINGS:
            self.assertIn(f"{name}=", example, name)
            self.assertIn(f"`{name}`", manual, name)
        values = {name: value or "" for name, value in dotenv_values(ROOT / ".env.example").items()}
        fresh = self.dir / "fresh.sqlite3"
        self.use_database(fresh)
        self.store.cookie_secure = values["IADOPT_COOKIE_SECURE"] == "true"
        raw = dict(zip(SETTINGS, ("environment_raw", "auth_mode_raw", "oidc_issuer_raw", "oidc_client_id_raw", "public_url_raw", "oidc_post_logout_raw", "trusted_proxy_hops_raw", "sign_in_limit_raw")))
        with patch.multiple(core_config.settings, **{field: values[name] for name, field in raw.items()}):
            with started_app() as client:
                self.assertEqual(client.get("/api/auth/me").headers["x-iadopt-sign-in"], "password")

    def test_the_deployment_files_name_no_new_setting(self):
        """Covers: AC-keycloak-deployment-009 (with test_nginx_access, which is unchanged)."""
        for path in ("docker-compose.yml", "docker-compose.pcss.yml", "docker-compose.portainer.yml", "frontend/nginx.conf", "deploy/openshift/iadopt.yaml"):
            text = (ROOT / path).read_text()
            for name in SETTINGS:
                self.assertNotIn(name, text, f"{path}: {name}")


# ------------------------------------------------------------------------------------------------
# The local Keycloak and the runbooks
# ------------------------------------------------------------------------------------------------


def members(value: Any) -> Iterator[Tuple[str, Any]]:
    """Every member of a JSON value, at any depth."""
    if isinstance(value, dict):
        for name, item in value.items():
            yield name, item
            yield from members(item)
    elif isinstance(value, list):
        for item in value:
            yield from members(item)


class LocalStackTests(unittest.TestCase):
    def test_the_realm_has_one_client_and_nobody_in_it(self):
        """Covers: AC-keycloak-deployment-001."""
        realm = json.loads((FOLDER / "realm.json").read_text())
        [client] = realm["clients"]
        self.assertEqual({name: client[name] for name in client if name.endswith("Enabled") or name in ("publicClient", "enabled", "protocol", "frontchannelLogout")}, {
            "enabled": True, "protocol": "openid-connect", "publicClient": True, "standardFlowEnabled": True, "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False, "frontchannelLogout": False,
        })
        attributes = client["attributes"]
        self.assertEqual({name: attributes[name] for name in ("pkce.code.challenge.method", "oauth2.device.authorization.grant.enabled", "oidc.ciba.grant.enabled")}, {
            "pkce.code.challenge.method": "S256", "oauth2.device.authorization.grant.enabled": "false", "oidc.ciba.grant.enabled": "false",
        })
        addresses = [*client["redirectUris"], *client["webOrigins"], client["baseUrl"], attributes["post.logout.redirect.uris"], attributes["backchannel.logout.url"]]
        self.assertEqual(addresses, [
            f"{PUBLIC_URL}/api/auth/callback", f"{PUBLIC_URL}/", f"{PUBLIC_URL}/", "http://frontend:8080/api/auth/backchannel-logout",
        ])
        self.assertEqual(attributes["backchannel.logout.session.required"], "true")
        # The scopes of the client table and the subject (basic); no roles, audiences, offline tokens or mappers of its own.
        self.assertEqual((client["defaultClientScopes"], client["optionalClientScopes"]), (["basic", "profile", "email"], []))
        self.assertNotIn("protocolMappers", client)
        self.assertFalse([item for name, item in members(client) if isinstance(item, str) and ("*" in item or "##" in item or item == "+")])

        self.assertEqual({name: realm[name] for name in (
            "enabled", "registrationAllowed", "registrationEmailAsUsername", "loginWithEmailAllowed", "duplicateEmailsAllowed", "verifyEmail", "resetPasswordAllowed",
            "bruteForceProtected",
        )}, {
            "enabled": True, "registrationAllowed": True, "registrationEmailAsUsername": True, "loginWithEmailAllowed": True, "duplicateEmailsAllowed": False,
            "verifyEmail": True, "resetPasswordAllowed": True, "bruteForceProtected": True,
        })
        self.assertRegex(realm["passwordPolicy"], r"^length\(12\)")
        self.assertEqual((realm["smtpServer"]["host"], realm["smtpServer"]["auth"]), ("mail", "false"))
        # Nobody, and nothing secret, at any depth.
        named = {name for name, _ in members(realm)}
        self.assertEqual(named & {"users", "federatedUsers", "groups", "roles", "identityProviders", "components", "scopeMappings", "credentials", "secret",
                                  "password", "user", "privateKey", "certificate", "publicKey", "clientSecret", "registrationAccessToken"}, set())
        self.assertNotRegex((FOLDER / "realm.json").read_text(), r"BEGIN |PRIVATE|eyJ")

    def test_the_stack_is_pinned_local_and_keeps_nothing(self):
        """Covers: AC-keycloak-deployment-002."""
        text = (FOLDER / "compose.yml").read_text()
        stack = yaml.safe_load(text)
        self.assertEqual(set(stack["services"]), {"keycloak", "mail"})
        self.assertNotIn("volumes", stack)
        for name, service in stack["services"].items():
            with self.subTest(service=name):
                self.assertRegex(service["image"], r"^[a-z0-9.]+(/[a-z0-9.]+)+:v?\d+\.\d+\.\d+$")
                self.assertEqual(set(service) & {"volumes", "volumes_from", "tmpfs", "build", "env_file", "network_mode", "privileged", "expose"}, set())
                self.assertTrue(service["ports"])
                for port in service["ports"]:
                    self.assertRegex(port, r"^127\.0\.0\.1:\d+:\d+$")
        environment = stack["services"]["keycloak"]["environment"]
        for name in ("USERNAME", "PASSWORD"):  # ":?" stops the start without a value, and gives none
            self.assertRegex(environment[f"KC_BOOTSTRAP_ADMIN_{name}"], rf"^\$\{{KEYCLOAK_ADMIN_{name}:\?[^}}]+\}}$")
        self.assertEqual(sorted(name for name in environment if "PASSWORD" in name or "SECRET" in name), ["KC_BOOTSTRAP_ADMIN_PASSWORD"])
        self.assertEqual(stack["configs"], {"realm": {"file": "./realm.json"}})
        # The browser and the backend container reach it under the same address: one name, and one port inside and outside.
        self.assertEqual((environment["KC_HOSTNAME"], environment["KC_HTTP_PORT"]), ("http://keycloak.localhost:8180", "8180"))
        self.assertEqual(stack["services"]["keycloak"]["ports"], ["127.0.0.1:8180:8180"])
        self.assertIn("keycloak.localhost", stack["services"]["keycloak"]["networks"]["application"]["aliases"])
        self.assertNotIn("postgres", text.lower())

    def test_the_runbook_file_holds_the_table_the_conditions_and_the_six_runbooks(self):
        """Covers: AC-keycloak-deployment-008."""
        text = (FOLDER / "README.md").read_text()
        rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in text.splitlines() if line.startswith("| ")]
        self.assertEqual([row[0] for row in rows[1:]], [
            "Client type", "Flows", "PKCE", "Callback addresses", "Web origins", "Return address after sign-out", "Back-channel sign-out",
            "Front-channel sign-out", "Base URL", "Scopes", "ID-token signature", "Roles, audience mappers",
        ])
        production = {row[0]: row[2] for row in rows[1:]}
        for name in ("PKCE", "Callback addresses", "Web origins"):
            self.assertIn("**a condition**", production[name])
        self.assertEqual(re.findall(r"^### (\d)\. (.+)$", text, re.MULTILINE), [
            ("1", "Try the sign-in on a development machine"), ("2", "Switch a hosted deployment to ROHub"),
            ("3", "ROHub is not reachable, changed its keys, a client setting or its address"), ("4", "End sessions"), ("5", "The owner cannot sign in"), ("6", "Roll back"),
        ])
        self.assertIn("oidc check --pkce --refused-callback http://localhost:5173/api/auth/callback --refused-callback <public URL>/ "
                      "--refused-callback <public URL>/api/auth/callback/x --refused-callback https://<another host>/api/auth/callback", text)
        for section in re.split(r"^### \d\. .+$", text, flags=re.MULTILINE)[1:]:
            self.assertGreater(len(section.split("\n## ")[0].split()), 20, "a runbook with nothing in it")

    def test_the_files_hold_no_address_of_the_hosted_provider(self):
        """Covers: AC-keycloak-deployment-008."""
        files = [*(FOLDER / name for name in ("README.md", "compose.yml", "realm.json")), TOOL, ROOT / "backend" / "app" / "oidc" / "check.py"]
        for path in files:
            with self.subTest(path.name):
                self.assertNotRegex(path.read_text(), r"(?i)psnc|poznan|rohub\.org|apps\.paas|bst2")

    def test_the_settings_of_the_local_stack_start_the_application(self):
        """Covers: AC-keycloak-deployment-001, AC-keycloak-deployment-008 (the addresses fit the settings the runbook names)."""
        block = re.search(r"IADOPT_ENVIRONMENT=development\n   IADOPT_AUTH_MODE=both\n   IADOPT_OIDC_ISSUER=http://keycloak[^`]+", (FOLDER / "README.md").read_text())
        values = dict(line.strip().split("=", 1) for line in block.group(0).splitlines() if "=" in line)
        realm, stack = json.loads((FOLDER / "realm.json").read_text()), yaml.safe_load((FOLDER / "compose.yml").read_text())
        self.assertEqual(values["IADOPT_OIDC_ISSUER"], f"{stack['services']['keycloak']['environment']['KC_HOSTNAME']}/realms/{realm['realm']}")
        self.assertEqual((values["IADOPT_OIDC_CLIENT_ID"], values["IADOPT_PUBLIC_URL"]), (realm["clients"][0]["clientId"], PUBLIC_URL))
        with patch.multiple(core_config.settings, environment_raw=values["IADOPT_ENVIRONMENT"], auth_mode_raw=values["IADOPT_AUTH_MODE"],
                            oidc_issuer_raw=values["IADOPT_OIDC_ISSUER"], oidc_client_id_raw=values["IADOPT_OIDC_CLIENT_ID"],
                            public_url_raw=values["IADOPT_PUBLIC_URL"], oidc_post_logout_raw=values["IADOPT_OIDC_POST_LOGOUT"]):
            judged = oidc_settings.judge(auth_enabled=True, cookie_secure=values["IADOPT_COOKIE_SECURE"] == "true")
        self.assertEqual((judged.mode, judged.post_logout), ("both", True))


class RunbookTests(AccountsTestCase):
    def test_the_owner_keeps_its_account_by_the_steps_of_runbook_2(self):
        """Covers: AC-keycloak-deployment-008 (runbook 2's steps for the owner do what they say)."""
        self.bind_owner("--username", "admin")  # after the first release with roles
        self.store.save_orcid(self.admin["id"], "https://orcid.org/0000-0002-1825-0097")
        claims = {"email": "owner@example.org", "email_verified": True, "preferred_username": "owner-at-rohub"}
        new, created = self.accounts.ensure_account(ISSUER, "subject-owner", claims)  # the first sign-in
        self.assertTrue(created)
        code, out = run_cli("legacy", "show", "--username", "owner-at-rohub")
        self.assertEqual(code, 0, out)
        shown = json.loads(out)
        self.assertEqual((shown["account"], [(one["issuer"], one["subject"]) for one in shown["identities"]]), (new["id"], [(ISSUER, "subject-owner")]))
        code, out = run_cli("legacy", "fold", "--from", str(shown["account"]), "--into", str(self.admin["id"]), "--reason", "The owner's ROHub sign-in")
        self.assertEqual(code, 0, out)
        again, created = self.accounts.ensure_account(ISSUER, "subject-owner", claims)  # the next sign-in
        self.assertEqual((created, again["id"], again["role"]), (False, self.admin["id"], "owner"))
        code, out = run_cli("legacy", "show", "--account", str(self.admin["id"]))
        self.assertEqual((code, json.loads(out)["saved_orcid_ids"]), (0, 1), out)

    def test_binding_the_new_account_first_is_the_dead_end_runbook_2_warns_of(self):
        """Covers: AC-keycloak-deployment-008 (why the runbook folds instead of binding)."""
        self.bind_owner("--username", "admin")
        new, _ = self.accounts.ensure_account(ISSUER, "subject-owner", {"email": "owner@example.org", "email_verified": True})
        self.assertEqual(run_cli("owner", "rebind", "--issuer", ISSUER, "--subject", "subject-owner", "--reason", "x")[0], 0)
        code, out = run_cli("legacy", "fold", "--from", str(new["id"]), "--into", str(self.admin["id"]), "--reason", "x")
        self.assertEqual(code, 1, out)
        self.assertEqual(run_cli("owner", "rebind", "--username", "admin", "--reason", "x")[0], 0)
        self.assertEqual(run_cli("legacy", "fold", "--from", str(new["id"]), "--into", str(self.admin["id"]), "--reason", "x")[0], 1)
        self.assertIn("history", run_cli("legacy", "fold", "--from", str(new["id"]), "--into", str(self.admin["id"]), "--reason", "x")[1])


class LocalCheckTests(unittest.TestCase):
    ELSEWHERE = (
        "https://login.example.org/realms/x", "http://localhost.example.org/realms/x", "http://127.0.0.1.example.org/x", "http://10.0.0.1/x", "http://0.0.0.0/x",
        "http://192.168.1.2/x", "http://user@localhost/x", "http://localhost@evil.example/x", "http://localhost\\@evil.example/x", "http://evil.example#.localhost",
        "http://evil.example/.localhost", "http://evil.example?.localhost", "http://[::2]/x", "http://[fe80::1]/x", "ftp://localhost/x", "//localhost/x", "localhost", "http://",
        "", "http://localhost:99999/x", "http://localhost:0/x", "http://evil.localhost./x", "http://xn--evil.example/x", "http://127.0.0.2.nip.example/x",
        "http://\u2460.localhost/x",
    )
    HERE = ("http://localhost:5173", "http://keycloak.localhost:8180/realms/x", "http://127.0.0.1:8180/x", "http://[::1]:8180/x", "https://KEYCLOAK.localhost/x")

    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("keycloak_check", TOOL)
        assert spec and spec.loader
        self.tool = importlib.util.module_from_spec(spec)
        sys.modules["keycloak_check"] = self.tool  # its dataclasses look their module up
        self.addCleanup(sys.modules.pop, "keycloak_check")
        spec.loader.exec_module(self.tool)
        self.sent: List[Any] = []

    def forbidden(self, *args: Any, **kwargs: Any) -> Any:
        self.sent.append((args, kwargs))
        raise AssertionError("something was sent")

    def test_another_machine_is_refused_before_anything_is_sent(self):
        """Covers: AC-keycloak-deployment-010."""
        with patch.dict(os.environ, {"KEYCLOAK_ADMIN_USERNAME": "admin", "KEYCLOAK_ADMIN_PASSWORD": "made-up"}), \
                patch.object(requests.Session, "request", self.forbidden), patch.object(socket, "getaddrinfo", self.forbidden), \
                patch.object(socket.socket, "connect", self.forbidden), patch.object(self.tool, "stay_on_this_machine", self.forbidden):
            for url in self.ELSEWHERE:
                for option in ("--issuer", "--public-url", "--mail"):
                    with self.subTest(option=option, url=url), patch.object(sys, "stderr"):
                        self.assertEqual(self.tool.main([f"{option}={url}"]), 2)
        self.assertEqual(self.sent, [])
        for url in self.HERE:
            self.assertTrue(self.tool.is_this_machine(url), url)

    def test_no_page_of_another_machine_is_asked_for_on_the_way(self):
        """Covers: AC-keycloak-deployment-010 (section 7: it sends nothing to an issuer that is not a loopback address)."""
        browser = self.tool.Browser()
        answer = requests.Response()
        answer.status_code, answer.url = 302, "http://localhost:5173/api/auth/login"
        answer.headers["Location"] = "https://login.example.org/realms/x"
        with patch.object(requests.Session, "request", self.forbidden):
            for url in self.ELSEWHERE:
                with self.subTest(url=url), self.assertRaises(self.tool.Failed):
                    browser.send("GET", url)
            with self.assertRaises(self.tool.Failed):
                browser.follow(answer, "http://localhost:5173/")
        self.assertEqual(self.sent, [])
        self.assertFalse(browser.session.trust_env, "no proxy")

    def test_names_of_this_machine_are_resolved_here_whatever_a_name_server_says(self):
        """Covers: AC-keycloak-deployment-010."""
        asked: List[Any] = []
        with patch.object(socket, "getaddrinfo", side_effect=lambda host, *_: asked.append(host)), patch.dict(os.environ):
            self.tool.stay_on_this_machine()
            for host in ("keycloak.localhost", b"keycloak.localhost", "KEYCLOAK.LOCALHOST", "localhost", "127.0.0.1", None):
                socket.getaddrinfo(host, 80)
            self.assertEqual((os.environ["NO_PROXY"], os.environ["no_proxy"]), ("*", "*"))
        self.assertEqual(asked, ["127.0.0.1", "127.0.0.1", "127.0.0.1", "localhost", "127.0.0.1", None])

    def test_no_other_machine_is_found_whoever_asks(self):
        """Covers: AC-keycloak-deployment-010.

        ``oidc check`` sends its requests where the provider's metadata says, past the tool's own browser.
        """
        asked: List[Any] = []
        with patch.object(socket, "getaddrinfo", side_effect=lambda host, *_, **__: asked.append(host) or []), patch.dict(os.environ):
            self.tool.stay_on_this_machine()
            for host in ("keys.example.org", b"login.example.org", "localhost.example.org", "evil.localhost.", "10.0.0.1", "0.0.0.0", "::2", "fe80::1", "", "LOCALHOST.", "xn--localhost-"):
                with self.subTest(host=host), self.assertRaises(socket.gaierror):
                    socket.getaddrinfo(host, 443)
            for url in ("https://keys.example.org/certs", "http://10.0.0.1/realms/x/protocol/openid-connect/auth"):
                with self.subTest(url=url), self.assertRaises(requests.ConnectionError):
                    requests.get(url, timeout=5)
            for host in ("127.0.0.2", "::1", "LocalHost"):
                socket.getaddrinfo(host, 80)
        self.assertEqual(asked, ["127.0.0.2", "::1", "LocalHost"])

    def test_an_error_of_the_application_is_no_sign_out(self):
        """Covers: AC-keycloak-deployment-010 (the check reports a session that ended, not one that could not be read)."""
        places = self.tool.Places("http://keycloak.localhost:8180/realms/x", "x", "http://localhost:5173", "http://localhost:8125", "admin", "made-up")
        check, browser = self.tool.Check(places), self.tool.Browser()
        for status, body, expected in ((200, b'{"user": {"id": 1}}', {"user": {"id": 1}}), (401, b'{"detail": "Authentication required."}', None)):
            answer = requests.Response()
            answer.status_code, answer._content = status, body
            with self.subTest(status=status), patch.object(browser, "send", return_value=answer):
                self.assertEqual(check.me(browser), expected)
        for status in (403, 429, 500, 502, 503):
            answer = requests.Response()
            answer.status_code, answer._content = status, b"{}"
            with self.subTest(status=status), patch.object(browser, "send", return_value=answer), self.assertRaises(self.tool.Failed):
                check.me(browser)

    def test_without_an_administrator_nothing_is_sent(self):
        """Covers: AC-keycloak-deployment-010 (the check needs the name and password of the run)."""
        for values in ({}, {"KEYCLOAK_ADMIN_USERNAME": "admin"}, {"KEYCLOAK_ADMIN_PASSWORD": "made-up"}, {"KEYCLOAK_ADMIN_USERNAME": "", "KEYCLOAK_ADMIN_PASSWORD": "made-up"}):
            with self.subTest(values=list(values)), patch.dict(os.environ, values), patch.object(requests.Session, "request", self.forbidden), patch.object(sys, "stderr"):
                for name in {"KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD"} - set(values):
                    os.environ.pop(name, None)
                self.assertEqual(self.tool.main([]), 2)
        self.assertEqual(self.sent, [])
