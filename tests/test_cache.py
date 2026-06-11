"""Tests for the Redis cache layer (app/cache.py)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

import app.cache as cache_module


# ---------------------------------------------------------------------------
# No-Redis mode — all operations must be safe no-ops
# ---------------------------------------------------------------------------

class TestNoRedisMode:
    async def test_get_tile_returns_none(self):
        with patch.object(cache_module, "_redis", None):
            assert await cache_module.get_tile("123", 1, 0, 0) is None

    async def test_set_tile_does_not_raise(self):
        with patch.object(cache_module, "_redis", None):
            await cache_module.set_tile("123", 1, 0, 0, b"data")

    async def test_get_thumbnail_returns_none(self):
        with patch.object(cache_module, "_redis", None):
            assert await cache_module.get_thumbnail("123", 256, 256) is None

    async def test_get_patient_returns_none(self):
        with patch.object(cache_module, "_redis", None):
            assert await cache_module.get_patient("P-0001") is None

    async def test_get_metadata_returns_none(self):
        with patch.object(cache_module, "_redis", None):
            assert await cache_module.get_metadata("123") is None

    async def test_get_raw_returns_none(self):
        with patch.object(cache_module, "_redis", None):
            assert await cache_module.get_raw("search:abc") is None


# ---------------------------------------------------------------------------
# Redis key formatting
# ---------------------------------------------------------------------------

def _make_redis():
    r = AsyncMock()
    r.get    = AsyncMock(return_value=None)
    r.set    = AsyncMock()
    r.setex  = AsyncMock()
    return r


class TestKeyFormats:
    async def test_tile_key(self):
        r = _make_redis()
        with patch.object(cache_module, "_redis", r):
            await cache_module.get_tile("abc", 5, 10, 20)
        r.get.assert_called_once_with("tile:abc:5:10:20")

    async def test_thumbnail_key(self):
        r = _make_redis()
        with patch.object(cache_module, "_redis", r):
            await cache_module.get_thumbnail("abc", 256, 128)
        r.get.assert_called_once_with("thumbnail:abc:256:128")

    async def test_metadata_key(self):
        r = _make_redis()
        with patch.object(cache_module, "_redis", r):
            await cache_module.get_metadata("1492807")
        r.get.assert_called_once_with("meta:1492807")

    async def test_patient_key(self):
        r = _make_redis()
        mock_settings = AsyncMock()
        mock_settings.patient_cache_ttl = 86400
        with (
            patch.object(cache_module, "_redis", r),
            patch("app.cache.settings", mock_settings),
        ):
            await cache_module.get_patient("P-0001")
        r.get.assert_called_once_with("patient:P-0001")


# ---------------------------------------------------------------------------
# TTL behaviour
# ---------------------------------------------------------------------------

class TestTtlBehaviour:
    async def test_metadata_stored_without_ttl(self):
        """Slide metadata is immutable — must use SET, not SETEX."""
        r = _make_redis()
        with patch.object(cache_module, "_redis", r):
            await cache_module.set_metadata("123", {"key": "value"})
        r.set.assert_called_once()
        r.setex.assert_not_called()

    async def test_raw_cache_uses_setex(self):
        """Generic raw cache (search results) must expire."""
        r = _make_redis()
        with patch.object(cache_module, "_redis", r):
            await cache_module.set_raw("search:foo", [{"id": "1"}], ttl=300)
        r.setex.assert_called_once()
        args = r.setex.call_args[0]
        assert args[0] == "search:foo"
        assert args[1] == 300          # TTL in seconds


# ---------------------------------------------------------------------------
# Roundtrip — JSON encode/decode
# ---------------------------------------------------------------------------

class TestRoundtrip:
    async def test_metadata_roundtrip(self):
        data = {"dimensions": {"width": 1000, "height": 800}, "vendor": "aperio"}
        r = _make_redis()
        r.get = AsyncMock(return_value=json.dumps(data).encode())
        with patch.object(cache_module, "_redis", r):
            result = await cache_module.get_metadata("123")
        assert result == data

    async def test_raw_roundtrip(self):
        data = [{"type": "patient", "id": "P-0001"}]
        r = _make_redis()
        r.get = AsyncMock(return_value=json.dumps(data).encode())
        with patch.object(cache_module, "_redis", r):
            result = await cache_module.get_raw("search:p-0")
        assert result == data
