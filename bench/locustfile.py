"""
Locust load test for the tile server.

Usage:
    pip install locust
    locust -f bench/locustfile.py --host http://localhost:8080 \
           --users 200 --spawn-rate 10 --run-time 2m

Edit SLIDE_IDS to match real slides in your bucket.
"""

import random
from locust import HttpUser, task, between


# ---------------------------------------------------------------------------
# Configure these
# ---------------------------------------------------------------------------

SLIDE_IDS = [
    "sample1",
    "sample2",
    "sample3",
]

MAX_ZOOM = 15        # maximum zoom level in your slides
TILE_GRID = 8        # number of tiles along each axis at MAX_ZOOM
ZOOM_WEIGHTS = [1, 1, 1, 2, 4, 8]   # relative frequency per zoom offset from max


# ---------------------------------------------------------------------------

def random_tile_coord():
    # Bias towards high-zoom tiles (more requests in practice)
    z_offset = random.choices(range(len(ZOOM_WEIGHTS)), weights=ZOOM_WEIGHTS)[0]
    z = MAX_ZOOM - z_offset
    max_xy = (TILE_GRID >> z_offset) - 1
    if max_xy < 0:
        max_xy = 0
    x = random.randint(0, max_xy)
    y = random.randint(0, max_xy)
    return z, x, y


class TileUser(HttpUser):
    wait_time = between(0.05, 0.3)

    def on_start(self):
        self.slide_id = random.choice(SLIDE_IDS)
        # Warm up — prefetch metadata
        self.client.get(f"/tiles/{self.slide_id}/metadata")

    @task(20)
    def get_tile(self):
        z, x, y = random_tile_coord()
        self.client.get(
            f"/tiles/{self.slide_id}/zxy/{z}/{x}/{y}",
            name="/tiles/[slide]/zxy/[z]/[x]/[y]",
        )

    @task(1)
    def get_thumbnail(self):
        self.client.get(
            f"/tiles/{self.slide_id}/thumbnail?width=256&height=256",
            name="/tiles/[slide]/thumbnail",
        )

    @task(1)
    def get_metadata(self):
        self.client.get(
            f"/tiles/{self.slide_id}/metadata",
            name="/tiles/[slide]/metadata",
        )
