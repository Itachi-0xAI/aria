"""
ARIA — Prompt Coverage Analyzer

Like code coverage but for prompts. For every domain, generates edge-case
queries for each Gold-layer entity and measures whether the existing
probe_questions actually cover them.

Coverage = the fraction of edge cases for which at least one probe question
           has embedding similarity >= COVERAGE_THRESHOLD (default 0.55).

Edge case types per entity
──────────────────────────
  exact_value   → "What is the [value_column] for [entity]?"
  tier / class  → "What [tier/class] is [entity] assigned to?"
  auth / flag   → "Does [entity] require prior authorization / approval?"
  comparison    → "How does [entity] compare to other items in [domain]?"
  expiry        → "When does [entity]'s current [value] expire or change?"
  boundary      → "What is the threshold or limit for [entity] in [domain]?"
  stale_belief  → "Has [entity]'s [value_column] changed since [model_belief]?"
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent.parent
_DOMAINS_CFG = _ROOT / "config" / "dksm" / "domains.yaml"
_COVERAGE_THRESHOLD = 0.55   # min similarity to count a probe as "covering" a case


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class EdgeCase:
    entity: str
    case_type: str          # exact_value | tier | auth | comparison | expiry | boundary | stale_belief
    canonical_query: str
    best_probe: str | None  # probe question with highest similarity
    best_score: float       # cosine similarity to best_probe (0–1)
    covered: bool           # best_score >= threshold


@dataclass
class DomainCoverageReport:
    domain: str
    display_name: str
    total_probes: int
    total_edge_cases: int
    covered_count: int
    uncovered_count: int
    coverage_pct: float
    edge_cases: list[EdgeCase]
    uncovered_gaps: list[EdgeCase]          # edge_cases where covered=False
    suggested_probes: list[str]             # auto-generated for each gap
    entities_fully_covered: list[str]
    entities_with_gaps: list[str]


# ── embedding ─────────────────────────────────────────────────────────────────

_model = None

def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _embed(texts: list[str]) -> np.ndarray:
    return _get_model().encode(texts, normalize_embeddings=True, show_progress_bar=False)


def _cosine_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Returns shape (len(a), len(b)) cosine similarity matrix."""
    return np.clip(a @ b.T, 0.0, 1.0)


# ── gold layer loader ─────────────────────────────────────────────────────────

def _load_gold_entities(domain: str, cfg: dict) -> list[dict]:
    """Load current (is_current=True) records from the Gold layer CSV."""
    gold_cfg = cfg["domains"][domain]["medallion"]["gold"]
    gold_path = _ROOT / gold_cfg["path"]
    key_col = gold_cfg["key_column"]
    val_col = gold_cfg["value_column"]

    if not gold_path.exists():
        logger.warning("Gold layer not found: %s", gold_path)
        return []

    records = []
    with open(gold_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("is_current", "true").lower() in ("true", "1", "yes"):
                records.append({
                    "entity": row.get(key_col, ""),
                    "value": row.get(val_col, ""),
                    "model_belief": row.get("model_belief", ""),
                    "expiry_date": row.get("expiry_date", ""),
                    "tier": row.get("formulary_tier", row.get("tier", "")),
                    "prior_auth": row.get("prior_auth_required", ""),
                    "raw": dict(row),
                })
    return records


# ── edge case generator ───────────────────────────────────────────────────────

def _generate_edge_cases(
    entity: str,
    value: str,
    model_belief: str,
    expiry_date: str,
    tier: str,
    prior_auth: str,
    domain_display: str,
    value_col: str,
) -> list[EdgeCase]:
    """Generate canonical edge-case queries for one entity."""

    val_label = value_col.replace("_", " ").replace("usd", "").strip()

    cases = []

    # 1 — exact value
    cases.append(EdgeCase(
        entity=entity,
        case_type="exact_value",
        canonical_query=f"What is the {val_label} for {entity}?",
        best_probe=None, best_score=0.0, covered=False,
    ))

    # 2 — tier / class (only if tier data available)
    if tier:
        cases.append(EdgeCase(
            entity=entity,
            case_type="tier",
            canonical_query=f"What tier or category is {entity} assigned to in {domain_display}?",
            best_probe=None, best_score=0.0, covered=False,
        ))

    # 3 — prior auth / flag
    if prior_auth:
        cases.append(EdgeCase(
            entity=entity,
            case_type="auth",
            canonical_query=f"Does {entity} require prior authorization or special approval?",
            best_probe=None, best_score=0.0, covered=False,
        ))

    # 4 — comparison
    cases.append(EdgeCase(
        entity=entity,
        case_type="comparison",
        canonical_query=f"How does {entity} compare to other items in {domain_display}?",
        best_probe=None, best_score=0.0, covered=False,
    ))

    # 5 — expiry / change date
    if expiry_date:
        cases.append(EdgeCase(
            entity=entity,
            case_type="expiry",
            canonical_query=f"When does {entity}'s current {val_label} expire or change?",
            best_probe=None, best_score=0.0, covered=False,
        ))

    # 6 — boundary / threshold
    cases.append(EdgeCase(
        entity=entity,
        case_type="boundary",
        canonical_query=f"What is the boundary or threshold that defines {entity} in {domain_display}?",
        best_probe=None, best_score=0.0, covered=False,
    ))

    # 7 — stale belief (only if model_belief differs from value)
    if model_belief and str(model_belief).strip() != str(value).strip():
        cases.append(EdgeCase(
            entity=entity,
            case_type="stale_belief",
            canonical_query=(
                f"Has {entity}'s {val_label} changed from {model_belief} "
                f"to a new value recently?"
            ),
            best_probe=None, best_score=0.0, covered=False,
        ))

    return cases


# ── suggestion generator ──────────────────────────────────────────────────────

def _suggest_probe(gap: EdgeCase) -> str:
    templates = {
        "exact_value":   f"What is the current {gap.entity} value?",
        "tier":          f"What tier or classification does {gap.entity} belong to?",
        "auth":          f"Does {gap.entity} require any prior authorization or approval?",
        "comparison":    f"How does {gap.entity} differ from similar items in the same domain?",
        "expiry":        f"When is {gap.entity}'s current value scheduled to change or expire?",
        "boundary":      f"What boundary or threshold defines {gap.entity}?",
        "stale_belief":  f"Has {gap.entity}'s value been updated recently — what is the current figure?",
    }
    return templates.get(gap.case_type, f"What should I know about {gap.entity}?")


# ── main analyzer ─────────────────────────────────────────────────────────────

class PromptCoverageAnalyzer:
    """
    Analyzes how well a domain's probe_questions cover its Gold-layer entities.
    """

    def __init__(self, config_path: str | Path = _DOMAINS_CFG,
                 threshold: float = _COVERAGE_THRESHOLD) -> None:
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)
        self.threshold = threshold

    def analyze_domain(self, domain: str) -> DomainCoverageReport:
        domain_cfg = self.cfg["domains"][domain]
        display = domain_cfg.get("display_name", domain)
        probes: list[str] = domain_cfg.get("probe_questions", [])
        gold_cfg = domain_cfg["medallion"]["gold"]
        value_col = gold_cfg["value_column"]

        entities = _load_gold_entities(domain, self.cfg)
        if not entities:
            return DomainCoverageReport(
                domain=domain, display_name=display,
                total_probes=len(probes), total_edge_cases=0,
                covered_count=0, uncovered_count=0, coverage_pct=0.0,
                edge_cases=[], uncovered_gaps=[], suggested_probes=[],
                entities_fully_covered=[], entities_with_gaps=[],
            )

        # Generate all edge cases
        all_cases: list[EdgeCase] = []
        for ent in entities:
            all_cases.extend(_generate_edge_cases(
                entity=ent["entity"],
                value=ent["value"],
                model_belief=ent["model_belief"],
                expiry_date=ent["expiry_date"],
                tier=ent["tier"],
                prior_auth=ent["prior_auth"],
                domain_display=display,
                value_col=value_col,
            ))

        if not all_cases or not probes:
            return DomainCoverageReport(
                domain=domain, display_name=display,
                total_probes=len(probes), total_edge_cases=len(all_cases),
                covered_count=0, uncovered_count=len(all_cases), coverage_pct=0.0,
                edge_cases=all_cases, uncovered_gaps=all_cases,
                suggested_probes=[_suggest_probe(c) for c in all_cases],
                entities_fully_covered=[], entities_with_gaps=[e["entity"] for e in entities],
            )

        # Embed probes and canonical queries
        probe_embs = _embed(probes)
        query_embs = _embed([c.canonical_query for c in all_cases])

        # Similarity matrix: [n_cases × n_probes]
        sim_matrix = _cosine_matrix(query_embs, probe_embs)

        # Score each case
        for i, case in enumerate(all_cases):
            best_idx = int(np.argmax(sim_matrix[i]))
            case.best_score = float(round(sim_matrix[i][best_idx], 4))
            case.best_probe = probes[best_idx]
            case.covered = case.best_score >= self.threshold

        # Aggregate
        gaps = [c for c in all_cases if not c.covered]
        covered_count = len(all_cases) - len(gaps)
        coverage_pct = round(covered_count / len(all_cases) * 100, 1) if all_cases else 0.0

        entity_names = [e["entity"] for e in entities]
        entity_fully_covered, entity_with_gaps = [], []
        for ent_name in entity_names:
            ent_cases = [c for c in all_cases if c.entity == ent_name]
            if all(c.covered for c in ent_cases):
                entity_fully_covered.append(ent_name)
            else:
                entity_with_gaps.append(ent_name)

        return DomainCoverageReport(
            domain=domain,
            display_name=display,
            total_probes=len(probes),
            total_edge_cases=len(all_cases),
            covered_count=covered_count,
            uncovered_count=len(gaps),
            coverage_pct=coverage_pct,
            edge_cases=all_cases,
            uncovered_gaps=gaps,
            suggested_probes=[_suggest_probe(g) for g in gaps],
            entities_fully_covered=entity_fully_covered,
            entities_with_gaps=entity_with_gaps,
        )

    def analyze_all(self) -> list[DomainCoverageReport]:
        return [
            self.analyze_domain(domain)
            for domain in self.cfg["domains"]
        ]
