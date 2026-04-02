from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from schemas.remediation import RemediationAction
from services.llm.tasks.judge import (
    _build_generate_content_request,
    _extract_response_text,
    _get_gemini_api_key,
)


def _crashloop_actions() -> list[RemediationAction]:
    return [
        RemediationAction(
            action_id="rollout_undo_cartservice",
            action_type="rollout_undo_deployment",
            description="Roll back cartservice Deployment to the previous ReplicaSet.",
            confidence_score=0.9,
            blast_radius_score=0.3,
            requires_approval=True,
            parameters={"namespace": "default", "deployment": "cartservice"},
        )
    ]


class GeminiJudgeLLMTest(unittest.TestCase):
    def test_get_gemini_api_key_prefers_gemini_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-key", "GOOGLE_API_KEY": "google-key"},
            clear=True,
        ):
            self.assertEqual(_get_gemini_api_key(), "gemini-key")

    def test_build_generate_content_request_uses_judge_json_schema_output(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            url, headers, body = _build_generate_content_request(
                model="gemini-2.5-flash",
                incident_summary="[critical] crashloop",
                evidence={"incident_class": "crashloop", "namespace": "default"},
                actions=_crashloop_actions(),
                fixer_rationale="Rollback the last deployment first.",
            )

        self.assertIn("gemini-2.5-flash:generateContent", url)
        self.assertEqual(headers["x-goog-api-key"], "gemini-key")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("responseJsonSchema", body["generationConfig"])

    def test_extract_response_text_reads_candidate_parts(self) -> None:
        text = _extract_response_text(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"verdict":"pass","reason":"Plan is bounded."}',
                                }
                            ]
                        }
                    }
                ]
            }
        )

        self.assertEqual(text, '{"verdict":"pass","reason":"Plan is bounded."}')


if __name__ == "__main__":
    unittest.main()
