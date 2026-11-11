# Managed OpenJules service

The managed service connects ZPM worker capacity to Keystone Polyphony's
authenticated canonical task authority. It runs on a single trusted Linux host
with local Docker and persistent storage. It has its own entry point and never
falls back to the legacy in-memory Polyphony client or background executor.

```sh
python -m src.worker.managed_service --config /private/installation/config/openjules.json
```

Use ZPM's single-host installation package to generate the matching authority,
runtime and controller configuration. Run each application from its own checkout
and Python environment because OpenJules and Polyphony both use a `src` package.
The service listens only on loopback. Remote access requires a deliberately
configured HTTPS reverse proxy and the existing scoped bearer authentication.

## Ownership

ZPM owns repository enrollment and the desired number of workers. OpenJules owns
the container lifecycle and the delivery of canonical work to an admitted
worker. Polyphony owns task requirements, budgets, claims, artifacts, review and
acceptance. The dispatcher journal contains delivery intents and receipts; it is
not a second task ledger.

Each worker incarnation executes at most one root task. The whole container is
then stopped and its workspace captured before capacity can be released. ZPM can
replace that retired incarnation while preserving the operator's selected target.
An independently bounded watchdog inside the container limits execution even
when the trusted service dies. See [the runtime contract](managed-docker-runtime.md)
for confinement, source staging, capture and unresolved launch behavior.

## Configuration

The version 1 service object requires `state_directory`, `lifecycle_database`,
`credentials_file`, `authority` and `profiles`. Paths are absolute local paths.
The authority object contains an HTTPS or loopback HTTP service root and an
environment-variable `token_ref`. Profiles are operator-approved immutable
Docker configurations, including an exact source commit and command argv.

The private credential file has this shape; replace the placeholder through the
installation tool or another secret-provisioning mechanism:

```json
{
  "DEDICATED_RANDOM_ZPM_CREDENTIAL_AT_LEAST_32_CHARACTERS": [
    {"deployment_id": "my-installation", "repository": "github:github.com/example/project"}
  ]
}
```

The credential file must have mode `0600`. It authorizes the exact deployment
and repository pair. The Polyphony runtime credential is separate and never
enters a task container. Provider error bodies, credentials and repository
output are not copied into dispatcher diagnostics.

Optional configuration includes `host` (loopback only), `port` (default 8000),
`poll_interval_seconds` (default 1) and `max_parallel` (default 16). The last value
bounds concurrent dispatcher threads, including active commands; size it for the
approved host capacity. Each task also has the smaller of its canonical attempt
allocation and approved runtime limit. A fixed monotonic cutoff covers network,
admission and runtime preparation time; retries cannot extend it.

## Recovery

Optional `models` entries enable the [managed coding agent](managed-model-broker.md)
for explicitly matched profiles. The service keeps provider credentials on the
host and adds only that worker's authenticated read-only socket mount. Model
requests require the original admitted canonical binding and its current
permission. Profiles without this configuration remain command-only.

Optional `children` entries enable [bounded child agents](managed-children.md)
for approved model profiles. They require a matching canonical child policy and
a new immutable runtime profile. The root and children share one worker and one
model allowance. Final child capture and terminal receipts precede root release.

The dispatcher persists an immutable claim request before contacting Polyphony.
A lost reply retains that same request identity. A generic conflict or transport
failure is not evidence that no attempt was allocated. Only the authority's
explicit `claim_not_allocated` response permits choosing a different task.
Duplicate claims recover their original binding even if the approved profile
has changed or been removed; revoked permission permits cleanup, never a new
launch.

Canonical authorization and local lifecycle admission are both required before
repository code runs. Docker receives the original immutable execution identity.
Its durable intent and tombstones prevent replay after a lost response. Unknown
execution, uncertain Docker start and failed capture retain reservations.

A drain or stop response can acknowledge durable intent while another runtime
operation still holds the worker lock. Local admission closes immediately. A
`draining` snapshot does not by itself prove that the runtime or canonical child
authority received the closure. The snapshot reason identifies control intents
that still await runtime confirmation.

The trusted dispatcher reconciles at most one incomplete control per worker in
each pass, reusing the original scoped request key and immutable payload. It
does this even when all execution slots are occupied. An explicit stop takes
priority over pending drains. Confirmed terminal evidence completes an intent
without signaling the runtime again; uncertain delivery remains durable across
restart. Drain preserves existing root and child grants while closing new
admission. Ordinary observation and discovery remain read-only.

After termination, the dispatcher delivers immutable patch, log and manifest
content to Polyphony and verifies the returned content hashes. Only a complete
terminal receipt allows the local execution reference to clear. A successful
command with a nonempty patch may create a candidate for independent review;
that action does not accept the task or authorize a GitHub merge. Stale work is
preserved even when a requirements revision prevents a current candidate.

Confirmed dead idle containers are retired and captured by a separate trusted
daemon recovery operation. Ordinary observation remains read-only. Missing
containers, inaccessible Docker and unconfirmed start intents remain unknown.

## Current supported scope

This release provides an authenticated, recoverable **root execution** path,
with an optional host-owned model broker and opt-in depth-one child execution.
Children use distinct UIDs and canonical grants; a child deadline or cancellation
retires its whole worker. Existing root-only profiles remain unchanged.
The Docker network remains disabled; model traffic goes through the configured
broker. Deterministic model and code-change trials prove transport, execution
and capture, including the separately enabled child Docker qualification. They
do not prove live-provider behavior, model quality, independent review or forge
publication.

Durable archives, stopped containers and volumes are retained for recovery.
There is no automatic garbage collection or per-volume disk quota. The trusted
host's storage capacity remains an operational limit. Do not delete recovery
evidence to make an unknown reservation appear terminated.
