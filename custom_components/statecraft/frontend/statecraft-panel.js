// Statecraft sidebar panel. Vanilla web component, no build step.
// Reads/writes composite-state config via the statecraft/* websocket
// commands and renders an editor that matches the HA look via theme vars.

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

const KIND_STATE = "state";
const KIND_NUMERIC = "numeric";
const KIND_TIME = "time";
const KIND_GROUP = "group";

// Compact human duration: 180 -> "3m", 3600 -> "1h", 90 -> "1m 30s".
function humanDur(secs) {
  const n = Number(secs);
  if (!n || n <= 0) return "";
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = Math.round(n % 60);
  return [h && `${h}h`, m && `${m}m`, s && `${s}s`].filter(Boolean).join(" ");
}

function isPresencePreset(src) {
  return src.attribute === "presence"
    && Array.isArray(src.states) && src.states.length === 1 && src.states[0] === "home";
}

// The "match" operator shown in the dropdown, derived from the stored fields.
function opOf(src) {
  if (isPresencePreset(src)) return src.negate ? "is_away" : "is_home";
  if (src.kind === KIND_NUMERIC) return src.above != null ? "above" : "below";
  return src.negate ? "is_not" : "is";
}

function applyOp(src, op) {
  const wasPreset = isPresencePreset(src);
  switch (op) {
    case "is": src.kind = KIND_STATE; src.negate = false; break;
    case "is_not": src.kind = KIND_STATE; src.negate = true; break;
    case "above": src.kind = KIND_NUMERIC; if (src.above == null) src.above = src.below ?? 0; src.below = null; break;
    case "below": src.kind = KIND_NUMERIC; if (src.below == null) src.below = src.above ?? 0; src.above = null; break;
    case "is_home": src.kind = KIND_STATE; src.negate = false; src.attribute = "presence"; src.states = ["home"]; break;
    case "is_away": src.kind = KIND_STATE; src.negate = true; src.attribute = "presence"; src.states = ["home"]; break;
  }
  // leaving the home shortcut for a plain match: clear the presence preset
  if (wasPreset && op !== "is_home" && op !== "is_away") { src.attribute = undefined; src.states = []; }
}

// Plain-language echo of one node, recursively.
function nodeText(node) {
  const kind = node.kind || KIND_STATE;
  if (kind === KIND_GROUP) {
    const inner = (node.sources || []).map(nodeText).join(node.combine === "and" ? " and " : " or ");
    return `${node.negate ? "not " : ""}(${inner || "…"})`;
  }
  if (kind === KIND_TIME) {
    const bits = [];
    if (node.after) bits.push(`after ${node.after}`);
    if (node.before) bits.push(`before ${node.before}`);
    return `time ${bits.join(" ") || "…"}`;
  }
  const ent = node.entity_id || "…";
  const forp = node.for_seconds ? ` for ${humanDur(node.for_seconds)}` : "";
  const op = opOf(node);
  if (op === "is_home") return `${ent} is home`;
  if (op === "is_away") return `${ent} is away`;
  const field = node.attribute ? `${ent}.${node.attribute}` : ent;
  if (kind === KIND_NUMERIC) {
    const b = node.above != null ? `above ${node.above}` : node.below != null ? `below ${node.below}` : "…";
    return `${field} ${b}${forp}`;
  }
  const v = (node.states || []).join(" or ") || "…";
  return `${field} ${node.negate ? "is not" : "is"} ${v}${forp}`;
}

function builderText(b) {
  if (!b || !b.sources || !b.sources.length) return "";
  const joiner = b.combine === "and" ? " · and " : " · or ";
  return b.sources.map(nodeText).join(joiner);
}

// stored state -> editable draft state (deep clone; sources may be a tree)
function toBuilder(rows) {
  return rows
    ? { combine: rows.combine || "or", sources: JSON.parse(JSON.stringify(rows.sources || [])) }
    : { combine: "or", sources: [] };
}

function toEditorState(s) {
  const hasBuilder = !!s.builder;
  return {
    name: s.name || "",
    icon: s.icon || "",
    mode: hasBuilder ? "builder" : "yaml",
    builder: toBuilder(s.builder),
    yaml: hasBuilder ? "" : JSON.stringify(s.condition ?? {}, null, 2),
    hold: s.hold
      ? {
          mode: s.hold_builder ? "builder" : "yaml",
          builder: toBuilder(s.hold_builder),
          yaml: s.hold_builder ? "" : JSON.stringify(s.hold ?? {}, null, 2),
        }
      : null,
  };
}

function newState() {
  return { name: "", icon: "", mode: "builder", builder: { combine: "or", sources: [] }, yaml: "", hold: null };
}

function newHold() {
  return { mode: "builder", builder: { combine: "and", sources: [] }, yaml: "" };
}

function newSource() {
  return { kind: KIND_STATE, entity_id: "", states: [], negate: false, above: null, below: null, for_seconds: null };
}

function newTime() {
  return { kind: KIND_TIME, after: "", before: "" };
}

function newGroup() {
  return { kind: KIND_GROUP, combine: "and", negate: false, sources: [] };
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
    this._debugTimer = null;
  }

  set narrow(v) {
    // Reflect to a host attribute; CSS shows the menu button only when narrow.
    this.toggleAttribute("narrow", !!v);
  }

  // HA passes the panel_custom config here; read the version out of it and,
  // if the header is already drawn, patch it in place (avoids a full re-render).
  set panel(panel) {
    this._version = panel && panel.config ? panel.config.version : undefined;
    const el = this.shadowRoot && this.shadowRoot.querySelector(".topbar-ver");
    if (el) el.textContent = this._version ? `v${this._version}` : "";
  }

  connectedCallback() {
    if (this._debug) this._startDebugTimer();
  }

  disconnectedCallback() {
    this._stopDebugTimer();
  }

  // While debug is on, tick once a second so `for:` countdowns decrement and
  // time-window rows flip even when no HA state has changed. This patches only
  // the debug blocks in place — it never rebuilds the panel — so the scroll
  // container stays put and the scrollbar doesn't flash.
  _startDebugTimer() {
    if (this._debugTimer) return;
    this._debugTimer = setInterval(() => this._refreshDebug(), 1000);
  }

  _refreshDebug() {
    if (!this.shadowRoot || !this._draft) return;
    const slots = this.shadowRoot.querySelectorAll("[data-dbg]");
    if (!slots.length) return;
    const cur = this._current();
    const live = this._liveSubject() || (cur && cur.live ? cur.live : {});
    slots.forEach((slot) => {
      const st = this._draft.states[+slot.dataset.dbg];
      if (st) slot.innerHTML = this._debugInner(st, live);
    });
  }

  _stopDebugTimer() {
    if (this._debugTimer) {
      clearInterval(this._debugTimer);
      this._debugTimer = null;
    }
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
  _addState() { this._draft.states.push(newState()); this.render(); }
  _delState(i) { this._draft.states.splice(i, 1); this.render(); }
  _moveState(i, d) {
    const j = i + d;
    if (j < 0 || j >= this._draft.states.length) return;
    const a = this._draft.states;
    [a[i], a[j]] = [a[j], a[i]];
    this.render();
  }
  _toggleHold(si) {
    const st = this._draft.states[si];
    st.hold = st.hold ? null : newHold();
    this.render();
  }

  // ---- rendering ----------------------------------------------------------
  render() {
    if (!this.shadowRoot) return;
    // .content is the scroll container; replacing innerHTML resets its scroll to
    // the top. Preserve it so a live update (debug ticks once a second) doesn't
    // yank the page back up while the user is reading a scope further down.
    const prev = this.shadowRoot.querySelector(".content");
    const scroll = prev ? prev.scrollTop : 0;
    this.shadowRoot.innerHTML = `<style>${this._css()}</style>${this._html()}`;
    this._wire();
    const next = this.shadowRoot.querySelector(".content");
    if (next && scroll) next.scrollTop = scroll;
    this._lastSig = this._liveSig();
  }

  _entityDatalist() {
    const ids = this._hass ? Object.keys(this._hass.states).sort() : [];
    return `<datalist id="ps-entities">${ids.map((id) => `<option value="${esc(id)}">`).join("")}</datalist>`;
  }

  _personName(s) {
    return (s && s.live && s.live.attributes && s.live.attributes.friendly_name) || (s && s.subject) || "";
  }

  _scopeIcon(s) {
    // Person scopes: the person's avatar if it has one, else a person glyph.
    // Custom scopes: the icon chosen at creation, else the default state glyph.
    if ((s.scope_type || "person") === "person") {
      const live = this._hass && this._hass.states ? this._hass.states[s.subject] : null;
      const pic = live && live.attributes ? live.attributes.entity_picture : null;
      if (pic) return `<img class="pic" src="${esc(pic)}" alt="">`;
      return `<ha-icon class="sic" icon="mdi:account"></ha-icon>`;
    }
    return `<ha-icon class="sic" icon="${esc(s.icon || "mdi:state-machine")}"></ha-icon>`;
  }

  _topbar() {
    // A custom panel gets no HA toolbar, so render one that matches HA's own app
    // header. The menu button is CSS-hidden on desktop (:host([narrow]) shows
    // it), so the sidebar is reachable on mobile without a button on desktop.
    const ver = this._version ? `v${esc(this._version)}` : "";
    return `<div class="topbar">
      <div class="menu-btn" data-act="menu" title="Open menu"><ha-icon icon="mdi:menu"></ha-icon></div>
      <h1 class="topbar-title">Statecraft</h1>
      <span class="topbar-ver">${ver}</span>
    </div>`;
  }

  _html() {
    if (!this._subjects.length) {
      return `${this._topbar()}<div class="content"><div class="wrap"><div class="empty">No people configured yet.<br>
        Add one via <b>Settings → Devices &amp; Services → Statecraft → Add</b>, then return here.</div></div></div>`;
    }
    const cur = this._current();
    const liveNow = this._liveSubject();
    const live = liveNow || (cur && cur.live ? cur.live : {});
    const people = this._subjects
      .map((s) => `
        <button class="person ${s.entry_id === this._selected ? "active" : ""}" data-act="select" data-id="${esc(s.entry_id)}" title="${esc(s.subject || "")}">
          ${this._scopeIcon(s)}
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
      ${this._topbar()}
      <div class="content">
        ${this._entityDatalist()}
        <div class="layout">
          <aside class="people">
            <div class="people-title">Scopes</div>
            ${people}
          </aside>
          <main class="detail">${detail}</main>
        </div>
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
          <label class="iconfield" title="Optional mdi icon shown while this state is active. Note: a person with a profile picture keeps showing the picture — the icon appears where no picture is rendered. Leave blank for the default.">
            <ha-icon class="iconpreview" icon="${esc(st.icon || "mdi:account")}"></ha-icon>
            <input class="icon-input" data-field="icon" data-si="${i}" type="text" placeholder="mdi:sleep" value="${esc(st.icon)}">
          </label>
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
        ${this._debug ? this._debugBlock(st, live, i) : ""}
      </div>`;
  }

  // ---- live debug ---------------------------------------------------------
  _nowHM() {
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    try {
      return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz }).format(new Date());
    } catch (e) {
      return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date());
    }
  }

  _evalNode(node) {
    const kind = node.kind || KIND_STATE;
    if (kind === KIND_GROUP) {
      const results = (node.sources || []).map((n) => this._evalNode(n));
      let pass = node.combine === "and" ? results.every((r) => r.pass) : (results.length ? results.some((r) => r.pass) : true);
      if (node.negate) pass = !pass;
      return { pass, group: true };
    }
    if (kind === KIND_TIME) {
      const now = this._nowHM();
      const a = node.after, b = node.before;
      let pass;
      if (a && b) pass = a <= b ? (now >= a && now < b) : (now >= a || now < b);
      else if (a) pass = now >= a;
      else if (b) pass = now < b;
      else pass = true;
      return { pass, value: now };
    }
    const s = this._hass && this._hass.states ? this._hass.states[node.entity_id] : null;
    if (!node.entity_id) return { value: "—", pass: false, missing: true };
    if (!s) return { value: "unavailable", pass: false, missing: true };
    const raw = node.attribute ? s.attributes[node.attribute] : s.state;
    const val = raw === undefined ? "—" : String(raw);
    let pass;
    if (kind === KIND_NUMERIC) {
      const n = parseFloat(raw);
      pass = !isNaN(n) && (node.above == null || n > node.above) && (node.below == null || n < node.below);
    } else {
      const inSet = (node.states || []).map(String).includes(val);
      pass = node.negate ? !inSet : inSet;
    }
    let pending = null;
    if (pass && node.for_seconds) {
      const held = (Date.now() - new Date(s.last_changed).getTime()) / 1000;
      if (held < node.for_seconds) { pass = false; pending = Math.ceil(node.for_seconds - held); }
    }
    return { value: val, pass, pending };
  }

  _dbgNodeChip(node, active) {
    const kind = node.kind || KIND_STATE;
    const r = this._evalNode(node);
    const okMark = active ? "held" : "✗";
    const okCls = active ? "hold" : "no";
    if (kind === KIND_GROUP) {
      const joiner = `<span class="dbg-op">${node.combine === "and" ? "and" : "or"}</span>`;
      const inner = (node.sources || []).map((n) => this._dbgNodeChip(n, active)).join(joiner) || "…";
      return `<span class="chip grp ${r.pass ? "ok" : okCls}">${node.negate ? "not " : ""}( ${inner} ) ${r.pass ? "✓" : okMark}</span>`;
    }
    if (kind === KIND_TIME) {
      const bits = [];
      if (node.after) bits.push(`≥${node.after}`);
      if (node.before) bits.push(`<${node.before}`);
      return `<span class="chip ${r.pass ? "ok" : okCls}" title="now ${r.value}">time ${bits.join(" ") || "any"} ${r.pass ? "✓" : okMark}</span>`;
    }
    const short = node.entity_id ? (node.entity_id.split(".").slice(1).join(".") || node.entity_id) : "—";
    const label = node.attribute ? `${short}.${node.attribute}` : short;
    let cls, mark, extra = "";
    if (r.missing) { cls = active ? "hold" : "unk"; mark = active ? "held" : "?"; }
    else if (r.pending != null) { cls = okCls; mark = okMark; extra = ` · ${r.pending}s`; }
    else { cls = r.pass ? "ok" : "no"; mark = r.pass ? "✓" : "✗"; }
    return `<span class="chip ${cls}" title="${esc(node.entity_id || "")}">${esc(label)} = ${esc(String(r.value))}${extra} ${mark}</span>`;
  }

  _dbgChips(builder, active) {
    if (!builder.sources.length) return `<span class="dbg-note">no conditions</span>`;
    const joiner = `<span class="dbg-op">${builder.combine === "and" ? "and" : "or"}</span>`;
    return builder.sources.map((n) => this._dbgNodeChip(n, active)).join(joiner);
  }

  // The `data-dbg` slot is a stable wrapper; the timer patches only its inner
  // HTML each second, so the scroll container is never rebuilt (no scrollbar
  // flashing) and the page scroll position is untouched.
  _debugBlock(st, live, i) {
    return `<div class="dbg" data-dbg="${i}">${this._debugInner(st, live)}</div>`;
  }

  _debugInner(st, live) {
    const attrs = (live && live.attributes) || {};
    const engine = attrs[st.name];
    const active = engine === true;
    const verdict = engine === true ? "active" : engine === false ? "inactive" : "unknown";
    const enter = st.mode === "builder"
      ? this._dbgChips(st.builder, active)
      : `<span class="dbg-note">YAML mode — per-row values not shown; see engine verdict</span>`;
    const hold = st.hold && st.hold.mode === "builder"
      ? `<div class="dbg-line"><span class="dbg-k" title="Only latches once the state is already active">hold</span>${this._dbgChips(st.hold.builder, active)}</div>`
      : "";
    return `
        <div class="dbg-line">
          <span class="dbg-k">engine</span>
          <span class="chip ${engine ? "ok" : "no"}">${esc(st.name || "state")} = ${verdict}</span>
          <span class="dbg-note">now: <b>${esc((live && live.state) || "—")}</b></span>
        </div>
        <div class="dbg-line"><span class="dbg-k">enter</span>${enter}</div>
        ${hold}`;
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

  // ---- recursive builder tree helpers -------------------------------------
  _rootBuilder(st, scope) { return scope === "hold" ? st.hold.builder : st.builder; }

  _nodeAt(root, path) {
    if (!path) return root; // "" -> the root group
    let node = root;
    for (const idx of path.split(".")) node = node.sources[+idx];
    return node;
  }

  _parentArr(root, path) {
    const parts = path.split(".");
    const last = +parts.pop();
    let node = root;
    for (const idx of parts) node = node.sources[+idx];
    return { arr: node.sources, index: last };
  }

  _childPath(basePath, j) { return basePath === "" ? `${j}` : `${basePath}.${j}`; }

  // ---- rendering ----------------------------------------------------------
  _builderHtml(b, si, scope) {
    // the root builder is itself a group; render its body at path ""
    return `<div class="builder">${this._groupBody(b, si, scope, "")}</div>`;
  }

  _groupBody(g, si, scope, path) {
    const rows = (g.sources || [])
      .map((node, j) => this._nodeHtml(node, si, scope, this._childPath(path, j)))
      .join("");
    return `
      <label class="combine" title="AND = every row must be true. OR = any one is enough.">Combine
        <select data-field="combine" data-scope="${scope}" data-si="${si}" data-path="${path}">
          <option value="or" ${g.combine === "or" ? "selected" : ""}>Any · OR</option>
          <option value="and" ${g.combine === "and" ? "selected" : ""}>All · AND</option>
        </select>
      </label>
      <div class="sources">${rows || `<div class="hint">No conditions yet — add one below.</div>`}</div>
      ${this._addMenu(si, scope, path)}`;
  }

  _addMenu(si, scope, path) {
    const a = (act, label, title) =>
      `<button class="btn small" data-act="${act}" data-scope="${scope}" data-si="${si}" data-path="${path}" title="${title}">${label}</button>`;
    return `<div class="add-menu">
      ${a("add-cond", "+ Condition", "Test an entity's state or attribute")}
      ${a("add-time", "+ Time", "Match a time-of-day window")}
      ${a("add-group", "+ Group", "A nested ( … ) with its own AND/OR")}
    </div>`;
  }

  _nodeHtml(node, si, scope, path) {
    const kind = node.kind || KIND_STATE;
    if (kind === KIND_GROUP) return this._groupHtml(node, si, scope, path);
    if (kind === KIND_TIME) return this._timeHtml(node, si, scope, path);
    return this._leafHtml(node, si, scope, path);
  }

  _groupHtml(node, si, scope, path) {
    const d = (f, ...kv) => `data-scope="${scope}" data-si="${si}" data-path="${path}"`;
    return `
      <div class="group">
        <div class="group-head">
          <span class="group-tag">group</span>
          <label class="ck" title="Invert this whole group (NOT)"><input type="checkbox" data-act="toggle-gnot" ${d()} ${node.negate ? "checked" : ""}> not</label>
          <div class="grow"></div>
          <button class="icon del" data-act="del-node" ${d()} title="Remove group">✕</button>
        </div>
        ${this._groupBody(node, si, scope, path)}
      </div>`;
  }

  _leafHtml(node, si, scope, path) {
    const d = `data-scope="${scope}" data-si="${si}" data-path="${path}"`;
    const numeric = node.kind === KIND_NUMERIC;
    const op = opOf(node);
    const preset = op === "is_home" || op === "is_away";
    const isPerson = (node.entity_id || "").startsWith("person.");
    const value = preset
      ? `<span class="val muted-cell">home</span>`
      : numeric
        ? `<input class="val" data-field="src_num" ${d} type="number" step="any" placeholder="threshold" value="${(node.above ?? node.below) ?? ""}" title="Numeric threshold">`
        : `<input class="val" data-field="src_states" ${d} type="text" placeholder="e.g. on, off" value="${esc((node.states || []).join(", "))}" title="State value(s). Comma-separated = any of these.">`;
    const attr = preset
      ? ""
      : `<input class="attr" data-field="src_attr" ${d} type="text" placeholder="attr" value="${esc(node.attribute || "")}" title="Optional: match this attribute instead of the state (e.g. presence)">`;
    return `
      <div class="source">
        <input class="ent" list="ps-entities" data-field="src_entity" ${d} type="text" placeholder="entity_id" value="${esc(node.entity_id)}" title="${esc(node.entity_id || "Pick an entity")}">
        ${attr}
        <select class="op" data-field="src_op" ${d} title="How to test the entity">
          <option value="is" ${op === "is" ? "selected" : ""}>is</option>
          <option value="is_not" ${op === "is_not" ? "selected" : ""}>is not</option>
          <option value="above" ${op === "above" ? "selected" : ""}>&gt; above</option>
          <option value="below" ${op === "below" ? "selected" : ""}>&lt; below</option>
          ${isPerson || preset ? `<option value="is_home" ${op === "is_home" ? "selected" : ""}>is home</option><option value="is_away" ${op === "is_away" ? "selected" : ""}>is away</option>` : ""}
        </select>
        ${value}
        <input class="for" data-field="src_for" ${d} type="number" min="0" placeholder="—" value="${node.for_seconds ?? ""}" title="Seconds the condition must hold before it counts (optional)">
        <button class="icon del" title="Remove this condition" data-act="del-node" ${d}>✕</button>
      </div>`;
  }

  _timeHtml(node, si, scope, path) {
    const d = `data-scope="${scope}" data-si="${si}" data-path="${path}"`;
    return `
      <div class="source time-row">
        <span class="tlabel">time</span>
        <label class="tfield">after <input data-field="time_after" ${d} type="time" value="${esc(node.after || "")}" title="From this time (leave blank for none)"></label>
        <label class="tfield">before <input data-field="time_before" ${d} type="time" value="${esc(node.before || "")}" title="Until this time. If before is earlier than after, the window crosses midnight."></label>
        <div class="grow"></div>
        <button class="icon del" title="Remove time" data-act="del-node" ${d}>✕</button>
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
    const scope = el.dataset.scope || "cond";
    const path = el.dataset.path;
    switch (act) {
      case "menu": this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })); break;
      case "select": this._selected = el.dataset.id; this._status = ""; this._loadDraft(); this.render(); break;
      case "save": this._save(); break;
      case "toggle-debug":
        this._debug = !this._debug;
        if (this._debug) this._startDebugTimer(); else this._stopDebugTimer();
        this.render(); break;
      case "add-state": this._addState(); break;
      case "del-state": this._delState(si); break;
      case "up": this._moveState(si, -1); break;
      case "down": this._moveState(si, 1); break;
      case "add-cond": this._addNode(si, scope, path, newSource()); break;
      case "add-time": this._addNode(si, scope, path, newTime()); break;
      case "add-group": this._addNode(si, scope, path, newGroup()); break;
      case "del-node": this._delNode(si, scope, path); break;
      case "toggle-gnot": {
        const g = this._nodeAt(this._rootBuilder(this._draft.states[si], scope), path);
        g.negate = !g.negate; this.render(); break;
      }
      case "toggle-hold": this._toggleHold(si); break;
      case "mode": this._switchMode(si, scope, el.dataset.mode); break;
    }
  }

  _addNode(si, scope, path, node) {
    this._nodeAt(this._rootBuilder(this._draft.states[si], scope), path).sources.push(node);
    this.render();
  }

  _delNode(si, scope, path) {
    const { arr, index } = this._parentArr(this._rootBuilder(this._draft.states[si], scope), path);
    arr.splice(index, 1);
    this.render();
  }

  // Builder <-> YAML: convert the current config through the server so it stays
  // native HA. YAML the builder can't draw keeps the state in YAML mode.
  async _switchMode(si, scope, mode) {
    const st = this._draft.states[si];
    const cur = scope === "hold" ? st.hold : st;
    if (!cur || cur.mode === mode) return;
    try {
      if (mode === "yaml") {
        const res = await this._hass.connection.sendMessagePromise({
          type: "statecraft/to_yaml", combine: cur.builder.combine, sources: cur.builder.sources,
        });
        cur.yaml = res.yaml || "";
        cur.mode = "yaml";
      } else {
        const res = await this._hass.connection.sendMessagePromise({
          type: "statecraft/from_yaml", yaml: cur.yaml || "",
        });
        if (res.representable === false) {
          this._status = "That YAML uses a condition the builder can't show, so it stays in YAML mode.";
          this.render();
          return;
        }
        cur.builder = res.builder || { combine: "or", sources: [] };
        cur.mode = "builder";
      }
      this._status = "";
    } catch (err) {
      this._status = `Convert failed: ${(err && err.message) || err}`;
    }
    this.render();
  }

  _onChange(e) {
    const el = e.target;
    const f = el.dataset.field;
    if (!f) return;
    const si = el.dataset.si !== undefined ? +el.dataset.si : null;
    const scope = el.dataset.scope || "cond";
    const path = el.dataset.path;
    const d = this._draft;
    const st = si !== null ? d.states[si] : null;
    const root = st ? this._rootBuilder(st, scope) : null;
    const node = root && path !== undefined ? this._nodeAt(root, path) : null;
    const val = el.type === "checkbox" ? el.checked : el.value;
    switch (f) {
      case "away_from": d.away_from = val; break;
      case "away_state": d.away_state = val; break;
      case "default_state": d.default_state = val; break;
      case "name": st.name = val; break;
      case "icon":
        st.icon = val;
        // Update the preview in place rather than re-rendering the whole panel
        // for one field. Every other case here leaves rendering to the caller.
        {
          const prev = el.parentElement.querySelector(".iconpreview");
          if (prev) prev.setAttribute("icon", val || "mdi:account");
        }
        break;
      case "yaml": if (scope === "hold") st.hold.yaml = val; else st.yaml = val; break;
      case "combine": node.combine = val; break;  // node is the group at path
      case "src_entity": node.entity_id = val; break;
      case "src_attr": node.attribute = val || undefined; break;
      case "src_op": applyOp(node, val); this.render(); break;
      case "src_states": node.states = val.split(",").map((x) => x.trim()).filter(Boolean); break;
      case "src_num":
        if (node.above != null) node.above = val === "" ? null : Number(val);
        else node.below = val === "" ? null : Number(val);
        break;
      case "src_for": node.for_seconds = val === "" ? null : Number(val); this._refreshSummary(st, si); break;
      case "time_after": node.after = val; break;
      case "time_before": node.before = val; break;
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
      :host { display:flex; flex-direction:column; height:100%; box-sizing:border-box;
        background:var(--primary-background-color); color:var(--primary-text-color);
        font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      * { box-sizing:border-box; }
      .topbar { display:flex; align-items:center; gap:8px; flex:none;
        height:var(--header-height,56px); padding:0 16px;
        background:var(--app-header-background-color, var(--primary-color));
        color:var(--app-header-text-color, var(--text-primary-color, #fff)); }
      .menu-btn { display:none; align-items:center; justify-content:center;
        cursor:pointer; margin-right:8px; --mdc-icon-size:24px; color:inherit; }
      :host([narrow]) .menu-btn { display:flex; }
      .topbar-title { margin:0; flex:1; font-size:20px; font-weight:400; }
      .topbar-ver { font-size:14px; opacity:.7; margin-left:8px; font-variant-numeric:tabular-nums; }
      .content { flex:1; overflow-y:auto; overflow-x:hidden; padding:16px; }
      .wrap { max-width:880px; margin:0 auto; }
      .layout { display:flex; gap:16px; align-items:flex-start; max-width:1120px; margin:0 auto; }
      .people { flex:0 0 240px; display:flex; flex-direction:column; gap:6px;
        background:var(--card-background-color); border:1px solid var(--divider-color);
        border-radius:12px; padding:10px; position:sticky; top:16px; }
      .people-title { font-size:12px; letter-spacing:.04em; text-transform:uppercase;
        color:var(--secondary-text-color); padding:2px 6px 6px; }
      .person { display:flex; align-items:center; gap:10px;
        background:none; color:var(--primary-text-color); border:1px solid transparent;
        border-radius:8px; padding:9px 12px; cursor:pointer; text-align:left; width:100%; }
      .person:hover { background:var(--secondary-background-color); }
      .person.active { background:var(--primary-color); color:var(--text-primary-color,#fff); }
      .person .pic { width:24px; height:24px; border-radius:50%; object-fit:cover; flex:none; }
      .person .sic { --mdc-icon-size:22px; width:24px; height:24px; color:inherit; flex:none; opacity:.9; }
      .pname { font-weight:500; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .pstate { font-size:12px; opacity:.85; text-transform:capitalize; flex:none; }
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
      .iconfield { display:flex; align-items:center; gap:6px; flex:0 1 170px; cursor:help; }
      .iconfield .iconpreview { color:var(--secondary-text-color); flex:none; --mdc-icon-size:20px; }
      .icon-input { flex:1 1 auto; min-width:0; font-family:ui-monospace,Menlo,monospace; font-size:12px; }
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
      .sources { display:flex; flex-direction:column; gap:7px; }
      .source { display:flex; gap:7px; align-items:center; flex-wrap:wrap; }
      .source .ent { flex:1 1 170px; min-width:120px; }
      .source .attr { flex:0 1 96px; min-width:70px; }
      .source .op { flex:0 0 auto; }
      .source .val { flex:1 1 110px; min-width:80px; }
      .source .for { flex:0 0 70px; width:70px; }
      .muted-cell { flex:1 1 110px; color:var(--secondary-text-color); font-size:13px; padding:6px 2px; }
      .time-row .tlabel { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--secondary-text-color); }
      .time-row .tfield { display:flex; align-items:center; gap:5px; font-size:12px; color:var(--secondary-text-color); }
      .add-menu { display:flex; gap:6px; flex-wrap:wrap; }
      .group { border:1px solid var(--divider-color); border-left:3px solid var(--primary-color); border-radius:10px; padding:9px 10px; display:flex; flex-direction:column; gap:8px; background:color-mix(in srgb, var(--primary-color) 5%, transparent); }
      .group-head { display:flex; align-items:center; gap:10px; }
      .group-tag { font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:var(--primary-color); font-weight:700; }
      .summary { font-size:12px; color:var(--secondary-text-color); font-style:italic; margin-top:2px; padding-left:2px; overflow-wrap:anywhere; }
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
      .chip.hold { color:var(--warning-color,#e0a400); background:color-mix(in srgb, var(--warning-color,#e0a400) 15%, transparent); }
      .chip.grp { background:none; border:1px solid var(--divider-color); white-space:normal; }
      .chip.grp.ok { border-color:var(--success-color); }
      .chip.grp.no { border-color:var(--error-color); }
      .chip.grp.hold { border-color:var(--warning-color,#e0a400); }
      .dbg-op { font-size:10px; text-transform:uppercase; letter-spacing:.03em; color:var(--secondary-text-color); margin:0 2px; }
      .btn.add-state { width:100%; margin-bottom:14px; border-style:dashed; }
      .hint { font-size:11.5px; color:var(--secondary-text-color); padding:4px 2px; }
      .empty { text-align:center; color:var(--secondary-text-color); margin-top:40px; line-height:1.7; }
      @media (max-width:720px) {
        .layout { flex-direction:column; }
        .people { position:static; width:100%; flex:none; }
        .away-grid { grid-template-columns:1fr; }
        .head { flex-wrap:wrap; }
        .head .grow { display:none; }
        .state-head { flex-wrap:wrap; }
        .name { flex:1 1 100%; order:-1; }
        .source .ent { flex:1 1 100%; min-width:0; }
        .source .val { flex:1 1 100%; min-width:0; }
        .source .attr { flex:1 1 42%; min-width:0; }
        .source .op { flex:1 1 42%; }
        .source .for { flex:0 0 66px; width:66px; }
        .muted-cell { flex:1 1 100%; }
        .group { padding:8px; }
        .time-row { flex-wrap:wrap; }
      }
    `;
  }
}

// A version-stamped module URL reloads this file after an update, but the
// element name is already registered from the previous load; redefining throws.
// Guard it (the running definition persists until a full page reload).
if (!customElements.get("statecraft-panel")) {
  customElements.define("statecraft-panel", StatecraftPanel);
}
