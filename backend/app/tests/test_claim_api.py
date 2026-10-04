"""Orchestration regression through the real routes: pre-refactor main paths,
the expiry-edge ruling, and one expiry semantics across wall/detail/mine."""
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.db import connect
from app.main import app

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as cl:
        yield cl

def make_wish(client, title="w"):
    return client.post("/api/wishes", json={"title": title}).json()["id"]

def backdate_expiry(wid, ts="2020-01-01T00:00:00+00:00"):
    c = connect(); c.execute("UPDATE wishes SET expires_at=? WHERE id=?", (ts, wid)); c.commit(); c.close()

def set_ttl(seconds):
    c = connect(); c.execute("UPDATE settings SET value=? WHERE key='ttl_seconds'", (str(seconds),)); c.commit(); c.close()

def wall_card(client, wid):
    return next(w for w in client.get("/api/wishes").json() if w["id"] == wid)

# --- regression: pre-refactor main paths ---

def test_relock_during_lock_fails(client):
    wid = make_wish(client)
    assert client.post(f"/api/wishes/{wid}/claim", json={"claimer": "alice"}).status_code == 200
    r = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "bob"})
    assert r.status_code == 409 and r.json()["detail"] == "locked"
    assert client.get(f"/api/wishes/{wid}").json()["claimer"] == "alice"

def test_expired_lock_reclaimable_by_other(client):
    wid = make_wish(client)
    client.post(f"/api/wishes/{wid}/claim", json={"claimer": "alice"})
    backdate_expiry(wid)
    r = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "bob"})
    assert r.status_code == 200
    assert client.get(f"/api/wishes/{wid}").json()["claimer"] == "bob"

def test_release_then_reclaim(client):
    wid = make_wish(client)
    client.post(f"/api/wishes/{wid}/claim", json={"claimer": "alice"})
    assert client.post(f"/api/wishes/{wid}/release").status_code == 200
    r = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "bob"})
    assert r.status_code == 200
    assert client.get(f"/api/wishes/{wid}").json()["claimer"] == "bob"

# --- expiry edge ruling: fail the whole claim, keep the original lock ---

def test_dead_on_arrival_claim_fails_keeping_wish_open(client):
    set_ttl(0)  # new expires_at would not be in the future at write moment
    wid = make_wish(client)
    r = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "alice"})
    assert r.status_code == 409 and r.json()["detail"] == "lock_expired_at_write"
    w = client.get(f"/api/wishes/{wid}").json()
    assert w["status"] == "open" and w["claimer"] is None and w["expires_at"] is None

# --- one expiry semantics across wall card / detail / mine ---

def test_three_views_pin_same_lock_state(client):
    wid = make_wish(client)
    payload = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "alice"}).json()
    card = wall_card(client, wid)
    detail = client.get(f"/api/wishes/{wid}").json()
    mine = client.get("/api/mine", params={"claimer": "alice"}).json()
    assert card["status"] == "claimed" and card["claimer"] == "alice"
    assert detail["expires_at"] == payload["expires_at"]
    assert [w["id"] for w in mine] == [wid] and mine[0]["expires_at"] == payload["expires_at"]

    backdate_expiry(wid)
    card = wall_card(client, wid)
    detail = client.get(f"/api/wishes/{wid}").json()
    mine = client.get("/api/mine", params={"claimer": "alice"}).json()
    assert card["status"] == "open" and card["claimer"] is None
    assert detail["status"] == "open" and detail["expires_at"] is None
    assert mine == []

# --- structural guard: routes never re-parse expiry timestamps ---

def test_routes_do_not_parse_expiry():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "parse_ts" not in src and "fromisoformat" not in src
