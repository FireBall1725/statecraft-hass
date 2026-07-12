"""The config-flow only offers rows it can edit without data loss."""

from __future__ import annotations

from custom_components.statecraft import condition_builder as cb
from custom_components.statecraft.config_flow import _flow_editable


def test_plain_state_and_numeric_rows_are_editable():
    assert _flow_editable({cb.SRC_KIND: cb.KIND_STATE, cb.SRC_ENTITY: "a.a"}) is True
    assert _flow_editable({cb.SRC_KIND: cb.KIND_NUMERIC, cb.SRC_ENTITY: "a.a"}) is True


def test_time_and_group_rows_are_not_editable_in_the_flow():
    assert _flow_editable({cb.SRC_KIND: cb.KIND_TIME, cb.SRC_AFTER: "22:00"}) is False
    assert _flow_editable({cb.SRC_KIND: cb.KIND_GROUP, cb.G_SOURCES: []}) is False


def test_attribute_rows_are_not_editable_in_the_flow():
    # An attribute-match row can't be expressed by the flow form, so editing it
    # there would drop the attribute — it stays panel-only.
    row = {
        cb.SRC_KIND: cb.KIND_STATE,
        cb.SRC_ENTITY: "a.a",
        cb.SRC_ATTRIBUTE: "battery",
    }
    assert _flow_editable(row) is False
