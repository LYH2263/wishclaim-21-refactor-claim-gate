"""Pure decision tests — zero DB, zero clock reads, no app.db import."""
from datetime import datetime, timedelta, timezone
from app.engines.claim_lock import claim_allowed, lock_alive, lock_payload, release_if_expired

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(hours=1)).isoformat()
PAST = (NOW - timedelta(minutes=1)).isoformat()

def test_fulfilled_never_claimable():
    r = claim_allowed("fulfilled", "alice", NOW, FUTURE)
    assert r["ok"] is False and r["reason"] == "already_fulfilled"

def test_open_and_released_claimable():
    assert claim_allowed("open", None, NOW, None)["ok"] is True
    assert claim_allowed("released", None, NOW, None)["ok"] is True

def test_claimed_without_claimer_is_bad_status():
    r = claim_allowed("claimed", None, NOW, FUTURE)
    assert r["ok"] is False and r["reason"] == "bad_status"

def test_unknown_status_is_bad_status():
    assert claim_allowed("archived", None, NOW, None)["reason"] == "bad_status"

def test_lock_expiring_exactly_now_is_dead():
    # Boundary shared with the sweep: expires_at == now means expired.
    r = claim_allowed("claimed", "alice", NOW, NOW.isoformat())
    assert r["ok"] is True and r["reason"] == "ttl_expired_reclaim"
    assert lock_alive(NOW.isoformat(), NOW) is False

def test_lock_alive_boundary():
    assert lock_alive(FUTURE, NOW) is True
    assert lock_alive(PAST, NOW) is False
    assert lock_alive((NOW + timedelta(microseconds=1)).isoformat(), NOW) is True

def test_lock_alive_mirrors_release_if_expired():
    # One expiry semantics for both the write guard and the sweep.
    for delta in (-3600, -1, 0, 1, 3600):
        exp = (NOW + timedelta(seconds=delta)).isoformat()
        assert lock_alive(exp, NOW) is (release_if_expired("claimed", exp, NOW) is None)

def test_lock_payload_fields():
    p = lock_payload("bob", NOW, 3600)
    assert p == {
        "status": "claimed",
        "claimer": "bob",
        "claimed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=3600)).isoformat(),
    }

def test_release_if_expired_only_touches_dead_claims():
    assert release_if_expired("open", PAST, NOW) is None
    assert release_if_expired("claimed", None, NOW) is None
    assert release_if_expired("claimed", FUTURE, NOW) is None
    rel = release_if_expired("claimed", PAST, NOW)
    assert rel == {"status": "open", "claimer": None, "claimed_at": None, "expires_at": None}

def test_parse_handles_z_suffix_and_naive():
    assert lock_alive("2026-01-01T13:00:00Z", NOW) is True
    assert lock_alive("2026-01-01T13:00:00", NOW) is True
