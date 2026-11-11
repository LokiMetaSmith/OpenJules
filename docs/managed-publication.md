# Final child composition and draft publication

The standalone operator command reconstructs all final child patches and checks
that their combined Git tree exactly matches an existing canonical root
candidate. It creates one real commit and one ordinary **draft** GitHub PR.
The component does not run repository tests, claim independent review, accept a
task, mark a PR ready or merge it.

This is the first strict mode for OpenJules issue #6. It adds no task authority or
extra worker. Polyphony retains canonical tasks, requirements, final artifacts
and review. OpenJules records mechanical composition and delivery facts. The
repository's usual CI, reviewers and merge policy determine what happens after
draft publication.

## Prerequisites and supported inputs

- Run the command in the trusted OpenJules host environment, with Python 3.11 or
  newer, Linux file locking and Git available.
- Use a final managed root candidate, complete final child captures and an
  authenticated authority credential bound to that runtime/repository scope.
- The approved source must be a complete local Git checkout or bare repository
  containing the exact approved base. Git worktrees whose `.git` is a pointer
  file are not supported by the sanitized source exporter.
- Select the explicit GitHub repository identity, numeric repository ID, target
  branch, feature namespace and host credential reference. The source base must
  still be the target branch head before initial publication.
- Provide a private state directory. Preserve it across retries and restarts.
  Its files contain captured artifacts and operational facts, so do not publish
  or upload the directory wholesale.

The initial composition mode supports textual file additions, changes and
deletions with regular modes `100644` and `100755`. It rejects symlinks,
gitlinks/submodules, rename/copy patches, Git binary patches, unsafe paths and
unsupported encodings or modes. These are explicit limits, not a claim that
unsupported contributions are invalid. Preserve those artifacts for a supported
correction or later composition mode.

Every final child must appear exactly once in the requested order. With no
`--child` options, the order is canonical child-ID order. Identical retry input
retains that order; a later request cannot silently replace it. Empty child
patches can represent discussion-only contributions, but the complete resulting
root tree must contain a change.

Root edits outside the supplied final child patches are not included implicitly.
If the final root tree differs, the result is `candidate_tree_mismatch`. The
operation retains all inputs and publishes nothing. It never overwrites a final
canonical capture or creates a second candidate against a terminated attempt.

## Explicit configuration

Create a private JSON configuration using real approved values:

```json
{
  "schema_version": 1,
  "state_directory": "/absolute/private/openjules-publication",
  "source_path": "/absolute/checkouts/enrolled-repository",
  "base_commit": "REPLACE_WITH_FULL_APPROVED_BASE_SHA",
  "authority": {
    "endpoint": "http://127.0.0.1:8765",
    "token_ref": "env:POLYPHONY_PUBLICATION_READ_TOKEN"
  },
  "forge": {
    "deployment_id": "my-deployment",
    "repository": "github:github.com/owner/project",
    "repository_id": 123456789,
    "target_branch": "main",
    "branch_prefix": "openjules/managed/",
    "token_ref": "env:GITHUB_PUBLICATION_TOKEN",
    "author_name": "OpenJules",
    "author_email": "operator-approved-author@example.invalid"
  },
  "operation_seconds": 300
}
```

Credential values come from the host's secret mechanism. References are explicit
and distinct; there is no default `GITHUB_TOKEN` fallback. Forge credentials are
sent only in the host HTTP authorization header to `https://api.github.com`.
They do not enter Git URLs, Git subprocess environments, argv, guest containers,
manifests or generated PR bodies. Redirects, inherited proxy configuration and
compressed response bodies are rejected. Raw remote error bodies are not logged.

The GitHub credential needs access to that selected repository for Git object
and feature-reference creation plus draft PR creation. Repository protections
continue to apply. A broad token does not broaden the configured destination.
The numeric repository ID is checked before publication to avoid adopting a
different repository under a reused name.

The authority endpoint accepts HTTPS, or loopback HTTP for the supported local
installation. The authority credential may read the selected canonical root and
its private child artifacts. The command exposes no authority mutation API.

```sh
python -m src.worker.managed_publication \
  --config /absolute/private/publication.json \
  --candidate FULL_CANONICAL_CANDIDATE_SHA256 \
  --child FIRST_CANONICAL_CHILD_ID \
  --child SECOND_CANONICAL_CHILD_ID
```

The command returns JSON. A draft result includes the operation ID, canonical
candidate ID, provenance manifest SHA-256, exact Git tree and commit SHA, and the
actual observed PR number/URL/head/base. These identifiers are different and
cannot substitute for one another.

The summary's `ready_for_review: false` and `release_authorized: false` mean this
component grants neither permission. The nested PR record reports its actual
forge state, including a later human promotion or closure.

## Mechanical and credential boundaries

The sanitized exporter reads only the approved commit's tree/blob closure. The
compositor uses two independent private Git indexes. One receives the ordered
final child patches; the other receives the captured root patch. Exact tree
equality is required before a commit or branch can be published.

There is no repository checkout or repository command execution on the host.
Git uses fixed argv, scrubbed environment, disabled hooks, external configuration,
replacement objects and filesystem monitors. Repository tests, setup commands,
filters and model code are never run by this command.

The commit has one parent, the approved base. Its metadata uses the configured
identity and the canonical root's start timestamp rounded down to seconds. This
choice makes the same immutable inputs reproducible across independent journal
recovery. It describes the work's provenance, not an independent human author or
test result. The message records the manifest and root attempt.

Only changed blobs are uploaded. The resulting complete Git tree and commit are
checked against locally calculated Git object IDs before branch creation.
Feature branches are created once: an absent branch may be created, an identical
existing SHA reconciles, and a different SHA conflicts. This component never
updates or force-pushes a reference, and cannot target the protected base branch.

## Budgets

Defaults bound each artifact and blob to 4 MB, all retrieved artifact text to
32 MB, the approved source object closure to 16 MB, the final tree to 10,000 files,
the lineage to 64 children, paths to 1024 UTF-8 bytes and all child-plus-root
patches to 20,000 file records in aggregate. The source export and both private
repositories together are limited to 100,000 Git objects and 64 MB of uncompressed
object content. Conservative object allowances are checked before patch, tree
and commit writes, then verified against the actual stored objects. Git stdout
is bounded to 16 MB per command; diagnostics have a separate 64 KB bound. The Git
stage has a 60-second deadline and is also clamped to the invocation's remaining
deadline. Git binary deltas are rejected before application to avoid unbounded
expansion. These object limits are not a general host filesystem quota.

The optional `limits` object accepts the named fields from `CompositionLimits`;
all have enforced finite upper bounds. The journal has a 70 MB serialized limit,
checked before any file replacement or external publication effect. Text that
fits the raw artifact limit can still exceed this encoded limit through escaping;
it is rejected with `journal_byte_limit` before creating an unreadable checkpoint.
Each HTTP request has bounded bytes and an enclosing timeout. The complete
operator invocation defaults to 300 seconds and cannot exceed 600 seconds.
Mechanical work, observation and retries make zero model calls.

## Recovery and observed results

One host process owns the private journal through a nonblocking OS file lock.
Before each external effect, its intent and exact immutable inputs are flushed
to durable storage. Interrupted composition can rebuild private indexes from the
retained final artifacts. It cannot choose another candidate, child order, policy
or commit on an existing operation.

| Status | Meaning |
| --- | --- |
| `draft_published` | The actual matching draft PR was observed. CI and independent review are still external requirements. |
| `existing_ready` | The same PR has already been promoted by another actor. This command did not promote or validate it. |
| `existing_closed` | The same PR is closed. It is retained as that result and is not reopened or replaced. |
| `draft_stale` | The draft exists, but canonical requirements/candidate or forge base/head changed during or after publication. Fresh work/evidence is required. |
| `draft_pending_reconciliation` | The draft exists, but current authority or forge state could not be verified. |
| `unavailable` | Required credentials, authority, transport or resources are unavailable. A completed mechanical result remains retained. |
| `conflict` | Input, tree, branch, scope, policy or artifact validation failed. No partial branch is published by composition. Earlier observed effects remain retained. |
| `uncertain` | An external effect may have happened. The operation retains its intent and only performs permitted reconciliation. |

Git objects are content addressed and can be uploaded again with exactly the same
bytes. An uncertain branch create can reconcile its exact reference, without
overwriting another SHA. For a possibly dispatched PR create, all-state PR reads
look for the exact repository, branch, commit, base branch and provenance marker.
A matching closed PR is still the original operation.

**An absent PR after an ambiguous create is not proof of noncreation.** The
command does not resend that POST. This also covers a process dying immediately
after saving intent but before sending. The result stays uncertain until the
original proposal is found or an operator investigates the forge and journal.
The initial version provides no automatic reset, delete or replacement operation.

Canonical currentness and the expected base are checked immediately before
branch and PR effects, and again after observing the PR. A GitHub request cannot
be atomic with a separate Polyphony transaction. The post-effect check detects
changes; it cannot prevent a stale draft from being created during that race.
Such a draft is retained and reported as stale, with no readiness or merge claim.

## Ordinary review and completion evidence

The draft is the review vehicle. Run the repository's configured combined checks
in its approved CI isolation and obtain independent review against the actual
head and base before promotion or merge. Changed head/base needs fresh applicable
evidence. Canonical candidate IDs, artifact hashes and internal receipts are not
forge workflow IDs or GitHub review evidence.

The component tests use real Git and a deterministic localhost HTTP fixture for
the authority and GitHub API contract. They test final lineage, tree equality,
hashes, scope, bounds, concurrency, lost replies and draft-only behavior. They
do not create a live GitHub PR or qualify arbitrary model output. Issue #6 remains
open until the integrated parent-plus-two-children workflow reaches an actual
PR, real combined checks and independent review.
