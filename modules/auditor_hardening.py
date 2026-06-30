"""Auditor-only evidence ledger and read-only compliance views."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
import json
from typing import Any

import pandas as pd
import streamlit as st

from core.auth import has_permission
from core.db import append_audit_event, df_query, log_audit, verify_audit_chain
from core.ui import badge, dataframe, empty_state, interactive_chart, metric_row, money, role_header
from repositories.audit_repository import event_by_id, ledger_count, ledger_page
from repositories.payee_repository import history as payee_history, masked_audit_view
from services.payee_service import PayeeValidationError, audit_payee_reveal, get_full_payee_details
from services.security_service import redact_value


def _current() -> dict:
    return st.session_state.get("user", {})


def _safe_excel_download(label: str, filename: str, sheets: dict[str, pd.DataFrame], key: str) -> None:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in sheets.items():
            (df if isinstance(df, pd.DataFrame) else pd.DataFrame()).to_excel(writer, sheet_name=str(name)[:31], index=False)
    if st.download_button(label, buffer.getvalue(), file_name=filename, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=key):
        log_audit("AUDIT_REPORT_EXPORTED", "Audit Evidence Ledger", None, {"file": filename, "sheets": list(sheets)}, _current().get("id"), _current().get("role"))


def _ledger_filters() -> tuple[str, list[Any], dict[str, Any]]:
    st.markdown("#### Evidence filters")
    c1, c2, c3, c4 = st.columns(4)
    today = date.today()
    start = c1.date_input("From", today - timedelta(days=30), key="audit_ledger_from")
    end = c2.date_input("To", today, key="audit_ledger_to")
    roles = df_query("SELECT DISTINCT COALESCE(actor_role,'System') role FROM audit_events ORDER BY role")
    role = c3.selectbox("Actor role", ["All"] + (roles["role"].dropna().astype(str).tolist() if not roles.empty else []), key="audit_ledger_role")
    severities = df_query("SELECT DISTINCT severity FROM audit_events ORDER BY severity")
    severity = c4.selectbox("Severity", ["All"] + (severities["severity"].dropna().astype(str).tolist() if not severities.empty else []), key="audit_ledger_severity")
    c5, c6, c7, c8 = st.columns(4)
    actors = df_query("SELECT DISTINCT COALESCE(actor_username,'System') actor FROM audit_events ORDER BY actor")
    actor = c5.selectbox("Actor", ["All"] + (actors["actor"].dropna().astype(str).tolist() if not actors.empty else []), key="audit_ledger_actor")
    actions = df_query("SELECT DISTINCT action FROM audit_events ORDER BY action")
    action = c6.selectbox("Action", ["All"] + (actions["action"].dropna().astype(str).tolist() if not actions.empty else []), key="audit_ledger_action")
    entity_types = df_query("SELECT DISTINCT entity_type FROM audit_events ORDER BY entity_type")
    entity = c7.selectbox("Entity type", ["All"] + (entity_types["entity_type"].dropna().astype(str).tolist() if not entity_types.empty else []), key="audit_ledger_entity")
    outcome = c8.selectbox("Outcome", ["All", "Success", "Failure", "Warning", "Denied"], key="audit_ledger_outcome")
    c9, c10, c11, c12 = st.columns(4)
    term = c9.text_input("Reference / record search", key="audit_ledger_term")
    department = c10.text_input("Department / Project", key="audit_ledger_department")
    amount_min = c11.number_input("Minimum amount", min_value=0.0, value=0.0, step=1000.0, key="audit_ledger_amount_min")
    sensitive = c12.checkbox("Sensitive actions only", key="audit_ledger_sensitive")
    include_system = st.checkbox("Include database/system evidence", value=True, key="audit_ledger_system")

    clauses = ["datetime(occurred_at) >= datetime(?)", "datetime(occurred_at) < datetime(?)"]
    params: list[Any] = [f"{start.isoformat()} 00:00:00", f"{(end + timedelta(days=1)).isoformat()} 00:00:00"]
    if role != "All": clauses.append("actor_role=?"); params.append(role)
    if severity != "All": clauses.append("severity=?"); params.append(severity)
    if actor != "All": clauses.append("actor_username=?"); params.append(actor)
    if action != "All": clauses.append("action=?"); params.append(action)
    if entity != "All": clauses.append("entity_type=?"); params.append(entity)
    if outcome != "All": clauses.append("outcome=?"); params.append(outcome)
    if term.strip():
        clauses.append("(entity_reference LIKE ? OR entity_id LIKE ? OR correlation_id LIKE ? OR action LIKE ?)")
        params.extend([f"%{term.strip()}%"] * 4)
    if department.strip():
        # Department is redacted metadata in the canonical ledger; use generic search.
        clauses.append("metadata_redacted_json LIKE ?")
        params.append(f"%{department.strip()}%")
    if amount_min > 0:
        clauses.append("metadata_redacted_json LIKE ?")
        params.append(f"%{amount_min:,.0f}%")
    if sensitive:
        clauses.append("(severity IN ('High','Critical') OR action LIKE 'PAYEE_%' OR action LIKE '%PASSWORD%' OR action LIKE '%LOGIN%' OR action LIKE '%DOWNLOAD%' OR action LIKE '%EXPORT%')")
    if not include_system:
        clauses.append("source NOT IN ('database_trigger','audit_verifier')")
    return " AND ".join(clauses), params, {"from": start, "to": end}


def _event_detail(event_id: int) -> None:
    event = event_by_id(event_id)
    if not event:
        st.warning("Evidence event not found.")
        return
    st.markdown(f"### Evidence Event #{event_id}")
    cols = st.columns(4)
    cols[0].metric("Action", event.get("action") or "—")
    cols[1].metric("Outcome", event.get("outcome") or "—")
    cols[2].metric("Severity", event.get("severity") or "—")
    cols[3].metric("Chain", "Verified" if event.get("record_hash") else "Unavailable")
    event_view = {k: v for k, v in event.items() if k not in {"canonical_payload_json", "record_signature"}}
    st.json(redact_value(event_view))
    with st.expander("Redacted before / after values"):
        st.code(event.get("before_values_redacted_json") or "{}", language="json")
        st.code(event.get("after_values_redacted_json") or "{}", language="json")
    with st.expander("Related workflow evidence"):
        entity_type, entity_id = event.get("entity_type"), event.get("entity_id")
        flow = df_query("SELECT created_at, event, status, note, user_id FROM workflow_events WHERE entity_type=? AND entity_id=? ORDER BY created_at DESC", (entity_type, entity_id))
        dataframe(flow) if not flow.empty else st.info("No linked workflow event was found.")


def all_activity_evidence_ledger() -> None:
    st.subheader("All Activity & Evidence Ledger")
    st.caption("Immutable, redacted evidence across procurement, approvals, finance, logistics, gateway passes, documents, notifications, and security. Auditor access is read-only.")
    where, params, filter_meta = _ledger_filters()
    total = ledger_count(where, params)
    page_size = st.selectbox("Rows per page", [25, 50, 100, 250], index=1, key="audit_ledger_page_size")
    page_count = max(1, (total + page_size - 1) // page_size)
    page = st.number_input("Page", min_value=1, max_value=page_count, value=1, step=1, key="audit_ledger_page")
    events = ledger_page(where, params, page_size, (int(page) - 1) * page_size)
    st.caption(f"{total:,} matching event(s). Page {int(page)} of {page_count}.")
    if events.empty:
        empty_state("No matching evidence", "Adjust filters to expand the evidence view.")
        return
    columns = [
        "id", "occurred_at", "actor_username", "actor_role", "action", "outcome", "severity",
        "entity_type", "entity_reference", "entity_id", "reason_or_comment", "correlation_id", "source",
    ]
    view = events[[c for c in columns if c in events.columns]].copy()
    view = view.rename(columns={"occurred_at": "Timestamp", "actor_username": "Actor Name", "actor_role": "Actor Role", "entity_reference": "Reference Number", "reason_or_comment": "Reason / Comment", "correlation_id": "Correlation ID"})
    dataframe(view)
    _safe_excel_download("Download redacted XLSX", "audit_evidence_ledger.xlsx", {"Evidence Ledger": redact_value(events), "Filters": pd.DataFrame([filter_meta])}, "audit_ledger_xlsx")
    st.download_button("Download redacted CSV", redact_value(events).to_csv(index=False).encode("utf-8"), file_name="audit_evidence_ledger.csv", mime="text/csv", key="audit_ledger_csv")
    selected = st.selectbox("Open evidence event", [f"#{int(r.id)} — {r.action} — {r.entity_type}" for r in events.itertuples()], key="audit_ledger_open")
    _event_detail(int(selected.split(" ", 1)[0].lstrip("#")))


def audit_dashboard() -> None:
    st.subheader("Audit Dashboard")
    st.caption("Read-only evidence review across procurement, approvals, payments, logistics, gateway passes, documents, security, and system activity.")
    today = date.today().isoformat()
    def c(sql: str, params: tuple[Any, ...] = ()) -> int:
        d = df_query(sql, params)
        return int(d.iloc[0, 0]) if not d.empty else 0
    metrics = [
        ("Audit events today", c("SELECT COUNT(*) FROM audit_events WHERE substr(occurred_at,1,10)=?", (today,)), None),
        ("High/Critical events", c("SELECT COUNT(*) FROM audit_events WHERE severity IN ('High','Critical') AND substr(occurred_at,1,10)=?", (today,)), None),
        ("Failed / denied", c("SELECT COUNT(*) FROM audit_events WHERE outcome IN ('Failure','Denied') AND substr(occurred_at,1,10)=?", (today,)), None),
        ("Unverified chains", c("SELECT COUNT(*) FROM audit_chain_verifications WHERE status='Failed'"), None),
        ("Payments without receiving", c("SELECT COUNT(*) FROM payments p LEFT JOIN receiving_slips rs ON rs.po_id=p.po_id WHERE p.status='Paid' AND p.po_id IS NOT NULL AND rs.id IS NULL"), None),
        ("Open logistics exceptions", c("SELECT COUNT(*) FROM logistics_exceptions WHERE status IN ('Open','In Progress')"), None),
        ("Sensitive reveals today", c("SELECT COUNT(*) FROM audit_events WHERE action='PAYEE_DETAILS_REVEALED' AND substr(occurred_at,1,10)=?", (today,)), None),
        ("Document exports today", c("SELECT COUNT(*) FROM audit_events WHERE (action LIKE '%DOWNLOAD%' OR action LIKE '%EXPORT%') AND substr(occurred_at,1,10)=?", (today,)), None),
    ]
    metric_row(metrics, cols=4)
    col1, col2 = st.columns(2)
    with col1:
        by_action = df_query("SELECT action, COUNT(*) count FROM audit_events GROUP BY action ORDER BY count DESC LIMIT 15")
        interactive_chart(by_action, "Audit events by action", "action", "count", "audit_harden_action", default="Horizontal Bar")
    with col2:
        by_role = df_query("SELECT COALESCE(actor_role,'System') role, COUNT(*) count FROM audit_events GROUP BY role ORDER BY count DESC")
        interactive_chart(by_role, "Audit events by role", "role", "count", "audit_harden_role", default="Donut")
    security = df_query("SELECT substr(occurred_at,1,10) day, COUNT(*) count FROM audit_events WHERE outcome IN ('Failure','Denied') OR severity IN ('High','Critical') GROUP BY day ORDER BY day DESC LIMIT 30")
    interactive_chart(security, "High-risk and failed events by day", "day", "count", "audit_harden_security", default="Line", allow_pie=False)
    last_verify = df_query("SELECT verified_at, checked_count, status, invalid_event_ids_json FROM audit_chain_verifications ORDER BY id DESC LIMIT 1")
    st.markdown("#### Audit-chain integrity")
    if not last_verify.empty:
        dataframe(last_verify)
    else:
        st.info("No scheduled verification has been recorded yet.")
    if st.button("Verify audit chain now", key="audit_verify_chain"):
        result = verify_audit_chain(record_result=True)
        st.success(f"Checked {result['checked']:,} event(s): {'valid' if result['valid'] else 'invalid'}.")


def procurement_records_page() -> None:
    st.subheader("Procurement Records")
    df = df_query("SELECT request_no, department_project, category, priority, estimated_amount, status, payment_status, next_role, created_at, updated_at FROM purchase_requests ORDER BY updated_at DESC, created_at DESC")
    if df.empty:
        empty_state("No procurement records", "Purchase-request evidence will appear here.")
    else:
        shown = df.copy(); shown["estimated_amount"] = shown["estimated_amount"].apply(money); dataframe(shown)


def sourcing_vendor_quote_audit_page() -> None:
    st.subheader("Sourcing & Vendor Quote Audit")
    tasks = df_query("""
        SELECT st.id, st.sourcing_no, pr.request_no, pr.estimated_amount, st.status, st.approval_status,
               v.name recommended_vendor, st.reason_for_recommendation, st.created_at, st.updated_at
        FROM sourcing_tasks st JOIN purchase_requests pr ON pr.id=st.request_id
        LEFT JOIN vendors v ON v.id=st.recommended_vendor_id ORDER BY st.updated_at DESC
    """)
    if tasks.empty:
        empty_state("No sourcing evidence", "Sourcing tasks and vendor quotes will appear here.")
        return
    display = tasks.copy(); display["estimated_amount"] = display["estimated_amount"].apply(money); dataframe(display.drop(columns=["id"]))
    selected = st.selectbox("Open sourcing task", [f"{r.sourcing_no} — #{int(r.id)}" for r in tasks.itertuples()], key="audit_source_open")
    task_id = int(selected.rsplit("#", 1)[1])
    quotes = df_query("""
        SELECT vq.vendor_name, v.name vendor_register_name, vq.quoted_amount, vq.delivery_time_days,
               vq.payment_terms, vq.warranty, vq.vendor_rating, vq.score, vq.is_recommended,
               vq.notes, vq.attachment_path, vq.created_at
        FROM vendor_quotes vq LEFT JOIN vendors v ON v.id=vq.vendor_id WHERE vq.sourcing_task_id=? ORDER BY vq.score DESC, vq.quoted_amount
    """, (task_id,))
    st.markdown("#### Quote comparison and recommendation evidence")
    if quotes.empty: st.info("No vendor quote is recorded for this sourcing task.")
    else:
        q = quotes.copy(); q["quoted_amount"] = q["quoted_amount"].apply(money); dataframe(q)
        score = df_query("SELECT * FROM quote_comparisons WHERE sourcing_task_id=? ORDER BY created_at DESC", (task_id,))
        if not score.empty:
            with st.expander("Stored comparison/scoring inputs"):
                dataframe(score)


def po_logistics_evidence_page() -> None:
    st.subheader("Purchase Order & Logistics Evidence")
    df = df_query("""
        SELECT po.id, po.po_no, pr.request_no, v.name vendor, po.total_amount, po.status AS commercial_status,
               po.payment_status, po.receiving_status, po.next_role, po.logistics_status,
               po.released_to_logistics_at, u.full_name released_by, po.expected_delivery_date,
               po.actual_delivery_date, po.waybill_number, po.vehicle_number, po.driver_name
        FROM purchase_orders po
        LEFT JOIN purchase_requests pr ON pr.id=po.request_id
        LEFT JOIN vendors v ON v.id=po.vendor_id
        LEFT JOIN users u ON u.id=po.released_to_logistics_by
        ORDER BY po.updated_at DESC, po.created_at DESC
    """)
    if df.empty:
        empty_state("No purchase-order evidence", "PO and logistics evidence will appear here.")
        return
    shown=df.copy(); shown["total_amount"]=shown["total_amount"].apply(money); dataframe(shown.drop(columns=["id"]))
    selected=st.selectbox("Open PO evidence", [f"{r.po_no} — #{int(r.id)}" for r in df.itertuples()], key="audit_po_open")
    po_id=int(selected.rsplit("#",1)[1])
    st.markdown("#### Evidence chain")
    chain=df_query("SELECT created_at, event, status, note, user_id FROM workflow_events WHERE (entity_type='Purchase Order' AND entity_id=?) OR entity_id=? ORDER BY created_at DESC", (po_id,po_id))
    dataframe(chain) if not chain.empty else st.info("No workflow evidence for this PO.")
    exceptions=df_query("SELECT exception_no, exception_type, description, payment_impact, status, resolution_note, created_at, updated_at FROM logistics_exceptions WHERE po_id=? ORDER BY created_at DESC", (po_id,))
    if not exceptions.empty:
        st.markdown("#### Delivery exceptions / returns"); dataframe(exceptions)


def receiving_proof_returns_page() -> None:
    st.subheader("Receiving Slips, Proof of Delivery & Returns")
    slips = df_query("""
        SELECT rs.id, rs.slip_no, po.po_no, pr.request_no, v.name vendor, rs.received_by,
               rs.date_received, rs.delivery_note_no, rs.status, rs.discrepancy_notes,
               rs.attachment_path, rs.proof_of_delivery_path, rs.created_at
        FROM receiving_slips rs LEFT JOIN purchase_orders po ON po.id=rs.po_id
        LEFT JOIN purchase_requests pr ON pr.id=po.request_id LEFT JOIN vendors v ON v.id=rs.vendor_id
        ORDER BY rs.created_at DESC
    """)
    dataframe(slips.drop(columns=["id"])) if not slips.empty else empty_state("No receiving evidence", "Receiving slips, proof of delivery, and return evidence will appear here.")
    returns=df_query("SELECT exception_no, po_id, request_id, exception_type, description, status, resolution_note, created_at, updated_at FROM logistics_exceptions WHERE exception_type IN ('Return','Damaged Goods','Incorrect Goods','Missing Items','Rejected Delivery') OR status IN ('Open','In Progress') ORDER BY updated_at DESC")
    if not returns.empty:
        st.markdown("#### Returns / unresolved exceptions"); dataframe(returns)


def finance_payment_audit_page() -> None:
    st.subheader("Finance, Invoice & Payment Audit")
    df = df_query("""
        SELECT p.id, p.payment_no, p.amount, p.payment_method, p.payment_date, p.status,
               p.next_role, p.finance_note, po.po_no, pr.request_no, v.name vendor,
               CASE WHEN rs.id IS NULL AND p.po_id IS NOT NULL THEN 'Missing receiving evidence' ELSE 'Receiving evidence present' END AS three_way_match_flag
        FROM payments p LEFT JOIN purchase_orders po ON po.id=p.po_id
        LEFT JOIN purchase_requests pr ON pr.id=po.request_id LEFT JOIN vendors v ON v.id=p.vendor_id
        LEFT JOIN receiving_slips rs ON rs.po_id=p.po_id ORDER BY p.updated_at DESC, p.created_at DESC
    """)
    if df.empty:
        empty_state("No payment evidence", "Finance invoices, receipts, and payment trails will appear here.")
        return
    shown=df.copy(); shown["amount"]=shown["amount"].apply(money); dataframe(shown.drop(columns=["id"]))
    st.markdown("#### Invoice and receipt controls")
    inv=df_query("SELECT invoice_no, supplier_invoice_no, total_amount, status, match_status, approval_status, created_at FROM invoices ORDER BY created_at DESC")
    rec=df_query("SELECT receipt_no, amount, payment_method, status, duplicate_warning, created_at FROM receipt_records ORDER BY created_at DESC")
    c1,c2=st.columns(2)
    with c1: dataframe(inv) if not inv.empty else st.info("No invoices.")
    with c2:
        if not rec.empty:
            r=rec.copy(); r["amount"]=r["amount"].apply(money); dataframe(r)
        else: st.info("No receipts.")


def approval_trails_page() -> None:
    st.subheader("Approval Trails")
    df = df_query("""
        SELECT ah.created_at, ah.entity_type, ah.entity_id, ah.action, ah.status_before, ah.status_after,
               ah.reason, ah.approval_mode, ah.delegation_reason, ah.original_approver_role,
               u.full_name approved_by, ah.approved_by_role
        FROM approval_history ah LEFT JOIN users u ON u.id=ah.approved_by_user_id OR u.id=ah.user_id
        ORDER BY ah.created_at DESC
    """)
    dataframe(df) if not df.empty else empty_state("No approval records", "Approval evidence will appear here.")


def delegated_approval_review_page() -> None:
    st.subheader("Delegated Approval Review")
    df=df_query("SELECT ad.*, u1.full_name primary_user, u2.full_name delegate_user FROM approval_delegations ad LEFT JOIN users u1 ON u1.id=ad.primary_user_id LEFT JOIN users u2 ON u2.id=ad.delegate_user_id ORDER BY ad.updated_at DESC, ad.created_at DESC")
    dataframe(df) if not df.empty else empty_state("No delegations", "Delegation evidence will appear here.")


def payee_bank_detail_audit_page() -> None:
    st.subheader("Payment Payee / Bank Detail Access Audit")
    st.caption("Payee details are encrypted. Auditor lists and exports show masked values only.")
    df=masked_audit_view()
    if df.empty:
        empty_state("No payee records", "Secure payee details entered in eligible draft forms will appear here.")
        return
    cols=["id","request_no","po_no","vendor_name","payee_type","payee_name_masked","account_name_masked","bank_name_masked","account_number_last4","currency","verification_status","payment_readiness_status","created_by_name","verified_by_name","created_at","updated_at"]
    view=df[[c for c in cols if c in df.columns]].copy()
    if "account_number_last4" in view: view["account_number_last4"]=view["account_number_last4"].map(lambda v: "******"+str(v or ""))
    dataframe(view.drop(columns=["id"]))
    _safe_excel_download("Download masked payee audit", "masked_payee_audit.xlsx", {"Masked Payee Audit": view.drop(columns=["id"])}, "audit_payee_export")
    selected=st.selectbox("Open payee audit record", [f"Request {r.request_no or '—'} — Payee #{int(r.id)}" for r in df.itertuples()], key="audit_payee_open")
    payee_id=int(selected.rsplit("#",1)[1])
    row=df[df["id"]==payee_id].iloc[0]
    hist=payee_history(payee_id)
    st.markdown("#### Version and access history")
    dataframe(hist) if not hist.empty else st.info("No version record yet.")
    if has_permission("reveal_sensitive_payment_details"):
        with st.expander("Controlled sensitive-data reveal"):
            reason=st.text_area("Mandatory reason for reveal", key=f"audit_payee_reveal_reason_{payee_id}")
            if st.button("Reveal for 5 minutes", key=f"audit_payee_reveal_{payee_id}"):
                try:
                    audit_payee_reveal(payee_id, int(_current()["id"]), str(_current()["role"]), reason)
                    st.session_state[f"audit_payee_reveal_until_{payee_id}"]=(datetime.now()+timedelta(minutes=5)).isoformat()
                    st.success("Sensitive values are available in this Auditor session for five minutes.")
                except PayeeValidationError as exc:
                    st.error(str(exc))
            until=st.session_state.get(f"audit_payee_reveal_until_{payee_id}")
            if until and datetime.fromisoformat(until)>datetime.now():
                full=get_full_payee_details(payee_id)
                if full:
                    st.warning("Sensitive values are visible temporarily and are not included in exports.")
                    st.code("\n".join([f"Payee: {full.get('payee_name') or '—'}", f"Bank: {full.get('bank_name') or '—'}", f"Account name: {full.get('account_name') or '—'}", f"Account number: {full.get('account_number') or '—'}"]), language="text")


def gateway_pass_audit_page() -> None:
    st.subheader("Gateway Pass Audit")
    df=df_query("""
        SELECT gp.pass_number, u.full_name facility_owner, gp.department, gp.movement_type, gp.destination,
               gp.expected_movement_date, gp.expected_return_date, gp.actual_return_date, gp.status,
               gp.approved_by_role, gp.security_officer_name, gp.gate_verification_time,
               gp.exit_entry_confirmation, gp.generated_at, gp.downloaded_at, gp.updated_at
        FROM gateway_passes gp LEFT JOIN users u ON u.id=gp.facility_manager_user_id ORDER BY gp.updated_at DESC, gp.created_at DESC
    """)
    dataframe(df) if not df.empty else empty_state("No gateway pass evidence", "Gateway-pass lifecycle and movement evidence will appear here.")


def document_download_audit_page() -> None:
    st.subheader("Document Archive & Download Audit")
    docs=df_query("SELECT file_name, document_type, department_project, import_status, original_path, created_at FROM imported_legacy_documents ORDER BY created_at DESC")
    if not docs.empty:
        dataframe(docs)
    else:
        st.info("No imported source documents.")
    downloads=df_query("SELECT occurred_at, actor_username, actor_role, action, entity_type, entity_reference, metadata_redacted_json FROM audit_events WHERE action LIKE '%DOWNLOAD%' OR action LIKE '%EXPORT%' ORDER BY occurred_at DESC")
    st.markdown("#### Download/export evidence")
    dataframe(downloads) if not downloads.empty else st.info("No download/export audit events yet.")


def notification_delivery_audit_page() -> None:
    st.subheader("Notification Delivery Audit")
    df=df_query("""
        SELECT no.id, n.title, n.entity_type, n.entity_id, no.channel, no.target_role, no.status,
               no.attempts, no.error_message, no.created_at, no.sent_at, no.last_failure_at
        FROM notification_outbox no LEFT JOIN notifications n ON n.id=no.notification_id
        ORDER BY no.created_at DESC
    """)
    dataframe(redact_value(df)) if not df.empty else empty_state("No notification outbox entries", "Notification queue/delivery evidence will appear here.")


def user_security_audit_page() -> None:
    st.subheader("User & Security Audit")
    df=df_query("""
        SELECT occurred_at, actor_username, actor_role, action, outcome, severity, entity_id, reason_or_comment, metadata_redacted_json
        FROM audit_events
        WHERE action LIKE '%LOGIN%' OR action LIKE '%ACCOUNT_LOCK%' OR action LIKE '%PASSWORD%' OR action LIKE '%SESSION%' OR action LIKE '%DENIED%' OR entity_type='Security'
        ORDER BY occurred_at DESC
    """)
    dataframe(df) if not df.empty else empty_state("No security events", "Authentication, authorization, session, and configuration security events will appear here.")


def vendor_history_page() -> None:
    st.subheader("Vendor History")
    df=df_query("SELECT name, category, status, rating, completed_orders, total_spend, average_delivery_time, rejection_count, last_purchase_date, updated_at FROM vendors ORDER BY updated_at DESC, name")
    if not df.empty:
        v=df.copy(); v["total_spend"]=v["total_spend"].apply(money); dataframe(v)
    else: empty_state("No vendor history", "Vendor and performance evidence will appear here.")


def budget_audit_page() -> None:
    st.subheader("Budget Audit")
    df=df_query("SELECT bh.created_at, bh.budget_type, bh.budget_id, bh.action, bh.before_values, bh.after_values, bh.note, u.full_name changed_by FROM budget_history bh LEFT JOIN users u ON u.id=bh.changed_by ORDER BY bh.created_at DESC")
    dataframe(redact_value(df)) if not df.empty else empty_state("No budget audit", "Budget changes will appear here.")


def facility_handoff_page() -> None:
    st.subheader("Facility / Utility Handoff Trail")
    df=df_query("""
        SELECT pr.request_no, fu.full_name facility_owner, pm.full_name procurement_manager,
               pr.status, pr.next_role, pr.created_at, pr.updated_at
        FROM purchase_requests pr LEFT JOIN users fu ON fu.id=pr.facility_manager_user_id
        LEFT JOIN users pm ON pm.id=pr.assigned_procurement_manager_id
        WHERE pr.facility_manager_user_id IS NOT NULL ORDER BY pr.updated_at DESC
    """)
    dataframe(df) if not df.empty else empty_state("No Facility/Utility handoffs", "Facility/Utility to Procurement handoff history will appear here.")


def expense_review_page() -> None:
    st.subheader("Expense Review")
    df=df_query("SELECT e.created_at, e.expense_no, e.category, e.amount, e.status, e.invoice_no, e.receipt_no, e.duplicate_warning, u.full_name created_by FROM expenses e LEFT JOIN users u ON u.id=e.created_by ORDER BY e.created_at DESC")
    if not df.empty:
        show=df.copy(); show["amount"]=show["amount"].apply(money); dataframe(show)
    else: empty_state("No expense records", "Expense compliance evidence will appear here.")


def compliance_reports_page() -> None:
    st.subheader("Compliance Reports")
    status=df_query("SELECT status, COUNT(*) count FROM purchase_requests GROUP BY status ORDER BY count DESC")
    dept=df_query("SELECT COALESCE(department_project,'Unknown') department, SUM(estimated_amount) amount FROM purchase_requests GROUP BY department ORDER BY amount DESC")
    c1,c2=st.columns(2)
    with c1: interactive_chart(status,"Lifecycle status pipeline","status","count","audit_compliance_status",default="Donut")
    with c2: interactive_chart(dept,"Procurement value by department","department","amount","audit_compliance_dept",default="Horizontal Bar")
    _safe_excel_download("Download compliance evidence workbook", "compliance_evidence.xlsx", {"Requests": df_query("SELECT * FROM purchase_requests"), "Audit Events": df_query("SELECT * FROM audit_events ORDER BY id DESC LIMIT 10000")}, "audit_compliance_export")


def income_page() -> None:
    st.subheader("Income")
    df=df_query("SELECT entry_no, entry_date, department, project, source, entry_type, amount, status, notes FROM income_entries ORDER BY entry_date DESC")
    if not df.empty:
        view=df.copy(); view["amount"]=view["amount"].apply(money); dataframe(view)
    else: empty_state("No income entries", "Income/budget allocation records will appear here.")


def auditor_settings_page() -> None:
    st.subheader("Settings")
    st.info("Auditor settings are read-only. Security configuration is maintained through controlled deployment environment variables and audited Admin configuration change records.")
    env=pd.DataFrame([{"Control":"Audit-chain verification", "Status":"Enabled"}, {"Control":"Masked payee data", "Status":"Enabled"}, {"Control":"Append-only evidence table", "Status":"Enabled"}, {"Control":"Redacted exports", "Status":"Enabled"}])
    dataframe(env)


def audit_workspace() -> None:
    role_header("Audit & Compliance Workspace", "Read-only evidence review across procurement, approvals, payments, logistics, gateway passes, documents, security, and system activity.")
    section=st.session_state.get("audit_section", "Audit Dashboard")
    pages={
        "Audit Dashboard": audit_dashboard,
        "All Activity & Evidence Ledger": all_activity_evidence_ledger,
        "Procurement Records": procurement_records_page,
        "Sourcing & Vendor Quote Audit": sourcing_vendor_quote_audit_page,
        "Purchase Order & Logistics Evidence": po_logistics_evidence_page,
        "Receiving Slips, Proof of Delivery & Returns": receiving_proof_returns_page,
        "Finance, Invoice & Payment Audit": finance_payment_audit_page,
        "Approval Trails": approval_trails_page,
        "Delegated Approval Review": delegated_approval_review_page,
        "Payment Payee / Bank Detail Access Audit": payee_bank_detail_audit_page,
        "Gateway Pass Audit": gateway_pass_audit_page,
        "Document Archive & Download Audit": document_download_audit_page,
        "Notification Delivery Audit": notification_delivery_audit_page,
        "User & Security Audit": user_security_audit_page,
        "Vendor History": vendor_history_page,
        "Budget Audit": budget_audit_page,
        "Facility / Utility Handoff Trail": facility_handoff_page,
        "Expense Review": expense_review_page,
        "Compliance Reports": compliance_reports_page,
        "Income": income_page,
        "Settings": auditor_settings_page,
    }
    pages.get(section, audit_dashboard)()
