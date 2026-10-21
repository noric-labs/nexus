package web

import (
	"io/fs"
	"strings"
	"testing"
)

// The agents.json editor is one self-contained page: no external URLs and no
// script or stylesheet files of its own (it must not add files under js/).
func TestAgentsPageSelfContained(t *testing.T) {
	b, err := fs.ReadFile(FS, "agents.html")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	for _, bad := range []string{"http://", "https://", "<script src", "<link "} {
		if strings.Contains(s, bad) {
			t.Errorf("agents.html contains %q", bad)
		}
	}
	if !strings.Contains(s, `fetch("api/agents"`) {
		t.Error("agents.html does not call api/agents")
	}
}
