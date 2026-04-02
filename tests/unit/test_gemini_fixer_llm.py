from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.llm.tasks.fixer import (
    _build_generate_content_request,
    _extract_response_text,
    _get_gemini_api_key,
)


class GeminiFixerLLMTest(unittest.TestCase):
    def test_get_gemini_api_key_prefers_gemini_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-key", "GOOGLE_API_KEY": "google-key"},
            clear=True,
        ):
            self.assertEqual(_get_gemini_api_key(), "gemini-key")

    def test_build_generate_content_request_uses_json_schema_output(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            url, headers, body = _build_generate_content_request(
                model="gemini-2.5-flash",
                incident_summary="[critical] test",
                evidence={"incident_class": "crashloop"},
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
                                    "text": '{"rationale":"x","actions":[]}',
                                }
                            ]
                        }
                    }
                ]
            }
        )

        self.assertEqual(text, '{"rationale":"x","actions":[]}')


if __name__ == "__main__":
    unittest.main()
