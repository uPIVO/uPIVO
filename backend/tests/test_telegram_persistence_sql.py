from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


def test_sync_state_migration_uses_fake_operations_only(monkeypatch):
    migration_path = Path(__file__).parents[1] / "alembic/versions/tg_sync_state_001.py"
    spec = spec_from_file_location("tg_sync_state_migration_test", migration_path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = []
    fake_op = SimpleNamespace(
        create_index=lambda *args, **kwargs: operations.append(("index", args, kwargs)),
        create_table=lambda *args, **kwargs: operations.append(("table", args, kwargs)),
        drop_table=lambda *args, **kwargs: operations.append(("drop_table", args, kwargs)),
        drop_index=lambda *args, **kwargs: operations.append(("drop_index", args, kwargs)),
    )
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    assert migration.down_revision == "0003"
    assert operations[0][0] == "index"
    assert operations[0][2]["unique"] is True
    assert str(operations[0][2]["postgresql_where"]) == "type = 'telegram'"
    assert operations[1][0] == "table"
    assert operations[1][1][0] == "telegram_sync_state"
