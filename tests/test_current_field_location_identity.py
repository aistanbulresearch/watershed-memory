"""A trusted site version has one meaning across plans and later configuration."""

from dataclasses import replace

import pytest
from test_current_field_store import CASE, COORDINATOR, NOW, SITE, SPEC, contents, mutate, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_types import DecideFieldPlan, ModifyFieldPlan, ProposeFieldPlan
from watershed_memory.current.locations import LocationRegistry


@pytest.mark.parametrize("operation", ["APPROVE", "MODIFY", "PROPOSE"])
def test_mutation_rejects_changed_content_under_the_same_saved_site_version(field, operation):
    plan = proposed(field).plan
    if operation == "PROPOSE":
        mutate(field, "decide_plan", DecideFieldPlan(plan.plan_id, plan.revision, "CANCEL"))
    replacement = FieldStore(
        field[1], LocationRegistry((replace(SITE, label="Changed site identity"),))
    )
    before = contents(field[0])
    if operation == "APPROVE":
        method = "decide_plan"
        command = DecideFieldPlan(plan.plan_id, plan.revision, "APPROVE")
    elif operation == "MODIFY":
        method = "modify_plan"
        command = ModifyFieldPlan(plan.plan_id, plan.revision, SITE.location_id, 1, SPEC, True)
    else:
        method = "propose_plan"
        command = ProposeFieldPlan(
            field[3].task_id, field[3].revision, SITE.location_id, 1, SPEC, True
        )
    with pytest.raises(ValueError):
        getattr(replacement, method)(
            CASE,
            command,
            principal=COORDINATOR,
            request_id="changed-site-version",
            expected_case_revision=field[1].context(CASE)["revision"],
            now=NOW,
        )
    assert contents(field[0]) == before


def test_a_new_explicit_site_revision_can_be_selected_by_human_modification(field):
    plan = proposed(field).plan
    updated = replace(SITE, revision=2, label="Revised approved inspection site")
    replacement = FieldStore(field[1], LocationRegistry((SITE, updated)))
    receipt = replacement.modify_plan(
        CASE,
        ModifyFieldPlan(plan.plan_id, plan.revision, SITE.location_id, 2, SPEC, True),
        principal=COORDINATOR,
        request_id="new-site-version",
        expected_case_revision=field[1].context(CASE)["revision"],
        now=NOW,
    )
    assert receipt.plan.location == updated
    assert field[2].get_plan(CASE, plan.plan_id, revision=1).location == SITE
