"""
Tests for modules/dksm/prompt_coverage.py
"""
import sys
sys.path.insert(0, "..")

import pytest
from pathlib import Path
from modules.dksm.prompt_coverage import (
    PromptCoverageAnalyzer,
    _generate_edge_cases,
    _suggest_probe,
    EdgeCase,
)


@pytest.fixture(scope="module")
def analyzer():
    return PromptCoverageAnalyzer()


# ── edge case generator ───────────────────────────────────────────────────────

def test_generates_exact_value_case():
    cases = _generate_edge_cases(
        entity="Enterprise", value="7500000", model_belief="6000000",
        expiry_date="2026-12-31", tier="", prior_auth="",
        domain_display="Customer Segments", value_col="min_annual_revenue_usd",
    )
    types = [c.case_type for c in cases]
    assert "exact_value" in types

def test_generates_stale_belief_case_when_belief_differs():
    cases = _generate_edge_cases(
        entity="Enterprise", value="7500000", model_belief="6000000",
        expiry_date="", tier="", prior_auth="",
        domain_display="Customer Segments", value_col="min_annual_revenue_usd",
    )
    types = [c.case_type for c in cases]
    assert "stale_belief" in types

def test_no_stale_belief_when_belief_matches():
    cases = _generate_edge_cases(
        entity="Enterprise", value="7500000", model_belief="7500000",
        expiry_date="", tier="", prior_auth="",
        domain_display="Customer Segments", value_col="min_annual_revenue_usd",
    )
    types = [c.case_type for c in cases]
    assert "stale_belief" not in types

def test_generates_tier_case_when_tier_present():
    cases = _generate_edge_cases(
        entity="Humira Biosimilar", value="45", model_belief="90",
        expiry_date="2026-06-30", tier="Tier 3", prior_auth="true",
        domain_display="Drug Formulary", value_col="tier_copay_usd",
    )
    types = [c.case_type for c in cases]
    assert "tier" in types
    assert "auth" in types
    assert "expiry" in types

def test_generates_comparison_and_boundary_always():
    cases = _generate_edge_cases(
        entity="SMB", value="1000000", model_belief="",
        expiry_date="", tier="", prior_auth="",
        domain_display="Customer Segments", value_col="min_annual_revenue_usd",
    )
    types = [c.case_type for c in cases]
    assert "comparison" in types
    assert "boundary" in types


# ── suggest probe ─────────────────────────────────────────────────────────────

def test_suggest_probe_returns_string_for_all_types():
    for case_type in ["exact_value", "tier", "auth", "comparison", "expiry", "boundary", "stale_belief"]:
        gap = EdgeCase(entity="TestEntity", case_type=case_type,
                       canonical_query="", best_probe=None, best_score=0.0, covered=False)
        suggestion = _suggest_probe(gap)
        assert isinstance(suggestion, str)
        assert len(suggestion) > 0


# ── domain analysis ───────────────────────────────────────────────────────────

def test_analyze_domain_returns_report(analyzer):
    report = analyzer.analyze_domain("drug_formulary")
    assert report.domain == "drug_formulary"
    assert report.total_probes >= 1
    assert report.total_edge_cases > 0
    assert 0.0 <= report.coverage_pct <= 100.0

def test_analyze_domain_coverage_pct_consistent(analyzer):
    report = analyzer.analyze_domain("drug_formulary")
    expected_pct = round(report.covered_count / report.total_edge_cases * 100, 1)
    assert report.coverage_pct == expected_pct

def test_covered_plus_uncovered_equals_total(analyzer):
    report = analyzer.analyze_domain("customer_segments")
    assert report.covered_count + report.uncovered_count == report.total_edge_cases

def test_uncovered_gaps_match_uncovered_count(analyzer):
    report = analyzer.analyze_domain("customer_segments")
    assert len(report.uncovered_gaps) == report.uncovered_count

def test_suggested_probes_match_gap_count(analyzer):
    report = analyzer.analyze_domain("drug_formulary")
    assert len(report.suggested_probes) == report.uncovered_count

def test_entities_with_gaps_are_subset_of_all_entities(analyzer):
    # drug_formulary is the domain with confirmed Gold layer data
    report = analyzer.analyze_domain("drug_formulary")
    all_entities = set(report.entities_fully_covered) | set(report.entities_with_gaps)
    assert len(all_entities) > 0
    for e in report.entities_with_gaps:
        assert e not in report.entities_fully_covered

def test_each_edge_case_has_best_probe(analyzer):
    report = analyzer.analyze_domain("drug_formulary")
    for case in report.edge_cases:
        assert case.best_probe is not None
        assert 0.0 <= case.best_score <= 1.0

def test_covered_cases_score_above_threshold(analyzer):
    report = analyzer.analyze_domain("drug_formulary")
    for case in report.edge_cases:
        if case.covered:
            assert case.best_score >= analyzer.threshold

def test_uncovered_cases_score_below_threshold(analyzer):
    report = analyzer.analyze_domain("drug_formulary")
    for case in report.edge_cases:
        if not case.covered:
            assert case.best_score < analyzer.threshold


# ── analyze all domains ───────────────────────────────────────────────────────

def test_analyze_all_returns_all_domains(analyzer):
    reports = analyzer.analyze_all()
    assert len(reports) == 7   # 7 domains in domains.yaml

def test_analyze_all_no_domain_exceeds_100_pct(analyzer):
    for report in analyzer.analyze_all():
        assert report.coverage_pct <= 100.0

def test_stricter_threshold_reduces_coverage():
    loose = PromptCoverageAnalyzer(threshold=0.40)
    strict = PromptCoverageAnalyzer(threshold=0.75)
    r_loose = loose.analyze_domain("drug_formulary")
    r_strict = strict.analyze_domain("drug_formulary")
    assert r_loose.covered_count >= r_strict.covered_count
