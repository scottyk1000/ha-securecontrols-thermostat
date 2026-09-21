/*
 * Secure Controls schedule card
 * Weekly schedule editor for Secure programmers (H3747 / C1727), served by the
 * securecontrols_thermostat integration. Reads each zone's schedule from its calendar
 * entity and saves through securecontrols_thermostat.set_schedule, which writes to the
 * programmer and reads it back to confirm.
 *
 *   type: custom:securecontrols-schedule-card
 *   entity: calendar.kitchen_schedule      # optional; without it, all zones get tabs
 */
(() => {
  const DOMAIN = "securecontrols_thermostat";
  const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  const SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const MAX_PERIODS = 6;
  const MIN_T = 5;
  const MAX_T = 30;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
  const toMin = (t) => {
    const [h, m] = String(t).split(":").map(Number);
    return h * 60 + m;
  };
  const toTime = (m) =>
    `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const fmtTemp = (t) => `${Number(t).toFixed(1)}°`;

  // Temperature bands: setback blues, comfort ambers/oranges, warm reds.
  const BANDS = [
    [13, [66, 133, 244]], // up to 13 °C
    [15.5, [128, 178, 240]], // 13.5-15.5
    [17.5, [245, 176, 50]], // 16-17.5
    [19.5, [240, 128, 32]], // 18-19.5
    [Infinity, [224, 72, 32]], // 20+
  ];
  const tempRgb = (t) => BANDS.find(([max]) => Number(t) <= max)[1];
  const tempColour = (t) => `rgb(${tempRgb(t).join(",")})`;
  // Dark or light label, whichever reads better on that colour.
  const labelColour = (t) => {
    const [r, g, b] = tempRgb(t);
    return 0.299 * r + 0.587 * g + 0.114 * b > 160 ? "rgba(0,0,0,.75)" : "#fff";
  };

  class SecureControlsScheduleCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._zone = null; // selected calendar entity id
      this._day = null; // day being edited
      this._drafts = {}; // entity -> {day: periods} with unsaved edits
      this._copyTo = new Set();
      this._busy = false;
      this._note = null; // {kind: "ok"|"error", text}
      this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
      this.shadowRoot.addEventListener("input", (e) => this._onTime(e));
      this.shadowRoot.addEventListener("change", (e) => this._onTime(e));
    }

    // ---------- Lovelace API ----------

    setConfig(config) {
      this._config = config || {};
      if (this._config.entity) this._zone = this._config.entity;
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      // Don't redraw under the user's fingers while a day is being edited.
      if (!this._day || !this.shadowRoot.innerHTML) this._render();
    }

    getCardSize() {
      return this._day ? 12 : 6;
    }

    static getStubConfig(hass) {
      const first = Object.keys(hass.states).find(
        (id) => id.startsWith("calendar.") && hass.states[id].attributes.zone_type
      );
      return first ? { entity: first } : {};
    }

    // ---------- data ----------

    _zones() {
      if (!this._hass) return [];
      return Object.keys(this._hass.states)
        .filter((id) => {
          const a = this._hass.states[id].attributes;
          return id.startsWith("calendar.") && a.zone_type && "schedule" in a;
        })
        .sort((a, b) => this._hass.states[a].attributes.zone - this._hass.states[b].attributes.zone);
    }

    _attrs() {
      return this._hass?.states[this._zone]?.attributes || null;
    }

    _isHotWater() {
      return this._attrs()?.zone_type === "hot_water";
    }

    _saved(day) {
      return clone(this._attrs()?.schedule?.[day] || []);
    }

    _draft() {
      return (this._drafts[this._zone] = this._drafts[this._zone] || {});
    }

    _periods(day) {
      const d = this._draft();
      return d[day] ? d[day] : this._saved(day);
    }

    _dirtyDays() {
      return Object.keys(this._draft());
    }

    _setDay(day, periods) {
      this._draft()[day] = periods;
    }

    // Value in force at the start of a day (carried over from the last change before it).
    _carryIn(dayIndex) {
      for (let back = 1; back <= 7; back++) {
        const p = this._periods(DAYS[(dayIndex - back + 7) % 7]);
        if (p.length) return p[p.length - 1];
      }
      return null;
    }

    // ---------- validation ----------

    _problems(day) {
      const p = this._periods(day);
      const out = [];
      if (!p.length) out.push("needs at least one period");
      if (p.length > MAX_PERIODS) out.push(`at most ${MAX_PERIODS} periods`);
      for (let i = 1; i < p.length; i++) {
        if (toMin(p[i].start) <= toMin(p[i - 1].start)) {
          out.push("start times must be in order");
          break;
        }
      }
      if (!this._isHotWater()) {
        for (const x of p) {
          const t = Number(x.temperature);
          if (!(t >= MIN_T && t <= MAX_T) || Math.round(t * 2) !== t * 2) {
            out.push(`temperatures must be ${MIN_T}–${MAX_T} °C in 0.5° steps`);
            break;
          }
        }
      }
      return out;
    }

    // ---------- actions ----------

    async _call(service, data) {
      const res = await this._hass.callWS({
        type: "call_service",
        domain: DOMAIN,
        service,
        service_data: data || {},
        target: { entity_id: this._zone },
        return_response: true,
      });
      return res?.response?.[this._zone];
    }

    async _save() {
      const days = this._dirtyDays();
      const bad = days.filter((d) => this._problems(d).length);
      if (bad.length) {
        this._note = { kind: "error", text: `Fix ${bad.map((d) => SHORT[DAYS.indexOf(d)]).join(", ")} first.` };
        return this._render();
      }
      const schedule = {};
      for (const d of days) schedule[d] = this._periods(d);
      this._busy = true;
      this._note = null;
      this._render();
      try {
        const res = await this._call("set_schedule", { schedule });
        this._drafts[this._zone] = {};
        this._lastRead = res?.schedule || null;
        this._note = { kind: "ok", text: "Saved to the programmer ✓" };
        this._day = null;
      } catch (err) {
        this._note = { kind: "error", text: err?.message || String(err) };
      }
      this._busy = false;
      this._render();
    }

    async _refresh() {
      this._busy = true;
      this._note = null;
      this._render();
      try {
        await this._call("get_schedule");
        this._note = { kind: "ok", text: "Up to date with the programmer" };
      } catch (err) {
        this._note = { kind: "error", text: err?.message || String(err) };
      }
      this._busy = false;
      this._render();
    }

    _onClick(e) {
      const el = e.target.closest("button[data-a]");
      if (!el || this._busy) return;
      const a = el.dataset.a;
      const i = Number(el.dataset.i);
      const day = this._day;
      if (a === "zone") {
        this._zone = el.dataset.zone;
        this._day = null;
        this._note = null;
      } else if (a === "day") {
        this._day = this._day === el.dataset.day ? null : el.dataset.day;
        this._copyTo = new Set();
        this._note = null;
      } else if (a === "close") {
        this._day = null;
      } else if (a === "step") {
        const p = this._periods(day);
        const t = Math.min(MAX_T, Math.max(MIN_T, Number(p[i].temperature) + Number(el.dataset.d)));
        p[i].temperature = t;
        this._setDay(day, p);
      } else if (a === "hw") {
        const p = this._periods(day);
        p[i].state = el.dataset.v;
        this._setDay(day, p);
      } else if (a === "del") {
        const p = this._periods(day);
        p.splice(i, 1);
        this._setDay(day, p);
      } else if (a === "add") {
        const p = this._periods(day);
        if (p.length >= MAX_PERIODS) return;
        const last = p[p.length - 1];
        const start = last ? Math.min(toMin(last.start) + 60, 23 * 60 + 59) : 7 * 60;
        const item = this._isHotWater()
          ? { start: toTime(start), state: last?.state === "on" ? "off" : "on" }
          : { start: toTime(start), temperature: last ? Number(last.temperature) : 18 };
        p.push(item);
        this._setDay(day, p);
      } else if (a === "copyday") {
        const d = el.dataset.day;
        this._copyTo.has(d) ? this._copyTo.delete(d) : this._copyTo.add(d);
      } else if (a === "copyset") {
        const set = { weekdays: DAYS.slice(0, 5), weekend: DAYS.slice(5), all: DAYS }[el.dataset.set];
        this._copyTo = new Set(set.filter((d) => d !== day));
      } else if (a === "copy") {
        const src = this._periods(day);
        for (const d of this._copyTo) this._setDay(d, clone(src));
        this._note = { kind: "ok", text: `Copied to ${[...this._copyTo].map((d) => SHORT[DAYS.indexOf(d)]).join(", ")} (not saved yet)` };
        this._copyTo = new Set();
      } else if (a === "revert") {
        this._drafts[this._zone] = {};
        this._note = null;
      } else if (a === "save") {
        return this._save();
      } else if (a === "refresh") {
        return this._refresh();
      }
      this._render();
    }

    // Typing in a time box must not redraw the editor (that would close the picker and
    // lose focus), so only the parts that depend on it are refreshed.
    _onTime(e) {
      const el = e.target;
      if (el.dataset?.a !== "time" || !this._day || !el.value) return;
      const p = this._periods(this._day);
      const i = Number(el.dataset.i);
      if (!p[i] || p[i].start === el.value) return;
      p[i].start = el.value;
      this._setDay(this._day, p);
      this._refreshParts();
    }

    _refreshParts() {
      const root = this.shadowRoot;
      const week = root.querySelector(".week");
      const problems = root.querySelector(".problems");
      const actions = root.querySelector(".actions");
      if (week) week.innerHTML = this._weekHtml();
      if (problems) problems.innerHTML = this._problemsHtml();
      if (actions) actions.innerHTML = this._actionsHtml();
    }

    // ---------- rendering ----------

    _bar(dayIndex) {
      const hw = this._isHotWater();
      const p = this._periods(DAYS[dayIndex]);
      const segs = [];
      let cur = this._carryIn(dayIndex);
      let from = 0;
      const same = (a, b) => (hw ? a.state === b.state : Number(a.temperature) === Number(b.temperature));
      for (const x of p) {
        if (cur && same(cur, x)) continue; // no change: keep one segment
        const at = toMin(x.start);
        if (at > from && cur) segs.push([from, at, cur]);
        from = at;
        cur = x;
      }
      if (cur) segs.push([from, 1440, cur]);
      return segs
        .map(([a, b, v]) => {
          const left = (a / 1440) * 100;
          const width = ((b - a) / 1440) * 100;
          if (hw) {
            if (v.state !== "on") return "";
            return `<div class="seg on" style="left:${left}%;width:${width}%"
              title="${toTime(a)}–${b === 1440 ? "24:00" : toTime(b)} on"></div>`;
          }
          const label = width > 9 ? fmtTemp(v.temperature) : "";
          return `<div class="seg" style="left:${left}%;width:${width}%;background:${tempColour(v.temperature)};color:${labelColour(v.temperature)}"
            title="${toTime(a)}–${b === 1440 ? "24:00" : toTime(b)} ${fmtTemp(v.temperature)}C">${label}</div>`;
        })
        .join("");
    }

    _editor() {
      const day = this._day;
      const hw = this._isHotWater();
      const p = this._periods(day);
      const rows = p
        .map((x, i) => {
          const value = hw
            ? `<div class="toggle">
                 <button class="${x.state === "on" ? "sel" : ""}" data-a="hw" data-i="${i}" data-v="on">On</button>
                 <button class="${x.state !== "on" ? "sel" : ""}" data-a="hw" data-i="${i}" data-v="off">Off</button>
               </div>`
            : `<div class="stepper">
                 <button data-a="step" data-i="${i}" data-d="-0.5" aria-label="Colder">−</button>
                 <span class="temp"><i class="sw" style="background:${tempColour(x.temperature)}"></i>${fmtTemp(x.temperature)}C</span>
                 <button data-a="step" data-i="${i}" data-d="0.5" aria-label="Warmer">+</button>
               </div>`;
          return `<div class="row">
              <input type="time" step="60" value="${esc(x.start)}" data-a="time" data-i="${i}" aria-label="Start time">
              ${value}
              <button class="icon" data-a="del" data-i="${i}" aria-label="Remove period" title="Remove">✕</button>
            </div>`;
        })
        .join("");
      const others = DAYS.filter((d) => d !== day);
      return `<div class="editor">
          <div class="edhead">
            <strong>${SHORT[DAYS.indexOf(day)]}</strong>
            <span class="hint">${hw ? "Each period switches hot water on or off" : "Each period runs until the next one starts"}</span>
            <button class="icon" data-a="close" aria-label="Close">✕</button>
          </div>
          ${rows || `<div class="hint">No periods yet.</div>`}
          <button class="add" data-a="add" ${p.length >= MAX_PERIODS ? "disabled" : ""}>+ Add period</button>
          <div class="problems">${this._problemsHtml()}</div>
          <div class="copy">
            <span class="hint">Copy ${SHORT[DAYS.indexOf(day)]} to</span>
            <div class="chips">
              ${others
                .map(
                  (d) => `<button class="chip ${this._copyTo.has(d) ? "sel" : ""}" data-a="copyday" data-day="${d}">${SHORT[DAYS.indexOf(d)]}</button>`
                )
                .join("")}
            </div>
            <div class="chips">
              <button class="chip" data-a="copyset" data-set="weekdays">Weekdays</button>
              <button class="chip" data-a="copyset" data-set="weekend">Weekend</button>
              <button class="chip" data-a="copyset" data-set="all">All days</button>
              <button class="chip go" data-a="copy" ${this._copyTo.size ? "" : "disabled"}>Copy</button>
            </div>
          </div>
        </div>`;
    }

    _weekHtml() {
      const dirty = this._dirtyDays();
      const hours = [0, 6, 12, 18, 24]
        .map((h) => `<span style="left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}</span>`)
        .join("");
      const days = DAYS.map(
        (d, i) => `<button class="day ${this._day === d ? "sel" : ""}" data-a="day" data-day="${d}"
            aria-label="Edit ${SHORT[i]}">
            <span class="dname">${SHORT[i]}${dirty.includes(d) ? '<i class="dot" title="Unsaved"></i>' : ""}</span>
            <span class="bar">${this._bar(i)}</span>
          </button>`
      ).join("");
      return `<div class="hours"><span class="dname"></span><span class="scale">${hours}</span></div>${days}`;
    }

    _problemsHtml() {
      const problems = this._day ? this._problems(this._day) : [];
      return problems.length ? `<div class="problem">${problems.map(esc).join("; ")}</div>` : "";
    }

    _actionsHtml() {
      const dirty = this._dirtyDays();
      return `
        <button class="ghost" data-a="refresh" ${this._busy ? "disabled" : ""} title="Read the schedule from the programmer">↻ Refresh</button>
        <span class="spacer"></span>
        ${dirty.length ? `<button class="ghost" data-a="revert" ${this._busy ? "disabled" : ""}>Discard</button>` : ""}
        <button class="primary" data-a="save" ${!dirty.length || this._busy ? "disabled" : ""}>
          ${this._busy ? "Saving…" : dirty.length ? `Save ${dirty.length} day${dirty.length > 1 ? "s" : ""}` : "Saved"}
        </button>`;
    }

    _render() {
      if (!this._config) return;
      const zones = this._zones();
      if (!this._zone || !zones.includes(this._zone)) this._zone = this._config.entity || zones[0] || null;
      const attrs = this._attrs();
      let body;
      if (!this._hass) {
        body = "";
      } else if (!attrs) {
        body = `<div class="hint pad">No Secure Controls schedules found${
          this._config.entity ? ` for ${esc(this._config.entity)}` : ""
        }.</div>`;
      } else if (!attrs.schedule) {
        body = `<div class="hint pad">Waiting for ${esc(attrs.zone_name)}'s schedule from the programmer…</div>`;
      } else {
        body = `
          <div class="week">${this._weekHtml()}</div>
          ${this._day ? this._editor() : `<div class="hint pad">Tap a day to edit it.</div>`}
          <div class="actions">${this._actionsHtml()}</div>`;
      }
      const tabs =
        !this._config.entity && zones.length > 1
          ? `<div class="tabs">${zones
              .map(
                (z) => `<button class="tab ${z === this._zone ? "sel" : ""}" data-a="zone" data-zone="${z}">${esc(
                  this._hass.states[z].attributes.zone_name
                )}</button>`
              )
              .join("")}</div>`
          : "";
      const title = this._config.title ?? (this._config.entity && attrs ? `${attrs.zone_name} schedule` : "Heating schedule");
      const note = this._note
        ? `<div class="note ${this._note.kind}" role="status">${esc(this._note.text)}</div>`
        : "";
      this.shadowRoot.innerHTML = `<style>${STYLE}</style>
        <ha-card>
          <div class="head"><span class="title">${esc(title)}</span></div>
          ${tabs}${body}${note}
        </ha-card>`;
    }
  }

  const STYLE = `
    :host { display:block; }
    ha-card { display:block; padding:12px 16px 16px; container-type:inline-size; }
    button { font:inherit; color:inherit; cursor:pointer; }
    button:disabled { cursor:default; opacity:.45; }
    .head { display:flex; align-items:center; margin-bottom:8px; }
    .title { font-size:1.15em; font-weight:500; }
    .hint { color:var(--secondary-text-color); font-size:.9em; }
    .pad { padding:10px 0 4px; }
    .tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
    .tab, .chip { border:1px solid var(--divider-color); background:none; border-radius:16px; padding:4px 12px; }
    .tab.sel, .chip.sel { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color, #fff); }
    .week { display:flex; flex-direction:column; gap:4px; }
    .hours, .day { display:grid; grid-template-columns:44px 1fr; align-items:center; gap:8px; }
    .hours .scale { position:relative; height:14px; font-size:.7em; color:var(--secondary-text-color); }
    .hours .scale span { position:absolute; transform:translateX(-50%); }
    .hours .scale span:first-child { transform:none; }
    .hours .scale span:last-child { transform:translateX(-100%); }
    .day { background:none; border:none; padding:2px 0; text-align:left; border-radius:6px; }
    .day.sel .bar { outline:2px solid var(--primary-color); outline-offset:1px; }
    .dname { font-size:.9em; display:flex; align-items:center; gap:4px; }
    .dot { width:7px; height:7px; border-radius:50%; background:var(--warning-color, #ff9800); display:inline-block; }
    .bar { position:relative; height:26px; border-radius:6px; overflow:hidden;
      background:var(--secondary-background-color, rgba(127,127,127,.12)); }
    .seg { position:absolute; top:0; bottom:0; display:flex; align-items:center; justify-content:center;
      font-size:.75em; font-weight:500; white-space:nowrap; overflow:hidden;
      border-right:1px solid var(--card-background-color, #fff); box-sizing:border-box; }
    .seg.on { background:var(--primary-color); }
    .editor { margin-top:12px; padding:12px; border:1px solid var(--divider-color); border-radius:10px;
      display:flex; flex-direction:column; gap:8px; }
    .edhead { display:flex; align-items:center; gap:8px; }
    .edhead .hint { flex:1; }
    .row { display:flex; align-items:center; gap:8px; }
    input[type=time] { font:inherit; color:var(--primary-text-color); background:var(--card-background-color);
      border:1px solid var(--divider-color); border-radius:6px; padding:4px 6px; flex:none; }
    .stepper { display:flex; align-items:center; gap:4px; flex:none; }
    .stepper button, .icon { width:30px; height:30px; flex:none; border-radius:50%; border:1px solid var(--divider-color);
      background:none; font-size:1.1em; line-height:1; }
    .icon { border:none; font-size:.95em; color:var(--secondary-text-color); margin-left:auto; }
    .temp { min-width:64px; white-space:nowrap; display:inline-flex; align-items:center; justify-content:center; gap:6px; font-weight:600; font-variant-numeric:tabular-nums; }
    .sw { width:10px; height:10px; border-radius:50%; display:inline-block; }
    .toggle { display:flex; border:1px solid var(--divider-color); border-radius:16px; overflow:hidden; }
    .toggle button { border:none; background:none; padding:4px 14px; }
    .toggle button.sel { background:var(--primary-color); color:var(--text-primary-color, #fff); }
    .add { align-self:flex-start; border:1px dashed var(--divider-color); background:none; border-radius:16px; padding:4px 12px; }
    .problem { color:var(--error-color, #db4437); font-size:.9em; }
    .copy { border-top:1px solid var(--divider-color); padding-top:8px; display:flex; flex-direction:column; gap:6px; }
    .chips { display:flex; gap:6px; flex-wrap:wrap; }
    .chip.go { border-color:var(--primary-color); color:var(--primary-color); }
    .actions { display:flex; align-items:center; gap:8px; margin-top:12px; }
    .spacer { flex:1; }
    .primary { background:var(--primary-color); color:var(--text-primary-color, #fff); border:none;
      border-radius:18px; padding:7px 16px; font-weight:500; }
    .ghost { background:none; border:none; color:var(--primary-color); padding:6px 8px; }
    .note { margin-top:10px; font-size:.9em; }
    .note.ok { color:var(--success-color, #43a047); }
    @container (max-width: 340px) {
      .sw { display:none; }
      .temp { min-width:50px; }
      .row { gap:4px; }
      .editor { padding:10px 8px; }
    }
    .note.error { color:var(--error-color, #db4437); }
  `;

  if (!customElements.get("securecontrols-schedule-card")) {
    customElements.define("securecontrols-schedule-card", SecureControlsScheduleCard);
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === "securecontrols-schedule-card")) {
    window.customCards.push({
      type: "securecontrols-schedule-card",
      name: "Secure Controls schedule",
      description: "Edit the weekly heating and hot water schedules on a Secure programmer.",
    });
  }
})();
