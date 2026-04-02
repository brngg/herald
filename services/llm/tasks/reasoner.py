from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemas.intents import CapabilityCatalog
from schemas.observations import ObservationBundle
from services.llm.gemini import (
    build_generate_content_request as _shared_build_generate_content_request,
    extract_response_text as _shared_extract_response_text,
    get_gemini_api_key as _shared_get_gemini_api_key,
)
from services.llm.task_runner import build_chat_style_prompt, run_gemini_structured_task
from services.llm.tasks.reasoner_contract import (
    ReasonerLLM,
    ReasonerLLMResult,
    build_reasoner_prompts,
    parse_reasoner_llm_result,
    reasoner_output_json_schema,
)


def _build_generate_content_request(
    *,
    model: str,
    incident_summary: str,
    observations: ObservationBundle,
    incident_class_hint: str,
    capability_catalog: CapabilityCatalog,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    system_prompt, user_prompt = build_reasoner_prompts(
        incident_summary=incident_summary,
        observations=observations,
        incident_class_hint=incident_class_hint,
        capability_catalog=capability_catalog,
    )
    prompt = build_chat_style_prompt(system_prompt, user_prompt)
    return _shared_build_generate_content_request(
        model=model,
        prompt=prompt,
        response_json_schema=reasoner_output_json_schema(),
    )


def _extract_response_text(payload: dict[str, Any]) -> str:
    return _shared_extract_response_text(payload)


def _get_gemini_api_key() -> str:
    return _shared_get_gemini_api_key()


@dataclass(slots=True)
class GeminiReasonerLLM(ReasonerLLM):
    model: str = "gemini-2.5-flash"
    timeout_seconds: float = 30.0

    def reason(
        self,
        *,
        incident_summary: str,
        observations: ObservationBundle,
        incident_class_hint: str,
        capability_catalog: CapabilityCatalog,
    ) -> ReasonerLLMResult:
        output_text, result = run_gemini_structured_task(
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            build_prompts=build_reasoner_prompts,
            response_json_schema=reasoner_output_json_schema(),
            parse_result=parse_reasoner_llm_result,
            incident_summary=incident_summary,
            observations=observations,
            incident_class_hint=incident_class_hint,
            capability_catalog=capability_catalog,
        )
        return ReasonerLLMResult(
            output=result,
            raw_response_text=output_text,
        )


ReasonerLLMClient = GeminiReasonerLLM
