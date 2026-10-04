"""Claim mutex + TTL release for wishes. Pure decision module: no DB, no clock reads."""
from datetime import datetime, timedelta, timezone

def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def claim_allowed(status: str, claimer: str | None, now: datetime, expires_at: str | None) -> dict:
    """Only open wishes (or expired locks) can be claimed."""
    if status == "fulfilled":
        return {"ok": False, "reason": "already_fulfilled"}
    if status == "claimed" and claimer:
        if expires_at and parse_ts(expires_at) <= now:
            return {"ok": True, "reason": "ttl_expired_reclaim"}
        return {"ok": False, "reason": "locked"}
    if status in ("open", "released"):
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": "bad_status"}

def lock_payload(claimer: str, now: datetime, ttl_seconds: int) -> dict:
    exp = now + timedelta(seconds=ttl_seconds)
    return {
        "status": "claimed",
        "claimer": claimer,
        "claimed_at": now.isoformat(),
        "expires_at": exp.isoformat(),
    }

def release_if_expired(status: str, expires_at: str | None, now: datetime) -> dict | None:
    if status != "claimed" or not expires_at:
        return None
    if parse_ts(expires_at) <= now:
        return {"status": "open", "claimer": None, "claimed_at": None, "expires_at": None}
    return None

def lock_alive(expires_at: str, now: datetime) -> bool:
    """A lock is alive only while its expiry is strictly in the future.

    Exact negation of the expiry check in release_if_expired (expires_at <= now
    is dead), so the write-side guard and the sweep share one boundary: a lock
    expiring exactly at `now` is dead.
    """
    return parse_ts(expires_at) > now
