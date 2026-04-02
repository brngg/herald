from __future__ import annotations

from typing import Any, Callable, TypeVar

from services.llm.gemini import request_structured_json


ResultT = TypeVar("ResultT")


PromptBuilder = Callable[..., tuple[str, str]]
Parser = Callable[[dict[str, Any]], ResultT]


def build_chat_style_prompt(system_prompt: str, user_prompt: str) -> str:
    return (
        "System instructions:\n"
        f"{system_prompt}\n\n"
        "User request:\n"
        f"{user_prompt}"
    )


def run_gemini_structured_task(
    *,
    model: str,
    timeout_seconds: float,
    build_prompts: PromptBuilder,
    response_json_schema: dict[str, Any],
    parse_result: Parser[ResultT],
    **prompt_kwargs: Any,
) -> tuple[str, ResultT]:
    system_prompt, user_prompt = build_prompts(**prompt_kwargs)
    prompt = build_chat_style_prompt(system_prompt, user_prompt)
    output_text, output_payload = request_structured_json(
        model=model,
        prompt=prompt,
        response_json_schema=response_json_schema,
        timeout_seconds=timeout_seconds,
    )
    return output_text, parse_result(output_payload)

