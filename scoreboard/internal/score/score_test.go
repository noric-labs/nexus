package score

import (
	"math"
	"testing"
	"time"
)

func near(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestDecayAndPoints(t *testing.T) {
	w := DefaultWeights()
	cases := []struct {
		typ  string
		n    int
		want float64
	}{
		{"sanitizer", 1, 5}, {"sanitizer", 2, 2.5}, {"sanitizer", 3, 1.25},
		{"crash", 1, 3}, {"crash", 4, 0.375}, {"hang", 1, 2}, {"visual", 2, 0.5},
		{"agent_error", 1, 0}, {"heartbeat", 1, 0},
	}
	for _, c := range cases {
		if got := w.Points(c.typ, c.n); !near(got, c.want) {
			t.Errorf("Points(%s,%d)=%v want %v", c.typ, c.n, got, c.want)
		}
	}
}

func TestClusterKey(t *testing.T) {
	if k := ClusterKey("crash", "a1b2c3d4e5f6", "x"); k != "a1b2c3d4e5f6" {
		t.Fatal(k)
	}
	if k := ClusterKey("hang", "", "no-output-60s"); k != "hang|no-output-60s" {
		t.Fatal(k)
	}
	if ClusterKey("hang", "", "a") == ClusterKey("visual", "", "a") {
		t.Fatal("type must be part of the fallback key")
	}
}

func TestTallyDecayPerLaneAndKey(t *testing.T) {
	tl := NewTally(DefaultWeights())
	t0 := time.Unix(0, 0)
	// lane A hits cluster X three times (5 + 2.5 + 1.25) and Y once (3).
	tl.Add("A", "sanitizer", "X", t0)
	tl.Add("A", "sanitizer", "X", t0)
	if n, p := tl.Add("A", "sanitizer", "X", t0); n != 3 || !near(p, 1.25) {
		t.Fatalf("n=%d p=%v", n, p)
	}
	tl.Add("A", "crash", "Y", t0)
	// lane B's first hit on X is n=1 again (decay is per lane).
	if n, p := tl.Add("B", "sanitizer", "X", t0); n != 1 || p != 5 {
		t.Fatalf("B n=%d p=%v", n, p)
	}
	a := tl.Get("A")
	if !near(a.Score, 11.75) || a.Hits != 4 || a.Clusters != 2 {
		t.Fatalf("A=%+v", a)
	}
	// same stack hash shares n across types
	if n, _ := tl.Add("A", "crash", "X", t0); n != 4 {
		t.Fatalf("shared key n=%d", n)
	}
	if tl.Peek("A", "X") != 5 || tl.Peek("C", "X") != 1 {
		t.Fatal("peek")
	}
}

func TestRankTieBreaks(t *testing.T) {
	t0 := time.Unix(100, 0)
	r := Rank([]Entry{
		{Lane: "c", Score: 5, LastScoreAt: t0.Add(2 * time.Second)},
		{Lane: "a", Score: 3, LastScoreAt: t0},
		{Lane: "b", Score: 5, LastScoreAt: t0.Add(time.Second)},
		{Lane: "z"}, {Lane: "y"},
	})
	want := []string{"b", "c", "a", "y", "z"}
	for i, w := range want {
		if r[i].Lane != w || r[i].Pos != i+1 {
			t.Fatalf("rank %d = %+v, want %s", i, r[i], w)
		}
	}
}

func TestSeasonPoints(t *testing.T) {
	tab := DefaultSeasonPoints()
	for pos, want := range map[int]int{1: 25, 2: 18, 3: 15, 10: 1, 11: 0, 0: 0} {
		if got := SeasonPointsFor(pos, 1, tab); got != want {
			t.Errorf("pos %d = %d want %d", pos, got, want)
		}
	}
	if SeasonPointsFor(1, 0, tab) != 0 {
		t.Error("zero score must not earn points")
	}
}

func TestSeasonAndGroups(t *testing.T) {
	rs := []HeatResult{
		{HeatID: "h-0001", Lane: "a", Game: "brogue", Fault: "malloc-1pct", Score: 10, Pos: 1, Points: 25},
		{HeatID: "h-0001", Lane: "b", Game: "greed", Fault: "none", Score: 4, Pos: 2, Points: 18},
		{HeatID: "h-0001", Lane: "c", Game: "brogue", Fault: "none", Score: 0, Pos: 3, Points: 0},
		{HeatID: "h-0002", Lane: "b", Game: "greed", Fault: "none", Score: 9, Pos: 1, Points: 25},
		{HeatID: "h-0002", Lane: "a", Game: "brogue", Fault: "malloc-1pct", Score: 2, Pos: 2, Points: 18},
	}
	s := Season(rs)
	// a and b both 43; b wins on... both have 1 win, 2 podiums; b has score 13 vs a 12.
	if s[0].Lane != "b" || s[0].Points != 43 || s[1].Lane != "a" || s[1].Points != 43 {
		t.Fatalf("season %+v", s)
	}
	if s[2].Lane != "c" || s[2].Podiums != 0 || s[2].Best != 0 || s[2].Heats != 1 {
		t.Fatalf("c %+v", s[2])
	}
	live := []LiveLane{{Lane: "a", Game: "brogue", Fault: "malloc-1pct", Score: 7}, {Lane: "d", Game: "dcss", Fault: "keystorm", Score: 1}}
	cons := Groups(live, rs, ByFault)
	if cons[0].Key != "malloc-1pct" || cons[0].SeasonPoints != 43 || cons[0].Score != 7 || cons[0].Lanes != 1 || cons[1].Key != "none" || cons[1].SeasonPoints != 43 || cons[2].Key != "keystorm" {
		t.Fatalf("constructors %+v", cons)
	}
	games := Groups(live, rs, ByGame)
	if games[0].Key != "brogue" || games[0].SeasonPoints != 43 || games[2].Key != "dcss" {
		t.Fatalf("games %+v", games)
	}
}
