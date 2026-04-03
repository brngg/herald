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

## What HERALD Is Today

HERALD today is a capability-driven Verified Recovery orchestrator for Kubernetes incidents.

The default runtime path is `v2_execute`, which means the normal flow is:

- observe live Kubernetes and Prometheus state
- reason over bounded capability families
- critique those candidate plans against safety policy
- synthesize exact `ExecutionPlan` candidates
- require human approval through the HITL Gate
- execute only bounded approved tools
- verify whether recovery actually happened
- record the full lifecycle in the `DecisionTrace`

Three principles still anchor the system:

- **Human authority**: a human operator approves before anything executes
- **Auditability**: every plan, verdict, approval, and outcome is recorded
- **Verified recovery**: every remediation follows pre-check, execute, and post-check

The current control plane has two human gates:

- **Gate 0 investigation approval**: Alertmanager intake stores pending alerts in a
  filesystem inbox under `artifacts/inbox/`, and an operator explicitly chooses whether
  HERALD should investigate or ignore each alert.
- **Execution approval**: after Gate 0 investigation starts, HERALD still requires a
  second explicit human approval before any mutation executes.

---

## Design Evolution

### Backstory

HERALD started from a simple observation: most teams already have detection, but they
still do recovery manually. Alertmanager fires, an operator opens dashboards, runs a
known command, and then has to decide whether the system actually recovered. The goal
of HERALD is to close that gap with a control plane that is explainable, approval-gated,
and verification-driven.

That is why the project emphasizes the full chain:

- Fixer generates a plan
- Judge evaluates whether the plan is safe enough to surface
- the HITL Gate requires human approval before execution
- HERALD verifies whether recovery actually happened
- the `DecisionTrace` records the full lifecycle

### How HERALD Works Today

The system is no longer just a classifier plus a fixed action lookup table, but it is
also not unconstrained autonomy.

Today HERALD reasons over a bounded capability catalog and executes only approved,
typed tool families. The current catalog includes:

- `rollout.undo_deployment`
- `rollout.restart_deployment`
- `scale.deployment`
- `pod.delete_stateless_pod`
- `chaos.delete_stresschaos`
- `chaos.delete_networkchaos`
- `escalate.human_review`

Each capability has:

- a bounded execution family
- a blast-radius prior
- a verification recipe
- an escalation fallback

That means the dynamic part of HERALD is:

- it derives the right capability from live evidence
- it synthesizes the exact plan to execute
- it verifies the outcome before calling recovery complete

What HERALD does **not** do today:

- run arbitrary shell commands as a normal control surface
- invent unrestricted kubectl mutations
- patch arbitrary cluster objects without a bounded capability contract
- merge or deploy code fixes automatically

`engine_mode=v2_execute` is the default for the CLI, terminal inbox, and replay runner.
`engine_mode=v2_shadow` still exists as a diagnostic mode. Explicit legacy behavior is
now compatibility-only and no longer the normal runtime story.

### What Remains Transitional

The repo still contains some compatibility scaffolding from the older architecture:

- `schemas/remediation.py`
- `agents/fixer.py`
- `agents/judge.py`
- a few legacy upgrade helpers that map older action-shaped artifacts into the
  current candidate-first runtime contract

Those pieces are no longer the normal operating path, but they still exist so older
artifacts and test fixtures can be read during the migration window.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Target system | Google Online Boutique |
| Chaos injection | Chaos Mesh |
| Metrics and alerting | Prometheus, Grafana, Alertmanager |
| Agent orchestration | LangGraph |
| Workflow execution today | Python workflow runner + typed execution worker |
| Durable orchestration target | Temporal |
| LLM evaluation and tracing | Langfuse |
| Human approval | Slack API |
| Infrastructure | Kubernetes on minikube |

---

## Runtime Layout

The codebase is now organized around capability boundaries instead of a flat
`services/` directory:

- `services/llm/` holds provider transports plus task-specific LLM adapters
- `services/recovery/` holds capability catalogs, synthesis, policy, and verification
- `services/infra/kubernetes/` holds typed Kubernetes access and the execution worker
- `services/observability/` holds Prometheus access and cluster observation helpers
- `services/alerts/` holds Alertmanager ingestion and the filesystem inbox
- `services/normalization/` and `services/runtime/` hold smaller shared runtime helpers

This keeps the workflow and agent layers focused on orchestration while the platform
interfaces stay typed and testable.

## Cluster and Metrics Access

HERALD does not rely on unrestricted shell access as its normal control surface.
Instead, the agents use typed tool wrappers backed by Kubernetes and Prometheus:

- `services/infra/kubernetes/client.py` exposes read and bounded write operations
- `services/observability/prometheus.py` exposes pre-check and post-check metric queries
- `services/observability/cluster_observer.py` builds compact observation summaries for
  the Reasoner

In local development this means HERALD needs:

- a working `kubectl` context pointed at the target cluster
- Kubernetes RBAC that allows the read and bounded write operations you want HERALD to use
- a reachable Prometheus base URL for verification and incident signals

The long-term model is: broad read access through typed observation tools, narrow
mutation access through bounded execution tools, and exact-plan approval before any
write action executes.

HERALD's current dynamic recovery catalog is capability-driven rather than
incident-branch-driven. The default catalog includes:

- `rollout.undo_deployment`
- `rollout.restart_deployment`
- `scale.deployment`
- `pod.delete_stateless_pod`
- `chaos.delete_stresschaos`
- `chaos.delete_networkchaos`
- `escalate.human_review`

Each capability carries a bounded execution family, a blast-radius prior, and an
explicit verification recipe. New mutation families should follow that same rule:
no write capability lands without typed verification and escalation behavior.

---

## Benchmark Scenarios

HERALD still uses four checked-in benchmark scenarios as primary evaluation anchors:

| Scenario | Failure Type |
|---|---|
| Bad deployment crash loop | CrashLoopBackOff on `cartservice` |
| CPU saturation | High CPU and response-time degradation on `frontend` |
| Frontend bad cart config | User-facing HTTP failures from a bad `CART_SERVICE_ADDR` config |
| Dependency network disruption | Network partition from `frontend` to `cartservice` |

Checked-in chaos and alerting coverage currently includes:

- `crashloop-cartservice-bad-deploy.yaml`
- `chaos-frontend-cpu-saturation.yaml`
- `frontend-bad-cart-config.yaml`
- `chaos-cartservice-network-partition.yaml`

These slices are benchmarks, not the full architecture boundary anymore. HERALD now
also has capability-driven paths for readiness shortfall, stateless pod replacement,
and safe escalation validation.

Live validation status today:

- crashloop rollback: live-validated end to end on `v2_execute`
- bad config rollback: live-validated end to end
- scale shortfall: live-validated end to end on `v2_execute`
- CPU chaos deletion: live-exercised, but sustained high-load timing remains more sensitive
- network-partition deletion: replay-validated and safe-skip path proven live, but not yet fully live-validated end to end
- stateless pod replacement: replay-validated, live helper exists, but the live setup is still precondition-sensitive
- escalation-only path: replay-validated; live proof still needs a cleaner neutral scenario than the current placeholder

---

## Project Status

HERALD today is best understood as a late-stage capability-driven recovery system.

Strong and already real:

- candidate-first approval via exact `ExecutionPlan` artifacts
- default `v2_execute` runtime on the CLI, inbox, and replay runner
- typed observation bundles from Kubernetes and Prometheus
- capability-driven reasoning and synthesis
- bounded Gemini-backed execution worker with typed tools
- pre-check, execute, and post-check verification on the normal runtime path
- replay metrics for both benchmark scenarios and newer dynamic capability scenarios

Live-proven on the current architecture:

- crashloop rollback recovery
- frontend bad-config rollback recovery
- scale shortfall recovery

Implemented and replay-proven, but not all live-proven yet:

- CPU chaos deletion
- network-partition chaos deletion
- stateless pod replacement
- safe escalation on non-actionable evidence

Still planned or incomplete:

- durable Temporal orchestration
- Slack-based HITL approval
- final deletion of all remaining compatibility-only v1 scaffolding
- a richer provider-backed bounded `verify -> replan` loop

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

## Gate 0 Alert Inbox

HERALD now accepts Alertmanager webhooks into a filesystem-backed inbox before Fixer or
Judge run. Each incident is stored under `artifacts/inbox/<artifact-id>/` with:

- `alert.json`: raw webhook payload, normalized incident metadata, arrival timestamp, and Gate 0 status
- `first-pass.json`: saved planning result after Gate 0 investigation starts
- `final-result.json`: saved final workflow result after the second approval gate completes

The v1 inbox statuses are:

- `pending_investigation`
- `ignored`
- `planning_started`
- `pending_execution_approval`
- `completed`

### Run the Alert Intake Service

Start the local webhook receiver:

```bash
./.venv/bin/uvicorn services.alerts.inbox_service:app --host 0.0.0.0 --port 8080
```

Send an Alertmanager payload to it:

```bash
curl -X POST http://localhost:8080/alerts \
  -H 'content-type: application/json' \
  --data-binary @payloads/crashloop_alert.json
```

That writes one pending inbox artifact per incident under `artifacts/inbox/`.

### Run the Persistent Terminal Watcher

For the live Gate 0 operator flow, keep a terminal watcher open:

```bash
./.venv/bin/python -m workflows.operator_inbox \
  --watch \
  --prometheus-base-url http://localhost:9090
```

The watcher will:

- poll the filesystem inbox for new alerts
- automatically surface `pending_investigation` incidents in terminal
- prioritize `pending_execution_approval` incidents if planning already happened
- prompt `1 = investigate` or `2 = ignore`
- if investigated, run planning and then prompt `1 = approve execution` or `2 = reject`

The watcher now defaults to `--engine-mode v2_execute`.
Add `--engine-mode v2_shadow` if you want the watcher to collect live observation
context plus shadow Reasoner, Critic, and Synthesizer output before bounded v1
planning, then attach shadow verifier and replanner results after any real bounded
execution without changing execution behavior.

`--engine-mode v1` is still accepted if you need the old action-based corridor
for comparison or compatibility testing.

### Run the One-Shot Terminal Inbox Flow

For debugging or manual selection, you can still open the operator inbox directly:

```bash
./.venv/bin/python -m workflows.operator_inbox --prometheus-base-url http://localhost:9090
```

The one-shot flow will:

- list actionable inbox alerts
- prompt `1 = investigate` or `2 = ignore`
- if ignored, mark the inbox artifact `ignored` and exit
- if investigated, mark the alert `planning_started`, run the existing recovery planning flow, save `first-pass.json`, and move to `pending_execution_approval`
- continue into the existing second approval gate and prompt `1 = approve execution` or `2 = reject`

Gate 0 investigation approval does not authorize execution by itself. The second execution
approval gate remains required exactly as before.

You can also pass `--engine-mode v2_shadow` here to exercise the shadow-only
observe-to-replan loop while keeping the compatibility executor authoritative.
Pass `--engine-mode v1` only when you specifically want to compare against the
legacy action-based path.

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
- approve `reasoner-rollout-undo-cartservice`
- let the spawned Gemini execution agent execute the approved rollback
- watch the spawned worker report its lifecycle and tool calls live in the terminal
- wait for rollout completion and verify recovery
- finish with `decision_trace.final_state = recovered`

See [docs/recovery_architecture.md](/Users/bcheng/Projects/herald/docs/recovery_architecture.md) for the
current control-plane diagram, recovery semantics, and the canonical crashloop artifact layout.

### Fastest Demo Path

If your local stack and Prometheus port-forward are already running, use:

```bash
./scripts/run_crashloop_demo.sh
```

That helper:

- applies the crashloop scenario
- waits until `cartservice` is visibly failing
- runs the first HERALD pass
- saves the first-pass JSON to `artifacts/crashloop/<timestamp>/first-pass.json`
- prompts you to approve, reject, or stop
- resumes approval or rejection from the saved first-pass artifact instead of rerunning Fixer and Judge
- if approved, saves the final JSON to `artifacts/crashloop/<timestamp>/approval-run.json`
- if approved, saves the live worker stream to `artifacts/crashloop/<timestamp>/worker-stream.log`
- if rejected, saves the rejection JSON to `artifacts/crashloop/<timestamp>/rejection-run.json`

### Frontend CPU Demo

The repo now also includes a full demo helper for the `cpu_saturation` slice on
`frontend`. It applies the Chaos Mesh CPU stress scenario, waits for the Prometheus
signal to turn positive, runs the first HERALD pass, and then resumes approval or
rejection from the saved first-pass artifact.

If your local stack and Prometheus port-forward are already running, use:

```bash
./scripts/run_frontend_cpu_demo.sh
```

That helper:

- applies the frontend CPU saturation scenario
- saves the first-pass JSON to `artifacts/frontend_cpu/<timestamp>/first-pass.json`
- prompts you to approve, reject, or stop
- resumes from the saved first-pass artifact instead of rerunning Fixer and Judge
- if approved, deletes the active `frontend-cpu-saturation` `StressChaos` object
- saves the final JSON and worker stream under `artifacts/frontend_cpu/<timestamp>/`

The bounded remediation for this slice is intentionally honest: HERALD removes the
active CPU chaos object and then verifies that CPU pressure and readiness recover.

### Dynamic Capability Validation

HERALD now also has concrete validation assets for the newer capability-driven paths:

- `scale.deployment`
- `pod.delete_stateless_pod`
- `escalate.human_review`

#### Live Scale Shortfall Demo

This helper creates a safe readiness shortfall by scaling `frontend` to `0`, then lets
HERALD recommend the bounded scale-up plan and resume from the saved first-pass artifact.

```bash
./scripts/run_scale_shortfall_demo.sh
```

That helper:

- scales `frontend` to `0`
- waits for ready replicas to reach zero
- runs the first HERALD pass with `payloads/readiness_shortfall_alert.json`
- saves `artifacts/readiness_shortfall/<timestamp>/first-pass.json`
- optionally resumes approval from the saved artifact
- if approved, verifies that readiness recovers after the bounded scale action

#### Live Stateless Pod Replacement Helper

The stateless pod-replacement capability is implemented and replay-tested, but its live
validation is still more precondition-sensitive than the benchmark demos. The helper
below expects a workload that already has:

- more than one desired replica
- at least one non-ready pod for `app=<deployment>`

It will validate that HERALD chooses the `pod.delete_stateless_pod` family when those
preconditions already exist.

```bash
./scripts/run_stateless_pod_replacement_demo.sh --deployment cartservice
```

If the cluster is not already in that state, the helper exits with guidance instead of
forcing a misleading scenario setup.

#### Escalation Validation Helper

This helper validates the bounded “diagnose and escalate” path when no safe mutation
family is justified by the current evidence:

```bash
./scripts/run_unknown_dependency_escalation_demo.sh
```

It uses `payloads/unknown_dependency_alert.json` and resumes from the saved first-pass
artifact just like the recovery demos.

### Live Execution View

When you run the approval command, HERALD now streams the spawned execution worker lifecycle
to the terminal on `stderr` while keeping the final structured workflow JSON on `stdout`.

You will see messages like:

```text
[HERALD worker-...] spawned Gemini execution agent pid=...
[HERALD worker-...] agent started allowed_tools=[...]
[HERALD worker-...] step 1 deciding next tool
[HERALD worker-...] step 1 requested tool=get_deployment_context
[HERALD worker-...] step 1 running tool=get_deployment_context
[HERALD worker-...] step 1 completed tool=get_deployment_context status=succeeded
[HERALD worker-...] step 2 requested tool=rollout_undo_deployment
[HERALD worker-...] step 2 running tool=rollout_undo_deployment
[HERALD worker-...] step 2 completed tool=rollout_undo_deployment status=succeeded
[HERALD worker-...] step 3 returned finish
[HERALD worker-...] agent finished status=succeeded
[HERALD worker-...] worker exited status=succeeded
```

This gives a visible execution trace for the spawned Gemini worker without exposing hidden
chain-of-thought, and the final workflow JSON remains easy to save or parse separately.

If the bad deploy is already active and you only want to run the first HERALD pass:

```bash
./scripts/run_crashloop_demo.sh --skip-apply
```

To run the whole crashloop path in one operator command and auto-approve the recommended action:

```bash
./scripts/run_crashloop_demo.sh --auto-approve
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

To exercise the shadow path without changing execution behavior, add
`--engine-mode v2_shadow` to the same command. The workflow will record
`observe`, `reason`, `critique`, and `synthesize` nodes, include
`observation_bundle`, `reasoner_state`, `critic_state`, and `synthesizer_state`
in the result, and store nested shadow output under
`decision_trace.fixer_plan["v2_shadow"]` before handing off to the bounded v1 planner.
If you later approve from that saved first pass, the resumed run will append shadow
`verifier_state` and bounded `replanner_state` after the real execution outcome.

The CLI now defaults to `--engine-mode v2_execute`. In that mode the saved first
pass is candidate-based: HITL approval is attached to an exact execution plan
candidate instead of a v1 remediation action id.

What to look for:

- `hitl_decision.routing_decision`
- `hitl_decision.recommended_candidate`
- `decision_trace.human_approval` should be `n/a`
- `decision_trace.final_state` should be `pending_approval`

### Step 2: Run With Explicit Approval

Copy the `candidate_id` from `hitl_decision.recommended_candidate` in the first run
and pass it back with `--approve-action-id`. On the default heuristic path, the
recommended candidate for the crashloop benchmark is typically
`reasoner-rollout-undo-cartservice`.

The recommended approval flow resumes from the saved first-pass artifact so the
second command does not rerun Fixer or Judge. This second run approves the
selected action and allows the workflow to execute the bounded crashloop remediation:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --resume-from-file /tmp/herald-first-pass.json \
  --approve-action-id reasoner-rollout-undo-cartservice \
  --prometheus-base-url http://localhost:9090
```

What to look for:

- `decision_trace.human_approval` should be `approved`
- `decision_trace.execution_result`
- `decision_trace.verification_result.pre_check`
- `decision_trace.verification_result.post_check`
- `decision_trace.final_state` should end as `recovered`, `rolled_back`, or `escalated`

If you use the new Gate 0 inbox flow instead of calling the recovery workflow directly,
the operator sequence is:

1. Alertmanager sends the webhook to `/alerts`.
2. HERALD stores a `pending_investigation` inbox artifact.
3. The operator keeps `python -m workflows.operator_inbox --watch` running in terminal.
4. The watcher prompts `1 = investigate` or `2 = ignore`.
5. If investigated, HERALD saves `first-pass.json` and then prompts for the existing
   execution approval gate.

### Step 2b: Run With Explicit Rejection

If you want to exercise the human rejection path instead of executing the action:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --resume-from-file /tmp/herald-first-pass.json \
  --reject-action-id reasoner-rollout-undo-cartservice \
  --prometheus-base-url http://localhost:9090
```

What to look for:

- `decision_trace.human_approval` should be `rejected`
- `decision_trace.execution_result.status` should be `not_executed`
- `decision_trace.final_state` should be `rejected`

### Recovery semantics

- `pending_approval`: HERALD produced a bounded plan and is waiting for human input
- `rejected`: the operator rejected the action, so nothing executed
- `recovered`: the approved action executed and verification confirmed recovery
- `rolled_back`: HERALD executed a bounded rollback after a failed restart path and recovery was restored
- `escalated`: HERALD halted, execution failed, rollout did not converge, or recovery could not be verified safely

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

When using Gemini, candidate ordering, scores, and `candidate_id` values can vary.
Run the workflow once without approval, copy the recommended `candidate_id`, then
resume from that saved artifact for approval so the planning stack is not called
twice.

Gemini no-rerun approval flow:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --prometheus-base-url http://localhost:9090 \
  --fixer-provider gemini \
  --judge-provider gemini | tee /tmp/herald-first-pass.json
```

```bash
CANDIDATE_ID="$(jq -r '.hitl_decision.recommended_candidate.candidate_id' /tmp/herald-first-pass.json)"

./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --prometheus-base-url http://localhost:9090 \
  --resume-from-file /tmp/herald-first-pass.json \
  --approve-action-id "$CANDIDATE_ID" | tee /tmp/herald-approval-run.json
```

Gemini Fixer -> Gemini Judge:

```bash
export GEMINI_API_KEY=your_key_here

./.venv/bin/python - <<'PY'
import json
from agents.fixer import run_fixer_for_alertmanager_payload
from agents.judge import run_judge_pipeline
from services.llm.tasks.fixer import GeminiFixerLLM
from services.llm.tasks.judge import GeminiJudgeLLM

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

---

## Evaluation Harness

HERALD now includes a replay runner that writes machine-readable artifacts and a
metrics summary for deterministic scenario replays.

Run one success path plus one failure path:

```bash
./.venv/bin/python -m evaluation.run_scenario \
  --scenario evaluation/scenarios/crashloop_recovered.json \
  --scenario evaluation/scenarios/crashloop_worker_failure.json \
  --scenario evaluation/scenarios/frontend_cpu_recovered.json \
  --scenario evaluation/scenarios/readiness_shortfall_recovered.json \
  --scenario evaluation/scenarios/pod_unhealthy_recovered.json \
  --scenario evaluation/scenarios/unknown_dependency_escalated.json \
  --runs 1 \
  --output-dir /tmp/herald-eval
```

This writes:

- one JSON artifact per replayed run
- `metrics-summary.json`
- `metrics-summary.md`

The current metrics include:

- recommendation top-1 / top-2 rate
- approval policy correctness
- execution success rate
- verification correctness
- false recovery rate
- `DecisionTrace` coverage
- median and p95 recovery latency
