# Finance Receipt and Refresh Fix

This release changes only the two requested behaviours.

## Finance: Mark Paid separately from receipt recording

- `Approved for Payment` now shows **Mark Paid** only.
- Finance is no longer required to upload a receipt before marking an approved item as paid.
- A paid payment remains available in **Finance → Receipts** until a receipt is recorded.
- The Receipts form now includes **Link this receipt to a paid payment (optional)**. Selecting a paid payment pre-fills its amount, payment method, vendor, date, department, and reference where available.
- Saving the receipt links it to the payment and records the receipt separately.

## Browser refresh retention

- The active opaque server-side session token is stored in an encrypted browser cookie.
- After a normal browser refresh, ProcureFlow restores the valid signed-in session and continues using the existing role/section URL hint, rather than sending the user back to login.
- Logout and session expiry still invalidate the server-side session and clear the browser session cookie.

## Setup note

This package adds `streamlit-cookies-manager` to `requirements.txt`. Run `INSTALL_WINDOWS.bat` again after extracting the update so the virtual environment installs the added dependency.
