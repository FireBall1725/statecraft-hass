"""Compile structured source rows into native HA condition configs.

The config flow's builder mode collects a list of simple "source" rows plus a
combine mode (and/or). This module turns those into the same condition dict the
YAML mode produces, so the engine never knows which authoring path was used.

Pure functions, no Home Assistant imports, so the compilation is unit testable.
"""

from __future__ import annotations

from typing import Any

# Source row keys (kept flat to map cleanly onto a config-flow form).
SRC_ENTITY = "entity_id"
SRC_KIND = "kind"  # "state" | "numeric"
SRC_STATES = "states"  # list[str], for kind == state
SRC_NEGATE = "negate"  # bool, for kind == state
SRC_ABOVE = "above"  # float | None, for kind == numeric
SRC_BELOW = "below"  # float | None, for kind == numeric
SRC_FOR = "for_seconds"  # int | None

KIND_STATE = "state"
KIND_NUMERIC = "numeric"

COMBINE_ANY = "or"
COMBINE_ALL = "and"


def _fmt_for(seconds: Any) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def compile_source(src: dict[str, Any]) -> dict[str, Any]:
    """Compile one source row into a condition dict."""
    entity_id = src[SRC_ENTITY]
    kind = src.get(SRC_KIND, KIND_STATE)
    for_seconds = src.get(SRC_FOR)

    if kind == KIND_NUMERIC:
        cond: dict[str, Any] = {"condition": "numeric_state", "entity_id": entity_id}
        if src.get(SRC_ABOVE) is not None:
            cond["above"] = src[SRC_ABOVE]
        if src.get(SRC_BELOW) is not None:
            cond["below"] = src[SRC_BELOW]
        if for_seconds:
            cond["for"] = _fmt_for(for_seconds)
        return cond

    states = src.get(SRC_STATES) or []
    if isinstance(states, str):
        states = [states]
    cond = {"condition": "state", "entity_id": entity_id, "state": list(states)}
    if for_seconds:
        cond["for"] = _fmt_for(for_seconds)
    if src.get(SRC_NEGATE):
        cond = {"condition": "not", "conditions": [cond]}
    return cond


def compile_condition(
    combine: str, sources: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Compile source rows + combine mode into a single condition dict.

    A single source needs no wrapper; multiple are wrapped in an and/or block.
    """
    compiled = [compile_source(s) for s in sources]
    if not compiled:
        return None
    if len(compiled) == 1:
        return compiled[0]
    return {"condition": combine, "conditions": compiled}


def validate_source(src: dict[str, Any]) -> str | None:
    """Return an error key if a source row is incomplete, else None."""
    if not src.get(SRC_ENTITY):
        return "source_entity"
    kind = src.get(SRC_KIND, KIND_STATE)
    if kind == KIND_STATE and not src.get(SRC_STATES):
        return "source_states"
    if kind == KIND_NUMERIC and src.get(SRC_ABOVE) is None and src.get(SRC_BELOW) is None:
        return "source_bounds"
    return None


def source_label(src: dict[str, Any]) -> str:
    """Human-readable one-line summary for menus."""
    entity_id = src.get(SRC_ENTITY, "?")
    kind = src.get(SRC_KIND, KIND_STATE)
    if kind == KIND_NUMERIC:
        bits = []
        if src.get(SRC_ABOVE) is not None:
            bits.append(f">{src[SRC_ABOVE]}")
        if src.get(SRC_BELOW) is not None:
            bits.append(f"<{src[SRC_BELOW]}")
        detail = " ".join(bits) or "numeric"
    else:
        states = src.get(SRC_STATES) or []
        detail = ("not " if src.get(SRC_NEGATE) else "") + "/".join(states)
    if src.get(SRC_FOR):
        detail += f" for {int(src[SRC_FOR])}s"
    return f"{entity_id}: {detail}"
