from core.permissions import can_approve, can_approve_gateway_pass, can_create_payment_request, can_delete_draft, display_role, safe_role_permissions


def test_only_admin_and_approver_can_approve():
    assert can_approve("Admin")
    assert can_approve("Approver")
    for role in ["Finance", "Procurement Manager", "Facility Manager", "Auditor", None]:
        assert not can_approve(role)


def test_finance_cannot_create_payment_requests():
    assert not can_create_payment_request("Finance")
    assert can_create_payment_request("Admin")
    assert can_create_payment_request("Approver")


def test_procurement_manager_reviews_gateway_but_cannot_approve():
    perms = safe_role_permissions("Procurement Manager")
    assert "approve_request" not in perms
    assert "approve_payment" not in perms
    assert "approve_gateway_pass" not in perms
    assert "review_gateway_pass" in perms
    assert "submit_for_approval" in perms
    assert not can_approve("Procurement Manager")
    assert not can_approve_gateway_pass("Procurement Manager")


def test_utility_visible_label():
    assert display_role("Facility Manager") == "Utility Head / Facility Head"


def test_delete_draft_rule():
    assert can_delete_draft("Facility Manager", 7, 7, "Draft")
    assert not can_delete_draft("Facility Manager", 7, 8, "Draft")
    assert not can_delete_draft("Facility Manager", 7, 7, "Sent for Procurement Review")
    assert not can_delete_draft("Finance", 7, 7, "Draft")
