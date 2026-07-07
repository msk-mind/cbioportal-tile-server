import logging
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from . import cache as tile_cache
from . import meta
from .config import settings
from .http_utils import (
    PHI_CACHE_HEADERS,
    THUMB_CACHE_HEADERS,
    TILE_CACHE_HEADERS,
    jpeg_response,
    json_response,
)
from .meta import get_patient_hierarchy, get_slide_dbmeta, search_suggestions
from .tiles import get_thumbnail_bytes, get_tile_bytes, slide_metadata

logger = logging.getLogger(__name__)

ThreadRunner = Callable[..., Awaitable[Any]]
SlideGetter = Callable[[str], Any]


def resolve_slide_id(image_id: str, path_cache: dict[str, str]) -> str:
    if image_id in path_cache:
        return path_cache[image_id]
    path = meta.get_slide_path(image_id, settings.databricks_warehouse_id)
    if not path:
        raise FileNotFoundError(f"Slide not found: {image_id}")
    path_cache[image_id] = path
    return path


def get_slide(image_id: str, slides: Any, path_cache: dict[str, str]) -> Any:
    try:
        s3_uri = resolve_slide_id(image_id, path_cache)
        return slides.get(s3_uri)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Slide not found: {image_id}")
    except Exception:
        logger.exception("Failed to open slide %s", image_id)
        raise HTTPException(status_code=500, detail="Failed to open slide")


async def patient_hierarchy_route(patient_id: str, in_thread: ThreadRunner):
    cached = await tile_cache.get_patient(patient_id)
    if cached is not None:
        return json_response(cached)

    try:
        result = await in_thread(
            get_patient_hierarchy,
            patient_id,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Databricks query failed for patient %s", patient_id)
        raise HTTPException(status_code=502, detail="Metadata query failed")

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Patient not found in slide inventory",
        )

    await tile_cache.set_patient(patient_id, result)
    return json_response(result)


async def slide_dbmeta_route(image_id: str, in_thread: ThreadRunner):
    try:
        result = await in_thread(
            get_slide_dbmeta,
            image_id,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Databricks query failed for slide %s", image_id)
        raise HTTPException(status_code=502, detail="Metadata query failed")

    if result is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    return json_response(result)


async def search_route(q: str, in_thread: ThreadRunner):
    q = q.strip()
    if len(q) < 2:
        return []

    cache_key = f"search:{q.lower()}"
    cached = await tile_cache.get_raw(cache_key)
    if cached is not None:
        return json_response(cached)

    try:
        results = await in_thread(
            search_suggestions,
            q,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Search query failed for %r", q)
        raise HTTPException(status_code=502, detail="Search query failed")

    await tile_cache.set_raw(cache_key, results, ttl=300)
    return json_response(results)


async def metadata_route(slide_id: str, in_thread: ThreadRunner, slide_getter: SlideGetter):
    cached = await tile_cache.get_metadata(slide_id)
    if cached is not None:
        return cached
    slide = await in_thread(slide_getter, slide_id)
    result = await in_thread(slide_metadata, slide)
    await tile_cache.set_metadata(slide_id, result)
    return result


def warmup_route(slide_id: str, slide_getter: SlideGetter):
    try:
        slide = slide_getter(slide_id)
        get_tile_bytes(slide, 0, 0, 0)
    except Exception:
        pass
    return {"status": "ok"}


async def thumbnail_route(
    slide_id: str,
    width: int,
    height: int,
    in_thread: ThreadRunner,
    slide_getter: SlideGetter,
):
    width = max(1, min(width, 2048))
    height = max(1, min(height, 2048))

    cached = await tile_cache.get_thumbnail(slide_id, width, height)
    if cached:
        return jpeg_response(cached, THUMB_CACHE_HEADERS)

    slide = await in_thread(slide_getter, slide_id)
    data = await in_thread(get_thumbnail_bytes, slide, width, height)

    await tile_cache.set_thumbnail(slide_id, width, height, data)
    return jpeg_response(data, THUMB_CACHE_HEADERS)


async def tile_route(
    slide_id: str,
    z: int,
    x: int,
    y: int,
    in_thread: ThreadRunner,
    slide_getter: SlideGetter,
):
    cached = await tile_cache.get_tile(slide_id, z, x, y)
    if cached:
        return jpeg_response(cached, TILE_CACHE_HEADERS)

    slide = await in_thread(slide_getter, slide_id)

    try:
        data = await in_thread(get_tile_bytes, slide, z, x, y)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Tile extraction failed for %s z=%d x=%d y=%d", slide_id, z, x, y)
        raise HTTPException(status_code=500, detail=str(exc))

    await tile_cache.set_tile(slide_id, z, x, y, data)
    return jpeg_response(data, TILE_CACHE_HEADERS)
