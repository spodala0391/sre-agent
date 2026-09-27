"""Tiered incident pipeline:  Nano (triage) -> Super (investigate w/ tools) -> Ultra (deep RCA, only if needed)."""
import json
import time

from . import config
from .llm import chat, parse_json
from .store import store
from .tools import READ_TOOL_SCHEMAS, READ_TOOLS, WRITE_TOOLS, WRITE_TOOL_DOCS, run_tool

TRIAGE_PROMPT = """You are an SRE alert triager. Classify the alert.
Return ONLY JSON: {"verdict": "noise"|"known"|"investigate", "severity": "low"|"medium"|"high",
"summary": "<one line>", "suspect": {"namespace": "...", "deployment": "..."}}
"noise" = informational/flapping/self-resolved. When unsure, choose "investigate"."""

INVESTIGATE_PROMPT = f"""You are a Kubernetes SRE investigating a live incident. Use the tools to gather evidence.
Strategy: get_pods -> logs (previous=true for crashed containers) -> events -> recent_changes -> metrics.
Only namespaces {config.TARGET_NAMESPACES} are in scope. Be efficient: stop once the cause is clear.

{WRITE_TOOL_DOCS}

When done, reply with ONLY JSON:
{{"root_cause": "...", "evidence": ["..."], "confidence": 0.0-1.0, "multi_component": true|false,
 "action": {{"tool": "<name or none>", "args": {{...}}}}, "rationale": "..."}}"""

DEEP_RCA_PROMPT = f"""You are a principal SRE. A junior investigator gathered evidence but is not confident.
Reason carefully across all components, consider alternative hypotheses, and pick the most likely root cause.
Prefer the least-risky remediation that fixes the cause (rollback beats restart when a recent change is implicated).

{WRITE_TOOL_DOCS}

Reply with ONLY JSON:
{{"root_cause": "...", "evidence": ["..."], "confidence": 0.0-1.0, "alternatives_ruled_out": ["..."],
 "action": {{"tool": "<name or none>", "args": {{...}}}}, "rationale": "..."}}"""


def _alert_text(alert: dict) -> str:
    return json.dumps(alert, default=str)[:3000]


# ---------------------------------------------------------------- stage 1: Nano
def triage(inc_id: str, alert: dict) -> dict:
    msg = chat(config.MODEL_NANO, [{"role": "system", "content": TRIAGE_PROMPT},
                                   {"role": "user", "content": _alert_text(alert)}],
               incident_id=inc_id, max_tokens=300)
    result = parse_json(msg.content) or {"verdict": "investigate", "summary": "unparseable triage"}
    store.event(inc_id, "triage", f"{result.get('verdict')} / {result.get('severity')}: {result.get('summary')}", "nano")
    return result


# ---------------------------------------------------------------- stage 2: Super (tool loop)
def investigate(inc_id: str, alert: dict, triage_result: dict) -> tuple[dict, list]:
    messages = [
        {"role": "system", "content": INVESTIGATE_PROMPT},
        {"role": "user", "content": f"ALERT:\n{_alert_text(alert)}\n\nTRIAGE:\n{json.dumps(triage_result)}"},
    ]
    evidence_log = []
    for _ in range(config.MAX_TOOL_ROUNDS):
        msg = chat(config.MODEL_SUPER, messages, incident_id=inc_id, tools=READ_TOOL_SCHEMAS)
        if not msg.tool_calls:
            result = parse_json(msg.content)
            if result:
                return result, evidence_log
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": "Reply with the final JSON only."})
            continue
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            if tc.function.name not in READ_TOOLS:
                out = "write actions are not allowed during investigation; propose them in the final JSON"
            else:
                out = run_tool(tc.function.name, args)
            evidence_log.append({"tool": tc.function.name, "args": args, "output": out[:1500]})
            store.event(inc_id, "tool", f"{tc.function.name}({json.dumps(args)})", "super")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

    # ran out of rounds: force a conclusion
    messages.append({"role": "user", "content": "Tool budget exhausted. Give your final JSON now."})
    msg = chat(config.MODEL_SUPER, messages, incident_id=inc_id)
    return parse_json(msg.content) or {"root_cause": "undetermined", "confidence": 0.0,
                                       "action": {"tool": "none", "args": {}}}, evidence_log


# ---------------------------------------------------------------- stage 3: Ultra
def deep_rca(inc_id: str, alert: dict, diag: dict, evidence_log: list) -> dict:
    ctx = {"alert": alert, "investigator_conclusion": diag, "evidence": evidence_log}
    msg = chat(config.MODEL_ULTRA, [{"role": "system", "content": DEEP_RCA_PROMPT},
                                    {"role": "user", "content": json.dumps(ctx, default=str)[:30000]}],
               incident_id=inc_id, max_tokens=4096)
    return parse_json(msg.content) or diag


# ---------------------------------------------------------------- actions
def execute_action(inc_id: str) -> str:
    inc = store.get(inc_id)
    action = (inc or {}).get("proposed_action") or {}
    tool = action.get("tool")
    if tool not in WRITE_TOOLS:
        return "no executable action"
    result = run_tool(tool, action.get("args", {}))
    ok = not result.startswith(("tool error", "namespace", "no "))
    store.update(inc_id, action_result=result, status="remediated" if ok else "failed",
                 resolved_at=time.time() if ok else None)
    store.event(inc_id, "action", result, None)
    return result


# ---------------------------------------------------------------- pipeline
def handle_alert(alert: dict) -> str:
    inc = store.new_incident(alert)
    inc_id = inc["id"]
    try:
        t = triage(inc_id, alert)
        if t.get("verdict") == "noise":
            store.update(inc_id, status="dismissed", resolved_at=time.time())
            return inc_id

        store.update(inc_id, status="investigating")
        diag, evidence = investigate(inc_id, alert, t)
        store.event(inc_id, "diagnosis", f"{diag.get('root_cause')} (conf={diag.get('confidence')})", "super")

        conf = float(diag.get("confidence") or 0)
        if conf < config.ESCALATION_CONFIDENCE or diag.get("multi_component"):
            store.update(inc_id, status="escalated")
            store.event(inc_id, "escalate", f"confidence {conf} < {config.ESCALATION_CONFIDENCE} or multi-component", "super")
            diag = deep_rca(inc_id, alert, diag, evidence)
            store.event(inc_id, "deep_rca", f"{diag.get('root_cause')} (conf={diag.get('confidence')})", "ultra")

        action = diag.get("action") or {"tool": "none", "args": {}}
        store.update(inc_id, diagnosis=diag, proposed_action=action)

        if action.get("tool") in WRITE_TOOLS:
            if config.AUTO_REMEDIATE:
                execute_action(inc_id)
            else:
                store.update(inc_id, status="awaiting_approval")
                store.event(inc_id, "approval", f"waiting for human approval: {action}", None)
        else:
            store.update(inc_id, status="awaiting_approval")
            store.event(inc_id, "recommendation", diag.get("rationale", "manual follow-up required"), None)
    except Exception as e:
        store.update(inc_id, status="failed")
        store.event(inc_id, "error", f"{type(e).__name__}: {e}", None)
    return inc_id
