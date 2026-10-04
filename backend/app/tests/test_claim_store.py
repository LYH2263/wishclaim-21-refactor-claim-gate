"""Persistence tests for claim_store — every write is verified by reading the row back."""
import pytest
from datetime import datetime, timedelta, timezone
from app import seed
from app.db import connect
from app.engines import claim_store

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
TTL = 3600

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM wishes")
    c.commit()
    yield c
    c.close()

def add_wish(c, status="open", claimer=None, claimed_at=None, expires_at=None):
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality) VALUES (?,?,?,?,?,?,?)",
        ("t", "n", status, claimer, claimed_at, expires_at, "clean"))
    c.commit()
    return cur.lastrowid

def row(c, wid):
    return dict(c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone())

def snap(wid, status="claimed", claimer="carol", expires_at=None):
    return {"id": wid, "status": status, "claimer": claimer, "expires_at": expires_at}

def test_write_lock_persists_and_reads_back(db):
    wid = add_wish(db)
    res = claim_store.write_lock(db, wid, "alice", NOW, TTL, expected=snap(wid, "open", None, None))
    assert res["ok"] is True
    db.commit()
    r = row(db, wid)
    assert r["status"] == "claimed" and r["claimer"] == "alice"
    assert r["claimed_at"] == NOW.isoformat()
    assert r["expires_at"] == (NOW + timedelta(seconds=TTL)).isoformat()
    assert res["payload"]["expires_at"] == r["expires_at"]

@pytest.mark.parametrize("bad_ttl", [0, -60])
def test_write_lock_born_dead_fails_and_keeps_row(db, bad_ttl):
    # Clock side of the expiry edge: expires_at would not be in the future at
    # write moment -> whole claim fails, original (unlocked) row kept.
    wid = add_wish(db)
    res = claim_store.write_lock(db, wid, "alice", NOW, bad_ttl, expected=snap(wid, "open", None, None))
    assert res["ok"] is False and res["reason"] == "lock_expired_at_write"
    db.commit()
    r = row(db, wid)
    assert r["status"] == "open" and r["claimer"] is None and r["expires_at"] is None

def test_write_lock_stale_snapshot_keeps_current_lock(db):
    # DB side of the expiry edge: the row moved (concurrent claim) after our
    # decision snapshot -> whole claim fails, the current lock stays untouched.
    wid = add_wish(db, "claimed", "carol", NOW.isoformat(), (NOW + timedelta(seconds=900)).isoformat())
    stale = claim_store.fetch_claim_state(db, wid)
    db.execute("UPDATE wishes SET claimer=?, expires_at=? WHERE id=?",
               ("dave", (NOW + timedelta(seconds=1800)).isoformat(), wid))
    db.commit()
    res = claim_store.write_lock(db, wid, "alice", NOW, TTL, expected=stale)
    assert res["ok"] is False and res["reason"] == "lock_snapshot_stale"
    r = row(db, wid)
    assert r["claimer"] == "dave"
    assert r["expires_at"] == (NOW + timedelta(seconds=1800)).isoformat()

def test_fetch_claim_state_missing(db):
    assert claim_store.fetch_claim_state(db, 999) is None

def test_sweep_expired_releases_only_dead_locks(db):
    dead = add_wish(db, "claimed", "a", NOW.isoformat(), (NOW - timedelta(seconds=1)).isoformat())
    alive = add_wish(db, "claimed", "b", NOW.isoformat(), (NOW + timedelta(seconds=60)).isoformat())
    plain = add_wish(db)
    assert claim_store.sweep_expired(db, NOW) == 1
    db.commit()
    r = row(db, dead)
    assert r["status"] == "open" and r["claimer"] is None and r["claimed_at"] is None and r["expires_at"] is None
    assert row(db, alive)["claimer"] == "b"
    assert row(db, plain)["status"] == "open"

def test_release_lock_reads_back_released(db):
    wid = add_wish(db, "claimed", "alice", NOW.isoformat(), (NOW + timedelta(seconds=60)).isoformat())
    assert claim_store.release_lock(db, wid) is True
    db.commit()
    r = row(db, wid)
    assert r["status"] == "released" and r["claimer"] is None and r["expires_at"] is None
    assert claim_store.release_lock(db, wid) is False

def test_mark_fulfilled_keeps_claimer_record(db):
    wid = add_wish(db, "claimed", "alice", NOW.isoformat(), (NOW + timedelta(seconds=60)).isoformat())
    assert claim_store.mark_fulfilled(db, wid) is True
    db.commit()
    r = row(db, wid)
    assert r["status"] == "fulfilled" and r["claimer"] == "alice"
    open_wid = add_wish(db)
    assert claim_store.mark_fulfilled(db, open_wid) is False
