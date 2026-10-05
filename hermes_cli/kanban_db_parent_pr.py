"""Native parent-publication provenance and short-lived authenticated merge proof."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import hermes_home_key


# A terminal merge is immutable, but the profile's access is not. Periodically
# reauthenticate; never renew a receipt merely because another tick used it.
_VERIFIED_PR_TTL_SECONDS = 300.0
_VERIFIED_PR_CACHE_LIMIT = 128


@dataclass(frozen=True)
class _VerifiedPR:
    evidence: tuple
    expires_at: float


_VERIFIED_PRS: OrderedDict[tuple, _VerifiedPR] = OrderedDict()
_VERIFIED_PR_LOCK = threading.Lock()


def _board_identity(conn: sqlite3.Connection) -> tuple | None:
    filename = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    if not filename:
        # Separate in-memory databases can have identical row/run IDs.
        return None
    path = Path(filename).resolve()
    stat = path.stat()
    return (str(path), stat.st_dev, stat.st_ino)


def _profile_identity(home: str) -> tuple:
    from hermes_cli import kanban_pr_acceptance as acceptance

    stat = Path(home).stat()
    env = acceptance._gh_env(home)
    # Store a fingerprint, never the profile's token, in the process cache.
    auth = {key: env.get(key) for key in
            ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "HOME", "XDG_CONFIG_HOME")}
    digest = hashlib.sha256(json.dumps(auth, sort_keys=True).encode()).digest()
    config = (Path(env["GH_CONFIG_DIR"]) if env.get("GH_CONFIG_DIR") else
              Path(env["XDG_CONFIG_HOME"]) / "gh" if env.get("XDG_CONFIG_HOME") else
              Path(env["HOME"]) / ".config" / "gh")
    try:
        hosts = (config / "hosts.yml").stat()
        login = (hosts.st_dev, hosts.st_ino, hosts.st_mtime_ns, hosts.st_ctime_ns, hosts.st_size)
    except FileNotFoundError:
        login = None
    return (hermes_home_key(), hermes_home_key(home), stat.st_dev, stat.st_ino, digest, login)


def _remember(subject: tuple | None, receipt: _VerifiedPR) -> None:
    if subject is None:
        return
    with _VERIFIED_PR_LOCK:
        now = time.monotonic()
        for key in list(_VERIFIED_PRS):
            if _VERIFIED_PRS[key].expires_at <= now:
                del _VERIFIED_PRS[key]
        _VERIFIED_PRS[subject] = receipt
        _VERIFIED_PRS.move_to_end(subject)
        while len(_VERIFIED_PRS) > _VERIFIED_PR_CACHE_LIMIT:
            _VERIFIED_PRS.popitem(last=False)


def is_merged_parent_pr(conn: sqlite3.Connection, task_id: str, url: str,
                        assignee: str | None, *, created_at: int, now: int) -> bool:
    """Recheck native evidence every tick; reuse only matching, unexpired remote proof."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_pr_acceptance as acceptance

    try:
        board = _board_identity(conn)
    except OSError:
        return False
    subject = (board, task_id, url) if board is not None else None
    with _VERIFIED_PR_LOCK:
        # Removing first invalidates old permission on *any* failed local check,
        # including a profile/evidence change followed by restoration (A->B->A).
        previous = _VERIFIED_PRS.pop(subject, None)
    match = acceptance._PR.fullmatch(url)
    if not match or not assignee or created_at > now:
        return False
    own_runs = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ?", (task_id,))
    if any(kb._json_dict(run["metadata"]).get("published_pr") == url for run in own_runs):
        return False
    parents = conn.execute(
        "SELECT r.id, r.task_id, r.started_at, r.ended_at, r.metadata, child.created_at "
        "FROM task_links l JOIN tasks child ON child.id = l.child_id "
        "JOIN tasks p ON p.id = l.parent_id "
        "JOIN task_runs r ON r.id = (SELECT id FROM task_runs WHERE task_id = p.id "
        "ORDER BY id DESC LIMIT 1) "
        "WHERE l.child_id = ? AND p.status = 'done' AND p.assignee = 'devops' "
        "AND r.profile = 'devops' AND r.status = 'done' "
        "AND r.outcome = 'completed' AND r.started_at < r.ended_at "
        # A child comment normally precedes review/merge. Only evaluation must
        # follow completion strictly; the child must strictly predate its comment.
        "AND r.ended_at < ? AND child.created_at < ?",
        (task_id, now, created_at),
    ).fetchall()
    for parent in parents:
        metadata = kb._json_dict(parent["metadata"])
        head, merge = metadata.get("head"), metadata.get("merge_commit")
        if (metadata.get("published_pr") != url
                or metadata.get("review_outcome") != "approved_and_merged_dev"
                or not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
                or not isinstance(merge, str) or not re.fullmatch(r"[0-9a-f]{40}", merge)):
            continue
        developer = conn.execute(
            "SELECT id, profile, status, outcome, started_at, ended_at, metadata FROM task_runs "
            "WHERE task_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (parent["task_id"], parent["id"]),
        ).fetchone()
        if (developer is None or developer["profile"] != "developer"
                or developer["status"] != "review" or developer["outcome"] != "review_requested"
                or developer["started_at"] is None or developer["ended_at"] is None
                or not developer["started_at"] < developer["ended_at"] < parent["started_at"]):
            continue
        published = kb._json_dict(developer["metadata"])
        if published.get("published_pr") != url or published.get("head") != head:
            continue
        handoff = conn.execute(
            "SELECT id, created_at, payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = 'review_requested' AND created_at >= ? AND created_at < ? "
            "ORDER BY id DESC LIMIT 1",
            (parent["task_id"], developer["id"], developer["ended_at"], parent["started_at"]),
        ).fetchone()
        provenance = kb._json_dict(handoff["payload"]) if handoff else {}
        if provenance.get("implementer") != "developer" or provenance.get("reviewer") != "devops":
            continue
        try:
            home = acceptance._assignee_profile_home(assignee)
            # Metadata and handoff summaries can contain private worker context.
            # Bind the complete native receipt without retaining its raw payload.
            native = hashlib.sha256(json.dumps(
                [tuple(parent), tuple(developer), tuple(handoff)]).encode()).digest()
            evidence = (assignee, _profile_identity(home), url, head, merge, native)
            if (previous is not None and previous.evidence == evidence
                    and time.monotonic() < previous.expires_at):
                _remember(subject, previous)
                return True
            current = acceptance._api(f"repos/{match[1]}/pulls/{match[2]}", profile_home=home)
            verified = (current["state"] == "closed" and current["merged"] is True
                        and current["head"]["sha"] == head and current["merge_commit_sha"] == merge)
            if verified:
                _remember(subject, _VerifiedPR(evidence, time.monotonic() + _VERIFIED_PR_TTL_SECONDS))
            return verified
        except (acceptance._GateAuthError, OSError, subprocess.SubprocessError,
                ValueError, KeyError, TypeError, IndexError):
            # Expired receipts and failed reads never grant permission; retry next
            # tick so recovery from authentication/API errors needs no restart.
            return False
    return False
