package agents

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const sample = `{
  "version": 1,
  "defaults": {"profile": "fast"},
  "profiles": {
    "fast": {"model": "m-a", "style": "win", "interval_ms": 1000, "notes": "x"},
    "deep": {"model": "m-b", "style": "explore", "interval_ms": 3000, "max_tokens": 512, "sampling": {"temperature": 0.7}}
  },
  "models": {"m-b": {"min_interval_ms": 1700, "max_concurrent_lanes": 3, "notes": "probe"}},
  "games": {"greed": {"profile": "deep", "keys": ["hjkl", "<Esc>", "<Space>", "<C-x>", "<F12>", "<lt>"], "prompt": "greed.md", "goal": "<eat> & win", "interval_ms": 500}},
  "x_extra": {"kept": true}
}
`

func mustDecode(t *testing.T, s string) any {
	t.Helper()
	v, err := decode([]byte(s))
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func fields(errs []FieldError) map[string]bool {
	m := map[string]bool{}
	for _, e := range errs {
		m[e.Field] = true
	}
	return m
}

func TestValidateOK(t *testing.T) {
	errs, warns := Validate(mustDecode(t, sample), Known{Models: []string{"m-a", "m-b"}, Games: []string{"greed"}})
	if len(errs) > 0 {
		t.Fatalf("unexpected errors: %+v", errs)
	}
	if len(warns) != 1 || warns[0].Field != "x_extra" {
		t.Fatalf("want one warning for x_extra, got %+v", warns)
	}
	// Without lists, ids are not checked.
	if errs, _ := Validate(mustDecode(t, sample), Known{}); len(errs) > 0 {
		t.Fatalf("unexpected errors: %+v", errs)
	}
}

func TestValidatePromptFile(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "greed.md"), []byte("x"), 0o644)
	_, warns := Validate(mustDecode(t, sample), Known{PromptsDir: dir})
	for _, w := range warns {
		if strings.HasPrefix(w.Field, "games.") {
			t.Errorf("unexpected warning %+v", w)
		}
	}
	os.Remove(filepath.Join(dir, "greed.md"))
	_, warns = Validate(mustDecode(t, sample), Known{PromptsDir: dir})
	if !fields(warns)["games.greed.prompt"] {
		t.Errorf("missing prompt file not warned: %+v", warns)
	}
}

func TestValidateErrors(t *testing.T) {
	bad := `{
	  "version": 2,
	  "defaults": {"profile": "nope"},
	  "profiles": {
	    "a": {"model": "m-z", "style": "fast", "interval_ms": 100, "max_tokens": 0, "sampling": [1], "notes": 3, "tmp": 1},
	    "b": {"style": "win", "interval_ms": 1.5}
	  },
	  "models": {"m-z": {"min_interval_ms": -5, "max_concurrent_lanes": "3"}, "m-a": {"min_interval_ms": 0, "max_concurrent_lanes": 0, "notes": 5}, "m-b": {}},
	  "games": {"chess": {"profile": "zzz", "keys": ["a", 1, "<Esq>", ""], "goal": 4, "interval_ms": 10, "prompt": "../x.md"},
	            "greed": {"prompt": "sub/greed.md"}}
	}`
	errs, warns := Validate(mustDecode(t, bad), Known{Models: []string{"m-a", "m-b"}, Games: []string{"greed"}})
	got := fields(errs)
	for _, f := range []string{
		"version", "defaults.profile",
		"profiles.a.model", "profiles.a.style", "profiles.a.interval_ms", "profiles.a.max_tokens",
		"profiles.a.sampling", "profiles.a.notes", "profiles.b.model", "profiles.b.interval_ms",
		"models.m-z", "models.m-z.min_interval_ms", "models.m-z.max_concurrent_lanes",
		"models.m-a.max_concurrent_lanes", "models.m-a.notes", "models.m-b.min_interval_ms",
		"games.chess", "games.chess.profile", "games.chess.keys[1]", "games.chess.keys[2]", "games.chess.keys[3]",
		"games.chess.goal", "games.chess.interval_ms", "games.chess.prompt", "games.greed.prompt",
	} {
		if !got[f] {
			t.Errorf("missing error for %s; got %+v", f, errs)
		}
	}
	if got["models.m-a.min_interval_ms"] || got["games.chess.keys[0]"] {
		t.Errorf("valid values flagged: %+v", errs)
	}
	if !fields(warns)["profiles.a.tmp"] || fields(errs)["profiles.a.tmp"] {
		t.Errorf("unknown field should be a warning only: errs %+v warns %+v", errs, warns)
	}
	if errs, _ := Validate(mustDecode(t, `[1]`), Known{}); len(errs) != 1 {
		t.Errorf("non-object: %+v", errs)
	}
}

func setup(t *testing.T, content string, writable bool) (*Editor, *httptest.Server) {
	t.Helper()
	dir := t.TempDir()
	e := &Editor{Path: filepath.Join(dir, "agents.json"), Writable: writable}
	if content != "" {
		if err := os.WriteFile(e.Path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	models := filepath.Join(dir, "models.json")
	os.WriteFile(models, []byte(`{"providers":{"p":{"models":[{"id":"m-b"},{"id":"m-a"}]}}}`), 0o644)
	games := filepath.Join(dir, "games.toml")
	os.WriteFile(games, []byte("[games.greed]\ncommand=[\"greed\"]\n[games.brogue]\ncommand=[\"b\"]\n"), 0o644)
	e.ModelsPath, e.GamesPath = models, games
	if err := e.Check(); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	e.Register(mux)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return e, srv
}

type getResp struct {
	Config   map[string]any `json:"config"`
	Models   []string       `json:"models"`
	Games    []string       `json:"games"`
	ETag     string         `json:"etag"`
	Exists   bool           `json:"exists"`
	Writable bool           `json:"writable"`
}

func get(t *testing.T, url string) getResp {
	t.Helper()
	r, err := http.Get(url + "/api/agents")
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var g getResp
	if err := json.NewDecoder(r.Body).Decode(&g); err != nil {
		t.Fatal(err)
	}
	return g
}

func put(t *testing.T, url, etag, body string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest("PUT", url+"/api/agents", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if etag != "" {
		req.Header.Set("If-Match", etag)
	}
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var m map[string]any
	json.NewDecoder(r.Body).Decode(&m)
	return r.StatusCode, m
}

func TestGetPutRoundTrip(t *testing.T) {
	e, srv := setup(t, sample, true)
	g := get(t, srv.URL)
	if !g.Exists || !g.Writable || g.ETag == "" || g.ETag == absentETag {
		t.Fatalf("bad GET: %+v", g)
	}
	if strings.Join(g.Models, ",") != "m-a,m-b" || strings.Join(g.Games, ",") != "brogue,greed" {
		t.Fatalf("lists: %v %v", g.Models, g.Games)
	}
	// Send the body the way a browser would (key order kept, no HTML escaping).
	body := strings.Replace(sample, `{"profile": "fast"}`, `{"profile": "deep"}`, 1)
	code, m := put(t, srv.URL, g.ETag, body)
	if code != 200 {
		t.Fatalf("PUT %d %v", code, m)
	}
	newTag, _ := m["etag"].(string)
	if newTag == "" || newTag == g.ETag {
		t.Fatalf("etag not updated: %v", m)
	}
	disk, _ := os.ReadFile(e.Path)
	if !strings.Contains(string(disk), `"profile": "deep"`) || !strings.Contains(string(disk), `"x_extra"`) ||
		!strings.Contains(string(disk), `"<eat> & win"`) || !strings.Contains(string(disk), `"temperature": 0.7`) {
		t.Fatalf("written file:\n%s", disk)
	}
	if strings.Index(string(disk), `"version"`) > strings.Index(string(disk), `"profiles"`) ||
		strings.Index(string(disk), `"fast": {`) > strings.Index(string(disk), `"deep": {`) {
		t.Errorf("key order not kept:\n%s", disk)
	}
	if bak, _ := os.ReadFile(e.Path + ".bak"); string(bak) != sample {
		t.Fatalf(".bak does not hold the previous content:\n%s", bak)
	}
	if st, _ := os.Stat(e.Path); st.Mode().Perm() != 0o600 {
		t.Errorf("mode not kept: %v", st.Mode())
	}
	if g2 := get(t, srv.URL); g2.ETag != newTag {
		t.Errorf("GET etag %s, PUT returned %s", g2.ETag, newTag)
	}
	// No temp files left behind.
	ents, _ := os.ReadDir(filepath.Dir(e.Path))
	for _, de := range ents {
		if strings.Contains(de.Name(), ".tmp-") {
			t.Errorf("temp file left: %s", de.Name())
		}
	}
}

func TestPutConflictAndInvalid(t *testing.T) {
	e, srv := setup(t, sample, true)
	g := get(t, srv.URL)
	body, _ := json.Marshal(g.Config)

	if code, _ := put(t, srv.URL, `"stale"`, string(body)); code != 409 {
		t.Errorf("stale etag: %d, want 409", code)
	}
	if code, _ := put(t, srv.URL, "", string(body)); code != 428 {
		t.Errorf("missing If-Match: %d, want 428", code)
	}
	code, m := put(t, srv.URL, g.ETag, `{"version":1,"defaults":{"profile":"fast"},"profiles":{"fast":{"model":"m-q","style":"win","interval_ms":10}}}`)
	if code != 400 {
		t.Fatalf("invalid: %d %v", code, m)
	}
	errs, _ := m["errors"].([]any)
	if len(errs) != 2 {
		t.Errorf("want 2 field errors, got %v", m["errors"])
	}
	if code, _ := put(t, srv.URL, g.ETag, `{nope`); code != 400 {
		t.Errorf("bad JSON: %d", code)
	}
	if disk, _ := os.ReadFile(e.Path); string(disk) != sample {
		t.Error("file changed by a rejected PUT")
	}
	// Someone edits the file by hand: the old etag is now stale.
	os.WriteFile(e.Path, []byte(sample+" "), 0o600)
	if code, _ := put(t, srv.URL, g.ETag, string(body)); code != 409 {
		t.Errorf("after external edit: %d, want 409", code)
	}
}

func TestReadOnlyAndCreate(t *testing.T) {
	_, ro := setup(t, sample, false)
	g := get(t, ro.URL)
	if g.Writable {
		t.Error("writable reported on a read-only editor")
	}
	body, _ := json.Marshal(g.Config)
	if code, _ := put(t, ro.URL, g.ETag, string(body)); code != 403 {
		t.Errorf("read-only PUT: %d, want 403", code)
	}

	e, srv := setup(t, "", true)
	g = get(t, srv.URL)
	if g.Exists || g.ETag != absentETag {
		t.Fatalf("missing file: %+v", g)
	}
	if code, m := put(t, srv.URL, g.ETag, sample); code != 200 {
		t.Fatalf("create: %d %v", code, m)
	}
	if _, err := os.Stat(e.Path + ".bak"); err == nil {
		t.Error("a .bak was written for a new file")
	}
}

func TestIsLoopback(t *testing.T) {
	for addr, want := range map[string]bool{
		"127.0.0.1:8088": true, "localhost:1": true, "[::1]:80": true,
		":8080": false, "0.0.0.0:8080": false, "10.0.0.2:80": false, "nonsense": false,
	} {
		if IsLoopback(addr) != want {
			t.Errorf("IsLoopback(%q) != %v", addr, want)
		}
	}
}
