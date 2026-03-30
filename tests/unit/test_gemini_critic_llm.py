from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from schemas.critic import CriticOutput
from schemas.intents import CapabilityCatalog, OperationIntent, ReasonerOutput, ResourceTarget
from schemas.observations import ObservationBundle
from services.gemini_critic_llm import (
    _build_generate_content_request,
    _extract_response_text,
    _get_gemini_api_key,
)


class GeminiCriticLLMTest(unittest.TestCase):
    def test_get_gemini_api_key_prefers_gemini_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-key", "GOOGLE_API_KEY": "google-key"},
            clear=True,
        ):
            self.assertEqual(_get_gemini_api_key(), "gemini-key")

    def test_build_generate_content_request_uses_json_schema_output(self) -> None:
        observations = ObservationBundle(
            incident_id="incident-123",
            incident_class_hint="crashloop",
            namespace_hint="default",
            source="prometheus",
            alert_context={"labels": {"namespace": "default"}, "annotations": {}},
            kubernetes={},
            prometheus={},
            collected_at="2026-03-29T20:00:00+00:00",
        )
        reasoner_output = ReasonerOutput(
            diagnosis_summary="cartservice is crash looping",
            likely_causes=["bad deployment"],
            missing_information=[],
            intents=[
                OperationIntent(
                    intent_id="reasoner-rollout-undo-cartservice",
                    intent="Roll back the cartservice Deployment.",
                    operation_family="rollout.undo_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                    arguments={},
                    reversible=True,
                    confidence_score=0.9,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    verification_hints={},
                    rollback_hints={},
                )
            ],
        )
        catalog = CapabilityCatalog(version="phase2-shadow-v1", capabilities=[])

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            url, headers, body = _build_generate_content_request(
                model="gemini-2.5-flash",
                incident_summary="[critical] crashloop",
                observations=observations,
                reasoner_output=reasoner_output,
                policy_summary={"approved_candidate_count": 1},
                capability_catalog=catalog,
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
                                    "text": '{"summary":"Policy validation approved the safe intent.","global_concerns":[],"candidates":[]}',
                                }
                            ]
                        }
                    }
                ]
            }
        )

        self.assertEqual(
            text,
            '{"summary":"Policy validation approved the safe intent.","global_concerns":[],"candidates":[]}',
        )


if __name__ == "__main__":
    unittest.main()
