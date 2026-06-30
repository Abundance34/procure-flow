"""Run periodic audit-chain verification from a scheduler/worker container.

Example:
    python -m workers.audit_chain_worker
"""
from core.db import init_db
from services.audit_service import verify_audit_chain

if __name__ == "__main__":
    init_db()
    result = verify_audit_chain(record_result=True)
    print(result)
