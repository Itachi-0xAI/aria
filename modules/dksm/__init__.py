"""
ARIA DKSM module — Domain Knowledge Staleness Monitor.
Re-exports core classes; emits STALENESS_DETECTED events to the ARIA event bus.
"""
from .scorer import StalenessScorer, StalenessScore
from .prober import DomainProber, ProbeResult

__all__ = ["StalenessScorer", "StalenessScore", "DomainProber", "ProbeResult"]
