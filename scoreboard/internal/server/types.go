package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"time"
)

// U64 accepts a JSON number or a numeric string (seeds are u64 and may
// exceed float precision) and always encodes as a JSON number.
type U64 uint64

func (u *U64) UnmarshalJSON(b []byte) error {
	b = bytes.Trim(b, `"`)
	if len(b) == 0 || string(b) == "null" {
		*u = 0
		return nil
	}
	v, err := strconv.ParseUint(string(b), 10, 64)
	if err != nil {
		return fmt.Errorf("seed: %w", err)
	}
	*u = U64(v)
	return nil
}

// Event is schema v1 (lane -> scoreboard).
type Event struct {
	V         int    `json:"v"`
	EventID   string `json:"event_id"`
	TS        string `json:"ts"`
	HeatID    string `json:"heat_id"`
	LaneID    string `json:"lane_id"`
	Game      string `json:"game,omitempty"`
	GameVer   string `json:"game_ver,omitempty"`
	Fault     string `json:"fault,omitempty"`
	Seed      U64    `json:"seed,omitempty"`
	Model     string `json:"model,omitempty"`
	Type      string `json:"type"`
	Signal    *int   `json:"signal,omitempty"`
	ExitCode  *int   `json:"exit_code,omitempty"`
	StackHash string `json:"stack_hash,omitempty"`
	Detail    string `json:"detail,omitempty"`
	Turn      *int64 `json:"turn,omitempty"`
	TokensIn  *int64 `json:"tokens_in,omitempty"`
	TokensOut *int64 `json:"tokens_out,omitempty"`
	Replay    string `json:"replay,omitempty"`
}

var knownTypes = map[string]bool{
	"crash": true, "error_exit": true, "sanitizer": true, "hang": true, "visual": true, "agent_error": true,
	"heartbeat": true, "lane_start": true, "lane_end": true,
}

func decodeEvent(b []byte, e *Event) error {
	if err := json.Unmarshal(b, e); err != nil {
		return err
	}
	switch {
	case e.V != 1:
		return fmt.Errorf("unsupported schema version %d", e.V)
	case e.EventID == "":
		return fmt.Errorf("missing event_id")
	case e.LaneID == "":
		return fmt.Errorf("missing lane_id")
	case !knownTypes[e.Type]:
		return fmt.Errorf("unknown type %q", e.Type)
	}
	return nil
}

// Heat is one race period followed by a pit.
type Heat struct {
	ID        string
	Seq       int
	StartedAt time.Time
	EndsAt    time.Time
	PitEndsAt time.Time
	Finalized bool
}

func heatID(seq int) string { return fmt.Sprintf("h-%04d", seq) }

// HeatView is the JSON form of the heat (in /api/state and `heat`/`pit` events).
type HeatView struct {
	ID          string `json:"id"`
	Seq         int    `json:"seq"`
	Phase       string `json:"phase"` // "race" | "pit"
	StartedAt   string `json:"started_at"`
	EndsAt      string `json:"ends_at"`
	PitEndsAt   string `json:"pit_ends_at"`
	NextID      string `json:"next_id"`
	RemainingMS int64  `json:"remaining_ms"` // until the end of the current phase
}

// Lane is the scoreboard's view of a lane (identity from its latest event).
type Lane struct {
	ID, Game, GameVer, Fault, Model, HeatID string
	Seed                                    uint64
	LastSeen                                time.Time
	Started                                 time.Time
	Ended                                   bool
	Turn, TokensIn, TokensOut               int64
	LastFailure                             *FailureBrief
}

// FailureBrief is the most recent failure of a lane.
type FailureBrief struct {
	Type   string  `json:"type"`
	Detail string  `json:"detail,omitempty"`
	TS     string  `json:"ts"`
	Points float64 `json:"points"`
}

// LaneView is a lane row in /api/state and `state` deltas.
type LaneView struct {
	ID           string        `json:"lane_id"`
	Game         string        `json:"game"`
	GameVer      string        `json:"game_ver,omitempty"`
	Fault        string        `json:"fault"`
	Seed         uint64        `json:"seed"`
	Model        string        `json:"model"`
	HeatID       string        `json:"heat_id"`
	Status       string        `json:"status"` // running | stale | ended
	LastSeen     string        `json:"last_seen"`
	Turn         int64         `json:"turn"`
	TokensIn     int64         `json:"tokens_in"`
	TokensOut    int64         `json:"tokens_out"`
	Pos          int           `json:"pos"`
	Score        float64       `json:"score"`
	Laps         int           `json:"laps"`     // floor(score / lap_points)
	LapFrac      float64       `json:"lap_frac"` // position within the current lap, 0..1
	Hits         int           `json:"hits"`
	Clusters     int           `json:"clusters"`
	SeasonPoints int           `json:"season_points"`
	LastFailure  *FailureBrief `json:"last_failure,omitempty"`
}

// FailureView is a failure in the `failure` SSE event and the lane failure log.
type FailureView struct {
	EventID    string  `json:"event_id"`
	TS         string  `json:"ts"`
	HeatID     string  `json:"heat_id"`
	LaneID     string  `json:"lane_id"`
	Game       string  `json:"game"`
	Fault      string  `json:"fault"`
	Model      string  `json:"model"`
	Seed       uint64  `json:"seed"`
	Type       string  `json:"type"`
	Signal     *int    `json:"signal"`
	ExitCode   *int    `json:"exit_code"`
	StackHash  string  `json:"stack_hash"`
	Detail     string  `json:"detail"`
	Turn       *int64  `json:"turn"`
	Replay     string  `json:"replay"`
	Cluster    string  `json:"cluster"`     // "<game>/<key>"
	N          int     `json:"n"`           // hit number within (lane, key) this heat; 0 if unscored
	Points     float64 `json:"points"`      // points awarded
	Scored     bool    `json:"scored"`      // false if outside the heat window
	NewCluster bool    `json:"new_cluster"` // first sighting season-wide
	Score      float64 `json:"lane_score"`  // lane's heat score after this hit
}

func (f *FailureView) fill(e *Event) {
	f.EventID, f.TS, f.HeatID, f.LaneID = e.EventID, e.TS, e.HeatID, e.LaneID
	f.Game, f.Fault, f.Model, f.Seed, f.Type = e.Game, e.Fault, e.Model, uint64(e.Seed), e.Type
	f.Signal, f.ExitCode, f.StackHash, f.Detail, f.Turn, f.Replay = e.Signal, e.ExitCode, e.StackHash, e.Detail, e.Turn, e.Replay
}

// Ticker is a commentary line.
type Ticker struct {
	Kind   string `json:"kind"` // first_blood | new_cluster | lead_change
	Text   string `json:"text"`
	LaneID string `json:"lane_id"`
	HeatID string `json:"heat_id"`
	TS     string `json:"ts"`
}
