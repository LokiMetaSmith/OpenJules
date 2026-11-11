"""Child delivery/recovery barriers with explicit authority and runtime fixtures.

These exercise the production journal and supervisor. They do not qualify
Docker termination, UID separation, or the external authority implementation.
"""

from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from src.polyphony.child_client import ChildRejected
from src.polyphony.managed_client import AuthorityUnavailable
from src.worker.child_supervisor import ChildDenied, ChildSupervisor
from src.worker.child_types import canonical, text_hash
from src.worker.lifecycle import LifecycleConflict, RuntimeUnavailable
from tests.unit.test_child_authority_contract import context, grant, policy, request


class FixtureAuthority:
    def __init__(self, root):
        self.root = root
        self.events = []
        self.grants = {}
        self.closed = False
        self.lose_grant = False
        self.lose_capture = False
        self.bad_terminal = False
        self.permission_callback = None
        self.artifacts = {}

    def call(self, operation, scope, attempt_id, **payload):
        assert scope == self.root.spec.scope and attempt_id == self.root.attempt_id
        self.events.append((operation, payload))
        if operation == "close":
            self.closed = True
            return {"action": "close"}
        if operation == "grant":
            key = payload["request_key"]
            if key not in self.grants:
                if self.closed:
                    raise ChildRejected(409, child_not_allocated=True)
                fields = {name: payload[name] for name in request()}
                child_id = "child_" + f"{len(self.grants) + 1:032x}"
                self.grants[key] = grant(self.root, fields, child_id)
            response = self.grants[key]
            if self.lose_grant:
                self.lose_grant = False
                raise AuthorityUnavailable("fixture lost grant reply after allocation")
            return response
        child_id = payload["child_id"]
        response = next(
            value
            for value in self.grants.values()
            if value["binding"]["child_id"] == child_id
        )
        if operation in {"permission", "turn"}:
            if self.permission_callback:
                self.permission_callback()
            if operation == "turn":
                return response | {
                    "turn": {
                        "child_id": child_id,
                        "root_attempt_id": attempt_id,
                        "request_digest": payload["request_digest"],
                    }
                }
            return response
        if operation == "capture":
            self.artifacts[(child_id, payload["kind"])] = payload["content"]
            if self.lose_capture:
                self.lose_capture = False
                raise AuthorityUnavailable(
                    "fixture lost capture reply after persistence"
                )
            return {
                "child_id": child_id,
                "kind": payload["kind"],
                "artifact_id": text_hash(payload["content"]),
                "runtime_principal": self.root.runtime_principal,
            }
        if operation == "observe":
            hashes = {
                kind: text_hash(content)
                for (identity, kind), content in self.artifacts.items()
                if identity == child_id
            }
            assert hashes == payload["artifacts"] and set(hashes) == {
                "patch",
                "log",
                "manifest",
            }
            return {
                "child": response["child"]
                | {
                    "state": payload["state"],
                    "termination_confirmed": not self.bad_terminal,
                    "launch_fenced": True,
                    "captured_artifacts": hashes,
                }
            }
        if operation == "revoke":
            return {"action": "revoke"}
        raise AssertionError(operation)

    def children(self, scope, attempt_id):
        assert scope == self.root.spec.scope and attempt_id == self.root.attempt_id
        self.events.append(("children", {}))
        return {"children": list(self.grants.values())}


class FixtureLifecycle:
    def __init__(self, root):
        self.root = root
        self.open = True
        self.active = [root.attempt_id]
        self.ended = []

    def root_admission_open(self, scope, worker_id, attempt_id):
        assert scope == self.root.spec.scope and worker_id == self.root.worker_id
        assert attempt_id == self.root.attempt_id
        return self.open

    def observe_worker(self, scope, worker_id):
        return {"active_executions": list(self.active)}

    def begin_execution(self, scope, worker_id, child_id, parent_id):
        assert parent_id == self.root.attempt_id and self.open
        if child_id not in self.active:
            self.active.append(child_id)

    def record_execution_end(self, scope, worker_id, child_id, **facts):
        assert facts == {"termination_confirmed": True, "artifacts_captured": True}
        self.active.remove(child_id)
        self.ended.append(child_id)


class FixtureDriver:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.launches = []
        self.captures = []
        self.fail_capture = False

    def launch(self, root, child, cutoff):
        assert cutoff <= min(root.monotonic_deadline, child["deadline"])
        assert self.supervisor.local_allowed(root, child, admission=True)
        self.launches.append((child["child_id"], cutoff))
        self.supervisor.journal.update(
            root.worker_id,
            child["child_id"],
            phase="running",
            exec_id="exec-" + child["child_id"],
            exec_dispatched=1,
        )

    def observe(self, root, child):
        return child

    def provisional(self, root, child, kind):
        return "provisional é\n"

    def final_capture(self, root, child, receipt):
        self.captures.append(child["child_id"])
        if self.fail_capture:
            raise RuntimeUnavailable("fixture capture not verified")
        assert (
            receipt.termination_confirmed
            and receipt.launch_fenced
            and receipt.artifacts_captured
        )
        return {
            "patch": "fixture diff\n",
            "log": "fixture log\n",
            "manifest": canonical({"child_id": child["child_id"]}),
        }


@pytest.fixture
def supervised(tmp_path):
    root = replace(
        context(),
        monotonic_deadline=time.monotonic() + 60,
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    )
    lifecycle = FixtureLifecycle(root)
    active = [True]
    supervisor = ChildSupervisor(
        tmp_path / "children",
        {(root.spec.deployment_id, root.spec.repository, root.spec.profile): policy()},
        None,
        lifecycle,
        SimpleNamespace(),
        None,
        lambda _: 50,
        lambda _: active[0],
    )
    supervisor.authority = FixtureAuthority(root)
    supervisor.driver = FixtureDriver(supervisor)
    supervisor.endpoints = SimpleNamespace(
        prepare_child=lambda *_: None,
        revoke=lambda *_: None,
        close=lambda callback: callback(),
    )
    supervisor.journal.prepare(root.worker_id, root.spec, policy())
    supervisor.journal.bind(root)
    yield supervisor, root, lifecycle, active
    supervisor.close()


def spawn(supervisor, root, request_id="spawn-one", **overrides):
    return supervisor.handle(
        root.worker_id,
        {
            "schema_version": 1,
            "request_id": request_id,
            "operation": "spawn",
            **request(),
            **overrides,
        },
    )


def receipt():
    return SimpleNamespace(
        termination_confirmed=True, launch_fenced=True, artifacts_captured=True
    )


def test_lost_grant_is_recovered_after_close_without_new_launch(supervised):
    supervisor, root, lifecycle, _ = supervised
    supervisor.authority.lose_grant = True
    with pytest.raises(AuthorityUnavailable):
        spawn(supervisor, root)
    first = supervisor.journal.intents(root.worker_id)[0]
    assert first["child_id"] is None and not supervisor.driver.launches
    supervisor.close_admission(root.worker_id, "drain")
    recovered = spawn(supervisor, root)
    assert (
        recovered["child_id"]
        == next(iter(supervisor.authority.grants.values()))["binding"]["child_id"]
    )
    assert recovered["launch_allowed"] is False and not supervisor.driver.launches
    operations = [operation for operation, _ in supervisor.authority.events]
    assert operations == ["grant", "close", "grant"]
    assert supervisor.authority.events[0][1] == supervisor.authority.events[-1][1]
    with pytest.raises(ChildDenied, match="not_authorized"):
        spawn(supervisor, root, "new-key")
    with pytest.raises(LifecycleConflict):
        spawn(supervisor, root, instructions="changed retry")
    supervisor.finalize(root.worker_id, receipt())
    assert supervisor.journal.children(root.worker_id)[0]["phase"] == "completed"
    assert lifecycle.active == [root.attempt_id]


def test_pending_undelivered_grant_is_explicitly_rejected_before_root_release(
    supervised,
):
    supervisor, root, _, _ = supervised
    supervisor.journal.reserve(root.worker_id, "pending", request(), policy())
    supervisor.finalize(root.worker_id, receipt())
    assert [operation for operation, _ in supervisor.authority.events] == [
        "close",
        "grant",
        "children",
    ]
    assert supervisor.journal.intents(root.worker_id)[0]["status"] == "rejected"
    assert not supervisor.journal.children(root.worker_id)


@pytest.mark.parametrize("change", ["shorten", "revoke", "phase"])
def test_root_callback_cannot_extend_or_resurrect_local_permission(supervised, change):
    supervisor, root, _, active = supervised
    target = time.monotonic() + 1

    def callback(_):
        if change == "shorten":
            supervisor.journal.bind(replace(root, monotonic_deadline=target))
        elif change == "revoke":
            supervisor.mark_retiring(root.worker_id)
        else:
            active[0] = False
        return 50

    supervisor.root_permission = callback
    if change == "shorten":
        assert supervisor._root_permission(root) <= target
    else:
        with pytest.raises(ChildDenied, match="not_authorized"):
            supervisor._root_permission(root)


def test_child_callback_rechecks_fresh_root_cutoff_and_revocation(supervised):
    supervisor, root, _, _ = supervised
    child_id = spawn(supervisor, root)["child_id"]
    target = time.monotonic() + 1
    supervisor.authority.permission_callback = lambda: supervisor.journal.bind(
        replace(root, monotonic_deadline=target)
    )
    assert supervisor.permission(root.worker_id, child_id) <= target
    supervisor.authority.permission_callback = lambda: supervisor.journal.update(
        root.worker_id, child_id, revoked=1
    )
    with pytest.raises(ChildDenied, match="not_authorized"):
        supervisor.permission(root.worker_id, child_id)


def test_terminal_recovery_preserves_deadline_and_retries_capture_without_exec(
    supervised,
):
    supervisor, root, lifecycle, _ = supervised
    child_id = spawn(supervisor, root)["child_id"]
    original = supervisor.journal.child(root.worker_id, child_id)["deadline"]
    for key, value in list(supervisor.authority.grants.items()):
        supervisor.authority.grants[key] = value | {
            "launch_allowed": False,
            "remaining_seconds": 0,
            "reason": "revoked",
        }
    supervisor.authority.lose_capture = True
    with pytest.raises(AuthorityUnavailable):
        supervisor.finalize(root.worker_id, receipt())
    assert child_id in lifecycle.active and not lifecycle.ended
    assert supervisor.journal.child(root.worker_id, child_id)["deadline"] == original
    supervisor.finalize(root.worker_id, receipt())
    assert len(supervisor.driver.launches) == len(supervisor.driver.captures) == 1
    assert lifecycle.ended == [child_id]
    child = supervisor.journal.child(root.worker_id, child_id)
    assert (
        child["phase"] == "completed"
        and json.loads(child["capture"])["state"] == "exited"
    )


@pytest.mark.parametrize("failure", ["capture", "terminal", "orphan"])
def test_child_failure_holds_root_barrier(supervised, failure):
    supervisor, root, lifecycle, _ = supervised
    child_id = spawn(supervisor, root)["child_id"]
    if failure == "capture":
        supervisor.driver.fail_capture = True
    elif failure == "terminal":
        supervisor.authority.bad_terminal = True
    else:
        supervisor.authority.grants["foreign-intent"] = grant(
            root, request(), "child_" + "f" * 32
        )
    with pytest.raises((RuntimeUnavailable, AuthorityUnavailable)):
        supervisor.finalize(root.worker_id, receipt())
    assert child_id in lifecycle.active and not lifecycle.ended
    assert supervisor.journal.child(root.worker_id, child_id)["phase"] != "completed"


def test_canonical_listing_cannot_change_or_duplicate_recovered_binding(supervised):
    supervisor, root, lifecycle, _ = supervised
    child_id = spawn(supervisor, root)["child_id"]
    original = next(iter(supervisor.authority.grants.values()))
    supervisor.authority.children = lambda *_: {"children": [original, original]}
    with pytest.raises(RuntimeUnavailable, match="lineage"):
        supervisor.finalize(root.worker_id, receipt())
    changed = grant(
        root, request() | {"instructions": "changed canonical listing"}, child_id
    )
    supervisor.authority.children = lambda *_: {"children": [changed]}
    with pytest.raises(AuthorityUnavailable):
        supervisor.finalize(root.worker_id, receipt())
    assert child_id in lifecycle.active and not supervisor.driver.captures


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": []},
        {"operation": {}},
        {"operation": "status", "child_id": []},
        {
            "operation": "result",
            "child_id": "child_" + "0" * 32,
            "kind": [],
            "offset": 0,
            "limit": 1,
        },
    ],
)
def test_unhashable_control_fields_are_bounded_refusals(supervised, payload):
    supervisor, root, _, _ = supervised
    if payload.get("operation") == "result":
        child_id = spawn(supervisor, root)["child_id"]
        payload = payload | {"child_id": child_id}
    with pytest.raises(ChildDenied, match="invalid_request"):
        supervisor.handle(
            root.worker_id, {"schema_version": 1, "request_id": "bad", **payload}
        )


def test_dispatcher_does_not_publish_root_before_child_barrier(tmp_path):
    from tests.unit.test_managed_dispatch import SPEC, setup

    runtime, authority, lifecycle, dispatcher, worker = setup(tmp_path)
    events = []
    allow = [False]

    def finalize(worker_id, capture):
        assert capture.termination_confirmed
        events.append("children")
        if not allow[0]:
            raise RuntimeUnavailable("fixture child capture incomplete")

    children = SimpleNamespace(
        enabled=lambda _: True,
        retiring=lambda _: False,
        bind_root=lambda *_: None,
        reconcile=lambda *_: None,
        finalize=finalize,
    )
    dispatcher.child_supervisor = children
    original_call = authority.call

    def call(operation, *args, **kwargs):
        if operation in {"capture", "observe", "candidate"}:
            events.append(operation)
        return original_call(operation, *args, **kwargs)

    authority.call = call
    with pytest.raises(RuntimeUnavailable):
        dispatcher.tick_worker(worker)
    assert events == ["children"] and not authority.artifacts
    assert lifecycle.observe_worker(SPEC.scope, worker["worker_id"])[
        "active_executions"
    ]
    allow[0] = True
    dispatcher.tick_worker(worker)
    assert events == [
        "children",
        "children",
        "capture",
        "capture",
        "capture",
        "observe",
        "candidate",
    ]
    assert dispatcher.journal.get(worker["worker_id"])["phase"] == "completed"
