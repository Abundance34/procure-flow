from __future__ import annotations

import json
import os
import smtplib
import sqlite3
from email.message import EmailMessage
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "procureflow_workspace.db"
ATTACHMENT_DIR = DATA_DIR / "attachments"
IMPORT_DIR = DATA_DIR / "imports"
BACKUP_DIR = DATA_DIR / "backups"

for folder in [DATA_DIR, ATTACHMENT_DIR, IMPORT_DIR, BACKUP_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

_DB_INIT_DONE = False


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def month_key(value: date | None = None) -> str:
    return (value or date.today()).strftime("%Y-%m")


def get_conn() -> sqlite3.Connection:
    """Open SQLite with production-safer defaults for Streamlit reruns.

    WAL + busy_timeout reduce "database is locked" errors when several users
    click around at the same time. The app still remains SQLite-simple.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    return conn


def run_query(query: str, params: Iterable[Any] = (), fetch: bool = False, many: bool = False):
    conn = get_conn()
    cur = conn.cursor()
    if many:
        cur.executemany(query, params)
    else:
        cur.execute(query, tuple(params))
    rows = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return rows


def run_insert(query: str, params: Iterable[Any] = ()) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, tuple(params))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(new_id)


def df_query(query: str, params: Iterable[Any] = ()) -> pd.DataFrame:
    conn = get_conn()
    try:
        return pd.read_sql_query(query, conn, params=tuple(params))
    finally:
        conn.close()


def table_exists(table: str) -> bool:
    rows = run_query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,), fetch=True)
    return bool(rows)


def table_columns(table: str) -> set[str]:
    if not table_exists(table):
        return set()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cur.fetchall()}
    conn.close()
    return cols


def add_column_if_missing(table: str, column: str, ddl: str):
    if column not in table_columns(table):
        run_query(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def ensure_performance_indexes():
    """Add non-destructive indexes for fast role navigation and dashboards.

    These indexes target the columns repeatedly used by sidebar notification
    counts, dashboard counters, approval queues, Facility Manager handoffs and
    Gateway Pass views. They are safe for existing databases.
    """
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_read_popup ON notifications(user_id, is_read, popup_shown, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_role_read_popup ON notifications(role, is_read, popup_shown, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_purchase_requests_status_updated ON purchase_requests(status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_purchase_requests_requested_by ON purchase_requests(requested_by, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_purchase_requests_fm_status ON purchase_requests(facility_manager_user_id, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_purchase_requests_pm_status ON purchase_requests(assigned_procurement_manager_id, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_activity_logs_user_role ON activity_logs(user_id, role, related_user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_gateway_passes_fm_status ON gateway_passes(facility_manager_user_id, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_gateway_passes_status_dates ON gateway_passes(status, created_at, submitted_at, approved_at)",
        "CREATE INDEX IF NOT EXISTS idx_gateway_pass_items_pass_fragile ON gateway_pass_items(gateway_pass_id, fragility_status)",
        "CREATE INDEX IF NOT EXISTS idx_user_availability_status ON user_availability(user_id, status, admin_review_status, away_start_date, away_end_date)",
        "CREATE INDEX IF NOT EXISTS idx_approval_delegations_enabled ON approval_delegations(enabled, primary_role, delegate_role, start_date, end_date)",
    ]
    for sql in indexes:
        try:
            run_query(sql)
        except Exception:
            # Some indexes reference enterprise/phase2 tables that may not exist
            # yet during older migration paths; init_db calls this again after
            # schema creation.
            pass


def make_ref(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}"


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def log_audit(
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    details: str | dict | None = None,
    user_id: int | None = None,
    role: str | None = None,
    before_values: dict | None = None,
    after_values: dict | None = None,
):
    """Log sensitive actions. The signature remains backwards-compatible with the original scaffold."""
    if isinstance(details, dict):
        details = json_dump(details)
    columns = table_columns("audit_logs")
    if {"role", "before_values", "after_values", "event_date", "event_time"}.issubset(columns):
        ts = now_iso()
        run_query(
            """
            INSERT INTO audit_logs (action, entity_type, entity_id, user_id, role, details, before_values, after_values, created_at, event_date, event_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                user_id,
                role,
                details,
                json_dump(before_values or {}) if before_values else None,
                json_dump(after_values or {}) if after_values else None,
                ts,
                ts[:10],
                ts[11:19],
            ),
        )
    elif {"role", "before_values", "after_values"}.issubset(columns):
        run_query(
            """
            INSERT INTO audit_logs (action, entity_type, entity_id, user_id, role, details, before_values, after_values, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                user_id,
                role,
                details,
                json_dump(before_values or {}) if before_values else None,
                json_dump(after_values or {}) if after_values else None,
                now_iso(),
            ),
        )
    else:
        run_query(
            """
            INSERT INTO audit_logs (action, entity_type, entity_id, user_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (action, entity_type, str(entity_id) if entity_id is not None else None, user_id, details, now_iso()),
        )


    # Auditor activity feed: every audited action creates an unread Auditor notification.
    # This is intentionally direct SQL instead of create_notification() to avoid recursion.
    try:
        if action not in {"AUDITOR_ACTIVITY_NOTIFICATION_CREATED", "CRITICAL_NOTIFICATION_CREATED"}:
            cols = table_columns("notifications")
            section_target = "Audit Dashboard"
            title = f"Audit activity: {action}"
            msg = f"{role or 'System'} performed {action} on {entity_type or 'Record'} {entity_id or ''}"
            ts = now_iso()
            if {"popup_shown", "importance", "delivery_channel", "push_sent", "email_sent", "action_label", "section_target"}.issubset(cols):
                run_query(
                    """
                    INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, popup_shown, importance, delivery_channel, push_sent, email_sent, action_label, section_target, created_at)
                    VALUES (NULL, 'Auditor', ?, ?, ?, ?, 0, 0, 'Normal', 'in_app', 0, 0, 'Open Audit Dashboard', ?, ?)
                    """,
                    (title, msg, entity_type, int(entity_id) if str(entity_id or '').isdigit() else None, section_target, ts),
                )
            elif "section_target" in cols:
                run_query(
                    "INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, section_target, created_at) VALUES (NULL, 'Auditor', ?, ?, ?, ?, 0, ?, ?)",
                    (title, msg, entity_type, int(entity_id) if str(entity_id or '').isdigit() else None, section_target, ts),
                )
            else:
                run_query(
                    "INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, created_at) VALUES (NULL, 'Auditor', ?, ?, ?, ?, 0, ?)",
                    (title, msg, entity_type, int(entity_id) if str(entity_id or '').isdigit() else None, ts),
                )
    except Exception:
        pass


def add_workflow_event(entity_type: str, entity_id: int, event: str, status: str | None = None, note: str | None = None, user_id: int | None = None):
    run_query(
        """
        INSERT INTO workflow_events (entity_type, entity_id, event, status, note, user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, event, status, note, user_id, now_iso()),
    )
    log_audit(event, entity_type, entity_id, note, user_id)


def notify(user_id: int | None, role: str | None, title: str, message: str, entity_type: str | None = None, entity_id: int | None = None):
    run_query(
        """
        INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (user_id, role, title, message, entity_type, entity_id, now_iso()),
    )


def init_db():
    global _DB_INIT_DONE
    if _DB_INIT_DONE and DB_PATH.exists():
        return

    schemas = [
        """
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS role_permissions (
            role_name TEXT NOT NULL,
            permission_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(role_name, permission_name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            must_change_password INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            last_login_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'Active',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            category_type TEXT DEFAULT 'Procurement',
            status TEXT DEFAULT 'Active',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            category TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            bank_name TEXT,
            account_no TEXT,
            tax_id TEXT,
            rating INTEGER DEFAULT 3,
            completed_orders INTEGER DEFAULT 0,
            total_spend REAL DEFAULT 0,
            average_delivery_time REAL DEFAULT 0,
            rejection_count INTEGER DEFAULT 0,
            last_purchase_date TEXT,
            status TEXT DEFAULT 'Active',
            documents_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vendor_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id INTEGER,
            title TEXT,
            document_type TEXT,
            file_path TEXT,
            file_hash TEXT,
            notes TEXT,
            uploaded_by INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS purchase_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_no TEXT UNIQUE NOT NULL,
            requested_by INTEGER NOT NULL,
            department_project TEXT,
            request_date TEXT NOT NULL,
            required_date TEXT,
            category TEXT,
            justification TEXT,
            priority TEXT DEFAULT 'Normal',
            estimated_amount REAL DEFAULT 0,
            vendor_preference TEXT,
            status TEXT DEFAULT 'Draft',
            source_type TEXT DEFAULT 'Manual',
            imported_doc_id INTEGER,
            import_confidence REAL DEFAULT 0,
            attachments_json TEXT,
            notes TEXT,
            approval_history_json TEXT,
            linked_sourcing_task_id INTEGER,
            linked_po_id INTEGER,
            linked_receiving_slip_id INTEGER,
            linked_expense_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS purchase_request_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            description TEXT,
            quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            total REAL NOT NULL,
            category TEXT,
            suggested_vendor TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sourcing_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sourcing_no TEXT UNIQUE NOT NULL,
            request_id INTEGER NOT NULL,
            required_item_service TEXT,
            assigned_to INTEGER,
            status TEXT DEFAULT 'Open',
            recommended_vendor_id INTEGER,
            reason_for_recommendation TEXT,
            approval_status TEXT DEFAULT 'Pending',
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vendor_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sourcing_task_id INTEGER NOT NULL,
            vendor_id INTEGER,
            vendor_name TEXT,
            quoted_amount REAL NOT NULL,
            delivery_time_days REAL DEFAULT 0,
            payment_terms TEXT,
            warranty TEXT,
            vendor_rating INTEGER DEFAULT 3,
            notes TEXT,
            attachment_path TEXT,
            is_recommended INTEGER DEFAULT 0,
            score REAL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS quote_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sourcing_task_id INTEGER NOT NULL,
            lowest_price_vendor TEXT,
            fastest_delivery_vendor TEXT,
            best_rated_vendor TEXT,
            recommended_vendor TEXT,
            scoring_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_no TEXT UNIQUE NOT NULL,
            request_id INTEGER,
            vendor_id INTEGER,
            po_date TEXT NOT NULL,
            expected_delivery_date TEXT,
            status TEXT DEFAULT 'Draft',
            total_amount REAL DEFAULT 0,
            approved_by INTEGER,
            sent_to_vendor_date TEXT,
            payment_status TEXT DEFAULT 'Unpaid',
            receiving_status TEXT DEFAULT 'Pending Receipt',
            attachments_json TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            description TEXT,
            quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            total REAL NOT NULL,
            category TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS receiving_slips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slip_no TEXT UNIQUE NOT NULL,
            po_id INTEGER NOT NULL,
            vendor_id INTEGER,
            received_by INTEGER,
            date_received TEXT NOT NULL,
            delivery_note_no TEXT,
            discrepancy_notes TEXT,
            attachment_path TEXT,
            status TEXT DEFAULT 'Pending Receipt',
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS receiving_slip_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slip_id INTEGER NOT NULL,
            po_item_id INTEGER,
            item_name TEXT NOT NULL,
            quantity_ordered REAL NOT NULL,
            quantity_received REAL NOT NULL,
            item_condition TEXT DEFAULT 'Good',
            discrepancy_notes TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT,
            receipt_no TEXT,
            po_id INTEGER,
            vendor_id INTEGER,
            invoice_date TEXT,
            amount REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            file_path TEXT,
            file_hash TEXT,
            ocr_text TEXT,
            ocr_json TEXT,
            match_status TEXT DEFAULT 'Needs Review',
            mismatch_reasons TEXT,
            status TEXT DEFAULT 'Uploaded',
            uploaded_by INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_no TEXT UNIQUE NOT NULL,
            expense_date TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            vendor_id INTEGER,
            amount REAL NOT NULL,
            payment_method TEXT NOT NULL,
            project_department TEXT,
            status TEXT NOT NULL,
            receipt_path TEXT,
            receipt_hash TEXT,
            receipt_no TEXT,
            invoice_no TEXT,
            tax_amount REAL DEFAULT 0,
            linked_po_id INTEGER,
            invoice_match_status TEXT DEFAULT 'Not Matched',
            duplicate_warning INTEGER DEFAULT 0,
            requested_by INTEGER NOT NULL,
            approved_by INTEGER,
            approved_at TEXT,
            rejection_reason TEXT,
            ocr_text TEXT,
            ocr_json TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cash_advances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advance_no TEXT UNIQUE NOT NULL,
            date_collected TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            amount_collected REAL NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            approved_by INTEGER,
            approved_at TEXT,
            due_date TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS advance_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advance_id INTEGER NOT NULL,
            spent_date TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            receipt_path TEXT,
            receipt_hash TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_no TEXT UNIQUE NOT NULL,
            invoice_id INTEGER,
            po_id INTEGER,
            vendor_id INTEGER,
            amount REAL NOT NULL,
            payment_method TEXT,
            payment_date TEXT,
            status TEXT DEFAULT 'Pending Approval',
            approved_by INTEGER,
            paid_by INTEGER,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            budget_month TEXT NOT NULL,
            category TEXT NOT NULL,
            department_project TEXT DEFAULT 'General',
            limit_amount REAL NOT NULL,
            override_required INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(budget_month, category, department_project)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS approval_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            threshold_amount REAL DEFAULT 0,
            approver_role TEXT DEFAULT 'Approver',
            requires_sourcing INTEGER DEFAULT 0,
            requires_finance INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS approval_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            status_before TEXT,
            status_after TEXT,
            reason TEXT,
            user_id INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            status TEXT,
            note TEXT,
            user_id INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            entity_type TEXT,
            entity_id INTEGER,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            comment_text TEXT NOT NULL,
            is_internal INTEGER DEFAULT 0,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_hash TEXT,
            mime_type TEXT,
            uploaded_by INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS imported_legacy_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_zip_name TEXT,
            original_path TEXT UNIQUE,
            file_name TEXT,
            file_path TEXT,
            file_hash TEXT,
            document_type TEXT,
            department_project TEXT,
            title TEXT,
            likely_date TEXT,
            likely_vendor TEXT,
            total_amount REAL DEFAULT 0,
            import_status TEXT DEFAULT 'Imported - Needs Review',
            confidence REAL DEFAULT 0,
            extracted_text TEXT,
            parsed_json TEXT,
            linked_request_id INTEGER,
            duplicate_warning INTEGER DEFAULT 0,
            imported_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS parsed_document_line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imported_doc_id INTEGER NOT NULL,
            row_number INTEGER,
            item_name TEXT,
            description TEXT,
            quantity REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            total_price REAL DEFAULT 0,
            category TEXT,
            status_of_purchase TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_extraction_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_zip_name TEXT,
            original_path TEXT,
            action TEXT,
            status TEXT,
            message TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT UNIQUE NOT NULL,
            email_enabled INTEGER DEFAULT 0,
            in_app_enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            user_id INTEGER,
            role TEXT,
            details TEXT,
            before_values TEXT,
            after_values TEXT,
            created_at TEXT NOT NULL
        )
        """,
    ]
    for schema in schemas:
        run_query(schema)
    ensure_schema_migrations()
    ensure_enterprise_schema()
    ensure_phase2_schema()
    ensure_hardening_schema()
    ensure_finance_document_schema()
    ensure_dashboard_upgrade_schema()
    ensure_performance_indexes()
    seed_defaults()
    seed_enterprise_defaults()
    seed_phase2_defaults()
    ensure_command_chain_schema()
    _DB_INIT_DONE = True


def ensure_schema_migrations():
    migrations = {
        "users": [("must_change_password", "must_change_password INTEGER DEFAULT 0"), ("is_active", "is_active INTEGER DEFAULT 1"), ("last_login_at", "last_login_at TEXT"), ("email", "email TEXT")],
        "vendors": [("email", "email TEXT"), ("tax_id", "tax_id TEXT"), ("completed_orders", "completed_orders INTEGER DEFAULT 0"), ("total_spend", "total_spend REAL DEFAULT 0"), ("average_delivery_time", "average_delivery_time REAL DEFAULT 0"), ("rejection_count", "rejection_count INTEGER DEFAULT 0"), ("last_purchase_date", "last_purchase_date TEXT"), ("status", "status TEXT DEFAULT 'Active'"), ("documents_json", "documents_json TEXT"), ("updated_at", "updated_at TEXT")],
        "purchase_requests": [("source_type", "source_type TEXT DEFAULT 'Manual'"), ("imported_doc_id", "imported_doc_id INTEGER"), ("import_confidence", "import_confidence REAL DEFAULT 0")],
        "expenses": [("receipt_hash", "receipt_hash TEXT"), ("receipt_no", "receipt_no TEXT"), ("invoice_no", "invoice_no TEXT"), ("tax_amount", "tax_amount REAL DEFAULT 0"), ("linked_po_id", "linked_po_id INTEGER"), ("invoice_match_status", "invoice_match_status TEXT DEFAULT 'Not Matched'"), ("duplicate_warning", "duplicate_warning INTEGER DEFAULT 0"), ("ocr_json", "ocr_json TEXT"), ("ocr_text", "ocr_text TEXT")],
        "cash_advances": [("due_date", "due_date TEXT")],
        "advance_expenses": [("receipt_hash", "receipt_hash TEXT")],
        "budgets": [("department_project", "department_project TEXT DEFAULT 'General'"), ("override_required", "override_required INTEGER DEFAULT 0")],
        "audit_logs": [("role", "role TEXT"), ("before_values", "before_values TEXT"), ("after_values", "after_values TEXT")],
    }
    for table, cols in migrations.items():
        for column, ddl in cols:
            add_column_if_missing(table, column, ddl)


# ---------------- Enterprise extension migrations and helpers ----------------

def ensure_enterprise_schema():
    """Add enterprise procurement workflow extensions without dropping existing data."""
    enterprise_schemas = [
        """
        CREATE TABLE IF NOT EXISTS annual_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            budget_year INTEGER NOT NULL,
            department_project TEXT DEFAULT 'General',
            category TEXT DEFAULT 'All',
            annual_amount REAL NOT NULL DEFAULT 0,
            distribution_json TEXT,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            UNIQUE(budget_year, department_project, category)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS budget_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            budget_type TEXT NOT NULL,
            budget_id INTEGER,
            budget_month TEXT,
            budget_year INTEGER,
            department_project TEXT,
            category TEXT,
            adjustment_amount REAL NOT NULL DEFAULT 0,
            reason TEXT,
            adjusted_by INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS budget_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            budget_type TEXT NOT NULL,
            budget_id INTEGER,
            action TEXT NOT NULL,
            before_values TEXT,
            after_values TEXT,
            note TEXT,
            changed_by INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS approval_delegations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            primary_role TEXT NOT NULL,
            delegate_role TEXT NOT NULL,
            enabled INTEGER DEFAULT 0,
            start_date TEXT,
            end_date TEXT,
            reason TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS facility_manager_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility_manager_user_id INTEGER NOT NULL,
            procurement_manager_user_id INTEGER NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            UNIQUE(facility_manager_user_id, procurement_manager_user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS collaboration_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            facility_manager_user_id INTEGER,
            procurement_manager_user_id INTEGER,
            visibility_scope TEXT DEFAULT 'FM_PM_ADMIN',
            created_at TEXT NOT NULL,
            updated_at TEXT,
            UNIQUE(entity_type, entity_id, facility_manager_user_id, procurement_manager_user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS collaboration_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            sender_user_id INTEGER NOT NULL,
            message_text TEXT,
            attachment_path TEXT,
            is_private INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            action TEXT NOT NULL,
            entity_type TEXT,
            entity_id INTEGER,
            public_summary TEXT,
            private_details TEXT,
            visibility_scope TEXT DEFAULT 'role',
            related_user_id INTEGER,
            created_at TEXT NOT NULL
        )
        """,
    ]
    for schema in enterprise_schemas:
        run_query(schema)

    migrations = {
        "users": [
            ("account_locked", "account_locked INTEGER DEFAULT 0"),
            ("failed_login_count", "failed_login_count INTEGER DEFAULT 0"),
            ("updated_at", "updated_at TEXT"),
        ],
        "notifications": [
            ("popup_shown", "popup_shown INTEGER DEFAULT 0"),
            ("importance", "importance TEXT DEFAULT 'Normal'"),
        ],
        "approval_rules": [
            ("primary_approver_role", "primary_approver_role TEXT DEFAULT 'Approver'"),
            ("backup_approver_role", "backup_approver_role TEXT"),
            ("pm_fallback_enabled", "pm_fallback_enabled INTEGER DEFAULT 0"),
            ("finance_required", "finance_required INTEGER DEFAULT 1"),
            ("sourcing_required", "sourcing_required INTEGER DEFAULT 0"),
            ("approval_timeout_hours", "approval_timeout_hours INTEGER DEFAULT 48"),
            ("updated_at", "updated_at TEXT"),
        ],
        "purchase_requests": [
            ("payment_status", "payment_status TEXT DEFAULT 'Not Ready'"),
            ("facility_manager_user_id", "facility_manager_user_id INTEGER"),
            ("assigned_procurement_manager_id", "assigned_procurement_manager_id INTEGER"),
            ("official_request_id", "official_request_id INTEGER"),
            ("converted_from_draft_id", "converted_from_draft_id INTEGER"),
            ("approval_due_at", "approval_due_at TEXT"),
            ("delegated_approval_allowed", "delegated_approval_allowed INTEGER DEFAULT 0"),
            ("finance_note", "finance_note TEXT"),
        ],
        "purchase_orders": [
            ("approved_by_role", "approved_by_role TEXT"),
            ("approval_mode", "approval_mode TEXT DEFAULT 'Normal Approval Mode'"),
        ],
        "payments": [
            ("proof_path", "proof_path TEXT"),
            ("finance_note", "finance_note TEXT"),
        ],
        "approval_history": [
            ("approved_by_user_id", "approved_by_user_id INTEGER"),
            ("approved_by_role", "approved_by_role TEXT"),
            ("approval_mode", "approval_mode TEXT DEFAULT 'Normal Approval Mode'"),
            ("delegation_reason", "delegation_reason TEXT"),
            ("original_approver_role", "original_approver_role TEXT"),
            ("note", "note TEXT"),
        ],
        "imported_legacy_documents": [
            ("assigned_procurement_manager_id", "assigned_procurement_manager_id INTEGER"),
            ("facility_manager_user_id", "facility_manager_user_id INTEGER"),
        ],
    }
    for table, cols in migrations.items():
        for column, ddl in cols:
            add_column_if_missing(table, column, ddl)

    # Backfill new approval rule aliases from the original columns.
    run_query("""
        UPDATE approval_rules
        SET primary_approver_role = COALESCE(NULLIF(primary_approver_role, ''), approver_role),
            finance_required = COALESCE(finance_required, requires_finance),
            sourcing_required = COALESCE(sourcing_required, requires_sourcing)
    """)


def seed_enterprise_defaults():
    """Seed Facility Manager role, permissions, demo user, default link, and delegation safely."""
    hash_password = _seed_hash_password
    roles = [
        ("Facility Manager", "Assistant procurement preparation role linked to Procurement Manager"),
    ]
    for name, desc in roles:
        run_query("INSERT OR IGNORE INTO roles (name, description, created_at) VALUES (?, ?, ?)", (name, desc, now_iso()))

    new_permissions = [
        "submit_to_procurement_manager", "import_documents_limited", "upload_supporting_documents",
        "view_own_requests", "view_own_activity_history", "communicate_with_procurement_manager",
        "delegated_approval", "view_budget_tracker", "manage_approval_delegation", "view_notifications_monitor",
        "view_all_activity_logs", "approved_for_payment", "return_for_clarification"
    ]
    for p in new_permissions:
        run_query("INSERT OR IGNORE INTO permissions (name, description, created_at) VALUES (?, ?, ?)", (p, p.replace('_', ' ').title(), now_iso()))

    admin_perms = [r["name"] for r in run_query("SELECT name FROM permissions", fetch=True)]
    role_map = {
        "Admin": admin_perms,
        "Procurement Manager": [
            "change_password", "create_request", "edit_request", "submit_request", "procurement_review",
            "create_sourcing", "manage_quotes", "recommend_vendor", "create_po", "receive_goods",
            "record_expense", "manage_vendor", "import_documents", "view_reports", "delegated_approval",
            "approved_for_payment", "return_for_clarification", "communicate_with_procurement_manager",
        ],
        "Facility Manager": [
            "change_password", "create_request", "edit_own_request", "submit_to_procurement_manager",
            "import_documents_limited", "upload_supporting_documents", "view_own_requests",
            "view_own_activity_history", "communicate_with_procurement_manager",
        ],
        "Finance": [
            "change_password", "create_request", "record_expense", "review_invoice", "approve_expense",
            "manage_payments", "approve_payment", "manage_budget", "view_reports", "approved_for_payment",
            "return_for_clarification",
        ],
        "Approver": ["change_password", "approve_request", "reject_request", "approve_po", "approve_payment", "view_reports"],
        "Auditor": ["change_password", "view_reports", "audit", "read_only_all"],
    }
    for role, perms in role_map.items():
        for perm in perms:
            run_query("INSERT OR IGNORE INTO role_permissions (role_name, permission_name, created_at) VALUES (?, ?, ?)", (role, perm, now_iso()))

    demo_users = [
        ("facility", "Facility Manager", "Facility Manager", "facility123"),
    ]
    for username, full_name, role, pwd in demo_users:
        exists = run_query("SELECT id FROM users WHERE username=?", (username,), fetch=True)
        if not exists:
            run_query(
                "INSERT INTO users (username, full_name, role, password_hash, must_change_password, is_active, created_at) VALUES (?, ?, ?, ?, 0, 1, ?)",
                (username, full_name, role, hash_password(pwd), now_iso()),
            )

    pm = run_query("SELECT id FROM users WHERE role='Procurement Manager' ORDER BY id LIMIT 1", fetch=True)
    fm = run_query("SELECT id FROM users WHERE username='facility' ORDER BY id LIMIT 1", fetch=True)
    admin = run_query("SELECT id FROM users WHERE role='Admin' ORDER BY id LIMIT 1", fetch=True)
    if pm and fm:
        run_query(
            "INSERT OR IGNORE INTO facility_manager_links (facility_manager_user_id, procurement_manager_user_id, is_active, created_by, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
            (fm[0]["id"], pm[0]["id"], admin[0]["id"] if admin else None, now_iso(), now_iso()),
        )

    existing_delegation = run_query("SELECT id FROM approval_delegations WHERE primary_role='Approver' AND delegate_role='Procurement Manager' LIMIT 1", fetch=True)
    if not existing_delegation:
        run_query(
            "INSERT INTO approval_delegations (primary_role, delegate_role, enabled, reason, created_by, created_at, updated_at) VALUES ('Approver', 'Procurement Manager', 0, 'Default delegation record; enable from Admin Approval Configuration.', ?, ?, ?)",
            (admin[0]["id"] if admin else None, now_iso(), now_iso()),
        )

    # Ensure approval rules can show Procurement Manager as fallback without changing the original primary approver.
    run_query("UPDATE approval_rules SET backup_approver_role=COALESCE(backup_approver_role, 'Procurement Manager') WHERE backup_approver_role IS NULL")


def create_activity_log(
    user_id: int | None,
    role: str | None,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    public_summary: str | None = None,
    private_details: str | dict | None = None,
    visibility_scope: str = "role",
    related_user_id: int | None = None,
):
    if isinstance(private_details, dict):
        private_details = json_dump(private_details)
    run_query(
        """
        INSERT INTO activity_logs (user_id, role, action, entity_type, entity_id, public_summary, private_details, visibility_scope, related_user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, role, action, entity_type, entity_id, public_summary, private_details, visibility_scope, related_user_id, now_iso()),
    )


def create_notification(
    user_id: int | None,
    role: str | None,
    title: str,
    message: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    importance: str = "Normal",
):
    cols = table_columns("notifications")
    if "popup_shown" in cols and "importance" in cols:
        run_query(
            """
            INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, popup_shown, importance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (user_id, role, title, message, entity_type, entity_id, importance, now_iso()),
        )
    else:
        run_query(
            """
            INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (user_id, role, title, message, entity_type, entity_id, now_iso()),
        )


def notify(user_id: int | None, role: str | None, title: str, message: str, entity_type: str | None = None, entity_id: int | None = None):
    create_notification(user_id, role, title, message, entity_type, entity_id)


def notify_related_users(request_id: int, title: str, message: str, include_finance: bool = False, include_procurement: bool = False):
    rows = run_query("SELECT requested_by, facility_manager_user_id, assigned_procurement_manager_id FROM purchase_requests WHERE id=?", (request_id,), fetch=True)
    if not rows:
        return
    row = rows[0]
    targets = {row["requested_by"], row["facility_manager_user_id"], row["assigned_procurement_manager_id"]}
    for uid in [x for x in targets if x]:
        create_notification(uid, None, title, message, "Purchase Request", request_id, "Important")
    if include_finance:
        create_notification(None, "Finance", title, message, "Purchase Request", request_id, "Important")
    if include_procurement:
        create_notification(None, "Procurement Manager", title, message, "Purchase Request", request_id, "Important")


def transition_request_status(
    request_id: int,
    new_status: str,
    event: str,
    note: str | None = None,
    actor_user_id: int | None = None,
    actor_role: str | None = None,
    approval_mode: str = "Normal Approval Mode",
    delegation_reason: str | None = None,
    original_approver_role: str | None = None,
    payment_status: str | None = None,
):
    rows = run_query("SELECT * FROM purchase_requests WHERE id=?", (request_id,), fetch=True)
    if not rows:
        return
    old = dict(rows[0])
    update_bits = ["status=?", "updated_at=?"]
    params: list[Any] = [new_status, now_iso()]
    if payment_status is not None and "payment_status" in table_columns("purchase_requests"):
        update_bits.append("payment_status=?")
        params.append(payment_status)
    params.append(request_id)
    run_query(f"UPDATE purchase_requests SET {', '.join(update_bits)} WHERE id=?", params)
    add_workflow_event("Purchase Request", request_id, event, new_status, note, actor_user_id)
    create_activity_log(actor_user_id, actor_role, event, "Purchase Request", request_id, f"{old.get('request_no')} moved from {old.get('status')} to {new_status}", note, "workflow", old.get("requested_by"))
    if event.lower().startswith(("approved", "rejected", "returned")) or new_status in {"Approved", "Rejected", "Returned", "Returned to Facility Manager"}:
        run_query(
            """
            INSERT INTO approval_history (entity_type, entity_id, action, status_before, status_after, reason, user_id, approved_by_user_id, approved_by_role, approval_mode, delegation_reason, original_approver_role, note, created_at)
            VALUES ('Purchase Request', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (request_id, event, old.get("status"), new_status, note, actor_user_id, actor_user_id, actor_role, approval_mode, delegation_reason, original_approver_role, note, now_iso()),
        )
    log_audit(event, "Purchase Request", request_id, note, actor_user_id, actor_role, before_values={"status": old.get("status")}, after_values={"status": new_status, "payment_status": payment_status})
    notify_related_users(
        request_id,
        f"Request {new_status}",
        f"{old.get('request_no')} is now {new_status}. {note or ''}".strip(),
        include_finance=(new_status in {"Approved", "Finance Review", "Approved for Payment", "Paid"}),
        include_procurement=(new_status in {"Submitted", "Approved", "Returned", "Paid"}),
    )


def active_delegation(primary_role: str = "Approver", delegate_role: str = "Procurement Manager"):
    today = date.today().isoformat()
    rows = run_query(
        """
        SELECT * FROM approval_delegations
        WHERE primary_role=? AND delegate_role=? AND enabled=1
          AND (start_date IS NULL OR start_date='' OR start_date<=?)
          AND (end_date IS NULL OR end_date='' OR end_date>=?)
        ORDER BY updated_at DESC, created_at DESC LIMIT 1
        """,
        (primary_role, delegate_role, today, today),
        fetch=True,
    )
    return dict(rows[0]) if rows else None


def _seed_hash_password(password: str) -> str:
    # Same PBKDF2-HMAC-SHA256 format used by core.auth, duplicated here so
    # database initialization can run without importing Streamlit.
    import base64, hashlib, os
    iterations = 260_000
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode('ascii')}${base64.b64encode(digest).decode('ascii')}"



# ---------------- Phase 2: push notifications, availability, gateway passes ----------------

def ensure_phase2_schema():
    """Add notification preferences, web-push outbox, away notices, and gateway pass workflow safely."""
    phase2_schemas = [
        """
        CREATE TABLE IF NOT EXISTS notification_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            in_app_enabled INTEGER DEFAULT 1,
            browser_push_enabled INTEGER DEFAULT 0,
            email_enabled INTEGER DEFAULT 0,
            important_only INTEGER DEFAULT 1,
            approval_notifications INTEGER DEFAULT 1,
            gateway_pass_notifications INTEGER DEFAULT 1,
            finance_notifications INTEGER DEFAULT 1,
            delegation_notifications INTEGER DEFAULT 1,
            browser_permission_status TEXT DEFAULT 'not_requested',
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT,
            p256dh_key TEXT,
            auth_key TEXT,
            user_agent TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            last_success_at TEXT,
            last_failure_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER,
            channel TEXT NOT NULL,
            target_user_id INTEGER,
            target_role TEXT,
            status TEXT DEFAULT 'Queued',
            attempts INTEGER DEFAULT 0,
            error_message TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            status TEXT DEFAULT 'Away Requested',
            away_start_date TEXT NOT NULL,
            away_end_date TEXT NOT NULL,
            reason TEXT NOT NULL,
            handover_note TEXT,
            recommended_delegate_role TEXT,
            recommended_delegate_user_id INTEGER,
            urgency TEXT DEFAULT 'Normal',
            admin_review_status TEXT DEFAULT 'Pending Review',
            reviewed_by_admin_id INTEGER,
            reviewed_at TEXT,
            linked_delegation_id INTEGER,
            admin_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gateway_passes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pass_number TEXT UNIQUE NOT NULL,
            facility_manager_user_id INTEGER NOT NULL,
            department_id INTEGER,
            department TEXT,
            movement_type TEXT NOT NULL,
            purpose TEXT NOT NULL,
            origin_location TEXT,
            destination TEXT,
            expected_movement_date TEXT,
            expected_return_date TEXT,
            vehicle_number TEXT,
            driver_name TEXT,
            driver_phone TEXT,
            receiver_name TEXT,
            receiver_organization TEXT,
            security_checkpoint TEXT,
            status TEXT DEFAULT 'Draft',
            submitted_at TEXT,
            approved_at TEXT,
            approved_by_user_id INTEGER,
            approved_by_role TEXT,
            approval_note TEXT,
            rejected_at TEXT,
            rejected_by_user_id INTEGER,
            rejection_reason TEXT,
            generated_at TEXT,
            downloaded_at TEXT,
            generated_file_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gateway_pass_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gateway_pass_id INTEGER NOT NULL,
            item_description TEXT NOT NULL,
            item_category TEXT,
            quantity REAL NOT NULL,
            unit_of_measure TEXT NOT NULL,
            quality_condition TEXT NOT NULL,
            estimated_value REAL,
            serial_number TEXT,
            asset_tag TEXT,
            fragility_status TEXT NOT NULL,
            handling_instruction TEXT,
            remarks TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gateway_pass_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gateway_pass_id INTEGER NOT NULL,
            approver_user_id INTEGER,
            approver_role TEXT,
            decision TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gateway_pass_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gateway_pass_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            status TEXT,
            note TEXT,
            user_id INTEGER,
            created_at TEXT NOT NULL
        )
        """,
    ]
    for schema in phase2_schemas:
        run_query(schema)

    phase2_migrations = {
        "users": [
            ("email", "email TEXT"),
        ],
        "notification_outbox": [
            ("recipient_email", "recipient_email TEXT"),
            ("subject", "subject TEXT"),
            ("body", "body TEXT"),
            ("last_failure_at", "last_failure_at TEXT"),
        ],
        "notifications": [
            ("popup_shown", "popup_shown INTEGER DEFAULT 0"),
            ("importance", "importance TEXT DEFAULT 'Normal'"),
            ("delivery_channel", "delivery_channel TEXT DEFAULT 'in_app'"),
            ("push_sent", "push_sent INTEGER DEFAULT 0"),
            ("email_sent", "email_sent INTEGER DEFAULT 0"),
            ("action_url", "action_url TEXT"),
            ("action_label", "action_label TEXT"),
            ("expires_at", "expires_at TEXT"),
        ],
        "approval_delegations": [
            ("source_availability_id", "source_availability_id INTEGER"),
            ("source_reason", "source_reason TEXT"),
            ("activated_by_admin_id", "activated_by_admin_id INTEGER"),
            ("activation_note", "activation_note TEXT"),
            ("primary_user_id", "primary_user_id INTEGER"),
            ("delegate_user_id", "delegate_user_id INTEGER"),
        ],
        "gateway_pass_items": [
            ("colour", "colour TEXT"),
        ],
    }
    for table, cols in phase2_migrations.items():
        for column, ddl in cols:
            add_column_if_missing(table, column, ddl)


def seed_phase2_defaults():
    """Create notification preference rows for all users and seed new permissions."""
    new_permissions = [
        "manage_notification_preferences", "browser_push_setup", "mark_away", "manage_availability",
        "create_gateway_pass", "edit_own_gateway_pass", "submit_gateway_pass", "review_gateway_pass",
        "approve_gateway_pass", "audit_gateway_pass", "generate_gateway_pass", "download_gateway_pass",
    ]
    for p in new_permissions:
        run_query("INSERT OR IGNORE INTO permissions (name, description, created_at) VALUES (?, ?, ?)", (p, p.replace('_', ' ').title(), now_iso()))
    role_map = {
        "Admin": new_permissions,
        "Procurement Manager": ["manage_notification_preferences", "browser_push_setup", "mark_away", "review_gateway_pass", "approve_gateway_pass"],
        "Facility Manager": ["manage_notification_preferences", "browser_push_setup", "create_gateway_pass", "edit_own_gateway_pass", "submit_gateway_pass", "generate_gateway_pass", "download_gateway_pass"],
        "Finance": ["manage_notification_preferences", "browser_push_setup"],
        "Approver": ["manage_notification_preferences", "browser_push_setup", "mark_away", "review_gateway_pass", "approve_gateway_pass"],
        "Auditor": ["manage_notification_preferences", "browser_push_setup", "audit_gateway_pass"],
    }
    for role, perms in role_map.items():
        for perm in perms:
            run_query("INSERT OR IGNORE INTO role_permissions (role_name, permission_name, created_at) VALUES (?, ?, ?)", (role, perm, now_iso()))

    users = run_query("SELECT id FROM users", fetch=True)
    for u in users:
        run_query(
            """
            INSERT OR IGNORE INTO notification_preferences
            (user_id, in_app_enabled, browser_push_enabled, email_enabled, important_only, approval_notifications, gateway_pass_notifications, finance_notifications, delegation_notifications, browser_permission_status, created_at, updated_at)
            VALUES (?, 1, 0, 0, 1, 1, 1, 1, 1, 'not_requested', ?, ?)
            """,
            (u["id"], now_iso(), now_iso()),
        )


def _notification_category_allowed(pref: dict, title: str, entity_type: str | None) -> bool:
    title_l = (title or "").lower()
    entity_l = (entity_type or "").lower()
    if "gateway" in title_l or entity_l == "gateway pass":
        return bool(pref.get("gateway_pass_notifications", 1))
    if any(x in title_l for x in ["approval", "approved", "rejected", "returned"]):
        return bool(pref.get("approval_notifications", 1))
    if "finance" in title_l or "payment" in title_l:
        return bool(pref.get("finance_notifications", 1))
    if any(x in title_l for x in ["away", "delegation", "delegate"]):
        return bool(pref.get("delegation_notifications", 1))
    return True


def _queue_notification_channel(notification_id: int | None, channel: str, user_id: int | None, role: str | None, status: str = "Queued", error: str | None = None):
    run_query(
        """
        INSERT INTO notification_outbox (notification_id, channel, target_user_id, target_role, status, attempts, error_message, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (notification_id, channel, user_id, role, status, error, now_iso()),
    )


def _infer_notification_section(target_role: str | None, title: str | None, entity_type: str | None, action_label: str | None = None) -> str | None:
    """Map a notification to the sidebar section whose red badge should light up.

    The badge is section-aware and clears when the user opens that section.
    This helper keeps notification routing simple and avoids making every
    notification appear on every tab.
    """
    role = target_role or ""
    text = f"{title or ''} {entity_type or ''} {action_label or ''}".lower()
    if role == "Admin":
        if "gateway" in text:
            return "Gateway Pass Management"
        if any(x in text for x in ["away", "delegate", "delegation", "availability"]):
            return "Availability & Delegation Requests"
        if "notification" in text or "push" in text or "outbox" in text:
            return "Notifications Monitor"
        if "budget" in text:
            return "Budget Tracker"
        if "login" in text or "logout" in text or "audit" in text:
            return "Audit Logs"
        return "Admin Dashboard"
    if role == "Procurement Manager":
        if "gateway" in text:
            return "Gateway Pass Review"
        if any(x in text for x in ["closure", "paid request", "post-payment", "complete, close", "archive"]):
            return "Post-Payment Closure"
        if "facility manager" in text or "utility head" in text or "facility head" in text or "fm draft" in text:
            return "Utility Head / Facility Head Inbox"
        if "delegated" in text or "acting" in text:
            return "Acting Approval Queue"
        if any(x in text for x in ["away", "delegate", "availability"]):
            return "Availability / Away Notice"
        if "purchase request" in text or "request" in text:
            return "Purchase Requests"
        return "Operations Dashboard"
    if role == "Facility Manager":
        if "gateway" in text:
            return "Gateway Pass"
        if "returned" in text:
            return "Returned Requests"
        if any(x in text for x in ["approved", "accepted", "converted"]):
            return "Approved / Accepted Requests"
        if "draft" in text:
            return "My Draft Requests"
        return "Utility / Facility Dashboard"
    if role == "Finance":
        if "invoice" in text:
            return "Invoices"
        if "receipt" in text:
            return "Receipts"
        if "payment" in text or "finance" in text:
            return "Approved for Payment"
        return "Financial Dashboard"
    if role == "Approver":
        if "gateway" in text:
            return "Gateway Pass Approval"
        if "po" in text or "purchase order" in text:
            return "PO Approval"
        if "payment" in text:
            return "Payment Approval"
        if any(x in text for x in ["away", "delegate", "availability"]):
            return "Availability / Away Notice"
        if "approval" in text or "request" in text:
            return "Pending Approvals"
        return "Approval Dashboard"
    if role == "Auditor":
        if "gateway" in text:
            return "Gateway Pass Audit"
        if "budget" in text:
            return "Budget Audit"
        if "delegat" in text:
            return "Delegated Approval Review"
        if "approval" in text:
            return "Approval Trails"
        return "Audit Dashboard"
    return None


def create_notification(
    user_id: int | None = None,
    role: str | None = None,
    title: str = "",
    message: str = "",
    entity_type: str | None = None,
    entity_id: int | None = None,
    importance: str = "Normal",
    channels: list[str] | tuple[str, ...] | None = None,
    action_url: str | None = None,
    action_label: str | None = None,
):
    """Create one in-app notification and queue optional external delivery.

    Streamlit toasts are in-app only. Browser push/email delivery is modeled as an outbox so
    supported deployments can send externally, while local deployments safely retain unread alerts.
    """
    ensure_phase2_schema()
    channels = list(channels or ["in_app"])
    delivery_channel = ",".join(channels)
    target_user_ids: list[int | None]
    if user_id is not None:
        target_user_ids = [int(user_id)]
    elif role:
        rows = run_query("SELECT id FROM users WHERE role=? AND is_active=1", (role,), fetch=True)
        target_user_ids = [int(r["id"]) for r in rows] or [None]
    else:
        target_user_ids = [None]

    created_ids: list[int] = []
    for target_uid in target_user_ids:
        pref = {}
        if target_uid is not None:
            rows = run_query("SELECT * FROM notification_preferences WHERE user_id=?", (target_uid,), fetch=True)
            if rows:
                pref = dict(rows[0])
            else:
                run_query(
                    "INSERT OR IGNORE INTO notification_preferences (user_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (target_uid, now_iso(), now_iso()),
                )
                pref = dict(run_query("SELECT * FROM notification_preferences WHERE user_id=?", (target_uid,), fetch=True)[0])
        if pref and not _notification_category_allowed(pref, title, entity_type):
            continue
        if pref and int(pref.get("important_only") or 0) and "browser_push" in channels and importance not in ("High", "Critical", "Important"):
            channels = [c for c in channels if c != "browser_push"]
        should_insert_in_app = not pref or int(pref.get("in_app_enabled", 1)) or "in_app" in channels
        notification_id = None
        if should_insert_in_app:
            notification_id = run_insert(
                """
                INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, popup_shown, importance, delivery_channel, push_sent, email_sent, action_url, action_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 0, 0, ?, ?, ?)
                """,
                (target_uid, None if target_uid is not None else role, title, message, entity_type, entity_id, importance, delivery_channel, action_url, action_label, now_iso()),
            )
            created_ids.append(notification_id)
        if "browser_push" in channels or "all" in channels:
            enabled = pref and int(pref.get("browser_push_enabled") or 0)
            if enabled:
                _queue_notification_channel(notification_id, "browser_push", target_uid, None if target_uid is not None else role)
            else:
                _queue_notification_channel(notification_id, "browser_push", target_uid, None if target_uid is not None else role, "Fallback", "Browser push not enabled; kept as in-app unread alert.")
        if "email" in channels or "all" in channels:
            enabled = pref and int(pref.get("email_enabled") or 0)
            _queue_notification_channel(notification_id, "email", target_uid, None if target_uid is not None else role, "Queued" if enabled else "Skipped", None if enabled else "Email not enabled/configured.")
        if importance in ("Critical", "High"):
            log_audit("CRITICAL_NOTIFICATION_CREATED", "Notification", notification_id, {"title": title, "entity_type": entity_type, "entity_id": entity_id}, user_id=target_uid, role=role)
    return created_ids[0] if len(created_ids) == 1 else created_ids


def notify(user_id: int | None, role: str | None, title: str, message: str, entity_type: str | None = None, entity_id: int | None = None):
    return create_notification(user_id=user_id, role=role, title=title, message=message, entity_type=entity_type, entity_id=entity_id, importance="Normal", channels=["in_app"])


def log_gateway_pass_event(gateway_pass_id: int, event: str, status: str | None = None, note: str | None = None, user_id: int | None = None):
    run_query(
        "INSERT INTO gateway_pass_events (gateway_pass_id, event, status, note, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (gateway_pass_id, event, status, note, user_id, now_iso()),
    )
    log_audit(event, "Gateway Pass", gateway_pass_id, note, user_id)


def notify_gateway_pass_reviewers(gateway_pass_id: int, title: str = "Gateway Pass Requires Review", message: str | None = None):
    gp = run_query("SELECT * FROM gateway_passes WHERE id=?", (gateway_pass_id,), fetch=True)
    if not gp:
        return
    gp = gp[0]
    msg = message or f"Gateway pass {gp['pass_number']} requires review."
    create_notification(None, "Procurement Manager", title, msg, "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")
    create_notification(None, "Approver", title, msg, "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")
    create_notification(None, "Admin", "Gateway Pass Oversight", msg, "Gateway Pass", gateway_pass_id, "Normal", ["in_app"])

def seed_defaults():
    hash_password = _seed_hash_password

    roles = [
        ("Admin", "System administration and all records"),
        ("Procurement Manager", "Procurement operations, sourcing, PO and receiving"),
        ("Finance", "Invoices, payments, expenses, budgets and cash advances"),
        ("Approver", "Executive approval and decision workflow"),
        ("Auditor", "Read-only audit, compliance and source document review"),
    ]
    for name, desc in roles:
        run_query("INSERT OR IGNORE INTO roles (name, description, created_at) VALUES (?, ?, ?)", (name, desc, now_iso()))

    permissions = [
        "admin", "create_user", "manage_roles", "change_password", "create_request", "edit_request", "submit_request", "procurement_review", "create_sourcing", "manage_quotes", "recommend_vendor", "approve_request", "reject_request", "create_po", "approve_po", "receive_goods", "record_expense", "review_invoice", "approve_expense", "manage_payments", "approve_payment", "manage_vendor", "manage_budget", "import_documents", "view_reports", "audit", "read_only_all"
    ]
    for p in permissions:
        run_query("INSERT OR IGNORE INTO permissions (name, description, created_at) VALUES (?, ?, ?)", (p, p.replace('_', ' ').title(), now_iso()))

    role_map = {
        "Admin": permissions,
        "Procurement Manager": ["change_password", "create_request", "edit_request", "submit_request", "procurement_review", "create_sourcing", "manage_quotes", "recommend_vendor", "create_po", "receive_goods", "record_expense", "manage_vendor", "import_documents", "view_reports"],
        "Finance": ["change_password", "create_request", "record_expense", "review_invoice", "approve_expense", "manage_payments", "approve_payment", "manage_budget", "view_reports"],
        "Approver": ["change_password", "approve_request", "reject_request", "approve_po", "approve_payment", "view_reports"],
        "Auditor": ["change_password", "view_reports", "audit", "read_only_all"],
    }
    for role, perms in role_map.items():
        for perm in perms:
            run_query("INSERT OR IGNORE INTO role_permissions (role_name, permission_name, created_at) VALUES (?, ?, ?)", (role, perm, now_iso()))

    if not run_query("SELECT COUNT(*) AS count FROM users", fetch=True)[0]["count"]:
        users = [
            ("admin", "System Admin", "Admin", hash_password("admin123"), 0, 1, now_iso()),
            ("procurement", "Procurement Manager", "Procurement Manager", hash_password("procure123"), 0, 1, now_iso()),
            ("finance", "Finance Manager", "Finance", hash_password("finance123"), 0, 1, now_iso()),
            ("approver", "Managing Director", "Approver", hash_password("approve123"), 0, 1, now_iso()),
            ("auditor", "Internal Auditor", "Auditor", hash_password("audit123"), 0, 1, now_iso()),
        ]
        run_query("INSERT INTO users (username, full_name, role, password_hash, must_change_password, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", users, many=True)

    departments = ["General", "CMOTD", "RACAM", "CMOTD AND RACAM", "Operations", "Finance", "Administration", "Facilities", "Maintenance", "Logistics"]
    for dept in departments:
        run_query("INSERT OR IGNORE INTO departments (name, description, status, created_at) VALUES (?, ?, 'Active', ?)", (dept, f"{dept} procurement records", now_iso()))

    categories = ["Diesel/Fuel", "Water", "Office Supplies", "Repairs/Maintenance", "Vehicle Maintenance", "Generator Maintenance", "Plumbing", "Welding/Fabrication", "Grass Cutting", "Transport/Logistics", "Staff Welfare", "ICT/Software", "Utilities", "Construction Materials", "Professional Services", "Operational Purchases", "Other"]
    for cat in categories:
        run_query("INSERT OR IGNORE INTO categories (name, category_type, status, created_at) VALUES (?, 'Procurement', 'Active', ?)", (cat, now_iso()))

    if not run_query("SELECT COUNT(*) AS count FROM vendors", fetch=True)[0]["count"]:
        vendors = [
            ("ABC Diesel Supply", "Diesel/Fuel", "08030000001", "sales@abcdiesel.local", "Industrial Area", "GTBank", "0123456789", "TIN-001", 4, "Active", now_iso()),
            ("Prime Office Mart", "Office Supplies", "08030000002", "orders@primeoffice.local", "Main Market", "Access Bank", "9876543210", "TIN-002", 5, "Active", now_iso()),
            ("FixRight Maintenance", "Repairs/Maintenance", "08030000003", "support@fixright.local", "Workshop Road", "UBA", "2233445566", "TIN-003", 4, "Active", now_iso()),
        ]
        run_query("INSERT INTO vendors (name, category, phone, email, address, bank_name, account_no, tax_id, rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", vendors, many=True)

    if not run_query("SELECT COUNT(*) AS count FROM approval_rules", fetch=True)[0]["count"]:
        rules = [
            ("Diesel/Fuel", 250000, "Approver", 0, 1, 1, now_iso()),
            ("Construction Materials", 500000, "Approver", 1, 1, 1, now_iso()),
            ("Operational Purchases", 150000, "Approver", 0, 1, 1, now_iso()),
            ("Repairs/Maintenance", 250000, "Approver", 1, 1, 1, now_iso()),
            ("Other", 200000, "Approver", 0, 1, 1, now_iso()),
        ]
        run_query("INSERT INTO approval_rules (category, threshold_amount, approver_role, requires_sourcing, requires_finance, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rules, many=True)


# ---------------- Phase 3 hardening + separated invoices/receipts ----------------

def ensure_hardening_schema():
    """Non-destructive security and workflow hardening migrations."""
    add_column_if_missing("approval_delegations", "primary_user_id", "primary_user_id INTEGER")
    add_column_if_missing("approval_delegations", "delegate_user_id", "delegate_user_id INTEGER")
    add_column_if_missing("payments", "proof_path", "proof_path TEXT")
    add_column_if_missing("payments", "finance_note", "finance_note TEXT")
    add_column_if_missing("payments", "receipt_id", "receipt_id INTEGER")
    add_column_if_missing("invoices", "invoice_type", "invoice_type TEXT DEFAULT 'Supplier Invoice'")
    add_column_if_missing("invoices", "document_stage", "document_stage TEXT DEFAULT 'Invoice'")
    add_column_if_missing("invoices", "supplier_invoice_no", "supplier_invoice_no TEXT")
    add_column_if_missing("invoices", "due_date", "due_date TEXT")
    add_column_if_missing("invoices", "payment_terms", "payment_terms TEXT")
    add_column_if_missing("invoices", "billing_address", "billing_address TEXT")
    add_column_if_missing("invoices", "shipping_address", "shipping_address TEXT")
    add_column_if_missing("invoices", "subtotal", "subtotal REAL DEFAULT 0")
    add_column_if_missing("invoices", "discount_amount", "discount_amount REAL DEFAULT 0")
    add_column_if_missing("invoices", "balance_due", "balance_due REAL DEFAULT 0")
    add_column_if_missing("invoices", "linked_request_id", "linked_request_id INTEGER")
    add_column_if_missing("invoices", "approval_status", "approval_status TEXT DEFAULT 'Needs Review'")
    add_column_if_missing("expenses", "document_kind", "document_kind TEXT DEFAULT 'Expense'")
    add_column_if_missing("expenses", "receipt_id", "receipt_id INTEGER")
    try:
        run_query("CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe_recent ON notifications(user_id, role, title, entity_type, entity_id, message)")
    except Exception:
        pass
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_invoices_status_date ON invoices(status, invoice_date, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_po_vendor ON invoices(po_id, vendor_id, total_amount)",
        "CREATE INDEX IF NOT EXISTS idx_payments_status_method ON payments(status, payment_method, created_at)",
    ]:
        try:
            run_query(sql)
        except Exception:
            pass


def ensure_finance_document_schema():
    """Create receipt/invoice detail tables while preserving legacy expenses/invoices."""
    schemas = [
        """
        CREATE TABLE IF NOT EXISTS receipt_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_no TEXT,
            receipt_type TEXT DEFAULT 'Payment Receipt',
            payment_method TEXT NOT NULL,
            payment_date TEXT NOT NULL,
            vendor_id INTEGER,
            payer_name TEXT,
            payee_name TEXT,
            amount REAL NOT NULL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            currency TEXT DEFAULT 'NGN',
            purpose TEXT,
            department_project TEXT,
            linked_invoice_id INTEGER,
            linked_payment_id INTEGER,
            linked_po_id INTEGER,
            cash_received_by TEXT,
            cash_collected_from TEXT,
            cash_denominations TEXT,
            bank_name TEXT,
            account_name TEXT,
            account_number TEXT,
            transfer_reference TEXT,
            sender_bank TEXT,
            receiver_bank TEXT,
            card_type TEXT,
            masked_card_number TEXT,
            card_auth_code TEXT,
            pos_terminal_id TEXT,
            pos_merchant_id TEXT,
            pos_rrn TEXT,
            cheque_number TEXT,
            cheque_bank TEXT,
            cheque_due_date TEXT,
            mobile_wallet_provider TEXT,
            mobile_transaction_id TEXT,
            status TEXT DEFAULT 'Recorded',
            file_path TEXT,
            file_hash TEXT,
            ocr_text TEXT,
            ocr_json TEXT,
            duplicate_warning INTEGER DEFAULT 0,
            notes TEXT,
            uploaded_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            item_description TEXT NOT NULL,
            quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0,
            total REAL DEFAULT 0,
            category TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            item_description TEXT NOT NULL,
            quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            total REAL DEFAULT 0,
            category TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_ocr_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_type TEXT NOT NULL,
            entity_id INTEGER,
            file_hash TEXT,
            engine TEXT,
            success INTEGER DEFAULT 0,
            extracted_chars INTEGER DEFAULT 0,
            error_message TEXT,
            created_at TEXT NOT NULL
        )
        """,
    ]
    for schema in schemas:
        run_query(schema)
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_receipts_method_status ON receipt_records(payment_method, status, payment_date)",
        "CREATE INDEX IF NOT EXISTS idx_receipts_vendor_amount ON receipt_records(vendor_id, amount, payment_date)",
        "CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt ON receipt_items(receipt_id)",
        "CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(invoice_id)",
        "CREATE INDEX IF NOT EXISTS idx_ocr_attempts_doc ON document_ocr_attempts(document_type, entity_id, created_at)",
    ]:
        try:
            run_query(sql)
        except Exception:
            pass


def redact_dataframe(df: pd.DataFrame, table: str | None = None) -> pd.DataFrame:
    """Return a UI-safe copy of a database table with sensitive values hidden."""
    if df is None or df.empty:
        return df
    safe = df.copy()
    sensitive_exact = {
        "password_hash", "auth_key", "p256dh_key", "endpoint", "file_hash", "receipt_hash",
    }
    sensitive_contains = ["private_details", "message_text"]
    for col in list(safe.columns):
        if col in sensitive_exact or any(token in col.lower() for token in sensitive_contains):
            safe[col] = "[hidden]"
    if table == "collaboration_messages" and "message_text" in safe.columns:
        safe["message_text"] = "[private message hidden]"
    return safe


def active_delegation(primary_role: str = "Approver", delegate_role: str = "Procurement Manager", delegate_user_id: int | None = None):
    """Return active delegation; supports old role-based and new user-specific rows."""
    today = date.today().isoformat()
    params: list[Any] = [primary_role, delegate_role, today, today]
    extra = ""
    if delegate_user_id is not None and "delegate_user_id" in table_columns("approval_delegations"):
        extra = " AND (delegate_user_id IS NULL OR delegate_user_id=?)"
        params.append(int(delegate_user_id))
    rows = run_query(
        f"""
        SELECT * FROM approval_delegations
        WHERE enabled=1 AND primary_role=? AND delegate_role=?
          AND (start_date IS NULL OR start_date <= ?)
          AND (end_date IS NULL OR end_date >= ?)
          {extra}
        ORDER BY created_at DESC LIMIT 1
        """,
        params,
        fetch=True,
    )
    return dict(rows[0]) if rows else None


def _notification_category_allowed(pref: dict, title: str, entity_type: str | None) -> bool:
    title_l = (title or "").lower()
    entity_l = (entity_type or "").lower()
    if "gateway" in title_l or entity_l == "gateway pass":
        return bool(int(pref.get("gateway_pass_notifications", 1)))
    if any(x in title_l for x in ["approval", "approved", "rejected", "returned"]):
        return bool(int(pref.get("approval_notifications", 1)))
    if any(x in title_l for x in ["finance", "payment", "paid"]):
        return bool(int(pref.get("finance_notifications", 1)))
    if any(x in title_l for x in ["away", "delegation", "delegate"]):
        return bool(int(pref.get("delegation_notifications", 1)))
    return True


def _queue_notification_channel(notification_id: int | None, channel: str, user_id: int | None, role: str | None, status: str = "Queued", error: str | None = None):
    ensure_phase2_schema()
    run_query(
        """
        INSERT INTO notification_outbox (notification_id, channel, target_user_id, target_role, status, attempts, error_message, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (notification_id, channel, user_id, role, status, error, now_iso()),
    )



def _smtp_config() -> dict:
    """Read SMTP settings from environment variables.

    This keeps the app simple for local Streamlit while allowing real email in
    production without storing SMTP passwords in SQLite.
    Required: PROCUREFLOW_SMTP_HOST and PROCUREFLOW_SMTP_FROM.
    Optional: PROCUREFLOW_SMTP_PORT, PROCUREFLOW_SMTP_USERNAME,
    PROCUREFLOW_SMTP_PASSWORD, PROCUREFLOW_SMTP_USE_TLS.
    """
    return {
        "host": os.environ.get("PROCUREFLOW_SMTP_HOST", "").strip(),
        "port": int(os.environ.get("PROCUREFLOW_SMTP_PORT", "587") or 587),
        "username": os.environ.get("PROCUREFLOW_SMTP_USERNAME", "").strip(),
        "password": os.environ.get("PROCUREFLOW_SMTP_PASSWORD", ""),
        "from_email": os.environ.get("PROCUREFLOW_SMTP_FROM", "").strip(),
        "use_tls": os.environ.get("PROCUREFLOW_SMTP_USE_TLS", "1") != "0",
    }


def email_delivery_ready() -> tuple[bool, str]:
    cfg = _smtp_config()
    if not cfg["host"] or not cfg["from_email"]:
        return False, "SMTP is not configured. Set PROCUREFLOW_SMTP_HOST and PROCUREFLOW_SMTP_FROM."
    return True, "SMTP appears configured."


def _user_email(user_id: int | None) -> str | None:
    if user_id is None:
        return None
    try:
        rows = run_query("SELECT email FROM users WHERE id=?", (int(user_id),), fetch=True)
        if rows and rows[0]["email"]:
            return str(rows[0]["email"]).strip()
    except Exception:
        pass
    return None


def _send_email_now(recipient: str, subject: str, body: str) -> tuple[bool, str | None]:
    ready, reason = email_delivery_ready()
    if not ready:
        return False, reason
    if not recipient or "@" not in recipient:
        return False, "Recipient email address is missing or invalid."
    cfg = _smtp_config()
    try:
        msg = EmailMessage()
        msg["From"] = cfg["from_email"]
        msg["To"] = recipient
        msg["Subject"] = subject[:180] or "ProcureFlow notification"
        msg.set_content(body or subject or "ProcureFlow notification")
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            if cfg["use_tls"]:
                server.starttls()
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _queue_email_channel(notification_id: int | None, target_uid: int | None, role: str | None, title: str, message: str, enabled: bool):
    """Queue or send an email notification with clear status messages.

    If SMTP is configured and the target user has an email address, the message
    is sent immediately and the outbox row is marked Sent. Otherwise the outbox
    explains exactly why it could not leave the app.
    """
    recipient = _user_email(target_uid)
    subject = f"ProcureFlow: {title}" if title else "ProcureFlow notification"
    body = message or title or "You have a ProcureFlow notification."

    def _annotate_last_email_outbox():
        try:
            run_query(
                "UPDATE notification_outbox SET recipient_email=?, subject=?, body=? WHERE id=(SELECT MAX(id) FROM notification_outbox WHERE channel='email' AND COALESCE(target_user_id,-1)=COALESCE(?, -1))",
                (recipient, subject, body, target_uid),
            )
        except Exception:
            pass

    if not enabled:
        _queue_notification_channel(notification_id, "email", target_uid, None if target_uid is not None else role, "Skipped", "Email notifications disabled by user preference.")
        _annotate_last_email_outbox()
        return
    if not recipient:
        _queue_notification_channel(notification_id, "email", target_uid, None if target_uid is not None else role, "Needs Email Address", "User has no email address saved in their profile.")
        _annotate_last_email_outbox()
        return
    ready, reason = email_delivery_ready()
    if not ready:
        _queue_notification_channel(notification_id, "email", target_uid, None if target_uid is not None else role, "Queued - SMTP Missing", reason)
        _annotate_last_email_outbox()
        return
    sent, error = _send_email_now(recipient, subject, body)
    if sent:
        _queue_notification_channel(notification_id, "email", target_uid, None if target_uid is not None else role, "Sent", None)
        try:
            run_query("UPDATE notification_outbox SET recipient_email=?, subject=?, body=?, sent_at=? WHERE id=(SELECT MAX(id) FROM notification_outbox WHERE channel='email' AND target_user_id=?)", (recipient, subject, body, now_iso(), target_uid))
            if notification_id:
                run_query("UPDATE notifications SET email_sent=1 WHERE id=?", (notification_id,))
        except Exception:
            pass
    else:
        _queue_notification_channel(notification_id, "email", target_uid, None if target_uid is not None else role, "Failed", error)
        try:
            run_query("UPDATE notification_outbox SET recipient_email=?, subject=?, body=?, last_failure_at=? WHERE id=(SELECT MAX(id) FROM notification_outbox WHERE channel='email' AND target_user_id=?)", (recipient, subject, body, now_iso(), target_uid))
        except Exception:
            pass

def _recent_duplicate_notification(target_uid, role, title, message, entity_type, entity_id) -> bool:
    # Suppress same notification created repeatedly in short UI rerun windows.
    rows = run_query(
        """
        SELECT id FROM notifications
        WHERE COALESCE(user_id, -1)=COALESCE(?, -1)
          AND COALESCE(role, '')=COALESCE(?, '')
          AND title=? AND message=?
          AND COALESCE(entity_type, '')=COALESCE(?, '')
          AND COALESCE(entity_id, -1)=COALESCE(?, -1)
          AND datetime(created_at) >= datetime('now','-10 minutes')
        LIMIT 1
        """,
        (target_uid, role, title, message, entity_type, entity_id),
        fetch=True,
    )
    return bool(rows)


def create_notification(
    user_id: int | None = None,
    role: str | None = None,
    title: str = "",
    message: str = "",
    entity_type: str | None = None,
    entity_id: int | None = None,
    importance: str = "Normal",
    channels: list[str] | tuple[str, ...] | None = None,
    action_url: str | None = None,
    action_label: str | None = None,
):
    """Create preference-aware in-app notifications and queue external delivery.

    This corrects the previous loophole where in_app_enabled=0 was ignored and
    prevents repeat duplicates from Streamlit reruns.
    """
    ensure_phase2_schema()
    ensure_finance_document_schema()
    try:
        ensure_dashboard_upgrade_schema()
    except Exception:
        pass
    base_channels = list(channels or ["in_app"])
    target_user_ids: list[int | None]
    if user_id is not None:
        target_user_ids = [int(user_id)]
    elif role:
        rows = run_query("SELECT id FROM users WHERE role=? AND is_active=1", (role,), fetch=True)
        target_user_ids = [int(r["id"]) for r in rows] or [None]
    else:
        target_user_ids = [None]

    created_ids: list[int] = []
    for target_uid in target_user_ids:
        channels_local = list(base_channels)
        pref: dict = {}
        if target_uid is not None:
            rows = run_query("SELECT * FROM notification_preferences WHERE user_id=?", (target_uid,), fetch=True)
            if rows:
                pref = dict(rows[0])
            else:
                run_query("INSERT OR IGNORE INTO notification_preferences (user_id, created_at, updated_at) VALUES (?, ?, ?)", (target_uid, now_iso(), now_iso()))
                pref = dict(run_query("SELECT * FROM notification_preferences WHERE user_id=?", (target_uid,), fetch=True)[0])
        if pref and not _notification_category_allowed(pref, title, entity_type):
            continue
        if pref and int(pref.get("important_only") or 0) and "browser_push" in channels_local and importance not in ("High", "Critical", "Important"):
            channels_local = [c for c in channels_local if c != "browser_push"]
        if pref and not int(pref.get("in_app_enabled", 1)):
            channels_local = [c for c in channels_local if c != "in_app"]
        delivery_channel = ",".join(channels_local)
        notification_id = None
        if "in_app" in channels_local or "all" in channels_local or (not channels_local and not pref):
            role_target = None if target_uid is not None else role
            target_role_for_section = role_target
            if target_uid is not None:
                try:
                    urow = run_query("SELECT role FROM users WHERE id=?", (target_uid,), fetch=True)
                    target_role_for_section = urow[0]["role"] if urow else role_target
                except Exception:
                    target_role_for_section = role_target
            section_target = _infer_notification_section(target_role_for_section, title, entity_type, action_label)
            if not _recent_duplicate_notification(target_uid, role_target, title, message, entity_type, entity_id):
                notification_id = run_insert(
                    """
                    INSERT INTO notifications (user_id, role, title, message, entity_type, entity_id, is_read, popup_shown, importance, delivery_channel, push_sent, email_sent, action_url, action_label, section_target, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 0, 0, ?, ?, ?, ?)
                    """,
                    (target_uid, role_target, title, message, entity_type, entity_id, importance, delivery_channel or "in_app", action_url, action_label, section_target, now_iso()),
                )
                created_ids.append(notification_id)
        if "browser_push" in channels_local or "all" in channels_local:
            enabled = bool(pref and int(pref.get("browser_push_enabled") or 0))
            _queue_notification_channel(notification_id, "browser_push", target_uid, None if target_uid is not None else role, "Queued" if enabled else "Fallback", None if enabled else "Browser push not enabled or unsupported; kept as in-app/persistent fallback where available.")
        # Make the Email checkbox meaningful: when enabled, important/high
        # notifications are eligible for email even if the caller only asked
        # for in-app/browser channels. Normal/low updates stay inside the app
        # to prevent notification fatigue.
        if pref and int(pref.get("email_enabled") or 0) and importance in ("High", "Critical", "Important") and "email" not in channels_local and "all" not in channels_local:
            channels_local.append("email")
        if "email" in channels_local or "all" in channels_local:
            enabled = bool(pref and int(pref.get("email_enabled") or 0))
            _queue_email_channel(notification_id, target_uid, role, title, message, enabled)
        if importance in ("Critical", "High") and notification_id:
            log_audit("CRITICAL_NOTIFICATION_CREATED", "Notification", notification_id, {"title": title, "entity_type": entity_type, "entity_id": entity_id}, user_id=target_uid, role=role)
    return created_ids[0] if len(created_ids) == 1 else created_ids


def notify(user_id: int | None, role: str | None, title: str, message: str, entity_type: str | None = None, entity_id: int | None = None):
    return create_notification(user_id=user_id, role=role, title=title, message=message, entity_type=entity_type, entity_id=entity_id, importance="Normal", channels=["in_app"])


def transition_payment_status(payment_id: int, new_status: str, note: str | None = None, actor_user_id: int | None = None, actor_role: str | None = None, proof_path: str | None = None):
    rows = run_query("SELECT * FROM payments WHERE id=?", (payment_id,), fetch=True)
    if not rows:
        return
    old = dict(rows[0])
    bits = ["status=?", "updated_at=?"]
    params: list[Any] = [new_status, now_iso()]
    if new_status == "Approved":
        bits.append("approved_by=?"); params.append(actor_user_id)
    if new_status == "Paid":
        bits.extend(["paid_by=?", "payment_date=?"]); params.extend([actor_user_id, date.today().isoformat()])
    if proof_path:
        bits.append("proof_path=?"); params.append(proof_path)
    if note is not None:
        bits.append("finance_note=?"); params.append(note)
    params.append(payment_id)
    run_query(f"UPDATE payments SET {', '.join(bits)} WHERE id=?", params)
    add_workflow_event("Payment", payment_id, f"Payment {new_status}", new_status, note, actor_user_id)
    log_audit(f"PAYMENT_{new_status.upper().replace(' ','_')}", "Payment", payment_id, note, actor_user_id, actor_role, before_values={"status": old.get("status")}, after_values={"status": new_status})
    if old.get("po_id"):
        run_query("UPDATE purchase_orders SET payment_status=?, updated_at=? WHERE id=?", (new_status, now_iso(), old.get("po_id")))
        req = run_query("SELECT request_id FROM purchase_orders WHERE id=?", (old.get("po_id"),), fetch=True)
        if req and req[0]["request_id"] and new_status == "Paid":
            transition_request_status(int(req[0]["request_id"]), "Paid", "Payment Completed", note or "Payment completed by Finance.", actor_user_id, actor_role, payment_status="Paid")
    if old.get("invoice_id"):
        run_query("UPDATE invoices SET status=? WHERE id=?", ("Paid" if new_status == "Paid" else new_status, old.get("invoice_id")))


def transition_po_status(po_id: int, new_status: str, note: str | None = None, actor_user_id: int | None = None, actor_role: str | None = None):
    rows = run_query("SELECT * FROM purchase_orders WHERE id=?", (po_id,), fetch=True)
    if not rows:
        return
    old = dict(rows[0])
    run_query("UPDATE purchase_orders SET status=?, approved_by=CASE WHEN ?='Approved' THEN ? ELSE approved_by END, approved_by_role=CASE WHEN ?='Approved' THEN ? ELSE approved_by_role END, updated_at=? WHERE id=?", (new_status, new_status, actor_user_id, new_status, actor_role, now_iso(), po_id))
    add_workflow_event("Purchase Order", po_id, f"PO {new_status}", new_status, note, actor_user_id)
    log_audit(f"PO_{new_status.upper().replace(' ','_')}", "Purchase Order", po_id, note, actor_user_id, actor_role, before_values={"status": old.get("status")}, after_values={"status": new_status})


# ---------------- Phase 4 dashboard/session/receipt UX upgrades ----------------

def ensure_dashboard_upgrade_schema():
    """Safe migrations for persistent sessions, login/logout auditing, and UI/OCR metadata."""
    schemas = [
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            login_at TEXT NOT NULL,
            logout_at TEXT,
            last_seen_at TEXT,
            status TEXT DEFAULT 'Active',
            user_agent TEXT,
            ip_address TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS custom_dropdown_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_name TEXT NOT NULL,
            custom_value TEXT NOT NULL,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(field_name, custom_value)
        )
        """,
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
        """,
    ]
    for schema in schemas:
        run_query(schema)
    for table, cols in {
        "audit_logs": [
            ("event_date", "event_date TEXT"),
            ("event_time", "event_time TEXT"),
            ("session_id", "session_id INTEGER"),
        ],
        "notifications": [
            ("section_target", "section_target TEXT"),
            ("attention_counted", "attention_counted INTEGER DEFAULT 1"),
        ],
        "receipt_records": [
            ("detected_document_type", "detected_document_type TEXT"),
            ("ocr_detected_date", "ocr_detected_date TEXT"),
            ("interface_mode", "interface_mode TEXT"),
        ],
        "invoices": [
            ("detected_document_type", "detected_document_type TEXT"),
            ("ocr_detected_date", "ocr_detected_date TEXT"),
            ("interface_mode", "interface_mode TEXT"),
        ],
    }.items():
        for column, ddl in cols:
            try:
                add_column_if_missing(table, column, ddl)
            except Exception:
                pass
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_status ON user_sessions(user_id, status, login_at, logout_at)",
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions(session_token)",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_action_date ON audit_logs(action, event_date, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_section_unread ON notifications(user_id, role, section_target, is_read, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_channel ON notification_outbox(channel, status, target_user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_section_attention_reads_user_section ON section_attention_reads(user_id, role, section, last_seen_at)",
    ]:
        try:
            run_query(sql)
        except Exception:
            pass
    try:
        run_query("UPDATE audit_logs SET event_date=substr(created_at,1,10), event_time=substr(created_at,12,8) WHERE event_date IS NULL OR event_time IS NULL")
    except Exception:
        pass


# ---------------- Command-chain workflow hardening ----------------

def ensure_command_chain_schema():
    """Apply non-destructive migrations for the corrected command chain.

    The legacy database uses the role value "Facility Manager" internally.
    The application now displays that role as "Utility Head / Facility Head"
    while keeping the existing DB rows compatible.
    """
    from core.permissions import safe_role_permissions

    # Additional workflow/routing columns.
    for table, cols in {
        "purchase_requests": [
            ("next_role", "next_role TEXT"),
            ("request_type", "request_type TEXT DEFAULT 'Procurement'"),
            ("submitted_at", "submitted_at TEXT"),
            ("approved_at", "approved_at TEXT"),
            ("approved_by_user_id", "approved_by_user_id INTEGER"),
            ("approved_by_role", "approved_by_role TEXT"),
            ("paid_at", "paid_at TEXT"),
            ("receipt_uploaded_at", "receipt_uploaded_at TEXT"),
            ("completed_at", "completed_at TEXT"),
            ("generated_at", "generated_at TEXT"),
        ],
        "gateway_passes": [
            ("next_role", "next_role TEXT"),
            ("reviewed_by_user_id", "reviewed_by_user_id INTEGER"),
            ("reviewed_at", "reviewed_at TEXT"),
            ("procurement_review_note", "procurement_review_note TEXT"),
            ("completed_at", "completed_at TEXT"),
        ],
        "payments": [
            ("next_role", "next_role TEXT"),
            ("submitted_for_verification_at", "submitted_for_verification_at TEXT"),
        ],
        "audit_logs": [
            ("amount", "amount REAL"),
            ("department", "department TEXT"),
            ("project", "project TEXT"),
            ("notes", "notes TEXT"),
        ],
    }.items():
        for column, ddl in cols:
            try:
                add_column_if_missing(table, column, ddl)
            except Exception:
                pass

    # Income/budget allocation table.
    run_query(
        """
        CREATE TABLE IF NOT EXISTS income_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_no TEXT UNIQUE,
            entry_date TEXT NOT NULL,
            month_key TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            department TEXT DEFAULT 'General',
            project TEXT DEFAULT 'General',
            source TEXT DEFAULT 'Opening income / budget allocation',
            entry_type TEXT DEFAULT 'Opening income / budget allocation',
            amount REAL NOT NULL DEFAULT 0,
            notes TEXT,
            status TEXT DEFAULT 'Active',
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )

    # Workflow indexes for fast queues/KPIs.
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_pr_next_role_status ON purchase_requests(next_role, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_pr_status_created ON purchase_requests(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_pr_department_project ON purchase_requests(department_project, category)",
        "CREATE INDEX IF NOT EXISTS idx_pr_request_type ON purchase_requests(request_type, status)",
        "CREATE INDEX IF NOT EXISTS idx_gp_next_role_status ON gateway_passes(next_role, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_pay_next_role_status ON payments(next_role, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_income_month_dept_project ON income_entries(year, month, department, project, status)",
        "CREATE INDEX IF NOT EXISTS idx_audit_department_project ON audit_logs(department, project, created_at)",
    ]:
        try:
            run_query(sql)
        except Exception:
            pass

    # Backfill next_role for common legacy statuses.
    try:
        run_query("UPDATE purchase_requests SET next_role='procurement_manager' WHERE status IN ('Submitted','Submitted to Procurement Manager','Sent for Procurement Review') AND (next_role IS NULL OR next_role='')")
        run_query("UPDATE purchase_requests SET next_role='approver' WHERE status IN ('Pending Approval','Pending Approver/MD Approval','Submitted for Approval') AND (next_role IS NULL OR next_role='')")
        run_query("UPDATE purchase_requests SET next_role='finance', payment_status=COALESCE(NULLIF(payment_status,''),'Approved for Payment') WHERE status IN ('Approved','Approved for Payment','Awaiting Payment') AND (next_role IS NULL OR next_role='')")
        run_query("UPDATE purchase_requests SET next_role='auditor' WHERE status IN ('Paid','Completed','Closed') AND (next_role IS NULL OR next_role='')")
        run_query("UPDATE gateway_passes SET next_role='procurement_manager' WHERE status IN ('Submitted','Pending Procurement Manager / Approver Review','Sent for Procurement Review') AND (next_role IS NULL OR next_role='')")
        run_query("UPDATE gateway_passes SET next_role='approver' WHERE status IN ('Submitted for Approval') AND (next_role IS NULL OR next_role='')")
    except Exception:
        pass

    # Correct role descriptions and DB permissions without deleting users.
    run_query("INSERT OR IGNORE INTO roles (name, description, created_at) VALUES ('Facility Manager', 'Utility Head / Facility Head role for drafts, gateway passes and facility/utility handoff', ?)", (now_iso(),))
    run_query("UPDATE roles SET description='Utility Head / Facility Head role for drafts, gateway passes and facility/utility handoff' WHERE name='Facility Manager'")

    # Remove unsafe permissions and rebuild baseline role permissions.
    for role in ["Admin", "Procurement Manager", "Facility Manager", "Finance", "Approver", "Auditor"]:
        allowed = safe_role_permissions(role)
        try:
            run_query("DELETE FROM role_permissions WHERE role_name=?", (role,))
            for perm in allowed:
                run_query("INSERT OR IGNORE INTO permissions (name, description, created_at) VALUES (?, ?, ?)", (perm, perm.replace('_', ' ').title(), now_iso()))
                run_query("INSERT OR IGNORE INTO role_permissions (role_name, permission_name, created_at) VALUES (?, ?, ?)", (role, perm, now_iso()))
        except Exception:
            pass

    # No Procurement Manager fallback approval. Delegation may notify Admin but must not make PM an approver.
    try:
        run_query("UPDATE approval_rules SET backup_approver_role=NULL, pm_fallback_enabled=0 WHERE 1=1")
    except Exception:
        pass

    # Seed a starter monthly allocation if no income exists so the tab is useful on first run.
    try:
        count = run_query("SELECT COUNT(*) AS c FROM income_entries", fetch=True)[0]["c"]
        if not count:
            today = date.today()
            run_query(
                "INSERT INTO income_entries (entry_no, entry_date, month_key, year, month, department, project, source, entry_type, amount, notes, status, created_at) VALUES (?, ?, ?, ?, ?, 'General', 'General', 'Opening allocation', 'Opening income / budget allocation', 0, 'Starter row; edit or add real allocation in Income tab.', 'Active', ?)",
                (make_ref('INC'), today.isoformat(), today.strftime('%Y-%m'), today.year, today.month, now_iso()),
            )
    except Exception:
        pass
