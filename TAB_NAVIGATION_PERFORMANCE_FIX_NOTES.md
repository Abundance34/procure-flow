# Tab Navigation Performance Fix

## Scope
This release changes only the internal work performed during normal sidebar/tab navigation. No role menu, workflow, approval rule, screen layout, button, permission, or data entry process was changed.

## Improvements
- Sidebar attention badges now fetch shared last-seen markers in one database query instead of one query per section.
- Sidebar notification badge counts now use one grouped database query instead of one query per section.
- The section-attention table/index setup runs once per browser session rather than repeating for each tab click.
- The sidebar notification panel uses one unread-notification read per rerun and batches popup acknowledgement updates.
- Added SQLite indexes that support notification/section attention lookups.

## Expected result
Sidebar tab changes should feel substantially faster, especially on Windows local deployments and on Auditor accounts with many navigation sections. No badge, notification, activity-history, workflow, or approval data is removed or changed by this optimization.
