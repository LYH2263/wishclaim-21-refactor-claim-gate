"""Persistence for wish claim locks.

All functions take an open sqlite3 connection and never open/commit/close it;
the route owns the transaction. No ``datetime.now`` lives here — the current
time is injected by the caller so the expiry policy is testable and single
sourced.

Expiry is the sole authority at write time: a single conditional UPDATE either
lands the new lock (rowcount 1) or leaves the existing row untouched
(rowcount 0). Read paths first run the same conditional sweep under the same
``now`` so the wall, detail and mine views never disagree.
"""
import sqlite3
from datetime import datetime

# Single source of truth for "this claimed lock is due". Shared by sweep_due
# and acquire — do not re-derive this predicate anywhere else. It mirrors the
# claimed+expired branch of engines.claim_lock.claim_allowed: claimed, with a
# real claimer, a parseable expires_at, and that instant <= now.
#
# julianday() normalises fractional/no-fractional seconds, Z suffix and
# offsets to one comparable number — raw ISO-string ordering is NOT safe
# because isoformat() drops the fraction on whole seconds
# ('...:00.000000+00:00' vs '...:00+00:00' are the same instant but sort
# wrong). Unparseable/NULL timestamps yield NULL -> predicate false, i.e. a
# conservative permanent lock, never an accidental release.
DUE = ("status='claimed' AND claimer IS NOT NULL AND claimer<>'' "
       "AND expires_at IS NOT NULL "
       "AND julianday(expires_at) IS NOT NULL "
       "AND julianday(expires_at) <= julianday(?)")

_WISH_COLS = "id, title, note, status, claimer, claimed_at, expires_at, data_quality"


def get_ttl(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT value FROM settings WHERE key='ttl_seconds'").fetchone()
    return int(row["value"] if row else 86400)


def find(c: sqlite3.Connection, wid: int) -> sqlite3.Row | None:
    return c.execute(f"SELECT {_WISH_COLS} FROM wishes WHERE id=?", (wid,)).fetchone()


def sweep_due(c: sqlite3.Connection, now: datetime) -> int:
    """Release every healthy claimed lock that is due at ``now``.

    Conditional UPDATE, so a lock refreshed into the future between the
    decision and the write is never wiped. Returns the number of rows reset.
    """
    cur = c.execute(
        "UPDATE wishes SET status='open', claimer=NULL, claimed_at=NULL, expires_at=NULL "
        f"WHERE {DUE}",
        (now.isoformat(),),
    )
    return cur.rowcount


def acquire(c: sqlite3.Connection, wid: int, payload: dict) -> int:
    """Atomically write a new lock iff the row is open/released or its current
    lock is due. Returns 1 when the lock was written, 0 when the row kept its
    existing (still valid / fulfilled / malformed) state.

    The cutoff is payload['claimed_at'] — the single ``now`` the route froze
    for the whole request.
    """
    cur = c.execute(
        "UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? "
        f"WHERE id=? AND (status IN ('open','released') OR ({DUE}))",
        (payload["status"], payload["claimer"], payload["claimed_at"], payload["expires_at"],
         wid, payload["claimed_at"]),
    )
    return cur.rowcount


def release(c: sqlite3.Connection, wid: int) -> int:
    """Voluntary unlock: only a claimed row can be released."""
    cur = c.execute(
        "UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL, expires_at=NULL "
        "WHERE id=? AND status='claimed'",
        (wid,),
    )
    return cur.rowcount


def fulfill(c: sqlite3.Connection, wid: int) -> int:
    """Mark claimed wish fulfilled; lock fields are intentionally preserved."""
    cur = c.execute(
        "UPDATE wishes SET status='fulfilled' WHERE id=? AND status='claimed'",
        (wid,),
    )
    return cur.rowcount


def list_wishes(c: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    sweep_due(c, now)
    return list(c.execute(f"SELECT {_WISH_COLS} FROM wishes ORDER BY id DESC"))


def get_wish(c: sqlite3.Connection, wid: int, now: datetime) -> sqlite3.Row | None:
    sweep_due(c, now)
    return find(c, wid)


def list_mine(c: sqlite3.Connection, claimer: str, now: datetime) -> list[sqlite3.Row]:
    sweep_due(c, now)
    return list(c.execute(
        f"SELECT {_WISH_COLS} FROM wishes WHERE claimer=? ORDER BY id DESC", (claimer,)))
