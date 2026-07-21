import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "versions" / "20260721_0001_create_domain_model.py"
TABLES_IN_ORDER = (
    "competitions",
    "challenges",
    "artifacts",
    "solver_runs",
    "checkpoints",
    "model_calls",
    "tool_calls",
    "evidence",
    "flag_candidates",
    "submissions",
    "eval_runs",
)
STATE_TABLES = {"domain_events", "event_deliveries"}


def test_migration_uses_required_entity_order() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    offsets = [
        re.search(rf"op\.create_table\(\s*['\"]{table}['\"]", migration).start()
        for table in TABLES_IN_ORDER
    ]
    assert offsets == sorted(offsets)


def test_alembic_upgrade_and_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config(ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert set(TABLES_IN_ORDER) <= tables
        assert tables >= STATE_TABLES
        assert "alembic_version" in tables
    finally:
        engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
