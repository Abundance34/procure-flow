# Gateway Pass Signature/Security Fix

Focused update only for the gateway pass template/input workflow.

## Changed

- Removed the duplicate/double signature underline in the HTML gateway pass preview by letting CSS draw one signing line instead of combining underscores with a border line.
- Added database-backed gateway pass fields:
  - `security_officer_name`
  - `gate_verification_time`
  - `exit_entry_confirmation`
  - `security_signature`
  - `actual_return_date`
- Added input tabs for gateway pass return/security details:
  - Return Date
  - Security Verification
- Added these same fields to the editable gateway pass draft form.
- Made approved/generated gateway passes editable only for return/security details so the Facility/Utility owner can fill the empty security lines before re-generating/downloading.
- Updated generated gateway pass PDF to show saved return/security values.

## Not Changed

- No role workflow changes.
- No Procurement Manager approval/workflow changes.
- No other user workspace changes.
