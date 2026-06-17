from core.workflow import workflow_next_role, canonical_status, STATUS_SENT_REVIEW, STATUS_SUBMITTED_APPROVAL, STATUS_APPROVED, STATUS_COMPLETED


def test_canonical_status_aliases():
    assert canonical_status("Submitted to Procurement Manager") == STATUS_SENT_REVIEW
    assert canonical_status("Pending Approver/MD Approval") == STATUS_SUBMITTED_APPROVAL


def test_next_role_routing():
    assert workflow_next_role(STATUS_SENT_REVIEW) == "procurement_manager"
    assert workflow_next_role(STATUS_SUBMITTED_APPROVAL) == "approver"
    assert workflow_next_role(STATUS_APPROVED) == "finance"
    assert workflow_next_role(STATUS_COMPLETED) == "auditor"
