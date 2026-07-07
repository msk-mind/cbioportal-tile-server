import json

from fastapi import Response

TILE_CACHE_HEADERS = {"Cache-Control": "public, max-age=604800, immutable"}
THUMB_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}
PHI_CACHE_HEADERS = {"Cache-Control": "private, no-store"}


def json_response(payload: object, *, default=str) -> Response:
    return Response(
        content=json.dumps(payload, default=default),
        media_type="application/json",
        headers=PHI_CACHE_HEADERS,
    )


def jpeg_response(payload: bytes, headers: dict[str, str]) -> Response:
    return Response(content=payload, media_type="image/jpeg", headers=headers)
