# Bounded child agents in a managed worker

An explicitly approved model profile can run one level of child coding agents
inside its existing worker container. The root asks for a canonical child grant,
the trusted host admits that exact child, and the child edits its own checkout.
The root can read provisional child patches and logs and compose the result in
its own checkout. Polyphony remains the authority for requirements, grants,
budgets, artifacts and review. ZPM still counts one worker for the entire group.

This is opt-in. Existing command and root model profiles keep their old
configuration and fingerprints. Enabling children or changing their policy
requires a new approved profile name and worker incarnation. The service does
not convert an already running root-only container into a child-enabled worker.

## Configuration and approval

The managed service accepts an optional `children` list. Every entry must match
an existing runtime profile and a `models` entry by the exact deployment,
repository and profile name. For example:

```json
{
  "children": [
    {
      "deployment_id": "my-installation",
      "repository": "github:github.com/example/project",
      "profile": "coding-with-children-v1",
      "policy": {
        "max_children": 2,
        "max_child_seconds": 60,
        "max_child_turns": 5,
        "capabilities": ["python"],
        "uid_start": 60000,
        "command": ["python3", "-I", "-S", "/opt/openjules/managed_runner.py"],
        "verify_argv": ["python3", "-m", "unittest", "discover"],
        "max_result_bytes": 4000000
      }
    }
  ]
}
```

The corresponding Polyphony profile must explicitly approve `max_children`,
`max_child_seconds`, `max_child_turns` and `child_capabilities`. The capability
list and all three limits must match the runtime policy. Canonical preflight
must advertise `child_grants: true` and that exact policy before the runtime
advertises `children: true`. A missing supervisor, unavailable socket transport,
unsupported Docker UID mapping or unavailable canonical authority keeps it
unready.

`max_children` is the total number of grants during the root attempt, from 1 to
64. A finished child does not free its grant slot or UID for reuse.
`max_child_seconds` must fit the root runtime and canonical attempt limits.
`max_child_turns` is from 1 to 1000. Child instructions are bounded to 64 KiB.
The host adds the child socket and granted turn limit to the approved command;
the guest cannot select a command, image, host source path, UID or provider.

Use the trusted root wrapper for the profile's root command:

```json
[
  "python3", "-I", "-S", "/opt/openjules/managed_root_runner.py",
  "--socket", "/run/openjules-model/model.sock",
  "--control-socket", "/run/openjules-model/control.sock",
  "--max-turns", "20"
]
```

The image must contain that wrapper, `child_tools.py`, and `managed_runner.py` at
their approved image paths. `docker/Dockerfile.managed-agent` includes all three.
The root wrapper imports its companions from the trusted image, including when
run with `python3 -I -S`; task checkout modules cannot replace them. Image IDs or
digests, source commits and root command argv retain the existing immutable
profile rules.

Capability names describe an operator-approved runner bundle. The first runtime
supports exactly the configured child bundle and refuses requests for a
different subset. Removing a capability label does not sandbox arbitrary
Python or shell code. Use a separately approved profile when the executable
environment needs a different boundary.

## Shared limits and process identities

Child grants use the root task's existing canonical time budget. Each admitted
child allocation is charged once, with no refund or second task budget. The task
budget must cover the root allocation and all requested child allocations.
Each child deadline is at or before the root deadline, and retries never extend
the original local cutoff.

All root and child model calls share the same durable root call and output-token
allowance. A child also consumes its canonical per-child turn allowance before
the provider request. A lost turn acknowledgement retries the same canonical
turn key without charging another model reservation. A live in-flight request
or recoverable canonical turn returns a bounded retry response to other callers.
An uncertain provider dispatch blocks subsequent inference for the whole root;
it is never automatically sent to the provider again.

The existing root UID is distinct from the protected watchdog UID 65534. Every
child gets a unique nonzero UID from the approved range. No UID is recycled in
that worker. The default range starts at 60000. Overlap with the root or
watchdog is rejected. The baseline requires local rootful Docker without
user-namespace remapping; other UID mappings need separate qualification.

Each child checkout, home and temporary directory is private to its UID. The
shared parent directory is owned by the watchdog with mode 0711, so the root and
children cannot rename another child's directory. Each child has a separate
read-only mounted model socket, with its identity fixed by the host and checked
through Linux `SO_PEERCRED`. The root-only control socket uses the same kernel
check. Supplying another identity in JSON cannot change the caller's authority.
Children receive the ordinary coding tools and cannot request more children.

All descendants share the existing container CPU, memory and process limits.
They receive no provider, authority, worker API or Docker credentials. Container
network access stays disabled. Independent workers and ordinary Polyphony peers
retain their own lifecycle and are outside this child's cancellation scope.

## Root controls and provisional work

The root model receives five additional tools:

| Tool | Behavior |
| --- | --- |
| `spawn_child` | Requests bounded instructions, seconds, turns and the approved capability bundle. The host persists a delivery key before contacting Polyphony. |
| `child_status` | Observes the existing child; it does not allocate or launch again. |
| `child_result` | Reads a bounded patch or log page after the child exec returns. The response is explicitly `final: false` and carries whole-artifact and page hashes. |
| `cancel_child` | Revokes the child and requests retirement of its entire worker. |
| `close_children` | Closes admission for new children. Existing children remain bounded by their original grants. |

The root wrapper also closes child admission when its agent loop exits or fails.
Control requests freeze their bytes and request identity before the first send.
Only explicit `retry_later` or `canonical_pending` replies retry within that
original request's timeout. A transport loss or conflicting retry stops the
guest root so host recovery can reconcile the original intent.

Child results are provisional guest-generated evidence. The host freezes the
first returned version of each artifact so paging and repeated reads have
stable hashes. The root must deliberately inspect and apply a patch in its own
checkout. Reading a child patch does not prove it was composed, tested,
independently reviewed or accepted.

## Deadlines, cancellation and final capture

**A child deadline or cancellation retires the entire worker, including the root
and its sibling children.** This is a deliberate conservative boundary for the
first version. With all Linux capabilities dropped, the watchdog cannot safely
signal arbitrary task UIDs individually. Instead it remains container PID1 and
exits at the earliest armed child or root deadline. Container termination fences
all descendants, including a process that outlived its Docker exec.

An exec return is never a final termination receipt. Child deadlines stay armed
after an exec returns. No live UID scan or process exit is used to release child
or root reservations. A lost exec-create or exec-start reply retains the original
identity and causes whole-worker retirement instead of another launch.

Local drain closes admission immediately. The supervisor delivers that closure
to Polyphony before replaying any unresolved grant request, preventing an old
undelivered request from allocating new work after drain. Owner cancellation or
canonical revocation also denies new inference. An explicit canonical
`child_not_allocated` response is the only grant rejection that can clear an
unallocated delivery intent.

After the container is fenced, OpenJules verifies the durable whole-workspace
archive hash and selects each child's bounded regular patch and log files from
it. The child manifest binds the root attempt, canonical grant digest, worker,
UID, original exec identity, deadlines, archive hash and root capture manifest.
Untracked or interrupted work remains in the raw archive outside the container.
Missing or symlinked required capture evidence, a changed archive or an unknown
canonical child keeps the reservation unresolved.

The supervisor recovers every persisted grant intent and compares the exact
canonical child set with its local records. It then uploads each child's patch,
log and manifest, verifies the content hashes and binding, and obtains a complete
child terminal receipt. Only after all admitted children pass that barrier can
the dispatcher publish root capture, record root completion and release local
execution references. A child never creates a separate candidate or acceptance
decision. The root candidate still requires the existing independent review.

An interrupted child may have no generated patch or log despite preserved raw
workspace data. Its manifest explicitly records which files exist and whether
exec-start was dispatched. Empty generated artifacts do not assert that the
child produced no work or that any work was verified.

## Qualification

The regular suite covers exact grant and turn retries, immutable policy,
shortened permission races, shared model allowance, lost Docker replies,
provisional output, archive integrity and the final publication barrier.
Real Unix-socket tests are mandatory and do not silently skip when sockets are
blocked by the test environment:

```sh
python -m pytest tests/unit/test_child_socket.py tests/unit/test_child_tools.py
```

The Docker qualification runs two children under distinct UIDs, composes their
actual returned patches in the root checkout, verifies credential and socket
boundaries, tests a lost exec-start reply, and checks watchdog enforcement and
journal recovery after host listeners stop:

```sh
OPENJULES_MANAGED_DOCKER_TESTS=1 \
  python -m pytest tests/integration/test_managed_children_runtime.py
```

The direct sibling regression runs two actual child agents, then probes their
private checkouts, instructions, generated patches, logs and production model
sockets in both directions. Each probe uses the child's recorded UID and first
proves access to its own files and socket. It requires permission denial for
foreign reads, writes, mode changes and UID changes, plus an authenticated
socket refusal. The root UID is also denied direct access to both children's
files and model sockets; its approved result tools remain the way to obtain
child contributions. These probes add no inference calls or turn reservations.

The probes run after child exec return while the root container, child listeners
and root/child execution references remain live. Final capture follows an
explicit whole-worker retirement. This checks the documented UID/filesystem and
socket boundaries inside one shared container. It does not establish separate
kernels, process namespaces, resistance to kernel exploits or a multi-tenant
sandbox. Actual Docker execution of this regression is a CI qualification;
collection or a local skipped test does not establish those outcomes.

These OpenJules tests use explicitly named canonical-authority and model
fixtures. They do not establish real Polyphony service interoperability or live
model quality. The installed three-service trial must additionally exercise the
actual Polyphony authority and ZPM controller with exact source pins.
