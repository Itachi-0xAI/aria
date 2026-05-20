"""
ARIA — LCI: Live Context Injector
Intercepts agent calls for stale domains and prepends verified Gold layer
values as grounding context — no retraining required.

Event subscriptions:
  STALENESS_DETECTED → trigger_injection_readiness()
"""
from __future__ import annotations

import csv
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from core.event_bus import ARIAEvent, get_bus
from core.config_loader import get_config

logger = logging.getLogger(__name__)

_LCI_LOG   = Path(__file__).parent.parent.parent / "data" / "lci_log.csv"
_LCI_COLS  = [
    "injection_id", "timestamp", "domain", "entity",
    "injected_value", "query_preview", "source_version",
    "expires_at", "triggered_by",
]


@dataclass
class InjectionResult:
    injected: bool
    entity: str | None = None
    injected_value: str | None = None
    injection_id: str | None = None
    context_block: str = ""
    source_version: str = ""
    expires_at: datetime | None = None


class LiveContextInjector:
    """
    Pre-fetches current Gold layer values when DKSM detects staleness,
    then injects them as verified context before any agent LLM call.
    """

    def __init__(self) -> None:
        self._cfg   = get_config()
        self._bus   = get_bus()
        self._pending: dict[str, dict] = {}   # entity → injection metadata
        self._ttl_hours: int = self._cfg.module("lci").get("max_injection_age_hours", 4)
        self._ensure_log()
        self._bus.subscribe("STALENESS_DETECTED", self.trigger_injection_readiness)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_log(self) -> None:
        _LCI_LOG.parent.mkdir(parents=True, exist_ok=True)
        if not _LCI_LOG.exists():
            with open(_LCI_LOG, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_LCI_COLS).writeheader()

    # ------------------------------------------------------------------
    # Subscription handler
    # ------------------------------------------------------------------

    def trigger_injection_readiness(self, event: ARIAEvent) -> None:
        """Called automatically when DKSM emits STALENESS_DETECTED."""
        domain = event.domain
        entity = event.entity or event.payload.get("entity", "")
        if not entity:
            return

        gold_value, version = self._fetch_gold_value(domain, entity)
        if gold_value is None:
            return

        expires = datetime.now(timezone.utc) + timedelta(hours=self._ttl_hours)
        self._pending[entity] = {
            "domain":        domain,
            "entity":        entity,
            "value":         gold_value,
            "version":       version,
            "ready_at":      datetime.now(timezone.utc).isoformat(),
            "expires":       expires,
        }
        logger.info("LCI: injection ready for '%s' in domain '%s'", entity, domain)

        self._bus.emit(ARIAEvent(
            source_module="LCI",
            event_type="CONTEXT_INJECTED",
            domain=domain,
            entity=entity,
            payload={"status": "ready", "value": gold_value, "version": version},
            severity="INFO",
        ))

    # ------------------------------------------------------------------
    # Core: inject
    # ------------------------------------------------------------------

    def inject(self, query: str, domain: str) -> InjectionResult:
        """
        Called by any agent before LLM inference.
        Returns an InjectionResult whose context_block is prepended to the prompt.
        """
        matched = self._find_pending(domain)
        if not matched:
            return InjectionResult(injected=False)

        entry = matched
        injection_id = str(uuid.uuid4())[:8]
        now           = datetime.now(timezone.utc)
        expires_at    = entry["expires"]

        context_block = (
            f"[ARIA VERIFIED CONTEXT — {now.strftime('%Y-%m-%d %H:%M UTC')}]\n"
            f"Source: Gold layer {entry['version']} (enterprise data warehouse)\n"
            f"Entity: {entry['entity']} (domain: {domain})\n"
            f"Current value: {entry['value']}\n"
            f"Note: This value supersedes any prior model knowledge. "
            f"Valid until {expires_at.strftime('%H:%M UTC')}.\n"
        )

        self._append_log({
            "injection_id":  injection_id,
            "timestamp":     now.isoformat(),
            "domain":        domain,
            "entity":        entry["entity"],
            "injected_value": entry["value"],
            "query_preview": query[:120],
            "source_version": entry["version"],
            "expires_at":    expires_at.isoformat(),
            "triggered_by":  "agent_call",
        })

        self._bus.emit(ARIAEvent(
            source_module="LCI",
            event_type="CONTEXT_INJECTED",
            domain=domain,
            entity=entry["entity"],
            payload={
                "injection_id":   injection_id,
                "injected_value": entry["value"],
                "query_preview":  query[:80],
            },
            severity="INFO",
        ))

        return InjectionResult(
            injected=True,
            entity=entry["entity"],
            injected_value=entry["value"],
            injection_id=injection_id,
            context_block=context_block,
            source_version=entry["version"],
            expires_at=expires_at,
        )

    def inject_and_prompt(self, query: str, domain: str, entity: str = "") -> dict:
        """
        Full LCI middleware: inject context then call Claude with the grounded prompt.
        In demo mode or without ANTHROPIC_API_KEY, returns a simulated response.
        """
        result = self.inject(query, domain)
        final_prompt = (result.context_block + "\n\n" + query) if result.injected else query

        demo_mode = getattr(self._cfg, "demo_mode", True)
        api_key   = os.getenv("ANTHROPIC_API_KEY", "")

        if demo_mode or not api_key:
            preview = result.context_block[:120].replace("\n", " ") if result.injected else ""
            response_text = (
                f"[DEMO MODE] Query received with {'verified context injected' if result.injected else 'no injection (context fresh)'}.\n"
                f"Context preview: {preview}"
            )
            cost = 0.0
        else:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            model  = self._cfg.module("dksm").get("model", "claude-haiku-4-5-20251001")
            msg    = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": final_prompt}],
            )
            response_text = msg.content[0].text
            cost = round(
                msg.usage.input_tokens  * 0.00000025 +
                msg.usage.output_tokens * 0.00000125, 6
            )

        return {
            "response":       response_text,
            "context_used":   result.injected,
            "injection_id":   result.injection_id,
            "entity":         result.entity,
            "injected_value": result.injected_value,
            "cost_usd":       cost,
        }

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_injection_history(self, domain: str | None = None,
                              hours_back: int = 24) -> list[dict]:
        if not _LCI_LOG.exists():
            return []
        df = pd.read_csv(_LCI_LOG)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df[df["timestamp"] >= cutoff]
        if domain:
            df = df[df["domain"] == domain]
        return df.to_dict("records")

    def get_injection_stats(self) -> dict[str, Any]:
        history = self.get_injection_history(hours_back=24)
        active  = sum(1 for e in self._pending.values()
                      if e["expires"] > datetime.now(timezone.utc))
        by_domain: dict[str, int] = {}
        for row in history:
            by_domain[row["domain"]] = by_domain.get(row["domain"], 0) + 1
        return {
            "total_injections_24h": len(history),
            "active_injections":    active,
            "by_domain":            by_domain,
            "pending_entities":     list(self._pending.keys()),
        }

    def active_injections(self) -> list[dict]:
        """Return unexpired pending injections (for dashboard table)."""
        now = datetime.now(timezone.utc)
        result = []
        for v in self._pending.values():
            if v["expires"] > now:
                result.append({
                    "domain":          v.get("domain", ""),
                    "entity":          v.get("entity", ""),
                    "value":           v.get("value", ""),
                    "version":         v.get("version", ""),
                    "ready_at":        v.get("ready_at", ""),
                    "expires_in_min":  int((v["expires"] - now).total_seconds() // 60),
                    # expires is intentionally excluded — raw datetime causes pandas dtype error
                })
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_pending(self, domain: str) -> dict | None:
        now = datetime.now(timezone.utc)
        for entry in list(self._pending.values()):
            if entry["domain"] == domain and entry["expires"] > now:
                return entry
            if entry["expires"] <= now:
                self._pending.pop(entry["entity"], None)
        return None

    def _fetch_gold_value(self, domain: str,
                          entity: str) -> tuple[str | None, str]:
        """Read current Gold layer value for an entity."""
        try:
            domains_cfg = self._cfg.dksm_domains
            if domain not in domains_cfg:
                return None, ""
            gold_cfg  = domains_cfg[domain]["medallion"]["gold"]
            _root     = Path(__file__).parent.parent.parent
            # Try symlinked dksm data first, then original relative path
            gold_path = _root / "data" / "dksm" / "gold_layer" / Path(gold_cfg["path"]).name
            if not gold_path.exists():
                gold_path = _root / gold_cfg["path"]
            if not gold_path.exists():
                gold_path = Path(gold_cfg["path"])
            df = pd.read_csv(gold_path)
            current = df[df.get("is_current", pd.Series([True] * len(df))) == True]  # noqa: E712
            row = current[current[gold_cfg["key_column"]] == entity]
            if row.empty:
                return None, ""
            val     = str(row.iloc[0][gold_cfg["value_column"]])
            version = str(row.iloc[0].get("version", "current"))
            return val, f"v{version}"
        except Exception as exc:
            logger.warning("LCI: could not fetch Gold value for %s/%s: %s",
                           domain, entity, exc)
            return None, ""

    def _append_log(self, row: dict) -> None:
        with open(_LCI_LOG, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_LCI_COLS).writerow(
                {k: row.get(k, "") for k in _LCI_COLS}
            )
