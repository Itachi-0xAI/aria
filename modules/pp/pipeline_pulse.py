"""
ARIA — PP: Pipeline Pulse
Traces upstream through pipeline_log + pipeline_map to find the exact dbt model
run that caused staleness. Classifies the failure and recommends remediation.

Event subscriptions:
  STALENESS_DETECTED → trace_root_cause()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from core.event_bus import ARIAEvent, get_bus
from core.config_loader import get_config

logger = logging.getLogger(__name__)

_PIPELINE_LOG  = Path(__file__).parent.parent.parent / "data" / "pipeline_log.csv"
_DBT_RESULTS   = Path(__file__).parent.parent.parent / "target" / "run_results.json"
_DBT_MANIFEST  = Path(__file__).parent.parent.parent / "target" / "manifest.json"
_LOG_REQUIRED  = {"run_timestamp", "status", "rows_affected", "schema_version", "dbt_model"}


@dataclass
class RemediationOption:
    action: str            # refresh | full_refresh | schema_patch | alert_only
    description: str
    estimated_minutes: int
    risk_level: str        # Low | Medium | High
    auto_executable: bool


@dataclass
class RootCauseReport:
    domain: str
    entity: str
    dbt_model: str
    failure_type: str           # silent_drop | schema_drift | gap | hard_failure | unknown
    failure_timestamp: datetime | None
    days_since_failure: int
    rows_affected_drop_pct: float | None
    downstream_domains: list[str]
    owner_email: str
    remediation_options: list[RemediationOption]
    requires_approval: bool


@dataclass
class RemediationResult:
    option: RemediationOption
    status: str                 # executed | pending_approval | failed
    message: str
    executed_at: datetime | None = None
    approved_by: str | None = None


class PipelinePulse:
    """
    Root-cause analyser for pipeline failures that cause AI knowledge staleness.
    """

    def __init__(self) -> None:
        self._cfg  = get_config()
        self._bus  = get_bus()
        self._bus.subscribe("STALENESS_DETECTED", self._on_staleness)

    # ------------------------------------------------------------------
    # Subscription handler
    # ------------------------------------------------------------------

    def _on_staleness(self, event: ARIAEvent) -> None:
        report = self.trace_root_cause(
            domain=event.domain,
            entity=event.entity or "",
            staleness_days=event.payload.get("days_since_update", 30),
        )
        if report and report.failure_type != "unknown":
            self._bus.emit(ARIAEvent(
                source_module="PP",
                event_type="PIPELINE_FAILURE_FOUND",
                domain=report.domain,
                entity=report.entity,
                payload={
                    "dbt_model":       report.dbt_model,
                    "failure_type":    report.failure_type,
                    "days_since":      report.days_since_failure,
                    "requires_approval": report.requires_approval,
                },
                severity="WARNING" if report.days_since_failure < 30 else "CRITICAL",
            ))

    # ------------------------------------------------------------------
    # Core: trace_root_cause
    # ------------------------------------------------------------------

    def trace_root_cause(self, domain: str, entity: str,
                         staleness_days: int = 30) -> RootCauseReport | None:
        """
        Trace the pipeline failure that caused staleness for a domain/entity.
        Returns None if the domain is not mapped.
        """
        mapping = self._cfg.domain_for_model(self._model_for_domain(domain))
        if not mapping:
            return None

        dbt_model   = mapping["dbt_model"]
        owner_email = mapping.get("owner", "unknown@company.com")

        df = self._load_log(dbt_model)
        if df.empty:
            return self._unknown_report(domain, entity, dbt_model, owner_email)

        failure_type, failure_ts, drop_pct = self._detect_failure(df, staleness_days)
        days_since   = (datetime.now(timezone.utc) - failure_ts).days if failure_ts else 0
        downstream   = self._downstream_domains(domain)
        options      = self._remediation_options(failure_type)
        req_approval = any(o.risk_level == "High" for o in options)

        return RootCauseReport(
            domain=domain,
            entity=entity,
            dbt_model=dbt_model,
            failure_type=failure_type,
            failure_timestamp=failure_ts,
            days_since_failure=days_since,
            rows_affected_drop_pct=drop_pct,
            downstream_domains=downstream,
            owner_email=owner_email,
            remediation_options=options,
            requires_approval=req_approval,
        )

    # ------------------------------------------------------------------
    # Failure detection
    # ------------------------------------------------------------------

    def _detect_failure(self, df: pd.DataFrame,
                        lookback_days: int) -> tuple[str, datetime | None, float | None]:
        """Return (failure_type, timestamp, drop_pct). Checks in priority order."""
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
        recent = df[pd.to_datetime(df["run_timestamp"], utc=True).apply(
            lambda x: x.timestamp()) >= cutoff].copy()
        if recent.empty:
            return "gap", None, None

        # 1. Hard failure
        failed = recent[recent["status"] == "failed"]
        if not failed.empty:
            ts = pd.to_datetime(failed.iloc[-1]["run_timestamp"], utc=True)
            return "hard_failure", ts.to_pydatetime(), None

        # 2. Silent row-count drop (>30% fall from rolling median) — checked before schema drift
        recent["rows"] = pd.to_numeric(recent["rows_affected"], errors="coerce")
        median_rows    = recent["rows"].median()
        if median_rows and median_rows > 0:
            drops = recent[recent["rows"] < median_rows * 0.70].sort_values("run_timestamp")
            if not drops.empty:
                ts      = pd.to_datetime(drops.iloc[0]["run_timestamp"], utc=True)
                drop_pct = float((median_rows - drops.iloc[0]["rows"]) / median_rows)
                return "silent_drop", ts.to_pydatetime(), round(drop_pct, 3)

        # 3. Schema drift
        if recent["schema_version"].nunique() > 1:
            drift_row = recent[recent["schema_version"] != recent["schema_version"].iloc[0]]
            if not drift_row.empty:
                ts = pd.to_datetime(drift_row.iloc[0]["run_timestamp"], utc=True)
                return "schema_drift", ts.to_pydatetime(), None

        # 4. Gap — no runs for >24h within the window
        times = pd.to_datetime(recent["run_timestamp"], utc=True).sort_values()
        for i in range(1, len(times)):
            gap_hours = (times.iloc[i] - times.iloc[i - 1]).total_seconds() / 3600
            if gap_hours > 24:
                return "gap", times.iloc[i - 1].to_pydatetime(), None

        return "unknown", None, None

    # ------------------------------------------------------------------
    # Remediation options
    # ------------------------------------------------------------------

    def _remediation_options(self, failure_type: str) -> list[RemediationOption]:
        base: dict[str, list[RemediationOption]] = {
            "silent_drop": [
                RemediationOption("refresh",      "Re-run the dbt model (incremental)", 8,  "Low",    True),
                RemediationOption("full_refresh", "Full rebuild of the model",          45, "Medium", False),
                RemediationOption("alert_only",   "Notify owner, no auto-action",       1,  "Low",    True),
            ],
            "schema_drift": [
                RemediationOption("schema_patch", "Apply schema migration and re-run",  30, "High",   False),
                RemediationOption("alert_only",   "Notify owner for manual review",      1,  "Low",    True),
            ],
            "gap": [
                RemediationOption("refresh",      "Re-run missed incremental loads",     15, "Low",    True),
                RemediationOption("alert_only",   "Notify owner of pipeline gap",         1,  "Low",    True),
            ],
            "hard_failure": [
                RemediationOption("full_refresh", "Full rebuild after source fix",       60, "High",   False),
                RemediationOption("alert_only",   "Escalate to data engineering team",    1,  "Low",    True),
            ],
            "unknown": [
                RemediationOption("alert_only",   "No pattern detected — alert only",    1,  "Low",    True),
            ],
        }
        return base.get(failure_type, base["unknown"])

    # ------------------------------------------------------------------
    # execute_remediation
    # ------------------------------------------------------------------

    def execute_remediation(self, option: RemediationOption,
                            domain: str, approved_by: str = "") -> RemediationResult:
        """Execute or queue a remediation action."""
        if option.risk_level == "High" and not approved_by:
            self._bus.emit(ARIAEvent(
                source_module="PP",
                event_type="APPROVAL_REQUIRED",
                domain=domain,
                payload={
                    "action":      option.action,
                    "description": option.description,
                    "risk_level":  option.risk_level,
                },
                severity="WARNING",
            ))
            return RemediationResult(
                option=option,
                status="pending_approval",
                message="High-risk action queued for ASGC approval.",
            )

        # Simulate execution — append a new "success" run to pipeline_log
        self._simulate_run(domain, option.action)
        logger.info("PP: remediation executed — %s / %s", domain, option.action)

        return RemediationResult(
            option=option,
            status="executed",
            message=f"Remediation '{option.action}' executed successfully.",
            executed_at=datetime.now(timezone.utc),
            approved_by=approved_by or "auto",
        )

    def _simulate_run(self, domain: str, action: str) -> None:
        model = self._model_for_domain(domain)
        new_row = {
            "run_id":          "SIM-" + str(hash(action + domain))[:6],
            "dbt_model":       model,
            "domain":          domain,
            "run_timestamp":   datetime.now(timezone.utc).isoformat(),
            "status":          "success",
            "duration_seconds": 90,
            "rows_affected":   10000,
            "error_message":   "",
            "schema_version":  "v-remediated",
            "source_table":    f"{domain}_raw",
        }
        df = pd.DataFrame([new_row])
        df.to_csv(_PIPELINE_LOG, mode="a", header=False, index=False)

    # ------------------------------------------------------------------
    # Scan + health
    # ------------------------------------------------------------------

    def scan_all_domains(self) -> list[RootCauseReport]:
        """Run trace_root_cause for every mapped domain."""
        reports = []
        for mapping in self._cfg.pipeline_mappings:
            domain = mapping["domain"]
            entity = mapping.get("key_entity", "")
            report = self.trace_root_cause(domain, entity, staleness_days=180)
            if report:
                reports.append(report)
        return reports

    def get_pipeline_health_summary(self) -> dict[str, Any]:
        """Aggregate health across all domains — used by ASGC and aria.py."""
        reports  = self.scan_all_domains()
        failures = [r for r in reports if r.failure_type not in ("unknown",)]
        return {
            "last_scan":           datetime.now(timezone.utc).isoformat(),
            "total_domains":       len(reports),
            "failures_found":      len(failures),
            "pending_remediations": sum(1 for r in failures if r.requires_approval),
            "by_domain": {
                r.domain: {
                    "failure_type":     r.failure_type,
                    "days_since":       r.days_since_failure,
                    "dbt_model":        r.dbt_model,
                    "requires_approval": r.requires_approval,
                }
                for r in reports
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_log(self, dbt_model: str) -> pd.DataFrame:
        """Load pipeline log — merges dbt run_results.json (if present) with CSV fallback."""
        import json as _json
        frames: list[pd.DataFrame] = []

        # Source 1: real dbt Core run_results.json artifact
        if _DBT_RESULTS.exists():
            try:
                with open(_DBT_RESULTS) as f:
                    results = _json.load(f)
                generated_at = results.get("metadata", {}).get(
                    "generated_at", datetime.now(timezone.utc).isoformat()
                )
                rows = []
                for r in results.get("results", []):
                    uid = r.get("unique_id", "")
                    if not uid.endswith(dbt_model):
                        continue
                    rows.append({
                        "run_id":           uid[-8:],
                        "dbt_model":        dbt_model,
                        "domain":           dbt_model.replace("fct_", ""),
                        "run_timestamp":    generated_at,
                        "status":           r.get("status", "unknown"),
                        "duration_seconds": r.get("execution_time", 0),
                        "rows_affected":    r.get("adapter_response", {}).get("rows_affected", 0),
                        "error_message":    r.get("message", ""),
                        "schema_version":   "dbt-live",
                        "source_table":     dbt_model + "_raw",
                    })
                if rows:
                    frames.append(pd.DataFrame(rows))
                    logger.debug("PP: loaded %d rows from dbt run_results for %s", len(rows), dbt_model)
            except Exception as exc:
                logger.warning("PP: dbt artifacts parse failed: %s", exc)

        # Source 2: pipeline_log.csv (simulated or historical)
        if _PIPELINE_LOG.exists():
            try:
                df = pd.read_csv(_PIPELINE_LOG)
                if _LOG_REQUIRED.issubset(set(df.columns)):
                    frames.append(df[df["dbt_model"] == dbt_model].copy())
                else:
                    logger.warning("PP: pipeline_log.csv missing required columns: %s",
                                   _LOG_REQUIRED - set(df.columns))
            except Exception as exc:
                logger.warning("PP: pipeline log read failed: %s", exc)

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).drop_duplicates()

    def _model_for_domain(self, domain: str) -> str:
        for m in self._cfg.pipeline_mappings:
            if m["domain"] == domain:
                return m["dbt_model"]
        return f"fct_{domain}"

    def _downstream_domains(self, domain: str) -> list[str]:
        """Other domains sharing the same source table (simplified)."""
        all_domains = [m["domain"] for m in self._cfg.pipeline_mappings]
        return [d for d in all_domains if d != domain]

    def _unknown_report(self, domain: str, entity: str,
                        dbt_model: str, owner: str) -> RootCauseReport:
        return RootCauseReport(
            domain=domain, entity=entity, dbt_model=dbt_model,
            failure_type="unknown", failure_timestamp=None,
            days_since_failure=0, rows_affected_drop_pct=None,
            downstream_domains=[], owner_email=owner,
            remediation_options=self._remediation_options("unknown"),
            requires_approval=False,
        )
