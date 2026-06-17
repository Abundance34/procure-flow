from core.db import init_db, df_query


def test_command_chain_schema_migrates():
    init_db()
    income = df_query("SELECT name FROM sqlite_master WHERE type='table' AND name='income_entries'")
    assert not income.empty
    finance = df_query("SELECT permission_name FROM role_permissions WHERE role_name='Finance'")
    perms = set(finance["permission_name"].tolist())
    assert "approve_request" not in perms
    assert "approve_payment" not in perms
