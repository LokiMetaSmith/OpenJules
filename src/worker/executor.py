"""Task worker executor running the full OpenJules autonomous coding lifecycle."""

import logging
import os
from typing import Any, Optional
from github import Github, Auth

from src.agent.engine import AgentEngine
from src.git_manager.lifecycle import GitLifecycleManager
from src.sandbox.config import OpenJulesConfig, load_config_from_yaml
from src.sandbox.docker_driver import SandboxManager
from src.worker.dispatcher import TaskPayload

logger = logging.getLogger(__name__)


class TaskExecutor:
    """Executes an issue task from sandbox spawn through agent run, git push, and PR opening."""

    def __init__(
        self,
        sandbox_manager: Optional[SandboxManager] = None,
        agent_engine: Optional[AgentEngine] = None,
        git_manager: Optional[GitLifecycleManager] = None,
        github_client: Optional[Github] = None,
    ):
        self.sandbox_manager = sandbox_manager or SandboxManager()
        self.agent_engine = agent_engine or AgentEngine(self.sandbox_manager)
        self.git_manager = git_manager or GitLifecycleManager(self.sandbox_manager)
        self.github_client = github_client

    def _get_github(self) -> Optional[Github]:
        if self.github_client:
            return self.github_client
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            return Github(auth=Auth.Token(token))
        return None

    def post_issue_comment(self, repo_name: str, issue_id: int, message: str) -> None:
        """Posts a comment on the GitHub issue if client is available."""
        gh = self._get_github()
        if not gh:
            logger.info(f"GitHub client not configured. Issue #{issue_id} update:\n{message}")
            return
        try:
            repo = gh.get_repo(repo_name)
            issue = repo.get_issue(number=issue_id)
            issue.create_comment(f"🤖 **[OpenJules]** {message}")
        except Exception as e:
            logger.warning(f"Failed to post comment to issue #{issue_id}: {e}")

    def create_pull_request(
        self,
        repo_name: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> Optional[str]:
        """Opens a Pull Request referencing the completed task."""
        gh = self._get_github()
        if not gh:
            logger.info(f"GitHub client not configured. Would open PR: {head_branch} -> {base_branch}")
            return None
        try:
            repo = gh.get_repo(repo_name)
            pr = repo.create_pull(
                title=title,
                body=body,
                head=head_branch,
                base=base_branch,
            )
            return pr.html_url
        except Exception as e:
            logger.error(f"Failed to create Pull Request for {repo_name}: {e}")
            return None

    def execute_task(self, payload: TaskPayload, git_token: Optional[str] = None) -> bool:
        """Runs the complete autonomous agent lifecycle."""
        logger.info(f"Starting task execution for issue #{payload.issue_id} in {payload.repo_full_name}")
        self.post_issue_comment(
            payload.repo_full_name,
            payload.issue_id,
            f"🚀 OpenJules received task: **{payload.issue_title}**. Initializing sandbox...",
        )

        effective_token = git_token or os.environ.get("GITHUB_TOKEN")
        initial_config = OpenJulesConfig()

        # Strict try...finally sandbox container & volume lifecycle
        try:
            with self.sandbox_manager.run_sandbox(
                repo_url=payload.repo_clone_url,
                branch=payload.default_branch,
                config=initial_config,
                git_token=effective_token,
            ) as context:
                # 1. Read .openjules.yaml if present in repository
                config_res = self.sandbox_manager.exec_in_sandbox(
                    context.container_id, "cat .openjules.yaml 2>/dev/null || cat .openjules.yml 2>/dev/null"
                )
                if config_res.exit_code == 0 and config_res.stdout.strip():
                    context.config = load_config_from_yaml(config_res.stdout)

                # 2. Create task branch
                branch_name = self.git_manager.create_task_branch(
                    context,
                    issue_id=payload.issue_id,
                    title=payload.issue_title,
                )

                # 3. Agent planning and execution loop
                task_instruction = f"Title: {payload.issue_title}\n\nDescription:\n{payload.issue_body}"

                def on_progress(msg: str):
                    self.post_issue_comment(payload.repo_full_name, payload.issue_id, msg)

                agent_result = self.agent_engine.run(
                    context=context,
                    task_instruction=task_instruction,
                    on_progress=on_progress,
                )

                if not agent_result.success:
                    self.post_issue_comment(
                        payload.repo_full_name,
                        payload.issue_id,
                        f"❌ Verification failed after {agent_result.turns_taken} turns. Automated tests did not pass cleanly.",
                    )
                    return False

                # 4. Commit changes
                commit_msg = f"fix: resolve #{payload.issue_id} - {payload.issue_title}"
                sha = self.git_manager.commit_changes(context, commit_msg)
                if not sha:
                    self.post_issue_comment(
                        payload.repo_full_name,
                        payload.issue_id,
                        "ℹ️ No file changes were detected to commit.",
                    )
                    return True

                # 5. Push branch to remote
                self.git_manager.push_branch(context, branch_name)

                # 6. Generate PR summary and diff
                test_output = "Automated test suite verified green."
                diff_summary = self.git_manager.generate_pr_summary(
                    context,
                    issue_id=payload.issue_id,
                    issue_title=payload.issue_title,
                    branch_name=branch_name,
                    test_output=test_output,
                    base_branch=payload.default_branch,
                )

                # 7. Create Pull Request
                pr_url = self.create_pull_request(
                    repo_name=payload.repo_full_name,
                    title=f"fix: {payload.issue_title} (fixes #{payload.issue_id})",
                    body=diff_summary.summary,
                    head_branch=branch_name,
                    base_branch=payload.default_branch,
                )

                pr_msg = f"🎉 Pull Request successfully opened: {pr_url}" if pr_url else f"🎉 Branch `{branch_name}` successfully pushed."
                self.post_issue_comment(payload.repo_full_name, payload.issue_id, pr_msg)
                return True

        except Exception as e:
            logger.exception(f"Fatal error executing task #{payload.issue_id}: {e}")
            self.post_issue_comment(
                payload.repo_full_name,
                payload.issue_id,
                f"⚠️ Fatal error during execution: `{str(e)}`",
            )
            return False
