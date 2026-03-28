from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from typing import Any, NotRequired, TypedDict

from schemas.incident import Incident
from schemas.remediation import RemediationAction
from services.fixer_llm import FixerLLM, FixerLLMResult
from services.incident_normalization import normalize_incident_class


class FixerAgentState(TypedDict):
    """Fixer state passed between LangGraph nodes.

    Keep the full Incident in state; derive normalized fields into `evidence`.
    """

    incident: Incident
    evidence: dict[str, Any]
    incident_summary: str
    actions: list[RemediationAction]
    raw_plan: str
    errors: list[str]
    final: bool

    # Optional fields for future nodes; keep here so we can evolve without churn.
    debug: NotRequired[dict[str, Any]]
    fixer_rationale: NotRequired[str]


def initial_fixer_state(incident: Incident) -> FixerAgentState:
    """Convenience initializer to avoid KeyError on required FixerAgentState keys."""

    return {
        "incident": incident,
        "evidence": {},
        "incident_summary": "",
        "actions": [],
        "raw_plan": "",
        "errors": [],
        "final": False,
    }


def _infer_deployment_from_labels(labels: dict[str, Any]) -> str | None:
    """Infer a Kubernetes workload target from alert labels.

    v0 heuristic: prefer explicit labels; fall back to deriving from pod name.
    """

    for key in ("deployment", "app", "service"):
        value = labels.get(key)
        if isinstance(value, str) and value:
            return value

    pod = labels.get("pod")
    if isinstance(pod, str) and pod:
        # Online Boutique pods are usually like "cartservice-<hash>-<suffix>".
        return pod.split("-", 1)[0]
    return None


def extract_evidence_node(state: FixerAgentState) -> dict[str, Any]:
    """Normalize Alertmanager payload shape into evidence.

    This node should be the *only* place that reaches into Incident.raw_context
    to avoid duplicating parsing logic across nodes.
    """

    incident = state["incident"]
    alert = incident.raw_context.get("alert", {})

    labels: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    if isinstance(alert, dict):
        maybe_labels = alert.get("labels")
        if isinstance(maybe_labels, dict):
            labels = maybe_labels

        maybe_annotations = alert.get("annotations")
        if isinstance(maybe_annotations, dict):
            annotations = maybe_annotations

    evidence = dict(state.get("evidence", {}))
    incident_class = incident.incident_class
    evidence.update(
        {
            "incident_id": incident.incident_id,
            "incident_class": incident_class,
            "incident_class_normalized": normalize_incident_class(incident_class),
            "labels": labels,
            "annotations": annotations,
            # First-class fields for easier downstream consumption.
            "alertname": labels.get("alertname"),
            "namespace": labels.get("namespace"),
            "severity": labels.get("severity"),
            "summary": annotations.get("summary") or annotations.get("description"),
            "pod": labels.get("pod"),
            "container": labels.get("container"),
        }
    )
    return {"evidence": evidence}


def build_incident_summary_node(state: FixerAgentState) -> dict[str, Any]:
    """Create a human-readable 1-liner summary from normalized evidence."""

    evidence = state.get("evidence", {})
    severity = evidence.get("severity") or "unknown_severity"
    alertname = evidence.get("alertname") or "unknown_alert"
    incident_class = evidence.get("incident_class") or "unknown_incident_class"
    namespace = evidence.get("namespace") or "unknown_ns"
    summary_text = evidence.get("summary") or ""

    summary = f"[{severity}] {alertname} ({incident_class}) ns={namespace} - {summary_text}".strip()
    return {"incident_summary": summary}


def propose_actions_node(state: FixerAgentState) -> dict[str, Any]:
    """Propose bounded remediation actions (Fixer does not execute)."""

    evidence = state.get("evidence", {})
    alertname = str(evidence.get("alertname") or "")
    namespace = str(evidence.get("namespace") or "default")
    incident_class_raw = str(evidence.get("incident_class") or "")
    incident_class = normalize_incident_class(incident_class_raw)

    actions: list[RemediationAction] = []
    errors = list(state.get("errors", []))

    if incident_class == "crashloop":
        labels = evidence.get("labels")
        deployment = None
        if isinstance(labels, dict):
            deployment = _infer_deployment_from_labels(labels)
        deployment = deployment or "cartservice"

        actions.append(
            RemediationAction(
                action_id="rollout_undo_cartservice",
                action_type="rollout_undo_deployment",
                description=f"Roll back {deployment} Deployment to the previous ReplicaSet.",
                confidence_score=0.9,
                blast_radius_score=0.3,
                requires_approval=True,
                parameters={"namespace": namespace, "deployment": deployment},
            )
        )
        actions.append(
            RemediationAction(
                action_id="restart_cartservice",
                action_type="rollout_restart_deployment",
                description=f"Restart {deployment} Deployment to clear transient crashloop state.",
                confidence_score=0.5,
                blast_radius_score=0.2,
                requires_approval=True,
                parameters={"namespace": namespace, "deployment": deployment},
            )
        )
    elif incident_class == "cpu_saturation":
        labels = evidence.get("labels")
        deployment = None
        if isinstance(labels, dict):
            deployment = _infer_deployment_from_labels(labels)
        deployment = deployment or "frontend"

        actions.append(
            RemediationAction(
                action_id="delete_frontend_cpu_stresschaos",
                action_type="delete_stresschaos",
                description="Delete the active frontend CPU StressChaos object to remove synthetic saturation.",
                confidence_score=0.9,
                blast_radius_score=0.2,
                requires_approval=True,
                parameters={"namespace": namespace, "name": "frontend-cpu-saturation"},
            )
        )
        actions.append(
            RemediationAction(
                action_id="escalate_frontend_cpu_saturation",
                action_type="escalate",
                description=f"Escalate {deployment} CPU saturation incident for deeper investigation.",
                confidence_score=0.35,
                blast_radius_score=0.0,
                requires_approval=True,
                parameters={"reason": "Bounded CPU remediation did not appear safe or sufficient."},
            )
        )
    else:
        errors.append(
            "unsupported incident_class for v0 Fixer: "
            f"{incident_class_raw or '<empty>'} (normalized={incident_class or '<empty>'})"
        )

    return {"actions": actions, "errors": errors}


def finalize_plan_node(state: FixerAgentState) -> dict[str, Any]:
    """Finalize raw plan string and mark Fixer output as complete."""

    actions = state.get("actions", [])
    errors = state.get("errors", [])
    rationale = state.get("fixer_rationale")
    if not actions:
        raw_plan = "No remediation actions proposed."
    else:
        lines = ["Proposed remediation actions:"]
        for action in actions:
            lines.append(
                f"- {action.action_id} ({action.action_type}): {action.description} "
                f"[confidence={action.confidence_score:.2f}, blast_radius={action.blast_radius_score:.2f}]"
            )
        raw_plan = "\n".join(lines)

    if isinstance(rationale, str) and rationale.strip():
        raw_plan = raw_plan + "\n\nRationale:\n" + rationale.strip()

    if errors:
        error_lines = ["", "Errors:"]
        for err in errors:
            error_lines.append(f"- {err}")
        raw_plan = raw_plan + "\n" + "\n".join(error_lines)

    return {"raw_plan": raw_plan, "final": True}


def make_llm_propose_actions_node(llm: FixerLLM) -> Any:
    """Create a LangGraph node function that calls an injected FixerLLM.

    If LLM call/parse/validation fails, we fall back to the heuristic propose_actions_node.
    """

    def _node(state: FixerAgentState) -> dict[str, Any]:
        errors = list(state.get("errors", []))
        incident_summary = state.get("incident_summary", "")
        evidence = state.get("evidence", {})
        try:
            result: FixerLLMResult = llm.propose(
                incident_summary=incident_summary, evidence=evidence
            )
            return {"actions": result.actions, "fixer_rationale": result.rationale, "errors": errors}
        except Exception as exc:
            errors.append(f"LLM propose failed; falling back to heuristic: {exc}")
            fallback = propose_actions_node(state)
            merged_errors = list(fallback.get("errors", []))
            return {"actions": fallback.get("actions", []), "errors": errors + merged_errors}

    return _node


def run_fixer_pipeline(incident: Incident, llm: FixerLLM | None = None) -> FixerAgentState:
    """Run the Fixer pipeline directly without requiring LangGraph."""

    state = initial_fixer_state(incident)
    state.update(extract_evidence_node(state))
    state.update(build_incident_summary_node(state))
    if llm is None:
        state.update(propose_actions_node(state))
    else:
        state.update(make_llm_propose_actions_node(llm)(state))
    state.update(finalize_plan_node(state))
    return state


def run_fixer_for_alertmanager_payload(
    payload: dict[str, Any],
    llm: FixerLLM | None = None,
) -> list[FixerAgentState]:
    """Convert an Alertmanager payload into Incident objects and run the Fixer."""

    from services.alertmanager_client import incidents_from_alertmanager_payload

    incidents = incidents_from_alertmanager_payload(payload)
    return [run_fixer_pipeline(incident, llm=llm) for incident in incidents]


def build_fixer_graph() -> Any:
    """Build the Fixer LangGraph.

    This returns a compiled graph object with an `.invoke(initial_state)` API.
    """

    try:
        from langgraph.graph import END, START, StateGraph
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ModuleNotFoundError(
            "langgraph is required to build the Fixer graph. Install it in your venv."
        ) from exc

    builder: Any = StateGraph(FixerAgentState)
    builder.add_node("extract_evidence", extract_evidence_node)
    builder.add_node("build_incident_summary", build_incident_summary_node)
    builder.add_node("propose_actions", propose_actions_node)
    builder.add_node("finalize_plan", finalize_plan_node)

    builder.add_edge(START, "extract_evidence")
    builder.add_edge("extract_evidence", "build_incident_summary")
    builder.add_edge("build_incident_summary", "propose_actions")
    builder.add_edge("propose_actions", "finalize_plan")
    builder.add_edge("finalize_plan", END)

    return builder.compile()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run HERALD Fixer against an Alertmanager webhook payload."
    )
    parser.add_argument(
        "--payload-file",
        required=True,
        help="Path to a JSON file containing the Alertmanager webhook body.",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="LLM model to use when provider mode is enabled.",
    )
    parser.add_argument(
        "--provider",
        choices=("gemini", "openai"),
        default="gemini",
        help="LLM provider to use when not running with --no-llm.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use the heuristic Fixer instead of a live LLM provider.",
    )
    parser.add_argument(
        "--include-debug-context",
        action="store_true",
        help="Include raw incident context and full evidence in CLI output.",
    )
    return parser


def _load_json_payload(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError("payload JSON must be an object")
    return payload


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _serialize_results_for_output(
    results: list[FixerAgentState],
    *,
    include_debug_context: bool,
) -> list[dict[str, Any]]:
    serialized_results: list[dict[str, Any]] = []

    for result in results:
        incident = result["incident"]
        serialized: dict[str, Any] = {
            "incident": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "detected_at": incident.detected_at,
                "source": incident.source,
            },
            "incident_summary": result.get("incident_summary", ""),
            "actions": result.get("actions", []),
            "raw_plan": result.get("raw_plan", ""),
            "errors": result.get("errors", []),
            "final": result.get("final", False),
        }

        fixer_rationale = result.get("fixer_rationale")
        if isinstance(fixer_rationale, str) and fixer_rationale:
            serialized["fixer_rationale"] = fixer_rationale

        if include_debug_context:
            serialized["incident"]["raw_context"] = incident.raw_context
            serialized["evidence"] = result.get("evidence", {})

        serialized_results.append(serialized)

    return serialized_results


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    payload = _load_json_payload(args.payload_file)

    llm: FixerLLM | None = None
    if not args.no_llm:
        if args.provider == "gemini":
            from services.gemini_fixer_llm import GeminiFixerLLM

            llm = GeminiFixerLLM(model=args.model)
        else:
            from services.openai_fixer_llm import OpenAIFixerLLM

            llm = OpenAIFixerLLM(model=args.model)

    results = run_fixer_for_alertmanager_payload(payload, llm=llm)
    output = _serialize_results_for_output(
        results,
        include_debug_context=args.include_debug_context,
    )
    print(json.dumps(_to_jsonable(output), default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
