"""
Tile server — FastAPI application.

Endpoints:
  GET /health
  GET /tiles/{slide_id}/metadata
  GET /tiles/{slide_id}/thumbnail?width=256&height=256
  GET /tiles/{slide_id}/zxy/{z}/{x}/{y}

All tile and thumbnail responses carry long-lived Cache-Control headers so a
CDN or nginx proxy_cache can absorb the bulk of repeat requests.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

# Ensure app.* loggers emit to stderr alongside uvicorn's own loggers.
# uvicorn's dictConfig only configures uvicorn.* — root logger has no handler
# by default, so INFO from app.* would be silently dropped without this.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import cache as tile_cache
from .annotations import init_db as init_annotation_db
from .annotations import router as annotation_router
from .oncokb import router as oncokb_router
from .config import settings
from .http_utils import (
    PHI_CACHE_HEADERS,
    THUMB_CACHE_HEADERS,
    TILE_CACHE_HEADERS,
    jpeg_response as _jpeg_response,
    json_response as _json_response,
)
from .slides import SlideCache
from .tiles import get_tile_bytes
from .tile_routes import (
    get_slide as _tile_routes_get_slide,
    metadata_route,
    patient_hierarchy_route,
    resolve_slide_id as _tile_routes_resolve_slide_id,
    search_route,
    slide_dbmeta_route,
    thumbnail_route,
    tile_route,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_slides: SlideCache | None = None
_path_cache: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _slides
    _slides = SlideCache(capacity=settings.max_open_slides)
    await tile_cache.init_cache()
    if not settings.aws_endpoint_url:
        logger.warning(
            "AWS_ENDPOINT_URL is not set — S3 requests will go to public AWS "
            "(set this to your Dell ECS endpoint in production)"
        )
    logger.info(
        "Tile server ready. max_open_slides=%d endpoint=%s",
        settings.max_open_slides,
        settings.aws_endpoint_url or "AWS default",
    )
    await init_annotation_db()
    logger.info(
        "Annotation DB ready: %s",
        settings.annotation_database_url or settings.annotation_db_path,
    )
    yield
    _slides.close_all()
    await tile_cache.close_cache()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WSI Tile Server",
    description="Serve SVS whole-slide image tiles directly from Dell ECS (S3) via tiffslide.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(annotation_router)
app.include_router(oncokb_router)


async def _in_thread(fn, *args):
    """Run a blocking function in the default thread-pool executor."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_slide_id(image_id: str) -> str:
    """Resolve an image_id to its S3 URI, with in-process caching."""
    return _tile_routes_resolve_slide_id(image_id, _path_cache)


def _get_slide(image_id: str):
    """Resolve image_id → S3 path, open/retrieve from cache; raise 404 on failure."""
    return _tile_routes_get_slide(image_id, _slides, _path_cache)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "n_workers": settings.n_workers}


# ---------------------------------------------------------------------------
# Databricks metadata routes
# ---------------------------------------------------------------------------

@app.get("/patient/{patient_id}")
async def patient_hierarchy(patient_id: str):
    """
    Return the full patient slide hierarchy sourced from Databricks
    (DEID_TABLE joined with INVENTORY_TABLE — see app/constants.py).

    Structure: { patient_id, samples: [{ sample_id, cancer_type, …,
      parts: [{ part_number, …, blocks: [{ block_number, …,
        slides: [{ image_id, stain_name, can_serve_tiles, … }] }] }] }] }
    """
    return await patient_hierarchy_route(patient_id, _in_thread)


@app.get("/slides/{image_id}/dbmeta")
async def slide_dbmeta(image_id: str):
    """Return the raw Databricks metadata row for a single slide (by numeric image_id)."""
    return await slide_dbmeta_route(image_id, _in_thread)


@app.get("/search")
async def search(q: str = ""):
    """
    Autocomplete suggestions for the search bar.

    Returns up to 8 items: [{ type, id, label, sublabel }]
    Detects query pattern: P-xxx → patients, P-xxx-Tx → samples, digits → slides.
    Results are cached in Redis for 5 minutes.
    """
    return await search_route(q, _in_thread)


@app.get("/tiles/{slide_id}/metadata")
async def metadata(slide_id: str):
    return await metadata_route(slide_id, _in_thread, _get_slide)


@app.get("/tiles/{slide_id}/warmup", include_in_schema=False)
def warmup(slide_id: str):
    """Fetch and discard the overview tile to prime the TiffSlide cache on this worker."""
    try:
        slide = _get_slide(slide_id)
        get_tile_bytes(slide, 0, 0, 0)
    except Exception:
        pass
    return {"status": "ok"}


@app.get("/tiles/{slide_id}/thumbnail")
async def thumbnail(
    slide_id: str,
    width: int = 256,
    height: int = 256,
):
    return await thumbnail_route(slide_id, width, height, _in_thread, _get_slide)


@app.get("/tiles/{slide_id}/zxy/{z}/{x}/{y}")
async def tile(slide_id: str, z: int, x: int, y: int):
    return await tile_route(slide_id, z, x, y, _in_thread, _get_slide)
