"""GitHub and Radicle Webhook handler and FastAPI control plane."""

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.api.ui_routes import record_ui_task, ui_router
from src.api.ws import ws_manager, ws_router
from src.worker.dispatcher import BackgroundTasksDispatcher, TaskDispatcher, TaskPayload
from src.worker.executor import TaskExecutor

logger = logging.getLogger(__name__)

app = FastAPI(
    title="OpenJules Control Plane & Dashboard",
    description="Autonomous Sandboxed Coding Agent Service API & 3-Pane UI for GitHub and Radicle",
    version="0.3.0",
)

executor = TaskExecutor()

# Include dashboard and UI endpoints
app.include_router(ui_router)
app.include_router(ws_router)

# Mount static files
static_dir = Path(__file__).parent.parent / "ui" / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def serve_dashboard():
    """Serves the intuitive 3-pane web interface."""
    index_file = static_dir / "index.html"
    if index_file.is_file():
        return FileResponse(str(index_file))
    return {"message": "OpenJules Dashboard"}


def verify_github_signature(payload_body: bytes, signature_header: Optional[str], secret: str) -> bool:
    """Verifies that the webhook payload was sent from GitHub using HMAC-SHA256."""
    if not secret:
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
    return {"status": "ok", "service": "openjules", "forges": ["github", "radicle"]}


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
                forge="github",
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
                forge="github",
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
            "forge": "github",
            "issue_id": task_payload.issue_id,
            "repo": task_payload.repo_full_name,
        }

    return {"status": "ignored", "reason": "No relevant OpenJules trigger in webhook payload"}


class RadicleWebhookPayload(BaseModel):
    """Payload model for Radicle event webhooks / proxy."""

    event_type: str = Field(default="issue.created", description="Type of Radicle event")
    rid: str = Field(description="Radicle Repository ID (e.g. rad:z4V1sJY...)")
    issue_id: str | int = Field(description="Radicle issue ID or hash")
    issue_title: str
    issue_body: Optional[str] = ""
    labels: list[str] = Field(default_factory=list)
    default_branch: str = "master"


@app.post("/api/radicle/webhook")
async def radicle_webhook(
    event: RadicleWebhookPayload,
    background_tasks: BackgroundTasks,
):
    """Webhook adapter for self-hosted or local Radicle node events."""
    label_triggered = any("openjules" in str(l).lower() for l in event.labels) or event.event_type == "openjules:run"

    clean_rid = event.rid if event.rid.startswith("rad:") else f"rad:{event.rid}"

    if label_triggered or event.event_type in ("openjules:run", "issue.labeled"):
        task_payload = TaskPayload(
            forge="radicle",
            issue_id=event.issue_id,
            issue_title=event.issue_title,
            issue_body=event.issue_body or "",
            repo_full_name=clean_rid,
            repo_clone_url=clean_rid,
            default_branch=event.default_branch,
        )
        dispatcher = BackgroundTasksDispatcher(
            background_tasks=background_tasks,
            task_handler=executor.execute_task,
        )
        dispatcher.dispatch(task_payload)
        return {
            "status": "dispatched",
            "forge": "radicle",
            "rid": clean_rid,
            "issue_id": event.issue_id,
        }

    return {"status": "ignored", "reason": "No openjules label in Radicle event"}
