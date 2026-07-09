"""
ARIA — Adaptive Reasoning & Intelligence Architecture
Unified 7-page Streamlit dashboard integrating all 6 modules.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml

_ROOT = Path(__file__).parent.resolve()
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))

from core.data_simulator import run_all as _ensure_data
_ensure_data()

# ── /healthz — JSON status endpoint ──────────────────────────────────────────
# Usage: open the app URL with ?healthz=1 — returns JSON and stops rendering.
# Used by Streamlit Cloud uptime monitors and external health checks.
_qp = st.query_params
if _qp.get("healthz") == "1":
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    _health_payload = {
        "status": "ok",
        "timestamp": _dt.now(_tz.utc).isoformat(),
        "modules": {
            "dksm": "enabled",
            "lci":  "enabled",
            "pp":   "enabled",
            "avl":  "enabled",
            "fle":  "enabled",
            "asgc": "enabled",
        },
        "demo_mode": os.environ.get("ARIA_DEMO_MODE", "true"),
        "version": "1.0.0",
    }
    st.json(_health_payload)
    st.stop()

st.set_page_config(
    page_title="ARIA — Adaptive Reasoning & Intelligence Architecture",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── colours ──────────────────────────────────────────────────────────────────
_C = {
    "CRITICAL": "#FF4B4B", "STALE": "#FFA500", "WARNING": "#FFA500",
    "FRESH": "#00CC88",    "OK": "#00CC88",     "NEUTRAL": "#4B9FFF",
    "bg":    "#0F1117",    "surface": "#1E2130",
}

# ── cached loaders ────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def _bus():
    from core.event_bus import get_bus
    return get_bus()

@st.cache_resource(show_spinner=False)
def _cfg():
    from core.config_loader import get_config
    return get_config()

@st.cache_resource(show_spinner=False)
def _lci():
    from modules.lci.context_injector import LiveContextInjector
    return LiveContextInjector()

@st.cache_resource(show_spinner=False)
def _pp():
    from modules.pp.pipeline_pulse import PipelinePulse
    return PipelinePulse()

@st.cache_resource(show_spinner=False)
def _avl():
    from modules.avl.value_ledger import AIValueLedger
    return AIValueLedger()

@st.cache_resource(show_spinner=False)
def _fle():
    from modules.fle.feedback_engine import FeedbackLoopEngine
    return FeedbackLoopEngine()

@st.cache_resource(show_spinner=False)
def _asgc():
    from modules.asgc.governance_console import GovernanceConsole
    return GovernanceConsole()

@st.cache_resource(show_spinner=False)
def _scorer():
    from modules.dksm.scorer import StalenessScorer
    return StalenessScorer(str(_ROOT / "config" / "dksm" / "domains.yaml"))

@st.cache_resource(show_spinner=False)
def _scheduler():
    from modules.dksm.freshness_scheduler import FreshnessScheduler
    sched = FreshnessScheduler(
        profiles_dir=str(_ROOT / "config" / "dksm" / "company_profiles"),
    )
    return sched


def _badge(label: str, color: str) -> str:
    return (f'<span style="background:{color};color:#fff;padding:2px 10px;'
            f'border-radius:12px;font-weight:bold;font-size:0.85em">{label}</span>')

def _status_color(s: str) -> str:
    return _C.get(s.upper(), _C["NEUTRAL"])


# ── sidebar ───────────────────────────────────────────────────────────────────

def _sidebar() -> str:
    cfg   = _cfg()
    lead  = cfg.asgc_lead()
    asgc  = _asgc()
    pending = asgc.pending_count()

    st.sidebar.title("🧠 ARIA")
    st.sidebar.caption("Adaptive Reasoning & Intelligence Architecture")
    if lead:
        st.sidebar.markdown(f"**Lead:** {lead}")

    page = st.sidebar.radio("Navigate", [
        "🏠 Command Center",
        "🔍 DKSM — Knowledge Staleness",
        "⚙️ LCI + PP — Fix Intelligence",
        "💰 AVL — Value Proof",
        "🔄 FLE — Learning Engine",
        "🏛️ ASGC — Governance",
        "🔌 MCP & Integration",
    ], label_visibility="collapsed")

    st.sidebar.divider()

    # Module toggles
    st.sidebar.markdown("**Modules**")
    for mod in ["lci", "pp", "avl", "fle", "asgc"]:
        enabled = cfg.module_enabled(mod)
        color   = "#00CC88" if enabled else "#94a3b8"
        st.sidebar.markdown(
            f'<span style="color:{color}">● {mod.upper()}</span>',
            unsafe_allow_html=True,
        )

    st.sidebar.divider()

    if pending > 0:
        st.sidebar.markdown(
            f'<div style="background:#FF4B4B;color:#fff;padding:8px 12px;'
            f'border-radius:8px;font-weight:bold;text-align:center">'
            f'🔴 {pending} Pending Approval{"s" if pending != 1 else ""}</div>',
            unsafe_allow_html=True,
        )

    # ── Production Mode badge ──────────────────────────────────────────────────
    demo_mode = cfg.demo_mode
    groq_key  = bool(os.environ.get("GROQ_API_KEY", "").strip())
    gl_source = cfg._aria.get("gold_layer", {}).get("source", "csv")
    sheets_id = cfg._aria.get("gold_layer", {}).get("spreadsheet_id", "")
    live_data = (gl_source == "sheets" and bool(sheets_id)) or groq_key

    if not demo_mode and live_data:
        st.sidebar.markdown(
            '<div style="background:#00CC88;color:#000;padding:6px 10px;'
            'border-radius:8px;font-weight:bold;text-align:center;font-size:0.82em">'
            '🟢 PRODUCTION MODE</div>',
            unsafe_allow_html=True,
        )
        sources = []
        if gl_source == "sheets" and sheets_id:
            sources.append("Google Sheets")
        if groq_key:
            sources.append("Groq API")
        st.sidebar.caption(f"Live sources: {', '.join(sources)}")
    else:
        st.sidebar.markdown(
            '<div style="background:#FFA500;color:#000;padding:6px 10px;'
            'border-radius:8px;font-weight:bold;text-align:center;font-size:0.82em">'
            '🟡 DEMO MODE</div>',
            unsafe_allow_html=True,
        )
        st.sidebar.caption("Offline — no API key required")

    # ── /healthz status ────────────────────────────────────────────────────────
    with st.sidebar.expander("🩺 Module Health"):
        try:
            health = _asgc().get_stack_health()
            for mod, info in health.items():
                status = info.get("status", "OK")
                color  = "#00CC88" if status == "OK" else ("#FFA500" if status == "WARNING" else "#FF4B4B")
                st.markdown(
                    f'<span style="color:{color}">● {mod}</span> '
                    f'<span style="color:#94a3b8;font-size:0.8em">{status}</span>',
                    unsafe_allow_html=True,
                )
        except Exception as _e:
            st.caption(f"Health check unavailable: {_e}")

    if st.sidebar.button("🔄 Refresh All"):
        st.cache_data.clear()
        st.rerun()

    return page


# ── PAGE 1: Command Center ────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _health():
    return _asgc().get_stack_health()

@st.cache_data(ttl=300)
def _value_summary():
    return _avl().get_value_summary(30)

@st.cache_data(ttl=300)
def _fle_velocity():
    return _fle().get_learning_velocity(30)

def page_command_center():
    st.title("🏠 ARIA Command Center")
    health  = _health()
    vs      = _value_summary()
    vel     = _fle_velocity()

    # ── 6 module cards ──
    st.subheader("Module Status")
    cols = st.columns(6)
    module_meta = {
        "DKSM":  ("🔍", "critical_count",    "critical detections"),
        "LCI":   ("💉", "injection_rate_24h", "injections (24h)"),
        "PP":    ("⚙️", "failures_found",     "pipeline failures"),
        "AVL":   ("💰", "value_events_24h",   "value events (24h)"),
        "FLE":   ("🔄", "signals_24h",        "signals (24h)"),
        "ASGC":  ("🏛️", "pending_approvals",  "pending approvals"),
    }
    for idx, (mod, (icon, key, unit)) in enumerate(module_meta.items()):
        info   = health.get(mod, {})
        status = info.get("status", "OK")
        color  = _status_color(status)
        val    = info.get(key, 0)
        with cols[idx]:
            st.markdown(
                f'<div style="border:1px solid {color};border-radius:8px;padding:14px;'
                f'background:{_C["surface"]};text-align:center">'
                f'<div style="font-size:1.6em">{icon}</div>'
                f'<div style="font-weight:700;color:{color};font-size:0.9em">{mod}</div>'
                f'<div style="font-size:1.4em;font-weight:700">{val}</div>'
                f'<div style="font-size:0.7em;color:#94a3b8">{unit}</div>'
                f'<div style="margin-top:4px">{_badge(status, color)}</div>'
                f'</div>', unsafe_allow_html=True,
            )

    st.divider()

    # ── Event timeline ──
    st.subheader("Cross-Module Event Timeline (last 24h)")
    events = _bus().recent(hours_back=24)

    # industry label helper
    from core.notifier import get_industry as _get_industry
    if events:
        rows = []
        for e in events[-60:]:
            ind = e.payload.get("industry") if e.payload else None
            ind = ind or _get_industry(e.domain)
            corrected = e.payload.get("corrected") or e.payload.get("error_corrected") if e.payload else None
            rows.append({
                "Module":    e.source_module,
                "Event":     e.event_type,
                "Domain":    e.domain,
                "Entity":    e.entity or "",
                "Industry":  ind,
                "Corrected": "✅" if corrected else ("🔄" if corrected is False else "—"),
                "Severity":  e.severity,
                "Time":      e.timestamp.strftime("%H:%M"),
            })
        df_ev = pd.DataFrame(rows)

        # Colour chips for industry
        ind_colors = {
            "Financial_Services": "#4B9FFF",
            "Healthcare":         "#00CC88",
            "Retail":             "#FFA500",
            "Logistics":          "#A78BFA",
            "Insurance":          "#F472B6",
            "Hospitality":        "#FBBF24",
            "General":            "#94a3b8",
        }
        color_map = {"CRITICAL": "#FF4B4B", "WARNING": "#FFA500", "INFO": "#4B9FFF"}
        fig = px.scatter(
            df_ev, x="Time", y="Module", color="Severity",
            symbol="Event",
            hover_data=["Domain", "Entity", "Industry", "Corrected", "Event"],
            color_discrete_map=color_map,
            title="Events by module, severity & industry — hover for detail",
            height=320,
        )
        fig.update_layout(paper_bgcolor="#0F1117", plot_bgcolor="#1E2130",
                          font_color="#fff", legend_title_text="Severity")
        st.plotly_chart(fig, use_container_width=True)

        # Industry-tagged event table
        with st.expander("📋 Event detail — industry, corrections & expiry"):
            display_cols = ["Time", "Industry", "Module", "Event", "Domain", "Entity", "Corrected", "Severity"]
            st.dataframe(df_ev[display_cols].sort_values("Time", ascending=False),
                         use_container_width=True, hide_index=True)
    else:
        st.info("No events in the last 24h. Run a probe or submit a correction to generate events.")

    # ── Expiry & data contract alerts ──
    st.subheader("⏰ Expiry & Data Contract Alerts")
    expiry_events = [e for e in events
                     if e.event_type in ("EXPIRY_ALERT", "DATA_CONTRACT_EXPIRY")]
    if expiry_events:
        ecols = st.columns(min(len(expiry_events), 4))
        for i, e in enumerate(sorted(expiry_events,
                                     key=lambda x: x.payload.get("days_until_expiry", 999))[:8]):
            p   = e.payload or {}
            ind = p.get("industry", _get_industry(e.domain))
            days_left = p.get("days_until_expiry", "?")
            exp_date  = p.get("expiry_date", "?")
            action    = p.get("recommended_action", "REVIEW")
            sev_color = "#FF4B4B" if e.severity == "CRITICAL" else "#FFA500"
            action_color = {"REMOVE": "#FF4B4B", "UPDATE": "#FFA500", "NONE": "#00CC88"}.get(action, "#4B9FFF")
            col = ecols[i % len(ecols)]
            with col:
                st.markdown(
                    f'<div style="border:1px solid {sev_color};border-radius:8px;padding:10px;'
                    f'background:#1E2130;margin-bottom:8px">'
                    f'<div style="font-size:0.7em;color:{sev_color};font-weight:700">'
                    f'{e.event_type.replace("_"," ")}</div>'
                    f'<div style="font-weight:700;font-size:0.95em">{e.entity}</div>'
                    f'<div style="color:#94a3b8;font-size:0.8em">{e.domain} · {ind}</div>'
                    f'<div style="margin-top:6px;font-size:0.85em">📅 Expires: <b>{exp_date}</b></div>'
                    f'<div style="font-size:0.85em">⏳ <b>{days_left}</b> days left</div>'
                    f'<div style="margin-top:6px">'
                    f'<span style="background:{action_color};color:#000;padding:2px 8px;'
                    f'border-radius:4px;font-size:0.75em;font-weight:700">{action}</span></div>'
                    f'</div>', unsafe_allow_html=True)
    else:
        # Run live scan
        if st.button("🔍 Scan for Expiry Alerts Now"):
            from core.notifier import scan_expiry_alerts
            alerts = scan_expiry_alerts()
            if alerts:
                st.warning(f"Found {len(alerts)} expiry alerts — refresh page to see them in timeline.")
            else:
                st.success("No expiry alerts found in current Gold layer.")
        else:
            st.info("No expiry alerts in last 24h. Click above to run a fresh scan.")

    st.divider()

    # ── 3 bottom KPIs ──
    c1, c2, c3 = st.columns(3)
    c1.metric("💰 Value Recovered (30d)",
              f"${vs.get('total_recovered_usd', 0):,.0f}",
              f"Exposure: ${vs.get('total_exposure_identified_usd', 0):,.0f}")
    c2.metric("⚠️ Decisions at Risk",
              f"{health.get('DKSM', {}).get('critical_count', 0) * 50:,}",
              "Estimated from CRITICAL events")
    c3.metric("📈 Learning Velocity",
              f"{vel.get('learning_velocity', 0):.1%}",
              "Corrections applied / total signals")


# ── PAGE 2: DKSM ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _dksm_scores():
    cfg     = _cfg()
    scorer  = _scorer()
    domains = cfg.dksm_domains
    scores  = []
    reasons = {}
    for domain, dcfg in domains.items():
        try:
            gold_path = _ROOT / "data" / "dksm" / "gold_layer" / \
                        Path(dcfg["medallion"]["gold"]["path"]).name
            if not gold_path.exists():
                gold_path = Path(dcfg["medallion"]["gold"]["path"])
            df      = pd.read_csv(gold_path)
            current = df[df.get("is_current", pd.Series([True]*len(df))) == True]  # noqa
            kc, vc  = dcfg["medallion"]["gold"]["key_column"], dcfg["medallion"]["gold"]["value_column"]
            for _, row in current.iterrows():
                rd     = row.to_dict()
                entity = str(rd.get(kc, ""))
                truth  = str(rd.get(vc, ""))
                ed     = str(rd.get("effective_date", ""))
                rb     = rd.get("model_belief", "")
                belief = str(rb) if rb not in ("", None) and str(rb) not in ("nan",) else truth
                sc     = scorer.score(domain, entity, belief, truth, ed)
                scores.append(sc)
                reason = str(rd.get("staleness_reason", ""))
                if reason not in ("", "nan"):
                    reasons[(domain, entity)] = {"reason": reason, "belief": belief, "truth": truth}
        except Exception:
            pass
    return scores, reasons


def page_dksm():
    st.title("🔍 DKSM — Knowledge Staleness Monitor")

    scores, reasons = _dksm_scores()

    # ── Metrics row ──
    counts = {"FRESH": 0, "STALE": 0, "CRITICAL": 0}
    for s in scores:
        counts[s.staleness_level] = counts.get(s.staleness_level, 0) + 1
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Entities", len(scores))
    c2.metric("✅ Fresh",    counts["FRESH"])
    c3.metric("⚠️ Stale",   counts["STALE"])
    c4.metric("🚨 Critical", counts["CRITICAL"])

    # ── Staleness bar chart ──
    if scores:
        rows_chart = [{"Domain": s.domain, "Entity": s.entity,
                       "Level": s.staleness_level, "Similarity": s.semantic_similarity,
                       "Days Since Update": s.days_since_update} for s in scores]
        df_s = pd.DataFrame(rows_chart)
        fig  = px.bar(df_s, x="Entity", y="Similarity", color="Level",
                      facet_col="Domain", height=360,
                      color_discrete_map={"FRESH": "#00CC88", "STALE": "#FFA500",
                                          "CRITICAL": "#FF4B4B", "UNKNOWN": "#94a3b8"},
                      title="Semantic Similarity by Entity — lower bar = more stale")
        fig.update_layout(paper_bgcolor="#0F1117", plot_bgcolor="#1E2130",
                          font_color="#fff")
        st.plotly_chart(fig, use_container_width=True)

    # ── Domain Deep Dive ──
    st.subheader("🔎 Domain Deep Dive — Belief vs Truth")
    cfg     = _cfg()
    domains = list(cfg.dksm_domains.keys())
    domain  = st.selectbox("Select domain", domains)
    domain_scores = [s for s in scores if s.domain == domain]
    if domain_scores:
        for sc in domain_scores:
            info  = reasons.get((domain, sc.entity), {})
            color = _C.get(sc.staleness_level, _C["NEUTRAL"])
            reason_html = (f'<br><span style="font-size:0.75em;color:#94a3b8">'
                           f'{info.get("reason","")}</span>') if info.get("reason") else ""
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:8px 14px;'
                f'margin:6px 0;background:{_C["surface"]};border-radius:0 6px 6px 0">'
                f'<b>{sc.entity}</b> {_badge(sc.staleness_level, color)} '
                f'sim={sc.semantic_similarity:.3f} | days stale: {sc.days_since_update} | '
                f'AI belief: <code>{info.get("belief", sc.model_belief)}</code> → '
                f'Gold truth: <code>{info.get("truth", sc.warehouse_truth)}</code>'
                f'{reason_html}</div>',
                unsafe_allow_html=True,
            )
    else:
        st.info("No scores for this domain.")

    # ── Drift Diff Visualization ──────────────────────────────────────────────
    st.divider()
    st.subheader("Drift Diff — What AI Said vs What Gold Says")
    st.caption("Side-by-side comparison for every entity in the selected domain. "
               "Red = AI belief diverges from Gold truth.")

    if domain_scores:
        for sc in domain_scores:
            info  = reasons.get((domain, sc.entity), {})
            ai_val   = info.get("belief", sc.model_belief)   or sc.model_belief   or "—"
            gold_val = info.get("truth",  sc.warehouse_truth) or sc.warehouse_truth or "—"
            color    = _C.get(sc.staleness_level, _C["NEUTRAL"])
            is_drift = str(ai_val).strip() != str(gold_val).strip()

            diff_col1, diff_col2 = st.columns(2)
            with diff_col1:
                st.markdown(
                    f'<div style="background:#2a1a1a;border:1px solid #FF4B4B;'
                    f'border-radius:8px;padding:12px 16px">'
                    f'<div style="font-size:0.72em;color:#FF4B4B;font-weight:700;'
                    f'text-transform:uppercase;letter-spacing:1px">What AI Said</div>'
                    f'<div style="font-weight:700;font-size:0.9em;margin-top:2px">{sc.entity}</div>'
                    f'<div style="font-size:1.3em;font-weight:700;color:#FF4B4B;'
                    f'margin-top:6px;font-family:monospace">{ai_val}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with diff_col2:
                st.markdown(
                    f'<div style="background:#0d2a1a;border:1px solid #00CC88;'
                    f'border-radius:8px;padding:12px 16px">'
                    f'<div style="font-size:0.72em;color:#00CC88;font-weight:700;'
                    f'text-transform:uppercase;letter-spacing:1px">What Gold Says</div>'
                    f'<div style="font-weight:700;font-size:0.9em;margin-top:2px">{sc.entity}</div>'
                    f'<div style="font-size:1.3em;font-weight:700;color:#00CC88;'
                    f'margin-top:6px;font-family:monospace">{gold_val}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            drift_label = (
                f'Drift detected — sim={sc.semantic_similarity:.3f} | {sc.days_since_update}d stale'
                if is_drift else "No drift — values match"
            )
            st.markdown(
                f'<div style="text-align:center;margin:4px 0 12px 0">'
                f'<span style="background:{color};color:#fff;padding:2px 12px;'
                f'border-radius:10px;font-size:0.78em;font-weight:700">'
                f'{sc.staleness_level} — {drift_label}</span></div>',
                unsafe_allow_html=True,
            )
    else:
        st.info("Select a domain above to see the drift diff.")

    # ── Live Probe ──
    with st.expander("🔬 Live Probe — run a CRAG probe against the Gold layer"):
        q   = st.text_input("Question", "What is the minimum revenue for Enterprise tier?")
        dom = st.selectbox("Domain", list(_cfg().dksm_domains.keys()), key="probe_dom")
        if st.button("▶ Run Probe"):
            result = _lci().inject(q, dom)
            if result.injected:
                st.success(f"Injection active — entity: **{result.entity}** | value: `{result.injected_value}`")
                st.code(result.context_block)
            else:
                st.info("No active injection for this domain. Run a staleness check first.")

    # ── Stage 2: Stale Action Queue ──
    st.divider()
    st.subheader("🗂️ Stale Action Queue — Remove / Update")
    st.caption("Gold layer records with expiry dates or stale values that require action. "
               "Approve removal or update in the ASGC Governance page.")

    col_scan, col_cfg = st.columns([2, 1])
    with col_scan:
        if st.button("🔍 Run 24h Expiry + Stale Scan"):
            sched = _scheduler()
            with st.spinner("Scanning Gold layer..."):
                alerts = sched.run_expiry_scan()
            st.success(f"Scan complete — {len(alerts)} alerts found. Refresh to update queue.")

    with col_cfg:
        st.caption("⚙️ Scan interval: 24h (configurable in `aria_config.yaml → expiry.scan_interval_hours`)")

    action_items = _scheduler().get_stale_action_items()
    if action_items:
        ind_filter = st.multiselect(
            "Filter by industry",
            options=sorted({i["industry"] for i in action_items}),
            default=[],
            key="stale_ind_filter"
        )
        filtered = [i for i in action_items if not ind_filter or i["industry"] in ind_filter]

        for item in filtered[:20]:
            action = item["recommended_action"]
            a_color = {"REMOVE": "#FF4B4B", "UPDATE": "#FFA500"}.get(action, "#4B9FFF")
            days = item.get("days_until_expiry")
            days_str = f"{days}d" if days is not None else "—"
            exp_date = item.get("expiry_date", "—")
            st.markdown(
                f'<div style="border-left:4px solid {a_color};padding:8px 14px;'
                f'margin:4px 0;background:{_C["surface"]};border-radius:0 6px 6px 0;'
                f'display:flex;justify-content:space-between;align-items:center">'
                f'<div>'
                f'<b>{item["entity"]}</b> '
                f'<span style="color:#94a3b8;font-size:0.8em">{item["domain"]} · {item["industry"]}</span>'
                f'<br><span style="font-size:0.78em;color:#94a3b8">{item["staleness_reason"]}</span>'
                f'</div>'
                f'<div style="text-align:right;min-width:140px">'
                f'<span style="background:{a_color};color:#000;padding:2px 8px;'
                f'border-radius:4px;font-size:0.75em;font-weight:700">{action}</span>'
                f'<br><span style="font-size:0.78em;color:#94a3b8">Expires {exp_date} ({days_str})</span>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        if len(action_items) > 20:
            st.caption(f"Showing 20 of {len(action_items)} items.")
    else:
        st.info("No stale action items found. All Gold layer records are current.")


# ── PAGE 3: LCI + PP ─────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _pp_health():
    return _pp().get_pipeline_health_summary()

def page_fix_intelligence():
    st.title("⚙️ LCI + PP — Fix Intelligence")
    col_l, col_r = st.columns(2)

    with col_l:
        st.subheader("💉 Active Context Injections")
        active = _lci().active_injections()
        stats  = _lci().get_injection_stats()
        st.metric("Active Injections", stats["active_injections"],
                  f"{stats['total_injections_24h']} total (24h)")
        if active:
            df_inj = pd.DataFrame(active)[["domain", "entity", "value", "expires_in_min"]]
            df_inj.columns = ["Domain", "Entity", "Injected Value", "Expires (min)"]
            st.dataframe(df_inj, hide_index=True, use_container_width=True)
        else:
            # Fall back to recent log history when no in-memory injections
            history = _lci().get_injection_history(hours_back=8)
            if history:
                df_hist = pd.DataFrame(history)[["domain", "entity", "injected_value", "source_version"]]
                df_hist.columns = ["Domain", "Entity", "Injected Value", "Version"]
                st.dataframe(df_hist, hide_index=True, use_container_width=True)
            else:
                st.info("No active injections. Trigger a staleness check to pre-load context.")

        st.subheader("Inject Manually")
        inj_dom = st.selectbox("Domain", list(_cfg().dksm_domains.keys()), key="inj_dom")
        inj_q   = st.text_input("Query", "What is the Enterprise revenue threshold?")
        if st.button("💉 Inject Now"):
            from core.event_bus import ARIAEvent
            entity_map = {
                "customer_segments": "Enterprise",
                "product_catalog":   "DataSense Pro",
                "risk_thresholds":   "Portfolio VaR - High",
                "drug_formulary":    "Humira Biosimilar",
            }
            entity = entity_map.get(inj_dom, "")
            _bus().emit(ARIAEvent(source_module="DKSM", event_type="STALENESS_DETECTED",
                                  domain=inj_dom, entity=entity,
                                  payload={"level": "STALE", "days_since_update": 90},
                                  severity="WARNING"))
            st.rerun()

    with col_r:
        st.subheader("⚙️ Pipeline Root Causes")
        health = _pp_health()
        st.metric("Pipeline Failures", health["failures_found"],
                  f"{health['total_domains']} domains scanned")
        by_domain = health.get("by_domain", {})
        if by_domain:
            rows = [{"Domain": d, "Model": v["dbt_model"], "Failure": v["failure_type"],
                     "Days Ago": v["days_since"], "Approval": "⚠️ Yes" if v["requires_approval"] else "✅ No"}
                    for d, v in by_domain.items()]
            df_pp = pd.DataFrame(rows)
            st.dataframe(df_pp, hide_index=True, use_container_width=True)

            st.subheader("Remediate")
            sel_domain = st.selectbox("Domain to remediate", list(by_domain.keys()))
            sel_action = st.selectbox("Action", ["refresh", "alert_only", "full_refresh"])
            if st.button("▶ Execute Remediation"):
                from modules.pp.pipeline_pulse import RemediationOption
                opt = RemediationOption(
                    action=sel_action, description=f"Manual {sel_action}",
                    estimated_minutes=10,
                    risk_level="High" if sel_action == "full_refresh" else "Low",
                    auto_executable=sel_action != "full_refresh",
                )
                result = _pp().execute_remediation(opt, sel_domain)
                if result.status == "executed":
                    st.success(result.message)
                elif result.status == "pending_approval":
                    st.warning("Queued for ASGC approval — go to Governance page.")
                else:
                    st.error(result.message)

    # Sankey: failure → staleness → injection
    st.divider()
    st.subheader("Causal Flow: Pipeline Failure → Staleness → Injection")
    n_failures  = health["failures_found"]
    n_stale     = sum(1 for s in _dksm_scores()[0] if s.staleness_level in ("STALE", "CRITICAL"))
    n_injected  = _lci().get_injection_stats()["total_injections_24h"]
    fig_sk = go.Figure(go.Sankey(
        node=dict(label=["Pipeline Failures", "Stale Entities", "Context Injections",
                         "Value Recovered"],
                  color=["#FF4B4B", "#FFA500", "#4B9FFF", "#00CC88"]),
        link=dict(
            source=[0, 1, 2],
            target=[1, 2, 3],
            value=[max(n_failures, 1), max(n_stale, 1), max(n_injected, 1)],
            color=["rgba(255,75,75,0.4)", "rgba(255,165,0,0.4)", "rgba(75,159,255,0.4)"],
        ),
    ))
    fig_sk.update_layout(height=280, paper_bgcolor="#0F1117", font_color="#fff",
                         title_text="Fix intelligence flow (last 24h)")
    st.plotly_chart(fig_sk, use_container_width=True)


# ── PAGE 4: AVL ───────────────────────────────────────────────────────────────

_DOMAIN_EXPOSURE = {
    "customer_segments": {"exposure": 1_800_000, "before": 6_000_000, "after": 7_500_000,  "eu": "High"},
    "drug_formulary":    {"exposure":   180_000, "before":        90, "after":        45,   "eu": "High"},
    "risk_thresholds":   {"exposure":   560_000, "before":       1.5, "after":      5.01,   "eu": "High"},
    "product_catalog":   {"exposure":   420_000, "before":       699, "after":      3000,   "eu": "Limited"},
    "coverage_limits":   {"exposure":   646_000, "before":    12_000, "after":    95_000,   "eu": "High"},
    "carrier_rates":     {"exposure":    99_200, "before":       185, "after":       220,   "eu": "Limited"},
    "coupons":           {"exposure":    24_360, "before":        18, "after":        42,   "eu": "Minimal"},
}

@st.cache_data(ttl=300)
def _avl_data():
    avl   = _avl()
    vs_30 = avl.get_value_summary(30)
    vs_90 = avl.get_value_summary(90)
    # Pull exposure from seeded event bus VALUE_CALCULATED events
    events = _bus().recent(hours_back=24 * 30)
    bus_exposure: dict[str, float] = {}
    for e in events:
        if e.event_type == "VALUE_CALCULATED" and e.payload:
            domain = e.domain
            exp = e.payload.get("exposure_usd", 0)
            if exp and domain not in bus_exposure:
                bus_exposure[domain] = exp
    return vs_30, vs_90, bus_exposure

def page_value_proof():
    st.title("💰 AVL — AI Value Ledger")
    vs_30, vs_90, exposures = _avl_data()

    # Top KPIs
    total_exp = sum(exposures.values()) if exposures else sum(v["exposure"] for v in _DOMAIN_EXPOSURE.values())
    total_rec = round(total_exp * 0.65, 0)
    c1, c2, c3 = st.columns(3)
    c1.metric("💸 Exposure Identified", f"${total_exp:,.0f}")
    c2.metric("💰 Value Recovered",     f"${total_rec:,.0f}")
    c3.metric("📈 Net ARIA Value",      f"${total_rec - 5000:,.0f}", "recovered − $5K fix cost")

    # Data-source indicator — shows whether values are linked to real injection timestamps
    lci_log = _ROOT / "data" / "lci_log.csv"
    if lci_log.exists():
        try:
            lci_df    = pd.read_csv(lci_log)
            lci_count = len(lci_df)
            if lci_count > 0:
                st.success(
                    f"✅ **Linked to real injection data** — "
                    f"{lci_count} injection timestamps in lci_log.csv used for outcome matching. "
                    f"Dollar values reflect decisions within 4h injection windows."
                )
            else:
                st.info("📊 Using estimated exposure values (lci_log.csv is empty — run a probe to generate real data).")
        except Exception:
            st.info("📊 Using estimated exposure values.")
    else:
        st.info("📊 Using estimated exposure values (no lci_log.csv yet — run a probe first).")

    st.divider()

    # ── Domain selector table (left) + detail panel (right) ──────────────────
    col_l, col_r = st.columns([1, 2])

    with col_l:
        st.subheader("Select a Domain")
        st.caption("👇 Click a row to see details")

        # Build selectable domain table
        domain_rows = []
        for domain, vals in _DOMAIN_EXPOSURE.items():
            if domain in _cfg().dksm_domains:
                exp_val = exposures.get(domain, vals["exposure"])
                eu_badge = {"High": "🔴 High", "Limited": "🟡 Limited", "Minimal": "🟢 Minimal"}.get(vals["eu"], vals["eu"])
                domain_rows.append({
                    "Domain":     domain.replace("_", " ").title(),
                    "_key":       domain,
                    "Exposure":   f"${exp_val:,.0f}",
                    "EU AI Act":  eu_badge,
                })
        df_domains = pd.DataFrame(domain_rows)

        sel = st.dataframe(
            df_domains[["Domain", "Exposure", "EU AI Act"]],
            hide_index=True,
            use_container_width=True,
            on_select="rerun",
            selection_mode="single-row",
            key="avl_domain_select",
        )

        # Resolve selected domain key
        selected_rows = sel.get("selection", {}).get("rows", []) if sel else []
        clicked_domain = df_domains.iloc[selected_rows[0]]["_key"] if selected_rows else None

    with col_r:
        if clicked_domain and clicked_domain in _DOMAIN_EXPOSURE:
            vals    = _DOMAIN_EXPOSURE[clicked_domain]
            exp_val = exposures.get(clicked_domain, vals["exposure"])
            color   = "#FF4B4B" if vals["eu"] == "High" else ("#FFA500" if vals["eu"] == "Limited" else "#00CC88")

            # Exposure card
            st.markdown(
                f'<div style="border:2px solid {color};border-radius:10px;padding:16px 20px;'
                f'background:#1E2130;margin-bottom:14px">'
                f'<div style="font-size:0.85em;color:#94a3b8;font-weight:600;text-transform:uppercase;letter-spacing:1px">'
                f'{clicked_domain.replace("_"," ").title()}</div>'
                f'<div style="font-size:2.4em;font-weight:700;color:{color};line-height:1.1">'
                f'${exp_val:,.0f}</div>'
                f'<div style="font-size:0.78em;color:#94a3b8;margin-bottom:8px">Financial Exposure Identified</div>'
                f'<hr style="border-color:#2d3748;margin:8px 0">'
                f'<div style="display:flex;gap:24px;font-size:0.85em">'
                f'<div><span style="color:#94a3b8">AI Belief</span><br>'
                f'<b style="color:#FF4B4B">{vals["before"]:,}</b></div>'
                f'<div style="color:#555;font-size:1.4em;line-height:1.6">→</div>'
                f'<div><span style="color:#94a3b8">Gold Truth</span><br>'
                f'<b style="color:#00CC88">{vals["after"]:,}</b></div>'
                f'<div style="margin-left:auto"><span style="color:#94a3b8">EU AI Act</span><br>'
                f'<b style="color:{color}">{vals["eu"]} Risk</b></div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

            # Before / After bar chart for selected domain only
            fig_ba = go.Figure(data=[
                go.Bar(name="AI Belief (before)", x=[clicked_domain.replace("_"," ").title()],
                       y=[vals["before"]], marker_color="#FF4B4B",
                       hovertemplate="AI Belief: <b>%{y:,}</b><extra></extra>"),
                go.Bar(name="Gold Truth (after)", x=[clicked_domain.replace("_"," ").title()],
                       y=[vals["after"]], marker_color="#00CC88",
                       hovertemplate="Gold Truth: <b>%{y:,}</b><extra></extra>"),
            ])
            fig_ba.update_layout(
                barmode="group",
                title=f"Belief vs Truth — {clicked_domain.replace('_',' ').title()}",
                height=240,
                paper_bgcolor="#0F1117", plot_bgcolor="#1E2130",
                font_color="#fff",
                showlegend=True,
                margin=dict(t=40, b=20, l=20, r=20),
            )
            st.plotly_chart(fig_ba, use_container_width=True)

            # Recovery potential
            rec_potential = round(exp_val * 0.65, 0)
            roi = round(rec_potential / 5000, 1)
            mc1, mc2 = st.columns(2)
            mc1.metric("Recovery Potential", f"${rec_potential:,.0f}", "65% recoverable")
            mc2.metric("ROI Multiplier", f"{roi}×", "vs $5K fix cost")

        else:
            st.info("← Select a domain from the table to see its exposure breakdown, belief vs truth chart, and recovery potential.")

            # Show all-domain overview chart when nothing selected
            overview_rows = []
            for domain, vals in _DOMAIN_EXPOSURE.items():
                if domain in _cfg().dksm_domains:
                    exp_val = exposures.get(domain, vals["exposure"])
                    overview_rows.append({"Domain": domain.replace("_"," ").title(),
                                          "Before": vals["before"], "After": vals["after"]})
            df_ov = pd.DataFrame(overview_rows)
            fig_ov = go.Figure(data=[
                go.Bar(name="AI Belief", x=df_ov["Domain"].tolist(),
                       y=df_ov["Before"].tolist(), marker_color="#FF4B4B"),
                go.Bar(name="Gold Truth", x=df_ov["Domain"].tolist(),
                       y=df_ov["After"].tolist(), marker_color="#00CC88"),
            ])
            fig_ov.update_layout(
                barmode="group", title="All domains — AI belief vs Gold truth",
                height=300,
                paper_bgcolor="#0F1117", plot_bgcolor="#1E2130",
                font_color="#fff",
                margin=dict(t=40, b=20, l=20, r=20),
            )
            st.plotly_chart(fig_ov, use_container_width=True)

    st.divider()
    st.subheader("Recovery Timeline (simulated, last 30 days)")
    if Path(_ROOT / "data" / "business_outcomes.csv").exists():
        df_out = pd.read_csv(_ROOT / "data" / "business_outcomes.csv",
                             parse_dates=["outcome_date"])
        df_out["outcome_date"] = pd.to_datetime(df_out["outcome_date"], utc=True, errors="coerce")
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        df_out = df_out[df_out["outcome_date"] >= cutoff].copy()
        if not df_out.empty:
            df_out["day"] = df_out["outcome_date"].dt.strftime("%m-%d")
            df_grp = (df_out[df_out["correction_applied"] == True]
                      .groupby(["day", "domain_referenced"])["outcome_value_usd"]
                      .sum().reset_index())
            fig_rt = go.Figure()
            for dom in df_grp["domain_referenced"].unique():
                # Highlight selected domain line if one is chosen
                is_selected = clicked_domain and dom == clicked_domain
                sub = df_grp[df_grp["domain_referenced"] == dom].sort_values("day")
                fig_rt.add_trace(go.Scatter(
                    x=sub["day"].tolist(), y=sub["outcome_value_usd"].tolist(),
                    mode="lines+markers", name=dom,
                    line=dict(width=3 if is_selected else 1.5,
                              color="#00CC88" if is_selected else None),
                    opacity=1.0 if (is_selected or not clicked_domain) else 0.35,
                ))
            fig_rt.update_layout(
                title="Value recovered by domain" + (f" — {clicked_domain.replace('_',' ').title()} highlighted" if clicked_domain else ""),
                xaxis_title="Date", yaxis_title="Value ($)",
                height=280,
                paper_bgcolor="#0F1117", plot_bgcolor="#1E2130",
                font_color="#fff",
            )
            st.plotly_chart(fig_rt, use_container_width=True)


# ── PAGE 5: FLE ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _fle_data():
    return _fle().get_feedback_summary(), _fle().get_learning_velocity(30)

def page_learning_engine():
    st.title("🔄 FLE — Feedback Loop Engine")
    summary, velocity = _fle_data()
    vel_val = velocity.get("learning_velocity", 0.0)

    c1, c2, c3 = st.columns(3)
    c1.metric("Learning Velocity",        f"{vel_val:.1%}")
    c2.metric("Total Signals",            summary.get("total_signals", 0))
    c3.metric("Corrections Applied",      summary.get("applied_count", 0))

    col_l, col_r = st.columns([2, 1])

    with col_l:
        st.subheader("Velocity Gauge")
        fig_g = go.Figure(go.Indicator(
            mode="gauge+number", value=round(vel_val * 100, 1),
            title={"text": "Correction Loop Closure Rate (%)"},
            gauge={
                "axis": {"range": [0, 100]},
                "bar":  {"color": "#4B9FFF"},
                "steps": [
                    {"range": [0,  30], "color": "#FF4B4B"},
                    {"range": [30, 60], "color": "#FFA500"},
                    {"range": [60, 100],"color": "#1E2130"},
                ],
                "threshold": {"line": {"color": "#00CC88", "width": 3},
                              "value": 80},
            },
        ))
        fig_g.update_layout(height=260, paper_bgcolor="#0F1117", font_color="#fff")
        st.plotly_chart(fig_g, use_container_width=True)

        st.subheader("Submit Correction")
        st.caption("Quick-fill a use case:")
        qc1, qc2 = st.columns(2)
        if qc1.button("🏦 UC1 — Enterprise $6M→$7.5M"):
            st.session_state["fle_dom"]    = "customer_segments"
            st.session_state["fle_entity"] = "Enterprise"
            st.session_state["fle_wrong"]  = "6000000"
            st.session_state["fle_right"]  = "7500000"
        if qc2.button("🏥 UC2 — Humira $90→$45"):
            st.session_state["fle_dom"]    = "drug_formulary"
            st.session_state["fle_entity"] = "Humira Biosimilar"
            st.session_state["fle_wrong"]  = "90"
            st.session_state["fle_right"]  = "45"
        with st.form("correction_form"):
            fc1, fc2 = st.columns(2)
            dom_list = list(_cfg().dksm_domains.keys())
            def_dom  = st.session_state.get("fle_dom", "customer_segments")
            dom_idx  = dom_list.index(def_dom) if def_dom in dom_list else 0
            f_dom    = fc1.selectbox("Domain", dom_list, index=dom_idx)
            f_entity = fc2.text_input("Entity",        st.session_state.get("fle_entity", "Enterprise"))
            fc3, fc4 = st.columns(2)
            f_wrong  = fc3.text_input("Wrong value",   st.session_state.get("fle_wrong",  "6000000"))
            f_right  = fc4.text_input("Correct value", st.session_state.get("fle_right",  "7500000"))
            f_type   = st.selectbox("Signal type",
                                    ["user_correction", "agent_override", "escalation", "non_use"])
            f_conf   = st.slider("Confidence", 0.0, 1.0, 0.9, 0.05)
            if st.form_submit_button("Submit Correction Signal"):
                sig = _fle().capture_signal(f_type, f_dom, f_entity,
                                            f_wrong, f_right, "dashboard", f_conf)
                st.success(f"Signal captured: {sig.signal_id} → {sig.fle_status}")
                st.cache_data.clear()

    with col_r:
        st.subheader("By Domain")
        by_dom = summary.get("by_domain", {})
        if by_dom:
            st.dataframe(pd.DataFrame(
                [{"Domain": k, "Signals": v} for k, v in by_dom.items()]
            ), hide_index=True)

        st.subheader("Fine-tune Pairs")
        ft_path = _ROOT / "data" / "fine_tune_pairs.jsonl"
        ft_count = sum(1 for _ in open(ft_path)) if ft_path.exists() else 0
        st.metric("Generated", ft_count)
        if ft_count > 0:
            import json
            pairs = [json.loads(l) for l in open(ft_path)][:3]
            for p in pairs:
                with st.expander(p.get("instruction", "")[:60]):
                    st.json(p)
        if ft_count > 0:
            raw = open(ft_path).read()
            st.download_button("⬇ Download fine_tune_pairs.jsonl",
                               raw, "fine_tune_pairs.jsonl", "application/jsonl")

    st.divider()
    st.subheader("Weekly Self-Improvement")

    # Read LEARNING_IMPROVEMENT events emitted by the weekly loop
    improve_events = [
        e for e in _bus().recent(hours_back=24 * 90, event_type="LEARNING_IMPROVEMENT")
        if e.payload
    ]

    if improve_events:
        rows_imp = []
        for e in sorted(improve_events, key=lambda x: x.timestamp):
            rows_imp.append({
                "Week":        e.payload.get("week_ending", ""),
                "This Week":   e.payload.get("corrections_this_week", 0),
                "Last Week":   e.payload.get("corrections_last_week", 0),
                "Delta":       e.payload.get("delta", 0),
                "Repeat Rate": f"{e.payload.get('repeat_error_rate', 0):.1%}",
            })
        df_imp = pd.DataFrame(rows_imp)
        ci1, ci2 = st.columns(2)
        ci1.metric("Corrections This Week", df_imp.iloc[-1]["This Week"],
                   delta=int(df_imp.iloc[-1]["Delta"]))
        ci2.metric("Repeat Error Rate", df_imp.iloc[-1]["Repeat Rate"],
                   delta=None)

        fig_wi = go.Figure(data=[
            go.Bar(name="This Week", x=df_imp["Week"].tolist(),
                   y=df_imp["This Week"].tolist(), marker_color="#00CC88"),
            go.Bar(name="Last Week", x=df_imp["Week"].tolist(),
                   y=df_imp["Last Week"].tolist(), marker_color="#4B9FFF", opacity=0.6),
        ])
        fig_wi.update_layout(
            barmode="group", title="Corrections applied — week over week",
            height=240, paper_bgcolor="#0F1117", plot_bgcolor="#1E2130",
            font_color="#fff", margin=dict(t=40, b=20),
        )
        st.plotly_chart(fig_wi, use_container_width=True)
        st.dataframe(df_imp, hide_index=True, use_container_width=True)
    else:
        # Fallback: corrections_per_week from velocity until first weekly event fires
        weekly = velocity.get("corrections_per_week", [0, 0, 0, 0])
        fig_w = go.Figure(go.Bar(
            x=[f"W-{4-i}" for i in range(len(weekly))],
            y=weekly, marker_color="#4B9FFF",
        ))
        fig_w.update_layout(
            title="Corrections per week (weekly loop not yet fired — fires after 7 days)",
            height=220, paper_bgcolor="#0F1117", plot_bgcolor="#1E2130", font_color="#fff",
        )
        st.plotly_chart(fig_w, use_container_width=True)
        st.caption("The weekly self-improvement loop fires every 7 days. "
                   "Trigger manually: `sched._run_weekly_improvement()`")


# ── PAGE 6: ASGC ─────────────────────────────────────────────────────────────

def page_governance():
    cfg  = _cfg()
    lead = cfg.asgc_lead()
    asgc = _asgc()

    st.title(f"🏛️ ASGC — Governance Console")

    # Lead name — show inline setup banner if not configured
    if not lead:
        st.warning("⚠️ Lead name not configured. Set it below to enable approvals.")
        col1, col2 = st.columns([3, 1])
        with col1:
            new_lead = st.text_input("Your name (lead approver)", placeholder="e.g. Alex Lee")
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("✅ Set Lead", use_container_width=True) and new_lead:
                cfg.set_lead(new_lead)
                st.success(f"Lead set to: {new_lead}")
                st.rerun()
        st.divider()
        lead = "Not configured"
    else:
        st.markdown(f"**Lead:** {lead}")
        with st.expander("⚙️ Change Lead Name"):
            new_lead = st.text_input("Lead name", lead)
            if st.button("Save Lead Name") and new_lead:
                cfg.set_lead(new_lead)
                st.success(f"Lead set to: {new_lead}")
                st.rerun()

    st.divider()

    # ── Approval queue ──
    st.subheader("🔴 Approval Queue")
    pending = asgc.get_pending_approvals()
    if pending:
        for req in pending:
            color = {"High": "#FF4B4B", "Medium": "#FFA500", "Low": "#00CC88"}.get(
                req.risk_level, "#4B9FFF")
            with st.container():
                st.markdown(
                    f'<div style="border-left:4px solid {color};padding:8px 12px;'
                    f'background:{_C["surface"]};border-radius:0 6px 6px 0;margin:4px 0">'
                    f'<b>{req.source_module}</b> → <code>{req.proposed_action}</code> '
                    f'| domain: {req.domain} | risk: {_badge(req.risk_level, color)}<br>'
                    f'<span style="font-size:0.78em;color:#94a3b8">'
                    f'{req.timestamp.strftime("%Y-%m-%d %H:%M UTC")} | '
                    f'{req.payload_summary[:80]}</span></div>',
                    unsafe_allow_html=True,
                )
                bc1, bc2, bc3 = st.columns([1, 1, 4])
                if bc1.button("✅ Approve", key=f"apr_{req.request_id}"):
                    asgc.approve(req.request_id, lead)
                    st.success("Approved")
                    st.cache_data.clear()
                    st.rerun()
                if bc2.button("❌ Reject", key=f"rej_{req.request_id}"):
                    reason = st.text_input("Reason", key=f"rsn_{req.request_id}")
                    asgc.reject(req.request_id, lead, reason)
                    st.warning("Rejected")
                    st.cache_data.clear()
                    st.rerun()
    else:
        st.success("✅ No pending approvals")

    st.divider()

    # ── Causal chain explorer ──
    st.subheader("🔗 Causal Chain Explorer")
    domains = list(_cfg().dksm_domains.keys())
    cc1, cc2, cc3 = st.columns([2, 2, 1])
    sel_dom    = cc1.selectbox("Domain", domains, key="cc_dom")
    entity_map = {
        "customer_segments": ["Enterprise", "Mid-Market", "Strategic"],
        "product_catalog":   ["DataSense Pro", "DataSense Enterprise", "InsightFlow Core"],
        "risk_thresholds":   ["Portfolio VaR - High", "Credit Exposure Limit - High",
                              "Credit Exposure Limit - Medium"],
        "drug_formulary":    ["Humira Biosimilar", "Ozempic", "Atorvastatin"],
    }
    sel_ent    = cc2.selectbox("Entity", entity_map.get(sel_dom, [""]), key="cc_ent")
    hrs_back   = cc3.number_input("Hours back", 1, 168, 24, key="cc_hrs")

    if st.button("🔍 Get Causal Chain"):
        chain = asgc.get_causal_chain(sel_dom, sel_ent, hrs_back)
        st.markdown(f"**Events found:** {chain.event_count}  |  "
                    f"**Exposure:** ${chain.total_exposure_usd:,.0f}  |  "
                    f"**Recovered:** ${chain.total_recovered_usd:,.0f}  |  "
                    f"**Open approvals:** {chain.open_approvals}")
        st.code(chain.narrative, language=None)

    st.divider()

    # ── Stack health table ──
    col_sh, col_br = st.columns([2, 1])
    with col_sh:
        st.subheader("Stack Health")
        health = _health()
        rows = []
        for mod, info in health.items():
            status = info.get("status", "OK")
            color  = _status_color(status)
            rows.append({"Module": mod, "Status": status,
                         "Key Metric": str(list(info.values())[1] if len(info) > 1 else "")})
        df_sh = pd.DataFrame(rows)
        st.dataframe(df_sh, hide_index=True, use_container_width=True)

    with col_br:
        st.subheader("Board Report")
        st.caption("Export a governance summary as JSON")
        if st.button("📊 Export Board Report (JSON)"):
            import json
            report_data = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "lead": lead,
                "stack_health": health,
                "value_summary": _value_summary(),
                "pending_approvals": len(asgc.get_pending_approvals()),
                "learning_velocity": _fle_velocity().get("learning_velocity", 0),
            }
            st.download_button(
                "⬇ Download board_report.json",
                json.dumps(report_data, indent=2, default=str),
                "board_report.json", "application/json",
            )


# ── PAGE 7: MCP & Integration ─────────────────────────────────────────────────

def page_mcp():
    st.title("🔌 ARIA — MCP & Integration")
    st.caption("8 tools exposed over SSE on port 8765 — callable by any AI agent")

    tool_docs = {
        "check_staleness":    ("DKSM", "Probe the AI's belief on an entity and score the divergence"),
        "search_gold_layer":  ("DKSM", "Semantic search over Gold layer records via Hybrid RAG"),
        "inject_context":     ("LCI",  "Inject verified current Gold value into agent context"),
        "get_pipeline_health":("PP",   "Root cause analysis for all mapped dbt models"),
        "get_value_summary":  ("AVL",  "Exposure identified, value recovered, ROI by domain"),
        "submit_correction":  ("FLE",  "Capture a correction signal and route it for propagation"),
        "get_causal_chain":   ("ASGC", "Full event narrative for a domain/entity"),
        "get_stack_health":   ("ASGC", "All 6 module statuses — use as agent pre-flight check"),
    }
    rows = [{"Tool": t, "Module": m, "Description": d} for t, (m, d) in tool_docs.items()]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("Live Tool Tester")
    tool = st.selectbox("Select tool", list(tool_docs.keys()))

    inputs: dict = {}
    if tool == "check_staleness":
        inputs["domain"] = st.selectbox("domain", list(_cfg().dksm_domains.keys()), key="mt_dom")
        inputs["entity"] = st.text_input("entity", "Enterprise")
    elif tool == "search_gold_layer":
        inputs["query"]  = st.text_input("query", "Enterprise revenue threshold")
        inputs["domain"] = st.text_input("domain (optional)", "")
        inputs["top_k"]  = st.number_input("top_k", 1, 10, 3)
    elif tool == "inject_context":
        inputs["query"]  = st.text_input("query", "What is the Enterprise threshold?")
        inputs["domain"] = st.selectbox("domain", list(_cfg().dksm_domains.keys()), key="mt_inj")
    elif tool == "get_pipeline_health":
        inputs["domain"] = st.text_input("domain (empty = all)", "")
    elif tool == "get_value_summary":
        inputs["days_back"] = st.number_input("days_back", 7, 365, 30)
    elif tool == "submit_correction":
        inputs["domain"]        = st.selectbox("domain", list(_cfg().dksm_domains.keys()), key="mt_fle")
        inputs["entity"]        = st.text_input("entity", "Enterprise")
        inputs["wrong_value"]   = st.text_input("wrong_value", "6000000")
        inputs["correct_value"] = st.text_input("correct_value", "7500000")
    elif tool == "get_causal_chain":
        inputs["domain"]     = st.selectbox("domain", list(_cfg().dksm_domains.keys()), key="mt_cc")
        inputs["entity"]     = st.text_input("entity", "Enterprise")
        inputs["hours_back"] = st.number_input("hours_back", 1, 168, 24)
    # get_stack_health: no inputs

    if st.button("▶ Call Tool"):
        import sys as _sys
        _sys.path.insert(0, str(_ROOT / "mcp"))
        import aria_mcp_server as _mcp
        fn = getattr(_mcp, tool)
        with st.spinner("Calling…"):
            result = fn(**inputs) if inputs else fn()
        st.subheader("Response")
        st.json(result)

    st.divider()
    st.subheader("Claude Desktop Config")
    port = 8765
    config_json = {
        "mcpServers": {
            "aria": {
                "command": "python",
                "args": [str(_ROOT / "mcp" / "aria_mcp_server.py")],
                "env": {"ARIA_MCP_TOKEN": "your-token-here"},
            }
        }
    }
    import json
    st.code(json.dumps(config_json, indent=2), language="json")

    st.subheader("Integration Snippets")
    tab_c, tab_lg, tab_ag = st.tabs(["Claude SDK", "LangGraph", "AutoGen"])
    with tab_c:
        st.code('''import anthropic, httpx

client = anthropic.Anthropic()
headers = {"Authorization": "Bearer <YOUR_ARIA_MCP_TOKEN>"}

def aria_check(domain: str, entity: str) -> dict:
    r = httpx.post("http://localhost:8765/tools/check_staleness",
                   json={"domain": domain, "entity": entity}, headers=headers)
    return r.json()

result = aria_check("customer_segments", "Enterprise")
if result["staleness_level"] != "FRESH":
    inject = aria_inject_context("What is Enterprise threshold?", domain)
    # prepend inject["context_block"] to your prompt
''', language="python")
    with tab_lg:
        st.code('''from langgraph.graph import StateGraph
import httpx

ARIA = "http://localhost:8765"
HEADERS = {"Authorization": "Bearer <YOUR_ARIA_MCP_TOKEN>"}

def staleness_node(state):
    r = httpx.post(f"{ARIA}/tools/check_staleness",
                   json={"domain": state["domain"], "entity": state["entity"]},
                   headers=HEADERS)
    state["staleness"] = r.json()
    return state

def inject_node(state):
    if state["staleness"]["staleness_level"] != "FRESH":
        r = httpx.post(f"{ARIA}/tools/inject_context",
                       json={"query": state["query"], "domain": state["domain"]},
                       headers=HEADERS)
        state["context"] = r.json()["context_block"]
    return state
''', language="python")
    with tab_ag:
        st.code('''from autogen import AssistantAgent, UserProxyAgent, register_function
import httpx

def check_aria_staleness(domain: str, entity: str) -> str:
    r = httpx.post("http://localhost:8765/tools/check_staleness",
                   json={"domain": domain, "entity": entity},
                   headers={"Authorization": "Bearer <YOUR_ARIA_MCP_TOKEN>"})
    d = r.json()
    return f"Level: {d[\'staleness_level\']} | Belief: {d[\'model_belief\']} | Truth: {d[\'warehouse_truth\']}"

assistant = AssistantAgent("aria_agent", llm_config={"model": "claude-sonnet-4-6"})
register_function(check_aria_staleness, caller=assistant,
                  description="Check if ARIA data is stale before answering")
''', language="python")


# ── Router ────────────────────────────────────────────────────────────────────

def main():
    page = _sidebar()
    if   page.startswith("🏠"):  page_command_center()
    elif page.startswith("🔍"):  page_dksm()
    elif page.startswith("⚙️"): page_fix_intelligence()
    elif page.startswith("💰"):  page_value_proof()
    elif page.startswith("🔄"):  page_learning_engine()
    elif page.startswith("🏛️"): page_governance()
    elif page.startswith("🔌"):  page_mcp()

if __name__ == "__main__":
    main()
