import os
from pathlib import Path
import subprocess

import pytest

from src.worker.source_snapshot import SnapshotUnavailable, SourceSnapshots


def git(repo, *arguments, input=None, check=True):
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                *arguments,
            ],
            input=input,
            capture_output=True,
            check=check,
        )
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "old-secret.txt").write_text("PRIVATE_ANCESTOR_MARKER")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "private ancestor")
    parent = git(repo, "rev-parse", "HEAD")
    old_blob = git(repo, "rev-parse", "HEAD:old-secret.txt")
    git(repo, "rm", "-q", "old-secret.txt")
    (repo / "approved.txt").write_text("approved tree\n")
    (repo / ".gitignore").write_text(".env\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "approved base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "replacement-secret.txt").write_text("PRIVATE_OTHER_REF_MARKER")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "unapproved future")
    future = git(repo, "rev-parse", "HEAD")
    future_blob = git(repo, "rev-parse", "HEAD:replacement-secret.txt")
    git(repo, "replace", base, future)
    (repo / ".env").write_text("PRIVATE_IGNORED_MARKER")
    git(
        repo,
        "config",
        "credential.helper",
        "!touch " + str(tmp_path / "credential-executed"),
    )
    git(
        repo,
        "config",
        "remote.origin.url",
        "https://PRIVATE_CONFIG_MARKER@example.invalid/private",
    )
    git(
        repo,
        "config",
        "uploadpack.packObjectsHook",
        "touch " + str(tmp_path / "pack-hook-executed"),
    )
    (repo / ".git/hooks/post-checkout").write_text(
        "touch " + str(tmp_path / "hook-executed")
    )
    return repo, base, parent, old_blob, future, future_blob


def test_export_contains_only_exact_commit_tree_and_blobs(history, tmp_path):
    repo, base, parent, old_blob, future, future_blob = history
    snapshot = SourceSnapshots(tmp_path / "cache").prepare(str(repo), base, 1048576)
    assert git(snapshot, "rev-parse", "HEAD") == base
    assert git(snapshot, "rev-list", "--all") == base
    assert not (snapshot / ".env").exists()
    assert not (snapshot / "hooks").exists()
    assert not (snapshot / "objects/info/alternates").exists()
    assert "PRIVATE_CONFIG_MARKER" not in (snapshot / "config").read_text()
    for identity in (parent, old_blob, future, future_blob):
        result = subprocess.run(
            ["git", "--git-dir=" + str(snapshot), "cat-file", "-e", identity],
            capture_output=True,
        )
        assert result.returncode != 0
    clone = tmp_path / "checkout"
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--no-local",
            str(snapshot),
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    assert (clone / "approved.txt").read_text() == "approved tree\n"
    assert git(clone, "rev-parse", "HEAD") == base
    assert not (clone / "replacement-secret.txt").exists()
    for name in ("credential-executed", "pack-hook-executed", "hook-executed"):
        assert not (tmp_path / name).exists()


def test_source_config_is_never_parsed_and_git_environment_is_scrubbed(
    history, tmp_path, monkeypatch
):
    repo, base, *_ = history
    (repo / ".git/config").write_text("[this config cannot be parsed\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(repo / ".git/config"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "malformed environment config")
    snapshot = SourceSnapshots(tmp_path / "cache").prepare(str(repo), base, 1048576)
    environment = dict(os.environ)
    environment.pop("GIT_CONFIG_PARAMETERS")
    environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
    result = subprocess.check_output(
        ["git", "--git-dir=" + str(snapshot), "show", base + ":approved.txt"],
        env=environment,
    )
    assert result == b"approved tree\n"


def test_export_enforces_cumulative_byte_and_time_bounds(history, tmp_path):
    repo, base, *_ = history
    builder = SourceSnapshots(tmp_path / "cache")
    with pytest.raises(SnapshotUnavailable):
        builder.prepare(str(repo), base, 64)
    assert not list((tmp_path / "cache").glob("export-*"))
    with pytest.raises(SnapshotUnavailable, match="deadline"):
        builder.prepare(str(repo), base, 1048576, timeout=0)


def test_cached_export_remains_immutable_and_respects_new_limit(history, tmp_path):
    repo, base, *_ = history
    builder = SourceSnapshots(tmp_path / "cache")
    first = builder.prepare(str(repo), base, 1048576)
    config = (first / "config").read_bytes()
    (repo / ".git/config").write_text("changed host secret")
    assert builder.prepare(str(repo), base, 1048576) == first
    assert (first / "config").read_bytes() == config
    with pytest.raises(SnapshotUnavailable):
        builder.prepare(str(repo), base, 1)


def test_corrupt_commit_never_produces_a_source_mount(history, tmp_path):
    repo, base, *_ = history
    commit = repo / ".git/objects" / base[:2] / base[2:]
    commit.chmod(0o600)
    commit.write_bytes(b"corrupt object")
    builder = SourceSnapshots(tmp_path / "cache")
    with pytest.raises(SnapshotUnavailable):
        builder.prepare(str(repo), base, 1048576)
    assert not list((tmp_path / "cache").glob("*/repo"))
