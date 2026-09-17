"""
API key auth. See docs/adr/0007-observability.md Decision 4 for what this
is (one static shared secret) and what it deliberately is not (per-caller
identity, scoping, rotation without downtime).
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import settings


def verify_api_key(provided: str, expected: str) -> bool:
    """
    Pure comparison, split out so the constant-time property is unit
    testable without going through FastAPI's Header parsing. Plain `==`
    short-circuits on the first differing byte, which leaks how many
    leading characters a guess got right through response timing — the
    textbook argument for `hmac.compare_digest` on anything secret.
    """
    return hmac.compare_digest(provided, expected)


async def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    """
    The header is OPTIONAL at the FastAPI level on purpose: `Header(...)`
    (required) makes FastAPI's own request-validation layer reject a missing
    header with 422 before this function ever runs — a different status code
    for "no key" than the 401 a wrong key gets here, from the same cause
    (not authenticated). A caller shouldn't see two different codes for two
    shades of the same rejection, so the header is optional and both cases
    are turned into the same 401 explicitly.
    """
    if x_api_key is None or not verify_api_key(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
