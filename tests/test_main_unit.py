from unittest.mock import patch

import app.main as main_module


class TestWarmup:
    def test_warmup_uses_resolved_slide_lookup(self):
        slide = object()

        with (
            patch("app.main._get_slide", return_value=slide) as mock_get_slide,
            patch("app.main.get_tile_bytes", return_value=b"jpeg") as mock_get_tile_bytes,
        ):
            response = main_module.warmup("1492807")

        assert response == {"status": "ok"}
        mock_get_slide.assert_called_once_with("1492807")
        mock_get_tile_bytes.assert_called_once_with(slide, 0, 0, 0)

    def test_warmup_swallows_lookup_errors(self):
        with patch("app.main._get_slide", side_effect=RuntimeError("boom")) as mock_get_slide:
            response = main_module.warmup("1492807")

        assert response == {"status": "ok"}
        mock_get_slide.assert_called_once_with("1492807")


class TestJsonHelpers:
    def test_json_response_uses_phi_headers(self):
        response = main_module._json_response({"ok": True})
        assert response.media_type == "application/json"
        assert response.headers["cache-control"] == "private, no-store"

    def test_jpeg_response_uses_image_media_type(self):
        response = main_module._jpeg_response(b"jpg", main_module.THUMB_CACHE_HEADERS)
        assert response.media_type == "image/jpeg"
        assert response.headers["cache-control"] == main_module.THUMB_CACHE_HEADERS["Cache-Control"]
