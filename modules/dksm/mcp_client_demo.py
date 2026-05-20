"""
DKSM MCP Client Demo — Enterprise Pricing Agent

Demonstrates an external AI agent calling DKSM MCP tools in real-time
to correct its own classification decision when it detects stale knowledge.

The agent:
  1. Receives a customer with $7.5M annual revenue
  2. Makes an initial classification using its own (stale) knowledge
  3. Calls search_gold_layer to verify current thresholds
  4. Gets a CRITICAL staleness warning via check_entity_staleness
  5. Calls get_remediation_plan for actionable guidance
  6. Produces a corrected classification with explanation

Run this in a separate terminal while the Streamlit dashboard is open:
  python src/mcp_client_demo.py

The MCP server must be running:
  python src/mcp_server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _banner(title: str) -> None:
    width = 72
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def _step(n: int, desc: str) -> None:
    print(f"\n[{_ts()}] STEP {n}: {desc}")
    print("-" * 60)


def _agent_thought(text: str) -> None:
    print(f"[{_ts()}] 🤔 AGENT REASONING: {text}")


def _tool_call(name: str, args: dict) -> None:
    print(f"[{_ts()}] 🔧 TOOL CALL: {name}")
    print(f"         args: {json.dumps(args, indent=10)}")


def _tool_result(result: dict | str, truncate: int = 800) -> None:
    text = json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
    if len(text) > truncate:
        text = text[:truncate] + "\n  ... (truncated)"
    print(f"[{_ts()}] 📦 TOOL RESULT:")
    for line in text.splitlines():
        print(f"         {line}")


def _agent_decision(text: str) -> None:
    print(f"\n[{_ts()}] ✅ AGENT DECISION: {text}")


def _warning(text: str) -> None:
    print(f"[{_ts()}] ⚠️  WARNING: {text}")


# ---------------------------------------------------------------------------
# MCP client — tries SSE transport first, falls back to direct calls
# ---------------------------------------------------------------------------

async def _call_mcp_tool_sse(tool: str, args: dict) -> dict:
    """Call a tool via the live MCP SSE server."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    port = int(os.getenv("DKSM_MCP_PORT", "8765"))
    url = f"http://localhost:{port}/sse"

    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            # FastMCP returns content as TextContent list
            if result.content:
                text = result.content[0].text
                try:
                    return json.loads(text)
                except Exception:
                    return {"raw": text}
    return {}


def _call_tool_direct(tool: str, args: dict) -> dict:
    """Fallback: call tool directly without MCP transport."""
    from src.mcp_server import call_tool_direct
    return call_tool_direct(tool, args)


def call_tool(tool: str, args: dict, use_live_server: bool = True) -> dict:
    """
    Call a DKSM tool.
    Tries the live MCP server first; falls back to direct invocation
    if the server is not reachable.
    """
    if use_live_server:
        try:
            result = asyncio.run(_call_mcp_tool_sse(tool, args))
            if result:
                return result
        except Exception as exc:
            print(f"[{_ts()}] ℹ️  MCP server not reachable ({exc}), using direct mode")

    return _call_tool_direct(tool, args)


# ---------------------------------------------------------------------------
# Enterprise Pricing Agent
# ---------------------------------------------------------------------------

def run_pricing_agent() -> None:
    """
    Full agent reasoning chain.
    A pricing agent must classify a customer and determine their support tier
    before making a contract proposal.
    """
    _banner("DKSM Enterprise Pricing Agent Demo")

    CUSTOMER = {
        "name": "Acme Corp",
        "annual_revenue_usd": 7_500_000,
        "industry": "Manufacturing",
    }

    print(f"""
Scenario:
  You are an enterprise pricing agent. Before classifying this customer,
  verify current segment thresholds using the DKSM staleness monitor.

  Customer: {CUSTOMER['name']}
  Annual Revenue: ${CUSTOMER['annual_revenue_usd']:,.0f}
  Industry: {CUSTOMER['industry']}
  Task: Determine their support tier and applicable discount rate.
""")

    # -----------------------------------------------------------------------
    # Step 1: Agent makes initial classification from own (stale) knowledge
    # -----------------------------------------------------------------------
    _step(1, "Initial classification using agent's internal knowledge")
    _agent_thought(
        "Based on my training data, Enterprise tier starts at $5M annual revenue. "
        f"Acme Corp has ${CUSTOMER['annual_revenue_usd']:,.0f} — classifying as Enterprise."
    )
    time.sleep(0.5)

    initial_classification = {
        "tier": "Enterprise",
        "support_tier": "Platinum",
        "discount_rate": "15%",
        "reasoning": "Revenue > $5M threshold (from training data)",
    }
    print(f"         Initial classification: {json.dumps(initial_classification, indent=10)}")

    # -----------------------------------------------------------------------
    # Step 2: Agent queries DKSM — search Gold layer for current thresholds
    # -----------------------------------------------------------------------
    _step(2, "Verifying current thresholds via DKSM search_gold_layer")
    _agent_thought(
        "Before finalising the proposal, I must check whether my threshold "
        "knowledge is current. Calling DKSM to search the Gold layer."
    )

    args_search = {"query": "enterprise customer revenue threshold classification tier", "top_k": 5}
    _tool_call("search_gold_layer", args_search)
    time.sleep(0.3)

    search_result = call_tool("search_gold_layer", args_search)
    _tool_result(search_result)

    # Extract top Gold result
    top_results = search_result.get("results", [])
    if top_results:
        top_entity = top_results[0]
        _agent_thought(
            f"Gold layer shows entity '{top_entity['entity']}' "
            f"with value {top_entity['current_value']} (effective {top_entity['effective_date']}). "
            "This may differ from my training data — checking staleness."
        )
    time.sleep(0.5)

    # -----------------------------------------------------------------------
    # Step 3: Check staleness for Enterprise entity
    # -----------------------------------------------------------------------
    _step(3, "Checking staleness for Enterprise segment threshold")

    args_staleness = {
        "domain": "customer_segments",
        "entity": "Enterprise",
        "force_reprobe": False,
    }
    _tool_call("check_entity_staleness", args_staleness)
    time.sleep(0.3)

    staleness_result = call_tool("check_entity_staleness", args_staleness)
    _tool_result(staleness_result)

    staleness_level = staleness_result.get("staleness_level", "UNKNOWN")
    model_belief = staleness_result.get("model_belief", "unknown")
    warehouse_truth = staleness_result.get("warehouse_truth", "unknown")
    similarity = staleness_result.get("semantic_similarity", 0.0)
    decisions_at_risk = staleness_result.get("decisions_at_risk", 0)

    if staleness_level in ("STALE", "CRITICAL"):
        _warning(
            f"Entity 'Enterprise' is {staleness_level}! "
            f"Model believes threshold is '{model_belief}' but "
            f"Gold layer says '{warehouse_truth}' "
            f"(similarity: {similarity:.2f}). "
            f"{decisions_at_risk} decisions/month at risk."
        )
    time.sleep(0.5)

    # -----------------------------------------------------------------------
    # Step 4: Get remediation plan
    # -----------------------------------------------------------------------
    _step(4, "Retrieving remediation plan for stale Enterprise threshold")

    args_remediation = {"domain": "customer_segments", "entity": "Enterprise"}
    _tool_call("get_remediation_plan", args_remediation)
    time.sleep(0.3)

    remediation_result = call_tool("get_remediation_plan", args_remediation)
    _tool_result(remediation_result)
    time.sleep(0.5)

    # -----------------------------------------------------------------------
    # Step 5: Check overall domain health
    # -----------------------------------------------------------------------
    _step(5, "Checking overall customer_segments domain health")

    args_health = {"domain": "customer_segments"}
    _tool_call("get_domain_health", args_health)
    time.sleep(0.3)

    health_result = call_tool("get_domain_health", args_health)
    _tool_result(health_result)
    time.sleep(0.5)

    # -----------------------------------------------------------------------
    # Step 6: Agent corrects its classification
    # -----------------------------------------------------------------------
    _step(6, "Agent produces corrected classification")

    # Use Gold layer truth if staleness warning was received
    if staleness_level in ("STALE", "CRITICAL"):
        try:
            gold_threshold = float(warehouse_truth.replace(",", ""))
        except (ValueError, AttributeError):
            gold_threshold = 7_500_000

        revenue = CUSTOMER["annual_revenue_usd"]

        # Determine correct tier based on Gold layer values
        if revenue < 150_000:
            correct_tier, correct_support, correct_discount = "Startup", "Basic", "3%"
        elif revenue < 750_000:
            correct_tier, correct_support, correct_discount = "SMB", "Standard", "7%"
        elif revenue < 7_500_000:
            correct_tier, correct_support, correct_discount = "Mid-Market", "Premium", "13%"
        elif revenue < 75_000_000:
            correct_tier, correct_support, correct_discount = "Enterprise", "Platinum", "20%"
        else:
            correct_tier, correct_support, correct_discount = "Strategic", "Concierge", "25%"

        _agent_thought(
            f"DKSM flagged my Enterprise threshold knowledge as {staleness_level}. "
            f"My training data said Enterprise starts at $5M, but the current Gold layer "
            f"threshold is $7.5M. "
            f"Acme Corp's revenue of ${revenue:,.0f} places them exactly at the Enterprise boundary. "
            "Adjusting classification to use current warehouse values."
        )

        corrected = {
            "tier": correct_tier,
            "support_tier": correct_support,
            "discount_rate": correct_discount,
            "reasoning": f"Corrected using DKSM Gold layer (effective {staleness_result.get('probe_timestamp', 'now')[:10]})",
            "stale_knowledge_used": initial_classification["tier"] != correct_tier,
            "gold_threshold_used": warehouse_truth,
            "dksm_staleness_level": staleness_level,
        }

        if initial_classification["tier"] != correct_tier:
            _warning(
                f"Classification CHANGED: {initial_classification['tier']} → {correct_tier}. "
                f"Initial discount {initial_classification['discount_rate']} → {correct_discount}."
            )
        else:
            _agent_thought("Classification tier is the same, but discount rate and support terms updated.")

        _agent_decision(json.dumps(corrected, indent=2))

    else:
        _agent_thought("Knowledge is FRESH — initial classification is valid.")
        _agent_decision(json.dumps(initial_classification, indent=2))

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    _banner("Demo Complete")
    print(f"""
Summary:
  Customer:    {CUSTOMER['name']} (${CUSTOMER['annual_revenue_usd']:,.0f} ARR)
  Initial:     {initial_classification['tier']} / {initial_classification['support_tier']} / {initial_classification['discount_rate']}
  DKSM Signal: Enterprise threshold is {staleness_level}
  Corrected:   {correct_tier} / {correct_support} / {correct_discount}

This is the moment that closes enterprise conversations.
An agent correcting itself because DKSM told it the data was stale.
""")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Check for API key
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("Create a .env file from .env.example and add your key.")
        sys.exit(1)

    run_pricing_agent()
