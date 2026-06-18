"""
Integration tests for the annotation API + Keycloak auth.

Requires a live Keycloak at http://localhost:8180 with:
  realm: cbio, client: annotation-api (public, direct grant enabled)
  Users (all password P@ssword1):
    testuser  → groups: [PUBLIC_STUDIES]
    viewer1   → groups: [ANNOTATION_VIEWERS, PUBLIC_STUDIES]
    editor1   → groups: [ANNOTATION_EDITORS, PUBLIC_STUDIES]
    admin1    → groups: [ANNOTATION_ADMINS, ANNOTATION_EDITORS, PUBLIC_STUDIES]
    noaccess  → groups: []

Start Keycloak:
  docker start keycloak-local   # or: ./keycloak-token.sh to verify it's up

Run:
  ANNOTATION_AUTH_ENABLED=true \
  KEYCLOAK_JWKS_URL=http://localhost:8180/auth/realms/cbio/protocol/openid-connect/certs \
  python3 -m pytest tests/test_annotations_integration.py -v

Skip when Keycloak is not running by passing --no-kc or via the KC_SKIP env var.
"""

import os
import urllib.parse
import uuid

import httpx
import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KC_URL = os.environ.get("KC_URL", "http://localhost:8180")
KC_REALM = os.environ.get("KC_REALM", "cbio")
KC_CLIENT = os.environ.get("KC_CLIENT", "annotation-api")
KC_TOKEN_URL = f"{KC_URL}/auth/realms/{KC_REALM}/protocol/openid-connect/token"
KC_JWKS_URL = f"{KC_URL}/auth/realms/{KC_REALM}/protocol/openid-connect/certs"

ANNOTATION_AUTH_ENABLED = os.environ.get("ANNOTATION_AUTH_ENABLED", "true").lower() != "false"

USERS = {
    "testuser": "P@ssword1",
    "viewer1": "P@ssword1",
    "editor1": "P@ssword1",
    "admin1": "P@ssword1",
    "noaccess": "P@ssword1",
}

SLIDE_ID = "integration-test-slide-001"
STUDY_ID = "integration-test-study"


# ---------------------------------------------------------------------------
# Pytest marker — skip all tests if Keycloak is unreachable
# ---------------------------------------------------------------------------

def _keycloak_reachable() -> bool:
    try:
        r = httpx.get(f"{KC_URL}/auth/realms/{KC_REALM}", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


kc_required = pytest.mark.skipif(
    not _keycloak_reachable(),
    reason=f"Keycloak not reachable at {KC_URL}",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    """Fetch fresh JWT access tokens for all test users."""
    result: dict[str, str] = {}
    for username, password in USERS.items():
        resp = httpx.post(
            KC_TOKEN_URL,
            data={
                "grant_type": "password",
                "client_id": KC_CLIENT,
                "username": username,
                "password": password,
            },
        )
        resp.raise_for_status()
        result[username] = resp.json()["access_token"]
    return result


@pytest_asyncio.fixture
async def db(tmp_path):
    """In-memory SQLite DB initialised for each test."""
    from app.annotations import init_db

    path = str(tmp_path / "test_annotations.db")
    await init_db(path)
    return path


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    """
    Async HTTPX client against the FastAPI app with:
      - auth ENABLED (real Keycloak JWT validation)
      - annotation DB pointing to the per-test SQLite file
    """
    monkeypatch.setenv("ANNOTATION_AUTH_ENABLED", "true")
    monkeypatch.setenv("KEYCLOAK_JWKS_URL", KC_JWKS_URL)
    monkeypatch.setenv("ANNOTATION_DB_PATH", db)

    # Reload settings singleton so env vars take effect
    import importlib
    import app.config as cfg_mod
    importlib.reload(cfg_mod)
    import app.auth as auth_mod
    importlib.reload(auth_mod)
    import app.annotations as ann_mod
    importlib.reload(ann_mod)
    # Point annotation router to fresh db path
    ann_mod._db_path = db
    # Clear JWKS cache so it re-fetches against local Keycloak
    auth_mod._jwks_cache = {}
    auth_mod._jwks_fetched_at = 0.0

    import app.main as main_mod
    from httpx import ASGITransport

    transport = ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ann_payload(**kwargs) -> dict:
    """Minimal valid annotation body; override fields via kwargs."""
    base = {
        "slide_id": SLIDE_ID,
        "study_id": STUDY_ID,
        "body": {"label": "test", "comment": "", "type": "rect"},
        "target": {"selector": {"type": "FragmentSelector", "value": "#xywh=10,20,100,80"}},
        "visible_to": None,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

@kc_required
class TestAuth:
    async def test_missing_token_returns_401(self, client):
        resp = await client.get(f"/annotations?slide_id={SLIDE_ID}&study_id={STUDY_ID}")
        assert resp.status_code == 401

    async def test_garbage_token_returns_401(self, client):
        resp = await client.get(
            f"/annotations?slide_id={SLIDE_ID}&study_id={STUDY_ID}",
            headers={"Authorization": "Bearer not.a.token"},
        )
        assert resp.status_code == 401

    async def test_valid_token_returns_200(self, client, tokens):
        resp = await client.get(
            f"/annotations?slide_id={SLIDE_ID}&study_id={STUDY_ID}",
            headers=_auth(tokens["testuser"]),
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_post_without_token_returns_401(self, client):
        resp = await client.post("/annotations", json=_ann_payload())
        assert resp.status_code == 401

    async def test_all_users_can_get_token(self, tokens):
        """Smoke-check that all test users retrieved tokens."""
        for username in USERS:
            assert tokens[username], f"No token for {username}"


# ---------------------------------------------------------------------------
# CRUD — basic
# ---------------------------------------------------------------------------

@kc_required
class TestAnnotationCRUD:
    async def test_create_returns_201(self, client, tokens):
        resp = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["editor1"]),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["slide_id"] == SLIDE_ID
        assert data["study_id"] == STUDY_ID
        assert data["version"] == 1
        assert uuid.UUID(data["id"])  # valid UUID

    async def test_created_by_matches_keycloak_sub(self, client, tokens):
        import base64, json as _json
        sub = _json.loads(base64.b64decode(tokens["editor1"].split(".")[1] + "==="))["sub"]

        resp = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["editor1"]),
        )
        assert resp.json()["created_by"] == sub

    async def test_list_returns_own_annotation(self, client, tokens):
        await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["testuser"]),
        )
        resp = await client.get(
            f"/annotations?slide_id={SLIDE_ID}&study_id={STUDY_ID}",
            headers=_auth(tokens["testuser"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_update_own_annotation(self, client, tokens):
        create = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["editor1"]),
        )
        ann = create.json()
        resp = await client.put(
            f"/annotations/{ann['id']}",
            json={"body": {"label": "updated", "comment": "c", "type": "rect"}, "version": 1},
            headers=_auth(tokens["editor1"]),
        )
        assert resp.status_code == 200
        assert resp.json()["body"]["label"] == "updated"
        assert resp.json()["version"] == 2

    async def test_delete_own_annotation(self, client, tokens):
        create = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["editor1"]),
        )
        ann_id = create.json()["id"]
        resp = await client.delete(
            f"/annotations/{ann_id}",
            headers=_auth(tokens["editor1"]),
        )
        assert resp.status_code == 204

        # Confirm gone (creator no longer sees it)
        lst = await client.get(
            f"/annotations?slide_id={SLIDE_ID}&study_id={STUDY_ID}",
            headers=_auth(tokens["editor1"]),
        )
        ids = [a["id"] for a in lst.json()]
        assert ann_id not in ids

    async def test_version_conflict_returns_409(self, client, tokens):
        create = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["editor1"]),
        )
        ann = create.json()
        # First update bumps version to 2
        await client.put(
            f"/annotations/{ann['id']}",
            json={"body": {"label": "v2", "comment": "", "type": "rect"}, "version": 1},
            headers=_auth(tokens["editor1"]),
        )
        # Second update with stale version=1 must conflict
        resp = await client.put(
            f"/annotations/{ann['id']}",
            json={"body": {"label": "stale", "comment": "", "type": "rect"}, "version": 1},
            headers=_auth(tokens["editor1"]),
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Visibility / ACL tests
# ---------------------------------------------------------------------------

@kc_required
class TestVisibility:
    """
    Verifies group-based visibility rules defined in annotations._is_visible().

    Groups in Keycloak:
      testuser  → [PUBLIC_STUDIES]
      viewer1   → [ANNOTATION_VIEWERS, PUBLIC_STUDIES]
      editor1   → [ANNOTATION_EDITORS, PUBLIC_STUDIES]
      admin1    → [ANNOTATION_ADMINS, ANNOTATION_EDITORS, PUBLIC_STUDIES]
      noaccess  → []
    """

    async def _create(self, client, tokens, username, visible_to):
        resp = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=visible_to),
            headers=_auth(tokens[username]),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def _ids_for(self, client, tokens, username):
        resp = await client.get(
            f"/annotations?slide_id={SLIDE_ID}&study_id={STUDY_ID}",
            headers=_auth(tokens[username]),
        )
        assert resp.status_code == 200
        return {a["id"] for a in resp.json()}

    async def test_private_annotation_visible_only_to_creator(self, client, tokens):
        """visible_to=None → only creator sees it."""
        ann_id = await self._create(client, tokens, "editor1", visible_to=None)

        assert ann_id in await self._ids_for(client, tokens, "editor1")   # creator ✓
        assert ann_id not in await self._ids_for(client, tokens, "viewer1")  # other ✗
        assert ann_id not in await self._ids_for(client, tokens, "admin1")   # even admin ✗

    async def test_world_readable_annotation_visible_to_all(self, client, tokens):
        """visible_to=[] → everyone in the study can read it."""
        ann_id = await self._create(client, tokens, "editor1", visible_to=[])

        for username in USERS:
            ids = await self._ids_for(client, tokens, username)
            assert ann_id in ids, f"{username} should see world-readable annotation"

    async def test_group_annotation_visible_to_group_members(self, client, tokens):
        """visible_to=["ANNOTATION_VIEWERS"] → viewer1 + admin1 (both in group) see it."""
        ann_id = await self._create(client, tokens, "editor1", visible_to=["ANNOTATION_VIEWERS"])

        assert ann_id in await self._ids_for(client, tokens, "editor1")   # creator ✓
        assert ann_id in await self._ids_for(client, tokens, "viewer1")   # in ANNOTATION_VIEWERS ✓
        assert ann_id not in await self._ids_for(client, tokens, "testuser")  # not in group ✗
        assert ann_id not in await self._ids_for(client, tokens, "noaccess")  # not in group ✗

    async def test_group_annotation_visible_to_multi_group(self, client, tokens):
        """visible_to=["ANNOTATION_EDITORS"] → editor1 and admin1 (both editors) see it."""
        ann_id = await self._create(client, tokens, "editor1", visible_to=["ANNOTATION_EDITORS"])

        assert ann_id in await self._ids_for(client, tokens, "editor1")  # creator + member ✓
        assert ann_id in await self._ids_for(client, tokens, "admin1")   # admin1 is ANNOTATION_EDITORS ✓
        assert ann_id not in await self._ids_for(client, tokens, "viewer1")  # not editor ✗

    async def test_creator_always_sees_private_annotation(self, client, tokens):
        """Creator always sees their own annotation regardless of visible_to."""
        ann_id = await self._create(client, tokens, "noaccess", visible_to=None)
        assert ann_id in await self._ids_for(client, tokens, "noaccess")


# ---------------------------------------------------------------------------
# Authorization enforcement
# ---------------------------------------------------------------------------

@kc_required
class TestAuthzEnforcement:
    async def test_non_creator_cannot_update(self, client, tokens):
        """Another user (even admin1) cannot update editor1's annotation."""
        resp = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["editor1"]),
        )
        ann = resp.json()

        resp2 = await client.put(
            f"/annotations/{ann['id']}",
            json={"body": {"label": "hijacked", "comment": "", "type": "rect"}, "version": 1},
            headers=_auth(tokens["admin1"]),
        )
        assert resp2.status_code == 403

    async def test_non_creator_cannot_delete(self, client, tokens):
        resp = await client.post(
            "/annotations",
            json=_ann_payload(visible_to=[]),
            headers=_auth(tokens["editor1"]),
        )
        ann_id = resp.json()["id"]

        resp2 = await client.delete(
            f"/annotations/{ann_id}",
            headers=_auth(tokens["admin1"]),
        )
        assert resp2.status_code == 403

    async def test_delete_nonexistent_returns_404(self, client, tokens):
        resp = await client.delete(
            f"/annotations/{uuid.uuid4()}",
            headers=_auth(tokens["editor1"]),
        )
        assert resp.status_code == 404

    async def test_update_nonexistent_returns_404(self, client, tokens):
        resp = await client.put(
            f"/annotations/{uuid.uuid4()}",
            json={"body": {"label": "x", "comment": "", "type": "rect"}, "version": 1},
            headers=_auth(tokens["editor1"]),
        )
        assert resp.status_code == 404
