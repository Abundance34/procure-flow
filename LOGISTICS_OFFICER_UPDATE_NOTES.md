# Logistics Officer Update Notes

## New role

**Role:** Logistics Officer  
**Purpose:** Delivery coordination, receiving, movement documentation, delivery exceptions, and proof-of-delivery management.

The role is included in Admin User Management and a demo account is seeded for local/demo use:

```text
Username: logistics
Password: logistics123
```

Change the password before production use.

## Procurement Manager scope after update

Procurement Manager retains commercial work:

- request review
- sourcing and vendor quotes
- vendor recommendation
- vendor management
- request and vendor recommendation submission to Approver/Admin
- PO creation and PO approval submission
- commercial PO release to Logistics
- gateway-pass review
- post-payment closure/archive

Procurement Manager no longer receives **Receiving Slips** in its navigation and no longer has the `receive_goods` permission.

## New PO command chain

```text
Draft PO
  → Pending Approval
  → Approved
  → Released to Logistics
  → Scheduled / Sent to Vendor / Dispatched / In Transit / Delayed / Arrived
  → Partially Received / Fully Received / Disputed / Returned
```

- Procurement releases an approved PO using **Release to Logistics**.
- Logistics completes the handover form, then tracks dispatch/delivery.
- Logistics records receipt, conditions, discrepancies, delivery note, and proof of delivery.
- A receiving slip notifies Finance and Procurement Manager.
- A delivery exception notifies Procurement Manager and Facility/Utility; Finance is notified only where the exception can affect invoice/payment matching.

## Gateway-pass guardrail

Logistics can coordinate the approved movement, enter driver/vehicle/waybill information, attach proof, and update movement status. Logistics cannot create, review, approve, reject, or generate a gateway pass.
