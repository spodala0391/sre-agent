#!/usr/bin/env bash
# One-shot local environment: k3d cluster, Prometheus+Alertmanager, demo apps, the agent in-cluster.
# Requires: docker, k3d, kubectl, helm, and a filled-in .env in the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Create .env from .env.example first"; exit 1; }

echo "==> cluster"
k3d cluster list | grep -q sre-demo || k3d cluster create sre-demo --agents 1

echo "==> monitoring (Prometheus + Alertmanager routed to the agent)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update >/dev/null
EXTRA_VALUES=()
TOKEN=$(grep -E '^AGENT_TOKEN=' .env | cut -d= -f2- || true)
if [ -n "$TOKEN" ]; then   # Alertmanager must present the agent token on the webhook
  cat > /tmp/am-auth.yaml <<EOV
alertmanager:
  config:
    receivers:
      - name: "null"
      - name: sre-agent
        webhook_configs:
          - url: http://sre-agent.sre-agent:8080/alert
            send_resolved: false
            http_config:
              authorization: {type: Bearer, credentials: "$TOKEN"}
EOV
  EXTRA_VALUES=(-f /tmp/am-auth.yaml)
fi
helm upgrade --install mon prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f k8s/alerting-values.yaml "${EXTRA_VALUES[@]}" --wait --timeout 10m

echo "==> demo workloads"
kubectl create ns app --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n app -f https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml
kubectl apply -f k8s/config-demo.yaml

echo "==> agent"
docker build -t sre-agent:dev .
k3d image import sre-agent:dev -c sre-demo
kubectl apply -f k8s/rbac.yaml
kubectl -n sre-agent create secret generic sre-agent-env --from-env-file=.env \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/agent-deployment.yaml
kubectl -n sre-agent rollout restart deploy/sre-agent
kubectl -n sre-agent rollout status deploy/sre-agent --timeout=180s

echo
echo "Done. Open the dashboard with:"
echo "  kubectl -n sre-agent port-forward svc/sre-agent 8080:8080   # then http://localhost:8080"
