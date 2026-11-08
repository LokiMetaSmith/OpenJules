# OpenJules — Autonomous Sandboxed Coding Agent Service

OpenJules is an open-source, asynchronous, cloud-agnostic equivalent of Google Jules.
The system receives tasks from GitHub issues and webhooks, boots an isolated container sandbox (Docker-in-Docker / DinD), clones the repository, executes an agentic tool loop (file edits, shell commands, test verifications), and pushes the verified diff back as a clean Pull Request.

---

## Architecture Overview

```
                      GitHub Webhook (Issue created/labeled /jules run)
                                            │
                                            ▼
                               ┌─────────────────────────┐
                               │     OpenJules API       │
                               │  (FastAPI Control Plane)│
                               └────────────┬────────────┘
                                            │
                                            ▼
                               ┌─────────────────────────┐
                               │   Background Worker     │
                               │    (Task Dispatcher)    │
                               └────────────┬────────────┘
                                            │
                                            ▼
                               ┌─────────────────────────┐
                               │     Sandbox Manager     │
                               │   (Docker-py Runtime)   │
                               └────────────┬────────────┘
                                            │
                     ┌──────────────────────┴──────────────────────┐
                     ▼                                             ▼
        ┌─────────────────────────┐                   ┌─────────────────────────┐
        │   Isolated DinD Daemon  │                   │    Two-Stage Engine     │
        │    (Docker Container)   │                   │  (LiteLLM / ReAct Loop) │
        └─────────────────────────┘                   └─────────────────────────┘
```

The system comprises 4 distinct phases:
1. **Repository Configuration & Sandbox Runtime (`src/sandbox/`)**:
   - `src/sandbox/config.py`: Schema parser for `.openjules.yaml` defining base image, setup scripts, test commands, timeouts, and env vars.
   - `src/sandbox/docker_driver.py`: `SandboxManager` handling container spawning with resource limits (`mem_limit="4g"`, `nano_cpus=2000000000`), workspace mounting, credential injection, command streaming, and strict `try...finally` cleanup.
2. **Agent Tools & Two-Stage Engine (`src/agent/`)**:
   - `src/agent/tools.py`: Minimal, robust tools executing directly inside the sandbox: `read_file`, `write_file`, `apply_patch`, `run_command`, `list_files`.
   - `src/agent/engine.py`:
     - **Step A (Planning Phase)**: Generates structured JSON plan (`rationale`, `target_files`, `planned_tests`, `action_steps`).
     - **Step B (Action Loop)**: Stateful ReAct loop with max turn limits.
     - **Step C (Verification)**: Automatically executes `config.test_command` with an iterative fix loop if tests fail.
3. **Git Lifecycle & Branch Management (`src/git_manager/`)**:
   - `src/git_manager/lifecycle.py`: Task branch generation (`openjules/task-{issue_id}-{slug}`), Conventional Commits, diff calculation (`git diff main...HEAD`), and automated PR summary formatting.
4. **Control Plane & GitHub Webhook Bot (`src/api/` & `src/worker/`)**:
   - `src/api/webhook.py`: FastAPI endpoint verifying HMAC-SHA256 signatures, listening for `issues.labeled` (`openjules:run`) or issue comments (`/jules run`).
   - `src/worker/dispatcher.py`: Extensible `TaskDispatcher` interface with FastAPI `BackgroundTasksDispatcher` default implementation.
   - `src/worker/executor.py`: End-to-end task runner opening PRs (`Fixes #{issue_id}`) and posting status comments to GitHub issues.

---

## Quickstart

### Prerequisites
- Python 3.11+
- Docker & Docker Compose
- GitHub App or Personal Access Token with repository permissions
- OpenAI, Anthropic, or Gemini API key

### 1. Local Development Setup

```bash
# Clone the repository
git clone https://github.com/your-org/openjules.git
cd openjules

# Install dependencies
pip install -r requirements-dev.txt

# Run test suite
pytest
```

### 2. Running with Docker Compose (DinD)

The included `docker-compose.yml` runs OpenJules alongside an isolated Docker-in-Docker daemon:

```bash
docker compose up -d
```

Check the health status:
```bash
curl http://localhost:8000/health
# {"status":"ok","service":"openjules"}
```

---

## Repository Configuration (`.openjules.yaml`)

Place `.openjules.yaml` in the root of your target repositories to customize runtime environments:

```yaml
# Base Docker image to spawn for the sandbox runtime
image: "python:3.11-slim"

# Commands executed inside the container to prepare the repository environment
setup:
  - pip install --no-cache-dir -r requirements.txt
  - pip install --no-cache-dir -e . 2>/dev/null || true

# Test command used by OpenJules to verify changes before opening a PR
test_command: "pytest"

# Environment variables or secrets to inject into sandbox tasks
env:
  PYTHONUNBUFFERED: "1"
  CI: "true"

# Execution timeout in seconds
timeout_seconds: 900
```

---

## GitHub App Configuration

1. In GitHub Settings, create a **New GitHub App**.
2. Set Webhook URL to: `https://your-openjules-host.com/api/webhook`
3. Provide a **Webhook secret** and add it to `.env` as `GITHUB_WEBHOOK_SECRET`.
4. Grant the following Repository Permissions:
   - **Issues**: Read & Write (to receive webhook events and post progress comments)
   - **Pull requests**: Read & Write (to create pull requests)
   - **Contents**: Read & Write (to commit and push task branches)
5. Subscribe to Webhook Events:
   - `Issues`
   - `Issue comment`
6. Trigger OpenJules by either:
   - Adding the label `openjules:run` to any issue.
   - Leaving a comment with `/jules run` on any issue.

---

## Architecture Background & Philosophy

### The Harness vs. The Model
In autonomous agent workflows, an agent can be understood as:
$$\text{Agent} = \text{Foundation Model} + \text{Harness}$$

When agents fail on open-world software tasks, the root cause is rarely raw model capability; rather, it stems from ambiguous system prompts, fragile tool schemas, unconstrained side effects, or lack of strict task-level verification loops.

OpenJules embraces this philosophy by isolating the execution harness:
- **Strict Sandbox Boundary**: Every task is constrained to an isolated container with deterministic resource limits (`4g` RAM, `2 CPUs`), ephemeral storage, and guaranteed `try...finally` teardown.
- **Two-Stage Deliberation**: Separates architectural planning (Step A) from action execution (Step B) to prevent aimless code modifications.
- **Closed-Loop Verification**: ReAct action loops are coupled to real environment test runners (Step C), creating an empirical feedback loop where code is only committed if test suites pass cleanly.
