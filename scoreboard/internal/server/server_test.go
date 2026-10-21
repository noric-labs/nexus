package server

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) Now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

func testCfg() Config {
	c := DefaultConfig()
	c.Heat.Duration = 10 * time.Minute
	c.Pit.Duration = time.Minute
	return c
}

func newTest(t *testing.T, dbPath string) (*Server, *fakeClock) {
	t.Helper()
	if dbPath == "" {
		dbPath = filepath.Join(t.TempDir(), "sb.db")
	}
	st, err := OpenStore(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	clk := &fakeClock{t: time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)}
	s, err := New(testCfg(), clk, st)
	if err != nil {
		t.Fatal(err)
	}
	return s, clk
}

var evSeq int

func ev(lane, typ, hash, detail string, extra ...string) string {
	evSeq++
	m := map[string]any{"v": 1, "event_id": fmt.Sprintf("e-%d", evSeq), "ts": "2026-10-21T12:00:01Z",
		"lane_id": lane, "game": "brogue", "fault": "malloc-1pct", "seed": 12345, "model": "m", "type": typ}
	if hash != "" {
		m["stack_hash"] = hash
	}
	if detail != "" {
		m["detail"] = detail
	}
	for i := 0; i+1 < len(extra); i += 2 {
		m[extra[i]] = extra[i+1]
	}
	b, _ := json.Marshal(m)
	return string(b)
}

func ingest(t *testing.T, s *Server, lines ...string) IngestResult {
	t.Helper()
	var evs []Event
	var raws [][]byte
	for _, l := range lines {
		var e Event
		if err := decodeEvent([]byte(l), &e); err != nil {
			t.Fatalf("decode %s: %v", l, err)
		}
		evs = append(evs, e)
		raws = append(raws, []byte(l))
	}
	r, err := s.Ingest(evs, raws)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func laneScore(s *Server, id string) float64 { return s.Snapshot().Scores[id] }

func TestIngestScoringAndIdempotency(t *testing.T) {
	s, _ := newTest(t, "")
	a1 := ev("l-001", "sanitizer", "aaaaaaaaaaaa", "heap-use-after-free")
	r := ingest(t, s, ev("l-001", "lane_start", "", ""), a1, a1,
		ev("l-001", "sanitizer", "aaaaaaaaaaaa", ""), ev("l-001", "hang", "", "no-output"),
		ev("l-002", "crash", "bbbbbbbbbbbb", ""), ev("l-002", "heartbeat", "", ""))
	if r.Accepted != 6 || r.Duplicates != 1 {
		t.Fatalf("%+v", r)
	}
	if got := laneScore(s, "l-001"); got != 5+2.5+2 {
		t.Fatalf("l-001 score %v", got)
	}
	st := s.Snapshot()
	if st.Standings.Lanes[0].Lane != "l-001" || st.Standings.Lanes[1].Lane != "l-002" {
		t.Fatalf("standings %+v", st.Standings.Lanes)
	}
	if st.Heat.ID != "h-0001" || st.Heat.Phase != "race" || st.Heat.NextID != "h-0002" {
		t.Fatalf("heat %+v", st.Heat)
	}
	card, err := s.LaneDetail("l-001")
	if err != nil || card == nil || len(card.Failures) != 3 || card.Failures[0].Type != "hang" || card.Failures[2].N != 1 || card.Failures[1].N != 2 {
		t.Fatalf("card %+v %v", card, err)
	}
}

func TestHeatCycleSeasonAndRestart(t *testing.T) {
	dir := t.TempDir()
	db := filepath.Join(dir, "sb.db")
	s, clk := newTest(t, db)
	sub, _ := s.Hub.Subscribe(0, false)
	ingest(t, s, ev("l-001", "crash", "c1", ""), ev("l-002", "sanitizer", "s1", ""), ev("l-003", "lane_start", "", ""))

	clk.Advance(10*time.Minute - time.Second)
	s.Tick()
	if s.Snapshot().Heat.Phase != "race" {
		t.Fatal("pit too early")
	}
	clk.Advance(time.Second)
	s.Tick()
	st := s.Snapshot()
	if st.Heat.Phase != "pit" || st.Heat.ID != "h-0001" {
		t.Fatalf("want pit, got %+v", st.Heat)
	}
	// Failure during the pit with ts after the flag is stored but not scored.
	late := ev("l-003", "sanitizer", "s9", "", "ts", "2026-10-21T12:10:30Z", "heat_id", "h-0001")
	ingest(t, s, late)
	if laneScore(s, "l-003") != 0 {
		t.Fatal("late failure scored")
	}
	// A failure stamped before the flag but delivered in the pit still counts.
	ingest(t, s, ev("l-003", "visual", "", "garbled", "ts", "2026-10-21T12:09:59Z", "heat_id", "h-0001"))
	if laneScore(s, "l-003") != 1 {
		t.Fatalf("in-window pit delivery not scored: %v", laneScore(s, "l-003"))
	}

	clk.Advance(time.Minute)
	s.Tick()
	st = s.Snapshot()
	if st.Heat.ID != "h-0002" || st.Heat.Phase != "race" {
		t.Fatalf("want h-0002 race, got %+v", st.Heat)
	}
	season := st.Standings.Season
	if len(season) != 3 || season[0].Lane != "l-002" || season[0].Points != 25 || season[1].Points != 18 || season[2].Points != 15 {
		t.Fatalf("season %+v", season)
	}
	if laneScore(s, "l-002") != 0 {
		t.Fatal("score not reset at new heat")
	}

	// Events seen on the hub: pit then heat.
	var names []string
	for len(sub.ch) > 0 {
		names = append(names, (<-sub.ch).Event)
	}
	j := strings.Join(names, ",")
	if !strings.Contains(j, "pit") || !strings.HasSuffix(j, "heat") || !strings.Contains(j, "ticker") {
		t.Fatalf("hub events %s", j)
	}

	// Restart mid-heat: heat and scores are restored from SQLite.
	ingest(t, s, ev("l-001", "crash", "c1", "", "heat_id", "h-0002"), ev("l-001", "crash", "c1", "", "heat_id", "h-0002"))
	st2, err := OpenStore(db)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	s2, err := New(testCfg(), clk, st2)
	if err != nil {
		t.Fatal(err)
	}
	snap := s2.Snapshot()
	if snap.Heat.ID != "h-0002" || snap.Scores["l-001"] != 4.5 || len(snap.Standings.Season) != 3 {
		t.Fatalf("restored %+v scores=%v", snap.Heat, snap.Scores)
	}
	// Restart long after: h-0002 is finalized and h-0003 starts.
	clk.Advance(time.Hour)
	st3, _ := OpenStore(db)
	defer st3.Close()
	s3, err := New(testCfg(), clk, st3)
	if err != nil {
		t.Fatal(err)
	}
	if s3.Snapshot().Heat.ID != "h-0003" || s3.seasonPt["l-001"] != 18+25 {
		t.Fatalf("after downtime: %+v %v", s3.Snapshot().Heat, s3.seasonPt)
	}
}

func TestTickers(t *testing.T) {
	s, _ := newTest(t, "")
	sub, _ := s.Hub.Subscribe(0, false)
	ingest(t, s, ev("l-001", "crash", "c1", ""), ev("l-002", "crash", "c1", ""), ev("l-002", "crash", "c2", ""),
		ev("l-003", "agent_error", "", "bad tool call"))
	var kinds []string
	for len(sub.ch) > 0 {
		m := <-sub.ch
		if m.Event == "ticker" {
			var tk Ticker
			json.Unmarshal(m.Data, &tk)
			kinds = append(kinds, tk.Kind+":"+tk.LaneID)
		}
	}
	want := "first_blood:l-001,new_cluster:l-001,new_cluster:l-002,lead_change:l-002"
	if strings.Join(kinds, ",") != want {
		t.Fatalf("tickers %v want %s", kinds, want)
	}
}

func TestStateThrottleDelta(t *testing.T) {
	s, _ := newTest(t, "")
	s.FlushState() // initial heat dirtyAll
	if s.FlushState() {
		t.Fatal("flush with nothing dirty")
	}
	sub, _ := s.Hub.Subscribe(0, false)
	ingest(t, s, ev("l-001", "heartbeat", "", ""), ev("l-002", "heartbeat", "", ""))
	ingest(t, s, ev("l-001", "heartbeat", "", ""))
	if !s.FlushState() {
		t.Fatal("expected flush")
	}
	m := <-sub.ch
	var st State
	json.Unmarshal(m.Data, &st)
	if m.Event != "state" || m.ID != 0 || st.Full || len(st.Lanes) != 2 {
		t.Fatalf("delta %s id=%d %+v", m.Event, m.ID, st)
	}
	if len(sub.ch) != 0 {
		t.Fatal("expected exactly one coalesced state message")
	}
}

func TestHTTPIntegration(t *testing.T) {
	s, _ := newTest(t, "")
	ts := httptest.NewServer(s.Handler(nil))
	defer ts.Close()

	// Open SSE first.
	req, _ := http.NewRequest("GET", ts.URL+"/stream", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("content-type %q", ct)
	}
	got := make(chan [2]string, 64)
	go func() {
		br := bufio.NewReader(resp.Body)
		var id, name string
		for {
			line, err := br.ReadString('\n')
			if err != nil {
				close(got)
				return
			}
			line = strings.TrimRight(line, "\n")
			switch {
			case strings.HasPrefix(line, "id: "):
				id = line[4:]
			case strings.HasPrefix(line, "event: "):
				name = line[7:]
			case line == "" && name != "":
				got <- [2]string{name, id}
				name, id = "", ""
			}
		}
	}()
	wait := func(name string) string {
		t.Helper()
		timeout := time.After(5 * time.Second)
		for {
			select {
			case m, ok := <-got:
				if !ok {
					t.Fatalf("stream closed waiting for %s", name)
				}
				if m[0] == name {
					return m[1]
				}
			case <-timeout:
				t.Fatalf("timeout waiting for %s", name)
			}
		}
	}
	wait("state") // full snapshot on connect

	body := ev("l-042", "lane_start", "", "") + "\n" + ev("l-042", "sanitizer", "a1b2c3d4e5f6", "heap-use-after-free") + "\n\nnot json\n"
	r, err := http.Post(ts.URL+"/events", "application/x-ndjson", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	var ir IngestResult
	json.NewDecoder(r.Body).Decode(&ir)
	r.Body.Close()
	if r.StatusCode != 202 || ir.Accepted != 2 || ir.Rejected != 1 {
		t.Fatalf("POST /events %d %+v", r.StatusCode, ir)
	}
	failID := wait("failure")
	if failID == "" {
		t.Fatal("failure event without id")
	}

	r, _ = http.Get(ts.URL + "/api/state")
	var st State
	json.NewDecoder(r.Body).Decode(&st)
	r.Body.Close()
	if st.Scores["l-042"] != 5 || len(st.Lanes) != 1 || st.Lanes[0].Game != "brogue" || st.Lanes[0].Pos != 1 || st.Heat.ID != "h-0001" {
		t.Fatalf("state %+v", st)
	}

	// Focus round-trip.
	r, _ = http.Post(ts.URL+"/wall/focus", "application/json", strings.NewReader(`{"lane_id":"l-042"}`))
	r.Body.Close()
	if r.StatusCode != 202 {
		t.Fatal(r.StatusCode)
	}
	wait("focus")

	// Last-Event-ID replay: a new client resuming before the failure gets it again.
	req2, _ := http.NewRequest("GET", ts.URL+"/stream", nil)
	req2.Header.Set("Last-Event-ID", "0")
	resp2, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 64<<10)
	var acc strings.Builder
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && !strings.Contains(acc.String(), "event: focus") {
		n, err := resp2.Body.Read(buf)
		acc.Write(buf[:n])
		if err != nil {
			break
		}
	}
	resp2.Body.Close()
	if !strings.Contains(acc.String(), "id: "+failID+"\nevent: failure") {
		t.Fatalf("replay missing failure:\n%s", acc.String())
	}

	// Lane card and screens.
	r, _ = http.Get(ts.URL + "/api/lanes/l-042")
	var card LaneCard
	json.NewDecoder(r.Body).Decode(&card)
	r.Body.Close()
	if len(card.Failures) != 1 || card.Failures[0].Points != 5 || card.Lane.Seed != 12345 {
		t.Fatalf("card %+v", card)
	}
	r, _ = http.Get(ts.URL + "/api/lanes/nope")
	r.Body.Close()
	if r.StatusCode != 404 {
		t.Fatal("unknown lane should 404")
	}

	sresp, err := http.Get(ts.URL + "/lanes/l-042/screen/stream")
	if err != nil {
		t.Fatal(err)
	}
	defer sresp.Body.Close()
	frame := `{"ts":"2026-10-21T12:00:02Z","cols":100,"rows":34,"cursor":[1,2],"lines":["@ hello"]}`
	r, _ = http.Post(ts.URL+"/lanes/l-042/screen", "application/json", strings.NewReader(frame))
	r.Body.Close()
	if r.StatusCode != 202 {
		t.Fatal(r.StatusCode)
	}
	sbr := bufio.NewReader(sresp.Body)
	done := make(chan string, 1)
	go func() {
		for {
			l, err := sbr.ReadString('\n')
			if err != nil {
				done <- ""
				return
			}
			if strings.HasPrefix(l, "data: ") {
				done <- l
				return
			}
		}
	}()
	select {
	case l := <-done:
		if !strings.Contains(l, `"lane_id":"l-042"`) || !strings.Contains(l, "@ hello") {
			t.Fatalf("frame %q", l)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no screen frame")
	}
}
