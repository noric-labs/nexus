// Package web holds the embedded browser UI served at "/". The race UI lives
// entirely in this directory (index.html plus plain ES modules in js/, no
// build step); it talks to the scoreboard only through the public API
// (/stream, /api/state, /api/lanes/{id}, /lanes/{id}/screen/stream,
// /wall/focus). agents.html is the separate lane/agents.json editor served at
// /agents (internal/agents, enabled by -agents-config).
package web

import "embed"

//go:embed index.html agents.html js/*.js
var FS embed.FS
