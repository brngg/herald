# HERALD
### Human-Evaluated Remediation Agent with Logged Decisions

> An agentic infrastructure operations platform where AI detects, diagnoses,
> and recommends remediations, but a human operator always has the final word.

---

## The Problem

Modern distributed systems fail constantly. Kubernetes pods crash, memory spikes,
and services return 500s. The monitoring exists, Prometheus fires the alert, but
recovery is still manual, slow, and undocumented. On-call engineers are woken up
at 2am to run the same runbook they ran last week.

The gap is not detection. The gap is **safe, explainable, auditable recovery**.

---

## The Solution

HERALD sits between alerting and operators. When an incident fires, HERALD gathers
context, proposes bounded remediation actions, routes them through policy and human
approval, and verifies whether the system actually recovered.

No autonomous execution. No black boxes. No unverified fixes.

---

## What HERALD Does

HERALD is built around three principles:

- **Human authority**: a human operator approves before anything executes
- **Auditability**: every plan, verdict, approval, and outcome is recorded
- **Verified recovery**: every remediation should follow pre-check, execute, and post-check

---

## Tech Stack

| Layer | Technology |
|---|---|
| Target system | Google Online Boutique |
| Chaos injection | Chaos Mesh |
| Metrics and alerting | Prometheus, Grafana, Alertmanager |
| Agent orchestration | LangGraph |
| Workflow execution | Temporal |
| LLM evaluation and tracing | Langfuse |
| Human approval | Slack API |
| Infrastructure | Kubernetes on minikube |

---

## Incident Scenarios

HERALD is currently scoped to four incident classes:

| Scenario | Failure Type |
|---|---|
| Bad deployment crash loop | CrashLoopBackOff on `cartservice` |
| CPU saturation | High CPU and response-time degradation on `frontend` |
| Frontend bad cart config | User-facing HTTP failures from a bad `CART_SERVICE_ADDR` config |
| Dependency network disruption | Network partition from `frontend` to `cartservice` |

Live-verified chaos and alerting setup currently covers:

- `crashloop-cartservice-bad-deploy.yaml`
- `chaos-frontend-cpu-saturation.yaml`
- `chaos-cartservice-network-partition.yaml`

Pending final live verification:

- `frontend-bad-cart-config.yaml` should keep the frontend process healthy while making `/cart` fail deterministically for users

---

## Project Status

Current implementation is strongest in:

- local environment setup
- chaos and alert generation/routing
- Alertmanager-to-`Incident` ingestion
- Fixer schema contracts and local CLI testing
- live Gemini-backed Fixer plan generation for the `crashloop` slice
- Judge contract, safety checks, and Gemini comparison path for the `crashloop` slice
- HITL Gate routing and `DecisionTrace` assembly for the `crashloop` slice
- a spawned Gemini-backed execution agent for the `crashloop` slice
- bounded crashloop execution through typed Kubernetes tools inside the execution agent
- pre-check and post-check verification for the `crashloop` slice, including Kubernetes-aware fallback when Prometheus readiness lags after rollout
- a live-validated crashloop recovery workflow entrypoint

Still not implemented end to end:

- rollback behavior after failed post-check
- durable workflow orchestration
- Slack-based approval flow
- evaluation harness
- the other three incident classes

Progress by phase:

- [x] Phase 1: Environment on minikube
- [x] Phase 2: Observability with Prometheus, Grafana, and Alertmanager
- [x] Phase 3: Chaos scenario setup
- [x] Phase 4: Fixer Agent vertical slice beyond plan generation
- [x] Phase 5: Judge layer beyond heuristic crashloop gating
- [x] Phase 6: Recovery workflow with pre-check and post-check
- [ ] Phase 7: HITL and Slack integration

---

## Python Setup

HERALD targets Python 3.11. Create a local virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

Run unit tests:

```bash
python -m unittest discover -s tests/unit -p 'test_*.py'
```

---

## Fixer Smoke Test

The current repo includes a CLI runner for the Fixer that accepts an Alertmanager
webhook payload JSON file and returns structured Fixer output.

Use the deterministic heuristic path:

```bash
python -m agents.fixer --payload-file /tmp/herald-crashloop-payload.json --no-llm
```

Use the hosted Gemini path:

```bash
export GEMINI_API_KEY=your_key_here
python -m agents.fixer --payload-file /tmp/herald-crashloop-payload.json
```

The default provider is Gemini. To override providers or models explicitly:

```bash
python -m agents.fixer \
  --provider gemini \
  --model gemini-2.5-flash \
  --payload-file /tmp/herald-crashloop-payload.json
```

By default, the CLI prints a sanitized summary and remediation plan. To include raw
incident context and full evidence for local debugging only:

```bash
python -m agents.fixer \
  --payload-file /tmp/herald-crashloop-payload.json \
  --include-debug-context
```

---

## Judge Smoke Test

The current Judge runs as a direct Python pipeline call after Fixer output. The
heuristic Judge remains authoritative, and a Gemini Judge provider can be injected
for comparison without bypassing the local safety policy.

Heuristic Fixer -> heuristic Judge:

```bash
./.venv/bin/python - <<'PY'
import json
from agents.fixer import run_fixer_for_alertmanager_payload
from agents.judge import run_judge_pipeline

with open('/tmp/herald-crashloop-payload.json', 'r', encoding='utf-8') as f:
    payload = json.load(f)

fixer_results = run_fixer_for_alertmanager_payload(payload, llm=None)
result = fixer_results[0]

judge_state = run_judge_pipeline(
    incident=result["incident"],
    evidence=result["evidence"],
    incident_summary=result["incident_summary"],
    actions=result["actions"],
    fixer_rationale=result.get("fixer_rationale"),
    llm=None,
)

print(judge_state["judge_verdict"])
print(judge_state["judge_reason"])
PY
```

---

## Crashloop Recovery Demo

The repo now includes an in-process recovery workflow for the `CrashLoopBackOff`
vertical slice on `cartservice`. It runs:

- Alertmanager payload ingestion
- Fixer
- Judge
- HITL Gate routing
- `DecisionTrace` assembly
- pre-check verification
- spawned Gemini execution agent for bounded rollout undo or rollout restart
- post-check verification

The current live demo has been validated end to end on minikube:

- apply the intentional bad deploy
- wait for `cartservice` to enter `CrashLoopBackOff`
- run the first HERALD pass to reach `pending_approval`
- approve `rollout_undo_cartservice`
- let the spawned Gemini execution agent execute the approved rollback
- wait for rollout completion and verify recovery
- finish with `decision_trace.final_state = recovered`

### Fastest Demo Path

If your local stack and Prometheus port-forward are already running, use:

```bash
./scripts/run_crashloop_demo.sh
```

That helper:

- applies the crashloop scenario
- waits until `cartservice` is visibly failing
- runs the first HERALD pass
- saves the first-pass JSON to `/tmp/herald-crashloop-plan.json`
- prints the exact approval command to run next

If the bad deploy is already active and you only want to run the first HERALD pass:

```bash
./scripts/run_crashloop_demo.sh --skip-apply
```

### Prerequisites

- Local cluster and observability stack are up
- `kubectl` points at the correct cluster
- Prometheus is reachable through `PROMETHEUS_BASE_URL` or `--prometheus-base-url`
- The crashloop payload file exists at `payloads/crashloop_alert.json`

### Step 1: Run Without Approval

This first run shows the proposed remediation, Judge result, HITL routing, and
pending `DecisionTrace`, but does not execute anything:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --prometheus-base-url http://localhost:9090
```

What to look for:

- `hitl_decision.routing_decision`
- `hitl_decision.recommended_action`
- `decision_trace.human_approval` should be `n/a`
- `decision_trace.final_state` should be `pending_approval`

### Step 2: Run With Explicit Approval

Copy the `action_id` from `hitl_decision.recommended_action` in the first run and
pass it back with `--approve-action-id`. On the default heuristic path, the
recommended action is typically `rollout_undo_cartservice`.

This second run approves the selected action and allows the workflow to execute
the bounded crashloop remediation:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --approve-action-id rollout_undo_cartservice \
  --prometheus-base-url http://localhost:9090
```

What to look for:

- `decision_trace.human_approval` should be `approved`
- `decision_trace.execution_result`
- `decision_trace.verification_result.pre_check`
- `decision_trace.verification_result.post_check`
- `decision_trace.final_state` should end as `recovered` or `unrecovered`

### Optional Gemini Comparison

The workflow defaults to heuristic Fixer and heuristic Judge. To compare hosted
Gemini-backed planning and verdicting:

```bash
export GEMINI_API_KEY=your_key_here

./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --prometheus-base-url http://localhost:9090 \
  --fixer-provider gemini \
  --judge-provider gemini
```

You can also override models explicitly:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --prometheus-base-url http://localhost:9090 \
  --fixer-provider gemini \
  --judge-provider gemini \
  --fixer-model gemini-2.5-flash \
  --judge-model gemini-2.5-flash
```

When using Gemini, action ordering, scores, and `action_id` values can vary. Run
the workflow once without approval, copy the recommended `action_id`, then rerun
with `--approve-action-id`.

Gemini Fixer -> Gemini Judge:

```bash
export GEMINI_API_KEY=your_key_here

./.venv/bin/python - <<'PY'
import json
from agents.fixer import run_fixer_for_alertmanager_payload
from agents.judge import run_judge_pipeline
from services.gemini_fixer_llm import GeminiFixerLLM
from services.gemini_judge_llm import GeminiJudgeLLM

with open('/tmp/herald-crashloop-payload.json', 'r', encoding='utf-8') as f:
    payload = json.load(f)

fixer_results = run_fixer_for_alertmanager_payload(
    payload,
    llm=GeminiFixerLLM(model="gemini-2.5-flash"),
)
result = fixer_results[0]

judge_state = run_judge_pipeline(
    incident=result["incident"],
    evidence=result["evidence"],
    incident_summary=result["incident_summary"],
    actions=result["actions"],
    fixer_rationale=result.get("fixer_rationale"),
    llm=GeminiJudgeLLM(model="gemini-2.5-flash"),
)

print(judge_state["judge_verdict"])
print(judge_state["judge_reason"])
PY
```
