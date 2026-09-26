"""Check that application startup and test fixtures build the same schema."""

import sqlite3

from updater import database
from updater.db.schema import build_schema


def normalized_schema(conn: sqlite3.Connection) -> dict:
    """Compare schema metadata without depending on CREATE statement layout."""
    tables = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ):
        identifier = '"' + name.replace('"', '""') + '"'
        indexes = {}
        for _, index_name, unique, origin, partial in conn.execute(
            f"PRAGMA index_list({identifier})"
        ):
            index_identifier = '"' + index_name.replace('"', '""') + '"'
            indexes[index_name] = {
                "unique": unique,
                "origin": origin,
                "partial": partial,
                "columns": [tuple(row) for row in conn.execute(
                    f"PRAGMA index_xinfo({index_identifier})"
                )],
            }
        tables[name] = {
            "columns": [tuple(row) for row in conn.execute(
                f"PRAGMA table_info({identifier})"
            )],
            "indexes": indexes,
            "foreign_keys": sorted(tuple(row) for row in conn.execute(
                f"PRAGMA foreign_key_list({identifier})"
            )),
        }
    triggers = {
        name: (table, " ".join(sql.split()))
        for name, table, sql in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    return {"tables": tables, "triggers": triggers}


def test_init_db_matches_memory_db(memory_db, tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "data" / "test.db")
    database.init_db()
    with database.get_db() as conn:
        expected = normalized_schema(memory_db)
        assert expected["tables"]
        assert expected["triggers"]
        assert normalized_schema(conn) == expected


def test_schema_can_be_built_again_without_changing_data(memory_db):
    memory_db.execute("INSERT INTO tower_sites (name) VALUES ('Test site')")
    memory_db.commit()
    schema = normalized_schema(memory_db)
    rows = {
        name: [tuple(row) for row in memory_db.execute(f'SELECT * FROM "{name}"')]
        for name in schema["tables"]
    }

    build_schema(memory_db)

    assert normalized_schema(memory_db) == schema
    for name, expected in rows.items():
        assert [tuple(row) for row in memory_db.execute(f'SELECT * FROM "{name}"')] == expected
