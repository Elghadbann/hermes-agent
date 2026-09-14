"""KR-P0-1B durable Owner authorization and dispatcher admission."""

from __future__ import annotations

import threading

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _task(conn, *, created_by="owner", status=None):
    task_id = kb.create_task(conn, title="authorization", assignee="default", created_by=created_by)
    if status:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        conn.commit()
    return task_id


def test_owner_authorize_allows_dispatch_and_records_authorization(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    with kbc.connect(db_path) as conn:
        task_id = _task(conn)
        result = kb.authorize_task(conn, task_id, owner="owner", expires_at=2_000_000_000)
        assert result.state == "authorized"
        dispatch = kbd.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 1234)
        assert [row[0] for row in dispatch.spawned] == [task_id]
        auth = conn.execute(
            "SELECT state, authorized_by, expires_at FROM kanban_execution_authorizations "
            "WHERE task_id = ?", (task_id,),
        ).fetchone()
        assert dict(auth) == {
            "state": "authorized", "authorized_by": "owner", "expires_at": 2_000_000_000,
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 1


def test_dispatch_refuses_legacy_manual_task_with_audit_and_no_run(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    with kbc.connect(db_path) as conn:
        task_id = _task(conn)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 1234)
        assert result.spawned == []
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0
        audit = conn.execute(
            "SELECT outcome, reason FROM kanban_transition_attempts WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert dict(audit) == {"outcome": "refused", "reason": "legacy_or_manual_unapproved"}


def test_revoke_is_idempotent_and_blocks_claim(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    with kbc.connect(db_path) as conn:
        task_id = _task(conn)
        kb.authorize_task(conn, task_id, owner="owner")
        first = kb.revoke_task_authorization(conn, task_id, owner="owner")
        attempts = conn.execute("SELECT COUNT(*) FROM kanban_transition_attempts WHERE task_id=?", (task_id,)).fetchone()[0]
        second = kb.revoke_task_authorization(conn, task_id, owner="owner")
        assert first.state == second.state == "revoked"
        assert conn.execute("SELECT COUNT(*) FROM kanban_transition_attempts WHERE task_id=?", (task_id,)).fetchone()[0] == attempts
        assert kb.claim_task(conn, task_id, authorization_required=True) is None
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0


def test_expired_authorization_is_refused_and_durable(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    with kbc.connect(db_path) as conn:
        task_id = _task(conn)
        auth = kb.authorize_task(conn, task_id, owner="owner", expires_at=100)
        assert auth.state == "expired"
        ok, reason = kb.authorization_admission(conn, task_id, now=101)
        assert (ok, reason) == (False, "authorization_expired")
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT reason FROM kanban_transition_attempts WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()[0] == "authorization_expired"


def test_inconsistent_authorization_fails_closed(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    with kbc.connect(db_path) as conn:
        task_id = _task(conn)
        kb.authorize_task(conn, task_id, owner="owner")
        conn.execute("UPDATE tasks SET execution_authorized=0 WHERE id=?", (task_id,))
        conn.commit()
        assert kb.claim_task(conn, task_id, authorization_required=True) is None
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0


def test_concurrent_authorized_claims_open_at_most_one_run(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    with kbc.connect(db_path) as conn:
        task_id = _task(conn)
        kb.authorize_task(conn, task_id, owner="owner")
    barrier = threading.Barrier(2)
    outcomes = []

    def claim():
        with kbc.connect(db_path) as conn:
            barrier.wait()
            outcomes.append(kb.claim_task(conn, task_id, claimer=threading.current_thread().name, authorization_required=True) is not None)

    threads = [threading.Thread(target=claim, name=f"claimer-{n}") for n in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    with kbc.connect(db_path) as conn:
        assert sum(outcomes) == 1
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 1
