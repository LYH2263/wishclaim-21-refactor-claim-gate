from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from app import seed
from app.db import connect
from app.engines.claim_lock import claim_allowed, lock_payload
from app.modules import claim_store as store

app = FastAPI(title="Wishclaim", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

def now(): return datetime.now(timezone.utc)

@app.get("/api/health")
def health(): return {"ok": True, "project": "wishclaim"}

@app.get("/api/wishes")
def list_wishes():
    c = connect()
    rows = [dict(r) for r in store.list_wishes(c, now())]
    c.commit(); c.close(); return rows

@app.get("/api/wishes/{wid}")
def get_wish(wid: int):
    c = connect()
    r = store.get_wish(c, wid, now()); c.commit(); c.close()
    if not r: raise HTTPException(404, "not found")
    return dict(r)

class WishIn(BaseModel):
    title: str
    note: str = ""

@app.post("/api/wishes")
def create_wish(body: WishIn):
    c = connect()
    cur = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    (body.title, body.note, "open", "clean"))
    c.commit(); wid = cur.lastrowid; c.close(); return {"id": wid}

class ClaimIn(BaseModel):
    claimer: str = Field(min_length=1)

@app.post("/api/wishes/{wid}/claim")
def claim(wid: int, body: ClaimIn):
    c = connect(); clock = now()
    r = store.find(c, wid)
    if not r:
        c.close(); raise HTTPException(404, "not found")
    verdict = claim_allowed(r["status"], r["claimer"], clock, r["expires_at"])
    if not verdict["ok"]:
        c.close(); raise HTTPException(409, verdict["reason"])
    payload = lock_payload(body.claimer, clock, store.get_ttl(c))
    if store.acquire(c, wid, payload) == 0:
        # Decision passed, but the row changed before the write: another valid
        # lock won the race. Fail the whole claim; the existing row is intact.
        c.close(); raise HTTPException(409, "locked")
    c.commit(); c.close(); return payload

@app.post("/api/wishes/{wid}/release")
def release(wid: int):
    c = connect()
    if store.release(c, wid) == 1:
        c.commit(); c.close(); return {"ok": True, "status": "released"}
    r = store.find(c, wid); c.close()
    if not r: raise HTTPException(404, "not found")
    raise HTTPException(400, "not_claimed")

@app.post("/api/wishes/{wid}/fulfill")
def fulfill(wid: int):
    c = connect()
    if store.fulfill(c, wid) == 1:
        c.commit(); c.close(); return {"ok": True, "status": "fulfilled"}
    r = store.find(c, wid); c.close()
    if not r: raise HTTPException(404, "not found")
    raise HTTPException(400, "need_claim")

@app.get("/api/mine")
def mine(claimer: str):
    c = connect()
    rows = [dict(r) for r in store.list_mine(c, claimer, now())]
    c.commit(); c.close(); return rows

@app.get("/api/done")
def done():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE status='fulfilled'")]; c.close()
    return rows

@app.get("/api/settings")
def settings():
    c = connect()
    rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close()
    return rows

@app.get("/api/rules")
def rules():
    return {
        "mutex": "同一愿望同时只能被一人认领",
        "ttl": "认领超时未核销则自动释放",
        "fulfill": "核销后状态变为 fulfilled",
    }
