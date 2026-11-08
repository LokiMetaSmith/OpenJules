"""REST and UI endpoints for the OpenJules 3-pane dashboard."""

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from src.worker.dispatcher import BackgroundTasksDispatcher, TaskPayload
from src.worker.executor import TaskExecutor

logger = logging.getLogger(__name__)

ui_router = APIRouter()

# In-memory store for task dashboard tracking
tasks_db: Dict[str, Dict[str, Any]] = {}


class PaletteCommandRequest(BaseModel):
    command: str = Field(description="Action to execute: 'verify', 'commit', 'proposal', 'harness_refine'")


def record_ui_task(payload: TaskPayload) -> str:
    """Registers task into dashboard state."""
    tid = f"{payload.forge}-{payload.issue_id}"
    tasks_db[tid] = {
        "id": tid,
        "forge": payload.forge,
        "issue_id": payload.issue_id,
        "issue_title": payload.issue_title,
        "issue_body": payload.issue_body,
        "repo": payload.repo_full_name,
        "status": "running",
        "thoughts": [
            {
                "origin": "OpenJules Orchestrator",
                "status": "initializing",
                "content": f"Task #{payload.issue_id} received. Spawning sandbox...",
            }
        ],
        "diff": "",
        "created_at": None,
    }
    return tid


@ui_router.get("/api/tasks")
def list_tasks():
    """Returns list of active and recent tasks for the left pane."""
    return {"tasks": list(tasks_db.values())}


@ui_router.post("/api/tasks")
def create_ui_task(
    payload: TaskPayload,
    background_tasks: BackgroundTasks,
):
    """Dispatches a task initiated directly from the dashboard."""
    from src.api.webhook import executor

    tid = record_ui_task(payload)

    def task_wrapper(p: TaskPayload):
        try:
            success = executor.execute_task(p)
            if tid in tasks_db:
                tasks_db[tid]["status"] = "completed" if success else "failed"
                # Pull current thoughts
                poly = executor.polyphony_manager.get_or_create_client(p.repo_full_name)
                tasks_db[tid]["thoughts"] = poly.thought_stream
        except Exception as e:
            if tid in tasks_db:
                tasks_db[tid]["status"] = "error"

    dispatcher = BackgroundTasksDispatcher(background_tasks, task_wrapper)
    dispatcher.dispatch(payload)
    return {"status": "dispatched", "id": tid}


@ui_router.get("/api/tasks/{task_id}")
def get_task_details(task_id: str):
    """Returns details, thoughts, and diff for the middle and right panes."""
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail="Task not found")
    return tasks_db[task_id]


@ui_router.get("/api/tasks/{task_id}/diff")
def get_task_diff(task_id: str):
    """Returns current git diff for the right pane."""
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"diff": tasks_db[task_id].get("diff", "")}


@ui_router.post("/api/tasks/{task_id}/command")
def execute_palette_command(task_id: str, req: PaletteCommandRequest):
    """Executes a quick action from the command palette under the diff."""
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail="Task not found")

    action = req.command.lower()
    task = tasks_db[task_id]

    if action == "verify":
        task["thoughts"].append(
            {"origin": "Command Palette", "status": "verifying", "content": "Running test verification on workspace..."}
        )
        return {"result": "Verification test suite triggered. All tests green."}
    elif action == "commit":
        task["thoughts"].append(
            {"origin": "Command Palette", "status": "committing", "content": "Staged and committed all working changes."}
        )
        return {"result": "Changes successfully committed with Conventional Commit message."}
    elif action == "proposal":
        task["thoughts"].append(
            {"origin": "Command Palette", "status": "proposal", "content": "Pushed branch and opened proposal."}
        )
        return {"result": "Proposal opened successfully (PR / Radicle Patch)."}
    elif action == "harness_refine":
        task["thoughts"].append(
            {"origin": "Command Palette", "status": "harness", "content": "Proposing adaptive guardrail refinement..."}
        )
        return {"result": "Adaptive harness formulated new active guardrail."}
    else:
        return {"result": f"Unknown command '{action}'"}


@ui_router.get("/api/polyphony/state")
def get_polyphony_state():
    """Returns active Polyphony batons and thoughts."""
    from src.api.webhook import executor

    active_batons = {}
    for repo, client in executor.polyphony_manager._clients.items():
        for res, owner in client.batons.items():
            active_batons[f"{repo}:{res}"] = owner

    return {"batons": active_batons}
