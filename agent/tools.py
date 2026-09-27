"""Kubernetes / Prometheus tools the agent can call.

READ tools run freely. WRITE tools are allowlisted and only execute after approval
(or when AUTO_REMEDIATE=true). Every tool returns a short string so it fits in context.
"""
import datetime as dt
import json

import requests

from . import config

_core = _apps = None


def _k8s():
    global _core, _apps
    if _core is None:
        from kubernetes import client, config as kcfg
        try:
            kcfg.load_incluster_config()
        except Exception:
            kcfg.load_kube_config()
        _core, _apps = client.CoreV1Api(), client.AppsV1Api()
    return _core, _apps


def _ns_ok(ns: str) -> bool:
    return ns in config.TARGET_NAMESPACES


def _trim(s: str, n: int = 4000) -> str:
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s) - n} chars]"


# --------------------------------------------------------------------------- READ tools
def get_pods(namespace: str, label_selector: str = "") -> str:
    if config.DRY_RUN:
        return "cartservice-7d9f  0/1  CrashLoopBackOff  restarts=6  lastState=OOMKilled\nfrontend-5c8b  1/1 Running restarts=0"
    core, _ = _k8s()
    pods = core.list_namespaced_pod(namespace, label_selector=label_selector or None).items
    lines = []
    for p in pods:
        cs = p.status.container_statuses or []
        ready = sum(1 for c in cs if c.ready)
        restarts = sum(c.restart_count for c in cs)
        reason = ""
        for c in cs:
            if c.state.waiting:
                reason = c.state.waiting.reason
            if c.last_state and c.last_state.terminated:
                reason += f" lastState={c.last_state.terminated.reason}"
        lines.append(f"{p.metadata.name}  {ready}/{len(cs)}  {p.status.phase}  restarts={restarts}  {reason}".strip())
    return "\n".join(lines) or "no pods"


def get_logs(namespace: str, pod: str, container: str = "", previous: bool = False, tail_lines: int = 80) -> str:
    if config.DRY_RUN:
        return "Unhandled exception. System.OutOfMemoryException\n   at cartservice.cartstore.RedisCartStore.GetCartAsync"
    core, _ = _k8s()
    try:
        out = core.read_namespaced_pod_log(pod, namespace, container=container or None,
                                           previous=previous, tail_lines=tail_lines)
    except Exception as e:  # previous logs may not exist
        out = f"error reading logs: {e}"
    return _trim(out)


def describe_resource(namespace: str, kind: str, name: str) -> str:
    """Compact 'kubectl describe' substitute: spec essentials + recent events."""
    if config.DRY_RUN:
        return "Deployment cartservice: image=cartservice:v0.8, limits.memory=64Mi (was 128Mi)"
    core, apps = _k8s()
    kind = kind.lower()
    if kind in ("deployment", "deploy"):
        d = apps.read_namespaced_deployment(name, namespace)
        c = d.spec.template.spec.containers
        info = {
            "replicas": d.spec.replicas, "ready": d.status.ready_replicas,
            "generation": d.metadata.generation,
            "containers": [{"name": x.name, "image": x.image,
                            "resources": x.resources.to_dict() if x.resources else None,
                            "envFrom": [e.to_dict() for e in (x.env_from or [])]} for x in c],
        }
    elif kind == "pod":
        p = core.read_namespaced_pod(name, namespace)
        info = {"node": p.spec.node_name, "phase": p.status.phase,
                "conditions": [(c.type, c.status) for c in (p.status.conditions or [])]}
    elif kind in ("configmap", "cm"):
        cm = core.read_namespaced_config_map(name, namespace)
        info = {"data": cm.data, "resourceVersion": cm.metadata.resource_version}
    else:
        return f"unsupported kind {kind}"
    return _trim(json.dumps(info, default=str, indent=1)) + "\n\nEVENTS:\n" + get_events(namespace, name)


def get_events(namespace: str, involved_name: str = "") -> str:
    if config.DRY_RUN:
        return "Warning BackOff cartservice-7d9f Back-off restarting failed container"
    core, _ = _k8s()
    evs = core.list_namespaced_event(namespace).items
    evs = [e for e in evs if not involved_name or involved_name in (e.involved_object.name or "")]
    evs.sort(key=lambda e: e.last_timestamp or e.event_time or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return "\n".join(f"{e.type} {e.reason} {e.involved_object.kind}/{e.involved_object.name}: {e.message}"
                     for e in evs[-25:]) or "no events"


def query_prometheus(promql: str) -> str:
    if config.DRY_RUN:
        return '{container="server",pod="cartservice-7d9f"} => 6.7e7 (memory working set, bytes)'
    try:
        r = requests.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": promql}, timeout=10)
        res = r.json().get("data", {}).get("result", [])
        return _trim("\n".join(f"{x['metric']} => {x['value'][1]}" for x in res[:30]) or "empty result")
    except Exception as e:
        return f"prometheus error: {e}"


def recent_changes(namespace: str, deployment: str) -> str:
    """ReplicaSet revision history: what changed and when (the #1 cause of incidents)."""
    if config.DRY_RUN:
        return "rev 4 (current, 6m ago): memory limit 128Mi -> 64Mi\nrev 3 (2d ago): image v0.8"
    _, apps = _k8s()
    d = apps.read_namespaced_deployment(deployment, namespace)
    sel = ",".join(f"{k}={v}" for k, v in d.spec.selector.match_labels.items())
    rss = apps.list_namespaced_replica_set(namespace, label_selector=sel).items
    rss.sort(key=lambda r: int(r.metadata.annotations.get("deployment.kubernetes.io/revision", 0)))
    lines = []
    for r in rss[-5:]:
        rev = r.metadata.annotations.get("deployment.kubernetes.io/revision")
        c = r.spec.template.spec.containers[0]
        lim = c.resources.limits if c.resources else None
        lines.append(f"rev {rev} created={r.metadata.creation_timestamp} image={c.image} limits={lim} replicas={r.status.replicas}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- WRITE tools (gated)
def rollout_restart(namespace: str, deployment: str) -> str:
    if config.DRY_RUN:
        return f"[dry-run] restarted {namespace}/{deployment}"
    _, apps = _k8s()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    apps.patch_namespaced_deployment(deployment, namespace, {
        "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}})
    return f"restarted {namespace}/{deployment}"


def rollback_deployment(namespace: str, deployment: str) -> str:
    """Roll back to the previous ReplicaSet template (like `kubectl rollout undo`)."""
    if config.DRY_RUN:
        return f"[dry-run] rolled back {namespace}/{deployment} to previous revision"
    _, apps = _k8s()
    d = apps.read_namespaced_deployment(deployment, namespace)
    sel = ",".join(f"{k}={v}" for k, v in d.spec.selector.match_labels.items())
    rss = apps.list_namespaced_replica_set(namespace, label_selector=sel).items
    rss.sort(key=lambda r: int(r.metadata.annotations.get("deployment.kubernetes.io/revision", 0)))
    if len(rss) < 2:
        return "no previous revision to roll back to"
    prev = rss[-2].spec.template
    labels = {k: v for k, v in (prev.metadata.labels or {}).items() if k != "pod-template-hash"}
    patch = {"spec": {"template": {"metadata": {"labels": labels, "annotations": prev.metadata.annotations},
                                   "spec": apps.api_client.sanitize_for_serialization(prev.spec)}}}
    apps.patch_namespaced_deployment(deployment, namespace, patch)
    return f"rolled back {namespace}/{deployment} to revision {rss[-2].metadata.annotations.get('deployment.kubernetes.io/revision')}"


def scale_deployment(namespace: str, deployment: str, replicas: int) -> str:
    replicas = max(0, min(int(replicas), 10))  # hard cap
    if config.DRY_RUN:
        return f"[dry-run] scaled {namespace}/{deployment} to {replicas}"
    _, apps = _k8s()
    apps.patch_namespaced_deployment_scale(deployment, namespace, {"spec": {"replicas": replicas}})
    return f"scaled {namespace}/{deployment} to {replicas}"


def restore_configmap(namespace: str, name: str, restart_deployment: str = "") -> str:
    """Restore a ConfigMap from the 'sre-agent/last-known-good' annotation (see k8s/config-demo.yaml)."""
    if config.DRY_RUN:
        return f"[dry-run] restored configmap {namespace}/{name}"
    core, _ = _k8s()
    cm = core.read_namespaced_config_map(name, namespace)
    good = (cm.metadata.annotations or {}).get("sre-agent/last-known-good")
    if not good:
        return "no last-known-good annotation; cannot restore"
    core.patch_namespaced_config_map(name, namespace, {"data": json.loads(good)})
    msg = f"restored configmap {namespace}/{name} from last-known-good"
    if restart_deployment:  # pods in CrashLoopBackOff pick up the fix faster after a restart
        msg += "; " + rollout_restart(namespace, restart_deployment)
    return msg


# --------------------------------------------------------------------------- registry
READ_TOOLS = {
    "get_pods": get_pods, "get_logs": get_logs, "describe_resource": describe_resource,
    "get_events": get_events, "query_prometheus": query_prometheus, "recent_changes": recent_changes,
}
WRITE_TOOLS = {
    "rollout_restart": rollout_restart, "rollback_deployment": rollback_deployment,
    "scale_deployment": scale_deployment, "restore_configmap": restore_configmap,
}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


_NS = {"type": "string", "description": "Kubernetes namespace"}
READ_TOOL_SCHEMAS = [
    _fn("get_pods", "List pods with readiness, restarts and waiting/terminated reasons.",
        {"namespace": _NS, "label_selector": {"type": "string"}}, ["namespace"]),
    _fn("get_logs", "Tail a pod's logs. Use previous=true for the crashed container.",
        {"namespace": _NS, "pod": {"type": "string"}, "container": {"type": "string"},
         "previous": {"type": "boolean"}, "tail_lines": {"type": "integer"}}, ["namespace", "pod"]),
    _fn("describe_resource", "Describe a deployment, pod or configmap plus its recent events.",
        {"namespace": _NS, "kind": {"type": "string", "enum": ["deployment", "pod", "configmap"]},
         "name": {"type": "string"}}, ["namespace", "kind", "name"]),
    _fn("get_events", "Recent Kubernetes events, optionally filtered by object name.",
        {"namespace": _NS, "involved_name": {"type": "string"}}, ["namespace"]),
    _fn("query_prometheus", "Run an instant PromQL query.", {"promql": {"type": "string"}}, ["promql"]),
    _fn("recent_changes", "Deployment revision history (image, limits, time). Check this early.",
        {"namespace": _NS, "deployment": {"type": "string"}}, ["namespace", "deployment"]),
]

WRITE_TOOL_DOCS = """Allowed remediation actions (choose at most one):
- rollout_restart(namespace, deployment)
- rollback_deployment(namespace, deployment)      # revert to previous revision
- scale_deployment(namespace, deployment, replicas)  # max 10
- restore_configmap(namespace, name, restart_deployment)  # from last-known-good annotation, then restart
- none                                            # recommend only, no automated action"""


def run_tool(name: str, args: dict) -> str:
    fn = READ_TOOLS.get(name) or WRITE_TOOLS.get(name)
    if not fn:
        return f"unknown tool {name}"
    ns = args.get("namespace")
    if ns and not _ns_ok(ns):
        return f"namespace '{ns}' is outside the agent's scope {config.TARGET_NAMESPACES}"
    try:
        return fn(**args)
    except Exception as e:
        return f"tool error: {type(e).__name__}: {e}"
