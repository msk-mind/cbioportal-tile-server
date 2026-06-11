"""
TiffSlide source cache.

Keeps up to MAX_OPEN_SLIDES TiffSlide objects open in an LRU cache so repeated
tile requests for the same slide don't pay the cost of re-opening from ECS.
Thread-safe via a lock.

When BLOCKCACHE_PATH is set, each slide is opened through an fsspec BlockCache
filesystem that stores fixed-size blocks (default 8 MB) on local NVMe.  After
the first read of a block, subsequent reads come from disk rather than ECS —
this turns the p95 ~160 ms ECS latency into <1 ms NVMe reads.
"""

import logging
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import fsspec
from tiffslide import TiffSlide

from .config import settings

log = logging.getLogger(__name__)


def _resolve_s3_location(slide_id: str) -> tuple[str, str, dict]:
    """
    Return (bucket, key, s3_opts) for a slide_id.

    slide_id must be a full s3:// URI as stored in the Databricks inventory table,
    e.g. "s3://mskmind-bkt/reef-slides/3735444.svs".
    """
    if not slide_id.startswith("s3://"):
        raise FileNotFoundError(f"Slide not found: {slide_id!r} (expected s3:// URI)")
    without_scheme = slide_id[5:]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise FileNotFoundError(f"Malformed slide URI: {slide_id!r}")
    return bucket, key, _s3_opts()


def _s3_opts() -> dict:
    """fsspec/s3fs options built from canonical AWS env vars."""
    opts: dict = {}
    if settings.aws_endpoint_url:
        opts["endpoint_url"] = settings.aws_endpoint_url
    if settings.aws_access_key_id:
        opts["key"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        opts["secret"] = settings.aws_secret_access_key
    return opts


def _open_slide(slide_id: str) -> tuple[TiffSlide, Any]:
    """
    Open a TiffSlide and return (slide, fileobj).

    Supports three slide_id forms — see _resolve_s3_location() for details.
    fileobj is the fsspec file handle kept alive alongside the slide.
    When BlockCache is not configured, fileobj is None.
    """
    bucket, key, s3_opts = _resolve_s3_location(slide_id)

    if settings.blockcache_path:
        # One cache subdirectory per slide keeps namespace clean.
        safe_id = slide_id.replace("/", "_").replace(":", "_").lstrip("_")
        cache_dir = os.path.join(settings.blockcache_path, safe_id)
        os.makedirs(cache_dir, exist_ok=True)

        def _open_with_cache(cache_dir: str) -> tuple[TiffSlide, Any]:
            fs = fsspec.filesystem(
                "blockcache",
                target_protocol="s3",
                target_options=s3_opts,
                cache_storage=cache_dir,
                block_size=settings.blockcache_block_size,
            )
            fileobj = fs.open(f"{bucket}/{key}", "rb")
            return TiffSlide(fileobj), fileobj

        slide, fileobj = _open_with_cache(cache_dir)

        if slide.level_count < 2:
            log.warning(
                "slide %s opened with level_count=%d — stale blockcache suspected; "
                "clearing cache dir and retrying",
                slide_id, slide.level_count,
            )
            try:
                fileobj.close()
                slide.close()
            except Exception:
                pass
            shutil.rmtree(cache_dir, ignore_errors=True)
            os.makedirs(cache_dir, exist_ok=True)
            slide, fileobj = _open_with_cache(cache_dir)
            log.info("slide %s re-opened: level_count=%d", slide_id, slide.level_count)

        return slide, fileobj
    else:
        url = f"s3://{bucket}/{key}"
        slide = TiffSlide(url, storage_options=s3_opts)
        return slide, None


@dataclass
class _Entry:
    slide: TiffSlide
    fileobj: Any  # fsspec file handle or None


class SlideCache:
    """Thread-safe LRU cache of open TiffSlide objects."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        # Per-slide locks prevent duplicate opens when many threads request
        # the same cold slide simultaneously.
        self._opening: dict[str, threading.Event] = {}

    def get(self, slide_id: str) -> TiffSlide:
        # Fast path — slide already open
        with self._lock:
            if slide_id in self._cache:
                self._cache.move_to_end(slide_id)
                return self._cache[slide_id].slide

            # If another thread is already opening this slide, wait for it
            if slide_id in self._opening:
                event = self._opening[slide_id]
            else:
                # Register intent to open; other threads will wait on the event
                event = threading.Event()
                self._opening[slide_id] = event
                event = None  # sentinel: this thread is the opener

        if event is not None:
            # Another thread is opening — wait then return from cache
            event.wait()
            with self._lock:
                if slide_id in self._cache:
                    self._cache.move_to_end(slide_id)
                    return self._cache[slide_id].slide
            # Fallthrough: opener failed; try opening ourselves
            return self.get(slide_id)

        # This thread is responsible for opening — do it WITHOUT the lock
        try:
            slide, fileobj = _open_slide(slide_id)
        except Exception:
            with self._lock:
                self._opening.pop(slide_id, None)
            raise
        finally:
            # Always signal waiters even on error so they don't hang
            with self._lock:
                ev = self._opening.pop(slide_id, None)
            if ev is not None:
                ev.set()

        with self._lock:
            # Evict LRU if at capacity
            if len(self._cache) >= self._capacity:
                _, evicted = self._cache.popitem(last=False)
                _close_entry(evicted)
            self._cache[slide_id] = _Entry(slide=slide, fileobj=fileobj)
            return slide

    def invalidate(self, slide_id: str) -> None:
        with self._lock:
            if slide_id in self._cache:
                _close_entry(self._cache.pop(slide_id))

    def close_all(self) -> None:
        with self._lock:
            for entry in self._cache.values():
                _close_entry(entry)
            self._cache.clear()


def _close_entry(entry: _Entry) -> None:
    try:
        entry.slide.close()
    except Exception:
        pass
    if entry.fileobj is not None:
        try:
            entry.fileobj.close()
        except Exception:
            pass

