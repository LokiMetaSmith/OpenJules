"""Real managed Docker execution through the model broker and dispatcher.

Model replies and canonical authority are explicit fixtures. This qualifies the
production OpenJules path, not an actual Polyphony authority or a live provider.
Once OPENJULES_MANAGED_DOCKER_TESTS=1 is set, unavailable Docker is a failure.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

import docker
import pytest

from src.worker.docker_runtime import DockerProfile, ManagedDockerRuntime
from src.worker.lifecycle import RuntimeCapabilities, WorkerLifecycle, WorkerSpec
from src.worker.managed_dispatch import ManagedDispatcher
from src.worker.model_broker import BrokerDenied, ModelBroker, ModelPolicy
from tests.integration.test_managed_agent_container import agent_image  # noqa: F401
from tests.unit.test_managed_agent import message, provider, response
from tests.unit.test_managed_model_integration import (
    FixtureAuthority,
    TOKEN_REFERENCE,
    TOKEN_VALUE,
    model_payload,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("OPENJULES_MANAGED_DOCKER_TESTS") != "1",
        reason="real managed Docker qualification requires explicit opt-in",
    ),
]


def test_real_managed_agent_captures_and_submits_once_without_guest_credentials(
    tmp_path, monkeypatch, agent_image
):
    client, image = agent_image
    source = tmp_path / "source"
    source.mkdir()
    (source / "answer.txt").write_text("old\n")
    (source / "AGENTS.md").write_text("Change only answer.txt and verify it.\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    base = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    # Controlled host-only values make accidental environment propagation
    # observable without reading or using any real provider credentials.
    monkeypatch.setenv(TOKEN_REFERENCE[4:], TOKEN_VALUE)
    monkeypatch.setenv(
        "MODEL_AUTHORITY_FIXTURE_TOKEN", "fixture-authority-only-on-host"
    )
    monkeypatch.setenv("MODEL_WORKER_FIXTURE_TOKEN", "fixture-worker-only-on-host")
    verification = [
        "python3",
        "-I",
        "-c",
        "import os,pathlib; assert os.getuid()==65532; "
        "assert pathlib.Path('answer.txt').read_text()=='implemented\\n'; "
        "assert not pathlib.Path('/var/run/docker.sock').exists(); "
        "assert not os.access('/control/deadline',os.W_OK); "
        "assert not any(k in os.environ for k in "
        "['MODEL_INTEGRATION_FIXTURE_TOKEN','MODEL_AUTHORITY_FIXTURE_TOKEN',"
        "'MODEL_WORKER_FIXTURE_TOKEN','POLYPHONY_RUNTIME_TOKEN','OPENJULES_WORKER_TOKEN',"
        "'OPENAI_API_KEY','ANTHROPIC_API_KEY','OPENROUTER_API_KEY']); "
        "print('MANAGED_AGENT_BOUNDARY_VERIFIED')",
    ]
    command = (
        "python3",
        "-I",
        "-S",
        "/opt/openjules/managed_runner.py",
        "--socket",
        "/run/openjules-model/model.sock",
        "--max-turns",
        "5",
        "--verify-json",
        json.dumps(verification),
    )
    profile = DockerProfile(
        "qualified-model-v1",
        "model-runtime-test",
        "github:github.com/fixture/model-runtime",
        str(source),
        image,
        command,
        base,
    )
    spec = WorkerSpec(
        profile.deployment_id, profile.repository, "slot", 1, "launch", profile.name
    )
    key = (spec.deployment_id, spec.repository, spec.profile)
    authority = FixtureAuthority()
    replies = [
        message("read_file", path="answer.txt"),
        message("write_file", path="answer.txt", content="implemented\n"),
        message("finish", summary="Changed answer.txt and requested review"),
    ]

    def deterministic_model(handler, requests):
        assert len(requests) <= len(replies), "unexpected duplicate inference sequence"
        response(handler, replies[len(requests) - 1])

    dispatcher = None
    worker_id = None
    with provider(deterministic_model) as (endpoint, requests):
        broker = ModelBroker(
            tmp_path / "models",
            {
                key: ModelPolicy(
                    endpoint,
                    "deterministic-integration-fixture",
                    TOKEN_REFERENCE,
                    max_calls=3,
                    max_output_tokens=2048,
                    max_total_output_tokens=6144,
                    request_timeout_seconds=5,
                )
            },
            lambda binding: dispatcher.model_permission(binding),
            peer_uids={key: profile.uid},
        )
        runtime = ManagedDockerRuntime(
            tmp_path / "runtime",
            [profile],
            lambda *_: RuntimeCapabilities(
                True, True, True, "explicit authority fixture"
            ),
            client,
            model_broker=broker,
        )
        try:
            lifecycle = WorkerLifecycle(tmp_path / "lifecycle.db", runtime)
            dispatcher = ManagedDispatcher(
                lifecycle,
                runtime,
                authority,
                [profile],
                tmp_path / "dispatch",
                model_broker=broker,
            )
            broker.check_ready()
            worker = lifecycle.ensure_worker(spec)
            worker_id = worker["worker_id"]
            assert worker["state"] == "ready"
            dispatcher.tick_worker(worker)
            delivery = dispatcher.journal.get(worker_id)
            assert delivery["phase"] == "completed", delivery
            attempt_id = json.loads(delivery["binding"])["binding"]["attempt_id"]
            receipt = runtime.inspect_attempt(worker_id, spec, attempt_id)
            assert receipt.exit_code == 0, receipt.log
            assert receipt.termination_confirmed and receipt.launch_fenced
            assert receipt.artifacts_captured and Path(receipt.capture_path).is_file()
            assert "+implemented" in receipt.patch and "-old" in receipt.patch
            assert "MANAGED_AGENT_BOUNDARY_VERIFIED" in receipt.log
            assert '"event": "configured_verification"' in receipt.log
            manifest = json.loads(receipt.manifest)
            assert manifest["attempt_id"] == attempt_id
            assert manifest["patch_present"] and manifest["log_present"]
            assert set(authority.artifacts) == {"patch", "log", "manifest"}
            assert authority.artifacts == {
                "patch": receipt.patch,
                "log": receipt.log,
                "manifest": receipt.manifest,
            }
            assert len(authority.claims) == len(authority.terminals) == 1
            assert authority.candidates == [
                {
                    "attempt_id": attempt_id,
                    "artifact_id": hashlib.sha256(receipt.patch.encode()).hexdigest(),
                }
            ]
            observed = lifecycle.observe_worker(spec.scope, worker_id)
            assert observed["active_executions"] == []
            assert observed["state"] == "stopped" and not observed["admission_open"]
            with tarfile.open(receipt.capture_path) as capture:
                assert (
                    capture.extractfile("workspace/run/repo/answer.txt").read()
                    == b"implemented\n"
                )
            assert (source / "answer.txt").read_text() == "old\n"

            with broker.connection() as db:
                model_record = db.execute(
                    "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
                ).fetchone()
                assert model_record["revoked"] == 1
                assert (
                    model_record["deadline"] == runtime._attempt(worker_id)["deadline"]
                )
                assert db.execute(
                    "SELECT COUNT(*),SUM(reserved_tokens) FROM calls WHERE worker_id=?",
                    (worker_id,),
                ).fetchone()[:] == (3, 6144)
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM calls WHERE status!='complete'"
                    ).fetchone()[0]
                    == 0
                )
            with pytest.raises(BrokerDenied, match="not_authorized"):
                broker.complete(worker_id, model_payload("after-terminal"))
            dispatcher.tick_worker(observed)
            assert len(authority.claims) == len(authority.candidates) == 1
            assert len(requests) == 3
            assert all(r["authorization"] == "Bearer " + TOKEN_VALUE for r in requests)
            assert authority.permission_checks >= 1 + len(requests)
            assert TOKEN_VALUE not in receipt.log + receipt.patch + receipt.manifest
            assert "fixture-authority-only-on-host" not in receipt.log
            assert "fixture-worker-only-on-host" not in receipt.log

            container = client.containers.get(runtime._name(worker_id))
            container.reload()
            assert container.attrs["State"]["Running"] is False
            assert container.attrs["HostConfig"]["NetworkMode"] == "none"
            assert container.attrs["HostConfig"]["ReadonlyRootfs"] is True
            mounts = container.attrs["Mounts"]
            mounted_model = next(
                m for m in mounts if m["Destination"] == "/run/openjules-model"
            )
            assert mounted_model["RW"] is False
            assert mounted_model["Source"] == str(broker.root / worker_id)
            assert not any(m["Destination"] == "/var/run/docker.sock" for m in mounts)
            assert (
                container.attrs["Config"]["Labels"]["openjules.model_policy"]
                == broker.policy_digests[key]
            )
        finally:
            if dispatcher is not None:
                dispatcher.stop()
            broker.close()
            if worker_id is not None:
                # Remove only this test's immutable container and volume.
                try:
                    client.containers.get(runtime._name(worker_id)).remove(force=True)
                except docker.errors.NotFound:
                    pass
                try:
                    client.volumes.get(
                        "openjules-work-"
                        + hashlib.sha256(worker_id.encode()).hexdigest()[:40]
                    ).remove()
                except docker.errors.NotFound:
                    pass
