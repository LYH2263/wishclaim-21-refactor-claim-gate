"""Orchestration tests: routes fetch -> pure verdict -> conditional write,
and translate the outcome to the existing HTTP codes. Routes are called
directly (no TestClient/httpx dependency).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import main
from app.db import connect
from app.modules import claim_store as store

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(main, "now", lambda: NOW)


def make_wish(c, status, claimer=None, expires_at=None):
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
        " VALUES (?,?,?,?,?,?,?)",
        ("t", "", status, claimer, None, expires_at, "clean"),
    )
    c.commit()
    return cur.lastrowid


def readback(wid):
    c = connect()
    row = dict(c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone())
    c.close()
    return row


def test_claim_open_row_returns_payload(db):
    wid = make_wish(db, "open")
    p = main.claim(wid, main.ClaimIn(claimer="carol"))
    assert p["status"] == "claimed" and p["claimer"] == "carol"
    row = readback(wid)
    assert row["claimer"] == "carol" and row["status"] == "claimed"


def test_claim_twice_within_ttl_is_conflict(db):
    wid = make_wish(db, "open")
    main.claim(wid, main.ClaimIn(claimer="alice"))
    with pytest.raises(HTTPException) as ei:
        main.claim(wid, main.ClaimIn(claimer="carol"))
    assert ei.value.status_code == 409 and ei.value.detail == "locked"
    assert readback(wid)["claimer"] == "alice"


def test_claim_after_expiry_succeeds(db):
    wid = make_wish(db, "claimed", "alice", (NOW - timedelta(seconds=1)).isoformat())
    main.claim(wid, main.ClaimIn(claimer="carol"))
    assert readback(wid)["claimer"] == "carol"


def test_claim_fulfilled_is_already_fulfilled(db):
    wid = make_wish(db, "fulfilled", "alice", (NOW - timedelta(days=1)).isoformat())
    with pytest.raises(HTTPException) as ei:
        main.claim(wid, main.ClaimIn(claimer="carol"))
    assert ei.value.status_code == 409 and ei.value.detail == "already_fulfilled"


def test_claim_malformed_row_is_bad_status(db):
    wid = make_wish(db, "claimed", "", (NOW - timedelta(minutes=1)).isoformat())
    with pytest.raises(HTTPException) as ei:
        main.claim(wid, main.ClaimIn(claimer="carol"))
    assert ei.value.status_code == 409 and ei.value.detail == "bad_status"


def test_claim_loses_race_when_lock_refreshed_between_verdict_and_write(db, monkeypatch):
    # Target row holds a valid (future) lock. The verdict step is handed a stale
    # snapshot showing it expired, so the decision passes — then the real
    # conditional UPDATE sees the future lock and must refuse the whole claim.
    target = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat())
    stale_id = make_wish(db, "claimed", "alice", (NOW - timedelta(minutes=1)).isoformat())
    stale_row = store.find(db, stale_id)
    real_find = store.find
    monkeypatch.setattr(store, "find",
                        lambda c, wid: stale_row if wid == target else real_find(c, wid))

    with pytest.raises(HTTPException) as ei:
        main.claim(target, main.ClaimIn(claimer="carol"))
    assert ei.value.status_code == 409 and ei.value.detail == "locked"
    row = readback(target)
    assert row["claimer"] == "alice" and row["status"] == "claimed"


def test_claim_missing_is_404(db):
    with pytest.raises(HTTPException) as ei:
        main.claim(999999, main.ClaimIn(claimer="carol"))
    assert ei.value.status_code == 404


def test_empty_claimer_rejected_by_model():
    with pytest.raises(ValidationError):
        main.ClaimIn(claimer="")


def test_release_claimed_then_open_release_is_400(db):
    wid = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat())
    assert main.release(wid) == {"ok": True, "status": "released"}
    assert readback(wid)["status"] == "released"
    with pytest.raises(HTTPException) as ei:
        main.release(wid)  # already released -> not claimed
    assert ei.value.status_code == 400 and ei.value.detail == "not_claimed"


def test_release_missing_is_404(db):
    with pytest.raises(HTTPException) as ei:
        main.release(999999)
    assert ei.value.status_code == 404


def test_fulfill_path_and_400(db):
    wid = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat())
    assert main.fulfill(wid) == {"ok": True, "status": "fulfilled"}
    assert readback(wid)["status"] == "fulfilled"
    with pytest.raises(HTTPException) as ei:
        main.fulfill(wid)
    assert ei.value.status_code == 400 and ei.value.detail == "need_claim"


def test_fulfill_missing_is_404(db):
    with pytest.raises(HTTPException) as ei:
        main.fulfill(999999)
    assert ei.value.status_code == 404


def test_read_views_sweep_and_serialise(db):
    expired = make_wish(db, "claimed", "ghost", "2020-01-01T01:00:00+00:00")
    rows = {r["id"]: r for r in main.list_wishes()}
    assert rows[expired]["status"] == "open" and rows[expired]["claimer"] is None
    detail = main.get_wish(expired)
    assert detail["status"] == "open"
    assert expired not in [r["id"] for r in main.mine("ghost")]
