"""
Optional OpenTelemetry tracing for ARIA.

Install tracing support:
    pip install opentelemetry-api opentelemetry-sdk

When OTel is not installed every call is a no-op — zero runtime cost, zero
import errors.
"""

from __future__ import annotations

import contextlib
from typing import Any, Generator

# ---------------------------------------------------------------------------
# Lazy OTel import — never raises at module load time
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import NonRecordingSpan  # noqa: F401 (used in tests)
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover — tested via separate no-op path
    _OTEL_AVAILABLE = False


_TRACER_NAME = "aria"


def get_tracer():
    """
    Return an OTel Tracer when opentelemetry-api is installed,
    otherwise return None.
    """
    if _OTEL_AVAILABLE:
        return _otel_trace.get_tracer(_TRACER_NAME)
    return None


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Generator[Any, None, None]:
    """
    Context manager that creates an OTel span when OTel is available,
    or yields None silently when it is not.

    Usage::

        with span("aria.dksm.score", aria_domain="finance") as s:
            ...  # s may be None
    """
    tracer = get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(name) as otel_span:
        for key, value in attrs.items():
            if value is not None:
                otel_span.set_attribute(key.replace("_", "."), value)
        yield otel_span
