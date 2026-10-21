package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"sort"
	"sync"
	"time"

	"github.com/noric-labs/nexus/scoreboard/internal/score"
)

const (
	phaseRace = "race"
	phasePit  = "pit"
)

// Server is the scoreboard core: heat cycle, ingest, scoring, standings.
type Server struct {
	cfg     Config
	clock   Clock
	store   *Store
	Hub     *Hub
	Screens *Screens
	Log     *log.Logger

	mu       sync.Mutex
	heat     Heat
	phase    string
	tally    *score.Tally
	lanes    map[string]*Lane
	results  []score.HeatResult // finalized heats, season source
	seasonPt map[string]int     // lane -> season points
	leader   string
	blooded  bool
	dirty    map[string]bool
	dirtyAll bool
}

// New opens the store and restores (or starts) the heat cycle.
func New(cfg Config, clock Clock, store *Store) (*Server, error) {
	if clock == nil {
		clock = RealClock
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	s := &Server{
		cfg: cfg, clock: clock, store: store,
		Hub: NewHub(cfg.RingSize), Screens: NewScreens(), Log: log.Default(),
		lanes: map[string]*Lane{}, dirty: map[string]bool{},
	}
	var err error
	if s.results, err = store.HeatResults(); err != nil {
		return nil, err
	}
	s.recomputeSeason()
	if err := s.restore(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Server) now() time.Time { return s.clock.Now().UTC() }

func (s *Server) restore() error {
	now := s.now()
	h, err := s.store.LastHeat()
	if err != nil {
		return err
	}
	ls, err := s.store.LoadLanes(now.Add(-2 * (s.cfg.Heat.Duration + s.cfg.Pit.Duration)))
	if err != nil {
		return err
	}
	for _, l := range ls {
		s.lanes[l.ID] = l
	}
	switch {
	case h == nil:
		return s.startHeat(1, now, false)
	case now.Before(h.PitEndsAt) && !h.Finalized:
		s.heat = *h
		s.phase = phaseRace
		if !now.Before(h.EndsAt) {
			s.phase = phasePit
		}
		s.tally = score.NewTally(s.cfg.Weights)
		evs, err := s.store.ScoredEvents(h.ID)
		if err != nil {
			return err
		}
		for _, e := range evs {
			s.tally.Add(e.LaneID, e.Type, score.ClusterKey(e.Type, e.StackHash, e.Detail), e.RecvAt)
			s.blooded = true
		}
		s.leader = s.leaderLocked()
		return nil
	default:
		if !h.Finalized {
			// Finalize from persisted events, then start fresh.
			s.heat = *h
			s.tally = score.NewTally(s.cfg.Weights)
			evs, err := s.store.ScoredEvents(h.ID)
			if err != nil {
				return err
			}
			for _, e := range evs {
				s.tally.Add(e.LaneID, e.Type, score.ClusterKey(e.Type, e.StackHash, e.Detail), e.RecvAt)
			}
			if err := s.finalizeLocked(); err != nil {
				return err
			}
		}
		return s.startHeat(h.Seq+1, now, false)
	}
}

// startHeat begins heat seq at t. Caller holds mu (or is in New).
func (s *Server) startHeat(seq int, t time.Time, announce bool) error {
	h := Heat{ID: heatID(seq), Seq: seq, StartedAt: t, EndsAt: t.Add(s.cfg.Heat.Duration)}
	h.PitEndsAt = h.EndsAt.Add(s.cfg.Pit.Duration)
	if err := s.store.InsertHeat(&h); err != nil {
		return err
	}
	s.heat, s.phase = h, phaseRace
	s.tally = score.NewTally(s.cfg.Weights)
	s.leader, s.blooded = "", false
	s.dirtyAll = true
	// Forget lanes not seen for two full cycles.
	cut := t.Add(-2 * (s.cfg.Heat.Duration + s.cfg.Pit.Duration))
	for id, l := range s.lanes {
		if l.LastSeen.Before(cut) {
			delete(s.lanes, id)
		}
		l.LastFailure = nil
	}
	if announce {
		s.Hub.Publish("heat", s.heatViewLocked())
	}
	return nil
}

// Tick advances the heat/pit cycle to the current clock time.
func (s *Server) Tick() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	if s.phase == phaseRace && !now.Before(s.heat.EndsAt) {
		s.phase = phasePit
		s.dirtyAll = true
		s.Hub.Publish("pit", map[string]any{
			"heat":    s.heatViewLocked(),
			"heat_id": s.heat.ID,
			"next_id": heatID(s.heat.Seq + 1),
			"podium":  s.provisionalLocked(),
		})
	}
	if s.phase == phasePit && !now.Before(s.heat.PitEndsAt) {
		if err := s.finalizeLocked(); err != nil {
			s.Log.Printf("finalize %s: %v", s.heat.ID, err)
			return
		}
		if err := s.startHeat(s.heat.Seq+1, now, true); err != nil {
			s.Log.Printf("start heat: %v", err)
		}
	}
}

// participantsLocked lists lanes that took part in the current heat.
func (s *Server) participantsLocked() []score.Entry {
	seen := map[string]bool{}
	var es []score.Entry
	add := func(id string) {
		if seen[id] {
			return
		}
		seen[id] = true
		t := s.tally.Get(id)
		es = append(es, score.Entry{Lane: id, Score: t.Score, LastScoreAt: t.LastScoreAt})
	}
	for _, id := range s.tally.Lanes() {
		add(id)
	}
	for id, l := range s.lanes {
		if !l.LastSeen.Before(s.heat.StartedAt) {
			add(id)
		}
	}
	return score.Rank(es)
}

type podiumRow struct {
	Pos    int     `json:"pos"`
	LaneID string  `json:"lane_id"`
	Game   string  `json:"game"`
	Fault  string  `json:"fault"`
	Model  string  `json:"model"`
	Score  float64 `json:"score"`
	Points int     `json:"points"`
}

func (s *Server) provisionalLocked() []podiumRow {
	out := []podiumRow{}
	for _, e := range s.participantsLocked() {
		if e.Pos > len(s.cfg.SeasonPoints) {
			break
		}
		r := podiumRow{Pos: e.Pos, LaneID: e.Lane, Score: e.Score,
			Points: score.SeasonPointsFor(e.Pos, e.Score, s.cfg.SeasonPoints)}
		if l := s.lanes[e.Lane]; l != nil {
			r.Game, r.Fault, r.Model = l.Game, l.Fault, l.Model
		}
		out = append(out, r)
	}
	return out
}

func (s *Server) finalizeLocked() error {
	var rs []score.HeatResult
	for _, e := range s.participantsLocked() {
		r := score.HeatResult{HeatID: s.heat.ID, Lane: e.Lane, Score: e.Score, Pos: e.Pos,
			Points: score.SeasonPointsFor(e.Pos, e.Score, s.cfg.SeasonPoints)}
		if l := s.lanes[e.Lane]; l != nil {
			r.Game, r.Fault, r.Model = l.Game, l.Fault, l.Model
		}
		rs = append(rs, r)
	}
	if err := s.store.FinalizeHeat(s.heat.ID, rs); err != nil {
		return err
	}
	s.heat.Finalized = true
	s.results = append(s.results, rs...)
	s.recomputeSeason()
	return nil
}

func (s *Server) recomputeSeason() {
	s.seasonPt = map[string]int{}
	for _, r := range s.results {
		s.seasonPt[r.Lane] += r.Points
	}
}

func (s *Server) heatViewLocked() HeatView {
	now := s.now()
	v := HeatView{ID: s.heat.ID, Seq: s.heat.Seq, Phase: s.phase,
		StartedAt: ts(s.heat.StartedAt), EndsAt: ts(s.heat.EndsAt), PitEndsAt: ts(s.heat.PitEndsAt),
		NextID: heatID(s.heat.Seq + 1)}
	end := s.heat.EndsAt
	if s.phase == phasePit {
		end = s.heat.PitEndsAt
	}
	if d := end.Sub(now); d > 0 {
		v.RemainingMS = d.Milliseconds()
	}
	return v
}

func (s *Server) leaderLocked() string {
	r := s.participantsLocked()
	if len(r) == 0 || r[0].Score <= 0 {
		return ""
	}
	return r[0].Lane
}

// IngestResult summarises one POST /events body.
type IngestResult struct {
	Accepted   int      `json:"accepted"`
	Duplicates int      `json:"duplicates"`
	Rejected   int      `json:"rejected"`
	Errors     []string `json:"errors,omitempty"`
}

// Ingest processes decoded events. raw[i] is the original line of evs[i].
func (s *Server) Ingest(evs []Event, raw [][]byte) (IngestResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var res IngestResult
	for i := range evs {
		dup, err := s.ingestOneLocked(&evs[i], raw[i])
		if err != nil {
			return res, err
		}
		if dup {
			res.Duplicates++
		} else {
			res.Accepted++
		}
	}
	return res, nil
}

func (s *Server) touchLane(e *Event, at time.Time) *Lane {
	l := s.lanes[e.LaneID]
	if l == nil {
		l = &Lane{ID: e.LaneID}
		s.lanes[e.LaneID] = l
	}
	set := func(dst *string, v string) {
		if v != "" {
			*dst = v
		}
	}
	set(&l.Game, e.Game)
	set(&l.GameVer, e.GameVer)
	set(&l.Fault, e.Fault)
	set(&l.Model, e.Model)
	set(&l.HeatID, e.HeatID)
	if e.Seed != 0 {
		l.Seed = uint64(e.Seed)
	}
	if e.Turn != nil {
		l.Turn = *e.Turn
	}
	if e.TokensIn != nil {
		l.TokensIn = *e.TokensIn
	}
	if e.TokensOut != nil {
		l.TokensOut = *e.TokensOut
	}
	l.LastSeen = at
	switch e.Type {
	case "lane_start":
		l.Ended, l.Started = false, at
		l.Turn, l.TokensIn, l.TokensOut = valOr(e.Turn), valOr(e.TokensIn), valOr(e.TokensOut)
	case "lane_end":
		l.Ended = true
	default:
		l.Ended = false
	}
	s.dirty[l.ID] = true
	return l
}

func valOr(p *int64) int64 {
	if p == nil {
		return 0
	}
	return *p
}

func (s *Server) ingestOneLocked(e *Event, raw []byte) (dup bool, err error) {
	recv := s.now()
	if e.Type == "heartbeat" {
		// Heartbeats only refresh lane liveness; they are not persisted.
		s.touchLane(e, recv)
		return false, nil
	}
	if has, err := s.store.HasEvent(e.EventID); err != nil || has {
		return has, err
	}
	at, perr := time.Parse(time.RFC3339Nano, e.TS)
	if perr != nil {
		at = recv
	}
	if e.HeatID == "" {
		e.HeatID = s.heat.ID
	}
	se := &StoredEvent{Event: *e, RecvAt: recv, At: at, Raw: string(raw)}
	isFail := score.IsFailure(e.Type)
	key := score.ClusterKey(e.Type, e.StackHash, e.Detail)
	if isFail {
		se.Cluster = e.Game + "/" + key
		// Scored iff it belongs to the current heat and happened before the
		// flag: during the race, or during the pit with ts <= ends_at.
		se.Scored = e.HeatID == s.heat.ID && !s.heat.Finalized &&
			(s.phase == phaseRace || !at.After(s.heat.EndsAt))
	}
	if se.Scored {
		// Persist n/points with the row; the tally is mutated only after insert.
		se.N = s.tally.Peek(e.LaneID, key)
		se.Points = s.cfg.Weights.Points(e.Type, se.N)
	}
	ok, err := s.store.InsertEvent(se)
	if err != nil || !ok {
		return !ok && err == nil, err
	}
	l := s.touchLane(e, recv)
	if !isFail {
		return false, s.store.UpsertLane(l)
	}
	fv := FailureView{Cluster: se.Cluster, Scored: se.Scored}
	fv.fill(e)
	if se.Scored {
		fv.N, fv.Points = s.tally.Add(e.LaneID, e.Type, key, recv)
		fv.Score = s.tally.Get(e.LaneID).Score
		s.dirtyAll = true
	}
	if e.Type != "agent_error" {
		fv.NewCluster, err = s.store.NewCluster(se.Cluster, e.Game, key, recv, e.LaneID, e.EventID)
		if err != nil {
			return false, err
		}
	}
	l.LastFailure = &FailureBrief{Type: e.Type, Detail: e.Detail, TS: ts(at), Points: fv.Points}
	if err := s.store.UpsertLane(l); err != nil {
		return false, err
	}
	s.Hub.Publish("failure", fv)
	s.tickersLocked(&fv, l)
	return false, nil
}

func laneTag(l *Lane) string {
	if l == nil {
		return "?"
	}
	if l.Game == "" && l.Fault == "" {
		return l.ID
	}
	return fmt.Sprintf("%s (%s/%s)", l.ID, l.Game, l.Fault)
}

func (s *Server) ticker(kind, lane, text string) {
	s.Hub.Publish("ticker", Ticker{Kind: kind, Text: text, LaneID: lane, HeatID: s.heat.ID, TS: ts(s.now())})
}

func (s *Server) tickersLocked(f *FailureView, l *Lane) {
	what := f.Type
	if f.Detail != "" {
		what += " " + f.Detail
	}
	if f.Scored && f.Points > 0 && !s.blooded {
		s.blooded = true
		s.ticker("first_blood", l.ID, fmt.Sprintf("FIRST BLOOD in %s! %s draws a %s", s.heat.ID, laneTag(l), what))
	}
	if f.NewCluster {
		name := f.StackHash
		if name == "" {
			name = f.Type + "|" + f.Detail
		}
		s.ticker("new_cluster", l.ID, fmt.Sprintf("NEW CLUSTER %s in %s: %s, found by %s", name, f.Game, what, l.ID))
	}
	if f.Scored && f.Points > 0 {
		lead := s.leaderLocked()
		if lead != s.leader {
			prev := s.leader
			s.leader = lead
			if prev != "" && lead != "" {
				sc := s.tally.Get(lead).Score
				s.ticker("lead_change", lead, fmt.Sprintf("LEAD CHANGE: %s passes %s with %s pts",
					laneTag(s.lanes[lead]), prev, fmtPts(sc)))
			}
		}
	}
}

func fmtPts(f float64) string {
	return fmt.Sprintf("%g", math.Round(f*1000)/1000)
}

// Focus broadcasts a wall focus request.
func (s *Server) Focus(lane string) {
	s.Hub.Publish("focus", map[string]string{"lane_id": lane, "ts": ts(s.now())})
}

// ---- snapshots ----

// State is GET /api/state and the `state` SSE payload.
type State struct {
	Full       bool               `json:"full"`
	ServerTime string             `json:"server_time"`
	Heat       HeatView           `json:"heat"`
	Config     map[string]any     `json:"config,omitempty"`
	Lanes      []LaneView         `json:"lanes"`
	Scores     map[string]float64 `json:"scores"`
	Standings  Standings          `json:"standings"`
}

// Standings are the four tables.
type Standings struct {
	Lanes        []score.Entry     `json:"lanes"`
	Constructors []score.GroupRow  `json:"constructors"` // by fault config
	Games        []score.GroupRow  `json:"games"`
	Season       []score.SeasonRow `json:"season"`
}

func (s *Server) laneViewLocked(l *Lane, pos map[string]int) LaneView {
	t := s.tally.Get(l.ID)
	v := LaneView{ID: l.ID, Game: l.Game, GameVer: l.GameVer, Fault: l.Fault, Seed: l.Seed, Model: l.Model,
		HeatID: l.HeatID, LastSeen: ts(l.LastSeen), Turn: l.Turn, TokensIn: l.TokensIn, TokensOut: l.TokensOut,
		Pos: pos[l.ID], Score: t.Score, Hits: t.Hits, Clusters: t.Clusters,
		SeasonPoints: s.seasonPt[l.ID], LastFailure: l.LastFailure}
	laps := t.Score / s.cfg.LapPoints
	v.Laps = int(math.Floor(laps))
	v.LapFrac = laps - math.Floor(laps)
	switch {
	case l.Ended:
		v.Status = "ended"
	case s.now().Sub(l.LastSeen) > s.cfg.LaneStale.Duration:
		v.Status = "stale"
	default:
		v.Status = "running"
	}
	return v
}

// snapshotLocked builds a full state, or (full=false) a delta of the
// changed lanes. Standings and scores are always complete.
func (s *Server) snapshotLocked(full bool) State {
	ranked := s.participantsLocked()
	pos := map[string]int{}
	scores := map[string]float64{}
	live := []score.LiveLane{}
	for _, e := range ranked {
		pos[e.Lane] = e.Pos
		scores[e.Lane] = e.Score
		ll := score.LiveLane{Lane: e.Lane, Score: e.Score}
		if l := s.lanes[e.Lane]; l != nil {
			ll.Game, ll.Fault = l.Game, l.Fault
		}
		live = append(live, ll)
	}
	st := State{Full: full, ServerTime: ts(s.now()), Heat: s.heatViewLocked(), Scores: scores, Lanes: []LaneView{},
		Standings: Standings{
			Lanes:        ranked,
			Constructors: score.Groups(live, s.results, score.ByFault),
			Games:        score.Groups(live, s.results, score.ByGame),
			Season:       score.Season(s.results),
		}}
	if st.Standings.Lanes == nil {
		st.Standings.Lanes = []score.Entry{}
	}
	if full {
		st.Config = map[string]any{
			"lap_points": s.cfg.LapPoints, "weights": s.cfg.Weights, "season_points": s.cfg.SeasonPoints,
			"heat_secs": s.cfg.Heat.Seconds(), "pit_secs": s.cfg.Pit.Seconds(),
		}
	}
	window := s.heat.StartedAt.Add(-(s.cfg.Heat.Duration + s.cfg.Pit.Duration))
	for id, l := range s.lanes {
		if !full && !s.dirtyAll && !s.dirty[id] {
			continue
		}
		if _, in := pos[id]; !in && l.LastSeen.Before(window) {
			continue
		}
		st.Lanes = append(st.Lanes, s.laneViewLocked(l, pos))
	}
	sort.Slice(st.Lanes, func(i, j int) bool {
		a, b := st.Lanes[i], st.Lanes[j]
		if (a.Pos == 0) != (b.Pos == 0) {
			return a.Pos != 0
		}
		if a.Pos != b.Pos {
			return a.Pos < b.Pos
		}
		return a.ID < b.ID
	})
	return st
}

// Snapshot returns the full state.
func (s *Server) Snapshot() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.snapshotLocked(true)
}

// FlushState broadcasts a `state` delta if anything changed. Called at <= state_hz.
func (s *Server) FlushState() bool {
	s.mu.Lock()
	if !s.dirtyAll && len(s.dirty) == 0 {
		s.mu.Unlock()
		return false
	}
	st := s.snapshotLocked(false)
	s.dirty, s.dirtyAll = map[string]bool{}, false
	s.mu.Unlock()
	b, _ := json.Marshal(st)
	s.Hub.Broadcast("state", b)
	return true
}

// LaneCard is GET /api/lanes/{id}.
type LaneCard struct {
	Lane     LaneView      `json:"lane"`
	Failures []FailureView `json:"failures"`
	Results  []score.HeatResult `json:"results"` // this lane's finalized heats
}

// LaneDetail returns the config card and failure log, or nil if unknown.
func (s *Server) LaneDetail(id string) (*LaneCard, error) {
	s.mu.Lock()
	l := s.lanes[id]
	var card LaneCard
	if l != nil {
		pos := map[string]int{}
		for _, e := range s.participantsLocked() {
			pos[e.Lane] = e.Pos
		}
		card.Lane = s.laneViewLocked(l, pos)
	}
	card.Results = []score.HeatResult{}
	for _, r := range s.results {
		if r.Lane == id {
			card.Results = append(card.Results, r)
		}
	}
	s.mu.Unlock()
	fl, err := s.store.FailureLog(id, s.cfg.FailureLog)
	if err != nil {
		return nil, err
	}
	if l == nil && len(fl) == 0 {
		return nil, nil
	}
	if l == nil {
		card.Lane = LaneView{ID: id, Status: "ended"}
	}
	card.Failures = fl
	return &card, nil
}

// Run drives the heat clock and the throttled state broadcaster until ctx ends.
func (s *Server) Run(ctx context.Context) {
	tick := time.NewTicker(200 * time.Millisecond)
	defer tick.Stop()
	flush := time.NewTicker(time.Duration(float64(time.Second) / s.cfg.StateHz))
	defer flush.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			s.Tick()
		case <-flush.C:
			s.FlushState()
		}
	}
}
