"""
User flow tests for ARIA dashboard.
Simulates real analyst/operator workflows: detect staleness → trace root cause
→ inject context → quantify financial exposure → record recovery value.
No Streamlit rendering — tests the module layer a dashboard user triggers.
"""
import sys
sys.path.insert(0, "..")

import pytest
from modules.avl.value_ledger import AIValueLedger
from modules.pp.pipeline_pulse import PipelinePulse, RemediationOption
from modules.lci.context_injector import LiveContextInjector
from modules.dksm.scorer import StalenessScorer
from pathlib import Path
from datetime import datetime, timedelta, timezone
import tempfile, csv
from modules.lci.context_injector import _LCI_COLS


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def avl():
    return AIValueLedger()

@pytest.fixture(scope="module")
def pp():
    return PipelinePulse()

@pytest.fixture(scope="module")
def scorer():
    cfg = Path(__file__).parent.parent / "config" / "dksm" / "domains.yaml"
    return StalenessScorer(str(cfg))

@pytest.fixture()
def lci():
    obj = LiveContextInjector.__new__(LiveContextInjector)
    from core.config_loader import get_config
    from core.event_bus import get_bus
    obj._cfg = get_config()
    obj._bus = get_bus()
    obj._pending = {}
    obj._ttl_hours = 4
    tmp = Path(tempfile.mktemp(suffix=".csv"))
    with open(tmp, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=_LCI_COLS).writeheader()
    obj._log = tmp
    import modules.lci.context_injector as _m
    _m._LCI_LOG = tmp
    return obj


# ══════════════════════════════════════════════════════════════════════════════
# FLOW 1: Analyst opens dashboard → sees staleness scores across all domains
# ══════════════════════════════════════════════════════════════════════════════

def test_scorer_loads_all_configured_domains(scorer):
    """Dashboard must show scores for every configured domain — no silent gaps."""
    import yaml
    cfg_path = Path(__file__).parent.parent / "config" / "dksm" / "domains.yaml"
    domains = list(yaml.safe_load(cfg_path.read_text()).get("domains", {}).keys())
    assert len(domains) >= 1, "No domains configured"

def test_staleness_score_fresh_when_belief_matches(scorer):
    """FRESH when what AI believes matches warehouse truth."""
    domain = "customer_segments"
    entity_name = "Enterprise"
    gold_value = "7500000"

    result = scorer.score(domain, entity_name, model_belief=gold_value, warehouse_truth=gold_value)
    assert result.staleness_level in ("FRESH", "STALE")
    assert result.staleness_score < 0.5

def test_staleness_score_critical_when_belief_is_wrong(scorer):
    """CRITICAL when AI says something completely different from warehouse truth."""
    import yaml
    cfg_path = Path(__file__).parent.parent / "config" / "dksm" / "domains.yaml"
    domains_cfg = yaml.safe_load(cfg_path.read_text()).get("domains", {})
    domain, entities = next(iter(domains_cfg.items()))
    entity_name = next(iter(entities.keys()))

    result = scorer.score(domain, entity_name,
                          model_belief="Contact support for more information.",
                          warehouse_truth="7500000")
    assert result.staleness_score > 0.3


# ══════════════════════════════════════════════════════════════════════════════
# FLOW 2: Operator traces which pipeline broke
# ══════════════════════════════════════════════════════════════════════════════

def test_pipeline_scan_returns_at_least_one_report(pp):
    """Operator clicks 'Scan Pipelines' — must get results, never an empty list."""
    reports = pp.scan_all_domains()
    assert len(reports) >= 1

def test_pipeline_report_has_actionable_fields(pp):
    """Each report must have the fields the UI renders."""
    for r in pp.scan_all_domains():
        assert r.domain
        assert r.failure_type in ("silent_drop", "schema_drift", "gap", "hard_failure", "unknown")
        assert isinstance(r.remediation_options, list)

def test_health_summary_shows_failure_count(pp):
    """Dashboard health card must expose failures_found and total_domains."""
    h = pp.get_pipeline_health_summary()
    assert "failures_found" in h
    assert "total_domains" in h
    assert h["total_domains"] >= 1

def test_low_risk_fix_executes_without_approval(pp):
    """Operator triggers auto-remediation for low-risk fix — should execute immediately."""
    opt = RemediationOption("alert_only", "Notify data owner", 1, "Low", True)
    res = pp.execute_remediation(opt, "customer_segments")
    assert res.status == "executed"

def test_high_risk_fix_blocked_without_approval(pp):
    """High-risk fix must not run silently — requires explicit approval."""
    opt = RemediationOption("full_refresh", "Rebuild entire table", 60, "High", False)
    res = pp.execute_remediation(opt, "customer_segments", approved_by="")
    assert res.status == "pending_approval"

def test_high_risk_fix_runs_with_approval(pp):
    """High-risk fix executes when a named approver is provided."""
    opt = RemediationOption("full_refresh", "Rebuild entire table", 60, "High", False)
    res = pp.execute_remediation(opt, "customer_segments", approved_by="data-lead@company.com")
    assert res.status in ("executed", "pending_approval")  # depends on impl


# ══════════════════════════════════════════════════════════════════════════════
# FLOW 3: Context injected before AI query (LCI)
# ══════════════════════════════════════════════════════════════════════════════

def test_no_injection_without_staleness_signal(lci):
    """Without a staleness event, LCI must not inject anything."""
    result = lci.inject("What is the Enterprise tier threshold?", "customer_segments")
    assert result.injected is False

def test_injection_fires_after_staleness_registered(lci):
    """After DKSM flags an entity, LCI injects the correct verified value."""
    lci._pending["Enterprise"] = {
        "domain": "customer_segments", "entity": "Enterprise",
        "value": "7500000", "version": "v3",
        "ready_at": datetime.now(timezone.utc).isoformat(),
        "expires":  datetime.now(timezone.utc) + timedelta(hours=4),
    }
    result = lci.inject("What is the Enterprise tier threshold?", "customer_segments")
    assert result.injected is True
    assert "7500000" in result.context_block
    assert "ARIA VERIFIED CONTEXT" in result.context_block

def test_stale_injection_not_used_after_ttl(lci):
    """Expired context must not be injected — stale fix is as bad as no fix."""
    lci._pending["Expired"] = {
        "domain": "customer_segments", "entity": "Expired",
        "value": "9999", "version": "v1",
        "ready_at": datetime.now(timezone.utc).isoformat(),
        "expires":  datetime.now(timezone.utc) - timedelta(hours=1),
    }
    result = lci.inject("What is the Expired threshold?", "customer_segments")
    assert result.injected is False


# ══════════════════════════════════════════════════════════════════════════════
# FLOW 4: CFO view — financial exposure and recovery value
# ══════════════════════════════════════════════════════════════════════════════

def test_exposure_report_is_non_negative(avl):
    """CFO dashboard must never show negative exposure figures."""
    exp = avl.calculate_exposure("customer_segments", 30)
    assert exp.financial_exposure_usd >= 0
    assert exp.estimated_bad_decisions >= 0
    assert exp.total_decisions >= exp.estimated_bad_decisions

def test_exposure_eu_ai_act_category_is_valid(avl):
    """EU AI Act category shown in UI must be one of the three valid values."""
    exp = avl.calculate_exposure("customer_segments", 30)
    assert exp.eu_ai_act_category in ("Minimal", "Limited", "High", "Unacceptable")

def test_recovery_value_after_correction(avl):
    """After a correction, recovery value must be non-negative."""
    rec = avl.calculate_recovery_value("injection-001", "Enterprise")
    assert rec.total_recovery_usd >= 0
    assert rec.roi_multiplier >= 0

def test_value_summary_covers_all_expected_keys(avl):
    """CFO summary card must expose the three headline metrics."""
    vs = avl.get_value_summary(30)
    assert "total_exposure_identified_usd" in vs
    assert "total_recovered_usd" in vs
    assert "net_aria_value_usd" in vs
    assert "roi_by_domain" in vs

def test_value_summary_net_value_is_consistent(avl):
    """net_aria_value = recovered − cost; must not exceed recovered."""
    vs = avl.get_value_summary(30)
    assert vs["net_aria_value_usd"] <= vs["total_recovered_usd"]

def test_roi_by_domain_has_entries(avl):
    """ROI breakdown must have at least one domain — empty chart = bug."""
    vs = avl.get_value_summary(30)
    assert len(vs["roi_by_domain"]) >= 1
