from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3

import pytest

from codex_history.config import load_config, write_initial_config
from codex_history.migration import migrate_legacy_database
from codex_history.pipeline import build_full, plan, update_incremental
from codex_history.schema import connect

from conftest import add_transcript


def test_lossless_database_migration(portable_profile, tmp_path: Path):
    config, codex_home = portable_profile
    add_transcript(
        codex_home,
        "thread-migrate",
        "Migration source",
        timestamp="2026-07-14T01:00:00Z",
        label="migration",
    )
    source_build = build_full(config)
    legacy_database = tmp_path / "legacy.sqlite3"
    shutil.copy2(source_build["database"], legacy_database)
    legacy_connection = sqlite3.connect(legacy_database)
    legacy_connection.execute("PRAGMA foreign_keys=OFF")
    legacy_connection.execute("UPDATE threads SET source_id=NULL")
    legacy_connection.execute("DELETE FROM source_chunks")
    legacy_connection.execute("DELETE FROM source_files")
    legacy_connection.execute(
        "UPDATE metadata SET value='codex-history-v2.1.1' WHERE key='schema_version'"
    )
    legacy_connection.commit()
    legacy_connection.close()

    legacy_chroma = tmp_path / "legacy-chroma"
    legacy_chroma.mkdir()
    (legacy_chroma / "marker").write_text("legacy semantic runtime", encoding="utf-8")

    target_home = tmp_path / "target-history"
    write_initial_config(target_home, profile="default", source_roots=[codex_home])
    target_config = load_config(target_home)
    migrated = migrate_legacy_database(
        target_config,
        legacy_database,
        source_chroma=legacy_chroma,
    )
    assert migrated["audit"]["passed"] is True
    assert migrated["source_adoption"]["adopted"] == 1
    assert migrated["chroma_migration"]["copied"] is True
    assert (target_config.root / "semantic/chroma/marker").is_file()
    connection = connect(Path(migrated["database"]), readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] > 0
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='legacy_import'"
        ).fetchone()[0] == "true"
        assert connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 1
    finally:
        connection.close()
    migration_plan = plan(target_config, mode="incremental")
    assert migration_plan["incremental_ready"] is False
    assert migration_plan["warnings"]
    with pytest.raises(RuntimeError, match="query-compatible legacy migration"):
        update_incremental(target_config)
