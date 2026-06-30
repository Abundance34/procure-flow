import streamlit as st

from core.db import init_db, df_query, run_query, now_iso
from core.auth import initialize_browser_session_storage, login_panel, logout_button, require_user
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
    "Logistics Officer": "Logistics Workspace",
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
            "Low-Value Approvals",
            "Utility Head / Facility Head Inbox",
            "Import Center",
            "Sourcing",
            "Vendor Quotes",
            "Vendor Recommendation",
            "Commercial PO Management",
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
    "Logistics Officer": (
        "Logistics Navigation",
        "logistics_section",
        [
            "Logistics Dashboard",
            "PO Delivery Handover",
            "Delivery Tracking",
            "Receiving Slips",
            "Delivery Exceptions & Returns",
            "Gateway Pass Coordination",
            "Logistics Documents",
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
            "All Activity & Evidence Ledger",
            "Procurement Records",
            "Sourcing & Vendor Quote Audit",
            "Purchase Order & Logistics Evidence",
            "Receiving Slips, Proof of Delivery & Returns",
            "Finance, Invoice & Payment Audit",
            "Approval Trails",
            "Delegated Approval Review",
            "Payment Payee / Bank Detail Access Audit",
            "Gateway Pass Audit",
            "Document Archive & Download Audit",
            "Notification Delivery Audit",
            "User & Security Audit",
            "Vendor History",
            "Budget Audit",
            "Facility / Utility Handoff Trail",
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
    """Create the attention-read table once per browser session.

    Sidebar navigation runs on every Streamlit rerun.  Repeating CREATE TABLE /
    CREATE INDEX calls for every section makes a simple tab click unnecessarily
    slow on local Windows SQLite installations.  The schema is still created
    safely when needed, but subsequent clicks use the session guard.
    """
    if st.session_state.get("_pf_section_attention_schema_ready"):
        return
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
        st.session_state["_pf_section_attention_schema_ready"] = True
    except Exception:
        # Keep navigation available even if a first-run database migration is
        # temporarily blocked. The next rerun can retry.
        pass


def _section_last_seen(current: dict, section: str) -> str:
    """Return one section's last seen timestamp for compatibility callers."""
    return _section_last_seen_map(current, [section]).get(section, "1970-01-01 00:00:00")


def _section_last_seen_map(current: dict, sections: list[str]) -> dict[str, str]:
    """Fetch all sidebar last-seen markers in one query.

    This replaces one SQLite connection per sidebar section. It keeps the same
    WhatsApp-style badge behaviour while avoiding dozens of database round trips
    each time a user clicks a tab.
    """
    _ensure_section_seen_schema()
    default = "1970-01-01 00:00:00"
    unique_sections = list(dict.fromkeys(str(section) for section in sections if section))
    if not unique_sections:
        return {}
    try:
        placeholders = ",".join("?" for _ in unique_sections)
        rows = df_query(
            f"""
            SELECT section, last_seen_at
            FROM section_attention_reads
            WHERE user_id=? AND role=? AND section IN ({placeholders})
            """,
            (int(current.get("id") or 0), str(current.get("role") or ""), *unique_sections),
        )
        found = {
            str(row["section"]): str(row["last_seen_at"] or default)
            for _, row in rows.iterrows()
        }
        return {section: found.get(section, default) for section in unique_sections}
    except Exception:
        return {section: default for section in unique_sections}


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
        # Opening a section clears only the red attention badge by updating
        # section_attention_reads. It must NOT mark notification records as read,
        # otherwise users think the notification never arrived after they open
        # the tab. The bell panel remains unread until the user clicks
        # "Mark all as read".
    except Exception:
        pass


def _unread_attention_counts(current: dict, sections: list[str]) -> dict[str, int]:
    """Return unread notification badge counts for every section in one query."""
    unique_sections = list(dict.fromkeys(str(section) for section in sections if section))
    if not unique_sections:
        return {}
    try:
        placeholders = ",".join("?" for _ in unique_sections)
        rows = df_query(
            f"""
            SELECT n.section_target AS section, COUNT(*) AS count
            FROM notifications n
            LEFT JOIN section_attention_reads seen
              ON seen.user_id=?
             AND seen.role=?
             AND seen.section=n.section_target
            WHERE n.is_read=0
              AND (n.user_id=? OR n.role=? OR n.role='All')
              AND n.section_target IN ({placeholders})
              AND datetime(n.created_at) > datetime(COALESCE(seen.last_seen_at, '1970-01-01 00:00:00'))
            GROUP BY n.section_target
            """,
            (
                int(current.get("id") or 0),
                str(current.get("role") or ""),
                int(current.get("id") or 0),
                str(current.get("role") or ""),
                *unique_sections,
            ),
        )
        return {
            str(row["section"]): int(row["count"] or 0)
            for _, row in rows.iterrows()
            if row.get("section") is not None
        }
    except Exception:
        return {}


def _count_since(sql: str, params: tuple, seen_at: str) -> int:
    return _nav_count_query(sql, tuple(params) + (seen_at,))


def attention_count_for_section(
    current: dict,
    section: str,
    seen_at: str | None = None,
    unread_count: int | None = None,
) -> int:
    """Return WhatsApp-style *new since opened* counts per role section.

    ``seen_at`` and ``unread_count`` are optional batch inputs used by the
    sidebar. The public two-argument form is retained for compatibility.
    """
    role = current.get("role")
    uid = int(current.get("id") or 0)
    if seen_at is None:
        seen_at = _section_last_seen(current, section)
    if unread_count is None:
        unread_count = _unread_attention_counts(current, [section]).get(section, 0)
    unread = int(unread_count or 0)
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
            "Gateway Pass Review": ("SELECT COUNT(*) FROM gateway_passes gp WHERE (gp.status IN ('Sent for Procurement Review','Submitted','Reviewed by Procurement','Pending Procurement Manager / Approver Review') OR gp.next_role='procurement_manager') AND datetime(COALESCE(gp.updated_at, gp.created_at)) > datetime(?)", ()),
            "Post-Payment Closure": ("SELECT COUNT(*) FROM purchase_requests WHERE (status IN ('Paid','Receipt Uploaded','Payment Submitted for Verification','Completed','Closed') OR (next_role='procurement_manager' AND payment_status='Paid')) AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Purchase Requests": ("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Submitted','Procurement Review','Requires Sourcing','Vendor Quote Collection','Approved') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Low-Value Approvals": (
                "SELECT "
                "(SELECT COUNT(*) FROM purchase_requests WHERE COALESCE(estimated_amount,0) <= 100000 "
                " AND status IN ('Draft','Sent for Procurement Review','Submitted','Reviewed by Procurement','Vendor Recommendation','Submitted for Approval')) "
                "+ (SELECT COUNT(*) FROM purchase_orders WHERE COALESCE(total_amount,0) <= 100000 "
                " AND status IN ('Draft','Pending Approval')) "
                "+ (SELECT COUNT(*) FROM payments WHERE COALESCE(amount,0) <= 100000 "
                " AND status='Pending Approval' AND COALESCE(next_role,'procurement_manager')='procurement_manager')",
                (),
            ),
            "Commercial PO Management": ("SELECT COUNT(*) FROM purchase_orders WHERE status IN ('Draft','Pending Approval','Approved') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
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
    elif role == "Logistics Officer":
        mapping = {
            "PO Delivery Handover": ("SELECT COUNT(*) FROM purchase_orders WHERE (next_role='logistics_officer' OR status='Released to Logistics') AND COALESCE(logistics_status,'Awaiting Handover') IN ('Awaiting Handover','Not Released') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Delivery Tracking": ("SELECT COUNT(*) FROM purchase_orders WHERE next_role='logistics_officer' AND status IN ('Scheduled','Dispatched','In Transit','Delayed','Arrived','Awaiting Delivery','Sent to Vendor') AND datetime(COALESCE(delivery_updated_at, updated_at, created_at)) > datetime(?)", ()),
            "Receiving Slips": ("SELECT COUNT(*) FROM purchase_orders WHERE next_role='logistics_officer' AND status IN ('Arrived','Awaiting Delivery','Partially Received') AND COALESCE(receiving_status,'Pending Receipt') IN ('Pending Receipt','Partially Received','Disputed') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Delivery Exceptions & Returns": ("SELECT COUNT(*) FROM logistics_exceptions WHERE status IN ('Open','In Progress') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Gateway Pass Coordination": ("SELECT COUNT(*) FROM gateway_passes WHERE status IN ('Approved','Generated','Downloaded') AND datetime(COALESCE(logistics_updated_at, updated_at, created_at)) > datetime(?)", ()),
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
            "Pending Approvals": (
                "SELECT COUNT(*) FROM purchase_requests pr LEFT JOIN users u ON u.id=pr.requested_by "
                "WHERE pr.status IN ('Submitted for Approval','Pending Approval','Pending Approver/MD Approval') "
                "AND (COALESCE(pr.estimated_amount,0) > 100000 OR (COALESCE(pr.estimated_amount,0) <= 100000 AND u.role='Procurement Manager')) "
                "AND datetime(COALESCE(pr.updated_at, pr.created_at)) > datetime(?)",
                (),
            ),
            "PO Approval": ("SELECT COUNT(*) FROM purchase_orders WHERE status='Pending Approval' AND COALESCE(total_amount,0) > 100000 AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Payment Approval": ("SELECT COUNT(*) FROM payments WHERE status='Pending Approval' AND COALESCE(amount,0) > 100000 AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Gateway Pass Approval": ("SELECT COUNT(*) FROM gateway_passes WHERE (status IN ('Submitted for Approval','Pending Approval') OR next_role='approver') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Availability / Away Notice": ("SELECT COUNT(*) FROM user_availability WHERE user_id=? AND status NOT IN ('Returned','Cancelled') AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", (uid,)),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    elif role == "Auditor":
        mapping = {
            "Audit Dashboard": ("SELECT COUNT(*) FROM audit_events WHERE datetime(occurred_at) > datetime(?)", ()),
            "All Activity & Evidence Ledger": ("SELECT COUNT(*) FROM audit_events WHERE datetime(occurred_at) > datetime(?)", ()),
            "Sourcing & Vendor Quote Audit": ("SELECT COUNT(*) FROM sourcing_tasks WHERE datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Purchase Order & Logistics Evidence": ("SELECT COUNT(*) FROM purchase_orders WHERE datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Receiving Slips, Proof of Delivery & Returns": ("SELECT COUNT(*) FROM receiving_slips WHERE datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Finance, Invoice & Payment Audit": ("SELECT COUNT(*) FROM payments WHERE datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Payment Payee / Bank Detail Access Audit": ("SELECT COUNT(*) FROM payment_payee_details WHERE datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Gateway Pass Audit": ("SELECT COUNT(*) FROM gateway_passes WHERE datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Document Archive & Download Audit": ("SELECT COUNT(*) FROM audit_events WHERE action LIKE '%DOWNLOAD%' AND datetime(occurred_at) > datetime(?)", ()),
            "Notification Delivery Audit": ("SELECT COUNT(*) FROM notification_outbox WHERE datetime(COALESCE(sent_at, last_failure_at, created_at)) > datetime(?)", ()),
            "User & Security Audit": ("SELECT COUNT(*) FROM audit_events WHERE (action LIKE '%LOGIN%' OR action LIKE '%PASSWORD%' OR action LIKE '%DENIED%') AND datetime(occurred_at) > datetime(?)", ()),
            "Approval Trails": ("SELECT COUNT(*) FROM approval_history WHERE datetime(created_at) > datetime(?)", ()),
            "Delegated Approval Review": ("SELECT COUNT(*) FROM approval_delegations WHERE enabled=1 AND datetime(COALESCE(updated_at, created_at)) > datetime(?)", ()),
            "Budget Audit": ("SELECT COUNT(*) FROM budget_history WHERE datetime(created_at) > datetime(?)", ()),
            "Compliance Reports": ("SELECT COUNT(*) FROM notifications WHERE (role='Auditor' OR user_id=?) AND is_read=0 AND datetime(created_at) > datetime(?)", (uid,)),
        }
        value = mapping.get(section)
        if value:
            count = _count_since(value[0], value[1], seen_at)
    return max(int(count or 0), int(unread or 0))


def _build_attention_count_map(current: dict, sections: list[str]) -> dict[str, int]:
    """Build sidebar red-badge counts with batched shared lookups.

    Per-section workflow counts remain accurate, but shared last-seen and
    notification calculations are now fetched once, which removes most of the
    repeated SQLite work from ordinary tab navigation.
    """
    _ensure_section_seen_schema()
    seen_map = _section_last_seen_map(current, sections)
    unread_map = _unread_attention_counts(current, sections)
    counts: dict[str, int] = {}
    for section in sections:
        try:
            counts[section] = int(
                attention_count_for_section(
                    current,
                    section,
                    seen_at=seen_map.get(section, "1970-01-01 00:00:00"),
                    unread_count=unread_map.get(section, 0),
                )
                or 0
            )
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

        # Programmatic navigation requests are stored under a separate pending
        # key because Streamlit forbids writing to a widget-backed key after
        # that widget has been instantiated in the same run. Action buttons can
        # safely set _pending_nav_<state_key>, rerun, and this block applies the
        # destination before the sidebar radio is created.
        pending_key = f"_pending_nav_{state_key}"
        pending_section = st.session_state.pop(pending_key, None)
        if pending_section in sections:
            st.session_state[state_key] = pending_section

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
    initialize_browser_session_storage()

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
