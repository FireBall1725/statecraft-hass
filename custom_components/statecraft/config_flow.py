"""Config + options flow for Statecraft.

The config step picks the subject (a person) and the away mapping. The options
flow is a menu to manage the ordered list of composite states. Each state is
authored in one of two modes:

  builder  -> add/edit source rows (entity + match + optional duration) that
              compile to a native HA condition (condition_builder.py)
  yaml     -> paste a native HA condition directly

Either way the stored state holds a `condition` dict the engine consumes; the
builder also stashes its source rows so editing reopens the builder.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
import yaml
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import condition, selector
from homeassistant.util import slugify

from .condition_builder import (
    COMBINE_ALL,
    COMBINE_ANY,
    KIND_NUMERIC,
    KIND_STATE,
    SRC_ABOVE,
    SRC_ATTRIBUTE,
    SRC_BELOW,
    SRC_ENTITY,
    SRC_FOR,
    SRC_KIND,
    SRC_NEGATE,
    SRC_STATES,
    compile_condition,
    source_label,
    validate_source,
)
from .const import (
    CONF_AWAY_FROM,
    CONF_AWAY_STATE,
    CONF_CONDITION,
    CONF_DEFAULT_STATE,
    CONF_HOLD,
    CONF_ICON,
    CONF_NAME,
    CONF_SCOPE_NAME,
    CONF_SCOPE_TYPE,
    CONF_STATES,
    CONF_SUBJECT,
    DEFAULT_AWAY_FROM,
    DEFAULT_AWAY_STATE,
    DEFAULT_STATE,
    DOMAIN,
    PERSON_DOMAIN,
    SCOPE_CUSTOM,
    SCOPE_PERSON,
)

_LOGGER = logging.getLogger(__name__)

# Builder-only key stored alongside the compiled condition on a state.
CONF_BUILDER = "builder"
B_COMBINE = "combine"
B_SOURCES = "sources"

# State-form field for authoring mode.
F_MODE = "mode"
MODE_BUILDER = "builder"
MODE_YAML = "yaml"
F_HOLD = "hold"  # optional native HA condition that latches the state on


def _opt(value: str, label: str) -> selector.SelectOptionDict:
    """A typed select option (HA wants SelectOptionDict, not a plain dict)."""
    return selector.SelectOptionDict(value=value, label=label)


_PERSON_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=PERSON_DOMAIN)
)
_ANY_ENTITY = selector.EntitySelector(selector.EntitySelectorConfig())
_TEXT = selector.TextSelector()
_YAML_FIELD = selector.TextSelector(selector.TextSelectorConfig(multiline=True))
_BOOL = selector.BooleanSelector()
_SECONDS = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=86400,
        step=10,
        unit_of_measurement="s",
        mode=selector.NumberSelectorMode.BOX,
    )
)
_NUMBER = selector.NumberSelector(
    selector.NumberSelectorConfig(step="any", mode=selector.NumberSelectorMode.BOX)
)
_STATES_FIELD = selector.SelectSelector(
    selector.SelectSelectorConfig(options=[], multiple=True, custom_value=True)
)
_MODE_FIELD = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            _opt(MODE_BUILDER, "Builder"),
            _opt(MODE_YAML, "YAML"),
        ],
        mode=selector.SelectSelectorMode.LIST,
    )
)
_KIND_FIELD = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            _opt(KIND_STATE, "State match"),
            _opt(KIND_NUMERIC, "Numeric threshold"),
        ],
        mode=selector.SelectSelectorMode.LIST,
    )
)
_COMBINE_FIELD = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            _opt(COMBINE_ANY, "Any source active (OR)"),
            _opt(COMBINE_ALL, "All sources active (AND)"),
        ],
        mode=selector.SelectSelectorMode.LIST,
    )
)


def _index_options(
    items: list[dict[str, Any]], label_key: str
) -> list[selector.SelectOptionDict]:
    return [_opt(str(i), s[label_key]) for i, s in enumerate(items)]


def _flow_editable(src: dict[str, Any]) -> bool:
    """True if the config-flow source form can edit this row without data loss.

    The form only expresses plain state / numeric rows. Time, group, and
    attribute-match rows are panel-only; offering them here would rewrite them as
    bare rows on save, so they are preserved instead of being editable here.
    """
    if src.get(SRC_KIND, KIND_STATE) not in (KIND_STATE, KIND_NUMERIC):
        return False
    return not src.get(SRC_ATTRIBUTE)


class StatecraftConfigFlow(ConfigFlow, domain=DOMAIN):
    """Choose what a scope tracks; states are added afterwards in the panel."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Type-first: extend an existing person, or create a custom state entity.
        return self.async_show_menu(
            step_id="user", menu_options=[SCOPE_PERSON, SCOPE_CUSTOM]
        )

    async def async_step_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Attach states onto an existing person entity."""
        if user_input is not None:
            subject = user_input[CONF_SUBJECT]
            await self.async_set_unique_id(subject)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=subject,
                data={CONF_SUBJECT: subject, CONF_SCOPE_TYPE: SCOPE_PERSON},
                options={
                    CONF_AWAY_FROM: user_input[CONF_AWAY_FROM],
                    CONF_AWAY_STATE: user_input[CONF_AWAY_STATE],
                    CONF_STATES: [],
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_SUBJECT): _PERSON_SELECTOR,
                vol.Required(CONF_AWAY_FROM, default=DEFAULT_AWAY_FROM): _TEXT,
                vol.Required(CONF_AWAY_STATE, default=DEFAULT_AWAY_STATE): _TEXT,
            }
        )
        return self.async_show_form(step_id="person", data_schema=schema)

    async def async_step_custom(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a new statecraft.* state entity we own and drive."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = (user_input[CONF_SCOPE_NAME] or "").strip()
            slug = slugify(name)
            if not slug:
                errors[CONF_SCOPE_NAME] = "invalid_name"
            else:
                subject = f"{DOMAIN}.{slug}"
                await self.async_set_unique_id(subject)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_SUBJECT: subject,
                        CONF_SCOPE_TYPE: SCOPE_CUSTOM,
                        CONF_SCOPE_NAME: name,
                        CONF_ICON: user_input.get(CONF_ICON) or None,
                    },
                    options={
                        CONF_DEFAULT_STATE: (
                            user_input.get(CONF_DEFAULT_STATE) or DEFAULT_STATE
                        ),
                        CONF_STATES: [],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SCOPE_NAME, default=""): _TEXT,
                vol.Required(CONF_DEFAULT_STATE, default=DEFAULT_STATE): _TEXT,
                vol.Optional(CONF_ICON): selector.IconSelector(),
            }
        )
        return self.async_show_form(step_id="custom", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry) -> OptionsFlow:
        return StatecraftOptionsFlow()


class StatecraftOptionsFlow(OptionsFlow):
    """Menu-driven editing of the subject settings and the state list."""

    def __init__(self) -> None:
        self._states: list[dict[str, Any]] = []
        self._away_from: str = DEFAULT_AWAY_FROM
        self._away_state: str = DEFAULT_AWAY_STATE
        self._default_state: str = DEFAULT_STATE
        self._scope_type: str = SCOPE_PERSON
        self._loaded = False
        # transient edit state
        self._editing: int | None = None
        self._editing_source: int | None = None
        self._draft: dict[str, Any] = {}

    # --- persistence helpers ------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        opts = self.config_entry.options
        self._scope_type = self.config_entry.data.get(CONF_SCOPE_TYPE, SCOPE_PERSON)
        self._states = [dict(s) for s in opts.get(CONF_STATES, [])]
        self._away_from = opts.get(CONF_AWAY_FROM, DEFAULT_AWAY_FROM)
        self._away_state = opts.get(CONF_AWAY_STATE, DEFAULT_AWAY_STATE)
        self._default_state = opts.get(CONF_DEFAULT_STATE, DEFAULT_STATE)
        self._loaded = True

    @property
    def _is_custom(self) -> bool:
        return self._scope_type == SCOPE_CUSTOM

    def _new_draft(self) -> dict[str, Any]:
        return {
            CONF_NAME: "",
            F_MODE: MODE_BUILDER,
            CONF_CONDITION: None,
            CONF_BUILDER: {B_COMBINE: COMBINE_ANY, B_SOURCES: []},
            CONF_HOLD: None,
        }

    def _draft_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        builder = state.get(CONF_BUILDER)
        return {
            CONF_NAME: state.get(CONF_NAME, ""),
            F_MODE: MODE_BUILDER if builder else MODE_YAML,
            CONF_CONDITION: state.get(CONF_CONDITION),
            CONF_BUILDER: builder or {B_COMBINE: COMBINE_ANY, B_SOURCES: []},
            CONF_HOLD: state.get(CONF_HOLD),
        }

    def _finalize_state(self) -> None:
        state: dict[str, Any] = {
            CONF_NAME: self._draft[CONF_NAME],
            CONF_CONDITION: self._draft[CONF_CONDITION],
        }
        if self._draft[F_MODE] == MODE_BUILDER:
            state[CONF_BUILDER] = self._draft[CONF_BUILDER]
        if self._draft.get(CONF_HOLD):
            state[CONF_HOLD] = self._draft[CONF_HOLD]

        if self._editing is None:
            self._states.append(state)
        else:
            self._states[self._editing] = state
        self._draft = {}
        self._editing = None
        # caller routes back to init

    def _save(self) -> ConfigFlowResult:
        if self._is_custom:
            data = {CONF_DEFAULT_STATE: self._default_state, CONF_STATES: self._states}
        else:
            data = {
                CONF_AWAY_FROM: self._away_from,
                CONF_AWAY_STATE: self._away_state,
                CONF_STATES: self._states,
            }
        return self.async_create_entry(data=data)

    # --- menu ---------------------------------------------------------------
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._load()
        options = ["add_state", "settings", "save"]
        if self._states:
            options[1:1] = ["edit_state", "remove_state"]
        return self.async_show_menu(step_id="init", menu_options=options)

    # --- add / edit a state -------------------------------------------------
    async def async_step_add_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._load()
        self._editing = None
        self._draft = self._new_draft()
        return await self.async_step_state_meta()

    async def async_step_edit_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._load()
        if user_input is not None:
            self._editing = int(user_input["index"])
            self._draft = self._draft_from_state(self._states[self._editing])
            return await self.async_step_state_meta()
        schema = vol.Schema(
            {
                vol.Required("index"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_index_options(self._states, CONF_NAME)
                    )
                )
            }
        )
        return self.async_show_form(step_id="edit_state", data_schema=schema)

    async def async_step_state_meta(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            hold_yaml = (user_input.get(F_HOLD) or "").strip()
            hold_cfg = None
            if hold_yaml:
                try:
                    hold_cfg = await condition.async_validate_condition_config(
                        self.hass, yaml.safe_load(hold_yaml)
                    )
                except yaml.YAMLError:
                    errors[F_HOLD] = "invalid_yaml"
                except Exception:  # noqa: BLE001
                    errors[F_HOLD] = "invalid_condition"
            if not errors:
                self._draft[CONF_NAME] = user_input[CONF_NAME].strip()
                self._draft[F_MODE] = user_input[F_MODE]
                self._draft[CONF_HOLD] = hold_cfg
                if self._draft[F_MODE] == MODE_YAML:
                    return await self.async_step_state_yaml()
                return await self.async_step_builder()

        return self.async_show_form(
            step_id="state_meta", data_schema=self._meta_schema(), errors=errors
        )

    def _meta_schema(self) -> vol.Schema:
        d = self._draft
        hold = d.get(CONF_HOLD)
        hold_default = yaml.safe_dump(hold, sort_keys=False) if hold else ""
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): _TEXT,
                vol.Required(F_MODE, default=d.get(F_MODE, MODE_BUILDER)): _MODE_FIELD,
                vol.Optional(F_HOLD, default=hold_default): _YAML_FIELD,
            }
        )

    # --- YAML authoring -----------------------------------------------------
    async def async_step_state_yaml(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            parsed = None
            try:
                parsed = yaml.safe_load(user_input[CONF_CONDITION])
            except yaml.YAMLError:
                errors[CONF_CONDITION] = "invalid_yaml"
            if not errors:
                if parsed is None:
                    errors[CONF_CONDITION] = "invalid_condition"
                else:
                    try:
                        parsed = await condition.async_validate_condition_config(
                            self.hass, parsed
                        )
                    except Exception:  # noqa: BLE001
                        errors[CONF_CONDITION] = "invalid_condition"
            if not errors:
                self._draft[CONF_CONDITION] = parsed
                self._draft[F_MODE] = MODE_YAML
                self._finalize_state()
                return await self.async_step_init()

        existing = self._draft.get(CONF_CONDITION)
        default = yaml.safe_dump(existing, sort_keys=False) if existing else ""
        schema = vol.Schema(
            {vol.Required(CONF_CONDITION, default=default): _YAML_FIELD}
        )
        return self.async_show_form(
            step_id="state_yaml", data_schema=schema, errors=errors
        )

    # --- builder menu -------------------------------------------------------
    async def async_step_builder(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        sources = self._draft[CONF_BUILDER][B_SOURCES]
        options = ["add_source", "set_combine", "finish_builder"]
        extra = []
        if any(_flow_editable(s) for s in sources):
            extra.append("edit_source")
        if sources:
            extra.append("remove_source")
        options[1:1] = extra
        return self.async_show_menu(
            step_id="builder",
            menu_options=options,
            description_placeholders={
                "summary": self._builder_summary(),
            },
        )

    def _builder_summary(self) -> str:
        b = self._draft[CONF_BUILDER]
        combine = "OR" if b[B_COMBINE] == COMBINE_ANY else "AND"
        if not b[B_SOURCES]:
            return "no sources yet"
        lines = [f"  - {source_label(s)}" for s in b[B_SOURCES]]
        return f"combine: {combine}\n" + "\n".join(lines)

    async def async_step_add_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._editing_source = None
        return await self.async_step_source_form(user_input)

    async def async_step_edit_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        sources = self._draft[CONF_BUILDER][B_SOURCES]
        if user_input is not None and "index" in user_input:
            idx = int(user_input["index"])
            # Guard: the source form can only express state/numeric rows, so
            # editing a time/group/attribute row here would silently rewrite it
            # as a bare row. Only rows this form can round-trip are offered, but
            # re-check in case of a stale selection.
            if not _flow_editable(sources[idx]):
                return await self.async_step_builder()
            self._editing_source = idx
            return await self.async_step_source_form(None)
        # Only list rows the config flow can edit without losing data; richer
        # rows (time, group, attribute match) are edited in the panel.
        labels = [
            _opt(str(i), source_label(s))
            for i, s in enumerate(sources)
            if _flow_editable(s)
        ]
        if not labels:
            return await self.async_step_builder()
        schema = vol.Schema(
            {
                vol.Required("index"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=labels)
                )
            }
        )
        return self.async_show_form(step_id="edit_source", data_schema=schema)

    async def async_step_source_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            src: dict[str, Any] = {
                SRC_ENTITY: user_input.get(SRC_ENTITY),
                SRC_KIND: user_input.get(SRC_KIND, KIND_STATE),
            }
            if src[SRC_KIND] == KIND_STATE:
                src[SRC_STATES] = user_input.get(SRC_STATES, [])
                src[SRC_NEGATE] = user_input.get(SRC_NEGATE, False)
            else:
                if user_input.get(SRC_ABOVE) is not None:
                    src[SRC_ABOVE] = user_input[SRC_ABOVE]
                if user_input.get(SRC_BELOW) is not None:
                    src[SRC_BELOW] = user_input[SRC_BELOW]
            if user_input.get(SRC_FOR):
                src[SRC_FOR] = user_input[SRC_FOR]

            err = validate_source(src)
            if err:
                errors["base"] = err
            else:
                sources = self._draft[CONF_BUILDER][B_SOURCES]
                if self._editing_source is None:
                    sources.append(src)
                else:
                    sources[self._editing_source] = src
                self._editing_source = None
                return await self.async_step_builder()

        return self.async_show_form(
            step_id="source_form",
            data_schema=self._source_schema(),
            errors=errors,
        )

    def _source_schema(self) -> vol.Schema:
        current: dict[str, Any] = {}
        if self._editing_source is not None:
            current = self._draft[CONF_BUILDER][B_SOURCES][self._editing_source]
        return vol.Schema(
            {
                vol.Required(
                    SRC_ENTITY, default=current.get(SRC_ENTITY, vol.UNDEFINED)
                ): _ANY_ENTITY,
                vol.Required(
                    SRC_KIND, default=current.get(SRC_KIND, KIND_STATE)
                ): _KIND_FIELD,
                vol.Optional(
                    SRC_STATES, default=current.get(SRC_STATES, [])
                ): _STATES_FIELD,
                vol.Optional(SRC_NEGATE, default=current.get(SRC_NEGATE, False)): _BOOL,
                vol.Optional(
                    SRC_ABOVE, default=current.get(SRC_ABOVE, vol.UNDEFINED)
                ): _NUMBER,
                vol.Optional(
                    SRC_BELOW, default=current.get(SRC_BELOW, vol.UNDEFINED)
                ): _NUMBER,
                vol.Optional(
                    SRC_FOR, default=current.get(SRC_FOR, vol.UNDEFINED)
                ): _SECONDS,
            }
        )

    async def async_step_set_combine(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._draft[CONF_BUILDER][B_COMBINE] = user_input[B_COMBINE]
            return await self.async_step_builder()
        schema = vol.Schema(
            {
                vol.Required(
                    B_COMBINE, default=self._draft[CONF_BUILDER][B_COMBINE]
                ): _COMBINE_FIELD
            }
        )
        return self.async_show_form(step_id="set_combine", data_schema=schema)

    async def async_step_remove_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        sources = self._draft[CONF_BUILDER][B_SOURCES]
        if user_input is not None:
            drop = {int(i) for i in user_input.get("indexes", [])}
            self._draft[CONF_BUILDER][B_SOURCES] = [
                s for i, s in enumerate(sources) if i not in drop
            ]
            return await self.async_step_builder()
        labels = [_opt(str(i), source_label(s)) for i, s in enumerate(sources)]
        schema = vol.Schema(
            {
                vol.Required("indexes", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=labels, multiple=True)
                )
            }
        )
        return self.async_show_form(step_id="remove_source", data_schema=schema)

    async def async_step_finish_builder(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        b = self._draft[CONF_BUILDER]
        compiled = compile_condition(b[B_COMBINE], b[B_SOURCES])
        if compiled is None:
            # nothing to compile, send the user back to add a source
            return await self.async_step_builder()
        try:
            compiled = await condition.async_validate_condition_config(
                self.hass, compiled
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("builder produced an invalid condition: %s", err)
            return await self.async_step_builder()

        self._draft[CONF_CONDITION] = compiled
        self._draft[F_MODE] = MODE_BUILDER
        self._finalize_state()
        return await self.async_step_init()

    # --- remove state -------------------------------------------------------
    async def async_step_remove_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._load()
        if user_input is not None:
            drop = {int(i) for i in user_input.get("indexes", [])}
            self._states = [s for i, s in enumerate(self._states) if i not in drop]
            return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required("indexes", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_index_options(self._states, CONF_NAME), multiple=True
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_state", data_schema=schema)

    # --- settings -----------------------------------------------------------
    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._load()
        if self._is_custom:
            if user_input is not None:
                self._default_state = user_input[CONF_DEFAULT_STATE]
                return await self.async_step_init()
            schema = vol.Schema(
                {vol.Required(CONF_DEFAULT_STATE, default=self._default_state): _TEXT}
            )
            return self.async_show_form(step_id="settings", data_schema=schema)

        if user_input is not None:
            self._away_from = user_input[CONF_AWAY_FROM]
            self._away_state = user_input[CONF_AWAY_STATE]
            return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_AWAY_FROM, default=self._away_from): _TEXT,
                vol.Required(CONF_AWAY_STATE, default=self._away_state): _TEXT,
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    # --- save ---------------------------------------------------------------
    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._load()
        return self._save()
