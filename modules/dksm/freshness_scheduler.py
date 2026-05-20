"""
Freshness Scheduler

Automatically runs CRAG staleness probes for company-profile domains on their
configured schedule (hourly / daily / weekly / manual).

State is persisted to data/probe_cache/scheduler_state.json so last-run times
survive process restarts.  Results are appended to
data/freshness_schedule_log.csv for dashboard consumption.

Usage (background thread):
    scheduler = FreshnessScheduler()
    scheduler.start()          # non-blocking — runs in a daemon thread
    ...
    scheduler.stop()

Usage (one-shot / on-demand):
    scheduler = FreshnessScheduler()
    scheduler.run_domain_now("retail_corp__coupons")

Usage (check what is due without running):
    due = scheduler.domains_due_now()
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from modules.dksm.company_profile import CompanyProfileLoader
except ImportError:
    from src.company_profile import CompanyProfileLoader  # type: ignore

logger = logging.getLogger(__name__)

_STATE_FILE = Path("data/probe_cache/scheduler_state.json")
_LOG_FILE   = Path("data/freshness_schedule_log.csv")
_LOG_COLS   = [
    "run_id", "company_id", "domain_key", "entity", "staleness_level",
    "semantic_similarity", "model_belief", "warehouse_truth",
    "staleness_reason", "scheduled_at", "completed_at", "triggered_by",
]

_SCHEDULE_SECONDS = {
    "hourly":  3600,
    "daily":   86400,
    "weekly":  604800,
    "manual":  None,
}


class FreshnessScheduler:
    """
    Background scheduler that probes company-profile domains on their
    configured cadence and writes results to the schedule log.
    """

    def __init__(
        self,
        profiles_dir: str | Path = "config/company_profiles",
        tick_seconds: int = 60,
    ) -> None:
        self.loader       = CompanyProfileLoader(profiles_dir)
        self.tick_seconds = tick_seconds
        self._state: dict[str, str] = {}   # domain_key → ISO last-run timestamp
        self._thread: threading.Thread | None = None
        self._stop_event  = threading.Event()
        self._last_weekly_ts: float = 0.0
        self._ensure_dirs()
        self._load_state()
        # Subscribe to reprobe requests from FLE after corrections
        try:
            from core.event_bus import get_bus
            get_bus().subscribe("REPROBE_REQUESTED", self._on_reprobe_requested)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not _LOG_FILE.exists():
            with open(_LOG_FILE, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_LOG_COLS).writeheader()

    def _load_state(self) -> None:
        if _STATE_FILE.exists():
            try:
                with open(_STATE_FILE) as f:
                    self._state = json.load(f)
            except Exception:
                self._state = {}

    def _save_state(self) -> None:
        with open(_STATE_FILE, "w") as f:
            json.dump(self._state, f, indent=2)

    # ------------------------------------------------------------------
    # Schedule logic
    # ------------------------------------------------------------------

    def _seconds_since_last_run(self, domain_key: str) -> float | None:
        ts = self._state.get(domain_key)
        if not ts:
            return None
        try:
            last = datetime.fromisoformat(ts)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last).total_seconds()
        except Exception:
            return None

    def is_due(self, domain_key: str) -> bool:
        schedule = self.loader.schedule_for(domain_key)
        interval = _SCHEDULE_SECONDS.get(schedule)
        if interval is None:
            return False                   # manual — never auto-trigger
        elapsed = self._seconds_since_last_run(domain_key)
        if elapsed is None:
            return True                    # never run → immediately due
        return elapsed >= interval

    def domains_due_now(self) -> list[str]:
        """Return all company domain keys that are currently due for a probe."""
        return [k for k in self.loader.all_custom_domains() if self.is_due(k)]

    def next_run_in(self, domain_key: str) -> str:
        """Human-readable time until next scheduled run (e.g. 'in 47 min')."""
        schedule = self.loader.schedule_for(domain_key)
        interval = _SCHEDULE_SECONDS.get(schedule)
        if interval is None:
            return "manual only"
        elapsed = self._seconds_since_last_run(domain_key)
        if elapsed is None:
            return "overdue — never run"
        remaining = max(0, interval - elapsed)
        if remaining < 60:
            return "due now"
        if remaining < 3600:
            return f"in {int(remaining // 60)} min"
        return f"in {int(remaining // 3600)}h {int((remaining % 3600) // 60)}m"

    def last_run_at(self, domain_key: str) -> str:
        return self._state.get(domain_key, "never")

    # ------------------------------------------------------------------
    # Probe execution
    # ------------------------------------------------------------------

    def run_domain_now(self, domain_key: str, triggered_by: str = "scheduler") -> list[dict]:
        """
        Run the full Gold-layer staleness check for a company domain.
        Returns a list of result dicts (one per Gold entity).
        Does NOT require the Anthropic API — uses the scorer directly
        against the Gold CSV model_belief column, same as the dashboard.
        """
        if "__" not in domain_key:
            logger.warning("run_domain_now: %s is not a namespaced company domain", domain_key)
            return []

        cid, local_key = domain_key.split("__", 1)
        domain_cfg = self.loader.domains_for(cid).get(local_key)
        if not domain_cfg:
            logger.error("Domain not found: %s", domain_key)
            return []

        gold_path = domain_cfg.get("medallion", {}).get("gold", {}).get("path", "")
        if not gold_path or not Path(gold_path).exists():
            logger.warning("No Gold layer CSV for domain %s at %s", domain_key, gold_path)
            return []

        import pandas as pd
        from src.scorer import StalenessScorer

        key_col = domain_cfg["medallion"]["gold"]["key_column"]
        val_col = domain_cfg["medallion"]["gold"]["value_column"]
        thresholds = domain_cfg.get("staleness_thresholds", {"fresh": 0.90, "stale": 0.70, "critical": 0.50})

        try:
            df = pd.read_csv(gold_path)
        except Exception as exc:
            logger.error("Could not read Gold CSV %s: %s", gold_path, exc)
            return []

        current_rows = df[df.get("is_current", pd.Series([True]*len(df))) == True]  # noqa: E712

        scorer = StalenessScorer.__new__(StalenessScorer)
        scorer.config = {"domains": {domain_key: domain_cfg}, "global_settings": {}}

        results = []
        scheduled_at = datetime.now(timezone.utc).isoformat()
        run_id = f"{domain_key}_{scheduled_at[:19].replace(':', '-')}"

        for _, row in current_rows.iterrows():
            row_d = row.to_dict()
            entity   = str(row_d.get(key_col, ""))
            truth    = str(row_d.get(val_col, ""))
            ed       = str(row_d.get("effective_date", ""))
            raw_belief = row_d.get("model_belief", "")
            belief = (
                str(raw_belief)
                if raw_belief not in ("", None) and str(raw_belief) not in ("nan", "")
                else truth
            )
            staleness_reason = str(row_d.get("staleness_reason", ""))
            if staleness_reason in ("nan", ""):
                staleness_reason = ""

            score = scorer.score(domain_key, entity, belief, truth, ed)

            result = {
                "run_id":             run_id,
                "company_id":         cid,
                "domain_key":         domain_key,
                "entity":             entity,
                "staleness_level":    score.staleness_level,
                "semantic_similarity": score.semantic_similarity,
                "model_belief":       belief,
                "warehouse_truth":    truth,
                "staleness_reason":   staleness_reason,
                "scheduled_at":       scheduled_at,
                "completed_at":       datetime.now(timezone.utc).isoformat(),
                "triggered_by":       triggered_by,
            }
            results.append(result)

        self._append_log(results)
        self._state[domain_key] = datetime.now(timezone.utc).isoformat()
        self._save_state()
        logger.info("Probe complete: %s — %d entities checked", domain_key, len(results))
        return results

    def _append_log(self, rows: list[dict]) -> None:
        with open(_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_COLS)
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in _LOG_COLS})

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    _EXPIRY_SCAN_INTERVAL = 86400  # 24 hours in seconds

    def _on_reprobe_requested(self, event) -> None:
        """Handle REPROBE_REQUESTED from FLE — re-run domain probe after a correction."""
        domain = getattr(event, "domain", None)
        entity = getattr(event, "entity", None)
        if not domain:
            return
        logger.info("Scheduler: reprobe requested domain=%s entity=%s", domain, entity)
        threading.Thread(
            target=self.run_domain_now,
            args=(domain,),
            kwargs={"triggered_by": "reprobe_after_correction"},
            daemon=True,
        ).start()

    def _run_weekly_improvement(self) -> None:
        """Compare this week vs last week corrections — emit LEARNING_IMPROVEMENT event."""
        import pandas as pd
        fb_path = Path(__file__).parent.parent.parent / "data" / "feedback_log.csv"
        if not fb_path.exists():
            return
        try:
            from core.event_bus import ARIAEvent, get_bus
            df  = pd.read_csv(fb_path)
            now = datetime.now(timezone.utc)

            def _filter_week(df, days_start, days_end):
                ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
                return df[(ts >= now - timedelta(days=days_end)) &
                          (ts <  now - timedelta(days=days_start))]

            this_week = _filter_week(df, 0, 7)
            last_week = _filter_week(df, 7, 14)

            status_col = "fle_status" if "fle_status" in df.columns else "status"
            corrections_this = int((this_week.get(status_col, pd.Series(dtype=str)) == "applied").sum())
            corrections_last = int((last_week.get(status_col, pd.Series(dtype=str)) == "applied").sum())

            repeat_rate = 0.0
            if not this_week.empty and "domain" in this_week.columns and "entity" in this_week.columns:
                repeats    = this_week.groupby(["domain", "entity"]).size()
                repeat_rate = round(float((repeats > 1).sum() / max(len(repeats), 1)), 3)

            get_bus().emit(ARIAEvent(
                source_module="FLE",
                event_type="LEARNING_IMPROVEMENT",
                domain="all",
                payload={
                    "corrections_this_week": corrections_this,
                    "corrections_last_week": corrections_last,
                    "delta":                 corrections_this - corrections_last,
                    "repeat_error_rate":     repeat_rate,
                    "week_ending":           now.strftime("%Y-W%V"),
                },
                severity="INFO",
            ))
            logger.info("Weekly improvement: corrections this=%d last=%d repeat_rate=%.1f%%",
                        corrections_this, corrections_last, repeat_rate * 100)
        except Exception as exc:
            logger.warning("Weekly improvement calc failed: %s", exc)

    def _tick(self) -> None:
        _last_expiry_scan = 0.0
        while not self._stop_event.is_set():
            try:
                import time
                now = time.time()
                # Run domain freshness probes
                for domain_key in self.domains_due_now():
                    logger.info("Scheduler: running due probe for %s", domain_key)
                    self.run_domain_now(domain_key, triggered_by="scheduler")
                # Run expiry scan every 24h
                if now - _last_expiry_scan >= self._EXPIRY_SCAN_INTERVAL:
                    logger.info("Scheduler: running 24h expiry scan")
                    try:
                        self.run_expiry_scan()
                    except Exception as exc:
                        logger.warning("Expiry scan error: %s", exc)
                    _last_expiry_scan = now
                # Run weekly improvement loop every 7 days
                if now - self._last_weekly_ts >= 604800:
                    self._last_weekly_ts = now
                    try:
                        self._run_weekly_improvement()
                    except Exception as exc:
                        logger.warning("Weekly improvement error: %s", exc)
            except Exception as exc:
                logger.error("Scheduler tick error: %s", exc)
            self._stop_event.wait(self.tick_seconds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._tick, daemon=True, name="dksm-freshness-scheduler")
        self._thread.start()
        logger.info("Freshness scheduler started (tick every %ds)", self.tick_seconds)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Freshness scheduler stopped")

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    # Dashboard helpers
    # ------------------------------------------------------------------

    def status_table(self) -> list[dict]:
        """Return a list of status rows for all company domains (for dashboard display)."""
        rows = []
        for domain_key, (cid, cfg) in self.loader.all_custom_domains().items():
            meta = self.loader.company_meta(cid)
            rows.append({
                "company":        meta.get("display_name", cid),
                "domain":         cfg.get("display_name", domain_key),
                "domain_key":     domain_key,
                "schedule":       cfg.get("probe_schedule", "manual"),
                "last_run":       self.last_run_at(domain_key),
                "next_run":       self.next_run_in(domain_key),
                "due_now":        self.is_due(domain_key),
            })
        return rows

    def recent_results(self, domain_key: str | None = None, n: int = 50) -> list[dict]:
        """Load the most recent n rows from the schedule log, optionally filtered."""
        if not _LOG_FILE.exists():
            return []
        try:
            import pandas as pd
            df = pd.read_csv(_LOG_FILE)
            if domain_key:
                df = df[df["domain_key"] == domain_key]
            df = df.tail(n)
            return df.to_dict("records")
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Stage 2: 24h expiry scan + stale action workflow
    # ------------------------------------------------------------------

    def run_expiry_scan(self) -> list[dict]:
        """
        Scan all Gold CSVs for expiry_date + recommended_action.
        Emits EXPIRY_ALERT events and queues REMOVE/UPDATE items for ASGC approval.
        Called by the 24h tick OR on-demand from the dashboard.
        """
        from core.notifier import scan_expiry_alerts
        from core.event_bus import ARIAEvent, get_bus

        alerts = scan_expiry_alerts()

        # Route REMOVE/UPDATE actions to ASGC approval queue
        for alert in alerts:
            action = alert.get("recommended_action", "NONE")
            if action in ("REMOVE", "UPDATE"):
                try:
                    from modules.asgc.governance_console import GovernanceConsole
                    gc = GovernanceConsole()
                    gc.queue_for_approval(
                        source_module="DKSM",
                        event_type="EXPIRY_ACTION_REQUIRED",
                        domain=alert["domain"],
                        entity=alert["entity"],
                        proposed_action=f"{action}_EXPIRED_RECORD",
                        risk_level="Medium" if action == "REMOVE" else "Low",
                        payload={
                            "expiry_date":        alert["expiry_date"],
                            "days_until_expiry":  alert["days_until_expiry"],
                            "industry":           alert["industry"],
                            "recommended_action": action,
                        },
                    )
                except Exception as exc:
                    logger.warning("ASGC queue failed for %s: %s", alert["entity"], exc)

        # Flush notifications
        try:
            from core.notifier import get_notifier
            get_notifier().flush()
        except Exception:
            pass

        logger.info("Expiry scan complete: %d alerts, %d routed to ASGC",
                    len(alerts),
                    sum(1 for a in alerts if a.get("recommended_action") in ("REMOVE", "UPDATE")))
        return alerts

    def get_stale_action_items(self) -> list[dict]:
        """
        Return Gold layer records where recommended_action != NONE and item is expired/stale.
        Used by the dashboard Stale Action Queue on DKSM page.
        """
        import csv as _csv
        from datetime import date
        from pathlib import Path

        gold_dir = Path(__file__).parent.parent.parent / "data" / "dksm" / "gold_layer"
        today    = date.today()
        items    = []

        for csv_path in sorted(gold_dir.glob("*.csv")):
            try:
                rows = list(_csv.DictReader(open(csv_path)))
            except Exception:
                continue
            for row in rows:
                if str(row.get("is_current", "true")).lower() != "true":
                    continue
                action = row.get("recommended_action", "NONE")
                if action == "NONE":
                    continue
                # Key column
                key_col = next((c for c in ("drug_name", "coupon_code", "coverage_type",
                                            "route_code", "segment_name", "rate_plan",
                                            "tier_name", "policy_type") if c in row), None)
                entity = row.get(key_col, csv_path.stem) if key_col else csv_path.stem

                # Days until expiry
                days_left = None
                exp_str = row.get("expiry_date", "")
                if exp_str:
                    try:
                        days_left = (date.fromisoformat(str(exp_str).strip()) - today).days
                    except ValueError:
                        pass

                # Staleness reason
                stale_reason = row.get("staleness_reason", "")
                model_belief = row.get("model_belief", "")

                items.append({
                    "domain":             csv_path.stem,
                    "entity":             entity,
                    "industry":           row.get("industry", "General"),
                    "recommended_action": action,
                    "expiry_date":        exp_str or "—",
                    "days_until_expiry":  days_left,
                    "model_belief":       model_belief,
                    "staleness_reason":   stale_reason[:80] if stale_reason else "—",
                })

        return sorted(items, key=lambda x: (
            x.get("days_until_expiry") is None,
            x.get("days_until_expiry", 9999)
        ))
