"""Central configuration. Everything comes from environment variables."""
import os

NEBIUS_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
NEBIUS_API_KEY = os.getenv("NEBIUS_API_KEY", "")

# Exact model IDs: run scripts/list-models.sh and paste the Nemotron IDs here / in .env
MODEL_NANO = os.getenv("M_NANO", "")    # fast triage
MODEL_SUPER = os.getenv("M_SUPER", "")  # tool-using investigator
MODEL_ULTRA = os.getenv("M_ULTRA", "")  # deep root-cause reasoning

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://mon-kube-prometheus-stack-prometheus.monitoring:9090")
TARGET_NAMESPACES = [n.strip() for n in os.getenv("TARGET_NAMESPACES", "app").split(",") if n.strip()]

# Escalate Super -> Ultra when confidence is below this
ESCALATION_CONFIDENCE = float(os.getenv("ESCALATION_CONFIDENCE", "0.7"))
# Max tool-calling rounds for the investigator
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
# If true, allowlisted actions run without human approval (keep false for demo)
AUTO_REMEDIATE = os.getenv("AUTO_REMEDIATE", "false").lower() == "true"
# Shared secret for state-changing calls (/alert, approve/reject, postmortem). Set it whenever
# the agent is reachable from outside the cluster (tunnel or Nebius endpoint).
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
# Extra JSON merged into every model request, e.g. to switch Nemotron reasoning off for Nano:
#   EXTRA_BODY_NANO='{"chat_template_kwargs": {"enable_thinking": false}}'   (check the model card)
EXTRA_BODY = {t: os.getenv(f"EXTRA_BODY_{t.upper()}", "") for t in ("nano", "super", "ultra")}
# Dry-run mode: tools return canned data, no cluster needed (for UI/dev work)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
