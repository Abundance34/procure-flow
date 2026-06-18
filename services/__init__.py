"""Command service layer for ProcureFlow.

Service modules are intentionally UI-free: Streamlit pages call them, services
validate role authority, and core.db persists workflow/audit/notification side
effects.  This gives the app a clearer command-and-communication architecture
without removing existing Streamlit screens.
"""
