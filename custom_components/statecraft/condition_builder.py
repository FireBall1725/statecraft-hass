"""Compile a builder tree into a native HA condition, and back again.

The panel edits a small tree of "nodes". Each node is one of:

  state    {kind:"state",   entity_id, attribute?, states[], negate, for_seconds?}
  numeric  {kind:"numeric", entity_id, attribute?, above?, below?, for_seconds?}
  time     {kind:"time",    after?, before?, weekday?[]}
  group    {kind:"group",   combine:"and"|"or", negate?, sources:[node]}

`compile_node` turns a node into the same native Home Assistant condition dict
the YAML mode produces, so the engine never knows which authoring path was used.
`decompile_condition` does the reverse for a representable HA condition, so the
panel can switch YAML -> Builder and carry the config over. Conditions the
builder can't represent (template, sun, zone, device, trigger, ...) decompile to
None, and the panel keeps them in YAML mode.

Pure functions, no Home Assistant imports, so it stays unit testable.
"""

from __future__ import annotations

from typing import Any

# --- node keys --------------------------------------------------------------
SRC_ENTITY = "entity_id"
SRC_KIND = "kind"  # state | numeric | time | group
SRC_ATTRIBUTE = "attribute"  # optional: match an attribute instead of the state
SRC_STATES = "states"  # list[str], state kind
SRC_NEGATE = "negate"  # bool, state kind
SRC_ABOVE = "above"  # float | None, numeric kind
SRC_BELOW = "below"  # float | None, numeric kind
SRC_FOR = "for_seconds"  # int | None, state/numeric kind
SRC_AFTER = "after"  # "HH:MM" | entity_id, time kind
SRC_BEFORE = "before"  # "HH:MM" | entity_id, time kind
SRC_WEEKDAY = "weekday"  # list[str], time kind

# group keys
G_COMBINE = "combine"
G_NEGATE = "negate"
G_SOURCES = "sources"

KIND_STATE = "state"
KIND_NUMERIC = "numeric"
KIND_TIME = "time"
KIND_GROUP = "group"

COMBINE_ANY = "or"
COMBINE_ALL = "and"


# --- helpers ----------------------------------------------------------------
def _fmt_for(seconds: Any) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _for_seconds(value: Any) -> int | None:
    """Parse an HA `for:` back into whole seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        return int(
            float(value.get("hours", 0)) * 3600
            + float(value.get("minutes", 0)) * 60
            + float(value.get("seconds", 0))
        )
    if isinstance(value, str):
        parts = value.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        while len(nums) < 3:
            nums.insert(0, 0.0)
        return int(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return None


def _fmt_time(value: Any) -> Any:
    """Normalise a builder time ("21:00") to HA's "HH:MM:SS"; pass entities through."""
    if not isinstance(value, str) or "." in value:
        return value  # entity_id (input_datetime.*, sensor.*) or non-string
    parts = value.split(":")
    while len(parts) < 3:
        parts.append("00")
    return ":".join(p.zfill(2) for p in parts[:3])


def _short_time(value: Any) -> Any:
    """Trim HA's "21:00:00" to "21:00" for the builder; pass entities through."""
    if not isinstance(value, str) or "." in value:
        return value
    parts = value.split(":")
    if len(parts) < 2:
        return value
    hm = f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    if len(parts) >= 3 and parts[2] not in ("", "0", "00"):
        return f"{hm}:{parts[2].zfill(2)}"
    return hm


# --- compile: node -> HA condition -----------------------------------------
def compile_node(node: dict[str, Any]) -> dict[str, Any] | None:  # noqa: PLR0912
    """Compile one builder node into a native HA condition dict (or None)."""
    kind = node.get(SRC_KIND, KIND_STATE)

    if kind == KIND_GROUP:
        children = [compile_node(c) for c in node.get(G_SOURCES, [])]
        children = [c for c in children if c]
        if not children:
            return None
        inner = (
            children[0]
            if len(children) == 1
            else {"condition": node.get(G_COMBINE, COMBINE_ANY), "conditions": children}
        )
        if node.get(G_NEGATE):
            return {"condition": "not", "conditions": [inner]}
        return inner

    if kind == KIND_TIME:
        cond: dict[str, Any] = {"condition": "time"}
        if node.get(SRC_AFTER):
            cond["after"] = _fmt_time(node[SRC_AFTER])
        if node.get(SRC_BEFORE):
            cond["before"] = _fmt_time(node[SRC_BEFORE])
        if node.get(SRC_WEEKDAY):
            cond["weekday"] = node[SRC_WEEKDAY]
        return cond if len(cond) > 1 else None

    if kind == KIND_NUMERIC:
        cond = {"condition": "numeric_state", "entity_id": node[SRC_ENTITY]}
        if node.get(SRC_ATTRIBUTE):
            cond["attribute"] = node[SRC_ATTRIBUTE]
        if node.get(SRC_ABOVE) is not None:
            cond["above"] = node[SRC_ABOVE]
        if node.get(SRC_BELOW) is not None:
            cond["below"] = node[SRC_BELOW]
        if node.get(SRC_FOR):
            cond["for"] = _fmt_for(node[SRC_FOR])
        return cond

    # state
    states = node.get(SRC_STATES) or []
    if isinstance(states, str):
        states = [states]
    cond = {"condition": "state", "entity_id": node[SRC_ENTITY], "state": list(states)}
    if node.get(SRC_ATTRIBUTE):
        cond["attribute"] = node[SRC_ATTRIBUTE]
    if node.get(SRC_FOR):
        cond["for"] = _fmt_for(node[SRC_FOR])
    if node.get(SRC_NEGATE):
        cond = {"condition": "not", "conditions": [cond]}
    return cond


# back-compat alias (older callers said compile_source for a leaf row)
compile_source = compile_node


def compile_condition(
    combine: str, sources: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Compile the root builder (combine + sources) into one condition dict.

    A single source needs no wrapper; multiple are wrapped in an and/or block.
    """
    compiled = [compile_node(s) for s in sources]
    compiled = [c for c in compiled if c]
    if not compiled:
        return None
    if len(compiled) == 1:
        return compiled[0]
    return {"condition": combine, "conditions": compiled}


# --- decompile: HA condition -> builder tree -------------------------------
def _leaf_state(cfg: dict[str, Any]) -> dict[str, Any] | None:
    eid = cfg.get("entity_id")
    if isinstance(eid, list):
        if len(eid) != 1:
            return None  # multi-entity row isn't representable as one builder row
        eid = eid[0]
    states = cfg.get("state")
    states = states if isinstance(states, list) else [states]
    node: dict[str, Any] = {
        SRC_KIND: KIND_STATE,
        SRC_ENTITY: eid,
        SRC_STATES: [str(s) for s in states],
        SRC_NEGATE: False,
        SRC_ABOVE: None,
        SRC_BELOW: None,
        SRC_FOR: _for_seconds(cfg.get("for")),
    }
    if cfg.get("attribute"):
        node[SRC_ATTRIBUTE] = cfg["attribute"]
    return node


def _leaf_numeric(cfg: dict[str, Any]) -> dict[str, Any] | None:
    eid = cfg.get("entity_id")
    if isinstance(eid, list):
        if len(eid) != 1:
            return None
        eid = eid[0]
    node: dict[str, Any] = {
        SRC_KIND: KIND_NUMERIC,
        SRC_ENTITY: eid,
        SRC_STATES: [],
        SRC_NEGATE: False,
        SRC_ABOVE: cfg.get("above"),
        SRC_BELOW: cfg.get("below"),
        SRC_FOR: _for_seconds(cfg.get("for")),
    }
    if cfg.get("attribute"):
        node[SRC_ATTRIBUTE] = cfg["attribute"]
    return node


def _leaf_time(cfg: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {SRC_KIND: KIND_TIME}
    if cfg.get("after"):
        node[SRC_AFTER] = _short_time(cfg["after"])
    if cfg.get("before"):
        node[SRC_BEFORE] = _short_time(cfg["before"])
    wd = cfg.get("weekday")
    if wd:
        node[SRC_WEEKDAY] = wd if isinstance(wd, list) else [wd]
    return node


def decompile_node(cfg: Any) -> dict[str, Any] | None:  # noqa: PLR0911, PLR0912
    """Turn one HA condition into a builder node, or None if unrepresentable."""
    if not isinstance(cfg, dict):
        return None
    cond = cfg.get("condition")

    if cond in (COMBINE_ALL, COMBINE_ANY):
        sources = []
        for child in cfg.get("conditions", []):
            node = decompile_node(child)
            if node is None:
                return None
            sources.append(node)
        return {
            SRC_KIND: KIND_GROUP,
            G_COMBINE: cond,
            G_NEGATE: False,
            G_SOURCES: sources,
        }

    if cond == "not":
        inner = cfg.get("conditions", [])
        if len(inner) == 1:
            node = decompile_node(inner[0])
            if node is None:
                return None
            k = node.get(SRC_KIND, KIND_STATE)
            if k == KIND_STATE:
                node = dict(node)
                node[SRC_NEGATE] = not node.get(SRC_NEGATE, False)
                return node
            if k == KIND_GROUP:
                node = dict(node)
                node[G_NEGATE] = not node.get(G_NEGATE, False)
                return node
            return None  # can't negate a time/numeric row in the builder
        sources = []
        for child in inner:
            node = decompile_node(child)
            if node is None:
                return None
            sources.append(node)
        return {
            SRC_KIND: KIND_GROUP,
            G_COMBINE: COMBINE_ALL,
            G_NEGATE: True,
            G_SOURCES: sources,
        }

    if cond == "state":
        return _leaf_state(cfg)
    if cond == "numeric_state":
        return _leaf_numeric(cfg)
    if cond == "time":
        return _leaf_time(cfg)
    return None  # template / sun / zone / device / trigger / ... -> YAML only


def decompile_condition(cfg: Any) -> dict[str, Any] | None:
    """HA condition dict -> root builder {combine, sources}, or None."""
    if not isinstance(cfg, dict):
        return None
    if cfg.get("condition") in (COMBINE_ALL, COMBINE_ANY):
        sources = []
        for child in cfg.get("conditions", []):
            node = decompile_node(child)
            if node is None:
                return None
            sources.append(node)
        return {G_COMBINE: cfg["condition"], G_SOURCES: sources}
    node = decompile_node(cfg)
    if node is None:
        return None
    # a lone condition becomes a one-row builder
    if node.get(SRC_KIND) == KIND_GROUP:
        return {G_COMBINE: node.get(G_COMBINE, COMBINE_ANY), G_SOURCES: node[G_SOURCES]}
    return {G_COMBINE: COMBINE_ANY, G_SOURCES: [node]}


# --- config-flow helpers (flat builder) ------------------------------------
def validate_source(src: dict[str, Any]) -> str | None:
    """Return an error key if a leaf row is incomplete, else None."""
    kind = src.get(SRC_KIND, KIND_STATE)
    if kind == KIND_TIME:
        if not src.get(SRC_AFTER) and not src.get(SRC_BEFORE):
            return "source_time"
        return None
    if not src.get(SRC_ENTITY):
        return "source_entity"
    if kind == KIND_STATE and not src.get(SRC_STATES):
        return "source_states"
    if (
        kind == KIND_NUMERIC
        and src.get(SRC_ABOVE) is None
        and src.get(SRC_BELOW) is None
    ):
        return "source_bounds"
    return None


def source_label(src: dict[str, Any]) -> str:
    """Human-readable one-line summary for menus."""
    kind = src.get(SRC_KIND, KIND_STATE)
    if kind == KIND_TIME:
        bits = []
        if src.get(SRC_AFTER):
            bits.append(f"after {src[SRC_AFTER]}")
        if src.get(SRC_BEFORE):
            bits.append(f"before {src[SRC_BEFORE]}")
        return "time: " + (" ".join(bits) or "any")
    if kind == KIND_GROUP:
        return (
            f"group ({src.get(G_COMBINE, COMBINE_ANY)}, {len(src.get(G_SOURCES, []))})"
        )
    entity_id = src.get(SRC_ENTITY, "?")
    attr = f".{src[SRC_ATTRIBUTE]}" if src.get(SRC_ATTRIBUTE) else ""
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
    return f"{entity_id}{attr}: {detail}"
