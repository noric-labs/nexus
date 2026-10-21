// Package score holds the pure scoring rules of the Torment Nexus race:
// failure weights, per-cluster decay, heat ranking, season points and the
// aggregated standings (lanes, constructors = fault configs, games, season).
package score

import (
	"math"
	"sort"
	"time"
)

// Weights maps a failure type to its base points.
type Weights map[string]float64

// DefaultWeights are the contract defaults.
func DefaultWeights() Weights {
	return Weights{"sanitizer": 5, "crash": 3, "hang": 2, "error_exit": 1, "visual": 1, "agent_error": 0}
}

// DefaultSeasonPoints are awarded by heat finish position (1st..10th).
func DefaultSeasonPoints() []int { return []int{25, 18, 15, 12, 10, 8, 6, 4, 2, 1} }

// Failure types (scored); everything else (heartbeat, lane_start, lane_end) is not.
var failureTypes = map[string]bool{"crash": true, "error_exit": true, "sanitizer": true, "hang": true, "visual": true, "agent_error": true}

// IsFailure reports whether an event type is a failure type.
func IsFailure(t string) bool { return failureTypes[t] }

// ClusterKey is the decay key within a lane: the stack hash when present,
// otherwise "type|detail" (hangs / visual corruption without a stack).
func ClusterKey(typ, stackHash, detail string) string {
	if stackHash != "" {
		return stackHash
	}
	return typ + "|" + detail
}

// Decay is 0.5^(n-1) for the n-th hit (n >= 1) of a cluster.
func Decay(n int) float64 {
	if n < 1 {
		n = 1
	}
	return math.Pow(0.5, float64(n-1))
}

// Points for the n-th hit of a cluster of the given type.
func (w Weights) Points(typ string, n int) float64 { return w[typ] * Decay(n) }

// LaneTally is one lane's running total in one heat.
type LaneTally struct {
	Lane        string
	Score       float64
	Hits        int       // failure events counted (any weight)
	Clusters    int       // distinct cluster keys
	LastScoreAt time.Time // when Score last increased (tie-break)
	counts      map[string]int
}

// Tally accumulates hits for one heat.
type Tally struct {
	W     Weights
	lanes map[string]*LaneTally
}

// NewTally returns an empty heat tally.
func NewTally(w Weights) *Tally { return &Tally{W: w, lanes: map[string]*LaneTally{}} }

// Add records a hit and returns its ordinal n within (lane, key) and its points.
func (t *Tally) Add(lane, typ, key string, at time.Time) (int, float64) {
	lt := t.lanes[lane]
	if lt == nil {
		lt = &LaneTally{Lane: lane, counts: map[string]int{}}
		t.lanes[lane] = lt
	}
	lt.counts[key]++
	n := lt.counts[key]
	if n == 1 {
		lt.Clusters++
	}
	lt.Hits++
	p := t.W.Points(typ, n)
	if p > 0 {
		lt.Score += p
		lt.LastScoreAt = at
	}
	return n, p
}

// Peek returns the n the next hit of (lane, key) would get.
func (t *Tally) Peek(lane, key string) int {
	if lt := t.lanes[lane]; lt != nil {
		return lt.counts[key] + 1
	}
	return 1
}

// Get returns a copy of a lane's tally (zero value if unseen).
func (t *Tally) Get(lane string) LaneTally {
	if lt := t.lanes[lane]; lt != nil {
		c := *lt
		c.counts = nil
		return c
	}
	return LaneTally{Lane: lane}
}

// Lanes returns the ids of every lane with at least one hit.
func (t *Tally) Lanes() []string {
	out := make([]string, 0, len(t.lanes))
	for id := range t.lanes {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// Entry is one row of a heat ranking.
type Entry struct {
	Pos         int       `json:"pos"`
	Lane        string    `json:"lane_id"`
	Score       float64   `json:"score"`
	LastScoreAt time.Time `json:"-"`
}

// Rank sorts entries: score desc, then whoever reached their score first,
// then lane id. Positions are 1-based and unique.
func Rank(es []Entry) []Entry {
	out := append([]Entry(nil), es...)
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Score != b.Score {
			return a.Score > b.Score
		}
		if !a.LastScoreAt.Equal(b.LastScoreAt) {
			if a.LastScoreAt.IsZero() {
				return false
			}
			if b.LastScoreAt.IsZero() {
				return true
			}
			return a.LastScoreAt.Before(b.LastScoreAt)
		}
		return a.Lane < b.Lane
	})
	for i := range out {
		out[i].Pos = i + 1
	}
	return out
}

// SeasonPointsFor returns the season points for a finish position. Lanes that
// finished with zero score get nothing.
func SeasonPointsFor(pos int, score float64, table []int) int {
	if score <= 0 || pos < 1 || pos > len(table) {
		return 0
	}
	return table[pos-1]
}

// HeatResult is one lane's final result in one finalized heat.
type HeatResult struct {
	HeatID string  `json:"heat_id"`
	Lane   string  `json:"lane_id"`
	Game   string  `json:"game"`
	Fault  string  `json:"fault"`
	Model  string  `json:"model"`
	Score  float64 `json:"score"`
	Pos    int     `json:"pos"`
	Points int     `json:"points"`
}

// SeasonRow is one lane's season line.
type SeasonRow struct {
	Pos     int     `json:"pos"`
	Lane    string  `json:"lane_id"`
	Points  int     `json:"points"`
	Wins    int     `json:"wins"`
	Podiums int     `json:"podiums"`
	Heats   int     `json:"heats"`
	Best    int     `json:"best"` // best scoring finish, 0 if never scored
	Score   float64 `json:"score"` // cumulative heat score
}

// Season aggregates finalized heat results into the season table, sorted by
// points, wins, podiums, cumulative score, then lane id.
func Season(rs []HeatResult) []SeasonRow {
	m := map[string]*SeasonRow{}
	for _, r := range rs {
		row := m[r.Lane]
		if row == nil {
			row = &SeasonRow{Lane: r.Lane}
			m[r.Lane] = row
		}
		row.Heats++
		row.Points += r.Points
		row.Score += r.Score
		if r.Score > 0 {
			if r.Pos == 1 {
				row.Wins++
			}
			if r.Pos <= 3 {
				row.Podiums++
			}
			if row.Best == 0 || r.Pos < row.Best {
				row.Best = r.Pos
			}
		}
	}
	out := make([]SeasonRow, 0, len(m))
	for _, r := range m {
		out = append(out, *r)
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		switch {
		case a.Points != b.Points:
			return a.Points > b.Points
		case a.Wins != b.Wins:
			return a.Wins > b.Wins
		case a.Podiums != b.Podiums:
			return a.Podiums > b.Podiums
		case a.Score != b.Score:
			return a.Score > b.Score
		}
		return a.Lane < b.Lane
	})
	for i := range out {
		out[i].Pos = i + 1
	}
	return out
}

// GroupRow is a standings row aggregated by fault config ("constructors") or game.
type GroupRow struct {
	Pos          int     `json:"pos"`
	Key          string  `json:"key"`
	Score        float64 `json:"score"`         // sum of lane scores in the current heat
	Lanes        int     `json:"lanes"`         // lanes in the current heat
	SeasonPoints int     `json:"season_points"` // sum of season points over finalized heats
	SeasonScore  float64 `json:"season_score"`  // sum of heat scores over finalized heats
}

// LiveLane is a lane's current-heat contribution to group standings.
type LiveLane struct {
	Lane, Game, Fault string
	Score             float64
}

// Groups builds standings grouped by key (e.g. fault or game). Current-heat
// rows contribute Score/Lanes; finalized results contribute season columns.
// Sorted by season points, then current score, then key.
func Groups(live []LiveLane, results []HeatResult, key func(game, fault string) string) []GroupRow {
	m := map[string]*GroupRow{}
	get := func(k string) *GroupRow {
		if m[k] == nil {
			m[k] = &GroupRow{Key: k}
		}
		return m[k]
	}
	for _, l := range live {
		g := get(key(l.Game, l.Fault))
		g.Score += l.Score
		g.Lanes++
	}
	for _, r := range results {
		g := get(key(r.Game, r.Fault))
		g.SeasonPoints += r.Points
		g.SeasonScore += r.Score
	}
	out := make([]GroupRow, 0, len(m))
	for _, g := range m {
		out = append(out, *g)
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		switch {
		case a.SeasonPoints != b.SeasonPoints:
			return a.SeasonPoints > b.SeasonPoints
		case a.Score != b.Score:
			return a.Score > b.Score
		}
		return a.Key < b.Key
	})
	for i := range out {
		out[i].Pos = i + 1
	}
	return out
}

// ByFault and ByGame are key functions for Groups.
func ByFault(_, fault string) string { return fault }
func ByGame(game, _ string) string  { return game }
