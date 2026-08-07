import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.config import settings
from app.db import init_db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    init_db(":memory:")
    monkeypatch.setattr(settings, "admin_token", "tok")
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def add(name="alice", platform="polymarket", identifier="0xabc", pem="", token="tok"):
    return client.post(
        "/admin/participants",
        data={
            "name": name,
            "platform": platform,
            "identifier": identifier,
            "private_key_pem": pem,
            "token": token,
        },
    )


def test_add_participant_ok():
    resp = add()
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_bad_token_rejected():
    assert add(token="wrong").status_code == 403


def test_duplicate_account_returns_409():
    assert add().status_code == 200
    resp = add()
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_invalid_cred_secret_explained(monkeypatch):
    monkeypatch.setattr(settings, "cred_secret", "not-a-fernet-key")
    resp = add(platform="kalshi", identifier="key-1", pem="-----BEGIN PRIVATE KEY-----")
    assert resp.status_code == 500
    assert "CRED_SECRET is misconfigured" in resp.json()["detail"]


def test_kalshi_requires_pem():
    resp = add(platform="kalshi", identifier="key-1", pem="")
    assert resp.status_code == 400
