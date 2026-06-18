"""Report service compatibility wrapper.

The concrete report builders live in core.report_service so existing imports
continue to work.  New UI modules should prefer core.report_service directly or
add report orchestration functions here.
"""
from core.report_service import *  # noqa: F401,F403
