# 2026-06-30 Audit hardening and encrypted payee details

This release is migration-safe for the existing SQLite database. Run:

```bash
python migrate_existing_db.py
```

`core.db.init_db()` performs the non-destructive migration and creates:

- `audit_events` and `audit_chain_verifications`
- append-only update/delete protection triggers for `audit_events`
- evidence triggers for key workflow tables
- `payment_payee_details` and `payment_payee_detail_versions`
- `password_history`
- supporting indexes

Production deployments must set `PROCUREFLOW_PAYEE_ENCRYPTION_KEY` and
`PROCUREFLOW_AUDIT_SIGNING_KEY` before invoking the migration.
