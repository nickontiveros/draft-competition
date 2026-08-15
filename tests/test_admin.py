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


def add(
    name="alice", platform="polymarket", identifier="0x" + "ab" * 20, pem="", token="tok"
):
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


def test_admin_page_delete_forms_not_nested():
    """Nested <form> tags are dropped by browsers, which silently rewires the
    per-row remove buttons to the outer GET form (regression test)."""
    from html.parser import HTMLParser

    add()
    page = client.get("/admin", params={"token": "tok"}).text
    assert "/accounts/" in page and "/delete" in page

    class FormNesting(HTMLParser):
        depth = 0
        max_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag == "form":
                self.depth += 1
                self.max_depth = max(self.max_depth, self.depth)

        def handle_endtag(self, tag):
            if tag == "form":
                self.depth -= 1

    parser = FormNesting()
    parser.feed(page)
    assert parser.max_depth == 1


def _account_id():
    from app.db import db_session
    from app.models import Account

    with db_session() as db:
        return db.query(Account).first().id


def test_rebaseline_resets_player(monkeypatch):
    monkeypatch.setattr(settings, "mock_connectors", True)
    add()
    aid = _account_id()
    # Simulate an inflated account: baseline never matching current value.
    resp = client.post(
        f"/admin/accounts/{aid}/rebaseline", data={"token": "tok"}, follow_redirects=False
    )
    assert resp.status_code == 303

    from app.db import db_session
    from app.models import Account
    from app.scoring import compute_standings

    with db_session() as db:
        account = db.get(Account, aid)
        standings = compute_standings(db)
    assert account.deposit_flag == ""
    assert account.baseline_adjustment == 0.0
    assert standings[0].game_value == pytest.approx(100.0)


def test_adjust_records_deposit_and_clears_flag():
    add()
    aid = _account_id()
    from app.db import db_session
    from app.models import Account

    with db_session() as db:
        db.get(Account, aid).deposit_flag = "cash +$40.00 not explained"

    resp = client.post(
        f"/admin/accounts/{aid}/adjust",
        data={"token": "tok", "amount": "40"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Withdrawals subtract.
    client.post(
        f"/admin/accounts/{aid}/adjust",
        data={"token": "tok", "amount": "-15.50"},
        follow_redirects=False,
    )
    with db_session() as db:
        account = db.get(Account, aid)
    assert account.baseline_adjustment == pytest.approx(24.50)
    assert account.deposit_flag == ""


def test_adjust_rejects_zero_and_bad_token():
    add()
    aid = _account_id()
    assert (
        client.post(
            f"/admin/accounts/{aid}/adjust", data={"token": "tok", "amount": "0"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/admin/accounts/{aid}/adjust", data={"token": "nope", "amount": "5"}
        ).status_code
        == 403
    )


def test_clear_flag():
    add()
    aid = _account_id()
    from app.db import db_session
    from app.models import Account

    with db_session() as db:
        db.get(Account, aid).deposit_flag = "cash +$9.00 not explained"
    resp = client.post(
        f"/admin/accounts/{aid}/clear-flag", data={"token": "tok"}, follow_redirects=False
    )
    assert resp.status_code == 303
    with db_session() as db:
        assert db.get(Account, aid).deposit_flag == ""


def test_history_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "mock_connectors", True)
    add()
    aid = _account_id()
    client.post(f"/admin/accounts/{aid}/rebaseline", data={"token": "tok"}, follow_redirects=False)

    assert client.get(f"/admin/accounts/{aid}/history").status_code == 403
    resp = client.get(f"/admin/accounts/{aid}/history", params={"token": "tok"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["participant"] == "alice"
    assert data["baseline"] is not None
    assert data["snapshots"][0]["total_value"] > 0
    assert any(s["is_baseline"] for s in data["snapshots"])
    assert "cash" in data["last_sync_note"] and "positions" in data["last_sync_note"]
    assert "new rows" in data["last_sync_note"]
    assert client.get("/admin/accounts/999/history", params={"token": "tok"}).status_code == 404


def test_backdate_endpoint(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "mock_connectors", True)
    add()
    aid = _account_id()
    now = datetime.now(timezone.utc)

    def backdate(to, token="tok"):
        return client.post(
            f"/admin/accounts/{aid}/backdate",
            data={"token": token, "to": to},
            follow_redirects=False,
        )

    assert backdate((now + timedelta(hours=1)).isoformat()).status_code == 400  # future
    assert backdate((now - timedelta(days=45)).isoformat()).status_code == 400  # too old
    assert backdate("not-a-date").status_code == 400
    assert backdate((now - timedelta(hours=2)).isoformat(), token="nope").status_code == 403

    T = now - timedelta(hours=2)
    with_db_flag_set()
    resp = backdate(T.isoformat())
    assert resp.status_code == 303

    from app.db import db_session
    from app.models import Account, Snapshot

    with db_session() as db:
        account = db.get(Account, aid)
        baseline = db.get(Snapshot, account.baseline_snapshot_id)
        ts = baseline.ts if baseline.ts.tzinfo else baseline.ts.replace(tzinfo=timezone.utc)
    assert abs((ts - T).total_seconds()) < 1
    assert account.deposit_flag == ""
    assert account.baseline_adjustment == 0.0


def with_db_flag_set():
    from app.db import db_session
    from app.models import Account

    with db_session() as db:
        db.query(Account).first().deposit_flag = "cash +$5.00 not explained"
