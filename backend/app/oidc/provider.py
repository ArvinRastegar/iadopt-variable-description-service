"""The identity provider: its metadata, its keys and the code exchange (oidc-identity, sections 4, 6 and 7).

Every request to the provider goes through :func:`send`: TLS verified, no redirect followed, at most
10 seconds and 1 MiB. One read of the metadata serves everyone who waits for it, a failed read is
remembered for 15 seconds, and at most 16 requests wait for the provider at a time, so a provider that
hangs never holds up the other routes. Failures carry a fixed word and nothing the provider sent.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Type

import requests
from authlib.integrations.requests_client import OAuth2Session
from joserfc.errors import JoseError
from joserfc.jwk import JWKRegistry, Key, RSAKey
from joserfc.jws import JWSRegistry
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from .settings import SignInSettings, is_allowed_address

REQUEST_SECONDS = 10.0
"""When a request to the provider stops."""

MAX_ANSWER_BYTES = 1024 * 1024
METADATA_SECONDS = 3600.0
"""How long good metadata, and the key set with it, is kept."""

FAILURE_SECONDS = 15.0
"""How long a failed read is remembered; sign-ins end at once meanwhile."""

KEY_REREAD_SECONDS = 60.0
"""How often an unknown key id may read the key set once more."""

MAX_WAITING = 16
"""Most requests that wait for the provider at a time."""

SIGNING_ALGORITHMS = ("RS256", "PS256", "ES256")
"""The signatures that are accepted, where the provider lists them; never ``none`` or a shared key."""

MIN_RSA_BITS = 2048
"""The smallest RSA key these signatures may be made with (RFC 7518)."""

clock: Callable[[], float] = time.monotonic
"""The clock of everything that is kept here."""


class ProviderError(Exception):
    """What went wrong with the provider, as a fixed word for the log."""

    def __init__(self, reason: str) -> None:
        """Name the reason.

        Args:
            reason: A fixed word.
        """
        super().__init__(reason)
        self.reason = reason


class ProviderUnavailable(ProviderError):
    """The provider cannot be used right now."""


class ProviderRefused(ProviderError):
    """The provider refused the code."""


# ------------------------------------------------------------------------------------------------
# One request
# ------------------------------------------------------------------------------------------------


class Turn:
    """One request's place among those that wait for the provider.

    The place is taken at the request's first wait and given back at the end of the ``with`` block, or
    when the last exchange with the provider that it left behind has ended, whichever is later.
    """

    def __init__(self, places: threading.BoundedSemaphore) -> None:
        """Begin without a place.

        Args:
            places: The places.
        """
        self._places = places
        self._lock = threading.Lock()
        self._holders = 0
        self._waiting = False

    def __enter__(self) -> "Turn":
        """Return the turn itself."""
        return self

    def __exit__(
        self, kind: Optional[Type[BaseException]], error: Optional[BaseException], trace: Optional[TracebackType]
    ) -> None:
        """Give the place back, if one was taken.

        Args:
            kind: The exception's class, if the block raised.
            error: The exception, if the block raised.
            trace: Its traceback.
        """
        if self._waiting:
            self.give()

    def wait(self) -> None:
        """Take the place before the request's first wait; ``ProviderUnavailable`` when none is free."""
        if not self._waiting:
            self.take()
            self._waiting = True

    def take(self) -> None:
        """Hold the place once more.

        Raises:
            ProviderUnavailable: When none is free.
        """
        with self._lock:
            if self._holders == 0 and not self._places.acquire(blocking=False):
                raise ProviderUnavailable("too_many_waiting")
            self._holders += 1

    def give(self) -> None:
        """Let the place go once."""
        with self._lock:
            self._holders -= 1
            if self._holders == 0:
                self._places.release()


@dataclass(frozen=True)
class Answer:
    """What the provider answered, read to its end."""

    status_code: int
    content: bytes
    location: Optional[str] = None

    def json(self) -> Any:
        """Parse the body; ``ValueError`` when it is no JSON."""
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        """Do what Authlib expects of a response; the status is judged before it sees the answer.

        Raises:
            ProviderUnavailable: When Authlib could not use the body.
        """
        raise ProviderUnavailable("no_json")


class _Sockets:
    """The sockets of one request, kept so that it can be ended from outside, whatever it waits for."""

    def __init__(self) -> None:
        """Begin with none."""
        self._lock = threading.Lock()
        self._open: List[socket.socket] = []
        self._ended = False

    def add(self, connected: socket.socket) -> socket.socket:
        """Keep a second handle of a socket, which stays valid when TLS takes the first one over.

        Args:
            connected: The socket.

        Returns:
            The socket.
        """
        with self._lock:
            self._open.append(connected.dup())
            ended = self._ended
        if ended:
            self.end()
        return connected

    def end(self) -> None:
        """Stop every exchange: whoever reads or writes gets an error or the end."""
        with self._lock:
            self._ended = True
            for handle in self._open:
                try:
                    handle.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # it ended by itself

    def close(self) -> None:
        """Let the handles go."""
        with self._lock:
            for handle in self._open:
                handle.close()
            self._open.clear()


class _WatchedAdapter(HTTPAdapter):
    """Sends a request over connections whose sockets are kept."""

    def __init__(self, sockets: _Sockets) -> None:
        """Remember where the sockets are kept.

        Args:
            sockets: The request's sockets.
        """
        self._sockets = sockets
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Make the pools with connections that hand over their sockets.

        Args:
            *args: As the library passes them.
            **kwargs: As the library passes them.
        """
        super().init_poolmanager(*args, **kwargs)
        sockets = self._sockets

        class Plain(HTTPConnection):
            def _new_conn(self) -> socket.socket:
                return sockets.add(super()._new_conn())

        class Secure(HTTPSConnection):
            def _new_conn(self) -> socket.socket:
                return sockets.add(super()._new_conn())

        class PlainPool(HTTPConnectionPool):
            ConnectionCls = Plain

        class SecurePool(HTTPSConnectionPool):
            ConnectionCls = Secure

        self.poolmanager.pool_classes_by_scheme = {"http": PlainPool, "https": SecurePool}


def send(turn: Turn, method: str, url: str, **kwargs: Any) -> Answer:
    """Send one request to the provider and read its answer; the one function that sends any.

    The request runs in a thread of its own, since no timeout of the HTTP library bounds a whole
    exchange. One that is still running at the end is ended by closing its connection, and keeps its
    place until it has.

    Args:
        turn: The waiting request's turn.
        method: The HTTP method.
        url: The address, which the caller has judged.
        **kwargs: ``data``, ``headers`` and ``auth`` for ``requests``.

    Returns:
        The answer, whatever its status.

    Raises:
        ProviderUnavailable: When there is no answer in time, or it is too large.
    """
    turn.wait()
    outcome: List[Any] = []
    sockets = _Sockets()

    def work() -> None:
        try:
            outcome.append(_exchange(sockets, method, url, time.monotonic() + REQUEST_SECONDS, **kwargs))
        except ProviderUnavailable as e:
            outcome.append(e)
        except requests.exceptions.SSLError:
            outcome.append(ProviderUnavailable("certificate"))
        except requests.exceptions.Timeout:
            outcome.append(ProviderUnavailable("timeout"))
        except Exception:  # whatever a hostile answer makes the HTTP library raise
            outcome.append(ProviderUnavailable("no_answer"))
        finally:
            sockets.close()
            turn.give()

    turn.take()
    thread = threading.Thread(target=work, name="oidc-provider", daemon=True)
    try:
        thread.start()
    except RuntimeError:  # the machine has no thread left
        turn.give()
        raise ProviderUnavailable("no_thread") from None
    thread.join(REQUEST_SECONDS)
    if not outcome:
        sockets.end()
        raise ProviderUnavailable("timeout")
    if isinstance(outcome[0], ProviderUnavailable):
        raise outcome[0]
    answer: Answer = outcome[0]
    return answer


def _no_credentials(request: requests.PreparedRequest) -> requests.PreparedRequest:
    """Authenticate as nobody, also where a ``.netrc`` would name someone."""
    return request


def _exchange(sockets: _Sockets, method: str, url: str, deadline: float, **kwargs: Any) -> Answer:
    """Send a request and read its answer up to the bounds.

    Raises:
        ProviderUnavailable: When the answer is too large or takes too long.
    """
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", **(kwargs.pop("headers", None) or {})}
    auth = kwargs.pop("auth", None) or _no_credentials
    with requests.Session() as session:
        for scheme in ("http://", "https://"):
            session.mount(scheme, _WatchedAdapter(sockets))
        with session.request(
            method, url, headers=headers, auth=auth, allow_redirects=False, stream=True, timeout=REQUEST_SECONDS, **kwargs
        ) as response:
            content = bytearray()
            while chunk := response.raw.read1(65_536, decode_content=True):
                content += chunk
                if len(content) > MAX_ANSWER_BYTES:
                    raise ProviderUnavailable("too_large")
                if time.monotonic() > deadline:
                    raise ProviderUnavailable("timeout")
            return Answer(response.status_code, bytes(content), response.headers.get("Location"))


class _TokenClient(OAuth2Session):
    """Authlib's client of a token endpoint, without a secret, sending through :func:`send`."""

    def __init__(self, turn: Turn, client_id: str) -> None:
        """Configure a public client.

        Args:
            turn: The waiting request's turn.
            client_id: The client ID.
        """
        super().__init__(client_id=client_id, token_endpoint_auth_method="none")
        self._turn = turn

    def request(self, method: str, url: str, withhold_token: bool = False, auth: Any = None, **kwargs: Any) -> Answer:
        """Send the token request once and judge its status.

        Args:
            method: The HTTP method.
            url: The token endpoint.
            withhold_token: Authlib's; no token is ever sent.
            auth: Authlib's client authentication, which adds the client ID.
            **kwargs: ``data`` and ``headers``.

        Returns:
            The answer, when its status is 200.

        Raises:
            ProviderRefused: When the provider answers 4xx.
            ProviderUnavailable: When it answers anything else but 200.
        """
        answer = send(self._turn, method, url, data=kwargs.get("data"), headers=kwargs.get("headers"), auth=auth)
        if 400 <= answer.status_code < 500:
            raise ProviderRefused("code_refused")
        if answer.status_code != 200:
            raise ProviderUnavailable("token_status")
        return answer


# ------------------------------------------------------------------------------------------------
# Metadata and keys
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Metadata:
    """The provider's metadata as far as it is used.

    Attributes:
        authorization_endpoint: Where the browser is sent.
        token_endpoint: Where the code is exchanged.
        jwks_uri: Where the key set is.
        algorithms: The accepted signatures the provider lists.
        end_session_endpoint: Where the browser signs out, or ``None``.
        sends_issuer: Whether a return always names the issuer (RFC 9207).
        backchannel_logout: Whether the provider offers back-channel sign-out.
    """

    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: Tuple[str, ...]
    end_session_endpoint: Optional[str]
    sends_issuer: bool
    backchannel_logout: bool


def metadata_url(issuer: str) -> str:
    """Return where an issuer's metadata is.

    Args:
        issuer: The issuer URL.
    """
    return f"{issuer.removesuffix('/')}/.well-known/openid-configuration"


ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri")
"""The endpoints that must be there and allowed."""


def metadata_problems(document: Mapping[str, Any], settings: SignInSettings) -> Dict[str, Optional[str]]:
    """Judge the provider's metadata: one fixed word per check, ``None`` where it passes.

    Args:
        document: The metadata's JSON object.
        settings: The judged settings.
    """
    bad = [name for name in ENDPOINTS if not is_allowed_address(document.get(name), development=settings.development)]
    return {
        "issuer": None if document.get("issuer") == settings.issuer else "other_issuer",
        "endpoints": f"endpoint_not_allowed ({', '.join(bad)})" if bad else None,
        "s256": None if "S256" in _texts(document.get("code_challenge_methods_supported")) else "no_s256",
        "algorithms": None if _algorithms(document) else "no_algorithm",
    }


def _texts(value: object) -> List[str]:
    """The texts of a JSON list; nothing for anything else."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _algorithms(document: Mapping[str, Any]) -> Tuple[str, ...]:
    """The accepted signatures the metadata lists."""
    listed = _texts(document.get("id_token_signing_alg_values_supported"))
    return tuple(name for name in SIGNING_ALGORITHMS if name in listed)


def read_metadata_document(turn: Turn, settings: SignInSettings) -> Dict[str, Any]:
    """Read the metadata's JSON object; ``ProviderUnavailable`` when there is none.

    Args:
        turn: The waiting request's turn.
        settings: The judged settings.
    """
    return _json_object(send(turn, "GET", metadata_url(settings.issuer)))


def accept_metadata(document: Mapping[str, Any], settings: SignInSettings) -> Metadata:
    """Return the metadata when every check passes.

    Args:
        document: The metadata's JSON object.
        settings: The judged settings.

    Raises:
        ProviderUnavailable: When a check does not; the reason names the first.
    """
    for problem in metadata_problems(document, settings).values():
        if problem:
            raise ProviderUnavailable(problem)
    end_session = document.get("end_session_endpoint")
    return Metadata(
        authorization_endpoint=document["authorization_endpoint"],
        token_endpoint=document["token_endpoint"],
        jwks_uri=document["jwks_uri"],
        algorithms=_algorithms(document),
        end_session_endpoint=end_session if is_allowed_address(end_session, development=settings.development) else None,
        sends_issuer=document.get("authorization_response_iss_parameter_supported") is True,
        backchannel_logout=document.get("backchannel_logout_supported") is True,
    )


def _json_object(answer: Answer) -> Dict[str, Any]:
    """Return a 200 answer's JSON object.

    Raises:
        ProviderUnavailable: When it is none.
    """
    if answer.status_code != 200:
        raise ProviderUnavailable("status")
    try:
        value = answer.json()
    except (ValueError, RecursionError):
        raise ProviderUnavailable("no_json") from None
    if not isinstance(value, dict):
        raise ProviderUnavailable("no_json")
    return value


def read_keys(turn: Turn, jwks_uri: str) -> List[Key]:
    """Read the provider's key set; a key the library cannot use is left out.

    Args:
        turn: The waiting request's turn.
        jwks_uri: Where the key set is.

    Raises:
        ProviderUnavailable: When there is no key set.
    """
    document = _json_object(send(turn, "GET", jwks_uri))
    if not isinstance(document.get("keys"), list):
        raise ProviderUnavailable("no_key_set")
    keys = []
    for value in document["keys"]:
        try:
            keys.append(JWKRegistry.import_key(value))
        except (JoseError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return keys


def fits(key: Key, algorithm: str) -> bool:
    """Return whether a key is for checking signatures of an algorithm.

    Args:
        key: A key of the provider.
        algorithm: One of :data:`SIGNING_ALGORITHMS`.
    """
    operations = key.get("key_ops")
    if key.get("use") not in (None, "sig") or key.get("alg") not in (None, algorithm):
        return False
    if operations is not None and "verify" not in operations:
        return False
    if isinstance(key, RSAKey) and key.raw_value.key_size < MIN_RSA_BITS:
        return False
    try:
        JWSRegistry.algorithms[algorithm].check_key(key)
    except JoseError:
        return False
    return True


class Provider:
    """What this process knows of the configured provider."""

    def __init__(self) -> None:
        """Begin knowing nothing."""
        self._places = threading.BoundedSemaphore(MAX_WAITING)
        self._state = threading.Lock()
        self._reading = threading.Lock()
        self._about: Optional[Tuple[str, bool]] = None
        self._forget()

    def _forget(self) -> None:
        """Drop everything, as when the issuer changes."""
        self._metadata: Optional[Metadata] = None
        self._metadata_at = 0.0
        self._metadata_failure: Optional[Tuple[float, str]] = None
        self._forget_keys()

    def _forget_keys(self) -> None:
        """Drop the key set."""
        self._keys: Optional[List[Key]] = None
        self._keys_failure: Optional[Tuple[float, str]] = None
        self._reread_at: Optional[float] = None

    def reset(self) -> None:
        """Forget everything and free every place, as at a restart."""
        with self._state:
            self._places = threading.BoundedSemaphore(MAX_WAITING)
            self._about = None
            self._forget()

    def turn(self) -> Turn:
        """Return a request's turn to wait for the provider."""
        return Turn(self._places)

    def metadata(self, settings: SignInSettings, turn: Turn) -> Metadata:
        """Return the provider's metadata, reading it when it is not known.

        Args:
            settings: The judged settings.
            turn: The waiting request's turn.

        Raises:
            ProviderUnavailable: When it cannot be used.
        """
        known = self._known_metadata(settings)
        if known:
            return known
        turn.wait()
        with self._reading:  # whoever waited here uses what the first one read
            known = self._known_metadata(settings)
            if known:
                return known
            try:
                metadata = accept_metadata(read_metadata_document(turn, settings), settings)
            except ProviderUnavailable as e:
                with self._state:
                    self._metadata_failure = (clock(), e.reason)
                raise
            with self._state:
                self._forget_keys()
                self._metadata, self._metadata_at, self._metadata_failure = metadata, clock(), None
            return metadata

    def _known_metadata(self, settings: SignInSettings) -> Optional[Metadata]:
        """Return the metadata while it is kept; a failed read that is remembered ends the request."""
        with self._state:
            about = (settings.issuer, settings.development)
            if self._about != about:
                self._about = about
                self._forget()
            if self._metadata and clock() - self._metadata_at < METADATA_SECONDS:
                return self._metadata
            _raise_remembered(self._metadata_failure)
            return None

    def find_key(self, metadata: Metadata, turn: Turn, key_id: Optional[str], algorithm: str) -> Optional[Key]:
        """Return the provider's key for a signature, or ``None`` when there is none.

        An unknown key id reads the key set once more, at most once a minute. Without a key id the
        set must hold exactly one key that fits. ``ProviderUnavailable`` when the key set cannot be read.

        Args:
            metadata: The provider's metadata.
            turn: The waiting request's turn.
            key_id: The token's ``kid``, or ``None``.
            algorithm: The token's algorithm.
        """
        keys = self._key_set(metadata, turn)
        if key_id is not None and not any(key.kid == key_id for key in keys) and self._may_reread():
            keys = self._key_set(metadata, turn, again=True)
        fitting = [key for key in keys if (key_id is None or key.kid == key_id) and fits(key, algorithm)]
        return fitting[0] if len(fitting) == 1 else None

    def _may_reread(self) -> bool:
        """Return whether the key set may be read once more now, and count it."""
        with self._state:
            if self._reread_at is not None and clock() - self._reread_at < KEY_REREAD_SECONDS:
                return False
            self._reread_at = clock()
            return True

    def _key_set(self, metadata: Metadata, turn: Turn, *, again: bool = False) -> List[Key]:
        """Return the key set, reading it when it is not known or ``again``.

        Raises:
            ProviderUnavailable: When it cannot be read.
        """
        with self._state:
            if self._keys is not None and not again:
                return self._keys
            if self._keys is None:
                _raise_remembered(self._keys_failure)
        turn.wait()
        with self._reading:
            with self._state:
                if self._keys is not None and not again:
                    return self._keys
                if self._keys is None:
                    _raise_remembered(self._keys_failure)
            try:
                keys = read_keys(turn, metadata.jwks_uri)
            except ProviderUnavailable as e:
                with self._state:
                    self._keys_failure = (clock(), e.reason)
                raise
            with self._state:
                self._keys, self._keys_failure = keys, None
            return keys

    def exchange(self, settings: SignInSettings, metadata: Metadata, turn: Turn, *, code: str, code_verifier: str) -> str:
        """Trade a code for the ID token; the provider's other tokens are dropped here.

        Args:
            settings: The judged settings.
            metadata: The provider's metadata.
            turn: The waiting request's turn.
            code: The code the browser brought.
            code_verifier: The verifier of the sign-in.

        Raises:
            ProviderError: ``ProviderRefused`` when the provider refuses the code.
            ProviderUnavailable: When it does not answer with an ID token.
        """
        client = _TokenClient(turn, settings.client_id)
        try:
            answer = client.fetch_token(
                metadata.token_endpoint,
                grant_type="authorization_code",
                code=code,
                redirect_uri=settings.callback_url,
                code_verifier=code_verifier,
            )
            id_token = answer.get("id_token")
        except ProviderError:
            raise
        except Exception:  # an answer Authlib cannot use; its text is not kept
            raise ProviderUnavailable("token_answer") from None
        if not isinstance(id_token, str) or not id_token:
            raise ProviderUnavailable("no_id_token")
        return id_token


def _raise_remembered(failure: Optional[Tuple[float, str]]) -> None:
    """End at once while a failed read is remembered.

    Raises:
        ProviderUnavailable: With the reason of the failed read.
    """
    if failure and clock() - failure[0] < FAILURE_SECONDS:
        raise ProviderUnavailable(failure[1])


provider = Provider()
"""The process's knowledge of the configured provider."""
