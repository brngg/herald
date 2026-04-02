from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.llm.gemini import (
    build_generate_content_request,
    extract_response_text,
    get_gemini_api_key,
)


class GeminiTransportTest(unittest.TestCase):
    def test_get_gemini_api_key_prefers_gemini_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-key", "GOOGLE_API_KEY": "google-key"},
            clear=True,
        ):
            self.assertEqual(get_gemini_api_key(), "gemini-key")

    def test_build_generate_content_request_uses_json_schema_output(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            url, headers, body = build_generate_content_request(
                model="gemini-2.5-flash",
                prompt="hello",
                response_json_schema={"type": "object"},
            )

        self.assertIn("gemini-2.5-flash:generateContent", url)
        self.assertEqual(headers["x-goog-api-key"], "gemini-key")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["responseJsonSchema"], {"type": "object"})

    def test_extract_response_text_reads_candidate_parts(self) -> None:
        text = extract_response_text(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"ok":true}',
                                }
                            ]
                        }
                    }
                ]
            }
        )

        self.assertEqual(text, '{"ok":true}')


if __name__ == "__main__":
    unittest.main()
