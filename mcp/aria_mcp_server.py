"""
ARIA Unified MCP Server — 8 tools over SSE on port 8765.
Replaces dksm/src/mcp_server.py.

Tools:
  1. check_staleness       — DKSM: probe entity, return level + score
  2. search_gold_layer     — DKSM: semantic search over Gold records
  3. inject_context        — LCI: inject verified Gold value into agent context
  4. get_pipeline_health   — PP: failure summary for all mapped domains
  5. get_value_summary     — AVL: exposure + recovered + ROI
  6. submit_correction     — FLE: capture a correction signal
  7. get_causal_chain      — ASGC: full event narrative for a domain/entity
  8. get_stack_health      — ASGC: all 6 module statuses
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Ensure aria/ root is on the path
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP
from core.config_loader import get_config
from core.event_bus import ARIAEvent, get_bus

logger  = logging.getLogger(__name__)
mcp     = FastMCP("aria-intelligence-server")
_cfg    = get_config()

# ── Bearer token auth ────────────────────────────────────────────────────────
_TOKEN = os.getenv("ARIA_MCP_TOKEN", "")
if _TOKEN:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as _Req
    from starlette.responses import JSONResponse as _JR

    class _BearerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: _Req, call_next):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != _TOKEN:
                return _JR({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

    mcp.app.add_middleware(_BearerMiddleware)
    logger.info("ARIA MCP: Bearer token auth enabled")
else:
    logger.warning("ARIA_MCP_TOKEN not set — server is unauthenticated (local dev only)")


# ── Lazy module loaders ──────────────────────────────────────────────────────

def _scorer():
    from modules.dksm.scorer import StalenessScorer
    return StalenessScorer(str(_ROOT / "config" / "dksm" / "domains.yaml"))

def _vs():
    from modules.dksm.vector_store import MedallionVectorStore
    return MedallionVectorStore()

def _lci():
    from modules.lci.context_injector import LiveContextInjector
    return LiveContextInjector()

def _pp():
    from modules.pp.pipeline_pulse import PipelinePulse
    return PipelinePulse()

def _avl():
    from modules.avl.value_ledger import AIValueLedger
    return AIValueLedger()

def _fle():
    from modules.fle.feedback_engine import FeedbackLoopEngine
    return FeedbackLoopEngine()

def _asgc():
    from modules.asgc.governance_console import GovernanceConsole
    return GovernanceConsole()


# ── Tool 1: check_staleness ──────────────────────────────────────────────────

@mcp.tool()
def check_staleness(domain: str, entity: str) -> dict:
    """
    Probe the AI's knowledge of a specific entity and return its staleness level.
    Args:
        domain: e.g. 'customer_segments', 'product_catalog', 'risk_thresholds'
        entity: e.g. 'Enterprise', 'DataSense Pro', 'Portfolio VaR - High'
    Returns:
        staleness_level (FRESH/STALE/CRITICAL), semantic_similarity, model_belief,
        warehouse_truth, staleness_score, remediation_hint
    """
    try:
        import pandas as pd
        domains_cfg = _cfg.dksm_domains
        if domain not in domains_cfg:
            return {"error": f"Domain '{domain}' not found"}
        gold_cfg  = domains_cfg[domain]["medallion"]["gold"]
        gold_path = _ROOT / "data" / "dksm" / "gold_layer" / Path(gold_cfg["path"]).name
        if not gold_path.exists():
            gold_path = Path(gold_cfg["path"])
        df      = pd.read_csv(gold_path)
        current = df[df.get("is_current", pd.Series([True]*len(df))) == True]  # noqa
        row     = current[current[gold_cfg["key_column"]] == entity]
        if row.empty:
            return {"error": f"Entity '{entity}' not found in domain '{domain}'"}
        r        = row.iloc[0].to_dict()
        truth    = str(r[gold_cfg["value_column"]])
        raw_b    = r.get("model_belief", "")
        belief   = str(raw_b) if raw_b not in ("", None) and str(raw_b) not in ("nan",) else truth
        ed       = str(r.get("effective_date", ""))
        score    = _scorer().score(domain, entity, belief, truth, ed)

        # Emit event to the bus
        get_bus().emit(ARIAEvent(
            source_module="DKSM", event_type="STALENESS_DETECTED",
            domain=domain, entity=entity,
            payload={"level": score.staleness_level, "sim": score.semantic_similarity,
                     "days_since_update": score.days_since_update},
            severity="CRITICAL" if score.staleness_level == "CRITICAL" else "INFO",
        ))

        hint = ("Update RAG context with Gold value" if score.staleness_level == "STALE"
                else "Inject verified context immediately" if score.staleness_level == "CRITICAL"
                else "No action required")
        return {
            "domain":             domain,
            "entity":             entity,
            "staleness_level":    score.staleness_level,
            "semantic_similarity": score.semantic_similarity,
            "staleness_score":    score.staleness_score,
            "model_belief":       belief,
            "warehouse_truth":    truth,
            "days_since_update":  score.days_since_update,
            "remediation_hint":   hint,
        }
    except Exception as exc:
        logger.error("check_staleness error: %s", exc)
        return {"error": str(exc)}


# ── Tool 2: search_gold_layer ────────────────────────────────────────────────

@mcp.tool()
def search_gold_layer(query: str, domain: str = "", top_k: int = 3) -> dict:
    """
    Semantic search over Gold layer records using Hybrid RAG.
    Args:
        query: natural language question or entity name
        domain: optional filter to a specific domain
        top_k: number of results to return (default 3)
    """
    try:
        vs = _vs()
        if not vs.is_initialized():
            vs.initialize_collections()
        results = vs.search(query, domain=domain or None, top_k=top_k)
        return {
            "query":   query,
            "results": [
                {"domain": r.domain, "entity": r.entity, "content": r.content,
                 "score": round(r.score, 4), "layer": r.layer}
                for r in results
            ],
        }
    except Exception as exc:
        logger.error("search_gold_layer error: %s", exc)
        return {"error": str(exc)}


# ── Tool 3: inject_context ───────────────────────────────────────────────────

@mcp.tool()
def inject_context(query: str, domain: str) -> dict:
    """
    Inject the current verified Gold layer value for a domain into agent context.
    Call this before any LLM inference on a domain known to be stale.
    Args:
        query: the agent's current query (used for logging)
        domain: domain to inject context for
    Returns:
        injected (bool), context_block (str to prepend to prompt), entity, value
    """
    try:
        result = _lci().inject(query, domain)
        return {
            "injected":       result.injected,
            "entity":         result.entity,
            "injected_value": result.injected_value,
            "injection_id":   result.injection_id,
            "context_block":  result.context_block,
            "source_version": result.source_version,
            "expires_at":     result.expires_at.isoformat() if result.expires_at else None,
        }
    except Exception as exc:
        logger.error("inject_context error: %s", exc)
        return {"error": str(exc)}


@mcp.tool()
def inject_and_prompt(query: str, domain: str, entity: str = "") -> dict:
    """
    Full LCI middleware: fetch verified Gold context, prepend it, call Claude, return response.
    Use this instead of calling Claude directly when the domain may be stale.
    Args:
        query:  the user or agent's question
        domain: ARIA domain to ground (e.g. customer_segments, drug_formulary)
        entity: optional entity hint (e.g. Enterprise, Humira Biosimilar)
    Returns:
        response (str), context_used (bool), injected_value, cost_usd
    """
    try:
        return _lci().inject_and_prompt(query, domain, entity)
    except Exception as exc:
        logger.error("inject_and_prompt error: %s", exc)
        return {"error": str(exc)}


# ── Tool 4: get_pipeline_health ──────────────────────────────────────────────

@mcp.tool()
def get_pipeline_health(domain: str = "") -> dict:
    """
    Return pipeline root cause analysis for all mapped domains (or one domain).
    Args:
        domain: optional — filter to a specific domain. Empty = all domains.
    """
    try:
        summary = _pp().get_pipeline_health_summary()
        if domain:
            by_d = summary.get("by_domain", {})
            return {"domain": domain, **by_d.get(domain, {"status": "not_mapped"})}
        return summary
    except Exception as exc:
        logger.error("get_pipeline_health error: %s", exc)
        return {"error": str(exc)}


# ── Tool 5: get_value_summary ────────────────────────────────────────────────

@mcp.tool()
def get_value_summary(days_back: int = 30) -> dict:
    """
    Return the AI Value Ledger summary: exposure identified, value recovered, ROI.
    Args:
        days_back: lookback window in days (default 30)
    """
    try:
        return _avl().get_value_summary(days_back)
    except Exception as exc:
        logger.error("get_value_summary error: %s", exc)
        return {"error": str(exc)}


# ── Tool 6: submit_correction ────────────────────────────────────────────────

@mcp.tool()
def submit_correction(domain: str, entity: str, wrong_value: str,
                      correct_value: str, signal_type: str = "agent_override",
                      confidence: float = 1.0) -> dict:
    """
    Submit a correction signal to the Feedback Loop Engine.
    Args:
        domain:        domain key (e.g. 'customer_segments')
        entity:        entity name (e.g. 'Enterprise')
        wrong_value:   the incorrect value the AI used
        correct_value: the verified correct value
        signal_type:   user_correction | agent_override | escalation | non_use
        confidence:    confidence in this correction (0-1, default 1.0)
    """
    try:
        signal = _fle().capture_signal(
            signal_type=signal_type, domain=domain, entity=entity,
            wrong_value=wrong_value, correct_value=correct_value,
            source="mcp_tool", confidence=confidence,
        )
        return {
            "signal_id":   signal.signal_id,
            "fle_status":  signal.fle_status,
            "domain":      domain,
            "entity":      entity,
            "routed":      signal.fle_status == "classified",
        }
    except Exception as exc:
        logger.error("submit_correction error: %s", exc)
        return {"error": str(exc)}


# ── Tool 7: get_causal_chain ─────────────────────────────────────────────────

@mcp.tool()
def get_causal_chain(domain: str, entity: str, hours_back: int = 24) -> dict:
    """
    Return the full causal event chain for a domain/entity.
    Shows: staleness detection → context injection → pipeline root cause →
           value calculation → correction signals → approvals.
    Args:
        domain:     domain key
        entity:     entity name
        hours_back: lookback window in hours (default 24)
    """
    try:
        chain = _asgc().get_causal_chain(domain, entity, hours_back)
        return {
            "domain":               chain.domain,
            "entity":               chain.entity,
            "event_count":          len(chain.events),
            "narrative":            chain.narrative,
            "total_exposure_usd":   chain.total_exposure_usd,
            "total_recovered_usd":  chain.total_recovered_usd,
            "open_approvals":       chain.open_approvals,
            "events": [
                {"type": e.event_type, "module": e.source_module,
                 "severity": e.severity, "ts": e.timestamp.isoformat()}
                for e in chain.events
            ],
        }
    except Exception as exc:
        logger.error("get_causal_chain error: %s", exc)
        return {"error": str(exc)}


# ── Tool 8: get_stack_health ─────────────────────────────────────────────────

@mcp.tool()
def get_stack_health() -> dict:
    """
    Return the health status of all 6 ARIA modules.
    Use this as an agent pre-flight check before any domain-sensitive decision.
    """
    try:
        return _asgc().get_stack_health()
    except Exception as exc:
        logger.error("get_stack_health error: %s", exc)
        return {"error": str(exc)}


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("ARIA_MCP_PORT", "8765"))
    logger.info("Starting ARIA MCP server on port %d …", port)
    uvicorn.run(mcp.app, host=os.getenv("ARIA_MCP_HOST", "127.0.0.1"), port=port)
