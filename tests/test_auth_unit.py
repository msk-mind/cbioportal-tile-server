from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import JWTError

import app.auth as auth


class TestAuthHelpers:
    def test_dev_user_shape(self):
        assert auth._dev_user() == {
            "sub": "dev-user",
            "groups": [],
            "preferred_username": "dev-user",
            "name": "Dev User",
        }

    def test_normalize_user_prefers_name_and_username(self):
        payload = {
            "sub": "u1",
            "groups": ["g1"],
            "preferred_username": "editor",
            "name": "Editor Name",
        }
        assert auth._normalize_user(payload) == {
            "sub": "u1",
            "groups": ["g1"],
            "preferred_username": "editor",
            "name": "Editor Name",
        }

    def test_normalize_user_falls_back_to_email(self):
        payload = {"sub": "u1", "email": "u1@example.org"}
        assert auth._normalize_user(payload)["preferred_username"] == "u1@example.org"

    def test_normalize_user_requires_sub(self):
        with pytest.raises(HTTPException) as exc:
            auth._normalize_user({})
        assert exc.value.status_code == 401

    def test_jwks_cache_fresh_checks_ttl_and_payload(self):
        with (
            patch("app.auth._jwks_cache", {"keys": []}),
            patch("app.auth._jwks_fetched_at", 100.0),
            patch("app.auth.time.monotonic", return_value=200.0),
        ):
            assert auth._jwks_cache_fresh() is True

    def test_prune_token_cache_removes_stale_entries(self):
        with patch("app.auth.time.monotonic", return_value=1000.0):
            auth._token_cache.clear()
            auth._token_cache["fresh"] = ({"sub": "a"}, 950.0)
            auth._token_cache["stale"] = ({"sub": "b"}, 800.0)
            auth._prune_token_cache()
        assert "fresh" in auth._token_cache
        assert "stale" not in auth._token_cache
        auth._token_cache.clear()


class TestRequireUser:
    @pytest.mark.asyncio
    async def test_returns_dev_user_when_auth_disabled(self):
        with patch("app.auth.settings.annotation_auth_enabled", False):
            result = await auth.require_user()
        assert result["sub"] == "dev-user"

    @pytest.mark.asyncio
    async def test_missing_creds_raises_401(self):
        with patch("app.auth.settings.annotation_auth_enabled", True):
            with pytest.raises(HTTPException) as exc:
                await auth.require_user(None)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_uses_cached_payload(self):
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token-1")
        with (
            patch("app.auth.settings.annotation_auth_enabled", True),
            patch("app.auth._get_cached_payload", return_value={"sub": "cached", "groups": []}),
        ):
            result = await auth.require_user(creds)
        assert result["sub"] == "cached"

    @pytest.mark.asyncio
    async def test_invalid_token_maps_to_401(self):
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token-1")
        with (
            patch("app.auth.settings.annotation_auth_enabled", True),
            patch("app.auth._get_cached_payload", return_value=None),
            patch("app.auth._get_jwks", new=AsyncMock(return_value={"keys": []})),
            patch("app.auth.jwt.decode", side_effect=JWTError("bad token")),
        ):
            with pytest.raises(HTTPException) as exc:
                await auth.require_user(creds)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_decoded_payload_is_normalized_and_cached(self):
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token-1")
        with (
            patch("app.auth.settings.annotation_auth_enabled", True),
            patch("app.auth._get_cached_payload", return_value=None),
            patch("app.auth._get_jwks", new=AsyncMock(return_value={"keys": []})),
            patch("app.auth.jwt.decode", return_value={"sub": "u1", "preferred_username": "editor"}),
            patch("app.auth._set_cached_payload") as mock_set,
        ):
            result = await auth.require_user(creds)
        assert result["sub"] == "u1"
        assert result["preferred_username"] == "editor"
        mock_set.assert_called_once()
