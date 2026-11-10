"""Worker Bootstrap Builder and AGENTS.md generator for Polyphony Swarm cohorts.

Generates the environment variables and seed AGENTS.md prompt for spawned worker sandboxes,
ensuring strict security isolation between SWARM_KEY and OPENJULES_HOST_KEY.
"""

from dataclasses import dataclass
import json
from typing import Any, Dict, List, Optional

from src.polyphony.models import PolyphonyCohortConfig


@dataclass
class WorkerBootstrapPackage:
    """Contains environment variables and workspace prompt instructions for a spawned worker."""

    env_vars: Dict[str, str]
    agents_md_content: str


class WorkerBootstrapBuilder:
    """Constructs environment configuration and prompt templates for cohort workers."""

    @classmethod
    def build(
        cls,
        config: PolyphonyCohortConfig,
        actor_id: str,
        role: str = "worker",  # "worker" or "reviewer"
        target_task_ids: Optional[List[str]] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> WorkerBootstrapPackage:
        """Builds environment variables and AGENTS.md seed prompt for a worker container.

        Enforces security invariants:
        - Injects SWARM_KEY for liminal_bridge MCP authentication.
        - NEVER leaks OPENJULES_HOST_KEY into worker container environment or prompt.
        """
        targets = target_task_ids or []
        env_vars: Dict[str, str] = {
            "SWARM_KEY": config.swarm_key,
            "JULES_ACTOR_ID": actor_id,
            "POLYPHONY_ROLE": role,
            "TARGET_TASK_IDS": json.dumps(targets),
            "SWARM_ID": config.swarm_id,
        }
        if config.liminal_db_path:
            env_vars["LIMINAL_COLLAB_DB"] = config.liminal_db_path
        if config.baseline_sha:
            env_vars["BASELINE_SHA"] = config.baseline_sha

        if extra_env:
            for k, v in extra_env.items():
                if k != "OPENJULES_HOST_KEY":  # Explicit guard against accidental host key leakage
                    env_vars[k] = v

        agents_md = cls._generate_agents_md(
            config=config,
            actor_id=actor_id,
            role=role,
            target_task_ids=targets,
        )

        return WorkerBootstrapPackage(
            env_vars=env_vars,
            agents_md_content=agents_md,
        )

    @classmethod
    def _generate_agents_md(
        cls,
        config: PolyphonyCohortConfig,
        actor_id: str,
        role: str,
        target_task_ids: List[str],
    ) -> str:
        targets_formatted = ", ".join(f"`{tid}`" for tid in target_task_ids) if target_task_ids else "None (discover via `list_tasks`)"

        if role == "reviewer":
            role_instructions = f"""### Role: Independent Reviewer (`{actor_id}`)
You are an ephemeral reviewer agent provisioned to resolve Polyphony's `author_actor_id != reviewer_actor_id` invariant.
1. Inspect pending candidates in `INTEGRATING` state.
2. If assigned target tasks ({targets_formatted}), prioritize reviewing candidates associated with those tasks.
3. Checkout candidate patch/commit into a clean worktree.
4. Run verification and tests (`pytest`, linting, build checks).
5. Submit an independent `record_review` receipt with your verdict (`APPROVED` or `CHANGES_REQUESTED`).
6. Exit or stand down once all assigned candidate reviews are completed."""
        else:
            role_instructions = f"""### Role: Swarm Worker (`{actor_id}`)
You are a peer coding agent participating in Polyphony swarm `{config.swarm_id}`.
1. Check `list_tasks` and active nudges first.
2. Target tasks for this session: {targets_formatted}.
3. Claim the first uncontested task via `dispatch_attempt` using a fenced Git worktree.
4. If all leaf tasks are contested and `active_workers >= 3`:
   - Temporarily claim `claim_emergent_conductor` to decompose bottlenecks or request host scaling.
   - Once decomposition or signal submission is complete, call `step_down_conductor` to resume coding/reviewing.
5. Coordinate changes with peer agents using mutex batons (`acquire_baton` / `release_baton`) and broadcast thoughts via `share_thought`."""

        return f"""# AGENTS.md - Polyphony Swarm Cohort Directive

Welcome to the Keystone Polyphony Swarm `{config.swarm_id}`.

## Identity & Authentication
- **Actor ID**: `{actor_id}`
- **Role**: `{role}`
- Connect to the repository's `liminal_bridge` MCP server using your injected `SWARM_KEY` and `JULES_ACTOR_ID`.

## Protocol Directives
{role_instructions}

## Coordination & Worktree Safety
- Always work inside designated fenced worktrees to avoid dirtying other agents' worktrees.
- Release batons promptly upon completion or unexpected failure.
- Never attempt to directly alter host provisioning or security keys.
"""
