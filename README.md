# Self-Healing SRE Agent (Nemotron on Nebius Token Factory)

An agent that receives Kubernetes alerts, investigates them with real cluster tools, finds the root
cause and proposes (or applies) a safe fix. It uses a tiered model design to stay fast and cheap:

| Tier | Model | Job | When |
|---|---|---|---|
| Nano | Nemotron Nano | Triage: noise / known / investigate | Every alert |
| Super | Nemotron Super | Tool-calling investigation (pods, logs, events, metrics, change history) | Non-noise alerts |
| Ultra | Nemotron Ultra | Deep root-cause reasoning across components | Only when Super's confidence is under 0.7 or the issue spans several components |

```
Prometheus ─► Alertmanager ─► POST /alert ─► Nano triage ─► Super + read-only tools ─► (Ultra) ─► proposed action
                                                                                                   │
                                     Dashboard  ◄── timeline, tier usage, MTTR ◄── human approval ─┘─► allowlisted write action
```

**Safety model:** read tools run freely. Write actions are limited to an allowlist (restart, rollback,
scale ≤ 10, restore ConfigMap), scoped to `TARGET_NAMESPACES`, backed by least-privilege RBAC, and
require human approval unless `AUTO_REMEDIATE=true`.

## Repo layout
```
agent/
  config.py     env-driven settings
  llm.py        Token Factory client + per-call tier/token/latency logging
  tools.py      k8s + Prometheus tools (READ and gated WRITE) with JSON schemas
  router.py     Nano -> Super -> Ultra pipeline and action execution
  server.py     FastAPI: /alert, /incidents, /incidents/{id}/approve|reject, /usage, /
  static/       dashboard (single HTML file)
jobs/rca_job.py post-incident postmortem writer (Nebius Serverless Job)
k8s/            RBAC, agent deployment, alert routing + demo rules, nginx config demo, Litmus experiment
scripts/        list-models, setup-cluster, inject-fault, fire-test-alert
```

---

## Step-by-step execution

### Step 1: Get models (15 min)
1. Create an API key in the Nebius Token Factory console.
2. `export NEBIUS_API_KEY=...` then run `bash scripts/list-models.sh`.
3. `cp .env.example .env` and paste the Nano, Super and Ultra IDs into `M_NANO`, `M_SUPER` and `M_ULTRA`.
   Check the Token Factory function-calling docs to confirm your Super model supports tools.

Then check that every tier answers in JSON and that Super can call tools:
```bash
set -a; source .env; set +a
python scripts/smoke-test-models.py
```
If Nano or Super writes long reasoning before its JSON, turn reasoning off with `EXTRA_BODY_NANO`
(see `.env.example`). If Super makes no tool call, try a different Nemotron Super ID from the model list.

### Step 2: Run the agent with no cluster (15 min)
This proves the model wiring works before you spend time on Kubernetes.
```bash
pip install -r requirements.txt
set -a; source .env; set +a
DRY_RUN=true uvicorn agent.server:app --port 8080
# another terminal
bash scripts/fire-test-alert.sh
open http://localhost:8080      # watch Nano -> Super -> (Ultra) in the timeline
```
In dry-run mode the tools return canned cluster data, but the model calls are real.

### Step 3: Stand up the cluster (45–60 min)
Requires docker, k3d, kubectl, helm and jq.
```bash
bash scripts/setup-cluster.sh
kubectl -n sre-agent port-forward svc/sre-agent 8080:8080
```
This creates a k3d cluster, installs kube-prometheus-stack with alerts routed to the agent, deploys
Online Boutique plus the `edge-proxy` config demo into `app`, and runs the agent in-cluster with its RBAC.

### Step 4: Break things and let it heal (30 min per scenario)
```bash
bash scripts/inject-fault.sh oom          # cartservice OOMKilled -> expect rollback_deployment
bash scripts/inject-fault.sh bad-image    # ImagePullBackOff (alert: PodImagePullFailing) -> expect rollback_deployment
bash scripts/inject-fault.sh bad-config   # nginx crashloop -> expect restore_configmap
bash scripts/inject-fault.sh reset        # clean up between takes
```
Alerts fire within about 1–2 minutes. Approve the fix in the dashboard and watch the pods recover.
If the diagnosis is off, tune the prompts in `router.py` (INVESTIGATE_PROMPT has the investigation order).

### Step 5: Deploy on Nebius (1–2 hr)
The agent must reach your cluster's API to heal it, so where the agent runs depends on where the cluster runs.

**Option A (recommended): cluster and agent at home, postmortems on Nebius.**
The cluster and agent run on your own server. The dashboard is exposed over HTTPS, and a Nebius
Serverless Job writes the postmortems.
```bash
# 1. Expose the dashboard (token-protected; set AGENT_TOKEN in .env before setup-cluster.sh)
kubectl -n sre-agent port-forward svc/sre-agent 8080:8080 &
cloudflared tunnel --url http://localhost:8080          # prints https://<random>.trycloudflare.com
# 2. Push the image somewhere Nebius can pull from
docker tag sre-agent:dev docker.io/<you>/sre-agent:v1 && docker push docker.io/<you>/sre-agent:v1
# 3. Run the postmortem job (check `nebius ai job create --help` for platform/preset names in your region)
nebius ai job create --name sre-postmortems \
  --image docker.io/<you>/sre-agent:v1 \
  --container-command "python -m jobs.rca_job" \
  --env AGENT_URL=https://<random>.trycloudflare.com --env AGENT_TOKEN=$AGENT_TOKEN \
  --env M_ULTRA=$M_ULTRA --env NEBIUS_API_KEY=$NEBIUS_API_KEY \
  --platform <smallest-available> --preset <smallest-available>
```
The job prints each postmortem to its log and posts it back, and a "View blameless postmortem" link
appears on the incident.

**Option B: agent on a Nebius Serverless Endpoint.**
Use this only if the cluster's API is reachable from the internet, for example a Nebius Managed
Kubernetes cluster. Put its kubeconfig in a MysteryBox secret and pass it in.
```bash
nebius ai endpoint create --name sre-agent \
  --image docker.io/<you>/sre-agent:v1 --container-port 8080 --public \
  --auth token --token "$ENDPOINT_TOKEN" \
  --env M_NANO=$M_NANO --env M_SUPER=$M_SUPER --env M_ULTRA=$M_ULTRA \
  --env AGENT_TOKEN=$AGENT_TOKEN --env-secret NEBIUS_API_KEY=<secret-selector> \
  --platform <platform> --preset <preset>
nebius ai endpoint get-by-name --name sre-agent --format json | jq -r '.status.public_endpoints[]'
```
Then point Alertmanager's webhook at `https://<endpoint>/alert`, with
`http_config.authorization.credentials` set to the endpoint token. Note that the endpoint's own `--auth token`
gate and `AGENT_TOKEN` are separate: either use the same value, or run with `--auth none` and rely on `AGENT_TOKEN`.
The Nebius docs show endpoints on GPU platforms. The agent needs no GPU, so check whether a CPU
platform is offered before paying for one.

### Step 6: Demo video (about 3 min)
1. Start on the dashboard with healthy pods (`kubectl get pods -n app`).
2. Run `inject-fault.sh oom` and show the pods crashlooping.
3. The alert arrives, and the timeline shows Nano triaging it, Super's tool calls, and escalation to Ultra.
4. Ultra identifies the change that cut the memory limit and proposes a rollback.
5. Click **Approve fix**, the pods recover, and the "Recovered in Ns" badge appears.
6. Close on the tier-usage tiles: most calls are Nano or Super, and Ultra is used only when needed.
7. Optionally, show a generated `rca-*.md` from the Serverless Job.

## Knobs
| Env | Default | Meaning |
|---|---|---|
| `ESCALATION_CONFIDENCE` | 0.7 | Below this, Super escalates to Ultra |
| `MAX_TOOL_ROUNDS` | 8 | Investigation tool-call budget |
| `AUTO_REMEDIATE` | false | Skip human approval for allowlisted actions |
| `DRY_RUN` | false | Canned tool outputs, no cluster needed |
| `TARGET_NAMESPACES` | app | Only these namespaces can be read or changed |

## Next steps if you have time
- Persist incidents in SQLite (the store is in-memory, so it resets on restart).
- Add a "known pattern" memory so repeated incidents skip straight to the fix, which pushes more traffic to Nano.
- Add a post-fix verification step that re-checks pod health and auto-reverts if the fix made things worse.
- Add Open5GS as the workload to tie it to the 5G self-healing story.
