from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from schemas.execution import ExecutionDispatch, ExecutionStatus, ExecutionToolName


GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


@dataclass(frozen=True, slots=True)
class ExecutionAgentDecision:
    decision_type: str
    tool_name: str
    arguments: dict[str, Any]
    status: ExecutionStatus
    summary: str

    def __post_init__(self) -> None:
        if self.decision_type not in {"tool_call", "finish"}:
            raise ValueError(f"unsupported decision_type: {self.decision_type}")
        if not isinstance(self.tool_name, str):
            raise TypeError("tool_name must be a str")
        if self.decision_type == "tool_call" and not self.tool_name:
            raise ValueError("tool_call decisions must include a tool_name")
        if not isinstance(self.arguments, dict):
            raise TypeError("arguments must be a dict")
        if self.status not in {"succeeded", "failed"}:
            raise ValueError(f"unsupported status: {self.status}")
        if not isinstance(self.summary, str):
            raise TypeError("summary must be a str")
        if not self.summary.strip():
            raise ValueError("summary must be non-empty")


class ExecutionAgentLLM(Protocol):
    def decide_next_step(
        self,
        *,
        dispatch: ExecutionDispatch,
        tool_transcript: list[dict[str, Any]],
    ) -> ExecutionAgentDecision: ...


ExecutionAgentEventLogger = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class GeminiExecutionAgentLLM(ExecutionAgentLLM):
    model: str = "gemini-2.5-flash"
    timeout_seconds: float = 30.0

    def decide_next_step(
        self,
        *,
        dispatch: ExecutionDispatch,
        tool_transcript: list[dict[str, Any]],
    ) -> ExecutionAgentDecision:
        import httpx

        url, headers, body = _build_generate_content_request(
            model=self.model,
            dispatch=dispatch,
            tool_transcript=tool_transcript,
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

        return parse_execution_agent_decision(output_payload)


@dataclass(slots=True)
class ExecutionTool:
    name: ExecutionToolName
    description: str
    callable: Any
    mutation: bool


@dataclass(slots=True)
class GeminiExecutionAgent:
    llm: ExecutionAgentLLM

    def run(
        self,
        *,
        dispatch: ExecutionDispatch,
        tools: dict[ExecutionToolName, ExecutionTool],
        event_logger: ExecutionAgentEventLogger | None = None,
    ) -> tuple[ExecutionStatus, str, list[str], int, str, str, list[dict[str, Any]]]:
        logger = event_logger or _noop_event_logger
        tool_transcript: list[dict[str, Any]] = []
        last_command: list[str] = []
        last_returncode = 0
        last_stdout = ""
        last_stderr = ""
        mutation_executed = False
        logger(
            "agent_started",
            {
                "worker_id": dispatch.worker_id,
                "action_id": dispatch.action_id,
                "max_steps": dispatch.max_steps,
                "allowed_tool_names": list(dispatch.allowed_tool_names),
            },
        )

        for step_index in range(1, dispatch.max_steps + 1):
            logger(
                "agent_step_started",
                {
                    "worker_id": dispatch.worker_id,
                    "action_id": dispatch.action_id,
                    "step": step_index,
                },
            )
            decision = self.llm.decide_next_step(dispatch=dispatch, tool_transcript=tool_transcript)
            logger(
                "agent_decision",
                {
                    "worker_id": dispatch.worker_id,
                    "action_id": dispatch.action_id,
                    "step": step_index,
                    "decision_type": decision.decision_type,
                    "tool_name": decision.tool_name,
                },
            )

            if decision.decision_type == "finish":
                if decision.status == "succeeded":
                    if not mutation_executed:
                        logger(
                            "agent_finished",
                            {
                                "worker_id": dispatch.worker_id,
                                "action_id": dispatch.action_id,
                                "status": "failed",
                            },
                        )
                        return (
                            "failed",
                            "Gemini execution agent finished successfully before executing the approved action.",
                            last_command,
                            1,
                            last_stdout,
                            last_stderr,
                            tool_transcript,
                        )
                    if last_returncode != 0:
                        logger(
                            "agent_finished",
                            {
                                "worker_id": dispatch.worker_id,
                                "action_id": dispatch.action_id,
                                "status": "failed",
                            },
                        )
                        return (
                            "failed",
                            "Gemini execution agent reported success but the approved action failed.",
                            last_command,
                            last_returncode,
                            last_stdout,
                            last_stderr,
                            tool_transcript,
                        )
                logger(
                    "agent_finished",
                    {
                        "worker_id": dispatch.worker_id,
                        "action_id": dispatch.action_id,
                        "status": decision.status,
                    },
                )
                return (
                    decision.status,
                    decision.summary.strip(),
                    last_command,
                    last_returncode,
                    last_stdout,
                    last_stderr,
                    tool_transcript,
                )

            if decision.tool_name not in dispatch.allowed_tool_names:
                logger(
                    "agent_finished",
                    {
                        "worker_id": dispatch.worker_id,
                        "action_id": dispatch.action_id,
                        "status": "failed",
                    },
                )
                return (
                    "failed",
                    f"Gemini execution agent requested disallowed tool {decision.tool_name!r}.",
                    last_command,
                    1,
                    last_stdout,
                    last_stderr,
                    tool_transcript,
                )

            tool = tools.get(decision.tool_name)
            if tool is None:
                logger(
                    "agent_finished",
                    {
                        "worker_id": dispatch.worker_id,
                        "action_id": dispatch.action_id,
                        "status": "failed",
                    },
                )
                return (
                    "failed",
                    f"Gemini execution agent requested unknown tool {decision.tool_name!r}.",
                    last_command,
                    1,
                    last_stdout,
                    last_stderr,
                    tool_transcript,
                )

            try:
                logger(
                    "tool_call_started",
                    {
                        "worker_id": dispatch.worker_id,
                        "action_id": dispatch.action_id,
                        "step": step_index,
                        "tool_name": decision.tool_name,
                    },
                )
                tool_result = tool.callable(**decision.arguments)
            except Exception as exc:
                logger(
                    "tool_call_failed",
                    {
                        "worker_id": dispatch.worker_id,
                        "action_id": dispatch.action_id,
                        "step": step_index,
                        "tool_name": decision.tool_name,
                    },
                )
                logger(
                    "agent_finished",
                    {
                        "worker_id": dispatch.worker_id,
                        "action_id": dispatch.action_id,
                        "status": "failed",
                    },
                )
                return (
                    "failed",
                    f"Gemini execution agent tool {decision.tool_name!r} failed: {exc}",
                    last_command,
                    1,
                    last_stdout,
                    str(exc),
                    tool_transcript,
                )

            compact_result = _compact_tool_result(tool_result)
            tool_transcript.append(
                {
                    "step": step_index,
                    "tool_name": decision.tool_name,
                    "arguments": dict(decision.arguments),
                    "result": compact_result,
                }
            )
            logger(
                "tool_call_finished",
                {
                    "worker_id": dispatch.worker_id,
                    "action_id": dispatch.action_id,
                    "step": step_index,
                    "tool_name": decision.tool_name,
                    "status": compact_result.get("status", "unknown"),
                },
            )

            if tool.mutation:
                mutation_executed = True
                last_command = [str(item) for item in tool_result.get("command", [])]
                last_returncode = int(tool_result.get("returncode", 0))
                last_stdout = str(tool_result.get("stdout", ""))
                last_stderr = str(tool_result.get("stderr", ""))

        logger(
            "agent_finished",
            {
                "worker_id": dispatch.worker_id,
                "action_id": dispatch.action_id,
                "status": "failed",
            },
        )
        return (
            "failed",
            f"Gemini execution agent exceeded max_steps={dispatch.max_steps} without finishing.",
            last_command,
            1,
            last_stdout,
            last_stderr,
            tool_transcript,
        )


def parse_execution_agent_decision(payload: dict[str, Any]) -> ExecutionAgentDecision:
    decision_type = payload.get("decision_type")
    tool_name = payload.get("tool_name")
    arguments = payload.get("arguments")
    status = payload.get("status")
    summary = payload.get("summary")
    return ExecutionAgentDecision(
        decision_type=decision_type,
        tool_name=tool_name or "",
        arguments=arguments or {},
        status=status,
        summary=summary,
    )


def execution_agent_output_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision_type": {
                "type": "string",
                "enum": ["tool_call", "finish"],
            },
            "tool_name": {
                "type": "string",
                "enum": list(_valid_tool_names()) + [""],
            },
            "arguments": {"type": "object"},
            "status": {"type": "string", "enum": ["succeeded", "failed"]},
            "summary": {"type": "string"},
        },
        "required": ["decision_type", "tool_name", "arguments", "status", "summary"],
    }


def _build_generate_content_request(
    *,
    model: str,
    dispatch: ExecutionDispatch,
    tool_transcript: list[dict[str, Any]],
) -> tuple[str, dict[str, str], dict[str, Any]]:
    prompt = _build_execution_agent_prompt(dispatch=dispatch, tool_transcript=tool_transcript)
    url = f"{GEMINI_API_BASE_URL}/{model}:generateContent"
    headers = {
        "x-goog-api-key": _get_gemini_api_key(),
        "Content-Type": "application/json",
    }
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": execution_agent_output_json_schema(),
        },
    }
    return url, headers, body


def _build_execution_agent_prompt(
    *,
    dispatch: ExecutionDispatch,
    tool_transcript: list[dict[str, Any]],
) -> str:
    return (
        "You are the HERALD execution agent.\n"
        "You may only execute the already approved remediation intent.\n"
        "You may inspect context only to validate or explain the approved action.\n"
        "You may not propose new actions, broaden scope, or touch unrelated workloads.\n"
        "You must respond as JSON matching the provided schema.\n\n"
        f"Approved action id: {dispatch.action_id}\n"
        f"Approved action type: {dispatch.action_type}\n"
        f"Approved parameters: {json.dumps(dispatch.parameters, sort_keys=True)}\n"
        f"Allowed tools: {json.dumps(dispatch.allowed_tool_names)}\n"
        f"Max steps: {dispatch.max_steps}\n\n"
        "If you need to inspect state first, choose a read-only tool.\n"
        "For tool_call decisions, keep summary short and action-oriented.\n"
        "For finish decisions, summary must be a short operator-facing paragraph that explains "
        "what you inspected, what approved action you executed, and what outcome you observed.\n"
        "When the approved action has been executed or you are blocked, return decision_type=finish.\n\n"
        "Transcript so far:\n"
        f"{json.dumps(tool_transcript, sort_keys=True)}"
    )


def _compact_tool_result(tool_result: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("status", "returncode"):
        if key in tool_result:
            compact[key] = tool_result[key]
    if "command" in tool_result:
        compact["command"] = tool_result["command"]
    if "stdout" in tool_result:
        compact["stdout"] = _truncate_text(str(tool_result["stdout"]))
    if "stderr" in tool_result:
        compact["stderr"] = _truncate_text(str(tool_result["stderr"]))
    if "output" in tool_result:
        compact["output"] = _truncate_text(str(tool_result["output"]))
    return compact


def _truncate_text(value: str, limit: int = 400) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


def _noop_event_logger(_: str, __: dict[str, Any]) -> None:
    return None


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


def _get_gemini_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise EnvironmentError("Set GEMINI_API_KEY or GOOGLE_API_KEY before calling Gemini.")
    return api_key


def _valid_tool_names() -> tuple[ExecutionToolName, ...]:
    return (
        "get_deployment_context",
        "get_rollout_status",
        "rollout_undo_deployment",
        "rollout_restart_deployment",
    )
