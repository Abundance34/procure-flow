# Procurement Request Register Guided Action Fix

Focused fix only for the Procurement Manager request-register guided-action issue.

## Fixed

1. Removed the StreamlitAPIException caused by writing directly to `st.session_state["procurement_section"]` after the sidebar radio widget had already been created.
2. Added safe pending navigation through `_pending_nav_procurement_section`; `app.py` now applies this pending destination before the sidebar widget is rendered on the next rerun.
3. Updated Procurement Manager request-register actions so sourcing/PO navigation can happen safely from inside a request detail panel.
4. Added the missing approved-request action in the request register: **Open Purchase Orders / Create PO**.
5. Kept existing Procurement Manager actions available where valid:
   - Mark Reviewed
   - Mark Requires Sourcing / Start Vendor Quotes
   - Open / Continue Sourcing
   - Submit to Approver/Admin
   - Submit Vendor Recommendation to Approver/Admin
   - Return for Correction
   - Open Purchase Orders / Create PO

## Not changed

No other role workflow, gateway-pass logic, finance logic, or facility-manager screens were intentionally changed.

## Validation

- Python compile check passed.
- Test suite passed: 10 tests.
