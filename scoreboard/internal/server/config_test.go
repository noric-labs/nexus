package server

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLoadConfig(t *testing.T) {
	p := filepath.Join(t.TempDir(), "scoreboard.toml")
	os.WriteFile(p, []byte("heat = \"30s\"\npit = \"5s\"\nstate_hz = 10\n[weights]\ncrash = 4\n"), 0o644)
	c, err := LoadConfig(p, false)
	if err != nil {
		t.Fatal(err)
	}
	if c.Heat.Duration != 30*time.Second || c.Pit.Duration != 5*time.Second || c.StateHz != 4 {
		t.Fatalf("%+v", c)
	}
	if c.Weights["crash"] != 4 || c.Weights["sanitizer"] != 5 || c.LapPoints != 20 || c.Listen != ":8080" {
		t.Fatalf("%+v", c)
	}
	if _, err := LoadConfig(filepath.Join(t.TempDir(), "missing.toml"), true); err != nil {
		t.Fatal(err)
	}
}
