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
import threading
from collections import OrderedDict

from tiffslide import TiffSlide

from .config import settings
from .slide_store import SlideEntry as _Entry
from .slide_store import close_entry as _close_entry
from .slide_store import open_slide as _open_slide

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

    opts: dict = {}
    if settings.aws_endpoint_url:
        opts["endpoint_url"] = settings.aws_endpoint_url
    if settings.aws_access_key_id:
        opts["key"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        opts["secret"] = settings.aws_secret_access_key
    return bucket, key, opts


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
            slide, fileobj = _open_slide(slide_id, log)
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
