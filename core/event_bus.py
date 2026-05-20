"""
ARIA Event Bus
Cross-module communication backbone. Every module emits events here.
No module imports another module directly — all coordination is through events.

Persistence: aria_event_bus.jsonl (append-only, never deleted).
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_BUS_FILE = Path(__file__).parent.parent / "data" / "aria_event_bus.jsonl"

# Valid source modules
MODULES = {"DKSM", "LCI", "PP", "AVL", "FLE", "ASGC"}

# Valid event types
EVENT_TYPES = {
    "STALENESS_DETECTED",
    "CONTEXT_INJECTED",
    "PIPELINE_FAILURE_FOUND",
    "VALUE_CALCULATED",
    "CORRECTION_RECEIVED",
    "CORRECTION_APPLIED",
    "APPROVAL_REQUIRED",
    "APPROVAL_GRANTED",
    "APPROVAL_REJECTED",
    "REPROBE_REQUESTED",
    "LEARNING_IMPROVEMENT",
    "EXPIRY_ALERT",
    "DATA_CONTRACT_EXPIRY",
}


@dataclass
class ARIAEvent:
    source_module: str
    event_type: str
    domain: str
    payload: dict
    severity: str = "INFO"           # INFO | WARNING | CRITICAL
    entity: str | None = None
    requires_approval: bool = False
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ARIAEvent":
        d = dict(d)
        d["timestamp"] = datetime.fromisoformat(d["timestamp"])
        return cls(**d)


class EventBus:
    """
    Append-only event bus. Emit events, subscribe handlers, query history.
    Thread-safe for single-process use.
    """

    def __init__(self, bus_file: Path = _BUS_FILE) -> None:
        self._bus_file = bus_file
        self._bus_file.parent.mkdir(parents=True, exist_ok=True)
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------

    def emit(self, event: ARIAEvent) -> None:
        """Persist event and route to all subscribers."""
        if event.severity == "CRITICAL":
            event.requires_approval = True

        with open(self._bus_file, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

        for handler in self._handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception as exc:
                logger.error("EventBus handler error [%s/%s]: %s",
                             event.event_type, handler.__name__, exc)

        if event.requires_approval:
            for handler in self._handlers.get("APPROVAL_REQUIRED", []):
                try:
                    handler(event)
                except Exception as exc:
                    logger.error("EventBus APPROVAL_REQUIRED handler error: %s", exc)

        logger.debug("Event emitted: %s / %s / %s", event.source_module,
                     event.event_type, event.domain)

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """Register a handler for an event type."""
        self._handlers[event_type].append(handler)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def _read_all(self) -> list[ARIAEvent]:
        if not self._bus_file.exists():
            return []
        events = []
        with open(self._bus_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(ARIAEvent.from_dict(json.loads(line)))
                except Exception:
                    continue
        return events

    def get_chain(self, domain: str, entity: str | None = None,
                  hours_back: int = 24) -> list[ARIAEvent]:
        """All events for a domain/entity in a time window, ordered by time."""
        cutoff = datetime.now(timezone.utc).timestamp() - hours_back * 3600
        return [
            e for e in self._read_all()
            if e.domain == domain
            and (entity is None or e.entity == entity)
            and e.timestamp.timestamp() >= cutoff
        ]

    def replay(self, start: datetime, end: datetime) -> list[ARIAEvent]:
        """All events between two datetimes (for audit/debug)."""
        return [
            e for e in self._read_all()
            if start <= e.timestamp <= end
        ]

    def recent(self, hours_back: int = 24,
               module: str | None = None,
               event_type: str | None = None) -> list[ARIAEvent]:
        """Convenience: recent events with optional filters."""
        cutoff = datetime.now(timezone.utc).timestamp() - hours_back * 3600
        return [
            e for e in self._read_all()
            if e.timestamp.timestamp() >= cutoff
            and (module is None or e.source_module == module)
            and (event_type is None or e.event_type == event_type)
        ]

    def stats(self) -> dict:
        """Summary counts — used by ASGC stack health."""
        all_events = self._read_all()
        by_type: dict[str, int] = defaultdict(int)
        by_module: dict[str, int] = defaultdict(int)
        for e in all_events:
            by_type[e.event_type] += 1
            by_module[e.source_module] += 1
        return {
            "total_events": len(all_events),
            "by_type": dict(by_type),
            "by_module": dict(by_module),
        }


# Singleton — all modules share one bus instance
_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
