from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from services.llm.tasks.fixer_contract import (
    FixerLLM,
    FixerLLMResult,
    build_fixer_prompts,
    fixer_output_json_schema,
    parse_fixer_llm_result,
)


@dataclass(slots=True)
class OpenAIFixerLLM(FixerLLM):
    """OpenAI Responses API implementation of the FixerLLM interface."""

    model: str

    def propose(self, *, incident_summary: str, evidence: dict[str, Any]) -> FixerLLMResult:
        # Import lazily so the rest of the repo can run without the dependency.
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ModuleNotFoundError(
                "openai package is required for OpenAIFixerLLM. Install dependencies first."
            ) from exc

        # OPENAI_API_KEY is the standard env var used by the official SDK.
        # We do not read or log the value to avoid leaking secrets.
        if not os.environ.get("OPENAI_API_KEY"):  # pragma: no cover
            raise EnvironmentError("OPENAI_API_KEY is not set")

        system_prompt, user_prompt = build_fixer_prompts(
            incident_summary=incident_summary, evidence=evidence
        )

        client = OpenAI()
        response = client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "herald_fixer_output",
                    "description": "Fixer proposed actions and a short rationale.",
                    "strict": True,
                    "schema": fixer_output_json_schema(),
                }
            },
        )

        # With Structured Outputs, the model will output JSON that matches the schema.
        # The SDK exposes a convenience string via output_text.
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise ValueError("OpenAI response missing output_text")

        payload = json.loads(output_text)
        if not isinstance(payload, dict):
            raise ValueError("OpenAI response JSON must be an object")

        return parse_fixer_llm_result(payload)
