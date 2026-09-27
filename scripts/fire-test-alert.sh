#!/usr/bin/env bash
# Send a fake Alertmanager payload straight to the agent (no cluster alerting needed).
# Useful with DRY_RUN=true to develop the pipeline and dashboard before the cluster exists.
set -euo pipefail
URL="${1:-http://localhost:8080}"
AUTH=(); [ -n "${AGENT_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${AGENT_TOKEN}")
curl -s -X POST "$URL/alert" "${AUTH[@]}" -H 'content-type: application/json' -d '{
  "alerts": [{
    "status": "firing",
    "labels": {"alertname": "PodCrashLooping", "namespace": "app", "pod": "cartservice-7d9f", "severity": "high"},
    "annotations": {"summary": "cartservice-7d9f restarting repeatedly"},
    "startsAt": "2026-09-26T10:00:00Z"
  }]
}'
echo
