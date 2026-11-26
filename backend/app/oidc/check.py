"""``python -m app.cli oidc check``: is the configured provider set up for us? (keycloak-deployment, section 5).

It reads the ROHub settings whatever the sign-in mode, sends requests to the provider only, follows
no redirect, signs nobody in, writes nothing and prints no secret setting.
"""

from __future__ import annotations

import secrets
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

from authlib.common.urls import add_params_to_uri
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from . import provider as providers
from .provider import Answer, ProviderUnavailable, Turn
from .settings import SignInSettings, environment_and_mode, rohub_settings

Print = Callable[[str], None]

ACCEPTED = "accepted"
REFUSED = "refused"
UNCLEAR = "unclear"

_REDIRECTS = (301, 302, 303, 307, 308)
_CHECK_NAMES = {
    "issuer": "the metadata and its issuer",
    "endpoints": "the endpoints' addresses",
    "s256": "PKCE S256 offered",
    "algorithms": "an allowed signing algorithm",
}


class _Report:
    """One line per check, and whether all were ok."""

    def __init__(self, write: Print) -> None:
        """Begin with nothing checked.

        Args:
            write: Prints a line.
        """
        self._write = write
        self.ok = True

    def check(self, name: str, problem: Optional[str]) -> None:
        """Report one check.

        Args:
            name: What was checked.
            problem: Why it failed, or ``None``.
        """
        self.ok = self.ok and problem is None
        self._write(f"ok      {name}" if problem is None else f"failed  {name}: {problem}")

    def note(self, text: str) -> None:
        """Say something that fails nothing.

        Args:
            text: What to say.
        """
        self._write(f"        {text}")


def run(*, pkce: bool, refused_callbacks: Sequence[str], write: Print = print) -> bool:
    """Check the configured provider and return whether every check was ok.

    Args:
        pkce: Whether to see that the client accepts ``S256`` and refuses everything else.
        refused_callbacks: Callback addresses the client must refuse.
        write: Prints a line.
    """
    report = _Report(write)
    environment, _, problems = environment_and_mode()
    settings, rohub_problems = rohub_settings(environment)
    # Which mode is set does not matter here, so only what the check itself reads can fail it.
    wrong = [problem for problem in problems if problem.startswith("IADOPT_ENVIRONMENT")] + rohub_problems
    report.check("the settings", " ".join(wrong) or None)
    if wrong:
        return False
    with providers.Provider().turn() as turn:
        document = _metadata(turn, settings, report)
        if document is None:
            return False
        _endpoints(turn, settings, document, report)
        if report.ok and (pkce or refused_callbacks):
            _client(turn, settings, document, report, pkce=pkce, refused_callbacks=refused_callbacks)
    return report.ok


def _metadata(turn: Turn, settings: SignInSettings, report: _Report) -> Optional[Dict[str, object]]:
    """Read the metadata and judge it; ``None`` when there is none."""
    try:
        document = providers.read_metadata_document(turn, settings)
    except ProviderUnavailable as e:
        report.check(_CHECK_NAMES["issuer"], e.reason)
        return None
    for name, problem in providers.metadata_problems(document, settings).items():
        report.check(_CHECK_NAMES[name], problem)
    return document


def _endpoints(turn: Turn, settings: SignInSettings, document: Dict[str, object], report: _Report) -> None:
    """Check that there is a usable key, and say what signing out can do."""
    try:
        metadata = providers.accept_metadata(document, settings)
        keys = providers.read_keys(turn, metadata.jwks_uri)
    except ProviderUnavailable as e:
        report.check("a usable key", e.reason)
        return
    usable = any(providers.fits(key, algorithm) for key in keys for algorithm in metadata.algorithms)
    report.check("a usable key", None if usable else "no_usable_key")
    offered = {True: "offered", False: "not offered"}
    report.note(f"a sign-out page is {offered[metadata.end_session_endpoint is not None]}")
    report.note(f"back-channel sign-out is {offered[metadata.backchannel_logout]}")


def _client(
    turn: Turn,
    settings: SignInSettings,
    document: Dict[str, object],
    report: _Report,
    *,
    pkce: bool,
    refused_callbacks: Sequence[str],
) -> None:
    """Ask for authorization as a browser would, and judge what the provider answers."""
    challenge = create_s256_code_challenge(secrets.token_urlsafe(32))
    s256 = {"code_challenge": challenge, "code_challenge_method": "S256"}
    endpoint = str(document["authorization_endpoint"])

    def ask(name: str, wanted: str, callback: str, parameters: Dict[str, str]) -> None:
        answer = _authorize(turn, settings, endpoint, callback, parameters)
        report.check(name, None if answer == wanted else f"was {answer}, not {wanted}")

    ask("the request with S256 is accepted", ACCEPTED, settings.callback_url, s256)
    if pkce:
        plain = {"code_challenge": secrets.token_urlsafe(32), "code_challenge_method": "plain"}
        ask("the request without a challenge is refused", REFUSED, settings.callback_url, {})
        ask("the request with plain is refused", REFUSED, settings.callback_url, plain)
    for callback in refused_callbacks:
        ask(f"the callback {callback} is refused", REFUSED, callback, s256)


def _authorize(turn: Turn, settings: SignInSettings, endpoint: str, callback: str, parameters: Dict[str, str]) -> str:
    """Send one authorization request and say what became of it."""
    request: List[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", settings.client_id),
        ("redirect_uri", callback),
        ("scope", "openid profile email"),
        ("state", secrets.token_urlsafe(32)),
        ("nonce", secrets.token_urlsafe(32)),
        *parameters.items(),
    ]
    try:
        answer = providers.send(turn, "GET", add_params_to_uri(endpoint, request), headers={"Accept": "text/html"})
    except ProviderUnavailable as e:
        return f"{UNCLEAR} ({e.reason})"
    return _judge(answer, settings)


def _judge(answer: Answer, settings: SignInSettings) -> str:
    """Return whether an authorization request was accepted, refused or neither."""
    if answer.status_code == 200:
        return ACCEPTED
    if answer.status_code == 400:
        return REFUSED
    if answer.status_code not in _REDIRECTS or not answer.location:
        return f"{UNCLEAR} (status {answer.status_code})"
    parts = urlsplit(answer.location)
    if not any("error" in parse_qs(text, keep_blank_values=True) for text in (parts.query, parts.fragment)):
        return ACCEPTED
    target = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return REFUSED if target == settings.callback_url else f"{UNCLEAR} (a redirect elsewhere with an error)"
