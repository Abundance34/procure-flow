# Metric Row Import Fix

## Issue corrected

The Auditor Evidence Ledger module imported two shared UI helpers from `core.ui`:

- `metric_row`
- `role_header`

They were missing from `core/ui.py`, which caused the application to stop at startup with:

```text
ImportError: cannot import name 'metric_row' from 'core.ui'
```

## Correction

Both shared helpers are now defined in `core/ui.py`. `metric_row` is the standard KPI-card renderer used by the Auditor dashboard, and `role_header` is the shared workspace header renderer required by the Auditor workspace.

No workflow, role permissions, or user interface controls were changed.

## Validation

- Python compile check: passed
- Auditor module import smoke test: passed
- Test suite: 23 passed
