"""
Domain prober: queries an LLM with probe questions and applies the CRAG
(Corrective RAG) validation loop to ensure reliable value extraction.

Architecture:
  DomainProber   — drives the probe loop for one or all domains
  CRAGValidator  — grades LLM responses and generates retry questions
  ProbeResult    — typed output of every probe attempt
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic
import yaml

try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    _TENACITY_AVAILABLE = True
except ImportError:
    _TENACITY_AVAILABLE = False

if TYPE_CHECKING:
    from src.vector_store import MedallionVectorStore, SearchResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Full result of one probe attempt (possibly with CRAG retries)."""
    domain: str
    entity: str
    question: str
    raw_response: str
    extracted_value: str | None
    staleness_level: str            # FRESH | STALE | CRITICAL | UNKNOWN
    crag_grade: str                 # CORRECT | AMBIGUOUS | INCORRECT
    crag_confidence: float
    crag_retries_used: int
    crag_fallback: bool
    fallback_source: str            # "" | "vector_store"
    gold_context_used: list[dict]
    final_question: str             # may differ from original if retried
    timestamp: str
    model: str
    cost_usd: float


@dataclass
class GradeResult:
    """Output of CRAGValidator.grade_extraction()."""
    grade: str              # CORRECT | AMBIGUOUS | INCORRECT
    extracted_value: str | None
    confidence: float
    reason: str
    retry_question: str | None


# ---------------------------------------------------------------------------
# CRAG log helpers
# ---------------------------------------------------------------------------

_CRAG_LOG_PATH = Path("data/probe_cache/crag_log.csv")
_CRAG_LOG_COLUMNS = [
    "timestamp", "question", "grade", "confidence",
    "retries_used", "final_extracted_value", "cost_usd",
]

def _ensure_crag_log() -> None:
    _CRAG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _CRAG_LOG_PATH.exists():
        with open(_CRAG_LOG_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CRAG_LOG_COLUMNS)
            writer.writeheader()


_CRAG_LOG_MAX_ROWS = 10_000


def _rotate_crag_log() -> None:
    """Archive crag_log.csv when it exceeds 10K rows to prevent unbounded growth."""
    if not _CRAG_LOG_PATH.exists():
        return
    with open(_CRAG_LOG_PATH) as f:
        row_count = sum(1 for _ in f) - 1  # subtract header row
    if row_count >= _CRAG_LOG_MAX_ROWS:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        archive = _CRAG_LOG_PATH.parent / f"crag_log_archive_{ts}.csv"
        _CRAG_LOG_PATH.rename(archive)
        logger.info("CRAG log rotated to %s (%d rows)", archive, row_count)
        _ensure_crag_log()


def _append_crag_log(row: dict) -> None:
    _ensure_crag_log()
    _rotate_crag_log()
    with open(_CRAG_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CRAG_LOG_COLUMNS)
        writer.writerow({k: row.get(k, "") for k in _CRAG_LOG_COLUMNS})


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

# Approximate cost per 1k tokens (USD) — Haiku is the grader
_COST_PER_1K: dict[str, float] = {
    "claude-haiku-4-5-20251001": 0.00025,
    "claude-sonnet-4-6": 0.003,
    "claude-opus-4-7": 0.015,
}

def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rate = _COST_PER_1K.get(model, 0.003)
    return round((input_tokens + output_tokens) / 1000 * rate, 6)


# ---------------------------------------------------------------------------
# CRAG Validator
# ---------------------------------------------------------------------------

class CRAGValidator:
    """
    Grades LLM extraction quality and generates retry questions.
    Uses claude-haiku-4-5-20251001 (cheapest) as the grader model to keep costs low.
    """

    GRADER_MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, client: anthropic.Anthropic, grader_model: str | None = None) -> None:
        self.client = client
        self.model = grader_model or self.GRADER_MODEL

    def grade_extraction(
        self,
        question: str,
        raw_response: str,
        expected_domain: str,
        gold_context: list["SearchResult"],
    ) -> GradeResult:
        """
        Grade whether raw_response contains a clear, extractable answer.

        Sends a structured grading prompt to the Haiku model and parses
        the JSON response. Falls back to AMBIGUOUS on any parsing error.
        """
        context_snippet = ""
        if gold_context:
            top = gold_context[0]
            context_snippet = (
                f"Entity: {top.entity}\n"
                f"Current Value: {top.current_value}\n"
                f"Effective Date: {top.effective_date}\n"
                f"Domain: {top.domain}"
            )

        grading_prompt = f"""Question: {question}

Model Response: {raw_response}

Domain Context (Gold Layer):
{context_snippet}

Grade the model response. Return JSON only — no other text.
{{
  "grade": "CORRECT|AMBIGUOUS|INCORRECT",
  "extracted_value": "<specific numeric or categorical value if clearly present, else null>",
  "confidence": <0.0-1.0>,
  "reason": "<one sentence>"
}}

Rules:
- CORRECT: response contains a clear, specific, extractable value
- AMBIGUOUS: response is vague, hedged, or contains multiple conflicting values
- INCORRECT: response gives a clearly wrong value (contradicts domain context) or refuses to answer"""

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=256,
                system=(
                    "You are an extraction quality grader. Given a question, a model "
                    "response, and the expected domain context, grade whether the response "
                    "contains a clear, extractable answer. Return JSON only."
                ),
                messages=[{"role": "user", "content": grading_prompt}],
            )
            text = resp.content[0].text.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text)
            return GradeResult(
                grade=data.get("grade", "AMBIGUOUS"),
                extracted_value=data.get("extracted_value"),
                confidence=float(data.get("confidence", 0.5)),
                reason=data.get("reason", ""),
                retry_question=None,
            )
        except Exception as exc:
            logger.warning("CRAG grading failed: %s", exc)
            return GradeResult(
                grade="AMBIGUOUS",
                extracted_value=None,
                confidence=0.3,
                reason=f"Grading error: {exc}",
                retry_question=None,
            )

    def generate_retry_question(
        self,
        original: str,
        reason: str,
        domain: str,
    ) -> str:
        """
        Rephrase an ambiguous probe question to elicit a more specific answer.
        Uses the Haiku model; falls back to a simple appended clarification.
        """
        prompt = f"""The following probe question produced an ambiguous response:

Original question: {original}
Domain: {domain}
Problem: {reason}

Write a single, more specific version of this question that:
1. Asks for an exact numeric value or categorical answer
2. Specifies units (USD, %, count/hour, etc.)
3. Removes any ambiguity

Return ONLY the rephrased question — no explanation."""

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:
            logger.warning("Retry question generation failed: %s", exc)
            return original + " (Please provide an exact numeric value with units.)"


# ---------------------------------------------------------------------------
# Domain Prober
# ---------------------------------------------------------------------------

class DomainProber:
    """
    Probes an LLM with domain-specific questions and runs the CRAG loop
    to validate and correct extracted values.

    The vector_store parameter is optional — if None, CRAG runs without
    Gold context (grading is less precise but still functional).
    """

    def __init__(
        self,
        config_path: str = "config/domains.yaml",
        vector_store: "MedallionVectorStore | None" = None,
    ) -> None:
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set")

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = self.config.get("global_settings", {}).get("model", "claude-sonnet-4-6")
        self.grader_model = self.config.get("global_settings", {}).get(
            "grader_model", "claude-haiku-4-5-20251001"
        )
        self.max_retries = self.config.get("global_settings", {}).get("max_crag_retries", 2)
        self.vector_store = vector_store
        self.crag = CRAGValidator(self.client, self.grader_model)

    # ------------------------------------------------------------------
    # Internal LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, question: str) -> tuple[str, float]:
        """
        Send question to the configured model.
        Returns (response_text, cost_usd).
        Retries with exponential backoff on rate-limit errors (429) when tenacity is available.
        """
        system = (
            "You are a business analyst. Answer factual questions about business "
            "rules, thresholds, and pricing with specific numeric values. "
            "Do not hedge — give the exact value you know."
        )

        def _do_call():
            return self.client.messages.create(
                model=self.model,
                max_tokens=256,
                system=system,
                messages=[{"role": "user", "content": question}],
            )

        if _TENACITY_AVAILABLE:
            _do_call = retry(
                retry=retry_if_exception_type(anthropic.RateLimitError),
                wait=wait_exponential(multiplier=1, min=4, max=60),
                stop=stop_after_attempt(4),
                reraise=True,
            )(_do_call)

        resp = _do_call()
        cost = _estimate_cost(
            self.model,
            resp.usage.input_tokens,
            resp.usage.output_tokens,
        )
        return resp.content[0].text.strip(), cost

    # ------------------------------------------------------------------
    # CRAG probe loop
    # ------------------------------------------------------------------

    def probe_single_with_crag(
        self,
        question: str,
        domain: str,
        entity: str = "",
        max_retries: int | None = None,
        _retries_used: int = 0,
        _original_question: str | None = None,
    ) -> ProbeResult:
        """
        Full CRAG-enhanced probe for a single question.

        Step 1: Retrieve Gold context from vector store (if available)
        Step 2: Call LLM with the question
        Step 3: Grade the extraction with CRAGValidator
        Step 4: Route — return, retry (AMBIGUOUS), or fallback (INCORRECT)
        """
        max_r = max_retries if max_retries is not None else self.max_retries
        original_q = _original_question or question
        total_cost = 0.0

        # Step 1 — Gold layer context
        gold_context: list[SearchResult] = []
        if self.vector_store is not None:
            try:
                gold_context = self.vector_store.hybrid_search(question, domain, top_k=3)
            except Exception as exc:
                logger.warning("Vector store search failed: %s", exc)

        # Step 2 — LLM probe
        try:
            raw_response, probe_cost = self._call_llm(question)
            total_cost += probe_cost
        except Exception as exc:
            logger.error("LLM call failed: %s", exc)
            return self._make_result(
                domain, entity, question, "", None,
                "UNKNOWN", "INCORRECT", 0.0, _retries_used,
                True, "llm_error", gold_context, original_q, total_cost,
            )

        # Step 3 — Grade
        grade = self.crag.grade_extraction(question, raw_response, domain, gold_context)

        # Step 4 — Route
        if grade.grade == "CORRECT":
            result = self._make_result(
                domain, entity, question, raw_response,
                grade.extracted_value, "UNKNOWN",
                "CORRECT", grade.confidence, _retries_used,
                False, "", gold_context, original_q, total_cost,
            )

        elif grade.grade == "AMBIGUOUS" and _retries_used < max_r:
            retry_q = self.crag.generate_retry_question(question, grade.reason, domain)
            logger.info("CRAG retry %d for domain=%s entity=%s", _retries_used + 1, domain, entity)
            result = self.probe_single_with_crag(
                question=retry_q,
                domain=domain,
                entity=entity,
                max_retries=max_r,
                _retries_used=_retries_used + 1,
                _original_question=original_q,
            )
            result.crag_retries_used = _retries_used + 1
            result.cost_usd += total_cost

        else:
            # INCORRECT or AMBIGUOUS exhausted retries — fallback to vector store
            fallback_value = None
            if gold_context:
                fallback_value = gold_context[0].current_value

            result = self._make_result(
                domain, entity, question, raw_response,
                fallback_value, "UNKNOWN",
                grade.grade, grade.confidence, _retries_used,
                True, "vector_store", gold_context, original_q, total_cost,
            )

        # Log to CRAG log
        _append_crag_log({
            "timestamp": result.timestamp,
            "question": original_q,
            "grade": result.crag_grade,
            "confidence": result.crag_confidence,
            "retries_used": result.crag_retries_used,
            "final_extracted_value": result.extracted_value or "",
            "cost_usd": result.cost_usd,
        })

        return result

    def _make_result(
        self,
        domain: str,
        entity: str,
        question: str,
        raw_response: str,
        extracted_value: str | None,
        staleness_level: str,
        crag_grade: str,
        crag_confidence: float,
        crag_retries_used: int,
        crag_fallback: bool,
        fallback_source: str,
        gold_context: list,
        final_question: str,
        cost_usd: float,
    ) -> ProbeResult:
        return ProbeResult(
            domain=domain,
            entity=entity,
            question=question,
            raw_response=raw_response,
            extracted_value=extracted_value,
            staleness_level=staleness_level,
            crag_grade=crag_grade,
            crag_confidence=crag_confidence,
            crag_retries_used=crag_retries_used,
            crag_fallback=crag_fallback,
            fallback_source=fallback_source,
            gold_context_used=[
                {
                    "entity": r.entity,
                    "current_value": r.current_value,
                    "effective_date": r.effective_date,
                    "similarity_score": r.combined_score,
                    "domain": r.domain,
                }
                for r in gold_context
            ],
            final_question=final_question,
            timestamp=datetime.now(timezone.utc).isoformat(),
            model=self.model,
            cost_usd=cost_usd,
        )

    # ------------------------------------------------------------------
    # Batch probing
    # ------------------------------------------------------------------

    def probe_domain(
        self,
        domain: str,
        questions: list[str] | None = None,
        delay_seconds: float = 0.5,
    ) -> list[ProbeResult]:
        """
        Probe all questions for a domain (or a custom question list).
        Applies a small delay between calls to respect rate limits.
        """
        cfg = self.config["domains"].get(domain)
        if cfg is None:
            raise ValueError(f"Unknown domain: {domain}")

        qs = questions or cfg.get("probe_questions", [])
        results = []
        failed_questions: list[str] = []
        for q in qs:
            try:
                r = self.probe_single_with_crag(q, domain)
                results.append(r)
            except Exception as exc:
                logger.error("Probe failed for question '%s': %s", q, exc)
                failed_questions.append(q)
            if delay_seconds > 0:
                time.sleep(delay_seconds)
        if failed_questions:
            logger.warning(
                "probe_domain(%s): %d/%d questions failed silently: %s",
                domain, len(failed_questions), len(qs), failed_questions,
            )
        return results

    def probe_all_domains(
        self,
        delay_seconds: float = 0.5,
    ) -> dict[str, list[ProbeResult]]:
        """Probe every configured domain."""
        return {
            domain: self.probe_domain(domain, delay_seconds=delay_seconds)
            for domain in self.config["domains"]
        }
