"""Tests for route handlers in app/main.py."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from PIL import Image

import app.cache as cache_module
import app.main as main_module
import app.meta as meta_module
from tests.conftest import make_mock_slide


def _run(coro):
    return asyncio.run(coro)


def _json_body(response):
    return json.loads(response.body)


@pytest.fixture
def route_env():
    mock_slide = make_mock_slide()
    mock_slide.get_thumbnail = MagicMock(
        return_value=Image.new("RGBA", (256, 256), (200, 200, 200, 255))
    )

    async def _noop_get(*a, **k):
        return None

    async def _noop_set(*a, **k):
        pass

    async def _call_direct(fn, *args):
        return fn(*args)

    patches = [
        patch.object(cache_module, "get_tile", _noop_get),
        patch.object(cache_module, "set_tile", _noop_set),
        patch.object(cache_module, "get_thumbnail", _noop_get),
        patch.object(cache_module, "set_thumbnail", _noop_set),
        patch.object(cache_module, "get_metadata", _noop_get),
        patch.object(cache_module, "set_metadata", _noop_set),
        patch.object(cache_module, "get_patient", _noop_get),
        patch.object(cache_module, "set_patient", _noop_set),
        patch.object(cache_module, "get_raw", _noop_get),
        patch.object(cache_module, "set_raw", _noop_set),
        patch.object(main_module, "_in_thread", _call_direct),
        patch.object(
            meta_module,
            "get_slide_path",
            lambda image_id, warehouse_id: f"s3://test-bucket/{image_id}.svs",
        ),
    ]
    for p in patches:
        p.start()

    main_module._slides = MagicMock()
    main_module._slides.get = MagicMock(return_value=mock_slide)
    main_module._slides.close_all = MagicMock()
    main_module._path_cache.clear()

    yield mock_slide

    main_module._path_cache.clear()
    for p in patches:
        p.stop()


class TestHealth:
    def test_status_ok(self, route_env):
        data = main_module.health()
        assert data["status"] == "ok"

    def test_n_workers_present(self, route_env):
        data = main_module.health()
        assert "n_workers" in data
        assert isinstance(data["n_workers"], int)
        assert data["n_workers"] > 0


class TestMetadataRoute:
    def test_returns_shape(self, route_env):
        data = _run(main_module.metadata("1492807"))
        for key in ("dimensions", "mpp", "vendor", "objective_power", "max_zoom", "tile_size", "levels"):
            assert key in data, f"missing key: {key}"

    def test_mpp_values(self, route_env):
        data = _run(main_module.metadata("1492807"))
        assert data["mpp"]["x"] == pytest.approx(0.5034)
        assert data["mpp"]["y"] == pytest.approx(0.5034)

    def test_objective_power(self, route_env):
        data = _run(main_module.metadata("1492807"))
        assert data["objective_power"] == 20

    def test_missing_slide_returns_404(self, route_env):
        main_module._slides.get.side_effect = FileNotFoundError("gone")
        try:
            with pytest.raises(HTTPException) as exc:
                _run(main_module.metadata("missing"))
            assert exc.value.status_code == 404
        finally:
            main_module._slides.get.side_effect = None


class TestTileRoute:
    def test_valid_tile_returns_jpeg(self, route_env):
        resp = _run(main_module.tile("1492807", 0, 0, 0))
        assert resp.media_type == "image/jpeg"
        assert resp.body[:2] == b"\xff\xd8"

    def test_cache_control_immutable(self, route_env):
        resp = _run(main_module.tile("1492807", 0, 0, 0))
        assert "immutable" in resp.headers.get("cache-control", "")

    def test_out_of_range_z_returns_404(self, route_env):
        with pytest.raises(HTTPException) as exc:
            _run(main_module.tile("1492807", 99, 0, 0))
        assert exc.value.status_code == 404

    def test_out_of_bounds_xy_returns_404(self, route_env):
        with pytest.raises(HTTPException) as exc:
            _run(main_module.tile("1492807", 2, 999, 0))
        assert exc.value.status_code == 404

    def test_warmup_uses_resolved_slide_path(self, route_env):
        main_module._slides.get.reset_mock()
        resp = main_module.warmup("1492807")
        assert resp == {"status": "ok"}
        main_module._slides.get.assert_called_once_with("s3://test-bucket/1492807.svs")


class TestThumbnailRoute:
    def test_returns_jpeg(self, route_env):
        resp = _run(main_module.thumbnail("1492807", width=256, height=256))
        assert resp.media_type == "image/jpeg"
        assert resp.body[:2] == b"\xff\xd8"

    def test_width_clamped_to_max(self, route_env):
        resp = _run(main_module.thumbnail("1492807", width=9999, height=256))
        assert resp.media_type == "image/jpeg"

    def test_cache_control_present(self, route_env):
        resp = _run(main_module.thumbnail("1492807", width=256, height=256))
        assert "max-age" in resp.headers.get("cache-control", "")


class TestPatientRoute:
    def test_not_found_returns_404(self, route_env):
        with patch("app.main._in_thread", new=AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                _run(main_module.patient_hierarchy("P-NOTEXIST"))
        assert exc.value.status_code == 404

    def test_databricks_error_returns_502(self, route_env):
        async def _raise(*a, **k):
            raise RuntimeError("Databricks down")

        with patch("app.main._in_thread", new=_raise):
            with pytest.raises(HTTPException) as exc:
                _run(main_module.patient_hierarchy("P-0001"))
        assert exc.value.status_code == 502


class TestSearchRoute:
    def test_short_query_returns_empty(self, route_env):
        assert _run(main_module.search("P")) == []

    def test_valid_query_returns_list(self, route_env):
        suggestions = [{"type": "patient", "id": "P-0001", "label": "P-0001", "sublabel": "CRC"}]
        with patch("app.main._in_thread", new=AsyncMock(return_value=suggestions)):
            resp = _run(main_module.search("P-0001"))
        assert isinstance(_json_body(resp), list)

    def test_search_error_returns_502(self, route_env):
        async def _raise(*a, **k):
            raise RuntimeError("Search failed")

        with patch("app.main._in_thread", new=_raise):
            with pytest.raises(HTTPException) as exc:
                _run(main_module.search("P-1234"))
        assert exc.value.status_code == 502
