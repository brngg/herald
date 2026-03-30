# HERALD 2.0 Implementation Roadmap

This roadmap assumes the current HERALD codebase remains functional for the bounded
supported slices while HERALD 2.0 is introduced incrementally behind a parallel,
shippable path.

The target architecture is:

```text
observe -> reason -> plan -> critique -> HITL -> synthesize -> execute -> verify -> replan
```

The five hard gates to remove from the core path are:

1. `normalize_incident_class` used as a routing gate
2. Closed `ActionTypes` enum in `schemas/remediation.py`
3. Judge per-class action allowlists in `agents/judge.py`
4. `recovery_workflow.py` dispatch by incident class
5. Executor hardcoded `action_type -> kubectl/tool` mapping

The following must be preserved throughout the migration:

- HITL Gate on every action
- pre-check / post-check verification hooks
- `requires_approval=True` enforcement
- `blast_radius_score` threshold enforcement
- LangGraph node-based pipeline structure

## Phase 1: Add An Observation-First Substrate

### 1. Goal

Introduce a real `observe` stage before planning, so HERALD 2.0 can reason from live
cluster state instead of only alert labels. v1 remains the default execution path.

### 2. Files Changed

- `workflows/recovery_workflow.py`
  - Add `engine_mode` (`v1`, `v2_shadow`, later `v2_execute`)
  - Call an observation step before planning when mode is not `v1`
  - Preserve existing v1 flow unchanged by default
- `workflows/operator_inbox.py`
  - Thread `engine_mode` through Gate 0 and Gate 1
  - Preserve current CLI defaults as `v1`
- `services/kubernetes_client.py`
  - Add read-only methods:
    - `get_resource_json(...)`
    - `list_pods(...)`
    - `get_events(...)`
    - `get_pod_logs(...)`
    - `get_rollout_history(...)`
    - `get_service_endpoints(...)`
- `services/prometheus_client.py`
  - Add generic:
    - `query(...)`
    - `range_query(...)`
    - raw metric snapshot helpers
  - Keep current slice-specific pre/post-checks intact
- `services/alertmanager_client.py`
  - Preserve the full single-alert payload as the seed for observation
- `schemas/decision_trace.py`
  - Add node names:
    - `observe`
    - `reason`
    - `critique`
    - `synthesize`
    - `verify`
    - `replan`
- `services/decision_trace_provenance.py`
  - Accept and serialize the new node names

### 3. New Files

- `schemas/observations.py`
  - Purpose: typed representation of live investigation context
  - Interface:
    - `observation_bundle_from_dict(payload: dict[str, Any]) -> ObservationBundle`
- `services/cluster_observer.py`
  - Purpose: collect live cluster observations before any v2 reasoning
  - Interface:
    - `class ClusterObserver`
    - `collect(*, incident: Incident, namespace_hint: str | None = None) -> ObservationBundle`
- `workflows/recovery_graph_v2.py`
  - Purpose: host the new LangGraph-based v2 pipeline
  - Interface:
    - `build_recovery_graph_v2() -> Any`
  - Initial implementation only needs:
    - `observe -> handoff_to_v1`

### 4. What Gets Deleted or Deprecated

- Nothing is removed in this phase
- `normalize_incident_class` is documented as “hint-only in v2” but still used by v1

### 5. Tests

Existing tests:

- `tests/integration/test_recovery_workflow.py`
  - Update only as needed to tolerate an optional `observe` node when `engine_mode != v1`

New tests:

- `tests/unit/test_cluster_observer.py`
- `tests/unit/test_observations_schema.py`
- `tests/integration/test_recovery_graph_v2_observe_only.py`

All current v1 tests must continue passing:

- `tests/unit/test_fixer.py`
- `tests/unit/test_judge.py`
- current replay and workflow tests

### 6. LLM Prompt Strategy

No new LLM node in this phase.

The observation layer is deterministic and read-only.

### 7. Risk

- Prompt context can become too large if logs and events are collected without summarization
- Observer latency can make Gate 0 feel slow
- Raw logs can leak secrets unless the observer redacts tokens, auth headers, and env-var values

## Phase 2: Introduce A Reasoner And Structured Operation Intents In Shadow Mode

### 1. Goal

Replace “classify then pick an action” with “observe then diagnose then emit ranked
intents,” but only in shadow mode at first. v1 Fixer remains authoritative for execution.

### 2. Files Changed

- `workflows/recovery_graph_v2.py`
  - Add a `reason` node after `observe`
- `workflows/recovery_workflow.py`
  - Run the v2 Reasoner in `v2_shadow`
  - Store v2 output in `DecisionTrace.fixer_plan` alongside the v1 Fixer output
- `workflows/operator_inbox.py`
  - Thread `engine_mode`
- `schemas/decision_trace.py`
  - Allow richer `fixer_plan` contents and observation refs for the `reason` node

### 3. New Files

- `schemas/intents.py`
  - Purpose: define the planning contract for v2
  - Types:
    - `ResourceTarget`
    - `OperationIntent`
    - `ReasonerOutput`
    - `CapabilityCatalog`
  - Interface:
    - `reasoner_output_from_dict(payload: dict[str, Any]) -> ReasonerOutput`
- `agents/reasoner.py`
  - Purpose: run the v2 reasoning pipeline
  - Interface:
    - `run_reasoner_pipeline(incident: Incident, observations: ObservationBundle, llm: ReasonerLLM) -> dict[str, Any]`
- `services/reasoner_llm.py`
  - Purpose: provider-agnostic Reasoner contract
  - Interface:
    - `ReasonerLLM`
    - `ReasonerLLMResult`
    - `build_reasoner_prompts(...)`
    - `reasoner_output_json_schema()`
- `services/gemini_reasoner_llm.py`
- `services/openai_reasoner_llm.py`
  - Purpose: provider implementations
- `services/capability_catalog.py`
  - Purpose: central capability description for the Reasoner
  - Interface:
    - `default_capability_catalog() -> CapabilityCatalog`
- `services/intent_compat.py`
  - Purpose: map v2 intents into v1-compatible remediation actions for parity analysis
  - Interface:
    - `intent_to_v1_remediation(intent: OperationIntent) -> RemediationAction | None`

### 4. What Gets Deleted or Deprecated

- `agents/fixer.py:propose_actions_node` is deprecated as the planner for v2
- It remains the default and authoritative planner for v1
- `services/fixer_llm.py` remains intact for v1

### 5. Tests

Existing v1 Fixer tests remain unchanged.

New tests:

- `tests/unit/test_reasoner_llm.py`
- `tests/unit/test_intents.py`
- `tests/unit/test_intent_compat.py`
- `tests/integration/test_reasoner_shadow_mode.py`

Replay assertions needed:

- `v2_shadow` produces at least one valid intent for:
  - `crashloop`
  - `cpu_saturation`
  - `bad_config`
- v1 still decides the executed action

### 6. LLM Prompt Strategy

System prompt intent:

- “You are the HERALD Reasoner.”
- Diagnose the Kubernetes incident from live observations
- Produce 1-3 ranked recovery intents
- Do not emit kubectl syntax
- Use the capability catalog as a description of what the platform can do, not as a fixed menu
- Always set `requires_approval=true`
- Prefer namespaced, reversible, low-blast-radius operations

Context passed:

- incident summary
- raw alert labels/annotations/fingerprint
- `ObservationBundle` sections
- optional `incident_class_hint` from `normalize_incident_class`
- capability catalog text

Returned schema:

- `ReasonerOutput`
  - `diagnosis_summary`
  - `likely_causes[]`
  - `missing_information[]`
  - `intents[]`

Each `OperationIntent` includes:

- `intent_id`
- `intent`
- `operation_family`
- `target`
- `arguments`
- `reversible`
- `confidence_score`
- `blast_radius_score`
- `requires_approval`
- `verification_hints`
- `rollback_hints`

### 7. Risk

- The Reasoner can hallucinate non-existent resources
- It can propose unsupported `operation_family` values
- If shadow outputs are not logged separately, parity analysis will be weak
- If capability text is too permissive, the model may jump to high-blast-radius ideas

## Phase 3: Replace The Judge’s Per-Class Allowlists With A Policy Critic

### 1. Goal

Stop judging plans by incident class and action allowlist. Start judging them by
policy, scope, reversibility, and blast radius.

### 2. Files Changed

- `workflows/recovery_graph_v2.py`
  - Add a `critique` node after `reason`
- `workflows/hitl_gate.py`
  - Add `route_intent_plan(...)`
  - Preserve `route_plan(...)` for v1
- `workflows/recovery_workflow.py`
  - Record both deterministic policy validation and Critic output in `DecisionTrace`
- `schemas/decision_trace.py`
  - Store `critique` node outputs
- `services/decision_trace_provenance.py`
  - Serialize critique outputs
- `services/judge_llm.py`
  - Leave as v1-only to avoid semantic confusion

### 3. New Files

- `agents/critic.py`
  - Purpose: run policy + LLM critique for v2 intents
  - Interface:
    - `run_critic_pipeline(observations: ObservationBundle, intents: list[OperationIntent], llm: CriticLLM | None = None) -> dict[str, Any]`
- `services/critic_llm.py`
  - Purpose: provider-agnostic Critic contract
  - Interface:
    - `CriticLLM`
    - `CriticLLMResult`
    - `build_critic_prompts(...)`
    - `critic_output_json_schema()`
- `services/gemini_critic_llm.py`
  - Purpose: provider implementation
- `services/policy_validator.py`
  - Purpose: deterministic policy checks before any LLM critique is trusted
  - Interface:
    - `validate_intent(intent: OperationIntent, observations: ObservationBundle) -> PolicyValidationResult`
- `schemas/critic.py`
  - Purpose: typed Critic + policy outputs
  - Types:
    - `PolicyValidationResult`
    - `CriticIntentVerdict`
    - `CriticOutput`

### 4. What Gets Deleted or Deprecated

- `agents/judge.py:evaluate_plan_node` is deprecated for v2
- It remains only as the v1 Judge
- The per-class allowlists in `agents/judge.py` remain but are no longer on the v2 path

### 5. Tests

Existing:

- `tests/unit/test_judge.py` stays for v1

New:

- `tests/unit/test_policy_validator.py`
- `tests/unit/test_critic_llm.py`
- `tests/integration/test_critic_shadow_mode.py`

New integration guarantees:

- `requires_approval=True` still hard-fails when absent
- `blast_radius_score >= 0.8` still escalates or blocks even if the Critic LLM says pass

### 6. LLM Prompt Strategy

System prompt intent:

- “You are the HERALD Critic.”
- Review candidate recovery intents against policy
- Do not invent new operations
- Fail intents that:
  - are not namespace-scoped without strong justification
  - are not reversible
  - omit verification or rollback hints
  - target unrelated resources
  - exceed blast-radius policy unless they escalate

Context passed:

- observation bundle
- reasoner output
- policy thresholds from `workflows/hitl_gate.py`
- capability catalog

Returned schema:

- `CriticOutput`
  - `overall_verdict`
  - `recommended_intent_id`
  - `intents[]`

Each item includes:

- `intent_id`
- `verdict`
- `reason`
- `policy_flags`
- `adjusted_confidence_score`
- `adjusted_blast_radius_score`
- `requires_human_warning`

### 7. Risk

- A pure LLM Critic will be inconsistent
- Deterministic validation and LLM critique can disagree
- The deterministic validator must remain authoritative until parity is proven

## Phase 4: Add A Synthesizer/Compiler And Generic Execution Plans

### 1. Goal

Replace hardcoded `action_type -> kubectl/tool` mappings with a compiler that turns
approved intents into executable plans. v2 can execute a supported subset of intents
while unsupported ones remain approval-only suggestions.

### 2. Files Changed

- `workflows/recovery_graph_v2.py`
  - Add `synthesize` and `execute` nodes
- `workflows/recovery_workflow.py`
  - Dispatch approved v2 intents into the compiler instead of `_continue_*_recovery`
- `workflows/operator_inbox.py`
  - Change Gate 1 from “approve action id” to “approve exact execution plan” when `engine_mode=v2_execute`
- `services/kubernetes_client.py`
  - Add generic mutation methods:
    - `patch_resource(...)`
    - `scale_resource(...)`
    - `delete_resource(...)`
    - `apply_manifest(...)`
    - `cordon_node(...)`
    - `drain_node(...)`
- `services/execution_worker.py`
  - Move from `ExecutionDispatch(action_type, allowed_tool_names)` to `ExecutionPlan(steps)`
- `schemas/execution.py`
  - Refactor toward generic execution primitives or relegate to compatibility mode
- `services/gemini_execution_agent.py`
  - No longer the primary v2 executor

### 3. New Files

- `schemas/execution_plan.py`
  - Purpose: define compiled plans and steps
  - Types:
    - `ExecutionPlan`
    - `ExecutionStep`
    - `CompiledCommand`
    - `ExecutionStepResult`
  - Interface:
    - `execution_plan_from_dict(payload: dict[str, Any]) -> ExecutionPlan`
- `services/intent_synthesizer.py`
  - Purpose: compile an approved intent into an execution plan
  - Interface:
    - `synthesize(intent: OperationIntent, observations: ObservationBundle) -> ExecutionPlan`
- `services/kubectl_compiler.py`
  - Purpose: translate generic execution steps into `kubectl` commands
  - Interface:
    - `compile_step(step: ExecutionStep) -> list[str]`
- `services/operation_executor.py`
  - Purpose: execute a compiled plan through KubernetesClient
  - Interface:
    - `execute(plan: ExecutionPlan, kubernetes_client: KubernetesClient) -> ExecutionResult`

### 4. What Gets Deleted or Deprecated

- `_allowed_tool_names_for_action`
- `_build_execution_dispatch`
- `ExecutionActionType`
- `ExecutionToolName`
- hardcoded action-type subsets in `schemas/execution.py`

All are deprecated on the v2 path.

- `services/gemini_execution_agent.py` becomes:
  - v1-only executor
  - or optional post-execution explainer

### 5. Tests

Current tests likely to break:

- `tests/unit/test_execution_schema.py`
- `tests/unit/test_execution_worker.py`

New tests:

- `tests/unit/test_execution_plan.py`
- `tests/unit/test_intent_synthesizer.py`
- `tests/unit/test_kubectl_compiler.py`
- `tests/integration/test_v2_execute_supported_families.py`

v1 execution tests must stay green under `engine_mode=v1`.

### 6. LLM Prompt Strategy

No new LLM node in this phase.

The compiler is deterministic. The model has already produced the intent.

The safety boundary becomes:

- HITL approves exact plan
- deterministic compiler emits exact commands

### 7. Risk

- Compiler bugs are dangerous because they change real commands
- Target ambiguity between `Deployment`, `ReplicaSet`, `Pod`, and `StatefulSet` can produce the wrong operation
- Generic mutation methods must reject cluster-wide or stateful changes unless explicitly allowed by deterministic policy

## Phase 5: Make Verification Intent-Driven And Add Replanning

### 1. Goal

Replace class-specific workflow branches with a generic `verify -> replan or finalize`
loop. A failed verification should produce a new observation cycle instead of immediate
terminal escalation.

### 2. Files Changed

- `workflows/recovery_graph_v2.py`
  - Add `verify` and `replan` nodes
  - Add a bounded loop counter
- `workflows/recovery_workflow.py`
  - Stop calling `_continue_crashloop_recovery`
  - Stop calling `_continue_cpu_saturation_recovery`
  - Stop calling `_continue_bad_config_recovery`
  - on the v2 path only
- `services/prometheus_client.py`
  - Add generic verification helpers:
    - `query_alert_signal(...)`
    - `query_readiness(...)`
    - `query_probe(...)`
    - `evaluate_verification_plan(...)`
- `services/kubernetes_client.py`
  - Add generic post-mutation checks:
    - rollout status
    - resource readiness
    - event deltas
- `schemas/decision_trace.py`
  - Store repeated observe/reason/critique/synthesize/verify cycles with attempt numbers
- `services/decision_trace_provenance.py`
  - Record loop iterations cleanly

### 3. New Files

- `schemas/verification.py`
  - Purpose: typed verification plans and results
  - Types:
    - `VerificationPlan`
    - `VerificationCheck`
    - `VerificationResultV2`
  - Interface:
    - `verification_plan_from_dict(payload: dict[str, Any]) -> VerificationPlan`
- `services/verification_engine.py`
  - Purpose: run generic verification plans
  - Interface:
    - `run_verification(plan: VerificationPlan, observations: ObservationBundle | None = None) -> dict[str, Any]`
- `agents/replanner.py`
  - Purpose: propose next intents after failed verification
  - Interface:
    - `run_replanner_pipeline(...) -> dict[str, Any]`
- `services/replanner_llm.py`
  - Purpose: provider-agnostic Replanner contract

### 4. What Gets Deleted or Deprecated

- On the v2 path, `normalize_incident_class` is now only stored as `incident_class_hint`
- The class-specific workflow continuations are marked deprecated and stop receiving new features
- The old pre/post-check helpers remain only for v1 until cutover

### 5. Tests

Current assumptions that will break:

- `tests/integration/test_recovery_workflow.py` assumes one execution attempt and one finalization path

Add:

- `tests/integration/test_recovery_workflow_v2.py`
- `tests/unit/test_verification_engine.py`
- `tests/unit/test_replanner_llm.py`
- `tests/integration/test_v2_replan_after_failed_verification.py`

Also add regression tests to ensure:

- the loop stops after a bounded attempt count
- the system escalates instead of retrying forever

### 6. LLM Prompt Strategy

System prompt intent:

- “You are the HERALD Replanner.”
- An approved execution plan ran and verification did not recover the incident
- Use the latest observations plus the failed execution transcript to decide whether to:
  - propose a new intent
  - request more investigation
  - escalate
- Do not repeat the same plan unless new evidence materially changes the diagnosis

Context passed:

- original alert
- latest observation bundle
- prior intents and critic outputs
- approved execution plan
- execution transcript
- verification result
- attempt count

Returned schema:

- `ReplanOutput`
  - `decision`
  - `rationale`
  - `intents[]`
  - `stop_reason`

Where `decision` is one of:

- `propose_new_intent`
- `escalate`
- `no_action`

### 7. Risk

- Replanning can create loops
- It can duplicate mutations
- It can produce contradictory diagnoses if prior failed intents are not fed back explicitly
- Verification drift can cause the system to solve the wrong symptom if verification plans are too loose

## Phase 6: Cut Over To HERALD 2.0 And Deprecate The v1 Ladders

### 1. Goal

Make the v2 graph the default execution path, keep v1 only as a compatibility mode for
replay and regression comparison, and explicitly retire the five hard gates from the
core path.

### 2. Files Changed

- `workflows/recovery_workflow.py`
  - Make `engine_mode=v2_execute` the default
  - Reduce to a thin wrapper around `workflows/recovery_graph_v2.py`
- `workflows/operator_inbox.py`
  - Default Gate 0 and Gate 1 to v2
- `agents/fixer.py`
  - Freeze as `v1_fixer` compatibility or reduce to a wrapper
- `agents/judge.py`
  - Freeze as `v1_judge` compatibility or reduce to a wrapper
- `services/fixer_llm.py`
  - Document as v1-only
- `services/judge_llm.py`
  - Document as v1-only
- `services/incident_normalization.py`
  - Retain only as context enrichment helper
- `schemas/remediation.py`
  - Convert to compatibility adapter instead of core planning contract
- `README.md`
  - Update architecture description to the v2 loop

### 3. New Files

- `services/v1_compat.py` (optional but recommended)
  - Purpose: isolate the legacy path
  - Interface:
    - `run_v1_recovery(...) -> dict[str, Any]`

### 4. What Gets Deleted or Deprecated

Formally removed from the core path:

- `normalize_incident_class` as a routing gate
- `ActionTypes` as the planner contract
- `evaluate_plan_node` allowlists on the default path
- `_continue_crashloop_recovery`
- `_continue_cpu_saturation_recovery`
- `_continue_bad_config_recovery`
- `_allowed_tool_names_for_action`
- hardcoded executor `action_type` mapping

They may remain temporarily behind `engine_mode=v1`.

### 5. Tests

Split the suite into:

- `v1_compat`
- `v2_default`

Required migration coverage:

- replay parity for:
  - `crashloop`
  - `cpu_saturation`
  - `bad_config`
- saved `first-pass.json` from v1 cannot be resumed as v2 without an explicit compatibility adapter
- saved v2 artifacts cannot be resumed by the v1 path without an explicit compatibility adapter

### 6. LLM Prompt Strategy

No new node in this phase.

This phase is about cutover, defaults, and deprecation.

The Reasoner, Critic, and Replanner prompts from earlier phases become canonical.

### 7. Risk

- Cutting over too early can break the strongest existing demos
- If parity is not established first, the project will feel less reliable after the migration
- Do not make v2 default until replay parity exists for:
  - `crashloop`
  - `cpu_saturation`
  - `bad_config`
- And until the v2 path has at least one live-validated end-to-end run per slice

## Recommended Rollout Order

1. Ship Phase 1 with `engine_mode=v2_shadow` and no execution changes
2. Ship Phase 2 and Phase 3 together, but keep v1 execution authoritative
3. Ship Phase 4 only for a narrow compiler subset first:
   - `rollout_restart`
   - `rollout_undo`
   - `delete_resource`
   - `scale_resource`
   - namespaced `patch_resource`
4. Ship Phase 5 after the compiler subset is stable
5. Ship Phase 6 only after parity and live validation

## Migration Of The Five Hard Gates

- `normalize_incident_class`
  - moves from routing gate to `incident_class_hint` in Phase 1
  - stops routing the default path in Phase 6
- `ActionTypes`
  - is superseded by `OperationIntent` in Phase 2
  - becomes compatibility-only in Phase 6
- `evaluate_plan_node` allowlists
  - are superseded by `PolicyValidator + Critic` in Phase 3
  - are removed from the default path in Phase 6
- `recovery_workflow` dispatch by class
  - is superseded by the v2 graph in Phase 5
  - is removed from the default path in Phase 6
- executor `action_type -> kubectl`
  - is superseded by `IntentSynthesizer + kubectl_compiler` in Phase 4
  - is removed from the default path in Phase 6
