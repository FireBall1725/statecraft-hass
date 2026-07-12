"""Builder tree <-> native HA condition, round trips and validation."""

from __future__ import annotations

from custom_components.statecraft import condition_builder as cb


def test_compile_state_row():
    node = {
        cb.SRC_KIND: cb.KIND_STATE,
        cb.SRC_ENTITY: "binary_sensor.door",
        cb.SRC_STATES: ["off"],
    }
    assert cb.compile_node(node) == {
        "condition": "state",
        "entity_id": "binary_sensor.door",
        "state": ["off"],
    }


def test_compile_negated_state_wraps_in_not():
    node = {
        cb.SRC_KIND: cb.KIND_STATE,
        cb.SRC_ENTITY: "person.a",
        cb.SRC_STATES: ["home"],
        cb.SRC_NEGATE: True,
    }
    out = cb.compile_node(node)
    assert out["condition"] == "not"
    assert out["conditions"][0]["entity_id"] == "person.a"


def test_compile_time_row_and_for_duration():
    time_cond = cb.compile_node({cb.SRC_KIND: cb.KIND_TIME, cb.SRC_AFTER: "22:00"})
    assert time_cond == {"condition": "time", "after": "22:00:00"}
    dur = cb.compile_node(
        {
            cb.SRC_KIND: cb.KIND_STATE,
            cb.SRC_ENTITY: "x.y",
            cb.SRC_STATES: ["on"],
            cb.SRC_FOR: 90,
        }
    )
    assert dur["for"] == "00:01:30"


def test_state_row_round_trips():
    node = {
        cb.SRC_KIND: cb.KIND_STATE,
        cb.SRC_ENTITY: "binary_sensor.door",
        cb.SRC_STATES: ["off"],
    }
    compiled = cb.compile_node(node)
    back = cb.decompile_node(compiled)
    assert back[cb.SRC_ENTITY] == "binary_sensor.door"
    assert back[cb.SRC_STATES] == ["off"]
    assert back[cb.SRC_KIND] == cb.KIND_STATE


def test_group_round_trips():
    root = cb.compile_condition(
        cb.COMBINE_ALL,
        [
            {cb.SRC_KIND: cb.KIND_STATE, cb.SRC_ENTITY: "a.a", cb.SRC_STATES: ["on"]},
            {cb.SRC_KIND: cb.KIND_STATE, cb.SRC_ENTITY: "b.b", cb.SRC_STATES: ["on"]},
        ],
    )
    assert root["condition"] == "and"
    builder = cb.decompile_condition(root)
    assert builder[cb.G_COMBINE] == "and"
    assert len(builder[cb.G_SOURCES]) == 2


def test_template_condition_is_not_representable():
    assert (
        cb.decompile_node({"condition": "template", "value_template": "{{ true }}"})
        is None
    )


def test_validate_source_error_keys():
    assert cb.validate_source({cb.SRC_KIND: cb.KIND_STATE}) == "source_entity"
    assert (
        cb.validate_source({cb.SRC_KIND: cb.KIND_STATE, cb.SRC_ENTITY: "a.a"})
        == "source_states"
    )
    assert cb.validate_source({cb.SRC_KIND: cb.KIND_TIME}) == "source_time"
    assert (
        cb.validate_source(
            {cb.SRC_KIND: cb.KIND_STATE, cb.SRC_ENTITY: "a.a", cb.SRC_STATES: ["on"]}
        )
        is None
    )
