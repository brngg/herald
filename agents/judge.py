from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from schemas.incident import Incident
from schemas.remediation import RemediationAction
from services.incident_normalization import normalize_incident_class
from services.judge_llm import JudgeLLM, JudgeLLMResult


JudgeVerdict = Literal["pass", "fail", "n/a"]


class JudgeAgentState(TypedDict):
    """Judge state for evaluating a Fixer plan."""

    incident: Incident
    evidence: dict[str, Any]
    incident_summary: str
    actions: list[RemediationAction]
    judge_verdict: JudgeVerdict
    judge_reason: str
    errors: list[str]
    final: bool

    fixer_rationale: NotRequired[str]
    judge_llm_reason: NotRequired[str]


def initial_judge_state(
    *,
    incident: Incident,
    evidence: dict[str, Any],
    incident_summary: str,
    actions: list[RemediationAction],
    fixer_rationale: str | None = None,
) -> JudgeAgentState:
    state: JudgeAgentState = {
        "incident": incident,
        "evidence": dict(evidence),
        "incident_summary": incident_summary,
        "actions": list(actions),
        "judge_verdict": "n/a",
        "judge_reason": "Judge has not evaluated the plan yet.",
        "errors": [],
        "final": False,
    }
    if isinstance(fixer_rationale, str) and fixer_rationale:
        state["fixer_rationale"] = fixer_rationale
    return state


def _infer_target_deployment(evidence: dict[str, Any]) -> str | None:
    labels = evidence.get("labels")
    if isinstance(labels, dict):
        for key in ("deployment", "app", "service"):
            value = labels.get(key)
            if isinstance(value, str) and value:
                return value

    deployment_hint = evidence.get("deployment_hint")
    if isinstance(deployment_hint, str) and deployment_hint:
        return deployment_hint

    pod = evidence.get("pod")
    if isinstance(pod, str) and pod:
        return pod.split("-", 1)[0]

    return None

def evaluate_plan_node(state: JudgeAgentState) -> dict[str, Any]:
    """Heuristic Judge for the current crashloop vertical slice."""

    actions = state.get("actions", [])
    evidence = state.get("evidence", {})
    incident = state["incident"]
    incident_class = normalize_incident_class(
        str(evidence.get("incident_class_normalized") or incident.incident_class)
    )
    target_deployment = _infer_target_deployment(evidence) or "cartservice"

    if not actions:
        return {
            "judge_verdict": "fail",
            "judge_reason": "Fixer proposed no remediation actions to evaluate.",
        }

    if incident_class != "crashloop":
        return {
            "judge_verdict": "fail",
            "judge_reason": f"Judge v0 only supports crashloop plans, not {incident_class!r}.",
        }

    allowed_action_types = {
        "rollout_undo_deployment",
        "rollout_restart_deployment",
        "escalate",
        "do_nothing",
    }

    for action in actions:
        if action.action_type not in allowed_action_types:
            return {
                "judge_verdict": "fail",
                "judge_reason": (
                    f"Action {action.action_id!r} has unsupported action_type "
                    f"{action.action_type!r} for crashloop Judge v0."
                ),
            }
        if not action.requires_approval:
            return {
                "judge_verdict": "fail",
                "judge_reason": (
                    f"Action {action.action_id!r} is missing requires_approval=true, "
                    "which violates the HITL Gate policy."
                ),
            }
        if action.blast_radius_score >= 0.8 and action.action_type not in {"escalate", "do_nothing"}:
            return {
                "judge_verdict": "fail",
                "judge_reason": (
                    f"Action {action.action_id!r} exceeds the blocked Blast Radius threshold."
                ),
            }
        if action.action_type in {"rollout_undo_deployment", "rollout_restart_deployment"}:
            action_namespace = action.parameters.get("namespace")
            action_deployment = action.parameters.get("deployment")
            incident_namespace = evidence.get("namespace") or "default"
            if action_namespace != incident_namespace or action_deployment != target_deployment:
                return {
                    "judge_verdict": "fail",
                    "judge_reason": (
                        f"Action {action.action_id!r} targets deployment "
                        f"{action_deployment!r} in namespace {action_namespace!r}, "
                        f"but the incident points to {target_deployment!r} in "
                        f"namespace {incident_namespace!r}."
                    ),
                }

    return {
        "judge_verdict": "pass",
        "judge_reason": (
            "Crashloop plan is bounded, reversible, approval-gated, and limited to "
            "supported v0 remediation actions."
        ),
    }


def finalize_judge_node(state: JudgeAgentState) -> dict[str, Any]:
    return {"final": True}


def make_llm_evaluate_node(llm: JudgeLLM) -> Any:
    """Create a Judge node that calls an injected LLM and falls back to heuristics."""

    def _node(state: JudgeAgentState) -> dict[str, Any]:
        errors = list(state.get("errors", []))
        heuristic = evaluate_plan_node(state)
        if heuristic["judge_verdict"] == "fail":
            return {
                "judge_verdict": heuristic["judge_verdict"],
                "judge_reason": heuristic["judge_reason"],
                "errors": errors,
            }
        try:
            result: JudgeLLMResult = llm.evaluate(
                incident_summary=state.get("incident_summary", ""),
                evidence=state.get("evidence", {}),
                actions=state.get("actions", []),
                fixer_rationale=state.get("fixer_rationale"),
            )
            if result.verdict == "fail":
                return {
                    "judge_verdict": result.verdict,
                    "judge_reason": result.reason,
                    "judge_llm_reason": result.reason,
                    "errors": errors,
                }
            return {
                "judge_verdict": heuristic["judge_verdict"],
                "judge_reason": heuristic["judge_reason"],
                "judge_llm_reason": result.reason,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"Judge LLM evaluate failed; falling back to heuristic: {exc}")
            return {
                "judge_verdict": heuristic["judge_verdict"],
                "judge_reason": heuristic["judge_reason"],
                "errors": errors,
            }

    return _node


def run_judge_pipeline(
    *,
    incident: Incident,
    evidence: dict[str, Any],
    incident_summary: str,
    actions: list[RemediationAction],
    fixer_rationale: str | None = None,
    llm: JudgeLLM | None = None,
) -> JudgeAgentState:
    state = initial_judge_state(
        incident=incident,
        evidence=evidence,
        incident_summary=incident_summary,
        actions=actions,
        fixer_rationale=fixer_rationale,
    )
    if llm is None:
        state.update(evaluate_plan_node(state))
    else:
        state.update(make_llm_evaluate_node(llm)(state))
    state.update(finalize_judge_node(state))
    return state
