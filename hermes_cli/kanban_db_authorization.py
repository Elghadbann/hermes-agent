"""Durable execution authorization and admission for the KR-P0-1B slice."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from hermes_cli import kanban_db as _kb


@dataclass(frozen=True)
class ExecutionAuthorization:
    authorization_id: str
    task_id: str
    state: str
    authorized_by: Optional[str]
    authorized_at: Optional[int]
    expires_at: Optional[int]
    evidence: Optional[dict[str, Any]]

    @classmethod
    def from_row(cls, row: sqlite3.Row, *, now: Optional[int] = None) -> "ExecutionAuthorization":
        expires = int(row["expires_at"]) if row["expires_at"] is not None else None
        state = row["state"]
        if state == "authorized" and expires is not None and expires <= (int(time.time()) if now is None else now):
            state = "expired"
        try:
            evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else None
        except (TypeError, ValueError):
            evidence = None
        return cls(
            authorization_id=row["authorization_id"], task_id=row["task_id"], state=state,
            authorized_by=row["authorized_by"], authorized_at=row["authorized_at"],
            expires_at=expires, evidence=evidence if isinstance(evidence, dict) else None,
        )


def _latest(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM kanban_execution_authorizations WHERE task_id = ? "
        "ORDER BY updated_at DESC, rowid DESC LIMIT 1", (task_id,),
    ).fetchone()


def _record_attempt(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, reason: str,
    authorization_id: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
    actor_type: str = "owner",
) -> None:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO kanban_transition_attempts "
        "(task_id, from_status, to_status, outcome, actor_type, actor_id, reason, "
        "authorization_id, metadata_json, attempted_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, row["status"], row["status"], outcome, actor_type, None, reason,
         authorization_id, json.dumps(metadata, sort_keys=True) if metadata else None,
         int(time.time()), int(time.time())),
    )


def authorize_task(
    conn: sqlite3.Connection, task_id: str, *, owner: str,
    expires_at: Optional[int] = None, evidence: Optional[dict[str, Any]] = None,
) -> ExecutionAuthorization:
    """Idempotently authorize a task when *owner* is its durable creator."""
    now = int(time.time())
    with _kb.write_txn(conn):
        task = conn.execute(
            "SELECT created_by FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task is None:
            raise KeyError(task_id)
        if not owner or task["created_by"] != owner:
            raise PermissionError("only the task owner may authorize execution")
        current = _latest(conn, task_id)
        if current is not None and current["state"] == "authorized":
            current_expiry = current["expires_at"]
            if current["authorized_by"] == owner and current_expiry == expires_at:
                return ExecutionAuthorization.from_row(current, now=now)
        auth_id = current["authorization_id"] if current is not None else f"auth-{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO kanban_execution_authorizations "
            "(authorization_id, task_id, state, authorized_by, authorized_at, expires_at, "
            "evidence_json, created_at, updated_at) VALUES (?, ?, 'authorized', ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(authorization_id) DO UPDATE SET state='authorized', authorized_by=excluded.authorized_by, "
            "authorized_at=excluded.authorized_at, expires_at=excluded.expires_at, evidence_json=excluded.evidence_json, "
            "updated_at=excluded.updated_at",
            (auth_id, task_id, owner, now, expires_at,
             json.dumps(evidence, sort_keys=True) if evidence else None, now, now),
        )
        conn.execute(
            "UPDATE tasks SET execution_policy='owner_gate', execution_authorized=1, "
            "authorization_state='authorized' WHERE id = ?", (task_id,),
        )
        _record_attempt(conn, task_id, outcome="authorized", reason="owner_authorized", authorization_id=auth_id)
        row = _latest(conn, task_id)
        return ExecutionAuthorization.from_row(row, now=now)


def revoke_task_authorization(
    conn: sqlite3.Connection, task_id: str, *, owner: str, reason: str = "owner_revoked",
) -> Optional[ExecutionAuthorization]:
    """Idempotently revoke the current authorization."""
    now = int(time.time())
    with _kb.write_txn(conn):
        task = conn.execute("SELECT created_by FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        if not owner or task["created_by"] != owner:
            raise PermissionError("only the task owner may revoke execution")
        current = _latest(conn, task_id)
        if current is None:
            return None
        if current["state"] != "revoked":
            conn.execute(
                "UPDATE kanban_execution_authorizations SET state='revoked', updated_at=? "
                "WHERE authorization_id=?", (now, current["authorization_id"]),
            )
            conn.execute(
                "UPDATE tasks SET execution_authorized=0, authorization_state='revoked' WHERE id=?",
                (task_id,),
            )
            _record_attempt(conn, task_id, outcome="revoked", reason=reason, authorization_id=current["authorization_id"])
        return ExecutionAuthorization.from_row(_latest(conn, task_id), now=now)


def authorization_admission(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
    record_refusal: bool = True,
) -> tuple[bool, str]:
    """Fail-closed admission check. Caller may already hold the write txn."""
    now = int(time.time()) if now is None else int(now)
    task = conn.execute(
        "SELECT execution_policy, execution_authorized, authorization_state, created_by "
        "FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if task is None:
        return False, "task_not_found"
    auth = _latest(conn, task_id)
    reason = None
    if task["execution_policy"] != "owner_gate": reason = "legacy_or_manual_unapproved"
    elif int(task["execution_authorized"] or 0) != 1 or task["authorization_state"] != "authorized": reason = "task_authorization_inconsistent"
    elif auth is None: reason = "authorization_missing"
    elif auth["state"] != "authorized": reason = f"authorization_{auth['state']}"
    elif auth["authorized_by"] != task["created_by"]: reason = "authorization_owner_inconsistent"
    elif auth["expires_at"] is not None and int(auth["expires_at"]) <= now: reason = "authorization_expired"
    if reason is None:
        return True, "authorized"
    if record_refusal:
        if reason == "authorization_expired" and auth is not None:
            _record_attempt(conn, task_id, outcome="expired", reason=reason,
                            authorization_id=auth["authorization_id"], metadata={"at": now})
        _record_attempt(conn, task_id, outcome="refused", reason=reason,
                        authorization_id=auth["authorization_id"] if auth else None,
                        metadata={"at": now}, actor_type="dispatcher")
        if reason == "authorization_expired" and auth is not None:
            conn.execute("UPDATE tasks SET execution_authorized=0, authorization_state='revoked' WHERE id=?", (task_id,))
    return False, reason
