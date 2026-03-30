from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, NotRequired, TypedDict

from schemas.critic import CriticOutput
from schemas.execution_plan import SynthesisOutput
from schemas.incident import Incident
from schemas.intents import ReasonerOutput
from schemas.observations import ObservationBundle
from services.intent_synthesizer import compile_shadow_dispatches, synthesize_execution_plans


class SynthesizerAgentState(TypedDict):
    incident: Incident
    observations: ObservationBundle
    reasoner_output: ReasonerOutput
    critic_output: CriticOutput | None
    synthesis_output: SynthesisOutput | None
    synthesized_v1_dispatches: list[dict[str, Any]]
    errors: list[str]
    final: bool
    status: str

    failure_reason: NotRequired[str]


def initial_synthesizer_state(
    incident: Incident,
    observations: ObservationBundle,
    reasoner_output: ReasonerOutput,
    critic_output: CriticOutput | None,
) -> SynthesizerAgentState:
    return {
        "incident": incident,
        "observations": observations,
        "reasoner_output": reasoner_output,
        "critic_output": critic_output,
        "synthesis_output": None,
        "synthesized_v1_dispatches": [],
        "errors": [],
        "final": False,
        "status": "failed",
    }


def run_synthesizer_pipeline(
    incident: Incident,
    observations: ObservationBundle,
    reasoner_output: ReasonerOutput,
    critic_output: CriticOutput | None,
) -> SynthesizerAgentState:
    state = initial_synthesizer_state(incident, observations, reasoner_output, critic_output)
    try:
        synthesis_output = synthesize_execution_plans(reasoner_output, critic_output)
        state["synthesis_output"] = synthesis_output
        state["synthesized_v1_dispatches"] = compile_shadow_dispatches(synthesis_output)
        state["status"] = "succeeded"
    except Exception as exc:
        failure_reason = f"Synthesizer pipeline failed unexpectedly: {exc}"
        state["errors"].append(failure_reason)
        state["failure_reason"] = failure_reason
        state["synthesis_output"] = SynthesisOutput(
            summary="Shadow synthesis failed; v1 execution continues unchanged.",
            plans=[],
            unsupported_intents=[],
            warnings=[failure_reason],
        )
        state["synthesized_v1_dispatches"] = []
        state["status"] = "failed"
    state["final"] = True
    return state


def serialize_synthesizer_state(state: SynthesizerAgentState) -> dict[str, Any]:
    return {key: _to_jsonable(value) for key, value in state.items()}


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
