# ARIA — Adaptive Reasoning & Intelligence Architecture

> **Stop your AI from confidently answering with yesterday's data.**
> ARIA detects when your AI's knowledge has drifted from your data warehouse, fixes it at inference time without retraining, traces which pipeline broke, quantifies the dollar exposure, and learns from every correction.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Streamlit](https://img.shields.io/badge/Streamlit-1.35%2B-red)
![License](https://img.shields.io/badge/License-MIT-green)
![Demo Mode](https://img.shields.io/badge/Demo%20Mode-No%20API%20Key%20Required-orange)

---

## The Problem

Every company running AI faces the same invisible risk: models are trained on historical data, but your business changes every week. Customer tiers get revised. Drug formularies get updated. Risk limits get restructured. Carrier rates expire.

The AI doesn't know — and it answers with complete confidence.

By the time a wrong answer surfaces (a mispriced quote, a denied claim, a failed compliance audit), the damage is done. And you still don't know *why* it happened, *how long* it's been wrong, or *which pipeline run* caused it.

**ARIA closes that loop end-to-end:**

```
Detect divergence → Fix at inference time → Trace root cause → Quantify impact → Learn
```

---

## Project Goal

ARIA is a **real-time AI knowledge integrity layer** that sits between your AI systems and your data warehouse. It continuously monitors whether the AI's beliefs about your business domain align with ground truth in your Gold layer, and takes corrective action automatically — without retraining, without touching model weights, and without changing any existing pipeline.

The system is designed for regulated industries where AI getting facts wrong has direct financial or compliance consequences: **insurance, healthcare, financial services, and logistics**.

---

## Quickstart

```bash
git clone https://github.com/your-username/aria.git
cd aria
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY, or leave blank for demo mode
streamlit run aria.py         # → http://localhost:8501
```

**Demo mode is on by default** — the full 7-page dashboard runs without an API key using simulated data across all 7 domains. To enable live Anthropic API calls (CRAG probes + `inject_and_prompt()`), set `demo_mode: false` in `config/aria_config.yaml` and add your API key to `.env`.

Optional — start the MCP server (9 tools for agent integration):
```bash
python mcp/aria_mcp_server.py   # → port 8765
```

---

## What ARIA Does

### 1 — Detects Knowledge Staleness (DKSM)

ARIA runs a **CRAG probe loop** (Retrieve → Probe → Grade) against every entity in your Gold layer. It asks your AI what it believes, compares it to the current Gold value, and scores the divergence using semantic similarity with a time-decay amplifier.

```
FRESH    → staleness_score < 0.15    (within tolerance)
STALE    → 0.15 ≤ score < 0.40      (warn + inject)
CRITICAL → score ≥ 0.40             (inject + escalate)
```

It also monitors `expiry_date` on Gold records — formularies, contracts, carrier rates — and fires alerts before they lapse.

### 2 — Fixes It at Inference Time (LCI)

On any STALE or CRITICAL detection, ARIA pre-fetches the current Gold value and prepends a verified context block to the next LLM call — no retraining, no fine-tuning, no pipeline changes.

```python
# One-line change in your existing agent code:
result = lci.inject_and_prompt(query, domain="drug_formulary")
response = result["response"]   # Claude answered with verified Gold context pre-injected
```

Context blocks have a 4-hour TTL and are version-tracked. Every injection is logged for traceability.

### 3 — Traces the Root Cause (PP)

When staleness is detected, Pipeline Pulse traces upstream through your dbt run history to find the exact model run that caused the divergence. It classifies failure mode automatically:

| Failure type | What it means |
|-------------|--------------|
| `silent_drop` | Row count fell >30% from median — data went missing without an error |
| `schema_drift` | Column schema changed between runs — downstream model silently broke |
| `gap` | No runs for >24h within the expected window |
| `hard_failure` | dbt model status = failed, error logged |

Reads live `target/run_results.json` from dbt Core when available; falls back to `pipeline_log.csv`.

### 4 — Quantifies the Dollar Exposure (AVL)

Every detected divergence is linked to a dollar figure — the potential cost of AI answering wrong for the affected decision volume:

```
Exposure = (wrong_value_cost_usd) × (decisions_affected_count)
```

Outcomes are linked to injection timestamps via a 4-hour matching window, so recovered value is traceable to specific corrections. Every domain is tagged with its **EU AI Act risk category** (High / Limited / Minimal) for compliance reporting.

### 5 — Learns From Every Correction (FLE)

When a user or agent submits a correction, the Feedback Loop Engine routes it to one of three learning paths:

- **Gold flag + reprobe** — marks the Gold record stale, triggers an immediate re-probe
- **ChromaDB reweight** — boosts the corrected entity's retrieval score so the right context surfaces faster next time
- **Fine-tune pair** — writes a (prompt, corrected_response) pair to JSONL for future model fine-tuning

High-risk routing decisions go to the ASGC approval queue. Low-risk corrections (≥92% confidence) apply automatically.

After each correction, a `REPROBE_REQUESTED` event fires and the domain is re-scored in a background thread. Every 7 days a `LEARNING_IMPROVEMENT` event summarises week-over-week correction counts and repeat error rates.

### 6 — Governs Every Decision (ASGC)

The AI Stack Governance Console maintains a complete audit trail of every high-risk action:

- Approval queue for schema patches, full refreshes, and Gold modifications
- Causal chain explorer — click any event to see its full upstream/downstream chain
- Stack health overview — all 6 modules, last probe time, failure counts
- Board report export — one-click PDF summarising decisions, exposure, corrections, approvals

---

## What Has Been Built

### Fully working (ship as-is)

| Component | Capability |
|-----------|-----------|
| DKSM scoring | CRAG probe loop, FRESH/STALE/CRITICAL classification, time-decay, semantic similarity |
| DKSM expiry monitoring | `EXPIRY_ALERT` + `DATA_CONTRACT_EXPIRY` events, per-industry thresholds, Stale Action Queue |
| PP failure detection | silent_drop, schema_drift, gap, hard_failure; reads dbt `run_results.json` + CSV fallback with column validation |
| ASGC governance | Approval queue, causal chain explorer, board report PDF, lead sign-off flow |
| Event bus | Append-only JSONL, 16 event types, subscribe/emit, zero cross-module imports |
| 7 domains across 5 industries | Config-driven — add a new domain via YAML + CSV, zero code changes |
| MCP server | 9 tools, Bearer auth, SSE transport — Claude Desktop / agent integration ready |
| LCI context injection + `inject_and_prompt()` | 4h TTL, Gold fetch, Claude API call, demo fallback — production middleware complete |
| ChromaDB boost | `boost_entity()` runs on every correction; hybrid search score multiplied by boost factor |
| Reprobe loop | FLE emits `REPROBE_REQUESTED` → FreshnessScheduler reruns domain probe in background thread |
| Weekly self-improvement | `_run_weekly_improvement()` every 7 days, emits `LEARNING_IMPROVEMENT`, dashboard tracks week-over-week delta |
| 7-page Streamlit dashboard | Command Center, DKSM, LCI+PP, AVL (clickable domain table), FLE, ASGC, MCP integration |

### Built but needs real data wired in

| Component | What's missing |
|-----------|---------------|
| LCI real inference | `inject_and_prompt()` is production-ready — your app must call it; it can't intercept existing LLM calls automatically |
| AVL dollar traceability | `_link_injections_to_outcomes()` is built — needs real CRM/claims data instead of simulated `business_outcomes.csv` |
| Weekly accuracy delta | Loop runs and emits events — improvement measured in correction counts, not model accuracy before/after |
| PP real dbt | Parser reads `run_results.json` — needs a live dbt project pointed at it |

### Not yet built

| Gap | Detail |
|-----|--------|
| Dashboard auth | No login on Streamlit UI — anyone with the URL can access |
| Live data warehouse | Gold layer = local CSVs; no Snowflake / BigQuery / DuckDB connector |
| Fine-tuning execution | Training pairs written to JSONL, never sent to a model API |
| Multi-tenant isolation | Company profiles exist but share the same event bus and data directory |
| Observability | No Prometheus metrics endpoint, no alerting, no SLAs |
| CI/CD | No automated test pipeline, no deployment workflow |

---

## What Will Be Built (3 Phases)

### Phase 1 — Real Data (Weeks 1–4)

Connect every CSV simulation to live sources:

- **Snowflake / BigQuery connector** — replace Gold layer CSVs with real data warehouse queries; zero downstream code changes
- **dbt Cloud API integration** — PP reads live `run_results.json` from real dbt runs automatically
- **Decision log connector** — wire `business_outcomes.csv` to Salesforce, HubSpot, or any claims system; AVL dollar values become real
- **LCI production integration** — one-line change in your existing agent code to enable `inject_and_prompt()` in your app

**After Phase 1:** ARIA runs on real data. Dollar values in AVL trace to actual business decisions. PP detects failures in your live dbt runs. DKSM probes your live Gold layer.

### Phase 2 — Self-Improving System (Weeks 5–8)

Close the feedback loop so the system improves week-over-week:

- **Accuracy baseline tracking** — compare staleness score before and after each correction; weekly accuracy delta becomes a real metric, not just a correction count
- **Fine-tuning execution** — submit JSONL training pairs to the Anthropic fine-tuning API; ARIA improves the base model over time
- **Auto-routing for low-risk corrections** — corrections above 92% confidence apply automatically, no approval needed
- **Containerised MCP server** — Dockerfile + docker-compose for production deployment at `https://your-domain/aria`

**After Phase 2:** ARIA is self-improving. Week-over-week accuracy delta is measurable. Low-risk corrections apply instantly. MCP server is deployable anywhere.

### Phase 3 — Enterprise Ready (Weeks 9–12)

Security, observability, compliance:

- **Dashboard SSO auth** — `streamlit-authenticator` or nginx + OAuth2-proxy; role-based access for leads vs analysts
- **Multi-tenant isolation** — each company gets its own event bus file, data directory, and ChromaDB collection namespace
- **Observability stack** — Prometheus metrics (`staleness_detections_total`, `injection_latency_seconds`, `exposure_usd`), Grafana dashboard template
- **CI/CD pipeline** — GitHub Actions: run tests → smoke test → build Docker image → push to registry on merge to main
- **Data governance** — full audit trail with user/timestamp/reason, 90-day event bus archiving, PII encryption at rest, secrets from AWS Secrets Manager / Vault

**After Phase 3:** ARIA is enterprise-grade — authenticated, multi-tenant, monitored, compliant with HIPAA and GDPR environments, and deployable via CI/CD.

---

## Architecture

```
                        ┌─────────────────────────────────┐
                        │        ARIA Event Bus            │
                        │  (core/event_bus.py — JSONL)    │
                        └───┬────┬────┬────┬────┬─────────┘
                            │    │    │    │    │
          ┌─────────────────┘    │    │    │    └─────────────────┐
          ▼                      │    │    │                       ▼
   ┌─────────────┐               │    │    │              ┌─────────────┐
   │    DKSM     │               │    │    │              │    ASGC     │
   │  Knowledge  │──STALENESS──▶ │    │    │ ◀─APPROVAL──│  Governance │
   │  Staleness  │               │    │    │              │  Console    │
   └─────────────┘               │    │    │              └─────────────┘
                                  │    │    │                      ▲
          ┌───────────────────────┘    │    └──────────────────────┤
          ▼                            │                            │
   ┌─────────────┐               ┌────▼────┐              ┌────────┴────┐
   │    LCI      │──INJECTED──▶  │   AVL   │              │    FLE      │
   │  Live       │               │  Value  │──VALUE──────▶│  Feedback   │
   │  Context    │               │  Ledger │              │  Loop       │
   │  Injector   │               └─────────┘              └─────────────┘
   └──────┬──────┘
          │
   ┌──────▼──────┐
   │     PP      │
   │  Pipeline   │
   │  Pulse      │
   └─────────────┘

Rule: no module imports another directly.
      All coordination through EventBus.emit() / subscribe().
```

---

## The 6 Modules

| Module | What It Does | Key Output |
|--------|-------------|------------|
| **DKSM** — Domain Knowledge Staleness Monitor | CRAG probe loop against Gold layer. Scores divergence. Monitors record expiry dates. | `STALENESS_DETECTED`, `EXPIRY_ALERT` |
| **LCI** — Live Context Injector | Pre-fetches Gold value, builds verified context block, calls Claude via `inject_and_prompt()`. 4h TTL. | `CONTEXT_INJECTED` |
| **PP** — Pipeline Pulse | Traces upstream to find the dbt model run that caused staleness. Classifies: silent_drop, schema_drift, gap, hard_failure. | `RootCauseReport` |
| **AVL** — AI Value Ledger | Links injections to business outcomes. Calculates exposure and recovered value. EU AI Act tagging. | `ExposureReport` |
| **FLE** — Feedback Loop Engine | Routes corrections to Gold flagging, ChromaDB reweighting, or fine-tune pair generation. Fires reprobe after every correction. | `RoutingDecision` |
| **ASGC** — AI Stack Governance Console | Approval queue, causal chain explorer, stack health, board report export. | Full audit trail |

---

## The 9 MCP Tools

```
check_staleness       → probe entity, score divergence vs Gold layer
search_gold_layer     → hybrid RAG semantic search over Gold records
inject_context        → get verified context block to prepend to any prompt
inject_and_prompt     → inject context + call Claude in one step (production middleware)
get_pipeline_health   → root cause analysis for all mapped dbt models
get_value_summary     → exposure + recovered value + ROI (configurable window)
submit_correction     → capture a correction signal from any user or agent
get_causal_chain      → full event narrative for a domain/entity
get_stack_health      → all 6 module statuses — agent pre-flight check
```

Connect any agent to ARIA by adding one entry to Claude Desktop config:
```json
{
  "mcpServers": {
    "aria": {
      "url": "http://localhost:8765/sse",
      "headers": { "Authorization": "Bearer <YOUR_ARIA_MCP_TOKEN>" }
    }
  }
}
```

---

## The 7 Dashboard Pages

| Page | What You See |
|------|-------------|
| **1 — Command Center** | 6 module status cards, live event timeline, Expiry & Data Contract Alerts, KPIs: value recovered / decisions at risk / learning velocity |
| **2 — DKSM: Knowledge Staleness** | Staleness bar chart per domain, entity deep-dive (belief vs truth), live CRAG probe trigger, Stale Action Queue with expiry dates and industry filter |
| **3 — LCI + PP: Fix Intelligence** | Active injections table, pipeline failures table, Sankey causal flow diagram |
| **4 — AVL: Value Proof** | Click a domain row → before/after bar chart, exposure card, EU AI Act risk badge, recovery potential, ROI multiplier |
| **5 — FLE: Learning Engine** | Learning velocity gauge, week-over-week improvement chart, correction submission form, fine-tune pairs download |
| **6 — ASGC: Governance Console** | Approval queue, causal chain explorer, stack health table, board report PDF export |
| **7 — MCP & Integration** | 9 tools table, live tester, Claude Desktop config, SDK / LangGraph / AutoGen snippets |

---

## Monitored Domains (7 built-in)

| Domain | Vertical | EU AI Act | Simulated failure | Exposure |
|--------|----------|-----------|-------------------|---------|
| `customer_segments` | Financial Services | 🔴 High | silent_drop (55d) | $1,800,000 |
| `risk_thresholds` | Financial Services | 🔴 High | hard_failure (156d) | $560,000 |
| `coverage_limits` | Insurance | 🔴 High | schema_drift (134d) | $646,000 |
| `drug_formulary` | Healthcare | 🔴 High | silent_drop (87d) | $180,000 |
| `product_catalog` | Retail | 🟡 Limited | schema_drift (90d) | $420,000 |
| `carrier_rates` | Logistics | 🟡 Limited | gap (179d) | $99,200 |
| `coupons` | Retail | 🟢 Minimal | silent_drop (113d) | $24,360 |

Add your own domain: create a Gold CSV, add a YAML entry to `config/dksm/domains.yaml`, map it in `config/pipeline_map.yaml`, restart.

---

## Real Example

```
Gold record:  Enterprise, min_annual_revenue_usd = 7_500_000
Model belief: 6_000_000  (from CRAG probe — 718 days stale)
Similarity:   6M / 7.5M = 0.80  →  sim=0.90 after decay  →  CRITICAL

Event chain:
  DKSM → STALENESS_DETECTED   {level: CRITICAL, sim: 0.80, days_since: 718}
  LCI  → CONTEXT_INJECTED     {entity: Enterprise, value: 7500000, version: v3}
  PP   → PIPELINE_FAILURE     {model: fct_customer_segments, type: silent_drop}
  AVL  → VALUE_CALCULATED     {exposure_usd: 1_800_000, eu_ai_act: High}
  FLE  → CORRECTION_RECEIVED  {signal_type: agent_override, wrong: 6000000}
  FLE  → REPROBE_REQUESTED    {triggered_by: correction_applied}
  ASGC → APPROVAL_REQUIRED    {action: flag_gold_and_reprobe, risk: Medium}
  ASGC → APPROVAL_GRANTED     {approved_by: Lead}
  FLE  → CORRECTION_APPLIED   {actions: [Gold flagged, chroma boosted, reprobe triggered]}
```

---

## Project Structure

```
aria/
├── aria.py                          # 7-page Streamlit dashboard
├── requirements.txt
├── config/
│   ├── aria_config.yaml             # thresholds, module toggles, schedules
│   ├── pipeline_map.yaml            # domain → dbt model mapping (7 domains)
│   └── dksm/
│       ├── domains.yaml             # 7 monitored domains across 5 verticals
│       └── company_profiles/        # per-company custom domain YAMLs
├── core/
│   ├── config_loader.py
│   ├── event_bus.py                 # ARIAEvent dataclass + JSONL bus (16 event types)
│   └── data_simulator.py            # demo data: 1,260 pipeline rows, 300 outcomes
├── modules/
│   ├── dksm/                        # scorer, prober, vector_store, freshness_scheduler
│   ├── lci/                         # context_injector (inject + inject_and_prompt)
│   ├── pp/                          # pipeline_pulse
│   ├── avl/                         # value_ledger
│   ├── fle/                         # feedback_engine
│   └── asgc/                        # governance_console
├── mcp/
│   └── aria_mcp_server.py           # FastMCP server, 9 tools, Bearer auth
└── tests/
    ├── test_event_bus.py
    ├── test_lci.py
    ├── test_pp.py
    ├── test_avl.py
    └── test_fle.py
```

---

## Running Tests

```bash
pytest tests/ -v
```

---

## License

MIT — see [LICENSE](LICENSE)
