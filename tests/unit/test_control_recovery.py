"""Durable control delivery through real lifecycle locks and dispatcher threads.

Runtime and canonical task responses are explicit fixtures, not Docker or
external Polyphony qualification. Ordinary observation must remain read-only.
"""

from contextlib import contextmanager
from dataclasses import replace
import threading
import time
from types import SimpleNamespace

import pytest

from src.worker.lifecycle import (
    LifecycleConflict,
    RuntimeObservation,
    WorkerLifecycle,
    WorkerNotFound,
    WorkerScope,
    WorkerSpec,
)
from src.worker.managed_dispatch import ManagedDispatcher
from tests.unit.test_managed_dispatch import Authority, Runtime as DispatchRuntime
from tests.unit.test_worker_lifecycle import ContractRuntime


class ControlRuntime(ContractRuntime):
    def __init__(self):
        super().__init__()
        self.drain_calls = []
        self.drain_effects = set()
        self.lose_drain_reply = False

    def drain(self, worker_id, spec, runtime_id):
        self.drain_calls.append(worker_id)
        self.drain_effects.add(worker_id)
        self.results[worker_id] = RuntimeObservation("draining", runtime_id)
        if self.lose_drain_reply:
            self.lose_drain_reply = False
            raise ConnectionError("fixture lost drain reply after the effect")
        return self.results[worker_id]


def setup(tmp_path):
    runtime = ControlRuntime()
    lifecycle = WorkerLifecycle(tmp_path / "lifecycle.sqlite3", runtime)
    spec = WorkerSpec(
        "deployment", "github:github.com/example/repo", "slot", 1, "launch", "fixture"
    )
    worker = lifecycle.ensure_worker(spec)["worker_id"]
    return runtime, lifecycle, spec, worker


def operations(lifecycle, worker):
    with lifecycle._transaction() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT request_key,action,payload,completed FROM worker_operations "
                "WHERE worker_id=? AND action IN ('drain','stop') ORDER BY rowid",
                (worker,),
            )
        ]


@contextmanager
def held_lock(lifecycle, worker):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with lifecycle._runtime_lock(worker) as acquired:
            if acquired:
                yield
                return
        time.sleep(0.01)
    pytest.fail("could not acquire test-owned lifecycle lock")


def eventually(predicate, message="trusted dispatcher did not reconcile control"):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(message)


@contextmanager
def running(dispatcher):
    errors = []

    def execute():
        try:
            dispatcher.run()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert dispatcher.running.wait(3)
        yield
    finally:
        dispatcher.stop()
        thread.join(6)
        assert not thread.is_alive() and not errors


def test_busy_control_is_durable_and_readers_do_not_deliver_it(tmp_path):
    runtime, lifecycle, spec, worker = setup(tmp_path)
    lifecycle.begin_execution(spec.scope, worker, "root")
    lifecycle.begin_execution(spec.scope, worker, "child", parent_id="root")
    with held_lock(lifecycle, worker):
        accepted = lifecycle.drain_worker(spec.scope, worker, "original-drain")
        assert accepted["state"] == "draining" and not accepted["admission_open"]
        assert "intent awaits runtime confirmation" in accepted["reason"]
        assert accepted["active_executions"] == ["child", "root"]
        assert runtime.drain_calls == []
        original = operations(lifecycle, worker)
        assert len(original) == 1 and original[0]["completed"] == 0
    reopened = WorkerLifecycle(lifecycle.database, runtime)
    for _ in range(3):
        assert reopened.observe_worker(spec.scope, worker)["state"] == "draining"
        assert reopened.discover_workers(spec.scope)[0]["worker_id"] == worker
    assert runtime.drain_calls == []
    result = reopened.reconcile_control(spec.scope, worker)
    assert result["active_executions"] == ["child", "root"]
    assert not result["admission_open"] and runtime.stops == 0
    assert runtime.drain_calls == [worker]
    assert operations(reopened, worker) == [original[0] | {"completed": 1}]
    assert "intent awaits runtime confirmation" not in result["reason"]
    reopened.reconcile_control(spec.scope, worker)
    reopened.drain_worker(spec.scope, worker, "original-drain")
    assert runtime.drain_calls == [worker]
    with pytest.raises(WorkerNotFound):
        reopened.reconcile_control(WorkerScope("foreign", spec.repository), worker)
    with pytest.raises(LifecycleConflict):
        reopened.begin_execution(spec.scope, worker, "late-child", parent_id="root")


def test_uncertain_drain_reuses_original_intent_after_restart(tmp_path):
    runtime, lifecycle, spec, worker = setup(tmp_path)
    lifecycle.begin_execution(spec.scope, worker, "root")
    runtime.lose_drain_reply = True
    accepted = lifecycle.drain_worker(spec.scope, worker, "original-drain")
    assert accepted["state"] == "unknown" and "unknown" in accepted["reason"]
    original = operations(lifecycle, worker)
    assert original[0]["completed"] == 0
    reopened = WorkerLifecycle(lifecycle.database, runtime)
    result = reopened.reconcile_control(spec.scope, worker)
    assert result["state"] == "draining" and result["active_executions"] == ["root"]
    assert operations(reopened, worker) == [original[0] | {"completed": 1}]
    reopened.reconcile_control(spec.scope, worker)
    assert runtime.drain_calls == [worker, worker]
    assert runtime.drain_effects == {worker} and runtime.stops == 0


def test_replay_is_bounded_and_observed_terminal_state_prevents_more_signals(tmp_path):
    runtime, lifecycle, spec, worker = setup(tmp_path)
    with held_lock(lifecycle, worker):
        lifecycle.drain_worker(spec.scope, worker, "drain-1")
        lifecycle.drain_worker(spec.scope, worker, "drain-2")
        lifecycle.stop_worker(spec.scope, worker, "stop")
    reopened = WorkerLifecycle(lifecycle.database, runtime)
    assert reopened.reconcile_control(spec.scope, worker)["state"] == "stopped"
    assert [row["completed"] for row in operations(reopened, worker)] == [0, 0, 1]
    assert runtime.stops == 1 and runtime.drain_calls == []
    reopened.reconcile_control(spec.scope, worker)
    assert [row["completed"] for row in operations(reopened, worker)] == [1, 0, 1]
    reopened.reconcile_control(spec.scope, worker)
    assert [row["completed"] for row in operations(reopened, worker)] == [1, 1, 1]
    assert runtime.stops == 1 and runtime.drain_calls == []


def test_replayed_stop_retains_unresolved_root_and_child_capture(tmp_path):
    runtime, lifecycle, spec, worker = setup(tmp_path)
    lifecycle.begin_execution(spec.scope, worker, "root")
    lifecycle.begin_execution(spec.scope, worker, "child", parent_id="root")
    with held_lock(lifecycle, worker):
        lifecycle.stop_worker(spec.scope, worker, "stop")
    reopened = WorkerLifecycle(lifecycle.database, runtime)
    stopped = reopened.reconcile_control(spec.scope, worker)
    assert stopped["state"] == "unknown"
    assert stopped["active_executions"] == ["child", "root"]
    assert operations(reopened, worker)[0]["completed"] == 0
    assert runtime.stops == 1
    with pytest.raises(LifecycleConflict):
        reopened.ensure_worker(replace(spec, generation=2, launch_key="new"))
    for execution in ("root", "child"):
        reopened.record_execution_end(
            spec.scope,
            worker,
            execution,
            termination_confirmed=True,
            artifacts_captured=True,
        )
    assert reopened.reconcile_control(spec.scope, worker)["state"] == "stopped"
    assert runtime.stops == 1
    assert operations(reopened, worker)[0]["completed"] == 1


def test_restarted_dispatcher_delivers_an_already_accepted_control(tmp_path, monkeypatch):
    runtime, lifecycle, spec, worker = setup(tmp_path)
    lifecycle.begin_execution(spec.scope, worker, "root")
    lifecycle.begin_execution(spec.scope, worker, "child", parent_id="root")
    profile = SimpleNamespace(
        name=spec.profile,
        deployment_id=spec.deployment_id,
        repository=spec.repository,
        base_commit="a" * 40,
        max_seconds=60,
    )
    authority = Authority()
    tried_reconcile = threading.Event()
    reconcile = lifecycle.reconcile_control

    def observed_reconcile(*args):
        result = reconcile(*args)
        tried_reconcile.set()
        return result

    monkeypatch.setattr(lifecycle, "reconcile_control", observed_reconcile)

    def dispatcher(service):
        return ManagedDispatcher(
            service,
            runtime,
            authority,
            [profile],
            tmp_path / "dispatch",
            poll_interval_seconds=0.1,
            max_parallel=1,
        )

    with held_lock(lifecycle, worker):
        lifecycle.drain_worker(spec.scope, worker, "restart-drain")
        original = operations(lifecycle, worker)
        with running(dispatcher(lifecycle)):
            assert tried_reconcile.wait(4)
            assert runtime.drain_calls == []
    reopened = WorkerLifecycle(lifecycle.database, runtime)
    with running(dispatcher(reopened)):
        eventually(lambda: operations(reopened, worker)[0]["completed"] == 1)
    assert runtime.drain_calls == [worker]
    assert operations(reopened, worker) == [original[0] | {"completed": 1}]
    observed = reopened.observe_worker(spec.scope, worker)
    assert observed["active_executions"] == ["child", "root"]
    assert not observed["admission_open"] and runtime.stops == 0
    assert authority.claims == [] and runtime.launches == 1


def test_busy_scope_does_not_starve_another_scopes_control_delivery(tmp_path):
    runtime, lifecycle, first_spec, first_worker = setup(tmp_path)
    second_spec = replace(first_spec, repository="github:github.com/example/z-other")
    second_worker = lifecycle.ensure_worker(second_spec)["worker_id"]
    profiles = [
        SimpleNamespace(
            name=spec.profile,
            deployment_id=spec.deployment_id,
            repository=spec.repository,
            base_commit="a" * 40,
            max_seconds=60,
        )
        for spec in (first_spec, second_spec)
    ]
    authority = Authority()
    dispatcher = ManagedDispatcher(
        lifecycle,
        runtime,
        authority,
        profiles,
        tmp_path / "dispatch",
        poll_interval_seconds=0.1,
        max_parallel=1,
    )
    with held_lock(lifecycle, first_worker):
        lifecycle.drain_worker(first_spec.scope, first_worker, "first-drain")
        with held_lock(lifecycle, second_worker):
            lifecycle.drain_worker(second_spec.scope, second_worker, "second-drain")
        with running(dispatcher):
            eventually(lambda: operations(lifecycle, second_worker)[0]["completed"] == 1)
            assert operations(lifecycle, first_worker)[0]["completed"] == 0
            assert runtime.drain_calls == [second_worker]
    assert authority.claims == []


class WaitingRuntime(DispatchRuntime):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.finish = threading.Event()
        self.drained = threading.Event()
        self.drain_calls = 0

    def execute_attempt(self, *args, **kwargs):
        self.entered.set()
        if not self.finish.wait(10):
            raise RuntimeError("test execution was not released")
        return super().execute_attempt(*args, **kwargs)

    def drain(self, worker_id, spec, runtime_id):
        self.drain_calls += 1
        self.drained.set()
        return RuntimeObservation("draining", runtime_id)


def test_dispatcher_replays_control_while_its_execution_limit_is_full(tmp_path):
    runtime, authority = WaitingRuntime(), Authority()
    lifecycle = WorkerLifecycle(tmp_path / "lifecycle.sqlite3", runtime)
    spec = WorkerSpec(
        "test", "github:github.com/example/repo", "slot", 1, "launch", "coding"
    )
    profile = SimpleNamespace(
        name=spec.profile,
        deployment_id=spec.deployment_id,
        repository=spec.repository,
        base_commit="a" * 40,
        max_seconds=60,
    )
    dispatcher = ManagedDispatcher(
        lifecycle,
        runtime,
        authority,
        [profile],
        tmp_path / "dispatch",
        poll_interval_seconds=0.1,
        max_parallel=1,
    )
    worker = lifecycle.ensure_worker(spec)["worker_id"]
    child_registered = False
    with running(dispatcher):
        try:
            assert runtime.entered.wait(3)
            root = "b" * 32

            def admit_child():
                try:
                    lifecycle.begin_execution(spec.scope, worker, "child", parent_id=root)
                    return True
                except LifecycleConflict as error:
                    assert str(error) == (
                        "Runtime observation is in progress; admission is closed"
                    )
                    return False

            eventually(admit_child, "could not register child before drain")
            child_registered = True
            with held_lock(lifecycle, worker):
                accepted = lifecycle.drain_worker(spec.scope, worker, "busy-drain")
                assert accepted["active_executions"] == [root, "child"]
                assert runtime.drain_calls == 0
            assert runtime.drained.wait(4), "active execution starved control delivery"
            eventually(lambda: operations(lifecycle, worker)[0]["completed"] == 1)
            assert not runtime.finish.is_set()
            assert lifecycle.observe_worker(spec.scope, worker)["active_executions"] == [
                root,
                "child",
            ]
            assert runtime.drain_calls == 1 and len(authority.claims) == 1
        finally:
            if child_registered:
                lifecycle.record_execution_end(
                    spec.scope,
                    worker,
                    "child",
                    termination_confirmed=True,
                    artifacts_captured=True,
                )
            runtime.finish.set()
