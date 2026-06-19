# Procurement Manager / Facility Workflow Fix Notes

This build focuses only on the Procurement Manager and Utility Head / Facility Head request workflow.

## Fixed Procurement Manager guided actions
- Procurement Manager self-created drafts no longer route back to Procurement Manager.
- A Procurement Manager draft can now be sent directly to Approver/Admin.
- Procurement Manager request actions now include:
  - Mark Reviewed
  - Mark Requires Sourcing / Start Vendor Quotes
  - Open / Continue Sourcing
  - Submit to Approver/Admin
  - Return for Correction
- The Purchase Requests → Guided Next Actions board now exposes practical PM queues instead of only queue summaries.

## Fixed sourcing activation
- `Requires Sourcing`, `Vendor Quote Collection`, and `Vendor Recommendation` are now real workflow statuses, not aliases of `Reviewed by Procurement`.
- Creating/opening sourcing now creates a sourcing task, links it to the request, keeps the request in the Procurement Manager queue, and opens the Sourcing tab.
- The Procurement Manager inbox now has a visible `Requires Sourcing` button.
- Vendor recommendations remain with Procurement Manager until Procurement Manager submits them to Approver/Admin.

## Added vendor capture while drafting
- Procurement Manager request creation now has `Suggested vendor details (optional)`.
- Utility Head / Facility Head draft creation now has `Suggested vendor details (optional)`.
- Suggested vendors are saved into the vendor register and become selectable later during Sourcing → Add Vendor Quote.
- Vendor details are also recorded into the purchase request notes/history for traceability.

## Tests
- Python compile check passed.
- Test suite passed: 10 tests.
