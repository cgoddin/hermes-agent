"""Once-only contract registration for PR cards predating Actions expectations.

Registration declares an immutable, versioned task/project snapshot, not CI
evidence. It must precede *any* acceptance evaluation, including failed reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

from hermes_cli.kanban_acceptance_spec import normalize_acceptance_spec
from hermes_cli.kanban_db_connect import write_txn

_ACTIVE = frozenset({"triage", "todo", "ready", "running", "blocked", "review"})


def _eligible(conn, task_id):
    row = conn.execute("SELECT current_run_id, status, completion_contract, acceptance_spec, assignee, project_id "
                       "FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or row["status"] not in _ACTIVE:
        raise ValueError("Acceptance registration requires an existing nonterminal task")
    if not row["completion_contract"] or row["completion_contract"] == "local-only":
        raise ValueError("Acceptance registration requires an existing explicit PR completion contract")
    if row["acceptance_spec"] is not None:
        raise ValueError("Acceptance expectations are already frozen; registration cannot be repeated or replaced")
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                    "('pr_acceptance_started', 'pr_acceptance', 'acceptance_spec_registered') LIMIT 1", (task_id,)).fetchone():
        raise ValueError("Acceptance evaluation has already begun; registration is closed")
    return dict(row)


def _contract_snapshot(value, task_id, row):
    doc = json.loads(value) if isinstance(value, str) else value
    if (not isinstance(doc, dict) or set(doc) != {"version", "source", "acceptance_spec"}
            or type(doc["version"]) is not int or doc["version"] != 1):
        raise ValueError("Registration requires a version 1 task/project contract snapshot with source and acceptance_spec")
    source = doc["source"]
    if (not isinstance(source, dict) or set(source) != {"kind", "id", "revision"}
            or type(source["revision"]) is not int or source["revision"] < 1):
        raise ValueError("Contract provenance requires kind, id, and a positive revision")
    identities = {"task": task_id, "project": row["project_id"]}
    kind = source["kind"]
    if not isinstance(kind, str) or kind not in identities or not identities[kind] or source["id"] != identities[kind]:
        raise ValueError("Contract provenance must identify this task or its linked project")
    spec = normalize_acceptance_spec(doc["acceptance_spec"], row["completion_contract"])
    if spec is None:
        raise ValueError("Contract snapshot must declare nonempty Actions expectations")
    canonical = json.dumps({"version": 1, "source": source, "acceptance_spec": json.loads(spec)},
                           sort_keys=True, separators=(",", ":"))
    return spec, canonical


def register_acceptance(conn, task_id, contract, *, published_pr=None):
    """CLI/native tool share this CAS; no completion metadata participates.

    A revision is the operator-declared version of the source contract. The
    complete snapshot and its content digest are retained in the task ledger;
    project edits cannot affect it and no external revision authority is inferred.
    """
    from hermes_cli import kanban_pr_acceptance as acceptance
    from hermes_cli.kanban_db import _append_event, redact_review_value
    from hermes_cli.kanban_pr_actions import _pr_identity
    from hermes_cli.profiles import current_profile_name

    if os.environ.get("HERMES_KANBAN_TASK"):
        raise ValueError("Acceptance registration is an orchestrator operation, not a task-worker completion operation")
    # Enforce the board/delegation fence and eligibility before doing network I/O.
    with write_txn(conn):
        snapshot = _eligible(conn, task_id)
    spec, canonical = _contract_snapshot(contract, task_id, snapshot)
    declaration = acceptance.validate_contract(snapshot["completion_contract"])
    declared_pr = acceptance._PR.fullmatch(declaration)
    url = declaration if declared_pr else published_pr
    match = acceptance._PR.fullmatch(url) if isinstance(url, str) else None
    if (not match or (declared_pr and published_pr is not None and published_pr != declaration)
            or (not declared_pr and match[1] != declaration)):
        raise ValueError("Supply a GitHub PR matching the persisted completion contract for repo/base validation")
    repo, number = match[1], int(match[2])
    try:
        home = acceptance._assignee_profile_home(snapshot["assignee"])
        identity = _pr_identity(acceptance._api(f"repos/{repo}/pulls/{number}", profile_home=home), repo, number)
    except acceptance._GateAuthError as exc:
        raise ValueError(f"Acceptance registration capability HOLD: {exc}") from None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        raise ValueError("Acceptance registration requires complete GitHub PR repo/base evidence; retry after restoring API access") from None
    if identity[1] != json.loads(spec)["base_branch"]:
        raise ValueError("PR base does not match the versioned contract snapshot")
    with write_txn(conn):
        if _eligible(conn, task_id) != snapshot:
            raise ValueError("Task ownership/contract changed during registration; retry")
        conn.execute("UPDATE tasks SET acceptance_spec=?, completion_contract=? WHERE id=?", (spec, url, task_id))
        _append_event(conn, task_id, "acceptance_spec_registered", redact_review_value({
            "actor": current_profile_name("user") or "user", "contract": json.loads(canonical),
            "contract_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "spec_digest": hashlib.sha256(spec.encode("utf-8")).hexdigest(),
            "pr_url": url, "head_sha": identity[0], "base_branch": identity[1],
        }), run_id=snapshot["current_run_id"])
    return {"ok": True, "task_id": task_id, "pr_url": url, "spec_digest": hashlib.sha256(spec.encode("utf-8")).hexdigest()}


def command_register_acceptance(args):
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_output import _print_json
    with connect_closing() as conn:
        result = register_acceptance(conn, args.task_id, args.contract, published_pr=args.pr)
    if args.json:
        _print_json(result)
    else:
        print(f"Registered frozen acceptance contract for {args.task_id}")
    return 0
