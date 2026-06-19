from core.workflow import (
    workflow_next_role,
    gateway_next_role_for_status,
    canonical_status,
    STATUS_SENT_REVIEW,
    STATUS_SUBMITTED_APPROVAL,
    STATUS_APPROVED,
    STATUS_COMPLETED,
    STATUS_CLOSED,
    STATUS_ARCHIVED,
)


def test_canonical_status_aliases():
    assert canonical_status("Submitted to Procurement Manager") == STATUS_SENT_REVIEW
    assert canonical_status("Pending Approver/MD Approval") == STATUS_SUBMITTED_APPROVAL


def test_next_role_routing():
    assert workflow_next_role(STATUS_SENT_REVIEW) == "procurement_manager"
    assert workflow_next_role(STATUS_SUBMITTED_APPROVAL) == "approver"
    assert workflow_next_role(STATUS_APPROVED) == "finance"
    assert canonical_status("Requires Sourcing") == "Requires Sourcing"
    assert workflow_next_role("Requires Sourcing") == "procurement_manager"
    assert canonical_status("Vendor Quote Collection") == "Vendor Quote Collection"
    assert workflow_next_role("Vendor Quote Collection") == "procurement_manager"
    assert workflow_next_role("Vendor Recommendation") == "procurement_manager"
    assert workflow_next_role("Paid") == "procurement_manager"
    assert workflow_next_role(STATUS_COMPLETED) == "procurement_manager"
    assert workflow_next_role(STATUS_CLOSED) == "auditor"
    assert workflow_next_role(STATUS_ARCHIVED) == "auditor"


def test_gateway_routing():
    assert gateway_next_role_for_status(STATUS_SENT_REVIEW) == "procurement_manager"
    assert gateway_next_role_for_status(STATUS_SUBMITTED_APPROVAL) == "approver"
    assert gateway_next_role_for_status(STATUS_APPROVED) == "facility_manager"
    assert gateway_next_role_for_status("Generated") is None
