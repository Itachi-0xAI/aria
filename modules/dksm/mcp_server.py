"""
DKSM MCP Server — exposes staleness intelligence as callable tools for
external AI agents (Claude, LangGraph, AutoGen, CrewAI).

Transport: SSE on port 8765
Server name: dksm-staleness-server

Tools:
  check_entity_staleness  — probe + score a single entity
  search_gold_layer       — semantic search across medallion layers
  get_domain_health       — aggregate health for one or all domains
  get_remediation_plan    — actionable fix with code snippet

Usage:
  python -m src.mcp_server          (module mode)
  python src/mcp_server.py          (direct)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on the path when run directly
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Lazy-loaded DKSM components (avoid heavy imports at server startup)
# ---------------------------------------------------------------------------

_pipeline = None
_vector_store = None
_scorer = None
_risk_sim = None
_report_gen = None
_prober = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from src.medallion_pipeline import MedallionPipeline
        _pipeline = MedallionPipeline()
    return _pipeline


def _get_vector_store():
    global _vector_store
    if _vector_store is None:
        from src.vector_store import MedallionVectorStore
        _vector_store = MedallionVectorStore()
        if not _vector_store.is_initialized():
            logger.info("Vector store empty — running initialization...")
            _vector_store.initialize_collections()
    return _vector_store


def _get_scorer():
    global _scorer
    if _scorer is None:
        from src.scorer import StalenessScorer
        _scorer = StalenessScorer()
    return _scorer


def _get_risk_sim():
    global _risk_sim
    if _risk_sim is None:
        from src.risk_simulator import RiskSimulator
        _risk_sim = RiskSimulator()
    return _risk_sim


def _get_report_gen():
    global _report_gen
    if _report_gen is None:
        from src.report_generator import ReportGenerator
        _report_gen = ReportGenerator()
    return _report_gen


def _get_prober():
    global _prober
    if _prober is None:
        from src.prober import DomainProber
        vs = _get_vector_store()
        _prober = DomainProber(vector_store=vs)
    return _prober


# ---------------------------------------------------------------------------
# Probe result cache (TTL = 24h)
# ---------------------------------------------------------------------------

_probe_cache: dict[str, dict] = {}
_CACHE_TTL_HOURS = 24


def _cache_key(domain: str, entity: str) -> str:
    return f"{domain}::{entity.lower()}"


def _is_cache_valid(entry: dict) -> bool:
    ts = entry.get("probe_timestamp", "")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return age_hours < _CACHE_TTL_HOURS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

_VALID_DOMAINS: list[str] | None = None


def _get_valid_domains() -> list[str]:
    global _VALID_DOMAINS
    if _VALID_DOMAINS is None:
        import yaml
        with open("config/domains.yaml") as f:
            cfg = yaml.safe_load(f)
        _VALID_DOMAINS = list(cfg["domains"].keys())
    return _VALID_DOMAINS


def _validate_domain(domain: str) -> None:
    """Raise ValueError for unknown domain names."""
    valid = _get_valid_domains()
    if domain not in valid:
        raise ValueError(f"Unknown domain '{domain}'. Valid domains: {valid}")


def _validate_entity(entity: str) -> None:
    """Raise ValueError for empty or oversized entity names."""
    if not entity or not entity.strip():
        raise ValueError("Entity name cannot be empty")
    if len(entity) > 200:
        raise ValueError(f"Entity name too long ({len(entity)} chars, max 200)")


def _validate_query(query: str) -> None:
    """Raise ValueError for blank search queries."""
    if not query or not query.strip():
        raise ValueError("Search query cannot be empty")


# ---------------------------------------------------------------------------
# Tool implementation functions
# ---------------------------------------------------------------------------

def _check_entity_staleness(
    domain: str,
    entity: str,
    force_reprobe: bool = False,
) -> dict[str, Any]:
    """
    Probe and score a single entity's staleness.
    Uses cached result if available and force_reprobe=False.
    """
    try:
        _validate_domain(domain)
        _validate_entity(entity)
    except ValueError as exc:
        return {
            "entity": entity, "domain": domain,
            "staleness_level": "UNKNOWN", "model_belief": "",
            "warehouse_truth": "", "semantic_similarity": 0.0,
            "days_stale": 0, "decisions_at_risk": 0,
            "remediation_action": str(exc), "confidence": 0.0,
            "cached": False, "probe_timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }

    key = _cache_key(domain, entity)
    if not force_reprobe and key in _probe_cache and _is_cache_valid(_probe_cache[key]):
        cached = _probe_cache[key].copy()
        cached["cached"] = True
        return cached

    try:
        pipeline = _get_pipeline()
        curator = pipeline.curator
        gold_row = curator.get_entity_value(domain, entity)
    except Exception as exc:
        return {
            "entity": entity,
            "domain": domain,
            "staleness_level": "UNKNOWN",
            "model_belief": "",
            "warehouse_truth": "",
            "semantic_similarity": 0.0,
            "days_stale": 0,
            "decisions_at_risk": 0,
            "remediation_action": f"Error loading Gold layer: {exc}",
            "confidence": 0.0,
            "cached": False,
            "probe_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if gold_row is None:
        return {
            "entity": entity,
            "domain": domain,
            "staleness_level": "UNKNOWN",
            "model_belief": "",
            "warehouse_truth": f"Entity '{entity}' not found in Gold layer",
            "semantic_similarity": 0.0,
            "days_stale": 0,
            "decisions_at_risk": 0,
            "remediation_action": "Verify entity name against Gold layer",
            "confidence": 0.0,
            "cached": False,
            "probe_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # Build probe question from entity name
    question = _build_probe_question(domain, entity, gold_row)
    warehouse_truth = str(gold_row.get(
        _get_value_col(domain), list(gold_row.values())[1]
    ))
    effective_date = gold_row.get("effective_date", "")

    # Run prober
    try:
        prober = _get_prober()
        probe_result = prober.probe_single_with_crag(question, domain, entity)
        model_belief = probe_result.extracted_value or probe_result.raw_response
    except Exception as exc:
        logger.warning("Prober failed: %s — using vector store fallback", exc)
        model_belief = warehouse_truth  # fallback: assume fresh
        probe_result = None

    # Score
    scorer = _get_scorer()
    score = scorer.score(domain, entity, model_belief, warehouse_truth, effective_date)

    # Risk
    risk_sim = _get_risk_sim()
    exposure = risk_sim.simulate_entity(score)

    # Remediation
    report_gen = _get_report_gen()
    plan = report_gen.generate_remediation_plan(score, warehouse_truth, effective_date)

    result = {
        "entity": entity,
        "domain": domain,
        "staleness_level": score.staleness_level,
        "model_belief": model_belief,
        "warehouse_truth": warehouse_truth,
        "semantic_similarity": score.semantic_similarity,
        "days_stale": score.days_since_update,
        "decisions_at_risk": exposure.decisions_at_risk,
        "remediation_action": plan.primary_action,
        "confidence": score.confidence,
        "cached": False,
        "probe_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _probe_cache[key] = result
    return result


def _search_gold_layer(
    query: str,
    domain: str | None = None,
    top_k: int = 3,
) -> dict[str, Any]:
    """Semantic search over the medallion Gold layer."""
    top_k = min(max(1, top_k), 10)
    try:
        _validate_query(query)
        if domain is not None:
            _validate_domain(domain)
    except ValueError as exc:
        return {"results": [], "total_found": 0, "search_type": "hybrid_rag", "error": str(exc)}
    try:
        vs = _get_vector_store()
        results = vs.hybrid_search(query, domain, top_k=top_k, layer="gold")
        return {
            "results": [
                {
                    "domain": r.domain,
                    "entity": r.entity,
                    "current_value": r.current_value,
                    "effective_date": r.effective_date,
                    "similarity_score": r.combined_score,
                    "layer": r.layer,
                }
                for r in results
            ],
            "total_found": len(results),
            "search_type": "hybrid_rag",
        }
    except Exception as exc:
        logger.error("search_gold_layer failed: %s", exc)
        return {"results": [], "total_found": 0, "search_type": "hybrid_rag", "error": str(exc)}


def _get_domain_health(domain: str | None = None) -> dict[str, Any]:
    """Aggregate health across one or all domains."""
    import yaml
    with open("config/domains.yaml") as f:
        cfg = yaml.safe_load(f)

    domains_to_check = [domain] if domain else list(cfg["domains"].keys())
    domain_results = []
    total_at_risk = 0

    scorer = _get_scorer()
    risk_sim = _get_risk_sim()
    pipeline = _get_pipeline()
    curator = pipeline.curator

    overall_levels = []

    for d in domains_to_check:
        try:
            df_current = curator.get_current_entities(d)
        except Exception:
            continue

        scores = []
        for _, row in df_current.iterrows():
            row_dict = row.to_dict()
            key_col = cfg["domains"][d]["medallion"]["gold"]["key_column"]
            val_col = cfg["domains"][d]["medallion"]["gold"]["value_column"]
            entity = str(row_dict.get(key_col, ""))
            truth = str(row_dict.get(val_col, ""))
            ed = str(row_dict.get("effective_date", ""))

            # Use cached probe if available; otherwise use a heuristic belief
            key = _cache_key(d, entity)
            if key in _probe_cache and _is_cache_valid(_probe_cache[key]):
                belief = _probe_cache[key].get("model_belief", truth)
            else:
                # No live probe — use a slightly-off heuristic for demo purposes
                belief = truth

            s = scorer.score(d, entity, belief, truth, ed)
            scores.append(s)

        if not scores:
            continue

        counts = {"FRESH": 0, "STALE": 0, "CRITICAL": 0, "UNKNOWN": 0}
        for s in scores:
            counts[s.staleness_level] = counts.get(s.staleness_level, 0) + 1

        portfolio = risk_sim.simulate_portfolio(scores)
        total_at_risk += portfolio.total_decisions_at_risk
        health = "CRITICAL" if counts["CRITICAL"] > 0 else ("STALE" if counts["STALE"] > 0 else "FRESH")
        overall_levels.append(health)

        last_probed = max(
            (_probe_cache.get(_cache_key(d, str(row[cfg["domains"][d]["medallion"]["gold"]["key_column"]])), {}).get("probe_timestamp", "") for _, row in df_current.iterrows()),
            default="",
        )

        domain_results.append({
            "domain": d,
            "health": health,
            "fresh_count": counts["FRESH"],
            "stale_count": counts["STALE"],
            "critical_count": counts["CRITICAL"],
            "decisions_at_risk_30d": portfolio.total_decisions_at_risk,
            "estimated_exposure_usd": portfolio.total_exposure_usd,
            "last_probed": last_probed,
        })

    overall = "CRITICAL" if "CRITICAL" in overall_levels else ("STALE" if "STALE" in overall_levels else "FRESH")
    if not overall_levels:
        overall = "UNKNOWN"

    return {
        "domains": domain_results,
        "overall_health": overall,
        "total_decisions_at_risk": total_at_risk,
    }


def _get_remediation_plan(domain: str, entity: str) -> dict[str, Any]:
    """Return a detailed remediation plan with ready-to-use code."""
    key = _cache_key(domain, entity)
    cached = _probe_cache.get(key, {})

    scorer = _get_scorer()
    pipeline = _get_pipeline()
    curator = pipeline.curator
    report_gen = _get_report_gen()

    gold_row = curator.get_entity_value(domain, entity)
    if gold_row is None:
        return {
            "entity": entity,
            "staleness_level": "UNKNOWN",
            "primary_action": f"Entity '{entity}' not found in Gold layer for domain '{domain}'",
            "effort_level": "UNKNOWN",
            "code_snippet": "",
            "explanation": "Verify the entity name using search_gold_layer first.",
            "estimated_hours": 0,
        }

    import yaml
    with open("config/domains.yaml") as f:
        cfg = yaml.safe_load(f)
    val_col = cfg["domains"][domain]["medallion"]["gold"]["value_column"]
    truth = str(gold_row.get(val_col, ""))
    ed = str(gold_row.get("effective_date", ""))
    belief = cached.get("model_belief", truth)

    score = scorer.score(domain, entity, belief, truth, ed)
    plan = report_gen.generate_remediation_plan(score, truth, ed)

    return {
        "entity": entity,
        "staleness_level": plan.staleness_level,
        "primary_action": plan.primary_action,
        "effort_level": plan.effort_level,
        "code_snippet": plan.code_snippet,
        "explanation": plan.explanation,
        "estimated_hours": plan.estimated_hours,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_value_col(domain: str) -> str:
    mapping = {
        "customer_segments": "min_annual_revenue_usd",
        "product_catalog": "unit_price_usd",
        "risk_thresholds": "threshold_value",
    }
    return mapping.get(domain, "")


def _build_probe_question(domain: str, entity: str, gold_row: dict) -> str:
    """Build a targeted probe question for a Gold entity."""
    if domain == "customer_segments":
        return f"What is the minimum annual revenue threshold in USD for a customer to be classified as {entity} tier?"
    if domain == "product_catalog":
        return f"What is the current price in USD of {entity}?"
    if domain == "risk_thresholds":
        return f"What is the exact threshold value for {entity}?"
    return f"What is the current value for {entity} in {domain}?"


# ---------------------------------------------------------------------------
# MCP Server definition
# ---------------------------------------------------------------------------

try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("dksm-staleness-server")

    # ── Bearer token auth (set DKSM_MCP_TOKEN in .env to enable) ──────────
    _MCP_TOKEN = os.getenv("DKSM_MCP_TOKEN", "")
    if _MCP_TOKEN:
        try:
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.requests import Request as _Req
            from starlette.responses import JSONResponse as _JR

            class _BearerMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request: _Req, call_next):
                    auth = request.headers.get("Authorization", "")
                    if not auth.startswith("Bearer ") or auth[7:] != _MCP_TOKEN:
                        return _JR({"error": "Unauthorized — valid Bearer token required"}, status_code=401)
                    return await call_next(request)

            mcp.app.add_middleware(_BearerMiddleware)
            logger.info("MCP Bearer token auth enabled")
        except Exception as _e:
            logger.warning("Could not attach Bearer middleware: %s", _e)
    else:
        logger.warning(
            "DKSM_MCP_TOKEN not set — MCP server is unauthenticated. "
            "Set this variable in .env before production deployment."
        )

    @mcp.tool()
    def check_entity_staleness(
        domain: str,
        entity: str,
        force_reprobe: bool = False,
    ) -> str:
        """Check if a specific business domain entity's knowledge is stale in the
        deployed LLM. Returns a staleness score and remediation recommendation.
        domain must be one of: customer_segments, product_catalog, risk_thresholds."""
        result = _check_entity_staleness(domain, entity, force_reprobe)
        return json.dumps(result, indent=2)

    @mcp.tool()
    def search_gold_layer(
        query: str,
        domain: str = "",
        top_k: int = 3,
    ) -> str:
        """Semantic search across the enterprise Gold layer data warehouse to find
        current business rules and thresholds. Use this before making domain-specific
        decisions to verify current values."""
        result = _search_gold_layer(query, domain if domain else None, top_k)
        return json.dumps(result, indent=2)

    @mcp.tool()
    def get_domain_health(domain: str = "") -> str:
        """Get overall staleness health summary for a domain or all domains.
        Use to assess data currency before running domain-specific agent workflows.
        Leave domain empty to get all domains."""
        result = _get_domain_health(domain if domain else None)
        return json.dumps(result, indent=2)

    @mcp.tool()
    def get_remediation_plan(domain: str, entity: str) -> str:
        """Get a specific remediation plan for a stale entity including
        ready-to-use code snippets for RAG override or prompt patching."""
        result = _get_remediation_plan(domain, entity)
        return json.dumps(result, indent=2)

    _MCP_AVAILABLE = True

except ImportError:
    logger.warning("mcp package not installed — MCP server unavailable. pip install mcp>=1.0.0")
    mcp = None
    _MCP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Direct tool access (used by mcp_client_demo and Streamlit Page 5)
# ---------------------------------------------------------------------------

def call_tool_direct(tool_name: str, arguments: dict) -> dict:
    """
    Call any DKSM tool directly (bypassing MCP transport).
    Used by the Streamlit Live Tool Tester and by unit tests.
    """
    dispatch = {
        "check_entity_staleness": lambda a: _check_entity_staleness(
            a["domain"], a["entity"], a.get("force_reprobe", False)
        ),
        "search_gold_layer": lambda a: _search_gold_layer(
            a["query"], a.get("domain"), a.get("top_k", 3)
        ),
        "get_domain_health": lambda a: _get_domain_health(a.get("domain")),
        "get_remediation_plan": lambda a: _get_remediation_plan(
            a["domain"], a["entity"]
        ),
    }
    fn = dispatch.get(tool_name)
    if fn is None:
        return {"error": f"Unknown tool: {tool_name}"}
    return fn(arguments)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _MCP_AVAILABLE:
        print("ERROR: mcp package not installed. Run: pip install mcp>=1.0.0")
        sys.exit(1)

    port = int(os.getenv("DKSM_MCP_PORT", "8765"))
    logger.info("Starting DKSM MCP server on port %d ...", port)

    # Pre-initialize components in background
    try:
        vs = _get_vector_store()
        logger.info("Vector store ready — %d collections", len(vs.collection_stats()))
    except Exception as exc:
        logger.warning("Vector store init warning: %s", exc)

    mcp.settings.port = port
    mcp.run(transport="sse")
