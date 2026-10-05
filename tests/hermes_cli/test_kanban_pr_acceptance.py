"""Exact-head lifecycle and auth invariants using SQLite and offline GitHub evidence."""
import json
import os
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_pr_acceptance as acceptance


def _stub_gh(monkeypatch, run):
    # Replace this module's binding, not subprocess.run process-wide: profile
    # resolution and environment construction may launch unrelated helpers.
    monkeypatch.setattr(acceptance, "subprocess", SimpleNamespace(
        run=run, DEVNULL=subprocess.DEVNULL,
        CalledProcessError=subprocess.CalledProcessError,
        SubprocessError=subprocess.SubprocessError))


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    def run(command, **kwargs):
        endpoint = command[2]
        state["requests"].append(endpoint)
        sha = state["head"]
        if endpoint == "graphql":
            value = {"data": {"repository": {"pullRequest": {
                "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": "required", "app": {"databaseId": 1}}]}}}}}}
        elif "/rules/branches/" in endpoint:
            assert command[-2:] == ["--paginate", "--slurp"]
            value = [[]]
        elif "/check-runs" in endpoint:
            assert command[-2:] == ["--paginate", "--slurp"]
            run = {"id": 42, "name": "required", "head_sha": sha,
                   "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                   "html_url": "https://github.com/acme/repo/actions/runs/42"}
            if state.get("stale"):
                run["head_sha"] = "b" * 40
            runs = [] if state.get("missing") else [run]
            value = [{"total_count": 100 + len(runs), "check_runs": [
                {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
            if state.get("incomplete"):
                value.pop()
            if state.get("wrong_app") and runs:
                runs[0]["app"] = {"id": 2}
            if state.get("race"):
                state["race"]()
            if state.get("head_change"):
                state["head"] = "b" * 40
        elif "/statuses" in endpoint:
            assert command[-2:] == ["--paginate", "--slurp"]
            value = [[]]
        elif "/pulls/" in endpoint:
            value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
        else:
            raise AssertionError("unexpected acceptance endpoint")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value))

    _stub_gh(monkeypatch, run)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    return state


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change", "incomplete", "wrong_app"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the profile and API phase — not a retryable `infra` failure."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    probe = tmp_path / "gh_identity_ok"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib\n"
                  "assert os.environ.get('GH_TOKEN') == 'b-token'\n"
                  "assert os.environ.get('GH_CONFIG_DIR') != '/nonexistent/launch/gh'\n"
                  f"pathlib.Path({str(probe)!r}).write_text('ok')\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "graphql" in receipts[-1]["detail"]
        assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")
    assert probe.read_text() == "ok"


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    cached = tmp_path / "launch-xdg" / "gh"
    cached.mkdir(parents=True)
    (cached / "hosts.yml").write_text("github.com:\n  oauth_token: dummy-cached-token\n")
    probe = tmp_path / "gh_identity_ok"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"assert os.environ.get('GH_CONFIG_DIR') == {str(assignee_home / 'gh')!r}\n"
                  "assert 'GH_TOKEN' not in os.environ and 'GITHUB_TOKEN' not in os.environ\n"
                  f"pathlib.Path({str(probe)!r}).write_text('ok')\n"
                  "sys.exit(4)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    assert probe.read_text() == "ok"


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]
    for name in ("dummy-secret\nAuthorization: Bearer dummy-secret", "x" * 1000,
                 "https://untrusted.invalid/?token=dummy-secret"):
        refused = acceptance.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7", name)
        assert refused["classification"] == "auth"
        assert "<invalid>" in refused["detail"]
        assert name not in refused["detail"] and "dummy-secret" not in refused["detail"]


_UNTRUSTED = "Authorization: Bearer dummy-secret; https://untrusted.invalid/?token=dummy-secret"


@pytest.mark.parametrize("body,exit_code,stderr,classification", [
    pytest.param({"errors": [{"type": code, "message": _UNTRUSTED}]}, 0, "", "auth", id=f"http200-{code}")
    for code in ("FORBIDDEN", "UNAUTHORIZED", "NOT_FOUND")
] + [
    pytest.param({"errors": [{"extensions": {"code": "FORBIDDEN"}, "message": _UNTRUSTED}]},
                 0, "", "auth", id="extensions-forbidden"),
    pytest.param({"errors": [{"type": "FORBIDDEN", "message": "Insufficient permission: " + _UNTRUSTED}]},
                 0, "", "auth", id="insufficient-permission"),
    pytest.param({"errors": [{"message": "Resource not accessible by personal access token"}]},
                 0, "", "auth", id="known-permission-message"),
    pytest.param({"errors": [{"type": "FORBIDDEN", "message": _UNTRUSTED}]},
                 1, _UNTRUSTED, "auth", id="exit1-structured-stdout"),
    pytest.param(None, 1, "gh: FORBIDDEN\n", "auth", id="exit1-forbidden"),
    pytest.param(None, 1, "gh: GraphQL: UNAUTHORIZED (repository.pullRequest)\n", "auth", id="exit1-unauthorized"),
    pytest.param(None, 1, "GraphQL: NOT_FOUND (repository)\n", "auth", id="exit1-not-found"),
    pytest.param(None, 1, "gh: Resource not accessible by integration (repository.pullRequest.baseRef.branchProtectionRule)\n",
                 "auth", id="exit1-gh-api-permission"),
    pytest.param(None, 1, "gh: Resource not accessible by personal access token (repository)\n",
                 "auth", id="exit1-token-permission"),
    *[pytest.param(None, 1, f"gh: API refused (HTTP {code}) " + _UNTRUSTED, "auth", id=f"http{code}")
      for code in (401, 403, 404)],
    pytest.param({"errors": [{"type": "INTERNAL", "message": "FORBIDDEN " + _UNTRUSTED}]},
                 0, "", "infra", id="unknown-graphql"),
    pytest.param({"errors": [{"type": "INTERNAL", "message": _UNTRUSTED}]},
                 1, "gh: FORBIDDEN\n", "infra", id="structured-unknown-wins"),
    pytest.param({"errors": [{"type": "FORBIDDEN"}, {"type": "INTERNAL"}]},
                 0, "", "infra", id="mixed-errors"),
    pytest.param({"errors": [{"message": ["FORBIDDEN"]}]}, 0, "", "infra", id="malformed-error"),
    pytest.param(None, 1, "gh: unknown field 'FORBIDDEN' " + _UNTRUSTED, "infra", id="stderr-message-injection"),
    pytest.param(None, 1, "gh: FORBIDDEN\n" + _UNTRUSTED, "infra", id="stderr-extra-line"),
    pytest.param(None, 1, "gh: https://untrusted.invalid/FORBIDDEN", "infra", id="stderr-url-injection"),
    pytest.param(None, 1, "gh: connection reset " + _UNTRUSTED, "infra", id="transport"),
    pytest.param(None, None, _UNTRUSTED, "infra", id="timeout"),
])
def test_auth_diagnosis_is_allowlisted_and_safe_to_persist(tmp_path, monkeypatch, body, exit_code, stderr, classification):
    """Real completion persists only an allowlisted diagnosis, never gh output."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    profile = home / "profiles" / "wm-auth"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("GITHUB_TOKEN=dummy-profile-token\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GH_TOKEN", "dummy-ambient-token")
    calls = []

    def run(command, **kwargs):
        calls.append(command[2])
        env = kwargs["env"]
        assert env.get("GITHUB_TOKEN") == "dummy-profile-token"
        assert "GH_TOKEN" not in env
        output = json.dumps(body) if body is not None else ""
        if exit_code is None:
            raise subprocess.TimeoutExpired(command, 30, stderr=stderr)
        if exit_code:
            raise subprocess.CalledProcessError(exit_code, command, output=output, stderr=stderr)
        return subprocess.CompletedProcess(command, 0, stdout=output)

    _stub_gh(monkeypatch, run)
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="offline auth", completion_contract="acme/repo", assignee="wm-auth")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        task = kb.get_task(conn, tid)
        assert task.status != "done"
        payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0]
    receipt = json.loads(payload)
    assert receipt["classification"] == classification and not receipt["ok"]
    assert calls == ["graphql"]  # no permissive policy/Actions fallback after refusal
    durable = payload + (task.last_failure_error or "")
    for forbidden in ("dummy-secret", "dummy-profile-token", "dummy-ambient-token", "Authorization", "untrusted.invalid"):
        assert forbidden not in durable
    if classification == "auth":
        assert "'wm-auth'" in receipt["detail"] and "graphql" in receipt["detail"]

    # HTTP diagnostics must not reflect even a valid-shaped path's dynamic data.
    if "HTTP " in stderr:
        for endpoint in ("repos/private/secret/rules/branches/secret?token=dummy-secret",
                         "https://untrusted.invalid/?token=dummy-secret", "graphql?token=dummy-secret"):
            with pytest.raises(acceptance._GateAuthError) as caught:
                acceptance._api(endpoint, profile_home=str(profile))
            reason = str(caught.value)
            assert "dummy-secret" not in reason and "untrusted.invalid" not in reason
            assert "private" not in reason and "secret" not in reason and "?" not in reason
            assert len(reason) < 100


@pytest.mark.platforms("posix")
def test_github_token_only_assignee_is_isolated_across_multiplex_reads(tmp_path, monkeypatch):
    """A→B→A uses real profile resolution and real children without env dumps."""
    from agent.secret_scope import is_multiplex_active, set_multiplex_active

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    a, b = (home / "profiles" / name for name in ("a", "b"))
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / ".env").write_text("GH_TOKEN=dummy-a-token\n")
    (b / ".env").write_text("GITHUB_TOKEN=dummy-b-token\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GH_TOKEN", "dummy-ambient-token")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "ambient-gh"))
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib\n"
                  "name = pathlib.Path(os.environ['HERMES_HOME']).name\n"
                  "if name == 'a':\n"
                  "    assert os.environ.get('GH_TOKEN') == 'dummy-a-token'\n"
                  "    assert 'GITHUB_TOKEN' not in os.environ\n"
                  "elif name == 'b':\n"
                  "    assert os.environ.get('GITHUB_TOKEN') == 'dummy-b-token'\n"
                  "    assert 'GH_TOKEN' not in os.environ\n"
                  "else:\n"
                  "    raise AssertionError('wrong profile')\n"
                  f"assert os.environ.get('GH_CONFIG_DIR') != {str(tmp_path / 'ambient-gh')!r}\n"
                  "print(json.dumps({'errors': [{'type': 'FORBIDDEN'}]}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    prior = is_multiplex_active()
    set_multiplex_active(True)
    try:
        for assignee in ("a", "b", "a"):
            receipt = acceptance.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7", assignee)
            assert receipt["classification"] == "auth"
            assert f"'{assignee}'" in receipt["detail"] and "FORBIDDEN" in receipt["detail"]
            assert "dummy-" not in json.dumps(receipt)
    finally:
        set_multiplex_active(prior)
