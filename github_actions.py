"""GitHub Actions API helpers for Controller."""
import os
import logging
from typing import List

import requests

logger = logging.getLogger("github_actions")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", "monitor.yml")
TIMEOUT = 15


def _headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def configured() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def trigger_workflow(ref: str = "main") -> bool:
    if not configured():
        logger.error("GitHub not configured")
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    try:
        r = requests.post(url, headers=_headers(), json={"ref": ref}, timeout=TIMEOUT)
    except Exception as e:
        logger.error("Trigger exception: %s", e)
        return False
    if r.status_code in (201, 204):
        logger.info("Workflow triggered")
        return True
    logger.error("Trigger failed %s %s", r.status_code, r.text[:300])
    return False


def list_runs(status: str = None, per_page: int = 5) -> List[dict]:
    if not configured():
        return []
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/runs"
    params = {"per_page": per_page}
    if status:
        params["status"] = status
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
    except Exception as e:
        logger.error("list_runs exception: %s", e)
        return []
    if r.status_code != 200:
        logger.error("list_runs %s %s", r.status_code, r.text[:200])
        return []
    return r.json().get("workflow_runs") or []


def cancel_run(run_id: int) -> bool:
    if not configured():
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}/cancel"
    try:
        r = requests.post(url, headers=_headers(), timeout=TIMEOUT)
    except Exception as e:
        logger.error("cancel exception: %s", e)
        return False
    ok = r.status_code in (202, 204)
    logger.info("Cancel run %s -> %s", run_id, r.status_code)
    return ok


def cancel_all_in_progress() -> int:
    n = 0
    for status in ("in_progress", "queued"):
        for run in list_runs(status=status):
            if cancel_run(run["id"]):
                n += 1
    return n


def has_active_run() -> bool:
    try:
        return bool(list_runs(status="in_progress") or list_runs(status="queued"))
    except Exception:
        return False
