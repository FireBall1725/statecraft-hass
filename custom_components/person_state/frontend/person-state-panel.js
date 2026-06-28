// Person State sidebar panel. Vanilla web component, no build step.
// Reads/writes composite-state config via the person_state/* websocket
// commands and renders an editor that matches the HA look via theme vars.

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

const KIND_STATE = "state";
const KIND_NUMERIC = "numeric";

// stored state -> editable draft state
function toEditorState(s) {
  const hasBuilder = !!s.builder;
  return {
    name: s.name || "",
    mode: hasBuilder ? "builder" : "yaml",
    builder: s.builder
      ? { combine: s.builder.combine || "or", sources: (s.builder.sources || []).map((x) => ({ ...x })) }
      : { combine: "or", sources: [] },
    yaml: hasBuilder ? "" : JSON.stringify(s.condition ?? {}, null, 2),
    grace: s.grace ? { ...s.grace } : null,
    persist: s.persist ? { ...s.persist } : null,
  };
}

function newState() {
  return {
    name: "",
    mode: "builder",
    builder: { combine: "or", sources: [] },
    yaml: "",
    grace: null,
    persist: null,
  };
}

function newSource() {
  return { entity_id: "", kind: KIND_STATE, states: [], negate: false, above: null, below: null, for_seconds: null };
}

class PersonStatePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._loaded = false;
    this._subjects = [];
    this._selected = null;
    this._draft = null; // { away_from, away_state, states: [editorState] }
    this._status = "";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
  }

  async _load() {
    try {
      const res = await this._hass.connection.sendMessagePromise({ type: "person_state/list" });
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
      away_from: cur.away_from,
      away_state: cur.away_state,
      states: (cur.states || []).map(toEditorState),
    };
  }

  async _save() {
    const cur = this._current();
    if (!cur || !this._draft) return;
    this._status = "Saving…";
    this.render();
    try {
      const res = await this._hass.connection.sendMessagePromise({
        type: "person_state/save",
        entry_id: cur.entry_id,
        away_from: this._draft.away_from,
        away_state: this._draft.away_state,
        states: this._draft.states,
      });
      this._status = "Saved.";
      // refresh from server (gets normalized conditions + live state)
      await this._load();
    } catch (e) {
      this._status = `Save failed: ${e.message || e}`;
      this.render();
    }
  }

  // ---- model mutations (re-render after structural change) ----------------
  _addState() { this._draft.states.push(newState()); this.render(); }
  _delState(i) { this._draft.states.splice(i, 1); this.render(); }
  _moveState(i, d) {
    const j = i + d;
    if (j < 0 || j >= this._draft.states.length) return;
    const a = this._draft.states;
    [a[i], a[j]] = [a[j], a[i]];
    this.render();
  }
  _addSource(si) { this._draft.states[si].builder.sources.push(newSource()); this.render(); }
  _delSource(si, sj) { this._draft.states[si].builder.sources.splice(sj, 1); this.render(); }

  // ---- rendering ----------------------------------------------------------
  render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `<style>${this._css()}</style>${this._html()}`;
    this._wire();
  }

  _entityDatalist() {
    const ids = this._hass ? Object.keys(this._hass.states).sort() : [];
    return `<datalist id="ps-entities">${ids.map((id) => `<option value="${esc(id)}">`).join("")}</datalist>`;
  }

  _html() {
    if (!this._subjects.length) {
      return `<div class="wrap"><div class="empty">No people configured yet.<br>
        Add one via <b>Settings → Devices &amp; Services → Person State → Add</b>, then return here.</div></div>`;
    }
    const cur = this._current();
    const live = cur && cur.live ? cur.live : {};
    const tabs = this._subjects
      .map((s) => `<button class="tab ${s.entry_id === this._selected ? "active" : ""}" data-act="select" data-id="${esc(s.entry_id)}">${esc(s.subject)}</button>`)
      .join("");
    return `
      ${this._entityDatalist()}
      <div class="wrap">
        <div class="tabs">${tabs}</div>
        <div class="head">
          <div class="livebox">
            <span class="livestate">${esc(live.state ?? "—")}</span>
            <span class="livesub">${cur ? esc(cur.subject) : ""}${cur && !cur.loaded ? " (not loaded)" : ""}</span>
          </div>
          <div class="grow"></div>
          <button class="btn primary" data-act="save">Save</button>
        </div>
        ${this._status ? `<div class="status">${esc(this._status)}</div>` : ""}
        ${this._draft ? this._statesHtml() : ""}
        <button class="btn add-state" data-act="add-state">+ Add state</button>
        ${this._draft ? this._awayHtml() : ""}
      </div>`;
  }

  _awayHtml() {
    return `
      <div class="away">
        <div class="away-title">Fallback (when no state matches)</div>
        <label>Presence treated as away
          <input data-field="away_from" type="text" value="${esc(this._draft.away_from)}"></label>
        <label>Name for away state
          <input data-field="away_state" type="text" value="${esc(this._draft.away_state)}"></label>
      </div>`;
  }

  _statesHtml() {
    return this._draft.states.map((st, i) => this._stateHtml(st, i)).join("");
  }

  _stateHtml(st, i) {
    const last = this._draft.states.length - 1;
    const body = st.mode === "yaml" ? this._yamlHtml(st, i) : this._builderHtml(st, i);
    return `
      <div class="state">
        <div class="state-head">
          <span class="pri">${i + 1}</span>
          <input class="name" data-field="name" data-si="${i}" type="text" placeholder="state name (e.g. sleep)" value="${esc(st.name)}">
          <div class="mode">
            <button class="seg ${st.mode === "builder" ? "on" : ""}" data-act="mode" data-si="${i}" data-mode="builder">Builder</button>
            <button class="seg ${st.mode === "yaml" ? "on" : ""}" data-act="mode" data-si="${i}" data-mode="yaml">YAML</button>
          </div>
          <button class="icon" title="up" data-act="up" data-si="${i}" ${i === 0 ? "disabled" : ""}>↑</button>
          <button class="icon" title="down" data-act="down" data-si="${i}" ${i === last ? "disabled" : ""}>↓</button>
          <button class="icon del" title="remove state" data-act="del-state" data-si="${i}">🗑</button>
        </div>
        ${body}
        ${this._carryHtml(st, i)}
      </div>`;
  }

  _builderHtml(st, i) {
    const rows = st.builder.sources.map((src, sj) => this._sourceHtml(src, i, sj)).join("");
    return `
      <div class="builder">
        <label class="combine">Combine
          <select data-field="combine" data-si="${i}">
            <option value="or" ${st.builder.combine === "or" ? "selected" : ""}>Any (OR)</option>
            <option value="and" ${st.builder.combine === "and" ? "selected" : ""}>All (AND)</option>
          </select>
        </label>
        <div class="sources">${rows || `<div class="hint">No sources yet.</div>`}</div>
        <button class="btn small" data-act="add-source" data-si="${i}">+ Add source</button>
      </div>`;
  }

  _sourceHtml(src, i, sj) {
    const numeric = src.kind === KIND_NUMERIC;
    return `
      <div class="source">
        <input class="ent" list="ps-entities" data-field="src_entity" data-si="${i}" data-sj="${sj}" type="text" placeholder="entity_id" value="${esc(src.entity_id)}">
        <select data-field="src_kind" data-si="${i}" data-sj="${sj}">
          <option value="state" ${!numeric ? "selected" : ""}>is state</option>
          <option value="numeric" ${numeric ? "selected" : ""}>numeric</option>
        </select>
        ${numeric ? `
          <input class="num" data-field="src_above" data-si="${i}" data-sj="${sj}" type="number" step="any" placeholder="above" value="${src.above ?? ""}">
          <input class="num" data-field="src_below" data-si="${i}" data-sj="${sj}" type="number" step="any" placeholder="below" value="${src.below ?? ""}">
        ` : `
          <input class="states" data-field="src_states" data-si="${i}" data-sj="${sj}" type="text" placeholder="states e.g. on, off" value="${esc((src.states || []).join(", "))}">
          <label class="neg"><input type="checkbox" data-field="src_negate" data-si="${i}" data-sj="${sj}" ${src.negate ? "checked" : ""}> not</label>
        `}
        <input class="for" data-field="src_for" data-si="${i}" data-sj="${sj}" type="number" min="0" placeholder="for s" value="${src.for_seconds ?? ""}">
        <button class="icon del" title="remove source" data-act="del-source" data-si="${i}" data-sj="${sj}">✕</button>
      </div>`;
  }

  _yamlHtml(st, i) {
    return `
      <div class="yaml">
        <textarea data-field="yaml" data-si="${i}" rows="6" spellcheck="false" placeholder="condition: state\n  entity_id: ...\n  state: 'on'">${esc(st.yaml)}</textarea>
        <div class="hint">Native HA condition (YAML or JSON). A single condition or an and/or/not block.</div>
      </div>`;
  }

  _carryHtml(st, i) {
    const g = st.grace;
    const p = st.persist;
    return `
      <div class="carry">
        <div class="carry-row">
          <label class="ck"><input type="checkbox" data-act="toggle-grace" data-si="${i}" ${g ? "checked" : ""}> open-door grace</label>
          ${g ? `
            <input list="ps-entities" data-field="g_door" data-si="${i}" type="text" placeholder="door entity" value="${esc(g.door_entity_id || "")}">
            <input class="sm" data-field="g_open" data-si="${i}" type="text" placeholder="open state" value="${esc(g.open_state || "on")}">
            <input class="for" data-field="g_secs" data-si="${i}" type="number" min="0" placeholder="grace s" value="${g.seconds ?? 300}">
          ` : ""}
        </div>
        <div class="carry-row">
          <label class="ck"><input type="checkbox" data-act="toggle-persist" data-si="${i}" ${p ? "checked" : ""}> persist out-of-window</label>
          ${p ? `
            <input list="ps-entities" data-field="p_window" data-si="${i}" type="text" placeholder="window helper" value="${esc(p.window_entity_id || "")}">
            <input class="sm" data-field="p_winoff" data-si="${i}" type="text" placeholder="off state" value="${esc(p.window_off_state || "off")}">
            <input list="ps-entities" data-field="p_door" data-si="${i}" type="text" placeholder="door entity" value="${esc(p.door_entity_id || "")}">
            <input class="sm" data-field="p_closed" data-si="${i}" type="text" placeholder="closed state" value="${esc(p.closed_state || "off")}">
          ` : ""}
        </div>
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
    switch (act) {
      case "select": this._selected = el.dataset.id; this._status = ""; this._loadDraft(); this.render(); break;
      case "save": this._save(); break;
      case "add-state": this._addState(); break;
      case "del-state": this._delState(si); break;
      case "up": this._moveState(si, -1); break;
      case "down": this._moveState(si, 1); break;
      case "add-source": this._addSource(si); break;
      case "del-source": this._delSource(si, sj); break;
      case "mode": this._draft.states[si].mode = el.dataset.mode; this.render(); break;
      case "toggle-grace":
        this._draft.states[si].grace = this._draft.states[si].grace
          ? null : { door_entity_id: "", open_state: "on", seconds: 300 };
        this.render(); break;
      case "toggle-persist":
        this._draft.states[si].persist = this._draft.states[si].persist
          ? null : { window_entity_id: "", window_off_state: "off", door_entity_id: "", closed_state: "off" };
        this.render(); break;
    }
  }

  _onChange(e) {
    const el = e.target;
    const f = el.dataset.field;
    if (!f) return;
    const si = el.dataset.si !== undefined ? +el.dataset.si : null;
    const sj = el.dataset.sj !== undefined ? +el.dataset.sj : null;
    const d = this._draft;
    const st = si !== null ? d.states[si] : null;
    const val = el.type === "checkbox" ? el.checked : el.value;
    switch (f) {
      case "away_from": d.away_from = val; break;
      case "away_state": d.away_state = val; break;
      case "name": st.name = val; break;
      case "yaml": st.yaml = val; break;
      case "combine": st.builder.combine = val; break;
      case "src_entity": st.builder.sources[sj].entity_id = val; break;
      case "src_kind": st.builder.sources[sj].kind = val; this.render(); break;
      case "src_states": st.builder.sources[sj].states = val.split(",").map((x) => x.trim()).filter(Boolean); break;
      case "src_negate": st.builder.sources[sj].negate = val; break;
      case "src_above": st.builder.sources[sj].above = val === "" ? null : Number(val); break;
      case "src_below": st.builder.sources[sj].below = val === "" ? null : Number(val); break;
      case "src_for": st.builder.sources[sj].for_seconds = val === "" ? null : Number(val); break;
      case "g_door": st.grace.door_entity_id = val; break;
      case "g_open": st.grace.open_state = val; break;
      case "g_secs": st.grace.seconds = Number(val); break;
      case "p_window": st.persist.window_entity_id = val; break;
      case "p_winoff": st.persist.window_off_state = val; break;
      case "p_door": st.persist.door_entity_id = val; break;
      case "p_closed": st.persist.closed_state = val; break;
    }
  }

  _css() {
    return `
      :host { display:block; padding:16px; color:var(--primary-text-color);
        font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      .wrap { max-width:880px; margin:0 auto; }
      .tabs { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
      .tab { background:var(--card-background-color); color:var(--primary-text-color);
        border:1px solid var(--divider-color); border-radius:18px; padding:6px 14px; cursor:pointer; }
      .tab.active { background:var(--primary-color); color:var(--text-primary-color, #fff); border-color:var(--primary-color); }
      .head { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
      .livebox { display:flex; flex-direction:column; }
      .livestate { font-size:24px; font-weight:600; text-transform:capitalize; }
      .livesub { font-size:12px; color:var(--secondary-text-color); }
      .grow { flex:1; }
      .status { background:var(--secondary-background-color); border-radius:8px; padding:8px 12px; margin-bottom:12px; font-size:13px; }
      .state { background:var(--card-background-color); border:1px solid var(--divider-color);
        border-radius:12px; padding:12px; margin-bottom:12px; }
      .state-head { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
      .pri { width:22px; height:22px; border-radius:50%; background:var(--secondary-background-color);
        display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:600; flex:none; }
      .name { flex:1; }
      input, select, textarea { background:var(--secondary-background-color); color:var(--primary-text-color);
        border:1px solid var(--divider-color); border-radius:6px; padding:6px 8px; font-size:13px; box-sizing:border-box; }
      textarea { width:100%; font-family:ui-monospace,Menlo,monospace; resize:vertical; }
      .mode { display:flex; }
      .seg { border:1px solid var(--divider-color); background:var(--secondary-background-color);
        color:var(--secondary-text-color); padding:5px 10px; cursor:pointer; font-size:12px; }
      .seg:first-child { border-radius:6px 0 0 6px; } .seg:last-child { border-radius:0 6px 6px 0; border-left:none; }
      .seg.on { background:var(--primary-color); color:var(--text-primary-color,#fff); border-color:var(--primary-color); }
      .icon { background:none; border:none; cursor:pointer; font-size:15px; color:var(--secondary-text-color); padding:4px; }
      .icon[disabled] { opacity:.3; cursor:default; }
      .icon.del:hover { color:var(--error-color, #db4437); }
      .builder { display:flex; flex-direction:column; gap:8px; }
      .combine { font-size:12px; color:var(--secondary-text-color); display:flex; align-items:center; gap:8px; }
      .sources { display:flex; flex-direction:column; gap:6px; }
      .source { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
      .source .ent { flex:1; min-width:160px; }
      .source .num, .source .for { width:90px; }
      .source .states { flex:1; min-width:120px; }
      .neg { font-size:12px; color:var(--secondary-text-color); display:flex; align-items:center; gap:4px; white-space:nowrap; }
      .carry { margin-top:10px; border-top:1px dashed var(--divider-color); padding-top:8px; display:flex; flex-direction:column; gap:6px; }
      .carry-row { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
      .ck { font-size:12px; display:flex; align-items:center; gap:6px; white-space:nowrap; }
      .sm { width:90px; }
      .away { background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:12px; padding:12px; margin-top:8px; }
      .away-title { font-size:12px; color:var(--secondary-text-color); margin-bottom:8px; }
      .away label { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--secondary-text-color); margin-bottom:8px; }
      .btn { background:var(--secondary-background-color); color:var(--primary-text-color);
        border:1px solid var(--divider-color); border-radius:8px; padding:8px 14px; cursor:pointer; font-size:13px; }
      .btn.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); border-color:var(--primary-color); }
      .btn.small { align-self:flex-start; padding:5px 10px; font-size:12px; }
      .btn.add-state { width:100%; margin-bottom:12px; }
      .hint { font-size:11px; color:var(--secondary-text-color); }
      .empty { text-align:center; color:var(--secondary-text-color); margin-top:40px; line-height:1.7; }
    `;
  }
}

customElements.define("person-state-panel", PersonStatePanel);
