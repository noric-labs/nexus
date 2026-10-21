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
matching config value. The agents editor flags are described in its own section below.

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
- `web/`: the embedded race UI (`go:embed`), described below.
- `tools/fake_lanes.py`: fake lane driver for demos (stdlib Python).
- `tools/smoke_ui.mjs`: headless smoke test of the UI against a running scoreboard.

## Race UI (`web/`)

Plain HTML and ES modules with no build step and no network access beyond the scoreboard
itself. It uses only the public API: `/stream`, `/api/lanes/{id}`, `/lanes/{id}/screen/stream`
and `POST /wall/focus`.

- Everything is drawn on one 960x540 canvas. The canvas is scaled by the largest integer that
  fits the window (2x on a 1920x1080 display) with `image-rendering: pixelated`. Drawing uses
  only `fillRect`, `drawImage` and `putImageData`, so pixels stay crisp.
- The track is an oval. Distance is the heat score, and one lap is `lap_points`. Lanes
  without a score wait on a grid behind the line. The top 10 cars are drawn larger and carry
  number plates. Car body colour is the constructor (fault config) and the stripe is the game.
- The timing tower lists every lane with its gap to the leader, in two columns when there are
  more than about 50 lanes. Tabs: HEAT, CONSTR (fault configs), GAMES, SEASON.
- Each failure type has an effect in its own colour. A sanitizer report explodes, a crash
  spins out, a hang stalls and smokes, an error exit sputters and a visual fault glitches the
  sprite.
- During the pit the track shows the podium and an instant replay. The replay cycles through
  the heat's top moments: the biggest hits, new clusters, first blood and lead changes. After
  a reload, the UI rebuilds these moments from the failure logs of the front runners.
- Clicking a car (or a tower row) opens the lane panel. It shows the live screen (80x24, or
  100x34 for Brogue), the config card, the failure log with replay bundle paths, and a
  "focus on wall" button.
- Sound effects are chiptune, synthesised with WebAudio. They are muted by default.
- The soundtrack is generative drum & bass (`web/js/music.js`, 172 BPM), synthesised live with
  no audio files. Each heat gets its own song, seeded by the heat id, and it builds with the race.
- Keys: `1`-`4` switch tabs, `A` cycles the tabs automatically, `M` toggles all sound
  (effects and music), `B` toggles the music only, `F` sends
  the open lane to the wall, `N`/`P` move to the next or previous lane, and `Esc` closes the
  panel.
- The bitmap font (`web/js/font.js`) was drawn for this project. It is not a third-party font
  and carries no separate licence.

Demo and check:

```sh
./scoreboard -heat 60s -pit 10s -listen :8088 -db /tmp/sb.db &
python3 tools/fake_lanes.py --url http://localhost:8088 --lanes 40     # open http://localhost:8088/
node tools/smoke_ui.mjs --url http://localhost:8088 --png /tmp/shots --wait-pit
```

`smoke_ui.mjs` runs the UI modules in node against the live server. It uses a software
canvas that rejects any 2D call outside the set above, an SSE client and a stub
AudioContext. It clicks a car, focuses the wall, switches tabs, waits through a pit, fails on
any JS error, and writes PNG screenshots.

## Agents editor (`/agents`)

A form editor for the lane agent config `lane/agents.json` (profiles, model limits and
per-game overrides). It is off unless `-agents-config` is given:

```sh
go build -o ../.build/scoreboard-dev .
../.build/scoreboard-dev -listen 127.0.0.1:8088 -heat 60s -pit 12s \
  -agents-config ../lane/agents.json -models ../lane/models.json -games ../harness/games.toml
open http://127.0.0.1:8088/agents
```

- Flags: `-agents-config PATH` enables it. `-models` (pi `models.json`) and `-games`
  (`games.toml`) are optional: they fill the dropdowns and make unknown model ids or game
  names an error. `-agents-writable` allows saving on a non-loopback listen address.
- Saving is allowed only when the scoreboard listens on loopback (`127.0.0.1`, `::1`,
  `localhost`) or with `-agents-writable`. Otherwise the page is read-only and PUT returns 403.
  The default `:8080` is not loopback.
- `GET /api/agents` returns `{config, models, games, path, etag, exists, writable}`. A missing
  file comes back as an empty skeleton with etag `"absent"`. A file that is not valid JSON
  comes back with `parse_error` and `raw`.
- `PUT /api/agents` takes the whole config with `If-Match: <etag>`. Responses: 200 `{etag}`,
  400 `{error, errors:[{field, error}]}`, 409 when the file changed since that etag, 428 without
  `If-Match`, 403 when read-only. The write goes to a temp file and is renamed into place. The
  previous content is kept in `agents.json.bak`, and the file mode is preserved.
- Validation follows the lane runtime's rules. A save with errors is refused:
  - `version` is 1.
  - `defaults.profile` and every `games.*.profile` name an existing profile.
  - Each profile needs `model`, `style` (`win`|`explore`) and an integer `interval_ms >= 250`.
    `max_tokens` is `>= 1` and `sampling` is an object.
  - `models.*.min_interval_ms` is `>= 0`, `max_concurrent_lanes` is `>= 1`, and `notes` is a string.
  - `games.*.interval_ms` is `>= 250`. `prompt` is a plain file name in `lane/prompts`, with no
    directories and no leading dot. `keys` is an array of strings in `lane/game.ts` notation
    (`"hjkl"`, `"<Esc>"`, `"<Space>"`, `"<C-x>"`, `"<F1>"`, and `<lt>` for a literal `<`). An
    unknown `<Name>` is an error.
  - Warnings don't block a save. Unknown fields get a warning and are kept. So does a prompt
    file missing from `prompts/` next to the config file. GET also reports the errors and
    warnings for the file as it is on disk.
- The file is written with the key order the page sends, which is the file's own order, so a
  one-field edit is a one-line diff.
- The page is self-contained `web/agents.html` (inline CSS and JS, no external URLs). The race UI
  does not link to it.
- In a Minimal session the scoreboard edits the copy in its own box, not your checkout or the
  lanes. Run the editor with the dev binary on the Mac against the checkout's
  `lane/agents.json`, and let `host-up.sh` take the file to the lanes.

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
