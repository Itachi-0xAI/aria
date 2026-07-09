# ARIA — System Design

**Adaptive Reasoning & Intelligence Architecture**
An AI knowledge integrity layer that detects, corrects, and governs stale AI knowledge in enterprise data pipelines.

---

## 1. Overview / Goals

ARIA solves the "stale AI" problem: production LLM systems answer from pre-indexed embedding stores built from historical snapshots. When the underlying business data changes (prices, thresholds, drug formularies, coverage limits) the retrieval index doesn't reflect the new ground truth — and the LLM confidently asserts outdated values as if they were current. In regulated industries this causes real financial and compliance exposure.

**Core goals:**

1. **Detect** when an AI model's knowledge diverges from the enterprise data warehouse (Gold layer)
2. **Correct** stale model responses at runtime, without retraining, via context injection
3. **Trace** the upstream pipeline failure that caused the staleness
4. **Quantify** the financial exposure from decisions made on stale AI knowledge
5. **Close the loop** by routing human corrections back to the data layer and RAG indices
6. **Govern** all high-risk actions through a human-in-the-loop approval queue
7. **Expose** every capability as MCP tools so any AI agent can use ARIA as a knowledge integrity pre-flight

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          ARIA — Module Overview                              │
│                                                                              │
│   ┌─────────┐    STALENESS_DETECTED     ┌─────────┐                         │
│   │  DKSM   │ ─────────────────────────►│   LCI   │  inject_and_prompt()    │
│   │ scorer  │                           │ context │ ──────────────────────► │
│   │ prober  │ ─────────────────────────►│ injector│         Claude API       │
│   │ sched.  │    STALENESS_DETECTED     └─────────┘                         │
│   │ vectors │                                                                │
│   └────┬────┘ ─────────────────────────►┌─────────┐                         │
│        │         STALENESS_DETECTED     │   PP    │ PIPELINE_FAILURE_FOUND  │
│        │                               │pipeline │ ──────────────┐          │
│        │                               │  pulse  │               │          │
│        │                               └─────────┘               ▼          │
│        │                                                    ┌─────────┐     │
│        │                               CONTEXT_INJECTED ──►│   AVL   │     │
│        │                               PIPELINE_FAILURE ──►│  value  │     │
│        │                               CORRECTION_APPLIED ►│ ledger  │     │
│        │                                                    └────┬────┘     │
│        │                                                         │          │
│        │                               ┌─────────┐  VALUE_CALCULATED        │
│        │        correction signals     │   FLE   │◄────────────────────     │
│        │◄── REPROBE_REQUESTED ─────────│feedback │                          │
│        │                               │  engine │──► Gold flag             │
│        │                               │         │──► ChromaDB reweight     │
│        │                               │         │──► fine_tune_pairs.jsonl │
│        │                               └─────────┘                          │
│        │                                                                     │
│        └────────────────────────────►  ┌─────────┐                          │
│                 APPROVAL_REQUIRED ────►│  ASGC   │                          │
│                 VALUE_CALCULATED ─────►│govern.  │──► Board PDF             │
│                 (all CRITICAL) ───────►│console  │──► Approval queue        │
│                                        └─────────┘                          │
│                                                                              │
│  ══════════════════════  EVENT BUS (append-only JSONL)  ═══════════════════  │
│                          core/event_bus.py                                   │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  MCP Server — port 8765, SSE, Bearer auth — mcp/aria_mcp_server.py  │   │
│  │  9 tools: check_staleness | search_gold_layer | inject_context |     │   │
│  │           inject_and_prompt | get_pipeline_health | get_value_summary│   │
│  │           submit_correction | get_causal_chain | get_stack_health    │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  Streamlit Dashboard — aria.py — 7 pages                             │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

```
Data Architecture — Medallion Layers

  Raw source                           ChromaDB
  data files                           vector store
      │                                    ▲
      ▼                                    │
  ┌────────┐   ingest   ┌────────┐        │  embed
  │ Bronze │ ─────────► │ Silver │        │
  │  CSVs  │            │  CSVs  │        │
  └────────┘            └────────┘        │
                            │  promote    │
                            ▼             │
                        ┌────────┐ ───────┘
                        │  Gold  │
                        │  CSVs  │ ◄── LCI fetches current value
                        └────────┘ ◄── FLE flags corrections
                        (is_current=True records)
```

---

## 3. Module Breakdown

### 3.1 DKSM — Domain Knowledge Staleness Monitor

**Responsibility:** Probe LLM knowledge against the Gold layer, score divergence, schedule re-probes, manage vector store.

| Component | File | Role |
|---|---|---|
| StalenessScorer | `modules/dksm/scorer.py` | Computes staleness_score (0-1) from semantic similarity + exponential time-decay |
| DomainProber | `modules/dksm/prober.py` | CRAG loop: probe LLM → grade extraction → retry on AMBIGUOUS → fallback to vector store |
| CRAGValidator | `modules/dksm/prober.py` | Haiku-based grader that scores CORRECT/AMBIGUOUS/INCORRECT and generates retry questions |
| MedallionVectorStore | `modules/dksm/vector_store.py` | ChromaDB + BM25 hybrid search over Gold/Silver/Bronze collections |
| FreshnessScheduler | `modules/dksm/freshness_scheduler.py` | Daemon thread that runs probes on hourly/daily/weekly/manual schedules; persists state to JSON |
| MedallionPipeline | `modules/dksm/medallion_pipeline.py` | Bronze → Silver → Gold ETL promotion with dedup, null checks, quality scoring |

**Inputs:** Gold layer CSVs, `config/dksm/domains.yaml` (probe questions, thresholds per domain)
**Outputs:** `STALENESS_DETECTED` events, `StalenessScore` objects, CRAG log CSV, probe cache JSON

**Scoring formula:**
```
divergence    = 1 - semantic_similarity
time_decay    = 1 - exp(-days_since_update / 180)   # half-life = 180 days
staleness_score = min(divergence * (1 + 0.3 * time_decay), 1.0)
```

**Classification thresholds** (configurable per domain in `domains.yaml`):
- FRESH: similarity ≥ 0.90
- STALE: similarity ≥ 0.70
- CRITICAL: similarity < 0.70

---

#### Prompt Coverage Analyzer (`modules/dksm/prompt_coverage.py`)

**Responsibility:** Measure how well a domain's `probe_questions` cover the edge
cases that matter for each Gold-layer entity. Analogous to code coverage — but
for prompts.

**The gap:** `domains.yaml` lists `probe_questions` (8 per domain on average).
Gold layer CSVs list the entities those probes are supposed to cover. Without
coverage analysis, it's unknown whether the probes ever ask about expiry dates,
tier changes, or stale model beliefs — the questions most likely to surface
hallucinations.

**Algorithm:**

```
For each domain:
  1. Load current Gold-layer entities (is_current=True records)
  2. For each entity, generate up to 7 canonical edge-case queries:
       exact_value   → "What is the [value_col] for [entity]?"
       tier          → "What tier is [entity] assigned to?" (if tier data present)
       auth          → "Does [entity] require prior authorization?" (if prior_auth present)
       comparison    → "How does [entity] compare to others in [domain]?"
       expiry        → "When does [entity]'s value expire?" (if expiry_date present)
       boundary      → "What is the threshold that defines [entity] in [domain]?"
       stale_belief  → "Has [entity]'s value changed from [model_belief]?" (if belief ≠ value)
  3. Embed all canonical queries + all probe questions (all-MiniLM-L6-v2)
  4. Build [n_cases × n_probes] cosine similarity matrix
  5. For each edge case: best_score = max similarity to any probe
  6. covered = best_score >= threshold (default 0.55)
  7. coverage_pct = covered_count / total_edge_cases × 100
```

**Output:** `DomainCoverageReport` with per-entity coverage, gap list, and
auto-generated `suggested_probes` for every uncovered edge case.

**CLI runner:** `python coverage.py` (all domains) or
`python coverage.py drug_formulary --threshold=0.60`

**Real results (drug_formulary, threshold=0.55):**
- 59.4% coverage — Lisinopril and Atorvastatin have no expiry or boundary probes
- 3 suggested probes auto-generated for uncovered gaps

---

### 3.2 LCI — Live Context Injector

**Responsibility:** Subscribe to STALENESS_DETECTED, pre-fetch the current Gold value, and prepend a verified context block to any LLM call before inference.

**File:** `modules/lci/context_injector.py`

**Inputs:** `STALENESS_DETECTED` events, agent query strings, Gold layer CSVs
**Outputs:** `CONTEXT_INJECTED` events, `InjectionResult` with `context_block` string, `lci_log.csv`

**Key behaviors:**
- On `STALENESS_DETECTED`: reads Gold CSV, caches entity → value with a 4-hour TTL in memory (`self._pending`)
- On `inject(query, domain)`: finds unexpired pending injection, builds context block, logs to CSV, emits `CONTEXT_INJECTED`
- On `inject_and_prompt(query, domain)`: wraps inject + Claude API call; falls back to demo mode when `ANTHROPIC_API_KEY` is absent

**Context block format:**
```
[ARIA VERIFIED CONTEXT — 2024-01-15 14:30 UTC]
Source: Gold layer v2.1 (enterprise data warehouse)
Entity: Enterprise (domain: customer_segments)
Current value: $85,000/yr
Note: This value supersedes any prior model knowledge. Valid until 18:30 UTC.
```

---

### 3.3 PP — Pipeline Pulse

**Responsibility:** Trace the upstream dbt pipeline failure that caused staleness. Classify the failure type and recommend remediation options.

**File:** `modules/pp/pipeline_pulse.py`

**Inputs:** `STALENESS_DETECTED` events, `data/pipeline_log.csv`, `target/run_results.json` (dbt artifacts), `config/pipeline_map.yaml`
**Outputs:** `PIPELINE_FAILURE_FOUND` events, `APPROVAL_REQUIRED` events (for High-risk remediation), `RootCauseReport` objects

**Failure classification (in priority order):**

| Type | Detection rule |
|---|---|
| `hard_failure` | A run record with `status == "failed"` exists in the lookback window |
| `silent_drop` | Any run has rows_affected < 70% of rolling median |
| `schema_drift` | `schema_version` changes across runs |
| `gap` | Time gap > 24h between consecutive run timestamps |
| `unknown` | No pattern detected |

**Remediation options per failure type:**

| Failure | Action | Risk | Auto-executable |
|---|---|---|---|
| silent_drop | incremental refresh | Low | Yes |
| silent_drop | full rebuild | Medium | No |
| schema_drift | schema_patch + re-run | High | No |
| gap | re-run missed loads | Low | Yes |
| hard_failure | full rebuild after source fix | High | No |

High-risk remediations emit `APPROVAL_REQUIRED` and are blocked until ASGC grants approval.

---

### 3.4 AVL — AI Value Ledger

**Responsibility:** Link every injection and correction to business outcomes. Produce CFO-ready exposure estimates with EU AI Act risk tagging.

**File:** `modules/avl/value_ledger.py`

**Inputs:** `CONTEXT_INJECTED`, `PIPELINE_FAILURE_FOUND`, `CORRECTION_APPLIED` events; `data/business_outcomes.csv`, `data/dksm/decision_log.csv`
**Outputs:** `VALUE_CALCULATED` events, `ExposureReport` / `RecoveryReport` objects, PDF CFO report (ReportLab)

**Exposure formula:**
```
total_decisions      = decisions in failure_period_days
estimated_bad        = total_decisions × error_rate_assumption (config: 0.15)
financial_exposure   = estimated_bad × avg_decision_value_usd (config: $50,000)
recovery_potential   = financial_exposure × 0.65
```

**Outcome linkage:** Joins `lci_log.csv` timestamps with `business_outcomes.csv` rows within a 4-hour window. Outcomes inside this window are tagged as "used verified context," enabling before/after comparison.

**EU AI Act risk categories by domain:**

| Domain | Category |
|---|---|
| customer_segments | High |
| risk_thresholds | High |
| drug_formulary | High |
| coverage_limits | High |
| product_catalog | Limited |
| carrier_rates | Limited |
| coupons | Minimal |

---

### 3.5 FLE — Feedback Loop Engine

**Responsibility:** Capture human/agent corrections, classify the error type, and route to the correct upstream fix (Gold flag, ChromaDB reweight, or fine-tune pair generation).

**File:** `modules/fle/feedback_engine.py`

**Inputs:** `CONTEXT_INJECTED`, `CORRECTION_RECEIVED` events; direct calls from dashboard / MCP tool `submit_correction`
**Outputs:** `CORRECTION_RECEIVED`, `CORRECTION_APPLIED`, `REPROBE_REQUESTED`, `APPROVAL_REQUIRED` events; `data/feedback_log.csv`, `data/fine_tune_pairs.jsonl`

**Error classification:**

| Type | Detection logic | Routing action |
|---|---|---|
| `THRESHOLD_ERROR` | Numeric values differ by ≥ 20% | flag_gold_and_reprobe |
| `DEFINITION_ERROR` | Token overlap < 30% (semantic mismatch) | flag_domain_definition; generate fine-tune pair if ≥ 10 repeat errors |
| `RETRIEVAL_ERROR` | Token overlap 30-100% (right concept, wrong value) | reweight_chroma (boost = 1.5) |
| `CLASSIFICATION_ERROR` | Fallback | update_gold_and_reprobe |

After `apply_routing()` executes:
1. Gold layer CSV is updated with `fle_flagged=True` and the correct value
2. ChromaDB entity boost is applied (if retrieval error)
3. Fine-tune pair is appended to JSONL (if definition error threshold reached)
4. `CORRECTION_APPLIED` + `REPROBE_REQUESTED` are emitted so DKSM re-scores

---

### 3.6 ASGC — AI Stack Governance Console

**Responsibility:** Own the approval queue for all high-risk actions, expose the causal chain view, generate board-level PDF reports.

**File:** `modules/asgc/governance_console.py`

**Inputs:** `APPROVAL_REQUIRED`, `VALUE_CALCULATED` events (all CRITICAL-severity events auto-set `requires_approval=True` in the bus)
**Outputs:** `APPROVAL_GRANTED`, `APPROVAL_REJECTED` events; `data/approval_queue.csv`, PDF board report

**Approval lifecycle:**
```
Any module emits APPROVAL_REQUIRED
        ↓
ASGC.queue_for_approval() → writes REQ-{uuid} to approval_queue.csv (status=pending)
        ↓
Lead reviews in dashboard Page 6 or via API
        ↓
approve(request_id, lead_name) → status=approved, emits APPROVAL_GRANTED
   OR
reject(request_id, lead_name, reason) → status=rejected, emits APPROVAL_REJECTED
```

**Causal chain:** `get_causal_chain(domain, entity, hours_back)` replays the event bus for that domain/entity window and builds a human-readable narrative showing the full STALENESS → INJECTION → PIPELINE → EXPOSURE → CORRECTION sequence.

---

## 4. Event Bus

**Implementation:** `core/event_bus.py`
**Persistence:** `data/aria_event_bus.jsonl` — append-only, never deleted
**Singleton:** `get_bus()` returns the shared instance; all modules import only this function (no cross-module imports)

**Auto-approval escalation:** Any event emitted with `severity="CRITICAL"` automatically sets `requires_approval=True`, which triggers all `APPROVAL_REQUIRED` handlers in addition to the event's own handlers.

### ARIAEvent schema

| Field | Type | Notes |
|---|---|---|
| event_id | str (UUID4) | auto-generated |
| timestamp | datetime (UTC) | auto-generated |
| source_module | str | DKSM \| LCI \| PP \| AVL \| FLE \| ASGC |
| event_type | str | one of 13 valid types below |
| domain | str | e.g. customer_segments |
| entity | str \| None | e.g. Enterprise |
| payload | dict | event-specific data |
| severity | str | INFO \| WARNING \| CRITICAL |
| requires_approval | bool | True if CRITICAL or set explicitly |

### Event type catalogue

| Event Type | Emitted by | Consumed by | Description |
|---|---|---|---|
| `STALENESS_DETECTED` | DKSM | LCI, PP | LLM knowledge diverges from Gold layer; payload includes `level`, `sim`, `days_since_update` |
| `CONTEXT_INJECTED` | LCI | AVL, FLE | Verified Gold context block has been prepared for injection into a prompt |
| `PIPELINE_FAILURE_FOUND` | PP | AVL | Root cause dbt failure identified; payload includes `failure_type`, `days_since`, `dbt_model` |
| `VALUE_CALCULATED` | AVL | ASGC | Financial exposure and recovery potential computed for a domain |
| `CORRECTION_RECEIVED` | FLE | FLE (self) | A correction signal met the confidence threshold (≥ 0.7) and is ready to classify |
| `CORRECTION_APPLIED` | FLE | AVL | Correction has been executed: Gold flagged, ChromaDB reweighted, and/or fine-tune pair written |
| `APPROVAL_REQUIRED` | PP, FLE, ASGC, EventBus | ASGC | A high-risk action requires human sign-off before execution |
| `APPROVAL_GRANTED` | ASGC | requesting module | Lead has approved a queued action |
| `APPROVAL_REJECTED` | ASGC | requesting module | Lead has rejected a queued action |
| `REPROBE_REQUESTED` | FLE | DKSM | Correction applied — DKSM should re-score the domain/entity |
| `LEARNING_IMPROVEMENT` | FLE | ASGC | Learning velocity metric updated (reserved for future use) |
| `EXPIRY_ALERT` | DKSM | ASGC | A domain entity's data contract or Gold record has passed its expiry date |
| `DATA_CONTRACT_EXPIRY` | DKSM | ASGC | Formal data contract expiry for a domain (used by the freshness scheduler) |

---

## 5. Data Flow Sequence

```
1. FreshnessScheduler tick (hourly/daily/weekly)
   └─► DomainProber.probe_single_with_crag(question, domain)
         ├─ [1a] MedallionVectorStore.hybrid_search() → Gold context
         ├─ [1b] Claude API call (probe model: claude-sonnet-4-6)
         ├─ [1c] CRAGValidator.grade_extraction() (grader: claude-haiku-4-5)
         │       ├─ CORRECT → extract value
         │       ├─ AMBIGUOUS → generate_retry_question() → retry (max 2)
         │       └─ INCORRECT → fallback to vector store value
         └─ StalenessScorer.score(model_belief, warehouse_truth, effective_date)
               └─► emit STALENESS_DETECTED {level, sim, days_since_update}

2. LCI receives STALENESS_DETECTED
   └─► trigger_injection_readiness()
         ├─ read Gold CSV for entity
         ├─ cache entity → value with 4h TTL
         └─► emit CONTEXT_INJECTED {status=ready, value, version}

3. PP receives STALENESS_DETECTED
   └─► trace_root_cause(domain, entity, staleness_days)
         ├─ load pipeline_log.csv + dbt run_results.json
         ├─ detect failure type (hard_failure / silent_drop / schema_drift / gap)
         └─► emit PIPELINE_FAILURE_FOUND {dbt_model, failure_type, days_since}
               (if risk=High) └─► emit APPROVAL_REQUIRED

4. AVL receives PIPELINE_FAILURE_FOUND
   └─► calculate_exposure(domain, failure_period_days)
         ├─ count decisions in window (from business_outcomes.csv)
         ├─ apply error_rate × avg_decision_value
         └─► emit VALUE_CALCULATED {exposure_usd, bad_decisions, eu_ai_act, recovery_potential}

5. Agent / user calls inject_and_prompt(query, domain)
   └─► LCI.inject() → build context_block from pending injection
         └─► Claude API call with grounded prompt
               └─► emit CONTEXT_INJECTED {injection_id, injected_value}

6. User / agent submits correction via dashboard or MCP submit_correction
   └─► FLE.capture_signal() → emit CORRECTION_RECEIVED
         └─► FLE.classify_and_route()
               ├─ classify error type
               ├─ (if approval needed) emit APPROVAL_REQUIRED
               └─► FLE.apply_routing() (after approval or auto-execute)
                     ├─ flag Gold CSV (fle_flagged=True)
                     ├─ ChromaDB.boost_entity() (if RETRIEVAL_ERROR)
                     ├─ append to fine_tune_pairs.jsonl (if DEFINITION_ERROR × ≥ 10)
                     ├─► emit CORRECTION_APPLIED
                     └─► emit REPROBE_REQUESTED → DKSM re-scores domain

7. ASGC manages all APPROVAL_REQUIRED events
   └─► queue_for_approval() → approval_queue.csv (status=pending)
         └─► Lead: approve(request_id) → emit APPROVAL_GRANTED
              OR   reject(request_id)  → emit APPROVAL_REJECTED
```

---

## 6. Data Architecture

### Medallion Layers

All data is stored as CSV files. The pipeline runs Bronze → Silver → Gold through `medallion_pipeline.py`.

| Layer | Location | Role | Key property |
|---|---|---|---|
| Bronze | `data/dksm/bronze_layer/` | Raw ingestion, no validation | append-only |
| Silver | `data/dksm/silver_layer/` | Cleaned, validated, deduplicated | quality_score added |
| Gold | `data/dksm/gold_layer/` | Current canonical truth | `is_current=True` records only |

**Gold layer columns (example for customer_segments):**

| segment_name | current_arr_usd | effective_date | version | is_current | fle_flagged | fle_correct_value |
|---|---|---|---|---|---|---|
| Enterprise | 85000 | 2024-01-01 | 2.1 | True | False | |

`fle_flagged` and `fle_correct_value` are written by FLE when a correction is applied.

### ChromaDB Collections

One persistent ChromaDB client at `data/chroma_db/`. Collections per domain/layer:

| Collection name | Contents |
|---|---|
| `gold_{domain}` | Current Gold records (is_current=True) |
| `gold_{domain}_history` | All Gold versions |
| `silver_{domain}` | Silver layer documents |
| `bronze_{domain}` | Bronze layer (lineage queries) |
| `probe_history` | All past CRAG probe results (drift detection) |

**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2` (local, no API cost)
**Search:** Hybrid — dense cosine similarity (ChromaDB) + BM25 re-ranking (`rank-bm25`); combined_score = weighted sum

### Operational CSVs / JSONL

| File | Written by | Consumed by |
|---|---|---|
| `data/aria_event_bus.jsonl` | EventBus | ASGC, dashboard |
| `data/lci_log.csv` | LCI | AVL, dashboard |
| `data/pipeline_log.csv` | PP (simulated runs) | PP |
| `data/business_outcomes.csv` | data_simulator | AVL |
| `data/feedback_log.csv` | FLE | FLE, dashboard |
| `data/fine_tune_pairs.jsonl` | FLE | external fine-tuning |
| `data/approval_queue.csv` | ASGC | ASGC, dashboard |
| `data/probe_cache/crag_log.csv` | DomainProber | dashboard |
| `data/freshness_schedule_log.csv` | FreshnessScheduler | dashboard |

### Config Files

| File | Contents |
|---|---|
| `config/aria_config.yaml` | Global settings, module enable flags, TTLs, thresholds, install_id |
| `config/dksm/domains.yaml` | Per-domain probe questions, staleness thresholds, medallion path config |
| `config/pipeline_map.yaml` | dbt model → domain → owner mappings |

---

## 7. MCP Integration

**Server:** `mcp/aria_mcp_server.py`
**Transport:** SSE (Server-Sent Events) via FastMCP
**Port:** 8765 (configurable via `ARIA_MCP_PORT`)
**Auth:** Bearer token middleware (`ARIA_MCP_TOKEN` env var); if unset, server is unauthenticated (local dev warning logged)

### Tool catalogue

| # | Tool | Module | Description |
|---|---|---|---|
| 1 | `check_staleness` | DKSM | Probe entity knowledge, return FRESH/STALE/CRITICAL + score + remediation hint; emits `STALENESS_DETECTED` |
| 2 | `search_gold_layer` | DKSM | Hybrid semantic search over Gold records; returns top-k with scores |
| 3 | `inject_context` | LCI | Fetch and return the context_block for a domain; caller prepends it to their prompt |
| 4 | `inject_and_prompt` | LCI | Full middleware: inject Gold context + call Claude + return response |
| 5 | `get_pipeline_health` | PP | Root cause failure summary for all or one mapped domain |
| 6 | `get_value_summary` | AVL | Exposure identified, value recovered, ROI by domain for a lookback window |
| 7 | `submit_correction` | FLE | Capture a correction signal (user_correction / agent_override / escalation / non_use) |
| 8 | `get_causal_chain` | ASGC | Full event narrative for a domain/entity over a time window |
| 9 | `get_stack_health` | ASGC | Status of all 6 modules; useful as agent pre-flight check |

**Typical agent usage pattern:**
```
1. get_stack_health()                          # pre-flight: any modules degraded?
2. check_staleness(domain, entity)             # is this domain safe to query?
3. inject_and_prompt(query, domain, entity)    # call LLM with verified context
4. submit_correction(domain, entity, ...)      # if agent detects a mistake
```

---

## 8. Key Design Decisions

### 8.0 Why Not Just Update the RAG Index?

A reasonable objection: "If the retrieval index is stale, just re-index it."

The answer is that re-indexing is **batch**, not **real-time**:

- Production RAG pipelines re-index nightly or weekly. Some run monthly because re-embedding a million documents costs real money and CPU/GPU time.
- Between two re-index runs, every wrong answer the AI gives is invisible — until a customer complains, a regulator audits, or a CFO notices the revenue gap.
- Even when re-indexing runs, *which* domain was actually stale and *why* it went stale is not recorded anywhere. Operators discover the failure after the damage is done.

ARIA is a **real-time check layer on top of whatever retrieval system already exists**. It is not a replacement for RAG. It does not rebuild your embeddings. It does not own your knowledge base.

What it does:

1. Probes the live retrieval index with CRAG-style queries and compares the answer against the Gold layer
2. Scores divergence (FRESH / STALE / CRITICAL) per entity, per domain
3. On STALE/CRITICAL, fetches the verified value and injects it into the prompt for the *next* call — the customer never sees the wrong answer
4. Fires a `REPROBE_REQUESTED` event so the existing RAG pipeline can re-index the affected domain on its normal schedule
5. Records the full causal chain (which Gold record drifted, which dbt run caused it, which injections fixed it) for audit

ARIA and the RAG pipeline are complementary: RAG owns retrieval, ARIA owns freshness.

### 8.1 Event bus over direct imports

No module imports another module. All coordination is through `get_bus()`. This enforces:
- **Testability:** each module can be unit-tested with a mock bus
- **Fault isolation:** a crash in AVL does not affect DKSM or LCI
- **Audit trail:** every coordination step is persisted to `aria_event_bus.jsonl`, enabling full replay and causal chain reconstruction
- **Extensibility:** new modules subscribe to existing event types without modifying producers

The tradeoff is that event payloads must be serializable dicts, which limits type safety between modules.

### 8.2 Medallion architecture for data

Bronze/Silver/Gold layers are deliberately separated because:
- **Gold is the single source of truth** for LCI context injection; it must be clean, validated, and versioned
- **Silver provides lineage** — auditors can trace exactly how a Gold value was derived
- **Bronze enables reprocessing** — if validation rules change, raw data can be re-promoted without re-ingestion
- All three layers are indexed in ChromaDB, allowing cross-layer lineage queries

Using CSVs (not a real database) is a deliberate simplicity choice for the MVP. Each Gold CSV has at most hundreds of rows; pandas reads are fast enough for the probe cadence.

### 8.3 4-hour TTL for LCI injections

The 4-hour window balances freshness against API call overhead:
- Most enterprise Gold layer values are updated once per business day
- A 4-hour TTL means an injection pre-fetched at 9am covers morning decision-making without redundant re-fetches
- The 4-hour window is also used by AVL to attribute business outcomes to a specific injection (causality window)
- Configurable per-deployment via `lci.max_injection_age_hours` in `aria_config.yaml`

### 8.4 CRAG probe loop

The CRAG (Corrective RAG) loop prevents ARIA from mis-classifying ambiguous LLM responses as definitive beliefs:
- **Grader model is Haiku** (cheapest) — grading is a classification task, not a reasoning task; Haiku is sufficient and keeps probe costs low
- **Max 2 retries by default** — empirically, a rephrased question resolves most ambiguity in 1 retry; 3+ retries yield diminishing returns and increase cost
- **Vector store fallback** — on INCORRECT or exhausted retries, the Gold layer value is used as the extracted value rather than returning no data, ensuring the scorer always has a comparand

### 8.5 Staleness score formula

`staleness_score = divergence × (1 + 0.3 × time_decay)` rather than just divergence:
- A value that is semantically similar but very old (e.g., a price from 2 years ago) should score higher staleness than a moderately-different but recent value
- The 0.3 multiplier caps the time-decay amplification at 30% (so a perfect score can still decay to 1.3× divergence at max age)
- The 180-day half-life matches typical enterprise data contract refresh cadences

### 8.6 Approval gating for high-risk actions

CRITICAL-severity events and High-risk remediation actions require explicit lead approval before execution. This design:
- Ensures ARIA never autonomously modifies production Gold data or triggers full dbt rebuilds
- Provides an audit trail (approval_queue.csv with decided_by + decided_at)
- Satisfies EU AI Act requirements for human oversight in High-risk AI decisions

---

## 9. Testing Strategy

### Unit tests (existing)

| Suite | What it tests |
|---|---|
| `test_event_bus.py` | Event emission, subscription, JSONL persistence, event ordering |
| `test_lci.py` | Context injection, TTL expiry, pending state management |
| `test_pp.py` | Pipeline scan, health summary, remediation approval gate (low vs high risk) |
| `test_avl.py` | Exposure reports, recovery value, CFO value summary keys, domain coverage |
| `test_fle.py` | Feedback routing decisions, correction signal handling |

### User flow tests (18 tests)

`tests/test_user_flows.py` tests the system as real users interact with it — no Streamlit rendering, tests the module layer the dashboard drives:

**Flow 1 — Analyst: Staleness Scores**
- All configured domains load without gaps
- FRESH classification when AI belief matches Gold value
- CRITICAL classification when belief is completely wrong

**Flow 2 — Operator: Pipeline Trace & Remediation**
- Scan returns at least one report with actionable fields
- Health summary exposes `failures_found` and `total_domains`
- Low-risk fixes execute immediately without approval
- High-risk fixes are blocked until a named approver is provided

**Flow 3 — System: Context Injection**
- No injection fires without a prior staleness signal
- Correct Gold value injected after staleness is registered
- Expired context (past TTL) is never injected

**Flow 4 — CFO: Financial Exposure & Recovery**
- Exposure figures are always non-negative
- EU AI Act categories are always one of: Minimal / Limited / High / Unacceptable
- Recovery value is non-negative after a correction
- CFO summary exposes all three headline metrics
- `net_aria_value_usd` never exceeds `total_recovered_usd`
- ROI breakdown has at least one domain entry

### Running all tests

```bash
pytest tests/ -v                    # all 23 tests
pytest tests/test_user_flows.py -v  # user flows only (18 tests)
```

**Total: 23 tests (5 unit + 18 user flows)**

> Phase 2 will add `test_prompt_coverage.py` (18 tests) — prompt coverage analyzer covering edge case generation, gap detection, and suggested probes across all 7 domains.

---

## 10. Known Limitations / Production Gaps

| Gap | Impact | Mitigation / Path to fix |
|---|---|---|
| CSV file storage | Not safe for concurrent writes from multiple processes | Replace with SQLite (single-process) or PostgreSQL (multi-process) |
| In-memory LCI pending dict | Injections lost on process restart; no persistence | Persist pending injections to SQLite or Redis |
| Single-process event bus | `EventBus._handlers` is in-memory; subscriptions are lost on restart | Move to a real message broker (Redis Streams, Kafka) for production |
| Event bus full-scan on every query | `_read_all()` reads the entire JSONL file on every `get_chain()` / `recent()` call | Add time-partitioned files or an index; or write to a DB |
| Simulated financial data | `business_outcomes.csv` is generated by `core/data_simulator.py`; exposure figures are estimates | Connect to real decision log from the enterprise BI layer |
| No real dbt artifacts | PP reads `target/run_results.json` if present; otherwise uses simulated `pipeline_log.csv` | Wire up real dbt Cloud webhook or Airflow metadata DB |
| ChromaDB single-node | No replication or backup | Use ChromaDB Cloud or migrate to a managed vector DB for production |
| Fine-tune pairs not consumed | `fine_tune_pairs.jsonl` is written but not automatically submitted to any fine-tuning pipeline | Build a scheduled job that submits JSONL batches to Anthropic or another provider's fine-tuning API |
| REPROBE_REQUESTED not auto-triggered | FLE emits `REPROBE_REQUESTED` but no handler in DKSM picks it up automatically | Wire `FreshnessScheduler` to subscribe and trigger `run_domain_now()` on receipt |
| No TLS on MCP server | Bearer token auth exists but transport is plain HTTP | Add TLS termination via reverse proxy (nginx / Caddy) in front of uvicorn |
| Demo mode default | `demo_mode=True` in `aria_config.yaml` suppresses real Claude API calls in `inject_and_prompt` | Set `demo_mode: false` and ensure `ANTHROPIC_API_KEY` is set in the deployment environment |
| No alerting integration | `core/notifier.py` exists but is not wired to PagerDuty/Slack/email | Implement notifier subscriptions to CRITICAL events and connect to alerting provider |
