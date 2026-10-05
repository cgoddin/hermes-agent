"""Real SQLite/CLI/tool/subprocess path for task-frozen Actions acceptance."""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect

SHA = "a" * 40
REPO = "acme/repo"
PR = "https://github.com/acme/repo/pull/7"
SPEC = {"version": 1, "repo": REPO, "base_branch": "main", "workflows": [
    {"path": ".github/workflows/ci.yml", "id": 10, "event": "pull_request",
     "jobs": [{"name": "tests", "steps": ["Run tests"]}]}]}


@pytest.fixture
def github(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    base_repo = {"full_name": REPO, "id": 1}
    head_repo = {"full_name": "contributor/repo", "id": 2}
    pr = {"number": 7, "state": "open", "merged": False,
          "head": {"sha": SHA, "ref": "feature", "repo": head_repo},
          "base": {"sha": "b" * 40, "ref": "main", "repo": base_repo}}
    run = {"id": 50, "workflow_id": 10, "run_number": 3, "run_attempt": 2,
           "event": "pull_request", "path": ".github/workflows/ci.yml", "head_sha": SHA,
           "head_branch": "feature", "repository": copy.deepcopy(base_repo), "head_repository": copy.deepcopy(head_repo),
           "pull_requests": [copy.deepcopy(pr)], "status": "completed", "conclusion": "success",
           "html_url": "https://untrusted.invalid/?token=dummy-secret"}
    job = {"id": 70, "name": "tests", "run_id": 50, "run_attempt": 2, "head_sha": SHA,
           "html_url": "https://github.com/untrusted/repo/actions/runs/1?token=dummy-secret",
           "status": "completed", "conclusion": "success", "steps": [
               {"name": "Run tests", "number": 1, "status": "completed", "conclusion": "success",
                "html_url": "https://untrusted.invalid/?token=dummy-secret"}]}
    routes = {
        f"repos/{REPO}/pulls/7": pr,
        f"repos/{REPO}/branches/main": {"name": "main", "protected": True},
        f"repos/{REPO}/branches/main/protection": {"required_status_checks": {
            "contexts": ["tests"], "checks": [{"context": "tests", "app_id": 15368}]}},
        f"repos/{REPO}/rules/branches/main?per_page=100": [[]],
        f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100": [{"total_count": 1, "workflow_runs": [run]}],
        f"repos/{REPO}/actions/runs/50": run,
        f"repos/{REPO}/actions/runs/50/attempts/2/jobs?per_page=100": [{"total_count": 1, "jobs": [job]}],
        f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&filter=latest": {"deny": True},
        "graphql": {"deny": True},
    }
    data, calls = tmp_path / "routes.json", tmp_path / "requests.jsonl"
    shim = tmp_path / "bin"
    shim.mkdir()
    executable = shim / "gh"
    executable.write_text(f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"routes = json.loads(pathlib.Path({str(data)!r}).read_text())\n"
        "endpoint = sys.argv[2]\n"
        f"with pathlib.Path({str(calls)!r}).open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "value = routes[endpoint]\n"
        "if isinstance(value, dict) and 'sequence' in value:\n"
        "    value = value['sequence'][0]\n"
        "    if len(routes[endpoint]['sequence']) > 1:\n"
        "        routes[endpoint]['sequence'].pop(0)\n"
        f"        pathlib.Path({str(data)!r}).write_text(json.dumps(routes))\n"
        "if isinstance(value, dict) and value.get('deny'):\n"
        "    print('gh: API refused (HTTP %s) Authorization: Bearer dummy-secret' % value.get('code', 403), file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(json.dumps(value))\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()

    def save():
        data.write_text(json.dumps(routes))

    return routes, run, job, save, calls


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("fault", [
    None, "no-protection", "external", "external-same-name", "external-missing", "external-failure", "rules-external", "rules-denied", "protection-denied",
    "external-success", "external-status-empty", "external-status-success", "external-status-newer-failure", "external-check-incomplete", "ruleset-classic-unavailable",
    "wrong-repo", "wrong-head-repo", "wrong-head", "wrong-base", "wrong-event", "wrong-branch", "wrong-path",
    "wrong-workflow", "wrong-pr", "wrong-linked-repo", "stale-green", "current-failure", "latest-attempt-failure",
    "run-pending", "run-skipped", "missing-job", "job-failure", "job-skipped", "job-pending", "wrong-job-head",
    "wrong-job-run", "wrong-job-attempt", "missing-step", "step-skipped", "step-failure", "step-pending",
    "duplicate-job", "duplicate-step", "pagination", "pagination-failure", "incomplete-runs", "incomplete-jobs", "no-expectations",
    "head-race", "policy-race", "attempt-race", "new-run-race",
    "required-jobs-steps", "second-job-missing", "second-job-failure", "second-step-missing", "second-step-failure", "second-step-skipped",
    "untrusted-run-conclusion", "untrusted-job-conclusion", "untrusted-step-conclusion",
])
def test_frozen_actions_gate_uses_complete_exact_provenance(github, fault):
    routes, run, job, save, calls = github
    runs_endpoint = f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100"
    jobs_endpoint = f"repos/{REPO}/actions/runs/50/attempts/2/jobs?per_page=100"
    protection = routes[f"repos/{REPO}/branches/main/protection"]
    spec = copy.deepcopy(SPEC)
    if fault == "no-protection" or fault == "no-expectations":
        routes[f"repos/{REPO}/branches/main"]["protected"] = False
    if fault in {"external", "external-missing", "external-failure", "external-success", "external-check-incomplete"}:
        protection["required_status_checks"]["checks"].append({"context": "external", "app_id": 123})
        if fault != "external":
            checks = [] if fault == "external-missing" else [{"id": 90, "name": "external", "app": {"id": 123},
                "head_sha": SHA, "status": "completed", "conclusion": "success" if fault == "external-success" else "failure",
                "html_url": "https://untrusted.invalid/?token=dummy-secret"}]
            routes[f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&filter=latest"] = [{"total_count": len(checks), "check_runs": checks}]
            routes[f"repos/{REPO}/commits/{SHA}/statuses?per_page=100"] = [[]]
            if fault == "external-check-incomplete":
                routes[f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&filter=latest"][0]["total_count"] += 1
    if fault in {"external-status-empty", "external-status-success", "external-status-newer-failure"}:
        protection["required_status_checks"]["contexts"].append("external")
        routes[f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&filter=latest"] = [{"total_count": 0, "check_runs": []}]
        statuses = [] if fault == "external-status-empty" else [{"id": 90, "context": "external", "state": "success",
            "target_url": "https://untrusted.invalid/?token=dummy-secret"}]
        if fault == "external-status-newer-failure":
            statuses.insert(0, {"id": 91, "context": "external", "state": "failure"})
        routes[f"repos/{REPO}/commits/{SHA}/statuses?per_page=100"] = [statuses]
    if fault == "external-same-name":
        protection["required_status_checks"]["checks"] = [{"context": "tests", "app_id": 123}]
    if fault == "rules-external":
        routes[f"repos/{REPO}/branches/main"]["protected"] = False
        routes[f"repos/{REPO}/rules/branches/main?per_page=100"] = [[{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "external", "integration_id": 123}]}}]]
    if fault == "rules-denied":
        routes[f"repos/{REPO}/rules/branches/main?per_page=100"] = {"deny": True}
    if fault == "protection-denied":
        routes[f"repos/{REPO}/branches/main/protection"] = {"deny": True}
    if fault == "ruleset-classic-unavailable":
        routes[f"repos/{REPO}/branches/main/protection"] = {"deny": True, "code": 404}
        routes[f"repos/{REPO}/rules/branches/main?per_page=100"] = [[{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "tests", "integration_id": 15368}]}}]]
    mutations = {
        "wrong-repo": (run["repository"], "full_name", "other/repo"),
        "wrong-head-repo": (run["head_repository"], "full_name", "other/repo"),
        "wrong-head": (run, "head_sha", "c" * 40), "wrong-base": (run["pull_requests"][0]["base"], "ref", "dev"),
        "wrong-event": (run, "event", "workflow_dispatch"), "wrong-branch": (run, "head_branch", "other"),
        "wrong-path": (run, "path", ".github/workflows/other.yml"), "wrong-workflow": (run, "workflow_id", 11),
        "wrong-pr": (run["pull_requests"][0], "number", 8),
        "wrong-linked-repo": (run["pull_requests"][0]["head"]["repo"], "id", 999),
        "run-pending": (run, "status", "in_progress"), "run-skipped": (run, "conclusion", "skipped"),
        "job-failure": (job, "conclusion", "failure"), "job-skipped": (job, "conclusion", "skipped"),
        "job-pending": (job, "status", "queued"), "wrong-job-head": (job, "head_sha", "c" * 40),
        "wrong-job-run": (job, "run_id", 49), "wrong-job-attempt": (job, "run_attempt", 1),
        "step-skipped": (job["steps"][0], "conclusion", "skipped"),
        "step-failure": (job["steps"][0], "conclusion", "failure"),
        "step-pending": (job["steps"][0], "status", "in_progress"),
        "untrusted-run-conclusion": (run, "conclusion", "https://untrusted.invalid/?token=dummy-secret"),
        "untrusted-job-conclusion": (job, "conclusion", "https://untrusted.invalid/?token=dummy-secret"),
        "untrusted-step-conclusion": (job["steps"][0], "conclusion", "https://untrusted.invalid/?token=dummy-secret"),
    }
    if fault in mutations:
        row, key, value = mutations[fault]
        row[key] = value
    if fault in {"stale-green", "current-failure", "latest-attempt-failure"}:
        older = copy.deepcopy(run)
        older.update(id=49, run_number=2)
        if fault == "stale-green":
            older["head_sha"] = "c" * 40
        if fault == "latest-attempt-failure":
            older.update(id=50, run_number=3, run_attempt=1)
            # The list reports only the current attempt. Earlier jobs cannot satisfy it.
        else:
            routes[runs_endpoint] = [{"total_count": 2, "workflow_runs": [older, run]}]
        run["conclusion"] = "failure"
    if fault == "missing-job":
        routes[jobs_endpoint] = [{"total_count": 0, "jobs": []}]
    if fault == "missing-step":
        job["steps"] = []
    if fault == "duplicate-job":
        routes[jobs_endpoint] = [{"total_count": 2, "jobs": [job, {**job, "id": 71}]}]
    if fault == "duplicate-step":
        job["steps"].append(copy.deepcopy(job["steps"][0]))
    if fault in {"pagination", "pagination-failure", "incomplete-runs", "incomplete-jobs"}:
        optional_runs = [{**run, "id": 1000 + i, "workflow_id": 1000 + i} for i in range(100)]
        optional_jobs = [{**job, "id": 2000 + i, "name": f"optional-{i}"} for i in range(100)]
        routes[runs_endpoint] = [{"total_count": 101, "workflow_runs": optional_runs}, {"total_count": 101, "workflow_runs": [run]}]
        routes[jobs_endpoint] = [{"total_count": 101, "jobs": optional_jobs}, {"total_count": 101, "jobs": [job]}]
        if fault == "incomplete-runs":
            routes[runs_endpoint].pop()
        if fault == "incomplete-jobs":
            routes[jobs_endpoint].pop()
        if fault == "pagination-failure":
            job["steps"][0]["conclusion"] = "failure"
    if fault == "head-race":
        endpoint = f"repos/{REPO}/pulls/7"
        changed = copy.deepcopy(routes[endpoint])
        changed["head"]["sha"] = "c" * 40
        routes[endpoint] = {"sequence": [routes[endpoint], changed]}
    if fault == "policy-race":
        endpoint = f"repos/{REPO}/branches/main/protection"
        routes[endpoint] = {"sequence": [protection, {"required_status_checks": None}]}
    if fault == "attempt-race":
        changed = {**run, "run_attempt": 3}
        routes[f"repos/{REPO}/actions/runs/50"] = {"sequence": [run, changed]}
    if fault == "new-run-race":
        changed = {**run, "id": 51, "run_number": 4, "conclusion": "failure"}
        routes[runs_endpoint] = {"sequence": [routes[runs_endpoint], [{"total_count": 2, "workflow_runs": [run, changed]}]]}
    if fault in {"required-jobs-steps", "second-job-missing", "second-job-failure", "second-step-missing", "second-step-failure", "second-step-skipped"}:
        spec["workflows"][0]["jobs"].append({"name": "lint", "steps": ["Lint", "Typecheck"]})
        spec["workflows"][0]["jobs"][0]["steps"].append("Verify coverage")
        job["steps"].append({"name": "Verify coverage", "number": 2, "status": "completed", "conclusion": "success"})
        lint = {**job, "id": 71, "name": "lint", "steps": [
            {"name": "Lint", "number": 1, "status": "completed", "conclusion": "success"},
            {"name": "Typecheck", "number": 2, "status": "completed", "conclusion": "success"}]}
        required_jobs = [job] if fault == "second-job-missing" else [job, lint]
        if fault == "second-job-failure":
            lint["conclusion"] = "failure"
        if fault == "second-step-missing":
            lint["steps"].pop()
        if fault in {"second-step-failure", "second-step-skipped"}:
            lint["steps"][1]["conclusion"] = "failure" if fault == "second-step-failure" else "skipped"
        optional_jobs = [{**job, "id": 2000 + i, "name": f"optional-{i}"} for i in range(100)]
        count = 100 + len(required_jobs)
        routes[jobs_endpoint] = [{"total_count": count, "jobs": optional_jobs}, {"total_count": count, "jobs": required_jobs}]
    save()
    before = datetime.now(timezone.utc)
    with connect() as conn:
        tid = kb.create_task(conn, title="CI acceptance", completion_contract=REPO,
                             acceptance_spec=None if fault == "no-expectations" else spec)
        frozen = json.loads(kb.get_task(conn, tid).acceptance_spec) if fault != "no-expectations" else None
        # Caller mutation and completion metadata cannot replace the frozen spec.
        spec["workflows"][0]["jobs"][0]["steps"] = ["Anything green"]
        ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": PR, "acceptance_spec": spec})
        assert ok is (fault in {None, "no-protection", "pagination", "external-success", "external-status-success", "required-jobs-steps"})
        assert (kb.get_task(conn, tid).status == "done") is ok
        receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
        assert receipt["ok"] is ok
        durable = "".join(r[0] or "" for r in conn.execute("SELECT payload FROM task_events WHERE task_id=?", (tid,)))
        durable += kb.get_task(conn, tid).last_failure_error or ""
        assert "dummy-secret" not in durable and "untrusted.invalid" not in durable
        assert "untrusted/repo" not in durable
        observed = datetime.fromisoformat(receipt["observed_at"])
        assert receipt["observed_at"].endswith("Z") and observed.utcoffset().total_seconds() == 0
        assert before <= observed <= datetime.now(timezone.utc)
        if fault in {"protection-denied", "rules-denied", "ruleset-classic-unavailable"}:
            assert receipt["classification"] == "capability" and receipt["hold"] is True
            assert "capability HOLD" in receipt["detail"]
        if fault in {"external-missing", "external-status-empty"}:
            external = next(c for c in receipt["checks"] if c.get("name") == "external")
            assert receipt["classification"] == external["classification"] == "missing"
        for check in receipt["checks"]:
            if "run_id" not in check:
                continue
            assert check["head_sha"] == SHA and check["run_attempt"] == 2
            assert check["conclusion"] in {"success", "failure", "skipped", "unknown"}
            if "workflow_id" in check:
                assert check["url"] == f"https://github.com/{REPO}/actions/runs/50/attempts/2"
                assert check["conclusion"] == ("unknown" if fault == "untrusted-run-conclusion" else run["conclusion"])
            else:
                assert check["url"] == f"https://github.com/{REPO}/actions/runs/50/job/{check['id']}"
                for step in check["steps"]:
                    assert step["head_sha"] == SHA and step["run_attempt"] == 2
                    assert step["url"] == (check["url"] + f"#step:{step['number']}:1" if step["number"] else check["url"])
                    assert step["conclusion"] in {"success", "failure", "skipped", "unknown", None}
        unknown_evidence = {
            "untrusted-run-conclusion": lambda: receipt["checks"][0],
            "untrusted-job-conclusion": lambda: receipt["checks"][1],
            "untrusted-step-conclusion": lambda: receipt["checks"][1]["steps"][0],
        }
        if fault in unknown_evidence:
            assert unknown_evidence[fault]()["conclusion"] == "unknown"
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    if fault in {"protection-denied", "rules-denied", "ruleset-classic-unavailable"}:
        assert all("/actions/" not in r[1] for r in requests)
    if ok:
        assert all(r[1] != "graphql" for r in requests)
        if fault in {"external-success", "external-status-success"}:
            assert any("/check-runs" in r[1] for r in requests)
            external = next(c for c in receipt["checks"] if c.get("name") == "external")
            assert external["classification"] == external["conclusion"] == "success"
        else:
            assert all("/check-runs" not in r[1] for r in requests)
        assert receipt["head_sha"] == SHA and receipt["checks"][0]["run_attempt"] == 2
        for job_spec in frozen["workflows"][0]["jobs"]:
            evidence = next(c for c in receipt["checks"] if c.get("name") == job_spec["name"])
            assert {s["name"] for s in evidence["steps"]} == set(job_spec["steps"])
            assert evidence["conclusion"] == "success" and all(s["conclusion"] == "success" for s in evidence["steps"])
    for request in requests:
        if "per_page=100" in request[1]:
            assert request[-2:] == ["--paginate", "--slurp"]


@pytest.mark.platforms("posix")
def test_cli_tool_completion_share_gate_but_review_handoff_does_not(github, monkeypatch):
    from tools import kanban_tools  # noqa: F401 - registers real handlers
    from tools.registry import registry

    routes, run, job, save, calls = github
    save()
    created = cli.run_slash("create 'CLI spec' --completion-contract acme/repo --acceptance-spec " + shlex.quote(json.dumps(SPEC)) + " --json")
    cli_tid = json.loads(created)["id"]
    result = json.loads(registry.dispatch("kanban_create", {"title": "Tool spec", "assignee": "default",
        "completion_contract": REPO, "acceptance_spec": json.dumps(SPEC)}))
    assert result["ok"], result
    tool_tid = result["task_id"]
    with connect() as conn:
        assert kb.get_task(conn, cli_tid).acceptance_spec == kb.get_task(conn, tool_tid).acceptance_spec
    # Review handoff is not terminal acceptance, even with red CI; neither
    # surface demands a reviewer receipt before the reviewer has received it.
    run["conclusion"] = "failure"
    save()
    assert "Requested review" in cli.run_slash(f"request-review {cli_tid} --summary 'Implementation ready'")
    handed_off = json.loads(registry.dispatch("kanban_request_review", {"task_id": tool_tid, "summary": "Implementation ready"}))
    assert handed_off["ok"], handed_off
    assert not calls.exists()
    assert "Completed" not in cli.run_slash(f"complete {cli_tid} --summary done --metadata " + shlex.quote(json.dumps({"published_pr": PR})))
    denied = json.loads(registry.dispatch("kanban_complete", {"task_id": tool_tid, "summary": "done", "metadata": {"published_pr": PR}}))
    assert not denied.get("ok", False), denied
    run["conclusion"] = "success"
    save()
    assert "Completed" in cli.run_slash(f"complete {cli_tid} --summary done --metadata " + shlex.quote(json.dumps({"published_pr": PR})))
    allowed = json.loads(registry.dispatch("kanban_complete", {"task_id": tool_tid, "summary": "done", "metadata": {"published_pr": PR}}))
    assert allowed["ok"], allowed
    with connect() as conn:
        for tid in (cli_tid, tool_tid):
            assert kb.get_task(conn, tid).status == "done"
        before = calls.read_text()
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="done")
        assert calls.read_text() == before
        for change in ({"version": 2}, {"repo": "other/repo"}, {"workflows": []}):
            with pytest.raises(ValueError):
                kb.create_task(conn, title="invalid", completion_contract=REPO, acceptance_spec={**SPEC, **change})


def _register_surface(surface, tid, contract, pr=PR):
    from hermes_cli.kanban_acceptance_registration import register_acceptance
    if surface == "cli":
        command = f"register-acceptance {tid} --contract {shlex.quote(json.dumps(contract))} --json"
        if pr is not None:
            command += " --pr " + shlex.quote(pr)
        output = cli.run_slash(command)
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"ok": False, "error": output}
    if surface == "tool":
        from tools import kanban_tools  # noqa: F401
        from tools.registry import registry
        return json.loads(registry.dispatch("kanban_register_acceptance", {
            "task_id": tid, "contract": contract, "published_pr": pr}))
    try:
        with connect() as conn:
            return register_acceptance(conn, tid, contract, published_pr=pr)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("surface", ["db", "cli", "tool"])
@pytest.mark.parametrize("origin", ["task", "project", "migrated"])
@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_existing_cards_register_audited_contract_before_real_completion(github, surface, origin, conclusion, tmp_path):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import projects_db
    routes, run, job, save, calls = github
    project_id = None
    if origin == "project":
        with projects_db.connect_closing() as projects:
            project_id = projects_db.create_project(projects, name="Acceptance project", primary_path=str(tmp_path / "repo"))
    with connect() as conn:
        tid = kb.create_task(conn, title="Existing PR card", completion_contract=REPO, project_id=project_id)
        assert kb.get_task(conn, tid).acceptance_spec is None
        if origin == "migrated":
            conn.execute("ALTER TABLE tasks DROP COLUMN acceptance_spec")
    conn.close()
    if origin == "migrated":
        kbc._INITIALIZED_PATHS.clear()
        with connect() as migrated:
            assert kb.get_task(migrated, tid).acceptance_spec is None
            assert kb.get_task(migrated, tid).title == "Existing PR card"
    contract = {"version": 1, "source": {"kind": "project" if origin == "project" else "task",
                "id": project_id or tid, "revision": 3}, "acceptance_spec": copy.deepcopy(SPEC)}
    save()
    landed = _register_surface(surface, tid, contract)
    assert landed.get("ok"), landed
    with connect() as conn:
        frozen = kb.get_task(conn, tid).acceptance_spec
        assert json.loads(frozen) == SPEC
        assert kb.get_task(conn, tid).completion_contract == PR
        event = next(e for e in kb.list_events(conn, tid) if e.kind == "acceptance_spec_registered")
        canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        assert event.payload["contract"] == contract
        assert event.payload["contract_digest"] == hashlib.sha256(canonical.encode()).hexdigest()
        assert event.payload["spec_digest"] == landed["spec_digest"] == hashlib.sha256(frozen.encode()).hexdigest()
        assert event.payload["actor"] and event.payload["base_branch"] == SPEC["base_branch"]
    before = calls.read_text()
    # Even identical repeat registration is rejected without fetching evidence.
    assert not _register_surface(surface, tid, contract).get("ok", False)
    contract["acceptance_spec"]["workflows"][0]["jobs"][0]["steps"] = ["Anything green"]
    assert not _register_surface(surface, tid, contract).get("ok", False)
    assert calls.read_text() == before
    run["conclusion"] = conclusion
    save()
    with connect() as conn:
        accepted = kb.complete_task(conn, tid, summary="Ready", metadata={"published_pr": PR, "acceptance_spec": contract["acceptance_spec"]})
        assert accepted is (conclusion == "success")
        assert (kb.get_task(conn, tid).status == "done") is accepted
        assert kb.get_task(conn, tid).acceptance_spec == frozen
        receipts = [e.payload for e in kb.list_events(conn, tid) if e.kind == "pr_acceptance"]
        assert receipts[-1]["spec_digest"] == landed["spec_digest"]
        assert receipts[-1]["ok"] is accepted
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "acceptance_spec_registered"]) == 1
    requests = [json.loads(line)[1] for line in calls.read_text().splitlines()]
    assert "graphql" not in requests
    if conclusion == "success":
        assert not any("/check-runs" in r for r in requests)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("surface", ["db", "cli", "tool"])
@pytest.mark.parametrize("fault", [
    "missing-contract", "local-only", "no-snapshot", "no-source", "wrong-task", "wrong-project", "bad-revision", "bad-version",
    "wrong-repo", "wrong-base", "wrong-pr", "no-pr", "empty-expectations", "creation-frozen", "terminal", "worker",
    "historical-evaluation", "after-evaluation", "evaluation-in-flight", "registration-race",
    "wrong-remote-repo", "wrong-remote-base", "api-denied",
])
def test_registration_cannot_change_or_supply_post_evaluation_expectations(github, surface, fault, monkeypatch):
    from hermes_cli import kanban_pr_acceptance as acceptance
    from hermes_cli.kanban_acceptance_registration import register_acceptance
    from hermes_cli.kanban_db_connect import write_txn
    routes, run, job, save, calls = github
    save()
    with connect() as conn:
        declaration = "local-only" if fault == "local-only" else REPO
        tid = kb.create_task(conn, title="Registration refusal", completion_contract=declaration,
                             acceptance_spec=SPEC if fault == "creation-frozen" else None)
        if fault == "missing-contract":
            with write_txn(conn):
                conn.execute("UPDATE tasks SET completion_contract=NULL WHERE id=?", (tid,))
        if fault == "terminal":
            with write_txn(conn):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        if fault == "historical-evaluation":
            with write_txn(conn):
                kb._append_event(conn, tid, "pr_acceptance", {"ok": False, "classification": "auth"})
    contract = {"version": 1, "source": {"kind": "task", "id": tid, "revision": 1}, "acceptance_spec": copy.deepcopy(SPEC)}
    changes = {
        "wrong-task": (contract["source"], "id", "t_other"),
        "wrong-project": (contract["source"], "kind", "project"),
        "bad-revision": (contract["source"], "revision", 0),
        "bad-version": (contract, "version", 2),
        "wrong-repo": (contract["acceptance_spec"], "repo", "other/repo"),
        "wrong-base": (contract["acceptance_spec"], "base_branch", "dev"),
        "empty-expectations": (contract["acceptance_spec"], "workflows", []),
    }
    if fault in changes:
        row, key, value = changes[fault]
        row[key] = value
    if fault == "no-source":
        contract.pop("source")
    if fault == "no-snapshot":
        contract = None
    if fault == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    if fault == "wrong-remote-repo":
        routes[f"repos/{REPO}/pulls/7"]["base"]["repo"]["full_name"] = "other/repo"
        save()
    if fault == "wrong-remote-base":
        routes[f"repos/{REPO}/pulls/7"]["base"]["ref"] = "dev"
        save()
    if fault == "api-denied":
        routes[f"repos/{REPO}/pulls/7"] = {"deny": True}
        save()
    if fault in {"after-evaluation", "evaluation-in-flight"}:
        real_api = acceptance._api
        def during_evaluation(endpoint, **kwargs):
            if endpoint == "graphql":
                with connect() as rival:
                    with pytest.raises(ValueError, match="evaluation has already begun"):
                        register_acceptance(rival, tid, contract, published_pr=PR)
            return real_api(endpoint, **kwargs)
        if fault == "evaluation-in-flight":
            monkeypatch.setattr(acceptance, "_api", during_evaluation)
        with connect() as conn:
            # Completion metadata cannot register a snapshot or downgrade the
            # legacy gate; the fixture refuses its GraphQL read.
            assert not kb.complete_task(conn, tid, summary="done", metadata={"published_pr": PR, "acceptance_spec": SPEC})
            assert kb.get_task(conn, tid).acceptance_spec is None
            assert any(e.kind == "pr_acceptance_started" for e in kb.list_events(conn, tid))
    if fault == "registration-race":
        real_api = acceptance._api
        def evaluation_wins(endpoint, **kwargs):
            if "/pulls/" in endpoint:
                with connect() as rival:
                    assert not kb.complete_task(rival, tid, summary="done", metadata={"published_pr": PR})
            return real_api(endpoint, **kwargs)
        monkeypatch.setattr(acceptance, "_api", evaluation_wins)
    pr = "https://github.com/other/repo/pull/7" if fault == "wrong-pr" else None if fault == "no-pr" else PR
    refused = _register_surface(surface, tid, contract, pr)
    assert not refused.get("ok", False), refused
    assert "dummy-secret" not in json.dumps(refused)
    with connect() as conn:
        task = kb.get_task(conn, tid)
        assert (task.acceptance_spec is not None) is (fault == "creation-frozen")
        assert not any(e.kind == "acceptance_spec_registered" for e in kb.list_events(conn, tid))
        if fault in {"missing-contract", "local-only"}:
            count = calls.read_text() if calls.exists() else ""
            assert kb.complete_task(conn, tid, summary="Local result")
            assert (calls.read_text() if calls.exists() else "") == count
