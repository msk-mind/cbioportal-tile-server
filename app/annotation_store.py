import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiosqlite

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - exercised in environments without postgres extras
    asyncpg = None

from .config import settings

_db_path: str = ""
_db_url: str = ""

_POSTGRES_SELECT_FIELDS = """
id, slide_id, study_id, body, target, created_by,
created_by_username, created_by_name, visible_to,
version
"""
_POSTGRES_CREATED_AT = (
    "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at"
)
_POSTGRES_UPDATED_AT = (
    "to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS updated_at"
)
_POSTGRES_RETURNING_ROW = f"""
{_POSTGRES_SELECT_FIELDS},
{_POSTGRES_CREATED_AT},
{_POSTGRES_UPDATED_AT}
"""
_SQLITE_EXISTING_FIELDS = (
    "id, slide_id, study_id, body, target, created_by, "
    "created_by_username, created_by_name, visible_to, version, created_at"
)


@dataclass(frozen=True)
class _StorageBackend:
    init: Any
    list_annotations: Any
    create_annotation: Any
    get_existing: Any
    update_annotation: Any
    delete_annotation: Any


def _settings_db_url() -> str:
    return getattr(settings, "annotation_database_url", "")


def _storage_kind() -> str:
    return "postgres" if (_db_url or _settings_db_url()) else "sqlite"


def _get_db_path() -> str:
    return _db_path or settings.annotation_db_path


def _get_db_url() -> str:
    return _db_url or _settings_db_url()


def _require_asyncpg():
    if asyncpg is None:
        raise RuntimeError(
            "Postgres annotation storage requires the optional 'asyncpg' dependency"
        )
    return asyncpg


async def _apply_sqlite_pragmas(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA synchronous  = NORMAL")
    await db.execute("PRAGMA cache_size   = -64000")
    await db.execute("PRAGMA temp_store   = MEMORY")
    await db.execute("PRAGMA mmap_size    = 268435456")
    await db.execute("PRAGMA busy_timeout = 5000")


def _sqlite_target_json(selector: dict[str, Any]) -> str:
    return json.dumps({"selector": selector})


def _encode_visible_to(value: list[str] | None) -> str | None:
    return json.dumps(value) if value is not None else None


def _annotation_identity(user: dict[str, Any]) -> tuple[str, str, str]:
    created_by = user["sub"]
    created_by_username = user.get("preferred_username") or created_by
    created_by_name = user.get("name") or created_by_username or created_by
    return created_by, created_by_username, created_by_name


@asynccontextmanager
async def _sqlite_connection():
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        db.row_factory = aiosqlite.Row
        yield db


@asynccontextmanager
async def _postgres_connection():
    conn = await _require_asyncpg().connect(_get_db_url())
    try:
        yield conn
    finally:
        await conn.close()


async def _sqlite_fetchall(query: str, params: tuple[Any, ...]) -> list[dict]:
    async with _sqlite_connection() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _sqlite_fetchone(query: str, params: tuple[Any, ...]) -> dict | None:
    async with _sqlite_connection() as db:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _sqlite_execute(query: str, params: tuple[Any, ...]) -> None:
    async with _sqlite_connection() as db:
        await db.execute(query, params)
        await db.commit()


async def _postgres_fetchall(query: str, *params: Any) -> list[dict]:
    async with _postgres_connection() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(row) for row in rows]


async def _postgres_fetchone(query: str, *params: Any) -> dict | None:
    async with _postgres_connection() as conn:
        row = await conn.fetchrow(query, *params)
    return dict(row) if row else None


async def _postgres_execute(query: str, *params: Any) -> str | None:
    async with _postgres_connection() as conn:
        row = await conn.fetchrow(query, *params)
    if row is None:
        return None
    if len(row) == 1:
        return next(iter(row.values()))
    return None


async def _postgres_execute_command(query: str, *params: Any) -> None:
    async with _postgres_connection() as conn:
        await conn.execute(query, *params)


async def _init_sqlite(path: str) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await _apply_sqlite_pragmas(db)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS annotations (
                id          TEXT PRIMARY KEY,
                slide_id    TEXT NOT NULL,
                study_id    TEXT NOT NULL,
                body        TEXT NOT NULL,
                target      TEXT NOT NULL,
                created_by  TEXT NOT NULL,
                created_by_username TEXT,
                created_by_name TEXT,
                visible_to  TEXT,
                version     INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        try:
            await db.execute("ALTER TABLE annotations ADD COLUMN created_by_username TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE annotations ADD COLUMN created_by_name TEXT")
        except Exception:
            pass
        await db.execute(
            """
            UPDATE annotations
            SET created_by_username = COALESCE(created_by_username, created_by),
                created_by_name = COALESCE(created_by_name, created_by_username, created_by)
            WHERE created_by_username IS NULL OR created_by_name IS NULL
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ann_slide_study ON annotations(slide_id, study_id)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ann_slide ON annotations(slide_id)")
        await db.commit()


async def _init_postgres(dsn: str) -> None:
    async with _postgres_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS annotations (
                id          TEXT PRIMARY KEY,
                slide_id    TEXT NOT NULL,
                study_id    TEXT NOT NULL,
                body        TEXT NOT NULL,
                target      TEXT NOT NULL,
                created_by  TEXT NOT NULL,
                created_by_username TEXT,
                created_by_name TEXT,
                visible_to  TEXT,
                version     INTEGER NOT NULL DEFAULT 1,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
            )
            """
        )
        await conn.execute("ALTER TABLE annotations ADD COLUMN IF NOT EXISTS created_by_username TEXT")
        await conn.execute("ALTER TABLE annotations ADD COLUMN IF NOT EXISTS created_by_name TEXT")
        await conn.execute(
            """
            UPDATE annotations
            SET created_by_username = COALESCE(created_by_username, created_by),
                created_by_name = COALESCE(created_by_name, created_by_username, created_by)
            WHERE created_by_username IS NULL OR created_by_name IS NULL
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ann_slide_study ON annotations(slide_id, study_id)"
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ann_slide ON annotations(slide_id)")


async def init_db(db_path: str | None = None, db_url: str | None = None) -> str:
    global _db_path, _db_url
    _db_path = db_path or settings.annotation_db_path
    _db_url = db_url or _settings_db_url()
    backend = _backend()
    if _get_db_url():
        await backend.init(_get_db_url())
        return "postgres"
    await backend.init(_get_db_path())
    return "sqlite"


async def _list_sqlite(slide_id: str, study_id: str, user_sub: str) -> list[dict]:
    return await _sqlite_fetchall(
        """
        SELECT * FROM annotations
        WHERE slide_id = ? AND study_id = ?
          AND (created_by = ? OR visible_to IS NOT NULL)
        """,
        (slide_id, study_id, user_sub),
    )


async def _list_postgres(slide_id: str, study_id: str, user_sub: str) -> list[dict]:
    return await _postgres_fetchall(
        f"""
        SELECT
            {_POSTGRES_RETURNING_ROW}
        FROM annotations
        WHERE slide_id = $1 AND study_id = $2
          AND (created_by = $3 OR visible_to IS NOT NULL)
        """,
        slide_id,
        study_id,
        user_sub,
    )


async def _create_sqlite(data: Any, user: dict[str, Any]) -> dict:
    ann_id = str(uuid.uuid4())
    visible_to_json = _encode_visible_to(data.visible_to)
    created_by, created_by_username, created_by_name = _annotation_identity(user)
    async with _sqlite_connection() as db:
        await db.execute(
            """
            INSERT INTO annotations (
                id, slide_id, study_id, body, target, created_by,
                created_by_username, created_by_name, visible_to
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ann_id,
                data.slide_id,
                data.study_id,
                data.body.model_dump_json(),
                _sqlite_target_json(data.target.selector),
                created_by,
                created_by_username,
                created_by_name,
                visible_to_json,
            ),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM annotations WHERE id = ?", (ann_id,))
        row = await cursor.fetchone()
    return dict(row)


async def _create_postgres(data: Any, user: dict[str, Any]) -> dict:
    ann_id = str(uuid.uuid4())
    visible_to_json = _encode_visible_to(data.visible_to)
    created_by, created_by_username, created_by_name = _annotation_identity(user)
    return await _postgres_fetchone(
        f"""
        INSERT INTO annotations (
            id, slide_id, study_id, body, target, created_by,
            created_by_username, created_by_name, visible_to
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING
            {_POSTGRES_RETURNING_ROW}
        """,
        ann_id,
        data.slide_id,
        data.study_id,
        data.body.model_dump_json(),
        _sqlite_target_json(data.target.selector),
        created_by,
        created_by_username,
        created_by_name,
        visible_to_json,
    )


async def _get_existing_sqlite(annotation_id: str) -> dict | None:
    return await _sqlite_fetchone(
        f"SELECT {_SQLITE_EXISTING_FIELDS} FROM annotations WHERE id = ?",
        (annotation_id,),
    )


async def _get_existing_postgres(annotation_id: str) -> dict | None:
    return await _postgres_fetchone(
        f"""
        SELECT
            {_POSTGRES_SELECT_FIELDS},
            {_POSTGRES_CREATED_AT}
        FROM annotations WHERE id = $1
        """,
        annotation_id,
    )


async def _update_sqlite(annotation_id: str, new_body: str, new_target: str, new_visible: str | None, new_version: int) -> None:
    await _sqlite_execute(
        """
        UPDATE annotations
        SET body = ?, target = ?, visible_to = ?, version = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (new_body, new_target, new_visible, new_version, annotation_id),
    )


async def _update_postgres(annotation_id: str, new_body: str, new_target: str, new_visible: str | None, new_version: int) -> str:
    updated_at = await _postgres_execute(
        """
        UPDATE annotations
        SET body = $1, target = $2, visible_to = $3, version = $4,
            updated_at = timezone('utc', now())
        WHERE id = $5
        RETURNING to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
        """,
        new_body,
        new_target,
        new_visible,
        new_version,
        annotation_id,
    )
    return updated_at or ""


async def _delete_sqlite(annotation_id: str) -> None:
    await _sqlite_execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))


async def _delete_postgres(annotation_id: str) -> None:
    await _postgres_execute_command("DELETE FROM annotations WHERE id = $1", annotation_id)


def _backend() -> _StorageBackend:
    if _storage_kind() == "postgres":
        return _StorageBackend(
            init=_init_postgres,
            list_annotations=_list_postgres,
            create_annotation=_create_postgres,
            get_existing=_get_existing_postgres,
            update_annotation=_update_postgres,
            delete_annotation=_delete_postgres,
        )
    return _StorageBackend(
        init=_init_sqlite,
        list_annotations=_list_sqlite,
        create_annotation=_create_sqlite,
        get_existing=_get_existing_sqlite,
        update_annotation=_update_sqlite,
        delete_annotation=_delete_sqlite,
    )
