"""The trip to ROHub and back, and signing out (oidc-identity, sections 5, 6 and 9).

The routes of ``app.routers.auth`` call these functions. A sign-in that is refused ends on the sign-in
page with a fixed code; only the refusals that need an account at the provider are written to the audit
log, and no log line, audit row or answer holds a code, ``state``, nonce, verifier, key, token, cookie
or text of the provider.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode

from authlib.common.urls import add_params_to_uri
from authlib.integrations.requests_client import OAuth2Session
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from .. import storage
from ..auth import NOBODY, parse_utc, utc_iso, utc_now
from ..core.dependencies import account_store, auth_store
from ..guards import BACKCHANNEL_LOGOUT, SIGN_IN, SIGN_IN_RETURN, RequestRefused, exact_cookie, is_limited
from . import store, tokens
from .provider import Metadata, ProviderRefused, ProviderUnavailable, Turn, provider
from .settings import SignInSettings, current

logger = logging.getLogger(__name__)

SIGN_IN_PAGE = "/login.html"
SCOPE = "openid profile email"
SIGN_IN_LIFETIME = timedelta(minutes=10)
"""How long a sign-in may take."""

MAX_CODE_CHARS = 4_096
MAX_TARGET_CHARS = 2_048
MAX_SID_CHARS = 255
BROWSER_KEY_COOKIE = "iadopt_sign_in"
"""The cookie that binds a sign-in to its browser; with ``Secure`` its name has the prefix ``__Host-``."""

NO_STORE = {"Cache-Control": "no-store"}
BROWSER_HEADERS = {**NO_STORE, "Referrer-Policy": "no-referrer"}
"""On every answer of the routes a browser visits."""

AUDITED = frozenset({"token_invalid", "email_not_verified", "blocked"})
"""The refusals written to the audit log; anyone can cause the others without an account."""

_BROWSER_KEY = re.compile(r"[A-Za-z0-9_-]{43}")
_TARGET = re.compile(r"/([._-]*[A-Za-z0-9][A-Za-z0-9._-]*)?(?:[?#][\x21-\x5b\x5d-\x7e]*)?")
_NO_PAGES = ("api", "login.html")


class _Refused(Exception):
    """A sign-in that ends with a code."""

    def __init__(
        self, code: str, *, reason: str = "", rule: Optional[str] = None, account: Optional[Dict[str, Any]] = None
    ) -> None:
        """Say how it ends.

        Args:
            code: The code the sign-in page gets.
            reason: A fixed word for the log.
            rule: For ``token_invalid``, the rule that failed, as a fixed word.
            account: For ``blocked``, the account.
        """
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.rule = rule
        self.account = account


def rohub_switched_on() -> None:
    """FastAPI dependency: the routes of this module do not exist in the mode ``local``.

    Raises:
        RequestRefused: 404 when ROHub sign-in is not switched on.
    """
    if not current().rohub:
        detail = "ROHub sign-in is not switched on."
        raise RequestRefused(404, {"detail": detail, "reason": "oidc_disabled"}, BROWSER_HEADERS)


def safe_target(values: List[str]) -> str:
    """Return the page to come back to: a page of the application, as given, or the main page.

    ``frontend/src/lib/safeRedirect.ts`` judges the sign-in page's own parameter by the same rule.

    Args:
        values: The decoded ``redirect`` parameters of a request.
    """
    if len(values) != 1 or len(values[0]) > MAX_TARGET_CHARS:
        return "/"
    target = _TARGET.fullmatch(values[0])
    return values[0] if target and target.group(1) not in _NO_PAGES else "/"


def _hash(value: str) -> str:
    """The SHA-256 of a value the database must not hold."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _leave(location: str) -> Response:
    """Send the browser somewhere else."""
    return Response(status_code=303, headers={**BROWSER_HEADERS, "Location": location})


def _sign_in_page(code: str, target: Optional[str] = None) -> Response:
    """Send the browser to the sign-in page with a code, and with the valid page to come back to when it is known."""
    parameters = {"error": code} if target is None else {"error": code, "redirect": target}
    return _leave(f"{SIGN_IN_PAGE}?{urlencode(parameters)}")


# ------------------------------------------------------------------------------------------------
# Starting a sign-in (section 5)
# ------------------------------------------------------------------------------------------------


def start(request: Request) -> Response:
    """Begin a sign-in and send the browser to the provider.

    Args:
        request: ``GET /api/auth/login``.
    """
    settings = current()
    target = safe_target(request.query_params.getlist("redirect"))
    if is_limited(SIGN_IN, request) is not None:
        logger.info("A ROHub sign-in was not started: rate_limited.")
        return _sign_in_page("rate_limited", target)
    try:
        with provider.turn() as turn:
            metadata = provider.metadata(settings, turn)
    except ProviderUnavailable as e:
        logger.warning("A ROHub sign-in was not started: the provider is unavailable (%s).", e.reason)
        return _sign_in_page("provider_unavailable", target)

    state, nonce, code_verifier = (secrets.token_urlsafe(32) for _ in range(3))
    browser_key = _browser_key(request) or secrets.token_urlsafe(32)
    now = utc_now()
    transaction = store.Transaction(
        nonce=nonce,
        code_verifier=code_verifier,
        issuer=settings.issuer,
        redirect=target,
        created_at=utc_iso(now),
        expires_at=utc_iso(now + SIGN_IN_LIFETIME),
    )
    with storage.transaction(immediate=True, path=auth_store.db_path) as conn:
        store.add_transaction(conn, transaction, state_hash=_hash(state), browser_key_hash=_hash(browser_key))

    client = OAuth2Session(settings.client_id, code_challenge_method="S256")
    location, _ = client.create_authorization_url(
        metadata.authorization_endpoint,
        state=state,
        code_verifier=code_verifier,
        redirect_uri=settings.callback_url,
        scope=SCOPE,
        nonce=nonce,
    )
    response = _leave(location)
    response.set_cookie(
        auth_store.host_cookie_name(BROWSER_KEY_COOKIE),
        browser_key,
        max_age=int(SIGN_IN_LIFETIME.total_seconds()),
        httponly=True,
        secure=auth_store.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


def _browser_key(request: Request) -> Optional[str]:
    """Return the well-formed key a browser already has."""
    key = exact_cookie(request, auth_store.host_cookie_name(BROWSER_KEY_COOKIE))
    return key if key and _BROWSER_KEY.fullmatch(key) else None


# ------------------------------------------------------------------------------------------------
# Coming back (section 6)
# ------------------------------------------------------------------------------------------------


def finish(request: Request) -> Response:
    """Finish a sign-in: check what came back and start a session.

    Args:
        request: ``GET /api/auth/callback``.
    """
    settings = current()
    if is_limited(SIGN_IN_RETURN, request) is not None:
        logger.info("A ROHub sign-in was not finished: rate_limited.")
        return _sign_in_page("rate_limited")
    transaction = _take_transaction(request)
    target = transaction.redirect if transaction else None
    try:
        began = _usable(transaction, settings)
        claims = _verified_claims(request, settings, began)
        return _start_session(request, settings, began, claims)
    except _Refused as refusal:
        _note(request, refusal)
        return _sign_in_page(refusal.code, target)


def _usable(transaction: Optional[store.Transaction], settings: SignInSettings) -> store.Transaction:
    """Return a taken sign-in that has not expired and began with the configured issuer (step 2).

    Raises:
        _Refused: ``sign_in_expired`` otherwise.
    """
    if transaction is None:
        raise _Refused("sign_in_expired", reason="unknown")
    if transaction.expires_at <= utc_iso() or transaction.issuer != settings.issuer:
        raise _Refused("sign_in_expired", reason="expired or of another issuer")
    return transaction


def _take_transaction(request: Request) -> Optional[store.Transaction]:
    """Take the sign-in that the returned ``state`` and the browser's key name."""
    states = request.query_params.getlist("state")
    browser_key = _browser_key(request)
    if len(states) != 1 or browser_key is None:
        return None
    with storage.transaction(immediate=True, path=auth_store.db_path) as conn:
        return store.take_transaction(conn, state_hash=_hash(states[0]), browser_key_hash=_hash(browser_key))


def _verified_claims(request: Request, settings: SignInSettings, transaction: store.Transaction) -> Dict[str, Any]:
    """Return the claims of a verified identity with a verified email (steps 3 to 8).

    Raises:
        _Refused: With the code of the step that fails.
    """
    try:
        with provider.turn() as turn:
            code = _returned_code(request, settings, turn)
            metadata = provider.metadata(settings, turn)
            id_token = provider.exchange(
                settings, metadata, turn, code=code, code_verifier=transaction.code_verifier
            )
            claims = tokens.id_token_claims(
                id_token,
                algorithms=metadata.algorithms,
                find_key=_key_finder(metadata, turn),
                issuer=settings.issuer,
                client_id=settings.client_id,
                nonce=transaction.nonce,
                began_at=parse_utc(transaction.created_at).timestamp(),
                now=time.time(),
            )
    except ProviderUnavailable as e:
        raise _Refused("provider_unavailable", reason=e.reason) from None
    except ProviderRefused as e:
        raise _Refused("provider_refused", reason=e.reason) from None
    except tokens.TokenInvalid as e:
        raise _Refused("token_invalid", reason=e.rule, rule=e.rule) from None
    email = claims.get("email")
    if claims.get("email_verified") is not True or not isinstance(email, str) or not email.strip():
        raise _Refused("email_not_verified")
    return claims


def _returned_code(request: Request, settings: SignInSettings, turn: Turn) -> str:
    """Return the code that came back (steps 3 to 5).

    Raises:
        _Refused: When another issuer answered, the provider refused, or there is no usable code.
    """
    issuers = request.query_params.getlist("iss")
    if issuers != [settings.issuer] and (issuers or provider.metadata(settings, turn).sends_issuer):
        raise _Refused("provider_refused", reason="iss")
    errors = request.query_params.getlist("error")
    if errors:
        raise _Refused("cancelled" if errors == ["access_denied"] else "provider_refused", reason="error")
    codes = request.query_params.getlist("code")
    if len(codes) != 1 or not 1 <= len(codes[0]) <= MAX_CODE_CHARS:
        raise _Refused("provider_refused", reason="code")
    return codes[0]


def _key_finder(metadata: Metadata, turn: Turn) -> tokens.FindKey:
    """Return what finds the provider's key for a signature."""
    return lambda key_id, algorithm: provider.find_key(metadata, turn, key_id, algorithm)


def _start_session(
    request: Request, settings: SignInSettings, transaction: store.Transaction, claims: Dict[str, Any]
) -> Response:
    """Find or create the account of a verified identity and start its session (step 9).

    Raises:
        _Refused: When the account is blocked.
    """
    subject: str = claims["sub"]
    account = account_store.account_for_identity(settings.issuer, subject)
    if account and not account["is_active"]:
        raise _Refused("blocked", account=account)
    account, created = account_store.ensure_account(settings.issuer, subject, _profile(claims))
    cookie = auth_store.create_rohub_session(
        account["id"], request, issuer=settings.issuer, subject=subject, sid=_session_id(claims.get("sid"))
    )
    if cookie is None:  # blocked meanwhile
        raise _Refused("blocked", account=account)
    response = _leave(transaction.redirect)
    auth_store.set_rohub_cookie(response, cookie)
    if auth_store.cookie_name in request.cookies:
        auth_store.clear_session_cookie(response)  # the password session that just ended
    auth_store.audit_event(
        action="auth.login",
        user=account,
        request=request,
        status_code=303,
        metadata={"method": "rohub", "account_created": created},
    )
    return response


def _session_id(value: object) -> Optional[str]:
    """The provider's session id as far as it can be kept; without one, a sign-out there names the subject."""
    usable = isinstance(value, str) and 1 <= len(value) <= MAX_SID_CHARS and tokens.is_storable(value)
    return str(value) if usable else None


def _profile(claims: Dict[str, Any]) -> Dict[str, Any]:
    """The claims an account is made from; JSON can hold halves of characters, which no text has."""
    names = ("email", "email_verified", "name", "given_name", "family_name", "preferred_username")
    return {
        name: value.encode("utf-8", "replace").decode("utf-8") if isinstance(value, str) else value
        for name, value in claims.items()
        if name in names
    }


def _note(request: Request, refusal: _Refused) -> None:
    """Log a refused sign-in, and audit the ones that need an account at the provider."""
    logger.info("A ROHub sign-in was refused: %s (%s).", refusal.code, refusal.reason or refusal.code)
    if refusal.code not in AUDITED:
        return
    metadata = {"method": "rohub", "code": refusal.code, **({"rule": refusal.rule} if refusal.rule else {})}
    auth_store.audit_event(
        action="auth.login_failed",
        user=refusal.account or NOBODY,
        request=request,
        status_code=303,
        metadata=metadata,
        error=refusal.code,
    )


# ------------------------------------------------------------------------------------------------
# Signing out (section 9)
# ------------------------------------------------------------------------------------------------


def end_session(request: Request) -> Response:
    """Send the browser to the provider's sign-out page; it carries no token.

    Args:
        request: ``GET /api/auth/end-session``.
    """
    settings = current()
    if is_limited(SIGN_IN, request) is not None:
        return _sign_in_page("sign_out_incomplete")
    try:
        with provider.turn() as turn:
            endpoint = provider.metadata(settings, turn).end_session_endpoint
    except ProviderUnavailable as e:
        logger.warning("The provider's sign-out page cannot be reached (%s).", e.reason)
        endpoint = None
    if endpoint is None:
        return _sign_in_page("sign_out_incomplete")
    parameters = [("client_id", settings.client_id)]
    if settings.post_logout:
        parameters.append(("post_logout_redirect_uri", f"{settings.public_url}/"))
    return _leave(add_params_to_uri(endpoint, parameters))


class _UsedTokens:
    """The ``jti`` of every logout token that passed, remembered for as long as it would pass again."""

    def __init__(self) -> None:
        """Begin with none."""
        self._lock = threading.Lock()
        self._until: Dict[str, float] = {}

    def use(self, token_id: str, until: float, now: float) -> bool:
        """Remember a token and return whether it was new.

        Args:
            token_id: Its ``jti``.
            until: When it would pass no more, in seconds.
            now: The current time, in seconds.
        """
        with self._lock:
            self._until = {known: end for known, end in self._until.items() if end >= now}
            if token_id in self._until:
                return False
            self._until[token_id] = until
            return True

    def forget(self, token_id: str) -> None:
        """Forget a token whose sessions could not be ended, so that the provider may try again.

        Args:
            token_id: Its ``jti``.
        """
        with self._lock:
            self._until.pop(token_id, None)


used_tokens = _UsedTokens()


def backchannel_logout(request: Request, body: bytes) -> Response:
    """End the sessions a logout token of the provider names.

    Args:
        request: ``POST /api/auth/backchannel-logout``, sent by the provider.
        body: Its form.
    """
    settings = current()
    wait = is_limited(BACKCHANNEL_LOGOUT, request)
    if wait is not None:
        refusal = {"detail": f"Too many requests. Try again in {wait} seconds.", "reason": "rate_limited"}
        return JSONResponse(refusal, status_code=429, headers={**NO_STORE, "Retry-After": str(wait)})
    try:
        claims = _new_logout_claims(settings, _logout_token(body))
    except ProviderUnavailable as e:
        logger.warning("A back-channel sign-out failed: the provider is unavailable (%s).", e.reason)
        return JSONResponse({"error": "logout_failed"}, status_code=400, headers=NO_STORE)
    except tokens.TokenInvalid as e:
        logger.warning("A back-channel sign-out was refused: %s.", e.rule)
        return JSONResponse({"error": "invalid_request"}, status_code=400, headers=NO_STORE)
    try:
        with storage.transaction(immediate=True, path=auth_store.db_path) as conn:
            ended = store.end_sessions(
                conn, issuer=settings.issuer, sid=claims.get("sid"), subject=claims.get("sub")
            )
    except BaseException:
        used_tokens.forget(claims["jti"])
        raise
    if ended:
        auth_store.audit_event(
            action="auth.backchannel_logout",
            user=NOBODY,
            request=request,
            status_code=200,
            metadata={"sessions_ended": ended},
        )
    return Response(status_code=200, headers=NO_STORE)


def _new_logout_claims(settings: SignInSettings, token: str) -> Dict[str, Any]:
    """Return the claims of a logout token that passes and was not used before; it counts as used now.

    Raises:
        TokenInvalid: When it was used before.
    """
    now = time.time()
    with provider.turn() as turn:
        metadata = provider.metadata(settings, turn)
        claims = tokens.logout_token_claims(
            token,
            algorithms=metadata.algorithms,
            find_key=_key_finder(metadata, turn),
            issuer=settings.issuer,
            client_id=settings.client_id,
            now=now,
        )
    passes_until = claims["iat"] + tokens.LOGOUT_TOKEN_SECONDS + tokens.LEEWAY_SECONDS
    if not used_tokens.use(claims["jti"], passes_until, now):
        raise tokens.TokenInvalid("jti")
    return claims


def _logout_token(body: bytes) -> str:
    """Return the form's one ``logout_token``.

    Raises:
        TokenInvalid: When there is none.
    """
    try:
        fields = parse_qs(body.decode("ascii"), keep_blank_values=True, max_num_fields=20)
    except ValueError:
        raise tokens.TokenInvalid("form") from None
    values = fields.get("logout_token", [])
    if len(values) != 1:
        raise tokens.TokenInvalid("form")
    return values[0]
