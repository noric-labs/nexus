package server

import (
	"fmt"
	"os"
	"time"

	"github.com/BurntSushi/toml"

	"github.com/noric-labs/nexus/scoreboard/internal/score"
)

// Duration is a time.Duration that decodes from TOML strings like "15m".
type Duration struct{ time.Duration }

func (d *Duration) UnmarshalText(b []byte) error {
	v, err := time.ParseDuration(string(b))
	if err != nil {
		return err
	}
	d.Duration = v
	return nil
}

func (d Duration) MarshalText() ([]byte, error) { return []byte(d.String()), nil }

// Config is scoreboard.toml.
type Config struct {
	Listen       string        `toml:"listen"`
	DB           string        `toml:"db"`
	Heat         Duration      `toml:"heat"`
	Pit          Duration      `toml:"pit"`
	LapPoints    float64       `toml:"lap_points"`
	Weights      score.Weights `toml:"weights"`
	SeasonPoints []int         `toml:"season_points"`
	StateHz      float64       `toml:"state_hz"`   // max state delta rate on /stream (<= 4)
	RingSize     int           `toml:"ring_size"`  // SSE replay buffer (events with ids)
	LaneStale    Duration      `toml:"lane_stale"` // no event for this long => status "stale"
	FailureLog   int           `toml:"failure_log"`
}

// DefaultConfig returns the contract defaults.
func DefaultConfig() Config {
	return Config{
		Listen:       ":8080",
		DB:           "scoreboard.db",
		Heat:         Duration{15 * time.Minute},
		Pit:          Duration{1 * time.Minute},
		LapPoints:    20,
		Weights:      score.DefaultWeights(),
		SeasonPoints: score.DefaultSeasonPoints(),
		StateHz:      4,
		RingSize:     1024,
		LaneStale:    Duration{30 * time.Second},
		FailureLog:   200,
	}
}

// LoadConfig reads path over the defaults. A missing file is not an error
// when allowMissing is set. Weights given in the file are merged over defaults.
func LoadConfig(path string, allowMissing bool) (Config, error) {
	c := DefaultConfig()
	if path == "" {
		return c, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		if allowMissing && os.IsNotExist(err) {
			return c, nil
		}
		return c, err
	}
	var f Config
	if _, err := toml.Decode(string(b), &f); err != nil {
		return c, fmt.Errorf("%s: %w", path, err)
	}
	if f.Listen != "" {
		c.Listen = f.Listen
	}
	if f.DB != "" {
		c.DB = f.DB
	}
	if f.Heat.Duration > 0 {
		c.Heat = f.Heat
	}
	if f.Pit.Duration > 0 {
		c.Pit = f.Pit
	}
	if f.LapPoints > 0 {
		c.LapPoints = f.LapPoints
	}
	for k, v := range f.Weights {
		c.Weights[k] = v
	}
	if len(f.SeasonPoints) > 0 {
		c.SeasonPoints = f.SeasonPoints
	}
	if f.StateHz > 0 {
		c.StateHz = f.StateHz
	}
	if f.RingSize > 0 {
		c.RingSize = f.RingSize
	}
	if f.LaneStale.Duration > 0 {
		c.LaneStale = f.LaneStale
	}
	if f.FailureLog > 0 {
		c.FailureLog = f.FailureLog
	}
	err = c.Validate()
	return c, err
}

// Validate clamps and checks values.
func (c *Config) Validate() error {
	if c.StateHz > 4 || c.StateHz <= 0 {
		c.StateHz = 4
	}
	if c.Heat.Duration <= 0 || c.Pit.Duration <= 0 {
		return fmt.Errorf("heat and pit durations must be positive")
	}
	if c.LapPoints <= 0 {
		c.LapPoints = 20
	}
	if c.RingSize <= 0 {
		c.RingSize = 1024
	}
	return nil
}

// Clock lets tests inject time.
type Clock interface{ Now() time.Time }

type realClock struct{}

func (realClock) Now() time.Time { return time.Now().UTC() }

// RealClock is the wall clock.
var RealClock Clock = realClock{}
