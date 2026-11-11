"""Real HTTP/journal budget tests with explicitly simulated child authority.

The listener fixture here is not evidence of Unix-socket or Docker isolation.
"""

import threading
import time

import pytest

from src.polyphony.managed_client import AuthorityUnavailable
from src.worker.child_supervisor import ChildDenied
from src.worker.model_broker import BrokerDenied, ModelPolicy
from tests.unit.test_child_authority_contract import policy
from tests.unit.test_managed_agent import (
    ATTEMPT,
    MESSAGES,
    WORKER,
    broker_client,
    message,
    provider,
    response,
)


class ChildAuthorityFixture:
    def __init__(self):
        self.allowed = True
        self.deadline = time.monotonic() + 60
        self.turns = {}
        self.lose_turn_reply = False
        self.on_permission = None
        self.on_turn = None

    def enabled(self, spec):
        return True

    def policy(self, spec):
        return policy()

    def permission(self, worker_id, child_id):
        if self.on_permission:
            self.on_permission()
        return self.local_cutoff(worker_id, child_id)

    def local_cutoff(self, worker_id, child_id):
        if (
            not self.allowed
            or worker_id != WORKER
            or child_id not in {"child-one", "child-two"}
        ):
            raise ChildDenied("not_authorized")
        return self.deadline

    def reserve_turn(self, worker_id, child_id, request_id, digest):
        key = (worker_id, child_id, request_id)
        if key in self.turns and self.turns[key] != digest:
            raise AssertionError("changed canonical turn retry")
        self.turns[key] = digest
        if self.on_turn:
            self.on_turn()
        if self.lose_turn_reply:
            self.lose_turn_reply = False
            raise AuthorityUnavailable("controlled lost turn reply")
        return self.local_cutoff(worker_id, child_id)


def payload(key="same-key", messages=None):
    return {"schema_version": 1, "request_id": key, "messages": messages or MESSAGES}


def rows(broker):
    with broker.connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM calls ORDER BY rowid")]


def test_children_share_root_allowance_and_cannot_collide_with_public_keys(
    tmp_path, monkeypatch
):
    with provider() as (endpoint, requests):
        policy = ModelPolicy(
            endpoint,
            "fixture",
            "env:MOCK_MODEL_TOKEN",
            max_calls=3,
            max_output_tokens=4,
            max_total_output_tokens=12,
        )
        with broker_client(tmp_path, monkeypatch, endpoint, policy=policy) as (
            broker,
            _,
            _,
        ):
            children = broker.child_supervisor = ChildAuthorityFixture()
            root = broker.complete(WORKER, payload())
            first = broker.complete_child(WORKER, "child-one", payload())
            second = broker.complete_child(WORKER, "child-two", payload())
            assert (
                first["request_id"]
                == second["request_id"]
                == root["request_id"]
                == "same-key"
            )
            assert broker.complete_child(WORKER, "child-one", payload()) == first
            records = rows(broker)
            assert len(records) == len(requests) == 3
            assert sum(row["reserved_tokens"] for row in records) == 12
            assert records[0]["request_id"] == "same-key"
            assert records[1]["request_id"].startswith("/child/")
            assert records[1]["request_id"] != records[2]["request_id"]
            assert len(children.turns) == 2
            with pytest.raises(BrokerDenied, match="invalid_request"):
                broker.complete(WORKER, payload(records[1]["request_id"]))
            with pytest.raises(BrokerDenied, match="budget_exhausted"):
                broker.complete(WORKER, payload("fourth"))
            assert len(requests) == 3
            # Only the root receives controls in the provider's tool schema.
            names = [
                {item["function"]["name"] for item in request["body"]["tools"]}
                for request in requests
            ]
            assert (
                "spawn_child" in names[0] and "spawn_child" not in names[1] | names[2]
            )


def test_lost_canonical_turn_reply_retries_only_that_turn(tmp_path, monkeypatch):
    with provider() as (endpoint, requests):
        with broker_client(tmp_path, monkeypatch, endpoint) as (broker, _, _):
            children = broker.child_supervisor = ChildAuthorityFixture()
            children.lose_turn_reply = True
            with pytest.raises(BrokerDenied, match="canonical_pending"):
                broker.complete_child(WORKER, "child-one", payload())
            reserved = rows(broker)
            assert len(reserved) == 1 and reserved[0]["status"] == "child_authorizing"
            assert not requests and len(children.turns) == 1
            with pytest.raises(BrokerDenied, match="retry_later"):
                broker.complete(WORKER, payload("waiting-root"))
            with pytest.raises(BrokerDenied, match="retry_later"):
                broker.complete_child(WORKER, "child-two", payload("waiting-child"))
            assert len(rows(broker)) == 1 and not requests
            with pytest.raises(BrokerDenied, match="conflict"):
                broker.complete_child(
                    WORKER,
                    "child-one",
                    payload(messages=[{"role": "user", "content": "changed"}]),
                )
            assert broker.complete_child(WORKER, "child-one", payload())["ok"] is True
            assert len(requests) == len(children.turns) == len(rows(broker)) == 1
            assert rows(broker)[0]["reserved_tokens"] == reserved[0]["reserved_tokens"]
            assert broker.complete(WORKER, payload("waiting-root"))["ok"] is True
            assert (
                broker.complete_child(WORKER, "child-two", payload("waiting-child"))[
                    "ok"
                ]
                is True
            )
            assert len(requests) == len(rows(broker)) == 3


def test_live_child_provider_call_is_busy_without_reserving_root_call(
    tmp_path, monkeypatch
):
    entered, finish = threading.Event(), threading.Event()

    def delayed(handler, requests):
        if len(requests) == 1:
            entered.set()
            assert finish.wait(5)
        response(handler, message())

    with provider(delayed) as (endpoint, requests):
        with broker_client(tmp_path, monkeypatch, endpoint) as (broker, _, _):
            broker.child_supervisor = ChildAuthorityFixture()
            outcome = []
            worker = threading.Thread(
                target=lambda: outcome.append(
                    broker.complete_child(WORKER, "child-one", payload())
                )
            )
            worker.start()
            try:
                assert entered.wait(3)
                with pytest.raises(BrokerDenied, match="retry_later"):
                    broker.complete(WORKER, payload("root-next"))
                assert len(rows(broker)) == 1 and len(requests) == 1
            finally:
                finish.set()
                worker.join(5)
            assert not worker.is_alive() and outcome[0]["ok"] is True
            assert broker.complete(WORKER, payload("root-next"))["ok"] is True
            assert len(rows(broker)) == len(requests) == 2


def test_uncertain_child_provider_call_blocks_entire_root_allowance(
    tmp_path, monkeypatch
):
    def failed(handler, requests):
        handler.send_response(502)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    with provider(failed) as (endpoint, requests):
        with broker_client(tmp_path, monkeypatch, endpoint) as (broker, _, _):
            broker.child_supervisor = ChildAuthorityFixture()
            with pytest.raises(BrokerDenied):
                broker.complete_child(WORKER, "child-one", payload())
            for child, key in (
                (None, "root"),
                ("child-two", "other"),
                ("child-one", "same-key"),
            ):
                with pytest.raises(BrokerDenied, match="uncertain"):
                    if child is None:
                        broker.complete(WORKER, payload(key))
                    else:
                        broker.complete_child(WORKER, child, payload(key))
            assert len(requests) == 1 and rows(broker)[0]["status"] == "uncertain"


def test_local_revocation_after_child_authority_reply_never_forwards(
    tmp_path, monkeypatch
):
    with provider() as (endpoint, requests):
        with broker_client(tmp_path, monkeypatch, endpoint) as (broker, _, _):
            children = broker.child_supervisor = ChildAuthorityFixture()
            children.on_permission = lambda: broker.revoke_attempt(WORKER, ATTEMPT)
            with pytest.raises(BrokerDenied, match="not_authorized"):
                broker.complete_child(WORKER, "child-one", payload())
            assert not requests and not rows(broker)


def test_child_revocation_during_turn_ack_preserves_charge_without_provider(
    tmp_path, monkeypatch
):
    with provider() as (endpoint, requests):
        with broker_client(tmp_path, monkeypatch, endpoint) as (broker, _, _):
            children = broker.child_supervisor = ChildAuthorityFixture()
            children.on_turn = lambda: setattr(children, "allowed", False)
            with pytest.raises(BrokerDenied, match="not_authorized"):
                broker.complete_child(WORKER, "child-one", payload())
            assert not requests and len(rows(broker)) == 1
            assert (
                rows(broker)[0]["status"] == "complete"
                and rows(broker)[0]["reserved_tokens"] > 0
            )
