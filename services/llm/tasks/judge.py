from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemas.remediation import RemediationAction
from services.llm.tasks.judge_contract import (
    JudgeLLM,
    JudgeLLMResult,
    build_judge_prompts,
    judge_output_json_schema,
    parse_judge_llm_result,
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
    actions: list[RemediationAction],
    fixer_rationale: str | None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    system_prompt, user_prompt = build_judge_prompts(
        incident_summary=incident_summary,
        evidence=evidence,
        actions=actions,
        fixer_rationale=fixer_rationale,
    )
    prompt = build_chat_style_prompt(system_prompt, user_prompt)
    return _shared_build_generate_content_request(
        model=model,
        prompt=prompt,
        response_json_schema=judge_output_json_schema(),
    )


def _extract_response_text(payload: dict[str, Any]) -> str:
    return _shared_extract_response_text(payload)


def _get_gemini_api_key() -> str:
    return _shared_get_gemini_api_key()


@dataclass(slots=True)
class GeminiJudgeLLM(JudgeLLM):
    """Gemini REST API implementation of the JudgeLLM interface."""

    model: str = "gemini-2.5-flash"
    timeout_seconds: float = 30.0

    def evaluate(
        self,
        *,
        incident_summary: str,
        evidence: dict[str, Any],
        actions: list[RemediationAction],
        fixer_rationale: str | None,
    ) -> JudgeLLMResult:
        _, result = run_gemini_structured_task(
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            build_prompts=build_judge_prompts,
            response_json_schema=judge_output_json_schema(),
            parse_result=parse_judge_llm_result,
            incident_summary=incident_summary,
            evidence=evidence,
            actions=actions,
            fixer_rationale=fixer_rationale,
        )
        return result


JudgeLLMClient = GeminiJudgeLLM
