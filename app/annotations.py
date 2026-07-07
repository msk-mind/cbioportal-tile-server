"""
Annotation CRUD with pluggable SQLite or Postgres storage.

Routes:
  GET    /annotations             list annotations for a slide/study
  POST   /annotations             create a new annotation
  PUT    /annotations/{id}        update (optimistic concurrency via version)
  DELETE /annotations/{id}        delete (creator only)

Auth: all routes require a valid Keycloak JWT via ``require_user()``.
Study-level ACL: annotations inherit study access from cBioPortal -
if a user can see the study they can read/write its annotations.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import annotation_store
from .auth import require_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotations", tags=["annotations"])

init_db = annotation_store.init_db
_StorageBackend = annotation_store._StorageBackend
_backend = annotation_store._backend
_annotation_identity = annotation_store._annotation_identity


def _decode_visible_to(value: str | None) -> list[str] | None:
    return json.loads(value) if value is not None else None


def _is_visible(row: dict, user_sub: str, user_groups: set[str]) -> bool:
    if row["created_by"] == user_sub:
        return True
    visible_to = row["visible_to"]
    if visible_to is None:
        return False
    groups = json.loads(visible_to) if isinstance(visible_to, str) else visible_to
    if not groups:
        return True
    return bool(user_groups.intersection(groups))


class AnnotationBody(BaseModel):
    label: str = ""
    comment: str = ""
    type: str = ""


class AnnotationTarget(BaseModel):
    selector: dict[str, Any]


class AnnotationIn(BaseModel):
    slide_id: str
    study_id: str
    body: AnnotationBody
    target: AnnotationTarget
    visible_to: list[str] | None = Field(
        default=None,
        description="Keycloak group names that may view this annotation; null = creator only",
    )


class AnnotationOut(BaseModel):
    id: str
    slide_id: str
    study_id: str
    body: AnnotationBody
    target: AnnotationTarget
    created_by: str
    created_by_username: str
    created_by_name: str
    visible_to: list[str] | None
    version: int
    created_at: str
    updated_at: str


class AnnotationUpdate(BaseModel):
    body: AnnotationBody | None = None
    target: AnnotationTarget | None = None
    visible_to: list[str] | None = None
    version: int = Field(..., description="Must match current version (optimistic lock)")


def _row_to_out(row: dict) -> AnnotationOut:
    return AnnotationOut(
        id=row["id"],
        slide_id=row["slide_id"],
        study_id=row["study_id"],
        body=AnnotationBody(**json.loads(row["body"])),
        target=AnnotationTarget(**json.loads(row["target"])),
        created_by=row["created_by"],
        created_by_username=row.get("created_by_username") or row["created_by"],
        created_by_name=row.get("created_by_name") or row.get("created_by_username") or row["created_by"],
        visible_to=_decode_visible_to(row["visible_to"]),
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _merged_annotation_fields(existing: dict, data: AnnotationUpdate) -> tuple[str, str, str | None, int]:
    new_body = data.body.model_dump_json() if data.body is not None else existing["body"]
    new_target = (
        annotation_store._sqlite_target_json(data.target.selector)
        if data.target is not None
        else existing["target"]
    )
    new_visible = (
        annotation_store._encode_visible_to(data.visible_to)
        if data.visible_to is not None
        else existing["visible_to"]
    )
    return new_body, new_target, new_visible, existing["version"] + 1


def _updated_annotation_out(
    existing: dict,
    *,
    body: str,
    target: str,
    visible_to: str | None,
    version: int,
    updated_at: str,
) -> AnnotationOut:
    return AnnotationOut(
        id=existing["id"],
        slide_id=existing["slide_id"],
        study_id=existing["study_id"],
        body=AnnotationBody(**json.loads(body)),
        target=AnnotationTarget(**json.loads(target)),
        created_by=existing["created_by"],
        created_by_username=existing.get("created_by_username") or existing["created_by"],
        created_by_name=existing.get("created_by_name")
        or existing.get("created_by_username")
        or existing["created_by"],
        visible_to=_decode_visible_to(visible_to),
        version=version,
        created_at=existing["created_at"],
        updated_at=updated_at,
    )


@router.get("", response_model=list[AnnotationOut])
async def list_annotations(
    slide_id: str = Query(..., description="Slide image_id"),
    study_id: str = Query(..., description="cBioPortal study ID"),
    user: dict = Depends(require_user),
) -> list[AnnotationOut]:
    user_sub: str = user["sub"]
    user_groups: set[str] = set(user["groups"])
    rows = await _backend().list_annotations(slide_id, study_id, user_sub)
    return [_row_to_out(row) for row in rows if _is_visible(row, user_sub, user_groups)]


@router.post("", response_model=AnnotationOut, status_code=status.HTTP_201_CREATED)
async def create_annotation(
    data: AnnotationIn,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    row = await _backend().create_annotation(data, user)
    return _row_to_out(row)


@router.put("/{annotation_id}", response_model=AnnotationOut)
async def update_annotation(
    annotation_id: str,
    data: AnnotationUpdate,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    backend = _backend()
    existing = await backend.get_existing(annotation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if existing["created_by"] != user["sub"]:
        raise HTTPException(status_code=403, detail="Only the creator may update this annotation")
    if existing["version"] != data.version:
        raise HTTPException(
            status_code=409,
            detail=f"Version conflict: expected {existing['version']}, got {data.version}",
        )

    new_body, new_target, new_visible, new_version = _merged_annotation_fields(existing, data)
    updated_at = await backend.update_annotation(
        annotation_id, new_body, new_target, new_visible, new_version
    )
    if updated_at is None:
        updated_at = ""

    return _updated_annotation_out(
        existing,
        body=new_body,
        target=new_target,
        visible_to=new_visible,
        version=new_version,
        updated_at=updated_at,
    )


@router.delete("/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(
    annotation_id: str,
    user: dict = Depends(require_user),
) -> None:
    backend = _backend()
    existing = await backend.get_existing(annotation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if existing["created_by"] != user["sub"]:
        raise HTTPException(status_code=403, detail="Only the creator may delete this annotation")

    await backend.delete_annotation(annotation_id)
