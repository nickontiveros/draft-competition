import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.config import settings
from app.db import db_session, init_db
from app.main import app
from app.scoring import compute_standings

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    init_db(":memory:")
    monkeypatch.setattr(settings, "signup_passphrase", "open-sesame")
    monkeypatch.setattr(settings, "mock_connectors", True)
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def join(passphrase="open-sesame", name="dana", platform="polymarket", identifier="0xdd1"):
    return client.post(
        "/join",
        data={
            "passphrase": passphrase,
            "name": name,
            "platform": platform,
            "identifier": identifier,
            "private_key_pem": "",
        },
        follow_redirects=False,
    )


def test_join_creates_player_at_100():
    resp = join()
    assert resp.status_code == 303
    assert "welcome=dana" in resp.headers["location"]

    with db_session() as db:
        standings = compute_standings(db)
    assert standings[0].name == "dana"
    # First sync ran and stamped the baseline: enters the game at exactly $100.
    assert standings[0].game_value == pytest.approx(100.0)
    assert standings[0].pnl == pytest.approx(0.0)


def test_wrong_passphrase_rejected():
    assert join(passphrase="nope").status_code == 403
    with db_session() as db:
        assert compute_standings(db) == []


def test_signup_disabled_without_passphrase(monkeypatch):
    monkeypatch.setattr(settings, "signup_passphrase", "")
    assert join().status_code == 503


def test_duplicate_redirects_with_error():
    assert join().status_code == 303
    resp = join()
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_join_page_renders():
    resp = client.get("/join")
    assert resp.status_code == 200
    assert "read-only" in resp.text
    assert "wallet address" in resp.text.lower()


def test_player_page_renders():
    join()
    with db_session() as db:
        pid = compute_standings(db)[0].participant_id
    resp = client.get(f"/player/{pid}")
    assert resp.status_code == 200
    assert "dana" in resp.text
    assert "Win rate" in resp.text
