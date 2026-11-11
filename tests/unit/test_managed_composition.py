"""Real Git composition proof. These fixtures never assert model quality."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import time

import pytest

from src.git_manager.managed_composition import (
    CompositionError,
    CompositionLimits,
    Git,
    canonical,
    compose,
    content_hash,
    digest,
    object_hash,
    safe_path,
    validate_inputs,
)


def git(path, *args, data=None):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        input=data,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout


def captures(patch):
    values = {
        "patch": patch,
        "log": "deterministic fixture output\n",
        "manifest": '{"schema_version":1}',
    }
    return values, {key: content_hash(value) for key, value in values.items()}


def final_case(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    (source / "alpha.txt").write_text("old alpha\n")
    (source / "beta.txt").write_text("old beta\n")
    git(source, "add", ".")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    base = git(source, "rev-parse", "HEAD").strip()
    patches = []
    for name, value in (("alpha.txt", "new alpha\n"), ("beta.txt", "new beta\n")):
        (source / name).write_text(value)
        git(source, "add", name)
        patches.append(
            git(source, "diff", "--cached", "--binary", "--no-renames", base)
        )
        git(source, "reset", "--hard", "-q", base)
    (source / "alpha.txt").write_text("new alpha\n")
    (source / "beta.txt").write_text("new beta\n")
    git(source, "add", ".")
    root_values, root_hashes = captures(
        git(source, "diff", "--cached", "--binary", "--no-renames", base)
    )
    git(source, "reset", "--hard", "-q", base)
    scope = {
        "deployment_id": "publication-test",
        "repository": "github:github.com/owner/project",
    }
    task_id, attempt_id, worker_id = "task-test", "a" * 32, "b" * 32
    common = {
        **scope,
        "runtime_principal": "runtime-test",
        "worker_id": worker_id,
        "profile": "approved-v1",
        "profile_digest": digest({"fixture": True}),
        "requirements_revision": 1,
        "base": base,
    }
    binding = {
        **common,
        "attempt_id": attempt_id,
        "launch_key": "polyphony:" + attempt_id,
        "deadline": 1740000300,
        "spec_digest": digest(common),
    }
    lineage, children, responses = [], [], []
    for index, patch in enumerate(patches, 1):
        child_id = "child_" + f"{index:032x}"
        values, hashes = captures(patch)
        instructions = "Change " + str(index)
        grant = {
            **common,
            "child_id": child_id,
            "root_attempt_id": attempt_id,
            "parent_attempt_id": attempt_id,
            "task_id": task_id,
            "launch_key": "polyphony-child:" + child_id,
            "allocated_seconds": 30,
            "deadline": 1740000030,
            "max_turns": 4,
            "capabilities": ["python"],
            "instructions_digest": content_hash(instructions),
        }
        grant["grant_digest"] = digest(grant)
        item = {
            "child_id": child_id,
            "grant_digest": grant["grant_digest"],
            "artifacts": hashes,
        }
        lineage.append(item)
        children.append({"child_id": child_id, "artifacts": values})
        child = {
            "id": child_id,
            "binding": grant,
            "instructions": instructions,
            "termination_confirmed": True,
            "launch_fenced": True,
            "captured_artifacts": hashes,
            "state": "exited",
        }
        responses.append(
            {
                "schema_version": 1,
                **scope,
                "root_attempt_id": attempt_id,
                "binding": grant,
                "child": child,
                "instructions": instructions,
                "launch_allowed": False,
                "remaining_seconds": 0,
                "reason": "terminated",
            }
        )
    requirements = "Change both files and request independent review"
    identity = {
        "base": base,
        "patch": root_hashes["patch"],
        "requirements": requirements,
        "revision": 1,
        "task": task_id,
        "dependency_admissions": {},
        "child_lineage": lineage,
    }
    candidate_id = digest(identity)
    author = "runtime:" + digest([common["runtime_principal"], worker_id])
    candidate = {
        "id": candidate_id,
        **identity,
        "author": author,
        "attempt": attempt_id,
        "author_principal": common["runtime_principal"],
    }
    root = {
        "id": attempt_id,
        "task": task_id,
        "worker": author,
        "requirements_revision": 1,
        "approved_requirements": requirements,
        "execution_mode": "external",
        "state": "exited",
        "termination_confirmed": True,
        "launch_fenced": True,
        "runtime_binding": binding,
        "captured_artifacts": root_hashes,
        "child_lineage": lineage,
        "started": 1740000000.25,
    }
    task = {
        "id": task_id,
        "managed_scope": scope,
        "status": "awaiting_review",
        "revision": 1,
        "requirements": requirements,
        "current_candidate": candidate_id,
    }
    manifest = {
        "schema_version": 1,
        **scope,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "candidate_id": candidate_id,
        "base": base,
        "revision": 1,
        "requirements_digest": content_hash(requirements),
        "dependency_admissions": {},
        "timestamp": 1740000000,
        **{
            key: common[key]
            for key in ("runtime_principal", "worker_id", "profile", "profile_digest")
        },
        "root_artifacts": root_hashes,
        "lineage": lineage,
    }
    return {
        "source": source,
        "inputs": {"manifest": manifest, "root": root_values, "children": children},
        "snapshot": {
            "schema_version": 1,
            **scope,
            "task": [task],
            "attempt": [root],
            "candidate": [candidate],
        },
        "children": {
            "schema_version": 1,
            **scope,
            "root_attempt_id": attempt_id,
            "admission_closed": True,
            "children": responses,
        },
    }


@pytest.fixture
def case(tmp_path):
    return final_case(tmp_path)


def perform(case, tmp_path, **kwargs):
    return compose(
        case["inputs"],
        str(case["source"]),
        tmp_path / "composition",
        {
            "name": "OpenJules",
            "email": "openjules@example.invalid",
            "timestamp": 1740000000,
        },
        **kwargs,
    )


def test_two_final_patches_equal_root_tree_and_make_one_real_stable_commit(
    case, tmp_path
):
    first = perform(case, tmp_path)
    second = perform(case, tmp_path)
    assert first == second
    repo = tmp_path / "composition/children"
    raw = subprocess.check_output(
        ["git", "--git-dir=" + str(repo), "cat-file", "commit", first["commit_sha"]]
    )
    assert object_hash("commit", raw) == first["commit_sha"]
    assert raw.startswith(
        f"tree {first['tree_sha']}\nparent {case['inputs']['manifest']['base']}\n".encode()
    )
    assert first["manifest_sha256"].encode() in raw
    assert {entry["path"] for entry in first["entries"]} == {"alpha.txt", "beta.txt"}
    assert (case["source"] / "alpha.txt").read_text() == "old alpha\n"
    assert not (repo / "alpha.txt").exists()  # No repository checkout on the host.


def test_conflicting_children_are_distinct_from_malformed_patch_and_tool_failure(
    case, tmp_path
):
    bad = deepcopy(case)
    bad["inputs"]["children"][1]["artifacts"]["patch"] = bad["inputs"]["children"][0][
        "artifacts"
    ]["patch"].replace("new alpha", "different alpha")
    bad["inputs"]["manifest"]["lineage"][1]["artifacts"]["patch"] = content_hash(
        bad["inputs"]["children"][1]["artifacts"]["patch"]
    )
    with pytest.raises(CompositionError) as conflict:
        perform(bad, tmp_path)
    assert conflict.value.code == "patch_conflict" and conflict.value.paths == (
        "alpha.txt",
    )
    bad["inputs"]["children"][1]["artifacts"]["patch"] = "not a patch"
    bad["inputs"]["manifest"]["lineage"][1]["artifacts"]["patch"] = content_hash(
        "not a patch"
    )
    with pytest.raises(CompositionError, match="malformed_patch"):
        perform(bad, tmp_path)
    runner = Git(case["source"] / ".git", time.monotonic() + 5, CompositionLimits())
    with pytest.raises(CompositionError, match="git_tool_failure"):
        runner.run("nonexistent-plumbing-command")


def test_root_candidate_must_equal_final_composition(case, tmp_path):
    case["inputs"]["root"]["patch"] = case["inputs"]["children"][0]["artifacts"][
        "patch"
    ]
    case["inputs"]["manifest"]["root_artifacts"]["patch"] = content_hash(
        case["inputs"]["root"]["patch"]
    )
    with pytest.raises(CompositionError, match="candidate_tree_mismatch"):
        perform(case, tmp_path)


def test_artifact_hash_and_aggregate_bounds_fail_before_git(case, tmp_path):
    broken = deepcopy(case["inputs"])
    broken["children"][0]["artifacts"]["patch"] += "tampered"
    with pytest.raises(CompositionError, match="artifact_hash_mismatch"):
        compose(broken, str(case["source"]), tmp_path / "never-created", {})
    assert not (tmp_path / "never-created").exists()
    with pytest.raises(CompositionError, match="artifact_limit"):
        perform(
            case, tmp_path, limits=replace(CompositionLimits(), total_artifact_bytes=40)
        )
    with pytest.raises(CompositionError, match="file_limit"):
        perform(case, tmp_path, limits=replace(CompositionLimits(), max_files=1))
    with pytest.raises(CompositionError, match="blob_limit"):
        perform(case, tmp_path, limits=replace(CompositionLimits(), blob_bytes=4))
    with pytest.raises(CompositionError, match="source_unavailable"):
        perform(case, tmp_path, limits=replace(CompositionLimits(), source_bytes=20))


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../escape",
        "a/../../escape",
        ".git/config",
        "a/.GiT/config",
        "a//b",
        "a\\b",
        "a\nb",
        "a\0b",
    ],
)
def test_unsafe_paths_are_explicitly_rejected(path):
    with pytest.raises(CompositionError, match="unsupported_path"):
        safe_path(path, CompositionLimits())


@pytest.mark.parametrize(
    "patch",
    [
        "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/outside\n",
        "diff --git a/sub b/sub\nnew file mode 160000\n",
        "diff --git a/x b/x\nGIT binary patch\nliteral 999999999999\n",
        "diff --git a/a b/b\nrename from a\nrename to b\n",
    ],
)
def test_unsupported_modes_and_binary_expansion_fail_before_apply(
    case, tmp_path, patch
):
    case["inputs"]["children"][0]["artifacts"]["patch"] = patch
    case["inputs"]["manifest"]["lineage"][0]["artifacts"]["patch"] = content_hash(patch)
    with pytest.raises(CompositionError, match="unsupported_"):
        perform(case, tmp_path)


def test_source_config_hooks_filters_environment_and_untracked_files_are_not_executed(
    case, tmp_path, monkeypatch
):
    marker = tmp_path / "must-not-exist"
    git(case["source"], "config", "core.hooksPath", str(tmp_path / "hooks"))
    git(case["source"], "config", "filter.evil.clean", f"touch {marker}")
    git(case["source"], "config", "core.fsmonitor", f"touch {marker}")
    (case["source"] / ".env").write_text("unrelated secret")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(case["source"] / ".git/config"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "must-not-run")
    result = perform(case, tmp_path)
    assert not marker.exists()
    assert all(entry["path"] != ".env" for entry in result["entries"])


def test_deadline_output_and_patch_record_limits_are_hard(case, tmp_path):
    runner = Git(case["source"] / ".git", time.monotonic() - 1, CompositionLimits())
    with pytest.raises(CompositionError, match="composition_deadline"):
        runner.run("rev-parse", "HEAD")
    runner = Git(
        case["source"] / ".git",
        time.monotonic() + 5,
        replace(CompositionLimits(), git_output_bytes=10),
    )
    with pytest.raises(CompositionError, match="git_output_limit"):
        runner.run("cat-file", "commit", "HEAD")
    with pytest.raises(CompositionError, match="patch_record_limit"):
        perform(
            case, tmp_path, limits=replace(CompositionLimits(), max_patch_records=1)
        )


def test_tree_policy_rejects_existing_symlink_without_following_it(tmp_path):
    case = final_case(tmp_path)
    (case["source"] / "link").symlink_to("/outside")
    git(case["source"], "add", "link")
    git(
        case["source"],
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "symlink base",
    )
    case["inputs"]["manifest"]["base"] = git(
        case["source"], "rev-parse", "HEAD"
    ).strip()
    with pytest.raises(CompositionError, match="unsupported_mode"):
        perform(case, tmp_path)


def test_aggregate_records_and_all_copied_object_stores_are_bounded(case, tmp_path):
    # Each child has one record and the root has two. The aggregate is four.
    with pytest.raises(CompositionError, match="patch_record_limit"):
        perform(
            case, tmp_path, limits=replace(CompositionLimits(), max_patch_records=3)
        )
    with pytest.raises(CompositionError, match="object_limit"):
        perform(case, tmp_path, limits=replace(CompositionLimits(), max_objects=1))
    with pytest.raises(CompositionError, match="object_limit"):
        perform(case, tmp_path, limits=replace(CompositionLimits(), object_bytes=1))


def test_composition_never_removes_an_unowned_existing_directory(case, tmp_path):
    directory = tmp_path / "composition"
    directory.mkdir(mode=0o700)
    original = directory / "children"
    original.mkdir()
    (original / "retain.txt").write_text("existing operator data")
    with pytest.raises(CompositionError, match="composition_directory_conflict"):
        perform(case, tmp_path)
    assert (original / "retain.txt").read_text() == "existing operator data"
