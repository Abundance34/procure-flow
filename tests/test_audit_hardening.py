import sqlite3
from uuid import uuid4

import pytest

from core.db import (
    df_query,
    get_conn,
    init_db,
    now_iso,
    run_insert,
    run_query,
    verify_audit_chain,
)
from services.payee_service import PayeeValidationError, get_masked_payee_for_request, save_payee_details


def _user():
    return df_query("SELECT id, role FROM users ORDER BY id LIMIT 1").iloc[0]


def _draft_request(reference: str) -> int:
    actor = _user()
    reference = f"{reference}-{uuid4().hex[:8]}"
    return run_insert(
        """
        INSERT INTO purchase_requests
        (request_no, requested_by, department_project, request_date, category, justification, estimated_amount, status, created_at, updated_at)
        VALUES (?, ?, 'Audit Test', ?, 'Testing', 'Audit evidence test', 1000, 'Draft', ?, ?)
        """,
        (reference, int(actor.id), now_iso()[:10], now_iso(), now_iso()),
    )


def test_audit_hardening_schema_and_append_only_protection():
    init_db()
    tables = df_query("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('audit_events','audit_chain_verifications','payment_payee_details','payment_payee_detail_versions','password_history')")
    assert set(tables['name']) == {'audit_events','audit_chain_verifications','payment_payee_details','payment_payee_detail_versions','password_history'}
    request_id = _draft_request('TEST-AUDIT-HARDENING-IMMUTABLE')
    row = df_query("SELECT id FROM audit_events WHERE entity_reference LIKE 'TEST-AUDIT-HARDENING-IMMUTABLE-%' ORDER BY id DESC LIMIT 1")
    assert not row.empty
    with pytest.raises(sqlite3.DatabaseError):
        run_query("UPDATE audit_events SET action='TAMPERED' WHERE id=?", (int(row.iloc[0]['id']),))
    assert verify_audit_chain(record_result=False)['valid'] is True
    run_query("DELETE FROM purchase_requests WHERE id=?", (request_id,))


def test_payee_details_are_encrypted_masked_and_audited():
    init_db()
    actor = _user()
    request_id = _draft_request('TEST-PAYEE-HARDENING-001')
    result = save_payee_details(
        request_id,
        {
            'recipient_known': True,
            'payee_type': 'Vendor',
            'payee_name': 'Sample Supplier Limited',
            'account_name': 'Sample Supplier Limited',
            'bank_name': 'FirstBank',
            'account_number': '1234567890',
            'currency': 'NGN',
            'payment_reference': 'Test payment',
            'confirmation': True,
        },
        int(actor.id),
        str(actor.role),
    )
    assert result.verification_status == 'Requester Confirmed'
    encrypted = df_query("SELECT account_number_encrypted, account_number_last4 FROM payment_payee_details WHERE id=?", (result.payee_detail_id,)).iloc[0]
    assert encrypted.account_number_encrypted != '1234567890'
    assert encrypted.account_number_last4 == '7890'
    masked = get_masked_payee_for_request(request_id)
    assert masked and masked['account_number'] == '******7890'
    events = df_query("SELECT before_values_redacted_json, after_values_redacted_json FROM audit_events WHERE entity_type='Payment Payee Details' AND entity_id=?", (str(result.payee_detail_id),))
    assert not events.empty
    assert all('1234567890' not in str(value) for value in events.astype(str).to_numpy().flatten())
    assert verify_audit_chain(record_result=False)['valid'] is True
    run_query("DELETE FROM payment_payee_details WHERE id=?", (result.payee_detail_id,))
    run_query("DELETE FROM purchase_requests WHERE id=?", (request_id,))


def test_ngn_payee_account_requires_ten_digits():
    with pytest.raises(PayeeValidationError):
        save_payee_details(
            999999,
            {
                'recipient_known': True,
                'payee_type': 'Vendor',
                'payee_name': 'Invalid',
                'account_name': 'Invalid',
                'bank_name': 'Bank',
                'account_number': '12345',
                'currency': 'NGN',
                'confirmation': True,
            },
            1,
            'Procurement Manager',
        )


def test_ledger_pagination_and_redacted_event_views():
    from repositories.audit_repository import ledger_count, ledger_page

    init_db()
    first = _draft_request('TEST-LEDGER-PAGE-A')
    second = _draft_request('TEST-LEDGER-PAGE-B')
    total = ledger_count("entity_type='Purchase Request' AND action='DATABASE_INSERT'")
    assert total >= 2
    first_page = ledger_page("entity_type='Purchase Request' AND action='DATABASE_INSERT'", (), limit=1, offset=0)
    second_page = ledger_page("entity_type='Purchase Request' AND action='DATABASE_INSERT'", (), limit=1, offset=1)
    assert len(first_page) == 1 and len(second_page) == 1
    assert int(first_page.iloc[0]['id']) != int(second_page.iloc[0]['id'])
    assert 'canonical_payload_json' in first_page.columns
    run_query("DELETE FROM purchase_requests WHERE id IN (?, ?)", (first, second))


def test_pending_payee_blocks_payment_and_finance_verifies_known_details():
    from services.payee_service import (
        PaymentPayeeNotReadyError,
        assert_request_payee_payment_ready,
    )

    init_db()
    actor = _user()
    request_id = _draft_request('TEST-PAYEE-READINESS')
    save_payee_details(
        request_id,
        {
            'recipient_known': False,
            'delayed_reason': 'Vendor account will be confirmed after sourcing.',
            'currency': 'NGN',
        },
        int(actor.id),
        'Procurement Manager',
    )
    with pytest.raises(PaymentPayeeNotReadyError):
        assert_request_payee_payment_ready(request_id, int(actor.id), 'Finance')
    save_payee_details(
        request_id,
        {
            'recipient_known': True,
            'payee_type': 'Vendor',
            'payee_name': 'Ready Supplier Limited',
            'account_name': 'Ready Supplier Limited',
            'bank_name': 'Example Bank',
            'account_number': '1111222233',
            'currency': 'NGN',
            'confirmation': True,
        },
        int(actor.id),
        'Procurement Manager',
    )
    assert_request_payee_payment_ready(request_id, int(actor.id), 'Finance')
    status = df_query("SELECT verification_status, payment_readiness_status FROM payment_payee_details WHERE purchase_request_id=?", (request_id,)).iloc[0]
    assert status.verification_status == 'Finance Verified'
    assert status.payment_readiness_status == 'Payment Ready'
    events = df_query("SELECT action, reason_or_comment FROM audit_events WHERE parent_entity_id=? ORDER BY id", (request_id,))
    assert 'PAYEE_DETAILS_PAYMENT_BLOCKED' in events['action'].tolist()
    assert 'PAYEE_DETAILS_FINANCE_VERIFIED' in events['action'].tolist()
    assert '1111222233' not in events.astype(str).to_string()
    run_query("DELETE FROM payment_payee_details WHERE purchase_request_id=?", (request_id,))
    run_query("DELETE FROM purchase_requests WHERE id=?", (request_id,))
