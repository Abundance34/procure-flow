# ₦100,000 Approval Rule and Gateway Pass Usability Fix

## Scope

This update makes only the requested command-chain, demo-credential, and Facility/Utility gateway-pass corrections. It does not add Logistics, Finance, Security, or Facility workflow responsibilities to Procurement Manager.

## 1. Logistics demo credential

The login screen now lists the Logistics Officer demo account:

| Role | Username | Password |
|---|---|---|
| Logistics Officer | `logistics` | `logistics123` |

The account is also guaranteed by the database bootstrap/migration.

## 2. Gateway-pass generation no longer leaves a blank screen

Generating an approved pass changes its state from `Approved` to `Generated`. Previously, the Facility/Utility user could remain on the `Ready to Generate` queue, which then contained no records.

The correction now stores a safe pending navigation target, reruns the page, and opens `History` before its navigation control is created. The generated pass remains visible for preview and download.

## 3. Active Return Date during pass creation

The Facility/Utility `Create Gateway Pass Draft` form includes an active `Return Date` tab with:

- Expected return date, defaulting to the day after the movement date.
- Optional actual return date.
- Validation that the expected return date is not earlier than the movement date.

Those values persist to the gateway-pass record and appear on the generated PDF.

## 4. ₦100,000 approval authority

The rule is inclusive:

| Transaction amount | Approval authority |
|---:|---|
| ₦100,000 and below | Procurement Manager |
| Above ₦100,000 | Approver / MD |

It is applied consistently to:

- Purchase requests.
- Purchase orders.
- Standalone/manual payment requests.

### Segregation-of-duties exception

A Procurement Manager cannot approve a request they created. A PM-created request is always routed directly to Approver / MD, including a request at or below ₦100,000. This preserves the earlier requirement that a PM draft must not be sent back to the same Procurement Manager for approval.

### Approver audit visibility

Every Procurement Manager low-value approval records:

- Approval history entry.
- Approver notification.
- Workflow event.
- Activity event.
- Audit log with actor, before/after state, amount-routing context, and approval mode.

Approver / MD sees these records in `My Approval History & Procurement Manager Approval Audit`, but does not receive a duplicate approval action for the same low-value record.

## Validation

- Python compile check passed.
- Automated tests passed: 18.
- Database migration validation confirmed `approval_mode`, threshold routing, and the Logistics demo account.
