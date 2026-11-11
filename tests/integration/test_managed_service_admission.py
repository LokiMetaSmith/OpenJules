"""Actual managed app/client with HTTP authority and Docker preflight fixtures.

The authority deliberately advertises health without a supported protocol.
FakeDocker supplies image/daemon preflight. Source snapshots use real Git.
No container or model runs.
"""

from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import httpx
import pytest

from src.worker.managed_service import build_app, load_configuration
from tests.unit.test_managed_docker_runtime import setup_runtime  # noqa: F401

pytestmark = pytest.mark.integration
TOKEN = "managed-admission-runtime-fixture-credential"
WORKER_TOKEN = "managed-admission-worker-fixture-credential"


@contextmanager
def authority_server(profile):
    state = {"mode": "supported", "requests": []}
    supported = {
        "schema_version": 1,
        "deployment_id": profile.deployment_id,
        "repository": profile.repository,
        "service": "keystone-polyphony",
        "protocol": "managed-runtime-v1",
        "profile": profile.name,
        "base": profile.base_commit,
        "managed_dispatch": True,
        "external_root": True,
        "profile_supported": True,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            state["requests"].append(
                (self.command, path, self.headers.get("Authorization"))
            )
            status, payload = 500, {"error": "unexpected fixture request"}
            if self.command == "GET" and path == "/health":
                status, payload = 200, {"status": "ok", "service": "keystone-polyphony"}
            elif self.command == "GET" and path == "/managed/capabilities":
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    status, payload = 403, {"error": "not_authorized"}
                elif state["mode"] == "missing":
                    status, payload = 404, {"error": "unsupported endpoint"}
                elif state["mode"] == "wrong_protocol":
                    status, payload = 200, supported | {
                        "protocol": "unsupported-fixture-v0"
                    }
                else:
                    status, payload = 200, supported
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", state
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)


def configuration(tmp_path, profile, endpoint):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({WORKER_TOKEN: [asdict(profile.scope)]}))
    credentials.chmod(0o600)
    path = tmp_path / "managed.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state_directory": str(tmp_path / "service"),
                "lifecycle_database": str(tmp_path / "service-lifecycle.sqlite3"),
                "credentials_file": str(credentials),
                "authority": {
                    "endpoint": endpoint,
                    "token_ref": "env:ADMISSION_RUNTIME_TOKEN",
                },
                "profiles": [asdict(profile)],
                "poll_interval_seconds": 0.1,
            }
        )
    )
    return path


def assert_no_execution(app, docker_fixture):
    lifecycle = app.state.worker_lifecycle
    assert docker_fixture.created == docker_fixture.executed == 0
    assert not docker_fixture.all_containers and not docker_fixture.all_volumes
    with sqlite3.connect(lifecycle.database) as db:
        for table in ("managed_workers", "worker_operations", "worker_executions"):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    with lifecycle.runtime._db() as db:
        assert db.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0
    with app.state.managed_dispatcher.journal.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert app.state.model_broker is None
    assert app.state.child_supervisor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "wrong_protocol"])
async def test_healthy_authority_without_managed_protocol_cannot_admit_or_fallback(
    tmp_path, monkeypatch, setup_runtime, mode
):
    _, docker_fixture, profile, spec = setup_runtime
    monkeypatch.setenv("ADMISSION_RUNTIME_TOKEN", TOKEN)

    def no_fallback(*_args, **_kwargs):
        raise AssertionError(
            "managed admission initialized a legacy or real Docker path"
        )

    monkeypatch.setattr("docker.DockerClient", no_fallback)
    monkeypatch.setattr("docker.from_env", no_fallback)
    monkeypatch.setattr("src.polyphony.client.PolyphonyClient.__init__", no_fallback)
    monkeypatch.setattr(
        "src.polyphony.manager.PolyphonySwarmManager.__init__", no_fallback
    )
    with authority_server(profile) as (endpoint, authority):
        path = configuration(tmp_path, profile, endpoint)
        app = build_app(*load_configuration(path))
        # Only Docker is a driver fixture. The app, dispatcher, scope checks,
        # source snapshot, journals and outbound HTTP authority client are real.
        app.state.worker_lifecycle.runtime._client = docker_fixture
        async with app.router.lifespan_context(app):
            assert app.state.managed_dispatcher.running.is_set()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://managed-service",
                headers={"Authorization": "Bearer " + WORKER_TOKEN},
                trust_env=False,
            ) as client:
                query = asdict(spec.scope) | {"profile": spec.profile}
                ready = await client.get("/api/workers/capabilities", params=query)
                assert ready.status_code == 200
                assert ready.json()["managed_dispatch"] is True
                assert_no_execution(app, docker_fixture)

                # A previous successful capability observation is not cached
                # admission. The same service remains health-positive.
                authority["mode"] = mode
                async with httpx.AsyncClient(trust_env=False) as health_client:
                    health = await health_client.get(endpoint + "/health")
                    assert health.status_code == 200
                    assert health.json()["status"] == "ok"
                blocked = await client.get("/api/workers/capabilities", params=query)
                assert blocked.status_code == 200
                assert blocked.json()["managed_dispatch"] is False
                assert (
                    blocked.json()["reason"]
                    == "Canonical managed authority is unavailable"
                )
                for _ in range(2):
                    ensured = await client.post(
                        "/api/workers/ensure", json=asdict(spec)
                    )
                    assert ensured.status_code == 503
                    assert ensured.json() == {
                        "detail": "Managed runtime preflight is not ready"
                    }
                listing = await client.get("/api/workers", params=asdict(spec.scope))
                assert listing.json() == {"schema_version": 1, "workers": []}

                # Managed deployment never mounts the standalone legacy app.
                # Review events share its webhook path; no BackgroundTasks
                # acknowledgment or synthetic palette success is available.
                for event in (
                    "issues",
                    "issue_comment",
                    "pull_request_review",
                    "pull_request_review_comment",
                ):
                    rejected = await client.post(
                        "/api/webhook",
                        json={"action": "created"},
                        headers={"X-GitHub-Event": event},
                    )
                    assert rejected.status_code == 404
                for route in (
                    "/api/radicle/webhook",
                    "/api/tasks",
                    "/api/tasks/task/command",
                ):
                    rejected = await client.post(route, json={"command": "proposal"})
                    assert rejected.status_code == 404
                assert_no_execution(app, docker_fixture)

                authority["mode"] = "supported"
                restored = await client.get("/api/workers/capabilities", params=query)
                assert restored.json()["managed_dispatch"] is True
                assert_no_execution(app, docker_fixture)
        assert not app.state.managed_dispatcher.running.is_set()
        assert all(
            method == "GET" and route in {"/health", "/managed/capabilities"}
            for method, route, _ in authority["requests"]
        )
        assert all(
            auth == "Bearer " + TOKEN
            for _, route, auth in authority["requests"]
            if route == "/managed/capabilities"
        )
        # Initial observation, unsupported observation, both ensure retries and
        # restored observation each perform a fresh authenticated preflight.
        assert sum(
            route == "/managed/capabilities"
            for _, route, _ in authority["requests"]
        ) == 5


def test_fresh_managed_app_does_not_import_or_initialize_legacy_execution(
    tmp_path, setup_runtime
):
    _, _, profile, _ = setup_runtime
    path = configuration(tmp_path, profile, "http://127.0.0.1:1")
    script = """import importlib.abc,sys
sys.path.insert(0,sys.argv[1])
forbidden=('src.api.webhook','src.api.ui_routes','src.cli','src.mcp.server',
           'src.worker.executor','src.worker.pr_reviewer','src.worker.radicle_poller',
           'src.agent.engine','litellm')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if any(fullname==name or fullname.startswith(name+'.') for name in forbidden):
            raise AssertionError('managed app imported legacy execution: '+fullname)
sys.meta_path.insert(0,Guard())
import docker
def no_docker(*args,**kwargs):
    raise AssertionError('building the managed app initialized Docker')
docker.DockerClient=docker.from_env=no_docker
from src.polyphony.client import PolyphonyClient
from src.polyphony.manager import PolyphonySwarmManager
from src.harness.adaptive_engine import AdaptiveHarness
from src.sandbox.docker_driver import SandboxManager
def no_legacy(*args,**kwargs):
    raise AssertionError('building the managed app initialized a legacy facade')
for legacy in (PolyphonyClient,PolyphonySwarmManager,AdaptiveHarness,SandboxManager):
    legacy.__init__=no_legacy
from src.worker.managed_service import build_app,load_configuration
app=build_app(*load_configuration(sys.argv[2]))
assert app.state.worker_lifecycle.runtime._client is None
assert app.state.model_broker is None
assert not app.state.managed_dispatcher.running.is_set()
assert not any(name in sys.modules for name in forbidden)
print('managed app built without legacy execution or Docker initialization')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path(__file__).parents[2]), str(path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "managed app built without legacy execution or Docker initialization"
    )
