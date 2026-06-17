from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
import hashlib

import pandas as pd
import streamlit as st

from core.auth import change_password_panel, has_permission
from core.db import ATTACHMENT_DIR, add_workflow_event, df_query, json_dump, log_audit, make_ref, month_key, notify, now_iso, run_insert, run_query
from core.ocr import duplicate_candidates, extract_text, match_invoice_to_po, parse_ocr_text
from core.ui import badge, dataframe, empty_state, inject_css, money, workflow_progress

EXPENSE_CATEGORIES = ["Diesel/Fuel", "Office Supplies", "Repairs/Maintenance", "Transport/Logistics", "Staff Welfare", "ICT/Software", "Utilities", "Construction Materials", "Professional Services", "Other"]
PR_STATUSES = ["Draft", "Submitted", "Procurement Review", "Requires Sourcing", "Pending Approval", "Approved", "Rejected", "PO Created", "Awaiting Delivery", "Received", "Paid", "Closed"]
PO_STATUSES = ["Draft", "Pending Approval", "Approved", "Sent to Vendor", "Partially Received", "Fully Received", "Invoiced", "Paid", "Closed", "Cancelled"]
RECEIVING_STATUSES = ["Pending Receipt", "Partially Received", "Fully Received", "Disputed", "Returned"]
PAYMENT_METHODS = ["Cash", "Bank Transfer", "POS/Card", "Cheque", "Mobile Money"]
PRIORITIES = ["Low", "Normal", "High", "Urgent"]


def current_user():
    return st.session_state["user"]


def save_upload(uploaded_file, subfolder: str) -> tuple[str | None, str | None]:
    if not uploaded_file:
        return None, None
    data = uploaded_file.getvalue()
    fhash = hashlib.sha256(data).hexdigest()
    safe_name = uploaded_file.name.replace(" ", "_")
    folder = ATTACHMENT_DIR / subfolder
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
    path.write_bytes(data)
    return str(path), fhash


def vendor_options(include_blank=True):
    vendors = df_query("SELECT id, name FROM vendors WHERE COALESCE(status,'Active') != 'Suspended' ORDER BY name")
    options = {"No vendor selected": None} if include_blank else {}
    for _, row in vendors.iterrows():
        options[row["name"]] = int(row["id"])
    return options


def render_procurement_workspace():
    inject_css()
    st.markdown("""
    <div class="pf-hero">
      <h1 style="margin:0;">Procurement Workspace</h1>
      <p>Enterprise-style command center for purchase requests, sourcing, POs, receiving slips, OCR invoices, cash advances, budgets, vendors, and audit compliance.</p>
    </div>
    """, unsafe_allow_html=True)
    quick_actions()
    labels = ["Overview", "Requests", "Sourcing", "Purchase Orders", "Receiving Slips", "Expenses", "Vendors", "Cash Advances", "Budgets", "Reports", "Audit Log", "Admin" if current_user()["role"] == "Admin" else "Settings"]
    tabs = st.tabs(labels)
    with tabs[0]: overview_tab()
    with tabs[1]: requests_tab()
    with tabs[2]: sourcing_tab()
    with tabs[3]: purchase_orders_tab()
    with tabs[4]: receiving_tab()
    with tabs[5]: expenses_tab()
    with tabs[6]: vendors_tab()
    with tabs[7]: cash_advances_tab()
    with tabs[8]: budgets_tab()
    with tabs[9]: reports_tab()
    with tabs[10]: audit_tab()
    with tabs[11]: admin_tab() if current_user()["role"] == "Admin" else settings_tab()


def quick_actions():
    st.markdown("#### Quick Actions")
    actions = ["New Purchase Request", "Upload Receipt / Invoice", "Create Vendor", "Create Purchase Order", "Record Receiving Slip", "Retire Cash Advance", "View Reports", "Ask Procurement AI"]
    cols = st.columns(4)
    for i, action in enumerate(actions):
        if cols[i % 4].button(action, use_container_width=True, key=f"qa_{action}"):
            st.toast(f"Open the {action} workflow from the tabs below.")


def overview_tab():
    st.subheader("Operations Overview")
    m = month_key()
    k = {
        "open_requests": df_query("SELECT COUNT(*) c, COALESCE(SUM(estimated_amount),0) v FROM purchase_requests WHERE status NOT IN ('Closed','Rejected','Paid')").iloc[0],
        "pending_approval": df_query("SELECT COUNT(*) c, COALESCE(SUM(estimated_amount),0) v FROM purchase_requests WHERE status='Pending Approval'").iloc[0],
        "sourcing_required": df_query("SELECT COUNT(*) c FROM purchase_requests WHERE status='Requires Sourcing'").iloc[0],
        "open_pos": df_query("SELECT COUNT(*) c, COALESCE(SUM(total_amount),0) v FROM purchase_orders WHERE status NOT IN ('Closed','Cancelled','Paid')").iloc[0],
        "po_delivery": df_query("SELECT COUNT(*) c FROM purchase_orders WHERE receiving_status IN ('Pending Receipt','Partially Received')").iloc[0],
        "pending_slips": df_query("SELECT COUNT(*) c FROM receiving_slips WHERE status IN ('Pending Receipt','Partially Received','Disputed')").iloc[0],
        "approved_spend": df_query("SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE status='Approved' AND substr(expense_date,1,7)=?", (m,)).iloc[0],
        "ocr_queue": df_query("SELECT COUNT(*) c FROM expenses WHERE ocr_json IS NOT NULL AND invoice_match_status IN ('Needs Review','Mismatch','Not Matched')").iloc[0],
    }
    adv = df_query("""
        SELECT ca.amount_collected, COALESCE(SUM(ae.amount),0) spent
        FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id
        WHERE ca.status IN ('Pending','Approved') GROUP BY ca.id
    """)
    outstanding = 0 if adv.empty else (adv["amount_collected"] - adv["spent"]).clip(lower=0).sum()
    metrics = [("Open Requests", int(k["open_requests"]["c"]), money(k["open_requests"]["v"])), ("Pending Approval", int(k["pending_approval"]["c"]), money(k["pending_approval"]["v"])), ("Requires Sourcing", int(k["sourcing_required"]["c"]), "supplier comparison"), ("Open POs", int(k["open_pos"]["c"]), money(k["open_pos"]["v"])), ("POs Pending Delivery", int(k["po_delivery"]["c"]), "awaiting delivery"), ("Pending Receiving Slips", int(k["pending_slips"]["c"]), "review delivery"), ("Outstanding Cash Advances", money(outstanding), "unretired cash"), ("Approved Spend This Month", money(k["approved_spend"]["v"]), m), ("OCR Review Queue", int(k["ocr_queue"]["c"]), "needs verification")]
    cols = st.columns(3)
    for i, (label, value, help_text) in enumerate(metrics):
        cols[i % 3].metric(label, value, help_text)
    st.divider()
    c1, c2 = st.columns([1.1, .9])
    with c1:
        st.subheader("Your Work")
        your_work_cards()
    with c2:
        st.subheader("Budget Risk")
        risk = budget_risk_df()
        if risk.empty:
            st.success("No categories are near their budget limit.")
        else:
            display = risk.copy()
            for col in ["limit_amount", "spent", "committed", "pending"]:
                display[col] = display[col].apply(money)
            dataframe(display)
    st.divider()
    c3, c4 = st.columns(2)
    with c3:
        st.subheader("Recent Activity")
        activity = df_query("""
            SELECT we.created_at, we.event, we.entity_type, we.status, u.full_name user, we.note
            FROM workflow_events we LEFT JOIN users u ON we.user_id=u.id
            ORDER BY we.created_at DESC LIMIT 12
        """)
        dataframe(activity) if not activity.empty else empty_state("No activity yet", "Create a request, sourcing task, PO, receipt, or expense to start the timeline.")
    with c4:
        st.subheader("Notifications")
        notes = df_query("SELECT title, message, created_at FROM notifications WHERE is_read=0 AND (user_id=? OR role=?) ORDER BY created_at DESC LIMIT 10", (current_user()["id"], current_user()["role"]))
        if notes.empty: st.success("No unread notifications.")
        for _, row in notes.iterrows(): st.info(f"**{row['title']}**\n\n{row['message']}\n\n{row['created_at']}")


def your_work_cards():
    role = current_user()["role"]
    maps = {
        "Procurement Manager": [("Requests needing review", "purchase_requests", "status='Submitted'"), ("Requests requiring sourcing", "purchase_requests", "status='Requires Sourcing'"), ("Vendor quotes pending", "sourcing_tasks", "status IN ('Open','Collecting Quotes')"), ("POs to create", "purchase_requests", "status='Approved' AND linked_po_id IS NULL"), ("OCR confirmation", "expenses", "ocr_json IS NOT NULL AND invoice_match_status IN ('Needs Review','Mismatch','Not Matched')")],
        "Finance": [("Approved requests awaiting payment", "purchase_requests", "status IN ('Approved','PO Created','Received')"), ("Budget exceptions", "budgets", "override_required=1"), ("Cash advances pending retirement", "cash_advances", "status='Approved'"), ("Invoices requiring verification", "invoices", "match_status IN ('Needs Review','Mismatch')")],
        "Approver": [("Requests pending approval", "purchase_requests", "status='Pending Approval'"), ("High-value purchases", "purchase_requests", "estimated_amount >= 500000 AND status NOT IN ('Closed','Rejected')"), ("Rejected requests", "purchase_requests", "status='Rejected'"), ("Approval history", "workflow_events", "event LIKE '%Approved%' OR event LIKE '%Rejected%'")],
        "Auditor": [("Expenses without receipts", "expenses", "receipt_path IS NULL OR receipt_path=''"), ("Duplicate warnings", "expenses", "duplicate_warning=1"), ("Budget overrides", "budgets", "override_required=1"), ("Complete audit log", "audit_logs", "1=1")],
        "Admin": [("User management", "users", "1=1"), ("Workflow settings", "approval_rules", "1=1"), ("Category settings", "budgets", "1=1"), ("System audit", "audit_logs", "1=1")],
    }
    cols = st.columns(2)
    for i, (title, table, where) in enumerate(maps.get(role, maps["Admin"])):
        count = df_query(f"SELECT COUNT(*) c FROM {table} WHERE {where}").iloc[0]["c"]
        cols[i % 2].metric(title, int(count))


def budget_risk_df():
    m = month_key()
    return df_query("""
    WITH spend AS (SELECT category, COALESCE(project_department,'General') dept, SUM(amount) spent FROM expenses WHERE status='Approved' AND substr(expense_date,1,7)=? GROUP BY category, dept),
    committed AS (SELECT pri.category, COALESCE(pr.department_project,'General') dept, SUM(poi.total) committed FROM purchase_orders po JOIN purchase_order_items poi ON po.id=poi.po_id LEFT JOIN purchase_requests pr ON po.request_id=pr.id LEFT JOIN purchase_request_items pri ON pr.id=pri.request_id WHERE po.status IN ('Approved','Sent to Vendor','Partially Received') GROUP BY pri.category, dept),
    pending AS (SELECT category, COALESCE(department_project,'General') dept, SUM(estimated_amount) pending FROM purchase_requests WHERE status IN ('Submitted','Procurement Review','Requires Sourcing','Pending Approval') GROUP BY category, dept)
    SELECT b.category, b.department_project, b.limit_amount, COALESCE(s.spent,0) spent, COALESCE(c.committed,0) committed, COALESCE(p.pending,0) pending, ROUND(((COALESCE(s.spent,0)+COALESCE(c.committed,0)+COALESCE(p.pending,0))/b.limit_amount)*100,1) usage_percent
    FROM budgets b LEFT JOIN spend s ON b.category=s.category AND b.department_project=s.dept LEFT JOIN committed c ON b.category=c.category AND b.department_project=c.dept LEFT JOIN pending p ON b.category=p.category AND b.department_project=p.dept
    WHERE b.budget_month=? AND ((COALESCE(s.spent,0)+COALESCE(c.committed,0)+COALESCE(p.pending,0))/b.limit_amount) >= .8 ORDER BY usage_percent DESC
    """, (m, m))

# ---------- Purchase Requests ----------

def requests_tab():
    st.subheader("Purchase Requests")
    t1, t2 = st.tabs(["New Purchase Request", "Request Register"])
    with t1: new_purchase_request_form()
    with t2: request_register()


def new_purchase_request_form():
    if not has_permission("create_request"):
        st.info("Your role can view requests but cannot create them.")
        return
    with st.form("pr_form"):
        c1, c2, c3 = st.columns(3)
        department = c1.text_input("Department / Project", placeholder="Operations")
        request_date = c2.date_input("Request date", value=date.today())
        required_date = c3.date_input("Required date", value=date.today() + timedelta(days=7))
        c4, c5, c6 = st.columns(3)
        category = c4.selectbox("Category", EXPENSE_CATEGORIES)
        priority = c5.selectbox("Priority", PRIORITIES, index=1)
        vendor_pref = c6.text_input("Vendor preference", placeholder="Optional")
        justification = st.text_area("Description / Business justification")
        notes = st.text_area("Notes")
        st.markdown("##### Line Items")
        item_count = st.number_input("Number of line items", 1, 10, 1)
        items, total_estimate = [], 0.0
        for i in range(int(item_count)):
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([1.3, .8, .9, 1])
                name = c1.text_input("Item name", key=f"pr_name_{i}")
                qty = c2.number_input("Qty", min_value=0.0, value=1.0, step=1.0, key=f"pr_qty_{i}")
                unit = c3.number_input("Unit price", min_value=0.0, value=0.0, step=1000.0, key=f"pr_unit_{i}")
                item_cat = c4.selectbox("Category", EXPENSE_CATEGORIES, index=EXPENSE_CATEGORIES.index(category), key=f"pr_cat_{i}")
                desc = st.text_input("Description", key=f"pr_desc_{i}")
                suggested_vendor = st.text_input("Suggested vendor", key=f"pr_vendor_{i}")
                total = qty * unit
                total_estimate += total
                st.caption(f"Line total: {money(total)}")
                items.append((name, desc, qty, unit, total, item_cat, suggested_vendor))
        submitted = st.form_submit_button("Create Draft Request", type="primary")
    if submitted:
        if not justification.strip() or not any(item[0].strip() for item in items):
            st.error("Business justification and at least one item are required.")
            return
        user = current_user(); request_no = make_ref("PR")
        pr_id = run_insert("""
            INSERT INTO purchase_requests (request_no, requested_by, department_project, request_date, required_date, category, justification, priority, estimated_amount, vendor_preference, status, notes, approval_history_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', ?, '[]', ?, ?)
        """, (request_no, user["id"], department.strip(), request_date.isoformat(), required_date.isoformat(), category, justification.strip(), priority, total_estimate, vendor_pref.strip(), notes.strip(), now_iso(), now_iso()))
        for item in items:
            name, desc, qty, unit, total, item_cat, suggested_vendor = item
            if name.strip():
                run_query("INSERT INTO purchase_request_items (request_id, item_name, description, quantity, unit_price, total, category, suggested_vendor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (pr_id, name.strip(), desc.strip(), qty, unit, total, item_cat, suggested_vendor.strip(), now_iso()))
        add_workflow_event("Purchase Request", pr_id, "Created", "Draft", f"{request_no} created", user["id"])
        st.success(f"Purchase request {request_no} created as Draft.")
        st.rerun()


def request_register():
    c1, c2, c3 = st.columns(3)
    status_filter = c1.selectbox("Status", ["All"] + PR_STATUSES)
    category_filter = c2.selectbox("Category", ["All"] + EXPENSE_CATEGORIES)
    search = c3.text_input("Search", placeholder="request no, department, justification")
    query = """
        SELECT pr.id, pr.request_no, u.full_name requested_by, pr.department_project, pr.request_date, pr.required_date, pr.category, pr.priority, pr.estimated_amount, pr.status, pr.vendor_preference, pr.justification
        FROM purchase_requests pr LEFT JOIN users u ON pr.requested_by=u.id WHERE 1=1
    """
    params = []
    if status_filter != "All": query += " AND pr.status=?"; params.append(status_filter)
    if category_filter != "All": query += " AND pr.category=?"; params.append(category_filter)
    if search:
        query += " AND (pr.request_no LIKE ? OR pr.department_project LIKE ? OR pr.justification LIKE ?)"; s = f"%{search}%"; params += [s, s, s]
    query += " ORDER BY pr.created_at DESC"
    df = df_query(query, params)
    if df.empty:
        empty_state("No purchase requests found", "Create a request to start procurement workflow.")
        return
    display = df.copy(); display["estimated_amount"] = display["estimated_amount"].apply(money); display["status"] = display["status"].apply(lambda x: badge(x))
    st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    st.divider()
    selected_no = st.selectbox("Open request", df["request_no"].tolist())
    render_request_detail(int(df[df["request_no"] == selected_no].iloc[0]["id"]))


def render_request_detail(pr_id: int):
    pr_df = df_query("SELECT pr.*, u.full_name requested_by_name FROM purchase_requests pr LEFT JOIN users u ON pr.requested_by=u.id WHERE pr.id=?", (pr_id,))
    if pr_df.empty: st.error("Request not found."); return
    pr = pr_df.iloc[0]
    with st.container(border=True):
        st.markdown(f"### {pr['request_no']} {badge(pr['status'])}", unsafe_allow_html=True)
        workflow_progress(pr["status"], PR_STATUSES)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Estimated", money(pr["estimated_amount"])); c2.metric("Priority", pr["priority"]); c3.metric("Department", pr["department_project"] or "General"); c4.metric("Required", pr["required_date"] or "--")
        st.write(f"**Requested by:** {pr['requested_by_name']}"); st.write(f"**Justification:** {pr['justification']}")
    items = df_query("SELECT item_name, description, quantity, unit_price, total, category, suggested_vendor FROM purchase_request_items WHERE request_id=?", (pr_id,))
    if not items.empty:
        display = items.copy(); display["unit_price"] = display["unit_price"].apply(money); display["total"] = display["total"].apply(money); dataframe(display)
    render_record_collaboration("Purchase Request", pr_id)
    action_cols = st.columns(5); user = current_user()
    if pr["status"] == "Draft" and action_cols[0].button("Submit", key=f"submit_pr_{pr_id}"):
        run_query("UPDATE purchase_requests SET status='Submitted', updated_at=? WHERE id=?", (now_iso(), pr_id)); add_workflow_event("Purchase Request", pr_id, "Submitted", "Submitted", "Request submitted", user["id"]); notify(None, "Procurement Manager", "New request submitted", f"{pr['request_no']} needs procurement review.", "Purchase Request", pr_id); st.rerun()
    if pr["status"] == "Submitted" and has_permission("procurement_review") and action_cols[1].button("Start Review", key=f"review_pr_{pr_id}"):
        run_query("UPDATE purchase_requests SET status='Procurement Review', updated_at=? WHERE id=?", (now_iso(), pr_id)); add_workflow_event("Purchase Request", pr_id, "Reviewed", "Procurement Review", "Procurement review started", user["id"]); st.rerun()
    if pr["status"] in ("Submitted", "Procurement Review") and has_permission("procurement_review"):
        if action_cols[2].button("Requires Sourcing", key=f"sourcing_pr_{pr_id}"):
            run_query("UPDATE purchase_requests SET status='Requires Sourcing', updated_at=? WHERE id=?", (now_iso(), pr_id)); create_sourcing_task_for_request(pr_id); add_workflow_event("Purchase Request", pr_id, "Sourcing started", "Requires Sourcing", "Supplier comparison required", user["id"]); notify(None, "Procurement Manager", "Sourcing required", f"{pr['request_no']} requires vendor quotes.", "Purchase Request", pr_id); st.rerun()
        if action_cols[3].button("Send for Approval", key=f"approval_pr_{pr_id}"):
            if would_exceed_budget(pr["category"], pr["department_project"], float(pr["estimated_amount"] or 0)): st.warning("This request may exceed budget.")
            run_query("UPDATE purchase_requests SET status='Pending Approval', updated_at=? WHERE id=?", (now_iso(), pr_id)); add_workflow_event("Purchase Request", pr_id, "Sent for approval", "Pending Approval", "Waiting for approver", user["id"]); notify(None, "Approver", "Request pending approval", f"{pr['request_no']} requires approval.", "Purchase Request", pr_id); st.rerun()
    if pr["status"] == "Pending Approval" and has_permission("approve_request"):
        c1, c2 = st.columns(2)
        if c1.button("Approve Request", key=f"approve_pr_{pr_id}", type="primary"):
            history = json.loads(pr["approval_history_json"] or "[]"); history.append({"action":"Approved","by":user["full_name"],"at":now_iso()})
            run_query("UPDATE purchase_requests SET status='Approved', approval_history_json=?, updated_at=? WHERE id=?", (json_dump(history), now_iso(), pr_id)); add_workflow_event("Purchase Request", pr_id, "Approved", "Approved", "Request approved", user["id"]); notify(None, "Procurement Manager", "Request approved", f"{pr['request_no']} is ready for PO creation.", "Purchase Request", pr_id); st.rerun()
        reason = c2.text_input("Rejection reason", key=f"reject_reason_pr_{pr_id}")
        if c2.button("Reject Request", key=f"reject_pr_{pr_id}"):
            history = json.loads(pr["approval_history_json"] or "[]"); history.append({"action":"Rejected","by":user["full_name"],"at":now_iso(),"reason":reason})
            run_query("UPDATE purchase_requests SET status='Rejected', approval_history_json=?, updated_at=? WHERE id=?", (json_dump(history), now_iso(), pr_id)); add_workflow_event("Purchase Request", pr_id, "Rejected", "Rejected", reason, user["id"]); st.rerun()


def create_sourcing_task_for_request(pr_id: int):
    existing = df_query("SELECT id FROM sourcing_tasks WHERE request_id=?", (pr_id,))
    if not existing.empty: return int(existing.iloc[0]["id"])
    pr = df_query("SELECT request_no, justification FROM purchase_requests WHERE id=?", (pr_id,)).iloc[0]
    sourcing_no = make_ref("SRC")
    task_id = run_insert("INSERT INTO sourcing_tasks (sourcing_no, request_id, required_item_service, status, created_at, updated_at) VALUES (?, ?, ?, 'Open', ?, ?)", (sourcing_no, pr_id, pr["justification"], now_iso(), now_iso()))
    run_query("UPDATE purchase_requests SET linked_sourcing_task_id=? WHERE id=?", (task_id, pr_id)); add_workflow_event("Sourcing Task", task_id, "Created", "Open", f"Sourcing created for {pr['request_no']}", current_user()["id"])
    return task_id


def would_exceed_budget(category: str, department: str | None, amount: float) -> bool:
    b = df_query("SELECT limit_amount FROM budgets WHERE budget_month=? AND category=? AND department_project IN (?, 'General') ORDER BY department_project DESC LIMIT 1", (month_key(), category, department or "General"))
    if b.empty: return False
    spent = df_query("SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE status='Approved' AND substr(expense_date,1,7)=? AND category=?", (month_key(), category)).iloc[0]["v"]
    return float(spent) + amount > float(b.iloc[0]["limit_amount"])

# ---------- Sourcing ----------

def sourcing_tab():
    st.subheader("Sourcing")
    tasks = df_query("""
        SELECT st.id, st.sourcing_no, pr.request_no, pr.category, pr.estimated_amount, st.status, st.approval_status, v.name recommended_vendor
        FROM sourcing_tasks st JOIN purchase_requests pr ON st.request_id=pr.id LEFT JOIN vendors v ON st.recommended_vendor_id=v.id ORDER BY st.created_at DESC
    """)
    if tasks.empty:
        empty_state("No sourcing tasks", "Tasks appear when procurement marks a request as requiring sourcing.")
        return
    display = tasks.copy(); display["estimated_amount"] = display["estimated_amount"].apply(money); display["status"] = display["status"].apply(lambda x: badge(x))
    st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    selected = st.selectbox("Open sourcing task", tasks["sourcing_no"].tolist())
    render_sourcing_detail(int(tasks[tasks["sourcing_no"] == selected].iloc[0]["id"]))


def render_sourcing_detail(task_id: int):
    task = df_query("SELECT st.*, pr.request_no, pr.estimated_amount, pr.category FROM sourcing_tasks st JOIN purchase_requests pr ON st.request_id=pr.id WHERE st.id=?", (task_id,)).iloc[0]
    with st.container(border=True):
        st.markdown(f"### {task['sourcing_no']} {badge(task['status'])}", unsafe_allow_html=True)
        st.write(f"**Linked request:** {task['request_no']}"); st.write(f"**Required item/service:** {task['required_item_service']}")
    if has_permission("create_sourcing"):
        with st.expander("Add Vendor Quote"):
            with st.form(f"quote_form_{task_id}"):
                vendors = vendor_options(include_blank=True)
                c1, c2, c3 = st.columns(3)
                vendor_name = c1.selectbox("Vendor", list(vendors.keys()), key=f"quote_vendor_{task_id}")
                manual_vendor = c1.text_input("Or vendor name", key=f"quote_manual_vendor_{task_id}")
                amount = c2.number_input("Quoted amount", min_value=0.0, step=1000.0, key=f"quote_amt_{task_id}")
                delivery = c3.number_input("Delivery time days", min_value=0.0, value=7.0, step=1.0, key=f"quote_del_{task_id}")
                terms = st.text_input("Payment terms"); warranty = st.text_input("Warranty / guarantee")
                rating = st.slider("Vendor rating", 1, 5, 3)
                notes = st.text_area("Notes")
                attachment = st.file_uploader("Quote attachment", type=["pdf","jpg","jpeg","png"], key=f"quote_file_{task_id}")
                submitted = st.form_submit_button("Save Quote")
            if submitted:
                path, _ = save_upload(attachment, "quotes")
                vendor_id = vendors[vendor_name]
                final_vendor_name = manual_vendor.strip() or (vendor_name if vendor_id else "")
                run_query("""
                    INSERT INTO vendor_quotes (sourcing_task_id, vendor_id, vendor_name, quoted_amount, delivery_time_days, payment_terms, warranty, vendor_rating, notes, attachment_path, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (task_id, vendor_id, final_vendor_name, amount, delivery, terms, warranty, rating, notes, path, now_iso()))
                run_query("UPDATE sourcing_tasks SET status='Collecting Quotes', updated_at=? WHERE id=?", (now_iso(), task_id)); add_workflow_event("Sourcing Task", task_id, "Quote added", "Collecting Quotes", final_vendor_name, current_user()["id"]); st.rerun()
    render_quote_comparison(task_id); render_record_collaboration("Sourcing Task", task_id)


def render_quote_comparison(task_id: int):
    quotes = df_query("SELECT vq.*, COALESCE(v.name, vq.vendor_name) vendor FROM vendor_quotes vq LEFT JOIN vendors v ON vq.vendor_id=v.id WHERE sourcing_task_id=? ORDER BY quoted_amount ASC", (task_id,))
    if quotes.empty: st.info("No vendor quotes have been added yet."); return
    max_amt, max_del = max(quotes["quoted_amount"].max(), 1), max(quotes["delivery_time_days"].max(), 1)
    scored = quotes.copy(); scored["price_score"] = (1 - scored["quoted_amount"] / max_amt) * 45; scored["delivery_score"] = (1 - scored["delivery_time_days"] / max_del) * 25; scored["rating_score"] = (scored["vendor_rating"] / 5) * 30; scored["recommended_vendor_score"] = (scored["price_score"] + scored["delivery_score"] + scored["rating_score"]).round(1)
    lowest, fastest, best_rated, recommended = scored.loc[scored["quoted_amount"].idxmin()], scored.loc[scored["delivery_time_days"].idxmin()], scored.loc[scored["vendor_rating"].idxmax()], scored.loc[scored["recommended_vendor_score"].idxmax()]
    c1, c2, c3, c4 = st.columns(4); c1.metric("Lowest Price", lowest["vendor"], money(lowest["quoted_amount"])); c2.metric("Fastest Delivery", fastest["vendor"], f"{fastest['delivery_time_days']} days"); c3.metric("Best Rated", best_rated["vendor"], f"{best_rated['vendor_rating']}/5"); c4.metric("Recommended", recommended["vendor"], recommended["recommended_vendor_score"])
    display = scored[["vendor","quoted_amount","delivery_time_days","payment_terms","warranty","vendor_rating","recommended_vendor_score","notes"]].copy(); display["quoted_amount"] = display["quoted_amount"].apply(money); dataframe(display)
    if has_permission("create_sourcing") and st.button("Use highest-scoring vendor as recommendation", key=f"recommend_{task_id}"):
        vendor_id = int(recommended["vendor_id"]) if pd.notna(recommended["vendor_id"]) else None
        run_query("UPDATE vendor_quotes SET is_recommended = CASE WHEN id=? THEN 1 ELSE 0 END WHERE sourcing_task_id=?", (int(recommended["id"]), task_id))
        run_query("UPDATE sourcing_tasks SET recommended_vendor_id=?, reason_for_recommendation=?, approval_status='Recommended', status='Recommended', updated_at=? WHERE id=?", (vendor_id, f"Recommended score {recommended['recommended_vendor_score']}", now_iso(), task_id))
        add_workflow_event("Sourcing Task", task_id, "Vendor selected", "Recommended", recommended["vendor"], current_user()["id"])
        req = df_query("SELECT request_id FROM sourcing_tasks WHERE id=?", (task_id,)).iloc[0]["request_id"]
        run_query("UPDATE purchase_requests SET status='Pending Approval', updated_at=? WHERE id=?", (now_iso(), int(req)))
        notify(None, "Approver", "Sourcing recommendation ready", "A supplier recommendation is ready for approval.", "Sourcing Task", task_id); st.rerun()

# ---------- Purchase Orders ----------

def purchase_orders_tab():
    st.subheader("Purchase Orders")
    t1, t2 = st.tabs(["Create Purchase Order", "PO Register"])
    with t1: create_po_form()
    with t2: po_register()


def create_po_form():
    if not has_permission("create_po"): st.info("Your role cannot create purchase orders."); return
    approved = df_query("SELECT id, request_no, estimated_amount FROM purchase_requests WHERE status='Approved' AND linked_po_id IS NULL ORDER BY created_at DESC")
    if approved.empty: st.info("No approved requests are waiting for PO creation."); return
    with st.form("create_po_form"):
        req_label = st.selectbox("Approved request", [f"{r.request_no} — {money(r.estimated_amount)}" for r in approved.itertuples()])
        req_no = req_label.split(" — ")[0]; req_id = int(approved[approved["request_no"] == req_no].iloc[0]["id"])
        vendors = vendor_options(include_blank=False); vendor_name = st.selectbox("Vendor", list(vendors.keys()))
        c1, c2 = st.columns(2); po_date = c1.date_input("PO date", value=date.today()); expected = c2.date_input("Expected delivery", value=date.today()+timedelta(days=7))
        attachment = st.file_uploader("PO attachment", type=["pdf","jpg","jpeg","png"])
        submitted = st.form_submit_button("Create PO", type="primary")
    if submitted:
        vendor_id = vendors[vendor_name]
        items = df_query("SELECT item_name, description, quantity, unit_price, total, category FROM purchase_request_items WHERE request_id=?", (req_id,))
        total_amount = float(items["total"].sum()) if not items.empty else 0.0
        po_no = make_ref("PO"); path, _ = save_upload(attachment, "pos")
        po_id = run_insert("""
            INSERT INTO purchase_orders (po_no, request_id, vendor_id, po_date, expected_delivery_date, status, total_amount, payment_status, receiving_status, attachments_json, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Draft', ?, 'Unpaid', 'Pending Receipt', ?, ?, ?, ?)
        """, (po_no, req_id, vendor_id, po_date.isoformat(), expected.isoformat(), total_amount, json_dump([path] if path else []), current_user()["id"], now_iso(), now_iso()))
        for _, item in items.iterrows():
            run_query("INSERT INTO purchase_order_items (po_id, item_name, description, quantity, unit_price, total, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (po_id, item["item_name"], item["description"], item["quantity"], item["unit_price"], item["total"], item["category"], now_iso()))
        run_query("UPDATE purchase_requests SET linked_po_id=?, status='PO Created', updated_at=? WHERE id=?", (po_id, now_iso(), req_id)); add_workflow_event("Purchase Order", po_id, "PO created", "Draft", po_no, current_user()["id"]); add_workflow_event("Purchase Request", req_id, "PO created", "PO Created", po_no, current_user()["id"]); st.success(f"Purchase order {po_no} created."); st.rerun()


def po_register():
    df = df_query("""
        SELECT po.id, po.po_no, pr.request_no, v.name vendor, po.po_date, po.expected_delivery_date, po.status, po.total_amount, po.payment_status, po.receiving_status
        FROM purchase_orders po LEFT JOIN purchase_requests pr ON po.request_id=pr.id LEFT JOIN vendors v ON po.vendor_id=v.id ORDER BY po.created_at DESC
    """)
    if df.empty: empty_state("No purchase orders", "Approved requests can be converted into purchase orders."); return
    display = df.copy(); display["total_amount"] = display["total_amount"].apply(money); display["status"] = display["status"].apply(lambda x: badge(x)); display["receiving_status"] = display["receiving_status"].apply(lambda x: badge(x))
    st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    selected = st.selectbox("Open PO", df["po_no"].tolist()); render_po_detail(int(df[df["po_no"] == selected].iloc[0]["id"]))


def render_po_detail(po_id: int):
    po = df_query("SELECT po.*, pr.request_no, v.name vendor, u.full_name approved_by_name FROM purchase_orders po LEFT JOIN purchase_requests pr ON po.request_id=pr.id LEFT JOIN vendors v ON po.vendor_id=v.id LEFT JOIN users u ON po.approved_by=u.id WHERE po.id=?", (po_id,)).iloc[0]
    with st.container(border=True):
        st.markdown(f"### {po['po_no']} {badge(po['status'])}", unsafe_allow_html=True); workflow_progress(po["status"], PO_STATUSES)
        c1,c2,c3,c4 = st.columns(4); c1.metric("Vendor", po["vendor"] or "--"); c2.metric("Total", money(po["total_amount"])); c3.metric("Payment", po["payment_status"]); c4.markdown(f"Receiving<br>{badge(po['receiving_status'])}", unsafe_allow_html=True)
    items = df_query("SELECT item_name, description, quantity, unit_price, total, category FROM purchase_order_items WHERE po_id=?", (po_id,))
    if not items.empty:
        display = items.copy(); display["unit_price"] = display["unit_price"].apply(money); display["total"] = display["total"].apply(money); dataframe(display)
    render_record_collaboration("Purchase Order", po_id)
    if has_permission("create_po"):
        c1,c2,c3 = st.columns(3)
        if po["status"] == "Draft" and c1.button("Send for PO Approval", key=f"po_pending_{po_id}"):
            run_query("UPDATE purchase_orders SET status='Pending Approval', updated_at=? WHERE id=?", (now_iso(), po_id)); add_workflow_event("Purchase Order", po_id, "PO submitted", "Pending Approval", "PO pending approval", current_user()["id"]); notify(None, "Approver", "PO pending approval", f"{po['po_no']} requires approval.", "Purchase Order", po_id); st.rerun()
        if po["status"] in ("Approved", "Pending Approval") and c2.button("Mark Sent to Vendor", key=f"po_sent_{po_id}"):
            run_query("UPDATE purchase_orders SET status='Sent to Vendor', sent_to_vendor_date=?, updated_at=? WHERE id=?", (date.today().isoformat(), now_iso(), po_id)); add_workflow_event("Purchase Order", po_id, "Sent to vendor", "Sent to Vendor", "PO sent", current_user()["id"]); st.rerun()
    if po["status"] == "Pending Approval" and has_permission("approve_request") and st.button("Approve PO", key=f"approve_po_{po_id}", type="primary"):
        run_query("UPDATE purchase_orders SET status='Approved', approved_by=?, updated_at=? WHERE id=?", (current_user()["id"], now_iso(), po_id)); add_workflow_event("Purchase Order", po_id, "Approved", "Approved", "PO approved", current_user()["id"]); st.rerun()

# ---------- Receiving ----------

def receiving_tab():
    st.subheader("Receiving Slips")
    t1, t2 = st.tabs(["Record Receiving Slip", "Receiving Register"])
    with t1: create_receiving_slip()
    with t2: receiving_register()


def create_receiving_slip():
    if not has_permission("receive_goods"): st.info("Your role cannot record receiving slips."); return
    pos = df_query("SELECT po.id, po.po_no, v.name vendor, po.vendor_id, po.total_amount FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id WHERE po.status IN ('Approved','Sent to Vendor','Partially Received') ORDER BY po.created_at DESC")
    if pos.empty: st.info("No approved/sent POs are available for receiving."); return
    selected = st.selectbox("Purchase Order", [f"{r.po_no} — {r.vendor}" for r in pos.itertuples()])
    po_no = selected.split(" — ")[0]; po = pos[pos["po_no"] == po_no].iloc[0]; po_id = int(po["id"])
    items = df_query("SELECT id, item_name, quantity FROM purchase_order_items WHERE po_id=?", (po_id,))
    with st.form("receiving_form"):
        c1,c2 = st.columns(2); date_received = c1.date_input("Date received", value=date.today()); delivery_note = c2.text_input("Delivery note number")
        status = st.selectbox("Receiving status", RECEIVING_STATUSES, index=2); discrepancy = st.text_area("Discrepancy notes"); attachment = st.file_uploader("Delivery note / photo", type=["pdf","jpg","jpeg","png"])
        received_rows = []
        for _, item in items.iterrows():
            c1,c2,c3,c4 = st.columns([1.2,.7,.7,1]); c1.write(item["item_name"]); c2.write(f"Ordered: {item['quantity']}")
            qty_received = c3.number_input("Received", min_value=0.0, value=float(item["quantity"]), key=f"recv_{item['id']}"); condition = c4.selectbox("Condition", ["Good","Damaged","Incomplete","Wrong Item"], key=f"cond_{item['id']}")
            received_rows.append((int(item["id"]), item["item_name"], float(item["quantity"]), qty_received, condition))
        submitted = st.form_submit_button("Save Receiving Slip", type="primary")
    if submitted:
        path, _ = save_upload(attachment, "receiving"); slip_no = make_ref("GRN")
        slip_id = run_insert("""
            INSERT INTO receiving_slips (slip_no, po_id, vendor_id, received_by, date_received, delivery_note_no, discrepancy_notes, attachment_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (slip_no, po_id, int(po["vendor_id"]), current_user()["id"], date_received.isoformat(), delivery_note, discrepancy, path, status, now_iso(), now_iso()))
        for po_item_id, name, ordered, received, condition in received_rows:
            run_query("INSERT INTO receiving_slip_items (slip_id, po_item_id, item_name, quantity_ordered, quantity_received, item_condition, discrepancy_notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (slip_id, po_item_id, name, ordered, received, condition, "" if condition == "Good" else condition, now_iso()))
        po_receiving = "Fully Received" if status == "Fully Received" else "Partially Received"; po_status = "Fully Received" if status == "Fully Received" else "Partially Received"
        run_query("UPDATE purchase_orders SET receiving_status=?, status=?, updated_at=? WHERE id=?", (po_receiving, po_status, now_iso(), po_id))
        req = df_query("SELECT request_id FROM purchase_orders WHERE id=?", (po_id,))
        if not req.empty and pd.notna(req.iloc[0]["request_id"]): run_query("UPDATE purchase_requests SET status='Received', linked_receiving_slip_id=?, updated_at=? WHERE id=?", (slip_id, now_iso(), int(req.iloc[0]["request_id"])))
        add_workflow_event("Receiving Slip", slip_id, "Received", status, slip_no, current_user()["id"]); notify(None, "Finance", "Goods received", f"{po_no} has a receiving slip ready for invoice/payment review.", "Receiving Slip", slip_id); st.success(f"Receiving slip {slip_no} saved."); st.rerun()


def receiving_register():
    df = df_query("SELECT rs.id, rs.slip_no, po.po_no, v.name vendor, u.full_name received_by, rs.date_received, rs.status, rs.delivery_note_no, rs.discrepancy_notes FROM receiving_slips rs LEFT JOIN purchase_orders po ON rs.po_id=po.id LEFT JOIN vendors v ON rs.vendor_id=v.id LEFT JOIN users u ON rs.received_by=u.id ORDER BY rs.created_at DESC")
    if df.empty: empty_state("No receiving slips", "Record goods or service delivery against a purchase order."); return
    display = df.copy(); display["status"] = display["status"].apply(lambda x: badge(x)); st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    selected = st.selectbox("Open receiving slip", df["slip_no"].tolist()); slip_id = int(df[df["slip_no"] == selected].iloc[0]["id"])
    items = df_query("SELECT item_name, quantity_ordered, quantity_received, item_condition, discrepancy_notes FROM receiving_slip_items WHERE slip_id=?", (slip_id,)); dataframe(items); render_record_collaboration("Receiving Slip", slip_id)

# ---------- Expenses / OCR ----------

def expenses_tab():
    st.subheader("Expenses / OCR Invoice Intake")
    t1, t2 = st.tabs(["Upload Receipt / Invoice", "Expense Register"])
    with t1: upload_receipt_invoice_form()
    with t2: expenses_register()


def upload_receipt_invoice_form():
    if not has_permission("record_expense"): st.info("Your role cannot record expenses."); return
    vendors_df = df_query("SELECT id, name, bank_name, account_no, rating FROM vendors")
    pos = df_query("SELECT po.id, po.po_no, v.name vendor, po.total_amount, po.vendor_id FROM purchase_orders po LEFT JOIN vendors v ON po.vendor_id=v.id ORDER BY po.created_at DESC")
    receipt = st.file_uploader("Upload receipt/invoice image or PDF", type=["png","jpg","jpeg","pdf"], key="expense_ocr_file")
    if st.button("Extract OCR & Match", disabled=receipt is None, type="primary"):
        text, meta, error = extract_text(receipt)
        if error: st.warning(error)
        parsed = parse_ocr_text(text, vendors_df); parsed["file_meta"] = meta
        st.session_state["ocr_result"] = parsed
        if text: st.success("OCR completed. Review and confirm the structured fields below.")
    parsed = st.session_state.get("ocr_result", {}); fields = parsed.get("fields", {}); bank_details = parsed.get("bank_details", {}); confidence = parsed.get("confidence", {})
    for w in parsed.get("warnings", []): st.warning(w)
    with st.expander("Structured OCR JSON", expanded=False): st.json(parsed or {})
    if parsed:
        c1,c2,c3,c4 = st.columns(4); c1.metric("Vendor confidence", f"{confidence.get('vendor',0)*100:.0f}%"); c2.metric("Amount confidence", f"{confidence.get('total_amount',0)*100:.0f}%"); c3.metric("Invoice confidence", f"{confidence.get('invoice_no',0)*100:.0f}%"); c4.metric("Category confidence", f"{confidence.get('category',0)*100:.0f}%")
    vendor_map = vendor_options(include_blank=True); matched_vendor_name = fields.get("matched_vendor_name") or "No vendor selected"; vendor_index = list(vendor_map.keys()).index(matched_vendor_name) if matched_vendor_name in vendor_map else 0
    with st.form("expense_confirm_form"):
        c1,c2,c3 = st.columns(3); expense_date = c1.date_input("Expense / invoice date", value=date.today()); cat_guess = fields.get("category") if fields.get("category") in EXPENSE_CATEGORIES else "Other"; category = c2.selectbox("Category", EXPENSE_CATEGORIES, index=EXPENSE_CATEGORIES.index(cat_guess)); vendor_name = c3.selectbox("Vendor", list(vendor_map.keys()), index=vendor_index)
        c4,c5,c6 = st.columns(3); amount = c4.number_input("Total amount", min_value=0.0, value=float(fields.get("total_amount") or 0), step=1000.0); tax_amount = c5.number_input("VAT / tax", min_value=0.0, value=float(fields.get("tax_amount") or 0), step=100.0); payment_method = c6.selectbox("Payment method", PAYMENT_METHODS)
        c7,c8,c9 = st.columns(3); invoice_no = c7.text_input("Invoice number", value=fields.get("invoice_no") or ""); receipt_no = c8.text_input("Receipt number", value=fields.get("receipt_no") or ""); department = c9.text_input("Department / Project")
        po_options = ["No PO selected"] + [f"{r.po_no} — {r.vendor} — {money(r.total_amount)}" for r in pos.itertuples()]
        po_label = st.selectbox("Match to Purchase Order", po_options)
        description = st.text_area("Description", value=fields.get("description") or ""); notes = st.text_area("Notes", value=f"Bank: {bank_details.get('bank_name','')} Account: {bank_details.get('account_no','')}".strip())
        submitted = st.form_submit_button("Submit Expense / Invoice for Approval", type="primary")
    if submitted:
        if amount <= 0 or not description.strip(): st.error("Amount and description are required."); return
        po_id = None
        if po_label != "No PO selected": po_id = int(pos[pos["po_no"] == po_label.split(" — ")[0]].iloc[0]["id"])
        vendor_id = vendor_map[vendor_name]; file_path, fhash = save_upload(receipt, "expenses")
        if not fhash and parsed.get("file_meta"): fhash = parsed["file_meta"].get("file_hash")
        match_status, mismatch_reasons = match_invoice_to_po(po_id, vendor_id, amount)
        duplicates = duplicate_candidates(fhash, amount, expense_date.isoformat(), vendor_id); duplicate_warning = 0 if duplicates.empty else 1
        expense_no = make_ref("EXP")
        exp_id = run_insert("""
            INSERT INTO expenses (expense_no, expense_date, category, description, vendor_id, amount, payment_method, project_department, status, receipt_path, receipt_hash, receipt_no, invoice_no, tax_amount, linked_po_id, invoice_match_status, duplicate_warning, requested_by, ocr_text, ocr_json, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (expense_no, expense_date.isoformat(), category, description.strip(), vendor_id, amount, payment_method, department.strip(), file_path, fhash, receipt_no, invoice_no, tax_amount, po_id, match_status, duplicate_warning, current_user()["id"], parsed.get("raw_text", ""), json_dump(parsed), notes.strip(), now_iso()))
        if po_id: run_query("UPDATE purchase_orders SET status='Invoiced', updated_at=? WHERE id=?", (now_iso(), po_id))
        if match_status == "Mismatch": notify(None, "Finance", "Invoice mismatch", f"{expense_no}: " + "; ".join(mismatch_reasons), "Expense", exp_id)
        add_workflow_event("Expense", exp_id, "Invoice uploaded", match_status, "; ".join(mismatch_reasons), current_user()["id"])
        st.success(f"{expense_no} submitted. Match status: {match_status}")
        if duplicate_warning: st.warning("Possible duplicate detected. Review before approving.")
        st.rerun()


def expenses_register():
    df = df_query("SELECT e.id, e.expense_no, e.expense_date, e.category, v.name vendor, e.amount, e.status, e.invoice_match_status, e.duplicate_warning, e.receipt_no, e.invoice_no, u.full_name requested_by FROM expenses e LEFT JOIN vendors v ON e.vendor_id=v.id LEFT JOIN users u ON e.requested_by=u.id ORDER BY e.created_at DESC")
    if df.empty: empty_state("No expenses yet", "Upload a receipt or invoice to populate the OCR review queue."); return
    display = df.copy(); display["amount"] = display["amount"].apply(money); display["status"] = display["status"].apply(lambda x: badge(x)); display["invoice_match_status"] = display["invoice_match_status"].apply(lambda x: badge(x)); st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    selected = st.selectbox("Open expense", df["expense_no"].tolist()); exp_id = int(df[df["expense_no"] == selected].iloc[0]["id"]); row = df_query("SELECT * FROM expenses WHERE id=?", (exp_id,)).iloc[0]
    with st.expander("OCR result"):
        try: st.json(json.loads(row["ocr_json"] or "{}"))
        except Exception: st.text(row["ocr_text"] or "")
    render_record_collaboration("Expense", exp_id)
    if row["status"] == "Pending" and has_permission("approve_expense"):
        c1,c2 = st.columns(2)
        if c1.button("Approve Expense", key=f"approve_exp_{exp_id}", type="primary"):
            run_query("UPDATE expenses SET status='Approved', approved_by=?, approved_at=? WHERE id=?", (current_user()["id"], now_iso(), exp_id))
            if pd.notna(row["vendor_id"]): run_query("UPDATE vendors SET total_spend=COALESCE(total_spend,0)+?, last_purchase_date=?, completed_orders=COALESCE(completed_orders,0)+1, updated_at=? WHERE id=?", (float(row["amount"]), row["expense_date"], now_iso(), int(row["vendor_id"])))
            add_workflow_event("Expense", exp_id, "Approved", "Approved", "Expense approved", current_user()["id"]); st.rerun()
        reason = c2.text_input("Return / rejection reason", key=f"reject_exp_reason_{exp_id}")
        if c2.button("Return for Clarification", key=f"return_exp_{exp_id}"):
            run_query("UPDATE expenses SET status='Rejected', rejection_reason=?, approved_by=?, approved_at=? WHERE id=?", (reason, current_user()["id"], now_iso(), exp_id)); add_workflow_event("Expense", exp_id, "Returned", "Rejected", reason, current_user()["id"]); st.rerun()

# ---------- Vendors ----------

def vendors_tab():
    st.subheader("Vendor Management")
    t1,t2,t3 = st.tabs(["Create Vendor", "Vendor Intelligence", "Vendor Register"])
    with t1: create_vendor_form()
    with t2: vendor_intelligence()
    with t3: vendor_register()


def create_vendor_form():
    if not has_permission("manage_vendor"): st.info("Your role cannot create or edit vendors."); return
    with st.form("vendor_form"):
        c1,c2,c3 = st.columns(3); name = c1.text_input("Vendor name"); category = c2.selectbox("Category", EXPENSE_CATEGORIES); status = c3.selectbox("Status", ["Active","Under Review","Suspended"])
        c4,c5,c6 = st.columns(3); phone = c4.text_input("Phone"); email = c5.text_input("Email"); tax_id = c6.text_input("Tax ID")
        address = st.text_area("Address"); c7,c8,c9 = st.columns(3); bank = c7.text_input("Bank name"); account = c8.text_input("Account number"); rating = c9.slider("Rating", 1, 5, 3)
        docs = st.file_uploader("Vendor document", type=["pdf","jpg","jpeg","png"]); submitted = st.form_submit_button("Save Vendor", type="primary")
    if submitted:
        if not name.strip(): st.error("Vendor name is required."); return
        try:
            path, _ = save_upload(docs, "vendors")
            vendor_id = run_insert("""
                INSERT INTO vendors (name, category, phone, email, address, bank_name, account_no, tax_id, rating, status, documents_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name.strip(), category, phone, email, address, bank, account, tax_id, rating, status, json_dump([path] if path else []), now_iso(), now_iso()))
            add_workflow_event("Vendor", vendor_id, "Created", status, name, current_user()["id"]); st.success("Vendor saved."); st.rerun()
        except Exception as exc: st.error(f"Could not save vendor: {exc}")


def vendor_intelligence():
    c1,c2 = st.columns(2)
    with c1:
        st.markdown("##### Top Vendors by Spend")
        df = df_query("SELECT name, category, total_spend, completed_orders, rating FROM vendors ORDER BY total_spend DESC LIMIT 10")
        if not df.empty:
            display = df.copy(); display["total_spend"] = display["total_spend"].apply(money); dataframe(display)
    with c2:
        st.markdown("##### Duplicate Vendor Detection")
        dupes = df_query("SELECT lower(trim(name)) normalized_name, COUNT(*) count, GROUP_CONCAT(name, ', ') vendors FROM vendors GROUP BY lower(trim(name)) HAVING COUNT(*) > 1")
        st.success("No duplicate vendor names detected.") if dupes.empty else dataframe(dupes)
    st.markdown("##### Vendor Performance")
    perf = df_query("""
        SELECT name, status, rating, completed_orders, total_spend, average_delivery_time, rejection_count,
        ROUND(((rating/5.0)*45) + (CASE WHEN rejection_count=0 THEN 30 ELSE MAX(0,30-(rejection_count*5)) END) + (CASE WHEN completed_orders>0 THEN 25 ELSE 5 END),1) performance_score
        FROM vendors ORDER BY performance_score DESC
    """)
    if not perf.empty:
        display = perf.copy(); display["total_spend"] = display["total_spend"].apply(money); dataframe(display)


def vendor_register():
    df = df_query("SELECT id, name, category, phone, email, bank_name, account_no, tax_id, rating, completed_orders, total_spend, average_delivery_time, rejection_count, last_purchase_date, status FROM vendors ORDER BY name")
    if df.empty: empty_state("No vendors", "Create suppliers for requests, sourcing, POs, and invoices."); return
    display = df.copy(); display["total_spend"] = display["total_spend"].apply(money); display["status"] = display["status"].apply(lambda x: badge(x)); st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
    if has_permission("manage_vendor"):
        st.markdown("##### Bank Detail Change Audit")
        vendor_name = st.selectbox("Select vendor to update bank details", df["name"].tolist()); v = df[df["name"] == vendor_name].iloc[0]
        with st.form(f"bank_change_{int(v['id'])}"):
            bank = st.text_input("Bank name", value=v["bank_name"] or ""); account = st.text_input("Account number", value=v["account_no"] or ""); submitted = st.form_submit_button("Update bank details")
        if submitted:
            log_audit("VENDOR_BANK_DETAIL_CHANGE", "Vendor", int(v["id"]), {"old":{"bank_name":v["bank_name"],"account_no":v["account_no"]}, "new":{"bank_name":bank,"account_no":account}}, current_user()["id"])
            run_query("UPDATE vendors SET bank_name=?, account_no=?, updated_at=? WHERE id=?", (bank, account, now_iso(), int(v["id"]))); st.warning("Bank detail change recorded in audit log."); st.rerun()

# ---------- Cash Advances ----------

def cash_advances_tab():
    st.subheader("Cash Advances")
    t1,t2,t3 = st.tabs(["Create Advance", "Retire Advance", "Advance Register"])
    with t1:
        with st.form("advance_form"):
            c1,c2,c3 = st.columns(3); date_collected = c1.date_input("Date collected", value=date.today()); due_date = c2.date_input("Retirement due date", value=date.today()+timedelta(days=7)); employee = c3.text_input("Employee", value=current_user()["full_name"])
            amount = st.number_input("Amount collected", min_value=0.0, step=1000.0); purpose = st.text_area("Purpose"); submitted = st.form_submit_button("Submit Advance")
        if submitted:
            if not employee.strip() or amount <= 0: st.error("Employee and amount are required.")
            else:
                adv_no = make_ref("ADV"); adv_id = run_insert("INSERT INTO cash_advances (advance_no, date_collected, employee_name, amount_collected, purpose, status, created_by, due_date, created_at) VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?, ?)", (adv_no, date_collected.isoformat(), employee, amount, purpose, current_user()["id"], due_date.isoformat(), now_iso()))
                add_workflow_event("Cash Advance", adv_id, "Created", "Pending", adv_no, current_user()["id"]); notify(None, "Finance", "Cash advance pending", f"{adv_no} needs approval.", "Cash Advance", adv_id); st.success(f"{adv_no} submitted."); st.rerun()
    with t2:
        advances = df_query("""
            SELECT ca.id, ca.advance_no, ca.employee_name, ca.amount_collected, ca.due_date, COALESCE(SUM(ae.amount),0) spent, ca.amount_collected-COALESCE(SUM(ae.amount),0) balance
            FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id WHERE ca.status='Approved' GROUP BY ca.id HAVING balance > 0 ORDER BY ca.created_at DESC
        """)
        if advances.empty: st.info("No approved advances require retirement.")
        else:
            label = st.selectbox("Advance", [f"{r.advance_no} — {r.employee_name} — Balance {money(r.balance)}" for r in advances.itertuples()]); adv_no = label.split(" — ")[0]; adv = advances[advances["advance_no"] == adv_no].iloc[0]
            with st.form("retire_adv_form"):
                c1,c2 = st.columns(2); spent_date = c1.date_input("Spent date", value=date.today()); category = c2.selectbox("Category", EXPENSE_CATEGORIES)
                desc = st.text_area("Description"); amt = st.number_input("Amount spent", min_value=0.0, step=1000.0); receipt = st.file_uploader("Receipt", type=["pdf","jpg","jpeg","png"]); submitted = st.form_submit_button("Add Retirement Expense")
            if submitted:
                if amt <= 0 or amt > float(adv["balance"]): st.error("Amount must be greater than zero and not exceed balance.")
                else:
                    path, fhash = save_upload(receipt, "expenses"); run_query("INSERT INTO advance_expenses (advance_id, spent_date, description, category, amount, receipt_path, receipt_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (int(adv["id"]), spent_date.isoformat(), desc, category, amt, path, fhash, now_iso())); add_workflow_event("Cash Advance", int(adv["id"]), "Retirement added", "Approved", desc, current_user()["id"]); st.success("Retirement expense added."); st.rerun()
    with t3:
        df = df_query("""
            SELECT ca.id, ca.advance_no, ca.date_collected, ca.due_date, ca.employee_name, ca.amount_collected, COALESCE(SUM(ae.amount),0) spent, ca.amount_collected-COALESCE(SUM(ae.amount),0) balance, ca.status
            FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id GROUP BY ca.id ORDER BY ca.created_at DESC
        """)
        if df.empty: empty_state("No cash advances", "Create an advance to track cash retirement.")
        else:
            display = df.copy();
            for col in ["amount_collected","spent","balance"]: display[col] = display[col].apply(money)
            display["status"] = display["status"].apply(lambda x: badge(x)); st.markdown(display.to_html(escape=False, index=False), unsafe_allow_html=True)
            if has_permission("finance_action"):
                pending = df[df["status"] == "Pending"]
                if not pending.empty:
                    selected = st.selectbox("Approve pending advance", pending["advance_no"].tolist()); adv_id = int(pending[pending["advance_no"] == selected].iloc[0]["id"])
                    if st.button("Approve Advance", key=f"approve_adv_{adv_id}"):
                        run_query("UPDATE cash_advances SET status='Approved', approved_by=?, approved_at=? WHERE id=?", (current_user()["id"], now_iso(), adv_id)); add_workflow_event("Cash Advance", adv_id, "Approved", "Approved", selected, current_user()["id"]); st.rerun()

# ---------- Budgets/Reports/Admin ----------

def budgets_tab():
    st.subheader("Budgets")
    with st.form("budget_form"):
        c1,c2,c3,c4 = st.columns(4); bmonth = c1.text_input("Month", value=month_key()); category = c2.selectbox("Category", EXPENSE_CATEGORIES); department = c3.text_input("Department / Project", value="General"); limit = c4.number_input("Limit amount", min_value=0.0, step=10000.0); submitted = st.form_submit_button("Save Budget")
    if submitted:
        if has_permission("manage_budget"):
            run_query("INSERT INTO budgets (budget_month, category, department_project, limit_amount, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(budget_month, category, department_project) DO UPDATE SET limit_amount=excluded.limit_amount", (bmonth, category, department or "General", limit, now_iso()))
            log_audit("BUDGET_UPSERT", "Budget", f"{bmonth}-{category}-{department}", f"Limit {limit}", current_user()["id"]); st.success("Budget saved.")
        else: st.warning("You do not have permission to manage budgets.")
    bdf = df_query("""
        SELECT b.budget_month, b.category, b.department_project, b.limit_amount,
        COALESCE((SELECT SUM(amount) FROM expenses e WHERE e.status='Approved' AND substr(e.expense_date,1,7)=b.budget_month AND e.category=b.category),0) spent,
        COALESCE((SELECT SUM(total_amount) FROM purchase_orders po WHERE po.status IN ('Approved','Sent to Vendor','Partially Received')),0) committed,
        COALESCE((SELECT SUM(estimated_amount) FROM purchase_requests pr WHERE pr.status IN ('Submitted','Procurement Review','Requires Sourcing','Pending Approval') AND pr.category=b.category),0) pending
        FROM budgets b ORDER BY b.budget_month DESC, b.category
    """)
    if bdf.empty: empty_state("No budgets configured", "Set monthly category or department budgets to activate budget risk monitoring.")
    else:
        bdf["remaining"] = bdf["limit_amount"] - bdf["spent"] - bdf["committed"] - bdf["pending"]; bdf["usage_%"] = ((bdf["spent"] + bdf["committed"] + bdf["pending"]) / bdf["limit_amount"] * 100).round(1)
        display = bdf.copy();
        for col in ["limit_amount","spent","committed","pending","remaining"]: display[col] = display[col].apply(money)
        dataframe(display)


def reports_tab():
    st.subheader("Reports, Analytics, Global Search & Procurement AI")
    search_and_saved_views(); st.divider(); analytics_dashboards(); st.divider(); procurement_ai_panel()


def search_and_saved_views():
    st.markdown("##### Global Search")
    term = st.text_input("Search across requests, vendors, POs, expenses, cash advances, and audit logs")
    saved_view = st.selectbox("Saved views", ["None", "Needs Approval", "Pending Delivery", "Budget Risk", "Missing Receipt"])
    if term:
        like = f"%{term}%"; results = []
        searches = [("Requests", "SELECT request_no ref, status, justification description FROM purchase_requests WHERE request_no LIKE ? OR justification LIKE ? OR department_project LIKE ?", (like, like, like)), ("Vendors", "SELECT name ref, status, category description FROM vendors WHERE name LIKE ? OR category LIKE ? OR phone LIKE ?", (like, like, like)), ("POs", "SELECT po_no ref, status, payment_status description FROM purchase_orders WHERE po_no LIKE ?", (like,)), ("Expenses", "SELECT expense_no ref, status, description FROM expenses WHERE expense_no LIKE ? OR description LIKE ?", (like, like)), ("Audit", "SELECT action ref, entity_type status, details description FROM audit_logs WHERE action LIKE ? OR details LIKE ?", (like, like))]
        for label, query, params in searches:
            d = df_query(query, params)
            if not d.empty: d.insert(0, "module", label); results.append(d)
        dataframe(pd.concat(results, ignore_index=True)) if results else st.info("No matching records found.")
    if saved_view != "None":
        if saved_view == "Needs Approval": dataframe(df_query("SELECT request_no, status, estimated_amount FROM purchase_requests WHERE status='Pending Approval'"))
        elif saved_view == "Pending Delivery": dataframe(df_query("SELECT po_no, status, receiving_status, total_amount FROM purchase_orders WHERE receiving_status IN ('Pending Receipt','Partially Received')"))
        elif saved_view == "Budget Risk": dataframe(budget_risk_df())
        elif saved_view == "Missing Receipt": dataframe(df_query("SELECT expense_no, expense_date, amount, status FROM expenses WHERE receipt_path IS NULL OR receipt_path=''"))


def analytics_dashboards():
    st.markdown("##### Analytics")
    c1,c2 = st.columns(2)
    with c1:
        d = df_query("SELECT category, SUM(amount) total FROM expenses WHERE status='Approved' GROUP BY category")
        if not d.empty: st.caption("Spend by Category"); st.bar_chart(d.set_index("category"))
    with c2:
        d = df_query("SELECT COALESCE(v.name,'No vendor') vendor, SUM(e.amount) total FROM expenses e LEFT JOIN vendors v ON e.vendor_id=v.id WHERE e.status='Approved' GROUP BY vendor")
        if not d.empty: st.caption("Spend by Vendor"); st.bar_chart(d.set_index("vendor"))
    c3,c4 = st.columns(2)
    with c3:
        d = df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status")
        if not d.empty: st.caption("Requests by Status"); st.bar_chart(d.set_index("status"))
    with c4:
        d = df_query("SELECT receiving_status, COUNT(*) count FROM purchase_orders GROUP BY receiving_status")
        if not d.empty: st.caption("POs by Delivery Status"); st.bar_chart(d.set_index("receiving_status"))
    c5,c6 = st.columns(2)
    with c5:
        d = df_query("SELECT substr(expense_date,1,7) month, SUM(amount) total FROM expenses WHERE status='Approved' GROUP BY substr(expense_date,1,7) ORDER BY month")
        if not d.empty: st.caption("Monthly Procurement Trend"); st.line_chart(d.set_index("month"))
    with c6:
        d = df_query("SELECT ca.employee_name, ca.amount_collected-COALESCE(SUM(ae.amount),0) outstanding FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id GROUP BY ca.id HAVING outstanding > 0")
        if not d.empty: st.caption("Cash Advance Outstanding by Employee"); st.bar_chart(d.set_index("employee_name"))


def procurement_ai_panel():
    st.markdown("##### Ask Procurement AI")
    st.caption("Local rule-based assistant that answers from app data. It does not call an external model.")
    question = st.text_input("Ask a question", placeholder="Which vendors had the highest spend this month?")
    if st.button("Ask Procurement AI"): st.info(answer_data_question(question))


def answer_data_question(question: str) -> str:
    q = (question or "").lower()
    if "highest spend" in q or "top vendor" in q or ("vendor" in q and "spend" in q):
        df = df_query("SELECT name, total_spend FROM vendors ORDER BY total_spend DESC LIMIT 5")
        return "No vendor spend data is available yet." if df.empty else "Top vendors by spend:\n" + "\n".join(f"- {r['name']}: {money(r['total_spend'])}" for _, r in df.iterrows())
    if "pending approval" in q:
        df = df_query("SELECT request_no, estimated_amount, department_project FROM purchase_requests WHERE status='Pending Approval'")
        return "No purchase requests are currently pending approval." if df.empty else "Requests pending approval:\n" + "\n".join(f"- {r['request_no']} ({money(r['estimated_amount'])}) — {r['department_project']}" for _, r in df.iterrows())
    if "over budget" in q or "budget" in q:
        df = budget_risk_df(); return "No active budget risk was detected." if df.empty else "Budget risk categories:\n" + "\n".join(f"- {r['category']} / {r['department_project']}: {r['usage_percent']:.1f}% used" for _, r in df.iterrows())
    if "cash advance" in q or "not retired" in q:
        df = df_query("SELECT ca.advance_no, ca.employee_name, ca.amount_collected-COALESCE(SUM(ae.amount),0) balance FROM cash_advances ca LEFT JOIN advance_expenses ae ON ca.id=ae.advance_id GROUP BY ca.id HAVING balance > 0")
        return "All cash advances appear fully retired or no advances exist." if df.empty else "Outstanding cash advances:\n" + "\n".join(f"- {r['advance_no']} — {r['employee_name']}: {money(r['balance'])}" for _, r in df.iterrows())
    if "without receipt" in q or "missing receipt" in q:
        df = df_query("SELECT expense_no, amount FROM expenses WHERE receipt_path IS NULL OR receipt_path='' ")
        return "No expenses without receipts were found." if df.empty else "Expenses without receipts:\n" + "\n".join(f"- {r['expense_no']}: {money(r['amount'])}" for _, r in df.iterrows())
    if ("supplier" in q and "choose" in q) or "quote" in q:
        df = df_query("SELECT st.sourcing_no, COALESCE(v.name, vq.vendor_name) vendor, vq.quoted_amount, vq.delivery_time_days, vq.vendor_rating FROM vendor_quotes vq JOIN sourcing_tasks st ON vq.sourcing_task_id=st.id LEFT JOIN vendors v ON vq.vendor_id=v.id ORDER BY vq.is_recommended DESC, vq.quoted_amount ASC LIMIT 5")
        return "No vendor quotes are available yet." if df.empty else "Supplier quote options:\n" + "\n".join(f"- {r['sourcing_no']}: {r['vendor']} — {money(r['quoted_amount'])}, {r['delivery_time_days']} days, rating {r['vendor_rating']}/5" for _, r in df.iterrows())
    return "I can answer questions about top vendor spend, pending approvals, budget risk, unretired cash advances, missing receipts, and supplier quote comparison."


def audit_tab():
    st.subheader("Audit Log")
    logs = df_query("SELECT a.created_at, a.action, a.entity_type, a.entity_id, u.full_name user, a.details FROM audit_logs a LEFT JOIN users u ON a.user_id=u.id ORDER BY a.created_at DESC LIMIT 500")
    dataframe(logs) if not logs.empty else empty_state("No audit entries", "Sensitive actions and workflow events will appear here.")
    st.markdown("##### Workflow Events")
    events = df_query("SELECT we.created_at, we.entity_type, we.entity_id, we.event, we.status, u.full_name user, we.note FROM workflow_events we LEFT JOIN users u ON we.user_id=u.id ORDER BY we.created_at DESC LIMIT 500")
    if not events.empty: dataframe(events)


def admin_tab():
    st.subheader("Admin")
    if current_user()["role"] != "Admin": st.warning("Admin only."); return
    t1,t2,t3 = st.tabs(["User Management", "Approval Rules", "Security Settings"])
    with t1: admin_user_management()
    with t2: approval_rules_management()
    with t3: settings_tab()


def admin_user_management():
    from core.auth import hash_password
    with st.form("admin_user_form"):
        c1,c2,c3 = st.columns(3); username = c1.text_input("Username"); full_name = c2.text_input("Full name"); role = c3.selectbox("Role", ["Admin","Procurement Manager","Finance","Approver","Auditor"]); password = st.text_input("Temporary password", type="password"); submitted = st.form_submit_button("Create User")
    if submitted:
        if not username or not full_name or len(password) < 8: st.error("Username, full name, and password of at least 8 characters are required.")
        else:
            try:
                uid = run_insert("INSERT INTO users (username, full_name, role, password_hash, must_change_password, is_active, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)", (username, full_name, role, hash_password(password), now_iso()))
                log_audit("USER_CREATED", "User", uid, f"Role {role}", current_user()["id"]); st.success("User created.")
            except Exception as exc: st.error(f"Could not create user: {exc}")
    users = df_query("SELECT username, full_name, role, is_active, must_change_password, last_login_at, created_at FROM users ORDER BY created_at DESC"); dataframe(users)


def approval_rules_management():
    with st.form("rules_form"):
        c1,c2,c3,c4 = st.columns(4); category = c1.selectbox("Category", EXPENSE_CATEGORIES); threshold = c2.number_input("Threshold amount", min_value=0.0, step=10000.0); approver_role = c3.selectbox("Approver role", ["Approver","Finance","Admin"]); requires_sourcing = c4.checkbox("Requires sourcing"); submitted = st.form_submit_button("Add Rule")
    if submitted:
        run_query("INSERT INTO approval_rules (category, threshold_amount, approver_role, requires_sourcing, requires_finance, is_active, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)", (category, threshold, approver_role, int(requires_sourcing), now_iso()))
        log_audit("APPROVAL_RULE_CREATED", "ApprovalRule", category, f"Threshold {threshold}", current_user()["id"]); st.success("Rule added.")
    rules = df_query("SELECT category, threshold_amount, approver_role, requires_sourcing, requires_finance, is_active, created_at FROM approval_rules ORDER BY created_at DESC"); dataframe(rules)


def settings_tab():
    st.subheader("Settings")
    st.markdown("##### Change Password"); change_password_panel()
    st.markdown("##### Session Timeout"); st.caption("Set environment variable PROCUREFLOW_SESSION_TIMEOUT_MINUTES to adjust timeout.")
    st.markdown("##### Production Mode"); st.caption("Set PROCUREFLOW_PRODUCTION=1 to hide local demo credentials from the login page.")


def render_record_collaboration(entity_type: str, entity_id: int):
    with st.expander("Activity Timeline, Comments & Internal Notes", expanded=False):
        events = df_query("SELECT we.created_at, we.event, we.status, u.full_name user, we.note FROM workflow_events we LEFT JOIN users u ON we.user_id=u.id WHERE we.entity_type=? AND we.entity_id=? ORDER BY we.created_at ASC", (entity_type, entity_id))
        st.markdown("###### Timeline")
        if events.empty: st.caption("No workflow events yet.")
        else:
            for _, event in events.iterrows():
                st.write(f"**{event['created_at']}** — {event['event']} · {event['status'] or ''} · {event['user'] or 'System'}")
                if event["note"]: st.caption(event["note"])
        comments = df_query("SELECT c.created_at, c.comment_text, c.is_internal, u.full_name user FROM comments c LEFT JOIN users u ON c.user_id=u.id WHERE c.entity_type=? AND c.entity_id=? ORDER BY c.created_at DESC", (entity_type, entity_id))
        st.markdown("###### Comments")
        for _, row in comments.iterrows():
            visibility = "Internal" if row["is_internal"] else "General"; st.info(f"**{row['user']}** · {visibility} · {row['created_at']}\n\n{row['comment_text']}")
        with st.form(f"comment_{entity_type}_{entity_id}"):
            text = st.text_area("Add comment"); internal = st.checkbox("Internal note only", value=False); submitted = st.form_submit_button("Post Comment")
        if submitted and text.strip():
            run_query("INSERT INTO comments (entity_type, entity_id, comment_text, is_internal, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (entity_type, entity_id, text.strip(), int(internal), current_user()["id"], now_iso()))
            add_workflow_event(entity_type, entity_id, "Comment added", None, text[:120], current_user()["id"]); st.rerun()
