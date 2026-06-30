# Auditor Evidence Ledger, Hardening & Payment Payee Update Notes

## Scope applied

This release changes only:

1. Auditor workspace and read-only audit/evidence views.
2. Backend security hardening, data protection, audit integrity, document handling, and deployment readiness.
3. Purchase-request drafting/editing for Procurement Manager and Utility/Facility Head.

No Admin, Approver/MD, Finance, Logistics Officer, Security, or other role navigation/menu/workflow buttons were added, removed, reordered, or redesigned.

## Delivered

- New append-only `audit_events` evidence ledger with redacted before/after data, correlation IDs, hash chain, signature, and integrity checker.
- Transactional database-trigger audit backstop for key workflow/finance/logistics/gateway tables.
- Auditor-only pages for full evidence filtering, sourcing/quotes, PO/logistics, receiving/returns, finance/payment, payee access audit, gateway pass, notifications, user/security, documents, budgets, vendors, and Facility/Utility handoffs.
- Controlled Auditor sensitive-payee reveal with mandatory reason, five-minute session limit, and immutable evidence event.
- Secure encrypted `payment_payee_details` and immutable version history.
- Payment payee fields on Procurement Manager and Utility/Facility Head request draft forms only.
- NGN account-number validation, masking, duplicate fingerprint warning, delayed-payee workflow guard, and Finance verification during the existing authorized payment action.
- Argon2id new-password support, legacy rehash-on-login, login failure/lockout logging, password-history policy, and URL session-token removal.
- Upload extension/header validation, size limits, safe ZIP checks, optional ClamAV scanning, file SHA-256 evidence, and audited upload blocks.
- Environment placeholders, migration notes, audit worker, repository helpers, tests, and documentation.

## Validation

```text
Python compile check: passed
Automated tests: 23 passed
```

## Important production items

- Set `PROCUREFLOW_PAYEE_ENCRYPTION_KEY` and `PROCUREFLOW_AUDIT_SIGNING_KEY` via a secret manager before enabling production mode.
- Use HTTPS with an approved reverse proxy for Secure/HttpOnly/SameSite cookie sessions.
- Move from SQLite to PostgreSQL for higher-concurrency deployment.
- Schedule `python -m workers.audit_chain_worker` and keep encrypted backups with restore tests.
