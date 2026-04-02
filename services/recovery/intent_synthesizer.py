from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from schemas.critic import CriticOutput
from schemas.execution_plan import ExecutionPlan, SynthesisOutput
from schemas.intents import OperationIntent, ReasonerOutput
from services.recovery.kubectl_compiler import (
    compile_execution_plan,
    compile_v1_dispatch_preview,
)


def synthesize_execution_plans(
    reasoner_output: ReasonerOutput,
    critic_output: CriticOutput | None,
) -> SynthesisOutput:
    ordered_intents = _ordered_intents(reasoner_output.intents, critic_output)
    plans: list[ExecutionPlan] = []
    unsupported_intents: list[dict[str, Any]] = []
    warnings: list[str] = []
    critic_by_intent_id = _critic_candidates_by_intent_id(critic_output)

    for intent in ordered_intents:
        plan = compile_execution_plan(intent)
        candidate = critic_by_intent_id.get(intent.intent_id)
        if plan is None:
            unsupported_intents.append(
                {
                    "intent_id": intent.intent_id,
                    "operation_family": intent.operation_family,
                    "reason": "Intent family is outside the bounded execution corridor.",
                    "intent": intent.intent,
                    "target": _to_jsonable(intent.target),
                }
            )
            continue

        plan_warnings = _plan_warnings(intent, candidate)
        if plan_warnings:
            warnings.extend(plan_warnings)
            if intent.operation_family == "escalate.human_review" or _is_blocked_candidate(candidate):
                plan = ExecutionPlan(
                    intent_id=plan.intent_id,
                    operation_family=plan.operation_family,
                    target=plan.target,
                    summary=f"Shadow-only non-executable plan: {plan.summary}",
                    steps=list(plan.steps),
                    allowed_tool_names=list(plan.allowed_tool_names),
                    blast_radius_score=plan.blast_radius_score,
                    requires_approval=plan.requires_approval,
                    rollback_outline=dict(plan.rollback_outline),
                )
        plans.append(plan)

    summary = (
        f"Compiled {len(plans)} shadow execution plan(s) from {len(reasoner_output.intents)} intent(s)."
    )
    if unsupported_intents:
        warnings.append(f"{len(unsupported_intents)} intent(s) were unsupported by the bounded compiler.")
    if critic_output is not None and critic_output.global_concerns:
        warnings.extend(list(critic_output.global_concerns))

    return SynthesisOutput(
        summary=summary,
        plans=plans,
        unsupported_intents=unsupported_intents,
        warnings=_dedupe_strings(warnings),
    )


def compile_shadow_dispatches(synthesis_output: SynthesisOutput) -> list[dict[str, Any]]:
    return [
        compile_v1_dispatch_preview(plan)
        for plan in synthesis_output.plans
    ]


def _ordered_intents(
    intents: list[OperationIntent],
    critic_output: CriticOutput | None,
) -> list[OperationIntent]:
    if critic_output is None:
        return list(intents)

    intents_by_id = {intent.intent_id: intent for intent in intents}
    ordered: list[OperationIntent] = []
    seen: set[str] = set()
    for candidate in sorted(critic_output.candidates, key=lambda item: (item.recommended_rank, item.intent_id)):
        intent = intents_by_id.get(candidate.intent_id)
        if intent is None or intent.intent_id in seen:
            continue
        ordered.append(intent)
        seen.add(intent.intent_id)
    for intent in intents:
        if intent.intent_id not in seen:
            ordered.append(intent)
    return ordered


def _critic_candidates_by_intent_id(
    critic_output: CriticOutput | None,
) -> dict[str, Any]:
    if critic_output is None:
        return {}
    return {candidate.intent_id: candidate for candidate in critic_output.candidates}


def _plan_warnings(intent: OperationIntent, candidate: Any | None) -> list[str]:
    warnings: list[str] = []
    if intent.operation_family == "escalate.human_review":
        warnings.append("Human review intent is explicitly non-executable.")
    if intent.blast_radius_score >= 0.8:
        warnings.append("High blast radius candidate should not be treated as runnable.")
    if candidate is not None and not candidate.approved_for_consideration:
        warnings.append("Critic did not approve this candidate for consideration.")
    if candidate is not None and candidate.requires_escalation:
        warnings.append("Critic recommends escalation for this candidate.")
    return warnings


def _is_blocked_candidate(candidate: Any | None) -> bool:
    return bool(candidate is not None and (not candidate.approved_for_consideration or candidate.requires_escalation))


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
