"""Tests for modules/avl/value_ledger.py"""
import sys; sys.path.insert(0, "..")
from modules.avl.value_ledger import AIValueLedger

def test_exposure_report_fields():
    avl = AIValueLedger()
    exp = avl.calculate_exposure("customer_segments", 30)
    assert exp.domain == "customer_segments"
    assert exp.financial_exposure_usd >= 0
    assert exp.eu_ai_act_category in ("Limited","High","Unacceptable")
    assert exp.estimated_bad_decisions <= exp.total_decisions

def test_recovery_report_fields():
    avl = AIValueLedger()
    rec = avl.calculate_recovery_value("test-id", "Enterprise")
    assert rec.total_recovery_usd >= 0
    assert rec.roi_multiplier >= 0

def test_value_summary_keys():
    avl = AIValueLedger()
    vs  = avl.get_value_summary(30)
    assert "total_exposure_identified_usd" in vs
    assert "total_recovered_usd"           in vs
    assert "net_aria_value_usd"            in vs
    assert "roi_by_domain"                 in vs

def test_all_domains_in_summary():
    avl     = AIValueLedger()
    vs      = avl.get_value_summary(30)
    domains = list(vs["roi_by_domain"].keys())
    assert len(domains) >= 1
