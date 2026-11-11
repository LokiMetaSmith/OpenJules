"""Credential-free, index-only composition of final canonical child artifacts.

This module executes fixed Git plumbing, never repository commands. A matching
tree establishes mechanical composition, not tests, semantic review or release.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time

from src.worker.source_snapshot import SnapshotUnavailable, SourceSnapshots

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
CHILD = re.compile(r"child_[0-9a-f]{32}")


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def object_hash(kind: str, content: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(content)}\0".encode() + content).hexdigest()


class CompositionError(RuntimeError):
    def __init__(self, code: str, *, child_id=None, paths=()):
        self.code, self.child_id, self.paths = code, child_id, tuple(paths)
        super().__init__(code)


@dataclass(frozen=True)
class CompositionLimits:
    artifact_bytes: int = 4_000_000
    total_artifact_bytes: int = 32_000_000
    source_bytes: int = 16_000_000
    blob_bytes: int = 4_000_000
    max_files: int = 10_000
    max_children: int = 64
    max_path_bytes: int = 1024
    max_patch_records: int = 20_000
    max_objects: int = 100_000
    object_bytes: int = 64_000_000
    git_output_bytes: int = 16_000_000
    seconds: float = 60

    def __post_init__(self):
        ceilings = {
            "artifact_bytes": 4_000_000,
            "total_artifact_bytes": 64_000_000,
            "source_bytes": 32_000_000,
            "blob_bytes": 4_000_000,
            "max_files": 20_000,
            "max_children": 64,
            "max_path_bytes": 4096,
            "max_patch_records": 40_000,
            "max_objects": 200_000,
            "object_bytes": 128_000_000,
            "git_output_bytes": 32_000_000,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid composition limit")
        if (
            type(self.seconds) not in {int, float}
            or not math.isfinite(self.seconds)
            or not 0 < self.seconds <= 120
        ):
            raise ValueError("invalid composition deadline")


def safe_path(path: str, limits: CompositionLimits) -> str:
    if (
        not isinstance(path, str)
        or not path
        or len(path.encode()) > limits.max_path_bytes
        or path.startswith("/")
        or "\\" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(
            part in {"", ".", ".."} or part.casefold() == ".git"
            for part in path.split("/")
        )
    ):
        raise CompositionError("unsupported_path")
    return path


class Git:
    """One bounded subprocess budget covering all Git work in this operation."""

    def __init__(self, directory: Path, deadline: float, limits: CompositionLimits):
        self.directory, self.deadline, self.limits = directory, deadline, limits

    def run(self, *arguments: str, data=b"", failure=None, limit=None) -> bytes:
        if len(arguments) > 32 or any(
            not isinstance(a, str) or "\0" in a or len(a) > 4096 for a in arguments
        ):
            raise CompositionError("git_arguments_invalid")
        if time.monotonic() >= self.deadline:
            raise CompositionError("composition_deadline")
        environment = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        command = [
            "git",
            "--no-pager",
            "--git-dir=" + str(self.directory),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "pack.threads=1",
            *arguments,
        ]
        try:
            process = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            raise CompositionError("git_tool_failure") from None
        selector, output, errors, position = (
            selectors.DefaultSelector(),
            bytearray(),
            0,
            0,
        )
        maximum = min(
            limit or self.limits.git_output_bytes, self.limits.git_output_bytes
        )
        try:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if data:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise CompositionError("composition_deadline")
                for key, _ in selector.select(min(remaining, 0.05)):
                    if key.data == "stdin":
                        try:
                            position += os.write(
                                key.fileobj.fileno(), data[position : position + 65536]
                            )
                        except BrokenPipeError:
                            position = len(data)
                        if position == len(data):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    elif key.data == "stdout":
                        if len(output) + len(chunk) > maximum:
                            raise CompositionError("git_output_limit")
                        output.extend(chunk)
                    else:
                        errors += len(chunk)
                        if errors > 65536:
                            raise CompositionError("git_output_limit")
            code = process.wait(timeout=max(0.001, self.deadline - time.monotonic()))
            if code:
                expected = (
                    failure
                    if (code == 1 or (code == 128 and failure == "malformed_patch"))
                    else None
                )
                raise CompositionError(expected or "git_tool_failure")
            return bytes(output)
        except (OSError, subprocess.SubprocessError):
            raise CompositionError("git_tool_failure") from None
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()


def _entries(git: Git, tree: str) -> list[dict]:
    if not SHA.fullmatch(tree):
        raise CompositionError("invalid_git_identity")
    raw = git.run("ls-tree", "-r", "-z", "--full-tree", tree)
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode, kind, sha = header.decode("ascii").split(" ")
            path = safe_path(path.decode("utf-8"), git.limits)
        except (ValueError, UnicodeError):
            raise CompositionError("unsupported_tree") from None
        if mode not in {"100644", "100755"} or kind != "blob" or not SHA.fullmatch(sha):
            raise CompositionError("unsupported_mode", paths=(path,))
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
        if len(entries) > git.limits.max_files:
            raise CompositionError("file_limit")
    identities = sorted({entry["sha"] for entry in entries})
    if identities:
        sizes = (
            git.run(
                "cat-file",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                data=("\n".join(identities) + "\n").encode(),
            )
            .decode("ascii")
            .splitlines()
        )
        total = 0
        if len(sizes) != len(identities):
            raise CompositionError("git_tool_failure")
        for expected, line in zip(identities, sizes):
            fields = line.split(" ")
            if (
                len(fields) != 3
                or fields[:2] != [expected, "blob"]
                or not fields[2].isdigit()
            ):
                raise CompositionError("git_tool_failure")
            size = int(fields[2])
            if size > git.limits.blob_bytes:
                raise CompositionError("blob_limit")
            total += size
        if total > git.limits.source_bytes + git.limits.total_artifact_bytes:
            raise CompositionError("tree_byte_limit")
    return entries


def _patch(git: Git, patch: str, child_id=None, *, apply=True):
    if not patch:
        return []
    raw = patch.encode()
    if len(raw) > git.limits.artifact_bytes:
        raise CompositionError("artifact_limit", child_id=child_id)
    # Binary delta streams can expand before Git reports the final blob size.
    # This first mode deliberately supports textual patches only.
    records = patch.splitlines()
    if any(
        line == "GIT binary patch"
        or line.startswith(
            ("Binary files ", "rename from ", "rename to ", "copy from ", "copy to ")
        )
        for line in records
    ):
        raise CompositionError("unsupported_patch", child_id=child_id)
    if (
        sum(line.startswith("diff --git ") for line in records)
        > git.limits.max_patch_records
    ):
        raise CompositionError("patch_record_limit", child_id=child_id)
    for line in records:
        mode = re.fullmatch(
            r"(?:old mode|new mode|new file mode|deleted file mode) (\d{6})", line
        )
        if mode and mode[1] not in {"100644", "100755"}:
            raise CompositionError("unsupported_mode", child_id=child_id)
    # Git uses status128 for malformed patch syntax. Parsing happens separately
    # from application; missing tools, signals and limits retain distinct errors.
    listing = git.run(
        "apply", "--numstat", "-z", "-", data=raw, failure="malformed_patch"
    )
    paths = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            added, deleted, name = record.split(b"\t", 2)
            if not added.isdigit() or not deleted.isdigit():
                raise ValueError
            paths.append(safe_path(name.decode("utf-8"), git.limits))
        except (ValueError, UnicodeError):
            raise CompositionError("unsupported_patch", child_id=child_id) from None
    if not paths or len(paths) > git.limits.max_patch_records:
        raise CompositionError("patch_record_limit", child_id=child_id)
    if not apply:
        return paths
    try:
        git.run(
            "apply",
            "--cached",
            "--check",
            "--whitespace=nowarn",
            "-",
            data=raw,
            failure="patch_conflict",
        )
        git.run(
            "apply",
            "--cached",
            "--whitespace=nowarn",
            "-",
            data=raw,
            failure="patch_conflict",
        )
    except CompositionError as exc:
        raise CompositionError(exc.code, child_id=child_id, paths=paths) from None
    return paths


def _object_usage(git):
    records = git.run(
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
    ).splitlines()
    size = 0
    for line in records:
        fields = line.split(b" ")
        if (
            len(fields) != 3
            or fields[1] not in {b"blob", b"tree", b"commit"}
            or not fields[2].isdigit()
        ):
            raise CompositionError("git_tool_failure")
        value = int(fields[2])
        if fields[1] == b"blob" and value > git.limits.blob_bytes:
            raise CompositionError("blob_limit")
        size += value
    return len(records), size


def _index(git):
    result = {}
    for record in git.run("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, sha, stage = header.decode("ascii").split(" ")
        name = safe_path(path.decode("utf-8"), git.limits)
        if mode not in {"100644", "100755"} or stage != "0" or not SHA.fullmatch(sha):
            raise CompositionError("unsupported_index")
        result[name] = (mode, sha)
    if len(result) > git.limits.max_files:
        raise CompositionError("file_limit")
    return result


def _patch_reservation(git, patch, paths):
    index = _index(git)
    affected = [index[path][1] for path in paths if path in index]
    sizes = {}
    if affected:
        raw = git.run(
            "cat-file",
            "--batch-check=%(objectname) %(objectsize)",
            data=("\n".join(sorted(set(affected))) + "\n").encode(),
        )
        for record in raw.splitlines():
            sha, size = record.decode("ascii").split(" ")
            sizes[sha] = int(size)
    # Textual application can write at most one new blob per file, with no more
    # bytes than its old content plus all supplied insertion text. Reserve the
    # conservative upper bound before Git writes, even if objects later dedupe.
    return len(paths), sum(sizes[sha] for sha in affected) + len(patch.encode())


def _tree_reservation(git):
    directories, size = {""}, 0
    for path, (mode, _) in _index(git).items():
        parts = path.split("/")
        size += len(mode) + len(parts[-1].encode()) + 22
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index])
            if directory not in directories:
                size += 5 + len(parts[index - 1].encode()) + 22
                directories.add(directory)
    return len(directories), size


def validate_inputs(inputs: dict, limits: CompositionLimits) -> dict:
    if not isinstance(inputs, dict) or set(inputs) != {"manifest", "root", "children"}:
        raise CompositionError("invalid_inputs")
    manifest = inputs["manifest"]
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("base"), str)
        or not SHA.fullmatch(manifest["base"])
    ):
        raise CompositionError("invalid_manifest")
    if (
        not isinstance(manifest.get("candidate_id"), str)
        or not DIGEST.fullmatch(manifest["candidate_id"])
        or not isinstance(manifest.get("attempt_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", manifest["attempt_id"])
    ):
        raise CompositionError("invalid_manifest")
    if len(canonical(manifest).encode()) > 100_000:
        raise CompositionError("manifest_limit")
    lineage, children = manifest.get("lineage"), inputs["children"]
    if (
        not isinstance(lineage, list)
        or not 1 <= len(lineage) <= limits.max_children
        or not isinstance(children, list)
        or len(children) != len(lineage)
    ):
        raise CompositionError("invalid_lineage")
    if any(not isinstance(item, dict) for item in lineage + children):
        raise CompositionError("invalid_lineage")
    total, seen = 0, set()
    for expected, content in [
        (manifest.get("root_artifacts"), inputs["root"]),
        *[
            (item.get("artifacts"), child.get("artifacts"))
            for item, child in zip(lineage, children)
        ],
    ]:
        if (
            not isinstance(expected, dict)
            or not isinstance(content, dict)
            or set(expected) != {"patch", "log", "manifest"}
            or set(content) != set(expected)
        ):
            raise CompositionError("incomplete_artifacts")
        for kind, value in content.items():
            if (
                not isinstance(value, str)
                or not isinstance(expected[kind], str)
                or not DIGEST.fullmatch(expected[kind])
                or content_hash(value) != expected[kind]
            ):
                raise CompositionError("artifact_hash_mismatch")
            size = len(value.encode())
            total += size
            if size > limits.artifact_bytes or total > limits.total_artifact_bytes:
                raise CompositionError("artifact_limit")
        try:
            if not isinstance(json.loads(content["manifest"]), dict):
                raise ValueError
        except ValueError:
            raise CompositionError("invalid_capture_manifest") from None
    for item, child in zip(lineage, children):
        child_id = item.get("child_id")
        if (
            not isinstance(child_id, str)
            or not CHILD.fullmatch(child_id)
            or child_id in seen
            or child.get("child_id") != child_id
        ):
            raise CompositionError("invalid_lineage")
        seen.add(child_id)
    return manifest


def compose(
    inputs: dict,
    source_path: str,
    directory: Path,
    identity: dict,
    *,
    limits: CompositionLimits | None = None,
    deadline: float | None = None,
) -> dict:
    limits = limits or CompositionLimits()
    manifest = validate_inputs(inputs, limits)
    if (
        set(identity) != {"name", "email", "timestamp"}
        or any(
            not isinstance(identity.get(k), str)
            or not identity[k]
            or len(identity[k]) > 200
            or any(c in identity[k] for c in "\r\n\0<>")
            for k in ("name", "email")
        )
        or type(identity["timestamp"]) is not int
        or not 0 < identity["timestamp"] <= 4_000_000_000
    ):
        raise CompositionError("invalid_commit_identity")
    deadline = min(
        time.monotonic() + limits.seconds,
        deadline if deadline is not None else math.inf,
    )
    if deadline <= time.monotonic():
        raise CompositionError("composition_deadline")
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = directory / "owner.json"
    if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise CompositionError("composition_directory_conflict")
    if owner.exists():
        if (
            owner.is_symlink()
            or owner.read_bytes() != b'{"protocol":"managed-composition-v1"}\n'
        ):
            raise CompositionError("composition_directory_conflict")
    else:
        if any(directory.iterdir()):
            raise CompositionError("composition_directory_conflict")
        fd = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b'{"protocol":"managed-composition-v1"}\n')
            handle.flush()
            os.fsync(handle.fileno())
    try:
        snapshot = SourceSnapshots(directory / "sources").prepare(
            source_path,
            manifest["base"],
            limits.source_bytes,
            timeout=max(0.000001, deadline - time.monotonic()),
        )
    except (OSError, SnapshotUnavailable):
        raise CompositionError("source_unavailable") from None
    source_usage = json.loads((snapshot.parent / "manifest.json").read_text())
    if (
        3 * source_usage["objects"] > limits.max_objects
        or 3 * source_usage["object_bytes"] > limits.object_bytes
    ):
        raise CompositionError("object_limit")
    repositories = []
    for name in ("children", "root"):
        target = directory / name
        if target.exists():
            shutil.rmtree(
                target
            )  # Private scratch only; immutable inputs live outside these indexes.
        shutil.copytree(snapshot, target)
        repositories.append(Git(target, deadline, limits))
    children_git, root_git = repositories

    def check_objects(reserve_count=0, reserve_bytes=0):
        counts, sizes = zip(*[_object_usage(git) for git in repositories])
        if (
            sum(counts) + source_usage["objects"] + reserve_count > limits.max_objects
            or sum(sizes) + source_usage["object_bytes"] + reserve_bytes
            > limits.object_bytes
        ):
            raise CompositionError("object_limit")

    base = manifest["base"]
    base_entries = _entries(children_git, base)
    check_objects()
    total_records = 0
    for patch, child_id in [
        *[
            (child["artifacts"]["patch"], child["child_id"])
            for child in inputs["children"]
        ],
        (inputs["root"]["patch"], None),
    ]:
        total_records += len(_patch(children_git, patch, child_id, apply=False))
        if total_records > limits.max_patch_records:
            raise CompositionError("patch_record_limit")
    for git in repositories:
        git.run("read-tree", base)
    for child in inputs["children"]:
        patch = child["artifacts"]["patch"]
        paths = _patch(children_git, patch, child["child_id"], apply=False)
        check_objects(*_patch_reservation(children_git, patch, paths))
        _patch(children_git, patch, child["child_id"])
        check_objects()
    root_paths = _patch(root_git, inputs["root"]["patch"], apply=False)
    check_objects(*_patch_reservation(root_git, inputs["root"]["patch"], root_paths))
    _patch(root_git, inputs["root"]["patch"])
    check_objects()
    tree_counts, tree_sizes = zip(*[_tree_reservation(git) for git in repositories])
    check_objects(sum(tree_counts), sum(tree_sizes))
    tree = children_git.run("write-tree").decode().strip()
    if tree != root_git.run("write-tree").decode().strip():
        raise CompositionError("candidate_tree_mismatch")
    check_objects()
    entries = _entries(children_git, tree)
    base_tree = children_git.run("rev-parse", base + "^{tree}").decode().strip()
    if tree == base_tree:
        raise CompositionError("empty_composition")
    manifest_hash = digest(manifest)
    message = (
        "Compose canonical candidate " + manifest["candidate_id"] + "\n\n"
        "OpenJules-Manifest: " + manifest_hash + "\n"
        "Root-Attempt: " + manifest["attempt_id"] + "\n"
        "Mechanical composition; repository checks and independent review pending.\n"
    )
    person = f"{identity['name']} <{identity['email']}> {identity['timestamp']} +0000"
    commit_content = f"tree {tree}\nparent {base}\nauthor {person}\ncommitter {person}\n\n{message}".encode()
    commit = object_hash("commit", commit_content)
    check_objects(1, len(commit_content))
    if (
        children_git.run(
            "hash-object", "-t", "commit", "-w", "--stdin", data=commit_content
        )
        .decode()
        .strip()
        != commit
    ):
        raise CompositionError("git_identity_mismatch")
    check_objects()
    original = {entry["sha"] for entry in base_entries}
    blobs = {}
    total = 0
    for sha in sorted({entry["sha"] for entry in entries} - original):
        content = children_git.run("cat-file", "blob", sha, limit=limits.blob_bytes)
        if object_hash("blob", content) != sha:
            raise CompositionError("git_identity_mismatch")
        total += len(content)
        if total > limits.total_artifact_bytes + limits.source_bytes:
            raise CompositionError("tree_byte_limit")
        blobs[sha] = content
    return {
        "manifest_sha256": manifest_hash,
        "tree_sha": tree,
        "base_tree_sha": base_tree,
        "commit_sha": commit,
        "commit_message": message,
        "identity": dict(identity),
        "entries": entries,
        "blobs": blobs,
        "limits": asdict(limits),
    }
