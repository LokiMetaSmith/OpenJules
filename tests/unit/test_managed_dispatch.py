"""Delivery crash windows must not create fresh canonical work or executions."""

import hashlib
from types import SimpleNamespace
import time

import pytest

from src.polyphony.managed_client import AuthorityRejected, AuthorityUnavailable
from src.worker.lifecycle import (
    RuntimeCapabilities,
    RuntimeObservation,
    WorkerLifecycle,
    WorkerSpec,
)
from src.worker.managed_dispatch import ManagedDispatcher

SPEC = WorkerSpec(
    "test", "github:github.com/example/repo", "slot-1", 1, "launch-1", "coding"
)
BASE = "a" * 40


class Runtime:
    def __init__(self):
        self.executions = {}
        self.cancelled = set()
        self.captured = True
        self.finished = False
        self.launches = 0

    def capabilities(self, *_):
        return RuntimeCapabilities(True, True, True, "test driver")

    def ensure(self, worker_id, spec):
        return RuntimeObservation("ready", "container-" + worker_id)

    def observe(self, worker_id, spec, runtime_id):
        if runtime_id is None:
            return RuntimeObservation("unknown")
        if self.finished:
            return RuntimeObservation(
                "stopped",
                runtime_id,
                termination_confirmed=True,
                artifacts_captured=self.captured,
                launch_fenced=True,
            )
        return RuntimeObservation("ready", runtime_id)

    def receipt(self):
        return SimpleNamespace(
            state="exited",
            termination_confirmed=True,
            artifacts_captured=self.captured,
            launch_fenced=True,
            exit_code=0,
            patch="diff --git a/result b/result\n",
            log="controlled process exited\n",
            manifest='{"capture":"durable"}',
        )

    def execute_attempt(
        self, worker_id, spec, attempt_id, text, seconds, *, deadline_monotonic=None
    ):
        assert (
            deadline_monotonic is None
            or deadline_monotonic <= time.monotonic() + seconds
        )
        if attempt_id not in self.executions:
            assert attempt_id not in self.cancelled
            self.executions[attempt_id] = text
            self.launches += 1
        self.finished = True
        return self.receipt()

    def cancel_attempt(self, worker_id, spec, attempt_id, text):
        self.cancelled.add(attempt_id)
        self.finished = True
        return self.receipt()


class Authority:
    def __init__(self):
        self.claims = []
        self.binding = None
        self.artifacts = {}
        self.terminals = []
        self.candidates = []
        self.lose_claim = False
        self.lose_capture = False
        self.reject_claim = False
        self.allowed = True

    def capabilities(self, *_):
        return {"attempt_seconds": 30, "capabilities": []}

    def snapshot(self, scope):
        return {
            "task": [
                {
                    "id": "task-1",
                    "status": "todo",
                    "max_attempts": 3,
                    "budget_seconds": 100,
                }
            ],
            "attempt": [],
        }

    def call(self, operation, scope, **data):
        common = {
            "schema_version": 1,
            "deployment_id": scope.deployment_id,
            "repository": scope.repository,
        }
        if operation == "claim":
            self.claims.append(data)
            if self.reject_claim:
                raise AuthorityRejected(409, claim_not_allocated=True)
            self.binding = self.binding or {
                "attempt_id": "b" * 32,
                "deployment_id": scope.deployment_id,
                "repository": scope.repository,
                "worker_id": data["worker_id"],
                "profile": data["profile"],
                "base": data["base"],
                "launch_key": "canonical-launch",
            }
            if self.lose_claim:
                self.lose_claim = False
                raise AuthorityUnavailable("lost claim response")
        if operation in {"claim", "permission"}:
            return common | {
                "binding": self.binding,
                "attempt": {"id": "b" * 32},
                "requirements": "Change result",
                "launch_allowed": self.allowed,
                "remaining_seconds": 30 if self.allowed else 0,
            }
        if operation == "capture":
            self.artifacts[data["kind"]] = data["content"]
            if self.lose_capture:
                self.lose_capture = False
                raise AuthorityUnavailable("capture reply lost after persistence")
            return common | {
                "attempt_id": data["attempt_id"],
                "kind": data["kind"],
                "artifact_id": hashlib.sha256(data["content"].encode()).hexdigest(),
            }
        if operation == "observe":
            if data["termination_confirmed"]:
                assert set(self.artifacts) == {"patch", "log", "manifest"}
                self.terminals.append(data)
            return common
        if operation == "candidate":
            self.candidates.append(data)
            return common
        raise AssertionError(operation)


def setup(tmp_path):
    runtime, authority = Runtime(), Authority()
    lifecycle = WorkerLifecycle(tmp_path / "workers.sqlite3", runtime)
    profile = SimpleNamespace(
        name="coding",
        deployment_id=SPEC.deployment_id,
        repository=SPEC.repository,
        base_commit=BASE,
        max_seconds=60,
    )
    dispatcher = ManagedDispatcher(
        lifecycle, runtime, authority, [profile], tmp_path / "dispatch"
    )
    worker = lifecycle.ensure_worker(SPEC)
    return runtime, authority, lifecycle, dispatcher, worker


def test_lost_claim_replays_original_request_after_reopening(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    authority.lose_claim = True
    with pytest.raises(AuthorityUnavailable):
        dispatch.tick_worker(worker)
    assert runtime.launches == 0
    recovered = ManagedDispatcher(
        lifecycle,
        runtime,
        authority,
        list(dispatch.profiles.values()),
        tmp_path / "dispatch",
    )
    recovered.tick_worker(worker)
    assert authority.claims[0] == authority.claims[1]
    assert runtime.launches == 1
    assert len(authority.terminals) == 1
    assert len(authority.candidates) == 1
    assert (
        lifecycle.observe_worker(SPEC.scope, worker["worker_id"])["active_executions"]
        == []
    )


def test_lost_capture_does_not_execute_root_again(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    authority.lose_capture = True
    with pytest.raises(AuthorityUnavailable):
        dispatch.tick_worker(worker)
    assert runtime.launches == 1
    assert not authority.terminals
    assert lifecycle.observe_worker(SPEC.scope, worker["worker_id"])[
        "active_executions"
    ]
    dispatch.tick_worker(lifecycle.observe_worker(SPEC.scope, worker["worker_id"]))
    assert runtime.launches == 1
    assert len(authority.terminals) == 1
    assert dispatch.journal.get(worker["worker_id"])["phase"] == "completed"


def test_failed_capture_retains_both_reservations(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    runtime.captured = False
    dispatch.tick_worker(worker)
    assert not authority.terminals
    observed = lifecycle.observe_worker(SPEC.scope, worker["worker_id"])
    assert observed["state"] == "unknown"
    assert observed["active_executions"] == ["b" * 32]
    runtime.captured = True
    dispatch.tick_worker(observed)
    assert runtime.launches == 1
    assert len(authority.terminals) == 1


def test_expired_claim_is_cancelled_before_process_start(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    authority.allowed = False
    dispatch.tick_worker(worker)
    assert runtime.launches == 0
    assert runtime.cancelled == {"b" * 32}
    assert len(authority.terminals) == 1


def test_explicit_atomic_claim_rejection_does_not_bind_worker(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    authority.reject_claim = True
    with pytest.raises(AuthorityRejected):
        dispatch.tick_worker(worker)
    assert dispatch.journal.get(worker["worker_id"]) is None
    assert runtime.launches == 0


def test_forged_binding_cannot_launch(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    authority.binding = {
        "deployment_id": "other",
        "repository": SPEC.repository,
        "attempt_id": "b" * 32,
        "worker_id": worker["worker_id"],
        "profile": "coding",
        "base": BASE,
        "launch_key": "forged",
    }
    with pytest.raises(AuthorityUnavailable):
        dispatch.tick_worker(worker)
    assert runtime.launches == 0


def test_runtime_capture_is_not_task_acceptance(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    dispatch.tick_worker(worker)
    assert set(authority.artifacts) == {"patch", "log", "manifest"}
    assert authority.candidates[0] == {
        "attempt_id": "b" * 32,
        "artifact_id": hashlib.sha256(runtime.receipt().patch.encode()).hexdigest(),
    }
    assert dispatch.journal.get(worker["worker_id"])["phase"] == "completed"
    dispatch.tick_worker(worker)
    assert len(authority.claims) == 1
    assert runtime.launches == 1


def test_worker_dispatch_is_exclusive_across_process_handles(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    with dispatch.journal.lock(worker["worker_id"]) as acquired:
        assert acquired
        second = ManagedDispatcher(
            lifecycle,
            runtime,
            authority,
            list(dispatch.profiles.values()),
            tmp_path / "dispatch",
        )
        second.tick_worker(worker)
    assert not authority.claims
    assert runtime.launches == 0


def test_admission_delay_cannot_extend_canonical_deadline(tmp_path, monkeypatch):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    original_call = authority.call

    def short_permission(operation, scope, **data):
        result = original_call(operation, scope, **data)
        if operation == "permission":
            result["remaining_seconds"] = 0.02
        return result

    authority.call = short_permission
    original_observe = lifecycle.observe_worker

    def slow_observe(scope, worker_id):
        time.sleep(0.04)
        return original_observe(scope, worker_id)

    monkeypatch.setattr(lifecycle, "observe_worker", slow_observe)
    dispatch.tick_worker(worker)
    assert runtime.launches == 0
    assert runtime.cancelled == {"b" * 32}


def test_generic_conflict_retains_uncertain_claim_identity(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)

    def conflict(*args, **kwargs):
        raise AuthorityRejected(409)

    authority.call = conflict
    with pytest.raises(AuthorityRejected):
        dispatch.tick_worker(worker)
    assert dispatch.journal.get(worker["worker_id"])["phase"] == "claiming"


def test_partial_budget_exhaustion_does_not_starve_later_tasks(tmp_path):
    runtime, authority, lifecycle, dispatch, worker = setup(tmp_path)
    tasks = [
        {
            "id": "older",
            "status": "in_progress",
            "created": 1,
            "max_attempts": 3,
            "budget_seconds": 90,
            "allocated_seconds": 70,
        },
        {
            "id": "later",
            "status": "todo",
            "created": 2,
            "max_attempts": 3,
            "budget_seconds": 90,
            "allocated_seconds": 0,
        },
    ]
    assert (
        dispatch._choose_task({"task": tasks, "attempt": []}, dispatch.profile(SPEC))[
            "id"
        ]
        == "later"
    )
