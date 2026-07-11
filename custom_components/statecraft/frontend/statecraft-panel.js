// Statecraft sidebar panel. Vanilla web component, no build step.
// Reads/writes composite-state config via the statecraft/* websocket
// commands and renders an editor that matches the HA look via theme vars.

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

const KIND_STATE = "state";
const KIND_NUMERIC = "numeric";

// Compact human duration: 180 -> "3m", 3600 -> "1h", 90 -> "1m 30s".
function humanDur(secs) {
  const n = Number(secs);
  if (!n || n <= 0) return "";
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = Math.round(n % 60);
  return [h && `${h}h`, m && `${m}m`, s && `${s}s`].filter(Boolean).join(" ");
}

// The single "match" operator shown in the UI, derived from the stored
// kind/negate/above/below quad so the wire format stays unchanged.
function opOf(src) {
  if (src.kind === KIND_NUMERIC) return src.above != null ? "above" : "below";
  return src.negate ? "is_not" : "is";
}

function applyOp(src, op) {
  switch (op) {
    case "is": src.kind = KIND_STATE; src.negate = false; break;
    case "is_not": src.kind = KIND_STATE; src.negate = true; break;
    case "above": src.kind = KIND_NUMERIC; if (src.above == null) src.above = src.below ?? 0; src.below = null; break;
    case "below": src.kind = KIND_NUMERIC; if (src.below == null) src.below = src.above ?? 0; src.above = null; break;
  }
}

// Plain-language echo of one row, e.g. "sun.sun is below_horizon".
function rowText(src) {
  const ent = src.entity_id || "…";
  if (src.kind === KIND_NUMERIC) {
    const b = src.above != null ? `above ${src.above}` : src.below != null ? `below ${src.below}` : "…";
    return `${ent} ${b}${src.for_seconds ? ` for ${humanDur(src.for_seconds)}` : ""}`;
  }
  const v = (src.states || []).join(" or ") || "…";
  const forp = src.for_seconds ? ` for ${humanDur(src.for_seconds)}` : "";
  return `${ent} ${src.negate ? "is not" : "is"} ${v}${forp}`;
}

function builderText(b) {
  if (!b || !b.sources.length) return "";
  const joiner = b.combine === "and" ? " · and " : " · or ";
  return b.sources.map(rowText).join(joiner);
}

// stored state -> editable draft state
function toBuilder(compiled, rows) {
  return rows
    ? { combine: rows.combine || "or", sources: (rows.sources || []).map((x) => ({ ...x })) }
    : { combine: "or", sources: [] };
}

function toEditorState(s) {
  const hasBuilder = !!s.builder;
  return {
    name: s.name || "",
    mode: hasBuilder ? "builder" : "yaml",
    builder: toBuilder(s.condition, s.builder),
    yaml: hasBuilder ? "" : JSON.stringify(s.condition ?? {}, null, 2),
    hold: s.hold
      ? {
          mode: s.hold_builder ? "builder" : "yaml",
          builder: toBuilder(s.hold, s.hold_builder),
          yaml: s.hold_builder ? "" : JSON.stringify(s.hold ?? {}, null, 2),
        }
      : null,
  };
}

function newState() {
  return { name: "", mode: "builder", builder: { combine: "or", sources: [] }, yaml: "", hold: null };
}

function newHold() {
  return { mode: "builder", builder: { combine: "and", sources: [] }, yaml: "" };
}

function newSource() {
  return { entity_id: "", kind: KIND_STATE, states: [], negate: false, above: null, below: null, for_seconds: null };
}

class StatecraftPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._loaded = false;
    this._subjects = [];
    this._selected = null;
    this._draft = null; // { away_from, away_state, states: [editorState] }
    this._status = "";
    this._debug = false;
    this._lastSig = "";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._load();
      return;
    }
    // Repaint when a value we actually display changes (any person's state, the
    // selected person's composite attributes), so the sidebar and live/debug
    // readouts stay current. Never repaint while a field is focused — it would
    // drop the caret mid-edit. The signature is built only from shown values so
    // churny attributes (gps_accuracy, etc.) don't cause constant re-renders.
    if (this._liveSig() !== this._lastSig) {
      const ae = this.shadowRoot.activeElement;
      if (!ae || !/^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName)) this.render();
    }
  }

  _liveSubject() {
    const cur = this._current();
    return cur && this._hass ? this._hass.states[cur.subject] : null;
  }

  _liveState(entity_id) {
    const s = this._hass && this._hass.states ? this._hass.states[entity_id] : null;
    return s ? s.state : null;
  }

  _liveSig() {
    if (!this._hass || !this._subjects) return "";
    const parts = this._subjects.map((s) => `${s.subject}=${this._liveState(s.subject) ?? "?"}`);
    const st = this._liveSubject();
    if (st && this._draft) {
      for (const ds of this._draft.states) parts.push(`${ds.name}:${st.attributes[ds.name]}`);
      parts.push(`presence:${st.attributes.presence}`);
    }
    return parts.join("|");
  }

  async _load() {
    try {
      const res = await this._hass.connection.sendMessagePromise({ type: "statecraft/list" });
      this._subjects = res.subjects || [];
      if (!this._selected && this._subjects.length) {
        this._selected = this._subjects[0].entry_id;
      }
      this._loadDraft();
      this.render();
    } catch (e) {
      this._status = `Failed to load: ${e.message || e}`;
      this.render();
    }
  }

  _current() {
    return this._subjects.find((s) => s.entry_id === this._selected) || null;
  }

  _loadDraft() {
    const cur = this._current();
    if (!cur) { this._draft = null; return; }
    this._draft = {
      scope_type: cur.scope_type || "person",
      away_from: cur.away_from,
      away_state: cur.away_state,
      default_state: cur.default_state,
      states: (cur.states || []).map(toEditorState),
    };
  }

  _isCustom() { return this._draft && this._draft.scope_type === "custom"; }

  async _save() {
    const cur = this._current();
    if (!cur || !this._draft) return;
    this._status = "Saving…";
    this.render();
    try {
      const msg = {
        type: "statecraft/save",
        entry_id: cur.entry_id,
        states: this._draft.states,
      };
      if (this._isCustom()) {
        msg.default_state = this._draft.default_state;
      } else {
        msg.away_from = this._draft.away_from;
        msg.away_state = this._draft.away_state;
      }
      await this._hass.connection.sendMessagePromise(msg);
      this._status = "Saved.";
      await this._load(); // refresh normalized conditions + live state
    } catch (e) {
      this._status = `Save failed: ${e.message || e}`;
      this.render();
    }
  }

  // ---- model mutations (re-render after structural change) ----------------
  _builderOf(st, scope) { return scope === "hold" ? st.hold.builder : st.builder; }
  _addState() { this._draft.states.push(newState()); this.render(); }
  _delState(i) { this._draft.states.splice(i, 1); this.render(); }
  _moveState(i, d) {
    const j = i + d;
    if (j < 0 || j >= this._draft.states.length) return;
    const a = this._draft.states;
    [a[i], a[j]] = [a[j], a[i]];
    this.render();
  }
  _addSource(si, scope) { this._builderOf(this._draft.states[si], scope).sources.push(newSource()); this.render(); }
  _delSource(si, sj, scope) { this._builderOf(this._draft.states[si], scope).sources.splice(sj, 1); this.render(); }
  _toggleHold(si) {
    const st = this._draft.states[si];
    st.hold = st.hold ? null : newHold();
    this.render();
  }

  // ---- rendering ----------------------------------------------------------
  render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `<style>${this._css()}</style>${this._html()}`;
    this._wire();
    this._lastSig = this._liveSig();
  }

  _entityDatalist() {
    const ids = this._hass ? Object.keys(this._hass.states).sort() : [];
    return `<datalist id="ps-entities">${ids.map((id) => `<option value="${esc(id)}">`).join("")}</datalist>`;
  }

  _personName(s) {
    return (s && s.live && s.live.attributes && s.live.attributes.friendly_name) || (s && s.subject) || "";
  }

  _html() {
    if (!this._subjects.length) {
      return `<div class="wrap"><div class="empty">No people configured yet.<br>
        Add one via <b>Settings → Devices &amp; Services → Statecraft → Add</b>, then return here.</div></div>`;
    }
    const cur = this._current();
    const liveNow = this._liveSubject();
    const live = liveNow || (cur && cur.live ? cur.live : {});
    const people = this._subjects
      .map((s) => `
        <button class="person ${s.entry_id === this._selected ? "active" : ""}" data-act="select" data-id="${esc(s.entry_id)}" title="${esc(s.subject || "")}">
          <span class="pname">${esc(this._personName(s))}</span>
          <span class="pstate">${esc(this._liveState(s.subject) ?? (s.live && s.live.state) ?? "—")}</span>
        </button>`)
      .join("");
    const detail = cur
      ? `
        <div class="head">
          <div class="livebox">
            <span class="livename">${esc(this._personName(cur))}</span>
            <span class="livestate">${esc(live.state ?? "—")}</span>
            <span class="livesub">${esc(cur.subject)}${!cur.loaded ? " · not loaded" : ""}</span>
          </div>
          <div class="grow"></div>
          <button class="btn ghost ${this._debug ? "on" : ""}" data-act="toggle-debug" title="Show each condition's live value and whether it currently passes, plus the engine's verdict for each state">${this._debug ? "● Debugging" : "Debug"}</button>
          <button class="btn primary" data-act="save" title="Validate and save all states for this person">Save</button>
        </div>
        <p class="lede">States are checked <b>top to bottom</b>. The first one whose rules match becomes the person's state; if none match, the presence fallback at the bottom is used.</p>
        ${this._status ? `<div class="status">${esc(this._status)}</div>` : ""}
        ${this._draft ? this._statesHtml(this._liveSubject() || { state: live.state, attributes: live.attributes || {} }) : ""}
        <button class="btn add-state" data-act="add-state" title="Add another composite state below the current ones">+ Add state</button>
        ${this._draft ? this._awayHtml() : ""}`
      : `<div class="empty">Select a person on the left.</div>`;
    return `
      ${this._entityDatalist()}
      <div class="layout">
        <aside class="people">
          <div class="people-title">Scopes</div>
          ${people}
        </aside>
        <main class="detail">${detail}</main>
      </div>`;
  }

  _awayHtml() {
    if (this._isCustom()) {
      return `
        <div class="away">
          <div class="away-title">Fallback <span class="muted">— the state to report when no state above matches</span></div>
          <div class="away-grid">
            <label title="What this entity reports when none of the states match. For example 'idle'.">Default state
              <input data-field="default_state" type="text" value="${esc(this._draft.default_state ?? "")}"></label>
          </div>
        </div>`;
    }
    return `
      <div class="away">
        <div class="away-title">Fallback <span class="muted">— used when no state above matches</span></div>
        <div class="away-grid">
          <label title="The raw presence value that should be renamed to the away state below. For a person this is normally 'not_home'.">Presence treated as away
            <input data-field="away_from" type="text" value="${esc(this._draft.away_from)}"></label>
          <label title="What to call the person when they are away. Any other presence (home, a zone name) passes through unchanged.">Name for away state
            <input data-field="away_state" type="text" value="${esc(this._draft.away_state)}"></label>
        </div>
      </div>`;
  }

  _statesHtml(live) {
    return this._draft.states.map((st, i) => this._stateHtml(st, i, live)).join("");
  }

  _stateHtml(st, i, live) {
    const last = this._draft.states.length - 1;
    const active = live && live.attributes && live.attributes[st.name] === true;
    return `
      <div class="state ${active ? "is-active" : ""}">
        <div class="state-head">
          <span class="pri" title="Priority ${i + 1}. Lower numbers win.">${i + 1}</span>
          <input class="name" data-field="name" data-si="${i}" type="text" placeholder="state name (e.g. sleep)" value="${esc(st.name)}" title="The value the person's state becomes when these rules match">
          ${active ? `<span class="livepill" title="This state matches right now">● matching</span>` : ""}
          <div class="grow"></div>
          <div class="mode" title="Author the rule visually (Builder) or paste a raw Home Assistant condition (YAML)">
            <button class="seg ${st.mode === "builder" ? "on" : ""}" data-act="mode" data-scope="cond" data-si="${i}" data-mode="builder">Builder</button>
            <button class="seg ${st.mode === "yaml" ? "on" : ""}" data-act="mode" data-scope="cond" data-si="${i}" data-mode="yaml">YAML</button>
          </div>
          <button class="icon" title="Move up (higher priority)" data-act="up" data-si="${i}" ${i === 0 ? "disabled" : ""}>↑</button>
          <button class="icon" title="Move down (lower priority)" data-act="down" data-si="${i}" ${i === last ? "disabled" : ""}>↓</button>
          <button class="icon del" title="Delete this state" data-act="del-state" data-si="${i}">🗑</button>
        </div>

        <div class="section-label">Active when <span class="muted">— enter this state</span></div>
        ${st.mode === "yaml" ? this._yamlHtml(st.yaml, i, "cond") : this._builderHtml(st.builder, i, "cond")}
        ${st.mode === "builder" && st.builder.sources.length ? `<div class="summary" title="Plain-language reading of the rule above">→ ${esc(builderText(st.builder))}</div>` : ""}

        ${this._holdHtml(st, i)}
        ${this._debug ? this._debugBlock(st, live) : ""}
      </div>`;
  }

  // ---- live debug ---------------------------------------------------------
  _evalRow(src) {
    const s = this._hass && this._hass.states ? this._hass.states[src.entity_id] : null;
    if (!src.entity_id) return { value: "—", pass: false, missing: true };
    if (!s) return { value: "unavailable", pass: false, missing: true };
    const val = s.state;
    let pass;
    if (src.kind === KIND_NUMERIC) {
      const n = parseFloat(val);
      pass = !isNaN(n) && (src.above == null || n > src.above) && (src.below == null || n < src.below);
    } else {
      const inSet = (src.states || []).includes(val);
      pass = src.negate ? !inSet : inSet;
    }
    let pending = null;
    if (pass && src.for_seconds) {
      const held = (Date.now() - new Date(s.last_changed).getTime()) / 1000;
      if (held < src.for_seconds) { pass = false; pending = Math.ceil(src.for_seconds - held); }
    }
    return { value: val, pass, pending };
  }

  _dbgChips(builder) {
    if (!builder.sources.length) return `<span class="dbg-note">no conditions</span>`;
    return builder.sources.map((src) => {
      const r = this._evalRow(src);
      const short = src.entity_id ? (src.entity_id.split(".").slice(1).join(".") || src.entity_id) : "—";
      const cls = r.missing ? "unk" : r.pass ? "ok" : "no";
      const mark = r.missing ? "?" : r.pass ? "✓" : "✗";
      const wait = r.pending != null ? ` · ${r.pending}s left` : "";
      return `<span class="chip ${cls}" title="${esc(src.entity_id || "no entity")}">${esc(short)} = ${esc(String(r.value))}${wait} ${mark}</span>`;
    }).join("");
  }

  _debugBlock(st, live) {
    const attrs = (live && live.attributes) || {};
    const engine = attrs[st.name];
    const verdict = engine === true ? "active" : engine === false ? "inactive" : "unknown";
    const enter = st.mode === "builder"
      ? this._dbgChips(st.builder)
      : `<span class="dbg-note">YAML mode — per-row values not shown; see engine verdict</span>`;
    const hold = st.hold && st.hold.mode === "builder"
      ? `<div class="dbg-line"><span class="dbg-k" title="Only latches once the state is already active">hold</span>${this._dbgChips(st.hold.builder)}</div>`
      : "";
    return `
      <div class="dbg">
        <div class="dbg-line">
          <span class="dbg-k">engine</span>
          <span class="chip ${engine ? "ok" : "no"}">${esc(st.name || "state")} = ${verdict}</span>
          <span class="dbg-note">now: <b>${esc((live && live.state) || "—")}</b></span>
        </div>
        <div class="dbg-line"><span class="dbg-k">enter</span>${enter}</div>
        ${hold}
      </div>`;
  }

  _holdHtml(st, i) {
    const on = !!st.hold;
    const toggle = `
      <label class="ck hold-toggle" title="Once this state is active, keep it active while the condition below stays true — even after the 'active when' rules stop matching. Example: stay 'sleep' until the door opens.">
        <input type="checkbox" data-act="toggle-hold" data-si="${i}" ${on ? "checked" : ""}>
        Then stay in this state while… <span class="muted">(hold / hysteresis)</span>
      </label>`;
    if (!on) return `<div class="hold">${toggle}</div>`;
    const h = st.hold;
    return `
      <div class="hold">
        ${toggle}
        <div class="hold-body">
          <div class="mode small" title="Author the hold visually or as raw YAML">
            <button class="seg ${h.mode === "builder" ? "on" : ""}" data-act="mode" data-scope="hold" data-si="${i}" data-mode="builder">Builder</button>
            <button class="seg ${h.mode === "yaml" ? "on" : ""}" data-act="mode" data-scope="hold" data-si="${i}" data-mode="yaml">YAML</button>
          </div>
          ${h.mode === "yaml" ? this._yamlHtml(h.yaml, i, "hold") : this._builderHtml(h.builder, i, "hold")}
          ${h.mode === "builder" && h.builder.sources.length ? `<div class="summary">stays active while ${esc(builderText(h.builder))}</div>` : ""}
        </div>
      </div>`;
  }

  _builderHtml(b, i, scope) {
    const rows = b.sources.map((src, sj) => this._sourceHtml(src, i, sj, scope)).join("");
    return `
      <div class="builder">
        <label class="combine" title="AND = every row must be true. OR = any one row is enough.">Combine
          <select data-field="combine" data-scope="${scope}" data-si="${i}">
            <option value="or" ${b.combine === "or" ? "selected" : ""}>Any · OR</option>
            <option value="and" ${b.combine === "and" ? "selected" : ""}>All · AND</option>
          </select>
        </label>
        ${b.sources.length ? `
          <div class="src-head">
            <span>Entity</span>
            <span title="How to test the entity">Match</span>
            <span title="State(s) to match (comma-separated for OR), or the numeric threshold">Value</span>
            <span title="Only true after the entity has held this for the given number of seconds">For (s)</span>
            <span></span>
          </div>` : ``}
        <div class="sources">${rows || `<div class="hint">No conditions yet — add one below.</div>`}</div>
        <button class="btn small" data-act="add-source" data-scope="${scope}" data-si="${i}" title="Add an entity condition to this rule">+ Add condition</button>
      </div>`;
  }

  _sourceHtml(src, i, sj, scope) {
    const numeric = src.kind === KIND_NUMERIC;
    const op = opOf(src);
    const value = numeric
      ? `<input class="val" data-field="src_num" data-scope="${scope}" data-si="${i}" data-sj="${sj}" type="number" step="any" placeholder="threshold" value="${(src.above ?? src.below) ?? ""}" title="Numeric threshold">`
      : `<input class="val" data-field="src_states" data-scope="${scope}" data-si="${i}" data-sj="${sj}" type="text" placeholder="e.g. on, off" value="${esc((src.states || []).join(", "))}" title="State value(s). Comma-separated means 'any of these'.">`;
    return `
      <div class="source">
        <input class="ent" list="ps-entities" data-field="src_entity" data-scope="${scope}" data-si="${i}" data-sj="${sj}" type="text" placeholder="entity_id" value="${esc(src.entity_id)}" title="${esc(src.entity_id || "Pick an entity")}">
        <select class="op" data-field="src_op" data-scope="${scope}" data-si="${i}" data-sj="${sj}" title="How to test the entity">
          <option value="is" ${op === "is" ? "selected" : ""}>is</option>
          <option value="is_not" ${op === "is_not" ? "selected" : ""}>is not</option>
          <option value="above" ${op === "above" ? "selected" : ""}>&gt; above</option>
          <option value="below" ${op === "below" ? "selected" : ""}>&lt; below</option>
        </select>
        ${value}
        <input class="for" data-field="src_for" data-scope="${scope}" data-si="${i}" data-sj="${sj}" type="number" min="0" placeholder="—" value="${src.for_seconds ?? ""}" title="Seconds the condition must hold before it counts (optional)">
        <button class="icon del" title="Remove this condition" data-act="del-source" data-scope="${scope}" data-si="${i}" data-sj="${sj}">✕</button>
      </div>`;
  }

  _yamlHtml(text, i, scope) {
    return `
      <div class="yaml">
        <textarea data-field="yaml" data-scope="${scope}" data-si="${i}" rows="6" spellcheck="false" placeholder="condition: state\n  entity_id: ...\n  state: 'on'">${esc(text)}</textarea>
        <div class="hint">Native HA condition (YAML or JSON): a single condition, or an and/or/not block.</div>
      </div>`;
  }

  // ---- event wiring -------------------------------------------------------
  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-act]").forEach((el) => {
      el.addEventListener("click", (e) => this._onClick(e));
    });
    root.addEventListener("change", (e) => this._onChange(e));
  }

  _onClick(e) {
    const el = e.currentTarget;
    const act = el.dataset.act;
    const si = el.dataset.si !== undefined ? +el.dataset.si : null;
    const sj = el.dataset.sj !== undefined ? +el.dataset.sj : null;
    const scope = el.dataset.scope || "cond";
    switch (act) {
      case "select": this._selected = el.dataset.id; this._status = ""; this._loadDraft(); this.render(); break;
      case "save": this._save(); break;
      case "toggle-debug": this._debug = !this._debug; this.render(); break;
      case "add-state": this._addState(); break;
      case "del-state": this._delState(si); break;
      case "up": this._moveState(si, -1); break;
      case "down": this._moveState(si, 1); break;
      case "add-source": this._addSource(si, scope); break;
      case "del-source": this._delSource(si, sj, scope); break;
      case "toggle-hold": this._toggleHold(si); break;
      case "mode":
        if (scope === "hold") this._draft.states[si].hold.mode = el.dataset.mode;
        else this._draft.states[si].mode = el.dataset.mode;
        this.render(); break;
    }
  }

  _onChange(e) {
    const el = e.target;
    const f = el.dataset.field;
    if (!f) return;
    const si = el.dataset.si !== undefined ? +el.dataset.si : null;
    const sj = el.dataset.sj !== undefined ? +el.dataset.sj : null;
    const scope = el.dataset.scope || "cond";
    const d = this._draft;
    const st = si !== null ? d.states[si] : null;
    const b = st && scope === "hold" && st.hold ? st.hold.builder : st ? st.builder : null;
    const src = b && sj !== null ? b.sources[sj] : null;
    const val = el.type === "checkbox" ? el.checked : el.value;
    switch (f) {
      case "away_from": d.away_from = val; break;
      case "away_state": d.away_state = val; break;
      case "default_state": d.default_state = val; break;
      case "name": st.name = val; break;
      case "yaml": if (scope === "hold") st.hold.yaml = val; else st.yaml = val; break;
      case "combine": b.combine = val; break;
      case "src_entity": src.entity_id = val; break;
      case "src_op": applyOp(src, val); this.render(); break;
      case "src_states": src.states = val.split(",").map((x) => x.trim()).filter(Boolean); break;
      case "src_num":
        if (src.above != null) src.above = val === "" ? null : Number(val);
        else src.below = val === "" ? null : Number(val);
        break;
      case "src_for": src.for_seconds = val === "" ? null : Number(val); this._refreshSummary(st, si); break;
    }
  }

  // Update just the plain-language summaries without a full re-render (keeps
  // focus in the text inputs while typing).
  _refreshSummary(st, si) {
    const root = this.shadowRoot;
    const card = root.querySelectorAll(".state")[si];
    if (!card) return;
    const sums = card.querySelectorAll(".summary");
    if (sums[0] && st.mode === "builder") sums[0].textContent = `→ ${builderText(st.builder)}`;
  }

  _css() {
    return `
      :host { display:block; padding:16px; color:var(--primary-text-color);
        font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      .wrap { max-width:880px; margin:0 auto; }
      .layout { display:flex; gap:16px; align-items:flex-start; max-width:1120px; margin:0 auto; }
      .people { flex:0 0 240px; display:flex; flex-direction:column; gap:6px;
        background:var(--card-background-color); border:1px solid var(--divider-color);
        border-radius:12px; padding:10px; position:sticky; top:16px; }
      .people-title { font-size:12px; letter-spacing:.04em; text-transform:uppercase;
        color:var(--secondary-text-color); padding:2px 6px 6px; }
      .person { display:flex; align-items:center; justify-content:space-between; gap:8px;
        background:none; color:var(--primary-text-color); border:1px solid transparent;
        border-radius:8px; padding:10px 12px; cursor:pointer; text-align:left; width:100%; }
      .person:hover { background:var(--secondary-background-color); }
      .person.active { background:var(--primary-color); color:var(--text-primary-color,#fff); }
      .pname { font-weight:500; }
      .pstate { font-size:12px; opacity:.85; text-transform:capitalize; }
      .detail { flex:1; min-width:0; }
      .head { display:flex; align-items:center; gap:12px; margin-bottom:6px; }
      .livebox { display:flex; flex-direction:column; }
      .livename { font-size:13px; color:var(--secondary-text-color); }
      .livestate { font-size:26px; font-weight:700; text-transform:capitalize; line-height:1.1; }
      .livesub { font-size:12px; color:var(--secondary-text-color); }
      .grow { flex:1; }
      .lede { font-size:12.5px; color:var(--secondary-text-color); margin:0 0 14px; line-height:1.5; max-width:70ch; }
      .lede b { color:var(--primary-text-color); }
      .status { background:var(--secondary-background-color); border-radius:8px; padding:8px 12px; margin-bottom:12px; font-size:13px; }
      .state { background:var(--card-background-color); border:1px solid var(--divider-color);
        border-radius:14px; padding:14px 14px 12px; margin-bottom:14px; transition:border-color .2s; }
      .state.is-active { border-color:var(--primary-color); box-shadow:0 0 0 1px var(--primary-color) inset; }
      .state-head { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
      .pri { width:24px; height:24px; border-radius:50%; background:var(--primary-color);
        color:var(--text-primary-color,#fff); display:flex; align-items:center; justify-content:center;
        font-size:12px; font-weight:700; flex:none; cursor:help; }
      .name { flex:0 1 240px; font-weight:600; }
      .livepill { font-size:11px; font-weight:600; color:var(--success-color,#43a047);
        background:color-mix(in srgb, var(--success-color,#43a047) 14%, transparent);
        border-radius:999px; padding:2px 8px; white-space:nowrap; }
      input, select, textarea { background:var(--secondary-background-color); color:var(--primary-text-color);
        border:1px solid var(--divider-color); border-radius:7px; padding:7px 9px; font-size:13px; box-sizing:border-box; }
      input:focus, select:focus, textarea:focus { outline:none; border-color:var(--primary-color); }
      textarea { width:100%; font-family:ui-monospace,Menlo,monospace; resize:vertical; }
      .mode { display:flex; flex:none; }
      .mode.small .seg { padding:3px 9px; font-size:11px; }
      .seg { border:1px solid var(--divider-color); background:var(--secondary-background-color);
        color:var(--secondary-text-color); padding:5px 11px; cursor:pointer; font-size:12px; }
      .seg:first-child { border-radius:7px 0 0 7px; } .seg:last-child { border-radius:0 7px 7px 0; border-left:none; }
      .seg.on { background:var(--primary-color); color:var(--text-primary-color,#fff); border-color:var(--primary-color); }
      .icon { background:none; border:none; cursor:pointer; font-size:15px; color:var(--secondary-text-color); padding:4px; border-radius:6px; }
      .icon:hover:not([disabled]) { background:var(--secondary-background-color); }
      .icon[disabled] { opacity:.3; cursor:default; }
      .icon.del:hover { color:var(--error-color, #db4437); }
      .section-label { font-size:11px; letter-spacing:.04em; text-transform:uppercase;
        color:var(--primary-text-color); font-weight:600; margin:4px 0 8px; }
      .section-label .muted, .muted { color:var(--secondary-text-color); font-weight:400; text-transform:none; letter-spacing:0; }
      .builder { display:flex; flex-direction:column; gap:8px; }
      .combine { font-size:12px; color:var(--secondary-text-color); display:flex; align-items:center; gap:8px; }
      .combine select { width:130px; }
      .src-head, .source { display:grid; grid-template-columns:minmax(150px,1fr) 108px minmax(110px,1fr) 74px 30px; gap:8px; align-items:center; }
      .src-head { font-size:10.5px; letter-spacing:.03em; text-transform:uppercase; color:var(--secondary-text-color); padding:0 2px; }
      .sources { display:flex; flex-direction:column; gap:7px; }
      .source .ent { min-width:0; }
      .summary { font-size:12px; color:var(--secondary-text-color); font-style:italic; margin-top:2px; padding-left:2px; }
      .hold { margin-top:12px; border-top:1px dashed var(--divider-color); padding-top:10px; }
      .hold-toggle { font-size:13px; }
      .hold-body { margin-top:10px; padding-left:12px; border-left:2px solid var(--primary-color); display:flex; flex-direction:column; gap:8px; }
      .ck { display:flex; align-items:center; gap:8px; color:var(--primary-text-color); cursor:pointer; }
      .away { background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:14px; padding:14px; margin-top:6px; }
      .away-title { font-size:11px; letter-spacing:.04em; text-transform:uppercase; color:var(--primary-text-color); font-weight:600; margin-bottom:12px; }
      .away-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
      .away label { display:flex; flex-direction:column; gap:5px; font-size:12px; color:var(--secondary-text-color); }
      .btn { background:var(--secondary-background-color); color:var(--primary-text-color);
        border:1px solid var(--divider-color); border-radius:9px; padding:8px 14px; cursor:pointer; font-size:13px; }
      .btn:hover { border-color:var(--primary-color); }
      .btn.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); border-color:var(--primary-color); }
      .btn.small { align-self:flex-start; padding:5px 11px; font-size:12px; }
      .btn.ghost { padding:6px 12px; font-size:12px; }
      .btn.ghost.on { background:color-mix(in srgb, var(--primary-color) 18%, transparent); border-color:var(--primary-color); color:var(--primary-text-color); }
      .dbg { margin-top:12px; background:var(--secondary-background-color); border:1px solid var(--divider-color);
        border-radius:10px; padding:9px 11px; display:flex; flex-direction:column; gap:7px; }
      .dbg-line { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
      .dbg-k { font-size:10px; text-transform:uppercase; letter-spacing:.04em; color:var(--secondary-text-color);
        min-width:44px; font-weight:600; }
      .dbg-note { font-size:12px; color:var(--secondary-text-color); }
      .chip { font-size:11.5px; font-family:ui-monospace,Menlo,monospace; padding:2px 8px; border-radius:6px;
        border:1px solid transparent; white-space:nowrap; }
      .chip.ok { color:var(--success-color); background:color-mix(in srgb, var(--success-color) 13%, transparent); }
      .chip.no { color:var(--error-color); background:color-mix(in srgb, var(--error-color) 13%, transparent); }
      .chip.unk { color:var(--secondary-text-color); background:var(--card-background-color); border-color:var(--divider-color); }
      .btn.add-state { width:100%; margin-bottom:14px; border-style:dashed; }
      .hint { font-size:11.5px; color:var(--secondary-text-color); padding:4px 2px; }
      .empty { text-align:center; color:var(--secondary-text-color); margin-top:40px; line-height:1.7; }
      @media (max-width:720px) {
        .layout { flex-direction:column; }
        .people { position:static; width:100%; flex:none; }
        .away-grid { grid-template-columns:1fr; }
        .src-head { display:none; }
        .source { grid-template-columns:1fr 1fr; }
      }
    `;
  }
}

customElements.define("statecraft-panel", StatecraftPanel);
