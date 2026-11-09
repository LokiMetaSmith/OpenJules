"""OpenJules CLI — Run autonomous agent tasks and manage swarms from the terminal."""

import argparse
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn

from src.agent.engine import AgentEngine
from src.polyphony.client import PolyphonyClient, derive_repo_swarm_key
from src.sandbox.config import OpenJulesConfig, load_config_from_file
from src.sandbox.docker_driver import SandboxManager
from src.worker.dispatcher import TaskPayload
from src.worker.executor import TaskExecutor


def run_cli_task(prompt: str, repo: str, branch: str = "main", test_cmd: str = None):
    """Executes an agent task directly in the specified repository."""
    print(f"🚀 OpenJules CLI: Launching task on '{repo}'...")
    print(f"Prompt: {prompt}\n")

    cfg = OpenJulesConfig()
    if test_cmd:
        cfg.test_command = test_cmd

    # Build task payload
    is_radicle = repo.startswith("rad:")
    forge = "radicle" if is_radicle else "github"
    clone_url = repo if is_radicle else (repo if repo.startswith("http") else f"https://github.com/{repo}.git")

    payload = TaskPayload(
        forge=forge,
        issue_id=1,
        issue_title=prompt[:80],
        issue_body=prompt,
        repo_full_name=repo,
        repo_clone_url=clone_url,
        default_branch=branch,
    )

    executor = TaskExecutor()
    success = executor.execute_task(payload)
    if success:
        print("\n🎉 Task completed and verified successfully!")
        return 0
    else:
        print("\n❌ Task execution or verification failed.")
        return 1


def serve_dashboard(host: str = "0.0.0.0", port: int = 8000):
    """Starts the OpenJules API server and 3-pane web interface."""
    print(f"🌐 Starting OpenJules Control Plane & Dashboard on http://{host}:{port}")
    uvicorn.run("src.api.webhook:app", host=host, port=port, reload=False)


def inspect_polyphony(repo: str):
    """Inspects the Keystone Polyphony swarm status for a repository."""
    swarm_key = derive_repo_swarm_key(repo)
    client = PolyphonyClient(repo, swarm_key=swarm_key)
    print(f"📡 Keystone Polyphony Swarm for '{repo}':")
    print(f"  - Swarm Key (SHA-256): {swarm_key}")
    print(f"  - Swarm Topic: {client.topic}")
    print(f"  - Node ID: {client.node_id}")
    print(f"  - Status: {client.status}")
    print(f"  - Active Batons: {list(client.batons.keys())}")


def main():
    parser = argparse.ArgumentParser(
        prog="openjules",
        description="OpenJules — Autonomous Sandboxed Coding Agent CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Command: run
    run_parser = subparsers.add_parser("run", help="Run an autonomous coding task directly")
    run_parser.add_argument("prompt", type=str, help="Instruction or bug report prompt")
    run_parser.add_argument(
        "--repo",
        type=str,
        default=os.environ.get("OPENJULES_REPO", "owner/repo"),
        help="Repository identifier (owner/repo or rad:z4V1sJY...)",
    )
    run_parser.add_argument("--branch", type=str, default="main", help="Target branch name")
    run_parser.add_argument("--test-cmd", type=str, default=None, help="Custom test command override")

    # Command: serve
    serve_parser = subparsers.add_parser("serve", help="Start the FastAPI control plane and web dashboard")
    serve_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host address")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port number")

    # Command: polyphony
    poly_parser = subparsers.add_parser("polyphony", help="Inspect repository Polyphony swarm key and state")
    poly_parser.add_argument("repo", type=str, help="Repository identifier")

    args = parser.parse_args()

    if args.command == "run":
        sys.exit(run_cli_task(args.prompt, args.repo, args.branch, args.test_cmd))
    elif args.command == "serve":
        serve_dashboard(args.host, args.port)
    elif args.command == "polyphony":
        inspect_polyphony(args.repo)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
