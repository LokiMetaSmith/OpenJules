"""Benchmarking and evaluation harness for OpenJules.

Evaluates coding accuracy, pass@1 rates, and resolution times across tasks.
"""

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field

from src.sandbox.config import OpenJulesConfig
from src.worker.dispatcher import TaskPayload
from src.worker.executor import TaskExecutor

logger = logging.getLogger(__name__)


class BenchmarkTask(BaseModel):
    """Definition of an evaluation task."""

    task_id: str
    repo_url: str
    issue_title: str
    problem_statement: str
    expected_test_cmd: str = "pytest"
    timeout_seconds: int = 300


class BenchmarkTaskResult(BaseModel):
    """Result for a single evaluation benchmark task."""

    task_id: str
    resolved: bool
    execution_time_seconds: float
    error: Optional[str] = None


class BenchmarkSummary(BaseModel):
    """Summary of benchmark run across a dataset."""

    total_tasks: int
    resolved_count: int
    pass_at_1: float
    average_duration_seconds: float
    results: List[BenchmarkTaskResult]


class BenchmarkRunner:
    """Runs a suite of benchmark coding tasks and computes metrics."""

    def __init__(self, executor: Optional[TaskExecutor] = None):
        self.executor = executor or TaskExecutor()

    def run_task(self, task: BenchmarkTask) -> BenchmarkTaskResult:
        """Executes a single benchmark task inside an isolated sandbox."""
        start_time = time.time()
        payload = TaskPayload(
            forge="github",
            issue_id=task.task_id,
            issue_title=task.issue_title,
            issue_body=task.problem_statement,
            repo_full_name=task.repo_url.split("/")[-1].replace(".git", ""),
            repo_clone_url=task.repo_url,
            default_branch="main",
        )

        try:
            success = self.executor.execute_task(payload)
            duration = time.time() - start_time
            return BenchmarkTaskResult(
                task_id=task.task_id,
                resolved=success,
                execution_time_seconds=round(duration, 2),
            )
        except Exception as e:
            duration = time.time() - start_time
            return BenchmarkTaskResult(
                task_id=task.task_id,
                resolved=False,
                execution_time_seconds=round(duration, 2),
                error=str(e),
            )

    def run_suite(self, tasks: List[BenchmarkTask]) -> BenchmarkSummary:
        """Executes a list of benchmark tasks and calculates pass@1 accuracy."""
        results: List[BenchmarkTaskResult] = []
        total_time = 0.0

        for t in tasks:
            logger.info(f"Running benchmark task {t.task_id}: {t.issue_title}")
            res = self.run_task(t)
            results.append(res)
            total_time += res.execution_time_seconds

        resolved = sum(1 for r in results if r.resolved)
        total = len(tasks)
        pass_at_1 = (resolved / total) if total > 0 else 0.0
        avg_dur = (total_time / total) if total > 0 else 0.0

        return BenchmarkSummary(
            total_tasks=total,
            resolved_count=resolved,
            pass_at_1=round(pass_at_1, 4),
            average_duration_seconds=round(avg_dur, 2),
            results=results,
        )
