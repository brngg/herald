# HERALD Vision

## Where HERALD Is Now

HERALD is no longer just a planning demo. It already proves a bounded Verified Recovery
control-plane flow for two incident classes:

- `crashloop` on `cartservice`
- `cpu_saturation` on `frontend`

For those slices, the repo already supports:

- alert parsing into a structured `Incident`
- Fixer plan generation
- Judge evaluation
- HITL Gate routing
- terminal approve/reject flow
- pre-check verification
- spawned Gemini execution agent with bounded tools
- rollout or chaos-removal execution
- rollout wait when applicable
- post-check verification
- `DecisionTrace` finalization with provenance
- replay scenarios and metrics output

This is the current proof point:

> HERALD can take some Kubernetes incidents from alert to approved, bounded, and
> verified recovery end to end.

What HERALD is **not** yet:

- a general incident solver
- a durable workflow platform
- a real async approval product
- a deployed remote execution-worker system

Right now, HERALD is best understood as a bounded recovery control plane prototype
with real end-to-end slices.

---

## Current Demo Thesis

The current demo is intentionally narrow:

- HERALD operates inside explicitly supported incident corridors
- every remediation is approval-gated
- execution is bounded and reversible
- recovery is only considered successful after verification
- the `DecisionTrace` is the audit trail for the full lifecycle

This is a feature, not a weakness. The purpose of the current phase is to prove
that approval-gated Verified Recovery works end to end before broadening the action
surface.

---

## Next Milestone

The next major milestone is:

## Complete all 4 supported demo slices

That means finishing the same end-to-end path for all four intended incident classes:

1. `CrashLoopBackOff` on `cartservice`
2. high CPU saturation on `frontend`
3. bad frontend dependency config (`CART_SERVICE_ADDR`)
4. dependency disruption between `frontend` and `cartservice`

For each slice, HERALD should support:

- alert payload
- Fixer
- Judge
- HITL Gate
- approve/reject flow
- bounded execution worker
- verification
- `DecisionTrace`
- replay scenario fixtures
- demo helper script

When this milestone is complete, HERALD will no longer feel like a one-path proof
of concept. It will feel like a real architecture that generalizes across multiple
failure modes.

---

## After The 4-Demo Milestone

Once all four bounded slices are working, the next phase should be control-plane
hardening rather than adding more incident classes immediately.

### 1. Temporal / durable workflow layer

Move the current in-process workflow shape into a more durable orchestration model.

Goal:

- resumable workflow execution
- better operator handoff
- cleaner lifecycle for long-running approval waits
- stronger workflow state management than the current local CLI path

### 2. Real async HITL surface

Replace terminal-only approval with a real approval interface, likely Slack first.

Goal:

- approval requests outside the terminal
- explicit approve/reject events
- better operator experience
- workflow pause/resume without rerunning local commands manually

### 3. Better execution-worker boundary

Today the worker is a spawned local process. After the demo phase, HERALD should
move toward a more explicit worker model.

Goal:

- worker dispatch as a real control-plane event
- worker report-back as structured output
- worker retirement as part of the workflow lifecycle

This does not have to be “production” immediately, but it should look less like a
local demo subprocess and more like an actual execution boundary.

---

## Path To The Broader Vision

The broader vision is **not** “let the model do anything.”

The broader vision is:

> HERALD can derive or assemble the remediation mechanism at runtime, instead of
> only choosing from a fixed menu, while still preserving approval, bounded execution,
> rollback, and verification.

The path from the current system to that future should be staged.

### Stage 1: Fixed bounded actions

This is where HERALD is now.

- predefined action types
- predefined worker tools
- incident-specific verification logic

### Stage 2: Safe primitives

Break larger actions into reusable low-level primitives.

Examples:

- read workload context
- inspect env/config
- inspect rollout status
- patch a bounded field
- restart workload
- undo rollout
- delete a known chaos object
- run verification checks

At this stage, HERALD still executes only known-safe primitives.

### Stage 3: Structured mutation plans

Move from “pick one named action” to “propose an exact bounded mutation plan.”

That plan should include:

- target resource
- ordered mutation steps
- rollback steps
- verification steps
- blast-radius explanation
- approval summary

### Stage 4: Plan-level Judge + exact-plan approval

Judge should evaluate the full mutation plan, not just a small action label.
The operator should approve the exact plan that will run.

### Stage 5: Runtime plan assembly from safe primitives

At that point, HERALD can begin assembling the remediation mechanism at runtime
from safe building blocks.

That means:

- more adaptive than today
- still bounded
- still approval-gated
- still verification-driven

### Stage 6: Controlled plan synthesis

Only after the earlier stages are stable should HERALD move toward more flexible
runtime-derived remediations.

Even then, the system should remain constrained by:

- approved primitive set
- explicit rollback path
- Judge review
- HITL approval
- post-check verification

The likely end state is **dynamic plan generation over static safe primitives**,
not unconstrained self-extending tooling.

---

## What Must Stay True At Every Stage

No matter how adaptive HERALD becomes later, these rules should remain constant:

- HERALD does not silently execute broad or ambiguous changes
- the human always approves before execution
- verification decides whether recovery succeeded
- rollback and escalation stay first-class outcomes
- the `DecisionTrace` remains the source of truth

If future work violates those constraints, HERALD may become more flexible, but it
will stop being the project it is supposed to be.

---

## Practical Roadmap Summary

1. Finish the remaining 2 supported demo slices.
2. Make all 4 slices replayable, measurable, and demo-ready.
3. Add real async HITL and durable workflow orchestration.
4. Strengthen the execution-worker boundary.
5. Refactor from named actions toward safe primitives.
6. Introduce structured mutation plans.
7. Let HERALD assemble approved plans from bounded primitives at runtime.

That is the bridge from the current bounded demo to the broader infrastructure
recovery vision.
