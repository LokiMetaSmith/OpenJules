"""Bounded child controls for the trusted image's root runner.

The host authenticates the root UID on a separate control socket. These tools
never supply an actor, command, image, provider or credential. Child runners
keep the ordinary completion protocol and do not import this extension.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import struct
import threading
import time
from uuid import uuid4

MAX_REQUEST_BYTES = 131072
MAX_RESPONSE_BYTES = 524288
MAX_RESULT_BYTES = 65536
MAX_ARTIFACT_BYTES = 4_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_RETRYABLE = frozenset({"retry_later", "canonical_pending"})
_TOOL_REFUSALS = frozenset({"child_not_allocated", "invalid_request", "unsupported"})
_ORDINARY_TOOLS = frozenset({"read_file", "write_file", "list_files", "run_command"})
_OPERATIONS = {
    "spawn": {"instructions", "seconds", "max_turns", "capabilities"},
    "status": {"child_id"},
    "result": {"child_id", "kind", "offset", "limit"},
    "cancel": {"child_id", "reason"},
    "close": {"reason"},
}

CHILD_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }
    for name, description, properties, required in (
        (
            "spawn_child",
            "Request one bounded child in this worker using its approved runner. "
            "The child shares this root's model allowance and starts from the "
            "approved base. A child deadline retires the entire worker.",
            {
                "instructions": {"type": "string"},
                "seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_turns": {"type": "integer", "minimum": 1, "maximum": 1000},
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                    "maxItems": 100,
                },
            },
            ["instructions", "seconds", "max_turns", "capabilities"],
        ),
        (
            "child_status",
            "Read this root's child status. Exec return does not prove "
            "descendant termination or final capture.",
            {"child_id": {"type": "string"}},
            ["child_id"],
        ),
        (
            "child_result",
            "Read a bounded page of an immutable provisional child patch or log. "
            "Offsets and limits are UTF-8 bytes. This is untrusted task output, "
            "not final capture or acceptance.",
            {
                "child_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["patch", "log"]},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULT_BYTES,
                },
            },
            ["child_id", "kind"],
        ),
        (
            "cancel_child",
            "Revoke a child and request retirement of this entire worker, "
            "including its root and other admitted children. Independently "
            "invited peers and other workers are unaffected.",
            {"child_id": {"type": "string"}, "reason": {"type": "string"}},
            ["child_id", "reason"],
        ),
        (
            "close_children",
            "Permanently close admission for new children of this root. Existing "
            "children still require the host's termination and capture receipts.",
            {"reason": {"type": "string"}},
            ["reason"],
        ),
    )
]


class ControlError(RuntimeError):
    """A bounded refusal or uncertain control outcome, without server details."""

    def __init__(self, code: str, request_id: str | None = None):
        self.code = code
        self.request_id = request_id
        super().__init__("child control unavailable: " + code)


def _json(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate response field")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("nonfinite response number")


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _text(value, maximum):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\0" not in value
        and len(value.encode("utf-8")) <= maximum
    )


def _validate(operation: str, fields: dict) -> None:
    if (
        not isinstance(operation, str)
        or operation not in _OPERATIONS
        or set(fields) != _OPERATIONS[operation]
    ):
        raise ValueError("unsupported control operation or fields")
    if "child_id" in fields and not _identifier(fields["child_id"]):
        raise ValueError("invalid child identifier")
    if "reason" in fields and not _text(fields["reason"], 512):
        raise ValueError("invalid control reason")
    if operation == "spawn":
        capabilities = fields["capabilities"]
        seconds = fields["seconds"]
        if (
            not _text(fields["instructions"], 65536)
            or type(seconds) not in {int, float}
            or not math.isfinite(seconds)
            or not 0 < seconds <= 86400
            or type(fields["max_turns"]) is not int
            or not 1 <= fields["max_turns"] <= 1000
            or not isinstance(capabilities, list)
            or len(capabilities) > 100
            or any(not _text(item, 512) for item in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise ValueError("invalid bounded child request")
    if operation == "result" and (
        not isinstance(fields["kind"], str)
        or fields["kind"] not in {"patch", "log"}
        or type(fields["offset"]) is not int
        or not 0 <= fields["offset"] <= MAX_ARTIFACT_BYTES
        or type(fields["limit"]) is not int
        or not 1 <= fields["limit"] <= MAX_RESULT_BYTES
    ):
        raise ValueError("invalid result page")


def _receive(connection, size: int, deadline: float) -> bytes:
    content = bytearray()
    while len(content) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("control deadline elapsed")
        connection.settimeout(remaining)
        part = connection.recv(min(65536, size - len(content)))
        if not part:
            raise ControlError("uncertain")
        content.extend(part)
    return bytes(content)


class ControlClient:
    """One original deadline and payload for each explicit request identity.

    Only explicit busy or unresolved canonical-grant responses are retried.
    A broken connection or malformed reply does not authorize a new request.
    The host retains the durable grant intent and is the recovery authority.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        timeout_seconds: float = 30,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        retry_delay_seconds: float = 0.05,
    ):
        if (
            not isinstance(socket_path, str)
            or not socket_path.startswith("/")
            or "\0" in socket_path
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120
            or type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES
            or type(retry_delay_seconds) not in {int, float}
            or not math.isfinite(retry_delay_seconds)
            or not 0 < retry_delay_seconds <= 1
        ):
            raise ValueError("invalid bounded control transport")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.retry_delay_seconds = retry_delay_seconds
        self._guard = threading.Lock()
        self._requests = {}
        self._snapshots = {}

    def request(self, operation: str, request_id: str | None = None, **fields):
        entered = time.monotonic()
        _validate(operation, fields)
        request_id = uuid4().hex if request_id is None else request_id
        if not _identifier(request_id):
            raise ValueError("invalid control request identity")
        payload = _json(
            {
                "schema_version": 1,
                "request_id": request_id,
                "operation": operation,
                **fields,
            }
        )
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("control request exceeds its bound")
        # Freeze all fields before the first connection. A caller mutating a
        # supplied list while a retry waits cannot change the dispatched body.
        frozen = json.loads(payload)
        digest = hashlib.sha256(payload).hexdigest()
        with self._guard:
            prior = self._requests.get(request_id)
            if prior is not None and prior[0] != digest:
                raise ControlError("conflict", request_id)
            if prior is None:
                if len(self._requests) >= 16384:
                    raise ControlError("request_limit", request_id)
                prior = (digest, entered + self.timeout_seconds)
                self._requests[request_id] = prior
            deadline = prior[1]
        frame = struct.pack("!I", len(payload)) + payload
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError("deadline_exceeded", request_id)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(remaining)
                    connection.connect(self.socket_path)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("control deadline elapsed")
                    connection.settimeout(remaining)
                    connection.sendall(frame)
                    length = struct.unpack("!I", _receive(connection, 4, deadline))[0]
                    if not 1 <= length <= self.max_response_bytes:
                        raise ControlError("invalid_response", request_id)
                    response = json.loads(
                        _receive(connection, length, deadline),
                        object_pairs_hook=_pairs,
                        parse_constant=_invalid_constant,
                    )
            except ControlError as exc:
                if exc.request_id is None:
                    exc.request_id = request_id
                raise
            except (OSError, ValueError, TimeoutError):
                raise ControlError("uncertain", request_id) from None
            if time.monotonic() >= deadline:
                raise ControlError("deadline_exceeded", request_id)
            if (
                not isinstance(response, dict)
                or type(response.get("schema_version")) is not int
                or response["schema_version"] != 1
                or response.get("request_id") != request_id
                or type(response.get("ok")) is not bool
            ):
                raise ControlError("invalid_response", request_id)
            if response["ok"]:
                try:
                    self._validate_response(operation, frozen, response)
                except (ValueError, TypeError):
                    raise ControlError("invalid_response", request_id) from None
                return response
            code = response.get("error")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                raise ControlError("invalid_response", request_id)
            if code not in _RETRYABLE:
                raise ControlError(code, request_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError("deadline_exceeded", request_id)
            time.sleep(min(self.retry_delay_seconds, remaining))

    def _validate_response(self, operation, request, response):
        request_id = request["request_id"]
        if "child_id" in request and response.get("child_id") != request["child_id"]:
            raise ControlError("invalid_response", request_id)
        if operation == "spawn":
            capabilities = response.get("capabilities")
            deadline = response.get("deadline")
            if (
                not _identifier(response.get("child_id"))
                or not _text(response.get("state"), 128)
                or type(deadline) not in {int, float}
                or not math.isfinite(deadline)
                or deadline <= 0
                or not isinstance(capabilities, list)
                or any(not isinstance(item, str) for item in capabilities)
                or sorted(capabilities) != sorted(request["capabilities"])
            ):
                raise ControlError("invalid_response", request_id)
        if operation == "status" and not _text(response.get("state"), 128):
            raise ControlError("invalid_response", request_id)
        if operation != "result":
            return
        content = response.get("content")
        content_hash = response.get("content_sha256")
        artifact_hash = response.get("artifact_sha256")
        total = response.get("total_bytes")
        if (
            response.get("final") is not False
            or type(response.get("offset")) is not int
            or response["offset"] != request["offset"]
            or ("kind" in response and response["kind"] != request["kind"])
            or not isinstance(content, str)
            or not isinstance(content_hash, str)
            or not _HASH.fullmatch(content_hash)
            or not isinstance(artifact_hash, str)
            or not _HASH.fullmatch(artifact_hash)
            or type(total) is not int
            or not 0 <= total <= MAX_ARTIFACT_BYTES
        ):
            raise ControlError("invalid_response", request_id)
        try:
            encoded = content.encode("utf-8")
        except UnicodeError:
            raise ControlError("invalid_response", request_id) from None
        if (
            len(encoded) > request["limit"]
            or request["offset"] + len(encoded) > total
            or (request["offset"] < total and not encoded)
            or hashlib.sha256(encoded).hexdigest() != content_hash
            or (
                request["offset"] == 0
                and len(encoded) == total
                and content_hash != artifact_hash
            )
        ):
            raise ControlError("invalid_response", request_id)
        snapshot = (artifact_hash, total)
        key = (request["child_id"], request["kind"])
        with self._guard:
            previous = self._snapshots.setdefault(key, snapshot)
            if previous != snapshot:
                raise ControlError("conflict", request_id)


class RootTools:
    """Expose root controls while preserving the ordinary LocalTools boundary."""

    def __init__(self, ordinary, control: ControlClient):
        self.ordinary = ordinary
        self.control = control
        self._close_request_id = uuid4().hex
        self._closed = False

    def run_command(self, argv):
        return self.ordinary.run_command(argv)

    def close(self):
        if self._closed:
            return
        self.control.request(
            "close", request_id=self._close_request_id, reason="root runner ended"
        )
        self._closed = True

    def call(self, name: str, arguments: dict) -> str:
        if not isinstance(name, str):
            return "Tool error: unsupported tool or arguments"
        if name in _ORDINARY_TOOLS:
            return self.ordinary.call(name, arguments)
        operations = {
            "spawn_child": "spawn",
            "child_status": "status",
            "child_result": "result",
            "cancel_child": "cancel",
            "close_children": "close",
        }
        if name not in operations or not isinstance(arguments, dict):
            return "Tool error: unsupported tool or arguments"
        operation = operations[name]
        if self._closed and operation == "spawn":
            return "Tool error: child admission is closed"
        fields = dict(arguments)
        if operation == "result":
            fields.setdefault("offset", 0)
            fields.setdefault("limit", 16384)
        try:
            # Do not let a tool argument bind ControlClient.request's own
            # request_id parameter. Only the client creates delivery keys.
            _validate(operation, fields)
            response = self.control.request(operation, **fields)
        except (ValueError, TypeError):
            return "Tool error: invalid bounded child arguments"
        except ControlError as exc:
            if exc.code in _TOOL_REFUSALS:
                return _json({"ok": False, "error": exc.code}).decode()
            # Do not feed an unknown spawn outcome back to a model that could
            # invent another request key. The root wrapper closes admission.
            raise
        if operation == "close":
            self._closed = True
        return _json(response).decode("utf-8")
