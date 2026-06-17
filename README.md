# ProcureFlow Command-Chain Workspace

ProcureFlow is a Streamlit-based internal procurement, finance, audit, reporting, income, and gateway-pass management application. This build hardens the workflow so every request follows the approved chain of command:

**Utility Head / Facility Head -> Procurement Manager -> Approver/Admin -> Finance -> Receipt/Completion/History/Audit**

Only **Admin** and **Approver / MD** can approve or reject workflow items. Finance can only act after approval.

---

## Demo Login Credentials

| Role | Username | Password |
|---|---:|---:|
| Admin | `admin` | `admin123` |
| Procurement Manager | `procurement` | `procure123` |
| Utility Head / Facility Head | `facility` | `facility123` |
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
  - Procurement Manager reviews, comments, returns for correction, or submits to Approver/Admin.
  - Approver/Admin approves or rejects.
  - Finance only sees approved items ready for payment.
  - Auditor is read-only and sees histories, audit logs, reports, and completed records.
- Removed unsafe approval rights from Finance and Procurement Manager.
- Removed Procurement Manager fallback approval from the approval rule migration.
- Draft deletion is restricted to own drafts before submission, except Admin audited override.

### Automatic Routing

The app now stores and uses `next_role` routing columns. Manual Admin linking is no longer required for Utility Head / Facility Head items to reach Procurement Manager queues.

| Action | Status | next_role |
|---|---|---|
| Utility Head sends draft | Sent for Procurement Review | `procurement_manager` |
| Procurement submits valid item | Submitted for Approval | `approver` |
| Approver/Admin approves | Approved / Awaiting Payment | `finance` |
| Finance pays/uploads receipt | Completed / auditor route | `auditor` |

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
- Generated gateway passes leave the ready-to-generate queue but remain in History and reports.

### Line Items and Other Fields

- Dynamic line items use unique Streamlit keys and do not overwrite prior rows.
- The `+ Add item` behavior supports two or more rows.
- Dropdowns with `Other` reveal a text input and save the typed value into records, review views, generated documents, exports, and audit trails where those fields are used.

### Audit Logs

Important actions are logged, including draft creation/edit/delete, sent for review, reviewed, returned, submitted for approval, approved/rejected, sent to finance, paid, receipt uploaded, completed, gateway pass generated, Excel report download, income entry creation, and login/logout where available.

---

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
