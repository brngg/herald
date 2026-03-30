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

The current control plane now has two human gates:

- **Gate 0 investigation approval**: Alertmanager intake stores pending alerts in a
  filesystem inbox under `artifacts/inbox/`, and an operator explicitly chooses whether
  HERALD should investigate or ignore each alert.
- **Execution approval**: after Gate 0 investigation starts, HERALD still runs Fixer ->
  Judge -> HITL Gate and requires a second explicit human approval before any action executes.

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

The current implementation is intentionally bounded. For the live crashloop slice, the
system already knows the supported incident class and operates over a small set of
reversible remediation actions such as rollout undo and rollout restart.

This can feel controlled because, today, the workflow is still operating inside a known
corridor:

- HERALD parses a supported alert
- Fixer ranks bounded remediation actions
- Judge evaluates the proposed plan against the safety rubric
- the HITL Gate surfaces the recommended action
- after approval, HERALD executes the bounded action
- HERALD runs pre-check and post-check verification
- the `DecisionTrace` records both the rolled-up state and node-run provenance

The system works this way right now for a reason:

- it keeps execution safe and reversible during the demo phase
- it makes evaluation and replay honest and repeatable
- it proves the control-plane architecture before expanding the action surface
- it avoids pretending HERALD can already fix arbitrary incidents safely

In other words, the current demo is not trying to show unlimited autonomy. It is trying
to prove that approval-gated Verified Recovery works end to end.

Parallel HERALD 2.0 work is now underway behind a shadow path.
When `engine_mode=v2_shadow`, HERALD collects live Kubernetes and Prometheus context,
runs `observe -> reason -> critique -> synthesize` before bounded v1 planning, and
then records shadow `verify -> replan` follow-up analysis after the real v1 execution
path finishes. Execution behavior stays unchanged while the new substrate is validated.

A first Phase 6 cutover is also in place behind `engine_mode=v2_execute`.
Today that pilot makes four bounded actions execute from the Synthesizer plan:
`delete_stresschaos` for CPU saturation, `delete_networkchaos` for the
network-partition slice, the bad-config `rollout_undo_deployment` path for
`frontend`, and the crashloop `rollout_undo_deployment` path for `cartservice`.
Other live paths still fall back to v1.

### Future Direction: Derived Mutation Plans

The longer-term idea is not to hand-author an enormous toolbox of incident-specific
commands forever. The intended evolution is for HERALD to become more dynamic by using
Fixer and Judge output to derive the executable mutation plan itself.

The future shape looks more like this:

- Fixer produces a structured remediation plan, not just a pre-named action choice
- Judge evaluates that plan for safety, blast radius, and plausibility
- HERALD translates the approved plan into an exact executable mutation plan
- the operator approves that exact plan through the HITL Gate
- HERALD executes it and verifies recovery

That direction keeps the current philosophy intact:

- HERALD should try to find a fix, not only recommend escalation
- a human still approves before execution
- recovery is only considered successful after verification
- the `DecisionTrace` still captures plan, verdict, approval, execution, and outcome

This is the bridge from the current bounded-tool demo to a more flexible system. The
demo phase uses a narrow execution corridor on purpose. Future HERALD should keep the
same control-plane safeguards while becoming more adaptive in how it generates the
mutation plan it asks the operator to approve.

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

Checked-in chaos and alerting coverage currently includes:

- `crashloop-cartservice-bad-deploy.yaml`
- `chaos-frontend-cpu-saturation.yaml`
- `frontend-bad-cart-config.yaml`
- `chaos-cartservice-network-partition.yaml`

The crashloop and bad-config slices have been live-validated end to end in the local
cluster. The CPU slice has also been live exercised through the new Gate 0/Gate 1
watcher flow, but its sustained-high-load execution path is still timing-sensitive.
The network-partition slice is now implemented in the bounded v1 path and replay-tested.
It has also been exercised through a live operator run far enough to prove the approval
and safe-skip path, but the bounded `NetworkChaos` deletion has not yet been
live-validated end to end.

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
- a full CPU saturation recovery slice for `frontend`, including Chaos Mesh deletion, approval-gated execution, and verification
- a live-validated frontend bad-config recovery slice for `frontend`, including the `/cart` probe alert, approval-gated rollout rollback, and verified recovery
- a bounded network-partition recovery slice for `frontend -> cartservice`, including approval-gated NetworkChaos deletion and verification
- a partially live-exercised network-partition operator path that safely skipped execution when the incident signal had already cleared
- a HERALD 2.0 shadow path behind `engine_mode=v2_shadow`, including typed observation bundles, a read-only cluster observer, a shadow Reasoner with typed intents, a policy Critic, a bounded Synthesizer/compiler, post-execution shadow verification, and bounded shadow replanning
- a Phase 6 `v2_execute` pilot for the bounded chaos-delete families plus the rollback paths, where the approved CPU-saturation, network-partition, bad-config, and crashloop rollback actions now dispatch from the Synthesizer plan instead of the v1 hardcoded execution mapping
- replay artifacts and metrics for crashloop, CPU saturation, bad-config, and network-partition scenarios

Still not implemented end to end:

- durable workflow orchestration
- Slack-based approval flow
- full `v2_execute` cutover where Synthesizer-driven plans replace class-based v1 routing and execution beyond the current delete-and-rollback pilot
- a provider-backed replanning layer and true bounded multi-attempt `verify -> replan` loop on the v2 path

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
./.venv/bin/uvicorn services.alert_inbox_service:app --host 0.0.0.0 --port 8080
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

Add `--engine-mode v2_shadow` if you want the watcher to collect live observation
context plus shadow Reasoner, Critic, and Synthesizer output before bounded v1
planning, then attach shadow verifier and replanner results after any real bounded
execution without changing execution behavior.

For the current Phase 6 pilot, `--engine-mode v2_execute` makes the chaos-delete
actions for the CPU saturation and network-partition slices, plus the bad-config
and crashloop rollback paths, execute from the Synthesizer plan. Other actions
still route through the bounded v1 execution path in that mode.

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

You can also pass `--engine-mode v2_shadow` here to exercise the new shadow
pipeline while keeping the v1 execution corridor authoritative.

`--engine-mode v2_execute` is now partially live: the CPU saturation and
network-partition slices use the Synthesizer plan for execution, and the
bad-config plus crashloop rollback paths do too, while the crashloop restart
path still falls back to v1.

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

To exercise the HERALD 2.0 shadow path without changing execution behavior, add
`--engine-mode v2_shadow` to the same command. The workflow will record
`observe`, `reason`, `critique`, and `synthesize` nodes, include
`observation_bundle`, `reasoner_state`, `critic_state`, and `synthesizer_state`
in the result, and store nested shadow output under
`decision_trace.fixer_plan["v2_shadow"]` before handing off to the bounded v1 planner.
If you later approve from that saved first pass, the resumed run will append shadow
`verifier_state` and bounded `replanner_state` after the real v1 execution outcome.

To exercise the current Phase 6 cutover, use `--engine-mode v2_execute`. In that
mode the approved chaos-delete actions for CPU saturation and network partition,
plus the bad-config and crashloop rollback paths, dispatch from the Synthesizer
plan, while the crashloop restart path still falls back to the bounded v1
executor path.

What to look for:

- `hitl_decision.routing_decision`
- `hitl_decision.recommended_action`
- `decision_trace.human_approval` should be `n/a`
- `decision_trace.final_state` should be `pending_approval`

### Step 2: Run With Explicit Approval

Copy the `action_id` from `hitl_decision.recommended_action` in the first run and
pass it back with `--approve-action-id`. On the default heuristic path, the
recommended action is typically `rollout_undo_cartservice`.

The recommended approval flow resumes from the saved first-pass artifact so the
second command does not rerun Fixer or Judge. This second run approves the
selected action and allows the workflow to execute the bounded crashloop remediation:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --resume-from-file /tmp/herald-first-pass.json \
  --approve-action-id rollout_undo_cartservice \
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
  --reject-action-id rollout_undo_cartservice \
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

When using Gemini, action ordering, scores, and `action_id` values can vary. Run
the workflow once without approval, copy the recommended `action_id`, then resume
from that saved artifact for approval so Fixer and Judge are not called twice.

Gemini no-rerun approval flow:

```bash
./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --prometheus-base-url http://localhost:9090 \
  --fixer-provider gemini \
  --judge-provider gemini | tee /tmp/herald-first-pass.json
```

```bash
ACTION_ID="$(jq -r '.hitl_decision.recommended_action.action_id' /tmp/herald-first-pass.json)"

./.venv/bin/python -m workflows.recovery_workflow \
  --payload-file payloads/crashloop_alert.json \
  --prometheus-base-url http://localhost:9090 \
  --resume-from-file /tmp/herald-first-pass.json \
  --approve-action-id "$ACTION_ID" | tee /tmp/herald-approval-run.json
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
