"""The sign-in switch and the ROHub settings, judged at startup (oidc-identity, section 3).

The settings stay text in ``app.core.config`` until they are judged here. :func:`judge` decides
whether the application may start, with one message that names every setting at fault and no value
of any. :func:`current` gives a request the judged values.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from ..core import config
from ..guards import guard_problems
from ..usage import usage_problems

PRODUCTION = "production"
DEVELOPMENT = "development"
LOCAL = "local"
OIDC = "oidc"
BOTH = "both"

CALLBACK_PATH = "/api/auth/callback"
MAX_CLIENT_ID_CHARS = 255

_DEFAULT_PORTS = {"http": 80, "https": 443}
_AUTHORITY = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[^\[\]:@]+)(?::[0-9]{1,5})?")
"""A host, an IPv6 address in brackets or a name, and a port; no user and nothing beside the brackets."""

_HOST_NAME = re.compile(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*")


class SettingsError(RuntimeError):
    """Settings that stop the start; the message names each of them and holds no value."""

    def __init__(self, problems: List[str]) -> None:
        """Join the problems into one message.

        Args:
            problems: One sentence for each setting at fault.
        """
        super().__init__("The application does not start with these settings. " + " ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class SignInSettings:
    """The judged settings of a deployment's sign-in.

    Attributes:
        environment: ``production`` or ``development``.
        mode: ``local``, ``oidc`` or ``both``.
        issuer: The provider's issuer URL exactly as it writes it; empty in ``local``.
        client_id: The client ID at the provider; empty in ``local``.
        public_url: The application's origin as a browser writes it; empty in ``local``.
        post_logout: Whether the provider has registered the return address after a sign-out.
    """

    environment: str = PRODUCTION
    mode: str = LOCAL
    issuer: str = ""
    client_id: str = ""
    public_url: str = ""
    post_logout: bool = False

    @property
    def development(self) -> bool:
        """Whether this is a development machine, where ``http`` to itself is allowed."""
        return self.environment == DEVELOPMENT

    @property
    def password(self) -> bool:
        """Whether people sign in with a password, and password sessions count."""
        return self.mode in (LOCAL, BOTH)

    @property
    def rohub(self) -> bool:
        """Whether people sign in with ROHub, and ROHub sessions count."""
        return self.mode in (OIDC, BOTH)

    @property
    def methods(self) -> str:
        """The sign-in methods as the header ``X-IADOPT-Sign-In`` names them."""
        return ", ".join(name for name, on in (("password", self.password), ("rohub", self.rohub)) if on)

    @property
    def callback_url(self) -> str:
        """Where the provider sends the browser back to."""
        return self.public_url + CALLBACK_PATH


@dataclass(frozen=True)
class _Address:
    """A parsed absolute ``http`` or ``https`` URL with its host as a browser writes it."""

    scheme: str
    host: str
    port: Optional[int]
    path: str

    @property
    def origin(self) -> str:
        """The origin as a browser writes it: without the scheme's own port."""
        port = "" if self.port in (None, _DEFAULT_PORTS[self.scheme]) else f":{self.port}"
        return f"{self.scheme}://{self.host}{port}"

    @property
    def is_loopback(self) -> bool:
        """Whether the address is the machine itself, by the names section 3 allows ``http`` for."""
        return self.host in ("localhost", "127.0.0.1", "[::1]") or self.host.endswith(".localhost")


def _parse_address(text: str) -> Optional[_Address]:
    """Parse an absolute ``http`` or ``https`` URL, or return ``None`` when it is none.

    ``urlsplit`` forgives characters that no URL has, so they are refused first.
    """
    if not text or any(not "\x21" <= character <= "\x7e" or character == "\\" for character in text):
        return None
    try:
        parts = urlsplit(text)
        host, port = _normal_host(parts.hostname or ""), parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or host is None or not _AUTHORITY.fullmatch(parts.netloc) or port == 0:
        return None
    return _Address(parts.scheme, host, port, parts.path)


def _normal_host(host: str) -> Optional[str]:
    """Return a host as a browser writes it, an IPv6 address in brackets; ``None`` when it is no host."""
    if ":" in host:
        return f"[{ipaddress.IPv6Address(host)}]" if "%" not in host else None
    if not _HOST_NAME.fullmatch(host):
        return None
    if re.fullmatch(r"[0-9]+|0x[0-9a-f]*", host.rpartition(".")[2]):  # a browser reads such a name as an IPv4 address
        return str(ipaddress.IPv4Address(host))
    return host


def is_allowed_address(text: object, *, development: bool) -> bool:
    """Return whether an address may be used: ``https``, or ``http`` to the machine itself in development.

    Args:
        text: The address, as a setting or the provider's metadata gives it.
        development: Whether this is a development machine.
    """
    address = _parse_address(text) if isinstance(text, str) and "#" not in text else None
    return address is not None and (address.scheme == "https" or (development and address.is_loopback))


_ADDRESS_RULE = "use https, or on a development machine http to localhost, a name ending in .localhost, 127.0.0.1 or [::1]"


def rohub_settings(environment: str) -> Tuple[SignInSettings, List[str]]:
    """Read the ROHub settings whatever the mode, as ``oidc check`` does too.

    Args:
        environment: ``production`` or ``development``.

    Returns:
        The values, with the mode left at its default, and one sentence for each setting at fault.
    """
    raw = config.settings
    development = environment == DEVELOPMENT
    problems: List[str] = []

    issuer = raw.oidc_issuer_raw
    if not issuer:
        problems.append("IADOPT_OIDC_ISSUER is missing.")
    elif _parse_address(issuer) is None or "?" in issuer or "#" in issuer:
        problems.append("IADOPT_OIDC_ISSUER must be the provider's issuer URL without query or fragment.")
    elif not is_allowed_address(issuer, development=development):
        problems.append(f"IADOPT_OIDC_ISSUER must {_ADDRESS_RULE}.")

    client_id = raw.oidc_client_id_raw
    if not client_id:
        problems.append("IADOPT_OIDC_CLIENT_ID is missing.")
    elif len(client_id) > MAX_CLIENT_ID_CHARS or not all(c.isprintable() and not c.isspace() for c in client_id):
        problems.append(f"IADOPT_OIDC_CLIENT_ID must have 1 to {MAX_CLIENT_ID_CHARS} printable characters without spaces.")

    public = _parse_address(raw.public_url_raw)
    if not raw.public_url_raw:
        problems.append("IADOPT_PUBLIC_URL is missing.")
    elif public is None or public.path not in ("", "/") or "?" in raw.public_url_raw or "#" in raw.public_url_raw:
        problems.append("IADOPT_PUBLIC_URL must be the application's scheme, host and optional port, with no path but /.")
    elif not is_allowed_address(raw.public_url_raw, development=development):
        problems.append(f"IADOPT_PUBLIC_URL must {_ADDRESS_RULE}.")

    post_logout = raw.oidc_post_logout_raw
    if post_logout not in ("", "true", "false"):
        problems.append("IADOPT_OIDC_POST_LOGOUT must be true or false.")

    values = SignInSettings(
        environment=environment,
        issuer=issuer,
        client_id=client_id,
        public_url=public.origin if public else "",
        post_logout=post_logout == "true",
    )
    return values, problems


def environment_and_mode() -> Tuple[str, Optional[str], List[str]]:
    """Read ``IADOPT_ENVIRONMENT`` and ``IADOPT_AUTH_MODE``.

    Returns:
        The environment, where anything unknown counts as production; the mode, ``None`` when it is
        unknown; and one sentence for each setting at fault.
    """
    raw = config.settings
    environment, mode = raw.environment_raw or PRODUCTION, raw.auth_mode_raw or LOCAL
    problems = []
    if environment not in (PRODUCTION, DEVELOPMENT):
        problems.append("IADOPT_ENVIRONMENT must be production or development.")
        environment = PRODUCTION
    if mode not in (LOCAL, OIDC, BOTH):
        problems.append("IADOPT_AUTH_MODE must be local, oidc or both.")
        return environment, None, problems
    if mode == BOTH and environment != DEVELOPMENT:
        problems.append("IADOPT_AUTH_MODE may be both only with IADOPT_ENVIRONMENT=development.")
    return environment, mode, problems


def judge(*, auth_enabled: bool, cookie_secure: bool) -> SignInSettings:
    """Judge the settings for a start; nothing is read but the settings.

    Args:
        auth_enabled: Whether the auth store enforces authentication.
        cookie_secure: Whether the auth store's cookies are ``Secure``.

    Raises:
        SettingsError: When the application must not start with them.
    """
    environment, mode, problems = environment_and_mode()
    problems += guard_problems() + usage_problems()
    if not auth_enabled:
        if environment != DEVELOPMENT:
            problems.append("IADOPT_AUTH_ENABLED must be true unless IADOPT_ENVIRONMENT is development.")
        if mode in (OIDC, BOTH):
            problems.append("IADOPT_AUTH_ENABLED must be true with IADOPT_AUTH_MODE oidc or both.")
    judged = SignInSettings(environment=environment)
    if mode in (OIDC, BOTH):
        values, rohub_problems = rohub_settings(environment)
        problems += rohub_problems
        if values.public_url and cookie_secure != values.public_url.startswith("https:"):
            needed = "false with an http" if cookie_secure else "true with an https"
            problems.append(f"IADOPT_COOKIE_SECURE must be {needed} IADOPT_PUBLIC_URL.")
        if mode == OIDC and environment == PRODUCTION and not config.settings.trusted_proxy_hops_raw:
            problems.append("IADOPT_TRUSTED_PROXY_HOPS is missing, the number of proxies in front of the backend.")
        judged = replace(values, mode=mode)
    if problems:
        raise SettingsError(problems)
    return judged


def current() -> SignInSettings:
    """Return the judged settings for a request; ``SettingsError`` when the start did not judge them.

    The answer follows the settings, so nothing has to be reset when they change.
    """
    raw = config.settings
    return _current(
        raw.environment_raw,
        raw.auth_mode_raw,
        raw.oidc_issuer_raw,
        raw.oidc_client_id_raw,
        raw.public_url_raw,
        raw.oidc_post_logout_raw,
    )


@lru_cache(maxsize=32)
def _current(*_: str) -> SignInSettings:
    """Read the settings once for each set of their texts.

    Raises:
        SettingsError: When a setting the mode needs is at fault.
    """
    environment, mode, problems = environment_and_mode()
    judged = SignInSettings(environment=environment)
    if mode in (OIDC, BOTH):
        values, rohub_problems = rohub_settings(environment)
        problems += rohub_problems
        judged = replace(values, mode=mode)
    if problems:
        raise SettingsError(problems)
    return judged
