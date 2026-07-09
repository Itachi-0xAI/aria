"""
Staleness scorer: compares LLM-extracted values against Gold layer ground truth.

Scoring is based on semantic similarity (via sentence-transformers or simple
numeric comparison for numeric fields) combined with time-decay weighting
based on the Gold record's effective_date.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import yaml

from core.tracing import span as _span

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class StalenessScore:
    """Full staleness assessment for a single probe."""
    domain: str
    entity: str
    model_belief: str           # what the LLM said
    warehouse_truth: str        # current Gold layer value
    staleness_score: float      # 0 = fresh, 1 = critical
    staleness_level: str        # FRESH | STALE | CRITICAL | UNKNOWN
    semantic_similarity: float  # 0-1 cosine sim between beliefs
    numeric_match: bool | None  # True/False/None for non-numeric
    effective_date: str         # when Gold value was last updated
    days_since_update: int
    confidence: float           # scorer confidence in assessment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NUMERIC_RE = re.compile(r"[\$,\s]*([\d]+(?:\.\d+)?)\s*[Mm]?")


def _extract_number(text: str) -> float | None:
    """Extract the first number from a string (handles $1.5M, 750,000, 1.2B, 5%, etc.)."""
    text = str(text).replace(",", "")
    m = re.search(r"([\d]+(?:\.\d+)?)\s*([BbMmKk%])?", text)
    if not m:
        return None
    val = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    if suffix == "B":
        val *= 1_000_000_000
    elif suffix == "M":
        val *= 1_000_000
    elif suffix == "K":
        val *= 1_000
    elif suffix == "%":
        val = val / 100  # normalise percentage to decimal
    return val


def _days_since(date_str: str) -> int:
    """Return days elapsed since an ISO date string."""
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


def _time_decay_factor(days: int, half_life_days: int = 180) -> float:
    """Exponential time-decay: older records amplify staleness."""
    return float(1 - np.exp(-days / half_life_days))


# ---------------------------------------------------------------------------
# Semantic similarity (lightweight, no model loading required)
# ---------------------------------------------------------------------------

def _token_overlap_similarity(a: str, b: str) -> float:
    """Jaccard-based token overlap — used as fallback when transformers unavailable."""
    tokens_a = set(re.findall(r"\w+", a.lower()))
    tokens_b = set(re.findall(r"\w+", b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _semantic_similarity(belief: str, truth: str) -> float:
    """
    Compute semantic similarity between model belief and ground truth.
    Prefers numeric comparison for numeric fields; falls back to token overlap.
    The vector_store module provides full embedding-based similarity when available.
    """
    belief_num = _extract_number(belief)
    truth_num = _extract_number(truth)

    if belief_num is not None and truth_num is not None and truth_num > 0:
        ratio = min(belief_num, truth_num) / max(belief_num, truth_num)
        return round(ratio, 4)

    return round(_token_overlap_similarity(belief, truth), 4)


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

class StalenessScorer:
    """
    Scores the staleness of LLM knowledge against the Gold layer.
    Loads thresholds from domains.yaml and applies time-decay weighting.
    """

    def __init__(self, config_path: str = "config/domains.yaml") -> None:
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

    def _get_thresholds(self, domain: str) -> dict[str, float]:
        return self.config["domains"][domain].get(
            "staleness_thresholds",
            {"fresh": 0.90, "stale": 0.70, "critical": 0.50},
        )

    def _classify(self, raw_similarity: float, thresholds: dict) -> str:
        if raw_similarity >= thresholds["fresh"]:
            return "FRESH"
        if raw_similarity >= thresholds["stale"]:
            return "STALE"
        if raw_similarity >= thresholds["critical"]:
            return "CRITICAL"
        return "CRITICAL"

    def score(
        self,
        domain: str,
        entity: str,
        model_belief: str,
        warehouse_truth: str,
        effective_date: str = "",
    ) -> StalenessScore:
        """
        Compute staleness score for a single entity.

        The staleness_score (0-1) combines:
          - 1 - semantic_similarity (divergence)
          - time_decay_factor (older gold records amplify staleness)
        """
        with _span("aria.dksm.score") as active_span:
            if not model_belief or not warehouse_truth:
                result = StalenessScore(
                    domain=domain,
                    entity=entity,
                    model_belief=model_belief or "",
                    warehouse_truth=warehouse_truth or "",
                    staleness_score=1.0,
                    staleness_level="UNKNOWN",
                    semantic_similarity=0.0,
                    numeric_match=None,
                    effective_date=effective_date,
                    days_since_update=0,
                    confidence=0.1,
                )
                if active_span is not None:
                    active_span.set_attribute("aria.domain", domain)
                    active_span.set_attribute("aria.entity", entity)
                    active_span.set_attribute("aria.staleness_score", 1.0)
                    active_span.set_attribute("aria.staleness_level", "UNKNOWN")
                return result

            sim = _semantic_similarity(model_belief, warehouse_truth)
            days = _days_since(effective_date) if effective_date else 0
            decay = _time_decay_factor(days)

            # staleness_score: divergence amplified by age
            divergence = 1.0 - sim
            staleness_score = round(min(divergence * (1 + 0.3 * decay), 1.0), 4)

            # Adjust similarity label: very stale data should read lower similarity
            adjusted_sim = sim

            thresholds = self._get_thresholds(domain)
            level = self._classify(adjusted_sim, thresholds)

            # Numeric match flag
            bnum = _extract_number(model_belief)
            tnum = _extract_number(warehouse_truth)
            numeric_match = (abs(bnum - tnum) < 1.0) if (bnum is not None and tnum is not None) else None

            confidence = round(0.6 + 0.4 * sim, 4)

            if active_span is not None:
                active_span.set_attribute("aria.domain", domain)
                active_span.set_attribute("aria.entity", entity)
                active_span.set_attribute("aria.staleness_score", staleness_score)
                active_span.set_attribute("aria.staleness_level", level)

            return StalenessScore(
                domain=domain,
                entity=entity,
                model_belief=model_belief,
                warehouse_truth=warehouse_truth,
                staleness_score=staleness_score,
                staleness_level=level,
                semantic_similarity=adjusted_sim,
                numeric_match=numeric_match,
                effective_date=effective_date,
                days_since_update=days,
                confidence=confidence,
            )

    def score_batch(self, records: list[dict[str, Any]]) -> list[StalenessScore]:
        """
        Score a list of records.
        Each record must have keys: domain, entity, model_belief, warehouse_truth,
        and optionally effective_date.
        """
        return [
            self.score(
                domain=r["domain"],
                entity=r["entity"],
                model_belief=r["model_belief"],
                warehouse_truth=r["warehouse_truth"],
                effective_date=r.get("effective_date", ""),
            )
            for r in records
        ]

    def get_domain_health(self, scores: list[StalenessScore]) -> dict[str, Any]:
        """Aggregate per-domain health from a list of StalenessScore objects."""
        by_domain: dict[str, list[StalenessScore]] = {}
        for s in scores:
            by_domain.setdefault(s.domain, []).append(s)

        result = {}
        for domain, domain_scores in by_domain.items():
            counts = {"FRESH": 0, "STALE": 0, "CRITICAL": 0, "UNKNOWN": 0}
            for s in domain_scores:
                counts[s.staleness_level] = counts.get(s.staleness_level, 0) + 1
            health = "FRESH" if counts["CRITICAL"] == 0 and counts["STALE"] == 0 else (
                "CRITICAL" if counts["CRITICAL"] > 0 else "STALE"
            )
            result[domain] = {
                "health": health,
                "counts": counts,
                "avg_similarity": round(
                    sum(s.semantic_similarity for s in domain_scores) / len(domain_scores), 4
                ),
            }
        return result
