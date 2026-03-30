from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from schemas.intents import CapabilityCatalog
from schemas.observations import ObservationBundle
from services.gemini_reasoner_llm import (
    _build_generate_content_request,
    _extract_response_text,
    _get_gemini_api_key,
)


class GeminiReasonerLLMTest(unittest.TestCase):
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
        catalog = CapabilityCatalog(version="phase2-shadow-v1", capabilities=[])

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            url, headers, body = _build_generate_content_request(
                model="gemini-2.5-flash",
                incident_summary="[critical] crashloop",
                observations=observations,
                incident_class_hint="crashloop",
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
                                    "text": '{"diagnosis_summary":"x","likely_causes":[],"missing_information":[],"intents":[]}',
                                }
                            ]
                        }
                    }
                ]
            }
        )

        self.assertEqual(
            text,
            '{"diagnosis_summary":"x","likely_causes":[],"missing_information":[],"intents":[]}',
        )


if __name__ == "__main__":
    unittest.main()
