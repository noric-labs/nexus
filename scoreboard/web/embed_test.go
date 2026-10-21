package web

import (
	"io/fs"
	"net/http"
	"net/http/httptest"
	"path"
	"regexp"
	"strings"
	"testing"
)

// Every module the page loads must be embedded, and served as JavaScript
// (module scripts are refused with any other MIME type).
func TestEmbeddedModulesResolve(t *testing.T) {
	idx, err := fs.ReadFile(FS, "index.html")
	if err != nil {
		t.Fatal(err)
	}
	src := regexp.MustCompile(`<script type="module" src="([^"]+)"`).FindAllStringSubmatch(string(idx), -1)
	if len(src) == 0 {
		t.Fatal("index.html loads no module")
	}
	imp := regexp.MustCompile(`(?m)^\s*import\s[^;]*?from\s+"(\./[^"]+)"`)
	seen := map[string]bool{}
	queue := []string{}
	for _, m := range src {
		queue = append(queue, m[1])
	}
	for len(queue) > 0 {
		p := queue[0]
		queue = queue[1:]
		if seen[p] {
			continue
		}
		seen[p] = true
		b, err := fs.ReadFile(FS, p)
		if err != nil {
			t.Fatalf("%s is referenced but not embedded: %v", p, err)
		}
		for _, m := range imp.FindAllStringSubmatch(string(b), -1) {
			queue = append(queue, path.Join(path.Dir(p), m[1]))
		}
		if strings.Contains(string(b), "http://") || strings.Contains(string(b), "https://") {
			t.Errorf("%s references an external URL; the UI must work offline", p)
		}
	}
	if len(seen) < 5 {
		t.Errorf("only %d modules reachable: %v", len(seen), seen)
	}

	srv := httptest.NewServer(http.FileServerFS(FS))
	defer srv.Close()
	for p := range seen {
		r, err := http.Get(srv.URL + "/" + p)
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		if ct := r.Header.Get("Content-Type"); r.StatusCode != 200 || !strings.Contains(ct, "javascript") {
			t.Errorf("%s: status %d, content-type %q", p, r.StatusCode, ct)
		}
	}
}
