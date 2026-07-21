# cbioportal-tile-server

FastAPI tile server that streams SVS whole-slide images from Dell ECS (S3-compatible) to
OpenSeadragon via ZXY tile requests.  Used as the backend for the cBioPortal H&E slide viewer.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/patient/{patient_id}` | Slide hierarchy from Databricks |
| GET | `/slides/{image_id}/dbmeta` | Raw Databricks row for a slide |
| GET | `/search?q=` | Autocomplete suggestions |
| GET | `/tiles/{slide_id}/metadata` | Slide dimensions, zoom levels, MPP |
| GET | `/tiles/{slide_id}/thumbnail` | JPEG thumbnail |
| GET | `/tiles/{slide_id}/zxy/{z}/{x}/{y}` | ZXY tile (JPEG) |

The same endpoints are also available under the explicit `/wsi` namespace,
for example `/wsi/patient/{patient_id}` and `/wsi/tiles/{slide_id}/...`.

All endpoints except `/health` require a cBioPortal-issued short-lived WSI
capability in the header:

```text
Authorization: Bearer <token>
```

The token must be an HMAC-SHA256 JWT with the configured audience, the
`wsi:read` scope, a non-empty subject, and valid `iat`/`exp` claims. The
`/wsi/health` alias is also unauthenticated for probes. Do not disable this
check in production.

## Quick start

```bash
python3 tools/write_dev_env.py > .env   # populate from ~/.aws/credentials + ~/.databrickscfg
printf 'WSI_AUTH_SECRET=%s\nREDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" >> .env
docker compose up --build
```

## Configuration

All settings are environment variables (see `app/config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | — | Dell ECS endpoint |
| `AWS_ACCESS_KEY_ID` | — | ECS access key |
| `AWS_SECRET_ACCESS_KEY` | — | ECS secret key |
| `DATABRICKS_HOST` | — | Databricks workspace URL |
| `DATABRICKS_TOKEN` | — | Databricks PAT |
| `DATABRICKS_WAREHOUSE_ID` | `0b49b7d78734ad5c` | SQL warehouse |
| `WSI_AUTH_SECRET` | — | At least 32 bytes; shared with the cBioPortal capability issuer |
| `WSI_AUTH_AUDIENCE` | `cbioportal-wsi` | Capability-token audience |
| `WSI_AUTH_REQUIRED` | `true` | Require Bearer capabilities for non-health routes |
| `TILE_SIZE` | `256` | Tile edge length in pixels |
| `JPEG_QUALITY` | `85` | JPEG encoding quality |
| `REDIS_URL` | `redis://redis:6379` | Redis connection; use a password-protected URL in production |
| `TILE_CACHE_TTL` | `86400` | Tile cache TTL in seconds; `0` means no expiry |
| `PATIENT_CACHE_TTL` | `86400` | Patient metadata cache TTL in seconds; `0` disables it |
| `MAX_OPEN_SLIDES` | `64` | LRU slide cache capacity; benchmark-backed default |
| `N_WORKERS` | `4` | Gunicorn worker count |
| `BLOCKCACHE_PATH` | — | NVMe block cache directory |
| `BLOCKCACHE_BLOCK_SIZE` | `8388608` | Block-cache block size in bytes |
| `USE_CANONICAL_ASSOCIATION_TABLE` | `true` | Read patient associations from the canonical snapshot |
| `ALLOW_LEGACY_ASSOCIATION_FALLBACK` | `false` | Permit fallback if the canonical table is unavailable |
| `CORS_ORIGINS` | internal MSK cBioPortal origins | Comma-separated allowed origins |

Tile and thumbnail responses are private-cacheable (`max-age=3600` and
`max-age=300` respectively). Patient, slide metadata, and search responses
are `private, no-store` because they may contain PHI. Redis is an optimization
only; requests must continue to work if the cache is unavailable.

## Running tests

```bash
uv run pytest
```
