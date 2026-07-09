"""
ARIA — Wikipedia Staleness Cross-Check

Calls the free, unauthenticated Wikipedia REST API to verify whether a
Gold-layer value is mentioned in the entity's Wikipedia summary.

Usage
-----
    from modules.dksm.wiki_verifier import verify_via_wikipedia

    result = verify_via_wikipedia("Metformin", "$4")
    # {
    #   "entity": "Metformin",
    #   "wikipedia_summary": "Metformin, sold under the brand name ...",
    #   "wiki_mentions_value": True,
    #   "wiki_url": "https://en.wikipedia.org/wiki/Metformin",
    #   "available": True,
    # }
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

_API_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary"
_TIMEOUT = 5  # seconds
_SUMMARY_CHARS = 300


def _strip_currency_units(value: str) -> str:
    """Return the numeric/core part of a value string for substring matching.

    '$4.00/month' → '4.00'  |  '125 mg' → '125'  |  'Tier 2' → 'Tier 2'
    Falls back to the original string when no strippable prefix is found.
    """
    stripped = re.sub(r"^[\$£€¥₹\s]+", "", value.strip())
    # Also remove trailing unit suffixes like /month, mg, %, etc.
    stripped = re.sub(r"[/\s].*$", "", stripped).strip()
    return stripped if stripped else value.strip()


def verify_via_wikipedia(entity: str, gold_value: str) -> dict[str, Any]:
    """Verify *gold_value* presence in the Wikipedia summary for *entity*.

    Parameters
    ----------
    entity:
        Entity name to look up (e.g. ``"Metformin"``).
    gold_value:
        The authoritative value from the Gold layer (e.g. ``"$4"``).

    Returns
    -------
    dict with keys:
        entity              – echoed input
        wikipedia_summary   – first 300 chars of the extract (empty on failure)
        wiki_mentions_value – True if gold_value or its stripped form appears
                              in the summary text (case-insensitive)
        wiki_url            – canonical Wikipedia URL
        available           – False on 404 / network error / disambiguation
    """
    _EMPTY: dict[str, Any] = {
        "entity": entity,
        "wikipedia_summary": "",
        "wiki_mentions_value": False,
        "wiki_url": f"https://en.wikipedia.org/wiki/{entity.replace(' ', '_')}",
        "available": False,
    }

    url = f"{_API_BASE}/{urllib.request.quote(entity, safe='')}"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ARIA-wiki-verifier/1.0 (educational; contact: aria-bot)"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data: dict = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404 = page not found; 303 = disambiguation — treat both as unavailable
        return _EMPTY
    except Exception:
        # Network timeout, DNS failure, JSON decode error, etc.
        return _EMPTY

    # Disambiguation pages don't have a reliable summary — skip them.
    if data.get("type") == "disambiguation":
        return _EMPTY

    extract: str = data.get("extract", "")
    summary_snippet = extract[:_SUMMARY_CHARS]

    # Check whether gold_value (or its stripped form) appears in the text.
    search_targets = {gold_value.strip(), _strip_currency_units(gold_value)}
    summary_lower = extract.lower()
    wiki_mentions = any(t.lower() in summary_lower for t in search_targets if t)

    return {
        "entity": entity,
        "wikipedia_summary": summary_snippet,
        "wiki_mentions_value": wiki_mentions,
        "wiki_url": data.get("content_urls", {}).get("desktop", {}).get(
            "page",
            f"https://en.wikipedia.org/wiki/{entity.replace(' ', '_')}",
        ),
        "available": True,
    }
