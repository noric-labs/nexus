// Client-side race state, built only from the public API (/stream payloads,
// /api/state, /api/lanes/{id}). No DOM here, so it is testable under node.

export class Model {
  constructor() {
    this.lanes = new Map();
    this.heat = null;
    this.config = { lap_points: 20, weights: {}, season_points: [25, 18, 15, 12, 10, 8, 6, 4, 2, 1] };
    this.standings = { lanes: [], constructors: [], games: [], season: [] };
    this.scores = {};
    this.offset = 0;         // server clock minus local clock (ms)
    this.seen = new Set();   // failure event ids (dedupe across SSE replays)
    this.failures = [];      // failures of the current heat, oldest first
    this.tickers = [];       // ticker events of the current heat
    this.podium = null;      // from the `pit` event
    this.focus = null;       // last wall focus {lane_id, ts}
    this.states = 0;
  }

  get lapPoints() { return this.config.lap_points || 20; }

  applyState(s, now = Date.now()) {
    this.states++;
    if (s.server_time) this.offset = Date.parse(s.server_time) - now;
    if (s.config) this.config = { ...this.config, ...s.config };
    const prev = this.heat && this.heat.id;
    if (s.heat) this.heat = s.heat;
    const ids = new Set();
    for (const l of s.lanes || []) {
      ids.add(l.lane_id);
      const cur = this.lanes.get(l.lane_id);
      this.lanes.set(l.lane_id, cur ? Object.assign(cur, l) : { ...l });
    }
    if (s.full) for (const id of [...this.lanes.keys()]) if (!ids.has(id)) this.lanes.delete(id);
    this.scores = s.scores || {};
    if (s.standings) this.standings = s.standings;
    const pos = new Map((this.standings.lanes || []).map(e => [e.lane_id, e.pos]));
    for (const e of this.standings.lanes || []) {
      if (!this.lanes.has(e.lane_id)) this.lanes.set(e.lane_id, { lane_id: e.lane_id, game: "", fault: "", status: "running" });
    }
    for (const [id, l] of this.lanes) {
      l.score = this.scores[id] ?? 0;
      l.pos = pos.get(id) || 0;
      const laps = l.score / this.lapPoints;
      l.laps = Math.floor(laps);
      l.lap_frac = laps - l.laps;
    }
    if (prev && this.heat && prev !== this.heat.id) this.resetHeat();
  }

  resetHeat() {
    this.failures = [];
    this.tickers = [];
    this.podium = null;
  }

  /** Returns the lane if the failure is new (not a replay we already saw). */
  applyFailure(f) {
    if (this.seen.has(f.event_id)) return null;
    this.seen.add(f.event_id);
    if (this.seen.size > 20000) this.seen = new Set([...this.seen].slice(-10000));
    if (!this.heat || f.heat_id === this.heat.id) {
      this.failures.push(f);
      if (this.failures.length > 3000) this.failures.splice(0, 1000);
    }
    let l = this.lanes.get(f.lane_id);
    if (!l) {
      l = { lane_id: f.lane_id, game: f.game, fault: f.fault, model: f.model, status: "running", score: 0, pos: 0 };
      this.lanes.set(f.lane_id, l);
    }
    if (f.scored) l.score = f.lane_score;
    l.last_failure = { type: f.type, detail: f.detail, ts: f.ts, points: f.points };
    return l;
  }

  /** Merge failures from /api/lanes/{id} (for attract mode after a reload). */
  mergeFailures(list) {
    let n = 0;
    for (const f of list || []) {
      if (this.seen.has(f.event_id) || !this.heat || f.heat_id !== this.heat.id) continue;
      this.seen.add(f.event_id);
      this.failures.push({ ...f, lane_score: f.lane_score ?? 0 });
      n++;
    }
    if (n) this.failures.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
    return n;
  }

  applyTicker(t) {
    if (!this.heat || t.heat_id === this.heat.id) this.tickers.push(t);
    if (this.tickers.length > 500) this.tickers.splice(0, 100);
  }

  applyHeat(h) {
    const changed = !this.heat || this.heat.id !== h.id;
    this.heat = h;
    if (changed) this.resetHeat();
  }

  applyPit(p) {
    if (p.heat) this.heat = p.heat;
    this.podium = p.podium || [];
  }

  phase() { return this.heat ? this.heat.phase : "race"; }

  /** Milliseconds left in the current phase, by the server clock. */
  remaining(now = Date.now()) {
    const h = this.heat;
    if (!h) return 0;
    const end = Date.parse(h.phase === "pit" ? h.pit_ends_at : h.ends_at);
    if (isNaN(end)) return h.remaining_ms || 0;
    return Math.max(0, end - (now + this.offset));
  }

  /** Lanes in finishing order: ranked participants first, then the rest by id. */
  ranked() {
    return [...this.lanes.values()].sort((a, b) =>
      ((a.pos || 1e9) - (b.pos || 1e9)) || String(a.lane_id).localeCompare(String(b.lane_id)));
  }

  leader() {
    const e = (this.standings.lanes || [])[0];
    return e && e.score > 0 ? e : null;
  }

  gap(id) {
    const lead = this.leader();
    const l = this.lanes.get(id);
    if (!lead || !l || !(l.score > 0)) return null;
    return lead.score - l.score;
  }

  counts() {
    const c = { running: 0, stale: 0, ended: 0, total: this.lanes.size };
    for (const l of this.lanes.values()) c[l.status] = (c[l.status] || 0) + 1;
    return c;
  }

  typeCounts() {
    const c = {};
    for (const f of this.failures) c[f.type] = (c[f.type] || 0) + 1;
    return c;
  }

  /**
   * The heat's top moments for attract mode, in race order: the biggest
   * scoring hits, new clusters, the first blood and lead changes.
   */
  moments(n = 8) {
    const tick = new Map();
    for (const t of this.tickers) {
      const k = t.lane_id;
      if (!tick.has(k)) tick.set(k, []);
      tick.get(k).push(t);
    }
    const scored = this.failures.filter(f => f.scored && f.points > 0 || f.new_cluster);
    const first = scored.find(f => f.scored && f.points > 0);
    const rated = scored.map(f => {
      const ts = Date.parse(f.ts);
      const near = (tick.get(f.lane_id) || []).filter(t => Math.abs(Date.parse(t.ts) - ts) < 3000);
      const lead = near.find(t => t.kind === "lead_change");
      let w = f.points + (f.new_cluster ? 3 : 0) + (lead ? 4 : 0) + (f === first ? 5 : 0);
      const tags = [];
      if (f === first) tags.push("FIRST BLOOD");
      if (f.new_cluster) tags.push("NEW CLUSTER");
      if (lead) tags.push("LEAD CHANGE");
      return { f, w, tags };
    });
    rated.sort((a, b) => b.w - a.w);
    return rated.slice(0, n).sort((a, b) => Date.parse(a.f.ts) - Date.parse(b.f.ts));
  }
}
