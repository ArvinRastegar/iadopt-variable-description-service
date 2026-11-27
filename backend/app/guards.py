"""Where a request comes from and how much of it the backend takes (``components/request-guards.md``).

The client's address behind the configured proxies, limits on how often an address or an account may
do something, and bounds on the size of a request. Limits live in this process: one replica with one
worker is a documented constraint (D-014).
"""

from __future__ import annotations

import ipaddress
import math
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Mapping, MutableMapping, Optional, Tuple, Union

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .core import config

# ------------------------------------------------------------------------------------------------
# Reading a request as it was sent
# ------------------------------------------------------------------------------------------------


def route_path(scope: Mapping[str, Any]) -> str:
    """Return the path the routes are matched by: without the root path a server may be started with.

    ``request.url`` is no answer, since it also holds the ``Host`` header.

    Args:
        scope: The ASGI connection.
    """
    path, root = str(scope["path"]), str(scope.get("root_path") or "")
    return path[len(root) :] if root and path.startswith(f"{root}/") else path


def exact_cookie(request: Request, name: str) -> Optional[str]:
    """Return a cookie by its exact name; ``None`` when it was sent twice.

    The framework's parser also trims characters a browser takes as part of a name, with which another
    host could pass a cookie off as one with the prefix ``__Host-``.

    Args:
        request: The request.
        name: The cookie's name.
    """
    values = []
    for header, line in request.scope["headers"]:
        if header != b"cookie":
            continue
        for part in line.split(b";"):
            found, _, value = part.strip(b" \t").partition(b"=")
            if found == name.encode("ascii"):
                values.append(value.decode("latin-1"))
    return values[0] if len(values) == 1 else None


# ------------------------------------------------------------------------------------------------
# The client's address (section 3)
# ------------------------------------------------------------------------------------------------

MAX_PROXY_HOPS = 10

_HOPS_RULE = f"IADOPT_TRUSTED_PROXY_HOPS must be a whole number from 0 to {MAX_PROXY_HOPS}."

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def trusted_proxy_hops() -> Optional[int]:
    """Return how many proxies in front of the backend append to ``X-Forwarded-For``; ``None`` when unset.

    Raises:
        ValueError: If the setting is not a whole number from 0 to 10.
    """
    text = config.settings.trusted_proxy_hops_raw
    if not text:
        return None
    if not re.fullmatch(r"[0-9]|10", text):
        raise ValueError(_HOPS_RULE)
    return int(text)


def client_ip(request: Optional[Request]) -> str:
    """Return the address a request comes from; everything that stores or limits by it calls this.

    Without the setting it is the first ``X-Forwarded-For`` value, else the socket peer, as before the
    setting existed. With N proxies it is the entry N places before the peer in the chain of every
    ``X-Forwarded-For`` value and the peer, so what a client writes in front never counts.

    Args:
        request: The request, or ``None`` for an offline command.

    Returns:
        The address in its normal form, or empty when there is none.
    """
    if not request:
        return ""
    hops = trusted_proxy_hops()
    peer = request.client.host if request.client else ""
    if hops is None:
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",", 1)[0].strip() if forwarded else peer
    values = [value.strip() for line in request.headers.getlist("x-forwarded-for") for value in line.split(",")]
    chain = [value for value in values if value] + [peer]
    entry = chain[max(0, len(chain) - 1 - hops)]
    return str(_parse_ip(entry) or _parse_ip(peer) or peer)


def _parse_ip(text: str) -> Optional[IPAddress]:
    """Parse an address that may carry a port or brackets; IPv4 written as IPv6 is IPv4."""
    bracketed = re.fullmatch(r"\[([^\]]*)\](?::[0-9]{1,5})?", text)
    with_port = re.fullmatch(r"([0-9.]+):[0-9]{1,5}", text)
    if bracketed:
        text = bracketed.group(1)
        if ":" not in text:
            return None
    elif with_port:
        text = with_port.group(1)
    try:
        # A zone names an interface of the machine that wrote it, which says nothing here.
        address = ipaddress.ip_address(text if "%" not in text else "")
    except ValueError:
        return None
    return address.ipv4_mapped or address if isinstance(address, ipaddress.IPv6Address) else address


# ------------------------------------------------------------------------------------------------
# Limits (section 4)
# ------------------------------------------------------------------------------------------------

MAX_BUCKETS = 10_000
"""Most buckets one scope keeps; the least recently used go first."""

SIGN_IN = "sign_in"
SIGN_IN_RETURN = "sign_in_return"
BACKCHANNEL_LOGOUT = "backchannel_logout"
OWNER_SIGN_IN = "owner_sign_in"
"""The owner's password route; counted after the body's validation, and no setting changes it (owner-sign-in, section 5)."""

Key = Tuple[str, str]
"""What a bucket belongs to within its scope: a kind and a name, so that kinds never share one."""


@dataclass(frozen=True)
class Limit:
    """At most ``burst`` requests at once, refilled evenly over ``seconds``."""

    burst: int
    seconds: int


_DEFAULT_LIMITS = {
    SIGN_IN: Limit(120, 300),
    SIGN_IN_RETURN: Limit(120, 300),
    BACKCHANNEL_LOGOUT: Limit(600, 300),
    OWNER_SIGN_IN: Limit(10, 600),
}
_MAX_LIMIT = Limit(100_000, 86_400)
_LIMIT_RULE = (
    "IADOPT_SIGN_IN_LIMIT must be <requests>/<seconds> with whole numbers from 1 to "
    f"{_MAX_LIMIT.burst} and from 1 to {_MAX_LIMIT.seconds}."
)


def limit_of(scope: str) -> Limit:
    """Return the limit of a scope; the setting changes those of the sign-in.

    Args:
        scope: One of the scopes above.

    Raises:
        ValueError: If the setting is malformed.
    """
    text = config.settings.sign_in_limit_raw
    if not text or scope not in (SIGN_IN, SIGN_IN_RETURN):
        return _DEFAULT_LIMITS[scope]
    parts = re.fullmatch(r"([1-9][0-9]{0,5})/([1-9][0-9]{0,4})", text)
    limit = Limit(int(parts.group(1)), int(parts.group(2))) if parts else None
    if limit is None or limit.burst > _MAX_LIMIT.burst or limit.seconds > _MAX_LIMIT.seconds:
        raise ValueError(_LIMIT_RULE)
    return limit


def guard_problems() -> List[str]:
    """Return one sentence for each setting of this module that stops the start."""
    problems = []
    for read in (trusted_proxy_hops, lambda: limit_of(SIGN_IN)):
        try:
            read()
        except ValueError as e:
            problems.append(str(e))
    return problems


def address_key(address: str) -> Key:
    """Return the key of a client's address: an IPv6 address counts by its /64 network.

    Args:
        address: What :func:`client_ip` returned; anything but an IP address is one shared key.
    """
    ip = _parse_ip(address)
    if ip is None:
        return ("address", "unknown")
    if isinstance(ip, ipaddress.IPv6Address):
        return ("address", str(ipaddress.IPv6Network((ip, 64), strict=False)))
    return ("address", str(ip))


def account_key(account_id: int) -> Key:
    """Return the key of an account.

    Args:
        account_id: The account's id.
    """
    return ("account", str(account_id))


class RateLimiter:
    """Token buckets per scope and key, kept in this process."""

    def __init__(self, limits: Callable[[str], Limit] = limit_of, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Start with every bucket full.

        Args:
            limits: Gives a scope's limit.
            clock: A monotonic clock in seconds.
        """
        self._limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: Dict[str, OrderedDict[Key, Tuple[float, float]]] = {}

    def check(self, scope: str, key: Key) -> Optional[int]:
        """Take one token, or say how long to wait for the next.

        Args:
            scope: What is limited.
            key: Whose bucket, from :func:`address_key` or :func:`account_key`.

        Returns:
            ``None`` when the request may pass, else the whole seconds until a token, at least 1.
        """
        limit = self._limits(scope)
        rate = limit.burst / limit.seconds
        with self._lock:
            now = self._clock()
            buckets = self._buckets.setdefault(scope, OrderedDict())
            tokens, seen = buckets.pop(key, (float(limit.burst), now))
            tokens = min(float(limit.burst), tokens + max(0.0, now - seen) * rate)
            # The smallest amounts are rounding: a token that is due counts as whole.
            passes = tokens > 1 - 1e-6
            buckets[key] = (max(0.0, tokens - 1) if passes else tokens, now)
            while len(buckets) > MAX_BUCKETS:
                buckets.popitem(last=False)
        return None if passes else max(1, math.ceil((1 - tokens) / rate - 1e-6))


limiter = RateLimiter()
"""The process's limiter."""


class RequestRefused(Exception):
    """A request the guards refuse: its status, body and headers."""

    def __init__(self, status_code: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        """Describe the response.

        Args:
            status_code: The HTTP status.
            body: The JSON body, ``detail`` and ``reason``.
            headers: Headers besides the content type.
        """
        super().__init__(body["detail"])
        self.status_code = status_code
        self.body = body
        self.headers = headers


def request_refused_response(_: Request, refusal: Exception) -> JSONResponse:
    """Exception handler: answer a :class:`RequestRefused`.

    Args:
        _: The refused request.
        refusal: The refusal.
    """
    assert isinstance(refusal, RequestRefused)
    return JSONResponse(refusal.body, status_code=refusal.status_code, headers=refusal.headers)


def is_limited(scope: str, request: Request) -> Optional[int]:
    """Count a request against its address's limit.

    Args:
        scope: What is limited.
        request: The request.

    Returns:
        ``None`` when it may pass, else the seconds to wait.
    """
    return limiter.check(scope, address_key(client_ip(request)))


def refuse_if_limited(scope: str, request: Request) -> None:
    """Count a request against its address's limit and answer 429 beyond it.

    A route calls it itself where the limit counts only requests that passed something first, such as
    the validation of their body.

    Args:
        scope: What is limited.
        request: The request.

    Raises:
        RequestRefused: 429 with ``Retry-After`` beyond the limit.
    """
    wait = is_limited(scope, request)
    if wait is None:
        return
    if RequestRefused not in request.app.exception_handlers:
        # The application registers the handler; any other app gets it here.
        request.scope["starlette.exception_handlers"][0][RequestRefused] = request_refused_response
    detail = f"Too many requests. Try again in {wait} seconds."
    raise RequestRefused(429, {"detail": detail, "reason": "rate_limited"}, {"Retry-After": str(wait)})


def limit(scope: str) -> Callable[[Request], None]:
    """Return the FastAPI dependency that answers 429 beyond an address's limit.

    Args:
        scope: What is limited.
    """

    def check(request: Request) -> None:
        refuse_if_limited(scope, request)

    return check


# ------------------------------------------------------------------------------------------------
# Size bounds (section 5)
# ------------------------------------------------------------------------------------------------

API_PATHS = "/api"
MAX_BODY_BYTES = 1024 * 1024
"""The bound of every request under ``/api`` without one of its own; nginx passes as much today."""

BODY_BOUNDS: Dict[Tuple[str, str], int] = {("POST", "/api/auth/backchannel-logout"): 64 * 1024}
"""Bounds of their own, keyed by method and path."""


class _BodyTooLarge(Exception):
    """A body passed its bound."""


class BodyBoundMiddleware:
    """Refuse a request whose body is larger than its route's bound, before anything else looks at it.

    The body is read here, at most to its bound, and handed on as it came.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an application.

        Args:
            app: What handles the requests that pass.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection.

        Args:
            scope: The connection.
            receive: Where its messages come from.
            send: Where the response goes.
        """
        if scope["type"] != "http" or not route_path(scope).startswith(API_PATHS):
            await self.app(scope, receive, send)
            return
        bound = BODY_BOUNDS.get((scope["method"], route_path(scope)), MAX_BODY_BYTES)
        try:
            messages = await _read_body(scope, receive, bound)
        except _BodyTooLarge:
            body = {"detail": "The request is too large.", "reason": "request_too_large"}
            await JSONResponse(body, status_code=413, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return

        async def replay() -> Message:
            return messages.popleft() if messages else await receive()

        await self.app(scope, replay, send)


async def _read_body(scope: Scope, receive: Receive, bound: int) -> Deque[Message]:
    """Read a request's body messages up to the bound.

    Raises:
        _BodyTooLarge: When the body is declared or found to be larger.
    """
    declared = [value for name, value in scope["headers"] if name == b"content-length"]
    if any(value.isdigit() and int(value) > bound for value in declared):
        raise _BodyTooLarge
    messages: Deque[Message] = deque()
    size = 0
    while True:
        message: MutableMapping[str, Any] = await receive()
        messages.append(message)
        size += len(message.get("body", b""))
        if size > bound:
            raise _BodyTooLarge
        if message["type"] != "http.request" or not message.get("more_body", False):
            return messages
