import streamlit as st

from core.db import init_db, df_query, run_query, now_iso
from core.auth import login_panel, logout_button, require_user
from modules.role_workspaces import render_app, render_notification_panel
from core.permissions import display_role


@st.cache_resource(show_spinner=False)
def boot_database_once():
    """Initialize SQLite schema/seeds once per Streamlit server process.

    Streamlit reruns the script on every click. Running all migrations and seed
    checks on every navigation click makes the UI feel slow, so this wrapper
    keeps startup safety while avoiding repeated database boot work.
    """
    init_db()
    return True


ROLE_LANDING = {
    "Admin": "Admin Console",
    "Procurement Manager": "Procurement Workspace",
    "Facility Manager": "Utility Head / Facility Head Workspace",
    "Finance": "Finance Workspace",
    "Approver": "Executive Approval Workspace",
    "Auditor": "Audit & Compliance Workspace",
}


ROLE_SECTIONS = {
    "Admin": (
        "Admin Navigation",
        "admin_section",
        [
            "Admin Dashboard",
            "Budget Tracker",
            "Income",
            "User Management",
            "Roles & Permissions",
            "Approval Configuration",
            "Import Center",
            "All Procurement Records",
            "Notifications Monitor",
            "Availability & Delegation Requests",
            "Gateway Pass Management",
            "Activity & History Logs",
            "Audit Logs",
            "Backup / Export",
            "Settings",
        ],
    ),
    "Procurement Manager": (
        "Procurement Navigation",
        "procurement_section",
        [
            "Operations Dashboard",
            "Purchase Requests",
            "Utility Head / Facility Head Inbox",
            "Import Center",
            "Sourcing",
            "Vendor Quotes",
            "Vendor Recommendation",
            "Purchase Orders",
            "Receiving Slips",
            "Vendors",
            "Gateway Pass Review",
            "Post-Payment Closure",
            "Availability / Away Notice",
            "Procurement Documents",
            "Procurement Reports",
            "Income",
            "My Activity History",
            "Settings",
        ],
    ),
    "Facility Manager": (
        "Utility / Facility Navigation",
        "facility_section",
        [
            "Utility / Facility Dashboard",
            "Create Request Draft",
            "My Draft Requests",
            "Submit to Procurement Manager",
            "Import Documents",
            "Gateway Pass",
            "Shared Thread with Procurement Manager",
            "Returned Requests",
            "Approved / Accepted Requests",
            "Income",
            "My Activity History",
            "Settings",
        ],
    ),
    "Finance": (
        "Finance Navigation",
        "finance_section",
        [
            "Financial Dashboard",
            "Approved for Payment",
            "Receipts",
            "Invoices",
            "Expenses",
            "Payments",
            "Cash Advances",
            "Budgets",
            "Income",
            "Vendor Payment Records",
            "Reconciliation",
            "Financial Reports",
            "Settings",
        ],
    ),
    "Approver": (
        "Executive Navigation",
        "executive_section",
        [
            "Approval Dashboard",
            "Pending Approvals",
            "Quote Comparison",
            "PO Approval",
            "Payment Approval",
            "Gateway Pass Approval",
            "Availability / Away Notice",
            "My Approval History",
            "Income",
            "Settings",
        ],
    ),
    "Auditor": (
        "Audit Navigation",
        "audit_section",
        [
            "Audit Dashboard",
            "Procurement Records",
            "Document Archive",
            "Approval Trails",
            "Delegated Approval Review",
            "Budget Audit",
            "Utility / Facility Head Handoff Trail",
            "Gateway Pass Audit",
            "Vendor History",
            "Expense Review",
            "Compliance Reports",
            "Income",
            "Settings",
        ],
    ),
}



def inject_shell_css():
    st.markdown(
        """
        <style>
        .pf-app-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 18px;
            padding: 16px 20px;
            margin: 0 0 18px 0;
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 18px;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
        }
        .pf-brand-wrap { display: flex; align-items: center; gap: 13px; }
        .pf-brand-icon {
            width: 42px;
            height: 42px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 14px;
            background: #f1f5f9;
            font-size: 22px;
        }
        .pf-brand-title { font-size: 22px; font-weight: 800; color: #0f172a; margin: 0; }
        .pf-brand-subtitle { color: #64748b; margin-top: 2px; font-size: 14px; }
        .pf-user-panel {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
            justify-content: flex-end;
            text-align: right;
        }
        .pf-user-name { font-weight: 800; color: #0f172a; }
        .pf-pill {
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            background: #e0f2fe;
            color: #075985;
            font-size: 12px;
            font-weight: 700;
            border: 1px solid #bae6fd;
        }
        .pf-landing-pill {
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            background: #ecfdf5;
            color: #047857;
            font-size: 12px;
            font-weight: 700;
            border: 1px solid #bbf7d0;
        }
        section[data-testid="stSidebar"] .stButton > button {
            width: 100%;
        }
        @media (max-width: 900px) {
            .pf-app-header { align-items: flex-start; flex-direction: column; }
            .pf-user-panel { justify-content: flex-start; text-align: left; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_top_header(current: dict):
    landing = ROLE_LANDING.get(current["role"], "Workspace")
    st.markdown(
        f"""
        <div class="pf-app-header">
            <div class="pf-brand-wrap">
                <div class="pf-brand-icon">🧾</div>
                <div>
                    <div class="pf-brand-title">ProcureFlow</div>
                    <div class="pf-brand-subtitle">Enterprise Procurement Management</div>
                </div>
            </div>
            <div class="pf-user-panel">
                <div>
                    <div class="pf-user-name">{current['full_name']}</div>
                    <div style="color:#64748b; font-size:13px;">Signed in workspace</div>
                </div>
                <span class="pf-pill">{display_role(current['role'])}</span>
                <span class="pf-landing-pill">{landing}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )




def _nav_count_query(sql: str, params: tuple = ()) -> int:
    try:
        df = df_query(sql, params)
        return int(df.iloc[0, 0]) if not df.empty else 0
    except Exception:
        return 0


def _ensure_section_seen_schema():
    """Small defensive migration for WhatsApp-style tab badge clearing.

    A badge means: "new attention item since you last opened this section".
    It should not behave as a permanent counter for all outstanding records.
    """
    try:
        run_query(
            """
            CREATE TABLE IF NOT EXISTS section_attention_reads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                section TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                UNIQUE(user_id, role, section)
            )
            """
        )
        run_query(
            "CREATE INDEX IF NOT EXISTS idx_section_attention_reads_user_section ON section_attention_reads(user_id, role, section, last_seen_at)"
        )
    except Exception:
        pass


def _section_last_seen(current: dict, section: str) -> str:
    _ensure_section_seen_schema()
    try:
        rows = df_query(
            "SELECT last_seen_at FROM section_attention_reads WHERE user_id=? AND role=? AND section=? LIMIT 1",
            (int(current.get("id") or 0), current.get("role"), section),
        )
        if not rows.empty and rows.iloc[0, 0]:
            return str(rows.iloc[0, 0])
    except Exception:
        pass
    return "1970-01-01 00:00:00"


def mark_section_attention_seen(current: dict, section: str):
    """Clear the red badge for the section the user has just opened.

    This does not delete or complete pending work. It simply records that the
    user has viewed the section, exactly like an unread chat badge clearing
    after the chat is opened. New or updated records will show the badge again.
    """
    _ensure_section_seen_schema()
    uid = int(current.get("id") or 0)
    role = current.get("role") or ""
    ts = now_iso()
    try:
        run_query(
            """
            INSERT INTO section_attention_reads (user_id, role, section, last_seen_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, role, section)
            DO UPDATE SET last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at
            """,
            (uid, role, section, ts, ts, ts),
        )
        # If a notification was explicitly routed to this section, opening the
        # section also marks that routed notification as read. General bell
        # notifications remain controlled by the notification panel.
        run_query(
            """
            UPDATE notifications
            SET is_read=1
            WHERE is_read=0
              AND section_target=?
              AND (user_id=? OR role=? OR role='All')
            """,
            (section, uid, role),
        )
    except Exception:
        pass


def _count_since(sql: str, params: tuple, seen_at: str) -> int:
    return _nav_count_query(sql, tuple(params) + (seen_at,))


def attention_count_for_section(current: dict, section: str) -> int:
    """Return WhatsApp-style *new since opened* counts per role section."""
    role = current.get("role")
    uid = int(current.get("id") or 0)
    seen_at = _section_last_seen(current, section)
    unread = _nav_count_query(
        """
        SELECT COUNT(*) FROM notifications
        WHERE is_read=0
          AND (user_id=? OR role=? OR role='All')
          AND section_target=?
          AND datetime(created_at) > datetime(?)
        """,
        (uid, role, section, seen_at),
    )
    count = 0
    if role == "Admin":
        mapping = {
            "Admin Dashboard": ("SELECT COUNT(*) FROM user_availability WHERE (admin_review_status='Pending Review' OR status IN ('Away Requested','Away Active')) AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Notifications Monitor": ("SELECT COUNT(*) FROM notification_outbox WHERE status IN ('Queued','Fallback') AND datetime(created_at) > datetime(?)", ()),
            "Availability & Delegation Requests": ("SELECT COUNT(*) FROM user_availability WHERE (admin_review_status='Pending Review' OR status='Away Requested') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Gateway Pass Management": ("SELECT COUNT(*) FROM gateway_passes WHERE status IN ('Sent for Procurement Review','Submitted','Submitted for Approval','Pending Approval','Pending Procurement Manager / Approver Review') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Audit Logs": ("SELECT COUNT(*) FROM audit_logs WHERE action IN ('LOGIN','LOGOUT','PASSWORD_RESET','ROLE_CHANGE') AND datetime(created_at) > datetime(?)", ()),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    elif role == "Procurement Manager":
        mapping = {
            "Utility Head / Facility Head Inbox": ("SELECT COUNT(*) FROM purchase_requests WHERE (status IN ('Sent for Procurement Review','Submitted to Procurement Manager') OR next_role='procurement_manager') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Gateway Pass Review": ("SELECT COUNT(*) FROM gateway_passes gp WHERE (gp.status IN ('Sent for Procurement Review','Submitted','Submitted for Approval','Pending Approval','Pending Procurement Manager / Approver Review') OR gp.next_role IN ('procurement_manager','approver')) AND datetime(COALESCE(gp.updated_at, gp.created_at)) > datetime(?)", ()),
            "Post-Payment Closure": ("SELECT COUNT(*) FROM purchase_requests WHERE (status IN ('Paid','Receipt Uploaded','Payment Submitted for Verification','Completed','Closed') OR (next_role='procurement_manager' AND payment_status='Paid')) AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Purchase Requests": ("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Submitted','Procurement Review','Requires Sourcing','Vendor Quote Collection','Approved') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Availability / Away Notice": ("SELECT COUNT(*) FROM user_availability WHERE user_id=? AND status NOT IN ('Returned','Cancelled') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    elif role == "Facility Manager":
        mapping = {
            "My Draft Requests": ("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status IN ('FM Draft','Returned to Facility Manager') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
            "Submit to Procurement Manager": ("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status IN ('FM Draft','Returned to Facility Manager') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
            "Gateway Pass": ("SELECT COUNT(*) FROM gateway_passes WHERE facility_manager_user_id=? AND status IN ('Approved','Generated','Downloaded','Returned for Correction','Returned') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
            "Returned Requests": ("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status='Returned to Facility Manager' AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
            "Approved / Accepted Requests": ("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status IN ('Accepted by Procurement Manager','Approved','Paid','Closed') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    elif role == "Finance":
        mapping = {
            "Approved for Payment": ("SELECT COUNT(*) FROM purchase_requests WHERE (status='Approved for Payment' OR payment_status='Approved for Payment') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Invoices": ("SELECT COUNT(*) FROM invoices WHERE (status IN ('Uploaded','Needs Review','Returned') OR match_status IN ('Needs Review','Mismatch')) AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Receipts": ("SELECT COUNT(*) FROM receipt_records WHERE status='Recorded' AND datetime(created_at) > datetime(?)", ()),
            "Payments": ("SELECT COUNT(*) FROM payments WHERE status IN ('Pending Approval','Approved') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    elif role == "Approver":
        mapping = {
            "Pending Approvals": ("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "PO Approval": ("SELECT COUNT(*) FROM purchase_orders WHERE status='Pending Approval' AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Payment Approval": ("SELECT COUNT(*) FROM payments WHERE status='Pending Approval' AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Gateway Pass Approval": ("SELECT COUNT(*) FROM gateway_passes WHERE status IN ('Sent for Procurement Review','Submitted','Submitted for Approval','Pending Approval','Pending Procurement Manager / Approver Review') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Availability / Away Notice": ("SELECT COUNT(*) FROM user_availability WHERE user_id=? AND status NOT IN ('Returned','Cancelled') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    elif role == "Auditor":
        mapping = {
            "Gateway Pass Audit": ("SELECT COUNT(*) FROM gateway_passes WHERE datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Approval Trails": ("SELECT COUNT(*) FROM approval_history WHERE datetime(created_at) > datetime(?)", ()),
            "Delegated Approval Review": ("SELECT COUNT(*) FROM approval_delegations WHERE enabled=1 AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Budget Audit": ("SELECT COUNT(*) FROM budget_history WHERE datetime(created_at) > datetime(?)", ()),
            "Audit Dashboard": ("SELECT COUNT(*) FROM audit_logs WHERE datetime(created_at) > datetime(?)", ()),
            "Compliance Reports": ("SELECT COUNT(*) FROM notifications WHERE (role='Auditor' OR user_id=?) AND is_read=0 AND datetime(created_at) > datetime(?)", (uid,)),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    return max(int(count or 0), int(unread or 0))


def _build_attention_count_map(current: dict, sections: list[str]) -> dict[str, int]:
    """Build sidebar red-badge counts once per rerun.

    The previous implementation recalculated every section inside the radio
    format function, so a single click could run dozens of SQLite queries and
    then trigger a second rerun after clearing the badge. This function keeps
    the WhatsApp-style badge behavior while making navigation feel immediate.
    """
    _ensure_section_seen_schema()
    counts: dict[str, int] = {}
    for section in sections:
        try:
            counts[section] = int(attention_count_for_section(current, section) or 0)
        except Exception:
            counts[section] = 0
    return counts


def format_nav_label(section: str, counts: dict[str, int]) -> str:
    count = int(counts.get(section, 0) or 0)
    return f"{section}  🔴 {count}" if count else section


def render_sidebar_navigation(current: dict):
    nav = ROLE_SECTIONS.get(current["role"])
    if nav:
        nav_title, state_key, sections = nav
        st.markdown(f"### {nav_title}")

        # Use an explicit widget key that is the same key read by the
        # workspace renderer. This prevents the common Streamlit "one click
        # behind" navigation issue where a page falls back to the dashboard
        # and only opens the requested section after a second click.
        if state_key not in st.session_state or st.session_state[state_key] not in sections:
            persisted = None
            try:
                if st.query_params.get("pf_role") == current.get("role"):
                    persisted = st.query_params.get("pf_section")
            except Exception:
                persisted = None
            st.session_state[state_key] = persisted if persisted in sections else sections[0]

        # Streamlit updates widget session_state before the script reruns. That
        # means the clicked section is already available here, so we can clear
        # its red badge *before* rendering the radio label and without forcing
        # another st.rerun(). The page now opens on the first click.
        preselected = st.session_state.get(state_key, sections[0])
        counts = _build_attention_count_map(current, list(sections))
        if int(counts.get(preselected, 0) or 0) > 0:
            mark_section_attention_seen(current, preselected)
            counts[preselected] = 0

        selected = st.radio(
            nav_title,
            sections,
            key=state_key,
            label_visibility="collapsed",
            format_func=lambda sec: format_nav_label(sec, counts),
        )
        try:
            # Avoid rewriting URL query params on every rerun. Updating them
            # unnecessarily can trigger extra browser/history work and make
            # navigation feel slower.
            if st.query_params.get("pf_section") != selected:
                st.query_params["pf_section"] = selected
            if st.query_params.get("pf_role") != current.get("role", ""):
                st.query_params["pf_role"] = current.get("role", "")
        except Exception:
            pass
    else:
        st.info("No navigation has been configured for this role.")


def main():
    st.set_page_config(
        page_title="ProcureFlow Enterprise Procurement",
        page_icon="🧾",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    boot_database_once()

    if not require_user():
        login_panel()
        return

    current = st.session_state["user"]
    inject_shell_css()
    render_top_header(current)

    with st.sidebar:
        render_sidebar_navigation(current)
        st.divider()
        render_notification_panel(current)
        st.divider()
        logout_button()

    render_app()


if __name__ == "__main__":
    main()
