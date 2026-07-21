import configparser
import os
from dataclasses import dataclass, field

from .constants import DEFAULT_WAREHOUSE_ID as _DEFAULT_WAREHOUSE_ID


def _aws_profile(key: str, fallback: str = "") -> str:
    """Read a value from the [ecs] section of ~/.aws/credentials, if present."""
    try:
        cfg = configparser.ConfigParser()
        cfg.read(os.path.expanduser("~/.aws/credentials"))
        return cfg.get("ecs", key, fallback=fallback)
    except Exception:
        return fallback


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() != "false"


def _env_csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


@dataclass
class Settings:
    wsi_auth_secret: str = field(default_factory=lambda: _env_str("WSI_AUTH_SECRET"))
    wsi_auth_audience: str = field(default_factory=lambda: _env_str("WSI_AUTH_AUDIENCE", "cbioportal-wsi"))
    wsi_auth_required: bool = field(default_factory=lambda: _env_bool("WSI_AUTH_REQUIRED", True))
    wsi_study_mapping_table: str = field(default_factory=lambda: _env_str("WSI_STUDY_MAPPING_TABLE"))

    aws_endpoint_url: str = field(default_factory=lambda: _env_str("AWS_ENDPOINT_URL", _aws_profile("endpoint_url", "")))
    aws_access_key_id: str = field(default_factory=lambda: _env_str("AWS_ACCESS_KEY_ID", _aws_profile("aws_access_key_id")))
    aws_secret_access_key: str = field(default_factory=lambda: _env_str("AWS_SECRET_ACCESS_KEY", _aws_profile("aws_secret_access_key")))

    tile_size: int = field(default_factory=lambda: _env_int("TILE_SIZE", 256))
    jpeg_quality: int = field(default_factory=lambda: _env_int("JPEG_QUALITY", 85))
    redis_url: str = field(default_factory=lambda: _env_str("REDIS_URL", "redis://redis:6379"))
    tile_cache_ttl: int = field(default_factory=lambda: _env_int("TILE_CACHE_TTL", 86_400))
    max_open_slides: int = field(default_factory=lambda: _env_int("MAX_OPEN_SLIDES", 64))
    n_workers: int = field(default_factory=lambda: _env_int("N_WORKERS", 4))

    databricks_warehouse_id: str = field(
        default_factory=lambda: _env_str("DATABRICKS_WAREHOUSE_ID", _DEFAULT_WAREHOUSE_ID)
    )
    use_canonical_association_table: bool = field(
        default_factory=lambda: _env_bool("USE_CANONICAL_ASSOCIATION_TABLE", True)
    )
    allow_legacy_association_fallback: bool = field(
        default_factory=lambda: _env_bool("ALLOW_LEGACY_ASSOCIATION_FALLBACK", False)
    )
    patient_cache_ttl: int = field(default_factory=lambda: _env_int("PATIENT_CACHE_TTL", 86_400))
    blockcache_path: str = field(default_factory=lambda: _env_str("BLOCKCACHE_PATH", ""))
    blockcache_block_size: int = field(default_factory=lambda: _env_int("BLOCKCACHE_BLOCK_SIZE", 8 * 1024 * 1024))

    annotation_database_url: str = field(default_factory=lambda: _env_str("ANNOTATION_DATABASE_URL"))
    annotation_db_path: str = field(default_factory=lambda: _env_str("ANNOTATION_DB_PATH", "/data/annotations.db"))
    keycloak_jwks_url: str = field(default_factory=lambda: _env_str("KEYCLOAK_JWKS_URL"))
    annotation_auth_enabled: bool = field(default_factory=lambda: _env_bool("ANNOTATION_AUTH_ENABLED", True))
    oncokb_api_token: str = field(default_factory=lambda: _env_str("ONCOKB_API_TOKEN"))

    cors_origins: list[str] = field(
        default_factory=lambda: _env_csv(
            "CORS_ORIGINS",
            "https://cbioportal.mskcc.org,https://triage.cbioportal.mskcc.org",
        )
    )


settings = Settings()
