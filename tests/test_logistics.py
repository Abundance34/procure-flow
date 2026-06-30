from core.db import init_db, df_query, table_columns
from core.permissions import safe_role_permissions
from core.workflow import po_next_role_for_status


def test_logistics_role_is_fulfilment_only():
    permissions = safe_role_permissions("Logistics Officer")
    assert "receive_goods" in permissions
    assert "update_delivery_tracking" in permissions
    assert "record_delivery_exception" in permissions
    assert "coordinate_gateway_pass" in permissions
    assert "create_sourcing" not in permissions
    assert "create_po" not in permissions
    assert "approve_request" not in permissions
    assert "approve_po" not in permissions


def test_procurement_keeps_commercial_release_but_not_receiving():
    permissions = safe_role_permissions("Procurement Manager")
    assert "release_po_to_logistics" in permissions
    assert "receive_goods" not in permissions


def test_po_fulfilment_routing():
    assert po_next_role_for_status("Approved") == "procurement_manager"
    assert po_next_role_for_status("Released to Logistics") == "logistics_officer"
    assert po_next_role_for_status("In Transit") == "logistics_officer"
    assert po_next_role_for_status("Fully Received") == "finance"
    assert po_next_role_for_status("Disputed") == "procurement_manager"


def test_logistics_schema_migrates():
    init_db()
    roles = df_query("SELECT name FROM roles WHERE name='Logistics Officer'")
    assert not roles.empty
    assert {"next_role", "released_to_logistics_at", "logistics_status", "waybill_number"}.issubset(table_columns("purchase_orders"))
    tables = df_query("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('logistics_exceptions','logistics_documents')")
    assert set(tables["name"]) == {"logistics_exceptions", "logistics_documents"}
