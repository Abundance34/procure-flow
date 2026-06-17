from __future__ import annotations

import base64
import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from html import escape

import pandas as pd
import streamlit as st

from core.auth import change_password_panel, has_permission, hash_password
from core.db import (
    BACKUP_DIR, DB_PATH, add_workflow_event, active_delegation, create_activity_log,
    create_notification, df_query, json_dump, log_audit, make_ref, month_key, notify,
    notify_related_users, now_iso, run_insert, run_query, transition_request_status, email_delivery_ready
)
from core.legacy_import import bundled_legacy_zip_path, import_procurement_zip, import_uploaded_zip
from core.ocr import duplicate_candidates, extract_text, match_invoice_to_po, parse_ocr_text
from core.ui import badge, dataframe, empty_state, inject_css, money, workflow_progress

EXPENSE_CATEGORIES = ["Diesel/Fuel", "Water", "Office Supplies", "Repairs/Maintenance", "Vehicle Maintenance", "Generator Maintenance", "Plumbing", "Welding/Fabrication", "Grass Cutting", "Transport/Logistics", "Staff Welfare", "ICT/Software", "Utilities", "Construction Materials", "Professional Services", "Operational Purchases", "Other"]
PR_STATUSES = ["FM Draft", "Submitted to Procurement Manager", "PM Reviewing", "Returned to Facility Manager", "Accepted by Procurement Manager", "Converted to Purchase Request", "Draft", "Submitted", "Procurement Review", "Requires Sourcing", "Vendor Quote Collection", "Vendor Recommendation", "Pending Approval", "Pending Approver/MD Approval", "Approved", "Rejected", "Returned", "PO Created", "PO Approved", "Sent to Vendor", "Awaiting Delivery", "Partially Received", "Fully Received", "Invoice Uploaded", "Invoice Matched to PO", "Finance Review", "Approved for Payment", "Payment Approved", "Paid", "Closed"]
PO_STATUSES = ["Draft", "Pending Approval", "Approved", "Sent to Vendor", "Awaiting Delivery", "Partially Received", "Fully Received", "Invoiced", "Paid", "Closed", "Cancelled"]
PAYMENT_STATUSES = ["Pending Approval", "Approved", "Paid", "Rejected", "Returned"]
PAYMENT_METHODS = ["Cash", "Bank Transfer", "POS/Card", "Cheque", "Mobile Money"]
PRIORITIES = ["Low", "Normal", "High", "Urgent"]
RECEIVING_STATUSES = ["Pending Receipt", "Partially Received", "Fully Received", "Disputed", "Returned"]


def user() -> dict[str, Any]:
    return st.session_state["user"]


def require(perm: str) -> bool:
    if has_permission(perm):
        return True
    st.warning("You do not have permission to perform this action.")
    return False


def _flash_success(message: str):
    """Persist a green success message across Streamlit reruns.

    This prevents users from double-clicking create/submit/approve/pay buttons
    because the confirmation remains visible after the page refreshes.
    """
    if message:
        st.session_state["pf_flash_success"] = str(message)
    st.success(message)


def _rerun_success(message: str):
    _flash_success(message)
    st.rerun()


def role_header(title: str, subtitle: str):
    inject_css()
    st.markdown(f"""
    <div class="pf-hero">
        <h1 style="margin:0;">{title}</h1>
        <p>{subtitle}</p>
    </div>
    """, unsafe_allow_html=True)
    flash = st.session_state.pop("pf_flash_success", None)
    if flash:
        st.success(flash)


def metric_row(metrics: list[tuple[str, Any, str | None]], cols: int = 4):
    columns = st.columns(cols)
    for i, (label, value, help_text) in enumerate(metrics):
        columns[i % cols].metric(label, value, help=help_text)


def csv_download(df: pd.DataFrame, name: str):
    if df is None or df.empty:
        return
    from core.report_service import build_excel_workbook, excel_mime
    payload = build_excel_workbook({"Detailed Records": df}, name)
    st.download_button(f"Download {name.replace('_', ' ').title()} Excel", payload, f"{name}.xlsx", excel_mime())


def vendor_options(include_blank: bool = True):
    df = df_query("SELECT id, name FROM vendors WHERE status != 'Suspended' ORDER BY name")
    opts = {"No vendor selected": None} if include_blank else {}
    for _, row in df.iterrows():
        opts[row["name"]] = int(row["id"])
    return opts


def department_options():
    df = df_query("SELECT name FROM departments WHERE status='Active' ORDER BY name")
    return df["name"].tolist() if not df.empty else ["General"]


def save_upload(uploaded_file, folder: str):
    if uploaded_file is None:
        return None, None
    import hashlib, re
    from core.db import ATTACHMENT_DIR
    data = uploaded_file.getvalue()
    fhash = hashlib.sha256(data).hexdigest()
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", uploaded_file.name).strip()
    target_dir = ATTACHMENT_DIR / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{date.today().isoformat()}_{make_ref('FILE')}_{clean}"
    path.write_bytes(data)
    return str(path), fhash


def render_app():
    if int(user().get("must_change_password") or 0):
        role_header("Password Change Required", "An administrator has required a password update before you continue.")
        change_password_panel()
        return
    role = user()["role"]
    if role == "Admin":
        admin_console()
    elif role == "Procurement Manager":
        procurement_workspace()
    elif role == "Facility Manager":
        facility_workspace()
    elif role == "Finance":
        finance_workspace()
    elif role == "Approver":
        executive_workspace()
    elif role == "Auditor":
        audit_workspace()
    else:
        role_header("ProcureFlow", "Your role is not configured.")
        change_password_panel()

# ---------------- Admin ----------------

def admin_console():
    role_header("Admin Console", "System administration, data import, configuration, users, audit and complete procurement visibility.")

    # The Admin Console now uses the left sidebar for navigation instead of
    # rendering a horizontal tab strip in the main content area.
    section = st.session_state.get("admin_section", "System Overview")

    if section == "System Overview":
        admin_metrics()
        admin_overview()
    elif section == "Users":
        user_management()
    elif section == "Roles & Permissions":
        roles_permissions_page()
    elif section == "Import Center":
        import_center()
    elif section == "Configuration":
        configuration_page()
    elif section == "Backup/Export":
        backup_export_page()
    elif section == "Audit Logs":
        audit_log_page(full=True)
    elif section == "All Records":
        all_records_page()
    elif section == "Settings":
        settings_page()
    else:
        admin_metrics()
        admin_overview()


def admin_metrics():
    q = lambda sql: df_query(sql).iloc[0, 0]
    metrics = [
        ("Total Users", int(q("SELECT COUNT(*) FROM users")), "all accounts"),
        ("Active Users", int(q("SELECT COUNT(*) FROM users WHERE is_active=1")), "can log in"),
        ("Pending Approvals", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Pending Approver/MD Approval','Pending Approval')")), "requests"),
        ("Open Requests", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status NOT IN ('Closed','Rejected','Paid')")), "active pipeline"),
        ("Total Spend", money(q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE status='Approved'")), "approved expenses"),
        ("Imported Documents", int(q("SELECT COUNT(*) FROM imported_legacy_documents")), "legacy archive"),
        ("Audit Events", int(q("SELECT COUNT(*) FROM audit_logs")), "logged actions"),
        ("Open POs", int(q("SELECT COUNT(*) FROM purchase_orders WHERE status NOT IN ('Closed','Cancelled','Paid')")), "purchase orders"),
    ]
    metric_row(metrics, cols=4)


def admin_overview():
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Procurement Pipeline")
        df = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status")
        if not df.empty: st.bar_chart(df.set_index("status"))
        st.subheader("Recently Imported Documents")
        docs = df_query("SELECT id, document_type, department_project, title, total_amount, confidence, import_status FROM imported_legacy_documents ORDER BY created_at DESC LIMIT 10")
        if not docs.empty:
            docs["total_amount"] = docs["total_amount"].apply(money)
            dataframe(docs)
        else: empty_state("No imports yet", "Use the Import Center to import the PROCUREMENT PROJECT ZIP.")
    with c2:
        st.subheader("System Activity")
        logs = df_query("SELECT created_at, action, entity_type, entity_id, details FROM audit_logs ORDER BY created_at DESC LIMIT 12")
        dataframe(logs) if not logs.empty else empty_state("No audit events", "System actions will be logged here.")
        st.subheader("Budget Risk")
        df = budget_risk_df()
        if not df.empty: dataframe(df)
        else: st.success("No budget risk detected.")


def user_management():
    st.subheader("User Management")
    with st.form("create_user"):
        c1, c2, c3 = st.columns(3)
        username = c1.text_input("Username")
        full_name = c2.text_input("Full name")
        role = c3.selectbox("Role", ["Admin", "Procurement Manager", "Finance", "Approver", "Auditor"])
        password = st.text_input("Temporary password", type="password")
        submitted = st.form_submit_button("Create User", type="primary")
    if submitted:
        if not username or not full_name or len(password) < 6:
            st.error("Username, full name, and password are required.")
        else:
            try:
                uid = run_insert("INSERT INTO users (username, full_name, role, password_hash, must_change_password, is_active, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)", (username.strip(), full_name.strip(), role, hash_password(password), now_iso()))
                log_audit("USER_CREATED", "User", uid, f"Role {role}", user()["id"], user()["role"])
                st.success("User created.")
            except Exception as exc:
                st.error(f"Could not create user: {exc}")
    users = df_query("SELECT id, username, full_name, role, is_active, must_change_password, last_login_at, created_at FROM users ORDER BY created_at DESC")
    dataframe(users)
    st.markdown("##### Account Actions")
    if not users.empty:
        label = st.selectbox("Select user", [f"{r.username} — {r.role}" for r in users.itertuples()])
        username = label.split(" — ")[0]
        selected = users[users["username"] == username].iloc[0]
        c1, c2, c3 = st.columns(3)
        if c1.button("Activate/Deactivate"):
            new_status = 0 if int(selected["is_active"]) else 1
            run_query("UPDATE users SET is_active=? WHERE id=?", (new_status, int(selected["id"])))
            log_audit("USER_STATUS_CHANGE", "User", int(selected["id"]), f"is_active={new_status}", user()["id"], user()["role"])
            st.rerun()
        new_role = c2.selectbox("Assign role", ["Admin", "Procurement Manager", "Finance", "Approver", "Auditor"], key="assign_role")
        if c2.button("Update Role"):
            run_query("UPDATE users SET role=? WHERE id=?", (new_role, int(selected["id"])))
            log_audit("ROLE_CHANGE", "User", int(selected["id"]), {"old": selected["role"], "new": new_role}, user()["id"], user()["role"])
            st.rerun()
        reset = c3.text_input("New password", type="password", key="reset_pwd")
        if c3.button("Reset Password") and reset:
            run_query("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?", (hash_password(reset), int(selected["id"])))
            log_audit("PASSWORD_RESET", "User", int(selected["id"]), "Admin reset password", user()["id"], user()["role"])
            st.success("Password reset.")


def roles_permissions_page():
    st.subheader("Role and Permission Management")
    roles = df_query("SELECT * FROM roles ORDER BY name")
    perms = df_query("SELECT * FROM permissions ORDER BY name")
    rp = df_query("SELECT * FROM role_permissions ORDER BY role_name, permission_name")
    c1, c2 = st.columns(2)
    with c1: dataframe(roles)
    with c2: dataframe(perms)
    dataframe(rp)
    with st.form("role_perm_form"):
        role = st.selectbox("Role", roles["name"].tolist() if not roles.empty else [])
        perm = st.selectbox("Permission", perms["name"].tolist() if not perms.empty else [])
        submitted = st.form_submit_button("Grant Permission")
    if submitted:
        run_query("INSERT OR IGNORE INTO role_permissions (role_name, permission_name, created_at) VALUES (?, ?, ?)", (role, perm, now_iso()))
        log_audit("PERMISSION_GRANTED", "Role", role, perm, user()["id"], user()["role"])
        st.success("Permission granted.")


def import_center():
    st.subheader("Data Import Center")
    st.caption("Import the PROCUREMENT PROJECT ZIP. The importer extracts .docx files, ignores Word lock files (~$), parses tables and creates draft purchase requests for review.")
    bundled = bundled_legacy_zip_path()
    c1, c2 = st.columns(2)
    with c1:
        if bundled.exists():
            st.success(f"Bundled legacy ZIP found: {bundled.name}")
            if st.button("Import Bundled PROCUREMENT PROJECT ZIP", type="primary"):
                with st.spinner("Importing real procurement documents..."):
                    summary = import_procurement_zip(bundled, user()["id"])
                st.session_state["last_import_summary"] = summary
                st.success(f"Import complete. Imported {summary['imported']}, skipped {summary['skipped']}, failed {summary['failed']}, partial {summary['partial']}.")
        else:
            st.warning("Bundled legacy ZIP is not present. Upload a ZIP below.")
    with c2:
        upload = st.file_uploader("Upload PROCUREMENT PROJECT ZIP", type=["zip"])
        if st.button("Import Uploaded ZIP"):
            with st.spinner("Importing uploaded ZIP..."):
                summary = import_uploaded_zip(upload, user()["id"])
            st.session_state["last_import_summary"] = summary
            if "error" in summary: st.error(summary["error"])
            else: st.success(f"Import complete. Imported {summary['imported']}, skipped {summary['skipped']}, failed {summary['failed']}.")
    if "last_import_summary" in st.session_state:
        st.json(st.session_state["last_import_summary"])
    st.markdown("##### Import Logs")
    logs = df_query("SELECT created_at, source_zip_name, original_path, action, status, message FROM document_extraction_logs ORDER BY created_at DESC LIMIT 300")
    dataframe(logs) if not logs.empty else empty_state("No import logs", "Run an import to populate this table.")
    st.markdown("##### Review Imported Documents")
    document_archive(editable=True)


def configuration_page():
    st.subheader("Configuration")
    t1, t2, t3 = st.tabs(["Approval Rules", "Budgets & Categories", "Departments"])
    with t1:
        with st.form("rule_form"):
            c1, c2, c3, c4 = st.columns(4)
            category = c1.selectbox("Category", EXPENSE_CATEGORIES)
            threshold = c2.number_input("Threshold amount", min_value=0.0, step=10000.0)
            approver = c3.selectbox("Approver Role", ["Approver", "Finance", "Admin"])
            sourcing = c4.checkbox("Requires sourcing")
            submitted = st.form_submit_button("Save Rule")
        if submitted:
            run_query("INSERT INTO approval_rules (category, threshold_amount, approver_role, requires_sourcing, requires_finance, is_active, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)", (category, threshold, approver, int(sourcing), now_iso()))
            log_audit("APPROVAL_RULE_CREATED", "ApprovalRule", category, f"Threshold {threshold}", user()["id"], user()["role"])
            st.success("Rule saved.")
        dataframe(df_query("SELECT * FROM approval_rules ORDER BY created_at DESC"))
    with t2:
        budgets_page(show_header=False)
        st.markdown("##### Categories")
        with st.form("cat_form"):
            cat = st.text_input("New category")
            if st.form_submit_button("Add Category") and cat:
                run_query("INSERT OR IGNORE INTO categories (name, category_type, status, created_at) VALUES (?, 'Procurement', 'Active', ?)", (cat, now_iso()))
                st.success("Category added.")
        dataframe(df_query("SELECT name, category_type, status FROM categories ORDER BY name"))
    with t3:
        with st.form("dept_form"):
            dept = st.text_input("Department / project")
            desc = st.text_input("Description")
            if st.form_submit_button("Add Department") and dept:
                run_query("INSERT OR IGNORE INTO departments (name, description, status, created_at) VALUES (?, ?, 'Active', ?)", (dept, desc, now_iso()))
                st.success("Department added.")
        dataframe(df_query("SELECT name, description, status FROM departments ORDER BY name"))


def backup_export_page():
    st.subheader("Database Backup and Export")
    if st.button("Create SQLite Backup"):
        target = BACKUP_DIR / f"procureflow_backup_{date.today().isoformat()}_{make_ref('DB')}.db"
        shutil.copy2(DB_PATH, target)
        st.success(f"Backup created: {target.name}")
        with open(target, "rb") as f:
            st.download_button("Download Backup", f, file_name=target.name)
    tables = ["users", "vendors", "purchase_requests", "purchase_orders", "receiving_slips", "expenses", "payments", "imported_legacy_documents", "audit_logs"]
    selected = st.selectbox("Export table", tables)
    df = df_query(f"SELECT * FROM {selected}")
    dataframe(df)
    csv_download(df, selected)


def all_records_page():
    tables = ["purchase_requests", "sourcing_tasks", "vendor_quotes", "purchase_orders", "receiving_slips", "invoices", "expenses", "payments", "cash_advances", "vendors", "imported_legacy_documents"]
    table = st.selectbox("Record table", tables)
    df = df_query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 500")
    dataframe(df)
    csv_download(df, table)

# ---------------- Procurement Manager ----------------

def procurement_workspace():
    role_header("Procurement Workspace", "Operational command center for requests, sourcing, vendor quotes, POs, delivery tracking and procurement documents.")
    section = st.session_state.get("procurement_section", "Operations Dashboard")

    if section == "Operations Dashboard":
        procurement_dashboard_metrics()
        procurement_dashboard()
    elif section == "Purchase Requests":
        requests_page(mode="procurement")
    elif section == "Sourcing":
        sourcing_page()
    elif section == "Vendor Quotes":
        quote_page()
    elif section == "Purchase Orders":
        purchase_orders_page()
    elif section == "Receiving Slips":
        receiving_page()
    elif section == "Vendors":
        vendors_page()
    elif section == "Procurement Documents":
        document_archive(editable=True)
    elif section == "Procurement Reports":
        procurement_reports()
    elif section == "Settings":
        settings_page()
    else:
        procurement_dashboard_metrics()
        procurement_dashboard()


def procurement_dashboard_metrics():
    q = lambda sql: df_query(sql).iloc[0, 0]
    metrics = [
        ("Open Requests", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status NOT IN ('Closed','Rejected','Paid')")), "pipeline"),
        ("Needs Review", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status='Submitted'")), "new submissions"),
        ("Requires Sourcing", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Requires Sourcing','Vendor Quote Collection')")), "supplier comparison"),
        ("POs to Create", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status='Approved' AND linked_po_id IS NULL")), "approved requests"),
        ("Pending Delivery", int(q("SELECT COUNT(*) FROM purchase_orders WHERE receiving_status IN ('Pending Receipt','Partially Received')")), "awaiting goods"),
        ("Active Vendors", int(q("SELECT COUNT(*) FROM vendors WHERE status='Active'")), "supplier base"),
    ]
    metric_row(metrics, cols=3)


def procurement_dashboard():
    quick_action_bar(["New Purchase Request", "Create Sourcing Task", "Create Vendor", "Create PO", "Record Receiving Slip", "Import Documents"])
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Requests Requiring Procurement Action")
        df = df_query("SELECT request_no, department_project, category, estimated_amount, status, priority FROM purchase_requests WHERE status IN ('Submitted','Procurement Review','Requires Sourcing','Vendor Quote Collection','Approved') ORDER BY updated_at DESC LIMIT 20")
        if not df.empty:
            df["estimated_amount"] = df["estimated_amount"].apply(money)
            dataframe(df)
        else: st.success("No procurement actions pending.")
    with c2:
        st.subheader("Procurement Pipeline by Status")
        df = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status")
        if not df.empty: st.bar_chart(df.set_index("status"))
    st.subheader("Vendor Performance")
    vendor_performance_table()


def quick_action_bar(labels: list[str]):
    cols = st.columns(min(len(labels), 4))
    for i, label in enumerate(labels):
        if cols[i % len(cols)].button(label, use_container_width=True, key=f"quick_{label}"):
            st.toast(f"Open the relevant tab: {label}")

# ---------------- Finance ----------------

def finance_workspace():
    role_header("Finance Workspace", "Budgets, invoices, payments, expenses, cash advances, reconciliation and financial controls.")
    section = st.session_state.get("finance_section", "Financial Dashboard")

    if section == "Financial Dashboard":
        finance_metrics()
        finance_dashboard()
    elif section == "Expenses":
        expenses_page()
    elif section == "Invoices":
        invoices_page()
    elif section == "Payments":
        payments_page()
    elif section == "Cash Advances":
        cash_advances_page()
    elif section == "Budgets":
        budgets_page()
    elif section == "Vendor Payment Records":
        vendor_payment_records()
    elif section == "Reconciliation":
        reconciliation_page()
    elif section == "Financial Reports":
        finance_reports()
    elif section == "Settings":
        settings_page()
    else:
        finance_metrics()
        finance_dashboard()


def finance_metrics():
    q = lambda sql: df_query(sql).iloc[0, 0]
    advances = df_query("SELECT ca.amount_collected, COALESCE(SUM(ae.amount),0) spent FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id WHERE ca.status='Approved' GROUP BY ca.id")
    outstanding = 0 if advances.empty else (advances["amount_collected"] - advances["spent"]).clip(lower=0).sum()
    metrics = [
        ("Pending Payment Approvals", int(q("SELECT COUNT(*) FROM payments WHERE status='Pending Approval'")), "payments"),
        ("Outstanding Invoices", int(q("SELECT COUNT(*) FROM invoices WHERE status NOT IN ('Paid','Rejected')")), "invoice queue"),
        ("POs Awaiting Payment", int(q("SELECT COUNT(*) FROM purchase_orders WHERE status IN ('Fully Received','Invoiced') AND payment_status!='Paid'")), "ready for finance"),
        ("Cash Advances Open", money(outstanding), "unretired balance"),
        ("Duplicate Warnings", int(q("SELECT COUNT(*) FROM expenses WHERE duplicate_warning=1")), "review required"),
        ("Spend This Month", money(q(f"SELECT COALESCE(SUM(amount),0) FROM expenses WHERE status='Approved' AND substr(expense_date,1,7)='{month_key()}'")), month_key()),
    ]
    metric_row(metrics, cols=3)


def finance_dashboard():
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Budget Utilization")
        df = budget_utilization_df()
        dataframe(df) if not df.empty else empty_state("No budget data", "Create budgets to monitor utilization.")
    with c2:
        st.subheader("Invoice-to-PO Matching Results")
        df = df_query("SELECT invoice_no, total_amount, match_status, mismatch_reasons, status FROM invoices ORDER BY created_at DESC LIMIT 20")
        if not df.empty:
            df["total_amount"] = df["total_amount"].apply(money)
            dataframe(df)
        else: st.info("No invoices uploaded.")
    st.subheader("Spend by Category")
    df = df_query("SELECT category, SUM(amount) total FROM expenses WHERE status='Approved' GROUP BY category")
    if not df.empty: st.bar_chart(df.set_index("category"))

# ---------------- Executive Approver ----------------

def executive_workspace():
    role_header("Executive Approval Workspace", "Decision-focused approvals for high-value requests, sourcing recommendations, purchase orders, payments and budget exceptions.")
    section = st.session_state.get("executive_section", "Approval Dashboard")

    if section == "Approval Dashboard":
        executive_metrics()
        executive_dashboard()
    elif section == "Pending Approvals":
        pending_approval_page()
    elif section == "Quote Comparison":
        quote_comparison_decision_page()
    elif section == "PO Approval":
        po_approval_page()
    elif section == "Payment Approval":
        payment_approval_page()
    elif section == "Executive Reports":
        executive_reports()
    elif section == "Settings":
        settings_page()
    else:
        executive_metrics()
        executive_dashboard()


def executive_metrics():
    q = lambda sql: df_query(sql).iloc[0, 0]
    metrics = [
        ("Requests Awaiting Approval", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Pending Approver/MD Approval','Pending Approval')")), "decisions"),
        ("High-Value Requests", int(q("SELECT COUNT(*) FROM purchase_requests WHERE estimated_amount>=500000 AND status NOT IN ('Closed','Rejected')")), "risk focus"),
        ("POs Awaiting Approval", int(q("SELECT COUNT(*) FROM purchase_orders WHERE status='Pending Approval'")), "purchase orders"),
        ("Payments Awaiting Approval", int(q("SELECT COUNT(*) FROM payments WHERE status='Pending Approval'")), "payments"),
        ("Spend This Month", money(q(f"SELECT COALESCE(SUM(amount),0) FROM expenses WHERE status='Approved' AND substr(expense_date,1,7)='{month_key()}'")), month_key()),
        ("Budget Exceptions", int(q("SELECT COUNT(*) FROM budgets WHERE override_required=1")), "requires attention"),
    ]
    metric_row(metrics, cols=3)


def executive_dashboard():
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Immediate Decisions")
        df = df_query("SELECT id, request_no, department_project, category, estimated_amount, priority, status FROM purchase_requests WHERE status IN ('Pending Approver/MD Approval','Pending Approval') ORDER BY estimated_amount DESC")
        if not df.empty:
            df["estimated_amount"] = df["estimated_amount"].apply(money)
            dataframe(df)
        else: st.success("No requests awaiting approval.")
    with c2:
        st.subheader("Top Vendors by Spend")
        top = df_query("SELECT name, total_spend, completed_orders, rating FROM vendors ORDER BY total_spend DESC LIMIT 10")
        if not top.empty:
            top["total_spend"] = top["total_spend"].apply(money)
            dataframe(top)
    st.subheader("Procurement Risk Alerts")
    alerts = []
    for label, sql in [("Missing supporting documents", "SELECT COUNT(*) c FROM purchase_requests WHERE attachments_json IS NULL OR attachments_json='[]'"), ("Duplicate expense warnings", "SELECT COUNT(*) c FROM expenses WHERE duplicate_warning=1"), ("Unmatched invoices", "SELECT COUNT(*) c FROM invoices WHERE match_status='Mismatch'"), ("Budget risk categories", "SELECT COUNT(*) c FROM budgets WHERE override_required=1")]:
        c = int(df_query(sql).iloc[0]["c"])
        if c: alerts.append((label, c))
    if alerts:
        for label, c in alerts: st.warning(f"{label}: {c}")
    else: st.success("No major risk alerts currently flagged.")

# ---------------- Audit Workspace ----------------

def audit_workspace():
    role_header("Audit & Compliance Workspace", "Read-only audit trail, imported source documents, approval history, vendor selection and compliance review.")
    section = st.session_state.get("audit_section", "Audit Dashboard")

    if section == "Audit Dashboard":
        audit_metrics()
        audit_dashboard()
    elif section == "Procurement Records":
        all_records_page()
    elif section == "Document Archive":
        document_archive(editable=False)
    elif section == "Approval Trails":
        approval_trails_page()
    elif section == "Vendor History":
        vendor_history_page()
    elif section == "Expense Review":
        expense_review_page()
    elif section == "Compliance Reports":
        compliance_reports()
    elif section == "Settings":
        settings_page()
    else:
        audit_metrics()
        audit_dashboard()


def audit_metrics():
    q = lambda sql: df_query(sql).iloc[0, 0]
    metrics = [
        ("Audit Events", int(q("SELECT COUNT(*) FROM audit_logs")), "all time"),
        ("Imported Documents", int(q("SELECT COUNT(*) FROM imported_legacy_documents")), "source archive"),
        ("Duplicate Warnings", int(q("SELECT COUNT(*) FROM expenses WHERE duplicate_warning=1")), "expenses"),
        ("Missing Docs", int(q("SELECT COUNT(*) FROM purchase_requests WHERE attachments_json IS NULL OR attachments_json='[]'")), "requests"),
        ("Rejected Requests", int(q("SELECT COUNT(*) FROM purchase_requests WHERE status='Rejected'")), "review"),
        ("Vendor Bank Changes", int(q("SELECT COUNT(*) FROM audit_logs WHERE action='VENDOR_BANK_DETAIL_CHANGE'")), "sensitive"),
    ]
    metric_row(metrics, cols=3)


def audit_dashboard():
    c1, c2 = st.columns(2)
    with c1:
        audit_log_page(full=False)
    with c2:
        st.subheader("Missing Document Alerts")
        df = df_query("SELECT request_no, department_project, category, estimated_amount, status FROM purchase_requests WHERE attachments_json IS NULL OR attachments_json='[]' ORDER BY created_at DESC LIMIT 20")
        if not df.empty:
            df["estimated_amount"] = df["estimated_amount"].apply(money)
            dataframe(df)
        else: st.success("No missing supporting document alerts.")

# ---------------- Shared Procurement Modules ----------------

def requests_page(mode="procurement"):
    t1, t2, t3 = st.tabs(["Create Request", "Request Register", "Imported Draft Review"])
    with t1: create_request_form()
    with t2: request_register(actions=True)
    with t3: imported_draft_review()


def create_request_form():
    if not has_permission("create_request"):
        st.info("Your role can view requests but cannot create requests.")
        return
    with st.form("request_form"):
        c1, c2, c3 = st.columns(3)
        dept = c1.selectbox("Department / Project", department_options())
        req_date = c2.date_input("Request date", date.today())
        req_required = c3.date_input("Required date", date.today() + timedelta(days=7))
        c4, c5, c6 = st.columns(3)
        cat = c4.selectbox("Category", EXPENSE_CATEGORIES)
        priority = c5.selectbox("Priority", PRIORITIES, index=1)
        vendor_pref = c6.text_input("Vendor preference")
        justification = st.text_area("Business justification")
        attachment = st.file_uploader("Supporting document", type=["docx", "pdf", "jpg", "jpeg", "png"])
        item_count = st.number_input("Line items", 1, 15, 1)
        items, estimated = [], 0.0
        for i in range(int(item_count)):
            c1, c2, c3, c4 = st.columns([1.4, .7, .9, 1])
            item = c1.text_input("Item", key=f"req_item_{i}")
            qty = c2.number_input("Qty", 0.0, value=1.0, step=1.0, key=f"req_qty_{i}")
            unit = c3.number_input("Unit price", 0.0, step=1000.0, key=f"req_unit_{i}")
            icat = c4.selectbox("Category", EXPENSE_CATEGORIES, index=EXPENSE_CATEGORIES.index(cat), key=f"req_cat_{i}")
            total = qty * unit
            estimated += total
            items.append((item, qty, unit, total, icat))
        submitted = st.form_submit_button("Create Draft Request", type="primary")
    if submitted:
        if not justification or not any(i[0] for i in items):
            st.error("Business justification and at least one item are required.")
            return
        path, _ = save_upload(attachment, "requests")
        req_no = make_ref("PR")
        req_id = run_insert("""
            INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, required_date, category, justification, priority, estimated_amount, vendor_preference, status, attachments_json, notes, approval_history_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', ?, '', '[]', ?, ?)
        """, (req_no, user()["id"], dept, req_date.isoformat(), req_required.isoformat(), cat, justification, priority, estimated, vendor_pref, json_dump([path] if path else []), now_iso(), now_iso()))
        for item, qty, unit, total, icat in items:
            if item:
                run_query("INSERT INTO purchase_request_items (request_id, item_name, description, quantity, unit_price, total, category, suggested_vendor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (req_id, item, item, qty, unit, total, icat, vendor_pref, now_iso()))
        add_workflow_event("Purchase Request", req_id, "Created", "Draft", req_no, user()["id"])
        _rerun_success(f"Created {req_no}")


def request_register(actions=True, approver_mode=False):
    c1, c2, c3 = st.columns(3)
    status = c1.selectbox("Status", ["All"] + PR_STATUSES, key=f"status_{approver_mode}")
    dept = c2.selectbox("Department", ["All"] + department_options(), key=f"dept_{approver_mode}")
    term = c3.text_input("Search", key=f"req_search_{approver_mode}")
    sql = """
        SELECT pr.id, pr.request_no, pr.department_project, pr.category, pr.priority, pr.estimated_amount, pr.status, pr.source_type, pr.import_confidence, u.full_name requested_by, pr.justification
        FROM purchase_requests pr LEFT JOIN users u ON pr.requested_by=u.id WHERE 1=1
    """
    params = []
    if status != "All": sql += " AND pr.status=?"; params.append(status)
    if dept != "All": sql += " AND pr.department_project=?"; params.append(dept)
    if term:
        sql += " AND (pr.request_no LIKE ? OR pr.justification LIKE ? OR pr.category LIKE ?)"; params += [f"%{term}%"]*3
    sql += " ORDER BY pr.updated_at DESC, pr.created_at DESC"
    df = df_query(sql, params)
    if df.empty:
        empty_state("No purchase requests", "Create or import purchase requests to begin workflow.")
        return
    display = df.drop(columns=["id", "justification"]).copy()
    display["estimated_amount"] = display["estimated_amount"].apply(money)
    display["status"] = display["status"].apply(lambda x: badge(x))
    st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    selected = st.selectbox("Open request", df["request_no"].tolist(), key=f"open_req_{approver_mode}")
    pr_id = int(df[df["request_no"] == selected].iloc[0]["id"])
    request_detail(pr_id, actions=actions, key_scope=f"request_register_{approver_mode}")
    csv_download(df, "purchase_requests")


def request_detail(pr_id: int, actions=True, key_scope: str | None = None):
    pr = df_query("SELECT pr.*, u.full_name requested_by_name FROM purchase_requests pr LEFT JOIN users u ON pr.requested_by=u.id WHERE pr.id=?", (pr_id,)).iloc[0]
    with st.container(border=True):
        st.markdown(f"### {pr['request_no']} {badge(pr['status'])}", unsafe_allow_html=True)
        workflow_progress(pr["status"], _request_workflow_steps_for_status(pr["status"]))
        metric_row([("Amount", money(pr["estimated_amount"]), None), ("Priority", pr["priority"], None), ("Department", pr["department_project"], None), ("Source", pr["source_type"], None)], cols=4)
        st.write(f"**Justification:** {pr['justification']}")
        st.write(f"**Requested by:** {pr['requested_by_name']}")
    items = df_query("SELECT item_name, quantity, unit_price, total, category, suggested_vendor FROM purchase_request_items WHERE request_id=?", (pr_id,))
    if not items.empty:
        show = items.copy(); show["unit_price"] = show["unit_price"].apply(money); show["total"] = show["total"].apply(money); dataframe(show)
    scope = key_scope or f"request_detail_{pr_id}"
    record_collaboration("Purchase Request", pr_id, key_scope=scope)
    if actions:
        request_actions(pr_id, pr, key_scope=scope)


def request_actions(pr_id: int, pr, key_scope: str | None = None):
    scope = key_scope or "default"
    prefix = f"{scope}_pr_{pr_id}"
    cols = st.columns(6)
    if pr["status"] == "Draft" and has_permission("submit_request") and cols[0].button("Submit", key=f"sub_{prefix}"):
        update_request_status(pr_id, "Submitted", "Submitted", "Request submitted for procurement review")
    if pr["status"] == "Submitted" and has_permission("procurement_review") and cols[1].button("Start Review", key=f"rev_{prefix}"):
        update_request_status(pr_id, "Procurement Review", "Reviewed", "Procurement review started")
    if pr["status"] in ["Submitted", "Procurement Review"] and has_permission("create_sourcing") and cols[2].button("Requires Sourcing", key=f"need_src_{prefix}"):
        update_request_status(pr_id, "Requires Sourcing", "Sourcing Required", "Supplier comparison required")
        create_sourcing_for_request(pr_id)
    if pr["status"] in ["Submitted", "Procurement Review", "Vendor Recommendation"] and has_permission("procurement_review") and cols[3].button("Send to MD", key=f"to_md_{prefix}"):
        update_request_status(pr_id, "Pending Approver/MD Approval", "Sent for Approval", "Awaiting MD approval")
        notify(None, "Approver", "Request pending approval", f"{pr['request_no']} requires approval", "Purchase Request", pr_id)
    if pr["status"] in ["Pending Approver/MD Approval", "Pending Approval"] and has_permission("approve_request") and cols[4].button("Approve", key=f"app_req_{prefix}"):
        approval_action("Purchase Request", pr_id, pr["status"], "Approved", "Approved")
    if pr["status"] in ["Pending Approver/MD Approval", "Pending Approval"] and has_permission("reject_request"):
        reason = st.text_input("Reject/request more information reason", key=f"reason_{prefix}")
        if cols[5].button("Reject", key=f"rej_req_{prefix}"):
            approval_action("Purchase Request", pr_id, pr["status"], "Rejected", "Rejected", reason)


def update_request_status(pr_id: int, status: str, event: str, note: str):
    run_query("UPDATE purchase_requests SET status=?, updated_at=? WHERE id=?", (status, now_iso(), pr_id))
    add_workflow_event("Purchase Request", pr_id, event, status, note, user()["id"])
    _rerun_success(f"{event} completed.")


def approval_action(entity: str, entity_id: int, old_status: str, new_status: str, action: str, reason: str = ""):
    table = "purchase_requests" if entity == "Purchase Request" else "purchase_orders" if entity == "Purchase Order" else "payments"
    run_query(f"UPDATE {table} SET status=?, updated_at=? WHERE id=?", (new_status, now_iso(), entity_id))
    run_query("INSERT INTO approval_history (entity_type, entity_id, action, status_before, status_after, reason, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (entity, entity_id, action, old_status, new_status, reason, user()["id"], now_iso()))
    add_workflow_event(entity, entity_id, action, new_status, reason, user()["id"])
    _rerun_success(f"{entity} {action.lower()}.")


def imported_draft_review():
    docs = df_query("SELECT id, title, document_type, department_project, total_amount, confidence, import_status, linked_request_id FROM imported_legacy_documents ORDER BY created_at DESC LIMIT 300")
    if docs.empty:
        empty_state("No imported drafts", "Admin or Procurement Manager can import the PROCUREMENT PROJECT ZIP.")
        return
    show = docs.copy(); show["total_amount"] = show["total_amount"].apply(money); dataframe(show)
    selected = st.selectbox("Review imported document", docs["title"].tolist())
    doc = docs[docs["title"] == selected].iloc[0]
    if pd.notna(doc["linked_request_id"]):
        request_detail(
            int(doc["linked_request_id"]),
            actions=True,
            key_scope=f"imported_draft_doc_{int(doc['id'])}"
        )
    if has_permission("procurement_review") and st.button("Mark Imported Document Reviewed", key=f"mark_imported_doc_reviewed_{int(doc['id'])}"):
        run_query("UPDATE imported_legacy_documents SET import_status='Reviewed', updated_at=? WHERE id=?", (now_iso(), int(doc["id"])))
        log_audit("IMPORTED_DOCUMENT_REVIEWED", "Imported Document", int(doc["id"]), selected, user()["id"], user()["role"])
        st.rerun()

# Sourcing and quotes

def create_sourcing_for_request(request_id: int):
    existing = df_query("SELECT id FROM sourcing_tasks WHERE request_id=?", (request_id,))
    if not existing.empty: return int(existing.iloc[0]["id"])
    pr = df_query("SELECT request_no, justification FROM purchase_requests WHERE id=?", (request_id,)).iloc[0]
    src_no = make_ref("SRC")
    sid = run_insert("INSERT INTO sourcing_tasks (sourcing_no, request_id, required_item_service, status, created_at, updated_at) VALUES (?, ?, ?, 'Open', ?, ?)", (src_no, request_id, pr["justification"], now_iso(), now_iso()))
    run_query("UPDATE purchase_requests SET linked_sourcing_task_id=?, status='Vendor Quote Collection', updated_at=? WHERE id=?", (sid, now_iso(), request_id))
    add_workflow_event("Sourcing Task", sid, "Created", "Open", src_no, user()["id"])
    return sid


def sourcing_page():
    st.subheader("Sourcing Tasks")
    df = df_query("SELECT st.id, st.sourcing_no, pr.request_no, pr.category, pr.estimated_amount, st.status, st.approval_status, v.name recommended_vendor FROM sourcing_tasks st JOIN purchase_requests pr ON st.request_id=pr.id LEFT JOIN vendors v ON st.recommended_vendor_id=v.id ORDER BY st.created_at DESC")
    if df.empty:
        empty_state("No sourcing tasks", "Mark a request as requiring sourcing to create a supplier comparison task.")
        return
    show = df.copy(); show["estimated_amount"] = show["estimated_amount"].apply(money); dataframe(show)
    selected = st.selectbox("Open sourcing task", df["sourcing_no"].tolist())
    sid = int(df[df["sourcing_no"] == selected].iloc[0]["id"])
    sourcing_detail(sid)


def sourcing_detail(sid: int):
    task = df_query("SELECT st.*, pr.request_no FROM sourcing_tasks st JOIN purchase_requests pr ON st.request_id=pr.id WHERE st.id=?", (sid,)).iloc[0]
    st.markdown(f"### {task['sourcing_no']} {badge(task['status'])}", unsafe_allow_html=True)
    st.write(f"Linked Request: **{task['request_no']}**")
    if has_permission("manage_quotes"):
        quote_form(sid)
    quote_comparison(sid, allow_recommend=has_permission("recommend_vendor"))
    record_collaboration("Sourcing Task", sid)


def quote_page():
    sourcing_page()


def quote_form(sid: int):
    with st.expander("Add Vendor Quote"):
        with st.form(f"quote_{sid}"):
            vendors = vendor_options(True)
            c1, c2, c3 = st.columns(3)
            vname = c1.selectbox("Vendor", list(vendors.keys()), key=f"qv_{sid}")
            manual_vendor = c1.text_input("Or new/manual vendor", key=f"manual_v_{sid}")
            amount = c2.number_input("Quoted amount", min_value=0.0, step=1000.0, key=f"quote_amt_{sid}")
            delivery = c3.number_input("Delivery days", min_value=0.0, value=7.0, step=1.0, key=f"quote_del_{sid}")
            terms = st.text_input("Payment terms")
            warranty = st.text_input("Warranty / guarantee")
            rating = st.slider("Vendor rating", 1, 5, 3, key=f"rating_{sid}")
            notes = st.text_area("Notes")
            attachment = st.file_uploader("Attachment", type=["pdf", "docx", "jpg", "jpeg", "png"], key=f"quote_att_{sid}")
            submitted = st.form_submit_button("Save Quote")
        if submitted:
            path, _ = save_upload(attachment, "quotes")
            final_vendor = manual_vendor.strip() or (vname if vendors[vname] else "")
            run_query("""
                INSERT INTO vendor_quotes (sourcing_task_id, vendor_id, vendor_name, quoted_amount, delivery_time_days, payment_terms, warranty, vendor_rating, notes, attachment_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (sid, vendors[vname], final_vendor, amount, delivery, terms, warranty, rating, notes, path, now_iso()))
            run_query("UPDATE sourcing_tasks SET status='Collecting Quotes', updated_at=? WHERE id=?", (now_iso(), sid))
            add_workflow_event("Sourcing Task", sid, "Quote Added", "Collecting Quotes", final_vendor, user()["id"])
            st.rerun()


def quote_comparison(sid: int, allow_recommend=False):
    quotes = df_query("SELECT vq.*, COALESCE(v.name, vq.vendor_name) vendor FROM vendor_quotes vq LEFT JOIN vendors v ON vq.vendor_id=v.id WHERE sourcing_task_id=?", (sid,))
    if quotes.empty:
        st.info("No quotes captured yet.")
        return
    q = quotes.copy()
    max_amt = max(float(q["quoted_amount"].max()), 1)
    max_del = max(float(q["delivery_time_days"].max()), 1)
    q["score"] = ((1 - q["quoted_amount"] / max_amt) * 45 + (1 - q["delivery_time_days"] / max_del) * 25 + (q["vendor_rating"] / 5) * 30).round(1)
    lowest = q.loc[q["quoted_amount"].idxmin()]
    fastest = q.loc[q["delivery_time_days"].idxmin()]
    best = q.loc[q["vendor_rating"].idxmax()]
    rec = q.loc[q["score"].idxmax()]
    metric_row([("Lowest Price", lowest["vendor"], money(lowest["quoted_amount"])), ("Fastest Delivery", fastest["vendor"], f"{fastest['delivery_time_days']} days"), ("Best Rated", best["vendor"], f"{best['vendor_rating']}/5"), ("Recommended", rec["vendor"], f"Score {rec['score']}")], cols=4)
    display = q[["vendor", "quoted_amount", "delivery_time_days", "payment_terms", "warranty", "vendor_rating", "score", "notes"]].copy()
    display["quoted_amount"] = display["quoted_amount"].apply(money)
    dataframe(display)
    if allow_recommend and st.button("Recommend Highest-Scoring Vendor", key=f"rec_{sid}"):
        run_query("UPDATE vendor_quotes SET score=?, is_recommended=CASE WHEN id=? THEN 1 ELSE 0 END WHERE sourcing_task_id=?", (float(rec["score"]), int(rec["id"]), sid))
        run_query("UPDATE sourcing_tasks SET recommended_vendor_id=?, reason_for_recommendation=?, status='Vendor Recommendation', approval_status='Recommended', updated_at=? WHERE id=?", (int(rec["vendor_id"]) if pd.notna(rec["vendor_id"]) else None, f"Highest weighted score: {rec['score']}", now_iso(), sid))
        req = df_query("SELECT request_id FROM sourcing_tasks WHERE id=?", (sid,)).iloc[0]["request_id"]
        run_query("UPDATE purchase_requests SET status='Vendor Recommendation', updated_at=? WHERE id=?", (now_iso(), int(req)))
        add_workflow_event("Sourcing Task", sid, "Vendor Recommended", "Vendor Recommendation", rec["vendor"], user()["id"])
        notify(None, "Approver", "Vendor recommendation ready", f"Recommended vendor: {rec['vendor']}", "Sourcing Task", sid)
        st.rerun()

# Purchase Orders

def purchase_orders_page():
    t1, t2 = st.tabs(["Create PO", "PO Register"])
    with t1: create_po_form()
    with t2: po_register(actions=True)


def create_po_form():
    if not has_permission("create_po"):
        st.info("Your role cannot create purchase orders.")
        return
    approved = df_query("SELECT id, request_no, estimated_amount FROM purchase_requests WHERE status='Approved' AND linked_po_id IS NULL ORDER BY updated_at DESC")
    if approved.empty:
        st.info("No approved requests awaiting PO creation.")
        return
    with st.form("po_form"):
        req_label = st.selectbox("Approved request", [f"{r.request_no} — {money(r.estimated_amount)}" for r in approved.itertuples()])
        req_id = int(approved[approved["request_no"] == req_label.split(" — ")[0]].iloc[0]["id"])
        vendors = vendor_options(False)
        vname = st.selectbox("Vendor", list(vendors.keys()))
        c1, c2 = st.columns(2)
        po_date = c1.date_input("PO date", date.today())
        expected = c2.date_input("Expected delivery date", date.today() + timedelta(days=7))
        submitted = st.form_submit_button("Create PO", type="primary")
    if submitted:
        items = df_query("SELECT item_name, description, quantity, unit_price, total, category FROM purchase_request_items WHERE request_id=?", (req_id,))
        total = float(items["total"].sum()) if not items.empty else 0.0
        po_no = make_ref("PO")
        po_id = run_insert("INSERT INTO purchase_orders (po_no, request_id, vendor_id, po_date, expected_delivery_date, status, total_amount, payment_status, receiving_status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'Draft', ?, 'Unpaid', 'Pending Receipt', ?, ?, ?)", (po_no, req_id, vendors[vname], po_date.isoformat(), expected.isoformat(), total, user()["id"], now_iso(), now_iso()))
        for _, it in items.iterrows():
            run_query("INSERT INTO purchase_order_items (po_id, item_name, description, quantity, unit_price, total, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (po_id, it["item_name"], it["description"], it["quantity"], it["unit_price"], it["total"], it["category"], now_iso()))
        run_query("UPDATE purchase_requests SET linked_po_id=?, status='PO Created', updated_at=? WHERE id=?", (po_id, now_iso(), req_id))
        add_workflow_event("Purchase Order", po_id, "Created", "Draft", po_no, user()["id"])
        st.success(f"Created PO {po_no}")
        st.rerun()


def po_register(actions=True):
    df = df_query("SELECT po.id, po.po_no, pr.request_no, v.name vendor, po.po_date, po.expected_delivery_date, po.status, po.total_amount, po.payment_status, po.receiving_status FROM purchase_orders po LEFT JOIN purchase_requests pr ON po.request_id=pr.id LEFT JOIN vendors v ON po.vendor_id=v.id ORDER BY po.created_at DESC")
    if df.empty:
        empty_state("No purchase orders", "Create POs from approved requests.")
        return
    show = df.drop(columns=["id"]).copy(); show["total_amount"] = show["total_amount"].apply(money); dataframe(show)
    selected = st.selectbox("Open PO", df["po_no"].tolist(), key="open_po")
    po_id = int(df[df["po_no"] == selected].iloc[0]["id"])
    po_detail(po_id, actions)
    csv_download(df, "purchase_orders")


def po_detail(po_id: int, actions=True):
    po = df_query("SELECT po.*, v.name vendor FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id WHERE po.id=?", (po_id,)).iloc[0]
    st.markdown(f"### {po['po_no']} {badge(po['status'])}", unsafe_allow_html=True)
    workflow_progress(po["status"], PO_STATUSES)
    metric_row([("Vendor", po["vendor"], None), ("Total", money(po["total_amount"]), None), ("Payment", po["payment_status"], None), ("Receiving", po["receiving_status"], None)], cols=4)
    items = df_query("SELECT item_name, quantity, unit_price, total, category FROM purchase_order_items WHERE po_id=?", (po_id,))
    if not items.empty:
        show=items.copy(); show["unit_price"]=show["unit_price"].apply(money); show["total"]=show["total"].apply(money); dataframe(show)
    record_collaboration("Purchase Order", po_id)
    if actions:
        c1, c2, c3 = st.columns(3)
        if po["status"] == "Draft" and has_permission("create_po") and c1.button("Send for PO Approval", key=f"po_send_{po_id}"):
            run_query("UPDATE purchase_orders SET status='Pending Approval', updated_at=? WHERE id=?", (now_iso(), po_id))
            add_workflow_event("Purchase Order", po_id, "Submitted for Approval", "Pending Approval", "PO approval requested", user()["id"])
            notify(None, "Approver", "PO pending approval", f"{po['po_no']} requires approval", "Purchase Order", po_id)
            st.rerun()
        if po["status"] == "Pending Approval" and has_permission("approve_po") and c2.button("Approve PO", key=f"po_app_{po_id}"):
            run_query("UPDATE purchase_orders SET status='Approved', approved_by=?, updated_at=? WHERE id=?", (user()["id"], now_iso(), po_id))
            add_workflow_event("Purchase Order", po_id, "Approved", "Approved", "PO approved", user()["id"])
            st.rerun()
        if po["status"] == "Approved" and has_permission("create_po") and c3.button("Mark Sent to Vendor", key=f"po_vendor_{po_id}"):
            run_query("UPDATE purchase_orders SET status='Sent to Vendor', receiving_status='Pending Receipt', sent_to_vendor_date=?, updated_at=? WHERE id=?", (date.today().isoformat(), now_iso(), po_id))
            add_workflow_event("Purchase Order", po_id, "Sent to Vendor", "Sent to Vendor", "PO sent", user()["id"])
            st.rerun()

# Receiving

def receiving_page():
    t1, t2 = st.tabs(["Record Receiving Slip", "Receiving Register"])
    with t1: create_receiving_form()
    with t2: receiving_register()


def create_receiving_form():
    if not has_permission("receive_goods"):
        st.info("Your role cannot record receiving slips.")
        return
    pos = df_query("SELECT po.id, po.po_no, v.name vendor, po.vendor_id FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id WHERE po.status IN ('Approved','Sent to Vendor','Awaiting Delivery','Partially Received') ORDER BY po.created_at DESC")
    if pos.empty:
        st.info("No approved/sent POs available for receiving.")
        return
    label = st.selectbox("PO", [f"{r.po_no} — {r.vendor}" for r in pos.itertuples()])
    po = pos[pos["po_no"] == label.split(" — ")[0]].iloc[0]
    items = df_query("SELECT id, item_name, quantity FROM purchase_order_items WHERE po_id=?", (int(po["id"]),))
    with st.form("recv_form"):
        c1, c2, c3 = st.columns(3)
        recv_date = c1.date_input("Date received", date.today())
        delivery_note = c2.text_input("Delivery note number")
        status = c3.selectbox("Status", RECEIVING_STATUSES, index=2)
        discrepancy = st.text_area("Discrepancy notes")
        rows = []
        for _, it in items.iterrows():
            c1, c2, c3 = st.columns([1.5, .8, 1])
            c1.write(it["item_name"])
            c2.write(f"Ordered {it['quantity']}")
            qty = c3.number_input("Received", min_value=0.0, value=float(it["quantity"]), key=f"rec_qty_{it['id']}")
            condition = st.selectbox("Condition", ["Good", "Damaged", "Incomplete", "Wrong Item"], key=f"rec_cond_{it['id']}")
            rows.append((int(it["id"]), it["item_name"], float(it["quantity"]), qty, condition))
        attachment = st.file_uploader("Delivery photo/note", type=["pdf", "jpg", "jpeg", "png"])
        submitted = st.form_submit_button("Save Receiving Slip")
    if submitted:
        path, _ = save_upload(attachment, "receiving")
        slip_no = make_ref("GRN")
        slip_id = run_insert("INSERT INTO receiving_slips (slip_no, po_id, vendor_id, received_by, date_received, delivery_note_no, discrepancy_notes, attachment_path, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (slip_no, int(po["id"]), int(po["vendor_id"]), user()["id"], recv_date.isoformat(), delivery_note, discrepancy, path, status, now_iso(), now_iso()))
        for po_item_id, name, ordered, received, condition in rows:
            run_query("INSERT INTO receiving_slip_items (slip_id, po_item_id, item_name, quantity_ordered, quantity_received, item_condition, discrepancy_notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (slip_id, po_item_id, name, ordered, received, condition, "" if condition=="Good" else condition, now_iso()))
        po_status = "Fully Received" if status == "Fully Received" else "Partially Received"
        run_query("UPDATE purchase_orders SET status=?, receiving_status=?, updated_at=? WHERE id=?", (po_status, status, now_iso(), int(po["id"])))
        add_workflow_event("Receiving Slip", slip_id, "Received", status, slip_no, user()["id"])
        notify(None, "Finance", "Goods received", f"{po['po_no']} is ready for invoice/payment review", "Receiving Slip", slip_id)
        st.rerun()


def receiving_register():
    df = df_query("SELECT rs.id, rs.slip_no, po.po_no, v.name vendor, rs.date_received, rs.status, rs.delivery_note_no, rs.discrepancy_notes FROM receiving_slips rs LEFT JOIN purchase_orders po ON rs.po_id=po.id LEFT JOIN vendors v ON rs.vendor_id=v.id ORDER BY rs.created_at DESC")
    if df.empty: empty_state("No receiving slips", "Record delivery against approved purchase orders."); return
    dataframe(df.drop(columns=["id"]))
    selected = st.selectbox("Open slip", df["slip_no"].tolist())
    sid = int(df[df["slip_no"] == selected].iloc[0]["id"])
    dataframe(df_query("SELECT item_name, quantity_ordered, quantity_received, item_condition, discrepancy_notes FROM receiving_slip_items WHERE slip_id=?", (sid,)))
    record_collaboration("Receiving Slip", sid)

# Vendors

def vendors_page():
    t1, t2, t3 = st.tabs(["Create Vendor", "Vendor Register", "Vendor Intelligence"])
    with t1: create_vendor_form()
    with t2: vendor_register()
    with t3: vendor_intelligence()


def create_vendor_form():
    if not has_permission("manage_vendor"):
        st.info("Your role cannot create vendors.")
        return
    with st.form("vendor_form"):
        c1,c2,c3 = st.columns(3)
        name = c1.text_input("Vendor name")
        category = c2.selectbox("Category", EXPENSE_CATEGORIES)
        status = c3.selectbox("Status", ["Active", "Under Review", "Suspended"])
        c4,c5,c6 = st.columns(3)
        phone = c4.text_input("Phone")
        email = c5.text_input("Email")
        tax = c6.text_input("Tax ID")
        address = st.text_area("Address")
        c7,c8,c9 = st.columns(3)
        bank = c7.text_input("Bank")
        account = c8.text_input("Account number")
        rating = c9.slider("Rating", 1, 5, 3)
        submitted = st.form_submit_button("Save Vendor")
    if submitted:
        try:
            vid = run_insert("INSERT INTO vendors (name, category, phone, email, address, bank_name, account_no, tax_id, rating, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (name, category, phone, email, address, bank, account, tax, rating, status, now_iso(), now_iso()))
            add_workflow_event("Vendor", vid, "Created", status, name, user()["id"])
            st.success("Vendor created.")
        except Exception as exc: st.error(f"Could not save vendor: {exc}")


def vendor_register():
    df = df_query("SELECT id, name, category, phone, email, bank_name, account_no, tax_id, rating, completed_orders, total_spend, average_delivery_time, rejection_count, last_purchase_date, status FROM vendors ORDER BY name")
    if df.empty: empty_state("No vendors", "Create a vendor."); return
    show = df.copy(); show["total_spend"] = show["total_spend"].apply(money); dataframe(show)
    if has_permission("manage_vendor"):
        selected = st.selectbox("Update bank details", df["name"].tolist())
        row = df[df["name"] == selected].iloc[0]
        with st.form("bank_update"):
            bank = st.text_input("Bank", value=row["bank_name"] or "")
            account = st.text_input("Account", value=row["account_no"] or "")
            submitted = st.form_submit_button("Update Bank Details")
        if submitted:
            before = {"bank_name": row["bank_name"], "account_no": row["account_no"]}
            after = {"bank_name": bank, "account_no": account}
            run_query("UPDATE vendors SET bank_name=?, account_no=?, updated_at=? WHERE id=?", (bank, account, now_iso(), int(row["id"])))
            log_audit("VENDOR_BANK_DETAIL_CHANGE", "Vendor", int(row["id"]), "Bank details updated", user()["id"], user()["role"], before, after)
            st.warning("Vendor bank detail change recorded in audit log.")
            st.rerun()


def vendor_intelligence():
    vendor_performance_table()
    st.subheader("Duplicate Vendor Detection")
    df = df_query("SELECT lower(trim(name)) normalized_name, COUNT(*) count, GROUP_CONCAT(name, ', ') vendors FROM vendors GROUP BY lower(trim(name)) HAVING COUNT(*)>1")
    dataframe(df) if not df.empty else st.success("No duplicate vendor names detected.")


def vendor_performance_table():
    df = df_query("SELECT name, category, status, rating, completed_orders, total_spend, average_delivery_time, rejection_count, ROUND(((rating/5.0)*45) + (CASE WHEN rejection_count=0 THEN 30 ELSE MAX(0,30-(rejection_count*5)) END) + (CASE WHEN completed_orders>0 THEN 25 ELSE 5 END),1) performance_score FROM vendors ORDER BY performance_score DESC")
    if not df.empty:
        df["total_spend"] = df["total_spend"].apply(money)
        dataframe(df)

# Expenses/Invoices/Payments/Budgets

def expenses_page():
    t1, t2 = st.tabs(["Record Expense / OCR", "Expense Register"])
    with t1: record_expense_ocr()
    with t2: expense_register()


def record_expense_ocr():
    if not has_permission("record_expense"):
        st.info("Your role cannot record expenses.")
        return
    receipt = st.file_uploader("Upload receipt/invoice image or PDF", type=["png", "jpg", "jpeg", "pdf"], key="receipt_file")
    vendors_df = df_query("SELECT id, name, bank_name, account_no, rating FROM vendors")
    if st.button("Extract OCR & Match", disabled=receipt is None):
        text, meta, error = extract_text(receipt)
        parsed = parse_ocr_text(text, vendors_df)
        parsed["file_meta"] = meta; parsed["error"] = error
        st.session_state["expense_ocr"] = parsed
        if error: st.warning(error)
        else: st.success("OCR extraction complete.")
    parsed = st.session_state.get("expense_ocr", {})
    if parsed:
        st.json(parsed)
    fields = parsed.get("fields", {}) if parsed else {}
    with st.form("expense_form"):
        vendors = vendor_options(True)
        vendor_name = fields.get("matched_vendor_name") or "No vendor selected"
        vendor_index = list(vendors.keys()).index(vendor_name) if vendor_name in vendors else 0
        c1,c2,c3 = st.columns(3)
        expense_date = c1.date_input("Date", date.today())
        category = c2.selectbox("Category", EXPENSE_CATEGORIES, index=EXPENSE_CATEGORIES.index(fields.get("category") if fields.get("category") in EXPENSE_CATEGORIES else "Other"))
        vendor = c3.selectbox("Vendor", list(vendors.keys()), index=vendor_index)
        c4,c5,c6 = st.columns(3)
        amount = c4.number_input("Amount", min_value=0.0, value=float(fields.get("total_amount") or 0), step=1000.0)
        tax = c5.number_input("VAT/Tax", min_value=0.0, value=float(fields.get("tax_amount") or 0), step=100.0)
        method = c6.selectbox("Payment method", PAYMENT_METHODS)
        c7,c8,c9 = st.columns(3)
        invoice_no = c7.text_input("Invoice No", value=fields.get("invoice_no") or "")
        receipt_no = c8.text_input("Receipt No", value=fields.get("receipt_no") or "")
        dept = c9.selectbox("Department", department_options())
        po_df = df_query("SELECT po.id, po.po_no, v.name vendor, po.total_amount FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id ORDER BY po.created_at DESC")
        po_options = ["No PO selected"] + [f"{r.po_no} — {r.vendor} — {money(r.total_amount)}" for r in po_df.itertuples()]
        po_label = st.selectbox("Match PO", po_options)
        desc = st.text_area("Description", value=fields.get("description") or "")
        submitted = st.form_submit_button("Submit for Finance Review")
    if submitted:
        path, fhash = save_upload(receipt, "expenses")
        po_id = None
        if po_label != "No PO selected":
            po_id = int(po_df[po_df["po_no"] == po_label.split(" — ")[0]].iloc[0]["id"])
        vendor_id = vendors[vendor]
        match_status, mismatch = match_invoice_to_po(po_id, vendor_id, amount)
        dup = duplicate_candidates(fhash, amount, expense_date.isoformat(), vendor_id)
        exp_no = make_ref("EXP")
        eid = run_insert("""INSERT INTO expenses (expense_no, expense_date, category, description, vendor_id, amount, payment_method, project_department, status, receipt_path, receipt_hash, receipt_no, invoice_no, tax_amount, linked_po_id, invoice_match_status, duplicate_warning, requested_by, ocr_text, ocr_json, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (exp_no, expense_date.isoformat(), category, desc, vendor_id, amount, method, dept, path, fhash, receipt_no, invoice_no, tax, po_id, match_status, 0 if dup.empty else 1, user()["id"], parsed.get("raw_text", ""), json_dump(parsed), "; ".join(mismatch), now_iso()))
        inv_id = run_insert("INSERT INTO invoices (invoice_no, receipt_no, po_id, vendor_id, invoice_date, amount, tax_amount, total_amount, file_path, file_hash, ocr_text, ocr_json, match_status, mismatch_reasons, status, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Uploaded', ?, ?)", (invoice_no, receipt_no, po_id, vendor_id, expense_date.isoformat(), amount-tax, tax, amount, path, fhash, parsed.get("raw_text", ""), json_dump(parsed), match_status, "; ".join(mismatch), user()["id"], now_iso()))
        add_workflow_event("Expense", eid, "Created", "Pending", match_status, user()["id"])
        notify(None, "Finance", "Invoice needs review", f"{exp_no} match status: {match_status}", "Expense", eid)
        st.success(f"Submitted {exp_no}; invoice record created.")
        if not dup.empty: st.warning("Duplicate warning detected.")


def expense_register():
    df = df_query("SELECT e.id, e.expense_no, e.expense_date, e.category, v.name vendor, e.amount, e.status, e.invoice_match_status, e.duplicate_warning, e.invoice_no, e.receipt_no FROM expenses e LEFT JOIN vendors v ON e.vendor_id=v.id ORDER BY e.created_at DESC")
    if df.empty: empty_state("No expenses", "Record expenses and invoices."); return
    show=df.drop(columns=["id"]).copy(); show["amount"]=show["amount"].apply(money); dataframe(show)
    selected = st.selectbox("Open expense", df["expense_no"].tolist())
    exp_id = int(df[df["expense_no"] == selected].iloc[0]["id"])
    row = df_query("SELECT * FROM expenses WHERE id=?", (exp_id,)).iloc[0]
    with st.expander("OCR / notes"):
        try: st.json(json.loads(row["ocr_json"] or "{}"))
        except Exception: st.text(row["ocr_text"] or "")
        st.write(row["notes"] or "")
    if has_permission("approve_expense") and row["status"] == "Pending":
        c1,c2=st.columns(2)
        if c1.button("Approve Expense", key=f"app_exp_{exp_id}"):
            run_query("UPDATE expenses SET status='Approved', approved_by=?, approved_at=? WHERE id=?", (user()["id"], now_iso(), exp_id))
            if pd.notna(row["vendor_id"]):
                run_query("UPDATE vendors SET total_spend=COALESCE(total_spend,0)+?, completed_orders=COALESCE(completed_orders,0)+1, last_purchase_date=?, updated_at=? WHERE id=?", (float(row["amount"]), row["expense_date"], now_iso(), int(row["vendor_id"])))
            add_workflow_event("Expense", exp_id, "Approved", "Approved", "Expense approved", user()["id"])
            st.rerun()
        reason = c2.text_input("Rejection reason", key=f"exp_reason_{exp_id}")
        if c2.button("Reject/Return", key=f"rej_exp_{exp_id}"):
            run_query("UPDATE expenses SET status='Rejected', rejection_reason=?, approved_by=?, approved_at=? WHERE id=?", (reason, user()["id"], now_iso(), exp_id))
            add_workflow_event("Expense", exp_id, "Rejected", "Rejected", reason, user()["id"])
            st.rerun()
    record_collaboration("Expense", exp_id)
    csv_download(df, "expenses")


def invoices_page():
    st.subheader("Invoices")
    df = df_query("SELECT inv.id, inv.invoice_no, po.po_no, v.name vendor, inv.invoice_date, inv.total_amount, inv.match_status, inv.mismatch_reasons, inv.status FROM invoices inv LEFT JOIN purchase_orders po ON inv.po_id=po.id LEFT JOIN vendors v ON inv.vendor_id=v.id ORDER BY inv.created_at DESC")
    if df.empty: empty_state("No invoices", "Upload invoices through Expenses/OCR."); return
    show=df.drop(columns=["id"]).copy(); show["total_amount"]=show["total_amount"].apply(money); dataframe(show)
    if has_permission("review_invoice"):
        selected = st.selectbox("Select invoice", df["id"].astype(str).tolist())
        inv = df[df["id"].astype(str)==selected].iloc[0]
        c1,c2=st.columns(2)
        if c1.button("Mark Finance Review Complete"):
            run_query("UPDATE invoices SET status='Finance Review' WHERE id=?", (int(inv["id"]),))
            add_workflow_event("Invoice", int(inv["id"]), "Finance Review", "Finance Review", "Invoice reviewed", user()["id"])
            st.rerun()
        if c2.button("Create Payment Request"):
            pno=make_ref("PAY")
            run_query("INSERT INTO payments (payment_no, invoice_id, po_id, vendor_id, amount, payment_method, status, created_by, created_at, updated_at) SELECT ?, id, po_id, vendor_id, total_amount, 'Bank Transfer', 'Pending Approval', ?, ?, ? FROM invoices WHERE id=?", (pno, user()["id"], now_iso(), now_iso(), int(inv["id"])))
            notify(None, "Approver", "Payment pending approval", f"{pno} requires approval", "Payment", int(inv["id"]))
            st.success(f"Payment request {pno} created.")


def payments_page():
    st.subheader("Payments")
    df = df_query("SELECT p.id, p.payment_no, v.name vendor, p.amount, p.payment_method, p.payment_date, p.status, p.notes FROM payments p LEFT JOIN vendors v ON p.vendor_id=v.id ORDER BY p.created_at DESC")
    if df.empty: st.info("No payment requests yet.")
    else:
        show=df.drop(columns=["id"]).copy(); show["amount"]=show["amount"].apply(money); dataframe(show)
    if has_permission("manage_payments"):
        st.markdown("##### Manual Payment Request")
        with st.form("manual_payment"):
            vendors=vendor_options(False)
            v=st.selectbox("Vendor", list(vendors.keys()))
            amount=st.number_input("Amount", min_value=0.0, step=1000.0)
            method=st.selectbox("Method", PAYMENT_METHODS)
            notes=st.text_area("Notes")
            submitted=st.form_submit_button("Create Payment Request")
        if submitted:
            pno=make_ref("PAY")
            run_query("INSERT INTO payments (payment_no, vendor_id, amount, payment_method, status, notes, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, 'Pending Approval', ?, ?, ?, ?)", (pno, vendors[v], amount, method, notes, user()["id"], now_iso(), now_iso()))
            add_workflow_event("Payment", int(df_query("SELECT last_insert_rowid()" ).iloc[0,0]) if False else 0, "Created", "Pending Approval", pno, user()["id"])
            st.success("Payment request created.")
            st.rerun()
    if not df.empty and has_permission("approve_payment"):
        selected = st.selectbox("Approve/Pay", df["payment_no"].tolist())
        row = df[df["payment_no"]==selected].iloc[0]
        c1,c2=st.columns(2)
        if row["status"] == "Pending Approval" and c1.button("Approve Payment"):
            run_query("UPDATE payments SET status='Approved', approved_by=?, updated_at=? WHERE id=?", (user()["id"], now_iso(), int(row["id"])))
            add_workflow_event("Payment", int(row["id"]), "Approved", "Approved", selected, user()["id"])
            st.rerun()
        if row["status"] == "Approved" and c2.button("Mark Paid"):
            run_query("UPDATE payments SET status='Paid', paid_by=?, payment_date=?, updated_at=? WHERE id=?", (user()["id"], date.today().isoformat(), now_iso(), int(row["id"])))
            add_workflow_event("Payment", int(row["id"]), "Paid", "Paid", selected, user()["id"])
            st.rerun()


def cash_advances_page():
    st.subheader("Cash Advances")
    t1,t2,t3=st.tabs(["Create", "Retire", "Register"])
    with t1:
        with st.form("cash_adv"):
            c1,c2,c3=st.columns(3)
            emp=c1.text_input("Employee", value=user()["full_name"])
            collected=c2.date_input("Date collected", date.today())
            due=c3.date_input("Due date", date.today()+timedelta(days=7))
            amount=st.number_input("Amount", min_value=0.0, step=1000.0)
            purpose=st.text_area("Purpose")
            submitted=st.form_submit_button("Submit Advance")
        if submitted:
            adv_no=make_ref("ADV")
            aid=run_insert("INSERT INTO cash_advances (advance_no, date_collected, employee_name, amount_collected, purpose, status, created_by, due_date, created_at) VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?, ?)", (adv_no, collected.isoformat(), emp, amount, purpose, user()["id"], due.isoformat(), now_iso()))
            add_workflow_event("Cash Advance", aid, "Created", "Pending", adv_no, user()["id"])
            notify(None, "Finance", "Cash advance pending", adv_no, "Cash Advance", aid)
            st.success("Advance submitted.")
    with t2:
        df = df_query("SELECT ca.id, ca.advance_no, ca.employee_name, ca.amount_collected, COALESCE(SUM(ae.amount),0) spent, ca.amount_collected-COALESCE(SUM(ae.amount),0) balance FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id WHERE ca.status='Approved' GROUP BY ca.id HAVING balance>0")
        if df.empty: st.info("No approved advances with balance.")
        else:
            label=st.selectbox("Advance", [f"{r.advance_no} — {r.employee_name} — {money(r.balance)}" for r in df.itertuples()])
            aid=int(df[df["advance_no"]==label.split(" — ")[0]].iloc[0]["id"])
            with st.form("retire"):
                amt=st.number_input("Amount spent", min_value=0.0, step=1000.0)
                cat=st.selectbox("Category", EXPENSE_CATEGORIES)
                desc=st.text_area("Description")
                submitted=st.form_submit_button("Add Retirement")
            if submitted:
                run_query("INSERT INTO advance_expenses (advance_id, spent_date, description, category, amount, created_at) VALUES (?, ?, ?, ?, ?, ?)", (aid, date.today().isoformat(), desc, cat, amt, now_iso()))
                add_workflow_event("Cash Advance", aid, "Retirement Added", "Approved", desc, user()["id"])
                st.rerun()
    with t3:
        df = df_query("SELECT ca.id, ca.advance_no, ca.employee_name, ca.date_collected, ca.due_date, ca.amount_collected, COALESCE(SUM(ae.amount),0) spent, ca.amount_collected-COALESCE(SUM(ae.amount),0) balance, ca.status FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id GROUP BY ca.id ORDER BY ca.created_at DESC")
        if not df.empty:
            show=df.copy();
            for c in ["amount_collected", "spent", "balance"]: show[c]=show[c].apply(money)
            dataframe(show)
            if has_permission("approve_expense") or has_permission("manage_payments"):
                pending=df[df["status"]=="Pending"]
                if not pending.empty:
                    selected=st.selectbox("Approve pending advance", pending["advance_no"].tolist())
                    aid=int(pending[pending["advance_no"]==selected].iloc[0]["id"])
                    if st.button("Approve Advance"):
                        run_query("UPDATE cash_advances SET status='Approved', approved_by=?, approved_at=? WHERE id=?", (user()["id"], now_iso(), aid))
                        add_workflow_event("Cash Advance", aid, "Approved", "Approved", selected, user()["id"])
                        st.rerun()


def budgets_page(show_header=True):
    if show_header: st.subheader("Budgets")
    if has_permission("manage_budget") or user()["role"] == "Admin":
        with st.form("budget"):
            c1,c2,c3,c4=st.columns(4)
            m=c1.text_input("Month", month_key())
            cat=c2.selectbox("Category", EXPENSE_CATEGORIES)
            dept=c3.selectbox("Department", department_options())
            limit=c4.number_input("Limit", min_value=0.0, step=10000.0)
            submitted=st.form_submit_button("Save Budget")
        if submitted:
            run_query("INSERT INTO budgets (budget_month, category, department_project, limit_amount, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(budget_month, category, department_project) DO UPDATE SET limit_amount=excluded.limit_amount", (m, cat, dept, limit, now_iso()))
            log_audit("BUDGET_UPDATED", "Budget", f"{m}-{cat}-{dept}", f"{money(limit)}", user()["id"], user()["role"])
            st.success("Budget saved.")
    df = budget_utilization_df()
    dataframe(df) if not df.empty else empty_state("No budgets", "Create monthly budgets by category/department.")


def budget_utilization_df():
    m=month_key()
    df = df_query("""
        SELECT b.budget_month, b.category, b.department_project, b.limit_amount,
        COALESCE((SELECT SUM(amount) FROM expenses e WHERE e.status='Approved' AND substr(e.expense_date,1,7)=b.budget_month AND e.category=b.category),0) spent,
        COALESCE((SELECT SUM(total_amount) FROM purchase_orders po WHERE po.status IN ('Approved','Sent to Vendor','Partially Received','Fully Received','Invoiced')),0) committed,
        COALESCE((SELECT SUM(estimated_amount) FROM purchase_requests pr WHERE pr.status IN ('Submitted','Procurement Review','Requires Sourcing','Vendor Quote Collection','Pending Approver/MD Approval') AND pr.category=b.category),0) pending
        FROM budgets b ORDER BY b.budget_month DESC, b.category
    """)
    if df.empty: return df
    df["remaining"] = df["limit_amount"] - df["spent"] - df["committed"] - df["pending"]
    df["usage_percent"] = (((df["spent"]+df["committed"]+df["pending"])/df["limit_amount"])*100).round(1)
    out=df.copy()
    for c in ["limit_amount","spent","committed","pending","remaining"]: out[c]=out[c].apply(money)
    return out


def budget_risk_df():
    raw = budget_utilization_df()
    if raw.empty: return raw
    # usage_percent is numeric before formatting only in raw? function formatted. Recompute compact.
    df=df_query("SELECT category, department_project, limit_amount FROM budgets")
    return raw[raw["usage_percent"].astype(float) >= 80] if "usage_percent" in raw else raw

# Approval pages

def pending_approval_page():
    request_register(actions=True, approver_mode=True)


def quote_comparison_decision_page():
    df = df_query("SELECT st.id, st.sourcing_no, pr.request_no, st.status, st.approval_status, st.reason_for_recommendation FROM sourcing_tasks st JOIN purchase_requests pr ON st.request_id=pr.id WHERE st.status='Vendor Recommendation' OR st.approval_status='Recommended'")
    if df.empty: empty_state("No sourcing recommendations", "Recommended vendor decisions appear here."); return
    dataframe(df)
    selected=st.selectbox("Open recommendation", df["sourcing_no"].tolist())
    sid=int(df[df["sourcing_no"]==selected].iloc[0]["id"])
    quote_comparison(sid, allow_recommend=False)
    c1,c2=st.columns(2)
    if c1.button("Approve Recommended Vendor"):
        task=df_query("SELECT request_id FROM sourcing_tasks WHERE id=?", (sid,)).iloc[0]
        run_query("UPDATE sourcing_tasks SET approval_status='Approved', updated_at=? WHERE id=?", (now_iso(), sid))
        run_query("UPDATE purchase_requests SET status='Approved', updated_at=? WHERE id=?", (now_iso(), int(task["request_id"])))
        add_workflow_event("Sourcing Task", sid, "Approved", "Approved", "Recommendation approved", user()["id"])
        st.rerun()
    if c2.button("Return for More Information"):
        run_query("UPDATE sourcing_tasks SET approval_status='Returned', updated_at=? WHERE id=?", (now_iso(), sid))
        add_workflow_event("Sourcing Task", sid, "Returned", "Returned", "More info requested", user()["id"])
        st.rerun()


def po_approval_page():
    df=df_query("SELECT id, po_no, total_amount, status FROM purchase_orders WHERE status='Pending Approval'")
    if df.empty: st.success("No POs awaiting approval."); return
    for _, row in df.iterrows():
        po_detail(int(row["id"]), actions=True)


def payment_approval_page():
    payments_page()

# Reports/Search/Audit

def global_search():
    st.subheader("Global Search")
    term=st.text_input("Search request numbers, PO numbers, vendors, item names, document text, invoice numbers, status, department")
    if not term: return
    like=f"%{term}%"
    blocks=[]
    queries=[
        ("Requests", "SELECT request_no ref, status, department_project context, justification text FROM purchase_requests WHERE request_no LIKE ? OR status LIKE ? OR department_project LIKE ? OR justification LIKE ?", (like,like,like,like)),
        ("POs", "SELECT po_no ref, status, receiving_status context, payment_status text FROM purchase_orders WHERE po_no LIKE ? OR status LIKE ?", (like,like)),
        ("Vendors", "SELECT name ref, status, category context, email text FROM vendors WHERE name LIKE ? OR category LIKE ?", (like,like)),
        ("Documents", "SELECT file_name ref, import_status status, department_project context, title text FROM imported_legacy_documents WHERE title LIKE ? OR extracted_text LIKE ? OR original_path LIKE ?", (like,like,like)),
        ("Invoices", "SELECT invoice_no ref, status, match_status context, mismatch_reasons text FROM invoices WHERE invoice_no LIKE ? OR receipt_no LIKE ? OR match_status LIKE ?", (like,like,like)),
    ]
    for module, sql, params in queries:
        df=df_query(sql, params)
        if not df.empty:
            df.insert(0,"module",module); blocks.append(df)
    if blocks: dataframe(pd.concat(blocks, ignore_index=True))
    else: st.info("No results found.")


def procurement_reports():
    global_search(); st.divider(); analytics(); csv_download(df_query("SELECT * FROM purchase_requests"), "purchase_requests_export")

def finance_reports():
    global_search(); st.divider(); analytics(); csv_download(df_query("SELECT * FROM expenses"), "expenses_export"); csv_download(df_query("SELECT * FROM payments"), "payments_export")

def executive_reports():
    analytics(); st.subheader("Executive Summary"); dataframe(df_query("SELECT status, COUNT(*) count, COALESCE(SUM(estimated_amount),0) value FROM purchase_requests GROUP BY status"))

def compliance_reports():
    global_search(); st.divider(); st.subheader("Compliance Flags"); expense_review_page(); st.subheader("Audit Export"); df=df_query("SELECT * FROM audit_logs ORDER BY created_at DESC"); dataframe(df); csv_download(df,"audit_report")


def analytics():
    c1,c2=st.columns(2)
    with c1:
        df=df_query("SELECT category, SUM(amount) total FROM expenses WHERE status='Approved' GROUP BY category")
        if not df.empty: st.caption("Spend by Category"); st.bar_chart(df.set_index("category"))
    with c2:
        df=df_query("SELECT COALESCE(v.name,'No vendor') vendor, SUM(e.amount) total FROM expenses e LEFT JOIN vendors v ON e.vendor_id=v.id WHERE e.status='Approved' GROUP BY vendor")
        if not df.empty: st.caption("Spend by Vendor"); st.bar_chart(df.set_index("vendor"))
    c3,c4=st.columns(2)
    with c3:
        df=df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status")
        if not df.empty: st.caption("Requests by Status"); st.bar_chart(df.set_index("status"))
    with c4:
        df=df_query("SELECT receiving_status, COUNT(*) count FROM purchase_orders GROUP BY receiving_status")
        if not df.empty: st.caption("PO Delivery Status"); st.bar_chart(df.set_index("receiving_status"))


def audit_log_page(full=False):
    st.subheader("Audit Log")
    limit=1000 if full else 100
    df=df_query(f"SELECT a.created_at, u.full_name user, a.role, a.action, a.entity_type, a.entity_id, a.details, a.before_values, a.after_values FROM audit_logs a LEFT JOIN users u ON a.user_id=u.id ORDER BY a.created_at DESC LIMIT {limit}")
    dataframe(df) if not df.empty else empty_state("No audit logs", "Sensitive actions will appear here.")
    csv_download(df,"audit_logs")


def approval_trails_page():
    df=df_query("SELECT ah.created_at, ah.entity_type, ah.entity_id, ah.action, ah.status_before, ah.status_after, u.full_name user, ah.reason FROM approval_history ah LEFT JOIN users u ON ah.user_id=u.id ORDER BY ah.created_at DESC")
    dataframe(df) if not df.empty else empty_state("No approval history", "Approvals and rejections will appear here.")


def vendor_history_page():
    vendor_register(); st.subheader("Vendor Related Audit")
    df=df_query("SELECT * FROM audit_logs WHERE entity_type='Vendor' OR action LIKE 'VENDOR%' ORDER BY created_at DESC")
    dataframe(df)


def expense_review_page():
    st.subheader("Expense Review")
    df=df_query("SELECT e.expense_no, e.expense_date, v.name vendor, e.amount, e.status, e.duplicate_warning, e.invoice_match_status, e.receipt_path FROM expenses e LEFT JOIN vendors v ON e.vendor_id=v.id WHERE e.duplicate_warning=1 OR e.receipt_path IS NULL OR e.invoice_match_status='Mismatch' ORDER BY e.created_at DESC")
    if not df.empty:
        df["amount"]=df["amount"].apply(money); dataframe(df)
    else: st.success("No expense compliance flags detected.")


def reconciliation_page():
    st.subheader("Reconciliation")
    df=df_query("SELECT po.po_no, v.name vendor, po.total_amount po_amount, po.payment_status, po.receiving_status, COALESCE(SUM(p.amount),0) payments FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id LEFT JOIN payments p ON po.id=p.po_id GROUP BY po.id")
    if not df.empty:
        for c in ["po_amount","payments"]: df[c]=df[c].apply(money)
        dataframe(df)
    else: st.info("No POs to reconcile.")


def vendor_payment_records():
    df=df_query("SELECT v.name vendor, COUNT(p.id) payments, COALESCE(SUM(p.amount),0) total_paid FROM vendors v LEFT JOIN payments p ON v.id=p.vendor_id AND p.status='Paid' GROUP BY v.id ORDER BY total_paid DESC")
    if not df.empty: df["total_paid"]=df["total_paid"].apply(money); dataframe(df)


def document_archive(editable=False):
    st.subheader("Document Archive")
    c1,c2,c3=st.columns(3)
    dtype=c1.selectbox("Document type", ["All"] + sorted(df_query("SELECT DISTINCT document_type FROM imported_legacy_documents WHERE document_type IS NOT NULL")["document_type"].dropna().tolist() or []), key=f"doc_type_{editable}")
    status=c2.selectbox("Status", ["All", "Imported - Needs Review", "Reviewed", "Approved Historical", "Rejected Import"], key=f"doc_status_{editable}")
    term=c3.text_input("Search documents", key=f"doc_search_{editable}")
    sql="SELECT id, file_name, document_type, department_project, title, likely_date, total_amount, confidence, import_status, original_path, linked_request_id FROM imported_legacy_documents WHERE 1=1"
    params=[]
    if dtype!="All": sql+=" AND document_type=?"; params.append(dtype)
    if status!="All": sql+=" AND import_status=?"; params.append(status)
    if term: sql+=" AND (title LIKE ? OR extracted_text LIKE ? OR original_path LIKE ?)"; params += [f"%{term}%"]*3
    sql += " ORDER BY created_at DESC"
    df=df_query(sql, params)
    if df.empty:
        empty_state("No imported documents", "Use the Admin Import Center to import the PROCUREMENT PROJECT ZIP.")
        return
    show=df.copy(); show["total_amount"]=show["total_amount"].apply(money); dataframe(show)
    selected=st.selectbox("Open document", df["id"].astype(str).tolist(), key=f"open_doc_{editable}")
    doc_id=int(selected)
    doc=df_query("SELECT * FROM imported_legacy_documents WHERE id=?", (doc_id,)).iloc[0]
    with st.expander("Extracted text and parsed JSON", expanded=False):
        st.write(doc["extracted_text"][:5000] if doc["extracted_text"] else "No text extracted")
        try: st.json(json.loads(doc["parsed_json"] or "{}"))
        except Exception: pass
    items=df_query("SELECT row_number, item_name, quantity, unit_price, total_price, category, status_of_purchase FROM parsed_document_line_items WHERE imported_doc_id=?", (doc_id,))
    if not items.empty:
        it=items.copy(); it["unit_price"]=it["unit_price"].apply(money); it["total_price"]=it["total_price"].apply(money); dataframe(it)
    if doc["file_path"] and Path(doc["file_path"]).exists():
        with open(doc["file_path"], "rb") as f: st.download_button("Download Source Word Document", f, file_name=doc["file_name"])
    if editable:
        c1,c2,c3=st.columns(3)
        if c1.button("Mark Reviewed", key=f"doc_rev_{doc_id}"):
            run_query("UPDATE imported_legacy_documents SET import_status='Reviewed', updated_at=? WHERE id=?", (now_iso(), doc_id)); log_audit("IMPORT_REVIEWED","Imported Document",doc_id,doc["title"],user()["id"],user()["role"]); st.rerun()
        if c2.button("Approve Historical", key=f"doc_hist_{doc_id}"):
            run_query("UPDATE imported_legacy_documents SET import_status='Approved Historical', updated_at=? WHERE id=?", (now_iso(), doc_id)); log_audit("IMPORT_APPROVED_HISTORICAL","Imported Document",doc_id,doc["title"],user()["id"],user()["role"]); st.rerun()
        if c3.button("Reject Import", key=f"doc_rej_{doc_id}"):
            run_query("UPDATE imported_legacy_documents SET import_status='Rejected Import', updated_at=? WHERE id=?", (now_iso(), doc_id)); log_audit("IMPORT_REJECTED","Imported Document",doc_id,doc["title"],user()["id"],user()["role"]); st.rerun()


def record_collaboration(entity_type: str, entity_id: int, key_scope: str | None = None):
    """Render timeline/comments for a record.

    Streamlit requires every widget key to be unique in a single script run.
    The same procurement record can be shown in multiple sections at once
    because Streamlit evaluates all tabs. key_scope separates each rendered
    instance while keeping the key deterministic across reruns.
    """
    safe_entity = entity_type.lower().replace(" ", "_").replace("/", "_")
    if key_scope is None:
        st.session_state["_collab_render_seq"] = st.session_state.get("_collab_render_seq", 0) + 1
        key_scope = f"auto_{st.session_state['_collab_render_seq']}"
    scope = key_scope.lower().replace(" ", "_").replace("/", "_")
    form_key = f"comment_{scope}_{safe_entity}_{entity_id}"

    with st.expander("Timeline, Comments & Internal Notes"):
        events=df_query("SELECT we.created_at, we.event, we.status, u.full_name user, we.note FROM workflow_events we LEFT JOIN users u ON we.user_id=u.id WHERE entity_type=? AND entity_id=? ORDER BY we.created_at", (entity_type, entity_id))
        if not events.empty: dataframe(events)
        comments=df_query("SELECT c.created_at, u.full_name user, c.comment_text, c.is_internal FROM comments c LEFT JOIN users u ON c.user_id=u.id WHERE entity_type=? AND entity_id=? ORDER BY c.created_at DESC", (entity_type, entity_id))
        if not comments.empty: dataframe(comments)
        if user()["role"] != "Auditor" or has_permission("audit"):
            with st.form(form_key):
                text=st.text_area("Add comment or audit note", key=f"{form_key}_text")
                internal=st.checkbox("Internal note", key=f"{form_key}_internal")
                submitted=st.form_submit_button("Post")
            if submitted and text:
                run_query("INSERT INTO comments (entity_type, entity_id, comment_text, is_internal, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (entity_type, entity_id, text, int(internal), user()["id"], now_iso()))
                add_workflow_event(entity_type, entity_id, "Comment Added", None, text[:120], user()["id"])
                st.rerun()


def settings_page():
    st.subheader("Settings")
    change_password_panel()
    st.caption("Production mode: set PROCUREFLOW_PRODUCTION=1 to hide demo credentials and require operational password practices.")

# ============================================================================
# Enterprise extension layer
# Later definitions intentionally override the compact MVP pages above while
# reusing its forms, registers, imports, OCR, vendor and PO modules.
# ============================================================================

FM_STATUSES = [
    "FM Draft", "Submitted to Procurement Manager", "PM Reviewing",
    "Returned to Facility Manager", "Accepted by Procurement Manager",
    "Converted to Purchase Request", "Rejected by Procurement Manager",
]
BUDGET_RISK_THRESHOLDS = [(100, "Exceeded"), (85, "Warning"), (70, "Watch"), (0, "Safe")]
APPROVER_ROLE_OPTIONS = ["Approver", "Procurement Manager", "Finance", "Admin"]


def risk_label(percent: float) -> str:
    try:
        pct = float(percent)
    except Exception:
        pct = 0.0
    for threshold, label in BUDGET_RISK_THRESHOLDS:
        if pct >= threshold:
            return label
    return "Safe"


def render_notification_panel(current: dict):
    """Sidebar notification bell/panel with one-time toast behavior."""
    uid = int(current["id"])
    role = current["role"]
    unread = df_query(
        """
        SELECT * FROM notifications
        WHERE is_read=0 AND (user_id=? OR role=? OR role='All')
        ORDER BY created_at DESC LIMIT 20
        """,
        (uid, role),
    )
    pending_popups = df_query(
        """
        SELECT * FROM notifications
        WHERE is_read=0 AND COALESCE(popup_shown,0)=0 AND (user_id=? OR role=? OR role='All')
        ORDER BY created_at DESC LIMIT 5
        """,
        (uid, role),
    )
    for _, n in pending_popups.iterrows():
        st.toast(f"{n['title']}: {n['message']}")
        run_query("UPDATE notifications SET popup_shown=1 WHERE id=?", (int(n["id"]),))

    st.markdown(f"### 🔔 Notifications ({len(unread)})")
    if unread.empty:
        st.caption("No unread notifications.")
        return
    with st.expander("Unread notifications", expanded=False):
        for _, n in unread.head(8).iterrows():
            st.markdown(f"**{n['title']}**")
            st.caption(f"{n['message']} · {n['created_at']}")
            st.divider()
        if st.button("Mark all as read", key=f"notif_mark_all_{uid}_{role}", use_container_width=True):
            run_query("UPDATE notifications SET is_read=1 WHERE is_read=0 AND (user_id=? OR role=? OR role='All')", (uid, role))
            st.rerun()


# ---------------- Central workflow overrides ----------------

def update_request_status(pr_id: int, status: str, event: str, note: str):
    transition_request_status(pr_id, status, event, note, user()["id"], user()["role"])
    _rerun_success(f"{event} completed.")


def approval_action(entity: str, entity_id: int, old_status: str, new_status: str, action: str, reason: str = ""):
    """Centralized approval action so status, history, notifications and audit stay aligned."""
    if entity == "Purchase Request":
        payment_status = "Approved for Payment" if new_status == "Approved" else None
        transition_request_status(
            entity_id,
            new_status,
            action,
            reason or f"{action} by {user()['role']}",
            user()["id"],
            user()["role"],
            "Normal Approval Mode",
            payment_status=payment_status,
        )
        if new_status == "Approved":
            create_notification(None, "Finance", "Approved item ready for Finance", f"A request has been approved and is ready for payment review.", entity, entity_id, "Important")
        _rerun_success(f"{entity} {action.lower()}.")
        return

    table = "purchase_orders" if entity == "Purchase Order" else "payments"
    run_query(f"UPDATE {table} SET status=?, updated_at=? WHERE id=?", (new_status, now_iso(), entity_id))
    run_query(
        """
        INSERT INTO approval_history (entity_type, entity_id, action, status_before, status_after, reason, user_id, approved_by_user_id, approved_by_role, approval_mode, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Normal Approval Mode', ?, ?)
        """,
        (entity, entity_id, action, old_status, new_status, reason, user()["id"], user()["id"], user()["role"], reason, now_iso()),
    )
    add_workflow_event(entity, entity_id, action, new_status, reason, user()["id"])
    create_activity_log(user()["id"], user()["role"], action, entity, entity_id, f"{entity} {action.lower()}", reason, "workflow")
    log_audit(action, entity, entity_id, reason, user()["id"], user()["role"], {"status": old_status}, {"status": new_status})
    _rerun_success(f"{entity} {action.lower()}.")


# ---------------- Admin console overrides ----------------

def admin_console():
    role_header("Admin Console", "Highest-authority workspace for users, budgets, workflow rules, imports, audit control and every system record.")
    section = st.session_state.get("admin_section", "Admin Dashboard")
    if section == "Admin Dashboard":
        admin_metrics(); admin_overview()
    elif section == "Budget Tracker":
        budget_command_center()
    elif section == "User Management":
        user_management()
    elif section == "Roles & Permissions":
        roles_permissions_page()
    elif section == "Approval Configuration":
        approval_configuration_page()
    elif section == "Import Center":
        import_center()
    elif section == "All Procurement Records":
        all_records_page()
    elif section == "Notifications Monitor":
        notifications_monitor_page()
    elif section == "Activity & History Logs":
        activity_history_page(scope="admin")
    elif section == "Audit Logs":
        audit_log_page(full=True)
    elif section == "Backup / Export":
        backup_export_page()
    elif section == "Settings":
        settings_page()
    else:
        admin_metrics(); admin_overview()


def user_management():
    st.subheader("User Management")
    roles = [r["name"] for r in run_query("SELECT name FROM roles ORDER BY name", fetch=True)] or ["Admin", "Procurement Manager", "Facility Manager", "Finance", "Approver", "Auditor"]
    with st.expander("Create new user", expanded=True):
        with st.form("ent_create_user_form"):
            c1, c2, c3 = st.columns(3)
            username = c1.text_input("Username", key="ent_new_username")
            full_name = c2.text_input("Full name", key="ent_new_full_name")
            role = c3.selectbox("Role", roles, key="ent_new_role")
            c4, c5 = st.columns(2)
            password = c4.text_input("Temporary password", type="password", key="ent_new_password")
            force = c5.checkbox("Force password change on next login", value=True, key="ent_new_force")
            submitted = st.form_submit_button("Create User", type="primary")
        if submitted:
            if not username or not full_name or len(password) < 6:
                st.error("Username, full name, and a password of at least 6 characters are required.")
            else:
                try:
                    uid = run_insert(
                        "INSERT INTO users (username, full_name, role, password_hash, must_change_password, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                        (username.strip(), full_name.strip(), role, hash_password(password), int(force), now_iso(), now_iso()),
                    )
                    log_audit("USER_CREATED", "User", uid, f"Created as {role}", user()["id"], user()["role"])
                    create_activity_log(user()["id"], user()["role"], "USER_CREATED", "User", uid, f"Created user {username} as {role}", visibility_scope="admin")
                    create_notification(uid, None, "Account created", "Your ProcureFlow account has been created.", "User", uid)
                    st.success("User created.")
                except Exception as exc:
                    st.error(f"Could not create user: {exc}")

    users = df_query("SELECT id, username, full_name, role, is_active, must_change_password, COALESCE(account_locked,0) account_locked, last_login_at, created_at FROM users ORDER BY role, username")
    dataframe(users)
    if users.empty:
        return

    st.markdown("#### Edit account")
    labels = [f"{r.username} — {r.role} — #{int(r.id)}" for r in users.itertuples()]
    selected_label = st.selectbox("Select user", labels, key="ent_select_user")
    selected_id = int(selected_label.rsplit("#", 1)[1])
    selected = df_query("SELECT * FROM users WHERE id=?", (selected_id,)).iloc[0]

    with st.form(f"ent_edit_user_{selected_id}"):
        c1, c2, c3 = st.columns(3)
        edit_username = c1.text_input("Username", value=selected["username"] or "", key=f"edit_username_{selected_id}")
        edit_full_name = c2.text_input("Full name", value=selected["full_name"] or "", key=f"edit_name_{selected_id}")
        edit_role = c3.selectbox("Role", roles, index=roles.index(selected["role"]) if selected["role"] in roles else 0, key=f"edit_role_{selected_id}")
        c4, c5, c6 = st.columns(3)
        is_active = c4.checkbox("Active", value=bool(selected["is_active"]), key=f"edit_active_{selected_id}")
        must_change = c5.checkbox("Force password change", value=bool(selected["must_change_password"]), key=f"edit_force_{selected_id}")
        locked = c6.checkbox("Locked", value=bool(selected.get("account_locked", 0)), key=f"edit_locked_{selected_id}")
        save_user = st.form_submit_button("Save user changes")
    if save_user:
        before = selected.to_dict()
        run_query(
            "UPDATE users SET username=?, full_name=?, role=?, is_active=?, must_change_password=?, account_locked=?, updated_at=? WHERE id=?",
            (edit_username.strip(), edit_full_name.strip(), edit_role, int(is_active), int(must_change), int(locked), now_iso(), selected_id),
        )
        after = {"username": edit_username, "full_name": edit_full_name, "role": edit_role, "is_active": int(is_active), "must_change_password": int(must_change), "account_locked": int(locked)}
        log_audit("USER_UPDATED", "User", selected_id, "Admin edited user", user()["id"], user()["role"], before, after)
        create_activity_log(user()["id"], user()["role"], "USER_UPDATED", "User", selected_id, f"Updated user {edit_username}", json_dump(after), "admin")
        if before.get("role") != edit_role:
            create_notification(selected_id, None, "Role changed", f"Your role is now {edit_role}.", "User", selected_id, "Important")
        st.success("User updated.")
        st.rerun()

    c1, c2, c3 = st.columns(3)
    new_password = c1.text_input("Overwrite/reset password", type="password", key=f"admin_reset_pwd_{selected_id}")
    force_after_reset = c1.checkbox("Force change after reset", value=True, key=f"admin_force_after_reset_{selected_id}")
    if c1.button("Reset / Overwrite Password", key=f"admin_reset_btn_{selected_id}", disabled=not new_password):
        run_query("UPDATE users SET password_hash=?, must_change_password=?, updated_at=? WHERE id=?", (hash_password(new_password), int(force_after_reset), now_iso(), selected_id))
        log_audit("PASSWORD_RESET", "User", selected_id, "Admin reset/overwrote password", user()["id"], user()["role"])
        create_notification(selected_id, None, "Password reset", "An Admin reset your password. Follow your organization’s password instructions.", "User", selected_id, "Important")
        st.success("Password reset securely. The old password was not required or exposed.")

    if c2.button("Unlock user", key=f"unlock_{selected_id}"):
        run_query("UPDATE users SET account_locked=0, failed_login_count=0, updated_at=? WHERE id=?", (now_iso(), selected_id))
        log_audit("USER_UNLOCKED", "User", selected_id, "Admin unlocked account", user()["id"], user()["role"])
        st.success("User unlocked.")

    st.markdown("#### Facility Manager ⇄ Procurement Manager link")
    facility_users = df_query("SELECT id, full_name, username FROM users WHERE role='Facility Manager' ORDER BY full_name")
    pm_users = df_query("SELECT id, full_name, username FROM users WHERE role='Procurement Manager' ORDER BY full_name")
    if not facility_users.empty and not pm_users.empty:
        with st.form("fm_pm_link_form"):
            fm_label = st.selectbox("Facility Manager", [f"{r.full_name} ({r.username}) #{int(r.id)}" for r in facility_users.itertuples()], key="link_fm")
            pm_label = st.selectbox("Procurement Manager", [f"{r.full_name} ({r.username}) #{int(r.id)}" for r in pm_users.itertuples()], key="link_pm")
            link_submit = st.form_submit_button("Link / Reassign Facility Manager")
        if link_submit:
            fm_id = int(fm_label.rsplit("#", 1)[1]); pm_id = int(pm_label.rsplit("#", 1)[1])
            run_query("UPDATE facility_manager_links SET is_active=0, updated_at=? WHERE facility_manager_user_id=?", (now_iso(), fm_id))
            run_query(
                "INSERT OR REPLACE INTO facility_manager_links (facility_manager_user_id, procurement_manager_user_id, is_active, created_by, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
                (fm_id, pm_id, user()["id"], now_iso(), now_iso()),
            )
            log_audit("FM_PM_LINK_UPDATED", "FacilityManagerLink", fm_id, f"FM {fm_id} linked to PM {pm_id}", user()["id"], user()["role"])
            create_notification(fm_id, None, "Procurement Manager assigned", "You have been linked to a Procurement Manager.", "User", fm_id, "Important")
            create_notification(pm_id, None, "Facility Manager assigned", "A Facility Manager has been linked to your workspace.", "User", fm_id, "Important")
            st.success("Facility Manager link saved.")
    links = df_query("""
        SELECT fml.id, fm.full_name facility_manager, pm.full_name procurement_manager, fml.is_active, fml.created_at, fml.updated_at
        FROM facility_manager_links fml
        LEFT JOIN users fm ON fm.id=fml.facility_manager_user_id
        LEFT JOIN users pm ON pm.id=fml.procurement_manager_user_id
        ORDER BY fml.updated_at DESC, fml.created_at DESC
    """)
    dataframe(links) if not links.empty else st.info("No Facility Manager links yet.")

    st.markdown("#### Role permissions")
    rp = df_query("SELECT role_name, permission_name FROM role_permissions ORDER BY role_name, permission_name")
    dataframe(rp)
    with st.form("grant_revoke_perm_form"):
        c1, c2, c3 = st.columns(3)
        role_for_perm = c1.selectbox("Role", roles, key="perm_role_select")
        perm_list = [r["name"] for r in run_query("SELECT name FROM permissions ORDER BY name", fetch=True)]
        perm_for_role = c2.selectbox("Permission", perm_list, key="perm_select")
        action = c3.selectbox("Action", ["Grant", "Revoke"], key="perm_action")
        perm_submit = st.form_submit_button("Apply permission change")
    if perm_submit:
        if action == "Grant":
            run_query("INSERT OR IGNORE INTO role_permissions (role_name, permission_name, created_at) VALUES (?, ?, ?)", (role_for_perm, perm_for_role, now_iso()))
            audit_action = "PERMISSION_GRANTED"
        else:
            run_query("DELETE FROM role_permissions WHERE role_name=? AND permission_name=?", (role_for_perm, perm_for_role))
            audit_action = "PERMISSION_REVOKED"
        log_audit(audit_action, "Role", role_for_perm, perm_for_role, user()["id"], user()["role"])
        st.success("Permission change recorded.")

    st.markdown("#### Selected user activity")
    history = df_query("SELECT created_at, role, action, entity_type, entity_id, public_summary FROM activity_logs WHERE user_id=? OR related_user_id=? ORDER BY created_at DESC LIMIT 100", (selected_id, selected_id))
    dataframe(history) if not history.empty else st.info("No activity yet for this user.")


def approval_configuration_page():
    st.subheader("Approval Configuration")
    st.caption("Define approval thresholds, backup/fallback behavior, delegated approval and finance/sourcing requirements.")
    with st.form("approval_rule_enterprise_form"):
        c1, c2, c3 = st.columns(3)
        category = c1.selectbox("Category", EXPENSE_CATEGORIES, key="appr_cat")
        threshold = c2.number_input("Threshold amount", min_value=0.0, step=10000.0, key="appr_threshold")
        primary = c3.selectbox("Primary approver role", APPROVER_ROLE_OPTIONS, key="appr_primary")
        c4, c5, c6 = st.columns(3)
        backup = c4.selectbox("Backup approver role", ["None"] + APPROVER_ROLE_OPTIONS, index=2, key="appr_backup")
        pm_fallback = c5.checkbox("Allow Procurement Manager fallback", value=True, key="appr_pm_fallback")
        timeout = c6.number_input("Approval timeout hours", min_value=1, value=48, step=1, key="appr_timeout")
        c7, c8, c9 = st.columns(3)
        finance_required = c7.checkbox("Finance required", value=True, key="appr_finance_req")
        sourcing_required = c8.checkbox("Sourcing required", value=False, key="appr_sourcing_req")
        active = c9.checkbox("Rule active", value=True, key="appr_active")
        submit_rule = st.form_submit_button("Save approval rule", type="primary")
    if submit_rule:
        rid = run_insert(
            """
            INSERT INTO approval_rules (category, threshold_amount, approver_role, requires_sourcing, requires_finance, is_active, primary_approver_role, backup_approver_role, pm_fallback_enabled, finance_required, sourcing_required, approval_timeout_hours, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (category, threshold, primary, int(sourcing_required), int(finance_required), int(active), primary, None if backup == "None" else backup, int(pm_fallback), int(finance_required), int(sourcing_required), int(timeout), now_iso(), now_iso()),
        )
        log_audit("APPROVAL_RULE_CREATED", "ApprovalRule", rid, f"{category} threshold {threshold}", user()["id"], user()["role"])
        st.success("Approval rule saved. Procurement Manager is available as a primary or backup approver.")

    rules = df_query("SELECT * FROM approval_rules ORDER BY is_active DESC, category, threshold_amount")
    dataframe(rules)

    st.markdown("### Delegated Approval")
    current = df_query("SELECT * FROM approval_delegations ORDER BY updated_at DESC, created_at DESC")
    if not current.empty:
        dataframe(current)
    with st.form("approval_delegation_form"):
        c1, c2, c3 = st.columns(3)
        enabled = c1.checkbox("Enable delegated approval", value=bool(current.iloc[0]["enabled"]) if not current.empty else False, key="deleg_enabled")
        start = c2.date_input("Start date", value=date.today(), key="deleg_start")
        end = c3.date_input("End date", value=date.today() + timedelta(days=7), key="deleg_end")
        reason = st.text_area("Reason", value="Approver unavailable / delegated by Admin", key="deleg_reason")
        submit_deleg = st.form_submit_button("Save Delegation")
    if submit_deleg:
        run_query("UPDATE approval_delegations SET enabled=0, updated_at=? WHERE primary_role='Approver' AND delegate_role='Procurement Manager'", (now_iso(),))
        run_query(
            "INSERT INTO approval_delegations (primary_role, delegate_role, enabled, start_date, end_date, reason, created_by, created_at, updated_at) VALUES ('Approver', 'Procurement Manager', ?, ?, ?, ?, ?, ?, ?)",
            (int(enabled), start.isoformat(), end.isoformat(), reason, user()["id"], now_iso(), now_iso()),
        )
        label = "Delegated Approval Mode" if enabled else "Normal Approval Mode"
        log_audit("APPROVAL_DELEGATION_UPDATED", "ApprovalDelegation", "Approver->Procurement Manager", f"{label}: {reason}", user()["id"], user()["role"])
        create_notification(None, "Procurement Manager", "Delegated approval updated", f"{label}. {reason}", "ApprovalDelegation", None, "Important")
        st.success(f"Saved: {label}. Acting Approver: Procurement Manager." if enabled else "Delegation disabled. Normal Approval Mode active.")


def notifications_monitor_page():
    st.subheader("Notifications Monitor")
    df = df_query("SELECT n.*, u.username, u.full_name FROM notifications n LEFT JOIN users u ON n.user_id=u.id ORDER BY n.created_at DESC LIMIT 500")
    dataframe(df) if not df.empty else st.info("No notifications yet.")
    csv_download(df, "notifications")


def all_records_page():
    tables = [
        "users", "roles", "permissions", "role_permissions", "purchase_requests", "purchase_request_items",
        "sourcing_tasks", "vendor_quotes", "purchase_orders", "purchase_order_items", "receiving_slips",
        "invoices", "expenses", "payments", "cash_advances", "vendors", "imported_legacy_documents",
        "annual_budgets", "budgets", "budget_adjustments", "budget_history", "approval_rules",
        "approval_delegations", "approval_history", "facility_manager_links", "collaboration_threads",
        "collaboration_messages", "activity_logs", "workflow_events", "notifications", "audit_logs",
    ]
    st.subheader("All Procurement Records")
    table = st.selectbox("Record table", tables, key="all_records_table")
    df = df_query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1000")
    dataframe(df)
    csv_download(df, table)


# ---------------- Budget command center ----------------

def _budget_source_frames(year: int, month: int | None = None, dept: str = "All", category: str = "All", vendor: str = "All", request_status: str = "All", payment_status: str = "All"):
    y = str(year)
    ym = f"{year}-{month:02d}" if month else None
    pr = df_query("SELECT pr.*, u.full_name requester FROM purchase_requests pr LEFT JOIN users u ON pr.requested_by=u.id")
    po = df_query("SELECT po.*, v.name vendor_name, pr.category, pr.department_project FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id LEFT JOIN purchase_requests pr ON po.request_id=pr.id")
    pay = df_query("SELECT p.*, v.name vendor_name, po.request_id FROM payments p LEFT JOIN vendors v ON p.vendor_id=v.id LEFT JOIN purchase_orders po ON p.po_id=po.id")
    exp = df_query("SELECT e.*, v.name vendor_name FROM expenses e LEFT JOIN vendors v ON e.vendor_id=v.id")

    def filter_date(df, col):
        if df.empty or col not in df.columns:
            return df
        if ym:
            return df[df[col].fillna("").astype(str).str.startswith(ym)]
        return df[df[col].fillna("").astype(str).str.startswith(y)]

    pr = filter_date(pr, "request_date")
    po = filter_date(po, "po_date")
    pay = filter_date(pay, "payment_date") if not pay.empty else pay
    exp = filter_date(exp, "expense_date")

    if dept != "All":
        if not pr.empty: pr = pr[pr["department_project"] == dept]
        if not po.empty and "department_project" in po.columns: po = po[po["department_project"] == dept]
        if not exp.empty and "project_department" in exp.columns: exp = exp[exp["project_department"] == dept]
    if category != "All":
        if not pr.empty: pr = pr[pr["category"] == category]
        if not po.empty and "category" in po.columns: po = po[po["category"] == category]
        if not exp.empty: exp = exp[exp["category"] == category]
    if request_status != "All" and not pr.empty:
        pr = pr[pr["status"] == request_status]
    if payment_status != "All":
        if not pr.empty and "payment_status" in pr.columns: pr = pr[pr["payment_status"].fillna("Not Ready") == payment_status]
        if not po.empty and "payment_status" in po.columns: po = po[po["payment_status"].fillna("Unpaid") == payment_status]
        if not pay.empty and "status" in pay.columns: pay = pay[pay["status"] == payment_status]
    if vendor != "All":
        if not po.empty: po = po[po["vendor_name"].fillna("") == vendor]
        if not pay.empty: pay = pay[pay["vendor_name"].fillna("") == vendor]
        if not exp.empty: exp = exp[exp["vendor_name"].fillna("") == vendor]
    return pr, po, pay, exp


def _budget_totals(year: int, month: int | None, dept: str, category: str, vendor: str, request_status: str, payment_status: str):
    pr, po, pay, exp = _budget_source_frames(year, month, dept, category, vendor, request_status, payment_status)
    pending_statuses = ["Submitted", "Procurement Review", "Requires Sourcing", "Vendor Quote Collection", "Vendor Recommendation", "Pending Approval", "Pending Approver/MD Approval", "Submitted to Procurement Manager", "PM Reviewing"]
    committed_statuses = ["Approved", "PO Created", "PO Approved", "Sent to Vendor", "Awaiting Delivery", "Partially Received", "Fully Received", "Invoice Uploaded", "Finance Review", "Approved for Payment"]
    pending = 0.0 if pr.empty else float(pr[pr["status"].isin(pending_statuses)]["estimated_amount"].fillna(0).sum())
    approved = 0.0 if pr.empty else float(pr[pr["status"].isin(committed_statuses + ["Paid", "Closed"])]["estimated_amount"].fillna(0).sum())
    committed = 0.0 if po.empty else float(po[po["status"].isin(["Approved", "Sent to Vendor", "Awaiting Delivery", "Partially Received", "Fully Received", "Invoiced"])] ["total_amount"].fillna(0).sum())
    paid = 0.0 if pay.empty else float(pay[pay["status"] == "Paid"]["amount"].fillna(0).sum())
    spent_exp = 0.0 if exp.empty else float(exp[exp["status"].isin(["Approved", "Paid"])] ["amount"].fillna(0).sum())
    paid = max(paid, spent_exp)
    return pr, po, pay, exp, pending, approved, committed, paid


def budget_command_center():
    st.subheader("Budget Tracker Command Center")
    st.caption("Track annual/monthly budgets, pending requests, committed POs, paid spend, risk and budget history.")
    today = date.today()
    c1, c2, c3, c4 = st.columns(4)
    year = c1.number_input("Year", min_value=2020, max_value=2100, value=today.year, step=1, key="budget_year_filter")
    month_choice = c2.selectbox("Month", ["All Year"] + [date(2000, m, 1).strftime("%B") for m in range(1, 13)], index=today.month, key="budget_month_filter")
    month_num = None if month_choice == "All Year" else [date(2000, m, 1).strftime("%B") for m in range(1, 13)].index(month_choice) + 1
    dept = c3.selectbox("Department / Project", ["All"] + department_options(), key="budget_dept_filter")
    cat = c4.selectbox("Category", ["All"] + EXPENSE_CATEGORIES, key="budget_cat_filter")
    c5, c6, c7 = st.columns(3)
    vendors = df_query("SELECT DISTINCT name FROM vendors ORDER BY name")
    vendor = c5.selectbox("Vendor", ["All"] + (vendors["name"].tolist() if not vendors.empty else []), key="budget_vendor_filter")
    req_status = c6.selectbox("Request status", ["All"] + PR_STATUSES, key="budget_req_status_filter")
    pay_status = c7.selectbox("Payment status", ["All", "Not Ready", "Approved for Payment", "Unpaid", "Pending Approval", "Approved", "Paid", "Returned"], key="budget_pay_status_filter")

    budgets = df_query("SELECT * FROM budgets")
    annuals = df_query("SELECT * FROM annual_budgets")
    month_key_filter = f"{int(year)}-{month_num:02d}" if month_num else None
    monthly_budget = budgets.copy()
    if not monthly_budget.empty:
        monthly_budget = monthly_budget[monthly_budget["budget_month"].astype(str).str.startswith(str(int(year)))]
        if month_key_filter:
            monthly_budget = monthly_budget[monthly_budget["budget_month"] == month_key_filter]
        if dept != "All": monthly_budget = monthly_budget[monthly_budget["department_project"] == dept]
        if cat != "All": monthly_budget = monthly_budget[monthly_budget["category"] == cat]
    annual_budget_df = annuals.copy()
    if not annual_budget_df.empty:
        annual_budget_df = annual_budget_df[annual_budget_df["budget_year"] == int(year)]
        if dept != "All": annual_budget_df = annual_budget_df[annual_budget_df["department_project"] == dept]
        if cat != "All": annual_budget_df = annual_budget_df[annual_budget_df["category"].isin([cat, "All"])]

    annual_budget = float(annual_budget_df["annual_amount"].fillna(0).sum()) if not annual_budget_df.empty else float(monthly_budget["limit_amount"].fillna(0).sum()) if not monthly_budget.empty else 0.0
    selected_month_budget = float(monthly_budget["limit_amount"].fillna(0).sum()) if not monthly_budget.empty else 0.0
    pr_y, po_y, pay_y, exp_y, annual_pending, annual_spent, annual_committed, annual_paid = _budget_totals(int(year), None, dept, cat, vendor, req_status, pay_status)
    pr_m, po_m, pay_m, exp_m, monthly_pending, monthly_spent, monthly_committed, monthly_paid = _budget_totals(int(year), month_num or today.month, dept, cat, vendor, req_status, pay_status)
    annual_remaining = annual_budget - annual_pending - annual_committed - annual_paid
    monthly_remaining = selected_month_budget - monthly_pending - monthly_committed - monthly_paid
    annual_usage = 0 if annual_budget <= 0 else ((annual_pending + annual_committed + annual_paid) / annual_budget) * 100
    monthly_usage = 0 if selected_month_budget <= 0 else ((monthly_pending + monthly_committed + monthly_paid) / selected_month_budget) * 100

    spend_by_cat = pd.DataFrame()
    if not pr_y.empty:
        spend_by_cat = pr_y.groupby("category", dropna=False)["estimated_amount"].sum().reset_index().sort_values("estimated_amount", ascending=False)
    spend_by_dept = pd.DataFrame()
    if not pr_y.empty:
        spend_by_dept = pr_y.groupby("department_project", dropna=False)["estimated_amount"].sum().reset_index().sort_values("estimated_amount", ascending=False)
    highest_cat = "—" if spend_by_cat.empty else str(spend_by_cat.iloc[0]["category"])
    highest_dept = "—" if spend_by_dept.empty else str(spend_by_dept.iloc[0]["department_project"])

    metric_row([
        ("Annual Budget", money(annual_budget), None), ("Annual Spent/Paid", money(annual_paid), None),
        ("Annual Committed", money(annual_committed), None), ("Annual Pending", money(annual_pending), None),
        ("Annual Remaining", money(annual_remaining), None), ("Monthly Budget", money(selected_month_budget), None),
        ("Monthly Spent/Paid", money(monthly_paid), None), ("Monthly Remaining", money(monthly_remaining), None),
        ("Highest Risk Category", highest_cat, None), ("Highest Spending Department", highest_dept, None),
        ("Annual Utilization", f"{annual_usage:.1f}%", risk_label(annual_usage)), ("Monthly Risk", risk_label(monthly_usage), f"{monthly_usage:.1f}%"),
    ], cols=4)

    if annual_usage >= 85 or monthly_usage >= 85:
        st.warning(f"Budget risk: annual {risk_label(annual_usage)} ({annual_usage:.1f}%), monthly {risk_label(monthly_usage)} ({monthly_usage:.1f}%).")

    t1, t2, t3, t4 = st.tabs(["Visuals", "Create / Update Budget", "Adjust & Notes", "Budget History"])
    with t1:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### Spend by category")
            if not spend_by_cat.empty: interactive_chart(_money_chart_df(spend_by_cat.rename(columns={"estimated_amount":"total"})), "Spend by category", "category", "total", "budget_spend_category", default="Bar")
            else: st.info("No category spend for filters.")
            st.markdown("#### Pending vs committed vs paid")
            interactive_chart(pd.DataFrame({"stage": ["Pending", "Committed", "Paid"], "amount": [annual_pending, annual_committed, annual_paid]}), "Pending vs committed vs paid", "stage", "amount", "budget_pending_committed_paid", default="Donut")
        with c2:
            st.markdown("#### Spend by department")
            if not spend_by_dept.empty: interactive_chart(_money_chart_df(spend_by_dept.rename(columns={"estimated_amount":"total"})), "Spend by department", "department_project", "total", "budget_spend_department", default="Horizontal Bar")
            else: st.info("No department spend for filters.")
            st.markdown("#### Monthly budget vs actual")
            month_rows = []
            for m in range(1, 13):
                key = f"{int(year)}-{m:02d}"
                budget_amt = 0.0 if budgets.empty else float(budgets[budgets["budget_month"] == key]["limit_amount"].fillna(0).sum())
                _, _, _, _, _, _, committed_m, paid_m = _budget_totals(int(year), m, dept, cat, vendor, "All", "All")
                month_rows.append({"month": key, "budget": budget_amt, "actual": committed_m + paid_m})
            mv = pd.DataFrame(month_rows)
            interactive_chart(mv.melt(id_vars=["month"], var_name="series", value_name="amount"), "Monthly budget versus actual", "month", "amount", "budget_monthly_budget_actual", default="Line", color="series", allow_pie=False)
        st.markdown("#### Budget risk table")
        risk = build_budget_risk_table(int(year))
        dataframe(risk) if not risk.empty else st.success("No budget risk table yet. Create budgets to activate risk tracking.")
        csv_download(risk, "budget_risk")
    with t2:
        with st.form("budget_create_update_form"):
            c1, c2, c3 = st.columns(3)
            byear = c1.number_input("Budget year", 2020, 2100, int(year), key="create_budget_year")
            bdept = c2.selectbox("Department / Project", department_options(), key="create_budget_dept")
            bcat = c3.selectbox("Category", ["All"] + EXPENSE_CATEGORIES, key="create_budget_cat")
            annual_amount = st.number_input("Annual budget amount", min_value=0.0, step=10000.0, key="annual_budget_amount")
            distribute = st.checkbox("Distribute annual budget evenly across 12 months", value=True, key="distribute_budget")
            note = st.text_area("Budget note", key="annual_budget_note")
            submit_budget = st.form_submit_button("Create / Update Annual Budget")
        if submit_budget:
            existing = df_query("SELECT * FROM annual_budgets WHERE budget_year=? AND department_project=? AND category=?", (int(byear), bdept, bcat))
            before = existing.iloc[0].to_dict() if not existing.empty else {}
            run_query(
                """
                INSERT INTO annual_budgets (budget_year, department_project, category, annual_amount, distribution_json, notes, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(budget_year, department_project, category) DO UPDATE SET annual_amount=excluded.annual_amount, distribution_json=excluded.distribution_json, notes=excluded.notes, updated_at=excluded.updated_at
                """,
                (int(byear), bdept, bcat, annual_amount, json_dump({"mode": "even" if distribute else "manual"}), note, user()["id"], now_iso(), now_iso()),
            )
            annual_id = df_query("SELECT id FROM annual_budgets WHERE budget_year=? AND department_project=? AND category=?", (int(byear), bdept, bcat)).iloc[0]["id"]
            if distribute:
                monthly = annual_amount / 12.0
                target_categories = EXPENSE_CATEGORIES if bcat == "All" else [bcat]
                per_category = monthly / max(len(target_categories), 1)
                for m in range(1, 13):
                    for target_cat in target_categories:
                        run_query(
                            """
                            INSERT INTO budgets (budget_month, category, department_project, limit_amount, override_required, created_at)
                            VALUES (?, ?, ?, ?, 0, ?)
                            ON CONFLICT(budget_month, category, department_project) DO UPDATE SET limit_amount=excluded.limit_amount
                            """,
                            (f"{int(byear)}-{m:02d}", target_cat, bdept, per_category, now_iso()),
                        )
            after = {"budget_year": int(byear), "department_project": bdept, "category": bcat, "annual_amount": annual_amount, "note": note}
            run_query("INSERT INTO budget_history (budget_type, budget_id, action, before_values, after_values, note, changed_by, created_at) VALUES ('Annual', ?, 'CREATE_OR_UPDATE', ?, ?, ?, ?, ?)", (int(annual_id), json_dump(before), json_dump(after), note, user()["id"], now_iso()))
            log_audit("BUDGET_UPDATED", "AnnualBudget", int(annual_id), note, user()["id"], user()["role"], before, after)
            st.success("Annual budget saved. Monthly distribution updated where selected.")

        with st.form("manual_monthly_budget_form"):
            c1, c2, c3, c4 = st.columns(4)
            bmonth = c1.text_input("Budget month (YYYY-MM)", value=month_key_filter or month_key(), key="manual_budget_month")
            mdept = c2.selectbox("Department", department_options(), key="manual_budget_dept")
            mcat = c3.selectbox("Category", EXPENSE_CATEGORIES, key="manual_budget_cat")
            mlimit = c4.number_input("Monthly limit", min_value=0.0, step=5000.0, key="manual_budget_limit")
            mnote = st.text_input("Note", key="manual_budget_note")
            manual_submit = st.form_submit_button("Save Manual Monthly Budget")
        if manual_submit:
            before_df = df_query("SELECT * FROM budgets WHERE budget_month=? AND category=? AND department_project=?", (bmonth, mcat, mdept))
            before = before_df.iloc[0].to_dict() if not before_df.empty else {}
            run_query(
                """
                INSERT INTO budgets (budget_month, category, department_project, limit_amount, override_required, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(budget_month, category, department_project) DO UPDATE SET limit_amount=excluded.limit_amount
                """,
                (bmonth, mcat, mdept, mlimit, now_iso()),
            )
            bid = df_query("SELECT id FROM budgets WHERE budget_month=? AND category=? AND department_project=?", (bmonth, mcat, mdept)).iloc[0]["id"]
            after = {"budget_month": bmonth, "category": mcat, "department_project": mdept, "limit_amount": mlimit}
            run_query("INSERT INTO budget_history (budget_type, budget_id, action, before_values, after_values, note, changed_by, created_at) VALUES ('Monthly', ?, 'CREATE_OR_UPDATE', ?, ?, ?, ?, ?)", (int(bid), json_dump(before), json_dump(after), mnote, user()["id"], now_iso()))
            log_audit("MONTHLY_BUDGET_UPDATED", "Budget", int(bid), mnote, user()["id"], user()["role"], before, after)
            st.success("Monthly budget saved.")
    with t3:
        monthly_budgets = df_query("SELECT id, budget_month, category, department_project, limit_amount FROM budgets ORDER BY budget_month DESC, department_project, category")
        if monthly_budgets.empty:
            st.info("Create a monthly budget before recording adjustments.")
        else:
            label = st.selectbox("Budget to adjust", [f"{r.budget_month} | {r.department_project} | {r.category} | {money(r.limit_amount)} | #{int(r.id)}" for r in monthly_budgets.itertuples()], key="adjust_budget_select")
            bid = int(label.rsplit("#", 1)[1]); row = monthly_budgets[monthly_budgets["id"] == bid].iloc[0]
            with st.form(f"adjust_budget_form_{bid}"):
                adj = st.number_input("Adjustment amount (+/-)", value=0.0, step=5000.0, key=f"adj_amt_{bid}")
                reason = st.text_area("Adjustment reason / note", key=f"adj_reason_{bid}")
                adj_submit = st.form_submit_button("Record Adjustment")
            if adj_submit:
                new_limit = float(row["limit_amount"]) + adj
                run_query("UPDATE budgets SET limit_amount=? WHERE id=?", (new_limit, bid))
                run_query("INSERT INTO budget_adjustments (budget_type, budget_id, budget_month, department_project, category, adjustment_amount, reason, adjusted_by, created_at) VALUES ('Monthly', ?, ?, ?, ?, ?, ?, ?, ?)", (bid, row["budget_month"], row["department_project"], row["category"], adj, reason, user()["id"], now_iso()))
                run_query("INSERT INTO budget_history (budget_type, budget_id, action, before_values, after_values, note, changed_by, created_at) VALUES ('Monthly', ?, 'ADJUSTMENT', ?, ?, ?, ?, ?)", (bid, json_dump({"limit_amount": float(row["limit_amount"])}), json_dump({"limit_amount": new_limit, "adjustment": adj}), reason, user()["id"], now_iso()))
                log_audit("BUDGET_ADJUSTED", "Budget", bid, reason, user()["id"], user()["role"], {"limit_amount": float(row["limit_amount"])}, {"limit_amount": new_limit})
                st.success("Budget adjustment recorded.")
        st.markdown("#### Current monthly budgets")
        if not monthly_budgets.empty:
            show = monthly_budgets.copy(); show["limit_amount"] = show["limit_amount"].apply(money); dataframe(show)
            csv_download(monthly_budgets, "monthly_budgets")
    with t4:
        hist = df_query("SELECT bh.*, u.full_name changed_by_name FROM budget_history bh LEFT JOIN users u ON bh.changed_by=u.id ORDER BY bh.created_at DESC LIMIT 500")
        dataframe(hist) if not hist.empty else st.info("No budget history yet.")
        csv_download(hist, "budget_history")


def build_budget_risk_table(year: int) -> pd.DataFrame:
    budgets = df_query("SELECT * FROM budgets WHERE budget_month LIKE ?", (f"{year}-%",))
    if budgets.empty:
        return budgets
    rows = []
    for _, b in budgets.iterrows():
        m = int(str(b["budget_month"])[5:7]) if len(str(b["budget_month"])) >= 7 else None
        dept = b["department_project"] or "General"
        cat = b["category"] or "Other"
        _, _, _, _, pending, approved, committed, paid = _budget_totals(year, m, dept, cat, "All", "All", "All")
        used = pending + committed + paid
        limit_amt = float(b["limit_amount"] or 0)
        pct = 0 if limit_amt <= 0 else used / limit_amt * 100
        rows.append({
            "budget_month": b["budget_month"], "department_project": dept, "category": cat,
            "limit_amount": limit_amt, "pending": pending, "committed": committed, "paid": paid,
            "remaining": limit_amt - used, "usage_percent": round(pct, 1), "risk_status": risk_label(pct),
        })
    out = pd.DataFrame(rows)
    for c in ["limit_amount", "pending", "committed", "paid", "remaining"]:
        out[c] = out[c].apply(money)
    return out.sort_values(["usage_percent"], ascending=False)


def budgets_page(show_header=True):
    if show_header:
        st.subheader("Budgets")
    budget_command_center()


def budget_utilization_df():
    current_year = date.today().year
    return build_budget_risk_table(current_year)


def budget_risk_df():
    df = build_budget_risk_table(date.today().year)
    if df.empty or "usage_percent" not in df.columns:
        return df
    return df[df["usage_percent"].astype(float) >= 70]


# ---------------- Procurement Manager workspace overrides ----------------

def procurement_workspace():
    role_header("Procurement Manager Workspace", "Operational command center for requests, Facility Manager handoffs, import review, sourcing, POs, receiving and delegated approvals.")
    section = st.session_state.get("procurement_section", "Operations Dashboard")
    if section == "Operations Dashboard":
        procurement_dashboard_metrics(); procurement_dashboard()
    elif section == "Purchase Requests":
        requests_page(mode="procurement")
    elif section == "Facility Manager Inbox":
        facility_manager_inbox()
    elif section == "Import Center":
        import_center()
    elif section == "Sourcing":
        sourcing_page()
    elif section == "Vendor Quotes":
        quote_page()
    elif section == "Vendor Recommendation":
        quote_comparison_decision_page()
    elif section == "Purchase Orders":
        purchase_orders_page()
    elif section == "Receiving Slips":
        receiving_page()
    elif section == "Vendors":
        vendors_page()
    elif section == "Acting Approval Queue":
        acting_approval_queue()
    elif section == "Procurement Documents":
        document_archive(editable=True)
    elif section == "Procurement Reports":
        procurement_reports()
    elif section == "My Activity History":
        activity_history_page(scope="mine")
    elif section == "Settings":
        settings_page()
    else:
        procurement_dashboard_metrics(); procurement_dashboard()


def procurement_dashboard():
    quick_action_bar(["New Purchase Request", "Facility Manager Inbox", "Import Documents", "Acting Approval Queue", "Create PO", "Record Receiving Slip"])
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("What needs my attention?")
        df = df_query("""
            SELECT request_no, department_project, category, estimated_amount, status, priority
            FROM purchase_requests
            WHERE status IN ('Submitted','Procurement Review','Requires Sourcing','Vendor Quote Collection','Approved','Submitted to Procurement Manager','PM Reviewing')
            ORDER BY updated_at DESC LIMIT 25
        """)
        if not df.empty:
            df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
        else: st.success("No procurement actions pending.")
    with c2:
        st.subheader("Facility Manager Handoffs")
        fm = df_query("""
            SELECT pr.request_no, u.full_name facility_manager, pr.department_project, pr.category, pr.estimated_amount, pr.status
            FROM purchase_requests pr LEFT JOIN users u ON pr.facility_manager_user_id=u.id
            WHERE pr.assigned_procurement_manager_id=? AND pr.status IN ('Submitted to Procurement Manager','PM Reviewing','Returned to Facility Manager','Accepted by Procurement Manager')
            ORDER BY pr.updated_at DESC LIMIT 20
        """, (user()["id"],))
        if not fm.empty:
            fm["estimated_amount"] = fm["estimated_amount"].apply(money); dataframe(fm)
        else: st.info("No Facility Manager handoffs waiting.")
    st.subheader("Pipeline by Status")
    pipe = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status")
    if not pipe.empty: st.bar_chart(pipe.set_index("status"))


def get_pm_for_facility_manager(fm_user_id: int) -> int | None:
    rows = run_query("""
        SELECT procurement_manager_user_id FROM facility_manager_links
        WHERE facility_manager_user_id=? AND is_active=1
        ORDER BY updated_at DESC, created_at DESC LIMIT 1
    """, (fm_user_id,), fetch=True)
    if rows:
        return int(rows[0]["procurement_manager_user_id"])
    pm = run_query("SELECT id FROM users WHERE role='Procurement Manager' AND is_active=1 ORDER BY id LIMIT 1", fetch=True)
    return int(pm[0]["id"]) if pm else None


def ensure_thread(entity_type: str, entity_id: int, fm_id: int | None, pm_id: int | None) -> int | None:
    if not fm_id or not pm_id:
        return None
    run_query(
        "INSERT OR IGNORE INTO collaboration_threads (entity_type, entity_id, facility_manager_user_id, procurement_manager_user_id, visibility_scope, created_at, updated_at) VALUES (?, ?, ?, ?, 'FM_PM_ADMIN', ?, ?)",
        (entity_type, entity_id, fm_id, pm_id, now_iso(), now_iso()),
    )
    rows = run_query("SELECT id FROM collaboration_threads WHERE entity_type=? AND entity_id=? AND facility_manager_user_id=? AND procurement_manager_user_id=?", (entity_type, entity_id, fm_id, pm_id), fetch=True)
    return int(rows[0]["id"]) if rows else None


def can_view_private_thread(thread: pd.Series | dict) -> bool:
    role = user()["role"]
    uid = int(user()["id"])
    return role == "Admin" or uid in {int(thread.get("facility_manager_user_id") or 0), int(thread.get("procurement_manager_user_id") or 0)}


def render_private_thread(entity_type: str, entity_id: int, fm_id: int | None, pm_id: int | None, key_scope: str):
    thread_id = ensure_thread(entity_type, entity_id, fm_id, pm_id)
    if not thread_id:
        st.info("No assigned Facility Manager / Procurement Manager link exists for this thread.")
        return
    thread = df_query("SELECT * FROM collaboration_threads WHERE id=?", (thread_id,)).iloc[0]
    st.markdown("#### Private Facility Manager / Procurement Manager Thread")
    can_read = can_view_private_thread(thread)
    is_auditor = user()["role"] == "Auditor"
    msgs = df_query("""
        SELECT cm.id, cm.created_at, u.full_name sender, cm.sender_user_id, cm.message_text, cm.attachment_path, cm.is_private
        FROM collaboration_messages cm LEFT JOIN users u ON cm.sender_user_id=u.id
        WHERE cm.thread_id=? ORDER BY cm.created_at DESC
    """, (thread_id,))
    if msgs.empty:
        st.caption("No messages yet.")
    elif can_read and not is_auditor:
        dataframe(msgs[["created_at", "sender", "message_text", "attachment_path"]])
    else:
        meta = msgs[["id", "created_at", "sender", "is_private"]].copy()
        meta["message_text"] = "Hidden private message content"
        dataframe(meta)
    if can_read and not is_auditor:
        with st.form(f"private_msg_{key_scope}_{thread_id}"):
            text = st.text_area("Add private message", key=f"private_msg_text_{key_scope}_{thread_id}")
            attachment = st.file_uploader("Optional attachment", type=["pdf", "docx", "jpg", "jpeg", "png"], key=f"private_msg_file_{key_scope}_{thread_id}")
            submitted = st.form_submit_button("Send private message")
        if submitted and (text or attachment):
            path, _ = save_upload(attachment, "private_threads") if attachment else (None, None)
            run_query("INSERT INTO collaboration_messages (thread_id, sender_user_id, message_text, attachment_path, is_private, created_at) VALUES (?, ?, ?, ?, 1, ?)", (thread_id, user()["id"], text, path, now_iso()))
            run_query("UPDATE collaboration_threads SET updated_at=? WHERE id=?", (now_iso(), thread_id))
            other_id = int(thread["procurement_manager_user_id"]) if user()["id"] == int(thread["facility_manager_user_id"]) else int(thread["facility_manager_user_id"])
            create_notification(other_id, None, "Private procurement message", f"New private message on {entity_type} #{entity_id}.", entity_type, entity_id, "Important")
            create_activity_log(user()["id"], user()["role"], "PRIVATE_MESSAGE_ADDED", entity_type, entity_id, "Message added to Facility Manager / Procurement Manager thread", "Private message content hidden from audit metadata", "private_thread", other_id)
            log_audit("PRIVATE_MESSAGE_ADDED", entity_type, entity_id, "Private message metadata recorded", user()["id"], user()["role"])
            st.rerun()


def facility_manager_inbox():
    st.subheader("Facility Manager Inbox")
    st.caption("Review drafts submitted by Facility Managers linked to you. Accept, return, request documents, reject or convert into official requests.")
    df = df_query("""
        SELECT pr.*, fm.full_name facility_manager
        FROM purchase_requests pr LEFT JOIN users fm ON pr.facility_manager_user_id=fm.id
        WHERE pr.assigned_procurement_manager_id=? AND pr.status IN ('Submitted to Procurement Manager','PM Reviewing','Returned to Facility Manager','Accepted by Procurement Manager','Rejected by Procurement Manager')
        ORDER BY pr.updated_at DESC, pr.created_at DESC
    """, (user()["id"],))
    if df.empty:
        empty_state("No Facility Manager drafts", "Submitted drafts from assigned Facility Managers will appear here.")
        return
    show = df[["id", "request_no", "facility_manager", "department_project", "category", "estimated_amount", "status", "updated_at"]].copy()
    show["estimated_amount"] = show["estimated_amount"].apply(money)
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open FM draft", [f"{r.request_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="pm_fm_inbox_select")
    pr_id = int(selected.rsplit("#", 1)[1])
    pr = df[df["id"] == pr_id].iloc[0]
    request_detail(pr_id, actions=False, key_scope=f"pm_fm_inbox_{pr_id}")
    render_private_thread("Purchase Request", pr_id, int(pr["facility_manager_user_id"]), int(pr["assigned_procurement_manager_id"]), f"pm_fm_{pr_id}")
    note = st.text_area("Review comment / reason", key=f"pm_fm_note_{pr_id}")
    c1, c2, c3, c4, c5 = st.columns(5)
    if c1.button("Start PM Review", key=f"pm_review_{pr_id}"):
        transition_request_status(pr_id, "PM Reviewing", "PM Reviewing", note or "Procurement Manager started review", user()["id"], user()["role"]); st.rerun()
    if c2.button("Return for Correction", key=f"pm_return_{pr_id}"):
        transition_request_status(pr_id, "Returned to Facility Manager", "Returned to Facility Manager", note or "Returned for correction", user()["id"], user()["role"])
        if note:
            tid = ensure_thread("Purchase Request", pr_id, int(pr["facility_manager_user_id"]), int(pr["assigned_procurement_manager_id"]));
            run_query("INSERT INTO collaboration_messages (thread_id, sender_user_id, message_text, is_private, created_at) VALUES (?, ?, ?, 1, ?)", (tid, user()["id"], note, now_iso()))
        st.rerun()
    if c3.button("Request More Documents", key=f"pm_docs_{pr_id}"):
        transition_request_status(pr_id, "Returned to Facility Manager", "Documents Requested", note or "More supporting documents requested", user()["id"], user()["role"]); st.rerun()
    if c4.button("Accept Draft", key=f"pm_accept_{pr_id}"):
        transition_request_status(pr_id, "Accepted by Procurement Manager", "Accepted by Procurement Manager", note or "Accepted into procurement preparation", user()["id"], user()["role"]); st.rerun()
    if c5.button("Reject Draft", key=f"pm_reject_{pr_id}"):
        transition_request_status(pr_id, "Rejected by Procurement Manager", "Rejected by Procurement Manager", note or "Draft rejected", user()["id"], user()["role"]); st.rerun()
    if st.button("Convert to Official Purchase Request", type="primary", key=f"pm_convert_{pr_id}"):
        convert_fm_draft_to_official(pr_id, note)
        st.rerun()


def convert_fm_draft_to_official(draft_id: int, note: str | None = None):
    draft = df_query("SELECT * FROM purchase_requests WHERE id=?", (draft_id,)).iloc[0]
    official_no = make_ref("PR")
    official_id = run_insert(
        """
        INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, required_date, category, justification, priority, estimated_amount, vendor_preference, status, source_type, imported_doc_id, import_confidence, attachments_json, notes, approval_history_json, facility_manager_user_id, assigned_procurement_manager_id, converted_from_draft_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Submitted', 'Facility Manager Draft', ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
        """,
        (official_no, int(draft["requested_by"]), draft["department_project"], date.today().isoformat(), draft["required_date"], draft["category"], draft["justification"], draft["priority"], float(draft["estimated_amount"] or 0), draft["vendor_preference"], draft["imported_doc_id"], draft["import_confidence"], draft["attachments_json"], note or "Converted from Facility Manager draft", draft["facility_manager_user_id"], draft["assigned_procurement_manager_id"], draft_id, now_iso(), now_iso()),
    )
    items = df_query("SELECT * FROM purchase_request_items WHERE request_id=?", (draft_id,))
    for _, item in items.iterrows():
        run_query("INSERT INTO purchase_request_items (request_id, item_name, description, quantity, unit_price, total, category, suggested_vendor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (official_id, item["item_name"], item["description"], item["quantity"], item["unit_price"], item["total"], item["category"], item["suggested_vendor"], now_iso()))
    run_query("UPDATE purchase_requests SET status='Converted to Purchase Request', official_request_id=?, updated_at=? WHERE id=?", (official_id, now_iso(), draft_id))
    add_workflow_event("Purchase Request", draft_id, "Converted to Purchase Request", "Converted to Purchase Request", official_no, user()["id"])
    add_workflow_event("Purchase Request", official_id, "Created from Facility Manager Draft", "Submitted", f"Converted from {draft['request_no']}", user()["id"])
    create_activity_log(user()["id"], user()["role"], "FM_DRAFT_CONVERTED", "Purchase Request", official_id, f"Converted {draft['request_no']} to {official_no}", note, "workflow", int(draft["facility_manager_user_id"] or 0))
    notify_related_users(official_id, "Draft converted", f"{draft['request_no']} was converted into official request {official_no}.", include_procurement=True)
    create_notification(None, "Procurement Manager", "New official request", f"{official_no} entered the procurement workflow.", "Purchase Request", official_id)
    log_audit("FM_DRAFT_CONVERTED", "Purchase Request", official_id, f"From draft {draft_id}", user()["id"], user()["role"])
    st.success(f"Converted to official request {official_no}.")


def _request_rule_for(pr: pd.Series | dict):
    amount = float(pr.get("estimated_amount") or 0)
    category = pr.get("category")
    rules = df_query("""
        SELECT * FROM approval_rules
        WHERE is_active=1 AND (category=? OR category='Other' OR category IS NULL)
        ORDER BY CASE WHEN category=? THEN 0 ELSE 1 END, threshold_amount DESC
    """, (category, category))
    if rules.empty:
        return None
    eligible = rules[rules["threshold_amount"].fillna(0) <= amount]
    return (eligible.iloc[0] if not eligible.empty else rules.iloc[0])


def can_pm_approve(pr: pd.Series | dict) -> tuple[bool, str, str]:
    rule = _request_rule_for(pr)
    delegation = active_delegation("Approver", "Procurement Manager")
    if delegation:
        return True, "Delegated Approval Mode", delegation.get("reason") or "Admin-enabled delegation"
    if int(pr.get("delegated_approval_allowed") or 0):
        return True, "Delegated Approval Mode", "Manually reassigned to Procurement Manager"
    if rule is not None and int(rule.get("pm_fallback_enabled") or 0):
        return True, "Delegated Approval Mode", "Approval rule allows Procurement Manager fallback"
    due_at = pr.get("approval_due_at")
    if due_at:
        try:
            if pd.to_datetime(due_at) < pd.Timestamp.now():
                return True, "Delegated Approval Mode", "Approval timeout has passed"
        except Exception:
            pass
    return False, "Normal Approval Mode", "Delegated approval is not currently enabled for this item"


def acting_approval_queue():
    st.subheader("Acting Approval Queue")
    st.info("You are reviewing this as delegated approver. This action will be recorded in the approval history.")
    df = df_query("""
        SELECT pr.*, u.full_name requester
        FROM purchase_requests pr LEFT JOIN users u ON pr.requested_by=u.id
        WHERE pr.status IN ('Pending Approval','Pending Approver/MD Approval')
        ORDER BY pr.estimated_amount DESC, pr.updated_at DESC
    """)
    if df.empty:
        st.success("No approval items are currently waiting.")
        return
    allowed_rows = []
    for _, row in df.iterrows():
        allowed, mode, reason = can_pm_approve(row)
        if allowed:
            d = row.to_dict(); d["approval_mode"] = mode; d["delegation_reason"] = reason; allowed_rows.append(d)
    if not allowed_rows:
        st.warning("There are pending approvals, but none are currently delegated to Procurement Manager.")
        return
    q = pd.DataFrame(allowed_rows)
    show = q[["id", "request_no", "department_project", "category", "estimated_amount", "status", "approval_mode", "delegation_reason"]].copy()
    show["estimated_amount"] = show["estimated_amount"].apply(money)
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open delegated approval", [f"{r.request_no} — {money(r.estimated_amount)} — #{int(r.id)}" for r in q.itertuples()], key="acting_approval_select")
    pr_id = int(selected.rsplit("#", 1)[1])
    pr = q[q["id"] == pr_id].iloc[0]
    request_detail(pr_id, actions=False, key_scope=f"acting_approval_{pr_id}")
    note = st.text_area("Approval note", key=f"acting_note_{pr_id}")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Approve as Delegated Approver", type="primary", key=f"acting_approve_{pr_id}"):
        transition_request_status(pr_id, "Approved", "Approved by Procurement Manager acting as delegated approver", note or "Approved by Procurement Manager acting as delegated approver.", user()["id"], user()["role"], pr["approval_mode"], pr["delegation_reason"], "Approver", payment_status="Approved for Payment")
        create_notification(None, "Finance", "Approved item ready for Finance", f"{pr['request_no']} was approved by Procurement Manager acting as delegated approver.", "Purchase Request", pr_id, "Important")
        st.rerun()
    if c2.button("Reject", key=f"acting_reject_{pr_id}"):
        transition_request_status(pr_id, "Rejected", "Rejected by delegated approver", note or "Rejected by Procurement Manager acting as delegated approver.", user()["id"], user()["role"], pr["approval_mode"], pr["delegation_reason"], "Approver")
        st.rerun()
    if c3.button("Return for More Information", key=f"acting_return_{pr_id}"):
        transition_request_status(pr_id, "Returned", "Returned by delegated approver", note or "Returned for more information.", user()["id"], user()["role"], pr["approval_mode"], pr["delegation_reason"], "Approver")
        st.rerun()
    if c4.button("Send Back to Approver", key=f"acting_back_{pr_id}"):
        create_notification(None, "Approver", "Approval returned to primary approver", f"{pr['request_no']} was sent back to Approver. {note}", "Purchase Request", pr_id, "Important")
        add_workflow_event("Purchase Request", pr_id, "Sent Back to Approver", pr["status"], note, user()["id"])
        st.success("Sent back to Approver.")


# ---------------- Facility Manager workspace ----------------

def facility_workspace():
    role_header("Facility Manager Workspace", "Create draft requests, import supporting documents, submit to Procurement Manager and collaborate privately on corrections.")
    section = st.session_state.get("facility_section", "Facility Dashboard")
    if section == "Facility Dashboard":
        facility_dashboard()
    elif section == "Create Request Draft":
        create_fm_draft_form()
    elif section == "My Draft Requests":
        facility_draft_register(status_filter=None)
    elif section == "Submit to Procurement Manager":
        facility_draft_register(status_filter=["FM Draft", "Returned to Facility Manager"])
    elif section == "Import Documents":
        facility_import_documents()
    elif section == "Shared Thread with Procurement Manager":
        facility_shared_threads()
    elif section == "Returned Requests":
        facility_draft_register(status_filter=["Returned to Facility Manager"])
    elif section == "Approved / Accepted Requests":
        facility_draft_register(status_filter=["Accepted by Procurement Manager", "Converted to Purchase Request", "Approved", "Paid", "Closed"])
    elif section == "My Activity History":
        activity_history_page(scope="mine")
    elif section == "Settings":
        settings_page()
    else:
        facility_dashboard()


def facility_dashboard():
    fm_id = user()["id"]
    pm_id = get_pm_for_facility_manager(fm_id)
    pm = df_query("SELECT full_name FROM users WHERE id=?", (pm_id,)) if pm_id else pd.DataFrame()
    st.info(f"Assigned Procurement Manager: {pm.iloc[0]['full_name'] if not pm.empty else 'Not assigned yet'}")
    q = lambda status: int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status=?", (fm_id, status)).iloc[0,0])
    metric_row([
        ("FM Drafts", q("FM Draft"), None),
        ("Submitted", q("Submitted to Procurement Manager"), None),
        ("Returned", q("Returned to Facility Manager"), None),
        ("Accepted", q("Accepted by Procurement Manager"), None),
        ("Converted", q("Converted to Purchase Request"), None),
        ("Approved/Paid", int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status IN ('Approved','Paid','Closed')", (fm_id,)).iloc[0,0]), None),
    ], cols=3)
    st.subheader("What needs my attention?")
    df = df_query("""
        SELECT request_no, department_project, category, estimated_amount, status, updated_at
        FROM purchase_requests
        WHERE facility_manager_user_id=? AND status IN ('FM Draft','Returned to Facility Manager','Submitted to Procurement Manager','Accepted by Procurement Manager')
        ORDER BY updated_at DESC LIMIT 20
    """, (fm_id,))
    if not df.empty:
        df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
    else: st.success("No Facility Manager actions pending.")


def create_fm_draft_form():
    if not has_permission("create_request"):
        st.info("Your role cannot create request drafts."); return
    pm_id = get_pm_for_facility_manager(user()["id"])
    if not pm_id:
        st.warning("No Procurement Manager is assigned yet. Ask Admin to create a Facility Manager link.")
    with st.form("fm_draft_create_form"):
        c1, c2, c3 = st.columns(3)
        dept = c1.selectbox("Department / Project", department_options(), key="fm_dept")
        req_required = c2.date_input("Required date", date.today() + timedelta(days=7), key="fm_required")
        cat = c3.selectbox("Category", EXPENSE_CATEGORIES, key="fm_cat")
        c4, c5 = st.columns(2)
        priority = c4.selectbox("Priority", PRIORITIES, index=1, key="fm_priority")
        vendor_pref = c5.text_input("Vendor preference", key="fm_vendor_pref")
        justification = st.text_area("Business justification", key="fm_justification")
        attachment = st.file_uploader("Supporting document", type=["docx", "pdf", "jpg", "jpeg", "png"], key="fm_support")
        item_count = st.number_input("Line items", 1, 15, 1, key="fm_line_items")
        items, estimated = [], 0.0
        for i in range(int(item_count)):
            c1, c2, c3, c4 = st.columns([1.4, .7, .9, 1])
            item = c1.text_input("Item", key=f"fm_req_item_{i}")
            qty = c2.number_input("Qty", 0.0, value=1.0, step=1.0, key=f"fm_req_qty_{i}")
            unit = c3.number_input("Unit price", 0.0, step=1000.0, key=f"fm_req_unit_{i}")
            icat = c4.selectbox("Category", EXPENSE_CATEGORIES, index=EXPENSE_CATEGORIES.index(cat), key=f"fm_req_cat_{i}")
            total = qty * unit; estimated += total; items.append((item, qty, unit, total, icat))
        submitted = st.form_submit_button("Create FM Draft", type="primary")
    if submitted:
        if not justification or not any(i[0] for i in items):
            st.error("Business justification and at least one item are required."); return
        path, _ = save_upload(attachment, "requests")
        req_no = make_ref("FM")
        pr_id = run_insert("""
            INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, required_date, category, justification, priority, estimated_amount, vendor_preference, status, source_type, attachments_json, notes, approval_history_json, facility_manager_user_id, assigned_procurement_manager_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FM Draft', 'Facility Manager', ?, '', '[]', ?, ?, ?, ?)
        """, (req_no, user()["id"], dept, date.today().isoformat(), req_required.isoformat(), cat, justification, priority, estimated, vendor_pref, json_dump([path] if path else []), user()["id"], pm_id, now_iso(), now_iso()))
        for item, qty, unit, total, icat in items:
            if item:
                run_query("INSERT INTO purchase_request_items (request_id, item_name, description, quantity, unit_price, total, category, suggested_vendor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (pr_id, item, item, qty, unit, total, icat, vendor_pref, now_iso()))
        ensure_thread("Purchase Request", pr_id, user()["id"], pm_id)
        add_workflow_event("Purchase Request", pr_id, "FM Draft Created", "FM Draft", req_no, user()["id"])
        create_activity_log(user()["id"], user()["role"], "FM_DRAFT_CREATED", "Purchase Request", pr_id, f"Created draft {req_no}", visibility_scope="own")
        st.success(f"Created FM draft {req_no}.")
        st.rerun()


def facility_draft_register(status_filter: list[str] | None = None):
    sql = "SELECT * FROM purchase_requests WHERE facility_manager_user_id=?"
    params: list[Any] = [user()["id"]]
    if status_filter:
        sql += " AND status IN (%s)" % ",".join(["?"] * len(status_filter)); params += status_filter
    sql += " ORDER BY updated_at DESC, created_at DESC"
    df = df_query(sql, params)
    if df.empty:
        empty_state("No Facility Manager drafts", "Create a draft or import a document to begin.")
        return
    show = df[["id", "request_no", "department_project", "category", "estimated_amount", "status", "updated_at", "official_request_id"]].copy()
    show["estimated_amount"] = show["estimated_amount"].apply(money)
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open my draft/request", [f"{r.request_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key=f"fm_open_{'_'.join(status_filter or ['all'])}")
    pr_id = int(selected.rsplit("#", 1)[1])
    pr = df[df["id"] == pr_id].iloc[0]
    request_detail(pr_id, actions=False, key_scope=f"fm_detail_{pr_id}")
    render_private_thread("Purchase Request", pr_id, int(pr["facility_manager_user_id"]), int(pr["assigned_procurement_manager_id"] or 0), f"fm_thread_{pr_id}")
    if pr["status"] in ["FM Draft", "Returned to Facility Manager"]:
        c1, c2 = st.columns(2)
        if c1.button("Submit to Procurement Manager", type="primary", key=f"fm_submit_{pr_id}"):
            transition_request_status(pr_id, "Submitted to Procurement Manager", "Submitted to Procurement Manager", "Facility Manager submitted draft to Procurement Manager", user()["id"], user()["role"])
            create_notification(int(pr["assigned_procurement_manager_id"]), None, "Facility Manager draft submitted", f"{pr['request_no']} is waiting in your Facility Manager Inbox.", "Purchase Request", pr_id, "Important")
            st.rerun()
        if c2.button("Attach update / note only", key=f"fm_note_only_{pr_id}"):
            st.toast("Use the private thread above to send notes or attachments.")


def facility_import_documents():
    st.subheader("Import Documents")
    st.caption("Limited import for Facility Manager. Uploaded documents become private FM drafts or supporting records for your assigned Procurement Manager.")
    pm_id = get_pm_for_facility_manager(user()["id"])
    upload = st.file_uploader("Upload supporting procurement document", type=["pdf", "docx", "jpg", "jpeg", "png"], key="fm_import_upload")
    title = st.text_input("Document title", key="fm_import_title")
    dept = st.selectbox("Department / Project", department_options(), key="fm_import_dept")
    cat = st.selectbox("Category", EXPENSE_CATEGORIES, key="fm_import_cat")
    amount = st.number_input("Estimated amount", min_value=0.0, step=1000.0, key="fm_import_amount")
    if st.button("Import as FM Draft", type="primary", key="fm_import_btn", disabled=upload is None):
        path, fhash = save_upload(upload, "facility_imports")
        doc_id = run_insert("""
            INSERT INTO imported_legacy_documents (source_zip_name, original_path, file_name, file_path, file_hash, document_type, department_project, title, likely_date, total_amount, import_status, confidence, imported_by, facility_manager_user_id, assigned_procurement_manager_id, created_at, updated_at)
            VALUES ('Facility Manager Upload', ?, ?, ?, ?, 'FM Supporting Document', ?, ?, ?, ?, 'Imported - Needs Review', 1.0, ?, ?, ?, ?, ?)
        """, (path, upload.name, path, fhash, dept, title or upload.name, date.today().isoformat(), amount, user()["id"], user()["id"], pm_id, now_iso(), now_iso()))
        req_no = make_ref("FM")
        pr_id = run_insert("""
            INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, category, justification, priority, estimated_amount, status, source_type, imported_doc_id, import_confidence, attachments_json, facility_manager_user_id, assigned_procurement_manager_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'Normal', ?, 'FM Draft', 'Facility Manager Import', ?, 1.0, ?, ?, ?, ?, ?)
        """, (req_no, user()["id"], dept, date.today().isoformat(), cat, title or f"Imported document {upload.name}", amount, doc_id, json_dump([path]), user()["id"], pm_id, now_iso(), now_iso()))
        run_query("UPDATE imported_legacy_documents SET linked_request_id=? WHERE id=?", (pr_id, doc_id))
        ensure_thread("Purchase Request", pr_id, user()["id"], pm_id)
        add_workflow_event("Purchase Request", pr_id, "FM Document Imported", "FM Draft", upload.name, user()["id"])
        create_activity_log(user()["id"], user()["role"], "FM_DOCUMENT_IMPORTED", "Purchase Request", pr_id, f"Imported document as draft {req_no}", visibility_scope="own")
        st.success(f"Imported and created draft {req_no}.")


def facility_shared_threads():
    threads = df_query("""
        SELECT ct.*, pr.request_no, pr.status, pm.full_name procurement_manager
        FROM collaboration_threads ct
        LEFT JOIN purchase_requests pr ON pr.id=ct.entity_id AND ct.entity_type='Purchase Request'
        LEFT JOIN users pm ON pm.id=ct.procurement_manager_user_id
        WHERE ct.facility_manager_user_id=? ORDER BY ct.updated_at DESC
    """, (user()["id"],))
    if threads.empty:
        empty_state("No shared threads", "Threads are created when you create or submit a Facility Manager draft.")
        return
    dataframe(threads[["request_no", "status", "procurement_manager", "updated_at"]])
    selected = st.selectbox("Open thread", [f"{r.request_no or r.entity_type} — #{int(r.id)}" for r in threads.itertuples()], key="fm_thread_select")
    thread_id = int(selected.rsplit("#", 1)[1])
    row = threads[threads["id"] == thread_id].iloc[0]
    render_private_thread(row["entity_type"], int(row["entity_id"]), int(row["facility_manager_user_id"]), int(row["procurement_manager_user_id"]), f"fm_thread_page_{thread_id}")


# ---------------- Finance workspace overrides ----------------

def finance_workspace():
    role_header("Finance Workspace", "Approved-for-payment queue, invoices, payment proof, budgets, expenses, cash advances and financial controls.")
    section = st.session_state.get("finance_section", "Financial Dashboard")
    if section == "Financial Dashboard":
        finance_metrics(); finance_dashboard()
    elif section == "Approved for Payment":
        approved_for_payment_page()
    elif section == "Expenses":
        expenses_page()
    elif section == "Invoices":
        invoices_page()
    elif section == "Payments":
        payments_page()
    elif section == "Cash Advances":
        cash_advances_page()
    elif section == "Budgets":
        budgets_page()
    elif section == "Vendor Payment Records":
        vendor_payment_records()
    elif section == "Reconciliation":
        reconciliation_page()
    elif section == "Financial Reports":
        finance_reports()
    elif section == "Settings":
        settings_page()
    else:
        finance_metrics(); finance_dashboard()


def finance_dashboard():
    ready = df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status IN ('Approved','Approved for Payment','Finance Review') AND COALESCE(payment_status,'')!='Paid'").iloc[0]["c"]
    if ready:
        st.warning(f"{int(ready)} item(s) are approved and waiting for Finance action.")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Approved for Payment")
        q = finance_ready_df()
        if not q.empty:
            q_show = q.head(15).copy(); q_show["Amount"] = q_show["Amount"].apply(money); dataframe(q_show)
        else: st.success("No approved payment queue items.")
    with c2:
        st.subheader("Budget Utilization")
        df = budget_utilization_df(); dataframe(df) if not df.empty else st.info("No budget data.")
    st.subheader("Spend by Category")
    df = df_query("SELECT category, SUM(amount) total FROM expenses WHERE status IN ('Approved','Paid') GROUP BY category")
    if not df.empty: st.bar_chart(df.set_index("category"))


def finance_ready_df() -> pd.DataFrame:
    reqs = df_query("""
        SELECT pr.id entity_id, 'Purchase Request' entity_type, pr.request_no request_no, '' po_no, COALESCE(pr.vendor_preference,'') vendor,
               pr.department_project department, pr.category, pr.estimated_amount amount, ah.approved_by_role approved_by,
               ah.created_at approval_date, COALESCE(pr.payment_status,'Approved for Payment') payment_status,
               'Review and record payment' required_action
        FROM purchase_requests pr
        LEFT JOIN approval_history ah ON ah.entity_type='Purchase Request' AND ah.entity_id=pr.id AND ah.status_after='Approved'
        WHERE pr.status IN ('Approved','Approved for Payment','Finance Review') AND COALESCE(pr.payment_status,'Approved for Payment')!='Paid'
        GROUP BY pr.id
    """)
    pos = df_query("""
        SELECT po.id entity_id, 'Purchase Order' entity_type, pr.request_no, po.po_no, COALESCE(v.name,'') vendor,
               pr.department_project department, pr.category, po.total_amount amount, COALESCE(u.full_name, po.approved_by_role) approved_by,
               po.updated_at approval_date, po.payment_status, 'Process PO payment' required_action
        FROM purchase_orders po
        LEFT JOIN purchase_requests pr ON po.request_id=pr.id
        LEFT JOIN vendors v ON po.vendor_id=v.id
        LEFT JOIN users u ON po.approved_by=u.id
        WHERE po.status IN ('Fully Received','Invoiced','Approved','Sent to Vendor') AND COALESCE(po.payment_status,'Unpaid')!='Paid'
    """)
    out = pd.concat([reqs, pos], ignore_index=True) if not reqs.empty or not pos.empty else pd.DataFrame()
    if out.empty:
        return out
    out = out.rename(columns={"amount": "Amount", "entity_type": "Type", "request_no": "Request number", "po_no": "PO number", "vendor": "Vendor", "department": "Department", "category": "Category", "approved_by": "Approved by", "approval_date": "Approval date", "payment_status": "Current payment status", "required_action": "Required finance action"})
    return out


def approved_for_payment_page():
    st.subheader("Approved for Payment")
    df = finance_ready_df()
    if df.empty:
        empty_state("No approved payment items", "Requests, POs and invoices approved for Finance will appear here.")
        return
    display = df.copy(); display["Amount"] = display["Amount"].apply(money)
    dataframe(display)
    selected = st.selectbox("Open finance item", [f"{r.Type} | {r._asdict().get('Request number', '') if False else getattr(r, 'Request_number', '')} | #{int(r.entity_id)}" for r in df.rename(columns={"Request number":"Request_number"}).itertuples()], key="finance_ready_select")
    entity_id = int(selected.rsplit("#", 1)[1]); entity_type = selected.split(" | ", 1)[0]
    note = st.text_area("Finance note", key=f"finance_note_{entity_type}_{entity_id}")
    proof = st.file_uploader("Upload receipt / payment proof", type=["pdf", "jpg", "jpeg", "png"], key=f"finance_proof_{entity_type}_{entity_id}")
    c1, c2, c3 = st.columns(3)
    if c1.button("Record payment and mark paid", type="primary", key=f"finance_paid_{entity_type}_{entity_id}"):
        path, _ = save_upload(proof, "payments") if proof else (None, None)
        if entity_type == "Purchase Request":
            pr = df_query("SELECT * FROM purchase_requests WHERE id=?", (entity_id,)).iloc[0]
            pno = make_ref("PAY")
            run_query("INSERT INTO payments (payment_no, amount, payment_method, payment_date, status, paid_by, notes, proof_path, created_by, created_at, updated_at) VALUES (?, ?, 'Bank Transfer', ?, 'Paid', ?, ?, ?, ?, ?, ?)", (pno, float(pr["estimated_amount"] or 0), date.today().isoformat(), user()["id"], note, path, user()["id"], now_iso(), now_iso()))
            transition_request_status(entity_id, "Paid", "Payment Completed", note or "Finance marked payment completed", user()["id"], user()["role"], payment_status="Paid")
        else:
            run_query("UPDATE purchase_orders SET payment_status='Paid', status='Paid', updated_at=? WHERE id=?", (now_iso(), entity_id))
            pno = make_ref("PAY")
            po = df_query("SELECT * FROM purchase_orders WHERE id=?", (entity_id,)).iloc[0]
            run_query("INSERT INTO payments (payment_no, po_id, vendor_id, amount, payment_method, payment_date, status, paid_by, notes, proof_path, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, 'Bank Transfer', ?, 'Paid', ?, ?, ?, ?, ?, ?)", (pno, entity_id, po["vendor_id"], float(po["total_amount"] or 0), date.today().isoformat(), user()["id"], note, path, user()["id"], now_iso(), now_iso()))
            if po["request_id"]:
                transition_request_status(int(po["request_id"]), "Paid", "Payment Completed", note or "PO payment completed", user()["id"], user()["role"], payment_status="Paid")
            add_workflow_event("Purchase Order", entity_id, "Payment Completed", "Paid", note, user()["id"])
        log_audit("PAYMENT_COMPLETED", entity_type, entity_id, note, user()["id"], user()["role"])
        st.success("Payment recorded and item marked paid.")
        st.rerun()
    if c2.button("Return for clarification", key=f"finance_return_{entity_type}_{entity_id}"):
        if entity_type == "Purchase Request":
            transition_request_status(entity_id, "Finance Review", "Returned for Finance Clarification", note or "Finance requested clarification", user()["id"], user()["role"])
        else:
            run_query("UPDATE purchase_orders SET payment_status='Returned', updated_at=? WHERE id=?", (now_iso(), entity_id))
            add_workflow_event("Purchase Order", entity_id, "Returned for Finance Clarification", "Returned", note, user()["id"])
        st.rerun()
    if c3.button("Add finance note", key=f"finance_add_note_{entity_type}_{entity_id}"):
        if entity_type == "Purchase Request":
            run_query("UPDATE purchase_requests SET finance_note=?, updated_at=? WHERE id=?", (note, now_iso(), entity_id))
        else:
            add_workflow_event("Purchase Order", entity_id, "Finance Note Added", None, note, user()["id"])
        create_activity_log(user()["id"], user()["role"], "FINANCE_NOTE_ADDED", entity_type, entity_id, "Finance note added", note, "finance")
        st.success("Finance note saved.")


# ---------------- Approver workspace overrides ----------------

def executive_workspace():
    role_header("Approver Workspace", "Simple decision workspace for request, quote, PO and payment approvals with synchronized status history.")
    section = st.session_state.get("executive_section", "Approval Dashboard")
    if section == "Approval Dashboard":
        executive_metrics(); executive_dashboard()
    elif section == "Pending Approvals":
        pending_approval_page()
    elif section == "Quote Comparison":
        quote_comparison_decision_page()
    elif section == "PO Approval":
        po_approval_page()
    elif section == "Payment Approval":
        payment_approval_page()
    elif section == "My Approval History":
        my_approval_history_page()
    elif section == "Settings":
        settings_page()
    else:
        executive_metrics(); executive_dashboard()


def my_approval_history_page():
    st.subheader("My Approval History")
    df = df_query("""
        SELECT ah.created_at, ah.entity_type, ah.entity_id, ah.action, ah.status_before, ah.status_after, ah.reason, ah.approval_mode, ah.delegation_reason, ah.original_approver_role
        FROM approval_history ah
        WHERE ah.user_id=? OR ah.approved_by_user_id=?
        ORDER BY ah.created_at DESC
    """, (user()["id"], user()["id"]))
    dataframe(df) if not df.empty else st.info("No approval actions recorded for you yet.")


# ---------------- Auditor workspace overrides ----------------

def audit_workspace():
    role_header("Audit & Compliance Workspace", "Read-only review of lifecycle, approvals, delegated approvals, budgets, imports, handoffs, vendors and finance status changes.")
    section = st.session_state.get("audit_section", "Audit Dashboard")
    if section == "Audit Dashboard":
        audit_metrics(); audit_dashboard()
    elif section == "Procurement Records":
        all_records_page()
    elif section == "Document Archive":
        document_archive(editable=False)
    elif section == "Approval Trails":
        approval_trails_page()
    elif section == "Delegated Approval Review":
        delegated_approval_review_page()
    elif section == "Budget Audit":
        budget_audit_page()
    elif section == "Facility Manager Handoff Trail":
        facility_handoff_trail_page()
    elif section == "Vendor History":
        vendor_history_page()
    elif section == "Expense Review":
        expense_review_page()
    elif section == "Compliance Reports":
        compliance_reports()
    elif section == "Settings":
        settings_page()
    else:
        audit_metrics(); audit_dashboard()


def delegated_approval_review_page():
    st.subheader("Delegated Approval Review")
    deleg = df_query("SELECT ad.*, u.full_name created_by_name FROM approval_delegations ad LEFT JOIN users u ON ad.created_by=u.id ORDER BY ad.created_at DESC")
    dataframe(deleg) if not deleg.empty else st.info("No delegation records.")
    hist = df_query("""
        SELECT ah.created_at, ah.entity_type, ah.entity_id, ah.action, ah.status_before, ah.status_after, ah.approved_by_role, ah.approval_mode, ah.delegation_reason, ah.original_approver_role, u.full_name approved_by
        FROM approval_history ah LEFT JOIN users u ON ah.approved_by_user_id=u.id
        WHERE ah.approval_mode='Delegated Approval Mode' OR ah.approved_by_role='Procurement Manager'
        ORDER BY ah.created_at DESC
    """)
    st.markdown("#### Delegated approval history")
    dataframe(hist) if not hist.empty else st.success("No delegated approvals have been used yet.")


def budget_audit_page():
    st.subheader("Budget Audit")
    risk = build_budget_risk_table(date.today().year)
    dataframe(risk) if not risk.empty else st.info("No budget risk data.")
    hist = df_query("SELECT bh.*, u.full_name changed_by_name FROM budget_history bh LEFT JOIN users u ON bh.changed_by=u.id ORDER BY bh.created_at DESC")
    st.markdown("#### Budget change history")
    dataframe(hist) if not hist.empty else st.info("No budget history yet.")
    logs = df_query("SELECT * FROM audit_logs WHERE action LIKE '%BUDGET%' ORDER BY created_at DESC")
    st.markdown("#### Budget audit logs")
    dataframe(logs) if not logs.empty else st.info("No budget audit logs yet.")


def facility_handoff_trail_page():
    st.subheader("Facility Manager Handoff Trail")
    handoffs = df_query("""
        SELECT pr.id, pr.request_no, fm.full_name facility_manager, pm.full_name procurement_manager, pr.status, pr.created_at, pr.updated_at, pr.official_request_id
        FROM purchase_requests pr
        LEFT JOIN users fm ON fm.id=pr.facility_manager_user_id
        LEFT JOIN users pm ON pm.id=pr.assigned_procurement_manager_id
        WHERE pr.facility_manager_user_id IS NOT NULL
        ORDER BY pr.updated_at DESC
    """)
    dataframe(handoffs) if not handoffs.empty else st.info("No Facility Manager handoffs yet.")
    meta = df_query("""
        SELECT cm.id message_id, cm.created_at, sender.full_name sender, ct.entity_type, ct.entity_id, fm.full_name facility_manager, pm.full_name procurement_manager,
               'Private message content hidden' message_text
        FROM collaboration_messages cm
        JOIN collaboration_threads ct ON cm.thread_id=ct.id
        LEFT JOIN users sender ON sender.id=cm.sender_user_id
        LEFT JOIN users fm ON fm.id=ct.facility_manager_user_id
        LEFT JOIN users pm ON pm.id=ct.procurement_manager_user_id
        ORDER BY cm.created_at DESC
    """)
    st.markdown("#### Private thread metadata")
    dataframe(meta) if not meta.empty else st.info("No private thread metadata yet.")


# ---------------- Activity/history ----------------

def activity_history_page(scope: str = "mine"):
    st.subheader("Activity & History Logs" if scope == "admin" else "My Activity History")
    if user()["role"] == "Admin" and scope == "admin":
        df = df_query("SELECT al.*, u.full_name user_name FROM activity_logs al LEFT JOIN users u ON al.user_id=u.id ORDER BY al.created_at DESC LIMIT 1000")
    elif user()["role"] == "Auditor":
        df = df_query("""
            SELECT al.created_at, al.role, al.action, al.entity_type, al.entity_id, al.public_summary, al.visibility_scope, al.related_user_id
            FROM activity_logs al
            WHERE al.visibility_scope!='private_content'
            ORDER BY al.created_at DESC LIMIT 500
        """)
    elif user()["role"] == "Facility Manager":
        df = df_query("""
            SELECT created_at, role, action, entity_type, entity_id, public_summary
            FROM activity_logs
            WHERE user_id=? OR related_user_id=? OR visibility_scope='own'
            ORDER BY created_at DESC LIMIT 300
        """, (user()["id"], user()["id"]))
    else:
        df = df_query("""
            SELECT created_at, role, action, entity_type, entity_id, public_summary
            FROM activity_logs
            WHERE user_id=? OR role=? OR related_user_id=? OR visibility_scope IN ('workflow', 'role')
            ORDER BY created_at DESC LIMIT 300
        """, (user()["id"], user()["role"], user()["id"]))
    dataframe(df) if not df.empty else st.info("No activity has been logged yet.")
    csv_download(df, "activity_logs")


# ---------------- Small page overrides ----------------

def configuration_page():
    approval_configuration_page()


def pending_approval_page():
    request_register(actions=True, approver_mode=True)


# ============================================================================
# Phase 2 extension layer: external notification preferences, away notices,
# and Facility Manager Gateway Pass workflow.
# ============================================================================

GATEWAY_PASS_STATUSES = [
    "Draft", "Submitted", "Pending Procurement Manager / Approver Review", "Approved",
    "Rejected", "Returned for Correction", "Generated", "Downloaded", "Cancelled",
]
GATEWAY_MOVEMENT_TYPES = ["Move Out", "Move In", "Internal Transfer", "Return", "Disposal", "Other"]
GATEWAY_QUALITY_OPTIONS = ["New", "Good", "Fair", "Damaged", "Requires Inspection", "Other"]
GATEWAY_FRAGILITY_OPTIONS = ["Fragile", "Non-Fragile"]
GATEWAY_UOMS = ["Unit", "Pieces", "Carton", "Box", "Kg", "Litre", "Set", "Bag", "Roll", "Other"]
AWAY_ROLES = ["Approver", "Procurement Manager"]



GATEWAY_COMPANY = {
    "center_name": "Center For Marine and Offshore Technology Development (CMOTD)",
    "unit_name": "Consultancy Services Unit, Rivers State University",
    "address": "Consultancy Unit, Rivers State University, Nkpolu-Oroworokwo, Port Harcourt, Rivers State",
    "email": "info@cmotd.org",
    "phone": "+2349163505000",
    "motto": "Where Theory becomes Reality and Individuals are Equipped to Lead in the Industry!",
}


def _gateway_asset_path(filename: str) -> Path:
    return Path(__file__).resolve().parents[1] / "static" / "assets" / filename


def _image_data_uri(path: Path) -> str:
    try:
        mime = "image/png"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception:
        return ""


def _clean(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except Exception:
        pass
    text = str(v).strip()
    return text if text else default


def _fmt_date(value: Any) -> str:
    text = _clean(value)
    if not text:
        return "N/A"
    try:
        # Accept YYYY-MM-DD or an ISO timestamp and return a formal date.
        dt = pd.to_datetime(text)
        return dt.strftime("%d %B %Y")
    except Exception:
        return text


def _fmt_dt(value: Any) -> str:
    text = _clean(value)
    if not text:
        return "N/A"
    try:
        return pd.to_datetime(text).strftime("%d %B %Y, %I:%M %p")
    except Exception:
        return text


def _qty_text(value: Any) -> str:
    try:
        val = float(value)
        return str(int(val)) if val.is_integer() else f"{val:g}"
    except Exception:
        return _clean(value, "0")


def _row_value(row: Any, key: str, default: str = "") -> str:
    try:
        return _clean(row.get(key), default)
    except Exception:
        try:
            return _clean(row[key], default)
        except Exception:
            return default


def gateway_pass_preview_html(gateway_pass_id: int) -> str:
    """Return a professional CMOTD/RSU-style HTML preview for a gateway pass.

    The preview intentionally mirrors the uploaded company template: dual logos,
    CMOTD/RSU header, motto, formal property movement wording, property details,
    transport details, authorization lines, and footer contacts. It also extends
    the template with a modern verification band, item table, quality/quantity
    visibility and fragile-item highlighting.
    """
    gp = gateway_pass_summary_df("gp.id=?", (gateway_pass_id,))
    if gp.empty:
        return "<div>Gateway pass not found.</div>"
    row = gp.iloc[0]
    items = gateway_pass_items_df(gateway_pass_id)
    cmotd_logo = _image_data_uri(_gateway_asset_path("cmotd_logo.png"))
    rsu_logo = _image_data_uri(_gateway_asset_path("rsu_logo.png"))
    rows_html = ""
    for no, item in enumerate(items.itertuples(), start=1):
        serial_asset = " / ".join([x for x in [_clean(getattr(item, "serial_number", "")), _clean(getattr(item, "asset_tag", ""))] if x]) or "-"
        fragile = _clean(getattr(item, "fragility_status", ""), "Non-Fragile")
        fragile_badge = "fragile" if fragile.lower() == "fragile" else "nonfragile"
        rows_html += f"""
        <tr>
          <td>{no}</td>
          <td>{escape(_clean(getattr(item, 'item_description', ''), '-'))}</td>
          <td>{escape(_clean(getattr(item, 'item_category', ''), '-'))}</td>
          <td>{escape(_clean(getattr(item, 'colour', ''), '-'))}</td>
          <td class="qty">{escape(_qty_text(getattr(item, 'quantity', '')))}</td>
          <td>{escape(_clean(getattr(item, 'unit_of_measure', ''), '-'))}</td>
          <td>{escape(_clean(getattr(item, 'quality_condition', ''), '-'))}</td>
          <td><span class="{fragile_badge}">{escape(fragile)}</span></td>
          <td>{escape(serial_asset)}</td>
          <td>{escape(_clean(getattr(item, 'handling_instruction', ''), '-'))}</td>
          <td>{escape(_clean(getattr(item, 'remarks', ''), '-'))}</td>
        </tr>
        """
    first_item = items.iloc[0] if not items.empty else {}
    item_sentence = _row_value(first_item, "item_description", "listed company asset") if len(items) <= 1 else f"{len(items)} listed company assets"
    status = _row_value(row, "status", "Draft")
    is_approved = status in ["Approved", "Generated", "Downloaded"]
    watermark = "APPROVED" if is_approved else status.upper()
    html = f"""
    <div class="gp-page">
      <div class="watermark">{escape(watermark)}</div>
      <div class="top-accent"></div>
      <div class="gp-header">
        <div class="logo-box">{'<img src="'+cmotd_logo+'" />' if cmotd_logo else '<b>CMOTD</b>'}</div>
        <div class="gp-title-block">
          <h1>{escape(GATEWAY_COMPANY['center_name'])}</h1>
          <h2>{escape(GATEWAY_COMPANY['unit_name'])}</h2>
          <p class="motto">{escape(GATEWAY_COMPANY['motto'])}</p>
        </div>
        <div class="logo-box right">{'<img src="'+rsu_logo+'" />' if rsu_logo else '<b>RSU</b>'}</div>
      </div>

      <div class="doc-title">PROPERTY MOVEMENT GATE PASS</div>
      <div class="meta-grid">
        <div><b>Reference No.:</b> {escape(_row_value(row, 'pass_number'))}</div>
        <div><b>Date:</b> {_fmt_date(_row_value(row, 'generated_at') or _row_value(row, 'created_at'))}</div>
        <div><b>Status:</b> <span class="status">{escape(status)}</span></div>
        <div><b>System Ref.:</b> GP-{gateway_pass_id}</div>
      </div>

      <p class="intro">This Gate Pass serves as official authorization for the movement of the underlisted company asset(s) from the premises of the Centre for Marine and Offshore Technology Development (CMOTD).</p>

      <h3>PROPERTY DETAILS</h3>
      <div class="summary-grid">
        <div><b>Item Description:</b> {escape(item_sentence)}</div>
        <div><b>Colour:</b> {escape(_row_value(first_item, 'colour', 'N/A')) if len(items) == 1 else 'See item table'}</div>
        <div><b>Quantity:</b> {escape(_qty_text(_row_value(first_item, 'quantity', '0'))) if len(items) == 1 else str(len(items)) + ' item line(s)'}</div>
        <div><b>Condition:</b> {escape(_row_value(first_item, 'quality_condition', 'N/A')) if len(items) == 1 else 'See item table'}</div>
        <div><b>Fragility:</b> {escape(_row_value(first_item, 'fragility_status', 'N/A')) if len(items) == 1 else ('Contains fragile item(s)' if (not items.empty and (items['fragility_status'] == 'Fragile').any()) else 'Non-Fragile items')}</div>
        <div><b>Department:</b> {escape(_row_value(row, 'department', 'N/A'))}</div>
      </div>

      <table class="items-table">
        <thead><tr><th>No.</th><th>Item Description</th><th>Category</th><th>Colour</th><th>Quantity</th><th>Unit</th><th>Quality / Condition</th><th>Fragile?</th><th>Serial / Asset Tag</th><th>Handling Instruction</th><th>Remarks</th></tr></thead>
        <tbody>{rows_html or '<tr><td colspan="11">No item lines added.</td></tr>'}</tbody>
      </table>

      <h3>PURPOSE OF MOVEMENT</h3>
      <p>The above-mentioned {escape(item_sentence)} is/are being moved for: <b>{escape(_row_value(row, 'purpose', 'N/A'))}</b>. Security personnel are hereby requested to permit the approved movement from <b>{escape(_row_value(row, 'origin_location', 'N/A'))}</b> to <b>{escape(_row_value(row, 'destination', 'N/A'))}</b>.</p>

      <h3>TRANSPORT DETAILS</h3>
      <div class="line-grid">
        <div><b>Movement Type:</b> {escape(_row_value(row, 'movement_type', 'N/A'))}</div>
        <div><b>Expected Movement Date:</b> {_fmt_date(_row_value(row, 'expected_movement_date'))}</div>
        <div><b>Expected Return Date:</b> {_fmt_date(_row_value(row, 'expected_return_date'))}</div>
        <div><b>Vehicle Number:</b> {escape(_row_value(row, 'vehicle_number', '________________'))}</div>
        <div><b>Driver's Name:</b> {escape(_row_value(row, 'driver_name', '________________'))}</div>
        <div><b>Driver's Phone Number:</b> {escape(_row_value(row, 'driver_phone', '________________'))}</div>
        <div><b>Receiver Name:</b> {escape(_row_value(row, 'receiver_name', '________________'))}</div>
        <div><b>Receiver Organization:</b> {escape(_row_value(row, 'receiver_organization', 'N/A'))}</div>
        <div><b>Security Checkpoint:</b> {escape(_row_value(row, 'security_checkpoint', 'Main Gate'))}</div>
      </div>

      <h3>AUTHORIZATION</h3>
      <p>I hereby certify that the movement of the above company property has been duly approved and authorized.</p>
      <div class="signature-grid">
        <div><b>Authorizing Officer:</b><span>{escape(_row_value(row, 'approved_by', '____________________________'))}</span></div>
        <div><b>Designation:</b><span>{escape(_row_value(row, 'approved_by_role', '____________________________'))}</span></div>
        <div><b>Signature:</b><span>____________________________</span></div>
        <div><b>Date:</b><span>{_fmt_date(_row_value(row, 'approved_at'))}</span></div>
      </div>

      <div class="security-box">
        <b>SECURITY VERIFICATION</b>
        <div class="signature-grid small">
          <div>Security Officer Name: ____________________________</div>
          <div>Gate Verification Time: ____________________________</div>
          <div>Exit / Entry Confirmation: ____________________________</div>
          <div>Signature: ____________________________</div>
        </div>
      </div>

      <div class="verification-band">
        <div><b>Validity Notice:</b> This gateway pass is valid only for the listed items and approved movement date.</div>
        <div><b>Generated:</b> {_fmt_dt(now_iso())}</div>
      </div>
      <div class="footer">
        {escape(GATEWAY_COMPANY['address'])}<br/>
        Email: {escape(GATEWAY_COMPANY['email'])} &nbsp; | &nbsp; Phone NO.: {escape(GATEWAY_COMPANY['phone'])}
      </div>
    </div>
    <style>
      .gp-page {{ position:relative; box-sizing:border-box; width:100%; max-width:980px; margin:0 auto; padding:28px 34px; background:#fff; color:#111827; border:1px solid #d1d5db; border-radius:16px; box-shadow:0 12px 32px rgba(15,23,42,.12); font-family: Georgia, 'Times New Roman', serif; overflow:hidden; }}
      .top-accent {{ height:8px; position:absolute; left:0; right:0; top:0; background:linear-gradient(90deg,#0f766e,#1d4ed8,#16a34a); }}
      .watermark {{ position:absolute; top:45%; left:11%; transform:rotate(-22deg); font-size:86px; color:rgba(15,118,110,.055); font-weight:900; letter-spacing:8px; pointer-events:none; }}
      .gp-header {{ display:grid; grid-template-columns:105px 1fr 105px; align-items:center; gap:18px; padding-top:6px; }}
      .logo-box img {{ max-width:92px; max-height:92px; object-fit:contain; }} .logo-box.right {{ text-align:right; }}
      .gp-title-block {{ text-align:center; }} .gp-title-block h1 {{ margin:0 0 6px 0; font-size:24px; line-height:1.2; font-weight:800; }} .gp-title-block h2 {{ margin:0 0 8px 0; font-size:23px; line-height:1.2; }}
      .motto {{ margin:0; font-size:16px; font-style:italic; font-weight:700; text-decoration:underline; }}
      .doc-title {{ margin:34px 0 24px 84px; font-size:22px; font-weight:800; letter-spacing:.3px; }}
      .meta-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px 24px; font-size:17px; margin:0 0 24px 84px; }}
      .status {{ background:#dcfce7; color:#166534; border:1px solid #86efac; border-radius:999px; padding:3px 9px; font-size:13px; font-family:Arial,sans-serif; font-weight:800; }}
      .intro, .gp-page p {{ font-size:17px; line-height:1.65; }}
      h3 {{ font-size:18px; margin:22px 0 12px 0; letter-spacing:.4px; }}
      .summary-grid, .line-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px 20px; font-size:16px; line-height:1.45; }}
      .items-table {{ width:100%; border-collapse:collapse; margin-top:12px; font-family:Arial,sans-serif; font-size:12px; }}
      .items-table th {{ background:#0f172a; color:white; padding:7px 6px; text-align:left; }} .items-table td {{ border:1px solid #d1d5db; padding:7px 6px; vertical-align:top; }} .items-table tr:nth-child(even) td {{ background:#f8fafc; }} .qty {{ font-weight:800; text-align:center; }}
      .fragile {{ background:#fee2e2; color:#991b1b; border:1px solid #fecaca; border-radius:999px; padding:2px 8px; font-size:11px; font-weight:800; }} .nonfragile {{ background:#e0f2fe; color:#075985; border:1px solid #bae6fd; border-radius:999px; padding:2px 8px; font-size:11px; font-weight:800; }}
      .signature-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px 24px; margin-top:12px; font-size:16px; }} .signature-grid span {{ display:block; border-bottom:1px solid #111827; min-height:22px; padding-top:6px; }} .signature-grid.small {{ font-size:14px; gap:10px 18px; }}
      .security-box {{ margin-top:22px; padding:14px; border:1px solid #94a3b8; border-radius:12px; background:#f8fafc; font-family:Arial,sans-serif; }}
      .verification-band {{ margin-top:22px; padding:11px 13px; border-left:5px solid #0f766e; background:#ecfdf5; font-family:Arial,sans-serif; font-size:13px; display:grid; grid-template-columns:2fr 1fr; gap:12px; }}
      .footer {{ margin-top:18px; text-align:center; font-size:15px; line-height:1.35; }}
    </style>
    """
    return html


def render_gateway_pass_preview(gateway_pass_id: int):
    try:
        import streamlit.components.v1 as components
        st.markdown("#### Gateway Pass Preview")
        st.caption("Review the final company-format gate pass before downloading. The Generate button remains disabled until approval.")
        components.html(gateway_pass_preview_html(gateway_pass_id), height=1050, scrolling=True)
    except Exception as exc:
        st.warning(f"Preview could not be displayed in this environment: {exc}")


def _phase2_bootstrap():
    """Ensure phase 2 tables exist once per user session.

    This function is called by notification, settings, availability and gateway
    pages. Without a session guard, every navigation click repeats schema/seed
    checks and adds avoidable latency.
    """
    if st.session_state.get("_phase2_bootstrap_done"):
        return
    try:
        from core.db import ensure_phase2_schema, seed_phase2_defaults
        ensure_phase2_schema(); seed_phase2_defaults()
        st.session_state["_phase2_bootstrap_done"] = True
    except Exception:
        # Do not block navigation if an older database is temporarily locked;
        # the next page interaction can try again.
        pass


def _table_exists_local(table: str) -> bool:
    try:
        return bool(df_query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).shape[0])
    except Exception:
        return False


def _safe_table_df(table: str, limit: int = 1000) -> pd.DataFrame:
    if not _table_exists_local(table):
        return pd.DataFrame()
    return df_query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT {int(limit)}")


def notification_preferences_page():
    _phase2_bootstrap()
    st.markdown("#### Notification Preferences")
    st.caption("In-app alerts are instant inside Streamlit. Email alerts require this checkbox, a saved email address, and SMTP settings on the server.")
    uid = int(user()["id"])
    rows = df_query("SELECT * FROM notification_preferences WHERE user_id=?", (uid,))
    if rows.empty:
        run_query("INSERT OR IGNORE INTO notification_preferences (user_id, created_at, updated_at) VALUES (?, ?, ?)", (uid, now_iso(), now_iso()))
        rows = df_query("SELECT * FROM notification_preferences WHERE user_id=?", (uid,))
    pref = rows.iloc[0]
    email_row = df_query("SELECT email FROM users WHERE id=?", (uid,))
    current_email = "" if email_row.empty or pd.isna(email_row.iloc[0].get("email")) else str(email_row.iloc[0].get("email") or "")
    smtp_ready, smtp_message = email_delivery_ready()

    with st.form(f"notification_pref_form_{uid}"):
        st.markdown("##### Where should ProcureFlow alert me?")
        c1, c2, c3 = st.columns(3)
        in_app = c1.checkbox("In-app notifications", value=bool(pref["in_app_enabled"]), key=f"pref_inapp_{uid}")
        browser_push = c2.checkbox("Browser/system push notifications", value=bool(pref["browser_push_enabled"]), key=f"pref_browser_{uid}")
        email_enabled = c3.checkbox("Email notifications", value=bool(pref["email_enabled"]), key=f"pref_email_{uid}", help="When enabled, high/important alerts can be emailed if email + SMTP are configured.")
        profile_email = st.text_input("My email address for notifications", value=current_email, key=f"pref_profile_email_{uid}", placeholder="name@company.com")
        c4, c5, c6 = st.columns(3)
        important_only = c4.checkbox("Important-only push notifications", value=bool(pref["important_only"]), key=f"pref_important_{uid}")
        approval_notifications = c5.checkbox("Approval notifications", value=bool(pref["approval_notifications"]), key=f"pref_approval_{uid}")
        gateway_notifications = c6.checkbox("Gateway pass notifications", value=bool(pref["gateway_pass_notifications"]), key=f"pref_gateway_{uid}")
        c7, c8 = st.columns(2)
        finance_notifications = c7.checkbox("Finance notifications", value=bool(pref["finance_notifications"]), key=f"pref_finance_{uid}")
        delegation_notifications = c8.checkbox("Away/delegation notifications", value=bool(pref["delegation_notifications"]), key=f"pref_delegation_{uid}")
        saved = st.form_submit_button("Save Notification Preferences")
    if saved:
        email_clean = (profile_email or "").strip()
        if email_enabled and (not email_clean or "@" not in email_clean):
            st.error("Enter a valid email address before enabling email notifications.")
        else:
            run_query(
                """
                UPDATE notification_preferences
                SET in_app_enabled=?, browser_push_enabled=?, email_enabled=?, important_only=?, approval_notifications=?, gateway_pass_notifications=?, finance_notifications=?, delegation_notifications=?, updated_at=?
                WHERE user_id=?
                """,
                (int(in_app), int(browser_push), int(email_enabled), int(important_only), int(approval_notifications), int(gateway_notifications), int(finance_notifications), int(delegation_notifications), now_iso(), uid),
            )
            run_query("UPDATE users SET email=? WHERE id=?", (email_clean or None, uid))
            log_audit("NOTIFICATION_PREFERENCES_UPDATED", "User", uid, "User updated notification preferences", uid, user()["role"])
            st.success("Notification preferences saved.")

    st.markdown("#### Email delivery status")
    if smtp_ready:
        st.success(smtp_message)
    else:
        st.warning(smtp_message)
    if not current_email:
        st.info("Save an email address above before enabling email notifications. SMTP settings are still required before real emails leave the app.")
    st.caption("Server SMTP variables: PROCUREFLOW_SMTP_HOST, PROCUREFLOW_SMTP_PORT, PROCUREFLOW_SMTP_USERNAME, PROCUREFLOW_SMTP_PASSWORD, PROCUREFLOW_SMTP_FROM, PROCUREFLOW_SMTP_USE_TLS.")

    st.markdown("#### Browser/system push setup")
    status = pref.get("browser_permission_status", "not_requested") if hasattr(pref, "get") else pref["browser_permission_status"]
    st.caption(f"Current saved browser permission status: {status}")
    if st.button("Enable Browser Notifications", key=f"enable_browser_notifications_{uid}", type="primary"):
        run_query(
            "UPDATE notification_preferences SET browser_push_enabled=1, browser_permission_status='requested', updated_at=? WHERE user_id=?",
            (now_iso(), uid),
        )
        run_query(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, user_agent, is_active, created_at, updated_at)
            VALUES (?, 'browser-permission-requested-local-session', 'Captured by browser when supported', 1, ?, ?)
            """,
            (uid, now_iso(), now_iso()),
        )
        try:
            import streamlit.components.v1 as components
            components.html(
                """
                <button id="pfNotifyBtn" style="padding:10px 14px;border-radius:10px;border:1px solid #ccc;background:#0f766e;color:white;font-weight:700;">Grant Browser Permission</button>
                <div id="pfNotifyResult" style="margin-top:8px;font-family:sans-serif;font-size:13px;"></div>
                <script>
                const result = document.getElementById('pfNotifyResult');
                document.getElementById('pfNotifyBtn').onclick = async () => {
                  if (!('Notification' in window)) { result.innerText = 'This browser does not support system notifications.'; return; }
                  try {
                    const permission = await Notification.requestPermission();
                    result.innerText = 'Browser permission: ' + permission + '. Service worker registration will be attempted if supported by this deployment.';
                    if (permission === 'granted') {
                      new Notification('ProcureFlow notifications enabled', { body: 'Important procurement alerts can appear here when supported.' });
                    }
                    if ('serviceWorker' in navigator) {
                      try { await navigator.serviceWorker.register('/static/procureflow_service_worker.js'); }
                      catch (e) { console.warn('Service worker registration not available in this deployment', e); }
                    }
                  } catch (e) { result.innerText = 'Browser notification setup could not complete: ' + e; }
                };
                </script>
                """,
                height=130,
            )
        except Exception as exc:
            st.warning(f"Browser permission widget could not load here: {exc}")
        st.info("External push is marked as requested. If the browser/deployment cannot support full web push, ProcureFlow keeps unread in-app alerts and queues email when SMTP is configured.")

    outbox = df_query("SELECT channel, status, COUNT(*) count FROM notification_outbox WHERE target_user_id=? GROUP BY channel, status", (uid,))
    if not outbox.empty:
        st.markdown("#### My external notification outbox summary")
        dataframe(outbox)

def settings_page():
    st.subheader("Settings")
    change_password_panel()
    notification_preferences_page()
    if user()["role"] in AWAY_ROLES:
        st.divider(); availability_panel(compact=True)
    st.caption("Production mode: set PROCUREFLOW_PRODUCTION=1 to hide demo credentials and require operational password practices.")


def render_notification_panel(current: dict):
    """Sidebar notification bell/panel with one-time toast and external delivery status."""
    _phase2_bootstrap()
    uid = int(current["id"])
    role = current["role"]
    unread = df_query(
        """
        SELECT * FROM notifications
        WHERE is_read=0 AND (user_id=? OR role=? OR role='All')
        ORDER BY CASE importance WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 WHEN 'Important' THEN 2 ELSE 3 END, created_at DESC LIMIT 30
        """,
        (uid, role),
    )
    pending_popups = df_query(
        """
        SELECT * FROM notifications
        WHERE is_read=0 AND COALESCE(popup_shown,0)=0 AND (user_id=? OR role=? OR role='All')
        ORDER BY CASE importance WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 WHEN 'Important' THEN 2 ELSE 3 END, created_at DESC LIMIT 5
        """,
        (uid, role),
    )
    for _, n in pending_popups.iterrows():
        msg = f"{n['title']}: {n['message']}"
        if str(n.get("importance", "Normal")) in ["Critical", "High", "Important"]:
            st.toast(msg, icon="🔔")
        run_query("UPDATE notifications SET popup_shown=1 WHERE id=?", (int(n["id"]),))

    st.markdown(f"### 🔔 Notifications ({len(unread)})")
    if unread.empty:
        st.caption("No unread notifications.")
        return
    critical = unread[unread["importance"].isin(["Critical", "High", "Important"])] if "importance" in unread.columns else pd.DataFrame()
    if not critical.empty:
        st.warning(f"{len(critical)} important unread alert(s).")
    with st.expander("Unread notifications", expanded=False):
        for _, n in unread.head(10).iterrows():
            imp = n.get("importance", "Normal") if hasattr(n, "get") else n["importance"]
            st.markdown(f"**{n['title']}** · `{imp}`")
            st.caption(f"{n['message']} · {n['created_at']}")
            if n.get("action_label") if hasattr(n, "get") else False:
                st.caption(f"Action: {n['action_label']}")
            st.divider()
        if st.button("Mark all as read", key=f"notif_mark_all_phase2_{uid}_{role}", use_container_width=True):
            run_query("UPDATE notifications SET is_read=1 WHERE is_read=0 AND (user_id=? OR role=? OR role='All')", (uid, role))
            st.rerun()


def availability_panel(compact: bool = False):
    _phase2_bootstrap()
    if user()["role"] not in AWAY_ROLES:
        st.info("Availability notices are available to Approver/MD and Procurement Manager roles.")
        return
    st.subheader("Availability / Away Notice" if not compact else "My Availability")
    uid = int(user()["id"])
    active = df_query(
        """
        SELECT * FROM user_availability
        WHERE user_id=? AND status NOT IN ('Returned','Cancelled')
        ORDER BY created_at DESC LIMIT 5
        """,
        (uid,),
    )
    if not active.empty:
        st.markdown("#### Current away/delegation status")
        dataframe(active[["id", "status", "away_start_date", "away_end_date", "urgency", "admin_review_status", "linked_delegation_id", "created_at"]])
        latest = active.iloc[0]
        if st.button("I am back", key=f"i_am_back_{uid}_{int(latest['id'])}"):
            run_query("UPDATE user_availability SET status='Returned', updated_at=? WHERE id=?", (now_iso(), int(latest["id"])))
            create_notification(None, "Admin", "User returned from away notice", f"{user()['full_name']} has marked themselves back. Review any active delegation.", "Availability", int(latest["id"]), "High", ["in_app", "browser_push"])
            create_activity_log(uid, user()["role"], "USER_RETURNED", "Availability", int(latest["id"]), "User marked themselves back", visibility_scope="workflow")
            log_audit("USER_RETURNED_FROM_AWAY", "Availability", int(latest["id"]), "User clicked I am back", uid, user()["role"])
            st.success("Admin has been notified that you are back.")
            st.rerun()
    else:
        st.success("You are currently marked as available.")

    with st.expander("Mark Myself Away", expanded=not compact):
        with st.form(f"mark_away_form_{uid}"):
            c1, c2, c3 = st.columns(3)
            start = c1.date_input("Away start date", date.today(), key=f"away_start_{uid}")
            end = c2.date_input("Away end date", date.today() + timedelta(days=1), key=f"away_end_{uid}")
            urgency = c3.selectbox("Urgency level", ["Normal", "High", "Critical"], key=f"away_urgency_{uid}")
            reason = st.text_area("Reason", key=f"away_reason_{uid}")
            note = st.text_area("Optional handover note", key=f"away_note_{uid}")
            c4, c5 = st.columns(2)
            default_delegate = "Procurement Manager" if user()["role"] == "Approver" else "Admin"
            delegate_role = c4.selectbox("Recommended delegate role", ["Procurement Manager", "Admin", "Approver", "Finance"], index=["Procurement Manager", "Admin", "Approver", "Finance"].index(default_delegate), key=f"away_delegate_role_{uid}")
            users_for_role = df_query("SELECT id, full_name, username FROM users WHERE role=? AND is_active=1 ORDER BY full_name", (delegate_role,))
            delegate_labels = ["No specific user"] + [f"{r.full_name} ({r.username}) #{int(r.id)}" for r in users_for_role.itertuples()]
            delegate_label = c5.selectbox("Recommended delegate user", delegate_labels, key=f"away_delegate_user_{uid}")
            submitted = st.form_submit_button("Submit Away Notice", type="primary")
        if submitted:
            if end < start:
                st.error("Away end date cannot be before away start date.")
            elif not reason.strip():
                st.error("Reason is required.")
            else:
                delegate_user_id = None if delegate_label == "No specific user" else int(delegate_label.rsplit("#", 1)[1])
                availability_id = run_insert(
                    """
                    INSERT INTO user_availability (user_id, role, status, away_start_date, away_end_date, reason, handover_note, recommended_delegate_role, recommended_delegate_user_id, urgency, admin_review_status, created_at, updated_at)
                    VALUES (?, ?, 'Away Requested', ?, ?, ?, ?, ?, ?, ?, 'Pending Review', ?, ?)
                    """,
                    (uid, user()["role"], start.isoformat(), end.isoformat(), reason.strip(), note.strip(), delegate_role, delegate_user_id, urgency, now_iso(), now_iso()),
                )
                create_notification(None, "Admin", f"{user()['role']} marked away", f"{user()['full_name']} will be away from {start.isoformat()} to {end.isoformat()}. Admin delegation review is needed.", "Availability", availability_id, "High" if urgency != "Critical" else "Critical", ["in_app", "browser_push"])
                add_workflow_event("Availability", availability_id, "Away Notice Submitted", "Away Requested", reason, uid)
                create_activity_log(uid, user()["role"], "AWAY_NOTICE_SUBMITTED", "Availability", availability_id, f"Away notice submitted for {start.isoformat()} to {end.isoformat()}", note, "workflow")
                log_audit("AWAY_NOTICE_SUBMITTED", "Availability", availability_id, {"reason": reason, "delegate_role": delegate_role}, uid, user()["role"])
                st.success("Away notice submitted. Admin has been notified.")
                st.rerun()


def availability_delegation_requests_page():
    _phase2_bootstrap()
    st.subheader("Availability & Delegation Requests")
    pending = df_query(
        """
        SELECT ua.*, u.full_name, u.username, du.full_name recommended_delegate_name
        FROM user_availability ua
        LEFT JOIN users u ON u.id=ua.user_id
        LEFT JOIN users du ON du.id=ua.recommended_delegate_user_id
        ORDER BY CASE ua.admin_review_status WHEN 'Pending Review' THEN 0 ELSE 1 END, ua.created_at DESC
        """
    )
    if pending.empty:
        st.success("No availability or delegation requests yet.")
        return
    display = pending[["id", "full_name", "role", "status", "away_start_date", "away_end_date", "urgency", "admin_review_status", "recommended_delegate_role", "recommended_delegate_name", "linked_delegation_id"]].copy()
    dataframe(display)
    csv_download(display, "availability_delegation_requests")
    labels = [f"#{int(r.id)} — {r.full_name} — {r.role} — {r.admin_review_status}" for r in pending.itertuples()]
    selected = st.selectbox("Open request", labels, key="admin_availability_select")
    av_id = int(selected.split("—", 1)[0].strip().lstrip("#"))
    row = pending[pending["id"] == av_id].iloc[0]
    st.info(f"Reason: {row['reason']}\n\nHandover note: {row['handover_note'] or 'None'}")
    delegate_roles = ["Procurement Manager", "Admin", "Approver", "Finance"]
    with st.form(f"admin_availability_action_{av_id}"):
        c1, c2, c3 = st.columns(3)
        action = c1.selectbox("Admin action", ["Approve away notice", "Reject away notice", "Activate delegation", "Close delegation / mark reviewed"], key=f"av_action_{av_id}")
        chosen_role = c2.selectbox("Delegate role", delegate_roles, index=delegate_roles.index(row["recommended_delegate_role"]) if row["recommended_delegate_role"] in delegate_roles else 0, key=f"av_delegate_role_{av_id}")
        delegate_users = df_query("SELECT id, full_name, username FROM users WHERE role=? AND is_active=1 ORDER BY full_name", (chosen_role,))
        user_labels = ["No specific user"] + [f"{r.full_name} ({r.username}) #{int(r.id)}" for r in delegate_users.itertuples()]
        chosen_user_label = c3.selectbox("Delegate user", user_labels, key=f"av_delegate_user_{av_id}")
        c4, c5 = st.columns(2)
        start = c4.date_input("Delegation start date", pd.to_datetime(row["away_start_date"]).date(), key=f"av_start_{av_id}")
        end = c5.date_input("Delegation end date", pd.to_datetime(row["away_end_date"]).date(), key=f"av_end_{av_id}")
        admin_note = st.text_area("Admin note", key=f"av_note_{av_id}")
        submit = st.form_submit_button("Apply Admin Action", type="primary")
    if submit:
        delegate_user_id = None if chosen_user_label == "No specific user" else int(chosen_user_label.rsplit("#", 1)[1])
        if action == "Reject away notice":
            run_query("UPDATE user_availability SET admin_review_status='Rejected', status='Cancelled', reviewed_by_admin_id=?, reviewed_at=?, admin_note=?, updated_at=? WHERE id=?", (user()["id"], now_iso(), admin_note, now_iso(), av_id))
            create_notification(int(row["user_id"]), None, "Away notice rejected", admin_note or "Admin rejected the away notice.", "Availability", av_id, "High", ["in_app", "browser_push"])
            log_audit("AWAY_NOTICE_REJECTED", "Availability", av_id, admin_note, user()["id"], user()["role"])
        elif action == "Approve away notice":
            run_query("UPDATE user_availability SET admin_review_status='Approved', status='Away Approved', reviewed_by_admin_id=?, reviewed_at=?, admin_note=?, updated_at=? WHERE id=?", (user()["id"], now_iso(), admin_note, now_iso(), av_id))
            create_notification(int(row["user_id"]), None, "Away notice approved", admin_note or "Admin approved your away notice.", "Availability", av_id, "High", ["in_app", "browser_push"])
            log_audit("AWAY_NOTICE_APPROVED", "Availability", av_id, admin_note, user()["id"], user()["role"])
        elif action == "Activate delegation":
            primary_role = row["role"]
            if primary_role == "Approver":
                delegate_role = "Procurement Manager" if chosen_role not in ["Admin", "Approver"] else chosen_role
            else:
                delegate_role = chosen_role
            delegation_id = run_insert(
                """
                INSERT INTO approval_delegations (primary_role, delegate_role, enabled, start_date, end_date, reason, created_by, created_at, updated_at, source_availability_id, source_reason, activated_by_admin_id, activation_note)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (primary_role, delegate_role, start.isoformat(), end.isoformat(), f"Delegation active due to away notice: {row['reason']}", user()["id"], now_iso(), now_iso(), av_id, row["reason"], user()["id"], admin_note),
            )
            run_query("UPDATE user_availability SET admin_review_status='Delegation Active', status='Away Active', linked_delegation_id=?, reviewed_by_admin_id=?, reviewed_at=?, admin_note=?, updated_at=? WHERE id=?", (delegation_id, user()["id"], now_iso(), admin_note, now_iso(), av_id))
            create_notification(int(row["user_id"]), None, "Delegation activated", f"Admin activated {delegate_role} as delegate during your away period.", "Availability", av_id, "High", ["in_app", "browser_push"])
            if delegate_user_id:
                create_notification(delegate_user_id, None, "Delegation assigned", f"You were selected as delegate for {row['full_name']} during their away period.", "Availability", av_id, "High", ["in_app", "browser_push"])
            else:
                create_notification(None, delegate_role, "Delegation activated", f"{delegate_role} delegation is active due to away notice.", "Availability", av_id, "High", ["in_app", "browser_push"])
            log_audit("DELEGATION_ACTIVATED_FROM_AWAY", "ApprovalDelegation", delegation_id, {"availability_id": av_id, "delegate_role": delegate_role, "delegate_user_id": delegate_user_id}, user()["id"], user()["role"])
        else:
            if row.get("linked_delegation_id"):
                run_query("UPDATE approval_delegations SET enabled=0, updated_at=?, activation_note=COALESCE(activation_note,'') || ? WHERE id=?", (now_iso(), f"\nClosed by Admin: {admin_note}", int(row["linked_delegation_id"])))
            run_query("UPDATE user_availability SET admin_review_status='Closed', status='Returned', reviewed_by_admin_id=?, reviewed_at=?, admin_note=?, updated_at=? WHERE id=?", (user()["id"], now_iso(), admin_note, now_iso(), av_id))
            create_notification(int(row["user_id"]), None, "Delegation reviewed/closed", admin_note or "Admin reviewed or closed the delegation linked to your away notice.", "Availability", av_id, "Normal", ["in_app"])
            log_audit("AWAY_DELEGATION_CLOSED", "Availability", av_id, admin_note, user()["id"], user()["role"])
        create_activity_log(user()["id"], user()["role"], action.upper().replace(" ", "_"), "Availability", av_id, f"Admin action: {action}", admin_note, "admin", int(row["user_id"]))
        st.success("Admin action applied.")
        st.rerun()


def log_gateway_event(gateway_pass_id: int, event: str, status: str | None = None, note: str | None = None):
    run_query("INSERT INTO gateway_pass_events (gateway_pass_id, event, status, note, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (gateway_pass_id, event, status, note, user()["id"], now_iso()))
    create_activity_log(user()["id"], user()["role"], event.upper().replace(" ", "_"), "Gateway Pass", gateway_pass_id, f"Gateway pass {event.lower()}", note, "workflow")
    log_audit(event.upper().replace(" ", "_"), "Gateway Pass", gateway_pass_id, note, user()["id"], user()["role"])


def gateway_pass_items_df(gateway_pass_id: int) -> pd.DataFrame:
    return df_query("SELECT * FROM gateway_pass_items WHERE gateway_pass_id=? ORDER BY id", (gateway_pass_id,))


def gateway_pass_summary_df(where_sql: str = "", params: tuple | list = ()) -> pd.DataFrame:
    sql = """
        SELECT gp.*, fm.full_name facility_manager, approver.full_name approved_by
        FROM gateway_passes gp
        LEFT JOIN users fm ON fm.id=gp.facility_manager_user_id
        LEFT JOIN users approver ON approver.id=gp.approved_by_user_id
    """
    if where_sql:
        sql += " WHERE " + where_sql
    sql += " ORDER BY gp.updated_at DESC, gp.created_at DESC"
    return df_query(sql, params)


def gateway_pass_detail(gateway_pass_id: int):
    gp = gateway_pass_summary_df("gp.id=?", (gateway_pass_id,))
    if gp.empty:
        st.error("Gateway pass not found.")
        return None
    row = gp.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pass No.", row["pass_number"])
    c2.metric("Status", row["status"])
    c3.metric("Movement", row["movement_type"])
    c4.metric("Destination", row["destination"] or "-")
    info_cols = ["facility_manager", "department", "purpose", "origin_location", "destination", "expected_movement_date", "expected_return_date", "vehicle_number", "driver_name", "driver_phone", "receiver_name", "receiver_organization", "security_checkpoint", "approved_by", "approved_by_role", "approved_at"]
    st.dataframe(pd.DataFrame([{k: row.get(k, "") for k in info_cols if k in row.index}]), use_container_width=True)
    items = gateway_pass_items_df(gateway_pass_id)
    st.markdown("#### Item lines — quality, quantity and fragility")
    if items.empty:
        st.warning("No item lines have been added yet.")
    else:
        show_cols = ["item_description", "item_category", "colour", "quantity", "unit_of_measure", "quality_condition", "fragility_status", "serial_number", "asset_tag", "handling_instruction", "remarks"]
        show = items[[c for c in show_cols if c in items.columns]].copy()
        dataframe(show)
        if (items["fragility_status"] == "Fragile").any():
            st.warning("This gateway pass contains fragile item(s). Review handling instructions before approval.")
    return row


def create_gateway_pass_form():
    _phase2_bootstrap()
    st.subheader("Create Gateway Pass Draft")
    with st.form("create_gateway_pass_form"):
        c1, c2, c3 = st.columns(3)
        dept = c1.selectbox("Department", department_options(), key="gp_create_dept")
        movement_type = c2.selectbox("Movement type", GATEWAY_MOVEMENT_TYPES, key="gp_create_movement")
        expected_movement = c3.date_input("Expected movement date", date.today(), key="gp_create_move_date")
        purpose = st.text_area("Purpose of movement", key="gp_create_purpose")
        c4, c5 = st.columns(2)
        origin = c4.text_input("Origin location", key="gp_create_origin")
        destination = c5.text_input("Destination", key="gp_create_destination")
        c6, c7, c8 = st.columns(3)
        return_required = c6.checkbox("Expected return date applies", value=False, key="gp_create_return_applies")
        expected_return = c6.date_input("Expected return date", date.today() + timedelta(days=1), key="gp_create_return") if return_required else None
        vehicle = c7.text_input("Vehicle number, optional", key="gp_create_vehicle")
        checkpoint = c8.text_input("Security checkpoint, optional", key="gp_create_checkpoint")
        c9, c10, c11 = st.columns(3)
        driver = c9.text_input("Driver name, optional", key="gp_create_driver")
        driver_phone = c10.text_input("Driver phone, optional", key="gp_create_driver_phone")
        receiver = c11.text_input("Receiver name", key="gp_create_receiver")
        receiver_org = st.text_input("Receiver organization, optional", key="gp_create_receiver_org")
        item_count = st.number_input("Number of item lines", min_value=1, max_value=20, value=1, step=1, key="gp_item_count")
        items = []
        st.markdown("##### Item details")
        for i in range(int(item_count)):
            st.markdown(f"**Item {i+1}**")
            a, b, c, d0 = st.columns([1.6, 1, .7, .8])
            desc = a.text_input("Item description", key=f"gp_item_desc_{i}")
            category = b.text_input("Item category", key=f"gp_item_cat_{i}")
            qty = c.number_input("Quantity", min_value=0.0, step=1.0, value=1.0, key=f"gp_item_qty_{i}")
            colour = d0.text_input("Colour", key=f"gp_item_colour_{i}")
            d, e, f = st.columns(3)
            uom = d.selectbox("Unit of measure", GATEWAY_UOMS, key=f"gp_item_uom_{i}")
            quality = e.selectbox("Quality / condition", GATEWAY_QUALITY_OPTIONS, key=f"gp_item_quality_{i}")
            fragile = f.selectbox("Fragility status", GATEWAY_FRAGILITY_OPTIONS, key=f"gp_item_fragile_{i}")
            g, h, k = st.columns(3)
            value = g.number_input("Estimated value, optional", min_value=0.0, step=1000.0, key=f"gp_item_value_{i}")
            serial = h.text_input("Serial number", key=f"gp_item_serial_{i}")
            asset = k.text_input("Asset tag", key=f"gp_item_asset_{i}")
            handling = st.text_input("Handling instruction", key=f"gp_item_handling_{i}")
            remarks = st.text_input("Remarks", key=f"gp_item_remarks_{i}")
            items.append({"desc": desc, "category": category, "qty": qty, "colour": colour, "uom": uom, "quality": quality, "fragile": fragile, "value": value, "serial": serial, "asset": asset, "handling": handling, "remarks": remarks})
        submitted = st.form_submit_button("Create Gateway Pass Draft", type="primary")
    if submitted:
        valid_items = [x for x in items if x["desc"].strip()]
        if not purpose.strip() or not receiver.strip():
            st.error("Purpose of movement and receiver name are required.")
            return
        if not valid_items:
            st.error("At least one item line is required before saving.")
            return
        bad_qty = [x for x in valid_items if float(x["qty"] or 0) <= 0 or not x["uom"]]
        if bad_qty:
            st.error("Each item quantity must be greater than 0 and unit of measure is required.")
            return
        pass_no = make_ref("GP")
        gp_id = run_insert(
            """
            INSERT INTO gateway_passes (pass_number, facility_manager_user_id, department, movement_type, purpose, origin_location, destination, expected_movement_date, expected_return_date, vehicle_number, driver_name, driver_phone, receiver_name, receiver_organization, security_checkpoint, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', ?, ?)
            """,
            (pass_no, user()["id"], dept, movement_type, purpose.strip(), origin, destination, expected_movement.isoformat(), expected_return.isoformat() if expected_return else None, vehicle, driver, driver_phone, receiver, receiver_org, checkpoint, now_iso(), now_iso()),
        )
        for item in valid_items:
            run_query(
                """
                INSERT INTO gateway_pass_items (gateway_pass_id, item_description, item_category, colour, quantity, unit_of_measure, quality_condition, estimated_value, serial_number, asset_tag, fragility_status, handling_instruction, remarks, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (gp_id, item["desc"].strip(), item["category"], item["colour"], float(item["qty"]), item["uom"], item["quality"], float(item["value"] or 0), item["serial"], item["asset"], item["fragile"], item["handling"], item["remarks"], now_iso()),
            )
        log_gateway_event(gp_id, "Gateway Pass Draft Created", "Draft", pass_no)
        _rerun_success(f"Gateway pass draft created: {pass_no}")


def submit_gateway_pass(gateway_pass_id: int):
    items = gateway_pass_items_df(gateway_pass_id)
    if items.empty:
        st.error("At least one item line is required before submission.")
        return
    if (items["quantity"].fillna(0) <= 0).any() or items["unit_of_measure"].fillna("").eq("").any():
        st.error("Every item must have quantity greater than 0 and a unit of measure.")
        return
    row = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,)).iloc[0]
    if row["status"] not in ["Draft", "Returned for Correction"]:
        st.warning("Only Draft or Returned gateway passes can be submitted.")
        return
    run_query("UPDATE gateway_passes SET status='Submitted', submitted_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), gateway_pass_id))
    log_gateway_event(gateway_pass_id, "Gateway Pass Submitted", "Submitted", "Submitted for Procurement Manager / Approver review")
    from core.db import notify_gateway_pass_reviewers
    notify_gateway_pass_reviewers(gateway_pass_id, "Gateway Pass Submitted", f"{row['pass_number']} has been submitted and requires review.")
    create_notification(int(row["facility_manager_user_id"]), None, "Gateway pass submitted", f"{row['pass_number']} was submitted for approval.", "Gateway Pass", gateway_pass_id, "Normal", ["in_app"])
    st.success("Gateway pass submitted for approval.")
    st.rerun()


def _gateway_approve(gateway_pass_id: int, decision: str, note: str):
    row = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,)).iloc[0]
    if user()["role"] not in ["Procurement Manager", "Approver", "Admin"]:
        st.error("You are not authorized to approve gateway passes.")
        return
    if decision == "Approved" and row["status"] in ["Approved", "Generated", "Downloaded"]:
        st.warning("This gateway pass has already been approved.")
        return
    if decision in ["Rejected", "Returned for Correction"] and not note.strip():
        st.error("A rejection or return reason is required.")
        return
    if decision == "Approved":
        run_query("UPDATE gateway_passes SET status='Approved', approved_at=?, approved_by_user_id=?, approved_by_role=?, approval_note=?, updated_at=? WHERE id=?", (now_iso(), user()["id"], user()["role"], note, now_iso(), gateway_pass_id))
        decision_label = "Approved"
        title = "Gateway Pass Approved"
        msg = f"{row['pass_number']} has been approved. You can now generate and download the final gateway pass."
    elif decision == "Rejected":
        run_query("UPDATE gateway_passes SET status='Rejected', rejected_at=?, rejected_by_user_id=?, rejection_reason=?, updated_at=? WHERE id=?", (now_iso(), user()["id"], note, now_iso(), gateway_pass_id))
        decision_label = "Rejected"
        title = "Gateway Pass Rejected"
        msg = f"{row['pass_number']} was rejected. Reason: {note}"
    else:
        run_query("UPDATE gateway_passes SET status='Returned for Correction', rejection_reason=?, updated_at=? WHERE id=?", (note, now_iso(), gateway_pass_id))
        decision_label = "Returned for Correction"
        title = "Gateway Pass Returned"
        msg = f"{row['pass_number']} was returned for correction. Reason: {note}"
    run_query("INSERT INTO gateway_pass_approvals (gateway_pass_id, approver_user_id, approver_role, decision, note, created_at) VALUES (?, ?, ?, ?, ?, ?)", (gateway_pass_id, user()["id"], user()["role"], decision_label, note, now_iso()))
    log_gateway_event(gateway_pass_id, f"Gateway Pass {decision_label}", decision_label, note)
    _notify_gateway_event({**row.to_dict(), "id": gateway_pass_id}, title, msg, target="facility", importance="High")
    _rerun_success(f"Gateway pass {decision_label.lower()}.")


def _gateway_cancel_or_reopen(gateway_pass_id: int, action: str, note: str):
    if user()["role"] != "Admin":
        st.error("Only Admin can cancel or reopen gateway passes.")
        return
    if not note.strip():
        st.error("Admin reason is required.")
        return
    new_status = "Cancelled" if action == "Cancel" else "Returned for Correction"
    run_query("UPDATE gateway_passes SET status=?, rejection_reason=?, updated_at=? WHERE id=?", (new_status, note, now_iso(), gateway_pass_id))
    log_gateway_event(gateway_pass_id, f"Gateway Pass {action}", new_status, note)
    row = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,)).iloc[0]
    create_notification(int(row["facility_manager_user_id"]), None, f"Gateway Pass {action}", f"{row['pass_number']} was {action.lower()}ed by Admin. {note}", "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"])
    st.success(f"Gateway pass {action.lower()}ed.")
    st.rerun()


def generate_gateway_pass_document(gateway_pass_id: int) -> str | None:
    gp = gateway_pass_summary_df("gp.id=?", (gateway_pass_id,))
    if gp.empty:
        st.error("Gateway pass not found.")
        return None
    row = gp.iloc[0]
    if row["status"] not in ["Approved", "Generated", "Downloaded"]:
        st.error("Generate is disabled until the gateway pass is approved.")
        return None
    items = gateway_pass_items_df(gateway_pass_id)
    if items.empty:
        st.error("Cannot generate a gateway pass without item lines.")
        return None
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Image as RLImage
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from core.db import ATTACHMENT_DIR

        target_dir = ATTACHMENT_DIR / "gateway_passes"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{row['pass_number'].replace('/', '-')}_gateway_pass.pdf"

        page_w, page_h = A4
        doc = SimpleDocTemplate(
            str(path),
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=12 * mm,
            bottomMargin=12 * mm,
            title=f"Gateway Pass {row['pass_number']}",
            author="ProcureFlow",
        )
        styles = getSampleStyleSheet()
        body = ParagraphStyle("GPBody", parent=styles["BodyText"], fontName="Times-Roman", fontSize=9.2, leading=11.6, alignment=TA_LEFT, spaceAfter=8)
        small = ParagraphStyle("GPSmall", parent=styles["BodyText"], fontName="Helvetica", fontSize=6.5, leading=7.6)
        section = ParagraphStyle("GPSection", parent=styles["Heading3"], fontName="Times-Bold", fontSize=10.8, leading=12.5, spaceBefore=5, spaceAfter=3)
        center_title = ParagraphStyle("GPCenterTitle", parent=styles["Heading1"], fontName="Times-Bold", fontSize=14.5, leading=16.5, alignment=TA_CENTER, spaceAfter=2)
        center_sub = ParagraphStyle("GPCenterSub", parent=styles["Heading2"], fontName="Times-Bold", fontSize=12.5, leading=15, alignment=TA_CENTER, spaceAfter=2)
        motto = ParagraphStyle("GPMotto", parent=styles["BodyText"], fontName="Times-BoldItalic", fontSize=8.8, leading=10.5, alignment=TA_CENTER)
        doc_title = ParagraphStyle("GPDocTitle", parent=styles["Heading2"], fontName="Times-Bold", fontSize=12.5, leading=14, alignment=TA_LEFT, spaceBefore=6, spaceAfter=6)
        note_style = ParagraphStyle("GPNote", parent=styles["BodyText"], fontName="Helvetica", fontSize=7.2, leading=8.4, textColor=colors.HexColor("#0f172a"))

        def P(txt: Any, style=body):
            return Paragraph(str(txt), style)

        def safe(v: Any, default: str = "") -> str:
            return escape(_clean(v, default)).replace("\n", "<br/>")

        def logo_flow(path: Path, w: float = 18 * mm, h: float = 18 * mm):
            if path.exists():
                img = RLImage(str(path), width=w, height=h)
                img.hAlign = "CENTER"
                return img
            return P("<b>LOGO</b>", motto)

        def page_frame(canvas, _doc):
            canvas.saveState()
            canvas.setStrokeColor(colors.HexColor("#0f766e"))
            canvas.setLineWidth(1.1)
            canvas.roundRect(14 * mm, 12 * mm, page_w - 28 * mm, page_h - 24 * mm, 5 * mm, stroke=1, fill=0)
            canvas.setFillColor(colors.HexColor("#0f766e"))
            canvas.rect(14 * mm, page_h - 14 * mm, page_w - 28 * mm, 2 * mm, stroke=0, fill=1)
            canvas.setFillColor(colors.HexColor("#64748b"))
            canvas.setFont("Helvetica", 7.4)
            footer = f"{GATEWAY_COMPANY['address']} | Email: {GATEWAY_COMPANY['email']} | Phone NO.: {GATEWAY_COMPANY['phone']}"
            canvas.drawCentredString(page_w / 2, 8 * mm, footer[:170])
            canvas.restoreState()

        story = []
        header_table = Table(
            [[
                logo_flow(_gateway_asset_path("cmotd_logo.png")),
                [
                    P(safe(GATEWAY_COMPANY["center_name"]), center_title),
                    P(safe(GATEWAY_COMPANY["unit_name"]), center_sub),
                    P(f"<u>{safe(GATEWAY_COMPANY['motto'])}</u>", motto),
                ],
                logo_flow(_gateway_asset_path("rsu_logo.png")),
            ]],
            colWidths=[23 * mm, 128 * mm, 23 * mm],
        )
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("ALIGN", (2, 0), (2, 0), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(header_table)
        story.append(P("PROPERTY MOVEMENT GATE PASS", doc_title))

        ref_table = Table([
            [P(f"<b>Reference No.:</b> {safe(row.get('pass_number'))}", body), P(f"<b>Date:</b> {_fmt_date(row.get('generated_at') or row.get('created_at'))}", body)],
            [P(f"<b>Status:</b> {safe(row.get('status'))}", body), P(f"<b>System Reference:</b> GP-{gateway_pass_id}", body)],
        ], colWidths=[87 * mm, 87 * mm])
        ref_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("BOX", (0, 0), (-1, -1), .6, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), .3, colors.HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(ref_table)
        story.append(Spacer(1, 8))
        story.append(P("This Gate Pass serves as official authorization for the movement of the underlisted company asset(s) from the premises of the Centre for Marine and Offshore Technology Development (CMOTD).", body))

        story.append(P("PROPERTY DETAILS", section))
        first_item = items.iloc[0] if not items.empty else {}
        item_sentence = _row_value(first_item, "item_description", "listed company asset") if len(items) <= 1 else f"{len(items)} listed company assets"
        summary_rows = [
            [P("<b>Item Description:</b>", body), P(safe(item_sentence), body), P("<b>Department:</b>", body), P(safe(row.get("department"), "N/A"), body)],
            [P("<b>Colour:</b>", body), P(safe(_row_value(first_item, "colour", "See item table") if len(items) == 1 else "See item table"), body), P("<b>Quantity:</b>", body), P(safe(_qty_text(_row_value(first_item, "quantity", "0")) if len(items) == 1 else f"{len(items)} item line(s)"), body)],
            [P("<b>Condition:</b>", body), P(safe(_row_value(first_item, "quality_condition", "See item table") if len(items) == 1 else "See item table"), body), P("<b>Fragility:</b>", body), P(safe(_row_value(first_item, "fragility_status", "See item table") if len(items) == 1 else ("Contains fragile item(s)" if (items["fragility_status"] == "Fragile").any() else "Non-Fragile items")), body)],
        ]
        summary_table = Table(summary_rows, colWidths=[28 * mm, 58 * mm, 28 * mm, 60 * mm])
        summary_table.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), .45, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), .25, colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1f5f9")),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(summary_table)
        story.append(Spacer(1, 8))

        item_rows = [[
            P("<b>No.</b>", small), P("<b>Item Description / Category</b>", small), P("<b>Colour</b>", small),
            P("<b>Quantity</b>", small), P("<b>Unit</b>", small), P("<b>Quality / Condition</b>", small),
            P("<b>Fragile?</b>", small), P("<b>Serial / Asset Tag</b>", small), P("<b>Handling / Remarks</b>", small),
        ]]
        for no, item in enumerate(items.itertuples(), start=1):
            desc_cat = f"{safe(getattr(item, 'item_description', ''))}<br/><font color='#64748b'>{safe(getattr(item, 'item_category', ''))}</font>"
            serial_asset = " / ".join([x for x in [_clean(getattr(item, "serial_number", "")), _clean(getattr(item, "asset_tag", ""))] if x]) or "-"
            handling = "<br/>".join([x for x in [safe(getattr(item, "handling_instruction", "")), safe(getattr(item, "remarks", ""))] if x]) or "-"
            item_rows.append([
                P(no, small), P(desc_cat, small), P(safe(getattr(item, "colour", "-")), small),
                P(_qty_text(getattr(item, "quantity", "")), small), P(safe(getattr(item, "unit_of_measure", "")), small),
                P(safe(getattr(item, "quality_condition", "")), small), P(safe(getattr(item, "fragility_status", "")), small),
                P(safe(serial_asset), small), P(handling, small),
            ])
        item_table = Table(item_rows, repeatRows=1, colWidths=[7 * mm, 42 * mm, 14 * mm, 14 * mm, 11 * mm, 22 * mm, 16 * mm, 25 * mm, 23 * mm])
        item_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BOX", (0, 0), (-1, -1), .55, colors.HexColor("#94a3b8")),
            ("INNERGRID", (0, 0), (-1, -1), .25, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 1), (0, -1), "CENTER"),
            ("ALIGN", (3, 1), (4, -1), "CENTER"),
            ("PADDING", (0, 0), (-1, -1), 3.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ]))
        story.append(item_table)

        story.append(P("PURPOSE OF MOVEMENT", section))
        story.append(P(f"The above-mentioned {safe(item_sentence)} is/are being moved for: <b>{safe(row.get('purpose'), 'N/A')}</b>. Security personnel are hereby requested to permit the approved movement from <b>{safe(row.get('origin_location'), 'N/A')}</b> to <b>{safe(row.get('destination'), 'N/A')}</b>.", body))

        story.append(P("TRANSPORT DETAILS", section))
        transport_rows = [
            [P("<b>Movement Type:</b>", body), P(safe(row.get("movement_type"), "N/A"), body), P("<b>Movement Date:</b>", body), P(_fmt_date(row.get("expected_movement_date")), body)],
            [P("<b>Expected Return:</b>", body), P(_fmt_date(row.get("expected_return_date")), body), P("<b>Vehicle Number:</b>", body), P(safe(row.get("vehicle_number"), "________________"), body)],
            [P("<b>Driver's Name:</b>", body), P(safe(row.get("driver_name"), "________________"), body), P("<b>Driver's Phone Number:</b>", body), P(safe(row.get("driver_phone"), "________________"), body)],
            [P("<b>Receiver Name:</b>", body), P(safe(row.get("receiver_name"), "________________"), body), P("<b>Security Checkpoint:</b>", body), P(safe(row.get("security_checkpoint"), "Main Gate"), body)],
        ]
        transport_table = Table(transport_rows, colWidths=[32 * mm, 55 * mm, 34 * mm, 53 * mm])
        transport_table.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), .45, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), .25, colors.HexColor("#e2e8f0")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1f5f9")),
            ("PADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(transport_table)

        story.append(P("AUTHORIZATION", section))
        story.append(P("I hereby certify that the movement of the above company property has been duly approved and authorized.", body))
        auth_rows = [
            [P("<b>Authorizing Officer:</b>", body), P(safe(row.get("approved_by"), "____________________________"), body)],
            [P("<b>Designation:</b>", body), P(safe(row.get("approved_by_role"), "____________________________"), body)],
            [P("<b>Signature:</b>", body), P("____________________________", body)],
            [P("<b>Date:</b>", body), P(_fmt_date(row.get("approved_at")), body)],
        ]
        auth_table = Table(auth_rows, colWidths=[45 * mm, 129 * mm])
        auth_table.setStyle(TableStyle([
            ("LINEBELOW", (1, 0), (1, -1), .45, colors.HexColor("#334155")),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(auth_table)

        security_box = Table([[
            P("<b>SECURITY VERIFICATION</b><br/>Security Officer Name: ______________________________ &nbsp;&nbsp; Gate Verification Time: ______________________________<br/>Exit/Entry Confirmation: ______________________________ &nbsp;&nbsp; Signature: ______________________________", note_style)
        ]], colWidths=[174 * mm])
        security_box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), .65, colors.HexColor("#94a3b8")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(Spacer(1, 3))
        story.append(security_box)

        validity = Table([[
            P("<b>Validity Notice:</b> This gateway pass is valid only for the listed items and approved movement date.", note_style),
            P(f"<b>Generated:</b><br/>{_fmt_dt(now_iso())}<br/><b>System Ref:</b> GP-{gateway_pass_id}", note_style),
        ]], colWidths=[123 * mm, 51 * mm])
        validity.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), .65, colors.HexColor("#86efac")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ecfdf5")),
            ("PADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(Spacer(1, 3))
        story.append(validity)

        doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
        run_query("UPDATE gateway_passes SET status='Generated', next_role=NULL, generated_at=?, generated_file_path=?, updated_at=? WHERE id=?", (now_iso(), str(path), now_iso(), gateway_pass_id))
        log_gateway_event(gateway_pass_id, "Gateway Pass Generated", "Generated", str(path))
        return str(path)
    except Exception as exc:
        st.error(f"Could not generate PDF gateway pass: {exc}")
        return None


def gateway_pass_download_button(row: pd.Series):
    path = row.get("generated_file_path") if hasattr(row, "get") else row["generated_file_path"]
    if not path:
        st.info("Generate the approved gateway pass before downloading.")
        return
    p = Path(path)
    if not p.exists():
        st.warning("Generated file path was recorded but the file is missing. Generate again.")
        return
    data = p.read_bytes()
    mime = "application/pdf" if p.suffix.lower() == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    label = "Download Final Gateway Pass PDF" if p.suffix.lower() == ".pdf" else "Download Generated Gateway Pass"
    if st.download_button(label, data=data, file_name=p.name, mime=mime, key=f"download_gp_{int(row['id'])}"):
        run_query("UPDATE gateway_passes SET downloaded_at=?, status='Downloaded', updated_at=? WHERE id=?", (now_iso(), now_iso(), int(row["id"])))
        log_gateway_event(int(row["id"]), "Gateway Pass Downloaded", "Downloaded", p.name)


def gateway_pass_register(where_sql: str, params: tuple | list, title: str, allow_submit: bool = False, allow_generate: bool = False, key_prefix: str = "gp_register"):
    st.subheader(title)
    df = gateway_pass_summary_df(where_sql, params)
    if df.empty:
        empty_state("No gateway passes", "Gateway pass records will appear here.")
        return
    show_cols = ["id", "pass_number", "facility_manager", "department", "movement_type", "destination", "expected_movement_date", "status", "approved_by", "updated_at"]
    show = df[[c for c in show_cols if c in df.columns]].copy()
    dataframe(show.drop(columns=["id"]) if "id" in show.columns else show)
    selected = st.selectbox("Open gateway pass", [f"{r.pass_number} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key=f"{key_prefix}_select")
    gp_id = int(selected.rsplit("#", 1)[1])
    row = df[df["id"] == gp_id].iloc[0]
    gateway_pass_detail(gp_id)
    events = df_query("SELECT event, status, note, user_id, created_at FROM gateway_pass_events WHERE gateway_pass_id=? ORDER BY created_at DESC", (gp_id,))
    with st.expander("Gateway pass history", expanded=False):
        dataframe(events) if not events.empty else st.info("No events yet.")
    if allow_submit and row["status"] in ["Draft", "Returned for Correction"]:
        if st.button("Submit Gateway Pass for Approval", type="primary", key=f"{key_prefix}_submit_{gp_id}"):
            submit_gateway_pass(gp_id)
    if allow_generate:
        ready = row["status"] in ["Approved", "Generated", "Downloaded"]
        if ready:
            render_gateway_pass_preview(gp_id)
        else:
            st.info("The final company-format preview and Generate button unlock after approval by Procurement Manager, Approver/MD, or Admin.")
        if st.button("Generate Final Gateway Pass PDF", type="primary", key=f"{key_prefix}_generate_{gp_id}", disabled=not ready):
            path = generate_gateway_pass_document(gp_id)
            if path:
                st.success("Gateway pass PDF generated. Review the preview above, then download the final PDF below.")
                st.rerun()
        if not ready:
            st.caption("Generate is disabled until the gateway pass is approved by Procurement Manager, Approver/MD, or Admin.")
        refreshed = gateway_pass_summary_df("gp.id=?", (gp_id,)).iloc[0]
        if refreshed["status"] in ["Generated", "Downloaded"] or refreshed.get("generated_file_path"):
            st.markdown("#### Download")
            st.caption("Preview is shown before download so the Facility Manager can verify company details, logos, quality, quantity and fragile/non-fragile status.")
            gateway_pass_download_button(refreshed)


def facility_gateway_pass_page():
    _phase2_bootstrap()
    st.subheader("Gateway Pass")
    st.caption("Create, submit, generate and download passes for items moved out, moved in, transferred, returned or released. Final generation is disabled until approval.")
    fm_id = int(user()["id"])
    counts = {
        "Draft Gateway Passes": int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status='Draft'", (fm_id,)).iloc[0, 0]),
        "Submitted Gateway Passes": int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status IN ('Submitted','Pending Procurement Manager / Approver Review')", (fm_id,)).iloc[0, 0]),
        "Approved Gateway Passes": int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status='Approved'", (fm_id,)).iloc[0, 0]),
        "Returned Gateway Passes": int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status='Returned for Correction'", (fm_id,)).iloc[0, 0]),
        "Ready to Generate": int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status='Approved'", (fm_id,)).iloc[0, 0]),
    }
    metric_row([(k, v, None) for k, v in counts.items()], cols=5)
    gp_sections = ["Dashboard", "Create Draft", "My Drafts", "Submitted", "Approved / Download", "Returned / Rejected", "History"]
    section = st.radio("Gateway Pass Sections", gp_sections, horizontal=True, key="fm_gp_local_section")

    # Use radio-driven sections instead of st.tabs. Streamlit renders every
    # st.tabs body on every click, which made Gateway Pass navigation slow.
    # This pattern renders only the selected section and opens on the first click.
    if section == "Dashboard":
        if st.button("Create Gateway Pass", type="primary", key="fm_gp_big_create"):
            st.session_state["fm_gp_local_section"] = "Create Draft"
            st.rerun()
        recent = gateway_pass_summary_df("gp.facility_manager_user_id=?", (fm_id,)).head(15)
        dataframe(recent[["pass_number", "department", "movement_type", "destination", "status", "updated_at"]]) if not recent.empty else st.info("No gateway pass records yet.")
    elif section == "Create Draft":
        create_gateway_pass_form()
    elif section == "My Drafts":
        gateway_pass_register("gp.facility_manager_user_id=? AND gp.status IN ('Draft','Returned for Correction','Returned')", (fm_id,), "My Gateway Pass Drafts", allow_submit=True, key_prefix="fm_gp_drafts")
    elif section == "Submitted":
        gateway_pass_register("gp.facility_manager_user_id=? AND gp.status IN ('Submitted','Pending Procurement Manager / Approver Review')", (fm_id,), "Submitted Gateway Passes", key_prefix="fm_gp_submitted")
    elif section == "Approved / Download":
        gateway_pass_register("gp.facility_manager_user_id=? AND gp.status IN ('Approved','Generated','Downloaded')", (fm_id,), "Approved Gateway Passes", allow_generate=True, key_prefix="fm_gp_approved")
    elif section == "Returned / Rejected":
        gateway_pass_register("gp.facility_manager_user_id=? AND gp.status IN ('Rejected','Returned for Correction','Cancelled')", (fm_id,), "Rejected / Returned Gateway Passes", allow_submit=True, key_prefix="fm_gp_returned")
    elif section == "History":
        gateway_pass_register("gp.facility_manager_user_id=?", (fm_id,), "Gateway Pass History", allow_generate=True, key_prefix="fm_gp_history")


def gateway_pass_review_queue(title: str, admin_mode: bool = False):
    _phase2_bootstrap()
    st.subheader(title)
    df = gateway_pass_summary_df("gp.status IN ('Submitted','Pending Procurement Manager / Approver Review')", ())
    if df.empty:
        st.success("No gateway passes are awaiting review.")
        return
    show = df[["id", "pass_number", "facility_manager", "department", "movement_type", "destination", "expected_movement_date", "status", "submitted_at"]].copy()
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open gateway pass for review", [f"{r.pass_number} — {r.facility_manager} — #{int(r.id)}" for r in df.itertuples()], key=f"gp_review_select_{user()['role']}_{admin_mode}")
    gp_id = int(selected.rsplit("#", 1)[1])
    gateway_pass_detail(gp_id)
    note = st.text_area("Review note / reason", key=f"gp_review_note_{gp_id}_{user()['role']}_{admin_mode}")
    c1, c2, c3 = st.columns(3)
    if c1.button("Approve Gateway Pass", type="primary", key=f"gp_approve_{gp_id}_{user()['role']}"):
        _gateway_approve(gp_id, "Approved", note or "Approved.")
    if c2.button("Return for Correction", key=f"gp_return_{gp_id}_{user()['role']}"):
        _gateway_approve(gp_id, "Returned for Correction", note)
    if c3.button("Reject Gateway Pass", key=f"gp_reject_{gp_id}_{user()['role']}"):
        _gateway_approve(gp_id, "Rejected", note)
    reviewed = gateway_pass_summary_df("gp.approved_by_user_id=?", (user()["id"],))
    with st.expander("My reviewed gateway passes", expanded=False):
        dataframe(reviewed[["pass_number", "status", "approval_note", "approved_at", "updated_at"]]) if not reviewed.empty else st.info("No reviewed gateway passes yet.")


def gateway_pass_management_page():
    _phase2_bootstrap()
    st.subheader("Gateway Pass Management")
    metric_row([
        ("Pending gateway pass approvals", int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Submitted','Pending Procurement Manager / Approver Review')").iloc[0, 0]), None),
        ("Gateway pass activity this month", int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE substr(created_at,1,7)=?", (month_key(),)).iloc[0, 0]), None),
        ("Fragile items moved", int(df_query("SELECT COUNT(*) c FROM gateway_pass_items WHERE fragility_status='Fragile'").iloc[0, 0]), None),
        ("Cancelled/rejected passes", int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Cancelled','Rejected')").iloc[0, 0]), None),
    ], cols=4)
    admin_gp_sections = ["Awaiting Approval", "All Gateway Passes", "Admin Actions", "History"]
    section = st.radio("Gateway Pass Management Sections", admin_gp_sections, horizontal=True, key="admin_gp_local_section")

    if section == "Awaiting Approval":
        gateway_pass_review_queue("Gateway Pass Oversight / Approval", admin_mode=True)
    elif section == "All Gateway Passes":
        gateway_pass_register("1=1", (), "All Gateway Passes", allow_generate=False, key_prefix="admin_gp_all")
    elif section == "Admin Actions":
        df = gateway_pass_summary_df("1=1", ())
        if df.empty:
            st.info("No gateway pass records.")
        else:
            selected = st.selectbox("Select pass for admin action", [f"{r.pass_number} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="admin_gp_action_select")
            gp_id = int(selected.rsplit("#", 1)[1])
            gateway_pass_detail(gp_id)
            reason = st.text_area("Mandatory admin reason", key=f"admin_gp_reason_{gp_id}")
            c1, c2 = st.columns(2)
            if c1.button("Cancel Incorrect Pass", key=f"admin_gp_cancel_{gp_id}"):
                _gateway_cancel_or_reopen(gp_id, "Cancel", reason)
            if c2.button("Reopen / Return for Correction", key=f"admin_gp_reopen_{gp_id}"):
                _gateway_cancel_or_reopen(gp_id, "Reopen", reason)
    elif section == "History":
        events = df_query("""
            SELECT ge.*, gp.pass_number, u.full_name user_name
            FROM gateway_pass_events ge
            LEFT JOIN gateway_passes gp ON gp.id=ge.gateway_pass_id
            LEFT JOIN users u ON u.id=ge.user_id
            ORDER BY ge.created_at DESC LIMIT 1000
        """)
        dataframe(events) if not events.empty else st.info("No gateway pass events yet.")
        csv_download(events, "gateway_pass_events")


def gateway_pass_audit_page():
    _phase2_bootstrap()
    st.subheader("Gateway Pass Audit")
    c1, c2, c3, c4 = st.columns(4)
    start = c1.date_input("From", date.today() - timedelta(days=30), key="gp_audit_from")
    end = c2.date_input("To", date.today(), key="gp_audit_to")
    statuses = ["All"] + GATEWAY_PASS_STATUSES
    status = c3.selectbox("Status", statuses, key="gp_audit_status")
    movement = c4.selectbox("Movement type", ["All"] + GATEWAY_MOVEMENT_TYPES, key="gp_audit_movement")
    c5, c6, c7, c8 = st.columns(4)
    fm_users = df_query("SELECT id, full_name FROM users WHERE role='Facility Manager' ORDER BY full_name")
    fm_label = c5.selectbox("Facility Manager", ["All"] + [f"{r.full_name} #{int(r.id)}" for r in fm_users.itertuples()], key="gp_audit_fm")
    approvers = df_query("SELECT DISTINCT u.id, u.full_name FROM gateway_passes gp JOIN users u ON u.id=gp.approved_by_user_id ORDER BY u.full_name")
    approver_label = c6.selectbox("Approved by", ["All"] + [f"{r.full_name} #{int(r.id)}" for r in approvers.itertuples()], key="gp_audit_approver")
    dept = c7.selectbox("Department", ["All"] + department_options(), key="gp_audit_dept")
    fragile_only = c8.checkbox("Fragile items only", key="gp_audit_fragile")
    where = ["date(substr(gp.created_at,1,10)) BETWEEN date(?) AND date(?)"]
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if status != "All": where.append("gp.status=?"); params.append(status)
    if movement != "All": where.append("gp.movement_type=?"); params.append(movement)
    if fm_label != "All": where.append("gp.facility_manager_user_id=?"); params.append(int(fm_label.rsplit("#",1)[1]))
    if approver_label != "All": where.append("gp.approved_by_user_id=?"); params.append(int(approver_label.rsplit("#",1)[1]))
    if dept != "All": where.append("gp.department=?"); params.append(dept)
    if fragile_only:
        where.append("EXISTS (SELECT 1 FROM gateway_pass_items gpi WHERE gpi.gateway_pass_id=gp.id AND gpi.fragility_status='Fragile')")
    df = gateway_pass_summary_df(" AND ".join(where), params)
    metric_row([
        ("Gateway pass audit count", len(df), None),
        ("Approved passes", int((df["status"] == "Approved").sum()) if not df.empty else 0, None),
        ("Generated passes", int(df["status"].isin(["Generated", "Downloaded"]).sum()) if not df.empty else 0, None),
        ("Fragile item movements", int(df_query("SELECT COUNT(DISTINCT gateway_pass_id) c FROM gateway_pass_items WHERE fragility_status='Fragile'").iloc[0,0]), None),
    ], cols=4)
    if df.empty:
        st.info("No gateway pass records match the selected filters.")
        return
    dataframe(df[["pass_number", "facility_manager", "department", "movement_type", "destination", "status", "approved_by", "approved_by_role", "approved_at", "generated_at", "downloaded_at", "created_at"]])
    csv_download(df, "gateway_pass_audit_report")
    selected = st.selectbox("Open audit record", [f"{r.pass_number} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="gp_audit_select")
    gp_id = int(selected.rsplit("#",1)[1])
    gateway_pass_detail(gp_id)
    events = df_query("SELECT * FROM gateway_pass_events WHERE gateway_pass_id=? ORDER BY created_at DESC", (gp_id,))
    approvals = df_query("SELECT * FROM gateway_pass_approvals WHERE gateway_pass_id=? ORDER BY created_at DESC", (gp_id,))
    st.markdown("#### Lifecycle events")
    dataframe(events)
    st.markdown("#### Approval decisions")
    dataframe(approvals)


def admin_phase2_alerts():
    pending_away = int(df_query("SELECT COUNT(*) c FROM user_availability WHERE admin_review_status='Pending Review'").iloc[0, 0])
    active_deleg = int(df_query("SELECT COUNT(*) c FROM approval_delegations WHERE enabled=1").iloc[0, 0])
    ending_soon = int(df_query("SELECT COUNT(*) c FROM approval_delegations WHERE enabled=1 AND end_date IS NOT NULL AND date(end_date) <= date('now','+3 day')").iloc[0, 0])
    users_away = int(df_query("SELECT COUNT(*) c FROM user_availability WHERE status IN ('Away Approved','Away Active') AND date(away_start_date) <= date('now') AND date(away_end_date) >= date('now')").iloc[0, 0])
    affected_queue = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval')").iloc[0, 0])
    pending_gp = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Submitted','Pending Procurement Manager / Approver Review')").iloc[0, 0])
    if pending_away or active_deleg or pending_gp or users_away or ending_soon:
        st.markdown("### What needs Admin attention?")
        metric_row([
            ("Pending away notices", pending_away, "availability"),
            ("Active delegations", active_deleg, "approval continuity"),
            ("Delegations ending soon", ending_soon, "next 3 days"),
            ("Users currently away", users_away, "active period"),
            ("Approval queues affected", affected_queue, "pending approvals"),
            ("Gateway Pass Oversight", pending_gp, "awaiting review"),
        ], cols=3)


def all_records_page():
    _phase2_bootstrap()
    tables = [
        "users", "roles", "permissions", "role_permissions", "purchase_requests", "purchase_request_items",
        "sourcing_tasks", "vendor_quotes", "purchase_orders", "purchase_order_items", "receiving_slips",
        "invoices", "expenses", "payments", "cash_advances", "vendors", "imported_legacy_documents",
        "annual_budgets", "budgets", "budget_adjustments", "budget_history", "approval_rules",
        "approval_delegations", "approval_history", "facility_manager_links", "collaboration_threads",
        "collaboration_messages", "activity_logs", "workflow_events", "notifications", "notification_preferences",
        "push_subscriptions", "notification_outbox", "user_availability", "gateway_passes", "gateway_pass_items",
        "gateway_pass_approvals", "gateway_pass_events", "audit_logs",
    ]
    tables = [t for t in tables if _table_exists_local(t)]
    st.subheader("All Procurement Records")
    table = st.selectbox("Record table", tables, key="all_records_table_phase2")
    df = _safe_table_df(table, 1000)
    dataframe(df)
    csv_download(df, table)


def notifications_monitor_page():
    _phase2_bootstrap()
    st.subheader("Notifications Monitor")
    df = df_query("SELECT n.*, u.username, u.full_name FROM notifications n LEFT JOIN users u ON n.user_id=u.id ORDER BY n.created_at DESC LIMIT 500")
    dataframe(df) if not df.empty else st.info("No notifications yet.")
    csv_download(df, "notifications")
    st.markdown("#### External notification outbox")
    outbox = _safe_table_df("notification_outbox", 500)
    dataframe(outbox) if not outbox.empty else st.info("No external notification outbox items yet.")
    st.markdown("#### User notification preferences")
    prefs = df_query("SELECT np.*, u.username, u.full_name, u.role FROM notification_preferences np LEFT JOIN users u ON u.id=np.user_id ORDER BY u.role, u.username")
    dataframe(prefs) if not prefs.empty else st.info("No preferences yet.")


def admin_console():
    role_header("Admin Console", "Highest-authority workspace for users, budgets, workflow rules, imports, audit control, availability, notifications and gateway passes.")
    section = st.session_state.get("admin_section", "Admin Dashboard")
    if section == "Admin Dashboard":
        admin_metrics(); admin_phase2_alerts(); admin_overview()
    elif section == "Budget Tracker":
        budget_command_center()
    elif section == "User Management":
        user_management()
    elif section == "Roles & Permissions":
        roles_permissions_page()
    elif section == "Approval Configuration":
        approval_configuration_page()
    elif section == "Import Center":
        import_center()
    elif section == "All Procurement Records":
        all_records_page()
    elif section == "Notifications Monitor":
        notifications_monitor_page()
    elif section == "Availability & Delegation Requests":
        availability_delegation_requests_page()
    elif section == "Gateway Pass Management":
        gateway_pass_management_page()
    elif section == "Activity & History Logs":
        activity_history_page(scope="admin")
    elif section == "Audit Logs":
        audit_log_page(full=True)
    elif section == "Backup / Export":
        backup_export_page()
    elif section == "Settings":
        settings_page()
    else:
        admin_metrics(); admin_phase2_alerts(); admin_overview()


def procurement_dashboard():
    st.subheader("What needs my attention?")
    c1, c2, c3 = st.columns(3)
    fm_inbox = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status='Submitted to Procurement Manager'").iloc[0, 0])
    gp_waiting = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Submitted','Pending Procurement Manager / Approver Review')").iloc[0, 0])
    away = df_query("SELECT * FROM user_availability WHERE user_id=? AND status NOT IN ('Returned','Cancelled') ORDER BY created_at DESC LIMIT 1", (user()["id"],))
    c1.metric("Facility Manager Inbox", fm_inbox)
    c2.metric("Gateway Passes Awaiting Review", gp_waiting)
    c3.metric("Availability", "Away/Delegated" if not away.empty else "Available")
    if gp_waiting:
        st.warning(f"{gp_waiting} gateway pass(es) are awaiting review.")
    df = df_query("SELECT request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE status NOT IN ('Closed','Rejected','Paid') ORDER BY updated_at DESC LIMIT 20")
    if not df.empty:
        df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
    else:
        st.success("No open procurement requests.")


def procurement_workspace():
    role_header("Procurement Manager Workspace", "Operational command center for procurement, Facility Manager handoffs, sourcing, POs, delegated approvals, away notices and gateway pass reviews.")
    section = st.session_state.get("procurement_section", "Operations Dashboard")
    if section == "Operations Dashboard":
        procurement_dashboard_metrics(); procurement_dashboard()
    elif section == "Purchase Requests":
        requests_page(mode="procurement")
    elif section == "Facility Manager Inbox":
        facility_manager_inbox()
    elif section == "Import Center":
        import_center()
    elif section == "Sourcing":
        sourcing_page()
    elif section == "Vendor Quotes":
        quote_page()
    elif section == "Vendor Recommendation":
        sourcing_page()
    elif section == "Purchase Orders":
        purchase_orders_page()
    elif section == "Receiving Slips":
        receiving_page()
    elif section == "Vendors":
        vendors_page()
    elif section == "Gateway Pass Review":
        gateway_pass_review_queue("Gateway Pass Review")
    elif section == "Acting Approval Queue":
        acting_approval_queue()
    elif section == "Availability / Away Notice":
        availability_panel()
    elif section == "Procurement Documents":
        document_archive(editable=True)
    elif section == "Procurement Reports":
        procurement_reports()
    elif section == "My Activity History":
        activity_history_page(scope="mine")
    elif section == "Settings":
        settings_page()
    else:
        procurement_dashboard_metrics(); procurement_dashboard()


def facility_dashboard():
    fm_id = user()["id"]
    pm_id = get_pm_for_facility_manager(fm_id)
    pm = df_query("SELECT full_name FROM users WHERE id=?", (pm_id,)) if pm_id else pd.DataFrame()
    st.info(f"Assigned Procurement Manager: {pm.iloc[0]['full_name'] if not pm.empty else 'Not assigned yet'}")
    q = lambda status: int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status=?", (fm_id, status)).iloc[0,0])
    gp_ready = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status='Approved'", (fm_id,)).iloc[0,0])
    gp_returned = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status='Returned for Correction'", (fm_id,)).iloc[0,0])
    metric_row([
        ("FM Drafts", q("FM Draft"), None),
        ("Submitted", q("Submitted to Procurement Manager"), None),
        ("Returned", q("Returned to Facility Manager"), None),
        ("Accepted", q("Accepted by Procurement Manager"), None),
        ("Gateway Ready to Generate", gp_ready, None),
        ("Gateway Returned", gp_returned, None),
    ], cols=3)
    st.subheader("What needs my attention?")
    if gp_ready:
        st.success(f"{gp_ready} approved gateway pass(es) are ready to generate/download.")
    if gp_returned:
        st.warning(f"{gp_returned} gateway pass(es) were returned for correction.")
    df = df_query("""
        SELECT request_no, department_project, category, estimated_amount, status, updated_at
        FROM purchase_requests
        WHERE facility_manager_user_id=? AND status IN ('FM Draft','Returned to Facility Manager','Submitted to Procurement Manager','Accepted by Procurement Manager')
        ORDER BY updated_at DESC LIMIT 20
    """, (fm_id,))
    if not df.empty:
        df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
    else:
        st.success("No Facility Manager procurement actions pending.")
    recent_gp = gateway_pass_summary_df("gp.facility_manager_user_id=?", (fm_id,)).head(10)
    st.markdown("#### Recent Gateway Passes")
    dataframe(recent_gp[["pass_number", "movement_type", "destination", "status", "updated_at"]]) if not recent_gp.empty else st.info("No gateway pass records yet.")


def facility_workspace():
    role_header("Facility Manager Workspace", "Create draft requests, import supporting documents, submit to Procurement Manager, collaborate privately, and manage gateway passes.")
    section = st.session_state.get("facility_section", "Facility Dashboard")
    if section == "Facility Dashboard":
        facility_dashboard()
    elif section == "Create Request Draft":
        create_fm_draft_form()
    elif section == "My Draft Requests":
        facility_draft_register(status_filter=None)
    elif section == "Submit to Procurement Manager":
        facility_draft_register(status_filter=["FM Draft", "Returned to Facility Manager"])
    elif section == "Import Documents":
        facility_import_documents()
    elif section == "Gateway Pass":
        facility_gateway_pass_page()
    elif section == "Shared Thread with Procurement Manager":
        facility_shared_threads()
    elif section == "Returned Requests":
        facility_draft_register(status_filter=["Returned to Facility Manager"])
    elif section == "Approved / Accepted Requests":
        facility_draft_register(status_filter=["Accepted by Procurement Manager", "Converted to Purchase Request", "Approved", "Paid", "Closed"])
    elif section == "My Activity History":
        activity_history_page(scope="mine")
    elif section == "Settings":
        settings_page()
    else:
        facility_dashboard()


def executive_dashboard():
    st.subheader("What needs my attention?")
    pending = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval')").iloc[0, 0])
    gp_waiting = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Submitted','Pending Procurement Manager / Approver Review')").iloc[0, 0])
    away = df_query("SELECT * FROM user_availability WHERE user_id=? AND status NOT IN ('Returned','Cancelled') ORDER BY created_at DESC LIMIT 1", (user()["id"],))
    metric_row([
        ("Pending approvals", pending, None),
        ("Gateway Passes Awaiting Review", gp_waiting, None),
        ("Availability", "Away/Delegated" if not away.empty else "Available", None),
    ], cols=3)
    if gp_waiting:
        st.warning(f"{gp_waiting} gateway pass(es) require Procurement Manager or Approver/MD review.")
    df = df_query("SELECT request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval') ORDER BY estimated_amount DESC LIMIT 20")
    if not df.empty:
        df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
    else:
        st.success("No pending request approvals.")


def executive_workspace():
    role_header("Approver Workspace", "Simple decision workspace for requests, POs, payments, availability continuity, and gateway pass approvals.")
    section = st.session_state.get("executive_section", "Approval Dashboard")
    if section == "Approval Dashboard":
        executive_metrics(); executive_dashboard()
    elif section == "Pending Approvals":
        pending_approval_page()
    elif section == "Quote Comparison":
        quote_comparison_decision_page()
    elif section == "PO Approval":
        po_approval_page()
    elif section == "Payment Approval":
        payment_approval_page()
    elif section == "Gateway Pass Approval":
        gateway_pass_review_queue("Gateway Pass Approval")
    elif section == "Availability / Away Notice":
        availability_panel()
    elif section == "My Approval History":
        my_approval_history_page()
    elif section == "Settings":
        settings_page()
    else:
        executive_metrics(); executive_dashboard()


def audit_dashboard():
    st.subheader("Compliance Snapshot")
    recent_notifs = df_query("SELECT title, message, entity_type, entity_id, created_at FROM notifications WHERE role='Auditor' OR user_id=? ORDER BY created_at DESC LIMIT 25", (user()["id"],))
    if not recent_notifs.empty:
        st.markdown("#### Recent activity notifications")
        dataframe(recent_notifs)
    metric_row([
        ("Gateway pass audit count", int(df_query("SELECT COUNT(*) c FROM gateway_passes").iloc[0,0]), None),
        ("Approved passes", int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status='Approved'").iloc[0,0]), None),
        ("Generated passes", int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Generated','Downloaded')").iloc[0,0]), None),
        ("Fragile item movements", int(df_query("SELECT COUNT(DISTINCT gateway_pass_id) c FROM gateway_pass_items WHERE fragility_status='Fragile'").iloc[0,0]), None),
    ], cols=4)
    logs = df_query("SELECT created_at, action, entity_type, entity_id, details FROM audit_logs ORDER BY created_at DESC LIMIT 20")
    dataframe(logs) if not logs.empty else st.info("No audit logs yet.")


def audit_workspace():
    role_header("Audit & Compliance Workspace", "Read-only review of lifecycles, approvals, delegated approvals, budgets, imports, handoffs, gateway passes, vendors and finance status changes.")
    section = st.session_state.get("audit_section", "Audit Dashboard")
    if section == "Audit Dashboard":
        audit_metrics(); audit_dashboard()
    elif section == "Procurement Records":
        all_records_page()
    elif section == "Document Archive":
        document_archive(editable=False)
    elif section == "Approval Trails":
        approval_trails_page()
    elif section == "Delegated Approval Review":
        delegated_approval_review_page()
    elif section == "Budget Audit":
        budget_audit_page()
    elif section == "Facility Manager Handoff Trail":
        facility_handoff_trail_page()
    elif section == "Gateway Pass Audit":
        gateway_pass_audit_page()
    elif section == "Vendor History":
        vendor_history_page()
    elif section == "Expense Review":
        expense_review_page()
    elif section == "Compliance Reports":
        compliance_reports()
    elif section == "Settings":
        settings_page()
    else:
        audit_metrics(); audit_dashboard()

# ============================================================================
# Phase 3 hardening overrides: safe audit views, gateway ownership, separated receipts/invoices
# ============================================================================

def _ensure_finance_doc_schema_ui():
    from core.db import ensure_finance_document_schema, ensure_hardening_schema
    ensure_hardening_schema()
    ensure_finance_document_schema()


def _redact_ui_df(df: pd.DataFrame, table: str | None = None) -> pd.DataFrame:
    try:
        from core.db import redact_dataframe
        return redact_dataframe(df, table)
    except Exception:
        if df is None or df.empty:
            return df
        safe = df.copy()
        for col in safe.columns:
            if col in {"password_hash", "endpoint", "p256dh_key", "auth_key", "file_hash", "receipt_hash"}:
                safe[col] = "[hidden]"
            if col in {"message_text", "private_details"}:
                safe[col] = "[private]"
        return safe


def all_records_page():
    _phase2_bootstrap()
    _ensure_finance_doc_schema_ui()
    tables = [
        "users", "roles", "permissions", "role_permissions", "purchase_requests", "purchase_request_items",
        "sourcing_tasks", "vendor_quotes", "purchase_orders", "purchase_order_items", "receiving_slips",
        "invoices", "invoice_items", "receipt_records", "receipt_items", "expenses", "payments", "cash_advances", "vendors",
        "imported_legacy_documents", "annual_budgets", "budgets", "budget_adjustments", "budget_history", "approval_rules",
        "approval_delegations", "approval_history", "facility_manager_links", "collaboration_threads", "collaboration_messages",
        "activity_logs", "workflow_events", "notifications", "notification_preferences", "push_subscriptions", "notification_outbox",
        "user_availability", "gateway_passes", "gateway_pass_items", "gateway_pass_approvals", "gateway_pass_events", "audit_logs",
    ]
    tables = [t for t in tables if _table_exists_local(t)]
    st.subheader("All Procurement Records")
    st.caption("Sensitive values such as password hashes, push keys, file hashes and private message text are hidden in the UI/export.")
    table = st.selectbox("Record table", tables, key="all_records_table_phase3")
    df = _safe_table_df(table, 1000)
    safe = _redact_ui_df(df, table)
    dataframe(safe)
    csv_download(safe, table)


def auditor_records_page():
    _phase2_bootstrap()
    _ensure_finance_doc_schema_ui()
    st.subheader("Procurement Records — Auditor Safe View")
    st.caption("Read-only compliance views. Private message content, passwords, push keys and internal user secrets are intentionally not shown.")
    views = {
        "Purchase Requests": """
            SELECT pr.request_no, requester.full_name requester, pr.department_project, pr.category, pr.estimated_amount,
                   pr.status, pr.payment_status, pr.created_at, pr.updated_at
            FROM purchase_requests pr LEFT JOIN users requester ON requester.id=pr.requested_by
            ORDER BY pr.updated_at DESC LIMIT 1000
        """,
        "Purchase Orders": """
            SELECT po.po_no, pr.request_no, v.name vendor, po.total_amount, po.status, po.payment_status,
                   po.receiving_status, po.created_at, po.updated_at
            FROM purchase_orders po
            LEFT JOIN purchase_requests pr ON pr.id=po.request_id
            LEFT JOIN vendors v ON v.id=po.vendor_id
            ORDER BY po.updated_at DESC LIMIT 1000
        """,
        "Invoices": """
            SELECT inv.invoice_no, inv.invoice_type, po.po_no, v.name vendor, inv.invoice_date, inv.due_date,
                   inv.total_amount, inv.balance_due, inv.match_status, inv.status, inv.created_at
            FROM invoices inv
            LEFT JOIN purchase_orders po ON po.id=inv.po_id
            LEFT JOIN vendors v ON v.id=inv.vendor_id
            ORDER BY inv.created_at DESC LIMIT 1000
        """,
        "Receipts": """
            SELECT rr.receipt_no, rr.receipt_type, rr.payment_method, rr.payment_date, v.name vendor,
                   rr.amount, rr.status, rr.department_project, rr.created_at
            FROM receipt_records rr LEFT JOIN vendors v ON v.id=rr.vendor_id
            ORDER BY rr.created_at DESC LIMIT 1000
        """,
        "Gateway Passes": """
            SELECT gp.pass_number, fm.full_name facility_manager, gp.department, gp.movement_type, gp.destination,
                   gp.status, gp.approved_by_role, approver.full_name approved_by, gp.approved_at, gp.generated_at, gp.downloaded_at
            FROM gateway_passes gp
            LEFT JOIN users fm ON fm.id=gp.facility_manager_user_id
            LEFT JOIN users approver ON approver.id=gp.approved_by_user_id
            ORDER BY gp.updated_at DESC LIMIT 1000
        """,
        "Private Handoff Metadata": """
            SELECT ct.entity_type, ct.entity_id, fm.full_name facility_manager, pm.full_name procurement_manager,
                   COUNT(cm.id) message_count, MAX(cm.created_at) last_message_at
            FROM collaboration_threads ct
            LEFT JOIN collaboration_messages cm ON cm.thread_id=ct.id
            LEFT JOIN users fm ON fm.id=ct.facility_manager_user_id
            LEFT JOIN users pm ON pm.id=ct.procurement_manager_user_id
            GROUP BY ct.id ORDER BY last_message_at DESC LIMIT 1000
        """,
        "Audit Logs": "SELECT created_at, action, entity_type, entity_id, user_id, role, details FROM audit_logs ORDER BY created_at DESC LIMIT 1000",
    }
    label = st.selectbox("Compliance view", list(views.keys()), key="auditor_safe_view")
    df = df_query(views[label])
    if "amount" in df.columns:
        df["amount"] = df["amount"].apply(money)
    if "estimated_amount" in df.columns:
        df["estimated_amount"] = df["estimated_amount"].apply(money)
    if "total_amount" in df.columns:
        df["total_amount"] = df["total_amount"].apply(money)
    dataframe(_redact_ui_df(df, None)) if not df.empty else st.info("No records in this compliance view.")
    csv_download(_redact_ui_df(df, None), f"auditor_{label.lower().replace(' ', '_')}")


def record_collaboration(entity_type: str, entity_id: int, key_scope: str | None = None):
    safe_entity = entity_type.lower().replace(" ", "_").replace("/", "_")
    if key_scope is None:
        st.session_state["_collab_render_seq"] = st.session_state.get("_collab_render_seq", 0) + 1
        key_scope = f"auto_{st.session_state['_collab_render_seq']}"
    scope = key_scope.lower().replace(" ", "_").replace("/", "_")
    form_key = f"comment_{scope}_{safe_entity}_{entity_id}"
    with st.expander("Timeline, Comments & Internal Notes"):
        events = df_query("SELECT we.created_at, we.event, we.status, u.full_name user, we.note FROM workflow_events we LEFT JOIN users u ON we.user_id=u.id WHERE entity_type=? AND entity_id=? ORDER BY we.created_at", (entity_type, entity_id))
        dataframe(events) if not events.empty else st.info("No workflow events yet.")
        comments = df_query("SELECT c.created_at, u.full_name user, c.comment_text, c.is_internal FROM comments c LEFT JOIN users u ON c.user_id=u.id WHERE entity_type=? AND entity_id=? ORDER BY c.created_at DESC", (entity_type, entity_id))
        dataframe(comments) if not comments.empty else st.info("No comments yet.")
        if user()["role"] == "Auditor":
            st.caption("Auditor access is read-only. Private comments are reviewed through metadata and official audit trails only.")
            return
        with st.form(form_key):
            text = st.text_area("Add comment or internal note", key=f"{form_key}_text")
            internal = st.checkbox("Internal note", key=f"{form_key}_internal")
            submitted = st.form_submit_button("Post")
        if submitted and text:
            run_query("INSERT INTO comments (entity_type, entity_id, comment_text, is_internal, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (entity_type, entity_id, text, int(internal), user()["id"], now_iso()))
            add_workflow_event(entity_type, entity_id, "Comment Added", None, text[:120], user()["id"])
            st.rerun()


RECEIPT_PAYMENT_METHODS = ["Cash", "Bank Transfer", "Card", "POS/Card", "Cheque", "Mobile Money"]
INVOICE_TYPES = ["Supplier Invoice", "Pro Forma Invoice", "Tax Invoice", "Service Invoice", "Recurring Invoice", "Credit Note", "Debit Note"]


def _ocr_upload_panel(upload_key: str, doc_kind: str):
    vendors_df = df_query("SELECT id, name, bank_name, account_no, rating FROM vendors")
    uploaded = st.file_uploader(f"Upload {doc_kind} image/PDF for OCR", type=["png", "jpg", "jpeg", "pdf"], key=upload_key)
    if st.button(f"Extract {doc_kind} OCR", disabled=uploaded is None, key=f"{upload_key}_extract"):
        text, meta, error = extract_text(uploaded)
        parsed = parse_ocr_text(text, vendors_df)
        parsed["file_meta"] = meta
        parsed["error"] = error
        st.session_state[f"{upload_key}_parsed"] = parsed
        if error:
            st.warning(error)
        elif len(text) < 20:
            st.warning("OCR ran but extracted very little text. Use manual entry or upload a clearer scan.")
        else:
            st.success(f"OCR extraction complete: {len(text)} characters detected.")
    parsed = st.session_state.get(f"{upload_key}_parsed", {})
    if parsed:
        fields = parsed.get("fields", {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Detected", fields.get("document_type", "Unknown"))
        c2.metric("Payment", fields.get("payment_method", "Unknown"))
        c3.metric("Amount", money(fields.get("total_amount") or 0))
        c4.metric("OCR chars", parsed.get("file_meta", {}).get("extracted_chars", 0))
        with st.expander("OCR text and extracted fields", expanded=False):
            st.json(parsed)
    return uploaded, parsed


def _save_ocr_attempt(doc_type: str, entity_id: int | None, parsed: dict):
    _ensure_finance_doc_schema_ui()
    meta = parsed.get("file_meta", {}) if parsed else {}
    run_query(
        "INSERT INTO document_ocr_attempts (document_type, entity_id, file_hash, engine, success, extracted_chars, error_message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_type, entity_id, meta.get("file_hash"), meta.get("engine") or meta.get("ocr_engine"), 0 if parsed.get("error") else 1, int(meta.get("extracted_chars") or 0), parsed.get("error"), now_iso()),
    )


def receipts_page():
    _ensure_finance_doc_schema_ui()
    st.subheader("Receipts")
    st.caption("Receipts are proof of payment. Choose the payment method and the form changes to capture the right evidence: cash acknowledgement, transfer reference, card/POS details, cheque, or mobile money.")
    section = st.radio("Receipt sections", ["Record Receipt", "Receipt Register", "OCR Attempts"], horizontal=True, key="receipt_sections")
    if section == "Record Receipt":
        uploaded, parsed = _ocr_upload_panel("receipt_ocr_upload", "receipt")
        fields = parsed.get("fields", {}) if parsed else {}
        receipt_details = parsed.get("receipt_details", {}) if parsed else {}
        bank_details = parsed.get("bank_details", {}) if parsed else {}
        vendors = vendor_options(True)
        vendor_name = fields.get("matched_vendor_name") or "No vendor selected"
        vendor_index = list(vendors.keys()).index(vendor_name) if vendor_name in vendors else 0
        method_default = fields.get("payment_method") if fields.get("payment_method") in RECEIPT_PAYMENT_METHODS else "Cash"
        with st.form("receipt_record_form"):
            c1, c2, c3 = st.columns(3)
            receipt_type = c1.selectbox("Receipt type", ["Payment Receipt", "Cash Receipt", "Transfer Receipt", "Card Receipt", "POS Receipt", "Cheque Receipt", "Mobile Money Receipt", "Refund Receipt"], key="receipt_type")
            payment_method = c2.selectbox("Payment method", RECEIPT_PAYMENT_METHODS, index=RECEIPT_PAYMENT_METHODS.index(method_default), key="receipt_method")
            payment_date = c3.date_input("Payment date", date.today(), key="receipt_payment_date")
            c4, c5, c6 = st.columns(3)
            receipt_no = c4.text_input("Receipt / transaction reference", value=fields.get("receipt_no") or "", key="receipt_no")
            vendor_label = c5.selectbox("Vendor / Payee", list(vendors.keys()), index=vendor_index, key="receipt_vendor")
            amount = c6.number_input("Amount paid", min_value=0.0, value=float(fields.get("total_amount") or 0), step=1000.0, key="receipt_amount")
            c7, c8, c9 = st.columns(3)
            tax = c7.number_input("VAT/Tax included", min_value=0.0, value=float(fields.get("tax_amount") or 0), step=100.0, key="receipt_tax")
            dept = c8.selectbox("Department / Project", department_options(), key="receipt_dept")
            currency = c9.selectbox("Currency", ["NGN", "USD", "EUR", "GBP"], key="receipt_currency")
            purpose = st.text_area("Purpose / what was paid for", value=fields.get("description") or "", key="receipt_purpose")

            method_data = {}
            st.markdown(f"##### {payment_method} receipt details")
            if payment_method == "Cash":
                c1, c2, c3 = st.columns(3)
                method_data["cash_received_by"] = c1.text_input("Cash received by", key="cash_received_by")
                method_data["cash_collected_from"] = c2.text_input("Cash collected from", value=user()["full_name"], key="cash_collected_from")
                method_data["cash_denominations"] = c3.text_input("Denomination breakdown", placeholder="e.g. 10x ₦1,000 + 5x ₦500", key="cash_denominations")
            elif payment_method == "Bank Transfer":
                c1, c2, c3 = st.columns(3)
                method_data["bank_name"] = c1.text_input("Receiving bank", value=bank_details.get("bank_name") or "", key="transfer_bank")
                method_data["account_number"] = c2.text_input("Receiving account number", value=bank_details.get("account_no") or "", key="transfer_acct")
                method_data["transfer_reference"] = c3.text_input("Transfer/session reference", value=bank_details.get("transfer_reference") or fields.get("receipt_no") or "", key="transfer_ref")
                c4, c5 = st.columns(2)
                method_data["sender_bank"] = c4.text_input("Sender bank", key="transfer_sender_bank")
                method_data["receiver_bank"] = c5.text_input("Receiver bank", key="transfer_receiver_bank")
            elif payment_method in ["Card", "POS/Card"]:
                c1, c2, c3, c4 = st.columns(4)
                method_data["card_type"] = selectbox_with_other("Card type", ["Unknown", "Visa", "Mastercard", "Verve", "Other"], "card_type", "card_type")
                method_data["masked_card_number"] = c2.text_input("Masked card number", placeholder="**** **** **** 1234", key="masked_card")
                method_data["card_auth_code"] = c3.text_input("Auth/approval code", value=receipt_details.get("auth_code") or "", key="card_auth")
                method_data["pos_rrn"] = c4.text_input("RRN/STAN", value=receipt_details.get("rrn") or "", key="pos_rrn")
                c5, c6 = st.columns(2)
                method_data["pos_terminal_id"] = c5.text_input("POS terminal ID", value=receipt_details.get("terminal_id") or "", key="pos_tid")
                method_data["pos_merchant_id"] = c6.text_input("Merchant ID", key="pos_mid")
            elif payment_method == "Cheque":
                c1, c2, c3 = st.columns(3)
                method_data["cheque_number"] = c1.text_input("Cheque number", key="cheque_number")
                method_data["cheque_bank"] = c2.text_input("Cheque bank", key="cheque_bank")
                method_data["cheque_due_date"] = c3.date_input("Cheque date", date.today(), key="cheque_due_date").isoformat()
            elif payment_method == "Mobile Money":
                c1, c2 = st.columns(2)
                method_data["mobile_wallet_provider"] = c1.text_input("Wallet/provider", placeholder="Opay, Moniepoint, Paga, etc.", key="mobile_provider")
                method_data["mobile_transaction_id"] = c2.text_input("Mobile transaction ID", value=fields.get("receipt_no") or "", key="mobile_txn")
            notes = st.text_area("Finance notes", key="receipt_notes")
            submitted = st.form_submit_button("Save Receipt", type="primary")
        if submitted:
            if amount <= 0:
                st.error("Receipt amount must be greater than zero.")
                return
            path, fhash = save_upload(uploaded, "receipts") if uploaded else (None, None)
            try:
                from core.ocr import duplicate_receipt_candidates
                dup = duplicate_receipt_candidates(fhash, amount, payment_date.isoformat(), vendors[vendor_label])
            except Exception:
                dup = pd.DataFrame()
            rid = run_insert(
                """
                INSERT INTO receipt_records (receipt_no, receipt_type, payment_method, payment_date, vendor_id, payer_name, payee_name, amount, tax_amount, currency, purpose, department_project,
                    cash_received_by, cash_collected_from, cash_denominations, bank_name, account_number, transfer_reference, sender_bank, receiver_bank, card_type, masked_card_number,
                    card_auth_code, pos_terminal_id, pos_merchant_id, pos_rrn, cheque_number, cheque_bank, cheque_due_date, mobile_wallet_provider, mobile_transaction_id,
                    status, file_path, file_hash, ocr_text, ocr_json, duplicate_warning, notes, uploaded_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Recorded', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (receipt_no or make_ref("RCT"), receipt_type, payment_method, payment_date.isoformat(), vendors[vendor_label], user()["full_name"], vendor_label if vendor_label != "No vendor selected" else "", amount, tax, currency, purpose, dept,
                 method_data.get("cash_received_by"), method_data.get("cash_collected_from"), method_data.get("cash_denominations"), method_data.get("bank_name"), method_data.get("account_number"), method_data.get("transfer_reference"), method_data.get("sender_bank"), method_data.get("receiver_bank"), method_data.get("card_type"), method_data.get("masked_card_number"), method_data.get("card_auth_code"), method_data.get("pos_terminal_id"), method_data.get("pos_merchant_id"), method_data.get("pos_rrn"), method_data.get("cheque_number"), method_data.get("cheque_bank"), method_data.get("cheque_due_date"), method_data.get("mobile_wallet_provider"), method_data.get("mobile_transaction_id"), path, fhash, parsed.get("raw_text", "") if parsed else "", json_dump(parsed) if parsed else "{}", 0 if dup.empty else 1, notes, user()["id"], now_iso(), now_iso()),
            )
            _save_ocr_attempt("Receipt", rid, parsed or {})
            # Legacy expenses compatibility for budget and older pages.
            exp_no = make_ref("EXP")
            run_insert("""INSERT INTO expenses (expense_no, expense_date, category, description, vendor_id, amount, payment_method, project_department, status, receipt_path, receipt_hash, receipt_no, tax_amount, duplicate_warning, requested_by, ocr_text, ocr_json, document_kind, receipt_id, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Approved', ?, ?, ?, ?, ?, ?, ?, ?, 'Receipt', ?, ?, ?)""", (exp_no, payment_date.isoformat(), fields.get("category") or "Other", purpose or receipt_type, vendors[vendor_label], amount, payment_method, dept, path, fhash, receipt_no, tax, 0 if dup.empty else 1, user()["id"], parsed.get("raw_text", "") if parsed else "", json_dump(parsed) if parsed else "{}", rid, notes, now_iso()))
            add_workflow_event("Receipt", rid, "Receipt Recorded", "Recorded", f"{payment_method} receipt", user()["id"])
            log_audit("RECEIPT_RECORDED", "Receipt", rid, {"payment_method": payment_method, "amount": amount}, user()["id"], user()["role"])
            st.success(f"Receipt saved: {receipt_no or 'auto reference'}")
            if not dup.empty:
                st.warning("Possible duplicate receipt detected.")
            st.rerun()
    elif section == "Receipt Register":
        df = df_query("""
            SELECT rr.id, rr.receipt_no, rr.receipt_type, rr.payment_method, rr.payment_date, v.name vendor, rr.amount, rr.status, rr.duplicate_warning, rr.department_project, rr.created_at
            FROM receipt_records rr LEFT JOIN vendors v ON v.id=rr.vendor_id ORDER BY rr.created_at DESC
        """)
        if df.empty:
            empty_state("No receipts", "Record payment receipts separately from invoices.")
            return
        show = df.drop(columns=["id"]).copy(); show["amount"] = show["amount"].apply(money)
        dataframe(show)
        selected = st.selectbox("Open receipt", [f"{r.receipt_no} — {r.payment_method} — #{int(r.id)}" for r in df.itertuples()], key="open_receipt")
        rid = int(selected.rsplit("#", 1)[1])
        row = df_query("SELECT rr.*, v.name vendor FROM receipt_records rr LEFT JOIN vendors v ON v.id=rr.vendor_id WHERE rr.id=?", (rid,)).iloc[0]
        st.dataframe(_redact_ui_df(pd.DataFrame([row.to_dict()]), "receipt_records"), use_container_width=True)
        with st.expander("OCR raw text", expanded=False):
            try: st.json(json.loads(row.get("ocr_json") or "{}"))
            except Exception: st.text(row.get("ocr_text") or "")
        csv_download(show, "receipts")
    else:
        attempts = df_query("SELECT * FROM document_ocr_attempts WHERE document_type='Receipt' ORDER BY created_at DESC LIMIT 300")
        dataframe(attempts) if not attempts.empty else st.info("No receipt OCR attempts yet.")


def invoices_page():
    _ensure_finance_doc_schema_ui()
    st.subheader("Invoices")
    st.caption("Invoices are requests for payment before payment is completed. They are now separate from receipts, which are proof of payment after money moves.")
    section = st.radio("Invoice sections", ["Upload / Record Invoice", "Invoice Register", "Invoice Items", "OCR Attempts"], horizontal=True, key="invoice_sections")
    vendors = vendor_options(True)
    if section == "Upload / Record Invoice":
        uploaded, parsed = _ocr_upload_panel("invoice_ocr_upload", "invoice")
        fields = parsed.get("fields", {}) if parsed else {}
        vendor_name = fields.get("matched_vendor_name") or "No vendor selected"
        vendor_index = list(vendors.keys()).index(vendor_name) if vendor_name in vendors else 0
        po_df = df_query("SELECT po.id, po.po_no, v.name vendor, po.total_amount FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id ORDER BY po.created_at DESC")
        po_options = ["No PO selected"] + [f"{r.po_no} — {r.vendor} — {money(r.total_amount)}" for r in po_df.itertuples()]
        with st.form("invoice_record_form"):
            c1, c2, c3 = st.columns(3)
            invoice_type = c1.selectbox("Invoice type", INVOICE_TYPES, key="invoice_type")
            invoice_no = c2.text_input("Invoice number", value=fields.get("invoice_no") or "", key="invoice_number")
            invoice_date = c3.date_input("Invoice date", date.today(), key="invoice_date")
            c4, c5, c6 = st.columns(3)
            due_date = c4.date_input("Due date", date.today() + timedelta(days=7), key="invoice_due_date")
            vendor_label = c5.selectbox("Vendor", list(vendors.keys()), index=vendor_index, key="invoice_vendor")
            po_label = c6.selectbox("Match Purchase Order", po_options, key="invoice_po_match")
            c7, c8, c9, c10 = st.columns(4)
            subtotal = c7.number_input("Subtotal", min_value=0.0, value=float(fields.get("subtotal") or 0), step=1000.0, key="invoice_subtotal")
            tax = c8.number_input("VAT/Tax", min_value=0.0, value=float(fields.get("tax_amount") or 0), step=100.0, key="invoice_tax")
            discount = c9.number_input("Discount", min_value=0.0, value=0.0, step=100.0, key="invoice_discount")
            total = c10.number_input("Total / Amount Due", min_value=0.0, value=float(fields.get("total_amount") or 0), step=1000.0, key="invoice_total")
            terms = selectbox_with_other("Payment terms", ["Due on Receipt", "Net 7", "Net 15", "Net 30", "Milestone", "Advance Payment", "Other"], "invoice_terms", "payment_terms")
            desc = st.text_area("Invoice description / scope", value=fields.get("description") or "", key="invoice_desc")
            submitted = st.form_submit_button("Save Invoice for Review", type="primary")
        if submitted:
            if not invoice_no.strip():
                st.error("Invoice number is required.")
                return
            if total <= 0:
                st.error("Invoice total must be greater than zero.")
                return
            path, fhash = save_upload(uploaded, "invoices") if uploaded else (None, None)
            po_id = None
            if po_label != "No PO selected":
                po_id = int(po_df[po_df["po_no"] == po_label.split(" — ")[0]].iloc[0]["id"])
            vendor_id = vendors[vendor_label]
            match_status, mismatch = match_invoice_to_po(po_id, vendor_id, total)
            inv_id = run_insert(
                """
                INSERT INTO invoices (invoice_no, receipt_no, po_id, vendor_id, invoice_date, amount, tax_amount, total_amount, file_path, file_hash, ocr_text, ocr_json, match_status, mismatch_reasons, status, uploaded_by, created_at,
                    invoice_type, document_stage, supplier_invoice_no, due_date, payment_terms, subtotal, discount_amount, balance_due, approval_status)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Uploaded', ?, ?, ?, 'Invoice', ?, ?, ?, ?, ?, ?, ?)
                """,
                (invoice_no.strip(), po_id, vendor_id, invoice_date.isoformat(), max(total - tax, 0), tax, total, path, fhash, parsed.get("raw_text", "") if parsed else "", json_dump(parsed) if parsed else "{}", match_status, "; ".join(mismatch), user()["id"], now_iso(), invoice_type, invoice_no.strip(), due_date.isoformat(), terms, subtotal or max(total - tax, 0), discount, total, match_status),
            )
            # Store guessed item lines when available.
            for item in (parsed.get("line_items", []) if parsed else []):
                run_query("INSERT INTO invoice_items (invoice_id, item_description, quantity, unit_price, tax_amount, total, category, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)", (inv_id, item.get("item_name"), item.get("quantity") or 1, item.get("unit_price") or 0, item.get("total") or 0, fields.get("category") or "Other", now_iso()))
            _save_ocr_attempt("Invoice", inv_id, parsed or {})
            add_workflow_event("Invoice", inv_id, "Invoice Uploaded", "Uploaded", match_status, user()["id"])
            create_notification(None, "Finance", "Invoice needs review", f"Invoice {invoice_no} match status: {match_status}", "Invoice", inv_id, "High", ["in_app", "browser_push"])
            log_audit("INVOICE_RECORDED", "Invoice", inv_id, {"invoice_no": invoice_no, "total": total, "match_status": match_status}, user()["id"], user()["role"])
            st.success(f"Invoice {invoice_no} saved for Finance review.")
            st.rerun()
    elif section == "Invoice Register":
        df = df_query("""
            SELECT inv.id, inv.invoice_no, inv.invoice_type, po.po_no, v.name vendor, inv.invoice_date, inv.due_date, inv.total_amount, inv.balance_due, inv.match_status, inv.mismatch_reasons, inv.status
            FROM invoices inv LEFT JOIN purchase_orders po ON inv.po_id=po.id LEFT JOIN vendors v ON inv.vendor_id=v.id ORDER BY inv.created_at DESC
        """)
        if df.empty:
            empty_state("No invoices", "Upload supplier invoices here. Receipts are recorded separately.")
            return
        show = df.drop(columns=["id"]).copy(); show["total_amount"] = show["total_amount"].apply(money); show["balance_due"] = show["balance_due"].apply(money)
        dataframe(show)
        selected = st.selectbox("Select invoice", [f"{r.invoice_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="invoice_select_phase3")
        inv_id = int(selected.rsplit("#", 1)[1])
        inv = df_query("SELECT * FROM invoices WHERE id=?", (inv_id,)).iloc[0]
        st.dataframe(_redact_ui_df(pd.DataFrame([inv.to_dict()]), "invoices"), use_container_width=True)
        with st.expander("OCR / mismatch details", expanded=False):
            try: st.json(json.loads(inv.get("ocr_json") or "{}"))
            except Exception: st.text(inv.get("ocr_text") or "")
            st.write(inv.get("mismatch_reasons") or "")
        if has_permission("review_invoice"):
            c1, c2, c3 = st.columns(3)
            if c1.button("Mark Finance Review Complete", key=f"invoice_reviewed_{inv_id}"):
                run_query("UPDATE invoices SET status='Finance Review', approval_status='Reviewed' WHERE id=?", (inv_id,))
                add_workflow_event("Invoice", inv_id, "Finance Review", "Finance Review", "Invoice reviewed", user()["id"])
                st.rerun()
            if c2.button("Create Payment Request", key=f"invoice_payment_{inv_id}"):
                pno = make_ref("PAY")
                pay_id = run_insert("INSERT INTO payments (payment_no, invoice_id, po_id, vendor_id, amount, payment_method, status, created_by, created_at, updated_at) SELECT ?, id, po_id, vendor_id, total_amount, 'Bank Transfer', 'Pending Approval', ?, ?, ? FROM invoices WHERE id=?", (pno, user()["id"], now_iso(), now_iso(), inv_id))
                create_notification(None, "Approver", "Payment pending approval", f"{pno} requires approval", "Payment", pay_id, "High", ["in_app", "browser_push"])
                add_workflow_event("Payment", pay_id, "Created from Invoice", "Pending Approval", pno, user()["id"])
                st.success(f"Payment request {pno} created.")
            if c3.button("Return Invoice", key=f"invoice_return_{inv_id}"):
                run_query("UPDATE invoices SET status='Returned', approval_status='Returned' WHERE id=?", (inv_id,))
                add_workflow_event("Invoice", inv_id, "Returned", "Returned", "Invoice returned for clarification", user()["id"])
                st.rerun()
        csv_download(show, "invoices")
    elif section == "Invoice Items":
        items = df_query("SELECT ii.*, inv.invoice_no FROM invoice_items ii LEFT JOIN invoices inv ON inv.id=ii.invoice_id ORDER BY ii.created_at DESC LIMIT 1000")
        dataframe(items) if not items.empty else st.info("No invoice item lines captured yet.")
    else:
        attempts = df_query("SELECT * FROM document_ocr_attempts WHERE document_type='Invoice' ORDER BY created_at DESC LIMIT 300")
        dataframe(attempts) if not attempts.empty else st.info("No invoice OCR attempts yet.")


def expenses_page():
    st.subheader("Expenses")
    st.info("Invoices and receipts are now separate. Use Invoices for supplier bills/amount due, and Receipts for proof of payment by cash, transfer, card/POS, cheque, or mobile money. This page remains as the legacy expense register for budget/spend compatibility.")
    expense_register()


def payments_page():
    st.subheader("Payments")
    _ensure_finance_doc_schema_ui()
    df = df_query("SELECT p.id, p.payment_no, v.name vendor, p.amount, p.payment_method, p.payment_date, p.status, p.notes, p.proof_path, p.finance_note FROM payments p LEFT JOIN vendors v ON p.vendor_id=v.id ORDER BY p.created_at DESC")
    if df.empty:
        st.info("No payment requests yet.")
    else:
        show = df.drop(columns=["id"]).copy(); show["amount"] = show["amount"].apply(money); dataframe(show)
    if has_permission("manage_payments"):
        st.markdown("##### Manual Payment Request")
        with st.form("manual_payment_phase3"):
            vendors = vendor_options(False)
            v = st.selectbox("Vendor", list(vendors.keys()), key="manual_pay_vendor")
            amount = st.number_input("Amount", min_value=0.0, step=1000.0, key="manual_pay_amount")
            method = st.selectbox("Method", RECEIPT_PAYMENT_METHODS, key="manual_pay_method")
            notes = st.text_area("Notes", key="manual_pay_notes")
            submitted = st.form_submit_button("Create Payment Request")
        if submitted:
            pno = make_ref("PAY")
            pay_id = run_insert("INSERT INTO payments (payment_no, vendor_id, amount, payment_method, status, notes, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, 'Pending Approval', ?, ?, ?, ?)", (pno, vendors[v], amount, method, notes, user()["id"], now_iso(), now_iso()))
            add_workflow_event("Payment", pay_id, "Created", "Pending Approval", pno, user()["id"])
            create_notification(None, "Approver", "Payment pending approval", f"{pno} requires approval", "Payment", pay_id, "High", ["in_app", "browser_push"])
            st.success("Payment request created.")
            st.rerun()
    if not df.empty and (has_permission("approve_payment") or has_permission("manage_payments")):
        selected = st.selectbox("Approve/Pay", df["payment_no"].tolist(), key="payment_select_phase3")
        row = df[df["payment_no"] == selected].iloc[0]
        finance_note = st.text_area("Payment / finance note", key=f"payment_note_{int(row['id'])}")
        proof = st.file_uploader("Upload payment proof / receipt", type=["pdf", "jpg", "jpeg", "png"], key=f"payment_proof_{int(row['id'])}")
        c1, c2, c3 = st.columns(3)
        if row["status"] == "Pending Approval" and c1.button("Approve Payment", key=f"pay_approve_{int(row['id'])}"):
            from core.db import transition_payment_status
            transition_payment_status(int(row["id"]), "Approved", finance_note or "Payment approved.", user()["id"], user()["role"])
            st.rerun()
        if row["status"] == "Approved" and c2.button("Mark Paid", key=f"pay_paid_{int(row['id'])}"):
            path, _ = save_upload(proof, "payment_proofs") if proof else (None, None)
            from core.db import transition_payment_status
            transition_payment_status(int(row["id"]), "Paid", finance_note or "Payment completed.", user()["id"], user()["role"], path)
            # Create payment receipt shell automatically.
            receipt_no = make_ref("RCT")
            rid = run_insert("INSERT INTO receipt_records (receipt_no, receipt_type, payment_method, payment_date, vendor_id, amount, purpose, linked_payment_id, status, file_path, notes, uploaded_by, created_at, updated_at) VALUES (?, 'Payment Receipt', ?, ?, ?, ?, ?, ?, 'Recorded', ?, ?, ?, ?, ?)", (receipt_no, row.get("payment_method") or "Bank Transfer", date.today().isoformat(), int(row.get("vendor") or 0) if False else None, float(row["amount"]), selected, int(row["id"]), path, finance_note, user()["id"], now_iso(), now_iso()))
            run_query("UPDATE payments SET receipt_id=? WHERE id=?", (rid, int(row["id"])))
            st.success("Payment marked paid and receipt record created.")
            st.rerun()
        if c3.button("Return Payment", key=f"pay_return_{int(row['id'])}"):
            from core.db import transition_payment_status
            transition_payment_status(int(row["id"]), "Returned", finance_note or "Payment returned for clarification.", user()["id"], user()["role"])
            st.rerun()


def _assert_gateway_owner(gateway_pass_id: int, acting_user: dict | None = None) -> bool:
    acting_user = acting_user or user()
    rows = df_query("SELECT facility_manager_user_id FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if rows.empty:
        st.error("Gateway pass not found.")
        return False
    if acting_user["role"] == "Facility Manager" and int(rows.iloc[0]["facility_manager_user_id"]) != int(acting_user["id"]):
        st.error("You can only access your own gateway passes.")
        return False
    return True


def _assert_gateway_reviewer(gateway_pass_id: int, acting_user: dict | None = None) -> bool:
    acting_user = acting_user or user()
    if acting_user["role"] in ["Admin", "Approver"]:
        return True
    if acting_user["role"] != "Procurement Manager":
        st.error("You are not authorized to review gateway passes.")
        return False
    gp = df_query("SELECT facility_manager_user_id FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if gp.empty:
        st.error("Gateway pass not found.")
        return False
    assigned = get_pm_for_facility_manager(int(gp.iloc[0]["facility_manager_user_id"]))
    if assigned and int(assigned) == int(acting_user["id"]):
        return True
    st.error("This gateway pass belongs to a Facility Manager assigned to another Procurement Manager.")
    return False


def submit_gateway_pass(gateway_pass_id: int):
    if not _assert_gateway_owner(gateway_pass_id):
        return
    row_df = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if row_df.empty:
        st.error("Gateway pass not found."); return
    row = row_df.iloc[0]
    if row["status"] not in ["Draft", "Returned for Correction"]:
        st.warning("Only Draft or Returned gateway passes can be submitted."); return
    items = gateway_pass_items_df(gateway_pass_id)
    if items.empty:
        st.error("At least one item line is required before submission."); return
    if (items["quantity"].fillna(0) <= 0).any() or items["unit_of_measure"].fillna("").eq("").any() or items["quality_condition"].fillna("").eq("").any() or items["fragility_status"].fillna("").eq("").any():
        st.error("Every item must include quantity > 0, unit, quality/condition, and fragile/non-fragile status."); return
    pm_id = get_pm_for_facility_manager(int(row["facility_manager_user_id"]))
    if not pm_id:
        st.error("No Procurement Manager is assigned. Ask Admin to link you before submitting."); return
    run_query("UPDATE gateway_passes SET status='Submitted', submitted_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), gateway_pass_id))
    log_gateway_event(gateway_pass_id, "Gateway Pass Submitted", "Submitted", "Submitted for Procurement Manager / Approver review")
    create_notification(pm_id, None, "Gateway Pass Submitted", f"{row['pass_number']} has been submitted by your Facility Manager and requires review.", "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")
    create_notification(None, "Approver", "Gateway Pass Requires Review", f"{row['pass_number']} requires review.", "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")
    create_notification(None, "Admin", "Gateway Pass Oversight", f"{row['pass_number']} has been submitted.", "Gateway Pass", gateway_pass_id, "Normal", ["in_app"])
    create_notification(int(row["facility_manager_user_id"]), None, "Gateway pass submitted", f"{row['pass_number']} was submitted for approval.", "Gateway Pass", gateway_pass_id, "Normal", ["in_app"])
    st.success("Gateway pass submitted for approval.")
    st.rerun()


def _gateway_approve(gateway_pass_id: int, decision: str, note: str):
    if not _assert_gateway_reviewer(gateway_pass_id):
        return
    row_df = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if row_df.empty:
        st.error("Gateway pass not found."); return
    row = row_df.iloc[0]
    if row["status"] not in ["Submitted", "Pending Procurement Manager / Approver Review"]:
        st.warning("Only submitted gateway passes can be approved, returned or rejected."); return
    if decision in ["Rejected", "Returned for Correction"] and not note.strip():
        st.error("A rejection or return reason is required."); return
    if decision == "Approved":
        run_query("UPDATE gateway_passes SET status='Approved', approved_at=?, approved_by_user_id=?, approved_by_role=?, approval_note=?, updated_at=? WHERE id=?", (now_iso(), user()["id"], user()["role"], note or "Approved.", now_iso(), gateway_pass_id))
        decision_label = "Approved"; title = "Gateway Pass Approved"; msg = f"{row['pass_number']} has been approved. You can now preview, generate and download the final gateway pass."
    elif decision == "Rejected":
        run_query("UPDATE gateway_passes SET status='Rejected', rejected_at=?, rejected_by_user_id=?, rejection_reason=?, updated_at=? WHERE id=?", (now_iso(), user()["id"], note, now_iso(), gateway_pass_id))
        decision_label = "Rejected"; title = "Gateway Pass Rejected"; msg = f"{row['pass_number']} was rejected. Reason: {note}"
    else:
        run_query("UPDATE gateway_passes SET status='Returned for Correction', rejection_reason=?, updated_at=? WHERE id=?", (note, now_iso(), gateway_pass_id))
        decision_label = "Returned for Correction"; title = "Gateway Pass Returned"; msg = f"{row['pass_number']} was returned for correction. Reason: {note}"
    run_query("INSERT INTO gateway_pass_approvals (gateway_pass_id, approver_user_id, approver_role, decision, note, created_at) VALUES (?, ?, ?, ?, ?, ?)", (gateway_pass_id, user()["id"], user()["role"], decision_label, note, now_iso()))
    log_gateway_event(gateway_pass_id, f"Gateway Pass {decision_label}", decision_label, note)
    action_label = "Ready to Generate" if decision_label == "Approved" else "Open Gateway Pass"
    create_notification(int(row["facility_manager_user_id"]), None, title, msg, "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label=action_label)
    _notify_auditors(title, msg, "Gateway Pass", gateway_pass_id)
    _rerun_success(f"Gateway pass {decision_label.lower()}.")


def edit_gateway_pass_form(gateway_pass_id: int):
    if not _assert_gateway_owner(gateway_pass_id):
        return
    row_df = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if row_df.empty:
        st.error("Gateway pass not found."); return
    row = row_df.iloc[0]
    if row["status"] not in ["Draft", "Returned for Correction"]:
        st.info("Only Draft or Returned for Correction passes can be edited."); return
    st.markdown("#### Edit Gateway Pass")
    with st.form(f"edit_gateway_pass_{gateway_pass_id}"):
        c1, c2, c3 = st.columns(3)
        dept = c1.selectbox("Department", department_options(), index=department_options().index(row["department"]) if row.get("department") in department_options() else 0, key=f"edit_gp_dept_{gateway_pass_id}")
        movement_type = c2.selectbox("Movement type", GATEWAY_MOVEMENT_TYPES, index=GATEWAY_MOVEMENT_TYPES.index(row["movement_type"]) if row["movement_type"] in GATEWAY_MOVEMENT_TYPES else 0, key=f"edit_gp_mov_{gateway_pass_id}")
        expected_movement = c3.date_input("Expected movement date", pd.to_datetime(row["expected_movement_date"] or date.today()).date(), key=f"edit_gp_date_{gateway_pass_id}")
        purpose = st.text_area("Purpose", value=row["purpose"] or "", key=f"edit_gp_purpose_{gateway_pass_id}")
        c4, c5 = st.columns(2)
        origin = c4.text_input("Origin", value=row["origin_location"] or "", key=f"edit_gp_origin_{gateway_pass_id}")
        destination = c5.text_input("Destination", value=row["destination"] or "", key=f"edit_gp_dest_{gateway_pass_id}")
        c6, c7, c8 = st.columns(3)
        vehicle = c6.text_input("Vehicle number", value=row["vehicle_number"] or "", key=f"edit_gp_vehicle_{gateway_pass_id}")
        driver = c7.text_input("Driver name", value=row["driver_name"] or "", key=f"edit_gp_driver_{gateway_pass_id}")
        driver_phone = c8.text_input("Driver phone", value=row["driver_phone"] or "", key=f"edit_gp_phone_{gateway_pass_id}")
        c9, c10 = st.columns(2)
        receiver = c9.text_input("Receiver name", value=row["receiver_name"] or "", key=f"edit_gp_receiver_{gateway_pass_id}")
        checkpoint = c10.text_input("Security checkpoint", value=row["security_checkpoint"] or "", key=f"edit_gp_check_{gateway_pass_id}")
        submitted = st.form_submit_button("Save Gateway Pass Details")
    if submitted:
        run_query("UPDATE gateway_passes SET department=?, movement_type=?, purpose=?, origin_location=?, destination=?, expected_movement_date=?, vehicle_number=?, driver_name=?, driver_phone=?, receiver_name=?, security_checkpoint=?, updated_at=? WHERE id=?", (dept, movement_type, purpose, origin, destination, expected_movement.isoformat(), vehicle, driver, driver_phone, receiver, checkpoint, now_iso(), gateway_pass_id))
        log_gateway_event(gateway_pass_id, "Gateway Pass Edited", row["status"], "Details updated after draft/return")
        _rerun_success("Gateway pass details updated.")
    st.markdown("#### Edit Item Lines")
    items = gateway_pass_items_df(gateway_pass_id)
    if not items.empty:
        for it in items.itertuples():
            with st.expander(f"Edit item #{int(it.id)} — {it.item_description}", expanded=False):
                with st.form(f"edit_gp_item_{int(it.id)}"):
                    c1, c2, c3 = st.columns(3)
                    desc = c1.text_input("Description", value=it.item_description, key=f"edit_item_desc_{int(it.id)}")
                    qty = c2.number_input("Quantity", min_value=0.01, value=float(it.quantity), step=1.0, key=f"edit_item_qty_{int(it.id)}")
                    unit = c3.text_input("Unit", value=it.unit_of_measure, key=f"edit_item_unit_{int(it.id)}")
                    c4, c5, c6 = st.columns(3)
                    quality = c4.selectbox("Quality / condition", GATEWAY_QUALITY_OPTIONS, index=GATEWAY_QUALITY_OPTIONS.index(it.quality_condition) if it.quality_condition in GATEWAY_QUALITY_OPTIONS else 0, key=f"edit_item_qual_{int(it.id)}")
                    fragile = c5.selectbox("Fragility", GATEWAY_FRAGILITY_OPTIONS, index=GATEWAY_FRAGILITY_OPTIONS.index(it.fragility_status) if it.fragility_status in GATEWAY_FRAGILITY_OPTIONS else 0, key=f"edit_item_frag_{int(it.id)}")
                    colour = c6.text_input("Colour", value=getattr(it, "colour", "") or "", key=f"edit_item_colour_{int(it.id)}")
                    handling = st.text_input("Handling instruction", value=it.handling_instruction or "", key=f"edit_item_handling_{int(it.id)}")
                    remarks = st.text_input("Remarks", value=it.remarks or "", key=f"edit_item_remarks_{int(it.id)}")
                    c7, c8 = st.columns(2)
                    save_item = c7.form_submit_button("Save item")
                    delete_item = c8.form_submit_button("Delete item")
                if save_item:
                    run_query("UPDATE gateway_pass_items SET item_description=?, quantity=?, unit_of_measure=?, quality_condition=?, fragility_status=?, colour=?, handling_instruction=?, remarks=? WHERE id=?", (desc, qty, unit, quality, fragile, colour, handling, remarks, int(it.id)))
                    log_gateway_event(gateway_pass_id, "Gateway Pass Item Edited", row["status"], desc)
                    st.rerun()
                if delete_item:
                    run_query("DELETE FROM gateway_pass_items WHERE id=?", (int(it.id),))
                    log_gateway_event(gateway_pass_id, "Gateway Pass Item Deleted", row["status"], it.item_description)
                    st.rerun()
    with st.form(f"add_gp_item_{gateway_pass_id}"):
        st.markdown("##### Add item line")
        c1, c2, c3 = st.columns(3)
        desc = c1.text_input("Item description", key=f"add_item_desc_{gateway_pass_id}")
        category = c2.text_input("Item category", key=f"add_item_cat_{gateway_pass_id}")
        qty = c3.number_input("Quantity", min_value=0.01, value=1.0, step=1.0, key=f"add_item_qty_{gateway_pass_id}")
        c4, c5, c6, c7 = st.columns(4)
        unit = c4.text_input("Unit", value="Unit", key=f"add_item_unit_{gateway_pass_id}")
        quality = c5.selectbox("Quality / condition", GATEWAY_QUALITY_OPTIONS, key=f"add_item_quality_{gateway_pass_id}")
        fragile = c6.selectbox("Fragility", GATEWAY_FRAGILITY_OPTIONS, key=f"add_item_fragile_{gateway_pass_id}")
        colour = c7.text_input("Colour", key=f"add_item_colour_{gateway_pass_id}")
        handling = st.text_input("Handling instruction", key=f"add_item_handling_{gateway_pass_id}")
        remarks = st.text_input("Remarks", key=f"add_item_remarks_{gateway_pass_id}")
        add_item = st.form_submit_button("Add Item")
    if add_item:
        if not desc.strip() or qty <= 0 or not unit.strip():
            st.error("Description, quantity greater than 0, and unit are required.")
        else:
            run_query("INSERT INTO gateway_pass_items (gateway_pass_id, item_description, item_category, quantity, unit_of_measure, quality_condition, fragility_status, colour, handling_instruction, remarks, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (gateway_pass_id, desc, category, qty, unit, quality, fragile, colour, handling, remarks, now_iso()))
            log_gateway_event(gateway_pass_id, "Gateway Pass Item Added", row["status"], desc)
            st.rerun()


def generate_gateway_pass_document(gateway_pass_id: int) -> str | None:
    if user()["role"] == "Facility Manager" and not _assert_gateway_owner(gateway_pass_id):
        return None
    # Reuse the existing professional PDF generator body by calling the previous implementation is not possible after override.
    # The original function remains available in source above, so this compact secured version delegates by copying its core behavior through a local alias stored before override is not available.
    # To keep behavior stable, call the original code path through the generated preview/export by temporarily trusting status guard: use _generate_gateway_pass_pdf_core.
    return _generate_gateway_pass_pdf_core(gateway_pass_id)


def _generate_gateway_pass_pdf_core(gateway_pass_id: int) -> str | None:
    # Minimal professional PDF fallback compatible with the company template. The richer preview remains in HTML.
    gp = gateway_pass_summary_df("gp.id=?", (gateway_pass_id,))
    if gp.empty:
        st.error("Gateway pass not found."); return None
    row = gp.iloc[0]
    if row["status"] not in ["Approved", "Generated", "Downloaded"]:
        st.error("Generate is disabled until the gateway pass is approved."); return None
    items = gateway_pass_items_df(gateway_pass_id)
    if items.empty:
        st.error("Cannot generate a gateway pass without item lines."); return None
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        out_dir = Path("data/attachments/gateway_passes")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{row['pass_number'].replace('/', '_')}.pdf"
        doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=14*mm, leftMargin=14*mm, topMargin=12*mm, bottomMargin=12*mm)
        styles = getSampleStyleSheet(); normal = styles["Normal"]
        title = ParagraphStyle("gp_title", parent=styles["Title"], fontSize=16, alignment=1, textColor=colors.HexColor("#0f172a"), spaceAfter=4)
        small = ParagraphStyle("gp_small", parent=normal, fontSize=8, leading=10)
        story = []
        story.append(Paragraph("Consultancy Services Unit, Rivers State University", title))
        story.append(Paragraph("Center For Marine and Offshore Technology Development (CMOTD)", ParagraphStyle("sub", parent=normal, alignment=1, fontSize=10, leading=12)))
        story.append(Paragraph("Consultancy Unit, Rivers State University, Nkpolu-Oroworokwo, Port Harcourt, Rivers State", ParagraphStyle("addr", parent=normal, alignment=1, fontSize=8)))
        story.append(Paragraph("Email: info@cmotd.org &nbsp;&nbsp; Phone NO.: +2349163505000", ParagraphStyle("addr2", parent=normal, alignment=1, fontSize=8)))
        story.append(Paragraph("Where Theory becomes Reality and Individuals are Equipped to Lead in the Industry!", ParagraphStyle("motto", parent=normal, alignment=1, fontSize=8, italic=True, textColor=colors.HexColor("#065f46"))))
        story.append(Spacer(1, 6))
        story.append(Paragraph("PROPERTY MOVEMENT GATE PASS", ParagraphStyle("doc_title", parent=styles["Heading1"], alignment=1, fontSize=14, textColor=colors.HexColor("#111827"))))
        info = [["Reference No.", row["pass_number"], "Date", date.today().strftime("%d %B %Y")], ["Facility Manager", row.get("facility_manager") or "", "Department", row.get("department") or ""], ["Movement Type", row.get("movement_type") or "", "Destination", row.get("destination") or ""], ["Origin", row.get("origin_location") or "", "Movement Date", str(row.get("expected_movement_date") or "")]]
        t = Table(info, colWidths=[34*mm, 58*mm, 34*mm, 58*mm]); t.setStyle(TableStyle([("GRID", (0,0), (-1,-1), .4, colors.grey), ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#f1f5f9")), ("BACKGROUND", (2,0), (2,-1), colors.HexColor("#f1f5f9")), ("FONTNAME", (0,0), (-1,-1), "Helvetica"), ("FONTSIZE", (0,0), (-1,-1), 8), ("PADDING", (0,0), (-1,-1), 5)])); story.append(t)
        story.append(Spacer(1, 7)); story.append(Paragraph("This Gate Pass serves as official authorization for the movement of the underlisted company asset(s) from the premises of the Centre for Marine and Offshore Technology Development (CMOTD).", normal))
        story.append(Spacer(1, 7)); story.append(Paragraph("PROPERTY DETAILS", styles["Heading3"]))
        data = [["No.", "Item Description", "Colour", "Quantity", "Unit", "Condition", "Fragile?", "Serial/Asset", "Handling"]]
        for idx, it in enumerate(items.itertuples(), 1):
            data.append([idx, Paragraph(str(it.item_description), small), getattr(it, "colour", "") or "", it.quantity, it.unit_of_measure, it.quality_condition, it.fragility_status, f"{it.serial_number or ''} {it.asset_tag or ''}", Paragraph(it.handling_instruction or "", small)])
        table = Table(data, colWidths=[8*mm, 42*mm, 18*mm, 16*mm, 14*mm, 23*mm, 18*mm, 24*mm, 27*mm], repeatRows=1)
        table.setStyle(TableStyle([("GRID", (0,0), (-1,-1), .35, colors.grey), ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#dbeafe")), ("FONTSIZE", (0,0), (-1,-1), 7), ("VALIGN", (0,0), (-1,-1), "TOP")]))
        story.append(table)
        story.append(Spacer(1, 7)); story.append(Paragraph("PURPOSE OF MOVEMENT", styles["Heading3"])); story.append(Paragraph(row.get("purpose") or "", normal))
        story.append(Spacer(1, 7)); story.append(Paragraph("TRANSPORT DETAILS", styles["Heading3"])); story.append(Paragraph(f"Driver's Name: {row.get('driver_name') or '____________________________________'}<br/>Driver's Phone Number: {row.get('driver_phone') or '_____________________________'}<br/>Vehicle Number: {row.get('vehicle_number') or '_____________________________'}", normal))
        story.append(Spacer(1, 7)); story.append(Paragraph("AUTHORIZATION", styles["Heading3"])); story.append(Paragraph(f"I hereby certify that the movement of the above company property has been duly approved and authorized.<br/><br/>Authorizing Officer: {row.get('approved_by') or '_______________________________'}<br/>Designation: {row.get('approved_by_role') or '______________________________________'}<br/>Signature: ________________________________________<br/>Date: {str(row.get('approved_at') or '')}", normal))
        story.append(Spacer(1, 7)); story.append(Paragraph("SECURITY VERIFICATION", styles["Heading3"])); story.append(Paragraph("Security Officer Name: ______________________________ &nbsp;&nbsp; Gate Verification Time: ______________________________<br/>Exit/Entry Confirmation: ______________________________ &nbsp;&nbsp; Signature: ______________________________", normal))
        story.append(Spacer(1, 5)); story.append(Paragraph("This gateway pass is valid only for the listed items and approved movement date. System reference number: GP-%s" % gateway_pass_id, small))
        doc.build(story)
        run_query("UPDATE gateway_passes SET status='Generated', next_role=NULL, generated_at=?, generated_file_path=?, updated_at=? WHERE id=?", (now_iso(), str(path), now_iso(), gateway_pass_id))
        log_gateway_event(gateway_pass_id, "Gateway Pass Generated", "Generated", str(path))
        return str(path)
    except Exception as exc:
        st.error(f"Could not generate PDF gateway pass: {exc}")
        return None


def gateway_pass_review_queue(title: str, admin_mode: bool = False):
    _phase2_bootstrap()
    st.subheader(title)
    if user()["role"] == "Procurement Manager" and not admin_mode:
        df = gateway_pass_summary_df("gp.status IN ('Submitted','Pending Procurement Manager / Approver Review') AND EXISTS (SELECT 1 FROM facility_manager_links fml WHERE fml.facility_manager_user_id=gp.facility_manager_user_id AND fml.procurement_manager_user_id=? AND fml.is_active=1)", (user()["id"],))
    else:
        df = gateway_pass_summary_df("gp.status IN ('Submitted','Pending Procurement Manager / Approver Review')", ())
    if df.empty:
        st.success("No gateway passes are awaiting review."); return
    show = df[["id", "pass_number", "facility_manager", "department", "movement_type", "destination", "expected_movement_date", "status", "submitted_at"]].copy()
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open gateway pass for review", [f"{r.pass_number} — {r.facility_manager} — #{int(r.id)}" for r in df.itertuples()], key=f"gp_review_select_phase3_{user()['role']}_{admin_mode}")
    gp_id = int(selected.rsplit("#", 1)[1])
    gateway_pass_detail(gp_id)
    note = st.text_area("Review note / reason", key=f"gp_review_note_phase3_{gp_id}_{user()['role']}_{admin_mode}")
    c1, c2, c3 = st.columns(3)
    if c1.button("Approve Gateway Pass", type="primary", key=f"gp_approve_phase3_{gp_id}_{user()['role']}"):
        _gateway_approve(gp_id, "Approved", note or "Approved.")
    if c2.button("Return for Correction", key=f"gp_return_phase3_{gp_id}_{user()['role']}"):
        _gateway_approve(gp_id, "Returned for Correction", note)
    if c3.button("Reject Gateway Pass", key=f"gp_reject_phase3_{gp_id}_{user()['role']}"):
        _gateway_approve(gp_id, "Rejected", note)


def gateway_pass_register(where_sql: str, params: tuple | list, title: str, allow_submit: bool = False, allow_generate: bool = False, key_prefix: str = "gp_register"):
    st.subheader(title)
    df = gateway_pass_summary_df(where_sql, params)
    if df.empty:
        empty_state("No gateway passes", "Gateway pass records will appear here."); return
    show_cols = ["id", "pass_number", "facility_manager", "department", "movement_type", "destination", "expected_movement_date", "status", "approved_by", "updated_at"]
    show = df[[c for c in show_cols if c in df.columns]].copy()
    dataframe(show.drop(columns=["id"]) if "id" in show.columns else show)
    selected = st.selectbox("Open gateway pass", [f"{r.pass_number} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key=f"{key_prefix}_select_phase3")
    gp_id = int(selected.rsplit("#", 1)[1])
    row = df[df["id"] == gp_id].iloc[0]
    gateway_pass_detail(gp_id)
    if user()["role"] == "Facility Manager" and row["status"] in ["Draft", "Returned for Correction"]:
        edit_gateway_pass_form(gp_id)
    events = df_query("SELECT event, status, note, user_id, created_at FROM gateway_pass_events WHERE gateway_pass_id=? ORDER BY created_at DESC", (gp_id,))
    with st.expander("Gateway pass history", expanded=False):
        dataframe(events) if not events.empty else st.info("No events yet.")
    if allow_submit and row["status"] in ["Draft", "Returned for Correction"]:
        if st.button("Submit Gateway Pass for Approval", type="primary", key=f"{key_prefix}_submit_phase3_{gp_id}"):
            submit_gateway_pass(gp_id)
    if allow_generate:
        ready = row["status"] in ["Approved", "Generated", "Downloaded"]
        if ready:
            render_gateway_pass_preview(gp_id)
        else:
            st.info("The final company-format preview and Generate button unlock after approval by Procurement Manager, Approver/MD, or Admin.")
        if st.button("Generate Final Gateway Pass PDF", type="primary", key=f"{key_prefix}_generate_phase3_{gp_id}", disabled=not ready):
            path = generate_gateway_pass_document(gp_id)
            if path:
                st.success("Gateway pass PDF generated. Review the preview above, then download the final PDF below."); st.rerun()
        if not ready:
            st.caption("Generate is disabled until the gateway pass is approved by Procurement Manager, Approver/MD, or Admin.")
        refreshed = gateway_pass_summary_df("gp.id=?", (gp_id,)).iloc[0]
        if refreshed["status"] in ["Generated", "Downloaded"] or refreshed.get("generated_file_path"):
            st.markdown("#### Download")
            gateway_pass_download_button(refreshed)


def finance_workspace():
    role_header("Finance Workspace", "Invoices, receipts, payments, expenses, budgets, reconciliation and financial controls.")
    section = st.session_state.get("finance_section", "Financial Dashboard")
    if section == "Financial Dashboard":
        finance_metrics(); finance_dashboard()
    elif section == "Approved for Payment":
        approved_for_payment_page()
    elif section == "Receipts":
        receipts_page()
    elif section == "Invoices":
        invoices_page()
    elif section == "Expenses":
        expenses_page()
    elif section == "Payments":
        payments_page()
    elif section == "Cash Advances":
        cash_advances_page()
    elif section == "Budgets":
        budgets_page()
    elif section == "Vendor Payment Records":
        payments_page()
    elif section == "Reconciliation":
        reconciliation_page()
    elif section == "Financial Reports":
        finance_reports()
    elif section == "Settings":
        settings_page()
    else:
        finance_metrics(); finance_dashboard()


def audit_workspace():
    role_header("Audit & Compliance Workspace", "Strictly read-only review of lifecycles, approvals, delegated approvals, budgets, imports, handoffs, gateway passes, vendors and finance status changes.")
    section = st.session_state.get("audit_section", "Audit Dashboard")
    if section == "Audit Dashboard":
        audit_metrics(); audit_dashboard()
    elif section == "Procurement Records":
        auditor_records_page()
    elif section == "Document Archive":
        document_archive(editable=False)
    elif section == "Approval Trails":
        approval_trails_page()
    elif section == "Delegated Approval Review":
        delegated_approval_review_page()
    elif section == "Budget Audit":
        budget_audit_page()
    elif section == "Facility Manager Handoff Trail":
        facility_handoff_trail_page()
    elif section == "Gateway Pass Audit":
        gateway_pass_audit_page()
    elif section == "Vendor History":
        vendor_history_page()
    elif section == "Expense Review":
        expense_review_page()
    elif section == "Compliance Reports":
        compliance_reports()
    elif section == "Settings":
        settings_page()
    else:
        audit_metrics(); audit_dashboard()

# ============================================================================
# Phase 4 UX upgrades: compact KPI cards, dynamic charts, badge counts, guided PRs,
# persistent-audit views, smarter Other dropdowns, and payment-method receipt OCR.
# These late definitions intentionally override earlier versions safely.
# ============================================================================

from datetime import datetime
from core.ui import interactive_chart, format_kpi_value


def _parse_date_value(raw: Any, fallback: date | None = None) -> date:
    """Normalize OCR date text into a Python date. Falls back only when OCR is absent/invalid."""
    fallback = fallback or date.today()
    if raw is None:
        return fallback
    text = str(raw).strip().replace(".", "/").replace("-", "/")
    if not text:
        return fallback
    # Month names require the original text.
    original = str(raw).strip()
    for fmt in [
        "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y",
        "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
        "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d",
    ]:
        try:
            value = original if "%b" in fmt or "%B" in fmt or "-" in fmt else text
            return datetime.strptime(value, fmt).date()
        except Exception:
            continue
    # Defensive Nigerian-style default: if ambiguous like 05/06/2026, treat as DD/MM/YYYY.
    import re
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mth, d)
        except Exception:
            try:
                return date(y, d, mth)
            except Exception:
                return fallback
    return fallback


def _custom_values(field_name: str) -> list[str]:
    try:
        rows = df_query("SELECT custom_value FROM custom_dropdown_values WHERE field_name=? ORDER BY custom_value", (field_name,))
        return rows["custom_value"].dropna().astype(str).tolist() if not rows.empty else []
    except Exception:
        return []


def _save_custom_value(field_name: str, value: str):
    value = (value or "").strip()
    if not value:
        return
    try:
        run_query(
            "INSERT OR IGNORE INTO custom_dropdown_values (field_name, custom_value, created_by, created_at) VALUES (?, ?, ?, ?)",
            (field_name, value, user().get("id"), now_iso()),
        )
    except Exception:
        pass


def selectbox_with_other(label: str, options: list[str], key: str, field_name: str | None = None, index: int = 0, help: str | None = None) -> str:
    """Dropdown that opens an input box when Other is selected and remembers custom values."""
    field_name = field_name or key
    merged = []
    for item in list(options) + _custom_values(field_name):
        if item and item not in merged:
            merged.append(item)
    if "Other" not in merged:
        merged.append("Other")
    index = min(index, max(len(merged) - 1, 0))
    chosen = st.selectbox(label, merged, index=index, key=key, help=help)
    if chosen == "Other":
        typed = st.text_input(f"Specify other {label.lower()}", key=f"{key}_other", placeholder="Type the exact value you want to use")
        if typed.strip():
            st.caption(f"This will be saved as: {typed.strip()}")
            return typed.strip()
        return "Other"
    return chosen


def metric_row(metrics: list[tuple[str, Any, str | None]], cols: int = 4):
    columns = st.columns(cols)
    for i, (label, value, help_text) in enumerate(metrics):
        columns[i % cols].metric(label, format_kpi_value(value), help=help_text)


def _money_chart_df(df: pd.DataFrame, amount_col: str = "total") -> pd.DataFrame:
    if df is None or df.empty:
        return df
    data = df.copy()
    try:
        data[amount_col] = pd.to_numeric(data[amount_col], errors="coerce").fillna(0)
    except Exception:
        pass
    return data


def analytics():
    st.subheader("Interactive Visual Analytics")
    st.caption("Use each chart selector to switch between bar, horizontal bar, line, area, pie, donut, or table views. This makes the same data explain different procurement questions.")
    c1, c2 = st.columns(2)
    with c1:
        df = df_query("SELECT category, SUM(amount) total FROM expenses WHERE status IN ('Approved','Paid') GROUP BY category ORDER BY total DESC")
        interactive_chart(_money_chart_df(df), "Spend by Category", "category", "total", "analytics_spend_category", default="Bar")
    with c2:
        df = df_query("SELECT COALESCE(v.name,'No vendor') vendor, SUM(e.amount) total FROM expenses e LEFT JOIN vendors v ON e.vendor_id=v.id WHERE e.status IN ('Approved','Paid') GROUP BY vendor ORDER BY total DESC")
        interactive_chart(_money_chart_df(df), "Spend by Vendor", "vendor", "total", "analytics_spend_vendor", default="Horizontal Bar")
    c3, c4 = st.columns(2)
    with c3:
        df = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status ORDER BY count DESC")
        interactive_chart(df, "Requests by Status", "status", "count", "analytics_req_status", default="Bar")
    with c4:
        df = df_query("SELECT receiving_status, COUNT(*) count FROM purchase_orders GROUP BY receiving_status ORDER BY count DESC")
        interactive_chart(df, "PO Delivery Status", "receiving_status", "count", "analytics_po_receiving", default="Donut")
    c5, c6 = st.columns(2)
    with c5:
        df = df_query("SELECT substr(created_at,1,7) month, COUNT(*) count FROM purchase_requests GROUP BY month ORDER BY month")
        interactive_chart(df, "Monthly Request Trend", "month", "count", "analytics_monthly_requests", default="Line", allow_pie=False)
    with c6:
        df = df_query("SELECT payment_method, SUM(amount) total FROM receipt_records GROUP BY payment_method ORDER BY total DESC")
        interactive_chart(_money_chart_df(df), "Receipt Value by Payment Method", "payment_method", "total", "analytics_receipt_methods", default="Pie")


def admin_overview():
    c1, c2 = st.columns(2)
    with c1:
        df = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status ORDER BY count DESC")
        interactive_chart(df, "Procurement Pipeline", "status", "count", "admin_pipeline", default="Bar")
        st.subheader("Recently Imported Documents")
        docs = df_query("SELECT id, document_type, department_project, title, total_amount, confidence, import_status FROM imported_legacy_documents ORDER BY created_at DESC LIMIT 10")
        if not docs.empty:
            docs["total_amount"] = docs["total_amount"].apply(money)
            dataframe(docs)
        else:
            empty_state("No imports yet", "Use the Import Center to import procurement documents.")
    with c2:
        st.subheader("System Activity")
        logs = df_query("SELECT created_at, event_date, event_time, action, entity_type, entity_id, details FROM audit_logs ORDER BY created_at DESC LIMIT 12")
        dataframe(_redact_ui_df(logs, "audit_logs")) if not logs.empty else empty_state("No audit events", "System actions will be logged here.")
        st.subheader("Budget Risk")
        df = budget_risk_df()
        if not df.empty:
            dataframe(df)
        else:
            st.success("No budget risk detected.")


def procurement_dashboard():
    st.subheader("What needs my attention?")
    c1, c2, c3 = st.columns(3)
    fm_inbox = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status='Submitted to Procurement Manager'").iloc[0, 0])
    gp_waiting = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Submitted','Pending Procurement Manager / Approver Review')").iloc[0, 0])
    sourcing = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status IN ('Requires Sourcing','Vendor Quote Collection')").iloc[0, 0])
    c1.metric("Facility Manager Inbox", format_kpi_value(fm_inbox))
    c2.metric("Gateway Passes Awaiting Review", format_kpi_value(gp_waiting))
    c3.metric("Needs Sourcing", format_kpi_value(sourcing))
    if gp_waiting:
        st.warning(f"{gp_waiting} gateway pass(es) are awaiting review.")
    df = df_query("SELECT request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE status NOT IN ('Closed','Rejected','Paid') ORDER BY updated_at DESC LIMIT 20")
    if not df.empty:
        show = df.copy(); show["estimated_amount"] = show["estimated_amount"].apply(money); dataframe(show)
    else:
        st.success("No open procurement requests.")
    c1, c2 = st.columns(2)
    with c1:
        pipe = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status ORDER BY count DESC")
        interactive_chart(pipe, "Procurement Pipeline", "status", "count", "pm_pipeline", default="Bar")
    with c2:
        spend = df_query("SELECT category, SUM(estimated_amount) total FROM purchase_requests WHERE status NOT IN ('Rejected','Cancelled') GROUP BY category ORDER BY total DESC")
        interactive_chart(_money_chart_df(spend), "Estimated Request Value by Category", "category", "total", "pm_category_value", default="Donut")


def finance_dashboard():
    st.subheader("Finance Attention Center")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Approved for Payment", format_kpi_value(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status='Approved for Payment' OR payment_status='Approved for Payment'").iloc[0,0]))
    c2.metric("Invoices Needing Review", format_kpi_value(df_query("SELECT COUNT(*) FROM invoices WHERE status IN ('Uploaded','Needs Review') OR match_status IN ('Needs Review','Mismatch')").iloc[0,0]))
    c3.metric("Payments Pending", format_kpi_value(df_query("SELECT COUNT(*) FROM payments WHERE status IN ('Pending Approval','Approved')").iloc[0,0]))
    c4.metric("Receipts Today", format_kpi_value(df_query("SELECT COUNT(*) FROM receipt_records WHERE substr(created_at,1,10)=date('now')").iloc[0,0]))
    c1, c2 = st.columns(2)
    with c1:
        df = df_query("SELECT payment_method, SUM(amount) total FROM receipt_records GROUP BY payment_method ORDER BY total DESC")
        interactive_chart(_money_chart_df(df), "Receipts by Payment Method", "payment_method", "total", "finance_receipts_method", default="Bar")
    with c2:
        df = df_query("SELECT status, COUNT(*) count FROM invoices GROUP BY status ORDER BY count DESC")
        interactive_chart(df, "Invoice Queue by Status", "status", "count", "finance_invoice_status", default="Donut")
    c3, c4 = st.columns(2)
    with c3:
        df = df_query("SELECT category, SUM(amount) total FROM expenses WHERE status IN ('Approved','Paid') GROUP BY category ORDER BY total DESC")
        interactive_chart(_money_chart_df(df), "Approved Spend by Category", "category", "total", "finance_category_spend", default="Horizontal Bar")
    with c4:
        df = df_query("SELECT substr(payment_date,1,7) month, SUM(amount) total FROM payments WHERE status='Paid' GROUP BY month ORDER BY month")
        interactive_chart(_money_chart_df(df), "Paid Payments Trend", "month", "total", "finance_paid_trend", default="Line", allow_pie=False)


def executive_dashboard():
    st.subheader("Executive Decision Center")
    pending = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval')").iloc[0, 0])
    gp_waiting = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Submitted','Pending Procurement Manager / Approver Review')").iloc[0, 0])
    payment_pending = int(df_query("SELECT COUNT(*) c FROM payments WHERE status='Pending Approval'").iloc[0, 0])
    metric_row([
        ("Pending approvals", pending, None),
        ("Gateway Passes Awaiting Review", gp_waiting, None),
        ("Payment approvals", payment_pending, None),
    ], cols=3)
    df = df_query("SELECT request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval') ORDER BY estimated_amount DESC LIMIT 20")
    if not df.empty:
        show = df.copy(); show["estimated_amount"] = show["estimated_amount"].apply(money); dataframe(show)
    else:
        st.success("No pending request approvals.")
    c1, c2 = st.columns(2)
    with c1:
        by_cat = df_query("SELECT category, COUNT(*) count FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval') GROUP BY category ORDER BY count DESC")
        interactive_chart(by_cat, "Pending Approvals by Category", "category", "count", "exec_pending_cat", default="Bar")
    with c2:
        by_value = df_query("SELECT category, SUM(estimated_amount) total FROM purchase_requests WHERE status IN ('Pending Approval','Pending Approver/MD Approval') GROUP BY category ORDER BY total DESC")
        interactive_chart(_money_chart_df(by_value), "Approval Value by Category", "category", "total", "exec_value_cat", default="Donut")


def audit_dashboard():
    st.subheader("Compliance Snapshot")
    recent_notifs = df_query("SELECT title, message, entity_type, entity_id, created_at FROM notifications WHERE role='Auditor' OR user_id=? ORDER BY created_at DESC LIMIT 25", (user()["id"],))
    if not recent_notifs.empty:
        st.markdown("#### Recent activity notifications")
        dataframe(recent_notifs)
    metric_row([
        ("Gateway pass audit count", int(df_query("SELECT COUNT(*) c FROM gateway_passes").iloc[0,0]), None),
        ("Approved passes", int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status='Approved'").iloc[0,0]), None),
        ("Generated passes", int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE status IN ('Generated','Downloaded')").iloc[0,0]), None),
        ("Fragile item movements", int(df_query("SELECT COUNT(DISTINCT gateway_pass_id) c FROM gateway_pass_items WHERE fragility_status='Fragile'").iloc[0,0]), None),
    ], cols=4)
    c1, c2 = st.columns(2)
    with c1:
        logs_by_action = df_query("SELECT action, COUNT(*) count FROM audit_logs GROUP BY action ORDER BY count DESC LIMIT 15")
        interactive_chart(logs_by_action, "Audit Events by Action", "action", "count", "audit_actions", default="Horizontal Bar")
    with c2:
        login = df_query("SELECT action, COUNT(*) count FROM audit_logs WHERE action IN ('LOGIN','LOGOUT') GROUP BY action")
        interactive_chart(login, "Login / Logout Activity", "action", "count", "audit_login_logout", default="Donut")
    logs = df_query("SELECT event_date, event_time, created_at, action, entity_type, entity_id, role, details FROM audit_logs ORDER BY created_at DESC LIMIT 20")
    dataframe(_redact_ui_df(logs, "audit_logs")) if not logs.empty else st.info("No audit logs yet.")


def audit_log_page(full=False):
    st.subheader("Audit Logs")
    st.caption("This view separates login/logout sessions from general audit events so a novice can see who entered the system, when they entered, and when they left.")
    limit = 1000 if full else 200
    session_df = df_query(f"""
        SELECT u.username, u.full_name, u.role, s.login_at, substr(s.login_at,1,10) login_date, substr(s.login_at,12,8) login_time,
               s.logout_at, substr(s.logout_at,1,10) logout_date, substr(s.logout_at,12,8) logout_time,
               s.last_seen_at, s.status
        FROM user_sessions s LEFT JOIN users u ON u.id=s.user_id
        ORDER BY s.login_at DESC LIMIT {limit}
    """)
    st.markdown("#### Login / Logout Sessions")
    if not session_df.empty:
        dataframe(session_df)
        csv_download(session_df, "login_logout_sessions")
    else:
        st.info("No session records yet. New logins will appear here.")
    st.markdown("#### Detailed Audit Events")
    df = df_query(f"""
        SELECT COALESCE(a.event_date, substr(a.created_at,1,10)) event_date,
               COALESCE(a.event_time, substr(a.created_at,12,8)) event_time,
               a.created_at, u.full_name user, a.role, a.action, a.entity_type, a.entity_id,
               a.details, a.before_values, a.after_values
        FROM audit_logs a LEFT JOIN users u ON a.user_id=u.id
        ORDER BY a.created_at DESC LIMIT {limit}
    """)
    safe = _redact_ui_df(df, "audit_logs")
    dataframe(safe) if not safe.empty else empty_state("No audit logs", "Sensitive actions will appear here.")
    csv_download(safe, "audit_logs")


def notifications_monitor_page():
    _phase2_bootstrap(); _ensure_finance_doc_schema_ui()
    st.subheader("Notifications Monitor")
    st.caption("Monitor in-app unread notifications, browser push readiness, fallback outbox, and user notification preferences.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Unread in-app", format_kpi_value(df_query("SELECT COUNT(*) FROM notifications WHERE is_read=0").iloc[0,0]))
    c2.metric("Push-enabled users", format_kpi_value(df_query("SELECT COUNT(*) FROM notification_preferences WHERE browser_push_enabled=1").iloc[0,0]))
    c3.metric("Active subscriptions", format_kpi_value(df_query("SELECT COUNT(*) FROM push_subscriptions WHERE is_active=1").iloc[0,0]))
    c4.metric("Outbox queued/fallback", format_kpi_value(df_query("SELECT COUNT(*) FROM notification_outbox WHERE status IN ('Queued','Fallback')").iloc[0,0]))
    st.info("Browser/system push is a progressive enhancement. If a browser or local Streamlit deployment does not support service workers/push subscriptions, ProcureFlow keeps the alert unread and queues a fallback record here.")
    smtp_ready, smtp_msg = email_delivery_ready()
    if smtp_ready:
        st.success(f"Email delivery: {smtp_msg}")
    else:
        st.warning(f"Email delivery: {smtp_msg}")
    st.caption("Users must also save an email address and enable Email notifications in their own Settings before important alerts are emailed.")
    st.markdown("#### Browser Notification Readiness")
    readiness = df_query("""
        SELECT u.username, u.full_name, u.role, np.browser_push_enabled, np.browser_permission_status,
               COUNT(ps.id) active_subscriptions, MAX(ps.last_success_at) last_push_success, MAX(ps.last_failure_at) last_push_failure
        FROM users u
        LEFT JOIN notification_preferences np ON np.user_id=u.id
        LEFT JOIN push_subscriptions ps ON ps.user_id=u.id AND ps.is_active=1
        GROUP BY u.id ORDER BY u.role, u.username
    """)
    dataframe(_redact_ui_df(readiness, "push_subscriptions")) if not readiness.empty else st.info("No browser readiness data yet.")
    st.markdown("#### Notifications")
    df = df_query("SELECT n.*, u.username, u.full_name FROM notifications n LEFT JOIN users u ON n.user_id=u.id ORDER BY n.created_at DESC LIMIT 500")
    dataframe(_redact_ui_df(df, "notifications")) if not df.empty else st.info("No notifications yet.")
    st.markdown("#### External Notification Outbox")
    outbox = _safe_table_df("notification_outbox", 500)
    dataframe(_redact_ui_df(outbox, "notification_outbox")) if not outbox.empty else st.info("No external notification outbox items yet.")
    st.markdown("#### User Preferences")
    prefs = df_query("SELECT np.*, u.username, u.full_name, u.role FROM notification_preferences np LEFT JOIN users u ON u.id=np.user_id ORDER BY u.role, u.username")
    dataframe(prefs) if not prefs.empty else st.info("No preferences yet.")


def requests_page(mode="procurement"):
    st.subheader("Purchase Requests")
    section = st.radio("Purchase Request sections", ["Create Request", "Guided Next Actions", "Request Register", "Imported Draft Review"], horizontal=True, key=f"requests_sections_{mode}")
    if section == "Create Request":
        create_request_form()
    elif section == "Guided Next Actions":
        request_next_action_board()
    elif section == "Request Register":
        request_register(actions=True)
    else:
        imported_draft_review()


def create_request_form():
    if not has_permission("create_request"):
        st.info("Your role can view requests but cannot create requests.")
        return
    st.caption("Create a draft request first. The next action board will guide procurement users to mark sourcing, send for approval, or create a PO.")
    c1, c2, c3 = st.columns(3)
    dept = selectbox_with_other("Department / Project", department_options() + ["Other"], "req_dept_phase4", "department_project")
    req_date = c2.date_input("Request date", date.today(), key="req_date_phase4")
    req_required = c3.date_input("Required date", date.today() + timedelta(days=7), key="req_required_phase4")
    c4, c5, c6 = st.columns(3)
    cat = selectbox_with_other("Category", EXPENSE_CATEGORIES, "req_cat_phase4", "category")
    priority = c5.selectbox("Priority", PRIORITIES, index=1, key="req_priority_phase4")
    vendor_pref = c6.text_input("Vendor preference", key="req_vendor_pref_phase4")
    justification = st.text_area("Business justification", key="req_justification_phase4")
    attachment = st.file_uploader("Supporting document", type=["docx", "pdf", "jpg", "jpeg", "png"], key="req_attachment_phase4")
    item_count = st.number_input("Line items", 1, 15, 1, key="req_item_count_phase4")
    items, estimated = [], 0.0
    for i in range(int(item_count)):
        c1, c2, c3, c4 = st.columns([1.4, .7, .9, 1])
        item = c1.text_input("Item", key=f"req4_item_{i}")
        qty = c2.number_input("Qty", 0.0, value=1.0, step=1.0, key=f"req4_qty_{i}")
        unit = c3.number_input("Unit price", 0.0, step=1000.0, key=f"req4_unit_{i}")
        icat = selectbox_with_other("Item category", EXPENSE_CATEGORIES, f"req4_item_cat_{i}", "category", index=EXPENSE_CATEGORIES.index("Other") if cat == "Other" else 0)
        total = qty * unit
        estimated += total
        items.append((item, qty, unit, total, icat))
    st.metric("Estimated request value", format_kpi_value(money(estimated)))
    if st.button("Create Draft Request", type="primary", key="create_draft_request_phase4"):
        if not justification or not any(i[0] for i in items):
            st.error("Business justification and at least one item are required.")
            return
        _save_custom_value("department_project", dept); _save_custom_value("category", cat)
        path, _ = save_upload(attachment, "requests")
        req_no = make_ref("PR")
        req_id = run_insert("""
            INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, required_date, category, justification, priority, estimated_amount, vendor_preference, status, attachments_json, notes, approval_history_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', ?, '', '[]', ?, ?)
        """, (req_no, user()["id"], dept, req_date.isoformat(), req_required.isoformat(), cat, justification, priority, estimated, vendor_pref, json_dump([path] if path else []), now_iso(), now_iso()))
        for item, qty, unit, total, icat in items:
            if item:
                _save_custom_value("category", icat)
                run_query("INSERT INTO purchase_request_items (request_id, item_name, description, quantity, unit_price, total, category, suggested_vendor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (req_id, item, item, qty, unit, total, icat, vendor_pref, now_iso()))
        add_workflow_event("Purchase Request", req_id, "Created", "Draft", req_no, user()["id"])
        create_notification(user_id=user()["id"], title="Request draft created", message=f"{req_no} was created as a draft.", entity_type="Purchase Request", entity_id=req_id, importance="Normal", channels=["in_app"], action_label="Open Purchase Requests")
        _rerun_success(f"Created {req_no}")


def request_next_action_board():
    st.caption("This is the simple command board for deciding what happens next. It avoids forcing users to understand every status in the status filter.")
    cards = [
        ("Submit drafts", "Draft", "Submit", "Submitted", "Submitted", "Request submitted for procurement review"),
        ("Start procurement review", "Submitted", "Start Review", "Procurement Review", "Reviewed", "Procurement review started"),
        ("Needs sourcing / supplier quotes", "Procurement Review", "Mark Requires Sourcing", "Requires Sourcing", "Sourcing Required", "Supplier comparison required"),
        ("Send to Approver/MD", "Vendor Recommendation", "Send to MD", "Pending Approver/MD Approval", "Sent for Approval", "Awaiting MD approval"),
        ("Approved requests needing PO", "Approved", "Open PO Creation", "Approved", "PO Action", "Create purchase order for approved request"),
    ]
    selected_action = st.selectbox("Action board", [c[0] for c in cards], key="pr_action_board_choice")
    title, status, btn, new_status, event, note = [c for c in cards if c[0] == selected_action][0]
    df = df_query("SELECT id, request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE status=? ORDER BY updated_at DESC", (status,))
    if df.empty:
        st.success(f"No request currently needs: {title.lower()}.")
        return
    show = df.copy(); show["estimated_amount"] = show["estimated_amount"].apply(money); dataframe(show.drop(columns=["id"]))
    chosen = st.selectbox("Select request", [f"{r.request_no} — {money(r.estimated_amount)} — #{int(r.id)}" for r in df.itertuples()], key="pr_action_board_request")
    pr_id = int(chosen.rsplit("#", 1)[1])
    if status == "Approved":
        st.info("Approved requests are ready for Purchase Order creation. Click below to go to the Purchase Orders section.")
        if st.button(btn, type="primary", key="pr_open_po_creation"):
            st.session_state["procurement_section"] = "Purchase Orders"
            st.rerun()
        return
    if st.button(btn, type="primary", key="pr_apply_guided_action"):
        update_request_status(pr_id, new_status, event, note)
        if new_status == "Requires Sourcing":
            create_sourcing_for_request(pr_id)
        if new_status == "Pending Approver/MD Approval":
            create_notification(None, "Approver", "Request pending approval", f"Request requires approval", "Purchase Request", pr_id, "High", ["in_app", "browser_push"], action_label="Open Pending Approvals")
        st.rerun()


def request_register(actions=True, approver_mode=False):
    st.caption("Use the filters to find records. Use the Guided Next Actions board when you want to move a request to sourcing, approval or PO creation.")
    c1, c2, c3 = st.columns(3)
    status = c1.selectbox("Filter status only — not an action", ["All"] + PR_STATUSES, key=f"status_phase4_{approver_mode}")
    dept = c2.selectbox("Department", ["All"] + department_options(), key=f"dept_phase4_{approver_mode}")
    term = c3.text_input("Search", key=f"req_search_phase4_{approver_mode}")
    sql = """
        SELECT pr.id, pr.request_no, pr.department_project, pr.category, pr.priority, pr.estimated_amount, pr.status, pr.source_type, pr.import_confidence, u.full_name requested_by, pr.justification
        FROM purchase_requests pr LEFT JOIN users u ON pr.requested_by=u.id WHERE 1=1
    """
    params = []
    if status != "All": sql += " AND pr.status=?"; params.append(status)
    if dept != "All": sql += " AND pr.department_project=?"; params.append(dept)
    if term:
        sql += " AND (pr.request_no LIKE ? OR pr.justification LIKE ? OR pr.category LIKE ?)"; params += [f"%{term}%"]*3
    sql += " ORDER BY pr.updated_at DESC, pr.created_at DESC"
    df = df_query(sql, params)
    if df.empty:
        empty_state("No purchase requests", "Create or import purchase requests to begin workflow.")
        return
    display = df.drop(columns=["id", "justification"]).copy()
    display["estimated_amount"] = display["estimated_amount"].apply(money)
    display["status"] = display["status"].apply(lambda x: badge(x))
    st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    selected = st.selectbox("Open request", df["request_no"].tolist(), key=f"open_req_phase4_{approver_mode}")
    pr_id = int(df[df["request_no"] == selected].iloc[0]["id"])
    request_detail(pr_id, actions=actions, key_scope=f"request_register_phase4_{approver_mode}")
    csv_download(_redact_ui_df(df, "purchase_requests"), "purchase_requests")


def request_actions(pr_id: int, pr, key_scope: str | None = None):
    scope = key_scope or "default"
    prefix = f"{scope}_pr_{pr_id}"
    st.markdown("#### Guided next action")
    st.caption("Only actions allowed for the current status and your role are shown. This is easier than manually changing statuses.")
    actions = []
    if pr["status"] == "Draft" and has_permission("submit_request"):
        actions.append(("Submit to Procurement Review", "Submitted", "Submitted", "Request submitted for procurement review"))
    if pr["status"] == "Submitted" and has_permission("procurement_review"):
        actions.append(("Start Procurement Review", "Procurement Review", "Reviewed", "Procurement review started"))
    if pr["status"] in ["Submitted", "Procurement Review"] and has_permission("create_sourcing"):
        actions.append(("Requires Sourcing / Vendor Quotes", "Requires Sourcing", "Sourcing Required", "Supplier comparison required"))
    if pr["status"] in ["Submitted", "Procurement Review", "Vendor Recommendation"] and has_permission("procurement_review"):
        actions.append(("Send to Approver/MD", "Pending Approver/MD Approval", "Sent for Approval", "Awaiting MD approval"))
    if pr["status"] in ["Pending Approver/MD Approval", "Pending Approval"] and has_permission("approve_request"):
        actions.append(("Approve Request", "Approved", "Approved", "Approved"))
        actions.append(("Reject Request", "Rejected", "Rejected", "Rejected"))
    if pr["status"] == "Approved" and has_permission("create_po"):
        st.success("This request is approved and ready for a Purchase Order.")
        if st.button("Go to Purchase Orders", key=f"go_po_{prefix}"):
            st.session_state["procurement_section"] = "Purchase Orders"
            st.rerun()
    if not actions:
        st.info("No direct action is available for this request at its current status or your role.")
        return
    action_labels = [a[0] for a in actions]
    chosen = st.selectbox("Choose next action", action_labels, key=f"guided_action_{prefix}")
    reason = ""
    if "Reject" in chosen:
        reason = st.text_input("Reject/request more information reason", key=f"reason_phase4_{prefix}")
    if st.button("Apply selected action", type="primary", key=f"apply_action_{prefix}"):
        label, new_status, event, note = [a for a in actions if a[0] == chosen][0]
        if new_status == "Rejected" and not reason.strip():
            st.error("Please enter a rejection reason.")
            return
        if new_status in ["Approved", "Rejected"]:
            approval_action("Purchase Request", pr_id, pr["status"], new_status, event, reason or note)
        else:
            update_request_status(pr_id, new_status, event, note)
            if new_status == "Requires Sourcing":
                create_sourcing_for_request(pr_id)
            if new_status == "Pending Approver/MD Approval":
                create_notification(None, "Approver", "Request pending approval", f"{pr['request_no']} requires approval", "Purchase Request", pr_id, "High", ["in_app", "browser_push"], action_label="Open Pending Approvals")
            st.rerun()


def receipts_page():
    _ensure_finance_doc_schema_ui()
    st.subheader("Receipts")
    st.caption("Receipts are proof that payment happened. Choose the payment method first; the form below changes immediately for cash, transfer, card, POS, cheque or mobile money evidence.")
    section = st.radio("Receipt sections", ["Record Receipt", "Receipt Register", "OCR Attempts"], horizontal=True, key="receipt_sections_phase4")
    if section == "Record Receipt":
        uploaded, parsed = _ocr_upload_panel("receipt_ocr_upload_phase4", "receipt")
        fields = parsed.get("fields", {}) if parsed else {}
        receipt_details = parsed.get("receipt_details", {}) if parsed else {}
        bank_details = parsed.get("bank_details", {}) if parsed else {}
        vendors = vendor_options(True)
        vendor_name = fields.get("matched_vendor_name") or "No vendor selected"
        vendor_index = list(vendors.keys()).index(vendor_name) if vendor_name in vendors else 0
        detected_method = fields.get("payment_method") if fields.get("payment_method") in RECEIPT_PAYMENT_METHODS else "Cash"
        receipt_type_options = ["Payment Receipt", "Cash Receipt", "Transfer Receipt", "Card Receipt", "POS Receipt", "Cheque Receipt", "Mobile Money Receipt", "Refund Receipt", "Other"]
        receipt_type = selectbox_with_other("Receipt type", receipt_type_options, "receipt_type_phase4", "receipt_type")
        method_index = RECEIPT_PAYMENT_METHODS.index(detected_method) if detected_method in RECEIPT_PAYMENT_METHODS else 0
        payment_method = selectbox_with_other("Payment method", RECEIPT_PAYMENT_METHODS + ["Other"], "receipt_method_phase4", "payment_method", index=method_index)
        default_pay_date = _parse_date_value(fields.get("date"), date.today())
        with st.form("receipt_record_form_phase4"):
            c1, c2, c3 = st.columns(3)
            payment_date = c1.date_input("Payment date", default_pay_date, key="receipt_payment_date_phase4")
            receipt_no = c2.text_input("Receipt / transaction reference", value=fields.get("receipt_no") or "", key="receipt_no_phase4")
            vendor_label = c3.selectbox("Vendor / Payee", list(vendors.keys()), index=vendor_index, key="receipt_vendor_phase4")
            c4, c5, c6, c7 = st.columns(4)
            amount = c4.number_input("Amount paid", min_value=0.0, value=float(fields.get("total_amount") or 0), step=1000.0, key="receipt_amount_phase4")
            tax = c5.number_input("VAT/Tax included", min_value=0.0, value=float(fields.get("tax_amount") or 0), step=100.0, key="receipt_tax_phase4")
            dept = c6.selectbox("Department / Project", department_options(), key="receipt_dept_phase4")
            currency = selectbox_with_other("Currency", ["NGN", "USD", "EUR", "GBP", "Other"], "receipt_currency_phase4", "currency")
            purpose = st.text_area("Purpose / what was paid for", value=fields.get("description") or "", key="receipt_purpose_phase4")
            method_data = {}
            st.markdown(f"##### {payment_method} receipt evidence")
            if payment_method == "Cash":
                c1, c2, c3 = st.columns(3)
                method_data["cash_received_by"] = c1.text_input("Cash received by", key="cash_received_by_phase4")
                method_data["cash_collected_from"] = c2.text_input("Cash collected from", value=user()["full_name"], key="cash_collected_from_phase4")
                method_data["cash_denominations"] = c3.text_input("Denomination breakdown", placeholder="e.g. 10x ₦1,000 + 5x ₦500", key="cash_denominations_phase4")
            elif payment_method == "Bank Transfer":
                c1, c2, c3 = st.columns(3)
                method_data["bank_name"] = c1.text_input("Receiving bank", value=bank_details.get("bank_name") or "", key="transfer_bank_phase4")
                method_data["account_number"] = c2.text_input("Receiving account number", value=bank_details.get("account_no") or "", key="transfer_acct_phase4")
                method_data["transfer_reference"] = c3.text_input("Transfer/session reference", value=bank_details.get("transfer_reference") or fields.get("receipt_no") or "", key="transfer_ref_phase4")
                c4, c5 = st.columns(2)
                method_data["sender_bank"] = c4.text_input("Sender bank", key="transfer_sender_bank_phase4")
                method_data["receiver_bank"] = c5.text_input("Receiver bank", key="transfer_receiver_bank_phase4")
            elif payment_method in ["Card", "POS/Card"] or "card" in payment_method.lower() or "pos" in payment_method.lower():
                c1, c2, c3, c4 = st.columns(4)
                method_data["card_type"] = selectbox_with_other("Card type", ["Unknown", "Visa", "Mastercard", "Verve", "Other"], "card_type_phase4", "card_type")
                method_data["masked_card_number"] = c2.text_input("Masked card number", placeholder="**** **** **** 1234", key="masked_card_phase4")
                method_data["card_auth_code"] = c3.text_input("Auth/approval code", value=receipt_details.get("auth_code") or "", key="card_auth_phase4")
                method_data["pos_rrn"] = c4.text_input("RRN/STAN", value=receipt_details.get("rrn") or "", key="pos_rrn_phase4")
                c5, c6 = st.columns(2)
                method_data["pos_terminal_id"] = c5.text_input("POS terminal ID", value=receipt_details.get("terminal_id") or "", key="pos_tid_phase4")
                method_data["pos_merchant_id"] = c6.text_input("Merchant ID", key="pos_mid_phase4")
            elif payment_method == "Cheque":
                c1, c2, c3 = st.columns(3)
                method_data["cheque_number"] = c1.text_input("Cheque number", key="cheque_number_phase4")
                method_data["cheque_bank"] = c2.text_input("Cheque bank", key="cheque_bank_phase4")
                method_data["cheque_due_date"] = c3.date_input("Cheque date", default_pay_date, key="cheque_due_date_phase4").isoformat()
            elif payment_method == "Mobile Money":
                c1, c2 = st.columns(2)
                method_data["mobile_wallet_provider"] = c1.text_input("Wallet/provider", placeholder="Opay, Moniepoint, Paga, etc.", key="mobile_provider_phase4")
                method_data["mobile_transaction_id"] = c2.text_input("Mobile transaction ID", value=fields.get("receipt_no") or "", key="mobile_txn_phase4")
            else:
                method_data["other_payment_details"] = st.text_area("Describe payment evidence", key="other_payment_details_phase4")
            notes = st.text_area("Finance notes", key="receipt_notes_phase4")
            submitted = st.form_submit_button("Save Receipt", type="primary")
        if submitted:
            if amount <= 0:
                st.error("Receipt amount must be greater than zero."); return
            _save_custom_value("receipt_type", receipt_type); _save_custom_value("payment_method", payment_method)
            path, fhash = save_upload(uploaded, "receipts") if uploaded else (None, None)
            try:
                from core.ocr import duplicate_receipt_candidates
                dup = duplicate_receipt_candidates(fhash, amount, payment_date.isoformat(), vendors[vendor_label])
            except Exception:
                dup = pd.DataFrame()
            receipt_ref = receipt_no.strip() or make_ref("RCT")
            rid = run_insert("""
                INSERT INTO receipt_records (receipt_no, receipt_type, payment_method, payment_date, vendor_id, payer_name, payee_name, amount, tax_amount, currency, purpose, department_project,
                    cash_received_by, cash_collected_from, cash_denominations, bank_name, account_number, transfer_reference, sender_bank, receiver_bank, card_type, masked_card_number,
                    card_auth_code, pos_terminal_id, pos_merchant_id, pos_rrn, cheque_number, cheque_bank, cheque_due_date, mobile_wallet_provider, mobile_transaction_id,
                    status, file_path, file_hash, ocr_text, ocr_json, duplicate_warning, notes, uploaded_by, created_at, updated_at, detected_document_type, ocr_detected_date, interface_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Recorded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (receipt_ref, receipt_type, payment_method, payment_date.isoformat(), vendors[vendor_label], user()["full_name"], vendor_label if vendor_label != "No vendor selected" else "", amount, tax, currency, purpose, dept,
                 method_data.get("cash_received_by"), method_data.get("cash_collected_from"), method_data.get("cash_denominations"), method_data.get("bank_name"), method_data.get("account_number"), method_data.get("transfer_reference"), method_data.get("sender_bank"), method_data.get("receiver_bank"), method_data.get("card_type"), method_data.get("masked_card_number"), method_data.get("card_auth_code"), method_data.get("pos_terminal_id"), method_data.get("pos_merchant_id"), method_data.get("pos_rrn"), method_data.get("cheque_number"), method_data.get("cheque_bank"), method_data.get("cheque_due_date"), method_data.get("mobile_wallet_provider"), method_data.get("mobile_transaction_id"), path, fhash, parsed.get("raw_text", "") if parsed else "", json_dump(parsed) if parsed else "{}", 0 if dup.empty else 1, notes, user()["id"], now_iso(), now_iso(), fields.get("document_type"), fields.get("date"), payment_method))
            _save_ocr_attempt("Receipt", rid, parsed or {})
            exp_no = make_ref("EXP")
            run_insert("""INSERT INTO expenses (expense_no, expense_date, category, description, vendor_id, amount, payment_method, project_department, status, receipt_path, receipt_hash, receipt_no, tax_amount, duplicate_warning, requested_by, ocr_text, ocr_json, document_kind, receipt_id, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Approved', ?, ?, ?, ?, ?, ?, ?, ?, 'Receipt', ?, ?, ?)""", (exp_no, payment_date.isoformat(), fields.get("category") or "Other", purpose or receipt_type, vendors[vendor_label], amount, payment_method, dept, path, fhash, receipt_ref, tax, 0 if dup.empty else 1, user()["id"], parsed.get("raw_text", "") if parsed else "", json_dump(parsed) if parsed else "{}", rid, notes, now_iso()))
            add_workflow_event("Receipt", rid, "Receipt Recorded", "Recorded", f"{payment_method} receipt", user()["id"])
            log_audit("RECEIPT_RECORDED", "Receipt", rid, {"payment_method": payment_method, "amount": amount, "payment_date": payment_date.isoformat()}, user()["id"], user()["role"])
            st.success(f"Receipt saved: {receipt_ref}")
            if not dup.empty: st.warning("Possible duplicate receipt detected.")
            st.rerun()
    elif section == "Receipt Register":
        df = df_query("""
            SELECT rr.id, rr.receipt_no, rr.receipt_type, rr.payment_method, rr.payment_date, v.name vendor, rr.amount, rr.status, rr.department_project, rr.duplicate_warning, rr.created_at
            FROM receipt_records rr LEFT JOIN vendors v ON rr.vendor_id=v.id ORDER BY rr.created_at DESC
        """)
        if df.empty:
            empty_state("No receipts", "Record cash, transfer, card/POS, cheque or mobile-money receipts here."); return
        show = df.drop(columns=["id"]).copy(); show["amount"] = show["amount"].apply(money); dataframe(show)
        selected = st.selectbox("Select receipt", [f"{r.receipt_no} — {r.payment_method} — #{int(r.id)}" for r in df.itertuples()], key="receipt_select_phase4")
        rid = int(selected.rsplit("#", 1)[1])
        detail = df_query("SELECT * FROM receipt_records WHERE id=?", (rid,))
        dataframe(_redact_ui_df(detail, "receipt_records")) if not detail.empty else None
        csv_download(show, "receipts")
    else:
        attempts = df_query("SELECT * FROM document_ocr_attempts WHERE document_type='Receipt' ORDER BY created_at DESC LIMIT 300")
        dataframe(_redact_ui_df(attempts, "document_ocr_attempts")) if not attempts.empty else st.info("No receipt OCR attempts yet.")


def invoices_page():
    _ensure_finance_doc_schema_ui()
    st.subheader("Invoices")
    st.caption("Invoices are payment requests before payment. The invoice type selector changes the fields and review hints immediately.")
    section = st.radio("Invoice sections", ["Upload / Record Invoice", "Invoice Register", "Invoice Items", "OCR Attempts"], horizontal=True, key="invoice_sections_phase4")
    vendors = vendor_options(True)
    if section == "Upload / Record Invoice":
        uploaded, parsed = _ocr_upload_panel("invoice_ocr_upload_phase4", "invoice")
        fields = parsed.get("fields", {}) if parsed else {}
        vendor_name = fields.get("matched_vendor_name") or "No vendor selected"
        vendor_index = list(vendors.keys()).index(vendor_name) if vendor_name in vendors else 0
        invoice_type = selectbox_with_other("Invoice type", INVOICE_TYPES + ["Other"], "invoice_type_phase4", "invoice_type")
        po_df = df_query("SELECT po.id, po.po_no, v.name vendor, po.total_amount FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id ORDER BY po.created_at DESC")
        po_options = ["No PO selected"] + [f"{r.po_no} — {r.vendor} — {money(r.total_amount)}" for r in po_df.itertuples()]
        default_invoice_date = _parse_date_value(fields.get("date"), date.today())
        default_due_date = _parse_date_value(fields.get("due_date"), default_invoice_date + timedelta(days=7))
        with st.form("invoice_record_form_phase4"):
            c1, c2, c3 = st.columns(3)
            invoice_no = c1.text_input("Invoice number", value=fields.get("invoice_no") or "", key="invoice_number_phase4")
            invoice_date = c2.date_input("Invoice date", default_invoice_date, key="invoice_date_phase4")
            due_date = c3.date_input("Due date", default_due_date, key="invoice_due_date_phase4")
            c4, c5 = st.columns(2)
            vendor_label = c4.selectbox("Vendor", list(vendors.keys()), index=vendor_index, key="invoice_vendor_phase4")
            po_label = c5.selectbox("Match Purchase Order", po_options, key="invoice_po_match_phase4")
            if invoice_type == "Tax Invoice":
                ctax1, ctax2 = st.columns(2)
                tax_id = ctax1.text_input("Supplier VAT/TIN", key="tax_invoice_tin_phase4")
                tax_note = ctax2.text_input("Tax breakdown note", key="tax_invoice_note_phase4")
            elif invoice_type == "Service Invoice":
                cserv1, cserv2 = st.columns(2)
                service_period = cserv1.text_input("Service period", placeholder="e.g. May 2026", key="service_period_phase4")
                service_owner = cserv2.text_input("Service owner/department", key="service_owner_phase4")
            elif invoice_type == "Recurring Invoice":
                rec_freq = selectbox_with_other("Recurring frequency", ["Monthly", "Quarterly", "Annually", "Milestone", "Other"], "recurring_freq_phase4", "recurring_frequency")
            elif invoice_type in ["Credit Note", "Debit Note"]:
                adjustment_reason = st.text_area("Adjustment reason", key="invoice_adjustment_reason_phase4")
            c7, c8, c9, c10 = st.columns(4)
            subtotal = c7.number_input("Subtotal", min_value=0.0, value=float(fields.get("subtotal") or 0), step=1000.0, key="invoice_subtotal_phase4")
            tax = c8.number_input("VAT/Tax", min_value=0.0, value=float(fields.get("tax_amount") or 0), step=100.0, key="invoice_tax_phase4")
            discount = c9.number_input("Discount", min_value=0.0, value=0.0, step=100.0, key="invoice_discount_phase4")
            total = c10.number_input("Total / Amount Due", min_value=0.0, value=float(fields.get("total_amount") or 0), step=1000.0, key="invoice_total_phase4")
            terms = selectbox_with_other("Payment terms", ["Due on Receipt", "Net 7", "Net 15", "Net 30", "Milestone", "Advance Payment", "Other"], "invoice_terms_phase4", "payment_terms")
            desc = st.text_area("Invoice description / scope", value=fields.get("description") or "", key="invoice_desc_phase4")
            submitted = st.form_submit_button("Save Invoice for Review", type="primary")
        if submitted:
            if not invoice_no.strip(): st.error("Invoice number is required."); return
            if total <= 0: st.error("Invoice total must be greater than zero."); return
            _save_custom_value("invoice_type", invoice_type); _save_custom_value("payment_terms", terms)
            path, fhash = save_upload(uploaded, "invoices") if uploaded else (None, None)
            po_id = None
            if po_label != "No PO selected":
                po_id = int(po_df[po_df["po_no"] == po_label.split(" — ")[0]].iloc[0]["id"])
            vendor_id = vendors[vendor_label]
            match_status, mismatch = match_invoice_to_po(po_id, vendor_id, total)
            inv_id = run_insert("""
                INSERT INTO invoices (invoice_no, receipt_no, po_id, vendor_id, invoice_date, amount, tax_amount, total_amount, file_path, file_hash, ocr_text, ocr_json, match_status, mismatch_reasons, status, uploaded_by, created_at,
                    invoice_type, document_stage, supplier_invoice_no, due_date, payment_terms, subtotal, discount_amount, balance_due, approval_status, detected_document_type, ocr_detected_date, interface_mode)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Uploaded', ?, ?, ?, 'Invoice', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (invoice_no.strip(), po_id, vendor_id, invoice_date.isoformat(), max(total - tax, 0), tax, total, path, fhash, parsed.get("raw_text", "") if parsed else "", json_dump(parsed) if parsed else "{}", match_status, "; ".join(mismatch), user()["id"], now_iso(), invoice_type, invoice_no.strip(), due_date.isoformat(), terms, subtotal or max(total - tax, 0), discount, total, match_status, fields.get("document_type"), fields.get("date"), invoice_type))
            for item in (parsed.get("line_items", []) if parsed else []):
                run_query("INSERT INTO invoice_items (invoice_id, item_description, quantity, unit_price, tax_amount, total, category, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)", (inv_id, item.get("item_name"), item.get("quantity") or 1, item.get("unit_price") or 0, item.get("total") or 0, fields.get("category") or "Other", now_iso()))
            _save_ocr_attempt("Invoice", inv_id, parsed or {})
            add_workflow_event("Invoice", inv_id, "Invoice Uploaded", "Uploaded", match_status, user()["id"])
            create_notification(None, "Finance", "Invoice needs review", f"Invoice {invoice_no} match status: {match_status}", "Invoice", inv_id, "High", ["in_app", "browser_push"], action_label="Open Invoices")
            log_audit("INVOICE_RECORDED", "Invoice", inv_id, {"invoice_no": invoice_no, "total": total, "invoice_date": invoice_date.isoformat(), "match_status": match_status}, user()["id"], user()["role"])
            _rerun_success(f"Invoice {invoice_no} saved for Finance review.")
    elif section == "Invoice Register":
        df = df_query("""
            SELECT inv.id, inv.invoice_no, inv.invoice_type, po.po_no, v.name vendor, inv.invoice_date, inv.due_date, inv.total_amount, inv.balance_due, inv.match_status, inv.mismatch_reasons, inv.status
            FROM invoices inv LEFT JOIN purchase_orders po ON inv.po_id=po.id LEFT JOIN vendors v ON inv.vendor_id=v.id ORDER BY inv.created_at DESC
        """)
        if df.empty:
            empty_state("No invoices", "Upload supplier invoices here. Receipts are recorded separately."); return
        show = df.drop(columns=["id"]).copy(); show["total_amount"] = show["total_amount"].apply(money); show["balance_due"] = show["balance_due"].apply(money); dataframe(show)
        selected = st.selectbox("Select invoice", [f"{r.invoice_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="invoice_select_phase4")
        inv_id = int(selected.rsplit("#", 1)[1])
        inv = df_query("SELECT * FROM invoices WHERE id=?", (inv_id,)).iloc[0]
        dataframe(_redact_ui_df(pd.DataFrame([inv.to_dict()]), "invoices"))
        with st.expander("OCR / mismatch details", expanded=False):
            try: st.json(json.loads(inv.get("ocr_json") or "{}"))
            except Exception: st.text(inv.get("ocr_text") or "")
            st.write(inv.get("mismatch_reasons") or "")
        if has_permission("review_invoice"):
            c1, c2, c3 = st.columns(3)
            if c1.button("Mark Finance Review Complete", key=f"invoice_reviewed_phase4_{inv_id}"):
                run_query("UPDATE invoices SET status='Finance Review', approval_status='Reviewed' WHERE id=?", (inv_id,)); add_workflow_event("Invoice", inv_id, "Finance Review", "Finance Review", "Invoice reviewed", user()["id"]); st.rerun()
            if c2.button("Create Payment Request", key=f"invoice_payment_phase4_{inv_id}"):
                pno = make_ref("PAY"); pay_id = run_insert("INSERT INTO payments (payment_no, invoice_id, po_id, vendor_id, amount, payment_method, status, created_by, created_at, updated_at) SELECT ?, id, po_id, vendor_id, total_amount, 'Bank Transfer', 'Pending Approval', ?, ?, ? FROM invoices WHERE id=?", (pno, user()["id"], now_iso(), now_iso(), inv_id)); create_notification(None, "Approver", "Payment pending approval", f"{pno} requires approval", "Payment", pay_id, "High", ["in_app", "browser_push"]); add_workflow_event("Payment", pay_id, "Created from Invoice", "Pending Approval", pno, user()["id"]); st.success(f"Payment request {pno} created.")
            if c3.button("Return Invoice", key=f"invoice_return_phase4_{inv_id}"):
                run_query("UPDATE invoices SET status='Returned', approval_status='Returned' WHERE id=?", (inv_id,)); add_workflow_event("Invoice", inv_id, "Returned", "Returned", "Invoice returned for clarification", user()["id"]); st.rerun()
        csv_download(show, "invoices")
    elif section == "Invoice Items":
        items = df_query("SELECT ii.*, inv.invoice_no FROM invoice_items ii LEFT JOIN invoices inv ON inv.id=ii.invoice_id ORDER BY ii.created_at DESC LIMIT 1000")
        dataframe(items) if not items.empty else st.info("No invoice item lines captured yet.")
    else:
        attempts = df_query("SELECT * FROM document_ocr_attempts WHERE document_type='Invoice' ORDER BY created_at DESC LIMIT 300")
        dataframe(_redact_ui_df(attempts, "document_ocr_attempts")) if not attempts.empty else st.info("No invoice OCR attempts yet.")

# ============================================================================
# Command-chain hardening overrides
# These final definitions intentionally override earlier MVP/phase functions.
# ============================================================================
from io import BytesIO
import csv
from core.permissions import (
    display_role, can_approve, can_pay, can_create_payment_request,
    can_delete_draft, can_edit_own_draft, is_read_only,
)
from core.report_service import build_excel_workbook, excel_mime
from core.workflow import normalize_status, next_role_for_status
from core.ui import interactive_chart, format_kpi_value

# Business-approved statuses. Legacy values remain accepted by queries and migrations.
PR_STATUSES = [
    "Draft", "FM Draft", "Sent for Procurement Review", "Submitted to Procurement Manager",
    "Returned for Correction", "Returned to Facility Manager", "Reviewed by Procurement",
    "Procurement Review", "Requires Sourcing", "Vendor Quote Collection", "Vendor Recommendation",
    "Submitted for Approval", "Pending Approver/MD Approval", "Approved", "Rejected",
    "Awaiting Payment", "Approved for Payment", "Paid", "Receipt Uploaded",
    "Payment Submitted for Verification", "Completed", "Closed", "Archived",
]
GATEWAY_DEPARTMENTS = ["CMOTD", "RACAM"]


def _request_workflow_steps_for_status(status: str) -> list[str]:
    """Keep the status rail focused on the user's actionable chain.

    Facility/Utility users should not see confusing post-payment steps they
    cannot action. Procurement Manager/Admin/Auditor can see the final
    Paid -> Completed -> Closed -> Archived chain.
    """
    base = ["Draft", "Sent for Procurement Review", "Reviewed by Procurement", "Submitted for Approval", "Approved", "Awaiting Payment", "Paid"]
    closure = ["Completed", "Closed", "Archived"]
    role = _current_role() if "user" in globals() and "user" in st.session_state else ""
    if role in ["Procurement Manager", "Admin", "Auditor"] or status in ["Completed", "Closed", "Archived"]:
        return base + closure
    return base


def _current_role() -> str:
    return str(user().get("role", ""))


def _is_utility() -> bool:
    return _current_role() == "Facility Manager"


def _next_role_for_status(status: str) -> str | None:
    return {
        "Sent for Procurement Review": "procurement_manager",
        "Submitted to Procurement Manager": "procurement_manager",
        "Submitted": "procurement_manager",
        "Procurement Review": "procurement_manager",
        "Reviewed by Procurement": "procurement_manager",
        "Requires Sourcing": "procurement_manager",
        "Vendor Quote Collection": "procurement_manager",
        "Vendor Recommendation": "procurement_manager",
        "Submitted for Approval": "approver",
        "Pending Approver/MD Approval": "approver",
        "Pending Approval": "approver",
        "Approved": "finance",
        "Awaiting Payment": "finance",
        "Approved for Payment": "finance",
        # After Finance records payment/receipt, Procurement Manager owns
        # final operational closure: Completed -> Closed -> Archived.
        "Paid": "procurement_manager",
        "Receipt Uploaded": "procurement_manager",
        "Payment Submitted for Verification": "procurement_manager",
        "Completed": "procurement_manager",
        "Closed": "procurement_manager",
        "Archived": "auditor",
    }.get(status)


def _set_next_role(entity_table: str, entity_id: int, status: str):
    next_role = _next_role_for_status(status)
    try:
        if next_role and "next_role" in __import__("core.db", fromlist=["table_columns"]).table_columns(entity_table):
            run_query(f"UPDATE {entity_table} SET next_role=? WHERE id=?", (next_role, entity_id))
        elif "next_role" in __import__("core.db", fromlist=["table_columns"]).table_columns(entity_table):
            run_query(f"UPDATE {entity_table} SET next_role=NULL WHERE id=?", (entity_id,))
    except Exception:
        pass


def _normalise_report_name(name: str) -> str:
    return str(name or "report").replace(" ", "_").replace("/", "_").replace("\\", "_").lower()


def _csv_bytes_from_sheets(sheets: dict[str, pd.DataFrame]) -> bytes:
    frames = []
    for sheet_name, df in (sheets or {}).items():
        data = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
        if data.empty:
            data = pd.DataFrame([{"message": "No records"}])
        data.insert(0, "sheet", str(sheet_name))
        frames.append(data)
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame([{"message": "No records"}])
    return combined.to_csv(index=False).encode("utf-8-sig")


def _pdf_bytes_from_sheets(sheets: dict[str, pd.DataFrame], title: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), rightMargin=10*mm, leftMargin=10*mm, topMargin=10*mm, bottomMargin=10*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("pf_pdf_title", parent=styles["Title"], fontSize=14, leading=16, spaceAfter=8)
    heading_style = ParagraphStyle("pf_pdf_heading", parent=styles["Heading2"], fontSize=11, leading=13, spaceBefore=8, spaceAfter=5)
    small = ParagraphStyle("pf_pdf_small", parent=styles["Normal"], fontSize=6.8, leading=8)
    story = [Paragraph(str(title).replace("_", " ").title(), title_style)]
    items = list((sheets or {}).items()) or [("Detailed Records", pd.DataFrame())]
    for sheet_idx, (sheet_name, df) in enumerate(items):
        if sheet_idx:
            story.append(PageBreak())
        data = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
        story.append(Paragraph(str(sheet_name), heading_style))
        if data.empty:
            story.append(Paragraph("No records available.", small))
            continue
        source_rows, source_cols = len(data), len(data.columns)
        data = data.fillna("").astype(str).iloc[:60, :8]
        table_data = [[Paragraph(str(c), small) for c in data.columns]]
        for row in data.itertuples(index=False):
            table_data.append([Paragraph(str(v), small) for v in row])
        width = 277 * mm
        col_count = max(1, len(table_data[0]))
        tbl = Table(table_data, colWidths=[width / col_count] * col_count, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("GRID", (0,0), (-1,-1), .25, colors.lightgrey),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e2e8f0")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("PADDING", (0,0), (-1,-1), 3),
        ]))
        story.append(tbl)
        if source_rows > 60 or source_cols > 8:
            story.append(Spacer(1, 5))
            story.append(Paragraph("PDF preview is limited for readability. Use Excel or CSV for full details.", small))
    doc.build(story)
    return out.getvalue()


def report_download_buttons(sheets: dict[str, pd.DataFrame], name: str, key: str):
    """Render Excel, PDF and CSV downloads for every role/report surface.

    The payload is generated only for the selected format, so opening tabs stays
    fast even when the report tables are large.
    """
    if not sheets:
        return
    clean_name = _normalise_report_name(name)
    safe_sheets: dict[str, pd.DataFrame] = {}
    for sheet_name, df in sheets.items():
        data = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
        safe_sheets[str(sheet_name)] = _redact_ui_df(data, clean_name) if "_redact_ui_df" in globals() else data
    format_choice = st.selectbox(
        f"Download format for {name.replace('_', ' ').title()}",
        ["Excel (.xlsx)", "PDF (.pdf)", "CSV (.csv)"],
        key=f"{key}_format",
        label_visibility="collapsed",
    )
    if format_choice.startswith("Excel"):
        payload = build_excel_workbook(safe_sheets, clean_name)
        mime = excel_mime(); ext = "xlsx"; action = "EXCEL_REPORT_DOWNLOADED"
    elif format_choice.startswith("PDF"):
        payload = _pdf_bytes_from_sheets(safe_sheets, clean_name)
        mime = "application/pdf"; ext = "pdf"; action = "PDF_REPORT_DOWNLOADED"
    else:
        payload = _csv_bytes_from_sheets(safe_sheets)
        mime = "text/csv"; ext = "csv"; action = "CSV_REPORT_DOWNLOADED"
    if st.download_button(f"Download {name.replace('_', ' ').title()} {ext.upper()}", payload, f"{clean_name}.{ext}", mime, key=f"{key}_{ext}"):
        log_audit(action, "Report", clean_name, f"Downloaded {clean_name}.{ext}", user().get("id"), user().get("role"))


def csv_download(df: pd.DataFrame, name: str):
    """Backwards-compatible helper now offering Excel, PDF and CSV."""
    if df is None or df.empty:
        return
    safe_key = f"download_{_normalise_report_name(name)}_{abs(hash(tuple(df.columns))) % 10_000_000}"
    report_download_buttons({"Detailed Records": df}, name, safe_key)


def _excel_download_button(label: str, filename: str, sheets: dict[str, pd.DataFrame], key: str):
    """Backwards-compatible helper now offering Excel, PDF and CSV."""
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    title = label.replace("Download ", "").replace(" Excel", "").replace(" Workbook", "") or base
    report_download_buttons(sheets, title, key)
    return False


def _status_values_for_queue(kind: str) -> tuple[str, ...]:
    return {
        "procurement": ("Sent for Procurement Review", "Submitted to Procurement Manager", "Submitted"),
        "approval": ("Submitted for Approval", "Pending Approver/MD Approval", "Pending Approval"),
        "finance": ("Approved", "Awaiting Payment", "Approved for Payment"),
        "receipt": ("Paid",),
        "completed": ("Paid", "Receipt Uploaded", "Payment Submitted for Verification", "Completed", "Closed"),
    }.get(kind, tuple())


def update_request_status(pr_id: int, status: str, event: str, note: str):
    rows = df_query("SELECT * FROM purchase_requests WHERE id=?", (pr_id,))
    if rows.empty:
        st.error("Request not found.")
        return
    old = rows.iloc[0]
    role = _current_role()
    # Guard final approval/rejection centrally.
    if status in ["Approved", "Rejected"] and not can_approve(role):
        st.error("Only Admin and Approver / MD can approve or reject requests.")
        return
    # Finance cannot move records backward into approval or create payment requests.
    if role == "Finance" and status in ["Submitted for Approval", "Pending Approval", "Pending Approver/MD Approval", "Approved", "Rejected"]:
        st.error("Finance cannot approve, reject, or submit items for approval.")
        return
    next_role = _next_role_for_status(status)
    payment_status = None
    if status in ["Approved", "Awaiting Payment", "Approved for Payment"]:
        payment_status = "Approved for Payment"
    elif status in ["Paid", "Completed", "Receipt Uploaded"]:
        payment_status = "Paid"
    transition_request_status(pr_id, status, event, note, user()["id"], role, payment_status=payment_status)
    try:
        extras = []
        params: list[Any] = []
        if next_role is not None:
            extras.append("next_role=?"); params.append(next_role)
        elif status in ["Rejected", "Archived"]:
            extras.append("next_role=NULL")
        if status in ["Sent for Procurement Review", "Submitted to Procurement Manager"]:
            extras.append("submitted_at=COALESCE(submitted_at, ?)"); params.append(now_iso())
        if status == "Approved":
            extras.extend(["approved_at=?", "approved_by_user_id=?", "approved_by_role=?"]); params.extend([now_iso(), user()["id"], role])
        if status in ["Paid", "Completed"]:
            extras.append("paid_at=COALESCE(paid_at, ?)"); params.append(now_iso())
        if status == "Completed":
            extras.append("completed_at=COALESCE(completed_at, ?)"); params.append(now_iso())
        if extras:
            params.append(pr_id)
            run_query(f"UPDATE purchase_requests SET {', '.join(extras)} WHERE id=?", params)
    except Exception:
        pass
    _rerun_success(f"{event} completed.")


def approval_action(entity: str, entity_id: int, old_status: str, new_status: str, action: str, reason: str = ""):
    role = _current_role()
    if not can_approve(role):
        st.error("Only Admin and Approver / MD can approve or reject workflow items.")
        return
    if entity == "Purchase Request":
        payment_status = "Approved for Payment" if new_status == "Approved" else None
        transition_request_status(
            entity_id, new_status, action, reason or f"{action} by {display_role(role)}",
            user()["id"], role, "Normal Approval Mode", payment_status=payment_status,
        )
        try:
            updates = ["next_role=?", "approved_at=?", "approved_by_user_id=?", "approved_by_role=?"] if new_status == "Approved" else ["next_role=NULL"]
            params: list[Any] = ["finance", now_iso(), user()["id"], role] if new_status == "Approved" else []
            params.append(entity_id)
            run_query(f"UPDATE purchase_requests SET {', '.join(updates)} WHERE id=?", params)
        except Exception:
            pass
        if new_status == "Approved":
            create_notification(None, "Finance", "Approved item ready for Finance", "A request has been approved and is ready for payment.", entity, entity_id, "Important", ["in_app", "browser_push"], action_label="Open Approved for Payment")
        _rerun_success(f"{entity} {action.lower()}.")
        return
    table = "purchase_orders" if entity == "Purchase Order" else "payments"
    run_query(f"UPDATE {table} SET status=?, updated_at=? WHERE id=?", (new_status, now_iso(), entity_id))
    _set_next_role(table, entity_id, new_status)
    run_query(
        """
        INSERT INTO approval_history (entity_type, entity_id, action, status_before, status_after, reason, user_id, approved_by_user_id, approved_by_role, approval_mode, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Normal Approval Mode', ?, ?)
        """,
        (entity, entity_id, action, old_status, new_status, reason, user()["id"], user()["id"], role, reason, now_iso()),
    )
    add_workflow_event(entity, entity_id, action, new_status, reason, user()["id"])
    log_audit(action, entity, entity_id, reason, user()["id"], role, {"status": old_status}, {"status": new_status})
    _rerun_success(f"{entity} {action.lower()}.")


def _line_row_ids(state_key: str) -> list[int]:
    if state_key not in st.session_state:
        st.session_state[state_key] = [1]
    return list(st.session_state[state_key])


def _add_line_row(state_key: str):
    rows = _line_row_ids(state_key)
    next_id = (max(rows) if rows else 0) + 1
    rows.append(next_id)
    st.session_state[state_key] = rows


def _remove_last_line_row(state_key: str, prefix: str) -> bool:
    """Remove the last dynamic line row while keeping all remaining row keys stable."""
    rows = _line_row_ids(state_key)
    if len(rows) <= 1:
        return False
    removed = rows.pop()
    st.session_state[state_key] = rows
    # Clear only the widgets that belonged to the removed row. Existing rows keep values.
    for key in list(st.session_state.keys()):
        k = str(key)
        if k.startswith(f"{prefix}_") and k.endswith(f"_{removed}"):
            del st.session_state[key]
    return True


def _clear_line_state(state_key: str, prefix: str):
    for k in list(st.session_state.keys()):
        if k == state_key or str(k).startswith(prefix):
            del st.session_state[k]


def _request_line_items(state_key: str, prefix: str, default_category: str) -> tuple[list[tuple[str, float, float, float, str]], float]:
    rows = _line_row_ids(state_key)
    add_col, remove_col, _ = st.columns([1.15, 1.15, 5])
    if add_col.button("＋ Add line item", key=f"{prefix}_add_line_button"):
        _add_line_row(state_key)
        st.rerun()
    if remove_col.button("− Remove line item", key=f"{prefix}_remove_line_button", disabled=len(rows) <= 1):
        _remove_last_line_row(state_key, prefix)
        st.rerun()
    rows = _line_row_ids(state_key)
    items, estimated = [], 0.0
    st.markdown("##### Line items")
    for idx, row_id in enumerate(rows, 1):
        st.caption(f"Item {idx}")
        c1, c2, c3, c4 = st.columns([1.4, .55, .8, .9])
        item = c1.text_input("Item", key=f"{prefix}_item_{row_id}")
        qty = c2.number_input("Qty", min_value=0.0, value=1.0, step=1.0, key=f"{prefix}_qty_{row_id}")
        unit = c3.number_input("Unit price", min_value=0.0, step=1000.0, key=f"{prefix}_unit_{row_id}")
        try:
            default_index = EXPENSE_CATEGORIES.index(default_category) if default_category in EXPENSE_CATEGORIES else 0
        except Exception:
            default_index = 0
        icat = selectbox_with_other("Item category", EXPENSE_CATEGORIES, f"{prefix}_cat_{row_id}", "category", index=default_index)
        total = float(qty or 0) * float(unit or 0)
        estimated += total
        items.append((item, float(qty or 0), float(unit or 0), total, icat))
    return items, estimated


def create_request_form():
    if not has_permission("create_request") or is_read_only(_current_role()):
        st.info("Your role can view requests but cannot create requests.")
        return
    st.caption("Create a draft request. Procurement can route it to Approver/Admin after review.")
    c1, c2, c3 = st.columns(3)
    dept = selectbox_with_other("Department / Project", department_options() + ["Other"], "req_dept_cmd", "department_project")
    req_date = c2.date_input("Request date", date.today(), key="req_date_cmd")
    req_required = c3.date_input("Required date", date.today() + timedelta(days=7), key="req_required_cmd")
    c4, c5, c6 = st.columns(3)
    cat = selectbox_with_other("Category", EXPENSE_CATEGORIES, "req_cat_cmd", "category")
    priority = c5.selectbox("Priority", PRIORITIES, index=1, key="req_priority_cmd")
    vendor_pref = c6.text_input("Vendor preference", key="req_vendor_pref_cmd")
    justification = st.text_area("Business justification", key="req_justification_cmd")
    attachment = st.file_uploader("Supporting document", type=["docx", "pdf", "jpg", "jpeg", "png", "xlsx"], key="req_attachment_cmd")
    items, estimated = _request_line_items("req_line_rows_cmd", "req_cmd", cat)
    st.metric("Estimated request value", format_kpi_value(money(estimated)))
    if st.button("Create Draft Request", type="primary", key="create_draft_request_cmd"):
        if not justification.strip() or not any(i[0].strip() for i in items):
            st.error("Business justification and at least one item are required.")
            return
        _save_custom_value("department_project", dept); _save_custom_value("category", cat)
        path, _ = save_upload(attachment, "requests")
        req_no = make_ref("PR")
        req_id = run_insert(
            """
            INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, required_date, category, justification, priority, estimated_amount, vendor_preference, status, source_type, attachments_json, notes, approval_history_json, next_role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', 'Manual', ?, '', '[]', NULL, ?, ?)
            """,
            (req_no, user()["id"], dept, req_date.isoformat(), req_required.isoformat(), cat, justification, priority, estimated, vendor_pref, json_dump([path] if path else []), now_iso(), now_iso()),
        )
        for item, qty, unit, total, icat in items:
            if item.strip():
                _save_custom_value("category", icat)
                run_query("INSERT INTO purchase_request_items (request_id, item_name, description, quantity, unit_price, total, category, suggested_vendor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (req_id, item.strip(), item.strip(), qty, unit, total, icat, vendor_pref, now_iso()))
        add_workflow_event("Purchase Request", req_id, "Draft Created", "Draft", req_no, user()["id"])
        log_audit("DRAFT_CREATED", "Purchase Request", req_id, {"request_no": req_no, "amount": estimated, "department": dept}, user()["id"], user()["role"], after_values={"status": "Draft"})
        _clear_line_state("req_line_rows_cmd", "req_cmd_")
        _rerun_success(f"Created {req_no}")


def create_fm_draft_form():
    if not has_permission("create_request") or not _is_utility():
        st.info("Only Utility Head / Facility Head can create this draft type.")
        return
    pm_id = get_pm_for_facility_manager(user()["id"])
    if pm_id:
        pm = df_query("SELECT full_name FROM users WHERE id=?", (pm_id,))
        st.info(f"Automatic routing is active. Procurement Manager queue: {pm.iloc[0]['full_name'] if not pm.empty else 'active Procurement Manager'}")
    else:
        st.warning("No active Procurement Manager user exists. Create one in Admin → User Management.")
    c1, c2, c3 = st.columns(3)
    dept = c1.selectbox("Department / Project", department_options(), key="uf_dept_cmd")
    req_required = c2.date_input("Required date", date.today() + timedelta(days=7), key="uf_required_cmd")
    cat = c3.selectbox("Category", EXPENSE_CATEGORIES, key="uf_cat_cmd")
    c4, c5 = st.columns(2)
    priority = c4.selectbox("Priority", PRIORITIES, index=1, key="uf_priority_cmd")
    vendor_pref = c5.text_input("Vendor preference", key="uf_vendor_pref_cmd")
    justification = st.text_area("Business justification", key="uf_justification_cmd")
    attachment = st.file_uploader("Supporting document", type=["docx", "pdf", "jpg", "jpeg", "png", "xlsx"], key="uf_support_cmd")
    items, estimated = _request_line_items("uf_line_rows_cmd", "uf_cmd", cat)
    st.metric("Estimated draft value", format_kpi_value(money(estimated)))
    if st.button("Create Utility / Facility Draft", type="primary", key="uf_create_draft_cmd"):
        if not justification.strip() or not any(i[0].strip() for i in items):
            st.error("Business justification and at least one item are required.")
            return
        path, _ = save_upload(attachment, "requests")
        req_no = make_ref("UF")
        pr_id = run_insert(
            """
            INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, required_date, category, justification, priority, estimated_amount, vendor_preference, status, source_type, attachments_json, notes, approval_history_json, facility_manager_user_id, assigned_procurement_manager_id, next_role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FM Draft', 'Utility Head / Facility Head', ?, '', '[]', ?, ?, NULL, ?, ?)
            """,
            (req_no, user()["id"], dept, date.today().isoformat(), req_required.isoformat(), cat, justification, priority, estimated, vendor_pref, json_dump([path] if path else []), user()["id"], pm_id, now_iso(), now_iso()),
        )
        for item, qty, unit, total, icat in items:
            if item.strip():
                run_query("INSERT INTO purchase_request_items (request_id, item_name, description, quantity, unit_price, total, category, suggested_vendor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (pr_id, item.strip(), item.strip(), qty, unit, total, icat, vendor_pref, now_iso()))
        ensure_thread("Purchase Request", pr_id, user()["id"], pm_id)
        add_workflow_event("Purchase Request", pr_id, "Draft Created", "FM Draft", req_no, user()["id"])
        log_audit("DRAFT_CREATED", "Purchase Request", pr_id, {"request_no": req_no, "amount": estimated, "department": dept}, user()["id"], user()["role"], after_values={"status": "FM Draft"})
        _clear_line_state("uf_line_rows_cmd", "uf_cmd_")
        st.success(f"Created draft {req_no}.")
        st.rerun()


def facility_dashboard():
    fm_id = user()["id"]
    pm_id = get_pm_for_facility_manager(fm_id)
    pm = df_query("SELECT full_name FROM users WHERE id=?", (pm_id,)) if pm_id else pd.DataFrame()
    st.info(f"Automatic Procurement Manager routing: {pm.iloc[0]['full_name'] if not pm.empty else 'No active Procurement Manager found'}")
    q = lambda statuses: int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE facility_manager_user_id=? AND status IN (%s)" % ",".join(["?"] * len(statuses)), tuple([fm_id] + list(statuses))).iloc[0, 0])
    metric_row([
        ("Drafts", q(("FM Draft", "Draft")), None),
        ("Pending Procurement Review", q(("Sent for Procurement Review", "Submitted to Procurement Manager")), None),
        ("Returned", q(("Returned for Correction", "Returned to Facility Manager")), None),
        ("Submitted for Approval", q(("Submitted for Approval", "Pending Approver/MD Approval")), None),
        ("Approved", q(("Approved", "Awaiting Payment", "Approved for Payment")), None),
        ("Completed", q(("Completed", "Closed", "Paid")), None),
    ], cols=3)
    df = df_query(
        """
        SELECT request_no, department_project, category, estimated_amount, status, updated_at
        FROM purchase_requests
        WHERE facility_manager_user_id=?
        ORDER BY updated_at DESC, created_at DESC LIMIT 20
        """,
        (fm_id,),
    )
    if not df.empty:
        df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
    else:
        st.success("No Utility Head / Facility Head actions pending.")


def facility_workspace():
    role_header("Utility Head / Facility Head Workspace", "Create drafts, manage gateway passes, submit to Procurement Manager, and respond to corrections.")
    section = st.session_state.get("facility_section", "Utility / Facility Dashboard")
    if section in ["Utility / Facility Dashboard", "Facility Dashboard"]:
        facility_dashboard()
    elif section == "Create Request Draft":
        create_fm_draft_form()
    elif section == "My Draft Requests":
        facility_draft_register(status_filter=None)
    elif section == "Submit to Procurement Manager":
        facility_draft_register(status_filter=["FM Draft", "Draft", "Returned for Correction", "Returned to Facility Manager"])
    elif section == "Import Documents":
        facility_import_documents()
    elif section == "Gateway Pass":
        facility_gateway_pass_page()
    elif section == "Shared Thread with Procurement Manager":
        facility_shared_threads()
    elif section == "Returned Requests":
        facility_draft_register(status_filter=["Returned for Correction", "Returned to Facility Manager"])
    elif section == "Approved / Accepted Requests":
        facility_draft_register(status_filter=["Reviewed by Procurement", "Submitted for Approval", "Approved", "Awaiting Payment", "Paid", "Completed", "Closed"])
    elif section == "My Activity History":
        activity_history_page(scope="mine")
    elif section == "Income":
        income_page(manage=False)
    elif section == "Settings":
        settings_page()
    else:
        facility_dashboard()


def facility_draft_register(status_filter: list[str] | None = None):
    sql = "SELECT * FROM purchase_requests WHERE facility_manager_user_id=?"
    params: list[Any] = [user()["id"]]
    if status_filter:
        sql += " AND status IN (%s)" % ",".join(["?"] * len(status_filter)); params += status_filter
    sql += " ORDER BY updated_at DESC, created_at DESC"
    df = df_query(sql, params)
    if df.empty:
        empty_state("No Utility Head / Facility Head drafts", "Create a draft or gateway pass to begin.")
        return
    show_cols = ["id", "request_no", "department_project", "category", "estimated_amount", "status", "updated_at"]
    show = df[[c for c in show_cols if c in df.columns]].copy()
    show["estimated_amount"] = show["estimated_amount"].apply(money)
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open my draft/request", [f"{r.request_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key=f"uf_open_{'_'.join(status_filter or ['all'])}")
    pr_id = int(selected.rsplit("#", 1)[1])
    pr = df[df["id"] == pr_id].iloc[0]
    request_detail(pr_id, actions=False, key_scope=f"uf_detail_{pr_id}")
    if int(pr.get("assigned_procurement_manager_id") or 0):
        render_private_thread("Purchase Request", pr_id, int(pr["facility_manager_user_id"]), int(pr["assigned_procurement_manager_id"] or 0), f"uf_thread_{pr_id}")
    if pr["status"] in ["FM Draft", "Draft", "Returned for Correction", "Returned to Facility Manager"]:
        c1, c2 = st.columns(2)
        if c1.button("Send to Procurement Manager", type="primary", key=f"uf_submit_{pr_id}"):
            pm_id = int(pr.get("assigned_procurement_manager_id") or 0) or get_pm_for_facility_manager(user()["id"])
            if not pm_id:
                st.error("No active Procurement Manager user exists."); return
            run_query("UPDATE purchase_requests SET assigned_procurement_manager_id=?, next_role='procurement_manager' WHERE id=?", (pm_id, pr_id))
            update_request_status(pr_id, "Sent for Procurement Review", "Sent for Procurement Review", "Utility Head / Facility Head sent draft to Procurement Manager")
        if c2.button("Delete draft", key=f"uf_delete_{pr_id}"):
            if not can_delete_draft(user()["role"], int(pr.get("facility_manager_user_id") or pr.get("requested_by") or 0), user()["id"], pr["status"]):
                st.error("Only your own unsubmitted draft can be deleted.")
            else:
                run_query("DELETE FROM purchase_request_items WHERE request_id=?", (pr_id,))
                run_query("DELETE FROM purchase_requests WHERE id=?", (pr_id,))
                log_audit("DRAFT_DELETED", "Purchase Request", pr_id, pr.get("request_no"), user()["id"], user()["role"], before_values={"status": pr["status"]})
                _rerun_success("Draft deleted.")


def facility_manager_inbox():
    st.subheader("Utility Head / Facility Head Inbox")
    st.caption("Requests sent by Utility Head / Facility Head appear here automatically by role-based routing. Review, return, or submit valid items to Approver/Admin.")
    df = df_query(
        """
        SELECT pr.*, fm.full_name facility_manager
        FROM purchase_requests pr LEFT JOIN users fm ON pr.facility_manager_user_id=fm.id
        WHERE (pr.next_role='procurement_manager' OR pr.status IN ('Sent for Procurement Review','Submitted to Procurement Manager','Submitted','Procurement Review','Reviewed by Procurement','Returned for Correction'))
        ORDER BY pr.updated_at DESC, pr.created_at DESC
        """
    )
    if df.empty:
        empty_state("No Utility Head / Facility Head requests", "Submitted drafts will appear here automatically.")
        return
    show = df[["id", "request_no", "facility_manager", "department_project", "category", "estimated_amount", "status", "updated_at"]].copy()
    show["estimated_amount"] = show["estimated_amount"].apply(money)
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open request", [f"{r.request_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="pm_uf_inbox_select_cmd")
    pr_id = int(selected.rsplit("#", 1)[1])
    pr = df[df["id"] == pr_id].iloc[0]
    request_detail(pr_id, actions=False, key_scope=f"pm_uf_inbox_{pr_id}")
    if int(pr.get("facility_manager_user_id") or 0):
        render_private_thread("Purchase Request", pr_id, int(pr["facility_manager_user_id"]), int(user()["id"]), f"pm_uf_thread_{pr_id}")
    note = st.text_area("Procurement review comment / correction reason", key=f"pm_uf_note_{pr_id}")
    c1, c2, c3 = st.columns(3)
    if c1.button("Mark Reviewed", key=f"pm_uf_review_{pr_id}"):
        update_request_status(pr_id, "Reviewed by Procurement", "Reviewed by Procurement", note or "Reviewed by Procurement Manager")
    if c2.button("Return for Correction", key=f"pm_uf_return_{pr_id}"):
        update_request_status(pr_id, "Returned for Correction", "Returned for Correction", note or "Returned for correction")
    if c3.button("Submit to Approver/Admin", type="primary", key=f"pm_uf_to_approver_{pr_id}"):
        run_query("UPDATE purchase_requests SET next_role='approver' WHERE id=?", (pr_id,))
        create_notification(None, "Approver", "Request submitted for approval", f"{pr['request_no']} requires final approval.", "Purchase Request", pr_id, "High", ["in_app", "browser_push"], action_label="Open Pending Approvals")
        create_notification(None, "Admin", "Request submitted for approval", f"{pr['request_no']} requires approval/oversight.", "Purchase Request", pr_id, "Important", ["in_app"])
        update_request_status(pr_id, "Submitted for Approval", "Submitted for Approval", note or "Submitted by Procurement Manager for final approval")


def request_actions(pr_id: int, pr, key_scope: str | None = None):
    scope = key_scope or "default"
    prefix = f"{scope}_pr_{pr_id}"
    role = _current_role()
    status = pr["status"]
    st.markdown("#### Guided next action")
    actions = []
    if status in ["Draft", "FM Draft", "Returned for Correction", "Returned to Facility Manager"] and role in ["Facility Manager", "Procurement Manager", "Admin"]:
        actions.append(("Send to Procurement Manager", "Sent for Procurement Review", "Sent for Procurement Review", "Sent for procurement review"))
    if status in ["Sent for Procurement Review", "Submitted to Procurement Manager", "Submitted", "Procurement Review", "Reviewed by Procurement"] and role in ["Procurement Manager", "Admin"]:
        actions.extend([
            ("Return for Correction", "Returned for Correction", "Returned for Correction", "Returned for correction"),
            ("Submit to Approver/Admin", "Submitted for Approval", "Submitted for Approval", "Submitted for final approval"),
        ])
    if status in ["Submitted for Approval", "Pending Approver/MD Approval", "Pending Approval"] and can_approve(role):
        actions.extend([
            ("Approve Request", "Approved", "Approved", "Approved"),
            ("Reject Request", "Rejected", "Rejected", "Rejected"),
            ("Return for Correction", "Returned for Correction", "Returned for Correction", "Returned for correction"),
        ])
    if role == "Finance" and status in ["Approved", "Awaiting Payment", "Approved for Payment"]:
        st.info("This item is approved. Use Finance → Approved for Payment to record payment and upload receipt.")
    if not actions:
        st.info("No direct action is available for this request at its current status or your role.")
        return
    chosen = st.selectbox("Choose next action", [a[0] for a in actions], key=f"guided_action_cmd_{prefix}")
    reason = st.text_area("Comment / reason", key=f"reason_cmd_{prefix}")
    if st.button("Apply selected action", type="primary", key=f"apply_action_cmd_{prefix}"):
        label, new_status, event, note = [a for a in actions if a[0] == chosen][0]
        if any(word in label for word in ["Reject", "Return"]) and not reason.strip():
            st.error("Please enter a reason.")
            return
        if new_status in ["Approved", "Rejected"]:
            approval_action("Purchase Request", pr_id, status, new_status, event, reason or note)
        else:
            if new_status == "Submitted for Approval":
                create_notification(None, "Approver", "Request pending approval", f"{pr['request_no']} requires final approval.", "Purchase Request", pr_id, "High", ["in_app", "browser_push"], action_label="Open Pending Approvals")
            update_request_status(pr_id, new_status, event, reason or note)


def request_next_action_board():
    st.caption("Queue KPIs reduce as items move forward; cumulative KPIs remain available in dashboards/reports.")
    cards = [
        ("Procurement review queue", ("Sent for Procurement Review", "Submitted to Procurement Manager", "Submitted"), "Open Utility Head / Facility Head Inbox"),
        ("Final approval queue", ("Submitted for Approval", "Pending Approver/MD Approval", "Pending Approval"), "Open Pending Approvals"),
        ("Finance payment queue", ("Approved", "Awaiting Payment", "Approved for Payment"), "Open Approved for Payment"),
    ]
    selected_action = st.selectbox("Queue", [c[0] for c in cards], key="pr_action_board_choice_cmd")
    title, statuses, action = [c for c in cards if c[0] == selected_action][0]
    placeholders = ",".join(["?"] * len(statuses))
    df = df_query(f"SELECT id, request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE status IN ({placeholders}) ORDER BY updated_at DESC", statuses)
    if df.empty:
        st.success(f"No request currently in {title.lower()}.")
        return
    show = df.copy(); show["estimated_amount"] = show["estimated_amount"].apply(money); dataframe(show.drop(columns=["id"]))
    st.info(action)


def procurement_dashboard():
    st.subheader("What needs my attention?")
    queue_review = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE next_role='procurement_manager' OR status IN ('Sent for Procurement Review','Submitted to Procurement Manager','Submitted')").iloc[0, 0])
    submitted_total = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status NOT IN ('Draft','FM Draft')").iloc[0, 0])
    approved_total = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status IN ('Approved','Awaiting Payment','Approved for Payment','Paid','Completed','Closed')").iloc[0, 0])
    metric_row([
        ("Pending Review", queue_review, "queue"),
        ("Total Submitted", submitted_total, "cumulative"),
        ("Total Approved", approved_total, "cumulative"),
    ], cols=3)
    df = df_query("SELECT request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE status NOT IN ('Rejected','Archived') ORDER BY updated_at DESC LIMIT 25")
    if not df.empty:
        df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
    c1, c2 = st.columns(2)
    with c1:
        pipe = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status ORDER BY count DESC")
        interactive_chart(pipe, "Procurement Pipeline", "status", "count", "pm_pipeline_cmd", default="Bar")
    with c2:
        spend = df_query("SELECT category, SUM(estimated_amount) total FROM purchase_requests WHERE status NOT IN ('Rejected','Archived') GROUP BY category ORDER BY total DESC")
        interactive_chart(_money_chart_df(spend), "Estimated Value by Category", "category", "total", "pm_category_value_cmd", default="Donut")


def post_payment_closure_page():
    st.subheader("Post-Payment Closure")
    st.caption("Finance records payment and uploads receipt. Procurement Manager then completes, closes, and archives the record for history/audit.")
    df = df_query("""
        SELECT id, request_no, department_project, category, estimated_amount, status, payment_status, paid_at, updated_at
        FROM purchase_requests
        WHERE status IN ('Paid','Receipt Uploaded','Payment Submitted for Verification','Completed','Closed')
           OR (next_role='procurement_manager' AND payment_status='Paid')
        ORDER BY COALESCE(paid_at, updated_at, created_at) DESC
        LIMIT 500
    """)
    if df.empty:
        st.success("No paid records are waiting for completion, closure, or archive.")
        return
    show = df.copy()
    show["estimated_amount"] = show["estimated_amount"].apply(money)
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open paid record", [f"{r.request_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="pm_postpay_select_cmd")
    pr_id = int(selected.rsplit("#", 1)[1])
    pr = df[df["id"] == pr_id].iloc[0]
    request_detail(pr_id, actions=False, key_scope=f"pm_postpay_{pr_id}")
    note = st.text_area("Closure note", key=f"pm_postpay_note_{pr_id}")
    c1, c2, c3 = st.columns(3)
    if c1.button("Mark Completed", type="primary", disabled=pr["status"] not in ["Paid", "Receipt Uploaded", "Payment Submitted for Verification"], key=f"pm_mark_completed_{pr_id}"):
        run_query("UPDATE purchase_requests SET status='Completed', next_role='procurement_manager', completed_at=COALESCE(completed_at, ?), updated_at=? WHERE id=?", (now_iso(), now_iso(), pr_id))
        add_workflow_event("Purchase Request", pr_id, "Completed", "Completed", note or "Completed after payment by Procurement Manager", user()["id"])
        log_audit("REQUEST_COMPLETED", "Purchase Request", pr_id, note or "Completed after payment", user()["id"], user()["role"], before_values={"status": pr["status"]}, after_values={"status": "Completed"})
        owner_df = df_query("SELECT COALESCE(facility_manager_user_id, requested_by) owner_id FROM purchase_requests WHERE id=?", (pr_id,))
        owner_id = int(owner_df.iloc[0,0] or 0) if not owner_df.empty else 0
        if owner_id:
            create_notification(owner_id, None, "Request completed", f"{pr['request_no']} has been marked Completed.", "Purchase Request", pr_id, "Normal", ["in_app"], action_label="Approved / Accepted Requests")
        _notify_auditors("Request completed", f"{pr['request_no']} was completed by Procurement Manager.", "Purchase Request", pr_id)
        _rerun_success("Record marked Completed.")
    if c2.button("Close Record", disabled=pr["status"] != "Completed", key=f"pm_close_record_{pr_id}"):
        run_query("UPDATE purchase_requests SET status='Closed', next_role='procurement_manager', updated_at=? WHERE id=?", (now_iso(), pr_id))
        add_workflow_event("Purchase Request", pr_id, "Closed", "Closed", note or "Closed by Procurement Manager", user()["id"])
        log_audit("REQUEST_CLOSED", "Purchase Request", pr_id, note or "Closed after completion", user()["id"], user()["role"], before_values={"status": pr["status"]}, after_values={"status": "Closed"})
        _notify_auditors("Request closed", f"{pr['request_no']} was closed by Procurement Manager.", "Purchase Request", pr_id)
        _rerun_success("Record closed.")
    if c3.button("Archive Record", disabled=pr["status"] != "Closed", key=f"pm_archive_record_{pr_id}"):
        run_query("UPDATE purchase_requests SET status='Archived', next_role='auditor', updated_at=? WHERE id=?", (now_iso(), pr_id))
        add_workflow_event("Purchase Request", pr_id, "Archived", "Archived", note or "Archived by Procurement Manager", user()["id"])
        log_audit("REQUEST_ARCHIVED", "Purchase Request", pr_id, note or "Archived after closure", user()["id"], user()["role"], before_values={"status": pr["status"]}, after_values={"status": "Archived"})
        _notify_auditors("Request archived", f"{pr['request_no']} was archived by Procurement Manager.", "Purchase Request", pr_id)
        _rerun_success("Record archived and visible in history/audit.")


def procurement_workspace():
    role_header("Procurement Manager Workspace", "Review Utility Head / Facility Head submissions, source vendors, and submit valid requests to Approver/Admin. This role cannot approve normal procurement/payment requests.")
    section = st.session_state.get("procurement_section", "Operations Dashboard")
    if section == "Operations Dashboard":
        procurement_dashboard_metrics(); procurement_dashboard()
    elif section == "Purchase Requests":
        requests_page(mode="procurement")
    elif section in ["Utility Head / Facility Head Inbox", "Facility Manager Inbox"]:
        facility_manager_inbox()
    elif section == "Import Center":
        import_center()
    elif section == "Sourcing":
        sourcing_page()
    elif section == "Vendor Quotes":
        quote_page()
    elif section == "Vendor Recommendation":
        sourcing_page()
    elif section == "Purchase Orders":
        purchase_orders_page()
    elif section == "Receiving Slips":
        receiving_page()
    elif section == "Vendors":
        vendors_page()
    elif section == "Gateway Pass Review":
        gateway_pass_review_queue("Gateway Pass Review")
    elif section == "Post-Payment Closure":
        post_payment_closure_page()
    elif section == "Availability / Away Notice":
        availability_panel()
    elif section == "Procurement Documents":
        document_archive(editable=True)
    elif section == "Procurement Reports":
        procurement_reports()
    elif section == "Income":
        income_page(manage=False)
    elif section == "My Activity History":
        activity_history_page(scope="mine")
    elif section == "Settings":
        settings_page()
    else:
        procurement_dashboard_metrics(); procurement_dashboard()


def _gateway_department_options():
    return GATEWAY_DEPARTMENTS


def _notify_auditors(title: str, message: str, entity_type: str, entity_id: int | None = None, importance: str = "Normal"):
    try:
        create_notification(None, "Auditor", title, message, entity_type, entity_id, importance, ["in_app"], action_label="Open Audit Dashboard")
    except Exception:
        pass


def _row_to_dict(row) -> dict:
    try:
        return row.to_dict()
    except Exception:
        return dict(row)


def _notify_gateway_event(row, title: str, message: str, target: str = "facility", importance: str = "High"):
    """Route gateway notifications to the correct sidebar badge and auditor feed."""
    data = _row_to_dict(row)
    gp_id = int(data.get("id") or data.get("gateway_pass_id") or 0) or None
    if target in ("facility", "all") and data.get("facility_manager_user_id"):
        try:
            create_notification(int(data["facility_manager_user_id"]), None, title, message, "Gateway Pass", gp_id, importance, ["in_app", "browser_push"], action_label="Open Gateway Pass")
        except Exception:
            pass
    if target in ("reviewers", "all"):
        create_notification(None, "Procurement Manager", title, message, "Gateway Pass", gp_id, importance, ["in_app", "browser_push"], action_label="Review Gateway Pass")
        create_notification(None, "Approver", title, message, "Gateway Pass", gp_id, importance, ["in_app", "browser_push"], action_label="Review Gateway Pass")
        create_notification(None, "Admin", title, message, "Gateway Pass", gp_id, "Important", ["in_app"], action_label="Gateway Pass Management")
    _notify_auditors(title, message, "Gateway Pass", gp_id, "Normal")


def create_gateway_pass_form():
    _phase2_bootstrap()
    if not _is_utility() and user()["role"] != "Admin":
        st.info("Only Utility Head / Facility Head can create gateway pass drafts.")
        return
    st.subheader("Create Gateway Pass Draft")
    rows = _line_row_ids("gp_line_rows_cmd")
    add_col, remove_col, _ = st.columns([1.25, 1.25, 5])
    if add_col.button("＋ Add gateway line item", key="gp_cmd_add_line"):
        _add_line_row("gp_line_rows_cmd")
        st.rerun()
    if remove_col.button("− Remove gateway line item", key="gp_cmd_remove_line", disabled=len(rows) <= 1):
        _remove_last_line_row("gp_line_rows_cmd", "gp_cmd")
        st.rerun()
    rows = _line_row_ids("gp_line_rows_cmd")
    with st.form("create_gateway_pass_form_cmd"):
        c1, c2, c3 = st.columns(3)
        dept = c1.selectbox("Department", _gateway_department_options(), key="gp_create_dept_cmd")
        movement_type = selectbox_with_other("Movement type", GATEWAY_MOVEMENT_TYPES, "gp_create_movement_cmd", "gateway_movement_type")
        expected_movement = c3.date_input("Movement date", date.today(), key="gp_create_move_date_cmd")
        purpose = st.text_area("Purpose of movement", key="gp_create_purpose_cmd")
        c4, c5 = st.columns(2)
        origin = c4.text_input("Origin", key="gp_create_origin_cmd")
        destination = c5.text_input("Destination", key="gp_create_destination_cmd")
        c6, c7, c8 = st.columns(3)
        return_required = c6.checkbox("Expected return date applies", value=False, key="gp_create_return_applies_cmd")
        expected_return = c6.date_input("Expected return date", date.today() + timedelta(days=1), key="gp_create_return_cmd") if return_required else None
        vehicle = c7.text_input("Vehicle number", key="gp_create_vehicle_cmd")
        checkpoint = c8.text_input("Security checkpoint", key="gp_create_checkpoint_cmd")
        c9, c10, c11 = st.columns(3)
        driver = c9.text_input("Driver name", key="gp_create_driver_cmd")
        driver_phone = c10.text_input("Driver phone", key="gp_create_driver_phone_cmd")
        receiver = c11.text_input("Receiver name", key="gp_create_receiver_cmd")
        receiver_org = st.text_input("Receiver organization", key="gp_create_receiver_org_cmd")
        items = []
        st.markdown("##### Item details")
        for idx, row_id in enumerate(rows, 1):
            st.caption(f"Item {idx}")
            a, b, c, d0 = st.columns([1.6, 1, .7, .8])
            desc = a.text_input("Item description", key=f"gp_cmd_item_desc_{row_id}")
            category = b.text_input("Item category", key=f"gp_cmd_item_cat_{row_id}")
            qty = c.number_input("Quantity", min_value=0.0, step=1.0, value=1.0, key=f"gp_cmd_item_qty_{row_id}")
            colour = d0.text_input("Colour", key=f"gp_cmd_item_colour_{row_id}")
            d, e, f = st.columns(3)
            uom = selectbox_with_other("Unit of measure", GATEWAY_UOMS, f"gp_cmd_item_uom_{row_id}", "gateway_uom")
            quality = selectbox_with_other("Quality / condition", GATEWAY_QUALITY_OPTIONS, f"gp_cmd_item_quality_{row_id}", "gateway_quality")
            fragile = f.selectbox("Fragility status", GATEWAY_FRAGILITY_OPTIONS, key=f"gp_cmd_item_fragile_{row_id}")
            g, h, k = st.columns(3)
            value = g.number_input("Estimated value", min_value=0.0, step=1000.0, key=f"gp_cmd_item_value_{row_id}")
            serial = h.text_input("Serial number", key=f"gp_cmd_item_serial_{row_id}")
            asset = k.text_input("Asset tag", key=f"gp_cmd_item_asset_{row_id}")
            handling = st.text_input("Handling instruction", key=f"gp_cmd_item_handling_{row_id}")
            remarks = st.text_input("Remarks", key=f"gp_cmd_item_remarks_{row_id}")
            items.append({"desc": desc, "category": category, "qty": qty, "colour": colour, "uom": uom, "quality": quality, "fragile": fragile, "value": value, "serial": serial, "asset": asset, "handling": handling, "remarks": remarks})
        submitted = st.form_submit_button("Create Gateway Pass Draft", type="primary")
    if submitted:
        valid_items = [x for x in items if x["desc"].strip()]
        if not purpose.strip() or not receiver.strip():
            st.error("Purpose of movement and receiver name are required."); return
        if not valid_items:
            st.error("At least one item line is required before saving."); return
        if any(float(x["qty"] or 0) <= 0 or not x["uom"] or x["uom"] == "Other" for x in valid_items):
            st.error("Every item needs quantity greater than 0 and a valid unit of measure."); return
        for x in valid_items:
            _save_custom_value("gateway_uom", x["uom"]); _save_custom_value("gateway_quality", x["quality"])
        _save_custom_value("gateway_movement_type", movement_type)
        pass_no = make_ref("GP")
        gp_id = run_insert(
            """
            INSERT INTO gateway_passes (pass_number, facility_manager_user_id, department, movement_type, purpose, origin_location, destination, expected_movement_date, expected_return_date, vehicle_number, driver_name, driver_phone, receiver_name, receiver_organization, security_checkpoint, status, next_role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', NULL, ?, ?)
            """,
            (pass_no, user()["id"], dept, movement_type, purpose.strip(), origin, destination, expected_movement.isoformat(), expected_return.isoformat() if expected_return else None, vehicle, driver, driver_phone, receiver, receiver_org, checkpoint, now_iso(), now_iso()),
        )
        for item in valid_items:
            run_query(
                """
                INSERT INTO gateway_pass_items (gateway_pass_id, item_description, item_category, colour, quantity, unit_of_measure, quality_condition, estimated_value, serial_number, asset_tag, fragility_status, handling_instruction, remarks, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (gp_id, item["desc"].strip(), item["category"], item["colour"], float(item["qty"]), item["uom"], item["quality"], float(item["value"] or 0), item["serial"], item["asset"], item["fragile"], item["handling"], item["remarks"], now_iso()),
            )
        log_gateway_event(gp_id, "Gateway Pass Draft Created", "Draft", pass_no)
        log_audit("DRAFT_CREATED", "Gateway Pass", gp_id, {"pass_number": pass_no, "department": dept}, user()["id"], user()["role"], after_values={"status": "Draft"})
        _clear_line_state("gp_line_rows_cmd", "gp_cmd_")
        _rerun_success(f"Gateway pass draft created: {pass_no}")


def submit_gateway_pass(gateway_pass_id: int):
    if not _assert_gateway_owner(gateway_pass_id):
        return
    row_df = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if row_df.empty:
        st.error("Gateway pass not found."); return
    row = row_df.iloc[0]
    if row["status"] not in ["Draft", "Returned for Correction"]:
        st.warning("Only Draft or Returned gateway passes can be submitted."); return
    items = gateway_pass_items_df(gateway_pass_id)
    if items.empty:
        st.error("At least one item line is required before submission."); return
    if (items["quantity"].fillna(0) <= 0).any() or items["unit_of_measure"].fillna("").eq("").any():
        st.error("Every item must include quantity > 0 and unit."); return
    run_query("UPDATE gateway_passes SET status='Sent for Procurement Review', next_role='procurement_manager', submitted_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), gateway_pass_id))
    log_gateway_event(gateway_pass_id, "Sent for Procurement Review", "Sent for Procurement Review", "Submitted to Procurement Manager")
    create_notification(None, "Procurement Manager", "Gateway pass sent for review", f"{row['pass_number']} requires Procurement Manager review before final approval.", "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")
    create_notification(int(row["facility_manager_user_id"]), None, "Gateway pass submitted to Procurement Manager", f"{row['pass_number']} was sent to Procurement Manager for review.", "Gateway Pass", gateway_pass_id, "Normal", ["in_app"], action_label="Open Gateway Pass")
    _notify_auditors("Gateway pass sent for review", f"{row['pass_number']} was sent to Procurement Manager for review.", "Gateway Pass", gateway_pass_id)
    _rerun_success("Gateway pass sent to Procurement Manager for review. Procurement Manager has been notified.")


def _assert_gateway_reviewer(gateway_pass_id: int, acting_user: dict | None = None) -> bool:
    acting_user = acting_user or user()
    role = acting_user["role"]
    if role in ["Admin", "Approver", "Procurement Manager"]:
        return True
    st.error("You are not authorized to review gateway passes.")
    return False


def _gateway_approve(gateway_pass_id: int, decision: str, note: str):
    role = _current_role()
    if role == "Procurement Manager":
        st.error("Procurement Manager can review and submit gateway passes to Approver / MD, but cannot approve them.")
        return
    if not can_approve(role):
        st.error("Only Admin and Approver / MD can approve, return, or reject gateway passes.")
        return
    row_df = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if row_df.empty:
        st.error("Gateway pass not found."); return
    row = row_df.iloc[0]
    allowed_statuses = ["Submitted for Approval", "Pending Approval"]
    if role == "Admin":
        # Admin keeps audited override ability without making Procurement Manager an approver.
        allowed_statuses += ["Sent for Procurement Review", "Reviewed by Procurement", "Submitted", "Pending Procurement Manager / Approver Review"]
    if row["status"] not in allowed_statuses and row.get("next_role") != "approver":
        st.warning("Gateway passes must be submitted by Procurement Manager to Approver / MD before final approval."); return
    if decision in ["Rejected", "Returned for Correction"] and not note.strip():
        st.error("A rejection or return reason is required."); return
    if decision == "Approved":
        # Final approval hands the record back to Utility Head / Facility Head for preview/generation.
        run_query("UPDATE gateway_passes SET status='Approved', next_role='facility_manager', approved_at=?, approved_by_user_id=?, approved_by_role=?, approval_note=?, updated_at=? WHERE id=?", (now_iso(), user()["id"], role, note or "Approved.", now_iso(), gateway_pass_id))
        decision_label = "Approved"; title = "Gateway Pass Approved - Ready to Generate"; msg = f"{row['pass_number']} has been approved by {display_role(role)}. Open Gateway Pass > Ready to Generate to preview and download it."
    elif decision == "Rejected":
        run_query("UPDATE gateway_passes SET status='Rejected', next_role=NULL, rejected_at=?, rejected_by_user_id=?, rejection_reason=?, updated_at=? WHERE id=?", (now_iso(), user()["id"], note, now_iso(), gateway_pass_id))
        decision_label = "Rejected"; title = "Gateway Pass Rejected"; msg = f"{row['pass_number']} was rejected. Reason: {note}"
    else:
        run_query("UPDATE gateway_passes SET status='Returned for Correction', next_role='facility_manager', rejection_reason=?, updated_at=? WHERE id=?", (note, now_iso(), gateway_pass_id))
        decision_label = "Returned for Correction"; title = "Gateway Pass Returned"; msg = f"{row['pass_number']} was returned for correction. Reason: {note}"
    run_query("INSERT INTO gateway_pass_approvals (gateway_pass_id, approver_user_id, approver_role, decision, note, created_at) VALUES (?, ?, ?, ?, ?, ?)", (gateway_pass_id, user()["id"], role, decision_label, note, now_iso()))
    log_gateway_event(gateway_pass_id, f"Gateway Pass {decision_label}", decision_label, note)
    action_label = "Ready to Generate" if decision_label == "Approved" else "Open Gateway Pass"
    create_notification(int(row["facility_manager_user_id"]), None, title, msg, "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label=action_label)
    if decision_label == "Approved":
        create_notification(None, "Procurement Manager", "Gateway pass final approval completed", f"{row['pass_number']} was approved by {display_role(role)} and routed to Utility Head / Facility Head for generation.", "Gateway Pass", gateway_pass_id, "Normal", ["in_app"], action_label="Review Gateway Pass")
    _notify_auditors(title, msg, "Gateway Pass", gateway_pass_id)
    _rerun_success(f"Gateway pass {decision_label.lower()}.")


def gateway_pass_review_queue(title: str, admin_mode: bool = False):
    _phase2_bootstrap()
    st.subheader(title)
    role = _current_role()
    if role == "Procurement Manager" and not admin_mode:
        df = gateway_pass_summary_df("(gp.status IN ('Sent for Procurement Review','Submitted','Pending Procurement Manager / Approver Review') OR gp.next_role='procurement_manager')", ())
    elif can_approve(role) or admin_mode:
        df = gateway_pass_summary_df("(gp.status IN ('Submitted for Approval','Pending Approval') OR gp.next_role='approver')", ())
    else:
        st.info("This queue is not available to your role."); return
    if df.empty:
        st.success("No gateway passes are awaiting action."); return
    show = df[["id", "pass_number", "facility_manager", "department", "movement_type", "destination", "expected_movement_date", "status", "submitted_at"]].copy()
    dataframe(show.drop(columns=["id"]))
    selected = st.selectbox("Open gateway pass", [f"{r.pass_number} — {r.facility_manager} — #{int(r.id)}" for r in df.itertuples()], key=f"gp_review_select_cmd_{role}_{admin_mode}")
    gp_id = int(selected.rsplit("#", 1)[1])
    row = df[df["id"] == gp_id].iloc[0]
    gateway_pass_detail(gp_id)
    note = st.text_area("Review note / reason", key=f"gp_review_note_cmd_{gp_id}_{role}_{admin_mode}")
    if role == "Procurement Manager":
        st.info("Procurement Manager reviews gateway passes and submits valid ones to Approver / MD. There is no Procurement Manager approval button.")
        c1, c2, c3 = st.columns(3)
        if c1.button("Mark Reviewed", key=f"gp_pm_reviewed_{gp_id}"):
            run_query("UPDATE gateway_passes SET status='Reviewed by Procurement', next_role='procurement_manager', reviewed_by_user_id=?, reviewed_at=?, procurement_review_note=?, updated_at=? WHERE id=?", (user()["id"], now_iso(), note or "Reviewed by Procurement Manager", now_iso(), gp_id))
            log_gateway_event(gp_id, "Reviewed by Procurement", "Reviewed by Procurement", note)
            _notify_auditors("Gateway pass reviewed by Procurement Manager", f"{row['pass_number']} was reviewed by Procurement Manager.", "Gateway Pass", gp_id)
            _rerun_success("Gateway pass marked reviewed by Procurement Manager.")
        if c2.button("Return for Correction", key=f"gp_pm_return_{gp_id}"):
            if not note.strip(): st.error("Please enter a correction reason."); return
            run_query("UPDATE gateway_passes SET status='Returned for Correction', next_role='facility_manager', rejection_reason=?, updated_at=? WHERE id=?", (note, now_iso(), gp_id))
            log_gateway_event(gp_id, "Returned for Correction", "Returned for Correction", note)
            _notify_gateway_event({**row.to_dict(), "id": gp_id}, "Gateway pass returned", f"{row['pass_number']} was returned for correction. {note}", target="facility", importance="High")
            _rerun_success("Gateway pass returned for correction.")
        if c3.button("Submit to Approver / MD", type="primary", key=f"gp_pm_submit_approver_{gp_id}"):
            run_query("UPDATE gateway_passes SET status='Submitted for Approval', next_role='approver', reviewed_by_user_id=?, reviewed_at=?, procurement_review_note=?, updated_at=? WHERE id=?", (user()["id"], now_iso(), note or "Submitted for final approval", now_iso(), gp_id))
            log_gateway_event(gp_id, "Submitted for Approval", "Submitted for Approval", note)
            create_notification(None, "Approver", "Gateway pass pending final approval", f"{row['pass_number']} requires final approval from Approver / MD.", "Gateway Pass", gp_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")
            create_notification(None, "Admin", "Gateway pass pending final approval", f"{row['pass_number']} requires final approval/oversight.", "Gateway Pass", gp_id, "Important", ["in_app"], action_label="Gateway Pass Management")
            create_notification(int(row["facility_manager_user_id"]), None, "Gateway pass submitted for final approval", f"{row['pass_number']} was reviewed by Procurement Manager and sent to Approver / MD.", "Gateway Pass", gp_id, "Normal", ["in_app"], action_label="Open Gateway Pass")
            _notify_auditors("Gateway pass submitted to Approver / MD", f"{row['pass_number']} was submitted for final approval.", "Gateway Pass", gp_id)
            _rerun_success("Gateway pass submitted to Approver / MD.")
    else:
        c1, c2, c3 = st.columns(3)
        if c1.button("Approve Gateway Pass", type="primary", key=f"gp_approve_cmd_{gp_id}_{role}"):
            _gateway_approve(gp_id, "Approved", note or "Approved.")
        if c2.button("Return for Correction", key=f"gp_return_cmd_{gp_id}_{role}"):
            _gateway_approve(gp_id, "Returned for Correction", note)
        if c3.button("Reject Gateway Pass", key=f"gp_reject_cmd_{gp_id}_{role}"):
            _gateway_approve(gp_id, "Rejected", note)


def edit_gateway_pass_form(gateway_pass_id: int):
    if not _assert_gateway_owner(gateway_pass_id):
        return
    row_df = df_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,))
    if row_df.empty:
        st.error("Gateway pass not found."); return
    row = row_df.iloc[0]
    if row["status"] not in ["Draft", "Returned for Correction"]:
        st.info("Only Draft or Returned for Correction passes can be edited."); return
    st.markdown("#### Edit Gateway Pass")
    with st.form(f"edit_gateway_pass_cmd_{gateway_pass_id}"):
        c1, c2, c3 = st.columns(3)
        dept = c1.selectbox("Department", _gateway_department_options(), index=0, key=f"edit_gp_dept_cmd_{gateway_pass_id}")
        movement_type = selectbox_with_other("Movement type", GATEWAY_MOVEMENT_TYPES, f"edit_gp_mov_cmd_{gateway_pass_id}", "gateway_movement_type")
        expected_movement = c3.date_input("Movement date", pd.to_datetime(row["expected_movement_date"] or date.today()).date(), key=f"edit_gp_date_cmd_{gateway_pass_id}")
        purpose = st.text_area("Purpose", value=row["purpose"] or "", key=f"edit_gp_purpose_cmd_{gateway_pass_id}")
        c4, c5 = st.columns(2)
        origin = c4.text_input("Origin", value=row["origin_location"] or "", key=f"edit_gp_origin_cmd_{gateway_pass_id}")
        destination = c5.text_input("Destination", value=row["destination"] or "", key=f"edit_gp_dest_cmd_{gateway_pass_id}")
        c6, c7, c8 = st.columns(3)
        vehicle = c6.text_input("Vehicle number", value=row["vehicle_number"] or "", key=f"edit_gp_vehicle_cmd_{gateway_pass_id}")
        driver = c7.text_input("Driver name", value=row["driver_name"] or "", key=f"edit_gp_driver_cmd_{gateway_pass_id}")
        driver_phone = c8.text_input("Driver phone", value=row["driver_phone"] or "", key=f"edit_gp_phone_cmd_{gateway_pass_id}")
        c9, c10 = st.columns(2)
        receiver = c9.text_input("Receiver name", value=row["receiver_name"] or "", key=f"edit_gp_receiver_cmd_{gateway_pass_id}")
        checkpoint = c10.text_input("Security checkpoint", value=row["security_checkpoint"] or "", key=f"edit_gp_check_cmd_{gateway_pass_id}")
        submitted = st.form_submit_button("Save Gateway Pass Details")
    if submitted:
        run_query("UPDATE gateway_passes SET department=?, movement_type=?, purpose=?, origin_location=?, destination=?, expected_movement_date=?, vehicle_number=?, driver_name=?, driver_phone=?, receiver_name=?, security_checkpoint=?, updated_at=? WHERE id=?", (dept, movement_type, purpose, origin, destination, expected_movement.isoformat(), vehicle, driver, driver_phone, receiver, checkpoint, now_iso(), gateway_pass_id))
        log_gateway_event(gateway_pass_id, "Gateway Pass Edited", row["status"], "Details updated")
        _rerun_success("Gateway pass details updated.")
    # Reuse existing item editing for stored lines, but new-line add uses unique stable keys.
    items = gateway_pass_items_df(gateway_pass_id)
    if not items.empty:
        st.markdown("#### Existing Item Lines")
        dataframe(items)
    with st.form(f"add_gp_item_cmd_{gateway_pass_id}"):
        st.markdown("##### Add item line")
        c1, c2, c3 = st.columns(3)
        desc = c1.text_input("Item description", key=f"add_item_desc_cmd_{gateway_pass_id}")
        category = c2.text_input("Item category", key=f"add_item_cat_cmd_{gateway_pass_id}")
        qty = c3.number_input("Quantity", min_value=0.01, value=1.0, step=1.0, key=f"add_item_qty_cmd_{gateway_pass_id}")
        c4, c5, c6, c7 = st.columns(4)
        unit = selectbox_with_other("Unit", GATEWAY_UOMS, f"add_item_unit_cmd_{gateway_pass_id}", "gateway_uom")
        quality = selectbox_with_other("Quality / condition", GATEWAY_QUALITY_OPTIONS, f"add_item_quality_cmd_{gateway_pass_id}", "gateway_quality")
        fragile = c6.selectbox("Fragility", GATEWAY_FRAGILITY_OPTIONS, key=f"add_item_fragile_cmd_{gateway_pass_id}")
        colour = c7.text_input("Colour", key=f"add_item_colour_cmd_{gateway_pass_id}")
        handling = st.text_input("Handling instruction", key=f"add_item_handling_cmd_{gateway_pass_id}")
        remarks = st.text_input("Remarks", key=f"add_item_remarks_cmd_{gateway_pass_id}")
        add_item = st.form_submit_button("Add Item")
    if add_item:
        if not desc.strip() or qty <= 0 or not unit.strip() or unit == "Other":
            st.error("Description, quantity greater than 0, and a valid unit are required.")
        else:
            run_query("INSERT INTO gateway_pass_items (gateway_pass_id, item_description, item_category, quantity, unit_of_measure, quality_condition, fragility_status, colour, handling_instruction, remarks, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (gateway_pass_id, desc, category, qty, unit, quality, fragile, colour, handling, remarks, now_iso()))
            log_gateway_event(gateway_pass_id, "Gateway Pass Item Added", row["status"], desc)
            st.rerun()


def _fmt_clean_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return pd.to_datetime(value).strftime("%d %B %Y")
    except Exception:
        return str(value)[:10]


def generate_gateway_pass_document(gateway_pass_id: int) -> str | None:
    if user()["role"] == "Facility Manager" and not _assert_gateway_owner(gateway_pass_id):
        return None
    gp = gateway_pass_summary_df("gp.id=?", (gateway_pass_id,))
    if gp.empty:
        st.error("Gateway pass not found."); return None
    row = gp.iloc[0]
    if row["status"] not in ["Approved", "Generated", "Downloaded", "Completed"]:
        st.error("Generate is disabled until the gateway pass is approved by Admin or Approver / MD."); return None
    items = gateway_pass_items_df(gateway_pass_id)
    if items.empty:
        st.error("Cannot generate a gateway pass without item lines."); return None

    def _ptext(value: Any, default: str = "") -> str:
        return escape(_clean(value, default)).replace("\n", "<br/>")

    def _first_item_text(key: str, default: str = "") -> str:
        if items.empty:
            return default
        return _clean(items.iloc[0].get(key), default)

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, KeepTogether

        out_dir = Path("data/attachments/gateway_passes")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{str(row['pass_number']).replace('/', '_')}.pdf"
        doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=16*mm, leftMargin=16*mm, topMargin=12*mm, bottomMargin=12*mm)
        styles = getSampleStyleSheet()
        normal = ParagraphStyle("gp_normal_template", parent=styles["Normal"], fontName="Helvetica", fontSize=9.2, leading=13, spaceAfter=4)
        body = ParagraphStyle("gp_body_template", parent=normal, fontSize=10.2, leading=15)
        title_style = ParagraphStyle("gp_title_template", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=14.5, leading=17, alignment=1, textColor=colors.black, spaceAfter=1)
        sub_style = ParagraphStyle("gp_sub_template", parent=title_style, fontSize=13, leading=15)
        motto_style = ParagraphStyle("gp_motto_template", parent=normal, fontName="Helvetica-BoldOblique", fontSize=8.8, leading=11, alignment=1)
        section_style = ParagraphStyle("gp_section_template", parent=styles["Heading3"], fontName="Helvetica-Bold", fontSize=10.8, leading=13, spaceBefore=8, spaceAfter=5, textColor=colors.black)
        small = ParagraphStyle("gp_small_template", parent=normal, fontSize=7.8, leading=9.5)
        cell = ParagraphStyle("gp_cell_template", parent=normal, fontSize=7.2, leading=8.6)

        story = []
        cmotd_path = _gateway_asset_path("cmotd_logo.png")
        rsu_path = _gateway_asset_path("rsu_logo.png")
        left_logo = Image(str(cmotd_path), width=22*mm, height=20*mm) if cmotd_path.exists() else Paragraph("CMOTD", normal)
        right_logo = Image(str(rsu_path), width=21*mm, height=20*mm) if rsu_path.exists() else Paragraph("RSU", normal)
        header_mid = [
            Paragraph("Center For Marine and Offshore Technology Development (CMOTD)", title_style),
            Paragraph("Consultancy Services Unit, Rivers State University", sub_style),
            Paragraph("Where Theory becomes Reality and Individuals are Equipped to Lead in the Industry!", motto_style),
        ]
        header = Table([[left_logo, header_mid, right_logo]], colWidths=[25*mm, 128*mm, 25*mm])
        header.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("ALIGN", (0,0), (0,0), "LEFT"),
            ("ALIGN", (2,0), (2,0), "RIGHT"),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
        ]))
        story.append(header)
        story.append(Spacer(1, 7))
        story.append(Paragraph("PROPERTY MOVEMENT GATE PASS", section_style))

        ref_table = Table([
            [Paragraph("<b>Reference No.:</b>", normal), Paragraph(_ptext(row.get("pass_number")), normal)],
            [Paragraph("<b>Date:</b>", normal), Paragraph(_fmt_clean_date(row.get("approved_at")) or date.today().strftime("%d %B %Y"), normal)],
            [Paragraph("<b>Department:</b>", normal), Paragraph(_ptext(row.get("department")), normal)],
            [Paragraph("<b>Movement Type:</b>", normal), Paragraph(_ptext(row.get("movement_type")), normal)],
            [Paragraph("<b>Movement Date:</b>", normal), Paragraph(_fmt_clean_date(row.get("expected_movement_date")), normal)],
            [Paragraph("<b>Origin:</b>", normal), Paragraph(_ptext(row.get("origin_location"), "N/A"), normal)],
            [Paragraph("<b>Destination:</b>", normal), Paragraph(_ptext(row.get("destination"), "N/A"), normal)],
        ], colWidths=[33*mm, 145*mm])
        ref_table.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(ref_table)
        story.append(Spacer(1, 4))
        story.append(Paragraph("This Gate Pass serves as official authorization for the movement of the underlisted company asset(s) from the premises of the Centre for Marine and Offshore Technology Development (CMOTD).", body))

        story.append(Paragraph("PROPERTY DETAILS", section_style))
        if len(items) == 1:
            item_text = _first_item_text("item_description", "listed company asset")
            quantity_text = f"{_qty_text(_first_item_text('quantity', '0'))} {_first_item_text('unit_of_measure', '')}".strip()
            prop_rows = [
                [Paragraph("Item Description:", normal), Paragraph(_ptext(item_text), normal)],
                [Paragraph("Colour:", normal), Paragraph(_ptext(_first_item_text("colour", "N/A")), normal)],
                [Paragraph("Quantity:", normal), Paragraph(_ptext(quantity_text), normal)],
                [Paragraph("Condition:", normal), Paragraph(_ptext(_first_item_text("quality_condition", "N/A")), normal)],
            ]
            prop_table = Table(prop_rows, colWidths=[32*mm, 146*mm])
            prop_table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 0), ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))
            story.append(prop_table)
        else:
            story.append(Paragraph(f"Item Description: {len(items)} listed item lines. Full details are shown below.", normal))

        data = [[Paragraph("No.", cell), Paragraph("Item Description", cell), Paragraph("Colour", cell), Paragraph("Qty", cell), Paragraph("Unit", cell), Paragraph("Condition", cell), Paragraph("Serial / Asset Tag", cell), Paragraph("Remarks", cell)]]
        for idx, it in enumerate(items.itertuples(), 1):
            data.append([
                str(idx), Paragraph(_ptext(getattr(it, "item_description", "")), cell), _clean(getattr(it, "colour", ""), "-"), _qty_text(getattr(it, "quantity", "")), _clean(getattr(it, "unit_of_measure", ""), "-"),
                _clean(getattr(it, "quality_condition", ""), "-"), Paragraph(_ptext(" / ".join([x for x in [_clean(getattr(it, "serial_number", "")), _clean(getattr(it, "asset_tag", ""))] if x]) or "-"), cell), Paragraph(_ptext(getattr(it, "remarks", "") or getattr(it, "handling_instruction", "") or "-"), cell)
            ])
        item_table = Table(data, colWidths=[8*mm, 51*mm, 19*mm, 12*mm, 17*mm, 23*mm, 27*mm, 21*mm], repeatRows=1)
        item_table.setStyle(TableStyle([
            ("GRID", (0,0), (-1,-1), .25, colors.grey),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f1f5f9")),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 7),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("PADDING", (0,0), (-1,-1), 3),
        ]))
        story.append(item_table)

        story.append(Paragraph("PURPOSE OF MOVEMENT", section_style))
        purpose_sentence = row.get("purpose") or "movement as approved by Management"
        story.append(Paragraph(f"The above-mentioned asset(s) is/are being moved for {escape(str(purpose_sentence))}. Security personnel are hereby requested to permit the approved movement.", body))

        story.append(Paragraph("TRANSPORT DETAILS", section_style))
        line_style = TableStyle([
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 7),
            ("LINEBELOW", (1,0), (1,-1), .55, colors.black),
            ("LINEBELOW", (3,0), (3,-1), .55, colors.black),
        ])
        transport = [
            [Paragraph("Driver's Name:", normal), Paragraph(_ptext(row.get("driver_name")), normal), Paragraph("Driver's Phone Number:", normal), Paragraph(_ptext(row.get("driver_phone")), normal)],
            [Paragraph("Vehicle Number:", normal), Paragraph(_ptext(row.get("vehicle_number")), normal), Paragraph("Receiver Name:", normal), Paragraph(_ptext(row.get("receiver_name")), normal)],
        ]
        story.append(Table(transport, colWidths=[33*mm, 55*mm, 42*mm, 48*mm], style=line_style))

        story.append(Paragraph("AUTHORIZATION", section_style))
        story.append(Paragraph("I hereby certify that the movement of the above company property has been duly approved and authorized.", body))
        auth = [
            [Paragraph("Authorizing Officer:", normal), Paragraph(_ptext(row.get("approved_by")), normal), Paragraph("Designation:", normal), Paragraph(_ptext(display_role(row.get("approved_by_role"))), normal)],
            [Paragraph("Signature:", normal), Paragraph("", normal), Paragraph("Date:", normal), Paragraph(_fmt_clean_date(row.get("approved_at")), normal)],
        ]
        story.append(Table(auth, colWidths=[35*mm, 54*mm, 27*mm, 62*mm], style=line_style))

        story.append(Paragraph("SECURITY VERIFICATION", section_style))
        security = [
            [Paragraph("Security Officer Name:", normal), Paragraph("", normal), Paragraph("Gate Verification Time:", normal), Paragraph("", normal)],
            [Paragraph("Exit / Entry Confirmation:", normal), Paragraph("", normal), Paragraph("Security Signature:", normal), Paragraph("", normal)],
        ]
        story.append(Table(security, colWidths=[42*mm, 46*mm, 44*mm, 46*mm], style=line_style))
        story.append(Spacer(1, 7))
        story.append(Paragraph("Consultancy Unit, Rivers State University, Nkpolu-Oroworokwo, Port Harcourt, Rivers State", ParagraphStyle("gp_footer_addr", parent=normal, alignment=1, fontSize=8)))
        story.append(Paragraph("Email: info@cmotd.org &nbsp;&nbsp; Phone NO.: +2349163505000", ParagraphStyle("gp_footer_contact", parent=normal, alignment=1, fontSize=8)))
        doc.build(story)
        run_query("UPDATE gateway_passes SET status='Generated', next_role=NULL, generated_at=?, generated_file_path=?, updated_at=? WHERE id=?", (now_iso(), str(path), now_iso(), gateway_pass_id))
        log_gateway_event(gateway_pass_id, "Gateway Pass Generated", "Generated", str(path))
        log_audit("GATEWAY_PASS_GENERATED", "Gateway Pass", gateway_pass_id, str(path), user()["id"], user()["role"])
        return str(path)
    except Exception as exc:
        st.error(f"Could not generate PDF gateway pass: {exc}")
        return None

def gateway_pass_register(where_sql: str, params: tuple | list, title: str, allow_submit: bool = False, allow_generate: bool = False, key_prefix: str = "gp_register"):
    st.subheader(title)
    df = gateway_pass_summary_df(where_sql, params)
    if df.empty:
        empty_state("No gateway passes", "Gateway pass records will appear here."); return
    show_cols = ["id", "pass_number", "facility_manager", "department", "movement_type", "destination", "expected_movement_date", "status", "approved_by", "updated_at"]
    show = df[[c for c in show_cols if c in df.columns]].copy()
    dataframe(show.drop(columns=["id"]) if "id" in show.columns else show)
    selected = st.selectbox("Open gateway pass", [f"{r.pass_number} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key=f"{key_prefix}_select_cmd")
    gp_id = int(selected.rsplit("#", 1)[1])
    row = df[df["id"] == gp_id].iloc[0]
    gateway_pass_detail(gp_id)
    if _is_utility() and row["status"] in ["Draft", "Returned for Correction"]:
        edit_gateway_pass_form(gp_id)
    events = df_query("SELECT event, status, note, user_id, created_at FROM gateway_pass_events WHERE gateway_pass_id=? ORDER BY created_at DESC", (gp_id,))
    with st.expander("Gateway pass history", expanded=False):
        dataframe(events) if not events.empty else st.info("No events yet.")
    if allow_submit and row["status"] in ["Draft", "Returned for Correction"]:
        c1, c2 = st.columns(2)
        if c1.button("Send to Procurement Manager", type="primary", key=f"{key_prefix}_submit_cmd_{gp_id}"):
            submit_gateway_pass(gp_id)
        if c2.button("Delete draft", key=f"{key_prefix}_delete_cmd_{gp_id}"):
            if can_delete_draft(user()["role"], int(row.get("facility_manager_user_id") or 0), user()["id"], row["status"]):
                run_query("DELETE FROM gateway_pass_items WHERE gateway_pass_id=?", (gp_id,)); run_query("DELETE FROM gateway_passes WHERE id=?", (gp_id,))
                log_audit("DRAFT_DELETED", "Gateway Pass", gp_id, row.get("pass_number"), user()["id"], user()["role"], before_values={"status": row["status"]})
                _rerun_success("Gateway pass draft deleted.")
            else:
                st.error("Only your own unsubmitted gateway pass draft can be deleted.")
    if allow_generate:
        ready = row["status"] in ["Approved", "Generated", "Downloaded", "Completed"]
        if ready:
            render_gateway_pass_preview(gp_id)
        else:
            st.info("The final preview and Generate button unlock after final approval by Admin or Approver / MD.")
        if st.button("Generate Final Gateway Pass PDF", type="primary", key=f"{key_prefix}_generate_cmd_{gp_id}", disabled=not ready):
            path = generate_gateway_pass_document(gp_id)
            if path:
                _rerun_success("Gateway pass PDF generated. It has left the ready-to-generate queue and remains in History.")
        refreshed = gateway_pass_summary_df("gp.id=?", (gp_id,)).iloc[0]
        if refreshed["status"] in ["Generated", "Downloaded", "Completed"] or refreshed.get("generated_file_path"):
            st.markdown("#### Download")
            gateway_pass_download_button(refreshed)


def facility_gateway_pass_page():
    uid = int(user()["id"])
    ready_count = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status='Approved'", (uid,)).iloc[0, 0])
    returned_count = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE facility_manager_user_id=? AND status IN ('Returned for Correction','Returned')", (uid,)).iloc[0, 0])
    sections = ["Create Draft", "Drafts / Returned", "Ready to Generate", "History"]
    # When an approval routes back to the Facility Head, land on the unlocked
    # generation queue instead of leaving the user on Create Draft.
    if ready_count and st.session_state.get("facility_gp_sections_cmd") in (None, "Create Draft"):
        st.session_state["facility_gp_sections_cmd"] = "Ready to Generate"
    section = st.radio("Gateway Pass", sections, horizontal=True, key="facility_gp_sections_cmd")
    if ready_count:
        st.success(f"{ready_count} gateway pass(es) are approved and ready to generate.")
    if returned_count:
        st.warning(f"{returned_count} gateway pass(es) were returned for correction.")
    if section == "Create Draft":
        create_gateway_pass_form()
    elif section == "Drafts / Returned":
        gateway_pass_register("gp.facility_manager_user_id=? AND gp.status IN ('Draft','Returned for Correction','Returned')", (uid,), "Drafts / Returned", allow_submit=True, key_prefix="facility_gp_drafts_cmd")
    elif section == "Ready to Generate":
        # Ready queue must contain only approved, not already generated/downloaded.
        # Generated items leave this queue and remain available under History.
        gateway_pass_register("gp.facility_manager_user_id=? AND gp.status='Approved'", (uid,), "Approved Gateway Passes Ready to Generate", allow_generate=True, key_prefix="facility_gp_ready_cmd")
    else:
        gateway_pass_register("gp.facility_manager_user_id=?", (uid,), "Gateway Pass History", allow_generate=True, key_prefix="facility_gp_history_cmd")


def finance_ready_df() -> pd.DataFrame:
    req = df_query(
        """
        SELECT 'Purchase Request' entity_type, pr.id entity_id, pr.request_no "Request number", '' "PO number", '' Vendor,
               pr.department_project Department, pr.category Category, pr.estimated_amount Amount,
               COALESCE(ah.approved_by_role, pr.approved_by_role) "Approved by", COALESCE(pr.approved_at, ah.created_at) "Approval date",
               COALESCE(pr.payment_status,'Approved for Payment') "Current payment status", 'Pay and upload receipt' "Required finance action"
        FROM purchase_requests pr
        LEFT JOIN approval_history ah ON ah.entity_type='Purchase Request' AND ah.entity_id=pr.id AND ah.status_after='Approved'
        WHERE (pr.next_role='finance' OR pr.status IN ('Approved','Awaiting Payment','Approved for Payment') OR pr.payment_status='Approved for Payment')
          AND pr.status NOT IN ('Paid','Completed','Closed','Rejected')
        ORDER BY COALESCE(pr.approved_at, pr.updated_at, pr.created_at) DESC
        """
    )
    po = df_query(
        """
        SELECT 'Purchase Order' entity_type, po.id entity_id, COALESCE(pr.request_no,'') "Request number", po.po_no "PO number", COALESCE(v.name,'') Vendor,
               COALESCE(pr.department_project,'') Department, COALESCE(pr.category,'') Category, po.total_amount Amount,
               COALESCE(u.full_name, po.approved_by_role) "Approved by", po.updated_at "Approval date",
               COALESCE(po.payment_status,'Unpaid') "Current payment status", 'Pay and upload receipt' "Required finance action"
        FROM purchase_orders po
        LEFT JOIN purchase_requests pr ON pr.id=po.request_id
        LEFT JOIN vendors v ON v.id=po.vendor_id
        LEFT JOIN users u ON u.id=po.approved_by
        WHERE po.status='Approved' AND COALESCE(po.payment_status,'Unpaid') NOT IN ('Paid')
        ORDER BY po.updated_at DESC
        """
    )
    return pd.concat([req, po], ignore_index=True) if not req.empty or not po.empty else pd.DataFrame()


def approved_for_payment_page():
    st.subheader("Approved for Payment")
    st.caption("Finance only sees items already approved by Admin or Approver / MD. There are no approval buttons here.")
    if not can_pay(_current_role()):
        st.warning("Only Finance/Admin can record payments."); return
    df = finance_ready_df()
    if df.empty:
        empty_state("No approved payment items", "Approved requests and POs will appear here after final approval.")
        return
    display = df.copy(); display["Amount"] = display["Amount"].apply(money)
    dataframe(display)
    selected = st.selectbox("Open finance item", [f"{r.entity_type} | {getattr(r, 'Request_number', '')} | #{int(r.entity_id)}" for r in df.rename(columns={"Request number":"Request_number"}).itertuples()], key="finance_ready_select_cmd")
    entity_id = int(selected.rsplit("#", 1)[1]); entity_type = selected.split(" | ", 1)[0]
    row = df[(df["entity_id"] == entity_id) & (df["entity_type"] == entity_type)].iloc[0]
    note = st.text_area("Finance note", key=f"finance_note_cmd_{entity_type}_{entity_id}")
    method = st.selectbox("Payment method", RECEIPT_PAYMENT_METHODS if "RECEIPT_PAYMENT_METHODS" in globals() else PAYMENT_METHODS, key=f"finance_method_cmd_{entity_type}_{entity_id}")
    proof = st.file_uploader("Upload receipt / payment proof", type=["pdf", "jpg", "jpeg", "png"], key=f"finance_proof_cmd_{entity_type}_{entity_id}")
    c1, c2 = st.columns(2)
    if c1.button("Mark Paid + Upload Receipt", type="primary", key=f"finance_paid_cmd_{entity_type}_{entity_id}"):
        if proof is None:
            st.error("Upload payment proof/receipt before completing the item."); return
        path, _ = save_upload(proof, "payments")
        amount = float(row["Amount"] or 0)
        pno = make_ref("PAY")
        if entity_type == "Purchase Request":
            pay_id = run_insert("INSERT INTO payments (payment_no, amount, payment_method, payment_date, status, paid_by, notes, proof_path, created_by, created_at, updated_at, next_role) VALUES (?, ?, ?, ?, 'Paid', ?, ?, ?, ?, ?, ?, 'auditor')", (pno, amount, method, date.today().isoformat(), user()["id"], note, path, user()["id"], now_iso(), now_iso()))
            receipt_no = make_ref("RCT")
            rid = run_insert("INSERT INTO receipt_records (receipt_no, receipt_type, payment_method, payment_date, amount, purpose, linked_payment_id, status, file_path, notes, uploaded_by, created_at, updated_at) VALUES (?, 'Payment Receipt', ?, ?, ?, ?, ?, 'Recorded', ?, ?, ?, ?, ?)", (receipt_no, method, date.today().isoformat(), amount, row.get("Request number") or pno, pay_id, path, note, user()["id"], now_iso(), now_iso()))
            run_query("UPDATE payments SET receipt_id=? WHERE id=?", (rid, pay_id))
            transition_request_status(entity_id, "Paid", "Payment Completed", note or "Finance paid and uploaded receipt.", user()["id"], user()["role"], payment_status="Paid")
            run_query("UPDATE purchase_requests SET next_role='procurement_manager', paid_at=COALESCE(paid_at, ?), receipt_uploaded_at=? WHERE id=?", (now_iso(), now_iso(), entity_id))
            create_notification(None, "Procurement Manager", "Paid request ready for closure", f"{row.get('Request number') or 'A request'} has been paid. Please complete, close and archive it.", "Purchase Request", entity_id, "High", ["in_app", "browser_push"], action_label="Post-Payment Closure")
            _notify_auditors("Payment completed", f"{row.get('Request number') or 'A request'} was paid by Finance and sent to Procurement Manager for closure.", "Purchase Request", entity_id)
        else:
            po = df_query("SELECT * FROM purchase_orders WHERE id=?", (entity_id,)).iloc[0]
            pay_id = run_insert("INSERT INTO payments (payment_no, po_id, vendor_id, amount, payment_method, payment_date, status, paid_by, notes, proof_path, created_by, created_at, updated_at, next_role) VALUES (?, ?, ?, ?, ?, ?, 'Paid', ?, ?, ?, ?, ?, ?, 'auditor')", (pno, entity_id, po.get("vendor_id"), amount, method, date.today().isoformat(), user()["id"], note, path, user()["id"], now_iso(), now_iso()))
            receipt_no = make_ref("RCT")
            rid = run_insert("INSERT INTO receipt_records (receipt_no, receipt_type, payment_method, payment_date, vendor_id, amount, purpose, linked_payment_id, status, file_path, notes, uploaded_by, created_at, updated_at) VALUES (?, 'Payment Receipt', ?, ?, ?, ?, ?, ?, 'Recorded', ?, ?, ?, ?, ?)", (receipt_no, method, date.today().isoformat(), po.get("vendor_id"), amount, row.get("PO number") or pno, pay_id, path, note, user()["id"], now_iso(), now_iso()))
            run_query("UPDATE payments SET receipt_id=? WHERE id=?", (rid, pay_id))
            run_query("UPDATE purchase_orders SET payment_status='Paid', status='Paid', updated_at=? WHERE id=?", (now_iso(), entity_id))
            if po.get("request_id"):
                transition_request_status(int(po["request_id"]), "Paid", "Payment Completed", note or "PO paid and receipt uploaded.", user()["id"], user()["role"], payment_status="Paid")
                run_query("UPDATE purchase_requests SET next_role='procurement_manager', paid_at=COALESCE(paid_at, ?), receipt_uploaded_at=? WHERE id=?", (now_iso(), now_iso(), int(po["request_id"])))
                create_notification(None, "Procurement Manager", "Paid request ready for closure", "A PO-linked request has been paid. Please complete, close and archive it.", "Purchase Request", int(po["request_id"]), "High", ["in_app", "browser_push"], action_label="Post-Payment Closure")
                _notify_auditors("Payment completed", "A PO-linked request was paid by Finance and sent to Procurement Manager for closure.", "Purchase Request", int(po["request_id"]))
        log_audit("PAYMENT_COMPLETED", entity_type, entity_id, {"payment_no": pno, "amount": amount, "receipt": path}, user()["id"], user()["role"], after_values={"status": "Paid", "next_role": "procurement_manager"})
        _rerun_success("Payment recorded and receipt uploaded. Procurement Manager has been notified to complete, close and archive the record.")
    if c2.button("Add finance note only", key=f"finance_note_only_cmd_{entity_type}_{entity_id}"):
        if entity_type == "Purchase Request":
            run_query("UPDATE purchase_requests SET finance_note=?, updated_at=? WHERE id=?", (note, now_iso(), entity_id))
        else:
            add_workflow_event("Purchase Order", entity_id, "Finance Note Added", None, note, user()["id"])
        log_audit("FINANCE_NOTE_ADDED", entity_type, entity_id, note, user()["id"], user()["role"])
        st.success("Finance note saved.")


def payments_page():
    st.subheader("Payments")
    _ensure_finance_doc_schema_ui()
    role = _current_role()
    if role == "Finance":
        st.info("Finance cannot create or approve payment requests. Finance can only pay approved items and upload receipts.")
    df = df_query("SELECT p.id, p.payment_no, v.name vendor, p.amount, p.payment_method, p.payment_date, p.status, p.notes, p.proof_path, p.finance_note FROM payments p LEFT JOIN vendors v ON p.vendor_id=v.id ORDER BY p.created_at DESC")
    if not df.empty:
        show = df.drop(columns=["id"]).copy(); show["amount"] = show["amount"].apply(money); dataframe(show); csv_download(show, "payments")
    else:
        st.info("No payment records yet.")
    if can_create_payment_request(role):
        st.markdown("##### Manual Payment Request")
        with st.form("manual_payment_cmd"):
            vendors = vendor_options(False)
            v = st.selectbox("Vendor", list(vendors.keys()), key="manual_pay_vendor_cmd")
            amount = st.number_input("Amount", min_value=0.0, step=1000.0, key="manual_pay_amount_cmd")
            method = st.selectbox("Method", RECEIPT_PAYMENT_METHODS if "RECEIPT_PAYMENT_METHODS" in globals() else PAYMENT_METHODS, key="manual_pay_method_cmd")
            notes = st.text_area("Notes", key="manual_pay_notes_cmd")
            submitted = st.form_submit_button("Create Payment Request")
        if submitted:
            pno = make_ref("PAY")
            pay_id = run_insert("INSERT INTO payments (payment_no, vendor_id, amount, payment_method, status, notes, created_by, created_at, updated_at, next_role) VALUES (?, ?, ?, ?, 'Pending Approval', ?, ?, ?, ?, 'approver')", (pno, vendors[v], amount, method, notes, user()["id"], now_iso(), now_iso()))
            add_workflow_event("Payment", pay_id, "Created", "Pending Approval", pno, user()["id"])
            create_notification(None, "Approver", "Payment pending approval", f"{pno} requires approval", "Payment", pay_id, "High", ["in_app", "browser_push"])
            _rerun_success("Payment request created.")
    if not df.empty and can_approve(role):
        selected = st.selectbox("Approve payment request", df["payment_no"].tolist(), key="payment_select_cmd")
        row = df[df["payment_no"] == selected].iloc[0]
        finance_note = st.text_area("Approval note", key=f"payment_note_cmd_{int(row['id'])}")
        c1, c2 = st.columns(2)
        if row["status"] == "Pending Approval" and c1.button("Approve Payment", key=f"pay_approve_cmd_{int(row['id'])}"):
            from core.db import transition_payment_status
            transition_payment_status(int(row["id"]), "Approved", finance_note or "Payment approved.", user()["id"], role)
            _rerun_success("Payment request approved.")
        if row["status"] == "Pending Approval" and c2.button("Reject Payment", key=f"pay_reject_cmd_{int(row['id'])}"):
            from core.db import transition_payment_status
            transition_payment_status(int(row["id"]), "Rejected", finance_note or "Payment rejected.", user()["id"], role)
            _rerun_success("Payment request rejected.")


def invoices_page():
    # Preserve the original invoice register behavior where possible, but remove Finance payment-request creation.
    st.subheader("Invoices")
    st.caption("Finance can upload/review invoices. Finance cannot create payment approval requests from invoices.")
    df = df_query("""
        SELECT inv.id, inv.invoice_no, inv.invoice_type, po.po_no, v.name vendor, inv.invoice_date, inv.due_date, inv.total_amount, inv.balance_due, inv.match_status, inv.mismatch_reasons, inv.status
        FROM invoices inv LEFT JOIN purchase_orders po ON inv.po_id=po.id LEFT JOIN vendors v ON inv.vendor_id=v.id ORDER BY inv.created_at DESC
    """)
    if df.empty:
        st.info("No invoices yet. Use the OCR/import pages to upload supplier invoices."); return
    show = df.drop(columns=["id"]).copy(); show["total_amount"] = show["total_amount"].apply(money); show["balance_due"] = show["balance_due"].apply(money); dataframe(show)
    selected = st.selectbox("Select invoice", [f"{r.invoice_no} — {r.status} — #{int(r.id)}" for r in df.itertuples()], key="invoice_select_cmd_restricted")
    inv_id = int(selected.rsplit("#", 1)[1])
    inv = df_query("SELECT * FROM invoices WHERE id=?", (inv_id,)).iloc[0]
    dataframe(_redact_ui_df(pd.DataFrame([inv.to_dict()]), "invoices"))
    if has_permission("review_invoice"):
        c1, c2 = st.columns(2)
        if c1.button("Mark Finance Review Complete", key=f"invoice_reviewed_cmd_{inv_id}"):
            run_query("UPDATE invoices SET status='Finance Review', approval_status='Reviewed' WHERE id=?", (inv_id,)); add_workflow_event("Invoice", inv_id, "Finance Review", "Finance Review", "Invoice reviewed", user()["id"]); st.rerun()
        if c2.button("Return Invoice", key=f"invoice_return_cmd_{inv_id}"):
            run_query("UPDATE invoices SET status='Returned', approval_status='Returned' WHERE id=?", (inv_id,)); add_workflow_event("Invoice", inv_id, "Returned", "Returned", "Invoice returned for clarification", user()["id"]); st.rerun()
    if can_create_payment_request(_current_role()):
        if st.button("Create Payment Request for Approval", key=f"invoice_payment_request_cmd_{inv_id}"):
            pno = make_ref("PAY")
            pay_id = run_insert("INSERT INTO payments (payment_no, invoice_id, po_id, vendor_id, amount, payment_method, status, created_by, created_at, updated_at, next_role) SELECT ?, id, po_id, vendor_id, total_amount, 'Bank Transfer', 'Pending Approval', ?, ?, ?, 'approver' FROM invoices WHERE id=?", (pno, user()["id"], now_iso(), now_iso(), inv_id))
            create_notification(None, "Approver", "Payment pending approval", f"{pno} requires approval", "Payment", pay_id, "High", ["in_app", "browser_push"])
            add_workflow_event("Payment", pay_id, "Created from Invoice", "Pending Approval", pno, user()["id"])
            st.success(f"Payment request {pno} created.")
    csv_download(show, "invoices")


def finance_dashboard():
    st.subheader("Finance Attention Center")
    metric_row([
        ("Awaiting Payment", int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE next_role='finance' OR status IN ('Approved','Awaiting Payment','Approved for Payment') OR payment_status='Approved for Payment'").iloc[0,0]), "queue"),
        ("Pending Receipt", int(df_query("SELECT COUNT(*) FROM payments WHERE status='Paid' AND (receipt_id IS NULL OR receipt_id='')").iloc[0,0]), "queue"),
        ("Total Paid", int(df_query("SELECT COUNT(*) FROM payments WHERE status='Paid'").iloc[0,0]), "cumulative"),
        ("Completed", int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Completed','Closed')").iloc[0,0]), "cumulative"),
    ], cols=4)
    c1, c2 = st.columns(2)
    with c1:
        df = df_query("SELECT payment_method, SUM(amount) total FROM receipt_records GROUP BY payment_method ORDER BY total DESC")
        interactive_chart(_money_chart_df(df), "Receipts by Payment Method", "payment_method", "total", "finance_receipts_method_cmd", default="Bar")
    with c2:
        df = df_query("SELECT status, COUNT(*) count FROM payments GROUP BY status ORDER BY count DESC")
        interactive_chart(df, "Payment Status Distribution", "status", "count", "finance_payment_status_cmd", default="Donut")


def finance_workspace():
    role_header("Finance Workspace", "Pay only approved items, upload receipts, and maintain income/budget records. Finance cannot approve anything.")
    section = st.session_state.get("finance_section", "Financial Dashboard")
    if section == "Financial Dashboard":
        finance_metrics(); finance_dashboard()
    elif section == "Approved for Payment":
        approved_for_payment_page()
    elif section == "Receipts":
        receipts_page()
    elif section == "Invoices":
        invoices_page()
    elif section == "Expenses":
        expenses_page()
    elif section == "Payments":
        payments_page()
    elif section == "Cash Advances":
        cash_advances_page()
    elif section == "Budgets":
        budgets_page()
    elif section == "Income":
        income_page(manage=True)
    elif section == "Vendor Payment Records":
        payments_page()
    elif section == "Reconciliation":
        reconciliation_page()
    elif section == "Financial Reports":
        finance_reports()
    elif section == "Settings":
        settings_page()
    else:
        finance_metrics(); finance_dashboard()


def executive_dashboard():
    st.subheader("Executive Decision Center")
    pending = int(df_query("SELECT COUNT(*) c FROM purchase_requests WHERE next_role='approver' OR status IN ('Submitted for Approval','Pending Approval','Pending Approver/MD Approval')").iloc[0, 0])
    gp_waiting = int(df_query("SELECT COUNT(*) c FROM gateway_passes WHERE next_role='approver' OR status='Submitted for Approval'").iloc[0, 0])
    metric_row([
        ("Pending approvals", pending, None),
        ("Gateway approvals", gp_waiting, None),
        ("Total approved", int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Approved','Awaiting Payment','Paid','Completed','Closed')").iloc[0,0]), "cumulative"),
    ], cols=3)
    df = df_query("SELECT request_no, department_project, category, estimated_amount, status, updated_at FROM purchase_requests WHERE next_role='approver' OR status IN ('Submitted for Approval','Pending Approval','Pending Approver/MD Approval') ORDER BY estimated_amount DESC LIMIT 30")
    if not df.empty:
        df["estimated_amount"] = df["estimated_amount"].apply(money); dataframe(df)
    c1, c2 = st.columns(2)
    with c1:
        by_cat = df_query("SELECT category, COUNT(*) count FROM purchase_requests WHERE next_role='approver' OR status IN ('Submitted for Approval','Pending Approval','Pending Approver/MD Approval') GROUP BY category ORDER BY count DESC")
        interactive_chart(by_cat, "Pending Approvals by Category", "category", "count", "exec_pending_cat_cmd", default="Bar")
    with c2:
        by_value = df_query("SELECT category, SUM(estimated_amount) total FROM purchase_requests WHERE next_role='approver' OR status IN ('Submitted for Approval','Pending Approval','Pending Approver/MD Approval') GROUP BY category ORDER BY total DESC")
        interactive_chart(_money_chart_df(by_value), "Approval Value by Category", "category", "total", "exec_value_cat_cmd", default="Donut")


def executive_workspace():
    role_header("Approver / MD Workspace", "Final approval authority for procurement, gateway passes, POs, and payment approval requests.")
    section = st.session_state.get("executive_section", "Approval Dashboard")
    if section == "Approval Dashboard":
        executive_metrics(); executive_dashboard()
    elif section == "Pending Approvals":
        pending_approval_page()
    elif section == "Quote Comparison":
        quote_comparison_decision_page()
    elif section == "PO Approval":
        po_approval_page()
    elif section == "Payment Approval":
        payment_approval_page()
    elif section == "Gateway Pass Approval":
        gateway_pass_review_queue("Gateway Pass Approval")
    elif section == "Availability / Away Notice":
        availability_panel()
    elif section == "My Approval History":
        my_approval_history_page()
    elif section == "Income":
        income_page(manage=False)
    elif section == "Settings":
        settings_page()
    else:
        executive_metrics(); executive_dashboard()


def _month_year_filters(key_prefix: str):
    today = date.today()
    c1, c2, c3, c4, c5 = st.columns(5)
    month = c1.selectbox("Month", list(range(1, 13)), index=today.month-1, key=f"{key_prefix}_month")
    year = c2.number_input("Year", min_value=2020, max_value=2100, value=today.year, step=1, key=f"{key_prefix}_year")
    dept_options = ["All"] + department_options()
    dept = c3.selectbox("Department", dept_options, key=f"{key_prefix}_dept")
    project = c4.text_input("Project", value="", key=f"{key_prefix}_project")
    status = c5.selectbox("Status", ["All", "Approved", "Awaiting Payment", "Paid", "Completed", "Rejected"], key=f"{key_prefix}_status")
    return int(month), int(year), dept, project.strip(), status


def _income_summary(month: int, year: int, dept: str = "All", project: str = "", status: str = "All") -> dict[str, float]:
    mk = f"{year:04d}-{month:02d}"
    params: list[Any] = [mk]
    inc_sql = "SELECT COALESCE(SUM(amount),0) FROM income_entries WHERE month_key=? AND status='Active'"
    if dept != "All": inc_sql += " AND department=?"; params.append(dept)
    if project: inc_sql += " AND project LIKE ?"; params.append(f"%{project}%")
    total_income = float(df_query(inc_sql, params).iloc[0,0])
    pr_params: list[Any] = [mk]
    pr_sql = "SELECT COALESCE(SUM(estimated_amount),0) FROM purchase_requests WHERE substr(COALESCE(approved_at,updated_at,created_at),1,7)=?"
    if dept != "All": pr_sql += " AND department_project=?"; pr_params.append(dept)
    if project: pr_sql += " AND department_project LIKE ?"; pr_params.append(f"%{project}%")
    approved_unpaid = float(df_query(pr_sql + " AND status IN ('Approved','Awaiting Payment','Approved for Payment')", pr_params).iloc[0,0])
    pending = float(df_query(pr_sql + " AND status IN ('Sent for Procurement Review','Submitted for Approval','Pending Approver/MD Approval','Pending Approval')", pr_params).iloc[0,0])
    paid = float(df_query("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='Paid' AND substr(COALESCE(payment_date,created_at),1,7)=?", (mk,)).iloc[0,0])
    return {"Total Income/Budget Allocation": total_income, "Approved Unpaid Commitments": approved_unpaid, "Pending Commitments": pending, "Paid Expenses": paid, "Remaining Balance": total_income - paid - approved_unpaid}


def income_page(manage: bool | None = None):
    if manage is None:
        manage = user()["role"] in ["Admin", "Finance"]
    st.subheader("Income")
    st.caption("Remaining Balance = Total Income or Budget Allocation - Paid Expenses - Approved Unpaid Commitments")
    month, year, dept, project, status = _month_year_filters("income_cmd")
    summary = _income_summary(month, year, dept, project, status)
    metric_row([(k, money(v), None) for k, v in summary.items()], cols=3)
    summary_df = pd.DataFrame([summary])
    entries = df_query("SELECT entry_no, entry_date, month_key, department, project, source, entry_type, amount, notes, status, created_at FROM income_entries ORDER BY entry_date DESC, created_at DESC LIMIT 500")
    if not entries.empty:
        show = entries.copy(); show["amount"] = show["amount"].apply(money); dataframe(show)
    else:
        st.info("No income entries yet.")
    c1, c2 = st.columns(2)
    with c1:
        chart_df = pd.DataFrame({"bucket": ["Income", "Paid", "Approved unpaid", "Remaining"], "amount": [summary["Total Income/Budget Allocation"], summary["Paid Expenses"], summary["Approved Unpaid Commitments"], summary["Remaining Balance"]]})
        interactive_chart(chart_df, "Income vs Commitments", "bucket", "amount", "income_bucket_cmd", default="Bar")
    with c2:
        monthly = df_query("SELECT month_key, SUM(amount) amount FROM income_entries WHERE status='Active' GROUP BY month_key ORDER BY month_key")
        interactive_chart(_money_chart_df(monthly, "amount"), "Income Trend", "month_key", "amount", "income_trend_cmd", default="Line", allow_pie=False)
    if manage:
        st.markdown("#### Add Income / Budget Allocation")
        with st.form("income_entry_form_cmd"):
            c1, c2, c3 = st.columns(3)
            entry_date = c1.date_input("Entry date", date.today(), key="income_entry_date_cmd")
            department = c2.selectbox("Department", department_options(), key="income_dept_cmd")
            project_name = c3.text_input("Project", value="General", key="income_project_cmd")
            c4, c5 = st.columns(2)
            entry_type = selectbox_with_other("Entry type", ["Opening income / budget allocation", "Additional income", "Adjustment", "Other"], "income_type_cmd", "income_entry_type")
            amount = c5.number_input("Amount", min_value=0.0, step=10000.0, key="income_amount_cmd")
            source = st.text_input("Source", value=entry_type, key="income_source_cmd")
            notes = st.text_area("Notes", key="income_notes_cmd")
            submitted = st.form_submit_button("Save Income Entry", type="primary")
        if submitted:
            eno = make_ref("INC")
            mk = entry_date.strftime("%Y-%m")
            inc_id = run_insert("INSERT INTO income_entries (entry_no, entry_date, month_key, year, month, department, project, source, entry_type, amount, notes, status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?)", (eno, entry_date.isoformat(), mk, entry_date.year, entry_date.month, department, project_name or "General", source, entry_type, amount, notes, user()["id"], now_iso(), now_iso()))
            log_audit("INCOME_ENTRY_CREATED", "Income", inc_id, {"entry_no": eno, "amount": amount, "department": department, "project": project_name}, user()["id"], user()["role"])
            _rerun_success("Income entry saved.")
    sheets = {"Summary": pd.DataFrame([summary]), "Income Entries": entries, "Payments": df_query("SELECT * FROM payments ORDER BY created_at DESC LIMIT 1000"), "Approved Commitments": df_query("SELECT * FROM purchase_requests WHERE status IN ('Approved','Awaiting Payment','Approved for Payment') ORDER BY updated_at DESC LIMIT 1000"), "Audit Logs": df_query("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 1000")}
    _excel_download_button("Download Income Workbook", f"income_{year}_{month:02d}.xlsx", sheets, "income_download_cmd")


def _report_sheets(month: int | None = None, year: int | None = None) -> dict[str, pd.DataFrame]:
    where_month = ""
    params: list[Any] = []
    if year and month:
        where_month = "WHERE substr(COALESCE(pr.updated_at, pr.created_at),1,7)=?"; params.append(f"{year:04d}-{month:02d}")
    elif year:
        where_month = "WHERE substr(COALESCE(pr.updated_at, pr.created_at),1,4)=?"; params.append(str(year))
    detailed = df_query(f"SELECT pr.*, u.full_name requested_by_name FROM purchase_requests pr LEFT JOIN users u ON u.id=pr.requested_by {where_month} ORDER BY pr.created_at DESC", params)
    summary = pd.DataFrame({
        "metric": ["Total Submitted", "Total Approved", "Total Rejected", "Total Paid", "Total Completed"],
        "value": [
            int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status NOT IN ('Draft','FM Draft')").iloc[0,0]),
            int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Approved','Awaiting Payment','Approved for Payment','Paid','Completed','Closed')").iloc[0,0]),
            int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status='Rejected'").iloc[0,0]),
            int(df_query("SELECT COUNT(*) FROM payments WHERE status='Paid'").iloc[0,0]),
            int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Completed','Closed')").iloc[0,0]),
        ],
    })
    dept = df_query("SELECT department_project Department, COUNT(*) Count, SUM(estimated_amount) Amount FROM purchase_requests GROUP BY department_project ORDER BY Amount DESC")
    vendor = df_query("SELECT COALESCE(v.name,'No vendor') Vendor, COUNT(p.id) Payments, SUM(p.amount) Amount FROM payments p LEFT JOIN vendors v ON v.id=p.vendor_id GROUP BY Vendor ORDER BY Amount DESC")
    monthly = df_query("SELECT substr(created_at,1,7) Month, COUNT(*) Requests, SUM(estimated_amount) EstimatedAmount FROM purchase_requests GROUP BY Month ORDER BY Month")
    yearly = df_query("SELECT substr(created_at,1,4) Year, COUNT(*) Requests, SUM(estimated_amount) EstimatedAmount FROM purchase_requests GROUP BY Year ORDER BY Year")
    approvals = df_query("SELECT * FROM approval_history ORDER BY created_at DESC LIMIT 5000")
    payments = df_query("SELECT * FROM payments ORDER BY created_at DESC LIMIT 5000")
    receipts = df_query("SELECT * FROM receipt_records ORDER BY created_at DESC LIMIT 5000")
    audit = df_query("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 5000")
    gateway = df_query("SELECT gp.*, u.full_name utility_head FROM gateway_passes gp LEFT JOIN users u ON u.id=gp.facility_manager_user_id ORDER BY gp.created_at DESC LIMIT 5000")
    return {"Summary": summary, "Detailed Records": detailed, "Expenses by Department": dept, "Expenses by Vendor": vendor, "Monthly Breakdown": monthly, "Yearly Breakdown": yearly, "Approval History": approvals, "Payment History": payments, "Receipt Index": receipts, "Audit Logs": audit, "Gateway Pass Movement": gateway}


def compliance_reports():
    st.subheader("Compliance Reports")
    st.caption("Auditor reporting is read-only and downloads Excel workbooks with multiple sheets.")
    today = date.today()
    c1, c2 = st.columns(2)
    month = c1.selectbox("Monthly report month", list(range(1,13)), index=today.month-1, key="audit_report_month_cmd")
    year = c2.number_input("Report year", min_value=2020, max_value=2100, value=today.year, step=1, key="audit_report_year_cmd")
    c1, c2 = st.columns(2)
    with c1:
        by_dept = df_query("SELECT COALESCE(department_project,'Unknown') department, SUM(estimated_amount) total FROM purchase_requests WHERE status NOT IN ('Rejected','Archived') GROUP BY department ORDER BY total DESC")
        interactive_chart(_money_chart_df(by_dept), "Expenses by Department", "department", "total", "audit_dept_spend_cmd", default="Horizontal Bar")
        status_df = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status ORDER BY count DESC")
        interactive_chart(status_df, "Status Distribution", "status", "count", "audit_status_cmd", default="Donut")
    with c2:
        trend = df_query("SELECT substr(created_at,1,7) month, COUNT(*) approvals FROM approval_history GROUP BY month ORDER BY month")
        interactive_chart(trend, "Approval Trend", "month", "approvals", "audit_approval_trend_cmd", default="Line", allow_pie=False)
        vendor = df_query("SELECT COALESCE(v.name,'No vendor') vendor, SUM(p.amount) total FROM payments p LEFT JOIN vendors v ON v.id=p.vendor_id GROUP BY vendor ORDER BY total DESC LIMIT 15")
        interactive_chart(_money_chart_df(vendor), "Vendor Spend Ranking", "vendor", "total", "audit_vendor_spend_cmd", default="Horizontal Bar")
    sheets_month = _report_sheets(int(month), int(year))
    sheets_year = _report_sheets(None, int(year))
    c3, c4 = st.columns(2)
    with c3:
        _excel_download_button("Download Monthly Expenses Report", f"monthly_expenses_{int(year)}_{int(month):02d}.xlsx", sheets_month, "audit_month_expenses_cmd")
        _excel_download_button("Download Monthly Operational Report", f"monthly_operational_{int(year)}_{int(month):02d}.xlsx", sheets_month, "audit_month_ops_cmd")
    with c4:
        _excel_download_button("Download Yearly Expenses Report", f"yearly_expenses_{int(year)}.xlsx", sheets_year, "audit_year_expenses_cmd")
        _excel_download_button("Download Yearly Operational Report", f"yearly_operational_{int(year)}.xlsx", sheets_year, "audit_year_ops_cmd")
    _excel_download_button("Download Full Audit Activity Workbook", f"audit_activity_{int(year)}.xlsx", sheets_year, "audit_full_activity_cmd")


def finance_reports():
    st.subheader("Financial Reports")
    month, year, dept, project, status = _month_year_filters("finance_reports_cmd")
    sheets = _report_sheets(month, year)
    c1, c2 = st.columns(2)
    with c1:
        monthly_paid = df_query("SELECT substr(payment_date,1,7) month, SUM(amount) total FROM payments WHERE status='Paid' GROUP BY month ORDER BY month")
        interactive_chart(_money_chart_df(monthly_paid), "Payment Trend", "month", "total", "finance_report_payment_trend_cmd", default="Line", allow_pie=False)
    with c2:
        status_df = df_query("SELECT status, COUNT(*) count FROM payments GROUP BY status ORDER BY count DESC")
        interactive_chart(status_df, "Payment Status", "status", "count", "finance_report_status_cmd", default="Donut")
    _excel_download_button("Download Financial Excel Report", f"financial_report_{year}_{month:02d}.xlsx", sheets, "finance_report_download_cmd")


def procurement_reports():
    st.subheader("Procurement Reports")
    month, year, dept, project, status = _month_year_filters("proc_reports_cmd")
    sheets = _report_sheets(month, year)
    c1, c2 = st.columns(2)
    with c1:
        status_df = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status ORDER BY count DESC")
        interactive_chart(status_df, "Status Distribution", "status", "count", "proc_report_status_cmd", default="Bar")
    with c2:
        dept_df = df_query("SELECT department_project department, SUM(estimated_amount) total FROM purchase_requests GROUP BY department ORDER BY total DESC")
        interactive_chart(_money_chart_df(dept_df), "Expenses by Project/Department", "department", "total", "proc_report_dept_cmd", default="Horizontal Bar")
    _excel_download_button("Download Procurement Excel Report", f"procurement_report_{year}_{month:02d}.xlsx", sheets, "proc_report_download_cmd")


def audit_dashboard():
    st.subheader("Compliance Snapshot")
    recent_notifs = df_query("SELECT title, message, entity_type, entity_id, created_at FROM notifications WHERE role='Auditor' OR user_id=? ORDER BY created_at DESC LIMIT 25", (user()["id"],))
    if not recent_notifs.empty:
        st.markdown("#### Recent activity notifications")
        dataframe(recent_notifs)
    metric_row([
        ("Total Submitted", int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status NOT IN ('Draft','FM Draft')").iloc[0,0]), "cumulative"),
        ("Total Approved", int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Approved','Awaiting Payment','Paid','Completed','Closed')").iloc[0,0]), "cumulative"),
        ("Total Paid", int(df_query("SELECT COUNT(*) FROM payments WHERE status='Paid'").iloc[0,0]), "cumulative"),
        ("Total Completed", int(df_query("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('Completed','Closed')").iloc[0,0]), "cumulative"),
    ], cols=4)
    c1, c2 = st.columns(2)
    with c1:
        logs_by_action = df_query("SELECT action, COUNT(*) count FROM audit_logs GROUP BY action ORDER BY count DESC LIMIT 15")
        interactive_chart(logs_by_action, "Audit Events by Action", "action", "count", "audit_actions_cmd", default="Horizontal Bar")
    with c2:
        status_df = df_query("SELECT status, COUNT(*) count FROM gateway_passes GROUP BY status ORDER BY count DESC")
        interactive_chart(status_df, "Gateway Pass Status", "status", "count", "audit_gateway_status_cmd", default="Donut")
    logs = df_query("SELECT event_date, event_time, created_at, action, entity_type, entity_id, role, details FROM audit_logs ORDER BY created_at DESC LIMIT 25")
    dataframe(_redact_ui_df(logs, "audit_logs")) if not logs.empty else st.info("No audit logs yet.")


def audit_workspace():
    role_header("Audit & Compliance Workspace", "Read-only review of procurement, finance, gateway pass, history and audit reports.")
    section = st.session_state.get("audit_section", "Audit Dashboard")
    if section == "Audit Dashboard":
        audit_metrics(); audit_dashboard()
    elif section == "Procurement Records":
        auditor_records_page() if "auditor_records_page" in globals() else all_records_page()
    elif section == "Document Archive":
        document_archive(editable=False)
    elif section == "Approval Trails":
        approval_trails_page()
    elif section == "Delegated Approval Review":
        delegated_approval_review_page()
    elif section == "Budget Audit":
        budget_audit_page()
    elif section in ["Utility / Facility Head Handoff Trail", "Facility Manager Handoff Trail"]:
        facility_handoff_trail_page()
    elif section == "Gateway Pass Audit":
        gateway_pass_audit_page()
    elif section == "Vendor History":
        vendor_history_page()
    elif section == "Expense Review":
        expense_review_page()
    elif section == "Compliance Reports":
        compliance_reports()
    elif section == "Income":
        income_page(manage=False)
    elif section == "Settings":
        settings_page()
    else:
        audit_metrics(); audit_dashboard()


def admin_console():
    role_header("Admin Console", "System administration, approval override, reports, exports, income, audit logs and full procurement visibility.")
    section = st.session_state.get("admin_section", "Admin Dashboard")
    if section in ["Admin Dashboard", "System Overview"]:
        admin_metrics(); admin_phase2_alerts() if "admin_phase2_alerts" in globals() else None; admin_overview()
    elif section == "Budget Tracker":
        budgets_page()
    elif section == "Income":
        income_page(manage=True)
    elif section == "User Management":
        user_management()
    elif section == "Roles & Permissions":
        roles_permissions_page()
    elif section == "Approval Configuration":
        approval_config_page() if "approval_config_page" in globals() else configuration_page()
    elif section == "Import Center":
        import_center()
    elif section == "All Procurement Records":
        all_records_page()
    elif section == "Notifications Monitor":
        notifications_monitor_page()
    elif section == "Availability & Delegation Requests":
        admin_availability_review_page() if "admin_availability_review_page" in globals() else availability_panel()
    elif section == "Gateway Pass Management":
        gateway_pass_management_page()
    elif section == "Activity & History Logs":
        activity_history_page(scope="all")
    elif section == "Audit Logs":
        audit_log_page(full=True)
    elif section == "Backup / Export":
        backup_export_page()
    elif section == "Settings":
        settings_page()
    else:
        admin_metrics(); admin_overview()


def render_app():
    if int(user().get("must_change_password") or 0):
        role_header("Password Change Required", "An administrator has required a password update before you continue.")
        change_password_panel()
        return
    role = user()["role"]
    if role == "Admin":
        admin_console()
    elif role == "Procurement Manager":
        procurement_workspace()
    elif role == "Facility Manager":
        facility_workspace()
    elif role == "Finance":
        finance_workspace()
    elif role == "Approver":
        executive_workspace()
    elif role == "Auditor":
        audit_workspace()
    else:
        role_header("ProcureFlow", "Your role is not configured.")
        change_password_panel()

# ---------------- Final visible-label-safe user management override ----------------
def user_management():
    """Admin user management with visible Utility Head / Facility Head label.

    The DB still stores the legacy role value 'Facility Manager' for compatibility,
    but no visible control uses that label.
    """
    st.subheader("User Management")
    role_values = [r["name"] for r in run_query("SELECT name FROM roles ORDER BY name", fetch=True)] or ["Admin", "Procurement Manager", "Facility Manager", "Finance", "Approver", "Auditor"]
    role_options = [(display_role(r), r) for r in role_values]
    label_to_role = {label: value for label, value in role_options}

    with st.expander("Create new user", expanded=True):
        with st.form("cmd_create_user_form"):
            c1, c2, c3 = st.columns(3)
            username = c1.text_input("Username", key="cmd_new_username")
            full_name = c2.text_input("Full name", key="cmd_new_full_name")
            role_label = c3.selectbox("Role", [r[0] for r in role_options], key="cmd_new_role_label")
            c4, c5 = st.columns(2)
            password = c4.text_input("Temporary password", type="password", key="cmd_new_password")
            force = c5.checkbox("Force password change on next login", value=True, key="cmd_new_force")
            submitted = st.form_submit_button("Create User", type="primary")
        if submitted:
            role_value = label_to_role[role_label]
            if not username or not full_name or len(password) < 6:
                st.error("Username, full name, and a password of at least 6 characters are required.")
            else:
                try:
                    uid = run_insert(
                        "INSERT INTO users (username, full_name, role, password_hash, must_change_password, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                        (username.strip(), full_name.strip(), role_value, hash_password(password), int(force), now_iso(), now_iso()),
                    )
                    log_audit("USER_CREATED", "User", uid, f"Created as {display_role(role_value)}", user().get("id"), user().get("role"))
                    create_activity_log(user().get("id"), user().get("role"), "USER_CREATED", "User", uid, f"Created user {username} as {display_role(role_value)}", visibility_scope="admin")
                    create_notification(uid, None, "Account created", "Your ProcureFlow account has been created.", "User", uid)
                    st.success("User created.")
                    _clear_dashboard_cache()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not create user: {exc}")

    users = df_query("SELECT id, username, full_name, role, is_active, must_change_password, COALESCE(account_locked,0) account_locked, last_login_at, created_at FROM users ORDER BY role, username")
    if not users.empty:
        shown = users.copy()
        shown["role"] = shown["role"].apply(display_role)
        dataframe(shown)
    else:
        st.info("No users found.")
        return

    st.markdown("#### Edit account")
    labels = [f"{r.username} - {display_role(r.role)} - #{int(r.id)}" for r in users.itertuples()]
    selected_label = st.selectbox("Select user", labels, key="cmd_select_user")
    selected_id = int(selected_label.rsplit("#", 1)[1])
    selected = df_query("SELECT * FROM users WHERE id=?", (selected_id,)).iloc[0]

    with st.form(f"cmd_edit_user_{selected_id}"):
        c1, c2, c3 = st.columns(3)
        edit_username = c1.text_input("Username", value=selected["username"] or "", key=f"cmd_edit_username_{selected_id}")
        edit_full_name = c2.text_input("Full name", value=selected["full_name"] or "", key=f"cmd_edit_name_{selected_id}")
        role_labels = [r[0] for r in role_options]
        current_label = display_role(selected["role"])
        edit_role_label = c3.selectbox("Role", role_labels, index=role_labels.index(current_label) if current_label in role_labels else 0, key=f"cmd_edit_role_{selected_id}")
        c4, c5, c6 = st.columns(3)
        is_active = c4.checkbox("Active", value=bool(selected["is_active"]), key=f"cmd_edit_active_{selected_id}")
        must_change = c5.checkbox("Force password change", value=bool(selected["must_change_password"]), key=f"cmd_edit_force_{selected_id}")
        locked = c6.checkbox("Locked", value=bool(selected.get("account_locked", 0)), key=f"cmd_edit_locked_{selected_id}")
        save_user = st.form_submit_button("Save user changes")
    if save_user:
        edit_role = label_to_role[edit_role_label]
        before = selected.to_dict()
        run_query(
            "UPDATE users SET username=?, full_name=?, role=?, is_active=?, must_change_password=?, account_locked=?, updated_at=? WHERE id=?",
            (edit_username.strip(), edit_full_name.strip(), edit_role, int(is_active), int(must_change), int(locked), now_iso(), selected_id),
        )
        after = {"username": edit_username, "full_name": edit_full_name, "role": edit_role, "is_active": int(is_active), "must_change_password": int(must_change), "account_locked": int(locked)}
        log_audit("USER_UPDATED", "User", selected_id, "Admin edited user", user().get("id"), user().get("role"), before, after)
        create_activity_log(user().get("id"), user().get("role"), "USER_UPDATED", "User", selected_id, f"Updated user {edit_username}", json_dump(after), "admin")
        if before.get("role") != edit_role:
            create_notification(selected_id, None, "Role changed", f"Your role is now {display_role(edit_role)}.", "User", selected_id, "Important")
        st.success("User updated.")
        _clear_dashboard_cache()
        st.rerun()

    c1, c2, c3 = st.columns(3)
    new_password = c1.text_input("Overwrite/reset password", type="password", key=f"cmd_admin_reset_pwd_{selected_id}")
    force_after_reset = c1.checkbox("Force change after reset", value=True, key=f"cmd_admin_force_after_reset_{selected_id}")
    if c1.button("Reset / Overwrite Password", key=f"cmd_admin_reset_btn_{selected_id}", disabled=not new_password):
        run_query("UPDATE users SET password_hash=?, must_change_password=?, updated_at=? WHERE id=?", (hash_password(new_password), int(force_after_reset), now_iso(), selected_id))
        log_audit("PASSWORD_RESET", "User", selected_id, "Admin reset/overwrote password", user().get("id"), user().get("role"))
        st.success("Password reset securely. The old password was not required or exposed.")

    if c2.button("Unlock user", key=f"cmd_unlock_{selected_id}"):
        run_query("UPDATE users SET account_locked=0, failed_login_count=0, updated_at=? WHERE id=?", (now_iso(), selected_id))
        log_audit("USER_UNLOCKED", "User", selected_id, "Admin unlocked account", user().get("id"), user().get("role"))
        st.success("User unlocked.")

    st.markdown("#### Automatic Utility Head / Facility Head -> Procurement Manager routing")
    st.info("Manual linking is no longer required. Items sent for Procurement Review are routed by role using next_role='procurement_manager' and are visible to active Procurement Manager users.")
    route_preview = df_query("""
        SELECT request_no, status, next_role, requested_by, department_project, estimated_amount, updated_at
        FROM purchase_requests
        WHERE COALESCE(next_role,'') IN ('procurement_manager','approver','finance','auditor')
        ORDER BY updated_at DESC LIMIT 100
    """)
    dataframe(route_preview) if not route_preview.empty else st.caption("No active routed records yet.")

    st.markdown("#### Role permissions")
    rp = df_query("SELECT role_name, permission_name FROM role_permissions ORDER BY role_name, permission_name")
    if not rp.empty:
        shown_rp = rp.copy(); shown_rp["role_name"] = shown_rp["role_name"].apply(display_role); dataframe(shown_rp)
    with st.form("cmd_grant_revoke_perm_form"):
        c1, c2, c3 = st.columns(3)
        role_label_for_perm = c1.selectbox("Role", [r[0] for r in role_options], key="cmd_perm_role_select")
        perm_list = [r["name"] for r in run_query("SELECT name FROM permissions ORDER BY name", fetch=True)]
        perm_for_role = c2.selectbox("Permission", perm_list, key="cmd_perm_select")
        action = c3.selectbox("Action", ["Grant", "Revoke"], key="cmd_perm_action")
        perm_submit = st.form_submit_button("Apply permission change")
    if perm_submit:
        role_for_perm = label_to_role[role_label_for_perm]
        unsafe = role_for_perm not in ["Admin", "Approver"] and perm_for_role in {"approve_request", "approve_payment", "approve_gateway_pass", "approve_po"}
        if unsafe:
            st.error("Approval permissions can only be assigned to Admin or Approver / MD.")
            return
        if action == "Grant":
            run_query("INSERT OR IGNORE INTO role_permissions (role_name, permission_name, created_at) VALUES (?, ?, ?)", (role_for_perm, perm_for_role, now_iso()))
        else:
            run_query("DELETE FROM role_permissions WHERE role_name=? AND permission_name=?", (role_for_perm, perm_for_role))
        log_audit("ROLE_PERMISSION_UPDATED", "Permission", None, f"{action} {perm_for_role} for {display_role(role_for_perm)}", user().get("id"), user().get("role"))
        st.success("Permission updated.")
        _clear_dashboard_cache()
        st.rerun()

# Cache invalidation helper used by admin/user actions and safe after definition.
def _clear_dashboard_cache():
    try:
        st.cache_data.clear()
    except Exception:
        pass

# ---------------- Final safe approval configuration override ----------------
def approval_config_page():
    st.subheader("Approval Configuration")
    st.caption("Only Admin and Approver / MD may approve. Finance and Procurement Manager cannot be configured as approvers.")
    with st.form("cmd_safe_approval_rule_form"):
        c1, c2, c3 = st.columns(3)
        category = c1.selectbox("Category", EXPENSE_CATEGORIES, key="cmd_safe_appr_cat")
        threshold = c2.number_input("Threshold amount", min_value=0.0, step=10000.0, key="cmd_safe_appr_threshold")
        primary = c3.selectbox("Primary approver role", ["Approver", "Admin"], key="cmd_safe_appr_primary")
        c4, c5, c6 = st.columns(3)
        backup = c4.selectbox("Backup approver role", ["None", "Approver", "Admin"], key="cmd_safe_appr_backup")
        finance_required = c5.checkbox("Finance required after approval", value=True, key="cmd_safe_finance_req")
        sourcing_required = c6.checkbox("Sourcing required", value=False, key="cmd_safe_sourcing_req")
        timeout = st.number_input("Approval timeout hours", min_value=1, value=48, step=1, key="cmd_safe_timeout")
        submit_rule = st.form_submit_button("Save approval rule", type="primary")
    if submit_rule:
        rid = run_insert(
            """
            INSERT INTO approval_rules (category, threshold_amount, approver_role, requires_sourcing, requires_finance, is_active, primary_approver_role, backup_approver_role, pm_fallback_enabled, finance_required, sourcing_required, approval_timeout_hours, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (category, threshold, primary, int(sourcing_required), int(finance_required), primary, None if backup == "None" else backup, int(finance_required), int(sourcing_required), int(timeout), now_iso(), now_iso()),
        )
        log_audit("APPROVAL_RULE_CREATED", "ApprovalRule", rid, f"{category} threshold {threshold}; approver={primary}; no PM/Finance approval", user().get("id"), user().get("role"))
        st.success("Approval rule saved. Procurement Manager and Finance remain blocked from approval authority.")
        st.rerun()

    rules = df_query("SELECT * FROM approval_rules ORDER BY is_active DESC, category, threshold_amount")
    if not rules.empty:
        dataframe(rules)

    st.markdown("### Delegation / Away Notice")
    st.info("Delegation may notify Admin/Approver coverage, but it does not grant approval rights to Procurement Manager or Finance.")
    current = df_query("SELECT * FROM approval_delegations ORDER BY updated_at DESC, created_at DESC")
    dataframe(current) if not current.empty else st.caption("No delegation records yet.")
    with st.form("cmd_safe_delegation_form"):
        c1, c2 = st.columns(2)
        enabled = c1.checkbox("Mark Approver / MD unavailable", value=False, key="cmd_safe_deleg_enabled")
        delegate = c2.selectbox("Notify backup role", ["Admin", "Approver"], key="cmd_safe_delegate_role")
        c3, c4 = st.columns(2)
        start = c3.date_input("Start date", value=date.today(), key="cmd_safe_deleg_start")
        end = c4.date_input("End date", value=date.today() + timedelta(days=7), key="cmd_safe_deleg_end")
        reason = st.text_area("Reason", value="Approver unavailable; Admin/Approver coverage required.", key="cmd_safe_deleg_reason")
        submit_deleg = st.form_submit_button("Save Away Notice")
    if submit_deleg:
        run_query("UPDATE approval_delegations SET enabled=0, updated_at=? WHERE primary_role='Approver'", (now_iso(),))
        run_query(
            "INSERT INTO approval_delegations (primary_role, delegate_role, enabled, start_date, end_date, reason, created_by, created_at, updated_at) VALUES ('Approver', ?, ?, ?, ?, ?, ?, ?, ?)",
            (delegate, int(enabled), start.isoformat(), end.isoformat(), reason, user().get("id"), now_iso(), now_iso()),
        )
        log_audit("APPROVER_AWAY_NOTICE_UPDATED", "ApprovalDelegation", "Approver", reason, user().get("id"), user().get("role"))
        create_notification(None, delegate, "Approver availability notice", reason, "ApprovalDelegation", None, "Important")
        st.success("Away notice saved without granting Procurement Manager or Finance approval authority.")
        st.rerun()


def configuration_page():
    approval_config_page()
