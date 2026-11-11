"""Adversarial Docker call fixtures, not proof of container isolation."""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
from types import SimpleNamespace

import pytest

from src.worker.child_runtime import ChildRuntime
from src.worker.child_types import WATCHDOG_UID
from src.worker.docker_runtime import ManagedDockerRuntime
from src.worker.lifecycle import LifecycleConflict, RuntimeUnavailable, WorkerLifecycle
from tests.unit.test_child_supervisor import supervised, spawn  # noqa: F401
from tests.unit.test_managed_docker_runtime import archive, setup_runtime  # noqa: F401


class FixtureEngine:
    def __init__(self):
        self.created = []
        self.started = []
        self.puts = []
        self.fail_create = False
        self.fail_start = False
        self.on_prepare = None
        self.states = {}
        self.row = {"retired": 0, "receipt": None, "start_confirmed": 1}
        self.container = SimpleNamespace(
            id="fixture-container",
            attrs={"State": {"Running": True}},
            put_archive=self.put_archive,
        )
        self.client = SimpleNamespace(
            api=SimpleNamespace(
                exec_create=self.exec_create,
                exec_start=self.exec_start,
                exec_inspect=lambda identity: self.states[identity],
            ),
            info=lambda: {"SecurityOptions": ["name=seccomp,profile=builtin"]},
        )

    @contextmanager
    def _lock(self, worker_id):
        yield True

    def _row(self, worker_id, spec):
        return self.row

    def _container(self, worker_id, spec, row):
        return self.container

    _tar = staticmethod(ManagedDockerRuntime._tar)

    def put_archive(self, path, content):
        self.puts.append((path, content))
        if self.on_prepare:
            self.on_prepare()
        return True

    def exec_create(self, container_id, command, **options):
        assert container_id == self.container.id
        identity = "fixture-exec-" + str(len(self.created) + 1)
        self.created.append((identity, command, options))
        self.states[identity] = {"Running": False, "ExitCode": 0}
        if options["user"] != f"{WATCHDOG_UID}:{WATCHDOG_UID}" and self.fail_create:
            raise TimeoutError("fixture lost create reply")
        return {"Id": identity}

    def exec_start(self, identity, detach):
        assert detach is True
        self.started.append(identity)
        _, _, options = next(item for item in self.created if item[0] == identity)
        if options["user"] != f"{WATCHDOG_UID}:{WATCHDOG_UID}":
            self.states[identity] = {"Running": True, "ExitCode": None}
            if self.fail_start:
                raise TimeoutError("fixture lost start reply")


def installed(supervised):
    supervisor, root, _, _ = supervised
    engine = FixtureEngine()
    supervisor.runtime = engine
    supervisor.driver = ChildRuntime(engine, supervisor)
    return supervisor, root, engine


@pytest.mark.parametrize("failure", ["create", "start"])
def test_lost_exec_reply_never_creates_or_starts_another_child(supervised, failure):
    supervisor, root, engine = installed(supervised)
    setattr(engine, "fail_" + failure, True)
    response = spawn(supervisor, root)
    child = supervisor.journal.child(root.worker_id, response["child_id"])
    assert child["phase"] == "uncertain" and supervisor.retiring(root.worker_id)
    assert bool(child["exec_dispatched"]) is (failure == "start")
    before = (len(engine.created), len(engine.started))
    supervisor.driver.launch(root, child, root.monotonic_deadline)
    assert (len(engine.created), len(engine.started)) == before
    assert (
        len(
            [
                item
                for item in engine.created
                if item[2]["user"] != f"{WATCHDOG_UID}:{WATCHDOG_UID}"
            ]
        )
        == 1
    )


def test_child_uid_workspace_and_deadline_are_bound_before_exec(supervised):
    supervisor, root, engine = installed(supervised)
    target = time.monotonic() + 2
    engine.on_prepare = lambda: supervisor.journal.bind(
        replace(root, monotonic_deadline=target)
    )
    response = spawn(supervisor, root)
    child = supervisor.journal.child(root.worker_id, response["child_id"])
    assert child["phase"] == "running" and child["armed_deadline"] <= target
    assert child["exec_dispatched"] == 1 and child["exec_id"] == engine.started[-1]
    control, execution = engine.created
    assert control[2]["user"] == "65534:65534"
    assert float(control[1][-1]) == child["armed_deadline"]
    assert control[1][-2] == child["child_id"]
    assert execution[2]["user"] == "60000:60000" and execution[2]["privileged"] is False
    approved = json.loads(execution[1][-1])
    assert approved["base"] == root.base
    assert approved["command"][-4:] == [
        "--socket",
        f"/run/openjules-model/children/{child['child_id']}/model.sock",
        "--max-turns",
        "2",
    ]
    with tarfile.open(fileobj=io.BytesIO(engine.puts[0][1])) as uploaded:
        assert {item.uid for item in uploaded} == {60000}
        assert uploaded.getmember(child["child_id"]).mode == 0o700
        assert uploaded.getmember(child["child_id"] + "/input.txt").mode == 0o400


@pytest.mark.parametrize("change", ["expire", "drain", "phase"])
def test_preparation_cannot_launch_after_authorization_changes(supervised, change):
    supervisor, root, engine = installed(supervised)

    def changed():
        if change == "expire":
            supervisor.journal.bind(
                replace(root, monotonic_deadline=time.monotonic() - 1)
            )
        elif change == "drain":
            supervisor.close_admission(root.worker_id)
        else:
            supervised[3][0] = False

    engine.on_prepare = changed
    response = spawn(supervisor, root)
    child = supervisor.journal.child(root.worker_id, response["child_id"])
    assert child["phase"] == "uncertain" and supervisor.retiring(root.worker_id)
    assert not engine.created and not engine.started


def test_exec_return_is_provisional_until_whole_container_fence(supervised):
    supervisor, root, engine = installed(supervised)
    child_id = spawn(supervisor, root)["child_id"]
    child = supervisor.journal.child(root.worker_id, child_id)
    engine.states[child["exec_id"]] = {"Running": False, "ExitCode": 0}
    returned = supervisor.driver.observe(root, child)
    assert returned["phase"] == "exec_returned" and returned["capture"] is None
    assert returned["armed_deadline"] == child["armed_deadline"]
    with pytest.raises(RuntimeUnavailable, match="whole-worker fence"):
        supervisor.driver.final_capture(
            root,
            returned,
            SimpleNamespace(
                termination_confirmed=False,
                launch_fenced=False,
                artifacts_captured=False,
            ),
        )
    assert child_id in supervised[2].active


def final_receipt(tmp_path, child_id, files=None):
    data = archive(
        files
        if files is not None
        else {
            f"workspace/children/{child_id}/run/change.patch": b"diff child\n",
            f"workspace/children/{child_id}/run/output.log": b"child output\n",
            f"workspace/children/{child_id}/run/repo/untracked.txt": b"preserved raw work\n",
        }
    )
    path = tmp_path / "capture.tar"
    path.write_bytes(data)
    return SimpleNamespace(
        termination_confirmed=True,
        launch_fenced=True,
        artifacts_captured=True,
        capture_path=str(path),
        capture_sha256=hashlib.sha256(data).hexdigest(),
        manifest='{"root":"fenced"}',
    )


def test_final_capture_binds_child_to_immutable_raw_workspace(supervised, tmp_path):
    supervisor, root, engine = installed(supervised)
    child_id = spawn(supervisor, root)["child_id"]
    child = supervisor.journal.child(root.worker_id, child_id)
    receipt = final_receipt(tmp_path, child_id)
    captured = supervisor.driver.final_capture(root, child, receipt)
    assert captured["patch"] == "diff child\n" and captured["log"] == "child output\n"
    manifest = json.loads(captured["manifest"])
    assert (
        manifest["child_id"] == child_id
        and manifest["root_attempt_id"] == root.attempt_id
    )
    assert manifest["archive_sha256"] == receipt.capture_sha256
    assert manifest["termination_method"] == "whole-container fence"
    assert manifest["uid"] == 60000 and manifest["exec_start_dispatched"] is True
    Path(receipt.capture_path).write_bytes(b"changed after capture")
    with pytest.raises(RuntimeUnavailable, match="archive changed"):
        supervisor.driver.final_capture(root, child, receipt)


def test_missing_or_symlinked_child_capture_holds_reservation(supervised, tmp_path):
    supervisor, root, engine = installed(supervised)
    child_id = spawn(supervisor, root)["child_id"]
    child = supervisor.journal.child(root.worker_id, child_id)
    receipt = final_receipt(
        tmp_path, child_id, {"workspace/run/change.patch": b"root diff"}
    )
    with pytest.raises(RuntimeUnavailable, match="workspace is missing"):
        supervisor.driver.final_capture(root, child, receipt)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as result:
        item = tarfile.TarInfo(f"workspace/children/{child_id}/run/change.patch")
        item.type = tarfile.SYMTYPE
        item.linkname = "/workspace/run/change.patch"
        result.addfile(item)
    content = output.getvalue()
    Path(receipt.capture_path).write_bytes(content)
    receipt.capture_sha256 = hashlib.sha256(content).hexdigest()
    with pytest.raises(RuntimeUnavailable, match="regular file"):
        supervisor.driver.final_capture(root, child, receipt)
    assert child_id in supervised[2].active


def test_child_readiness_rejects_unqualified_uid_mapping(supervised):
    supervisor, root, engine = installed(supervised)
    supervisor.driver.check_topology()
    for options in (None, ["name=rootless"], ["name=userns"], [None]):
        engine.client.info = lambda: {"SecurityOptions": options}
        with pytest.raises(RuntimeUnavailable, match="UID mapping"):
            supervisor.driver.check_topology()


def test_root_runtime_releases_launch_lock_for_real_lifecycle_child_admission(
    setup_runtime, tmp_path
):
    original, client, profile, spec = setup_runtime
    from tests.unit.test_child_authority_contract import policy

    key = (spec.deployment_id, spec.repository, spec.profile)
    model_root = tmp_path / "model-mount-fixture"
    model_root.mkdir()

    def prepare(worker_id, _spec):
        directory = model_root / worker_id
        directory.mkdir(exist_ok=True)
        return directory

    model = SimpleNamespace(
        policies={key: object()}, policy_digests={key: "m" * 64}, prepare_worker=prepare
    )
    client.info = lambda: {"SecurityOptions": ["name=seccomp"]}
    client.api.exec_inspect = lambda identity: {
        "Running": identity != "exec-control",
        "ExitCode": 0 if identity == "exec-control" else None,
    }
    runtime = ManagedDockerRuntime(
        tmp_path / "child-runtime",
        [profile],
        original.readiness,
        client,
        model_broker=model,
        child_policies={key: policy()},
    )
    closed = []
    runtime.child_supervisor = SimpleNamespace(
        driver=SimpleNamespace(check_topology=lambda: None),
        endpoints=SimpleNamespace(check_ready=lambda: None),
        enabled=lambda _spec: True,
        prepare_worker=lambda *_: None,
        close_admission=lambda worker_id: closed.append(worker_id),
        mark_retiring=lambda *_: None,
    )
    lifecycle = WorkerLifecycle(tmp_path / "real-lifecycle.db", runtime)
    worker = lifecycle.ensure_worker(spec)
    worker_id = worker["worker_id"]
    lifecycle.begin_execution(spec.scope, worker_id, "root-attempt")
    result = runtime.execute_attempt(
        worker_id, spec, "root-attempt", "fixture task", 20
    )
    assert result.state == "running" and not result.termination_confirmed
    assert client.container.stops == 0
    with runtime._lock(worker_id) as acquired:
        assert acquired
    lifecycle.begin_execution(
        spec.scope, worker_id, "child-one", parent_id="root-attempt"
    )
    assert set(
        lifecycle.observe_worker(spec.scope, worker_id)["active_executions"]
    ) == {"root-attempt", "child-one"}
    assert lifecycle.root_admission_open(spec.scope, worker_id, "root-attempt")
    with tarfile.open(fileobj=io.BytesIO(client.container.puts[0][1])) as initialized:
        assert initialized.getmember("children").uid == WATCHDOG_UID
        assert initialized.getmember("children").mode == 0o711
    assert (
        client.container.kwargs["labels"]["openjules.child_policy"]
        == policy().fingerprint
    )
    assert "/control/children" in client.container.kwargs["entrypoint"][-1]
    lifecycle.drain_worker(spec.scope, worker_id, "drain-once")
    assert closed == [worker_id] and not lifecycle.root_admission_open(
        spec.scope, worker_id, "root-attempt"
    )
    with pytest.raises(LifecycleConflict):
        lifecycle.begin_execution(
            spec.scope, worker_id, "late-child", parent_id="root-attempt"
        )


def test_existing_root_profile_cannot_silently_acquire_or_change_child_policy(
    setup_runtime, tmp_path
):
    runtime, client, profile, spec = setup_runtime
    from tests.unit.test_child_authority_contract import policy

    key = (spec.deployment_id, spec.repository, spec.profile)
    model = SimpleNamespace(policies={key: object()})
    before = profile.fingerprint
    with pytest.raises(LifecycleConflict, match="child policy changed"):
        ManagedDockerRuntime(
            runtime.root,
            [profile],
            runtime.readiness,
            client,
            model_broker=model,
            child_policies={key: policy()},
        )
    assert profile.fingerprint == before
    upgraded = ManagedDockerRuntime(
        tmp_path / "new-profile",
        [profile],
        runtime.readiness,
        client,
        model_broker=model,
        child_policies={key: policy()},
    )
    for changes in ({}, {key: replace(policy(), max_children=1)}):
        with pytest.raises(LifecycleConflict, match="child policy changed"):
            ManagedDockerRuntime(
                upgraded.root,
                [profile],
                runtime.readiness,
                client,
                model_broker=model,
                child_policies=changes,
            )
    # Upgrading an old database without a child_profiles row also fails closed.
    with runtime._db() as db:
        db.execute("DELETE FROM child_profiles")
    with pytest.raises(LifecycleConflict, match="new approved profile"):
        ManagedDockerRuntime(
            runtime.root,
            [profile],
            runtime.readiness,
            client,
            model_broker=model,
            child_policies={key: policy()},
        )
