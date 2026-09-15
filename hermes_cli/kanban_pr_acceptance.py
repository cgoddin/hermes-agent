"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

Note (2026-09-15): a scoped PAT cannot read
``baseRef.branchProtectionRule`` (GraphQL FORBIDDEN). The old gate treated any
GraphQL error as an infra failure and wedged every PR-completion. Now:
- the GraphQL error on that field is NO-SIGNAL (not an acceptance failure);
- PR identity/state/merge evidence comes from the REST pulls endpoint;
- required checks come from REST branch rules + the tolerated GraphQL probe;
- with zero repository-required checks, a PR the REST API verifies as merged
  (``merged``/``merged_at`` set) is accepted; open PRs stay fail-closed.
Also accepts ``metadata.published_pr`` in the short ``OWNER/REPO#N`` form.
"""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
_PR_SHORT = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def canonical_pr_url(value: str | None) -> str | None:
    """Accept both canonical published_pr forms and return the full URL."""
    if not isinstance(value, str):
        return None
    short = _PR_SHORT.fullmatch(value.strip())
    if short:
        return f"https://github.com/{short[1]}/pull/{short[2]}"
    if _PR.fullmatch(value.strip()):
        return value.strip()
    return None


def _api(endpoint: str, *, query: str | None = None,
         tolerate_graphql_errors: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=True)
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        if tolerate_graphql_errors:
            # A scoped GraphQL FORBIDDEN (e.g. branchProtectionRule on a fine-grained
            # PAT) is a NO-SIGNAL, not an acceptance failure; callers degrade to REST.
            return value
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def _pages(endpoint: str):
    """Paginate a list endpoint across gh versions.

    gh >= 2.52 ``--paginate --slurp`` prints ONE array of per-page arrays.
    Older gh (2.46 on this host) prints each page as a separate JSON document.
    Normalise both to a list of per-page payloads.
    """
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if _gh_supports_slurp():
        command += ["--paginate", "--slurp"]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=30, check=True)
        return json.loads(result.stdout)
    command += ["--paginate"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=60, check=True)
    decoder, index, out = json.JSONDecoder(), 0, []
    text = result.stdout.strip()
    while index < len(text):
        while index < len(text) and text[index] in " \n\r\t":
            index += 1
        if index >= len(text):
            break
        value, index = decoder.raw_decode(text, index)
        out.append(value)
    return out


_SLURP_OK: bool | None = None


def _gh_supports_slurp() -> bool:
    global _SLURP_OK
    if _SLURP_OK is None:
        try:
            result = subprocess.run(["gh", "--version"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10)
            match = re.search(r"gh version (\d+)\.(\d+)", result.stdout)
            major, minor = (int(match[1]), int(match[2])) if match else (0, 0)
            _SLURP_OK = (major, minor) >= (2, 52)
        except (OSError, subprocess.SubprocessError, ValueError):
            _SLURP_OK = False
    return _SLURP_OK


def _graphql_protection(repo: str, number: int) -> dict:
    """Best-effort requiredStatusChecks probe; any error degrades to no rules."""
    owner, name = repo.split("/")
    query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
        json.dumps(owner), json.dumps(name), number)
    try:
        payload = _api("graphql", query=query, tolerate_graphql_errors=True)
        data = (payload.get("data") or {}).get("repository") or {}
        return (((data.get("pullRequest") or {}).get("baseRef") or {})
                .get("branchProtectionRule")) or {}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        return {}


def collect_acceptance(contract: str, published_pr: str | None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        declared = _PR.fullmatch(contract)
        url = contract if declared else canonical_pr_url(published_pr)
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and url and published_pr and canonical_pr_url(published_pr) != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        # Identity/state/merge evidence: REST pulls (the GraphQL branchProtectionRule
        # field is FORBIDDEN on this token and is queried separately, tolerantly).
        pr = _api(f"repos/{repo}/pulls/{number}")
        sha, branch = pr["head"]["sha"], pr["base"]["ref"]
        receipt["head_sha"] = sha
        merged = bool(pr.get("merged")) or pr.get("merged_at") is not None
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"open", "closed"}:
            raise ValueError("PR is closed or current head is unavailable")
        if pr["state"] == "closed" and not merged:
            raise ValueError("PR is closed or current head is unavailable")
        if pr["state"] == "open" and pr.get("mergeable_state") == "dirty":
            raise ValueError("PR has a merge conflict with the base branch")
        required = {(r["context"], (r.get("app") or {}).get("databaseId"))
                    for r in _graphql_protection(repo, number).get("requiredStatusChecks", [])}
        rules = _pages(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100")
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            if merged:
                # Zero repository-required checks + REST-verified merge = acceptance.
                receipt.update(classification="success", ok=True,
                               detail="No repository-required checks are configured; PR verified merged via REST "
                                      "(branchProtectionRule GraphQL errors are treated as no-signal).")
                return receipt
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _pages(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest")
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _pages(f"repos/{repo}/commits/{sha}/statuses?per_page=100") for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}")
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
