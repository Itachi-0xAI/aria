"""
ARIA — ASGC: AI Stack Governance Console
The lead's command layer. Receives all approval requests, provides override
controls, generates board-level reports, and exposes the cross-module causal
chain view.

Event subscriptions:
  APPROVAL_REQUIRED → queue_for_approval()
  VALUE_CALCULATED  → update_value_dashboard()
  All CRITICAL      → escalate_to_lead()
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.event_bus import ARIAEvent, get_bus
from core.config_loader import get_config

logger = logging.getLogger(__name__)

_ROOT    = Path(__file__).parent.parent.parent
_QUEUE   = _ROOT / "data" / "approval_queue.csv"
_QUEUE_COLS = [
    "request_id", "timestamp", "source_module", "event_type",
    "domain", "entity", "proposed_action", "risk_level",
    "payload_summary", "status", "decided_by", "decided_at",
]


@dataclass
class ApprovalRequest:
    request_id: str
    timestamp: datetime
    source_module: str
    event_type: str
    domain: str
    entity: str
    proposed_action: str
    risk_level: str
    payload_summary: str
    status: str = "pending"        # pending | approved | rejected
    decided_by: str = ""
    decided_at: str = ""


@dataclass
class CausalChain:
    domain: str
    entity: str
    events: list[ARIAEvent]
    narrative: str
    total_exposure_usd: float
    total_recovered_usd: float
    open_approvals: int


class GovernanceConsole:
    """Central governance layer — owns the approval queue and board reporting."""

    def __init__(self) -> None:
        self._cfg = get_config()
        self._bus = get_bus()
        self._bus.subscribe("APPROVAL_REQUIRED", self.queue_for_approval)
        self._bus.subscribe("VALUE_CALCULATED",  self._on_value_calculated)
        self._ensure_queue()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_queue(self) -> None:
        _QUEUE.parent.mkdir(parents=True, exist_ok=True)
        if not _QUEUE.exists():
            with open(_QUEUE, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_QUEUE_COLS).writeheader()

    # ------------------------------------------------------------------
    # Subscription handlers
    # ------------------------------------------------------------------

    def queue_for_approval(self, event: ARIAEvent) -> ApprovalRequest:
        """Receive any APPROVAL_REQUIRED event and add to the queue."""
        req = ApprovalRequest(
            request_id=f"REQ-{str(uuid.uuid4())[:8]}",
            timestamp=datetime.now(timezone.utc),
            source_module=event.source_module,
            event_type=event.event_type,
            domain=event.domain,
            entity=event.entity or "",
            proposed_action=event.payload.get("action",
                            event.payload.get("routing_action", "review")),
            risk_level=event.payload.get("risk_level", "Medium"),
            payload_summary=str(event.payload)[:200],
        )
        self._append_queue(req)
        logger.info("ASGC: queued approval %s from %s", req.request_id, req.source_module)
        return req

    def _on_value_calculated(self, event: ARIAEvent) -> None:
        logger.info("ASGC: value update — domain=%s exposure=$%s",
                    event.domain, event.payload.get("exposure_usd", 0))

    # ------------------------------------------------------------------
    # Approve / Reject
    # ------------------------------------------------------------------

    def approve(self, request_id: str, lead_name: str) -> None:
        """Approve a pending request. Emits APPROVAL_GRANTED to the requesting module."""
        self._update_queue(request_id, "approved", lead_name)
        req = self._get_request(request_id)
        if req:
            self._bus.emit(ARIAEvent(
                source_module="ASGC",
                event_type="APPROVAL_GRANTED",
                domain=req.domain,
                entity=req.entity,
                payload={"request_id": request_id, "approved_by": lead_name},
                severity="INFO",
            ))
        logger.info("ASGC: approved %s by %s", request_id, lead_name)

    def reject(self, request_id: str, lead_name: str, reason: str = "") -> None:
        """Reject a pending request. Emits APPROVAL_REJECTED."""
        self._update_queue(request_id, "rejected", lead_name)
        req = self._get_request(request_id)
        if req:
            self._bus.emit(ARIAEvent(
                source_module="ASGC",
                event_type="APPROVAL_REJECTED",
                domain=req.domain,
                entity=req.entity,
                payload={"request_id": request_id, "rejected_by": lead_name,
                         "reason": reason},
                severity="INFO",
            ))
        logger.info("ASGC: rejected %s by %s — %s", request_id, lead_name, reason)

    # ------------------------------------------------------------------
    # Approval queue queries
    # ------------------------------------------------------------------

    def get_pending_approvals(self) -> list[ApprovalRequest]:
        df = self._load_queue()
        if df.empty:
            return []
        pending = df[df["status"] == "pending"]
        return [self._row_to_request(row) for _, row in pending.iterrows()]

    def pending_count(self) -> int:
        return len(self.get_pending_approvals())

    # ------------------------------------------------------------------
    # Causal chain
    # ------------------------------------------------------------------

    def get_causal_chain(self, domain: str, entity: str,
                         hours_back: int = 24) -> CausalChain:
        """Reconstruct the full event story for a domain/entity."""
        events = self._bus.get_chain(domain, entity, hours_back)
        narrative = self._build_narrative(events, domain, entity)

        # Exposure and recovery from AVL events
        exposure  = sum(e.payload.get("exposure_usd", 0) for e in events
                        if e.event_type == "VALUE_CALCULATED")
        recovered = sum(e.payload.get("total_recovery_usd", 0) for e in events
                        if e.event_type == "CORRECTION_APPLIED")
        open_reqs = self.pending_count()

        return CausalChain(
            domain=domain,
            entity=entity,
            events=events,
            narrative=narrative,
            total_exposure_usd=float(exposure),
            total_recovered_usd=float(recovered),
            open_approvals=open_reqs,
        )

    def _build_narrative(self, events: list[ARIAEvent],
                         domain: str, entity: str) -> str:
        if not events:
            return f"No events recorded for {domain}/{entity} in the last 24h."
        lines = [f"Causal chain for **{entity}** (domain: {domain}):\n"]
        type_labels = {
            "STALENESS_DETECTED":    "🔴 DKSM detected staleness",
            "CONTEXT_INJECTED":      "💉 LCI injected verified context",
            "PIPELINE_FAILURE_FOUND": "⚙️ PP found pipeline root cause",
            "VALUE_CALCULATED":      "💰 AVL calculated financial exposure",
            "CORRECTION_RECEIVED":   "📥 FLE received correction signal",
            "CORRECTION_APPLIED":    "✅ FLE applied correction",
            "APPROVAL_REQUIRED":     "⏳ ASGC approval requested",
            "APPROVAL_GRANTED":      "✅ ASGC approved action",
            "APPROVAL_REJECTED":     "❌ ASGC rejected action",
        }
        for e in sorted(events, key=lambda x: x.timestamp):
            ts    = e.timestamp.strftime("%H:%M UTC")
            label = type_labels.get(e.event_type, e.event_type)
            detail = ""
            if e.event_type == "STALENESS_DETECTED":
                detail = f" (level: {e.payload.get('level', '?')})"
            elif e.event_type == "CONTEXT_INJECTED":
                detail = f" → value: {e.payload.get('injected_value', e.payload.get('value', '?'))}"
            elif e.event_type == "VALUE_CALCULATED":
                detail = f" → exposure: ${e.payload.get('exposure_usd', 0):,.0f}"
            elif e.event_type == "PIPELINE_FAILURE_FOUND":
                detail = f" ({e.payload.get('failure_type', '?')}, {e.payload.get('days_since', 0)}d ago)"
            lines.append(f"  {ts} — {label}{detail}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Stack health
    # ------------------------------------------------------------------

    def get_stack_health(self) -> dict[str, Any]:
        """Health of all 6 modules — used by aria.py Page 1 command center."""
        bus_stats  = self._bus.stats()
        recent_24h = self._bus.recent(hours_back=24)

        def _count(module: str, event_type: str | None = None) -> int:
            return sum(1 for e in recent_24h
                       if (module is None or e.source_module == module)
                       and (event_type is None or e.event_type == event_type))

        criticals = [e for e in recent_24h if e.severity == "CRITICAL"]
        pending   = self.pending_count()

        return {
            "DKSM": {
                "status":         "CRITICAL" if any(e.event_type == "STALENESS_DETECTED"
                                                    and e.severity == "CRITICAL"
                                                    for e in recent_24h) else "OK",
                "critical_count": _count("DKSM", "STALENESS_DETECTED"),
                "last_event":     max((e.timestamp for e in recent_24h
                                       if e.source_module == "DKSM"),
                                      default=None),
            },
            "LCI": {
                "status":             "OK",
                "active_injections":  _count("LCI", "CONTEXT_INJECTED"),
                "injection_rate_24h": _count("LCI"),
            },
            "PP": {
                "status":               "WARNING" if _count("PP", "PIPELINE_FAILURE_FOUND") > 0 else "OK",
                "failures_found":       _count("PP", "PIPELINE_FAILURE_FOUND"),
                "pending_remediations": sum(1 for r in self.get_pending_approvals()
                                            if r.source_module == "PP"),
            },
            "AVL": {
                "status":                   "OK",
                "value_events_24h":         _count("AVL", "VALUE_CALCULATED"),
            },
            "FLE": {
                "status":          "OK",
                "signals_24h":     _count("FLE", "CORRECTION_RECEIVED"),
                "applied_24h":     _count("FLE", "CORRECTION_APPLIED"),
            },
            "ASGC": {
                "status":            "WARNING" if not self._cfg.asgc_lead() else "OK",
                "pending_approvals": pending,
                "lead_name":         self._cfg.asgc_lead() or "Not configured",
            },
        }

    # ------------------------------------------------------------------
    # Board report PDF
    # ------------------------------------------------------------------

    def generate_board_report(self) -> bytes:
        """Generate a board-level PDF combining stack health + AVL summary."""
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib import colors
            from reportlab.platypus import (
                SimpleDocTemplate, Paragraph, Spacer, Table,
                TableStyle, HRFlowable, KeepTogether,
            )
            from reportlab.lib.units import mm

            buf   = io.BytesIO()
            doc   = SimpleDocTemplate(buf, pagesize=A4,
                                      leftMargin=22*mm, rightMargin=22*mm,
                                      topMargin=22*mm, bottomMargin=22*mm)
            SS    = getSampleStyleSheet()
            NAVY  = colors.HexColor("#0f3460")
            RED   = colors.HexColor("#e94560")
            GREEN = colors.HexColor("#22c55e")
            AMBER = colors.HexColor("#f59e0b")

            lead   = self._cfg.asgc_lead() or "Lead"
            health = self.get_stack_health()
            ts     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            def h1(txt: str) -> Paragraph:
                return Paragraph(txt, ParagraphStyle("H1", parent=SS["Normal"],
                    fontSize=20, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=4))

            def h2(txt: str) -> Paragraph:
                return Paragraph(txt, ParagraphStyle("H2", parent=SS["Normal"],
                    fontSize=13, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=5))

            def body(txt: str) -> Paragraph:
                return Paragraph(txt, ParagraphStyle("body", parent=SS["Normal"],
                    fontSize=10, leading=15, spaceAfter=5))

            story = [
                h1("ARIA — Board Intelligence Report"),
                body(f"Prepared by: {lead} &nbsp;|&nbsp; {ts}"),
                HRFlowable(width="100%", thickness=1, color=RED, spaceAfter=10),
                h2("AI Stack Health"),
            ]

            # Stack health table
            sh_rows = [["Module", "Status", "Key Metric"]]
            metrics = {
                "DKSM": f"{health['DKSM']['critical_count']} critical detections (24h)",
                "LCI":  f"{health['LCI']['active_injections']} injections (24h)",
                "PP":   f"{health['PP']['failures_found']} pipeline failures",
                "AVL":  f"{health['AVL']['value_events_24h']} value events (24h)",
                "FLE":  f"{health['FLE']['signals_24h']} signals (24h)",
                "ASGC": f"{health['ASGC']['pending_approvals']} pending approvals",
            }
            for mod, info in health.items():
                status = info.get("status", "OK")
                sh_rows.append([mod, status, metrics.get(mod, "")])

            sh_tbl = Table(sh_rows, colWidths=[30*mm, 30*mm, 100*mm])
            sh_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.HexColor("#f7f9ff"), colors.white]),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dde4f0")),
                ("TOPPADDING",    (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING",   (0, 0), (-1, -1), 7),
            ]))
            story += [sh_tbl, Spacer(1, 10)]

            # Pending approvals
            story.append(h2(f"Pending Approvals ({health['ASGC']['pending_approvals']})"))
            pending = self.get_pending_approvals()
            if pending:
                pa_rows = [["Module", "Action", "Domain", "Risk", "Requested"]]
                for r in pending[:10]:
                    pa_rows.append([
                        r.source_module, r.proposed_action[:30],
                        r.domain, r.risk_level,
                        r.timestamp.strftime("%m-%d %H:%M"),
                    ])
                pa_tbl = Table(pa_rows, colWidths=[20*mm, 55*mm, 40*mm, 20*mm, 25*mm])
                pa_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e94560")),
                    ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                    ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.HexColor("#fff5f7"), colors.white]),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dde4f0")),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("TOPPADDING",    (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 5),
                ]))
                story += [pa_tbl, Spacer(1, 8)]
            else:
                story.append(body("✅ No pending approvals."))

            story.append(body(
                "This report is generated by ARIA and reflects the current state of the "
                "AI governance stack. All exposure figures are estimates based on "
                "simulated decision log data."
            ))

            doc.build(story)
            return buf.getvalue()
        except Exception as exc:
            logger.error("ASGC: board report generation failed: %s", exc)
            return b""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_queue(self) -> pd.DataFrame:
        if not _QUEUE.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(_QUEUE)
        except Exception:
            return pd.DataFrame()

    def _append_queue(self, req: ApprovalRequest) -> None:
        with open(_QUEUE, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_QUEUE_COLS).writerow({
                "request_id":     req.request_id,
                "timestamp":      req.timestamp.isoformat(),
                "source_module":  req.source_module,
                "event_type":     req.event_type,
                "domain":         req.domain,
                "entity":         req.entity,
                "proposed_action": req.proposed_action,
                "risk_level":     req.risk_level,
                "payload_summary": req.payload_summary,
                "status":         req.status,
                "decided_by":     "",
                "decided_at":     "",
            })

    def _update_queue(self, request_id: str, status: str, decided_by: str) -> None:
        df = self._load_queue()
        if df.empty:
            return
        mask = df["request_id"] == request_id
        df.loc[mask, "status"]     = status
        df.loc[mask, "decided_by"] = decided_by
        df.loc[mask, "decided_at"] = datetime.now(timezone.utc).isoformat()
        df.to_csv(_QUEUE, index=False)

    def _get_request(self, request_id: str) -> ApprovalRequest | None:
        df = self._load_queue()
        if df.empty:
            return None
        rows = df[df["request_id"] == request_id]
        if rows.empty:
            return None
        return self._row_to_request(rows.iloc[0])

    def _row_to_request(self, row: Any) -> ApprovalRequest:
        return ApprovalRequest(
            request_id=row["request_id"],
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            source_module=row["source_module"],
            event_type=row["event_type"],
            domain=row["domain"],
            entity=str(row.get("entity", "")),
            proposed_action=str(row.get("proposed_action", "")),
            risk_level=str(row.get("risk_level", "Medium")),
            payload_summary=str(row.get("payload_summary", "")),
            status=str(row.get("status", "pending")),
            decided_by=str(row.get("decided_by", "")),
            decided_at=str(row.get("decided_at", "")),
        )
