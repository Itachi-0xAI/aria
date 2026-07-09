"""
Tests for modules/dksm/wiki_verifier.py

All HTTP calls are mocked — no real network traffic.
"""

from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from modules.dksm.wiki_verifier import verify_via_wikipedia


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(payload: dict, status: int = 200) -> MagicMock:
    """Return a mock that behaves like urllib.request.urlopen context manager."""
    body = json.dumps(payload).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


_METFORMIN_PAYLOAD = {
    "type": "standard",
    "title": "Metformin",
    "extract": (
        "Metformin, sold under the brand name Glucophage among others, is the "
        "first-line medication for the treatment of type 2 diabetes. It costs "
        "approximately $4 per month for a generic version in the United States."
    ),
    "content_urls": {
        "desktop": {"page": "https://en.wikipedia.org/wiki/Metformin"}
    },
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestWikiVerifierSuccess(unittest.TestCase):
    """Happy-path: entity found and value mentioned in summary."""

    @patch("urllib.request.urlopen")
    def test_returns_available_true(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(_METFORMIN_PAYLOAD)
        result = verify_via_wikipedia("Metformin", "$4")

        self.assertTrue(result["available"])
        self.assertEqual(result["entity"], "Metformin")
        self.assertTrue(result["wiki_mentions_value"])
        self.assertIn("Metformin", result["wikipedia_summary"])
        self.assertEqual(result["wiki_url"], "https://en.wikipedia.org/wiki/Metformin")

    @patch("urllib.request.urlopen")
    def test_summary_truncated_to_300_chars(self, mock_urlopen):
        long_extract = "A" * 500
        payload = dict(_METFORMIN_PAYLOAD, extract=long_extract)
        mock_urlopen.return_value = _make_response(payload)
        result = verify_via_wikipedia("Metformin", "$4")

        self.assertLessEqual(len(result["wikipedia_summary"]), 300)


class TestWikiVerifier404(unittest.TestCase):
    """Entity page not found → available: False, no exception raised."""

    @patch("urllib.request.urlopen")
    def test_404_returns_unavailable(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
        )
        result = verify_via_wikipedia("NonExistentXyzEntity99", "42")

        self.assertFalse(result["available"])
        self.assertFalse(result["wiki_mentions_value"])
        self.assertEqual(result["wikipedia_summary"], "")
        self.assertEqual(result["entity"], "NonExistentXyzEntity99")


class TestWikiVerifierNetworkError(unittest.TestCase):
    """Network / timeout failure → available: False, no exception raised."""

    @patch("urllib.request.urlopen")
    def test_network_error_returns_unavailable(self, mock_urlopen):
        import socket
        mock_urlopen.side_effect = socket.timeout("timed out")
        result = verify_via_wikipedia("Aspirin", "$1")

        self.assertFalse(result["available"])
        self.assertFalse(result["wiki_mentions_value"])
        self.assertEqual(result["wikipedia_summary"], "")


class TestWikiVerifierValueNotInSummary(unittest.TestCase):
    """Entity page exists but gold_value does not appear in summary."""

    @patch("urllib.request.urlopen")
    def test_value_not_mentioned(self, mock_urlopen):
        payload = {
            "type": "standard",
            "title": "Aspirin",
            "extract": (
                "Aspirin, also known as acetylsalicylic acid, is a nonsteroidal "
                "anti-inflammatory drug used to reduce pain, fever, or inflammation."
            ),
            "content_urls": {
                "desktop": {"page": "https://en.wikipedia.org/wiki/Aspirin"}
            },
        }
        mock_urlopen.return_value = _make_response(payload)
        result = verify_via_wikipedia("Aspirin", "$999.99")

        self.assertTrue(result["available"])
        self.assertFalse(result["wiki_mentions_value"])


if __name__ == "__main__":
    unittest.main()
