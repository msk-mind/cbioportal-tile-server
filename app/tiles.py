"""
ZXY tile coordinate math and tile extraction.

Coordinate convention: z=0 is lowest resolution (whole slide in ~1 tile),
increasing z = increasing detail. x=0, y=0 is top-left. This matches the
convention used by OpenLayers, Leaflet, and the IIIF Image API.

Tile size is always TILE_SIZE × TILE_SIZE pixels. Edge tiles are padded with
white so callers never have to handle partial tiles.
"""

import io
import math

from PIL import Image
from tiffslide import TiffSlide

from .config import settings

TILE_SIZE = settings.tile_size


def max_zoom(slide: TiffSlide) -> int:
    """
    The highest zoom level for this slide.

    At max_zoom, one tile pixel ≈ one level-0 slide pixel (subject to
    rounding to the nearest power-of-two pyramid level).
    """
    w, h = slide.dimensions
    return math.ceil(math.log2(max(w, h) / TILE_SIZE))


def slide_metadata(slide: TiffSlide) -> dict:
    w, h = slide.dimensions
    mz = max_zoom(slide)
    try:
        props = slide.properties
        # tiffslide uses its own namespace; fall back to openslide for compat
        mpp_x = float(props.get("tiffslide.mpp-x") or props.get("openslide.mpp-x", 0) or 0)
        mpp_y = float(props.get("tiffslide.mpp-y") or props.get("openslide.mpp-y", 0) or 0)
        vendor = props.get("tiffslide.vendor") or props.get("openslide.vendor", "") or ""
        obj_power = props.get("tiffslide.objective-power") or props.get("openslide.objective-power")
        objective_power = int(obj_power) if obj_power is not None else None
    except Exception:
        mpp_x = mpp_y = 0.0
        vendor = ""
        objective_power = None

    return {
        "dimensions": {"width": w, "height": h},
        "levels": slide.level_count,
        "level_dimensions": [
            {"width": lw, "height": lh}
            for lw, lh in slide.level_dimensions
        ],
        "level_downsamples": list(slide.level_downsamples),
        "max_zoom": mz,
        "tile_size": TILE_SIZE,
        "mpp": {"x": mpp_x, "y": mpp_y},
        "objective_power": objective_power,
        "vendor": vendor,
    }


def get_tile_bytes(slide: TiffSlide, z: int, x: int, y: int) -> bytes:
    """
    Extract tile (x, y) at zoom level z and return JPEG bytes.

    Raises ValueError for out-of-range coordinates.
    """
    mz = max_zoom(slide)
    if z < 0 or z > mz:
        raise ValueError(f"zoom {z} out of range [0, {mz}]")

    # Number of level-0 pixels per tile pixel at this zoom level
    target_ds = 2 ** (mz - z)

    slide_w, slide_h = slide.dimensions

    # Top-left corner in level-0 coordinates
    x0 = x * TILE_SIZE * target_ds
    y0 = y * TILE_SIZE * target_ds

    if x0 >= slide_w or y0 >= slide_h:
        raise ValueError(f"tile ({x}, {y}, {z}) is outside slide bounds")

    # How many level-0 pixels this tile actually covers (may be < full tile at edges)
    src_w = min(TILE_SIZE * target_ds, slide_w - x0)
    src_h = min(TILE_SIZE * target_ds, slide_h - y0)

    # Desired output size before padding (≤ TILE_SIZE at edges)
    out_w = math.ceil(src_w / target_ds)
    out_h = math.ceil(src_h / target_ds)

    # Best available pyramid level (largest ds that doesn't exceed target)
    best_level = slide.get_best_level_for_downsample(target_ds)
    level_ds = slide.level_downsamples[best_level]

    # How many pixels to read from best_level to cover src region
    read_w = math.ceil(src_w / level_ds)
    read_h = math.ceil(src_h / level_ds)

    # Clamp to available pixels at this level
    level_w, level_h = slide.level_dimensions[best_level]
    read_w = min(read_w, level_w - math.floor(x0 / level_ds))
    read_h = min(read_h, level_h - math.floor(y0 / level_ds))

    if read_w <= 0 or read_h <= 0:
        return _blank_tile()

    # read_region returns RGBA; convert to RGB
    region = slide.read_region((x0, y0), best_level, (read_w, read_h))
    region = region.convert("RGB")

    # Always resize to desired output size — this is the key step that
    # downsamples when read_w > out_w (e.g. reading 512px to produce a
    # 256px tile at z=max_zoom-1) and upsamples when a sub-level was used.
    if region.size != (out_w, out_h):
        region = region.resize((out_w, out_h), Image.LANCZOS)

    # Pad edge tiles with white to reach full TILE_SIZE × TILE_SIZE
    if (out_w, out_h) != (TILE_SIZE, TILE_SIZE):
        canvas = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (255, 255, 255))
        canvas.paste(region, (0, 0))
        region = canvas

    return _encode_jpeg(region)


def get_thumbnail_bytes(slide: TiffSlide, width: int, height: int) -> bytes:
    thumb = slide.get_thumbnail((width, height))
    return _encode_jpeg(thumb.convert("RGB"))


def _blank_tile() -> bytes:
    img = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (255, 255, 255))
    return _encode_jpeg(img)


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=settings.jpeg_quality, optimize=True)
    return buf.getvalue()
