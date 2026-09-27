"""FastAPI service: Alertmanager webhook, approval endpoint, postmortems, dashboard API."""
import hmac
import pathlib

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from . import config
from .router import execute_action, handle_alert
from .store import store

app = FastAPI(title="Self-Healing SRE Agent")
STATIC = pathlib.Path(__file__).parent / "static"


def require_token(request: Request):
    """When AGENT_TOKEN is set, every call that changes state must present it.
    Accepted as 'Authorization: Bearer <token>' (Alertmanager, jobs) or 'X-Agent-Token' (dashboard)."""
    if not config.AGENT_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    supplied = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-agent-token", "")
    if not hmac.compare_digest(supplied, config.AGENT_TOKEN):
        raise HTTPException(401, "missing or invalid token")


@app.get("/healthz")
def healthz():
    missing = [k for k, v in {"NEBIUS_API_KEY": config.NEBIUS_API_KEY, "M_NANO": config.MODEL_NANO,
                              "M_SUPER": config.MODEL_SUPER, "M_ULTRA": config.MODEL_ULTRA}.items() if not v]
    return {"ok": not missing, "missing_config": missing, "dry_run": config.DRY_RUN,
            "auth_required": bool(config.AGENT_TOKEN)}


@app.post("/alert", dependencies=[Depends(require_token)])
async def alert(request: Request, bg: BackgroundTasks):
    """Alertmanager webhook (v4 payload). Each firing alert becomes an incident."""
    body = await request.json()
    alerts = body.get("alerts", [body])  # also accept a single raw alert for manual testing
    queued = 0
    for a in alerts:
        if a.get("status", "firing") != "firing":
            continue
        bg.add_task(handle_alert, {"labels": a.get("labels", {}), "annotations": a.get("annotations", {}),
                                   "startsAt": a.get("startsAt")})
        queued += 1
    return {"queued": queued}


@app.post("/incidents/{inc_id}/approve", dependencies=[Depends(require_token)])
def approve(inc_id: str):
    inc = store.get(inc_id)
    if not inc:
        raise HTTPException(404)
    if inc["status"] != "awaiting_approval":
        raise HTTPException(409, f"incident is {inc['status']}")
    return {"result": execute_action(inc_id)}


@app.post("/incidents/{inc_id}/reject", dependencies=[Depends(require_token)])
def reject(inc_id: str):
    if not store.get(inc_id):
        raise HTTPException(404)
    store.update(inc_id, status="dismissed")
    store.event(inc_id, "rejected", "human rejected proposed action", None)
    return {"ok": True}


@app.post("/incidents/{inc_id}/postmortem", dependencies=[Depends(require_token)])
async def put_postmortem(inc_id: str, request: Request):
    """Called by the Nebius Serverless Job (jobs/rca_job.py) with a markdown report."""
    if not store.get(inc_id):
        raise HTTPException(404)
    md = (await request.body()).decode("utf-8", "replace")
    store.update(inc_id, postmortem=md)
    store.event(inc_id, "postmortem", "blameless postmortem attached by Serverless Job", "ultra")
    return {"ok": True}


@app.get("/incidents/{inc_id}/postmortem", response_class=PlainTextResponse)
def get_postmortem(inc_id: str):
    inc = store.get(inc_id)
    if not inc or not inc.get("postmortem"):
        raise HTTPException(404)
    return inc["postmortem"]


@app.get("/incidents")
def incidents():
    return store.list()


@app.get("/usage")
def usage():
    return store.usage_summary()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
