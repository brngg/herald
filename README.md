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

Still not implemented end to end:

- Judge-to-HITL integration
- HITL Gate routing
- execution workflow
- Kubernetes action execution
- post-check verification and rollback
- evaluation harness

Progress by phase:

- [x] Phase 1: Environment on minikube
- [x] Phase 2: Observability with Prometheus, Grafana, and Alertmanager
- [x] Phase 3: Chaos scenario setup
- [ ] Phase 4: Fixer Agent vertical slice beyond plan generation
- [ ] Phase 5: Judge layer beyond heuristic crashloop gating
- [ ] Phase 6: Recovery workflow with pre-check and post-check
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
