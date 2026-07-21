import base64
import hashlib
import hmac
import json
import time

import pytest

from app.auth import InvalidWsiToken, validate_wsi_token


def token(secret, **claims):
    enc = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    header, payload = enc({"alg": "HS256", "typ": "JWT"}), enc(claims)
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def test_valid_capability():
    secret = "s" * 32
    value = validate_wsi_token(token(secret, sub="u", study_id="coad_msk_2025", aud="cbioportal-wsi", scope="wsi:read",
                                     iat=int(time.time()), exp=int(time.time()) + 300),
                               secret, "cbioportal-wsi")
    assert value["sub"] == "u"


@pytest.mark.parametrize("claim,value", [("scope", "wsi:write"), ("aud", "other"),
                                          ("exp", int(time.time()) - 1)])
def test_invalid_capability_claims(claim, value):
    secret = "s" * 32
    claims = {"sub": "u", "study_id": "coad_msk_2025", "aud": "cbioportal-wsi", "scope": "wsi:read",
              "iat": int(time.time()), "exp": int(time.time()) + 300}
    claims[claim] = value
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(token(secret, **claims), secret, "cbioportal-wsi")


def test_study_scope_must_match_request():
    secret = "s" * 32
    claims = {"sub": "u", "study_id": "coad_msk_2025", "aud": "cbioportal-wsi",
              "scope": "wsi:read", "iat": int(time.time()), "exp": int(time.time()) + 300}
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(token(secret, **claims), secret, "cbioportal-wsi", "other-study")


def test_study_scope_is_required():
    secret = "s" * 32
    claims = {"sub": "u", "aud": "cbioportal-wsi", "scope": "wsi:read",
              "iat": int(time.time()), "exp": int(time.time()) + 300}
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(token(secret, **claims), secret, "cbioportal-wsi")
