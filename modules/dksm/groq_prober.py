"""
Groq prober — OpenAI-compatible Groq endpoint, free tier.
Used as a fallback when ANTHROPIC_API_KEY is absent.

Reads GROQ_API_KEY from env.
Base URL: https://api.groq.com/openai/v1
Default model: llama-3.1-8b-instant (free tier)
"""
from __future__ import annotations

import os
from typing import Optional


_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.1-8b-instant"


def is_available() -> bool:
    """Return True if a Groq API key is configured."""
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def probe(
    question: str,
    context: str = "",
    model: Optional[str] = None,
) -> str:
    """
    Run a CRAG-style probe via Groq.

    Parameters
    ----------
    question : str
        The question to probe (e.g. "What is the Enterprise revenue threshold?").
    context : str
        Optional Gold layer context to inject into the system prompt.
    model : str, optional
        Groq model name. Defaults to llama-3.1-8b-instant.

    Returns
    -------
    str
        The model's answer, or an error message prefixed with "[Groq error]".
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return "[Groq error] GROQ_API_KEY not set."

    model = model or _DEFAULT_MODEL

    system_prompt = (
        "You are a precise data-fact checker. "
        "Answer only with the specific value or fact requested — no explanation."
    )
    if context:
        system_prompt += f"\n\nGold layer context:\n{context}"

    try:
        import urllib.request
        import json

        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            "max_tokens": 256,
            "temperature": 0.0,
        }).encode()

        req = urllib.request.Request(
            f"{_GROQ_BASE_URL}/chat/completions",
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

    except Exception as exc:
        return f"[Groq error] {exc}"
