"""Real-DB readback tests for the claim persistence module.

Every mutation is committed and re-read through a separate connection, so
these assert what is actually in the database, not what the function returns.
The pure decision in engines.claim_lock stays DB-free and has its own file.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db import connect
from app.engines.claim_lock import claim_allowed, lock_payload, parse_ts
from app.modules import claim_store as store

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
TTL = 3600


def make_wish(c, status, claimer, expires_at, *, claimed_at=None, title="t"):
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
        " VALUES (?,?,?,?,?,?,?)",
        (title, "", status, claimer, claimed_at, expires_at, "clean"),
    )
    c.commit()
    return cur.lastrowid


def readback(wid):
    c = connect()
    row = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    c.close()
    return None if row is None else dict(row)


def ghost_id(c):
    return c.execute("SELECT id FROM wishes WHERE claimer='ghost'").fetchone()["id"]


def payload(claimer="carol"):
    return lock_payload(claimer, NOW, TTL)


# ---------- main-path regressions ----------

def test_open_row_can_be_locked_and_values_read_back(db):
    wid = make_wish(db, "open", None, None)
    assert store.acquire(db, wid, payload("carol")) == 1
    db.commit()
    row = readback(wid)
    assert row["status"] == "claimed" and row["claimer"] == "carol"
    assert row["claimed_at"] == NOW.isoformat()
    assert parse_ts(row["expires_at"]) == NOW + timedelta(seconds=TTL)


def test_locked_within_ttl_blocks_others_and_keeps_existing_lock(db):
    wid = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat())
    assert store.acquire(db, wid, payload("carol")) == 0
    db.commit()
    row = readback(wid)
    assert row["claimer"] == "alice"
    assert row["expires_at"] == (NOW + timedelta(hours=1)).isoformat()


def test_expired_lock_can_be_taken_by_someone_else(db):
    wid = make_wish(db, "claimed", "alice", (NOW - timedelta(seconds=1)).isoformat())
    assert store.acquire(db, wid, payload("carol")) == 1
    db.commit()
    row = readback(wid)
    assert row["claimer"] == "carol"
    assert parse_ts(row["expires_at"]) == NOW + timedelta(seconds=TTL)


def test_released_row_can_be_locked_again(db):
    wid = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat())
    assert store.release(db, wid) == 1
    db.commit()
    assert readback(wid)["status"] == "released"
    assert store.acquire(db, wid, payload("bob")) == 1
    db.commit()
    row = readback(wid)
    assert row["status"] == "claimed" and row["claimer"] == "bob"


# ---------- expiry-boundary sentinels ----------

def test_equal_instant_due_despite_fraction_width_mismatch(db):
    # Same instant, but the stored value carries the .000000 fraction while the
    # request clock is a whole second. Raw string ordering gets this wrong;
    # julianday normalisation must treat them as equal -> due (<=).
    wid = make_wish(db, "claimed", "alice", "2026-01-01T12:00:00.000000+00:00")
    assert store.acquire(db, wid, payload("carol")) == 1
    db.commit()
    assert readback(wid)["claimer"] == "carol"


def test_reverse_fraction_width_also_due(db):
    wid = make_wish(db, "claimed", "alice", "2026-01-01T12:00:00+00:00")
    assert store.acquire(db, wid, payload("carol")) == 1
    db.commit()
    assert readback(wid)["claimer"] == "carol"


def test_seed_row_with_fractionless_timestamp_is_reclaimable(db):
    # Seeded ghost lock (2020, no fractional seconds) must read back as taken.
    wid = ghost_id(db)
    assert store.acquire(db, wid, payload("carol")) == 1
    db.commit()
    row = readback(wid)
    assert row["claimer"] == "carol" and row["status"] == "claimed"


def test_z_suffix_timestamp_uses_same_due_semantics(db):
    wid = make_wish(db, "claimed", "alice", "2020-01-01T01:00:00Z")
    assert store.acquire(db, wid, payload("carol")) == 1
    db.commit()
    assert readback(wid)["claimer"] == "carol"


# ---------- malformed / edge rows: conservative permanent lock ----------

def test_null_expires_at_is_a_permanent_lock(db):
    wid = make_wish(db, "claimed", "pat", None)
    assert store.acquire(db, wid, payload("carol")) == 0
    store.sweep_due(db, NOW)
    db.commit()
    row = readback(wid)
    assert row["claimer"] == "pat" and row["expires_at"] is None and row["status"] == "claimed"
    assert [r["id"] for r in store.list_mine(db, "pat", NOW)] == [wid]


def test_empty_claimer_expired_row_is_not_reclaimable_or_swept(db):
    wid = make_wish(db, "claimed", "", (NOW - timedelta(minutes=1)).isoformat())
    assert store.acquire(db, wid, payload("carol")) == 0
    store.sweep_due(db, NOW)
    db.commit()
    row = readback(wid)
    assert row["status"] == "claimed" and row["claimer"] == ""
    assert wid not in [r["id"] for r in store.list_mine(db, "carol", NOW)]
    assert wid not in [r["id"] for r in store.list_mine(db, "ghost", NOW)]


def test_garbage_expires_at_fails_closed(db):
    # parse_ts() would raise here; the SQL predicate must fail closed instead.
    wid = make_wish(db, "claimed", "alice", "not-a-timestamp")
    assert store.acquire(db, wid, payload("carol")) == 0
    store.sweep_due(db, NOW)
    db.commit()
    assert readback(wid)["claimer"] == "alice" and readback(wid)["status"] == "claimed"


def test_fulfilled_row_cannot_be_locked(db):
    wid = make_wish(db, "fulfilled", "alice", (NOW - timedelta(days=1)).isoformat())
    assert store.acquire(db, wid, payload("carol")) == 0
    db.commit()
    assert readback(wid)["status"] == "fulfilled"


def test_missing_id_returns_zero_for_all_writers(db):
    assert store.acquire(db, 999999, payload()) == 0
    assert store.release(db, 999999) == 0
    assert store.fulfill(db, 999999) == 0


# ---------- release / fulfill readback ----------

def test_release_clears_lock_fields(db):
    wid = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat())
    assert store.release(db, wid) == 1
    db.commit()
    row = readback(wid)
    assert row["status"] == "released"
    assert row["claimer"] is None and row["claimed_at"] is None and row["expires_at"] is None


def test_release_only_on_claimed(db):
    open_wid = make_wish(db, "open", None, None)
    done_wid = make_wish(db, "fulfilled", "alice", None)
    assert store.release(db, open_wid) == 0
    assert store.release(db, done_wid) == 0


def test_fulfill_preserves_lock_fields(db):
    wid = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat(),
                    claimed_at=(NOW - timedelta(minutes=5)).isoformat())
    assert store.fulfill(db, wid) == 1
    db.commit()
    row = readback(wid)
    assert row["status"] == "fulfilled"
    assert row["claimer"] == "alice" and row["expires_at"] is not None
    assert store.fulfill(db, wid) == 0  # already fulfilled


# ---------- sweep matrix ----------

def test_sweep_only_releases_healthy_due_locks(db):
    future = make_wish(db, "claimed", "f", (NOW + timedelta(hours=1)).isoformat())
    due = make_wish(db, "claimed", "d", (NOW - timedelta(minutes=1)).isoformat())
    forever = make_wish(db, "claimed", "p", None)
    blank = make_wish(db, "claimed", "", (NOW - timedelta(minutes=1)).isoformat())
    open_row = make_wish(db, "open", None, None)
    done = make_wish(db, "fulfilled", "x", None)

    store.sweep_due(db, NOW)
    db.commit()

    assert readback(due)["status"] == "open"
    assert readback(due)["claimer"] is None and readback(due)["expires_at"] is None
    assert readback(future)["status"] == "claimed" and readback(future)["claimer"] == "f"
    assert readback(forever)["status"] == "claimed" and readback(forever)["claimer"] == "p"
    assert readback(blank)["status"] == "claimed" and readback(blank)["claimer"] == ""
    assert readback(open_row)["status"] == "open"
    assert readback(done)["status"] == "fulfilled"


# ---------- wall / detail / mine share one policy ----------

def test_three_read_views_agree(db):
    expired = ghost_id(db)  # seeded 2020 lock, claimer='ghost'
    future = make_wish(db, "claimed", "alice", (NOW + timedelta(hours=1)).isoformat())
    forever = make_wish(db, "claimed", "pat", None)

    wall = {r["id"]: dict(r) for r in store.list_wishes(db, NOW)}
    db.commit()
    detail_expired = dict(store.get_wish(db, expired, NOW))
    db.commit()
    mine_ghost = [r["id"] for r in store.list_mine(db, "ghost", NOW)]
    mine_alice = [r["id"] for r in store.list_mine(db, "alice", NOW)]
    mine_pat = [r["id"] for r in store.list_mine(db, "pat", NOW)]
    db.commit()

    # Due lock: wall card has no claimer, detail is open, it is in nobody's mine.
    assert wall[expired]["status"] == "open" and wall[expired]["claimer"] is None
    assert detail_expired["status"] == "open" and detail_expired["claimer"] is None
    assert expired not in mine_ghost
    # Valid lock: claimed by alice in all three views.
    assert wall[future]["status"] == "claimed" and wall[future]["claimer"] == "alice"
    assert dict(store.get_wish(db, future, NOW))["claimer"] == "alice"
    assert future in mine_alice
    # Null-expiry permanent lock survives the sweep and stays in pat's mine.
    assert wall[forever]["status"] == "claimed" and forever in mine_pat


# ---------- write race ----------

def test_write_loses_when_another_connection_refreshes_lock_first(db):
    wid = make_wish(db, "claimed", "alice", (NOW - timedelta(minutes=1)).isoformat())

    racer = connect()
    racer.execute("UPDATE wishes SET claimer='zoe', expires_at=? WHERE id=?",
                  ((NOW + timedelta(hours=1)).isoformat(), wid))
    racer.commit(); racer.close()

    assert store.acquire(db, wid, payload("carol")) == 0
    db.commit()
    row = readback(wid)
    assert row["claimer"] == "zoe"  # original (refreshed) lock untouched by loser


# ---------- SQL predicate must not drift from the pure decision ----------

@pytest.mark.parametrize("status,claimer,expires_at,ok,reason", [
    ("open", None, None, True, ""),
    ("released", None, None, True, ""),
    ("claimed", "alice", (NOW + timedelta(hours=1)).isoformat(), False, "locked"),
    ("claimed", "alice", (NOW - timedelta(minutes=1)).isoformat(), True, "ttl_expired_reclaim"),
    ("claimed", "alice", NOW.isoformat(), True, "ttl_expired_reclaim"),  # exact equality is due
    ("claimed", "alice", None, False, "locked"),
    ("claimed", "", (NOW - timedelta(minutes=1)).isoformat(), False, "bad_status"),
    ("claimed", None, (NOW - timedelta(minutes=1)).isoformat(), False, "bad_status"),
    ("fulfilled", "alice", (NOW - timedelta(days=1)).isoformat(), False, "already_fulfilled"),
    ("weird", "alice", None, False, "bad_status"),
])
def test_pure_verdict_matches_conditional_write(db, status, claimer, expires_at, ok, reason):
    wid = make_wish(db, status, claimer, expires_at)
    verdict = claim_allowed(status, claimer, NOW, expires_at)
    assert verdict["ok"] is ok and verdict["reason"] == reason
    written = store.acquire(db, wid, payload("carol")) == 1
    assert written is ok
    db.commit()
    if not ok:
        assert readback(wid)["claimer"] == claimer
