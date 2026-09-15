"""Persist acceptance with the same ownership snapshot as the terminal write."""
from __future__ import annotations

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_pr_acceptance import _PR, _PR_SHORT, canonical_pr_url, collect_acceptance


def _snapshot(conn, task_id):
    row = conn.execute("SELECT current_run_id, status, completion_contract FROM tasks WHERE id=?", (task_id,)).fetchone()
    return tuple(row) if row else None


def _declared_pr_url(published_pr):
    """Both canonical published_pr forms -> full URL (None if not a PR ref)."""
    if not isinstance(published_pr, str):
        return None
    short = _PR_SHORT.fullmatch(published_pr.strip())
    if short:
        return f"https://github.com/{short[1]}/pull/{short[2]}"
    return published_pr.strip() if _PR.fullmatch(published_pr.strip()) else None


def prepare_acceptance(conn, task_id, expected_run_id, metadata):
    snapshot = _snapshot(conn, task_id)
    if snapshot is None:
        return False
    run_id, status, contract = snapshot
    if not contract or contract == "local-only":
        return None
    if status not in {"running", "ready", "blocked", "review"} or (expected_run_id is not None and run_id != expected_run_id):
        return False
    published_pr = metadata.get("published_pr") if isinstance(metadata, dict) else None
    pr_url = _declared_pr_url(published_pr)
    # Publication binds once. Retrying cannot replace the task's PR with a green sibling.
    if pr_url and contract == _PR.fullmatch(pr_url)[1]:
        with write_txn(conn):
            if _snapshot(conn, task_id) != snapshot:
                return False
            conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (pr_url, task_id))
        snapshot = (run_id, status, pr_url)
        contract = pr_url
    return snapshot, collect_acceptance(contract, published_pr)


def record_acceptance(conn, task_id, acceptance):
    """Called under complete_task's write_txn, before its terminal UPDATE."""
    from hermes_cli.kanban_db import _append_event
    snapshot, receipt = acceptance
    if _snapshot(conn, task_id) != snapshot:
        return False
    _append_event(conn, task_id, "pr_acceptance", receipt, run_id=snapshot[0])
    if not receipt["ok"]:
        detail = f"PR acceptance {receipt['classification']}: {receipt.get('detail', '')} {receipt['recovery']}"
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (detail, task_id))
    return receipt["ok"]
