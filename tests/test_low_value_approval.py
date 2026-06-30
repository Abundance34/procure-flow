from core.db import df_query, init_db, table_columns
from core.permissions import can_approve, can_approve_low_value, safe_role_permissions
from core.workflow import (
    PROCUREMENT_MANAGER_APPROVAL_THRESHOLD,
    STATUS_SUBMITTED_APPROVAL,
    is_low_value_approval,
    request_routing_for_status,
    required_approval_role_for_amount,
)


def test_threshold_boundary_routes_to_procurement_manager():
    assert PROCUREMENT_MANAGER_APPROVAL_THRESHOLD == 100_000.0
    assert is_low_value_approval(99_999.99)
    assert is_low_value_approval(100_000.00)
    assert not is_low_value_approval(100_000.01)
    assert required_approval_role_for_amount(100_000.00) == "procurement_manager"
    assert required_approval_role_for_amount(100_000.01) == "approver"


def test_submitted_request_routing_uses_amount_threshold():
    assert request_routing_for_status(STATUS_SUBMITTED_APPROVAL, 100_000.00).next_role == "procurement_manager"
    assert request_routing_for_status(STATUS_SUBMITTED_APPROVAL, 100_000.01).next_role == "approver"


def test_procurement_manager_has_scoped_low_value_authority_only():
    assert can_approve_low_value("Procurement Manager")
    assert "approve_low_value" in safe_role_permissions("Procurement Manager")
    assert not can_approve("Procurement Manager")
    assert "approve_request" not in safe_role_permissions("Procurement Manager")


def test_threshold_schema_and_logistics_demo_account_exist():
    init_db()
    assert "approval_mode" in table_columns("purchase_requests")
    assert "approval_mode" in table_columns("purchase_orders")
    assert "approval_mode" in table_columns("payments")
    logistics = df_query("SELECT username, role FROM users WHERE username='logistics'")
    assert not logistics.empty
    assert logistics.iloc[0]["role"] == "Logistics Officer"
