from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from agents.critic import run_critic_pipeline
from agents.reasoner import run_reasoner_pipeline
from agents.synthesizer import run_synthesizer_pipeline
from schemas.incident import Incident
from schemas.observations import ObservationBundle
from services.capability_catalog import default_capability_catalog
from services.cluster_observer import ClusterObserver


class RecoveryGraphV2State(TypedDict):
    incident: Incident
    observation_bundle: NotRequired[ObservationBundle]
    reasoner_state: NotRequired[dict[str, Any]]
    critic_state: NotRequired[dict[str, Any]]
    synthesizer_state: NotRequired[dict[str, Any]]
    handoff_summary: NotRequired[dict[str, Any]]


def build_recovery_graph_v2(
    *,
    observer: ClusterObserver | None = None,
) -> Any:
    try:
        from langgraph.graph import END, START, StateGraph
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ModuleNotFoundError(
            "langgraph is required to build the HERALD v2 recovery graph. Install it in your venv."
        ) from exc

    cluster_observer = observer or ClusterObserver()

    def observe_node(state: RecoveryGraphV2State) -> dict[str, Any]:
        return {
            "observation_bundle": cluster_observer.collect(incident=state["incident"]),
        }

    def reason_node(state: RecoveryGraphV2State) -> dict[str, Any]:
        bundle = state["observation_bundle"]
        reasoner_state = run_reasoner_pipeline(
            state["incident"],
            bundle,
            llm=None,
            capability_catalog=default_capability_catalog(),
        )
        return {"reasoner_state": reasoner_state}

    def critique_node(state: RecoveryGraphV2State) -> dict[str, Any]:
        bundle = state["observation_bundle"]
        reasoner_state = state["reasoner_state"]
        reasoner_output = reasoner_state["reasoner_output"]
        critic_state = run_critic_pipeline(
            state["incident"],
            bundle,
            reasoner_output,
            llm=None,
            capability_catalog=default_capability_catalog(),
        )
        return {"critic_state": critic_state}

    def synthesize_node(state: RecoveryGraphV2State) -> dict[str, Any]:
        bundle = state["observation_bundle"]
        reasoner_state = state["reasoner_state"]
        critic_state = state["critic_state"]
        synthesis_state = run_synthesizer_pipeline(
            state["incident"],
            bundle,
            reasoner_state["reasoner_output"],
            critic_state.get("critic_output"),
        )
        return {"synthesizer_state": synthesis_state}

    def handoff_to_v1_node(state: RecoveryGraphV2State) -> dict[str, Any]:
        bundle = state["observation_bundle"]
        reasoner_state = state["reasoner_state"]
        critic_state = state["critic_state"]
        synthesizer_state = state["synthesizer_state"]
        critic_output = critic_state.get("critic_output")
        return {
            "handoff_summary": {
                "status": "handoff_to_v1",
                "incident_id": bundle.incident_id,
                "incident_class_hint": bundle.incident_class_hint,
                "intent_count": len(reasoner_state["reasoner_output"].intents),
                "critic_status": critic_state.get("status", "failed"),
                "synthesis_status": synthesizer_state.get("status", "failed"),
                "approved_candidate_count": (
                    critic_state.get("policy_summary", {}).get("approved_candidate_count", 0)
                ),
                "synthesized_plan_count": len(synthesizer_state.get("synthesis_output").plans)
                if synthesizer_state.get("synthesis_output") is not None
                else 0,
                "critic_candidate_count": len(critic_output.candidates) if critic_output is not None else 0,
            }
        }

    builder: Any = StateGraph(RecoveryGraphV2State)
    builder.add_node("observe", observe_node)
    builder.add_node("reason", reason_node)
    builder.add_node("critique", critique_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_node("handoff_to_v1", handoff_to_v1_node)
    builder.add_edge(START, "observe")
    builder.add_edge("observe", "reason")
    builder.add_edge("reason", "critique")
    builder.add_edge("critique", "synthesize")
    builder.add_edge("synthesize", "handoff_to_v1")
    builder.add_edge("handoff_to_v1", END)
    return builder.compile()


def run_recovery_graph_v2_observe_only(
    incident: Incident,
    *,
    observer: ClusterObserver | None = None,
) -> RecoveryGraphV2State:
    try:
        graph = build_recovery_graph_v2(observer=observer)
    except ModuleNotFoundError:
        cluster_observer = observer or ClusterObserver()
        observation_bundle = cluster_observer.collect(incident=incident)
        reasoner_state = run_reasoner_pipeline(
            incident,
            observation_bundle,
            llm=None,
            capability_catalog=default_capability_catalog(),
        )
        critic_state = run_critic_pipeline(
            incident,
            observation_bundle,
            reasoner_state["reasoner_output"],
            llm=None,
            capability_catalog=default_capability_catalog(),
        )
        synthesizer_state = run_synthesizer_pipeline(
            incident,
            observation_bundle,
            reasoner_state["reasoner_output"],
            critic_state.get("critic_output"),
        )
        return {
            "incident": incident,
            "observation_bundle": observation_bundle,
            "reasoner_state": reasoner_state,
            "critic_state": critic_state,
            "synthesizer_state": synthesizer_state,
            "handoff_summary": {
                "status": "handoff_to_v1",
                "incident_id": observation_bundle.incident_id,
                "incident_class_hint": observation_bundle.incident_class_hint,
                "intent_count": len(reasoner_state["reasoner_output"].intents),
                "critic_status": critic_state.get("status", "failed"),
                "synthesis_status": synthesizer_state.get("status", "failed"),
                "approved_candidate_count": critic_state.get("policy_summary", {}).get(
                    "approved_candidate_count",
                    0,
                ),
                "synthesized_plan_count": len(synthesizer_state.get("synthesis_output").plans)
                if synthesizer_state.get("synthesis_output") is not None
                else 0,
                "critic_candidate_count": len(critic_state["critic_output"].candidates)
                if critic_state.get("critic_output") is not None
                else 0,
            },
        }
    return graph.invoke({"incident": incident})
