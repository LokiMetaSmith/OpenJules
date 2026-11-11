"""Host-derived initial instruction identity, separate from inference turns."""

from .child_types import digest


def work_outcome(binding, report):
    return {
        "schema_version": 1,
        "work_turn_id": digest(
            ["polyphony-child-work-v1", binding["child_id"], binding["grant_digest"], 1]
        ),
        "ordinal": 1,
        **{
            name: binding[name]
            for name in (
                "child_id",
                "root_attempt_id",
                "parent_attempt_id",
                "task_id",
                "requirements_revision",
                "grant_digest",
                "instructions_digest",
            )
        },
        "source": "unreported" if report is None else "child_report_via_bound_runtime",
        "report": report,
    }
