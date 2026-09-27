#!/usr/bin/env bash
# Inject a demo fault into the app namespace.
#   oom         cartservice memory limit cut to 32Mi -> OOMKilled / CrashLoop (fix: rollback)
#   bad-image   checkoutservice pinned to a non-existent tag -> ImagePullBackOff, rollout stuck (fix: rollback)
#   bad-config  edge-proxy gets a broken nginx.conf -> CrashLoop (fix: restore_configmap + restart)
#   pod-delete  Litmus pod-delete chaos on cartservice (optional, needs Litmus)
#   reset       undo everything
set -euo pipefail
cd "$(dirname "$0")/.."
NS=app

case "${1:-}" in
  oom)
    kubectl -n $NS set resources deploy/cartservice -c server --limits=memory=32Mi --requests=memory=32Mi ;;
  bad-image)
    img=$(kubectl -n $NS get deploy checkoutservice -o jsonpath='{.spec.template.spec.containers[0].image}')
    kubectl -n $NS set image deploy/checkoutservice server="${img%:*}:does-not-exist" ;;
  bad-config)
    kubectl -n $NS patch configmap edge-proxy-config --type merge \
      -p '{"data":{"default.conf":"server { listen 8080; location / { retrun 200; } }\n"}}'
    kubectl -n $NS rollout restart deploy/edge-proxy ;;
  pod-delete)
    kubectl apply -f k8s/litmus-pod-delete.yaml ;;
  reset)
    kubectl -n $NS rollout undo deploy/cartservice || true
    kubectl -n $NS rollout undo deploy/checkoutservice || true
    kubectl apply -f k8s/config-demo.yaml && kubectl -n $NS rollout restart deploy/edge-proxy
    kubectl -n $NS delete chaosengine --all --ignore-not-found ;;
  *)
    echo "usage: $0 {oom|bad-image|bad-config|pod-delete|reset}"; exit 1 ;;
esac
echo "Injected: ${1}. Watch: kubectl -n $NS get pods -w"
