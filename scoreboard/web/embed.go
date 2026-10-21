// Package web holds the embedded browser UI served at "/". The race UI lives
// entirely in this directory; it talks to the scoreboard only through the
// public API (/stream, /api/state, /api/lanes/{id}, /lanes/{id}/screen/stream,
// /wall/focus).
package web

import "embed"

//go:embed index.html
var FS embed.FS
