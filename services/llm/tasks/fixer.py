from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.llm.tasks.fixer_contract import (
    FixerLLM,
    FixerLLMResult,
    build_fixer_prompts,
    fixer_output_json_schema,
    parse_fixer_llm_result,
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
    evidence: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, Any]]:
    system_prompt, user_prompt = build_fixer_prompts(
        incident_summary=incident_summary,
        evidence=evidence,
    )
    prompt = build_chat_style_prompt(system_prompt, user_prompt)
    return _shared_build_generate_content_request(
        model=model,
        prompt=prompt,
        response_json_schema=fixer_output_json_schema(),
    )


def _extract_response_text(payload: dict[str, Any]) -> str:
    return _shared_extract_response_text(payload)


def _get_gemini_api_key() -> str:
    return _shared_get_gemini_api_key()


@dataclass(slots=True)
class GeminiFixerLLM(FixerLLM):
    """Gemini REST API implementation of the FixerLLM interface."""

    model: str = "gemini-2.5-flash"
    timeout_seconds: float = 30.0

    def propose(self, *, incident_summary: str, evidence: dict[str, Any]) -> FixerLLMResult:
        _, result = run_gemini_structured_task(
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            build_prompts=build_fixer_prompts,
            response_json_schema=fixer_output_json_schema(),
            parse_result=parse_fixer_llm_result,
            incident_summary=incident_summary,
            evidence=evidence,
        )
        return result


FixerLLMClient = GeminiFixerLLM
