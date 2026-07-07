import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import app.annotations as annotations


def _run(coro):
    return asyncio.run(coro)


def _existing_row(**overrides):
    row = {
        "id": "ann-1",
        "slide_id": "slide-1",
        "study_id": "study-1",
        "body": '{"label":"old","comment":"","type":"rect"}',
        "target": '{"selector":{"type":"FragmentSelector","value":"#xywh=1,2,3,4"}}',
        "created_by": "user-1",
        "created_by_username": "editor1",
        "created_by_name": "Editor One",
        "visible_to": '["GROUP_A"]',
        "version": 2,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


class TestHelpers:
    def test_annotation_identity_prefers_user_fields(self):
        assert annotations._annotation_identity(
            {"sub": "u1", "preferred_username": "editor", "name": "Editor Name"}
        ) == ("u1", "editor", "Editor Name")

    def test_merged_annotation_fields_preserves_existing_when_patch_omits_fields(self):
        existing = _existing_row()
        update = annotations.AnnotationUpdate(version=2)
        assert annotations._merged_annotation_fields(existing, update) == (
            existing["body"],
            existing["target"],
            existing["visible_to"],
            3,
        )

    def test_updated_annotation_out_preserves_creator_fields(self):
        existing = _existing_row()
        result = annotations._updated_annotation_out(
            existing,
            body=existing["body"],
            target=existing["target"],
            visible_to=existing["visible_to"],
            version=3,
            updated_at="2026-01-02T00:00:00Z",
        )
        assert result.created_by_username == "editor1"
        assert result.created_by_name == "Editor One"
        assert result.version == 3


class TestRoutes:
    def test_create_annotation_uses_selected_backend(self):
        backend = annotations._StorageBackend(
            init=AsyncMock(),
            list_annotations=AsyncMock(),
            create_annotation=AsyncMock(return_value=_existing_row()),
            get_existing=AsyncMock(),
            update_annotation=AsyncMock(),
            delete_annotation=AsyncMock(),
        )
        data = annotations.AnnotationIn(
            slide_id="slide-1",
            study_id="study-1",
            body={"label": "x", "comment": "", "type": "rect"},
            target={"selector": {"type": "FragmentSelector", "value": "#xywh=1,2,3,4"}},
            visible_to=[],
        )
        user = {"sub": "user-1", "groups": []}

        with patch("app.annotations._backend", return_value=backend):
            result = _run(annotations.create_annotation(data, user))

        backend.create_annotation.assert_awaited_once()
        assert result.id == "ann-1"

    def test_update_annotation_uses_selected_backend(self):
        backend = annotations._StorageBackend(
            init=AsyncMock(),
            list_annotations=AsyncMock(),
            create_annotation=AsyncMock(),
            get_existing=AsyncMock(return_value=_existing_row()),
            update_annotation=AsyncMock(return_value="2026-01-02T00:00:00Z"),
            delete_annotation=AsyncMock(),
        )
        update = annotations.AnnotationUpdate(
            body={"label": "new", "comment": "", "type": "rect"},
            version=2,
        )
        user = {"sub": "user-1", "groups": []}

        with patch("app.annotations._backend", return_value=backend):
            result = _run(annotations.update_annotation("ann-1", update, user))

        backend.get_existing.assert_awaited_once_with("ann-1")
        backend.update_annotation.assert_awaited_once()
        assert result.body.label == "new"
        assert result.version == 3

    def test_update_annotation_rejects_wrong_user(self):
        backend = annotations._StorageBackend(
            init=AsyncMock(),
            list_annotations=AsyncMock(),
            create_annotation=AsyncMock(),
            get_existing=AsyncMock(return_value=_existing_row(created_by="other-user")),
            update_annotation=AsyncMock(),
            delete_annotation=AsyncMock(),
        )
        update = annotations.AnnotationUpdate(version=2)

        with patch("app.annotations._backend", return_value=backend):
            with pytest.raises(HTTPException) as exc:
                _run(annotations.update_annotation("ann-1", update, {"sub": "user-1", "groups": []}))

        assert exc.value.status_code == 403

    def test_delete_annotation_uses_selected_backend(self):
        backend = annotations._StorageBackend(
            init=AsyncMock(),
            list_annotations=AsyncMock(),
            create_annotation=AsyncMock(),
            get_existing=AsyncMock(return_value=_existing_row()),
            update_annotation=AsyncMock(),
            delete_annotation=AsyncMock(),
        )

        with patch("app.annotations._backend", return_value=backend):
            _run(annotations.delete_annotation("ann-1", {"sub": "user-1", "groups": []}))

        backend.delete_annotation.assert_awaited_once_with("ann-1")

    def test_list_annotations_filters_visibility_after_backend_lookup(self):
        backend = annotations._StorageBackend(
            init=AsyncMock(),
            list_annotations=AsyncMock(
                return_value=[
                    _existing_row(id="visible", visible_to='["GROUP_A"]'),
                    _existing_row(id="hidden", created_by="other", visible_to='["GROUP_B"]'),
                ]
            ),
            create_annotation=AsyncMock(),
            get_existing=AsyncMock(),
            update_annotation=AsyncMock(),
            delete_annotation=AsyncMock(),
        )

        with patch("app.annotations._backend", return_value=backend):
            result = _run(
                annotations.list_annotations(
                    slide_id="slide-1",
                    study_id="study-1",
                    user={"sub": "user-1", "groups": ["GROUP_A"]},
                )
            )

        assert [row.id for row in result] == ["visible"]
