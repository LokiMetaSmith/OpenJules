"""One explicit semantic report through the child-bound host socket.

This stdlib module runs in the approved image. A receipt only proves local
retention. It does not prove process termination, verification or acceptance.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import struct
import time
from uuid import uuid4

OUTCOMES = frozenset({"completed", "declined", "needs_clarification", "failed"})
MAX_SUMMARY_BYTES = 8192
MAX_FRAME_BYTES = 65536
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
OUTCOME_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "report_outcome",
            "description": "End this child instruction with an explicit outcome and "
            "private summary. Use as the only tool call. Completion is a self-report, "
            "not verification, task acceptance or publication permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {"type": "string", "enum": sorted(OUTCOMES)},
                    "summary": {"type": "string"},
                },
                "required": ["outcome", "summary"],
                "additionalProperties": False,
            },
        },
    }
]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def report_value(request_key, outcome, summary):
    if (
        not isinstance(request_key, str)
        or not _IDENTIFIER.fullmatch(request_key)
        or not isinstance(outcome, str)
        or outcome not in OUTCOMES
        or not isinstance(summary, str)
        or "\0" in summary
        or len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES
    ):
        raise ValueError("invalid bounded child outcome")
    value = {"request_key": request_key, "outcome": outcome, "summary": summary}
    return value | {
        "payload_digest": hashlib.sha256(canonical(value).encode()).hexdigest()
    }


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate outcome response field")
        result[key] = value
    return result


class OutcomeError(RuntimeError):
    pass


class OutcomeClient:
    def __init__(self, socket_path, *, timeout_seconds=5):
        if (
            not isinstance(socket_path, str)
            or not socket_path.startswith("/")
            or "\0" in socket_path
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError("invalid bounded outcome transport")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self.request_key = uuid4().hex
        self.reported = None
        self.deadline = None
        self.receipt = None

    @staticmethod
    def _read(connection, size, deadline):
        content = bytearray()
        while len(content) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("outcome delivery deadline elapsed")
            connection.settimeout(remaining)
            part = connection.recv(min(65536, size - len(content)))
            if not part:
                raise OSError("outcome response was lost")
            content.extend(part)
        return bytes(content)

    def report(self, outcome, summary):
        report = report_value(self.request_key, outcome, summary)
        if self.reported is not None and report != self.reported:
            raise OutcomeError("child outcome cannot change")
        if self.reported is None:
            self.reported = report
            self.deadline = time.monotonic() + self.timeout_seconds
        if self.receipt is not None:
            return self.receipt
        encoded = canonical(
            {
                "schema_version": 1,
                "request_id": self.request_key,
                "outcome": outcome,
                "summary": summary,
            }
        ).encode()
        frame = struct.pack("!I", len(encoded)) + encoded
        while time.monotonic() < self.deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("outcome delivery deadline elapsed")
                    connection.settimeout(remaining)
                    connection.connect(self.socket_path)
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("outcome delivery deadline elapsed")
                    connection.settimeout(remaining)
                    connection.sendall(frame)
                    length = struct.unpack(
                        "!I", self._read(connection, 4, self.deadline)
                    )[0]
                    if not 1 <= length <= MAX_FRAME_BYTES:
                        raise OutcomeError("invalid outcome receipt")
                    response = json.loads(
                        self._read(connection, length, self.deadline),
                        object_pairs_hook=_pairs,
                    )
            except (OSError, TimeoutError):
                # This is idempotent evidence delivery, never process or model
                # execution. Retrying the identical slot/key/body is safe.
                time.sleep(min(0.05, max(0, self.deadline - time.monotonic())))
                continue
            except ValueError:
                raise OutcomeError("invalid outcome receipt") from None
            if (
                not isinstance(response, dict)
                or type(response.get("schema_version")) is not int
                or response["schema_version"] != 1
                or response.get("request_id") != self.request_key
                or response.get("ok") is not True
                or response.get("report") != self.reported
                or not isinstance(response.get("child_id"), str)
                or not re.fullmatch(r"child_[0-9a-f]{32}", response["child_id"])
                or not isinstance(response.get("work_turn_id"), str)
                or not _HASH.fullmatch(response["work_turn_id"])
                or response.get("final") is not False
                or response.get("canonical_committed") is not False
                or response.get("release_authorized") is not False
            ):
                raise OutcomeError(
                    "child outcome was not confirmed as locally retained"
                )
            self.receipt = response
            return response
        raise OutcomeError("child outcome delivery remains uncertain")
