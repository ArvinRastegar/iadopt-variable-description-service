import unittest
from unittest.mock import Mock, patch

from app import main


class ProviderConfigurationTests(unittest.TestCase):
    def test_missing_allowlist_enables_both_providers(self):
        self.assertEqual(
            main._parse_enabled_model_providers(None),
            ["openrouter", "psnc"],
        )

    def test_single_provider_is_supported(self):
        self.assertEqual(main._parse_enabled_model_providers("psnc"), ["psnc"])
        self.assertEqual(main._parse_enabled_model_providers("openrouter"), ["openrouter"])

    def test_invalid_provider_is_rejected(self):
        with self.assertRaises(RuntimeError):
            main._parse_enabled_model_providers("psnc,unknown")


class PsncRerankerTests(unittest.TestCase):
    @patch.object(main, "get_http_session")
    def test_scores_are_returned_in_document_order(self, get_http_session):
        response = Mock()
        response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.2},
                {"index": 0, "relevance_score": 0.9},
            ]
        }
        response.raise_for_status.return_value = None
        get_http_session.return_value.post.return_value = response

        scores = main.call_psnc_reranker("query", ["first", "second"])

        self.assertEqual(scores, [0.9, 0.2])
        request = get_http_session.return_value.post.call_args
        self.assertEqual(request.kwargs["json"]["model"], main.PSNC_RERANK_MODEL)
        self.assertEqual(request.kwargs["json"]["documents"], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
