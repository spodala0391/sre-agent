"""In-memory incident + model-usage store. Swap for SQLite/Postgres if you need persistence."""
import threading
import time
import uuid


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self.incidents: dict[str, dict] = {}
        self.calls: list[dict] = []

    # ---- incidents -------------------------------------------------------
    def new_incident(self, alert: dict) -> dict:
        inc = {
            "id": uuid.uuid4().hex[:8],
            "alert": alert,
            "status": "triaging",       # triaging|investigating|escalated|awaiting_approval|remediated|dismissed|failed
            "created_at": time.time(),
            "resolved_at": None,
            "timeline": [],
            "diagnosis": None,
            "proposed_action": None,
            "action_result": None,
        }
        with self._lock:
            self.incidents[inc["id"]] = inc
        return inc

    def event(self, incident_id: str, stage: str, detail: str, tier: str | None = None):
        with self._lock:
            inc = self.incidents.get(incident_id)
            if inc:
                inc["timeline"].append({"t": time.time(), "stage": stage, "tier": tier, "detail": detail})

    def update(self, incident_id: str, **fields):
        with self._lock:
            if incident_id in self.incidents:
                self.incidents[incident_id].update(fields)

    def get(self, incident_id: str) -> dict | None:
        return self.incidents.get(incident_id)

    def list(self) -> list[dict]:
        return sorted(self.incidents.values(), key=lambda i: i["created_at"], reverse=True)

    # ---- model usage -----------------------------------------------------
    def log_call(self, **row):
        row["t"] = time.time()
        with self._lock:
            self.calls.append(row)

    def usage_summary(self) -> dict:
        out: dict[str, dict] = {}
        for c in self.calls:
            s = out.setdefault(c["tier"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "avg_latency_ms": 0})
            s["calls"] += 1
            s["prompt_tokens"] += c["prompt_tokens"]
            s["completion_tokens"] += c["completion_tokens"]
            s["avg_latency_ms"] += c["latency_ms"]
        for s in out.values():
            s["avg_latency_ms"] = int(s["avg_latency_ms"] / max(s["calls"], 1))
        return out


store = Store()
