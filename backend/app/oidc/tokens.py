"""The provider's signed tokens: the ID token and the logout token (oidc-identity, sections 7 and 9).

joserfc checks the form, the algorithm, the signature and the times; this module says what they must
be and adds the rules the library does not have.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from joserfc import jwt
from joserfc.errors import (
    BadSignatureError,
    ClaimError,
    MissingAlgorithmError,
    UnsupportedAlgorithmError,
)
from joserfc.jwk import Key
from joserfc.jws import JWSRegistry

from .provider import ProviderError

LEEWAY_SECONDS = 60
MAX_SUBJECT_CHARS = 255
LOGOUT_TOKEN_SECONDS = 600
"""How old a logout token may be."""

LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"

FindKey = Callable[[Optional[str], str], Optional[Key]]
"""Finds the provider's key for a key id and an algorithm."""

_KEY_IN_HEADER = frozenset({"jwk", "jku", "x5c", "x5u"})


class TokenInvalid(Exception):
    """A token that fails a rule, as a fixed word."""

    def __init__(self, rule: str) -> None:
        """Name the rule.

        Args:
            rule: The part of section 7's table, or of section 9's list, in lower case.
        """
        super().__init__(rule)
        self.rule = rule


def signed_claims(token: object, *, algorithms: Sequence[str], find_key: FindKey) -> Dict[str, Any]:
    """Return the claims of a token the provider signed.

    Args:
        token: The token as it came.
        algorithms: The accepted algorithms.
        find_key: Finds the provider's key; it may raise ``ProviderUnavailable``.

    Raises:
        TokenInvalid: When its form, algorithm or signature is not accepted.
        ProviderError: When the keys cannot be read.
    """

    def key_of(signature: Any) -> Key:
        header = signature.headers()
        if _KEY_IN_HEADER & header.keys():
            raise TokenInvalid("signature")
        if "crit" in header or "b64" in header:
            raise TokenInvalid("form")
        key = find_key(header.get("kid"), header["alg"])
        if key is None:
            raise TokenInvalid("signature")
        return key

    if not isinstance(token, str):
        raise TokenInvalid("form")
    # A header the library does not know is ignored, as the standard asks, unless it is critical.
    registry = JWSRegistry(algorithms=list(algorithms), strict_check_header=False)
    try:
        return jwt.decode(token, key_of, registry=registry).claims
    except (TokenInvalid, ProviderError):
        raise
    except (MissingAlgorithmError, UnsupportedAlgorithmError):
        raise TokenInvalid("algorithm") from None
    except BadSignatureError:
        raise TokenInvalid("signature") from None
    except Exception:  # whatever a hostile token makes the library raise
        raise TokenInvalid("form") from None


def id_token_claims(
    token: object,
    *,
    algorithms: Sequence[str],
    find_key: FindKey,
    issuer: str,
    client_id: str,
    nonce: str,
    began_at: float,
    now: float,
) -> Dict[str, Any]:
    """Return the claims of an ID token that passes section 7.

    Args:
        token: The token as it came.
        algorithms: The accepted algorithms.
        find_key: Finds the provider's key; it may raise ``ProviderUnavailable``.
        issuer: The configured issuer.
        client_id: The client ID.
        nonce: The sign-in's nonce.
        began_at: When the sign-in began, in seconds.
        now: The current time, in seconds.

    Raises:
        TokenInvalid: When it fails a rule.
    """
    claims = signed_claims(token, algorithms=algorithms, find_key=find_key)
    _check_issuer_and_audience(claims, issuer=issuer, client_id=client_id, now=now)
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if "azp" not in claims and any(audience != client_id for audience in audiences):
        raise TokenInvalid("aud")
    if claims.get("azp", client_id) != client_id:
        raise TokenInvalid("azp")
    _check_times(claims, required=("exp", "iat"), allowed=("nbf",), now=now)
    if claims["iat"] < began_at - LEEWAY_SECONDS:
        raise TokenInvalid("iat")
    if claims.get("nonce") != nonce:
        raise TokenInvalid("nonce")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not 1 <= len(subject) <= MAX_SUBJECT_CHARS or not is_storable(subject):
        raise TokenInvalid("sub")
    return claims


def logout_token_claims(
    token: object, *, algorithms: Sequence[str], find_key: FindKey, issuer: str, client_id: str, now: float
) -> Dict[str, Any]:
    """Return the claims of a logout token that passes section 9, except that its ``jti`` may have been used.

    Args:
        token: The token as it came.
        algorithms: The accepted algorithms.
        find_key: Finds the provider's key; it may raise ``ProviderUnavailable``.
        issuer: The configured issuer.
        client_id: The client ID.
        now: The current time, in seconds.

    Raises:
        TokenInvalid: When it fails a rule.
    """
    claims = signed_claims(token, algorithms=algorithms, find_key=find_key)
    _check_issuer_and_audience(claims, issuer=issuer, client_id=client_id, now=now)
    _check_times(claims, required=("iat",), allowed=("exp",), now=now)
    if claims["iat"] < now - LOGOUT_TOKEN_SECONDS - LEEWAY_SECONDS:
        raise TokenInvalid("iat")
    token_id = claims.get("jti")
    if not isinstance(token_id, str) or not token_id:
        raise TokenInvalid("jti")
    events = claims.get("events")
    if not isinstance(events, dict) or not isinstance(events.get(LOGOUT_EVENT), dict):
        raise TokenInvalid("events")
    if "nonce" in claims:
        raise TokenInvalid("nonce")
    named = [claims[name] for name in ("sub", "sid") if name in claims]
    if not named or not all(isinstance(value, str) and value and is_storable(value) for value in named):
        raise TokenInvalid("sub")
    return claims


def _check_issuer_and_audience(claims: Dict[str, Any], *, issuer: str, client_id: str, now: float) -> None:
    """Check ``iss`` and that ``aud`` holds the client ID, or fail with the first that does not pass."""
    _validate(claims, ("iss", "aud"), now=now, iss={"essential": True, "value": issuer}, aud={"essential": True, "value": client_id})


def _check_times(claims: Dict[str, Any], *, required: Sequence[str], allowed: Sequence[str] = (), now: float) -> None:
    """Check that times are numbers within the leeway: the required ones, and the allowed ones where they are present."""
    required_times: Dict[str, jwt.ClaimsOption] = {name: {"essential": True} for name in required}
    _validate(claims, (*required, *allowed), now=now, **required_times)


def _validate(claims: Dict[str, Any], names: Sequence[str], *, now: float, **options: jwt.ClaimsOption) -> None:
    """Let the library check the named claims, and no other: those are ignored.

    Raises:
        TokenInvalid: Naming the first claim that fails.
    """
    registry = jwt.JWTClaimsRegistry(now=int(now), leeway=LEEWAY_SECONDS, **options)
    try:
        registry.validate({name: claims[name] for name in names if name in claims})
    except ClaimError as e:
        raise TokenInvalid(e.claim.split(",")[0]) from None


def is_storable(text: str) -> bool:
    """Return whether a text can be stored: JSON can hold halves of characters that no text has.

    Args:
        text: The text.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
