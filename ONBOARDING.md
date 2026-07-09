# ARIA — Use Case Demo Guide

> Walk through two end-to-end scenarios on the live dashboard at **http://localhost:8501**

---

## Quickstart

```bash
git clone https://github.com/Itachi-0xAI/aria.git
cd aria
pip install -r requirements.txt
streamlit run aria.py    # → http://localhost:8501  (demo mode, no API key needed)
```

**Demo mode is on by default.** All 7 pages and both use cases run entirely on simulated data — no API key, no data warehouse, no dbt project required.

To enable live Anthropic API calls (CRAG probes + `inject_and_prompt()`):
```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
# config/aria_config.yaml
demo_mode: false
```

---

## What's Running Right Now (vs What Needs Real Data)

| Capability | Demo mode | With real data wired |
|-----------|-----------|---------------------|
| Staleness scoring (DKSM) | ✅ Simulated Gold CSVs | ✅ Replace CSVs with Snowflake/BigQuery query |
| Context injection (LCI) | ✅ Builds context block | ✅ + calls Claude API (`inject_and_prompt()`) |
| Pipeline root cause (PP) | ✅ Reads `pipeline_log.csv` | ✅ + reads live `target/run_results.json` |
| Dollar traceability (AVL) | ⚠️ Simulated outcomes | ⚠️ Needs real CRM/claims data in `business_outcomes.csv` |
| Correction routing (FLE) | ✅ Full routing + Chroma boost + reprobe | ✅ (no change needed) |
| Governance approvals (ASGC) | ✅ Full queue + board report | ✅ (no change needed) |
| Weekly self-improvement | ✅ Emits `LEARNING_IMPROVEMENT` events | ✅ (no change needed) |
| MCP server (9 tools) | ✅ All tools work in demo mode | ✅ (no change needed) |
| Dashboard auth | ❌ No login | ❌ Phase 3 gap — see ROADMAP.md |
| Real data warehouse | ❌ Local CSVs only | Phase 1 — Snowflake / BigQuery connector |
| Fine-tuning execution | ❌ Pairs written, never sent | Phase 2 — once Anthropic fine-tuning API opens |

> **→ See [ROADMAP.md](ROADMAP.md)** for the full 3-phase production plan.

---

## Use Case 1 — Financial Services: Enterprise Tier Misclassification

**Scenario:** AI believes Enterprise minimum revenue = $6M. Gold layer says $7.5M. 718 days stale. Pipeline `fct_customer_segments` had a silent row-count drop. $1,800,000 financial exposure. EU AI Act: **HIGH risk**.

### Step-by-step Navigation

| Step | Page (sidebar) | Where to look | What you see |
|------|---------------|--------------|-------------|
| **①** | **Command Center** | Event timeline (middle panel) | `STALENESS_DETECTED · Enterprise · Financial_Services · CRITICAL` — red card |
| **②** | **DKSM** | Staleness bar chart (top) | `customer_segments` bar fills red — CRITICAL badge · "127 days since update" |
| **③** | **DKSM** | Entity deep-dive card + Stale Action Queue | Belief `$6,000,000` vs Truth `$7,500,000` · `UPDATE` chip queued |
| **④** | **LCI + PP** | Active injections table + Pipeline failures table | Injection: `Enterprise · $7.5M · v3 · 4h TTL` · Failure: `fct_customer_segments · silent_drop · 55d` |
| **⑤** | **AVL** | Domain selector table (left) → click **Customer Segments** row | Exposure card `$1,800,000` · Red bar `$6M` → Green bar `$7.5M` · EU AI Act `HIGH` · ROI multiplier |
| **⑥** | **ASGC** | Approval queue tab | `AQ-0001 · flag_gold_and_reprobe · Medium · pending` → click **Approve** |

### Event chain produced

```
DKSM → STALENESS_DETECTED   {level: CRITICAL, sim: 0.80, days_since: 718}
LCI  → CONTEXT_INJECTED     {entity: Enterprise, value: 7_500_000, version: v3}
PP   → PIPELINE_FAILURE     {model: fct_customer_segments, type: silent_drop}
AVL  → VALUE_CALCULATED     {exposure_usd: 1_800_000, eu_ai_act: High}
FLE  → CORRECTION_RECEIVED  {signal_type: agent_override, wrong: 6_000_000}
FLE  → REPROBE_REQUESTED    {triggered_by: correction_applied}
ASGC → APPROVAL_REQUIRED    {action: flag_gold_and_reprobe, risk: Medium}
ASGC → APPROVAL_GRANTED     {approved_by: Lead}
FLE  → CORRECTION_APPLIED   {actions: [Gold flagged, chroma boosted, reprobe triggered]}
```

---

## Use Case 2 — Healthcare: Humira Formulary Expiry + Staleness

**Scenario:** AI quotes Humira Biosimilar copay at $90. Formulary policy updated — biosimilar preferred copay is now $45. Pipeline `fct_drug_formulary` had a silent drop 87 days ago. Policy contract expires in 42 days. $180,000 claim exposure. EU AI Act: **HIGH risk**.

### Step-by-step Navigation

| Step | Page (sidebar) | Where to look | What you see |
|------|---------------|--------------|-------------|
| **①** | **Command Center** | Expiry Alerts card + event timeline | `EXPIRY_ALERT · Humira Biosimilar · Healthcare · 42 days` + `STALENESS_DETECTED · CRITICAL` |
| **②** | **DKSM** | Stale Action Queue (bottom of page) | Humira row · expiry `2026-06-30` · `UPDATE` chip · Healthcare tag |
| **③** | **DKSM** | Entity deep-dive (select `drug_formulary` domain) | Belief `$90` vs Truth `$45` · 87 days stale · CRITICAL badge |
| **④** | **LCI + PP** | Active injections table + Pipeline failures table | Injection: `Humira Biosimilar · $45 · v3 · 4h TTL` · Failure: `fct_drug_formulary · silent_drop · 87d` |
| **⑤** | **AVL** | Domain selector table (left) → click **Drug Formulary** row | Exposure card `$180,000` · Red bar `$90` → Green bar `$45` · EU AI Act `HIGH` · ROI multiplier |
| **⑥** | **ASGC** | Approval queue tab | `AQ-0002 · remove_formulary_record · Medium · pending` → click **Approve** |

### Event chain produced

```
DKSM → EXPIRY_ALERT         {entity: Humira Biosimilar, days_until_expiry: 42}
DKSM → STALENESS_DETECTED   {level: CRITICAL, sim: 0.50, days_since: 87}
LCI  → CONTEXT_INJECTED     {entity: Humira Biosimilar, value: 45, version: v3}
PP   → PIPELINE_FAILURE     {model: fct_drug_formulary, type: silent_drop}
AVL  → VALUE_CALCULATED     {exposure_usd: 180_000, eu_ai_act: High}
FLE  → CORRECTION_RECEIVED  {signal_type: user_correction, wrong: 90}
FLE  → REPROBE_REQUESTED    {triggered_by: correction_applied}
ASGC → APPROVAL_REQUIRED    {action: remove_formulary_record, risk: Medium}
ASGC → APPROVAL_GRANTED     {approved_by: Lead}
FLE  → CORRECTION_APPLIED   {actions: [Gold flagged, chroma boosted, reprobe triggered]}
```

---

## All 7 Monitored Domains

| Domain | Vertical | EU AI Act | Failure in demo | Exposure |
|--------|----------|-----------|-----------------|---------|
| `customer_segments` | Financial Services | 🔴 High | silent_drop (55d) | $1,800,000 |
| `risk_thresholds` | Financial Services | 🔴 High | hard_failure (156d) | $560,000 |
| `coverage_limits` | Insurance | 🔴 High | schema_drift (134d) | $646,000 |
| `drug_formulary` | Healthcare | 🔴 High | silent_drop (87d) | $180,000 |
| `product_catalog` | Retail | 🟡 Limited | schema_drift (90d) | $420,000 |
| `carrier_rates` | Logistics | 🟡 Limited | gap (179d) | $99,200 |
| `coupons` | Retail | 🟢 Minimal | silent_drop (113d) | $24,360 |

---

## Remaining Gaps & Production Path

Three gaps matter most before a production deployment:

| Gap | Impact | When it's closed |
|-----|--------|-----------------|
| No dashboard login | Anyone with the URL can access | Phase 3 — add `streamlit-authenticator` or nginx + OAuth2-proxy |
| Gold layer = local CSVs | Can't query live warehouse data | Phase 1 — add Snowflake / BigQuery connector |
| No real decision log | AVL dollar values are simulated | Phase 1 — wire `business_outcomes.csv` to Salesforce / CRM |

For the full system architecture, event bus schema, and design decisions: **[SYSTEM_DESIGN.md](SYSTEM_DESIGN.md)**

---

## ARIA + CoAgent for High-Risk Approvals

By default the ASGC approval queue is a single click. For high-stakes remediations (full re-index, Gold layer modification, schema patch) you can route the decision through [CoAgent](https://github.com/Itachi-0xAI/coagent)'s DEBATE mode:

```python
# On ASGC APPROVAL_REQUIRED event
from coagent import CollabSession, modes

session = CollabSession(mode=modes.DEBATE)
session.add_agent(name="Re-inject Now",   role="advocate",
    system_prompt="Argue for immediate context injection despite stale pipeline")
session.add_agent(name="Quarantine First", role="advocate",
    system_prompt="Argue for halting AI responses on this domain until pipeline fixed")
session.add_human(name="Data Lead")
session.start()
# DecisionRecord persisted alongside the ASGC event for the audit trail
```

See [STACK.md](STACK.md) for the full three-tool integration pattern.
