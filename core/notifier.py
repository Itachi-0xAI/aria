"""
ARIA Notifier
Listens to the event bus and dispatches notifications for:
  - EXPIRY_ALERT       (drug/coupon/contract/rate expiry approaching)
  - DATA_CONTRACT_EXPIRY
  - STALENESS_DETECTED (CRITICAL only)
  - PIPELINE_FAILURE_FOUND

Channels: log file (always) + email (optional, requires SMTP config).
Digest mode: collects events and sends a single summary every N hours.
Industry-aware: each industry has its own recipient group.
"""
from __future__ import annotations

import logging
import os
import smtplib
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.event_bus import ARIAEvent

logger = logging.getLogger(__name__)

_NOTIFY_LOG = Path(__file__).parent.parent / "data" / "notification_log.csv"


# ── Industry label map ─────────────────────────────────────────────────────────

_DOMAIN_INDUSTRY: dict[str, str] = {
    "customer_segments":  "Financial_Services",
    "risk_thresholds":    "Financial_Services",
    "loan_notations":     "Financial_Services",
    "product_catalog":    "Retail",
    "coupons":            "Retail",
    "clearance":          "Retail",
    "drug_formulary":     "Healthcare",
    "billing_codes":      "Healthcare",
    "carrier_rates":      "Logistics",
    "sla_windows":        "Logistics",
    "premium_tables":     "Insurance",
    "coverage_limits":    "Insurance",
    "rate_plans":         "Hospitality",
    "loyalty_tiers":      "Hospitality",
}


def get_industry(domain: str, payload: dict | None = None) -> str:
    """Return the industry label for a domain."""
    if payload and payload.get("industry"):
        return payload["industry"]
    return _DOMAIN_INDUSTRY.get(domain, "General")


# ── Notifier ──────────────────────────────────────────────────────────────────

class ARIANotifier:
    """
    Subscribes to the event bus and dispatches notifications.
    Call start() once on startup.  Safe to call multiple times (idempotent).
    """

    def __init__(self) -> None:
        from core.config_loader import get_config
        self._cfg    = get_config()
        self._ncfg   = getattr(self._cfg, "_raw", {}).get("notifications", {})
        self._expiry  = getattr(self._cfg, "_raw", {}).get("expiry", {})
        self._pending: dict[str, list[dict]] = defaultdict(list)   # industry → events
        self._last_digest: datetime = datetime.now(timezone.utc)
        self._started = False
        _NOTIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
        if not _NOTIFY_LOG.exists():
            with open(_NOTIFY_LOG, "w") as f:
                f.write("timestamp,event_type,domain,entity,industry,severity,channel,recipients\n")

    def start(self) -> None:
        if self._started:
            return
        from core.event_bus import get_bus
        bus = get_bus()
        notify_on = self._ncfg.get("notify_on",
            ["EXPIRY_ALERT", "STALENESS_DETECTED", "PIPELINE_FAILURE_FOUND", "DATA_CONTRACT_EXPIRY"])
        for evt_type in notify_on:
            bus.subscribe(evt_type, self._handle)
        self._started = True
        logger.info("ARIA Notifier started — watching: %s", notify_on)

    def _handle(self, event: "ARIAEvent") -> None:
        """Called by event bus on matching events."""
        if not self._ncfg.get("enabled", True):
            return
        # Only notify on CRITICAL/WARNING for STALENESS to avoid noise
        if event.event_type == "STALENESS_DETECTED" and event.severity not in ("CRITICAL", "WARNING"):
            return

        industry = get_industry(event.domain, event.payload)
        entry = {
            "event_type": event.event_type,
            "domain":     event.domain,
            "entity":     event.entity,
            "industry":   industry,
            "severity":   event.severity,
            "timestamp":  event.timestamp.isoformat(),
            "payload":    event.payload,
        }

        digest_hours = self._ncfg.get("digest_interval_hours", 24)
        if digest_hours == 0:
            self._dispatch([entry], industry)
        else:
            self._pending[industry].append(entry)
            # Check if digest is due
            elapsed = (datetime.now(timezone.utc) - self._last_digest).total_seconds() / 3600
            if elapsed >= digest_hours:
                self._flush_digest()

    def flush(self) -> None:
        """Force-send all pending notifications (called by scheduler or on demand)."""
        self._flush_digest()

    def _flush_digest(self) -> None:
        if not self._pending:
            return
        for industry, events in self._pending.items():
            if events:
                self._dispatch(events, industry)
        self._pending.clear()
        self._last_digest = datetime.now(timezone.utc)

    def _dispatch(self, events: list[dict], industry: str) -> None:
        channel  = self._ncfg.get("channel", "log")
        groups   = self._ncfg.get("industry_groups", {})
        recips   = groups.get(industry, []) or groups.get("default", [])
        subject  = self._format_subject(events, industry)
        body     = self._format_body(events, industry)

        # Always log
        self._write_log(events, industry, recips)
        logger.info("ARIA Notification [%s] %s → %d recipient(s)", industry, subject,
                    len(recips))

        if channel in ("email", "both") and recips:
            try:
                self._send_email(subject, body, recips)
            except Exception as exc:
                logger.warning("ARIA Notification email failed: %s", exc)

    def _format_subject(self, events: list[dict], industry: str) -> str:
        criticals = sum(1 for e in events if e["severity"] == "CRITICAL")
        expiries  = sum(1 for e in events if e["event_type"] == "EXPIRY_ALERT")
        parts = []
        if criticals:
            parts.append(f"{criticals} CRITICAL")
        if expiries:
            parts.append(f"{expiries} expiring")
        tag = ", ".join(parts) or f"{len(events)} events"
        return f"[ARIA] {industry} — {tag}"

    def _format_body(self, events: list[dict], industry: str) -> str:
        lines = [
            f"ARIA Notification — {industry}",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "=" * 60,
        ]
        for e in events:
            sev   = e["severity"]
            etype = e["event_type"]
            ent   = e.get("entity", "")
            dom   = e.get("domain", "")
            p     = e.get("payload", {})

            if etype == "EXPIRY_ALERT":
                exp = p.get("expiry_date", "unknown")
                days = p.get("days_until_expiry", "?")
                action = p.get("recommended_action", "REVIEW")
                lines.append(f"\n⚠️  EXPIRY [{sev}] {dom} / {ent}")
                lines.append(f"   Expires: {exp}  ({days} days)")
                lines.append(f"   Recommended action: {action}")

            elif etype == "DATA_CONTRACT_EXPIRY":
                lines.append(f"\n📋 DATA CONTRACT EXPIRY [{sev}] {dom} / {ent}")
                lines.append(f"   {p.get('detail', '')}")

            elif etype == "STALENESS_DETECTED":
                belief = p.get("belief", "?")
                truth  = p.get("truth", "?")
                days   = p.get("days_since_update", "?")
                lines.append(f"\n🔴 STALE [{sev}] {dom} / {ent}")
                lines.append(f"   AI believes: {belief}  |  Warehouse: {truth}  |  {days} days stale")

            elif etype == "PIPELINE_FAILURE_FOUND":
                ft    = p.get("failure_type", "unknown")
                model = p.get("dbt_model", "unknown")
                lines.append(f"\n⚙️  PIPELINE [{sev}] {model}")
                lines.append(f"   Failure: {ft}  |  Domain: {dom}")

        lines += ["", "=" * 60,
                  "Manage in ARIA dashboard → http://localhost:8501",
                  "To update notification recipients: config/aria_config.yaml → notifications.industry_groups"]
        return "\n".join(lines)

    def _send_email(self, subject: str, body: str, recipients: list[str]) -> None:
        smtp_host = self._ncfg.get("smtp_host", "smtp.gmail.com")
        smtp_port = self._ncfg.get("smtp_port", 587)
        user      = os.getenv("ARIA_SMTP_USER", "")
        pwd       = os.getenv("ARIA_SMTP_PASSWORD", "")
        from_addr = self._ncfg.get("from_address", "aria@localhost")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body, "plain"))

        import ssl
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            if user and pwd:
                s.login(user, pwd)
            s.sendmail(from_addr, recipients, msg.as_string())

    def _write_log(self, events: list[dict], industry: str, recips: list[str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        recip_str = ";".join(recips) if recips else "log_only"
        with open(_NOTIFY_LOG, "a") as f:
            for e in events:
                f.write(f"{now},{e['event_type']},{e['domain']},{e['entity']},"
                        f"{industry},{e['severity']},log,{recip_str}\n")


# ── Expiry scanner ─────────────────────────────────────────────────────────────

def scan_expiry_alerts(gold_dir: Path | None = None) -> list[dict]:
    """
    Scan all Gold layer CSVs for records with expiry_date approaching or passed.
    Returns list of alert dicts and emits EXPIRY_ALERT events to the bus.
    Called by FreshnessScheduler every 24h (Stage 2) or on-demand.
    """
    import csv as _csv
    from datetime import date
    from core.config_loader import get_config
    from core.event_bus import ARIAEvent, get_bus

    cfg      = get_config()
    exp_cfg  = getattr(cfg, "_raw", {}).get("expiry", {})
    ind_warn = exp_cfg.get("industry_warning_days", {})
    default_warn = exp_cfg.get("warning_days_before_expiry", 30)
    critical_days = exp_cfg.get("critical_days_before_expiry", 7)

    if gold_dir is None:
        gold_dir = Path(__file__).parent.parent / "data" / "dksm" / "gold_layer"

    today  = date.today()
    alerts = []

    for csv_path in sorted(gold_dir.glob("*.csv")):
        try:
            rows = list(_csv.DictReader(open(csv_path)))
        except Exception:
            continue
        if not rows or "expiry_date" not in rows[0]:
            continue

        for row in rows:
            if str(row.get("is_current", "true")).lower() != "true":
                continue
            exp_str = row.get("expiry_date", "")
            if not exp_str or exp_str in ("", "nan"):
                continue
            try:
                exp_date = date.fromisoformat(str(exp_str).strip())
            except ValueError:
                continue

            industry = row.get("industry", get_industry(csv_path.stem))
            warn_days = ind_warn.get(industry, default_warn)
            days_left = (exp_date - today).days

            if days_left < 0:
                sev, level = "CRITICAL", "EXPIRED"
            elif days_left <= critical_days:
                sev, level = "CRITICAL", "EXPIRING_SOON"
            elif days_left <= warn_days:
                sev, level = "WARNING", "EXPIRING"
            else:
                continue  # not due yet

            # Key column detection
            key_col = next((c for c in ("drug_name", "coupon_code", "coverage_type",
                                        "route_code", "segment_name", "rate_plan",
                                        "tier_name", "policy_type") if c in row), None)
            entity = row.get(key_col, csv_path.stem) if key_col else csv_path.stem
            action = row.get("recommended_action", "REVIEW")
            domain = csv_path.stem

            alert = {
                "domain":            domain,
                "entity":            entity,
                "industry":          industry,
                "expiry_date":       str(exp_str),
                "days_until_expiry": days_left,
                "level":             level,
                "severity":          sev,
                "recommended_action": action,
            }
            alerts.append(alert)

            get_bus().emit(ARIAEvent(
                source_module="DKSM",
                event_type="EXPIRY_ALERT",
                domain=domain,
                entity=entity,
                payload=alert,
                severity=sev,
            ))

    logger.info("Expiry scan: %d alerts found", len(alerts))
    return alerts


# ── Singleton ──────────────────────────────────────────────────────────────────

_notifier: ARIANotifier | None = None

def get_notifier() -> ARIANotifier:
    global _notifier
    if _notifier is None:
        _notifier = ARIANotifier()
    return _notifier
