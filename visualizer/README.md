# visualizer

A small native Bevy visualizer for the Torment Nexus scoreboard. It polls
`GET /api/state` and draws the same 960×540 pixel race as the web UI: the oval,
one car per lane, a header with the heat clock, and a timing tower.

Requires Rust 1.97.1 or newer (Bevy 0.20).

```sh
cargo run -- --url http://localhost:8080
```

Options:

| Flag | Meaning |
| --- | --- |
| `--url URL` | scoreboard base URL (default `http://localhost:8080`, or `NEXUS_SCOREBOARD`) |
| `--scale N` | window size 960N × 540N (default 2) |
| `--screenshot-after SECS` | save a screenshot after SECS and exit |
| `--screenshot-path PATH` | screenshot destination (default `screenshot.png`) |

On Wayland, launch with `WINIT_UNIX_BACKEND=wayland`. There is no audio, no lane
panel and no pit replay; those are deliberately out of scope.

## Demo

```sh
# terminal 1
cd ../scoreboard
go build -o /tmp/nexus-scoreboard .
/tmp/nexus-scoreboard -heat 60s -pit 10s -listen :8088 -db /tmp/sb.db

# terminal 2
cd ../scoreboard
python3 tools/fake_lanes.py --url http://localhost:8088 --lanes 40

# terminal 3
cargo run -- --url http://localhost:8088
```
