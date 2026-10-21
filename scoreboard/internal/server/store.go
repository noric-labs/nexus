package server

import (
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"time"

	_ "modernc.org/sqlite"

	"github.com/noric-labs/nexus/scoreboard/internal/score"
)

// Store is the SQLite persistence layer (WAL mode, single writer).
type Store struct{ db *sql.DB }

const schema = `
CREATE TABLE IF NOT EXISTS events (
  event_id   TEXT PRIMARY KEY,
  recv_at    TEXT NOT NULL,
  ts         TEXT NOT NULL,
  heat_id    TEXT NOT NULL,
  lane_id    TEXT NOT NULL,
  type       TEXT NOT NULL,
  game       TEXT, fault TEXT, model TEXT,
  stack_hash TEXT, detail TEXT, cluster TEXT,
  n          INTEGER NOT NULL DEFAULT 0,
  points     REAL NOT NULL DEFAULT 0,
  scored     INTEGER NOT NULL DEFAULT 0,
  raw        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_lane ON events(lane_id, recv_at);
CREATE INDEX IF NOT EXISTS events_heat ON events(heat_id, scored);
CREATE TABLE IF NOT EXISTS heats (
  id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE,
  started_at TEXT NOT NULL, ends_at TEXT NOT NULL, pit_ends_at TEXT NOT NULL,
  finalized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS heat_results (
  heat_id TEXT NOT NULL, lane_id TEXT NOT NULL, game TEXT, fault TEXT, model TEXT,
  score REAL NOT NULL, position INTEGER NOT NULL, points INTEGER NOT NULL,
  PRIMARY KEY (heat_id, lane_id)
);
CREATE TABLE IF NOT EXISTS clusters (
  id TEXT PRIMARY KEY, game TEXT, key TEXT, first_seen TEXT, first_lane TEXT, first_event TEXT
);
CREATE TABLE IF NOT EXISTS lanes (
  lane_id TEXT PRIMARY KEY, game TEXT, game_ver TEXT, fault TEXT, seed TEXT, model TEXT,
  heat_id TEXT, last_seen TEXT, ended INTEGER NOT NULL DEFAULT 0
);
`

// OpenStore opens (creating) the database at path in WAL mode.
func OpenStore(path string) (*Store, error) {
	q := url.Values{}
	for _, p := range []string{"journal_mode(WAL)", "busy_timeout(5000)", "synchronous(NORMAL)", "foreign_keys(ON)"} {
		q.Add("_pragma", p)
	}
	db, err := sql.Open("sqlite", "file:"+path+"?"+q.Encode())
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("schema: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func ts(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func parseTS(s string) time.Time {
	t, _ := time.Parse(time.RFC3339Nano, s)
	return t
}

// HasEvent reports whether event_id was already ingested.
func (s *Store) HasEvent(id string) (bool, error) {
	var one int
	err := s.db.QueryRow(`SELECT 1 FROM events WHERE event_id=?`, id).Scan(&one)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

// StoredEvent is a persisted (non-heartbeat) event with its scoring.
type StoredEvent struct {
	Event
	RecvAt  time.Time
	At      time.Time
	Cluster string
	N       int
	Points  float64
	Scored  bool
	Raw     string
}

// InsertEvent inserts idempotently; ok=false means duplicate event_id.
func (s *Store) InsertEvent(e *StoredEvent) (bool, error) {
	res, err := s.db.Exec(`INSERT OR IGNORE INTO events
	 (event_id,recv_at,ts,heat_id,lane_id,type,game,fault,model,stack_hash,detail,cluster,n,points,scored,raw)
	 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		e.EventID, ts(e.RecvAt), ts(e.At), e.HeatID, e.LaneID, e.Type, e.Game, e.Fault, e.Model,
		e.StackHash, e.Detail, e.Cluster, e.N, e.Points, e.Scored, e.Raw)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 1, nil
}

// NewCluster records a cluster if unseen; returns true if it was new.
func (s *Store) NewCluster(id, game, key string, at time.Time, lane, event string) (bool, error) {
	res, err := s.db.Exec(`INSERT OR IGNORE INTO clusters(id,game,key,first_seen,first_lane,first_event) VALUES (?,?,?,?,?,?)`,
		id, game, key, ts(at), lane, event)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 1, nil
}

// UpsertLane saves lane identity.
func (s *Store) UpsertLane(l *Lane) error {
	_, err := s.db.Exec(`INSERT INTO lanes(lane_id,game,game_ver,fault,seed,model,heat_id,last_seen,ended)
	 VALUES (?,?,?,?,?,?,?,?,?)
	 ON CONFLICT(lane_id) DO UPDATE SET game=excluded.game, game_ver=excluded.game_ver, fault=excluded.fault,
	 seed=excluded.seed, model=excluded.model, heat_id=excluded.heat_id, last_seen=excluded.last_seen, ended=excluded.ended`,
		l.ID, l.Game, l.GameVer, l.Fault, fmt.Sprint(l.Seed), l.Model, l.HeatID, ts(l.LastSeen), l.Ended)
	return err
}

// LoadLanes returns lanes seen since t.
func (s *Store) LoadLanes(since time.Time) ([]*Lane, error) {
	rows, err := s.db.Query(`SELECT lane_id,game,game_ver,fault,seed,model,heat_id,last_seen,ended FROM lanes WHERE last_seen>=?`, ts(since))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Lane
	for rows.Next() {
		l := &Lane{}
		var seed, last string
		if err := rows.Scan(&l.ID, &l.Game, &l.GameVer, &l.Fault, &seed, &l.Model, &l.HeatID, &last, &l.Ended); err != nil {
			return nil, err
		}
		fmt.Sscan(seed, &l.Seed)
		l.LastSeen = parseTS(last)
		out = append(out, l)
	}
	return out, rows.Err()
}

// ScoredEvents returns the scored failures of a heat in ingest order.
func (s *Store) ScoredEvents(heatID string) ([]StoredEvent, error) {
	rows, err := s.db.Query(`SELECT event_id,lane_id,type,game,stack_hash,detail,ts,recv_at FROM events WHERE heat_id=? AND scored=1 ORDER BY rowid`, heatID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []StoredEvent
	for rows.Next() {
		var e StoredEvent
		var at, recv string
		if err := rows.Scan(&e.EventID, &e.LaneID, &e.Type, &e.Game, &e.StackHash, &e.Detail, &at, &recv); err != nil {
			return nil, err
		}
		e.At, e.RecvAt = parseTS(at), parseTS(recv)
		out = append(out, e)
	}
	return out, rows.Err()
}

// FailureLog returns the latest failures of a lane, newest first.
func (s *Store) FailureLog(lane string, limit int) ([]FailureView, error) {
	rows, err := s.db.Query(`SELECT raw,heat_id,cluster,n,points,scored FROM events WHERE lane_id=? AND type IN ('crash','sanitizer','hang','visual','agent_error') ORDER BY rowid DESC LIMIT ?`, lane, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []FailureView{}
	for rows.Next() {
		var raw, heat string
		var f FailureView
		if err := rows.Scan(&raw, &heat, &f.Cluster, &f.N, &f.Points, &f.Scored); err != nil {
			return nil, err
		}
		var e Event
		if err := decodeEvent([]byte(raw), &e); err == nil {
			f.fill(&e)
			f.HeatID = heat
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

// Heat rows.
func (s *Store) LastHeat() (*Heat, error) {
	var h Heat
	var a, b, c string
	err := s.db.QueryRow(`SELECT id,seq,started_at,ends_at,pit_ends_at,finalized FROM heats ORDER BY seq DESC LIMIT 1`).
		Scan(&h.ID, &h.Seq, &a, &b, &c, &h.Finalized)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	h.StartedAt, h.EndsAt, h.PitEndsAt = parseTS(a), parseTS(b), parseTS(c)
	return &h, nil
}

func (s *Store) InsertHeat(h *Heat) error {
	_, err := s.db.Exec(`INSERT INTO heats(id,seq,started_at,ends_at,pit_ends_at,finalized) VALUES (?,?,?,?,?,0)`,
		h.ID, h.Seq, ts(h.StartedAt), ts(h.EndsAt), ts(h.PitEndsAt))
	return err
}

// FinalizeHeat writes results and marks the heat finalized atomically.
func (s *Store) FinalizeHeat(id string, rs []score.HeatResult) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, r := range rs {
		if _, err := tx.Exec(`INSERT OR REPLACE INTO heat_results(heat_id,lane_id,game,fault,model,score,position,points) VALUES (?,?,?,?,?,?,?,?)`,
			r.HeatID, r.Lane, r.Game, r.Fault, r.Model, r.Score, r.Pos, r.Points); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(`UPDATE heats SET finalized=1 WHERE id=?`, id); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) HeatResults() ([]score.HeatResult, error) {
	rows, err := s.db.Query(`SELECT heat_id,lane_id,game,fault,model,score,position,points FROM heat_results ORDER BY heat_id, position`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []score.HeatResult
	for rows.Next() {
		var r score.HeatResult
		if err := rows.Scan(&r.HeatID, &r.Lane, &r.Game, &r.Fault, &r.Model, &r.Score, &r.Pos, &r.Points); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
