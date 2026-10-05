"""Parent PR context must not disable duplicate-publication protection on a child."""
import json
import subprocess
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_parent_pr as parent_pr
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli.kanban_db_connect import connect


PARENT_PR = "https://github.com/synthetic/project/pull/7"
OWN_PR = "https://github.com/synthetic/project/pull/70"
HEAD = "4710b617" + "a" * 32
MERGE = "0c9845" + "b" * 34


@pytest.fixture
def github(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    for name in ("developer", "devops", "closer"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / ".env").write_text(f"GH_TOKEN=synthetic-{name}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GH_TOKEN", "synthetic-ambient")
    state = {"pr": {"state": "closed", "merged": True, "head": {"sha": HEAD},
                    "merge_commit_sha": MERGE}, "calls": [], "error": None,
             "now": int(time.time()), "monotonic": 1000.0}
    monkeypatch.setattr(time, "time", lambda: state["now"])
    monkeypatch.setattr(parent_pr, "time", SimpleNamespace(monotonic=lambda: state["monotonic"]))
    monkeypatch.setattr(parent_pr, "_VERIFIED_PRS", OrderedDict())

    def run(command, **kwargs):
        assert command == ["gh", "api", "repos/synthetic/project/pulls/7", "--hostname", "github.com"]
        state["calls"].append(kwargs["env"]["GH_TOKEN"])
        assert state["calls"][-1] != "synthetic-ambient"
        if state["error"] is not None:
            raise state["error"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(state["pr"]))

    # Keep profile/environment construction real without patching subprocess globally.
    monkeypatch.setattr(acceptance, "subprocess", SimpleNamespace(
        run=run, DEVNULL=subprocess.DEVNULL, CalledProcessError=subprocess.CalledProcessError,
        SubprocessError=subprocess.SubprocessError))
    kb.init_db()
    return state


def _assert_guard_and_tick(conn, child, *, allowed, board=None):
    assert dispatch.check_respawn_guard(conn, child) == (None if allowed else "active_pr")
    result = dispatch.dispatch_once(conn, dry_run=True, board=board)
    assert (child in [tid for tid, _, _ in result.spawned]) is allowed
    return result


def _assert_tick_reads(conn, child, github, *, allowed=True, reads=0, board=None):
    before = len(github["calls"])
    result = _assert_guard_and_tick(conn, child, allowed=allowed, board=board)
    assert len(github["calls"]) - before == reads
    if not allowed and kb.get_task(conn, child).status == "ready":
        assert dict(result.respawn_guarded)[child] == "active_pr"


def _update_run_metadata(conn, run_id, values):
    metadata = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])
    metadata.update(values)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run_id))


def _native_parent(conn, clock, *, early_comment=False, check_pending=False):
    now = clock["now"]
    clock["now"] = now - 100
    parent = kb.create_task(conn, title="parent publication", assignee="developer")
    claimed = kb.claim_task(conn, parent)
    developer_run = claimed.current_run_id
    clock["now"] = now - 98
    child = kb.create_task(conn, title="follow-up", assignee="developer")
    kb.link_tasks(conn, parent, child)
    if early_comment:
        clock["now"] = now - 95
        kb.add_comment(conn, child, author="developer", body=f"Parent publication: {PARENT_PR}.")
    if check_pending:
        assert kb.get_task(conn, parent).status == "running"
        _assert_guard_and_tick(conn, child, allowed=False)
        assert clock["calls"] == []
    clock["now"] = now - 90
    assert kb.request_review(conn, parent, reviewer="devops", summary="Published for review",
                             metadata={"published_pr": PARENT_PR, "head": HEAD},
                             expected_run_id=developer_run)
    clock["now"] = now - 80
    reviewed = kb.claim_review_task(conn, parent)
    review_run = reviewed.current_run_id
    if check_pending:
        _assert_guard_and_tick(conn, child, allowed=False)
        assert clock["calls"] == []
    clock["now"] = now - 70
    assert kb.complete_task(conn, parent, summary="Independently reviewed and merged",
                            metadata={"review_outcome": "approved_and_merged_dev", "published_pr": PARENT_PR,
                                      "head": HEAD, "merge_commit": MERGE},
                             expected_run_id=review_run)
    clock["now"] = now
    return parent, child, developer_run, review_run


@pytest.mark.parametrize("scenario", [
    "valid", "completion_tie", "completion_future", "child_comment_tie", "comment_future",
    "open", "own", "mixed", "bad_review_metadata", "bad_developer_metadata", "bad_handoff",
])
def test_pre_review_child_comment_requires_completed_parent_at_evaluation(github, scenario):
    with connect() as conn:
        parent, child, developer_run, review_run = _native_parent(
            conn, github, early_comment=True, check_pending=scenario == "valid")
        now = github["now"]
        mutations = {
            "completion_tie": ("UPDATE task_runs SET ended_at=? WHERE id=?", (now, review_run)),
            "completion_future": ("UPDATE task_runs SET ended_at=? WHERE id=?", (now + 1, review_run)),
            "child_comment_tie": ("UPDATE tasks SET created_at=? WHERE id=?", (now - 95, child)),
            "comment_future": ("UPDATE task_comments SET created_at=? WHERE task_id=?", (now + 1, child)),
            "bad_review_metadata": ("UPDATE task_runs SET metadata='[null]' WHERE id=?", (review_run,)),
            "bad_developer_metadata": ("UPDATE task_runs SET metadata='[null]' WHERE id=?", (developer_run,)),
            "bad_handoff": ("UPDATE task_events SET payload='not-json' WHERE task_id=? "
                            "AND kind='review_requested'", (parent,)),
            "own": ("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome,metadata) "
                    "VALUES (?,'developer','review',?,?,'review_requested',?)",
                    (child, now - 50, now - 40, json.dumps({"published_pr": PARENT_PR, "head": HEAD}))),
            "mixed": ("UPDATE task_comments SET body=? WHERE task_id=?", (PARENT_PR + " " + OWN_PR, child)),
        }
        with kb.write_txn(conn):
            conn.execute(*mutations.get(scenario, ("SELECT 1", ())))
        if scenario == "open":
            github["pr"]["state"] = "open"
        allowed = scenario == "valid"
        result = _assert_guard_and_tick(conn, child, allowed=allowed)
        if not allowed:
            assert dict(result.respawn_guarded)[child] == "active_pr"
        reads = {"valid": 1, "mixed": 1, "open": 2}.get(scenario, 0)
        assert github["calls"] == ["synthetic-developer"] * reads


@pytest.mark.parametrize("punctuation", [".", ",", ";", ":", "!"])
def test_parent_pr_in_normal_sentence_is_context(github, punctuation):
    with connect() as conn:
        _, child, _, _ = _native_parent(conn, github)
        kb.add_comment(conn, child, author="developer",
                       body=f"Parent work was merged at {PARENT_PR}{punctuation} Continue the follow-up.")
        assert dispatch.check_respawn_guard(conn, child) is None
        assert github["calls"] == ["synthetic-developer"]


@pytest.mark.parametrize("suffix", [".diff", "/files", "?x", "#x", "abc", "1"])
def test_parent_pr_with_genuine_suffix_stays_guarded(github, suffix):
    with connect() as conn:
        _, child, _, _ = _native_parent(conn, github)
        kb.add_comment(conn, child, author="developer",
                       body=f"Parent reference: {PARENT_PR}{suffix} Continue the follow-up.")
        assert dispatch.check_respawn_guard(conn, child) == "active_pr"
        assert github["calls"] == []


@pytest.mark.parametrize("target,field,value", [
    ("valid", None, None),
    ("review_metadata", "review_outcome", None),
    ("review_metadata", "review_outcome", "changes_requested"),
    ("review_metadata", "published_pr", OWN_PR),
    ("review_metadata", "head", "c" * 40),
    ("review_metadata", "merge_commit", None),
    ("review_metadata", "merge_commit", "not-a-sha"),
    ("developer_metadata", "published_pr", OWN_PR),
    ("developer_metadata", "head", "c" * 40),
    ("review", "profile", "developer"),
    ("review", "outcome", "crashed"),
    ("review", "status", "running"),
    ("review", "ended_at", None),
    ("review", "ended_at", "future"),
    ("review", "ended_at", "child_tie"),
    ("review", "ended_at", "comment_tie"),
    ("review", "started_at", "review_end_tie"),
    ("review", "started_at", "developer_end_tie"),
    ("developer", "profile", "other"),
    ("developer", "outcome", "completed"),
    ("developer", "status", "done"),
    ("developer", "ended_at", None),
    ("developer", "ended_at", "future"),
    ("developer", "ended_at", "review_start_tie"),
    ("developer", "ended_at", "developer_start_tie"),
    ("handoff", "created_at", "future"),
    ("handoff", "created_at", "review_start_tie"),
    ("parent", "status", "ready"),
    ("parent", "assignee", "developer"),
    ("child", "created_at", "before_parent_merge"),
    ("child", "created_at", "future"),
    ("child", "created_at", "comment_tie"),
    ("unlinked", None, None),
    ("no_native_handoff", None, None),
    ("newer_run", None, None),
])
def test_only_native_independent_parent_evidence_is_a_remote_candidate(github, target, field, value):
    with connect() as conn:
        parent, child, developer_run, review_run = _native_parent(conn, github)
        times = {"future": github["now"] + 10, "child_tie": github["now"] - 60,
                 "comment_tie": github["now"], "review_end_tie": github["now"] - 70,
                 "developer_end_tie": github["now"] - 90,
                 "before_parent_merge": github["now"] - 85,
                 "review_start_tie": github["now"] - 80,
                 "developer_start_tie": github["now"] - 100}
        with kb.write_txn(conn):
            if target.endswith("_metadata"):
                run_id = review_run if target == "review_metadata" else developer_run
                metadata = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])
                metadata[field] = value
                conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run_id))
            elif target in {"review", "developer", "parent", "child"}:
                table, row_id = {"review": ("task_runs", review_run),
                                 "developer": ("task_runs", developer_run),
                                 "parent": ("tasks", parent), "child": ("tasks", child)}[target]
                conn.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (times.get(value, value), row_id))
            elif target == "handoff":
                conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='review_requested'",
                             (times[value], parent))
            else:
                mutations = {
                    "valid": ("SELECT 1", ()),
                    "unlinked": ("DELETE FROM task_links WHERE parent_id=?", (parent,)),
                    "no_native_handoff": ("DELETE FROM task_events WHERE task_id=? AND kind='review_requested'", (parent,)),
                    "newer_run": ("INSERT INTO task_runs (task_id,profile,status,started_at) VALUES (?,'devops','running',?)",
                                  (parent, int(time.time()))),
                }
                conn.execute(*mutations[target])
        kb.add_comment(conn, child, author="developer", body=PARENT_PR)
        allowed = (target == "valid"
                   or (target, field, value) == ("review", "ended_at", "child_tie")
                   or (target, field, value) == ("child", "created_at", "before_parent_merge"))
        assert dispatch.check_respawn_guard(conn, child) == (None if allowed else "active_pr")
        assert github["calls"] == (["synthetic-developer"] if allowed else [])


@pytest.mark.parametrize("scenario", [
    "parent_only", "mixed_parent_first", "mixed_own_first", "older_own", "handoff_before_parent",
    "handoff_before_own", "handoff_after_own", "same_second_handoff", "open", "unmerged", "stale_head",
    "stale_merge", "incomplete", "invalid_json_shape", "timeout", "auth", "missing_profile", "profile_switch",
    "url_suffix", "url_query", "http", "different_case",
    "own_publication_same_url", "future_comment", "future_handoff", "newest_blocking_comment",
])
def test_parent_exception_preserves_url_handoff_and_dispatch_contracts(github, monkeypatch, scenario):
    now = int(time.time())
    with connect() as conn:
        parent, child, _, _ = _native_parent(conn, github)
        bodies = {
            "mixed_parent_first": [PARENT_PR + " " + OWN_PR],
            "mixed_own_first": [OWN_PR + " " + PARENT_PR],
            "older_own": [OWN_PR, PARENT_PR],
            "handoff_before_parent": [OWN_PR, PARENT_PR],
            "handoff_before_own": [PARENT_PR, OWN_PR],
            "url_suffix": [PARENT_PR + "/files"],
            "url_query": [PARENT_PR + "?diff=split"],
            "http": [PARENT_PR.replace("https:", "http:")],
            "different_case": [PARENT_PR.replace("github.com", "GitHub.com")],
            "handoff_after_own": [PARENT_PR + " " + OWN_PR],
            "same_second_handoff": [OWN_PR],
            "future_handoff": [OWN_PR],
            "newest_blocking_comment": [OWN_PR, OWN_PR, PARENT_PR],
        }.get(scenario, [PARENT_PR])
        for i, body in enumerate(bodies):
            comment = kb.add_comment(conn, child, author="developer", body=body)
            with kb.write_txn(conn):
                conn.execute("UPDATE task_comments SET created_at=? WHERE id=?", (now - 20 + i * 10, comment))
        handoff_times = {"handoff_before_parent": now - 15, "handoff_before_own": now - 15,
                         "handoff_after_own": now - 5, "same_second_handoff": now - 20,
                         "future_handoff": now + 10, "newest_blocking_comment": now - 15}
        if scenario in handoff_times:
            with kb.write_txn(conn):
                kb._append_event(conn, child, "assigned", {"from": "developer", "assignee": "closer"})
                conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='assigned'",
                              (handoff_times[scenario], child))
        if scenario == "future_comment":
            with kb.write_txn(conn):
                conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (now + 10, child))
        if scenario == "own_publication_same_url":
            with kb.write_txn(conn):
                conn.execute("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome,metadata) "
                             "VALUES (?,'developer','review',?,?,'review_requested',?)",
                             (child, now - 50, now - 40, json.dumps({"published_pr": PARENT_PR, "head": HEAD})))
        github["pr"].update({
            "open": {"state": "open"}, "unmerged": {"merged": False},
            "stale_head": {"head": {"sha": "c" * 40}}, "stale_merge": {"merge_commit_sha": "c" * 40},
        }.get(scenario, {}))
        if scenario in {"incomplete", "invalid_json_shape"}:
            github["pr"] = {} if scenario == "incomplete" else []
        github["error"] = {
            "timeout": subprocess.TimeoutExpired("gh", 30),
            "auth": subprocess.CalledProcessError(1, "gh", stderr="HTTP 403"),
        }.get(scenario)
        if scenario == "missing_profile":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET assignee='missing' WHERE id=?", (child,))
        allowed = scenario in {"parent_only", "handoff_before_parent", "handoff_after_own", "profile_switch"}
        assert dispatch.check_respawn_guard(conn, child) == (None if allowed else "active_pr")
        if scenario == "profile_switch":
            github["calls"].clear()
            for profile in ("developer", "closer", "developer"):
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (profile, child))
                assert dispatch.check_respawn_guard(conn, child) is None
            assert github["calls"] == ["synthetic-closer", "synthetic-developer"]
        if scenario in {"url_suffix", "url_query", "http", "different_case", "missing_profile", "same_second_handoff",
                        "own_publication_same_url", "future_comment"}:
            assert github["calls"] == []
        # Exercise the real ready-lane dispatcher, not only its predicate.
        result = dispatch.dispatch_once(conn, dry_run=True)
        assert (child in [task_id for task_id, _, _ in result.spawned]) is allowed
        if not allowed and scenario != "missing_profile":
            assert dict(result.respawn_guarded)[child] == "active_pr"


@pytest.mark.parametrize("scenario", [
    "repeated", "head", "merge", "review_receipt", "developer_receipt", "handoff_receipt",
    "invalid_receipt", "own", "mixed", "new_run", "profile", "credentials", "scope",
    "ttl", "ttl_timeout", "ttl_auth", "ttl_open", "ttl_malformed", "boards", "bounded",
])
def test_verified_receipts_recheck_local_evidence_and_scope_remote_permission(github, tmp_path, monkeypatch, scenario):
    with connect() as conn:
        parent, child, developer_run, review_run = _native_parent(conn, github, early_comment=True)
        _assert_tick_reads(conn, child, github, reads=1)
        # Neither repeated guard calls nor whole ticks slide the original TTL.
        for _ in range(3):
            github["monotonic"] += parent_pr._VERIFIED_PR_TTL_SECONDS / 4
            _assert_tick_reads(conn, child, github)
        if scenario == "repeated":
            assert github["calls"] == ["synthetic-developer"]
            return

        if scenario in {"head", "merge", "review_receipt", "developer_receipt", "handoff_receipt"}:
            updates = {
                "head": (review_run, {"head": "c" * 40}),
                "merge": (review_run, {"merge_commit": "d" * 40}),
                "review_receipt": (review_run, {"note": "synthetic-private-context"}),
                "developer_receipt": (developer_run, {"note": "synthetic-private-context"}),
            }
            if scenario == "handoff_receipt":
                with kb.write_txn(conn):
                    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='review_requested'",
                                 (json.dumps({"implementer": "developer", "reviewer": "devops",
                                              "summary": "synthetic-private-context"}), parent))
            else:
                _update_run_metadata(conn, *updates[scenario])
            if scenario in {"head", "merge"}:
                if scenario == "head":
                    _update_run_metadata(conn, developer_run, {"head": "c" * 40})
                # A valid but different native SHA must not reuse the old proof.
                _assert_tick_reads(conn, child, github, allowed=False, reads=2)
                github["pr"].update({"head": {"sha": "c" * 40}} if scenario == "head" else
                                    {"merge_commit_sha": "d" * 40})
            _assert_tick_reads(conn, child, github, reads=1)
            cached = repr(parent_pr._VERIFIED_PRS)
            assert "synthetic-private-context" not in cached
            assert "synthetic-developer" not in cached
            return

        if scenario in {"invalid_receipt", "own", "mixed", "new_run"}:
            original = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (review_run,)).fetchone()[0]
            mutations = {
                "invalid_receipt": ("UPDATE task_runs SET metadata='not-json' WHERE id=?", (review_run,)),
                "own": ("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome,metadata) "
                        "VALUES (?,'developer','review',?,?,'review_requested',?)",
                        (child, github["now"] - 50, github["now"] - 40,
                         json.dumps({"published_pr": PARENT_PR, "head": HEAD}))),
                "mixed": ("UPDATE task_comments SET body=? WHERE task_id=?", (PARENT_PR + " " + OWN_PR, child)),
                "new_run": ("INSERT INTO task_runs (task_id,profile,status,started_at) VALUES (?,'devops','running',?)",
                            (parent, github["now"])),
            }
            with kb.write_txn(conn):
                changed = conn.execute(*mutations[scenario]).lastrowid
            _assert_tick_reads(conn, child, github, allowed=False)
            restorations = {
                "invalid_receipt": ("UPDATE task_runs SET metadata=? WHERE id=?", (original, review_run)),
                "own": ("DELETE FROM task_runs WHERE id=?", (changed,)),
                "mixed": ("UPDATE task_comments SET body=? WHERE task_id=?", (PARENT_PR, child)),
                "new_run": ("DELETE FROM task_runs WHERE id=?", (changed,)),
            }
            with kb.write_txn(conn):
                conn.execute(*restorations[scenario])
            _assert_tick_reads(conn, child, github, reads=0 if scenario == "mixed" else 1)
            return

        if scenario.startswith("ttl"):
            github["monotonic"] += parent_pr._VERIFIED_PR_TTL_SECONDS / 4
            failures = {
                "ttl_timeout": subprocess.TimeoutExpired("gh", 30),
                "ttl_auth": subprocess.CalledProcessError(1, "gh", stderr="HTTP 403"),
            }
            github["error"] = failures.get(scenario)
            original = github["pr"]
            if scenario in {"ttl_open", "ttl_malformed"}:
                github["pr"] = {**original, "state": "open"} if scenario == "ttl_open" else []
            if scenario != "ttl":
                # An expired success cannot override an unavailable/different API
                # response, nor may an error become permission on the next tick.
                for _ in range(2):
                    _assert_tick_reads(conn, child, github, allowed=False, reads=2)
                github["error"], github["pr"] = None, original
            _assert_tick_reads(conn, child, github, reads=1)
            _assert_tick_reads(conn, child, github)
            return

        if scenario == "profile":
            for profile, reads in (("developer", 0), ("closer", 1), ("developer", 1)):
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (profile, child))
                _assert_tick_reads(conn, child, github, reads=reads)
            assert github["calls"] == ["synthetic-developer", "synthetic-closer", "synthetic-developer"]
            return

        if scenario == "credentials":
            home = Path(acceptance._assignee_profile_home("developer"))
            (home / ".env").write_text("GH_TOKEN=synthetic-rotated\n")
            _assert_tick_reads(conn, child, github, reads=1)
            assert github["calls"][-1] == "synthetic-rotated"
            return

        if scenario == "scope":
            from agent import secret_scope
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override

            db_path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
            monkeypatch.setenv("HERMES_KANBAN_DB", db_path)
            was_active = secret_scope.is_multiplex_active()
            secret_scope.set_multiplex_active(True)
            try:
                for name in ("developer", "closer", "developer"):
                    home = Path(acceptance._assignee_profile_home(name))
                    home_token = set_hermes_home_override(str(home))
                    secrets = secret_scope.build_profile_secret_scope(home)
                    secret_token = secret_scope.set_secret_scope(secrets, profile_home=str(home))
                    try:
                        _assert_tick_reads(conn, child, github, reads=1)
                    finally:
                        secret_scope.reset_secret_scope(secret_token)
                        reset_hermes_home_override(home_token)
            finally:
                secret_scope.set_multiplex_active(was_active)
            # The scoped caller changed, but authentication always belongs to the
            # child's assignee, never the ambient launch/closer token.
            assert github["calls"] == ["synthetic-developer"] * 4
            return

        # Clone exact IDs, timestamps and native receipts into a distinct board:
        # without DB identity in the key even a private-board auth failure leaks.
        assert scenario in {"boards", "bounded"}
        if scenario == "bounded":
            monkeypatch.setattr(parent_pr, "_VERIFIED_PR_CACHE_LIMIT", 1)
        source = Path(next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"))
        with connect(tmp_path / "other-board.db") as other:
            conn.backup(other)
            monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "other-board.db"))
            github["error"] = subprocess.CalledProcessError(1, "gh", stderr="HTTP 403")
            _assert_tick_reads(other, child, github, allowed=False, reads=2)
            github["error"] = None
            _assert_tick_reads(other, child, github, reads=1)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(source))
        _assert_tick_reads(conn, child, github, reads=1 if scenario == "bounded" else 0)
        assert len(parent_pr._VERIFIED_PRS) <= parent_pr._VERIFIED_PR_CACHE_LIMIT
