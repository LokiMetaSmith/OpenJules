"""Canonical bindings and durable publication effects; forge calls are labelled fixtures."""

from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.forge.managed_github import (
    ForgeError,
    ManagedGitHubPublisher,
    PublicationPolicy,
)
from src.git_manager.managed_composition import CompositionLimits
from src.polyphony.managed_client import ManagedAuthorityClient
from src.worker.managed_publication import (
    CanonicalPublicationReader,
    PublicationError,
    PublicationJournal,
    PublicationService,
    configured_service,
)
from tests.unit.test_managed_composition import final_case


def policy():
    return PublicationPolicy(
        "publication-test",
        "github:github.com/owner/project",
        123,
        "main",
        "env:PUBLICATION_TEST_TOKEN",
        "OpenJules",
        "openjules@example.invalid",
    )


class FixtureReader(CanonicalPublicationReader):
    def __init__(self, case):
        self.case = case
        self.calls = []
        super().__init__(
            ManagedAuthorityClient(
                "http://127.0.0.1:12345", "env:AUTHORITY_TEST_TOKEN"
            ),
            policy(),
            case["inputs"]["manifest"]["base"],
            CompositionLimits(),
        )

    def _get(self, resource, *, query=None, deadline, text=False):
        self.calls.append(resource)
        if resource == "snapshot":
            return deepcopy(self.case["snapshot"])
        if resource == "children":
            return deepcopy(self.case["children"])
        sha = resource.rsplit("/", 1)[1]
        collections = (
            [self.case["inputs"]["root"]]
            if resource.startswith("artifacts/")
            else [
                item["artifacts"]
                for item in self.case["inputs"]["children"]
                if item["child_id"] == resource.split("/")[1]
            ]
        )
        from src.git_manager.managed_composition import content_hash

        values = [
            value
            for collection in collections
            for value in collection.values()
            if content_hash(value) == sha
        ]
        assert values
        return values[0]


class FixtureForge:
    """Effectful in-memory fixture. Real HTTP/Git object checks live in integration tests."""

    def __init__(self, case):
        self.policy = policy()
        self.base = case["inputs"]["manifest"]["base"]
        self.refs = {"main": self.base}
        self.effects = []
        self.proposal = None
        self.fail_after = None
        self.before_create = None

    def credential(self):
        return ManagedGitHubPublisher(self.policy).credential()

    def validate_repository(self, base, base_tree, *, deadline):
        self.credential()
        if self.refs["main"] != base:
            raise ForgeError("stale_base")

    def read_branch(self, branch, *, deadline):
        return self.refs.get(branch)

    def upload_objects(self, composition, base, *, deadline):
        self.effects.append(("objects", composition["commit_sha"]))
        if self.fail_after == "objects":
            self.fail_after = None
            raise ForgeError("forge_unavailable", uncertain=True)

    def create_branch(self, branch, commit, *, deadline):
        assert branch not in self.refs
        self.effects.append(("branch", branch, commit))
        self.refs[branch] = commit
        if self.fail_after == "branch":
            self.fail_after = None
            raise ForgeError("forge_unavailable", uncertain=True)

    def find_proposal(self, branch, commit, marker, *, deadline):
        if self.proposal and (
            self.proposal["head_sha"] != commit
            or self.proposal["head_branch"] != branch
        ):
            raise ForgeError("forge_proposal_mismatch")
        return deepcopy(self.proposal)

    def create_draft(self, branch, commit, marker, body, *, deadline):
        assert self.proposal is None
        self.effects.append(("draft", branch, commit))
        if self.before_create:
            self.before_create()
        self.proposal = {
            "number": 7,
            "url": "https://github.com/owner/project/pull/7",
            "state": "open",
            "draft": True,
            "head_sha": commit,
            "base_sha": self.base,
            "head_branch": branch,
            "base_branch": "main",
            "repository_id": 123,
        }
        self.body, self.marker = body, marker
        if self.fail_after == "draft":
            self.fail_after = None
            raise ForgeError("forge_unavailable", uncertain=True)
        return deepcopy(self.proposal)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLICATION_TEST_TOKEN", "synthetic-publication-token")
    case = final_case(tmp_path)
    reader, forge = FixtureReader(case), FixtureForge(case)
    service = PublicationService(reader, forge, case["source"], tmp_path / "state")
    return case, reader, forge, service


def test_actual_canonical_contract_resolves_complete_final_lineage(setup):
    case, reader, _, _ = setup
    result = reader.read(
        case["inputs"]["manifest"]["candidate_id"], None, deadline=1e20
    )
    assert result == case["inputs"]
    calls = len(reader.calls)
    assert (
        reader.read(
            result["manifest"]["candidate_id"], None, deadline=1e20, cached=result
        )
        == result
    )
    assert reader.calls[calls:] == ["snapshot", "children"]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda c: c["snapshot"]["task"][0].update(revision=2), "stale_candidate"),
        (
            lambda c: c["snapshot"]["task"][0].update(current_candidate="f" * 64),
            "stale_candidate",
        ),
        (
            lambda c: c["snapshot"]["task"][0].update(status="needs_revision"),
            "stale_candidate",
        ),
        (
            lambda c: c["snapshot"].update(
                repository="github:github.com/other/project"
            ),
            "canonical_mismatch",
        ),
        (
            lambda c: c["snapshot"]["attempt"][0]["runtime_binding"].update(
                base="f" * 40
            ),
            "canonical_mismatch",
        ),
        (
            lambda c: c["children"]["children"][0]["child"].update(
                termination_confirmed=False
            ),
            "unresolved_child",
        ),
        (
            lambda c: c["children"]["children"][0]["binding"].update(worker_id="other"),
            "canonical_mismatch",
        ),
        (lambda c: c["children"]["children"].pop(), "invalid_lineage"),
    ],
)
def test_stale_foreign_unfenced_or_wrong_lineage_is_rejected_before_effects(
    setup, mutation, reason
):
    case, reader, forge, service = setup
    mutation(case)
    with pytest.raises(PublicationError, match=reason):
        service.run(case["inputs"]["manifest"]["candidate_id"])
    assert forge.effects == []


def test_order_must_include_every_final_child_once(setup):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    child = case["inputs"]["children"][0]["child_id"]
    for order in ([], [child], [child, child]):
        with pytest.raises(PublicationError, match="invalid_lineage"):
            service.run(cid, order)
    assert not forge.effects


def test_draft_creation_is_real_identity_and_replay_has_no_more_effects(setup):
    case, reader, forge, service = setup
    first = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert first["status"] == "draft_published"
    assert first["proposal"]["url"] == "https://github.com/owner/project/pull/7"
    assert not first["ready_for_review"] and not first["release_authorized"]
    assert [effect[0] for effect in forge.effects] == ["objects", "branch", "draft"]
    second = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert first == second and len(forge.effects) == 3
    state = service.journal.read(first["operation"])
    assert state["inputs"] == case["inputs"] and state["pr_intent"]
    assert "checks and independent review remain pending" in forge.body
    assert case["inputs"]["manifest"]["lineage"][0]["artifacts"]["patch"] in forge.body


@pytest.mark.parametrize("effect", ["objects", "branch", "draft"])
def test_lost_effect_reply_reconciles_same_operation_after_service_restart(
    setup, tmp_path, effect
):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    forge.fail_after = effect
    first = service.run(cid)
    assert first["status"] == "uncertain"
    restarted = PublicationService(reader, forge, case["source"], tmp_path / "state")
    second = restarted.run(cid)
    assert (
        second["status"] == "draft_published"
        and first["commit_sha"] == second["commit_sha"]
    )
    assert sum(item[0] == "branch" for item in forge.effects) == 1
    assert sum(item[0] == "draft" for item in forge.effects) == 1


def test_ambiguous_pr_intent_with_no_result_is_not_blindly_resent(setup):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    forge.fail_after = "draft"
    first = service.run(cid)
    forge.proposal = None  # Simulate ambiguity, not proof of remote noncreation.
    effects = list(forge.effects)
    replay = service.run(cid)
    assert (
        replay["status"] == "uncertain"
        and replay["reason"] == "pr_creation_requires_reconciliation"
    )
    assert forge.effects == effects
    assert service.journal.read(first["operation"])["pr_intent"] is True


def test_closed_proposal_is_same_closed_result_and_never_reopened(setup):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    first = service.run(cid)
    forge.proposal["state"] = "closed"
    case["snapshot"]["task"][0]["revision"] += 1
    replay = service.run(cid)
    assert (
        replay["status"] == "existing_closed"
        and replay["proposal"]["number"] == first["proposal"]["number"]
    )
    assert len(forge.effects) == 3


@pytest.mark.parametrize("change", ["canonical", "base"])
def test_concurrent_change_during_creation_reports_stale_draft_without_claiming_atomicity(
    setup, change
):
    case, reader, forge, service = setup
    if change == "canonical":
        forge.before_create = lambda: case["snapshot"]["task"][0].update(revision=2)
    else:
        forge.before_create = lambda: forge.refs.update(main="f" * 40)
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert result["status"] == "draft_stale" and result["proposal"]["draft"] is True
    assert not result["release_authorized"]


def test_stale_base_after_branch_intent_cannot_publish_or_replace_branch(setup):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    forge.fail_after = "branch"
    assert service.run(cid)["status"] == "uncertain"
    forge.refs["main"] = "f" * 40
    replay = service.run(cid)
    assert replay["status"] == "conflict" and replay["reason"] == "stale_base"
    assert sum(item[0] == "branch" for item in forge.effects) == 1
    assert sum(item[0] == "draft" for item in forge.effects) == 0


def test_missing_credential_retains_real_composition_without_fake_pr(
    setup, monkeypatch
):
    case, reader, forge, service = setup
    monkeypatch.delenv("PUBLICATION_TEST_TOKEN")
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert (
        result["status"] == "unavailable"
        and result["reason"] == "publication_unavailable"
    )
    assert len(result["commit_sha"]) == 40 and "proposal" not in result
    assert not forge.effects
    assert service.journal.read(result["operation"])["phase"] == "composed"


def test_changed_order_or_policy_does_not_create_another_operation(setup):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    service.run(cid)
    with pytest.raises(PublicationError, match="operation_input_changed"):
        service.run(
            cid, [item["child_id"] for item in reversed(case["inputs"]["children"])]
        )
    forge.policy = PublicationPolicy(
        **(asdict(forge.policy) | {"author_name": "Changed"})
    )
    altered = PublicationService(reader, forge, case["source"], service.journal.root)
    with pytest.raises(PublicationError, match="operation_input_changed"):
        altered.run(cid)
    assert len(forge.effects) == 3


def test_journal_owner_is_exclusive_across_processes_and_permissions_are_private(
    tmp_path,
):
    journal = PublicationJournal(tmp_path / "state")
    code = "from src.worker.managed_publication import PublicationJournal,PublicationError; import sys\ntry:\n with PublicationJournal(sys.argv[1]).locked(): pass\nexcept PublicationError as e:\n sys.exit(7 if e.code=='publication_busy' else 8)\n"
    with journal.locked():
        result = subprocess.run(
            [sys.executable, "-c", code, str(journal.root)],
            capture_output=True,
            timeout=10,
        )
    assert result.returncode == 7
    assert journal.root.stat().st_mode & 0o077 == 0
    assert (journal.root / "operation.lock").stat().st_mode & 0o077 == 0


def test_configuration_is_explicit_and_cannot_reuse_control_reference(setup, tmp_path):
    case, _, _, _ = setup
    configuration = {
        "schema_version": 1,
        "state_directory": str(tmp_path / "config-state"),
        "source_path": str(case["source"]),
        "base_commit": case["inputs"]["manifest"]["base"],
        "authority": {
            "endpoint": "http://127.0.0.1:12345",
            "token_ref": "env:AUTHORITY_TEST_TOKEN",
        },
        "forge": asdict(policy()),
    }
    assert isinstance(configured_service(configuration), PublicationService)
    configuration["forge"]["token_ref"] = "env:AUTHORITY_TEST_TOKEN"
    with pytest.raises(ValueError, match="distinct"):
        configured_service(configuration)
    configuration["forge"]["token_ref"] = "inline-secret"
    with pytest.raises(ValueError, match="environment"):
        configured_service(configuration)


def _replace_logs(case, text):
    from src.git_manager.managed_composition import content_hash, digest

    case["inputs"]["root"]["log"] = text
    case["inputs"]["manifest"]["root_artifacts"]["log"] = content_hash(text)
    for child, item in zip(
        case["inputs"]["children"], case["inputs"]["manifest"]["lineage"]
    ):
        child["artifacts"]["log"] = text
        item["artifacts"]["log"] = content_hash(text)
    candidate = case["snapshot"]["candidate"][0]
    candidate_id = digest(
        {
            key: candidate[key]
            for key in (
                "base",
                "patch",
                "requirements",
                "revision",
                "task",
                "dependency_admissions",
                "child_lineage",
            )
        }
    )
    candidate["id"] = candidate_id
    case["snapshot"]["task"][0]["current_candidate"] = candidate_id
    case["inputs"]["manifest"]["candidate_id"] = candidate_id
    return candidate_id


def test_multibyte_and_control_heavy_artifacts_survive_journal_restart(
    setup, monkeypatch
):
    case, reader, forge, service = setup
    cid = _replace_logs(case, "\0雪" * 10000)
    monkeypatch.delenv("PUBLICATION_TEST_TOKEN")
    retained = service.run(cid)
    assert retained["status"] == "unavailable"
    assert service.journal.read(retained["operation"])["inputs"] == case["inputs"]
    monkeypatch.setenv("PUBLICATION_TEST_TOKEN", "synthetic-publication-token")
    restarted = PublicationService(reader, forge, case["source"], service.journal.root)
    result = restarted.run(cid)
    assert (
        result["status"] == "draft_published"
        and result["commit_sha"] == retained["commit_sha"]
    )


def test_encoded_journal_limit_rejects_legal_raw_artifact_total_before_writes_or_forge(
    setup,
):
    case, reader, forge, service = setup
    # Three 4MB logs fit individual and aggregate raw bounds. Escaped JSON does not.
    cid = _replace_logs(case, "\0" * 4_000_000)
    with pytest.raises(PublicationError, match="journal_byte_limit"):
        service.run(cid)
    assert not forge.effects
    assert not list(service.journal.root.rglob("state.json"))
    assert not list(service.journal.root.rglob("*.pending"))


def test_overall_deadline_includes_canonical_reads_and_does_not_start_a_fresh_git_budget(
    setup, monkeypatch
):
    from src.worker import managed_publication as publication

    case, reader, forge, service = setup
    current = [100.0]
    monkeypatch.setattr(publication.time, "monotonic", lambda: current[0])
    original = reader.read
    calls = [0]

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            current[0] += 2
        return result

    monkeypatch.setattr(reader, "read", delayed)
    service.operation_seconds = 1
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert (
        result["status"] == "unavailable" and result["reason"] == "composition_deadline"
    )
    assert (
        not forge.effects
        and not (service.journal.location(result["operation"]) / "git").exists()
    )


def test_missing_credential_during_ambiguous_pr_recovery_does_not_clear_original_intent(
    setup, monkeypatch
):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    forge.fail_after = "draft"
    first = service.run(cid)

    def unavailable(*args, **kwargs):
        raise ForgeError("publication_unavailable")

    monkeypatch.setattr(forge, "find_proposal", unavailable)
    assert service.run(cid)["status"] == "uncertain"
    assert service.journal.read(first["operation"])["pr_intent"] is True
    assert sum(item[0] == "draft" for item in forge.effects) == 1


def test_explicit_local_rejection_before_pr_transport_can_retry_without_false_uncertainty(
    setup, monkeypatch
):
    case, reader, forge, service = setup
    cid = case["inputs"]["manifest"]["candidate_id"]
    original = forge.create_draft

    def local_failure(*args, **kwargs):
        raise ForgeError("forge_request_limit")

    monkeypatch.setattr(forge, "create_draft", local_failure)
    first = service.run(cid)
    assert (
        first["status"] == "unavailable"
        and service.journal.read(first["operation"])["pr_intent"] is False
    )
    monkeypatch.setattr(forge, "create_draft", original)
    assert service.run(cid)["status"] == "draft_published"


@pytest.mark.parametrize(
    "token", ["non-ascii-雪", "control-\x01", "x" * 8193, "has space"]
)
def test_invalid_header_credentials_fail_before_forge_or_pr_intent(
    setup, monkeypatch, token
):
    case, reader, forge, service = setup
    monkeypatch.setenv("PUBLICATION_TEST_TOKEN", token)
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert (
        result["status"] == "unavailable"
        and result["reason"] == "publication_unavailable"
    )
    assert not forge.effects and not service.journal.read(result["operation"]).get(
        "pr_intent"
    )
