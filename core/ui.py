from __future__ import annotations

import html
from typing import Any

import pandas as pd
import streamlit as st

STATUS_COLORS = {

    "FM Draft": ("#475569", "#f1f5f9"), "Submitted to Procurement Manager": ("#1d4ed8", "#dbeafe"), "PM Reviewing": ("#0369a1", "#e0f2fe"), "Returned to Facility Manager": ("#b45309", "#fef3c7"), "Accepted by Procurement Manager": ("#047857", "#d1fae5"), "Converted to Purchase Request": ("#5b21b6", "#ede9fe"), "Rejected by Procurement Manager": ("#b91c1c", "#fee2e2"), "Approved for Payment": ("#047857", "#d1fae5"), "Delegated Approval Mode": ("#5b21b6", "#ede9fe"), "Normal Approval Mode": ("#0369a1", "#e0f2fe"),
    "Draft": ("#475569", "#f1f5f9"), "Submitted": ("#1d4ed8", "#dbeafe"), "Procurement Review": ("#0369a1", "#e0f2fe"), "Requires Sourcing": ("#92400e", "#fef3c7"), "Pending Approval": ("#a16207", "#fef9c3"), "Approved": ("#047857", "#d1fae5"), "Rejected": ("#b91c1c", "#fee2e2"), "PO Created": ("#5b21b6", "#ede9fe"), "Awaiting Delivery": ("#7c2d12", "#ffedd5"), "Received": ("#166534", "#dcfce7"), "Paid": ("#15803d", "#dcfce7"), "Closed": ("#334155", "#e2e8f0"),
    "Open": ("#0369a1", "#e0f2fe"), "Pending": ("#a16207", "#fef9c3"), "Sent to Vendor": ("#5b21b6", "#ede9fe"), "Partially Received": ("#92400e", "#fef3c7"), "Fully Received": ("#047857", "#d1fae5"), "Invoiced": ("#4f46e5", "#e0e7ff"), "Cancelled": ("#b91c1c", "#fee2e2"), "Disputed": ("#b91c1c", "#fee2e2"), "Returned": ("#b91c1c", "#fee2e2"),
    "Active": ("#047857", "#d1fae5"), "Suspended": ("#b91c1c", "#fee2e2"), "Under Review": ("#a16207", "#fef9c3"), "Needs Review": ("#a16207", "#fef9c3"), "Matched": ("#047857", "#d1fae5"), "Mismatch": ("#b91c1c", "#fee2e2"), "Not Matched": ("#64748b", "#f1f5f9"),
}


def inject_css():
    st.markdown("""
    <style>
    .block-container {padding-top: 1.1rem; padding-bottom: 3rem;}
    div[data-testid="stMetric"] {background: #fff; border: 1px solid #e5e7eb; padding: 12px 14px; border-radius: 16px; box-shadow: 0 1px 2px rgba(15, 23, 42, .04); min-height: 96px;}
    div[data-testid="stMetric"] label { color: #475569 !important; font-size: .78rem !important; line-height: 1.1rem !important; white-space: normal !important;}
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {font-size: clamp(1.25rem, 2.2vw, 2rem) !important; line-height: 2rem !important; font-weight: 800 !important; letter-spacing: -0.02em;}
    .pf-section-count {display:inline-flex; align-items:center; justify-content:center; background:#dc2626; color:#fff; min-width:20px; height:20px; padding:0 6px; border-radius:999px; font-size:12px; font-weight:800;}
    .pf-card {background: #fff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 16px; box-shadow: 0 1px 2px rgba(15, 23, 42, .04); margin-bottom: 12px;}
    .pf-muted { color: #64748b; font-size: .92rem; }
    .pf-hero {background: linear-gradient(135deg, #0f172a, #1e293b); color: white; border-radius: 20px; padding: 22px 24px; margin-bottom: 16px;}
    .pf-hero p { color: #cbd5e1; margin: 0; }
    .pf-badge {display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; border: 1px solid rgba(0,0,0,.06);}
    </style>
    """, unsafe_allow_html=True)


def money(value: Any) -> str:
    try:
        return f"₦{float(value):,.2f}"
    except Exception:
        return "₦0.00"


def badge(status: str | None) -> str:
    status = status or "Unknown"
    display_status = {"Returned to Facility Manager": "Returned for Correction"}.get(status, status)
    fg, bg = STATUS_COLORS.get(status, ("#334155", "#f1f5f9"))
    return f'<span class="pf-badge" style="color:{fg}; background:{bg};">{html.escape(display_status)}</span>'


def dataframe(df: pd.DataFrame, hide_index: bool = True):
    # Presentation-only label cleanup. Keep DB values unchanged.
    if isinstance(df, pd.DataFrame) and not df.empty:
        view = df.copy()
        view = view.replace({
            "Facility Manager": "Utility Head / Facility Head",
            "Facility Manager Inbox": "Utility Head / Facility Head Inbox",
            "Returned to Facility Manager": "Returned for Correction",
        })
    else:
        view = df
    st.dataframe(view, use_container_width=True, hide_index=hide_index)


def empty_state(title: str, message: str, action: str | None = None):
    with st.container(border=True):
        st.subheader(title)
        st.caption(message)
        if action:
            st.info(action)


def workflow_progress(status: str, steps: list[str]):
    """Render workflow horizontally without tiny vertical text wrapping.

    Streamlit columns become unreadable when a workflow has many statuses.
    This compact horizontal rail keeps badges on one line and scrolls sideways
    when needed, so status names never stack letter-by-letter.
    """
    if not steps:
        return
    try:
        current = steps.index(status)
    except ValueError:
        current = -1
    parts = []
    for i, step in enumerate(steps):
        if i < current:
            cls = "done"
            symbol = "✓"
        elif i == current:
            cls = "current"
            symbol = "●"
        else:
            cls = "todo"
            symbol = "○"
        parts.append(f'<span class="pf-step {cls}"><span class="pf-step-dot">{symbol}</span>{html.escape(str(step))}</span>')
    st.markdown(
        """
        <style>
        .pf-workflow-rail {
            display:flex; gap:8px; align-items:center; overflow-x:auto; padding:8px 2px 12px;
            scrollbar-width: thin; white-space: nowrap; margin-bottom: 8px;
        }
        .pf-step {
            flex:0 0 auto; display:inline-flex; align-items:center; gap:6px; border-radius:999px;
            padding:7px 11px; font-size:12px; font-weight:700; border:1px solid #e5e7eb;
            line-height:1; white-space:nowrap; min-width:max-content;
        }
        .pf-step.done { color:#047857; background:#d1fae5; border-color:#bbf7d0; }
        .pf-step.current { color:#075985; background:#dbeafe; border-color:#bfdbfe; }
        .pf-step.todo { color:#6b7280; background:#ffffff; border-color:#e5e7eb; }
        .pf-step-dot {font-size:10px;}
        </style>
        <div class="pf-workflow-rail">%s</div>
        """ % "".join(parts),
        unsafe_allow_html=True,
    )


# ---------- KPI and interactive visualization helpers ----------

def compact_number(value: Any, decimals: int = 1) -> str:
    """Return compact human-readable numbers for dashboard cards: 1K, 2.5M, 1B."""
    try:
        num = float(str(value).replace("₦", "").replace(",", "").strip())
    except Exception:
        return str(value)
    sign = "-" if num < 0 else ""
    num = abs(num)
    units = [(1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")]
    for threshold, suffix in units:
        if num >= threshold:
            val = num / threshold
            if val >= 100 or val.is_integer():
                body = f"{val:.0f}"
            else:
                body = f"{val:.{decimals}f}".rstrip("0").rstrip(".")
            return f"{sign}{body}{suffix}"
    if num.is_integer():
        return f"{sign}{num:.0f}"
    return f"{sign}{num:,.{decimals}f}".rstrip("0").rstrip(".")


def compact_money(value: Any) -> str:
    try:
        raw = str(value).replace("₦", "").replace(",", "").strip()
        return "₦" + compact_number(float(raw))
    except Exception:
        return "₦0"


def format_kpi_value(value: Any) -> str:
    """Format only KPI/metric-card values, leaving normal tables free to use full amounts."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("₦"):
            return compact_money(stripped)
        if stripped.endswith("%") or any(ch.isalpha() for ch in stripped.replace("₦", "")):
            return stripped
        try:
            return compact_number(float(stripped.replace(",", "")))
        except Exception:
            return stripped
    if isinstance(value, (int, float)):
        return compact_number(value)
    return str(value)


def interactive_chart(
    df: pd.DataFrame,
    title: str,
    x: str,
    y: str,
    key: str,
    default: str = "Bar",
    color: str | None = None,
    allow_pie: bool = True,
):
    """Reusable interactive chart block with selectable chart type."""
    st.markdown(f"#### {title}")
    if df is None or df.empty or x not in df.columns or y not in df.columns:
        st.info("No data available for this chart yet.")
        return
    chart_types = ["Bar", "Horizontal Bar", "Line", "Area"] + (["Pie", "Donut"] if allow_pie else []) + ["Table"]
    if default not in chart_types:
        default = "Bar"
    chosen = st.selectbox("Chart type", chart_types, index=chart_types.index(default), key=f"{key}_chart_type")
    data = df.copy()
    try:
        data[y] = pd.to_numeric(data[y], errors="coerce").fillna(0)
    except Exception:
        pass
    try:
        import plotly.express as px
        fig = None
        if chosen == "Bar":
            fig = px.bar(data, x=x, y=y, color=color if color in data.columns else None, text_auto=True)
        elif chosen == "Horizontal Bar":
            fig = px.bar(data, x=y, y=x, orientation="h", color=color if color in data.columns else None, text_auto=True)
        elif chosen == "Line":
            fig = px.line(data, x=x, y=y, markers=True, color=color if color in data.columns else None)
        elif chosen == "Area":
            fig = px.area(data, x=x, y=y, color=color if color in data.columns else None)
        elif chosen == "Pie":
            fig = px.pie(data, names=x, values=y)
        elif chosen == "Donut":
            fig = px.pie(data, names=x, values=y, hole=0.45)
        elif chosen == "Table":
            dataframe(data)
            return
        if fig is not None:
            fig.update_layout(margin=dict(l=12, r=12, t=10, b=12), height=360)
            st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False, "responsive": True})
    except Exception:
        if chosen == "Line":
            st.line_chart(data.set_index(x)[y])
        elif chosen == "Area":
            st.area_chart(data.set_index(x)[y])
        elif chosen == "Table":
            dataframe(data)
        else:
            st.bar_chart(data.set_index(x)[y])
