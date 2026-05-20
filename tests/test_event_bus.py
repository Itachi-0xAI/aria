"""Tests for core/event_bus.py"""
import sys; sys.path.insert(0, "..")
from core.event_bus import EventBus, ARIAEvent
from pathlib import Path
import tempfile, os

def _tmp_bus():
    t = tempfile.mktemp(suffix=".jsonl")
    return EventBus(Path(t))

def test_emit_and_read():
    bus = _tmp_bus()
    bus.emit(ARIAEvent(source_module="DKSM", event_type="STALENESS_DETECTED",
                       domain="customer_segments", entity="Enterprise",
                       payload={"level": "CRITICAL"}, severity="CRITICAL"))
    events = bus.recent(hours_back=1)
    assert len(events) == 1
    assert events[0].event_type == "STALENESS_DETECTED"
    assert events[0].requires_approval is True   # CRITICAL auto-sets

def test_subscribe_handler():
    bus = _tmp_bus()
    received = []
    bus.subscribe("CONTEXT_INJECTED", lambda e: received.append(e))
    bus.emit(ARIAEvent(source_module="LCI", event_type="CONTEXT_INJECTED",
                       domain="customer_segments", payload={"value": "7500000"}))
    assert len(received) == 1

def test_get_chain_filters():
    bus = _tmp_bus()
    bus.emit(ARIAEvent(source_module="DKSM", event_type="STALENESS_DETECTED",
                       domain="customer_segments", entity="Enterprise", payload={}))
    bus.emit(ARIAEvent(source_module="PP", event_type="PIPELINE_FAILURE_FOUND",
                       domain="product_catalog", entity="DataSense Pro", payload={}))
    chain = bus.get_chain("customer_segments", "Enterprise", hours_back=1)
    assert all(e.domain == "customer_segments" for e in chain)

def test_stats():
    bus = _tmp_bus()
    for _ in range(3):
        bus.emit(ARIAEvent(source_module="AVL", event_type="VALUE_CALCULATED",
                           domain="customer_segments", payload={}))
    s = bus.stats()
    assert s["total_events"] >= 3
    assert "AVL" in s["by_module"]
