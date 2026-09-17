"""
require_api_key as a plain function call, bypassing FastAPI's request
validation layer entirely — which is exactly the point: this guards against
a regression where `Header(...)` (required) makes FastAPI reject a missing
header with its own 422 before require_api_key ever runs, instead of the 401
every other rejection here returns. Found via the integration suite: a
capture() call built without headers on purpose (concurrency test) got 422,
not the 401 the rest of the auth story promises. See ADR 0007 Decision 4.
"""

import pytest
from fastapi import HTTPException

from app.api.auth import require_api_key
from app.core.config import settings


async def test_correct_key_is_accepted():
    await require_api_key(x_api_key=settings.api_key)  # no raise


async def test_wrong_key_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(x_api_key="wrong-key")
    assert exc_info.value.status_code == 401


async def test_missing_key_raises_401_not_422():
    """
    The regression this file exists to catch: a missing header must fail the
    same way a wrong one does, not fall through to FastAPI's own validation
    error for an unset required Header().
    """
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(x_api_key=None)
    assert exc_info.value.status_code == 401
