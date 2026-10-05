"""Actions evidence for frozen task specs, without relying on Checks permissions.

Repository policy is still authoritative. Only contexts backed by proven Actions
jobs can avoid a Checks read; third-party/app-pinned requirements never can.
"""
from __future__ import annotations

import re
from urllib.parse import quote

_ACTIONS_APP_ID = 15368  # github-actions on github.com (the only supported host)
_CONCLUSIONS = frozenset({"success", "failure", "neutral", "cancelled", "skipped",
                          "timed_out", "action_required", "stale", "startup_failure", "error", "pending"})


class _PolicyCapabilityHold(RuntimeError):
    """Policy could not be read; Actions cannot substitute for unknown requirements."""


def _conclusion(value):
    if value is None:
        return None
    return value if isinstance(value, str) and value in _CONCLUSIONS else "unknown"


def _action_url(repo, run_id, attempt, job_id=None):
    from hermes_cli.kanban_pr_acceptance import _REPO
    if not _REPO.fullmatch(repo) or any(part in {".", ".."} for part in repo.split("/")):
        raise ValueError("Invalid GitHub repository identity")
    root = f"https://github.com/{repo}/actions/runs/{_positive(run_id)}"
    attempt = _positive(attempt)
    return f"{root}/attempts/{attempt}" if job_id is None else f"{root}/job/{_positive(job_id)}"


def _all_pages(api, endpoint, key):
    pages = api(endpoint, paginate=True)
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing pagination evidence")
    rows = [row for page in pages for row in page[key]]
    counts = {page["total_count"] for page in pages}
    if (any(type(page["total_count"]) is not int or page["total_count"] < 0 for page in pages)
            or len(counts) != 1 or len(rows) != pages[0]["total_count"] or len({r["id"] for r in rows}) != len(rows)):
        raise ValueError("Incomplete or inconsistent pagination")
    for row in rows:
        _positive(row["id"])
    return rows


def _positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Missing Actions identity")
    return value


def _policy(api, repo, branch):
    from hermes_cli.kanban_pr_acceptance import _GateAuthError
    try:
        return _read_policy(api, repo, branch)
    except _GateAuthError as exc:
        # `protected` includes rulesets, whose classic protection endpoint may
        # return 404. That is NOT proof of absent classic requirements/access.
        raise _PolicyCapabilityHold(str(exc)) from None


def _read_policy(api, repo, branch):
    encoded = quote(branch, safe="")
    info = api(f"repos/{repo}/branches/{encoded}")
    if info["name"] != branch or type(info["protected"]) is not bool:
        raise ValueError("Incomplete protection evidence")
    required = set()
    if info["protected"]:
        protection = api(f"repos/{repo}/branches/{encoded}/protection")
        checks = protection["required_status_checks"]
        if checks is not None:
            # Older protection settings have contexts without app bindings.
            required.update((r["context"], r.get("app_id")) for r in checks["checks"])
            bound = {name for name, _ in required}
            required.update((c, None) for c in checks["contexts"] if c not in bound)
    pages = api(f"repos/{repo}/rules/branches/{encoded}?per_page=100", paginate=True)
    if not isinstance(pages, list) or not pages:
        raise ValueError("Incomplete rules evidence")
    for page in pages:
        for rule in page:
            if rule["type"] == "required_status_checks":
                required.update((r["context"], r.get("integration_id"))
                                for r in rule["parameters"]["required_status_checks"])
            if rule["type"] == "workflows":
                # A ruleset workflow requirement needs its own provenance support.
                raise ValueError("Unsupported required workflow policy")
    for context, app in required:
        if not isinstance(context, str) or not context or len(context) > 256 or any(ord(c) < 32 for c in context):
            raise ValueError("Invalid required context")
        if app is not None and (type(app) is not int or app < -1):
            raise ValueError("Invalid required app")
    return info["protected"], required


def _pr_identity(pr, repo, number):
    if pr["number"] != number or pr["base"]["repo"]["full_name"] != repo:
        raise ValueError("Wrong PR repository")
    if pr["state"] not in {"open", "closed"} or (pr["state"] == "closed" and not pr.get("merged")):
        raise ValueError("Closed PR")
    sha = pr["head"]["sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Missing exact PR head")
    return (sha, pr["base"]["ref"], pr["head"]["ref"], pr["head"]["repo"]["full_name"],
            _positive(pr["base"]["repo"]["id"]), _positive(pr["head"]["repo"]["id"]))


def _latest(rows, workflow, identity, repo, number):
    sha, base, branch, head_repo, base_id, head_id = identity
    # Do not discard a malformed/wrong-provenance latest run to resurrect an
    # older green one. Identity/event/branch choose the candidates; validate
    # repository, SHA, workflow path and PR linkage only AFTER selecting latest.
    candidates = [r for r in rows if r["workflow_id"] == workflow["id"] and
                  r["event"] == workflow["event"] and r["head_branch"] == branch]
    if not candidates:
        raise ValueError("Missing expected workflow")
    for run in candidates:
        _positive(run["id"])
        _positive(run["run_number"])
        _positive(run["run_attempt"])
    run = max(candidates, key=lambda r: (r["run_number"], r["id"], r["run_attempt"]))
    if (run["repository"]["full_name"] != repo or run["head_repository"]["full_name"] != head_repo or
            run["repository"]["id"] != base_id or run["head_repository"]["id"] != head_id or
            run["head_sha"] != sha or run["path"] != workflow["path"]):
        raise ValueError("Wrong workflow provenance")
    if workflow["event"] == "pull_request":
        linked = [p for p in run["pull_requests"] if p["number"] == number]
        if len(linked) != 1 or linked[0]["head"]["sha"] != sha or linked[0]["head"]["ref"] != branch or linked[0]["base"]["ref"] != base:
            raise ValueError("Wrong PR linkage")
        if linked[0]["base"]["repo"]["id"] != run["repository"]["id"] or linked[0]["head"]["repo"]["id"] != run["head_repository"]["id"]:
            raise ValueError("Wrong linked repository")
    return run


def _success(row):
    return row["status"] == "completed" and row["conclusion"] == "success"


def _run_stamp(run):
    # Detail/list responses need not have identical optional fields. Pin only
    # the identity and result that participate in the acceptance predicate.
    return tuple(run[k] for k in ("id", "workflow_id", "run_number", "run_attempt", "status", "conclusion"))


def collect_actions(api, spec, repo, number, receipt):
    from hermes_cli.kanban_pr_acceptance import _classify
    pr_endpoint = f"repos/{repo}/pulls/{number}"
    identity = _pr_identity(api(pr_endpoint), repo, number)
    sha, base = identity[:2]
    receipt.update(head_sha=sha, spec_version=spec["version"])
    if base != spec["base_branch"]:
        raise ValueError("PR base differs from frozen acceptance spec")
    policy = _policy(api, repo, base)
    required = policy[1]
    receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
    endpoint = f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100"
    rows = _all_pages(api, endpoint, "workflow_runs")
    proven_contexts, outcomes, selected = set(), [], []
    for workflow in spec["workflows"]:
        run = _latest(rows, workflow, identity, repo, number)
        rid, attempt = run["id"], run["run_attempt"]
        detail_endpoint = f"repos/{repo}/actions/runs/{rid}"
        detail = api(detail_endpoint)
        if _run_stamp(_latest([detail], workflow, identity, repo, number)) != _run_stamp(run):
            raise ValueError("Run changed during collection")
        jobs = _all_pages(api, f"repos/{repo}/actions/runs/{rid}/attempts/{attempt}/jobs?per_page=100", "jobs")
        outcome = "success" if _success(run) else "pending" if run["status"] != "completed" else "failure"
        outcomes.append(outcome)
        receipt["checks"].append({"workflow_id": workflow["id"], "path": workflow["path"],
                                  "event": workflow["event"], "run_id": rid, "run_attempt": attempt,
                                  "head_sha": sha, "classification": outcome,
                                  "url": _action_url(repo, rid, attempt),
                                  "conclusion": _conclusion(run["conclusion"])})
        # Validate provenance on every page, including conditional jobs. Only
        # frozen required jobs/steps need success; the aggregate run still must
        # succeed, and repository-required contexts retain their policy gates.
        for job in jobs:
            if job["run_id"] != rid or job["run_attempt"] != attempt or job["head_sha"] != sha:
                raise ValueError("Wrong job attempt/head")
        for expected in workflow["jobs"]:
            matches = [j for j in jobs if j["name"] == expected["name"]]
            if len(matches) != 1:
                raise ValueError("Missing or ambiguous required job")
            job = matches[0]
            job_url = _action_url(repo, rid, attempt, job["id"])
            good = _success(job)
            step_evidence = []
            for name in expected["steps"]:
                steps = [s for s in job["steps"] if s["name"] == name]
                passed = len(steps) == 1 and _success(steps[0])
                good = good and passed
                step = steps[0] if len(steps) == 1 else None
                step_number = _positive(step["number"]) if step else None
                step_evidence.append({"name": name, "classification": "success" if passed else "failure",
                    "conclusion": _conclusion(step["conclusion"]) if step else None,
                    "number": step_number, "head_sha": sha, "run_id": rid, "run_attempt": attempt,
                    "url": f"{job_url}#step:{step_number}:1" if step else job_url})
            classification = "success" if good else "failure"
            outcomes.append(classification)
            receipt["checks"].append({"name": expected["name"], "id": job["id"],
                "run_id": rid, "run_attempt": attempt, "head_sha": sha,
                "classification": classification, "conclusion": _conclusion(job["conclusion"]),
                "url": job_url, "steps": step_evidence})
            if good and outcome == "success":
                proven_contexts.add(expected["name"])
        selected.append((workflow, run))

    unresolved = {(c, a) for c, a in required if c not in proven_contexts or a not in (None, -1, _ACTIONS_APP_ID)}
    if unresolved:
        runs = _all_pages(api, f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", "check_runs")
        status_pages = api(f"repos/{repo}/commits/{sha}/statuses?per_page=100", paginate=True)
        if not isinstance(status_pages, list) or not status_pages:
            raise ValueError("Missing statuses pagination")
        statuses = [s for page in status_pages for s in page]
        for context, app in sorted(unresolved, key=str):
            matches = [r for r in runs if r["name"] == context and (app in (None, -1) or r["app"]["id"] == app)]
            legacy = [s for s in statuses if s["context"] == context] if app in (None, -1) else []
            if legacy:
                matches.append({**max(legacy, key=lambda s: s["id"]), "sha": sha})
            if not matches:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "head_sha": sha, "classification": "missing"})
            for check in matches:
                _positive(check["id"])
                is_run = "conclusion" in check
                result = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, result, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"], "head_sha": sha,
                                          "classification": classification, "conclusion": _conclusion(result)})
    # Pin the attempt and latest run again; a rerun/new push invalidates old green.
    fresh = _all_pages(api, endpoint, "workflow_runs")
    for workflow, run in selected:
        latest = _latest(fresh, workflow, identity, repo, number)
        detail = _latest([api(f"repos/{repo}/actions/runs/{run['id']}")], workflow, identity, repo, number)
        if _run_stamp(latest) != _run_stamp(run) or _run_stamp(detail) != _run_stamp(run):
            raise ValueError("Latest run/attempt changed")
    if _pr_identity(api(pr_endpoint), repo, number) != identity or _policy(api, repo, base) != policy:
        receipt.update(classification="stale", detail="PR or required policy changed while collecting evidence; retry.")
        return receipt
    receipt["classification"] = next((o for o in outcomes if o != "success"), "missing" if not outcomes else "success")
    receipt["ok"] = receipt["classification"] == "success"
    return receipt
