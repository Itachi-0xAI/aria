"""
Cross-model staleness probe.

Probes the same entity with two free providers:
  - Groq  (llama-3.1-8b-instant, free tier)
  - Google Gemini Flash (gemini-1.5-flash, free tier)

Agreement logic:
  both_match_gold=True           → AI knowledge is fresh everywhere
  agreement=True, both wrong     → high confidence the index is universally stale
  agreement=False                → issue is model-specific, not a data gap

Uses only stdlib urllib — no new deps.
Falls back gracefully if either API key is missing.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional

_GROQ_BASE = "https://api.groq.com/openai/v1"
_GROQ_MODEL = "llama-3.1-8b-instant"

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_MODEL = "gemini-1.5-flash"

_SYSTEM = (
    "You are a precise fact checker. "
    "Answer only with the specific value requested — one short phrase, no explanation."
)


def _call_groq(question: str, api_key: str) -> str:
    payload = json.dumps({
        "model": _GROQ_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": question},
        ],
        "max_tokens": 64,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        f"{_GROQ_BASE}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def _call_gemini(question: str, api_key: str) -> str:
    payload = json.dumps({
        "contents": [{"parts": [{"text": f"{_SYSTEM}\n\n{question}"}]}],
        "generationConfig": {"maxOutputTokens": 64, "temperature": 0.0},
    }).encode()
    url = f"{_GEMINI_BASE}/{_GEMINI_MODEL}:generateContent?key={api_key}"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _values_match(a: str, b: str) -> bool:
    """Loose equality: strip whitespace/punctuation, case-insensitive."""
    def _norm(s: str) -> str:
        return s.lower().strip().rstrip(".,;:")
    return _norm(a) == _norm(b)


def probe(
    question: str,
    gold_value: str,
    groq_api_key: Optional[str] = None,
    gemini_api_key: Optional[str] = None,
) -> dict:
    """
    Probe *question* with Groq and Gemini, compare results to *gold_value*.

    Parameters
    ----------
    question       : the probe question (e.g. "What is the Enterprise revenue threshold?")
    gold_value     : the verified Gold layer answer to compare against
    groq_api_key   : override; falls back to env GROQ_API_KEY
    gemini_api_key : override; falls back to env GEMINI_API_KEY

    Returns
    -------
    dict with keys:
        groq           : str  — Groq answer (or "[unavailable]")
        gemini         : str  — Gemini answer (or "[unavailable]")
        agreement      : bool — both gave the same answer
        both_match_gold: bool — both answers match the gold value
        groq_matches   : bool
        gemini_matches : bool
        diagnosis      : str  — human-readable interpretation
    """
    groq_key   = groq_api_key   or os.environ.get("GROQ_API_KEY", "").strip()
    gemini_key = gemini_api_key or os.environ.get("GEMINI_API_KEY", "").strip()

    groq_answer   = "[unavailable]"
    gemini_answer = "[unavailable]"

    if groq_key:
        try:
            groq_answer = _call_groq(question, groq_key)
        except Exception as exc:
            groq_answer = f"[Groq error] {exc}"

    if gemini_key:
        try:
            gemini_answer = _call_gemini(question, gemini_key)
        except Exception as exc:
            gemini_answer = f"[Gemini error] {exc}"

    groq_ok   = "[unavailable]" not in groq_answer   and "[error]" not in groq_answer.lower()
    gemini_ok = "[unavailable]" not in gemini_answer and "[error]" not in gemini_answer.lower()

    groq_matches   = groq_ok   and _values_match(groq_answer,   gold_value)
    gemini_matches = gemini_ok and _values_match(gemini_answer, gold_value)
    both_match     = groq_matches and gemini_matches
    agreement      = groq_ok and gemini_ok and _values_match(groq_answer, gemini_answer)

    if not groq_ok and not gemini_ok:
        diagnosis = "Both providers unavailable — cannot determine staleness."
    elif both_match:
        diagnosis = "Both models agree with Gold. No staleness detected."
    elif agreement and not both_match:
        diagnosis = (
            "Both models agree on the same wrong value — "
            "high confidence the knowledge index is universally stale."
        )
    elif groq_matches and not gemini_matches:
        diagnosis = "Groq matches Gold but Gemini does not — issue may be model-specific (Gemini)."
    elif gemini_matches and not groq_matches:
        diagnosis = "Gemini matches Gold but Groq does not — issue may be model-specific (Groq)."
    else:
        diagnosis = (
            "Models disagree with each other and neither matches Gold — "
            "staleness confirmed but cause is inconclusive."
        )

    return {
        "groq":            groq_answer,
        "gemini":          gemini_answer,
        "agreement":       agreement,
        "both_match_gold": both_match,
        "groq_matches":    groq_matches,
        "gemini_matches":  gemini_matches,
        "diagnosis":       diagnosis,
    }
