"""An OpenID Connect provider for tests, with keys of its own, on a loopback port.

``FakeProvider`` answers like the Keycloak realm the service signs people in with, records every
request it gets, and can be told to answer badly. Tests play the browser themselves:
``authorize(location)`` takes the address the service sent the browser to and returns the query of the
way back. Run as a program it also shows a small sign-in page, for trying the pages in a browser:

    python tests/fake_provider.py --port 8181 --client-id iadopt-test

It signs in whoever asks: never run it where anyone else can reach it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import secrets
import ssl
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from joserfc.jwk import RSAKey

REALM = "/auth/realms/test"
LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


@dataclass
class Received:
    """One request the provider got."""

    method: str
    path: str
    query: str
    headers: Dict[str, str]
    body: bytes

    @property
    def form(self) -> Dict[str, List[str]]:
        return parse_qs(self.body.decode("ascii"), keep_blank_values=True)


@dataclass
class Code:
    """What a code stands for until it is exchanged."""

    request: Dict[str, str]
    claims: Dict[str, Any]
    id_token: Optional[str] = None


Override = Callable[["Handler", Received], None]


class FakeProvider:
    """The provider; ``with FakeProvider(...) as provider`` starts and stops it."""

    def __init__(self, *, client_id: str = "iadopt-test", port: int = 0, host: str = "127.0.0.1", name: str = "127.0.0.1",
                 tls: Optional[ssl.SSLContext] = None) -> None:
        self.client_id = client_id
        self.key = RSAKey.generate_key(2048, {"kid": "key-1", "use": "sig", "alg": "RS256"})
        self.published: List[Any] = [self.key]
        self.received: List[Received] = []
        self.metadata_changes: Dict[str, Any] = {}
        self.overrides: Dict[str, Override] = {}
        self.codes: Dict[str, Code] = {}
        self.people = DEFAULT_PEOPLE
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._server.provider = self  # type: ignore[attr-defined]
        if tls:
            self._server.socket = tls.wrap_socket(self._server.socket, server_side=True)
        self.origin = f"{'https' if tls else 'http'}://{name}:{self._server.server_address[1]}"
        self.issuer = f"{self.origin}{REALM}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> "FakeProvider":
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def serve(self) -> None:
        self._server.serve_forever()

    # ----------------------------------------------------------------- addresses and metadata

    def url(self, path: str) -> str:
        return f"{self.issuer}/protocol/openid-connect/{path}"

    def metadata(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": self.url("auth"),
            "token_endpoint": self.url("token"),
            "jwks_uri": self.url("certs"),
            "userinfo_endpoint": self.url("userinfo"),
            "end_session_endpoint": self.url("logout"),
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["plain", "S256"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256", "PS256", "HS256"],
            "authorization_response_iss_parameter_supported": True,
            "backchannel_logout_supported": True,
            "backchannel_logout_session_supported": True,
        }
        document.update(self.metadata_changes)
        return {name: value for name, value in document.items() if value is not REMOVED}

    def jwks(self) -> Dict[str, Any]:
        return {"keys": [key.as_dict(private=False) for key in self.published]}

    def paths(self, name: str) -> str:
        return f"{REALM}/.well-known/openid-configuration" if name == "metadata" else f"{REALM}/protocol/openid-connect/{name}"

    def count(self, name: str) -> int:
        """How many requests one of ``metadata``, ``token``, ``certs``, ``userinfo`` got."""
        return sum(1 for request in self.received if request.path == self.paths(name))

    def answer(self, name: str, status: int = 200, body: Any = b"", headers: Optional[Dict[str, str]] = None, *,
               wait: float = 0.0) -> None:
        """Answer one endpoint with this, whatever is asked."""
        content = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

        def reply(handler: "Handler", _: Received) -> None:
            time.sleep(wait)
            handler.reply(status, content, headers or {"Content-Type": "application/json"})

        self.overrides[self.paths(name)] = reply

    # ----------------------------------------------------------------- tokens

    def claims(self, requested_nonce: Optional[str] = None, /, **changes: Any) -> Dict[str, Any]:
        now = int(time.time())
        claims: Dict[str, Any] = {
            "iss": self.issuer, "aud": self.client_id, "azp": self.client_id, "sub": "subject-ada", "typ": "ID",
            "exp": now + 300, "iat": now, "jti": secrets.token_hex(8), "sid": "session-ada", "session_state": "session-ada",
            "acr": "1", "email": "ada@example.org", "email_verified": True, "name": "Ada Example",
            "given_name": "Ada", "family_name": "Example", "preferred_username": "ada",
        }
        if requested_nonce is not None:
            claims["nonce"] = requested_nonce
        claims.update(changes)
        return {name: value for name, value in claims.items() if value is not REMOVED}

    def logout_claims(self, **changes: Any) -> Dict[str, Any]:
        now = int(time.time())
        claims: Dict[str, Any] = {
            "iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 120, "jti": secrets.token_hex(8),
            "sub": "subject-ada", "sid": "session-ada", "typ": "Logout", "events": {LOGOUT_EVENT: {}},
        }
        claims.update(changes)
        return {name: value for name, value in claims.items() if value is not REMOVED}

    def sign(self, claims: Dict[str, Any], *, key: Any = None, alg: str = "RS256", header: Optional[Dict[str, Any]] = None) -> str:
        """Sign claims; ``header`` replaces or, with ``REMOVED``, drops header members."""
        key = key or self.key
        values: Dict[str, Any] = {"alg": alg, "typ": "JWT", "kid": key.kid, **(header or {})}
        values = {name: value for name, value in values.items() if value is not REMOVED}
        return raw_jws(values, json.dumps(claims).encode("utf-8"), key, alg)

    # ----------------------------------------------------------------- the browser's part

    def authorize(self, location: str, *, id_token: Optional[Callable[[Dict[str, Any]], str]] = None, **changes: Any) -> Dict[str, str]:
        """Sign a person in for an authorization request and return the query of the way back.

        ``changes`` change the claims of the ID token; ``id_token`` makes the whole token from them.
        """
        request = dict(parse_qsl(urlsplit(location).query, keep_blank_values=True))
        code = f"code-{secrets.token_urlsafe(24)}"
        claims = self.claims(request.get("nonce"), **changes)
        self.codes[code] = Code(request, claims, id_token(claims) if id_token else None)
        return {"code": code, "state": request["state"], "iss": self.issuer, "session_state": "session-ada"}

    def exchange(self, request: Received) -> Tuple[int, Dict[str, Any]]:
        """Answer the token endpoint as a Keycloak client that requires PKCE S256 does."""
        form = {name: values[0] for name, values in request.form.items()}
        code = self.codes.pop(form.get("code", ""), None)
        if form.get("grant_type") != "authorization_code" or form.get("client_id") != self.client_id:
            return 401, {"error": "invalid_client", "error_description": "provider text"}
        if code is None or form.get("redirect_uri") != code.request.get("redirect_uri"):
            return 400, {"error": "invalid_grant", "error_description": "provider text"}
        if code.request.get("code_challenge_method") != "S256" or s256(form.get("code_verifier", "")) != code.request.get("code_challenge"):
            return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
        return 200, {
            "access_token": f"access-{secrets.token_urlsafe(24)}", "refresh_token": f"refresh-{secrets.token_urlsafe(24)}",
            "expires_in": 300, "refresh_expires_in": 1800, "token_type": "Bearer", "scope": "openid profile email",
            "session_state": "session-ada", "id_token": code.id_token or self.sign(code.claims),
        }


REMOVED: Any = object()
"""A claim, header or metadata member that is left out."""

DEFAULT_PEOPLE = {
    "ada": {"sub": "subject-ada", "email": "ada@example.org", "name": "Ada Example", "preferred_username": "ada"},
    "ben": {"sub": "subject-ben", "email": "ben@example.org", "name": "Ben Example", "preferred_username": "ben"},
    "eve": {"sub": "subject-eve", "email": "eve@example.org", "name": "Eve Example", "preferred_username": "eve", "email_verified": False},
}


def s256(verifier: str) -> str:
    return b64(hashlib.sha256(verifier.encode("ascii")).digest())


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def raw_jws(header: Dict[str, Any], payload: bytes, key: Any, alg: str) -> str:
    """Sign by hand, so that a test can write any header; the library refuses many."""
    from joserfc.jws import JWSRegistry

    signing_input = f"{b64(json.dumps(header).encode('utf-8'))}.{b64(payload)}"
    if alg == "none":
        return f"{signing_input}."
    signature = JWSRegistry.algorithms[alg].sign(signing_input.encode("ascii"), key)
    return f"{signing_input}.{b64(signature)}"


def shared_key_jws(header: Dict[str, Any], payload: bytes, secret: bytes) -> str:
    """Sign with HS256 over any bytes, such as a public key's; the library refuses that too."""
    signing_input = f"{b64(json.dumps(header).encode('utf-8'))}.{b64(payload)}"
    return f"{signing_input}.{b64(hmac.new(secret, signing_input.encode('ascii'), hashlib.sha256).digest())}"


class Handler(BaseHTTPRequestHandler):
    """One connection to the provider."""

    protocol_version = "HTTP/1.1"
    server_version = "FakeProvider"

    @property
    def provider(self) -> FakeProvider:
        return self.server.provider  # type: ignore[attr-defined]

    def log_message(self, *_: Any) -> None:
        return

    def reply(self, status: int, content: bytes = b"", headers: Optional[Dict[str, str]] = None) -> None:
        try:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except OSError:
            self.close_connection = True

    def reply_json(self, status: int, value: Any) -> None:
        self.reply(status, json.dumps(value).encode("utf-8"), {"Content-Type": "application/json"})

    def handle_request(self) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        request = Received(self.command, parts.path, parts.query, dict(self.headers.items()), self.rfile.read(length))
        provider = self.provider
        provider.received.append(request)
        if request.path in provider.overrides:
            provider.overrides[request.path](self, request)
        elif request.path == provider.paths("metadata"):
            self.reply_json(200, provider.metadata())
        elif request.path == provider.paths("certs"):
            self.reply_json(200, provider.jwks())
        elif request.path == provider.paths("token") and request.method == "POST":
            self.reply_json(*provider.exchange(request))
        elif request.path == provider.paths("auth"):
            self.sign_in_page(request)
        elif request.path == provider.paths("logout"):
            self.sign_out_page(request)
        else:
            self.reply_json(404, {"error": "not found"})

    do_GET = do_POST = handle_request

    # ----------------------------------------------------------------- pages, for a browser

    def sign_in_page(self, request: Received) -> None:
        values = dict(parse_qsl(request.query, keep_blank_values=True))
        person = values.pop("person", None)
        back = values.get("redirect_uri", "")
        if person == "cancel":
            query = {"error": "access_denied", "error_description": "User cancelled", "state": values["state"], "iss": self.provider.issuer}
            self.reply(302, b"", {"Location": f"{back}?{urlencode(query)}"})
        elif person in self.provider.people:
            query = self.provider.authorize(f"?{urlencode(values)}", **self.provider.people[person])
            self.reply(302, b"", {"Location": f"{back}?{urlencode(query)}"})
        else:
            choices = {"ada": "Sign in as Ada", "ben": "Sign in as Ben", "eve": "Sign in as Eve (email not verified)", "cancel": "Cancel"}
            links = "".join(
                f'<p><a id="{name}" href="?{html.escape(urlencode({**values, "person": name}))}">{text}</a></p>'
                for name, text in choices.items()
            )
            self.page("Fake ROHub sign-in", links)

    def sign_out_page(self, request: Received) -> None:
        values = dict(parse_qsl(request.query, keep_blank_values=True))
        if "confirm" in values and values.get("post_logout_redirect_uri"):
            self.reply(302, b"", {"Location": values["post_logout_redirect_uri"]})
        elif "confirm" in values:
            self.page("Fake ROHub", "<p>You are logged out.</p>")
        else:
            self.page("Fake ROHub sign-out", f'<p><a id="confirm" href="?{html.escape(urlencode({**values, "confirm": "1"}))}">Log out</a></p>')

    def page(self, title: str, body: str) -> None:
        content = f"<!doctype html><title>{title}</title><h1>{title}</h1>{body}".encode("utf-8")
        self.reply(200, content, {"Content-Type": "text/html; charset=utf-8"})


def main() -> None:
    parser = argparse.ArgumentParser(description="A fake OpenID Connect provider, for tests only.")
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--host", default="127.0.0.1", help="the address to listen on")
    parser.add_argument("--name", default="localhost", help="the host in the issuer")
    parser.add_argument("--client-id", default="iadopt-test")
    arguments = parser.parse_args()
    provider = FakeProvider(client_id=arguments.client_id, port=arguments.port, host=arguments.host, name=arguments.name)
    print(f"issuer {provider.issuer}", flush=True)
    provider.serve()


if __name__ == "__main__":
    main()
