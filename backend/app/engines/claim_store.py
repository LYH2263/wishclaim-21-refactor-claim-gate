"""Persistence for the claim chain: every write of claimer/claimed_at/expires_at
lives here. Routes orchestrate; they never parse expiry timestamps themselves.

Expiry edge ruling (拍板): if the decision passed but at write-lock moment the
clock (new expires_at not in the future) or the in-DB expires_at (snapshot no
longer matches) says the lock cannot stand, the whole claim FAILS and the
original lock in the DB is kept untouched. No partial writes, no clobbering.
"""
from datetime import datetime

from app.engines.claim_lock import lock_alive, lock_payload, release_if_expired

def fetch_claim_state(c, wid: int) -> dict | None:
    """Snapshot the decision-relevant fields, or None if the wish is missing."""
    r = c.execute("SELECT id, status, claimer, expires_at FROM wishes WHERE id=?", (wid,)).fetchone()
    return dict(r) if r else None

def sweep_expired(c, now: datetime) -> int:
    """Release every claimed row whose TTL has expired. Returns release count."""
    n = 0
    rows = c.execute("SELECT id, status, expires_at FROM wishes WHERE status='claimed'").fetchall()
    for r in rows:
        rel = release_if_expired(r["status"], r["expires_at"], now)
        if rel:
            c.execute("UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
                      (rel["status"], rel["claimer"], rel["claimed_at"], rel["expires_at"], r["id"]))
            n += 1
    return n

def write_lock(c, wid: int, claimer: str, now: datetime, ttl_seconds: int, expected: dict) -> dict:
    """Persist a new lock, guarded twice against the expiry edge:

    1. clock side — the new expires_at must be alive at write moment, else the
       lock would be born dead and instantly swept;
    2. DB side — compare-and-swap on the exact snapshot the decision approved,
       so a concurrent claim or an in-DB expires_at that moved past `now`
       aborts the write.

    Either guard failing fails the whole claim and keeps the original lock.
    """
    p = lock_payload(claimer, now, ttl_seconds)
    if not lock_alive(p["expires_at"], now):
        return {"ok": False, "reason": "lock_expired_at_write"}
    cur = c.execute(
        "UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? "
        "WHERE id=? AND status IS ? AND claimer IS ? AND expires_at IS ?",
        (p["status"], p["claimer"], p["claimed_at"], p["expires_at"],
         wid, expected["status"], expected["claimer"], expected["expires_at"]))
    if cur.rowcount != 1:
        return {"ok": False, "reason": "lock_snapshot_stale"}
    return {"ok": True, "payload": p}

def release_lock(c, wid: int) -> bool:
    """Active unlock. True only if a held lock was actually released."""
    cur = c.execute(
        "UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL, expires_at=NULL "
        "WHERE id=? AND status='claimed'", (wid,))
    return cur.rowcount == 1

def mark_fulfilled(c, wid: int) -> bool:
    """Fulfill a held lock; claimer/expires_at stay as historical record."""
    cur = c.execute("UPDATE wishes SET status='fulfilled' WHERE id=? AND status='claimed'", (wid,))
    return cur.rowcount == 1
