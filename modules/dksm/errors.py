"""
Typed exception hierarchy for DKSM.

Catching DKSMError catches all project-specific errors.
Specific subclasses let callers handle narrow failure modes.
"""

from __future__ import annotations


class DKSMError(Exception):
    """Base class for all DKSM exceptions."""


class GoldSchemaError(DKSMError):
    """Gold layer CSV is missing a required column or has an incompatible schema."""


class ProbeError(DKSMError):
    """LLM probe call failed — network error, rate limit, or invalid response."""


class StaleKnowledgeError(DKSMError):
    """Raised when a domain entity is detected as CRITICAL and action is required."""

    def __init__(self, domain: str, entity: str, staleness_level: str, score: float) -> None:
        self.domain = domain
        self.entity = entity
        self.staleness_level = staleness_level
        self.score = score
        super().__init__(
            f"[{staleness_level}] {domain}/{entity} — staleness_score={score:.4f}"
        )


class VectorStoreError(DKSMError):
    """ChromaDB operation failed (connection, upsert, or query error)."""


class DomainNotFoundError(DKSMError):
    """Requested domain is not in domains.yaml."""

    def __init__(self, domain: str, valid_domains: list[str]) -> None:
        self.domain = domain
        self.valid_domains = valid_domains
        super().__init__(
            f"Unknown domain '{domain}'. Valid domains: {valid_domains}"
        )


class EntityNotFoundError(DKSMError):
    """Requested entity is not present in the Gold layer."""

    def __init__(self, domain: str, entity: str) -> None:
        self.domain = domain
        self.entity = entity
        super().__init__(f"Entity '{entity}' not found in Gold layer for domain '{domain}'")


class PipelineError(DKSMError):
    """Bronze → Silver → Gold pipeline step failed."""
