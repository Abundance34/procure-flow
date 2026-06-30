"""Persistence helpers for secure payment payee details."""
from __future__ import annotations

from core.db import df_query, run_query


def by_request(request_id: int):
    return df_query(
        "SELECT * FROM payment_payee_details WHERE purchase_request_id=? ORDER BY id DESC LIMIT 1",
        (int(request_id),),
    )


def history(payee_id: int):
    return df_query(
        "SELECT * FROM payment_payee_detail_versions WHERE payee_detail_id=? ORDER BY version_no DESC, created_at DESC",
        (int(payee_id),),
    )


def masked_audit_view():
    return df_query(
        """
        SELECT ppd.*, pr.request_no, po.po_no, v.name AS vendor_name,
               creator.full_name AS created_by_name, verifier.full_name AS verified_by_name
        FROM payment_payee_details ppd
        LEFT JOIN purchase_requests pr ON pr.id=ppd.purchase_request_id
        LEFT JOIN purchase_orders po ON po.id=ppd.purchase_order_id
        LEFT JOIN vendors v ON v.id=ppd.vendor_id
        LEFT JOIN users creator ON creator.id=ppd.created_by_user_id
        LEFT JOIN users verifier ON verifier.id=ppd.verified_by_user_id
        ORDER BY ppd.updated_at DESC, ppd.created_at DESC
        """
    )
