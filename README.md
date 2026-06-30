# ProcureFlow Command-Chain Workspace

ProcureFlow is a Streamlit-based internal procurement, finance, audit, reporting, income, and gateway-pass management application. This build hardens the workflow so every request follows the approved chain of command:

**Utility Head / Facility Head -> Procurement Manager -> value-based approval -> Finance -> Procurement Closure -> History/Audit**

The approval rule in this build is **₦100,000 inclusive**:

- **₦100,000 and below:** Procurement Manager approves the request, PO, or standalone payment request.
- **Above ₦100,000:** Approver / MD approves.
- A **Procurement Manager-created request** always goes to Approver / MD for independent approval, even below ₦100,000, so a requester cannot approve their own request.
- Finance can only act after the authorized approval is complete.

---

## Demo Login Credentials

| Role | Username | Password |
|---|---:|---:|
| Admin | `admin` | `admin123` |
| Procurement Manager | `procurement` | `procure123` |
| Utility Head / Facility Head | `facility` | `facility123` |
| Logistics Officer | `logistics` | `logistics123` |
| Finance | `finance` | `finance123` |
| Approver / MD | `approver` | `approve123` |
| Auditor | `auditor` | `audit123` |

> For production, set `PROCUREFLOW_PRODUCTION=1`, create real users, and rotate all demo passwords.

---

## What Changed in This Build

### Workflow and Permissions

- Centralized role rules in `core/permissions.py`.
- Added workflow constants/routing helpers in `core/workflow.py`.
- Enforced the command chain:
  - Utility Head / Facility Head creates drafts and sends them for Procurement Manager review.
  - Procurement Manager reviews, sources, returns for correction, and approves eligible transactions at or below ₦100,000.
  - Approver / MD approves transactions above ₦100,000 and independently approves Procurement Manager-created requests.
  - Finance only sees authorized approved items ready for payment.
  - Auditor is read-only and sees histories, audit logs, reports, and completed records.
- Procurement Manager receives a scoped `approve_low_value` authority; this does **not** grant general approval rights or gateway-pass final approval.
- The Approver / MD receives notification and permanent approval-history/audit records for every Procurement Manager low-value approval.
- Removed unsafe approval rights from Finance.
- Draft deletion is restricted to own drafts before submission, except Admin audited override.

### Automatic Routing

The app now stores and uses `next_role` routing columns. Manual Admin linking is no longer required for Utility Head / Facility Head items to reach Procurement Manager queues.

| Action | Status | next_role |
|---|---|---|
| Utility Head sends draft | Sent for Procurement Review | `procurement_manager` |
| Procurement submits a Facility/Utility item at or below ₦100,000 | Submitted for Approval | `procurement_manager` |
| Procurement submits a Facility/Utility item above ₦100,000 | Submitted for Approval | `approver` |
| Procurement Manager submits own request | Submitted for Approval | `approver` |
| Authorized PM/Approver approves | Approved / Awaiting Payment | `finance` |
| Finance pays/uploads receipt | Paid / Receipt Uploaded | `procurement_manager` |
| Procurement Manager completes/closes | Completed / Closed | `procurement_manager` then `auditor` |
| Record archived | Archived | `auditor` |

### Dashboard KPIs

Dashboard counters now separate:

- **Queue KPIs:** Pending Review, Pending Approval, Awaiting Payment, Pending Receipt.
- **Cumulative KPIs:** Total Submitted, Total Approved, Total Rejected, Total Paid, Total Completed.

Queue KPIs decrease when work moves forward; cumulative KPIs remain in history and reports.

### Income Tab

A new Income tab calculates available funds using:

```text
Remaining Balance = Total Income or Budget Allocation - Paid Expenses - Approved Unpaid Commitments
```

Filters include month, year, department, project, and status. Admin and Finance can create income entries; other roles view where appropriate.

### Excel Reports

All former CSV download helpers now produce `.xlsx` Excel files. The reporting service creates multi-sheet workbooks with sheets such as:

- Summary
- Detailed Records
- Department Breakdown
- Vendor Breakdown
- Monthly Breakdown
- Payment History
- Receipt Index
- Approval History
- Audit Logs

Auditor, Admin, Approver, Procurement Manager, Finance, and Utility Head / Facility Head download Excel files rather than CSV files.

### Gateway Pass Module

- Gateway pass is kept in the sidebar workflow, not as a distracting dashboard card.
- Gateway Pass department dropdown is restricted to two separate choices: `CMOTD` and `RACAM`.
- Visible labels now use **Utility Head / Facility Head**.
- Generated PDF includes reference number, date, department, movement type, origin, destination, movement date, item details, purpose, transport details, authorization, and security verification.
- Signature/date/security underlines are generated in aligned tables for consistent spacing.
- Return Date is active during gateway-pass draft creation and can be stored before submission.
- After PDF generation, the Facility/Utility workspace safely redirects to History so the generated pass remains visible for preview/download instead of leaving an empty queue.
- Generated gateway passes leave the ready-to-generate queue but remain in History and reports.

### Line Items and Other Fields

- Dynamic line items use unique Streamlit keys and do not overwrite prior rows.
- The `+ Add item` behavior supports two or more rows.
- Dropdowns with `Other` reveal a text input and save the typed value into records, review views, generated documents, exports, and audit trails where those fields are used.

### Audit Logs

Important actions are logged, including draft creation/edit/delete, sent for review, reviewed, returned, submitted for approval, approved/rejected, sent to finance, paid, receipt uploaded, completed, gateway pass generated, Excel report download, income entry creation, and login/logout where available.

---


### Architecture Correction in This Build

This version removes the most dangerous command-chain inconsistency: workflow routing is no longer defined separately by different screens. `core/workflow.py` is now the authoritative status-to-next-role map for purchase requests and gateway passes. `core/db.py` applies that map during status transitions, and `services/request_service.py`, `services/gateway_service.py`, `services/finance_service.py`, and `services/notification_service.py` now provide UI-free command functions for future refactoring.

The large `modules/role_workspaces.py` file remains for Streamlit compatibility, but its final workflow helper delegates to `core.workflow` rather than maintaining an independent routing table.

## Project Structure

```text
app.py
core/
  auth.py
  config.py
  db.py
  db_schema.py
  db_queries.py
  db_migrations.py
  models.py
  permissions.py
  workflow.py
  report_service.py
  ui.py
  ocr.py
  legacy_import.py
modules/
  role_workspaces.py
services/
  audit_service.py
  budget_service.py
  document_service.py
  finance_service.py
  gateway_service.py
  notification_service.py
  report_service.py
  request_service.py
tests/
  test_permissions.py
  test_workflow.py
  test_reports.py
  test_db_migration.py
migrate_existing_db.py
Dockerfile
docker-compose.yml
.env.example
```

The original modules remain in place for compatibility, while command-chain safety overrides are centralized and applied at runtime.

---

## Installation

### Windows

1. Extract the project to a short path, for example:

```powershell
C:\ProcureFlow
```

2. Open PowerShell or Command Prompt inside the project folder.
3. Create and activate a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

4. Install dependencies:

```powershell
pip install -r requirements.txt
```

5. Run migrations:

```powershell
python migrate_existing_db.py
```

6. Start the app:

```powershell
streamlit run app.py
```

You may also use the included `INSTALL_WINDOWS.bat` and `RUN_APP.bat` helper files.

### Linux / Server

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python migrate_existing_db.py
streamlit run app.py --server.address=0.0.0.0 --server.port=8501
```

### Docker

```bash
cp .env.example .env
docker compose up --build
```

Then open the URL printed by Streamlit, usually `http://localhost:8501`.

---

## Configuration

Copy `.env.example` to `.env` and configure values as needed.

Important variables:

```text
PROCUREFLOW_PRODUCTION=1
PROCUREFLOW_SESSION_TIMEOUT_MINUTES=60
PROCUREFLOW_DATA_DIR=data
PROCUREFLOW_UPLOAD_DIR=data/uploads
PROCUREFLOW_BACKUP_DIR=data/backups
PROCUREFLOW_MAX_UPLOAD_MB=15
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
```

SMTP settings are intentionally environment-based. Do not hard-code email credentials in source code.

---

## Running Tests

```bash
python -m pytest -q
```

Current tests cover:

- Role approval restrictions
- Finance payment-request restrictions
- Utility Head / Facility Head display label
- Draft deletion rules
- Workflow routing
- Excel workbook generation
- Database migration and Finance permission cleanup

---

## Manual Acceptance Test Flow

Use these login accounts in order:

1. Login as `facility`.
2. Create a gateway pass draft.
3. Delete the draft before submission.
4. Create another gateway pass draft and send it for Procurement Review.
5. Login as `procurement`.
6. Confirm the item appears automatically without Admin linking.
7. Return it for correction.
8. Login as `facility`, edit and resubmit.
9. Login as `procurement`, submit to Approver/Admin.
10. Login as `approver`, approve the item.
11. Login as `finance`, confirm only approved/awaiting-payment records appear.
12. Mark paid and upload receipt/payment evidence.
13. Confirm the record moves to Completed/History.
14. Login as `auditor`.
15. Open reports, verify charts, and download Excel reports.
16. Confirm no approval buttons appear for Procurement Manager, Finance, Utility Head / Facility Head, or Auditor.
17. Confirm all major actions appear in Audit Logs.

---

## Database and Backups

The default SQLite database is stored in `data/procureflow_workspace.db`.

Run migration after deploying a new build:

```bash
python migrate_existing_db.py
```

For production, schedule file-level backups of the `data/` folder or use the Admin maintenance/export tools where available. The database layer keeps SQLite compatibility and is organized so migration to PostgreSQL can be done later through the query/service layer.

---

## PostgreSQL Migration Notes

This build still uses SQLite for easy local/demo deployment. To migrate later:

1. Replace raw SQLite helpers in `core/db.py` with a connection factory that targets PostgreSQL.
2. Convert SQLite-specific functions and date expressions to PostgreSQL equivalents.
3. Preserve `core/permissions.py` and `core/workflow.py` unchanged.
4. Migrate tables using a tool such as Alembic, pgloader, or a controlled ETL script.
5. Run the test suite against PostgreSQL in CI before production cutover.

---

## Prioritized TODOs / Edge Cases

1. Add deeper end-to-end Streamlit UI tests with Playwright or Selenium.
2. Add real SMTP provider integration tests and delivery monitoring.
3. Add production virus scanning to the upload hook.
4. Replace remaining legacy UI screens with smaller module-specific files as the next refactor phase.
5. Add row-level redaction policies for sensitive exports if required by company policy.
6. Add optional PostgreSQL support through environment-based connection strings.
7. Add background notification worker if the deployment environment supports it.

---

## Troubleshooting

### Streamlit duplicate key errors

The corrected dynamic line-item forms use stable unique keys. If a custom page is added later, repeated widgets must use a unique prefix and row ID.

### Slow tab switching

Avoid rendering all reports/tabs at once. Use sidebar sections, cached query helpers, and pagination for large tables.

### OCR not working

Install Tesseract OCR on the host and ensure it is in PATH. Dockerfile includes `tesseract-ocr`.

### Windows long-path issues

Extract the project to a short path such as `C:\ProcureFlow` and avoid deeply nested folders.

---

## Changelog

### Command-chain hardened build

- Added centralized permissions and workflow modules.
- Rebuilt command-chain routing and role restrictions.
- Added Income tab and calculation logic.
- Replaced CSV downloads with Excel workbooks.
- Fixed gateway pass PDF alignment and department dropdown.
- Added stable line-item handling and Other-field persistence.
- Added migration script, Docker files, environment example, README, and pytest tests.

### 2026-06-17 quick correction update

- Gateway Pass department choices are now separated as `CMOTD` and `RACAM`.
- Gateway Pass approval is allowed only for Admin, Approver / MD, and Procurement Manager. Procurement Manager still cannot approve purchase, payment, PO, finance, or normal workflow items.
- Workflow progress/status rails render horizontally with side scrolling instead of vertical letter wrapping.
- Purchase request and Gateway Pass dynamic line items now include both `+ Add` and `− Remove` controls with stable Streamlit keys.
- Major workflow actions use persistent green success confirmations after rerun to reduce accidental double-click duplicate submissions.
- Dropdowns with `Other` continue to reveal manual input boxes and save the typed value for reuse, reports, and documents.

### 2026-06-17 focused workflow notification/report update

- Gateway Pass returns from Procurement Manager, Approver / MD, or Admin now remain visible in the Utility Head / Facility Head `Drafts / Returned` Gateway Pass section for correction and resubmission.
- Gateway Pass review routing now creates notifications and red sidebar badge targets for Procurement Manager, Approver / MD, Admin, Utility Head / Facility Head, and Auditor where relevant.
- Approved Gateway Passes now notify the Utility Head / Facility Head and unlock the `Ready to Generate` interface without changing the preserved professional PDF template.
- Finance payment completion now leaves the request as `Paid` and routes the final operational closure task to Procurement Manager.
- Procurement Manager now has a `Post-Payment Closure` section to mark paid records as `Completed`, then `Closed`, then `Archived`.
- Auditor dashboard now includes recent activity notifications in addition to the audit log and report views.
- Downloads now support three formats across report/download surfaces: Excel `.xlsx`, PDF `.pdf`, and CSV `.csv`. The selected format is generated only on demand to keep tab navigation fast.

## Focused Gateway Pass Approval and Notification Fix

This build corrects the Gateway Pass approval handoff and notification routing:

- Gateway Pass approval now follows the strict chain: Utility Head / Facility Head → Procurement Manager review → Approver / MD or Admin final approval → Utility Head / Facility Head ready-to-generate queue.
- Procurement Manager no longer has a Gateway Pass approval button or approval permission. Procurement Manager can review, return for correction, or submit Gateway Passes to Approver / MD.
- Approver / MD receives Gateway Pass notifications only after Procurement Manager submits the pass for final approval.
- When Approver / MD or Admin approves, the record is set to `Approved` and `next_role='facility_manager'`, which activates the Utility Head / Facility Head Ready to Generate tab.
- The Gateway Pass Ready to Generate queue shows only approved, not-yet-generated passes; after generation, the pass moves to History and remains downloadable.
- Notification badge logic was aligned with the updated routing so Procurement Manager, Approver / MD, Utility Head / Facility Head, Admin, and Auditor receive the correct workflow notices.
- The generated Gateway Pass PDF keeps the uploaded CMOTD/RSU-style template layout with logos, title, reference/date, property details, transport details, authorization, and security verification lines.

---

## Logistics Officer Fulfilment Update

This build separates commercial procurement from delivery execution.

**Command chain**

```text
Facility / Utility Head
        ↓
Procurement Manager — review, sourcing, vendor recommendation, PO creation
        ↓
Approver / Admin — request and PO approval
        ↓
Procurement Manager — commercial release of approved PO to Logistics
        ↓
Logistics Officer — delivery planning, tracking, gateway movement coordination,
                    receiving slips, exceptions, and proof of delivery
        ↓
Finance — receipt/invoice/payment review
        ↓
Procurement Manager — commercial closure and archive
```

### Procurement Manager changes

- **Receiving Slips** has been removed from Procurement navigation.
- **Commercial PO Management** replaces the prior PO/receiving combination.
- Procurement Manager can create POs, send them for PO approval, and use **Release to Logistics** after PO approval.
- Procurement Manager retains sourcing, vendor quotes, vendor recommendation, vendor management, commercial PO management, gateway-pass review, and post-payment closure.

### Logistics Officer interface

The new Logistics workspace contains:

- Logistics Dashboard
- PO Delivery Handover
- Delivery Tracking
- Receiving Slips
- Delivery Exceptions & Returns
- Gateway Pass Coordination
- Logistics Documents
- My Activity History
- Settings

Logistics cannot source vendors, select suppliers, create a PO, or approve a request/PO/gateway pass.

### Logistics records

The database now stores delivery handover, tracking, driver/vehicle, waybill, proof-of-delivery, receiving, and exception records in a role-safe way. Existing sent/delivery-stage POs are backfilled into the Logistics queue without removing their prior history.

A demo Logistics Officer account is included for the local/demo build:

| Role | Username | Password |
|---|---:|---:|
| Logistics Officer | `logistics` | `logistics123` |

Change all demo credentials before production use.

---

## Auditor Evidence Ledger, Security Hardening & Secure Payment Payees

This release adds a read-only **Auditor Evidence Ledger** and secure payment-recipient details without changing normal navigation, workflow actions, or command chains for Admin, Approver, Finance, Logistics, Security, or other roles.

### Auditor evidence coverage

The Auditor workspace now includes a paginated, redacted, append-only ledger plus dedicated read-only evidence views for procurement, sourcing/vendor quotes, POs/logistics, receiving/proof-of-delivery/returns, finance/payments, approvals, gateway passes, notifications, security events, documents, budgets, vendors, and Facility/Utility handoffs.

- Every existing `log_audit(...)` call also writes a tamper-evident ledger event.
- Database triggers create a transactional evidence backstop for major workflow tables, including requests, sourcing, quotes, POs, payments, gateway passes, receiving slips, logistics exceptions, invoices, receipts, and approval history.
- Events are redacted, hash-chained, signed, and append-only. Audit events cannot be changed or deleted through the application database connection.
- Run a manual integrity check with:

```bash
python -m workers.audit_chain_worker
```

### Secure request-draft payment payee details

Only the **Procurement Manager** and **Utility/Facility Head** purchase-request draft forms now contain the expandable `Payment Payee / Bank Details` section.

- Account numbers are encrypted at application level.
- Normal views retain masked names and only the final four account digits.
- NGN accounts require exactly 10 digits.
- A requester may state that the payee is not yet known, but the record stays payment-blocked until authorized Finance processing verifies completed details.
- Existing historical requests without an encrypted payee record remain compatible.
- Auditors see masked data by default. A temporary sensitive-data reveal requires permission, a reason, and writes its own audit event.

### Production configuration

Copy `.env.example` to `.env` and set these values through a secret manager before setting `PROCUREFLOW_PRODUCTION=1`:

```text
PROCUREFLOW_PAYEE_ENCRYPTION_KEY=
PROCUREFLOW_AUDIT_SIGNING_KEY=
```

New and changed production passwords use Argon2id. Legacy PBKDF2/SHA256 credentials are rehashed on successful login where Argon2 is available. Configure lockout, upload limits, and audit keys through the supplied environment file.

### Migration and deployment notes

The schema is non-destructive and is applied automatically during startup. For an existing database, you may also run:

```bash
python migrate_existing_db.py
```

SQLite remains suitable for a local/demo installation. For concurrent production usage, move the repository/service layer to PostgreSQL, place Streamlit behind an HTTPS reverse proxy, use Secure/HttpOnly/SameSite session cookies at the proxy layer, and store files in private object storage with signed downloads.
