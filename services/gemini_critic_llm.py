from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from schemas.intents import CapabilityCatalog, ReasonerOutput
from schemas.observations import ObservationBundle
from services.critic_llm import (
    CriticLLM,
    CriticLLMResult,
    build_critic_prompts,
    critic_output_json_schema,
    parse_critic_llm_result,
)


GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _get_gemini_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise EnvironmentError("Set GEMINI_API_KEY or GOOGLE_API_KEY before calling Gemini.")
    return api_key


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
    prompt = (
        "System instructions:\n"
        f"{system_prompt}\n\n"
        "User request:\n"
        f"{user_prompt}"
    )
    url = f"{GEMINI_API_BASE_URL}/{model}:generateContent"
    headers = {
        "x-goog-api-key": _get_gemini_api_key(),
        "Content-Type": "application/json",
    }
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": critic_output_json_schema(),
        },
    }
    return url, headers, body


def _extract_response_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini response missing candidates")

    first = candidates[0]
    if not isinstance(first, dict):
        raise ValueError("Gemini candidate must be an object")

    content = first.get("content")
    if not isinstance(content, dict):
        raise ValueError("Gemini candidate missing content")

    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gemini candidate missing content parts")

    text_fragments: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                text_fragments.append(text)

    output_text = "".join(text_fragments).strip()
    if not output_text:
        raise ValueError("Gemini response did not contain text output")
    return output_text


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
        import httpx

        url, headers, body = _build_generate_content_request(
            model=self.model,
            incident_summary=incident_summary,
            observations=observations,
            reasoner_output=reasoner_output,
            policy_summary=policy_summary,
            capability_catalog=capability_catalog,
        )

        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Gemini response JSON must be an object")

        output_text = _extract_response_text(payload)
        output_payload = json.loads(output_text)
        if not isinstance(output_payload, dict):
            raise ValueError("Gemini structured output must be a JSON object")

        return CriticLLMResult(
            output=parse_critic_llm_result(output_payload),
            raw_response_text=output_text,
        )
