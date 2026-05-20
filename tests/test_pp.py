"""Tests for modules/pp/pipeline_pulse.py"""
import sys; sys.path.insert(0, "..")
from modules.pp.pipeline_pulse import PipelinePulse, RemediationOption

def test_scan_returns_reports():
    pp = PipelinePulse()
    reports = pp.scan_all_domains()
    assert len(reports) >= 1
    for r in reports:
        assert r.domain
        assert r.failure_type in ("silent_drop","schema_drift","gap","hard_failure","unknown")

def test_health_summary_keys():
    pp = PipelinePulse()
    h  = pp.get_pipeline_health_summary()
    assert "failures_found" in h
    assert "total_domains"  in h
    assert "by_domain"      in h

def test_low_risk_remediation_executes():
    pp  = PipelinePulse()
    opt = RemediationOption("alert_only","Notify owner",1,"Low",True)
    res = pp.execute_remediation(opt, "customer_segments")
    assert res.status == "executed"

def test_high_risk_requires_approval():
    pp  = PipelinePulse()
    opt = RemediationOption("full_refresh","Full rebuild",60,"High",False)
    res = pp.execute_remediation(opt, "customer_segments", approved_by="")
    assert res.status == "pending_approval"
