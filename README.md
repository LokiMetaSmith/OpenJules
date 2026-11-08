# OpenJules — Autonomous Sandboxed Coding Agent Service

OpenJules is an open-source, asynchronous, cloud-agnostic equivalent of Google Jules.
The system receives tasks from **GitHub issues/webhooks** or **Radicle peer-to-peer repositories (`rad:<rid>`)**, boots an isolated container sandbox (Docker-in-Docker / DinD), coordinates concurrent agents working on the same repository via **Keystone Polyphony**, executes an agentic tool loop, and pushes the verified diff back as a **GitHub Pull Request** or **Radicle Patch**.

---

## Architecture Overview

```
          GitHub Webhook                                Radicle Node / Webhook
   (issue labeled openjules:run)                     (issue labeled openjules:run)
                 │                                                 │
                 ▼                                                 ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │                 OpenJules API (FastAPI Control Plane)            │
        │          /api/webhook (GitHub)   |   /api/radicle/webhook        │
        └─────────────────────────────────┬────────────────────────────────┘
                                          │
                                          ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │            Pluggable Task Dispatcher & Worker                    │
        │      (BackgroundTasksDispatcher / RadicleNodePoller)             │
        └─────────────────────────────────┬────────────────────────────────┘
                                          │
                                          ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │               Keystone Polyphony Swarm Coordination              │
        │         (Repo-Scoped Swarm Key, Batons, Thoughts, Backlog)       │
        └─────────────────────────────────┬────────────────────────────────┘
                                          │
                                          ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │                     Sandbox Manager (Docker-py)                  │
        │           Mounts /workspace, sets 4G RAM & 2 CPU quota           │
        └─────────────────────────────────┬────────────────────────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
     ┌─────────────────────────┐                     ┌─────────────────────────┐
     │    Two-Stage Engine     │                     │   Pluggable Forge Layer │
     │  Planning -> ReAct Loop │                     │   GitHubProvider        │
     │  -> Verification Loop   │                     │   RadicleProvider       │
     └─────────────────────────┘                     └─────────────────────────┘
```

The system comprises 4 core architectural phases plus multi-forge and multi-agent swarm support:
1. **Repository Configuration & Sandbox Runtime (`src/sandbox/`)**:
   - `src/sandbox/config.py`: Schema parser for `.openjules.yaml` defining base image, setup scripts, test commands, timeouts, and env vars.
   - `src/sandbox/docker_driver.py`: `SandboxManager` handling container spawning with resource limits (`mem_limit="4g"`, `nano_cpus=2000000000`), workspace mounting, credential injection, command streaming, and strict `try...finally` cleanup.
2. **Agent Tools & Two-Stage Engine (`src/agent/`)**:
   - `src/agent/tools.py`: Safe tools executing directly inside the sandbox: `read_file`, `write_file`, `apply_patch` (unified diff + search/replace block support), `run_command`, `list_files`.
   - `src/agent/engine.py`:
     - **Step A (Planning Phase)**: Generates structured JSON plan (`rationale`, `target_files`, `planned_tests`, `action_steps`).
     - **Step B (Action Loop)**: Stateful ReAct loop with turn limit.
     - **Step C (Verification)**: Automatically executes `config.test_command` with an iterative fix loop if tests fail.
3. **Keystone Polyphony Multi-Agent Swarm (`src/polyphony/`)**:
   - Auto-subscribes all agents targeting the same repo to the repo's Polyphony choir using deterministic `SWARM_KEY` derivation (`keystone-polyphony-v1:<repo>`).
   - **Baton Mutex Locks**: Prevents duplicate concurrent runs (`task:{issue_id}`) and coordinates file modifications (`file:{path}`).
   - **Liminal Space**: Broadcasts thoughts, status changes (`planning`, `busy`, `verifying`, `idle`), and audit logs.
   - **Swarm Backlog**: Synchronizes task progress across decentralized nodes.
4. **Pluggable Forge Layer (`src/forge/` & `src/git_manager/`)**:
   - `src/forge/base.py`: Abstract `ForgeProvider` interface.
   - `src/forge/github.py`: GitHub App and PAT integration via PyGithub.
   - `src/forge/radicle.py`: Radicle integration supporting the `rad` CLI (`rad clone`, `rad patch open`) and Radicle Node HTTP API.
   - `src/git_manager/lifecycle.py`: Task branch management (`openjules/task-{issue_id}-{slug}`), Conventional Commits, diff calculation, and PR/Patch summary generation.
5. **Control Plane & Background Task Workers (`src/api/` & `src/worker/`)**:
   - `src/api/webhook.py`: FastAPI endpoints for GitHub HMAC-SHA256 webhooks and Radicle webhook events.
   - `src/worker/dispatcher.py`: Extensible `TaskDispatcher` interface.
   - `src/worker/radicle_poller.py`: Background poller for local or self-hosted Radicle nodes (`rad:<rid>`).
   - `src/worker/executor.py`: End-to-end task runner opening PRs/Patches and posting status comments.

---

## Quickstart

### Prerequisites
- Python 3.11+
- Docker & Docker Compose
- GitHub App or Personal Access Token (for GitHub)
- Radicle node / CLI (for decentralized Radicle collaboration)
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

### 2. Running with Docker Compose (DinD + Optional Radicle Node + Polyphony MQTT)

```bash
# Standard mode (GitHub + OpenJules DinD)
docker compose up -d

# Multi-forge mode with self-hosted Radicle Node
docker compose --profile radicle up -d

# Swarm mode with Keystone Polyphony MQTT broker
docker compose --profile swarm up -d
```

Check health:
```bash
curl http://localhost:8000/health
# {"status":"ok","service":"openjules","forges":["github","radicle"]}
```

---

## Keystone Polyphony Integration (https://github.com/LokiMetaSmith/keystone-polyphony)

Every agent operating on the same repository automatically joins that repository's Polyphony:
- **Repo-Scoped Anchoring**: The `SWARM_KEY` is derived deterministically from the repository name or Radicle RID, partitioning coordination cleanly.
- **Mutex Batons**: Agents acquire `task:{issue_id}` on startup and `file:{path}` during planning, preventing race conditions or conflicting code changes.
- **Liminal Thoughts Stream**: Real-time progress thoughts ("Step A generated", "Running pytest", "Verification PASSED") stream into the shared mesh.

---

## Radicle Integration Guide (https://radicle.dev)

OpenJules natively supports Radicle peer-to-peer code collaboration:
- **Repository IDs (`rad:<rid>`)**: Clones and works on decentralized repos without a centralized server.
- **Radicle Patches**: Changes are proposed via `rad patch open` rather than GitHub PRs.
- **Dual Triggers**:
  1. **Webhook / Proxy (`POST /api/radicle/webhook`)**: Triggered when a Radicle issue has the `openjules:run` label.
  2. **Background Poller (`RadicleNodePoller`)**: Actively polls `GET /api/v1/projects/{rid}/issues` from your local node (`http://127.0.0.1:8080/api/v1`).

---

## Repository Configuration (`.openjules.yaml`)

Place `.openjules.yaml` in the root of your target repositories:

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

# Keystone Polyphony coordination settings
polyphony:
  enabled: true
  swarm_key: null  # Optional override

# Execution timeout in seconds
timeout_seconds: 900
```

---

## Architecture Background & Philosophy

### The Harness vs. The Model
In autonomous agent workflows, an agent can be understood as:
$$\text{Agent} = \text{Foundation Model} + \text{Harness}$$

When agents fail on open-world software tasks, the root cause is rarely raw model capability; rather, it stems from ambiguous system prompts, fragile tool schemas, unconstrained side effects, or lack of strict task-level verification loops.

OpenJules embraces this philosophy by isolating the execution harness:
- **Strict Sandbox Boundary**: Every task is constrained to an isolated container with deterministic resource limits (`4g` RAM, `2 CPUs`), ephemeral storage, and guaranteed `try...finally` teardown.
- **Keystone Polyphony Swarm**: Coordinates multiple agents working asynchronously on the same repo using RTOS-inspired mutex batons and shared liminal context.
- **Decentralized & Multi-Forge**: Pluggable forge layer decouples the agent core from any single centralized platform, natively powering both GitHub and Radicle p2p networks.
- **Two-Stage Deliberation**: Separates architectural planning (Step A) from action execution (Step B) to prevent aimless code modifications.
- **Closed-Loop Verification**: ReAct action loops are coupled to real environment test runners (Step C), creating an empirical feedback loop where code is only committed if test suites pass cleanly.
