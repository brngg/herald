from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemas.critic import PolicyCheckResult
from schemas.intents import OperationIntent


DEFAULT_BLAST_RADIUS_BLOCK_THRESHOLD = 0.8
LOW_BLAST_RADIUS_PREFERENCE_THRESHOLD = 0.5


@dataclass(slots=True)
class PolicyValidationResult:
    intent_id: str
    approved_for_consideration: bool
    concerns: list[str] = field(default_factory=list)
    policy_checks: list[PolicyCheckResult] = field(default_factory=list)
    requires_escalation: bool = False
    policy_score: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, str):
            raise TypeError("intent_id must be a str")
        if not self.intent_id:
            raise ValueError("intent_id must be non-empty")
        if not isinstance(self.approved_for_consideration, bool):
            raise TypeError("approved_for_consideration must be a bool")
        if not isinstance(self.concerns, list):
            raise TypeError("concerns must be a list[str]")
        for concern in self.concerns:
            if not isinstance(concern, str):
                raise TypeError("concerns must contain only strings")
            if not concern:
                raise ValueError("concerns must not contain empty strings")
        if not isinstance(self.policy_checks, list):
            raise TypeError("policy_checks must be a list[PolicyCheckResult]")
        for policy_check in self.policy_checks:
            if not isinstance(policy_check, PolicyCheckResult):
                raise TypeError("policy_checks must contain only PolicyCheckResult values")
        if not isinstance(self.requires_escalation, bool):
            raise TypeError("requires_escalation must be a bool")
        if not _is_number(self.policy_score):
            raise TypeError("policy_score must be a float-compatible number")
        self.policy_score = float(self.policy_score)


def validate_shadow_intent_policies(
    intents: list[OperationIntent],
    *,
    blast_radius_block_threshold: float = DEFAULT_BLAST_RADIUS_BLOCK_THRESHOLD,
    low_blast_radius_preference_threshold: float = LOW_BLAST_RADIUS_PREFERENCE_THRESHOLD,
) -> list[PolicyValidationResult]:
    results = [
        _validate_single_intent(
            intent,
            blast_radius_block_threshold=blast_radius_block_threshold,
            low_blast_radius_preference_threshold=low_blast_radius_preference_threshold,
        )
        for intent in intents
    ]

    any_unsafe = any(not result.approved_for_consideration or result.requires_escalation for result in results)
    if any_unsafe:
        for result, intent in zip(results, intents):
            if intent.operation_family == "escalate.human_review":
                result.approved_for_consideration = True
                result.requires_escalation = True
                result.concerns.append("Escalation is preferred because one or more intents violate policy bounds.")
                result.policy_score += 30.0

    return sorted(results, key=lambda item: (-item.policy_score, item.intent_id))


def summarize_policy_validation(
    results: list[PolicyValidationResult],
    *,
    blast_radius_block_threshold: float = DEFAULT_BLAST_RADIUS_BLOCK_THRESHOLD,
) -> dict[str, Any]:
    approved_ids = [result.intent_id for result in results if result.approved_for_consideration]
    escalated_ids = [result.intent_id for result in results if result.requires_escalation]
    blocked_ids = [result.intent_id for result in results if not result.approved_for_consideration]
    return {
        "total_candidates": len(results),
        "approved_candidate_count": len(approved_ids),
        "approved_candidate_ids": approved_ids,
        "blocked_candidate_ids": blocked_ids,
        "escalation_recommended": bool(escalated_ids),
        "escalation_candidate_ids": escalated_ids,
        "block_threshold": blast_radius_block_threshold,
        "best_candidate_id": results[0].intent_id if results else None,
        "best_candidate_score": results[0].policy_score if results else None,
    }


def _validate_single_intent(
    intent: OperationIntent,
    *,
    blast_radius_block_threshold: float,
    low_blast_radius_preference_threshold: float,
) -> PolicyValidationResult:
    checks: list[PolicyCheckResult] = []
    concerns: list[str] = []

    approval_passed = intent.requires_approval is True
    checks.append(
        PolicyCheckResult(
            policy_name="requires_approval_enforced",
            passed=approval_passed,
            reason=(
                "Intent already requires human approval."
                if approval_passed
                else "Intent must require human approval in the shadow Critic."
            ),
        )
    )

    blast_radius_passed = intent.blast_radius_score < blast_radius_block_threshold
    checks.append(
        PolicyCheckResult(
            policy_name="blast_radius_below_block_threshold",
            passed=blast_radius_passed,
            reason=(
                f"Blast Radius {intent.blast_radius_score:.2f} is below the blocking threshold."
                if blast_radius_passed
                else f"Blast Radius {intent.blast_radius_score:.2f} reaches or exceeds the blocking threshold."
            ),
        )
    )

    reversible_passed = intent.reversible is True
    checks.append(
        PolicyCheckResult(
            policy_name="reversible_preferred",
            passed=reversible_passed,
            reason=(
                "Intent is reversible."
                if reversible_passed
                else "Intent is not reversible and should be deprioritized."
            ),
        )
    )

    namespaced_passed = intent.operation_family == "escalate.human_review" or intent.target.namespace is not None
    checks.append(
        PolicyCheckResult(
            policy_name="namespaced_target_preferred",
            passed=namespaced_passed,
            reason=(
                "Intent targets a namespaced resource."
                if namespaced_passed
                else "Intent does not include a namespace and should be deprioritized."
            ),
        )
    )

    low_blast_passed = intent.blast_radius_score <= low_blast_radius_preference_threshold
    checks.append(
        PolicyCheckResult(
            policy_name="low_blast_radius_preferred",
            passed=low_blast_passed,
            reason=(
                f"Blast Radius {intent.blast_radius_score:.2f} is within the preferred range."
                if low_blast_passed
                else f"Blast Radius {intent.blast_radius_score:.2f} is above the preferred range."
            ),
        )
    )

    approved_for_consideration = approval_passed and blast_radius_passed and namespaced_passed
    requires_escalation = (not approved_for_consideration) or not reversible_passed or not low_blast_passed
    if not approval_passed:
        concerns.append("Intent violates the approval requirement.")
    if not blast_radius_passed:
        concerns.append("Intent exceeds the Blast Radius block threshold.")
    if not reversible_passed:
        concerns.append("Intent is not reversible.")
    if not namespaced_passed:
        concerns.append("Intent does not target a namespaced resource.")
    if not low_blast_passed:
        concerns.append("Intent is not in the preferred low Blast Radius range.")

    policy_score = 0.0
    policy_score += 45.0 if approval_passed else -40.0
    policy_score += 35.0 if blast_radius_passed else -60.0
    policy_score += 20.0 if reversible_passed else -15.0
    policy_score += 15.0 if namespaced_passed else -10.0
    policy_score += 10.0 if low_blast_passed else -5.0
    policy_score += max(0.0, intent.confidence_score * 10.0)
    if intent.operation_family == "escalate.human_review":
        policy_score += 5.0

    return PolicyValidationResult(
        intent_id=intent.intent_id,
        approved_for_consideration=approved_for_consideration,
        concerns=concerns,
        policy_checks=checks,
        requires_escalation=requires_escalation,
        policy_score=policy_score,
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
