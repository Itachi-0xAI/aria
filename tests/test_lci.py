"""Tests for modules/lci/context_injector.py"""
import sys; sys.path.insert(0, "..")
from core.event_bus import ARIAEvent, EventBus
from modules.lci.context_injector import LiveContextInjector
from pathlib import Path
import tempfile

def _lci():
    lci = LiveContextInjector.__new__(LiveContextInjector)
    from core.config_loader import get_config
    from core.event_bus import get_bus
    from pathlib import Path
    import csv
    lci._cfg = get_config()
    lci._bus = get_bus()
    lci._pending = {}
    lci._ttl_hours = 4
    tmp = Path(tempfile.mktemp(suffix=".csv"))
    from modules.lci.context_injector import _LCI_COLS
    with open(tmp, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=_LCI_COLS).writeheader()
    lci._log = tmp
    from modules.lci import context_injector as _m
    _m._LCI_LOG = tmp
    return lci

def test_no_injection_without_pending():
    lci = _lci()
    result = lci.inject("test query", "customer_segments")
    assert result.injected is False

def test_injection_after_staleness_event():
    lci = _lci()
    from datetime import datetime, timedelta, timezone
    lci._pending["Enterprise"] = {
        "domain": "customer_segments", "entity": "Enterprise",
        "value": "7500000", "version": "v3",
        "ready_at": datetime.now(timezone.utc).isoformat(),
        "expires":  datetime.now(timezone.utc) + timedelta(hours=4),
    }
    result = lci.inject("What is Enterprise threshold?", "customer_segments")
    assert result.injected is True
    assert "7500000" in result.context_block
    assert "ARIA VERIFIED CONTEXT" in result.context_block

def test_expired_injection_not_used():
    lci = _lci()
    from datetime import datetime, timedelta, timezone
    lci._pending["Enterprise"] = {
        "domain": "customer_segments", "entity": "Enterprise",
        "value": "7500000", "version": "v3",
        "ready_at": datetime.now(timezone.utc).isoformat(),
        "expires":  datetime.now(timezone.utc) - timedelta(hours=1),  # expired
    }
    result = lci.inject("test", "customer_segments")
    assert result.injected is False
