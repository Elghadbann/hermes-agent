"""Schema-foundation contracts for KR-P0-1A."""

from __future__ import annotations

import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


_RELIABILITY_TABLES = {
    "kanban_schema_ledger",
    "kanban_execution_authorizations",
    "kanban_transition_attempts",
    "kanban_dependency_evaluations",
    "kanban_workspace_allocations",
    "kanban_provider_capabilities",
    "kanban_acceptance_evidence",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def test_fresh_schema_records_reliability_migration_and_safe_defaults(tmp_path):
    db_path = tmp_path / "fresh-kanban.db"

    kb.init_db(db_path=db_path)
    with kbc.connect(db_path) as conn:
        tables = _table_names(conn)
        assert _RELIABILITY_TABLES <= tables

        ledger = conn.execute(
            "SELECT migration_id, version, checksum FROM kanban_schema_ledger "
            "WHERE migration_id = ?",
            ("kr-p0-1a-reliability-foundation",),
        ).fetchone()
        assert ledger is not None
        assert ledger["version"] == 1
        assert ledger["checksum"]

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
        }
        assert {
            "execution_policy",
            "execution_authorized",
            "authorization_state",
        } <= columns

        task_id = kb.create_task(conn, title="legacy-safe")
        row = conn.execute(
            "SELECT execution_policy, execution_authorized, authorization_state "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(row) == {
            "execution_policy": "manual",
            "execution_authorized": 0,
            "authorization_state": "not_authorized",
        }

        now = 100
        conn.execute(
            "INSERT INTO kanban_execution_authorizations "
            "(authorization_id, task_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("auth-1", task_id, now, now),
        )
        conn.execute(
            "INSERT INTO kanban_transition_attempts "
            "(task_id, attempted_at) VALUES (?, ?)",
            (task_id, now),
        )
        conn.execute(
            "INSERT INTO kanban_dependency_evaluations "
            "(task_id, dependency_key, evaluated_at) VALUES (?, ?, ?)",
            (task_id, "parents", now),
        )
        conn.execute(
            "INSERT INTO kanban_workspace_allocations "
            "(allocation_id, task_id) VALUES (?, ?)",
            ("allocation-1", task_id),
        )
        conn.execute(
            "INSERT INTO kanban_provider_capabilities (provider, capability) VALUES (?, ?)",
            ("provider", "pull_request"),
        )
        conn.execute(
            "INSERT INTO kanban_acceptance_evidence (task_id, evidence_type, recorded_at) VALUES (?, ?, ?)",
            (task_id, "tests", now),
        )
        assert conn.execute(
            "SELECT state FROM kanban_execution_authorizations WHERE authorization_id = 'auth-1'",
        ).fetchone()["state"] == "not_authorized"
        assert conn.execute(
            "SELECT outcome FROM kanban_transition_attempts WHERE task_id = ?", (task_id,),
        ).fetchone()["outcome"] == "pending"
        assert conn.execute(
            "SELECT state FROM kanban_dependency_evaluations WHERE task_id = ?", (task_id,),
        ).fetchone()["state"] == "unknown"
        assert conn.execute(
            "SELECT state FROM kanban_workspace_allocations WHERE allocation_id = 'allocation-1'",
        ).fetchone()["state"] == "unallocated"
        assert conn.execute(
            "SELECT observation_state FROM kanban_provider_capabilities WHERE provider = 'provider'",
        ).fetchone()["observation_state"] == "unknown"
        assert conn.execute(
            "SELECT status FROM kanban_acceptance_evidence WHERE task_id = ?", (task_id,),
        ).fetchone()["status"] == "unknown"


def test_legacy_migration_preserves_rows_and_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("legacy-task", "legacy", "ready", 10),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        ("legacy-task", "created", '{"legacy":true}', 11),
    )
    conn.commit()
    conn.close()

    with kbc.connect(db_path) as migrated:
        before_task = dict(
            migrated.execute(
                "SELECT id, title, status, created_at, execution_authorized, "
                "authorization_state FROM tasks WHERE id = ?",
                ("legacy-task",),
            ).fetchone()
        )
        before_events = [
            dict(row)
            for row in migrated.execute(
                "SELECT task_id, kind, payload, created_at FROM task_events "
                "WHERE task_id = ?",
                ("legacy-task",),
            )
        ]
        assert before_task["execution_authorized"] == 0
        assert before_task["authorization_state"] == "not_authorized"

        # Values written by a later reliability phase must survive a rerun of
        # this additive migration; this stage never resets them.
        migrated.execute(
            "UPDATE tasks SET execution_policy = ?, execution_authorized = ?, "
            "authorization_state = ? WHERE id = ?",
            ("owner_gate", 1, "authorized", "legacy-task"),
        )
        migrated.commit()
        before_task = dict(
            migrated.execute(
                "SELECT id, title, status, created_at, execution_authorized, "
                "authorization_state, execution_policy FROM tasks WHERE id = ?",
                ("legacy-task",),
            ).fetchone()
        )

    kb.init_db(db_path=db_path)
    kb.init_db(db_path=db_path)

    with kbc.connect(db_path) as rerun:
        after_task = dict(
            rerun.execute(
                "SELECT id, title, status, created_at, execution_authorized, "
                "authorization_state, execution_policy FROM tasks WHERE id = ?",
                ("legacy-task",),
            ).fetchone()
        )
        after_events = [
            dict(row)
            for row in rerun.execute(
                "SELECT task_id, kind, payload, created_at FROM task_events "
                "WHERE task_id = ?",
                ("legacy-task",),
            )
        ]
        assert after_task == before_task
        assert after_events == before_events
        assert rerun.execute(
            "SELECT COUNT(*) FROM kanban_schema_ledger WHERE migration_id = ?",
            ("kr-p0-1a-reliability-foundation",),
        ).fetchone()[0] == 1
        assert rerun.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
