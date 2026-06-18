"""
Keycloak JWT authentication for the annotation API.

Validates Bearer tokens against the Keycloak JWKS endpoint and exposes a
FastAPI dependency `require_user()` that returns the token's `sub` and
`groups` claims.

Set ANNOTATION_AUTH_ENABLED=false (via config) to skip validation in
development or CI environments.
"""

import logging
import time
from typing import Any

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------

_jwks_cache: dict[str, Any] = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL = 3600  # re-fetch public keys every hour


async def _get_jwks() -> dict[str, Any]:
    global _jwks_cache, _jwks_fetched_at
    if time.monotonic() - _jwks_fetched_at < _JWKS_TTL and _jwks_cache:
        return _jwks_cache
    if not settings.keycloak_jwks_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="KEYCLOAK_JWKS_URL is not configured",
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(settings.keycloak_jwks_url)
            resp.raise_for_status()
            _jwks_cache = resp.json()
            _jwks_fetched_at = time.monotonic()
            return _jwks_cache
    except Exception as exc:
        logger.error("Failed to fetch JWKS from %s: %s", settings.keycloak_jwks_url, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to reach Keycloak JWKS endpoint",
        )


# ---------------------------------------------------------------------------
# Token payload cache
#
# RSA signature verification (jose.decode) costs ~0.3 ms per call.  Caching
# decoded payloads by raw token string eliminates the cost for repeat requests
# from the same client within a short window.
#
# TTL is intentionally short (60 s) so revoked / expired tokens are not served
# from cache for more than one minute beyond their actual expiry.  JWTs issued
# by our Keycloak realm expire after 300 s, so the 60 s TTL is conservative.
# ---------------------------------------------------------------------------

_TOKEN_CACHE_TTL = 60  # seconds

# {raw_token: (payload_dict, cached_at_monotonic)}
_token_cache: dict[str, tuple[dict[str, Any], float]] = {}


def _get_cached_payload(token: str) -> dict[str, Any] | None:
    entry = _token_cache.get(token)
    if entry and (time.monotonic() - entry[1]) < _TOKEN_CACHE_TTL:
        return entry[0]
    return None


def _set_cached_payload(token: str, payload: dict[str, Any]) -> None:
    _token_cache[token] = (payload, time.monotonic())
    # Evict stale entries when the cache grows large (simple LRU-free eviction).
    if len(_token_cache) > 4096:
        cutoff = time.monotonic() - _TOKEN_CACHE_TTL
        stale = [k for k, (_, t) in _token_cache.items() if t < cutoff]
        for k in stale:
            _token_cache.pop(k, None)


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


async def require_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    """
    FastAPI dependency.  Returns ``{"sub": str, "groups": list[str]}`` from a
    valid Keycloak Bearer JWT.

    When ``ANNOTATION_AUTH_ENABLED=false``, skips validation and returns a
    synthetic dev identity so the API works without a real Keycloak setup.

    Decoded payloads are cached for ``_TOKEN_CACHE_TTL`` seconds to avoid
    repeated RSA signature verification on every request from the same client.
    """
    if not settings.annotation_auth_enabled:
        return {"sub": "dev-user", "groups": []}

    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = creds.credentials

    # Fast path: return cached payload if still fresh.
    cached = _get_cached_payload(token)
    if cached is not None:
        return cached

    try:
        jwks = await _get_jwks()
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    sub: str = payload.get("sub", "")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'sub' claim",
        )

    result = {"sub": sub, "groups": payload.get("groups", [])}
    _set_cached_payload(token, result)
    return result
