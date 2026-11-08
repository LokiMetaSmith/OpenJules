"""GitHub App webhook handler and FastAPI control plane."""

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from src.worker.dispatcher import BackgroundTasksDispatcher, TaskDispatcher, TaskPayload
from src.worker.executor import TaskExecutor

logger = logging.getLogger(__name__)

app = FastAPI(
    title="OpenJules Control Plane",
    description="Autonomous Sandboxed Coding Agent Service API",
    version="0.1.0",
)

executor = TaskExecutor()


def verify_github_signature(payload_body: bytes, signature_header: Optional[str], secret: str) -> bool:
    """Verifies that the webhook payload was sent from GitHub using HMAC-SHA256."""
    if not secret:
        # If secret is not configured, signature verification is skipped (development mode)
        return True
    if not signature_header:
        return False
    parts = signature_header.split("=", 1)
    if len(parts) != 2 or parts[0] != "sha256":
        return False
    expected_hash = hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hash, parts[1])


@app.get("/health")
def health_check():
    """Service health probe."""
    return {"status": "ok", "service": "openjules"}


@app.post("/api/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """Processes incoming GitHub webhooks for issues and comments."""
    body_bytes = await request.body()
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    if not verify_github_signature(body_bytes, x_hub_signature_256, webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload: Dict[str, Any] = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON")

    action = payload.get("action", "")
    repository = payload.get("repository", {})
    repo_full_name = repository.get("full_name", "")
    clone_url = repository.get("clone_url", "")
    default_branch = repository.get("default_branch", "main")
    installation = payload.get("installation", {})
    installation_id = installation.get("id")

    task_payload: Optional[TaskPayload] = None

    # Case 1: Issue labeled (e.g., openjules:run)
    if x_github_event == "issues" and action in ("labeled", "opened"):
        issue = payload.get("issue", {})
        labels = [l.get("name", "") for l in issue.get("labels", [])]
        label_triggered = "openjules:run" in labels or "openjules" in labels

        if label_triggered:
            task_payload = TaskPayload(
                issue_id=issue.get("number"),
                issue_title=issue.get("title", ""),
                issue_body=issue.get("body", "") or "",
                repo_full_name=repo_full_name,
                repo_clone_url=clone_url,
                default_branch=default_branch,
                installation_id=installation_id,
                sender=payload.get("sender", {}).get("login", ""),
            )

    # Case 2: Issue comment containing "/jules run" or "/openjules run"
    elif x_github_event == "issue_comment" and action == "created":
        comment = payload.get("comment", {})
        comment_body = comment.get("body", "")
        if "/jules run" in comment_body or "/openjules run" in comment_body:
            issue = payload.get("issue", {})
            task_payload = TaskPayload(
                issue_id=issue.get("number"),
                issue_title=issue.get("title", ""),
                issue_body=issue.get("body", "") or "",
                repo_full_name=repo_full_name,
                repo_clone_url=clone_url,
                default_branch=default_branch,
                installation_id=installation_id,
                sender=comment.get("user", {}).get("login", ""),
            )

    if task_payload:
        dispatcher = BackgroundTasksDispatcher(
            background_tasks=background_tasks,
            task_handler=executor.execute_task,
        )
        dispatcher.dispatch(task_payload)
        return {
            "status": "dispatched",
            "issue_id": task_payload.issue_id,
            "repo": task_payload.repo_full_name,
        }

    return {"status": "ignored", "reason": "No relevant OpenJules trigger in webhook payload"}
