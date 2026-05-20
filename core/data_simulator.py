"""
ARIA Data Simulator
Generates all simulation CSV files on first ARIA startup (skips if files exist).
Run: python -m core.data_simulator
"""
from __future__ import annotations

import csv
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DATA = Path(__file__).parent.parent / "data"
_PIPELINE_LOG      = _DATA / "pipeline_log.csv"
_BUSINESS_OUTCOMES = _DATA / "business_outcomes.csv"
_FEEDBACK_LOG      = _DATA / "feedback_log.csv"
_APPROVAL_QUEUE    = _DATA / "approval_queue.csv"
_FINE_TUNE_PAIRS   = _DATA / "fine_tune_pairs.jsonl"
_LCI_LOG           = _DATA / "lci_log.csv"

random.seed(42)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(n: float) -> datetime:
    return _now() - timedelta(days=n)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline log — 500 rows, 3 dbt models, 6 months of history
# ──────────────────────────────────────────────────────────────────────────────

_MODELS = [
    ("fct_customer_segments", "customer_segments"),
    ("fct_product_catalog",   "product_catalog"),
    ("fct_risk_thresholds",   "risk_thresholds"),
    ("fct_drug_formulary",    "drug_formulary"),
    ("fct_coverage_limits",   "coverage_limits"),
    ("fct_carrier_rates",     "carrier_rates"),
    ("fct_coupons",           "coupons"),
]

_SCHEMA_VERSIONS = {
    "fct_customer_segments": ["v1.2", "v1.3"],
    "fct_product_catalog":   ["v2.0", "v2.1"],
    "fct_risk_thresholds":   ["v3.1"],
    "fct_drug_formulary":    ["v2.0", "v3.0"],
    "fct_coverage_limits":   ["v4.0", "v4.1"],
    "fct_carrier_rates":     ["v1.5"],
    "fct_coupons":           ["v1.0", "v1.1"],
}


def generate_pipeline_log() -> None:
    if _PIPELINE_LOG.exists():
        return

    rows = []
    # Baseline rows — normal runs
    for day_offset in range(180):
        dt = _days_ago(180 - day_offset)
        for model, domain in _MODELS:
            schema_list = _SCHEMA_VERSIONS[model]
            schema = schema_list[0] if day_offset < 89 else schema_list[-1]
            base_rows = (
                10000 if domain == "customer_segments" else
                25000 if domain == "product_catalog"   else
                8000  if domain == "coverage_limits"   else
                30000 if domain == "carrier_rates"     else
                50000 if domain == "coupons"           else
                5000
            )

            # Inject anomalies
            status = "success"
            rows_affected = base_rows + random.randint(-200, 200)
            error_msg = ""
            duration = random.randint(45, 120)

            # Silent drop: customer_segments dropped 40% rows 127 days ago for 3 days
            if domain == "customer_segments" and 124 <= day_offset <= 127:
                rows_affected = int(base_rows * 0.58) + random.randint(-50, 50)

            # Silent drop: drug_formulary dropped biosimilar rows 94 days ago
            if domain == "drug_formulary" and 92 <= day_offset <= 95:
                rows_affected = int(base_rows * 0.60) + random.randint(-30, 30)

            # Schema drift: product_catalog changed schema version at day 89
            if domain == "product_catalog" and day_offset == 89:
                schema = schema_list[1]  # version bump

            # Silent drop: coupons dropped 35% rows when new coupon program launched 68 days ago
            if domain == "coupons" and 66 <= day_offset <= 69:
                rows_affected = int(base_rows * 0.62) + random.randint(-100, 100)

            # Schema drift: coverage_limits v4.0 → v4.1 at day 45 (benefits update)
            if domain == "coverage_limits" and day_offset == 45:
                schema = _SCHEMA_VERSIONS["fct_coverage_limits"][1]

            # Gap: carrier_rates missed a monthly refresh (day_offset 30 = 150 days ago)
            # (handled naturally — no runs for 48h around offset 30)

            # Hard failure: risk_thresholds failed for 6 hours at day 203 ago (~day 23)
            if domain == "risk_thresholds" and day_offset == 23:
                status = "failed"
                rows_affected = 0
                error_msg = "Source table timeout: RiskEngine_v2 connection refused"
                duration = 600

            rows.append({
                "run_id":         str(uuid.uuid4())[:8],
                "dbt_model":      model,
                "domain":         domain,
                "run_timestamp":  _iso(dt + timedelta(hours=random.randint(1, 3))),
                "status":         status,
                "duration_seconds": duration,
                "rows_affected":  rows_affected,
                "error_message":  error_msg,
                "schema_version": schema,
                "source_table":   f"{domain}_raw",
            })

    _write_csv(_PIPELINE_LOG, rows)
    print(f"Generated {len(rows)} pipeline log rows → {_PIPELINE_LOG}")


# ──────────────────────────────────────────────────────────────────────────────
# Business outcomes — 300 rows linked to decisions
# ──────────────────────────────────────────────────────────────────────────────

_DOMAINS = [
    "customer_segments", "product_catalog", "risk_thresholds",
    "drug_formulary", "coverage_limits", "carrier_rates", "coupons",
]
_OUTCOME_TYPES = ["deal_closed", "deal_lost", "escalated", "approved", "rejected"]


def generate_business_outcomes() -> None:
    if _BUSINESS_OUTCOMES.exists():
        return

    rows = []
    for i in range(300):
        domain = random.choice(_DOMAINS)
        used_stale = random.random() < 0.65
        correction = (not used_stale) or (random.random() < 0.3)

        # Stale context → worse outcomes; correction applied → better
        if used_stale and not correction:
            value = random.gauss(31000, 8000)
        elif correction:
            value = random.gauss(67000, 12000)
        else:
            value = random.gauss(52000, 9000)

        rows.append({
            "outcome_id":          f"OC-{i+1:04d}",
            "decision_id":         f"DEC-{random.randint(1, 500):04d}",
            "outcome_type":        random.choice(_OUTCOME_TYPES),
            "outcome_value_usd":   round(max(value, 0), 2),
            "outcome_date":        _iso(_days_ago(random.uniform(0, 90))),
            "domain_referenced":   domain,
            "used_stale_context":  used_stale,
            "correction_applied":  correction,
        })

    _write_csv(_BUSINESS_OUTCOMES, rows)
    print(f"Generated {len(rows)} business outcome rows → {_BUSINESS_OUTCOMES}")


# ──────────────────────────────────────────────────────────────────────────────
# Feedback log — 150 correction signals
# ──────────────────────────────────────────────────────────────────────────────

_SIGNAL_TYPES = ["user_correction", "agent_override", "escalation", "non_use"]
_FLE_STATUSES = ["applied", "applied", "applied", "pending", "classified"]

_ENTITIES = {
    "customer_segments": ["Enterprise", "Mid-Market", "Strategic"],
    "product_catalog":   ["DataSense Pro", "DataSense Enterprise", "InsightFlow Core"],
    "risk_thresholds":   ["Portfolio VaR - High", "Credit Exposure Limit - High",
                          "Credit Exposure Limit - Medium"],
    "drug_formulary":    ["Humira Biosimilar", "Ozempic", "Atorvastatin"],
    "coverage_limits":   ["Gold Plan OOP Max", "Silver Plan Deductible", "Platinum Benefit Cap"],
    "carrier_rates":     ["CPT 99213 PPO Rate", "MRI Allowed Amount", "Ambulance Rate/Mile"],
    "coupons":           ["Ozempic Copay Card", "Humira Copay Card", "Dupixent MyWay"],
}


def generate_feedback_log() -> None:
    if _FEEDBACK_LOG.exists():
        return

    rows = []
    for i in range(150):
        domain = random.choice(_DOMAINS)
        entity = random.choice(_ENTITIES[domain])
        signal_type = random.choice(_SIGNAL_TYPES)
        fle_status = random.choice(_FLE_STATUSES)

        rows.append({
            "signal_id":    f"SIG-{i+1:04d}",
            "timestamp":    _iso(_days_ago(random.uniform(0, 30))),
            "signal_type":  signal_type,
            "domain":       domain,
            "entity":       entity,
            "wrong_value":  str(round(random.uniform(1000, 9000000), 0)),
            "correct_value": str(round(random.uniform(1000, 9000000), 0)),
            "confidence":   round(random.uniform(0.6, 1.0), 3),
            "fle_status":   fle_status,
            "propagated":   fle_status == "applied",
        })

    _write_csv(_FEEDBACK_LOG, rows)
    print(f"Generated {len(rows)} feedback log rows → {_FEEDBACK_LOG}")


# ──────────────────────────────────────────────────────────────────────────────
# Supporting files (approval queue, fine-tune pairs)
# ──────────────────────────────────────────────────────────────────────────────

def generate_support_files() -> None:
    if not _APPROVAL_QUEUE.exists():
        _aq_cols = [
            "request_id", "timestamp", "source_module", "event_type",
            "domain", "entity", "proposed_action", "risk_level",
            "payload_summary", "status", "decided_by", "decided_at",
        ]
        _aq_rows = [
            {
                "request_id":     "AQ-0001",
                "timestamp":      _iso(_days_ago(7.9)),
                "source_module":  "FLE",
                "event_type":     "APPROVAL_REQUIRED",
                "domain":         "customer_segments",
                "entity":         "Enterprise",
                "proposed_action":"flag_gold_and_reprobe",
                "risk_level":     "Medium",
                "payload_summary":"Enterprise threshold mismatch: belief=$6M truth=$7.5M | exposure=$1.8M | EU AI Act: High",
                "status":         "pending",
                "decided_by":     "",
                "decided_at":     "",
            },
            {
                "request_id":     "AQ-0002",
                "timestamp":      _iso(_days_ago(3.3)),
                "source_module":  "DKSM",
                "event_type":     "APPROVAL_REQUIRED",
                "domain":         "drug_formulary",
                "entity":         "Humira Biosimilar",
                "proposed_action":"remove_formulary_record",
                "risk_level":     "Medium",
                "payload_summary":"Humira copay stale: belief=$90 truth=$45 | expiry=2026-06-30 | 42 days remaining | EU AI Act: High",
                "status":         "pending",
                "decided_by":     "",
                "decided_at":     "",
            },
        ]
        _write_csv(_APPROVAL_QUEUE, _aq_rows, fieldnames=[
            "request_id", "timestamp", "source_module", "event_type",
            "domain", "entity", "proposed_action", "risk_level",
            "payload_summary", "status", "decided_by", "decided_at",
        ])
        print(f"Created empty approval queue → {_APPROVAL_QUEUE}")

    if not _FINE_TUNE_PAIRS.exists():
        _FINE_TUNE_PAIRS.touch()
        print(f"Created empty fine-tune pairs → {_FINE_TUNE_PAIRS}")

    if not _LCI_LOG.exists():
        _lci_cols = ["injection_id","timestamp","domain","entity",
                     "injected_value","query_preview","source_version",
                     "expires_at","triggered_by"]
        _lci_rows = [
            {
                "injection_id":   "a1b2c3d4",
                "timestamp":      _iso(_days_ago(0.13)),
                "domain":         "customer_segments",
                "entity":         "Enterprise",
                "injected_value": "7500000",
                "query_preview":  "What is the minimum revenue threshold for Enterprise tier?",
                "source_version": "v3",
                "expires_at":     _iso(_now() + timedelta(hours=4)),
                "triggered_by":   "agent_call",
            },
            {
                "injection_id":   "e5f6g7h8",
                "timestamp":      _iso(_days_ago(0.08)),
                "domain":         "drug_formulary",
                "entity":         "Humira Biosimilar",
                "injected_value": "45",
                "query_preview":  "What is the current copay for Humira Biosimilar under the formulary?",
                "source_version": "v3",
                "expires_at":     _iso(_now() + timedelta(hours=3, minutes=45)),
                "triggered_by":   "agent_call",
            },
        ]
        _write_csv(_LCI_LOG, _lci_rows, fieldnames=_lci_cols)
        print(f"Seeded LCI log → {_LCI_LOG}")

    _seed_event_bus()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _write_csv(path: Path, rows: list[dict],
               fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and fieldnames is None:
        return
    keys = fieldnames or list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _seed_event_bus() -> None:
    """
    Write a realistic 24-hour cross-module event chain to the event bus JSONL.
    Only runs if the file is missing or empty.
    Covers all 6 modules: DKSM → LCI → PP → AVL → FLE → ASGC.
    """
    _BUS_FILE = _DATA / "aria_event_bus.jsonl"
    _BUS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Only seed if file is missing or empty
    if _BUS_FILE.exists() and _BUS_FILE.stat().st_size > 0:
        return

    now = _now()

    def _evt(module, event_type, domain, entity, payload, severity, minutes_ago,
             requires_approval=False):
        if severity == "CRITICAL":
            requires_approval = True
        return {
            "event_id":          str(uuid.uuid4()),
            "timestamp":         (now - timedelta(minutes=minutes_ago)).isoformat(),
            "source_module":     module,
            "event_type":        event_type,
            "domain":            domain,
            "entity":            entity,
            "payload":           payload,
            "severity":          severity,
            "requires_approval": requires_approval,
        }

    # --- Scenario 1: customer_segments / Enterprise (127 days stale) [Financial_Services]
    chain1 = [
        _evt("DKSM", "STALENESS_DETECTED", "customer_segments", "Enterprise",
             {"level": "CRITICAL", "sim": 0.80, "days_since_update": 127,
              "belief": "6000000", "truth": "7500000",
              "industry": "Financial_Services", "error_corrected": False}, "CRITICAL", 480),
        _evt("LCI",  "CONTEXT_INJECTED",   "customer_segments", "Enterprise",
             {"status": "ready", "value": "7500000", "version": "v3",
              "expires_hours": 4}, "INFO", 479),
        _evt("PP",   "PIPELINE_FAILURE_FOUND", "customer_segments", "Enterprise",
             {"dbt_model": "fct_customer_segments", "failure_type": "silent_drop",
              "days_since": 127, "rows_dropped_pct": 40}, "WARNING", 478),
        _evt("AVL",  "VALUE_CALCULATED",   "customer_segments", "Enterprise",
             {"exposure_usd": 1800000, "recovery_potential_usd": 1170000,
              "eu_ai_act": "High", "error_rate": 0.15}, "INFO", 477),
        _evt("FLE",  "CORRECTION_RECEIVED","customer_segments", "Enterprise",
             {"signal_type": "agent_override", "wrong_value": "6000000",
              "correct_value": "7500000", "confidence": 1.0}, "INFO", 460),
        _evt("ASGC", "APPROVAL_REQUIRED",  "customer_segments", "Enterprise",
             {"action": "flag_gold_and_reprobe", "risk_level": "Medium",
              "requested_by": "FLE"}, "WARNING", 459, requires_approval=True),
        _evt("FLE",  "CORRECTION_APPLIED", "customer_segments", "Enterprise",
             {"actions": ["Gold flagged", "reprobe triggered"],
              "error_type": "THRESHOLD_ERROR", "industry": "Financial_Services",
              "corrected": True}, "INFO", 450),
        _evt("ASGC", "APPROVAL_GRANTED",   "customer_segments", "Enterprise",
             {"action": "flag_gold_and_reprobe", "approved_by": "Lead",
              "industry": "Financial_Services"}, "INFO", 440),
    ]

    # --- Scenario 2: risk_thresholds / Portfolio VaR - High [Financial_Services]
    chain2 = [
        _evt("DKSM", "STALENESS_DETECTED", "risk_thresholds", "Portfolio VaR - High",
             {"level": "CRITICAL", "sim": 0.30, "days_since_update": 210,
              "belief": "1.5", "truth": "5.01",
              "industry": "Financial_Services", "error_corrected": False}, "CRITICAL", 360),
        _evt("LCI",  "CONTEXT_INJECTED",   "risk_thresholds", "Portfolio VaR - High",
             {"status": "ready", "value": "5.01", "version": "v2",
              "expires_hours": 4}, "INFO", 359),
        _evt("PP",   "PIPELINE_FAILURE_FOUND", "risk_thresholds", "Portfolio VaR - High",
             {"dbt_model": "fct_risk_thresholds", "failure_type": "hard_failure",
              "days_since": 23}, "WARNING", 358),
        _evt("AVL",  "VALUE_CALCULATED",   "risk_thresholds", "Portfolio VaR - High",
             {"exposure_usd": 560000, "recovery_potential_usd": 364000,
              "eu_ai_act": "High", "error_rate": 0.65}, "INFO", 357),
        _evt("FLE",  "CORRECTION_RECEIVED","risk_thresholds", "Portfolio VaR - High",
             {"signal_type": "user_correction", "wrong_value": "1.5",
              "correct_value": "5.01", "confidence": 0.95}, "INFO", 300),
        _evt("FLE",  "CORRECTION_APPLIED", "risk_thresholds", "Portfolio VaR - High",
             {"actions": ["Gold flagged", "RAG reweighted"],
              "error_type": "THRESHOLD_ERROR", "industry": "Financial_Services",
              "corrected": True}, "INFO", 295),
    ]

    # --- Scenario 3: product_catalog / DataSense Pro [Retail]
    chain3 = [
        _evt("DKSM", "STALENESS_DETECTED", "product_catalog", "DataSense Pro",
             {"level": "STALE", "sim": 0.84, "days_since_update": 89,
              "belief": "699", "truth": "3000",
              "industry": "Retail", "error_corrected": False}, "WARNING", 240),
        _evt("LCI",  "CONTEXT_INJECTED",   "product_catalog", "DataSense Pro",
             {"status": "ready", "value": "3000", "version": "v4",
              "expires_hours": 4}, "INFO", 239),
        _evt("PP",   "PIPELINE_FAILURE_FOUND", "product_catalog", "DataSense Pro",
             {"dbt_model": "fct_product_catalog", "failure_type": "schema_drift",
              "days_since": 89}, "WARNING", 238),
        _evt("AVL",  "VALUE_CALCULATED",   "product_catalog", "DataSense Pro",
             {"exposure_usd": 420000, "recovery_potential_usd": 273000,
              "eu_ai_act": "Limited", "error_rate": 0.10}, "INFO", 237),
        _evt("FLE",  "CORRECTION_RECEIVED","product_catalog", "DataSense Pro",
             {"signal_type": "agent_override", "wrong_value": "699",
              "correct_value": "3000", "confidence": 1.0}, "INFO", 180),
        _evt("FLE",  "CORRECTION_APPLIED", "product_catalog", "DataSense Pro",
             {"actions": ["fine_tune_pair generated"],
              "error_type": "THRESHOLD_ERROR", "industry": "Retail",
              "corrected": True}, "INFO", 175),
        _evt("ASGC", "APPROVAL_REQUIRED",  "product_catalog", "DataSense Pro",
             {"action": "pipeline_auto_remediate", "risk_level": "High",
              "requested_by": "PP", "industry": "Retail"}, "WARNING", 174, requires_approval=True),
        _evt("ASGC", "APPROVAL_GRANTED",   "product_catalog", "DataSense Pro",
             {"action": "pipeline_auto_remediate", "approved_by": "Lead",
              "industry": "Retail"}, "INFO", 120),
    ]

    # --- Scenario 4: recent corrections within last 2 hours [Financial_Services]
    recent = [
        _evt("DKSM", "STALENESS_DETECTED", "customer_segments", "Mid-Market",
             {"level": "STALE", "sim": 0.82, "days_since_update": 60,
              "belief": "1000000", "truth": "2500000",
              "industry": "Financial_Services", "error_corrected": False}, "WARNING", 90),
        _evt("LCI",  "CONTEXT_INJECTED",   "customer_segments", "Mid-Market",
             {"status": "ready", "value": "2500000", "version": "v2",
              "industry": "Financial_Services"}, "INFO", 89),
        _evt("FLE",  "CORRECTION_RECEIVED","customer_segments", "Mid-Market",
             {"signal_type": "user_correction", "wrong_value": "1000000",
              "correct_value": "2500000", "confidence": 0.9,
              "industry": "Financial_Services", "corrected": False}, "INFO", 45),
        _evt("FLE",  "CORRECTION_APPLIED", "customer_segments", "Mid-Market",
             {"actions": ["Gold flagged"], "error_type": "THRESHOLD_ERROR",
              "industry": "Financial_Services", "corrected": True}, "INFO", 40),
        _evt("ASGC", "APPROVAL_REQUIRED",  "customer_segments", "Mid-Market",
             {"action": "flag_gold_and_reprobe", "risk_level": "Low",
              "requested_by": "FLE", "industry": "Financial_Services"}, "INFO", 39),
        _evt("ASGC", "APPROVAL_GRANTED",   "customer_segments", "Mid-Market",
             {"action": "flag_gold_and_reprobe", "approved_by": "Lead",
              "industry": "Financial_Services"}, "INFO", 30),
    ]

    # --- Scenario 5: Healthcare — drug formulary expiry + staleness (Humira)
    chain5 = [
        _evt("DKSM", "EXPIRY_ALERT",          "drug_formulary", "Humira Biosimilar",
             {"industry": "Healthcare", "expiry_date": "2026-06-30",
              "days_until_expiry": 42, "level": "EXPIRING", "recommended_action": "UPDATE",
              "detail": "Formulary tier review due — copay may change at renewal"}, "WARNING", 200),
        _evt("DKSM", "STALENESS_DETECTED",    "drug_formulary", "Humira Biosimilar",
             {"level": "CRITICAL", "sim": 0.50, "days_since_update": 94,
              "belief": "90", "truth": "45", "industry": "Healthcare",
              "error_corrected": False}, "CRITICAL", 195),
        _evt("LCI",  "CONTEXT_INJECTED",      "drug_formulary", "Humira Biosimilar",
             {"status": "ready", "value": "45", "version": "v3",
              "expires_hours": 4, "industry": "Healthcare"}, "INFO", 194),
        _evt("PP",   "PIPELINE_FAILURE_FOUND","drug_formulary", "Humira Biosimilar",
             {"dbt_model": "fct_drug_formulary", "failure_type": "silent_drop",
              "days_since": 94, "rows_dropped_pct": 40,
              "detail": "Biosimilar rows dropped in bronze-to-silver ETL run",
              "industry": "Healthcare"}, "WARNING", 193),
        _evt("AVL",  "VALUE_CALCULATED",      "drug_formulary", "Humira Biosimilar",
             {"exposure_usd": 180000, "recovery_potential_usd": 117000,
              "eu_ai_act": "High", "error_rate": 0.50,
              "industry": "Healthcare"}, "INFO", 192),
        _evt("FLE",  "CORRECTION_RECEIVED",   "drug_formulary", "Humira Biosimilar",
             {"signal_type": "user_correction", "wrong_value": "90", "correct_value": "45",
              "confidence": 1.0, "industry": "Healthcare", "corrected": False}, "INFO", 150),
        _evt("ASGC", "APPROVAL_REQUIRED",     "drug_formulary", "Humira Biosimilar",
             {"action": "remove_formulary_record", "risk_level": "Medium",
              "requested_by": "DKSM", "industry": "Healthcare",
              "detail": "Expiry in 42 days — REMOVE queued for lead approval"}, "WARNING", 149,
             requires_approval=True),
        _evt("FLE",  "CORRECTION_APPLIED",    "drug_formulary", "Humira Biosimilar",
             {"actions": ["Gold flagged", "reprobe triggered", "fine_tune_pair generated"],
              "error_type": "THRESHOLD_ERROR", "industry": "Healthcare",
              "corrected": True}, "INFO", 145),
        _evt("ASGC", "APPROVAL_GRANTED",      "drug_formulary", "Humira Biosimilar",
             {"action": "remove_formulary_record", "approved_by": "Lead",
              "industry": "Healthcare"}, "INFO", 140),
    ]

    # --- Scenario 6: Retail — coupon expiry
    chain6 = [
        _evt("DKSM", "EXPIRY_ALERT",       "coupons", "SUMMER25",
             {"industry": "Retail", "expiry_date": "2026-08-31",
              "days_until_expiry": 104, "level": "EXPIRING", "recommended_action": "NONE",
              "detail": "Seasonal coupon — verify if extending past summer"}, "WARNING", 130),
        _evt("DKSM", "EXPIRY_ALERT",       "coupons", "SAVE10",
             {"industry": "Retail", "expiry_date": "2026-12-31",
              "days_until_expiry": 226, "level": "EXPIRING", "recommended_action": "REMOVE",
              "detail": "Discount reduced from 15%→10% — AI still believes old rate"}, "WARNING", 128),
        _evt("DKSM", "STALENESS_DETECTED", "coupons", "SAVE10",
             {"level": "STALE", "sim": 0.67, "days_since_update": 200,
              "belief": "15", "truth": "10", "industry": "Retail",
              "error_corrected": False}, "WARNING", 125),
        _evt("FLE",  "CORRECTION_RECEIVED","coupons", "SAVE10",
             {"signal_type": "agent_override", "wrong_value": "15", "correct_value": "10",
              "confidence": 1.0, "industry": "Retail", "corrected": False}, "INFO", 80),
        _evt("FLE",  "CORRECTION_APPLIED", "coupons", "SAVE10",
             {"actions": ["Gold flagged"], "error_type": "THRESHOLD_ERROR",
              "industry": "Retail", "corrected": True}, "INFO", 75),
    ]

    # --- Scenario 7: Insurance — coverage limit expiry + data contract
    chain7 = [
        _evt("DKSM", "DATA_CONTRACT_EXPIRY", "coverage_limits", "Medical_Emergency",
             {"industry": "Insurance", "expiry_date": "2026-12-31",
              "days_until_expiry": 226, "level": "EXPIRING",
              "detail": "ACA compliance contract renewal — coverage limit subject to change",
              "recommended_action": "UPDATE"}, "WARNING", 60),
        _evt("DKSM", "EXPIRY_ALERT",       "coverage_limits", "Business_Interruption",
             {"industry": "Insurance", "expiry_date": "2026-09-30",
              "days_until_expiry": 134, "level": "EXPIRING",
              "recommended_action": "UPDATE",
              "detail": "Policy contract renewal window opens in 45 days"}, "WARNING", 55),
        _evt("DKSM", "STALENESS_DETECTED", "coverage_limits", "Medical_Emergency",
             {"level": "CRITICAL", "sim": 0.50, "days_since_update": 139,
              "belief": "250000", "truth": "500000", "industry": "Insurance",
              "error_corrected": False}, "CRITICAL", 50),
        _evt("FLE",  "CORRECTION_RECEIVED","coverage_limits", "Medical_Emergency",
             {"signal_type": "user_correction", "wrong_value": "250000",
              "correct_value": "500000", "confidence": 1.0,
              "industry": "Insurance", "corrected": False}, "INFO", 35),
        _evt("FLE",  "CORRECTION_APPLIED", "coverage_limits", "Medical_Emergency",
             {"actions": ["Gold flagged", "reprobe triggered"],
              "error_type": "THRESHOLD_ERROR", "industry": "Insurance",
              "corrected": True}, "INFO", 20),
    ]

    all_events = sorted(chain1 + chain2 + chain3 + recent + chain5 + chain6 + chain7,
                        key=lambda e: e["timestamp"])

    with open(_BUS_FILE, "w") as f:
        for evt in all_events:
            f.write(json.dumps(evt) + "\n")

    print(f"Seeded event bus with {len(all_events)} events → {_BUS_FILE}")


def run_all() -> None:
    """Generate all missing simulation files. Safe to call repeatedly."""
    _DATA.mkdir(parents=True, exist_ok=True)
    generate_pipeline_log()
    generate_business_outcomes()
    generate_feedback_log()
    generate_support_files()
    print("Data simulation complete.")


if __name__ == "__main__":
    run_all()
