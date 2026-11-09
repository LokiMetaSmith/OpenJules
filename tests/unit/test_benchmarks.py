"""Unit tests for BenchmarkRunner and evaluation harness."""

from unittest.mock import MagicMock
import pytest

from src.benchmarks.runner import BenchmarkRunner, BenchmarkTask
from src.worker.executor import TaskExecutor


def test_benchmark_runner_single_task():
    mock_executor = MagicMock(spec=TaskExecutor)
    mock_executor.execute_task.return_value = True

    runner = BenchmarkRunner(executor=mock_executor)
    task = BenchmarkTask(
        task_id="bench-01",
        repo_url="https://github.com/org/repo.git",
        issue_title="Add division check",
        problem_statement="Avoid zero division",
    )

    result = runner.run_task(task)
    assert result.task_id == "bench-01"
    assert result.resolved is True
    assert result.execution_time_seconds >= 0.0


def test_benchmark_runner_suite_metrics():
    mock_executor = MagicMock(spec=TaskExecutor)
    # First task passes, second task fails
    mock_executor.execute_task.side_effect = [True, False]

    runner = BenchmarkRunner(executor=mock_executor)
    tasks = [
        BenchmarkTask(task_id="t1", repo_url="url1", issue_title="Task 1", problem_statement="desc1"),
        BenchmarkTask(task_id="t2", repo_url="url2", issue_title="Task 2", problem_statement="desc2"),
    ]

    summary = runner.run_suite(tasks)
    assert summary.total_tasks == 2
    assert summary.resolved_count == 1
    assert summary.pass_at_1 == 0.5
    assert len(summary.results) == 2
