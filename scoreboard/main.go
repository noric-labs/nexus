// Command scoreboard runs the Torment Nexus scoreboard: event ingest, heat
// cycle, scoring, standings, SSE and the embedded race UI.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/noric-labs/nexus/scoreboard/internal/agents"
	"github.com/noric-labs/nexus/scoreboard/internal/server"
	"github.com/noric-labs/nexus/scoreboard/web"
)

func main() {
	cfgPath := flag.String("config", "scoreboard.toml", "config file (missing is fine)")
	listen := flag.String("listen", "", "listen address (overrides config)")
	db := flag.String("db", "", "SQLite path (overrides config)")
	heat := flag.Duration("heat", 0, "heat duration, e.g. 30s (overrides config)")
	pit := flag.Duration("pit", 0, "pit duration, e.g. 5s (overrides config)")
	lap := flag.Float64("lap-points", 0, "points per lap (overrides config)")
	agentsCfg := flag.String("agents-config", "", "lane/agents.json to edit at /agents (empty: editor disabled)")
	agentsModels := flag.String("models", "", "lane/models.json: model ids for the agents editor (optional)")
	agentsGames := flag.String("games", "", "harness/games.toml: game names for the agents editor (optional)")
	agentsWritable := flag.Bool("agents-writable", false, "allow saving agents.json when listening on a non-loopback address")
	flag.Parse()

	cfg, err := server.LoadConfig(*cfgPath, true)
	if err != nil {
		log.Fatal(err)
	}
	if *listen != "" {
		cfg.Listen = *listen
	}
	if *db != "" {
		cfg.DB = *db
	}
	if *heat > 0 {
		cfg.Heat.Duration = *heat
	}
	if *pit > 0 {
		cfg.Pit.Duration = *pit
	}
	if *lap > 0 {
		cfg.LapPoints = *lap
	}
	store, err := server.OpenStore(cfg.DB)
	if err != nil {
		log.Fatal(err)
	}
	defer store.Close()
	srv, err := server.New(cfg, server.RealClock, store)
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go srv.Run(ctx)

	mux := http.NewServeMux()
	mux.Handle("/", srv.Handler(web.FS))
	mux.HandleFunc("GET /agents", func(w http.ResponseWriter, r *http.Request) {
		http.ServeFileFS(w, r, web.FS, "agents.html")
	})
	if *agentsCfg != "" {
		ed := &agents.Editor{Path: *agentsCfg, ModelsPath: *agentsModels, GamesPath: *agentsGames,
			Writable: *agentsWritable || agents.IsLoopback(cfg.Listen)}
		if err := ed.Check(); err != nil {
			log.Fatal(err)
		}
		ed.Register(mux)
		log.Printf("agents editor at /agents for %s (writable %v)", ed.Path, ed.Writable)
	}
	hs := &http.Server{Addr: cfg.Listen, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		hs.Shutdown(sctx)
	}()
	log.Printf("scoreboard on %s (db %s, heat %s, pit %s)", cfg.Listen, cfg.DB, cfg.Heat, cfg.Pit)
	if err := hs.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
