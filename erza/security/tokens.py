"""Constant-time secret comparison helpers.

``hmac.compare_digest`` only accepts ``bytes`` or ASCII-only ``str``: a
non-ASCII ``str`` (which a percent-decoded query token or an unusual header
value can easily be) raises ``TypeError``.  Callers used to pass user-supplied
strings straight in, so a crafted credential containing a non-ASCII character
turned an auth *rejection* into an uncaught exception — for the WebSocket
handshake that aborts the handshake instead of returning 401.

``constant_time_equals`` encodes both sides to UTF-8 first, so every input is
comparable and the timing property of ``compare_digest`` is preserved.
"""

from __future__ import annotations

import hmac

__all__ = ["constant_time_equals"]


def _to_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return str(value).encode("utf-8")


def constant_time_equals(candidate: object, expected: object) -> bool:
    """Return True when *candidate* equals *expected*, in constant time.

    Accepts ``str`` (any Unicode) or ``bytes`` on both sides.
    """
    return hmac.compare_digest(_to_bytes(candidate), _to_bytes(expected))
