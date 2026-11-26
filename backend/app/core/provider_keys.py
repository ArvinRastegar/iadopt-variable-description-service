"""The model providers' keys: refused when no request can carry them, and kept out of every text.

A key leaves the service in one place, the ``Authorization`` header of a request to its provider. One with a
line break in it, as a hand-made secret often has at its end, cannot stand there, and the HTTP libraries refuse
it with an error that quotes the whole header. A provider that refuses a key may quote it too. Hence two lines
of defence: :func:`usable_key` refuses a key that cannot be sent before any request, as a missing one is
refused, and :func:`without_keys` takes the keys, and any other ``Bearer`` value, out of a text before it is
shown, stored or logged.

Depends on ``core.config`` alone.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Iterator, Optional

from .config import settings

REDACTED = "[redacted]"
"""What stands in a text in place of a key."""

SENDABLE = re.compile(r"[\t -~]+")
"""A key that a request header can carry: visible ASCII characters, blanks and tabs."""

SENDABLE_BY_SDK = re.compile(r"[\t -~]*[!-~]")
"""The same for OpenRouter, whose HTTP client refuses a header that ends in a blank or a tab."""

_SHORTEST = 4
"""A shorter key is no secret, and taking it out everywhere would spoil ordinary texts."""

_WORD_LENGTH = 8
"""What is shorter than this could be part of an ordinary word, so it is only taken out where it stands alone."""

_LONG_PART = re.compile(rf"[!-~]{{{_WORD_LENGTH},}}")
"""A part of a key that gives it away without the rest."""

_PLAIN_PART = re.compile(rf"[A-Za-z0-9_.-]{{{_WORD_LENGTH},}}")
"""The same without quotes and backslashes, which a text may write in another way, or a provider leave out."""

# Every quantifier is bounded or matches in one pass: a text may hold what a person typed.
_BEARER = re.compile(
    r"(?P<scheme>(?P<quote>\\{0,4}[\"'`])?Bearer)(?:\s|%20)+(?(quote)(?:(?!(?P=quote)).)+|[^\s\"'`]+)",
    re.IGNORECASE | re.DOTALL,
)
"""A ``Bearer`` value: up to the closing quote when the header is quoted, otherwise up to the next white space."""


def usable_key(setting: str, key: Optional[str], sendable: re.Pattern[str] = SENDABLE) -> str:
    """Return a provider's key, or refuse one that is missing or that no request can carry.

    White space around a key is not trimmed. A line break cannot be sent and is refused; a blank is sent as it
    is, as far as the provider's HTTP client sends one there.

    Args:
        setting: The name of the key's setting, for the message.
        key: The value of that setting.
        sendable: What the provider's HTTP client can send.

    Returns:
        The key, unchanged.

    Raises:
        RuntimeError: If the key is missing, or holds a character that cannot be sent. The message names the
            setting and holds no part of the key.
    """
    if not key:
        raise RuntimeError(f"{setting} is not set.")
    if sendable.fullmatch(key) is None:
        raise RuntimeError(
            f"{setting} holds a character that cannot be sent in a request header, such as a line break at its end."
        )
    return key


def without_keys(text: object) -> str:
    """Take the providers' keys and every ``Bearer`` value out of a text.

    Take them out before a text is cut to a length: of a key that is cut in two, the first part would stay.

    Args:
        text: The text, or an exception, whose message it is.

    Returns:
        The text, with :data:`REDACTED` in their place.
    """
    cleaned = str(text)
    keys = _key_pattern((settings.psnc_api_key or "", settings.openrouter_api_key or ""))
    if keys is not None:
        cleaned = keys.sub(REDACTED, cleaned)
    return _BEARER.sub(rf"\g<scheme> {REDACTED}", cleaned)


@lru_cache(maxsize=8)
def _key_pattern(keys: tuple[str, ...]) -> Optional[re.Pattern[str]]:
    """Return what finds the keys in a text, however it writes them; ``None`` when there is nothing to find.

    Args:
        keys: The values of the settings, empty where one is not set.
    """
    written = {form for key in keys if len(key.strip()) >= _SHORTEST for form in _ways_to_write(key)}
    alternatives = [
        re.escape(form) if len(form) >= _WORD_LENGTH else rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])"
        for form in sorted(written, key=len, reverse=True)  # the longest first, so that no rest of it stays
    ]
    return re.compile("|".join(alternatives)) if alternatives else None


def _ways_to_write(key: str) -> Iterator[str]:
    """Yield a key as error texts write it: as it is, on one line, escaped as Python or JSON does, and its parts.

    Args:
        key: The key.
    """
    for text in (key, key.strip()):
        yield text
        yield " ".join(text.split())
        yield repr(text)[1:-1]
        yield ascii(text)[1:-1]
        yield json.dumps(text)[1:-1]
        for encoding in ("utf-8", "latin-1"):
            yield repr(text.encode(encoding, "backslashreplace"))[2:-1]
    yield from _LONG_PART.findall(key)
    yield from _PLAIN_PART.findall(key)
