# scoreboard

Go service for the Torment Nexus race. It ingests lane events, runs the heat/pit cycle,
scores failures, keeps the standings in SQLite, and serves SSE plus the embedded UI.
The interfaces are defined in [`docs/CONTRACTS.md`](../docs/CONTRACTS.md).

## Run

```sh
export GOCACHE=/state/gocache GOMODCACHE=/state/gomodcache
go build -o scoreboard .
./scoreboard                                         # reads ./scoreboard.toml if present
./scoreboard -heat 30s -pit 5s -db /tmp/sb.db -listen :8080   # short heats for testing
go vet ./... && go test ./...
```

Flags: `-config`, `-listen`, `-db`, `-heat`, `-pit`, `-lap-points`. A flag overrides the
matching config value.

Smoke test:

```sh
curl -s localhost:8080/api/state | head -c 300
printf '%s\n' '{"v":1,"event_id":"e1","ts":"2026-10-21T12:00:00Z","heat_id":"","lane_id":"l-001","game":"greed","fault":"none","seed":1,"model":"m","type":"crash","stack_hash":"a1b2c3d4e5f6"}' \
  | curl -s -XPOST --data-binary @- localhost:8080/events
curl -N localhost:8080/stream
```

## Config (`scoreboard.toml`)

See `scoreboard.toml.example`. Every key is optional: `listen`, `db`, `heat`, `pit`,
`lap_points`, `state_hz` (capped at 4), `ring_size`, `lane_stale`, `failure_log`,
`season_points` and `[weights]`. Weights you set are merged over the defaults.

## Layout

- `internal/score`: pure rules (weights, the `0.5^(n-1)` decay, ranking, season points,
  constructor and game standings).
- `internal/server`: SQLite store (WAL), heat cycle, ingest, SSE hub, screen fan-out, HTTP.
- `web/`: the embedded UI (`go:embed`). The current `index.html` is a placeholder table.
  The race UI replaces this directory and should use only the public API.

## API notes beyond the contract

- `POST /events` returns 202 with `{"accepted","duplicates","rejected","errors"}`.
  Invalid lines are skipped. The response is 400 only when no line in the body is valid.
- `GET /stream` always sends a full `state` (`"full":true`) first. After that, `state`
  messages are deltas (`"full":false`) with no SSE id. A delta contains only the changed
  `lanes`, but `heat`, `scores` and `standings` are always complete. `failure`, `heat`,
  `pit`, `ticker` and `focus` carry ids and are replayed from a ring buffer when the
  client sends `Last-Event-ID` (or `?last_event_id=`).
- `GET /lanes/{id}/screen/stream` emits `screen` events, starting with the latest frame.
  Each frame is the posted JSON with `lane_id` added. `GET /api/lanes/{id}/screen` returns
  the latest frame.
- `GET /healthz` returns `ok`.
