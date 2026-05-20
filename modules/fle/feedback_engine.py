"""
ARIA — FLE: Feedback Loop Engine
Captures user and agent correction signals, classifies them by error type,
and propagates corrections upstream — updating Gold flags, generating
fine-tuning pairs, or reweighting RAG indices.

Event subscriptions:
  CONTEXT_INJECTED  → verify_injection_helped()
  CORRECTION_RECEIVED → classify_and_route()
"""
from __future__ import annotations

import csv
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.event_bus import ARIAEvent, get_bus
from core.config_loader import get_config

logger = logging.getLogger(__name__)

_ROOT        = Path(__file__).parent.parent.parent
_FEEDBACK    = _ROOT / "data" / "feedback_log.csv"
_FINE_TUNE   = _ROOT / "data" / "fine_tune_pairs.jsonl"
_FEEDBACK_COLS = [
    "signal_id", "timestamp", "signal_type", "domain", "entity",
    "wrong_value", "correct_value", "confidence", "fle_status", "propagated",
]

_SIGNAL_TYPES = {"user_correction", "agent_override", "escalation", "non_use"}


@dataclass
class FeedbackSignal:
    signal_id: str
    timestamp: datetime
    signal_type: str
    domain: str
    entity: str
    wrong_value: str
    correct_value: str
    source: str
    confidence: float
    fle_status: str = "pending"


@dataclass
class RoutingDecision:
    signal_id: str
    error_type: str            # THRESHOLD_ERROR | DEFINITION_ERROR | RETRIEVAL_ERROR | CLASSIFICATION_ERROR
    routing_action: str
    auto_executable: bool
    requires_approval: bool
    approved_by: str | None = None
    fine_tune_pair: dict | None = None
    chroma_reweight: dict | None = None


@dataclass
class ApplicationResult:
    signal_id: str
    applied: bool
    actions_taken: list[str]
    message: str


class FeedbackLoopEngine:
    """Self-improving feedback loop: captures signals → classifies → routes → applies."""

    def __init__(self) -> None:
        self._cfg = get_config()
        self._bus = get_bus()
        self._bus.subscribe("CONTEXT_INJECTED",    self._verify_injection)
        self._bus.subscribe("CORRECTION_RECEIVED", self._on_correction)
        self._ensure_files()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_files(self) -> None:
        _FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
        if not _FEEDBACK.exists():
            with open(_FEEDBACK, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_FEEDBACK_COLS).writeheader()
        if not _FINE_TUNE.exists():
            _FINE_TUNE.touch()

    # ------------------------------------------------------------------
    # Subscription handlers
    # ------------------------------------------------------------------

    def _verify_injection(self, event: ARIAEvent) -> None:
        logger.debug("FLE: injection verified for %s/%s", event.domain, event.entity)

    def _on_correction(self, event: ARIAEvent) -> None:
        self.classify_and_route(
            signal_id=event.payload.get("signal_id", ""),
            domain=event.domain,
            entity=event.entity or "",
            wrong_value=event.payload.get("wrong_value", ""),
            correct_value=event.payload.get("correct_value", ""),
        )

    # ------------------------------------------------------------------
    # Core: capture_signal
    # ------------------------------------------------------------------

    def capture_signal(
        self,
        signal_type: str,
        domain: str,
        entity: str,
        wrong_value: str,
        correct_value: str,
        source: str,
        confidence: float = 1.0,
    ) -> FeedbackSignal:
        """Record a correction signal and emit CORRECTION_RECEIVED if confident enough."""
        if signal_type not in _SIGNAL_TYPES:
            signal_type = "user_correction"
        min_conf = self._cfg.module("fle").get("min_signal_confidence", 0.7)

        signal = FeedbackSignal(
            signal_id=f"SIG-{str(uuid.uuid4())[:8]}",
            timestamp=datetime.now(timezone.utc),
            signal_type=signal_type,
            domain=domain,
            entity=entity,
            wrong_value=wrong_value,
            correct_value=correct_value,
            source=source,
            confidence=confidence,
            fle_status="classified" if confidence >= min_conf else "pending",
        )

        self._append_signal(signal)

        if confidence >= min_conf:
            self._bus.emit(ARIAEvent(
                source_module="FLE",
                event_type="CORRECTION_RECEIVED",
                domain=domain,
                entity=entity,
                payload={
                    "signal_id":     signal.signal_id,
                    "wrong_value":   wrong_value,
                    "correct_value": correct_value,
                    "signal_type":   signal_type,
                },
                severity="INFO",
            ))

        return signal

    # ------------------------------------------------------------------
    # Core: classify_and_route
    # ------------------------------------------------------------------

    def classify_and_route(
        self,
        signal_id: str,
        domain: str,
        entity: str,
        wrong_value: str,
        correct_value: str,
    ) -> RoutingDecision:
        """Classify the correction type and determine routing action."""
        error_type     = self._classify_error(wrong_value, correct_value)
        auto_propagate = self._cfg.module("fle").get("auto_propagate", False)
        fine_tune_pair = None
        chroma_reweight = None

        if error_type == "THRESHOLD_ERROR":
            routing_action = "flag_gold_and_reprobe"
            auto_executable = False
            requires_approval = not auto_propagate
        elif error_type == "DEFINITION_ERROR":
            routing_action = "flag_domain_definition"
            auto_executable = False
            requires_approval = True
            if self._repeat_error_count(domain, entity) >= self._cfg.module("fle").get("retrain_threshold", 10):
                fine_tune_pair = self._build_fine_tune_pair(entity, wrong_value, correct_value)
        elif error_type == "RETRIEVAL_ERROR":
            routing_action = "reweight_chroma"
            auto_executable = True
            requires_approval = False
            chroma_reweight = {"entity": entity, "domain": domain, "boost": 1.5}
        else:  # CLASSIFICATION_ERROR
            routing_action = "update_gold_and_reprobe"
            auto_executable = False
            requires_approval = not auto_propagate

        decision = RoutingDecision(
            signal_id=signal_id,
            error_type=error_type,
            routing_action=routing_action,
            auto_executable=auto_executable,
            requires_approval=requires_approval,
            fine_tune_pair=fine_tune_pair,
            chroma_reweight=chroma_reweight,
        )

        if requires_approval:
            self._bus.emit(ARIAEvent(
                source_module="FLE",
                event_type="APPROVAL_REQUIRED",
                domain=domain,
                entity=entity,
                payload={
                    "signal_id":      signal_id,
                    "error_type":     error_type,
                    "routing_action": routing_action,
                    "risk_level":     "Medium",
                },
                severity="WARNING",
            ))

        return decision

    # ------------------------------------------------------------------
    # Core: apply_routing
    # ------------------------------------------------------------------

    def apply_routing(self, decision: RoutingDecision,
                      domain: str, entity: str,
                      correct_value: str,
                      approved_by: str = "") -> ApplicationResult:
        """Execute the routing action and log the result."""
        if decision.requires_approval and not approved_by:
            return ApplicationResult(
                signal_id=decision.signal_id,
                applied=False,
                actions_taken=[],
                message="Approval required before applying.",
            )

        actions: list[str] = []

        # Flag Gold layer entity
        if "gold" in decision.routing_action or "reprobe" in decision.routing_action:
            self._flag_gold_entity(domain, entity, correct_value)
            actions.append(f"Gold layer flagged: {entity} → {correct_value}")

        # Write fine-tune pair
        if decision.fine_tune_pair:
            self._write_fine_tune_pair(decision.fine_tune_pair)
            actions.append("Fine-tune pair written to fine_tune_pairs.jsonl")

        # Execute ChromaDB reweighting — boost corrected entity in retrieval
        if decision.chroma_reweight:
            try:
                from modules.dksm.vector_store import MedallionVectorStore
                vs = MedallionVectorStore()
                vs.boost_entity(
                    decision.chroma_reweight["domain"],
                    decision.chroma_reweight["entity"],
                    decision.chroma_reweight.get("boost", 1.5),
                )
                actions.append(
                    f"ChromaDB boosted: {decision.chroma_reweight['entity']} "
                    f"(boost={decision.chroma_reweight.get('boost', 1.5)})"
                )
            except Exception as exc:
                logger.warning("FLE: chroma reweight failed: %s", exc)

        # Update feedback log status
        self._update_signal_status(decision.signal_id, "applied", propagated=True)

        self._bus.emit(ARIAEvent(
            source_module="FLE",
            event_type="CORRECTION_APPLIED",
            domain=domain,
            entity=entity,
            payload={
                "signal_id":   decision.signal_id,
                "error_type":  decision.error_type,
                "actions":     actions,
                "approved_by": approved_by,
            },
            severity="INFO",
        ))

        # Trigger reprobe so DKSM re-scores the domain after correction
        if "reprobe" in decision.routing_action or "gold" in decision.routing_action:
            self._bus.emit(ARIAEvent(
                source_module="FLE",
                event_type="REPROBE_REQUESTED",
                domain=domain,
                entity=entity,
                payload={"triggered_by": "correction_applied", "signal_id": decision.signal_id},
                severity="INFO",
            ))

        return ApplicationResult(
            signal_id=decision.signal_id,
            applied=True,
            actions_taken=actions,
            message=f"Applied: {', '.join(actions)}",
        )

    # ------------------------------------------------------------------
    # Summary + velocity
    # ------------------------------------------------------------------

    def get_feedback_summary(self) -> dict[str, Any]:
        df = self._load_feedback()
        if df.empty:
            return {"total_signals": 0}
        by_type   = df["signal_type"].value_counts().to_dict()
        by_domain = df["domain"].value_counts().to_dict()
        applied   = int((df["fle_status"] == "applied").sum())
        pending   = int((df["fle_status"] == "pending").sum())
        total     = len(df)
        closure   = round(applied / total, 3) if total else 0.0
        ft_count  = sum(1 for _ in open(_FINE_TUNE)) if _FINE_TUNE.exists() else 0
        return {
            "total_signals":               total,
            "by_type":                     by_type,
            "by_domain":                   by_domain,
            "applied_count":               applied,
            "pending_approval_count":      pending,
            "fine_tune_pairs_generated":   ft_count,
            "correction_loop_closure_rate": closure,
        }

    def get_learning_velocity(self, days_back: int = 30) -> dict[str, Any]:
        """Measure how fast the system is improving."""
        df = self._load_feedback()
        if df.empty:
            return {"learning_velocity": 0.0}

        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        recent = df[df["timestamp"] >= cutoff]

        weekly_counts: list[int] = []
        for week in range(4):
            w_start = cutoff + timedelta(weeks=week)
            w_end   = w_start + timedelta(weeks=1)
            weekly_counts.append(int(((recent["timestamp"] >= w_start) &
                                      (recent["timestamp"] < w_end)).sum()))

        # Velocity: fraction of recent signals that are applied
        applied_recent = int((recent["fle_status"] == "applied").sum())
        velocity = round(applied_recent / len(recent), 3) if len(recent) else 0.0

        # Repeat error reduction — same (domain, entity) seen fewer times over time
        domain_entity_counts = recent.groupby(["domain", "entity"]).size()
        repeat_reduction = float(1.0 - min(domain_entity_counts.max() / max(len(recent), 1), 1.0))

        if velocity < 0.3:
            self._bus.emit(ARIAEvent(
                source_module="FLE",
                event_type="APPROVAL_REQUIRED",
                domain="all",
                payload={"alert": "learning_velocity_low", "velocity": velocity},
                severity="WARNING",
            ))

        return {
            "learning_velocity":          velocity,
            "corrections_per_week":       weekly_counts,
            "repeat_error_reduction_rate": round(repeat_reduction, 3),
            "signals_last_30d":           len(recent),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _classify_error(self, wrong: str, correct: str) -> str:
        try:
            wn, cn = float(wrong), float(correct)
            if cn != 0 and abs(wn - cn) / abs(cn) > 0.20:
                return "THRESHOLD_ERROR"
        except (ValueError, TypeError):
            pass
        # Simple token overlap for semantic check
        tw = set(str(wrong).lower().split())
        tc = set(str(correct).lower().split())
        if tw and tc:
            overlap = len(tw & tc) / len(tw | tc)
            if overlap < 0.6:
                return "DEFINITION_ERROR"
            if overlap >= 0.6:
                return "RETRIEVAL_ERROR"
        return "CLASSIFICATION_ERROR"

    def _repeat_error_count(self, domain: str, entity: str) -> int:
        df = self._load_feedback()
        if df.empty:
            return 0
        return int(((df["domain"] == domain) & (df["entity"] == entity)).sum())

    def _build_fine_tune_pair(self, entity: str, wrong: str, correct: str) -> dict:
        return {
            "instruction": f"What is the current value for {entity}?",
            "response":    f"The current value for {entity} is {correct}. "
                           f"Note: {wrong} is outdated.",
        }

    def _write_fine_tune_pair(self, pair: dict) -> None:
        with open(_FINE_TUNE, "a") as f:
            f.write(json.dumps(pair) + "\n")

    def _flag_gold_entity(self, domain: str, entity: str, correct_value: str) -> None:
        """Add fle_flagged column to Gold CSV."""
        try:
            domains_cfg = get_config().dksm_domains
            if domain not in domains_cfg:
                return
            gold_cfg  = domains_cfg[domain]["medallion"]["gold"]
            gold_path = _ROOT / "data" / "dksm" / "gold_layer" / Path(gold_cfg["path"]).name
            if not gold_path.exists():
                gold_path = Path(gold_cfg["path"])
            df = pd.read_csv(gold_path)
            key_col = gold_cfg["key_column"]
            if "fle_flagged" not in df.columns:
                df["fle_flagged"] = False
            df.loc[df[key_col] == entity, "fle_flagged"]      = True
            df.loc[df[key_col] == entity, "fle_correct_value"] = correct_value
            df.to_csv(gold_path, index=False)
        except Exception as exc:
            logger.warning("FLE: could not flag Gold entity %s/%s: %s", domain, entity, exc)

    def _load_feedback(self) -> pd.DataFrame:
        if not _FEEDBACK.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(_FEEDBACK)
        except Exception:
            return pd.DataFrame()

    def _append_signal(self, signal: FeedbackSignal) -> None:
        with open(_FEEDBACK, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FEEDBACK_COLS).writerow({
                "signal_id":    signal.signal_id,
                "timestamp":    signal.timestamp.isoformat(),
                "signal_type":  signal.signal_type,
                "domain":       signal.domain,
                "entity":       signal.entity,
                "wrong_value":  signal.wrong_value,
                "correct_value": signal.correct_value,
                "confidence":   signal.confidence,
                "fle_status":   signal.fle_status,
                "propagated":   False,
            })

    def _update_signal_status(self, signal_id: str, status: str,
                              propagated: bool = False) -> None:
        df = self._load_feedback()
        if df.empty:
            return
        df.loc[df["signal_id"] == signal_id, "fle_status"]  = status
        df.loc[df["signal_id"] == signal_id, "propagated"]  = propagated
        df.to_csv(_FEEDBACK, index=False)
