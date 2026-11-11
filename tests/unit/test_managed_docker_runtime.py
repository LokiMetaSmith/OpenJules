"""Adversarial driver contracts. Fake Docker is not confinement evidence."""

from dataclasses import replace
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace

from docker.errors import NotFound
import pytest

from src.worker.docker_runtime import DockerProfile, ManagedDockerRuntime
from src.worker.lifecycle import (
    LifecycleConflict,
    RuntimeCapabilities,
    WorkerSpec,
    WorkerLifecycle,
)

IMAGE = "sha256:" + "a" * 64


def archive(files=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as result:
        for name, data in (
            files
            or {
                "workspace/run/output.log": b"actual output\n",
                "workspace/run/change.patch": b"diff --git a/a b/a\n",
            }
        ).items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            result.addfile(item, io.BytesIO(data))
    return output.getvalue()


class FakeVolume:
    def __init__(self, labels):
        self.attrs = {"Labels": labels}

    def reload(self):
        pass


class FakeContainer:
    def __init__(self, client, kwargs):
        self.client = client
        self.id = hashlib.sha256(kwargs["name"].encode()).hexdigest()
        self.kwargs = kwargs
        self.attrs = {
            "Config": {"Labels": kwargs["labels"]},
            "State": {
                "Running": False,
                "Status": "created",
                "StartedAt": "0001-01-01T00:00:00Z",
            },
        }
        self.puts = []
        self.starts = 0
        self.stops = 0

    def reload(self):
        if self.client.fail_observe:
            raise TimeoutError("private backend details")

    def put_archive(self, path, data):
        self.puts.append((path, data))
        return True

    def start(self):
        self.starts += 1
        if self.client.late_start:
            raise TimeoutError("lost start response")
        self.publish_start()

    def publish_start(self):
        self.attrs["State"].update(
            Running=True, Status="running", StartedAt="2026-11-10T00:00:00Z"
        )

    def stop(self, timeout):
        self.stops += 1
        if self.client.fail_stop:
            raise TimeoutError("stop did not resolve")
        self.attrs["State"].update(Running=False, Status="exited")

    def get_archive(self, path):
        assert path == "/workspace"
        if self.client.fail_capture:
            raise OSError("capture store failed")
        return iter([self.client.archive]), {}


class FakeDocker:
    def __init__(self):
        self.container = None
        self.all_containers = {}
        self.unpublished = None
        self.volume = None
        self.all_volumes = {}
        self.created = 0
        self.executed = 0
        self.late_create = False
        self.late_start = False
        self.fail_observe = False
        self.fail_stop = False
        self.fail_capture = False
        self.archive = archive()
        self.containers = SimpleNamespace(get=self.get, create=self.create)
        self.volumes = SimpleNamespace(create=self.create_volume)
        self.images = SimpleNamespace(get=lambda image: SimpleNamespace(id=IMAGE))
        self.api = SimpleNamespace(
            exec_create=self.exec_create,
            exec_start=self.exec_start,
            exec_inspect=lambda execution: {"Running": False, "ExitCode": 0},
        )
        self.exec_options = None

    def ping(self):
        return True

    def get(self, identity):
        for item in self.all_containers.values():
            if identity in {item.id, item.kwargs["name"]}:
                return item
        if not self.container or identity not in {
            self.container.id,
            self.container.kwargs["name"],
        }:
            raise NotFound("not found")
        return self.container

    def create(self, image, **kwargs):
        if kwargs["name"] in self.all_containers:
            raise RuntimeError("name already allocated")
        self.created += 1
        container = FakeContainer(self, kwargs)
        if self.late_create:
            self.unpublished = container
            raise TimeoutError("creation reply lost")
        self.container = container
        self.all_containers[kwargs["name"]] = container
        return container

    def create_volume(self, name, labels):
        if name not in self.all_volumes:
            self.all_volumes[name] = FakeVolume(labels)
        self.volume = self.all_volumes[name]
        return self.volume

    def exec_create(self, identity, command, **kwargs):
        assert identity == self.container.id
        self.exec_options = kwargs | {"command": command}
        return {
            "Id": (
                "exec-control" if kwargs["user"] == "65534:65534" else "exec-immutable"
            )
        }

    def exec_start(self, execution, detach):
        assert detach
        if execution != "exec-control":
            self.executed += 1


@pytest.fixture
def setup_runtime(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        check=True,
    )
    base = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    profile = DockerProfile(
        "approved-v1",
        "deployment",
        "github:github.com/owner/repo",
        str(source),
        IMAGE,
        ("python3", "-c", "print('work')"),
        base,
    )
    client = FakeDocker()
    callback = lambda scope, profile: RuntimeCapabilities(True, True, True, "")
    runtime = ManagedDockerRuntime(tmp_path / "runtime", [profile], callback, client)
    spec = WorkerSpec(
        profile.deployment_id, profile.repository, "slot", 1, "launch", profile.name
    )
    return runtime, client, profile, spec


def test_profile_rejects_mutable_images_watchdog_uid_and_secret_injection(
    setup_runtime,
):
    runtime, client, profile, spec = setup_runtime
    with pytest.raises(ValueError):
        replace(profile, image="python:latest")
    with pytest.raises(ValueError):
        replace(profile, uid=65534)
    with pytest.raises(ValueError):
        replace(profile, environment_refs={"DOCKER_HOST": "env:DOCKER_HOST"})
    with pytest.raises(LifecycleConflict):
        ManagedDockerRuntime(
            runtime.root,
            [replace(profile, command=("other",))],
            runtime.readiness,
            client,
        )


def test_ready_requires_real_source_and_canonical_readiness(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    assert runtime.capabilities(spec.scope, profile.name).usable
    runtime.readiness = lambda scope, profile: RuntimeCapabilities(
        reason="canonical authority unavailable"
    )
    assert not runtime.capabilities(spec.scope, profile.name).managed_dispatch
    assert client.created == 0


def test_ensure_survives_restart_and_applies_container_boundary(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    first = runtime.ensure("worker", spec)
    assert first.state == "ready"
    reopened = ManagedDockerRuntime(runtime.root, [profile], runtime.readiness, client)
    assert reopened.ensure("worker", spec).runtime_id == first.runtime_id
    assert client.created == client.container.starts == 1
    config = client.container.kwargs
    assert config["network_mode"] == "none" and config["read_only"]
    assert config["user"] == "65534:65534" and config["cap_drop"] == ["ALL"]
    assert config["security_opt"] == ["no-new-privileges:true"]
    assert config["restart_policy"] == {"Name": "no"}
    assert config["mem_limit"] == config["memswap_limit"] == profile.memory_bytes
    mounted = [
        path for path, item in config["volumes"].items() if item["bind"] == "/source"
    ]
    assert len(mounted) == 1 and mounted[0] != profile.source_path
    assert Path(mounted[0]).is_relative_to(runtime.root / "sources")
    assert config["volumes"][mounted[0]]["mode"] == "ro"
    assert "docker.sock" not in json.dumps(config)


def test_late_start_cannot_be_mistaken_for_terminal_fence(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    client.late_start = True
    assert runtime.ensure("worker", spec).state == "unknown"
    reopened = ManagedDockerRuntime(runtime.root, [profile], runtime.readiness, client)
    first_stop = reopened.stop("worker", spec, "docker:" + client.container.id)
    assert first_stop.state == "unknown"
    assert not first_stop.launch_fenced and not first_stop.termination_confirmed
    assert client.container.stops == 0
    client.container.publish_start()
    final = reopened.stop("worker", spec, "docker:" + client.container.id)
    assert (
        final.termination_confirmed and final.artifacts_captured and final.launch_fenced
    )
    assert reopened.ensure("worker", spec).state == "stopped"
    assert client.container.starts == 1


def test_late_create_is_discovered_and_stopped_without_start(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    client.late_create = True
    assert runtime.ensure("worker", spec).state == "unknown"
    assert not runtime.stop("worker", spec, None).launch_fenced
    client.container = client.unpublished
    terminal = runtime.stop("worker", spec, None)
    assert terminal.launch_fenced and terminal.not_started
    assert client.container.starts == 0
    assert runtime.ensure("worker", spec).state == "stopped"


def test_foreign_container_cannot_be_adopted_or_signalled(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    ready = runtime.ensure("worker", spec)
    client.container.attrs["Config"]["Labels"]["openjules.worker"] = "foreign"
    result = runtime.stop("worker", spec, ready.runtime_id)
    assert result.state == "unknown" and not result.termination_confirmed
    assert client.container.stops == 0


def test_execution_retires_captures_and_never_runs_twice(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    runtime.ensure("worker", spec)
    receipt = runtime.execute_attempt("worker", spec, "attempt", "requirements", 10)
    assert (
        receipt.termination_confirmed
        and receipt.artifacts_captured
        and receipt.launch_fenced
    )
    assert receipt.exit_code == 0 and receipt.log == "actual output\n"
    assert Path(receipt.capture_path).is_file()
    assert json.loads(receipt.manifest)["patch_present"] is True
    assert client.exec_options["user"] == "65532:65532"
    assert (
        runtime.execute_attempt("worker", spec, "attempt", "requirements", 5) == receipt
    )
    assert runtime.inspect_attempt("worker", spec, "attempt") == receipt
    assert client.executed == 1
    with pytest.raises(LifecycleConflict):
        runtime.execute_attempt("worker", spec, "attempt", "changed requirements", 5)
    with pytest.raises(LifecycleConflict):
        runtime.execute_attempt("worker", spec, "new-attempt", "requirements", 5)


def test_capture_failure_holds_reservation_and_retry_recovers(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    runtime.ensure("worker", spec)
    client.fail_capture = True
    receipt = runtime.execute_attempt("worker", spec, "attempt", "requirements", 10)
    assert not receipt.artifacts_captured
    client.fail_capture = False
    result = runtime.inspect_attempt("worker", spec, "attempt")
    assert result.artifacts_captured and result.launch_fenced
    assert client.executed == 1


def test_cancel_before_dispatch_is_a_permanent_attempt_tombstone(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    receipt = runtime.cancel_attempt("worker", spec, "attempt", "requirements")
    assert (
        receipt.termination_confirmed
        and receipt.artifacts_captured
        and receipt.launch_fenced
    )
    assert (
        runtime.execute_attempt("worker", spec, "attempt", "requirements", 10)
        == receipt
    )
    assert client.created == client.executed == 0


def test_capture_byte_limit_preserves_container_and_unknown_capacity(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    runtime = ManagedDockerRuntime(
        runtime.root / "limited",
        [replace(profile, max_archive_bytes=1024)],
        runtime.readiness,
        client,
    )
    runtime.ensure("worker", spec)
    result = runtime.stop("worker", spec, None)
    assert result.termination_confirmed and result.launch_fenced
    assert not result.artifacts_captured and result.state == "unknown"


def test_tar_paths_are_not_extracted_onto_the_host(setup_runtime, tmp_path):
    runtime, client, profile, spec = setup_runtime
    client.archive = archive(
        {"../../escaped": b"untrusted", "workspace/run/output.log": b"log"}
    )
    runtime.ensure("worker", spec)
    receipt = runtime.execute_attempt("worker", spec, "attempt", "requirements", 10)
    assert receipt.artifacts_captured
    assert not (tmp_path / "escaped").exists()
    assert not json.loads(receipt.manifest)["patch_present"]


def test_duplicate_or_symlink_evidence_is_not_claimed_as_captured(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        link = tarfile.TarInfo("workspace/run/change.patch")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    client.archive = output.getvalue()
    runtime.ensure("worker", spec)
    receipt = runtime.execute_attempt("worker", spec, "attempt", "requirements", 10)
    assert not receipt.artifacts_captured


def test_idle_recovery_releases_only_verified_dead_incarnation(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    lifecycle = WorkerLifecycle(runtime.root / "lifecycle.sqlite3", runtime)
    worker = lifecycle.ensure_worker(spec)
    worker_id = worker["worker_id"]
    assert runtime.recover_idle(worker_id, spec).state == "ready"
    assert client.container.stops == 0
    client.container.attrs["State"].update(Running=False, Status="exited")
    assert lifecycle.observe_worker(spec.scope, worker_id)["state"] == "unknown"
    recovered = runtime.recover_idle(worker_id, spec)
    assert (
        recovered.termination_confirmed
        and recovered.artifacts_captured
        and recovered.launch_fenced
    )
    assert lifecycle.observe_worker(spec.scope, worker_id)["state"] == "stopped"
    replacement = lifecycle.ensure_worker(
        replace(spec, generation=2, launch_key="replacement")
    )
    assert replacement["state"] == "ready" and replacement["worker_id"] != worker_id


def test_idle_recovery_preserves_missing_and_uncertain_workers(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    client.late_start = True
    runtime.ensure("worker", spec)
    assert not runtime.recover_idle("worker", spec).launch_fenced
    client.fail_observe = True
    assert runtime.recover_idle("worker", spec).state == "unknown"
    assert runtime.recover_idle("absent", spec).state == "unknown"


def test_idle_recovery_never_substitutes_for_attempt_reconciliation(setup_runtime):
    runtime, client, profile, spec = setup_runtime
    runtime.ensure("worker", spec)
    runtime.execute_attempt("worker", spec, "attempt", "requirements", 10)
    assert runtime.recover_idle("worker", spec).state == "unknown"


def test_deadline_is_captured_before_lock_delay(setup_runtime, monkeypatch):
    runtime, client, profile, spec = setup_runtime
    runtime.ensure("worker", spec)
    current = [100.0]
    monkeypatch.setattr("src.worker.docker_runtime.time.monotonic", lambda: current[0])
    original = runtime._lock

    @contextmanager
    def delayed(worker_id):
        with original(worker_id) as acquired:
            current[0] += 5
            yield acquired

    monkeypatch.setattr(runtime, "_lock", delayed)
    receipt = runtime.execute_attempt("worker", spec, "attempt", "requirements", 1)
    assert receipt.termination_confirmed and receipt.launch_fenced
    assert runtime._attempt("worker")["deadline"] == 101
    assert client.executed == 0


def test_fixed_deadline_expires_during_helper_preparation(setup_runtime, monkeypatch):
    runtime, client, profile, spec = setup_runtime
    runtime.ensure("worker", spec)
    current = [100.0]
    monkeypatch.setattr("src.worker.docker_runtime.time.monotonic", lambda: current[0])
    create = client.api.exec_create

    def delayed_create(*args, **kwargs):
        result = create(*args, **kwargs)
        current[0] += 2
        return result

    client.api.exec_create = delayed_create
    receipt = runtime.execute_attempt(
        "worker", spec, "attempt", "requirements", 10, deadline_monotonic=101
    )
    assert receipt.termination_confirmed and receipt.launch_fenced
    assert runtime._attempt("worker")["deadline"] == 101
    assert client.executed == 0
