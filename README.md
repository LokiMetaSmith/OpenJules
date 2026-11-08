# jules
A love letter to Jules
Because Jules excels at unattended batch work with explicit specs and measurable targets, hand it a structured, multi-phase technical specification.

Create a fresh GitHub repository (e.g., `open-jules`), configure a clean project skeleton, and hand Jules this exact markdown specification as an initial task or issue prompt.

---

### Phase-by-Phase Prompt to Hand to Jules

```markdown
# Target: Implement "OpenJules" — Autonomous Sandboxed Coding Agent Service

## Context & Objective
We are building an open-source, asynchronous, cloud-agnostic equivalent of Google Jules.
The system receives tasks from GitHub issues/webhooks, boots an isolated sandbox (Docker/container), clones the repository, executes an agentic tool loop (file edits, shell commands, test verifications), and pushes the verified diff back as a clean Pull Request.

Build this service in Python (FastAPI + Docker SDK + LiteLLM/OpenAI format) with complete test coverage.

---

## Architecture & Core Deliverables

### Phase 1: Repository Configuration & Sandbox Runtime (`src/sandbox/`)
1. **Repository Config Parser (`src/sandbox/config.py`)**:
   - Define a Pydantic schema for `.openjules.yaml`:
     - `image`: string (default: `python:3.11-slim`)
     - `setup`: list of shell commands to prepare environment (e.g., `pip install -e .`)
     - `test_command`: shell command to verify the codebase (e.g., `pytest`)
     - `env`: dict of environment variables or secret keys to inject
     - `timeout_seconds`: integer (default: `900`)
2. **Container Sandbox Engine (`src/sandbox/docker_driver.py`)**:
   - Implement `SandboxManager` using `docker-py`.
   - `spawn_sandbox(repo_url: str, branch: str, config: OpenJulesConfig, git_token: str) -> SandboxContext`:
     - Provisions a container with resource constraints (`mem_limit="4g"`, `nano_cpus=2000000000`, network isolation options).
     - Mounts a dedicated workspace directory (`/workspace`).
     - Injects short-lived GitHub credentials via authenticated HTTPS remote URL.
   - `exec_in_sandbox(container_id: str, cmd: str, timeout: int) -> ExecResult`:
     - Runs commands with streaming `stdout`, `stderr`, and `exit_code`.
   - `cleanup_sandbox(container_id: str)`:
     - Guarantees complete container and volume teardown even on fatal task failures.

### Phase 2: Agent Tools & Execution Loop (`src/agent/`)
1. **Tool Definitions (`src/agent/tools.py`)**:
   - Provide minimal, robust tools for the agent:
     - `read_file(path: str, offset: int = 1, limit: int = 200)`
     - `write_file(path: str, content: str)`
     - `apply_patch(path: str, patch_str: str)`
     - `run_command(command: str, timeout: int = 120)`
     - `list_files(directory: str, max_depth: int = 3)`
2. **Two-Stage Planner & Executor (`src/agent/engine.py`)**:
   - **Step A (Planning Phase)**: Agent reads directory tree, README/context, and formulates a structured JSON plan (rationale, target files, planned test verification).
   - **Step B (Action Loop)**: Stateful ReAct loop with max turn limits (default 25 turns):
     - Executes tools inside the sandbox.
     - Feeds compilation errors, test tracebacks, and linter errors back to the model context.
   - **Step C (Verification)**: Automatically executes `config.test_command`. If exit code != 0, forces the agent back into a fix loop until green or turn limit reached.

### Phase 3: Git Lifecycle & Branch Management (`src/git_manager/`)
1. **Branch Workflow (`src/git_manager/lifecycle.py`)**:
   - Creates task branch: `openjules/task-{issue_id}-{slug}`.
   - Commits changes using Conventional Commits format (`fix: ...` or `feat: ...`).
   - Pushes branch to origin using the GitHub App installation token.
2. **Diff & Summary Generator**:
   - Runs `git diff main...HEAD` inside the workspace.
   - Summarizes changes and test outputs for the PR description.

### Phase 4: Control Plane & GitHub Webhook Bot (`src/api/`)
1. **GitHub App Webhook Handler (`src/api/webhook.py`)**:
   - FastAPI route `/api/webhook` verifying GitHub HMAC-SHA256 signatures.
   - Responds to `issues.labeled` (e.g., label: `openjules:run`) or issue comments containing `/jules run`.
   - Extracts issue description and repo metadata.
2. **Task Queue & Worker (`src/worker/`)**:
   - Run background execution via FastAPI `BackgroundTasks` or simple Celery/Redis queue.
   - Post progress comments directly on the GitHub Issue ("Plan generated", "Running tests...", "PR opened").
   - Open a draft or finalized Pull Request referencing `Fixes #{issue_id}`.

---

## Instructions for Execution
1. Generate the modular project structure under `src/` and `tests/`.
2. Include a `docker-compose.yml` and `Dockerfile` to run the OpenJules server locally.
3. Write comprehensive unit and integration tests using `pytest` and `pytest-mock` (mocking Docker SDK and LLM API calls).
4. Provide a sample `.openjules.yaml` and a `README.md` detailing quickstart instructions and GitHub App permission setup.
5. Ensure `pytest` passes cleanly across all modules.

```

---

### How to Sequence This with Jules

1. **Bootstrap the Repo**: Create the repository on GitHub with just an empty `README.md` and standard `.gitignore`.
2. **Issue Submission**: Open Issue #1 titled `feat: Implement OpenJules core architecture` and paste the prompt above into the issue body.
3. **Dispatch**: In the Jules dashboard or via `@jules`, assign the issue to Jules.
4. **Plan Review**: Jules will read the repo, generate its multi-file implementation plan, and highlight the modules it plans to write (`src/sandbox`, `src/agent`, `src/git_manager`, `src/api`). Review and approve the plan.
5. **PR Review**: When Jules finishes its test run inside its cloud sandbox, inspect the generated Pull Request diff and merge.

Here is the setup for running the OpenJules orchestrator alongside an isolated Docker-in-Docker (DinD) daemon. This avoids mounting the host's `/var/run/docker.sock` directly into the orchestrator, containing ephemeral sandbox containers inside the DinD daemon.

### `Dockerfile`

```dockerfile
FROM python:3.11-slim

# Install system dependencies: git for repository management, curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for caching layers
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV DOCKER_HOST=tcp://dind:2375

EXPOSE 8000

CMD ["uvicorn", "src.api.webhook:app", "--host", "0.0.0.0", "--port", "8000"]

```

---

### `requirements.txt`

```text
fastapi>=0.110.0
uvicorn[standard]>=0.28.0
docker>=7.0.0
pydantic>=2.6.0
pydantic-settings>=2.2.0
PyGithub>=2.2.0
litellm>=1.30.0
httpx>=0.27.0

```

---

### `docker-compose.yml`

```yaml
services:
  dind:
    image: docker:27-dind
    privileged: true
    restart: unless-stopped
    environment:
      # Disables TLS for internal container-to-container communication on default network
      DOCKER_TLS_CERTDIR: ""
    volumes:
      # Persistent cache for sandboxed images and workspace layers
      - dind-data:/var/lib/docker
      - workspace-data:/workspace
    networks:
      - openjules-net
    healthcheck:
      test: ["CMD", "docker", "info"]
      interval: 5s
      timeout: 3s
      retries: 5

  openjules:
    build:
      context: .
      dockerfile: Dockerfile
    restart: unless-stopped
    depends_on:
      dind:
        condition: service_healthy
    ports:
      - "8000:8000"
    environment:
      DOCKER_HOST: tcp://dind:2375
      # GitHub App credentials
      GITHUB_APP_ID: ${GITHUB_APP_ID:-}
      GITHUB_APP_PRIVATE_KEY_PATH: ${GITHUB_APP_PRIVATE_KEY_PATH:-/secrets/github-app.pem}
      GITHUB_WEBHOOK_SECRET: ${GITHUB_WEBHOOK_SECRET:-}
      # Model API keys
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
      GEMINI_API_KEY: ${GEMINI_API_KEY:-}
    volumes:
      - ./src:/app/src:ro
      - ./secrets:/secrets:ro
      - workspace-data:/workspace
    networks:
      - openjules-net

volumes:
  dind-data:
  workspace-data:

networks:
  openjules-net:
    driver: bridge

```

---

### Operational Details

* **Shared Volume (`workspace-data`):** Mounted into both containers at `/workspace`. When OpenJules spawns sandbox containers inside `dind`, it mounts subdirectories from `workspace-data` into the task containers so the host and agent share file access.
* **Network Isolation:** Sandbox containers spun up inside `dind` communicate with the `dind` daemon on its own internal network namespace, preventing sandbox workloads from reaching the OpenJules API directly unless explicitly bridged.
* **Volume Persistence:** `dind-data` persists downloaded base images (e.g., `python:3.11-slim`, `node:20`), avoiding repeated pulls for every inbound task.

[`google-research/harness2`](https://github.com/google-research/harness2) is an open-source framework by Google Cloud AI Research for **recursive agent harnessing**.

Rather than retraining or fine-tuning underlying model weights, Harness² focuses on automatically optimizing the **"harness"**—the scaffolding, prompts, tool definitions, memory, and orchestration layers wrapped around a frozen LLM.

---

### Key Concepts

* **The Harness vs. The Model:** In this paradigm, an agent consists of `Foundation Model + Harness`. When agents fail in open-world tasks, the failure is often not a limitation of raw model intelligence, but rather ambiguous system prompts, fragile tool schemas, unconstrained side effects, or lack of task-specific validation rules.
* **Propose–Probe–Compose Loop:**
1. **Propose:** The system generates alternative harness configurations (e.g., adding a validation guardrail, altering tool descriptions, or creating a domain-specific skill).
2. **Probe:** The candidate harnesses execute the active task in parallel or sequential sandbox runs.
3. **Compose:** By contrastively analyzing execution traces and behavioral differences (without needing ground-truth answers or labels), it composes an improved successor harness.


* **8 Editable Harness Components:**
* **Context / Persona:** System prompt, guardrails, memory, skills.
* **Tool Interfaces:** Tool guidance, executable helper scripts.
* **Execution:** Subagents (delegation), middleware plugins (tool-loop intercepts).


* **Dual Recursion:**
* *Task-Level Recursion:* Iteratively refines the harness for the current task.
* *Harnessing-Level Recursion:* Persists accumulated learnings, skills, and guardrails across tasks into an improvement library (`EXPERIENCE.md`) so subsequent tasks start with compounded capabilities.



---

### Tech Stack & Requirements

* **Runtime:** Linux, Python 3.12+ (managed with `uv`).
* **Executors / Benchmarks:** Supports executors like OpenCode and Codex, running evaluations against benchmarks like JobBench, LAB, and WorkBuddy-Bench.
* **License:** Apache-2.0 (Google Research experiment, not an officially supported Google product).
