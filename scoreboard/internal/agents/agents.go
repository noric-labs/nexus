// Package agents serves the editor API for the lane agent configuration
// (lane/agents.json): GET /api/agents returns the file plus the known model
// ids and game names, PUT /api/agents validates and writes it atomically.
// Validation works on the generic JSON object, so keys this package does not
// know about are kept as they are.
package agents

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"

	"github.com/BurntSushi/toml"
)

const (
	maxBody     = 1 << 20
	minInterval = 250
	// absentETag is the etag of a config file that does not exist yet. A PUT
	// with If-Match: "absent" creates it.
	absentETag = `"absent"`
)

// Editor serves the agents config API for one file.
type Editor struct {
	Path       string // agents.json
	ModelsPath string // lane/models.json; optional
	GamesPath  string // harness/games.toml; optional
	Writable   bool   // PUT allowed

	mu sync.Mutex
}

// FieldError is one validation failure. Field is a dotted path such as
// "profiles.fast.interval_ms".
type FieldError struct {
	Field string `json:"field"`
	Error string `json:"error"`
}

// IsLoopback reports whether a listen address only accepts local
// connections. ":8080" and "0.0.0.0:8080" are not loopback.
func IsLoopback(listen string) bool {
	host, _, err := net.SplitHostPort(listen)
	if err != nil {
		return false
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// Check loads the optional models and games files once, so a bad path fails
// at startup instead of on the first request.
func (e *Editor) Check() error {
	if _, err := LoadModels(e.ModelsPath); err != nil {
		return err
	}
	_, err := LoadGames(e.GamesPath)
	return err
}

// Register adds GET/PUT /api/agents to mux.
func (e *Editor) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/agents", e.handleGet)
	mux.HandleFunc("PUT /api/agents", e.handlePut)
}

// LoadModels returns the model ids in a pi models.json
// ({"providers":{"<p>":{"models":[{"id":...}]}}}), sorted. An empty path
// returns nil (no list, so model ids are not checked).
func LoadModels(path string) ([]string, error) {
	if path == "" {
		return nil, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var f struct {
		Providers map[string]struct {
			Models []struct {
				ID string `json:"id"`
			} `json:"models"`
		} `json:"providers"`
	}
	if err := json.Unmarshal(b, &f); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	seen := map[string]bool{}
	ids := []string{}
	for _, p := range f.Providers {
		for _, m := range p.Models {
			if m.ID != "" && !seen[m.ID] {
				seen[m.ID] = true
				ids = append(ids, m.ID)
			}
		}
	}
	sort.Strings(ids)
	return ids, nil
}

// LoadGames returns the [games.<name>] table names in harness/games.toml,
// sorted. An empty path returns nil.
func LoadGames(path string) ([]string, error) {
	if path == "" {
		return nil, nil
	}
	var f struct {
		Games map[string]toml.Primitive `toml:"games"`
	}
	if _, err := toml.DecodeFile(path, &f); err != nil {
		return nil, err
	}
	names := []string{}
	for n := range f.Games {
		names = append(names, n)
	}
	sort.Strings(names)
	return names, nil
}

func etagOf(b []byte) string {
	h := sha256.Sum256(b)
	return `"` + hex.EncodeToString(h[:8]) + `"`
}

// read returns the file bytes and etag; a missing file gives (nil, absentETag).
func (e *Editor) read() ([]byte, string, error) {
	b, err := os.ReadFile(e.Path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, absentETag, nil
	}
	if err != nil {
		return nil, "", err
	}
	return b, etagOf(b), nil
}

func skeleton() map[string]any {
	return map[string]any{
		"version":  json.Number("1"),
		"defaults": map[string]any{},
		"profiles": map[string]any{},
		"models":   map[string]any{},
		"games":    map[string]any{},
	}
}

func decode(b []byte) (any, error) {
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	var v any
	if err := d.Decode(&v); err != nil {
		return nil, err
	}
	if d.More() {
		return nil, errors.New("trailing data after the JSON value")
	}
	return v, nil
}

func (e *Editor) lists() (models, games []string, err error) {
	if models, err = LoadModels(e.ModelsPath); err != nil {
		return nil, nil, err
	}
	games, err = LoadGames(e.GamesPath)
	return models, games, err
}

func (e *Editor) handleGet(w http.ResponseWriter, r *http.Request) {
	e.mu.Lock()
	b, etag, err := e.read()
	e.mu.Unlock()
	if err != nil {
		writeJSON(w, 500, map[string]any{"error": err.Error()})
		return
	}
	// The file bytes go out as they are, so the editor keeps the key order.
	var cfg any = skeleton()
	parseErr := ""
	if b != nil {
		if _, err := decode(b); err != nil {
			cfg, parseErr = nil, err.Error()
		} else {
			cfg = json.RawMessage(b)
		}
	}
	var fileErrs, fileWarns []FieldError
	models, games, err := e.lists()
	if err != nil {
		writeJSON(w, 500, map[string]any{"error": err.Error()})
		return
	}
	if b != nil && parseErr == "" {
		v, _ := decode(b)
		fileErrs, fileWarns = Validate(v, e.known(models, games))
	}
	w.Header().Set("ETag", etag)
	resp := map[string]any{
		"errors":   nonNilFE(fileErrs),
		"warnings": nonNilFE(fileWarns),
		"config":   cfg,
		"models":   nonNil(models),
		"games":    nonNil(games),
		"path":     e.Path,
		"etag":     etag,
		"exists":   b != nil,
		"writable": e.Writable,
	}
	if parseErr != "" {
		// The UI shows the raw text so it can be fixed by hand.
		resp["parse_error"] = parseErr
		resp["raw"] = string(b)
	}
	writeJSON(w, 200, resp)
}

func nonNilFE(s []FieldError) []FieldError {
	if s == nil {
		return []FieldError{}
	}
	return s
}

// known builds the reference lists for Validate. Prompts live in prompts/
// next to agents.json (lane/prompts).
func (e *Editor) known(models, games []string) Known {
	return Known{Models: models, Games: games, PromptsDir: filepath.Join(filepath.Dir(e.Path), "prompts")}
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func (e *Editor) handlePut(w http.ResponseWriter, r *http.Request) {
	if !e.Writable {
		writeJSON(w, 403, map[string]any{"error": "read-only: start the scoreboard on a loopback address or with -agents-writable"})
		return
	}
	match := r.Header.Get("If-Match")
	if match == "" {
		writeJSON(w, 428, map[string]any{"error": "If-Match header (the etag from GET) is required"})
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		writeJSON(w, 400, map[string]any{"error": err.Error()})
		return
	}
	cfg, err := decode(body)
	if err != nil {
		writeJSON(w, 400, map[string]any{"error": "invalid JSON: " + err.Error(),
			"errors": []FieldError{{Field: "", Error: "invalid JSON: " + err.Error()}}})
		return
	}
	models, games, err := e.lists()
	if err != nil {
		writeJSON(w, 500, map[string]any{"error": err.Error()})
		return
	}
	errs, warns := Validate(cfg, e.known(models, games))
	if len(errs) > 0 {
		writeJSON(w, 400, map[string]any{"error": fmt.Sprintf("%d validation error(s)", len(errs)),
			"errors": errs, "warnings": nonNilFE(warns)})
		return
	}
	// Reindent the body as sent rather than re-encoding the decoded value:
	// this keeps the client's key order and the file stays diff-friendly.
	var buf bytes.Buffer
	if err := json.Indent(&buf, bytes.TrimSpace(body), "", "  "); err != nil {
		writeJSON(w, 500, map[string]any{"error": err.Error()})
		return
	}
	buf.WriteByte('\n')
	out := buf.Bytes()

	e.mu.Lock()
	defer e.mu.Unlock()
	old, cur, err := e.read()
	if err != nil {
		writeJSON(w, 500, map[string]any{"error": err.Error()})
		return
	}
	if match != cur && match != "*" {
		writeJSON(w, 409, map[string]any{"error": "the file changed since it was loaded; reload and reapply your edits", "etag": cur})
		return
	}
	if err := writeAtomic(e.Path, out, old); err != nil {
		writeJSON(w, 500, map[string]any{"error": err.Error()})
		return
	}
	etag := etagOf(out)
	w.Header().Set("ETag", etag)
	writeJSON(w, 200, map[string]any{"etag": etag, "path": e.Path, "warnings": nonNilFE(warns)})
}

// writeAtomic writes data to path through a temp file in the same directory
// and a rename. When old is non-nil it is saved to path+".bak" first.
func writeAtomic(path string, data, old []byte) error {
	mode := fs.FileMode(0o644)
	if st, err := os.Stat(path); err == nil {
		mode = st.Mode().Perm()
	}
	if old != nil {
		if err := writeTemp(path+".bak", old, mode); err != nil {
			return fmt.Errorf("backup: %w", err)
		}
	}
	return writeTemp(path, data, mode)
}

func writeTemp(path string, data []byte, mode fs.FileMode) error {
	f, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp) // no-op after a successful rename
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

// Known holds what Validate checks references against. A nil list or an
// empty PromptsDir skips that check.
type Known struct {
	Models     []string
	Games      []string
	PromptsDir string // a missing prompt file is a warning
}

// Field names the runtime reads (lane, D-040). Anything else is a warning.
var knownKeys = map[string][]string{
	"":         {"version", "defaults", "profiles", "models", "games"},
	"defaults": {"profile"},
	"profile":  {"model", "style", "interval_ms", "max_tokens", "sampling", "notes"},
	"model":    {"min_interval_ms", "max_concurrent_lanes", "notes"},
	"game":     {"profile", "prompt", "keys", "goal", "interval_ms"},
}

// Validate checks a decoded agents.json (numbers as json.Number) with the
// runtime's rules. It returns errors (the file is refused) and warnings
// (unknown fields, missing prompt files). Unknown keys are kept.
func Validate(cfg any, k Known) (errs, warns []FieldError) {
	v := &validator{}
	root, ok := cfg.(map[string]any)
	if !ok {
		v.add("", "the config must be a JSON object")
		return v.errs, nil
	}
	models, games := k.Models, k.Games
	v.unknown(root, "", "")
	if n, ok := root["version"]; !ok {
		v.add("version", "required (1)")
	} else if i, ok := intOf(n); !ok || i != 1 {
		v.add("version", "must be 1")
	}

	profiles := v.object(root, "profiles", "profiles", true)
	modelSet, gameSet := setOf(models), setOf(games)

	if defs := v.object(root, "defaults", "defaults", true); defs != nil {
		v.unknown(defs, "defaults", "defaults")
		if p, ok := defs["profile"]; !ok {
			v.add("defaults.profile", "required")
		} else {
			v.profileRef("defaults.profile", p, profiles)
		}
	}

	for _, name := range sortedKeys(profiles) {
		f := "profiles." + name
		if strings.TrimSpace(name) == "" {
			v.add(f, "profile name must not be empty")
		}
		p, ok := profiles[name].(map[string]any)
		if !ok {
			v.add(f, "must be an object")
			continue
		}
		v.unknown(p, "profile", f)
		if m, ok := p["model"]; !ok {
			v.add(f+".model", "required")
		} else if s, ok := m.(string); !ok || s == "" {
			v.add(f+".model", "must be a non-empty string")
		} else if modelSet != nil && !modelSet[s] {
			v.add(f+".model", fmt.Sprintf("unknown model %q", s))
		}
		if s, ok := p["style"]; !ok {
			v.add(f+".style", `required ("win" or "explore")`)
		} else if s != "win" && s != "explore" {
			v.add(f+".style", `must be "win" or "explore"`)
		}
		if _, ok := p["interval_ms"]; !ok {
			v.add(f+".interval_ms", fmt.Sprintf("required (integer >= %d)", minInterval))
		} else {
			v.intMin(p, "interval_ms", f+".interval_ms", minInterval)
		}
		v.intMin(p, "max_tokens", f+".max_tokens", 1)
		if s, ok := p["sampling"]; ok {
			if _, ok := s.(map[string]any); !ok {
				v.add(f+".sampling", "must be a JSON object")
			}
		}
		v.str(p, "notes", f+".notes")
	}

	mods := v.object(root, "models", "models", false)
	for _, id := range sortedKeys(mods) {
		f := "models." + id
		if modelSet != nil && !modelSet[id] {
			v.add(f, fmt.Sprintf("unknown model %q", id))
		}
		m, ok := mods[id].(map[string]any)
		if !ok {
			v.add(f, "must be an object")
			continue
		}
		v.unknown(m, "model", f)
		if _, ok := m["min_interval_ms"]; !ok {
			v.add(f+".min_interval_ms", "required (integer >= 0)")
		} else {
			v.intMin(m, "min_interval_ms", f+".min_interval_ms", 0)
		}
		v.intMin(m, "max_concurrent_lanes", f+".max_concurrent_lanes", 1)
		v.str(m, "notes", f+".notes")
	}

	gs := v.object(root, "games", "games", false)
	for _, name := range sortedKeys(gs) {
		f := "games." + name
		if gameSet != nil && !gameSet[name] {
			v.add(f, fmt.Sprintf("unknown game %q", name))
		}
		g, ok := gs[name].(map[string]any)
		if !ok {
			v.add(f, "must be an object")
			continue
		}
		v.unknown(g, "game", f)
		if p, ok := g["profile"]; ok {
			v.profileRef(f+".profile", p, profiles)
		}
		v.prompt(g, f+".prompt", k.PromptsDir)
		v.str(g, "goal", f+".goal")
		if k, ok := g["keys"]; ok {
			arr, ok := k.([]any)
			if !ok {
				v.add(f+".keys", "must be an array of strings")
			} else {
				for i, x := range arr {
					ks, ok := x.(string)
					if !ok || ks == "" {
						v.add(fmt.Sprintf("%s.keys[%d]", f, i), "must be a non-empty string")
					} else if msg := checkKeys(ks); msg != "" {
						v.add(fmt.Sprintf("%s.keys[%d]", f, i), msg)
					}
				}
			}
		}
		v.intMin(g, "interval_ms", f+".interval_ms", minInterval)
	}
	return v.errs, v.warns
}

type validator struct{ errs, warns []FieldError }

func (v *validator) add(field, msg string)  { v.errs = append(v.errs, FieldError{field, msg}) }
func (v *validator) warn(field, msg string) { v.warns = append(v.warns, FieldError{field, msg}) }

func (v *validator) unknown(m map[string]any, kind, prefix string) {
	known := map[string]bool{}
	for _, k := range knownKeys[kind] {
		known[k] = true
	}
	for _, k := range sortedKeys(m) {
		if !known[k] {
			f := k
			if prefix != "" {
				f = prefix + "." + k
			}
			v.warn(f, "unknown field (kept, ignored by the runtime)")
		}
	}
}

// prompt: a plain file name in lane/prompts, no directories.
func (v *validator) prompt(g map[string]any, field, dir string) {
	x, ok := g["prompt"]
	if !ok {
		return
	}
	s, ok := x.(string)
	switch {
	case !ok || s == "":
		v.add(field, "must be a file name in lane/prompts")
	case strings.ContainsAny(s, `/\`) || s == "." || s == ".." || strings.HasPrefix(s, "."):
		v.add(field, "must be a plain file name in lane/prompts (no directories, no leading dot)")
	case dir != "":
		if _, err := os.Stat(filepath.Join(dir, s)); err != nil {
			v.warn(field, fmt.Sprintf("%s not found in %s", s, dir))
		}
	}
}

// namedKeys are the <...> names lane/game.ts understands (parseKeys).
var namedKeys = map[string]bool{}

func init() {
	for _, k := range strings.Fields("lt enter ret return cr esc escape tab btab bspace backspace bs space " +
		"up down left right home end pgup pageup ppage pgdn pagedown npage del delete dc ins insert ic") {
		namedKeys[k] = true
	}
}

var (
	keyTok   = regexp.MustCompile(`<([^<>\s]{1,12})>`)
	fnKey    = regexp.MustCompile(`^f([1-9]|1[0-2])$`)
	modKey   = regexp.MustCompile(`^(?i)[cm]-.$`)
	cmModKey = regexp.MustCompile(`^(?i)c-[cm]-.$`)
)

// checkKeys rejects a <...> token game.ts would not read as a named key (it
// would type it literally, which in an allow list is almost always a typo).
// Plain characters are allowed as they are; use <lt> for a literal "<".
func checkKeys(s string) string {
	for _, m := range keyTok.FindAllStringSubmatch(s, -1) {
		raw, low := m[1], strings.ToLower(m[1])
		if namedKeys[low] || fnKey.MatchString(low) || modKey.MatchString(raw) || cmModKey.MatchString(raw) {
			continue
		}
		return fmt.Sprintf("unknown named key <%s> (see lane/game.ts; <lt> for a literal <)", raw)
	}
	return ""
}

// object returns root[key] as an object. A missing key is an error only when
// required; any other type is always an error.
func (v *validator) object(root map[string]any, key, field string, required bool) map[string]any {
	x, ok := root[key]
	if !ok {
		if required {
			v.add(field, "required (an object)")
		}
		return nil
	}
	m, ok := x.(map[string]any)
	if !ok {
		v.add(field, "must be an object")
		return nil
	}
	return m
}

func (v *validator) profileRef(field string, p any, profiles map[string]any) {
	s, ok := p.(string)
	if !ok || s == "" {
		v.add(field, "must be a profile name")
		return
	}
	if _, ok := profiles[s]; !ok {
		v.add(field, fmt.Sprintf("unknown profile %q", s))
	}
}

func (v *validator) intMin(m map[string]any, key, field string, min int64) {
	x, ok := m[key]
	if !ok {
		return
	}
	if i, ok := intOf(x); !ok || i < min {
		v.add(field, fmt.Sprintf("must be an integer >= %d", min))
	}
}

func (v *validator) str(m map[string]any, key, field string) {
	if x, ok := m[key]; ok {
		if _, ok := x.(string); !ok {
			v.add(field, "must be a string")
		}
	}
}

func intOf(x any) (int64, bool) {
	n, ok := x.(json.Number)
	if !ok {
		return 0, false
	}
	i, err := n.Int64()
	return i, err == nil
}

func setOf(s []string) map[string]bool {
	if s == nil {
		return nil
	}
	m := make(map[string]bool, len(s))
	for _, x := range s {
		m[x] = true
	}
	return m
}

func sortedKeys(m map[string]any) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}
