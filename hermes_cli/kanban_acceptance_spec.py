"""Frozen CI expectations declared at creation or explicit pre-evaluation registration."""
from __future__ import annotations

import json
import re


def normalize_acceptance_spec(value, contract: str) -> str | None:
    if value is None:
        return None
    from hermes_cli.kanban_pr_acceptance import _PR, _REPO
    spec = json.loads(value) if isinstance(value, str) else value
    if not isinstance(spec, dict) or set(spec) != {"version", "repo", "base_branch", "workflows"}:
        raise ValueError("acceptance_spec requires version, repo, base_branch and workflows")
    match = _PR.fullmatch(contract)
    repo = match[1] if match else contract
    if (type(spec["version"]) is not int or spec["version"] != 1 or not isinstance(spec["repo"], str)
            or not _REPO.fullmatch(spec["repo"]) or spec["repo"] != repo):
        raise ValueError("acceptance_spec version/repository does not match the PR contract")

    def text(s):
        if not isinstance(s, str) or not s.strip() or len(s) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in s):
            raise ValueError("acceptance_spec names must be bounded nonempty text")

    def items(xs):
        if not isinstance(xs, list) or not xs or len(xs) > 100:
            raise ValueError("acceptance_spec requires nonempty bounded lists")

    text(spec["base_branch"])
    items(spec["workflows"])
    paths, ids = set(), set()
    for workflow in spec["workflows"]:
        if not isinstance(workflow, dict) or set(workflow) != {"path", "id", "event", "jobs"}:
            raise ValueError("workflow requires path, id, event and jobs")
        path, wid = workflow["path"], workflow["id"]
        if not isinstance(path, str) or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", path):
            raise ValueError("workflow must identify a versioned workflow path")
        if type(wid) is not int or wid <= 0 or path in paths or wid in ids:
            raise ValueError("workflow identities must be positive and unique")
        paths.add(path)
        ids.add(wid)
        if not isinstance(workflow["event"], str) or workflow["event"] not in {"pull_request", "push"}:
            raise ValueError("workflow event must be pull_request or push")
        items(workflow["jobs"])
        names = set()
        for job in workflow["jobs"]:
            if not isinstance(job, dict) or set(job) != {"name", "steps"}:
                raise ValueError("required job needs name and steps")
            text(job["name"])
            if job["name"] in names:
                raise ValueError("required job names must be unique")
            names.add(job["name"])
            items(job["steps"])
            for step in job["steps"]:
                text(step)
            if len(set(job["steps"])) != len(job["steps"]):
                raise ValueError("required steps must be unique")
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))
