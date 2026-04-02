from __future__ import annotations

import json
import os
from typing import Any


GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def get_gemini_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise EnvironmentError("Set GEMINI_API_KEY or GOOGLE_API_KEY before calling Gemini.")
    return api_key


def build_generate_content_request(
    *,
    model: str,
    prompt: str,
    response_json_schema: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, Any]]:
    url = f"{GEMINI_API_BASE_URL}/{model}:generateContent"
    headers = {
        "x-goog-api-key": get_gemini_api_key(),
        "Content-Type": "application/json",
    }
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": response_json_schema,
        },
    }
    return url, headers, body


def extract_response_text(payload: dict[str, Any]) -> str:
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


def request_structured_json(
    *,
    model: str,
    prompt: str,
    response_json_schema: dict[str, Any],
    timeout_seconds: float,
) -> tuple[str, dict[str, Any]]:
    import httpx

    url, headers, body = build_generate_content_request(
        model=model,
        prompt=prompt,
        response_json_schema=response_json_schema,
    )
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Gemini response JSON must be an object")

    output_text = extract_response_text(payload)
    output_payload = json.loads(output_text)
    if not isinstance(output_payload, dict):
        raise ValueError("Gemini structured output must be a JSON object")
    return output_text, output_payload

