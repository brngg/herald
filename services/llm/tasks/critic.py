from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemas.intents import CapabilityCatalog, ReasonerOutput
from schemas.observations import ObservationBundle
from services.llm.tasks.critic_contract import (
    CriticLLM,
    CriticLLMResult,
    build_critic_prompts,
    critic_output_json_schema,
    parse_critic_llm_result,
)
from services.llm.gemini import (
    build_generate_content_request as _shared_build_generate_content_request,
    extract_response_text as _shared_extract_response_text,
    get_gemini_api_key as _shared_get_gemini_api_key,
)
from services.llm.task_runner import build_chat_style_prompt, run_gemini_structured_task


def _build_generate_content_request(
    *,
    model: str,
    incident_summary: str,
    observations: ObservationBundle,
    reasoner_output: ReasonerOutput,
    policy_summary: dict[str, Any],
    capability_catalog: CapabilityCatalog,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    system_prompt, user_prompt = build_critic_prompts(
        incident_summary=incident_summary,
        observations=observations,
        reasoner_output=reasoner_output,
        policy_summary=policy_summary,
        capability_catalog=capability_catalog,
    )
    prompt = build_chat_style_prompt(system_prompt, user_prompt)
    return _shared_build_generate_content_request(
        model=model,
        prompt=prompt,
        response_json_schema=critic_output_json_schema(),
    )


def _extract_response_text(payload: dict[str, Any]) -> str:
    return _shared_extract_response_text(payload)


def _get_gemini_api_key() -> str:
    return _shared_get_gemini_api_key()


@dataclass(slots=True)
class GeminiCriticLLM(CriticLLM):
    model: str = "gemini-2.5-flash"
    timeout_seconds: float = 30.0

    def critique(
        self,
        *,
        incident_summary: str,
        observations: ObservationBundle,
        reasoner_output: ReasonerOutput,
        policy_summary: dict[str, Any],
        capability_catalog: CapabilityCatalog,
    ) -> CriticLLMResult:
        output_text, result = run_gemini_structured_task(
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            build_prompts=build_critic_prompts,
            response_json_schema=critic_output_json_schema(),
            parse_result=parse_critic_llm_result,
            incident_summary=incident_summary,
            observations=observations,
            reasoner_output=reasoner_output,
            policy_summary=policy_summary,
            capability_catalog=capability_catalog,
        )
        return CriticLLMResult(
            output=result,
            raw_response_text=output_text,
        )


CriticLLMClient = GeminiCriticLLM
