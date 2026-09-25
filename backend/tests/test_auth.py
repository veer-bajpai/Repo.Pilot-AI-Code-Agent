from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from app.auth import AuthStore, verify_jwt
from app.config import load_settings
from app.main import create_app


def test_password_hash_and_refresh_lifecycle(tmp_path):
    store = AuthStore(f"sqlite:///{tmp_path / 'auth.db'}", auto_create=True)
    user, verification = store.create_user("Ada@example.com", "a-very-long-password", "Ada")
    assert user.email == "ada@example.com"
    assert store.authenticate("ada@example.com", "a-very-long-password").id == user.id
    assert store.consume_email_token(verification, "verify").email_verified is True
    refresh = store.issue_refresh(user.id, 30)
    assert store.refresh_user(refresh).id == user.id


def test_api_signup_login_refresh_and_protected_usage(tmp_path):
    base = load_settings()
    settings = dataclasses.replace(
        base,
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "data" / "workspaces",
        database_path=tmp_path / "legacy.db",
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        auth_required=True,
        auth_auto_create=True,
        jwt_secret="test-secret",
        cors_origins=(),
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/usage").status_code == 401
        signup = client.post("/api/auth/signup", json={"email": "ada@example.com", "password": "a-very-long-password", "name": "Ada"})
        assert signup.status_code == 201
        assert signup.json()["user"]["email"] == "ada@example.com"
        assert client.get("/api/auth/me").json()["user"]["email"] == "ada@example.com"
        usage = client.get("/api/usage")
        assert usage.status_code == 200 and usage.json()["plan"] == "free"
        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").status_code == 401
        login = client.post("/api/auth/login", json={"email": "ada@example.com", "password": "a-very-long-password"})
        assert login.status_code == 200
        assert verify_jwt(client.cookies.get("rp_access"), "test-secret")["email"] == "ada@example.com"


def test_monthly_quota_is_enforced(tmp_path):
    store = AuthStore(f"sqlite:///{tmp_path / 'auth.db'}", auto_create=True)
    user, _ = store.create_user("quota@example.com", "a-very-long-password")
    store.reserve_run(user, 1)
    try:
        store.reserve_run(user, 1)
        assert False, "quota should reject the second reservation"
    except ValueError as exc:
        assert "quota" in str(exc).lower()
