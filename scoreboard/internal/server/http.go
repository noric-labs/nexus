package server

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	maxEventsBody = 16 << 20
	maxLine       = 1 << 20
	maxFrameBody  = 512 << 10
)

// Handler returns the HTTP API. ui is the filesystem served at "/".
func (s *Server) Handler(ui fs.FS) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /events", s.handleEvents)
	mux.HandleFunc("POST /lanes/{id}/screen", s.handleScreenPost)
	mux.HandleFunc("GET /lanes/{id}/screen/stream", s.handleScreenStream)
	mux.HandleFunc("GET /api/lanes/{id}/screen", s.handleScreenLatest)
	mux.HandleFunc("GET /stream", s.handleStream)
	mux.HandleFunc("GET /api/state", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, s.Snapshot()) })
	mux.HandleFunc("GET /api/lanes/{id}", s.handleLane)
	mux.HandleFunc("POST /wall/focus", s.handleFocus)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "ok\n") })
	if ui != nil {
		mux.Handle("GET /", http.FileServerFS(ui))
	}
	return cors(mux)
}

func cors(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		if r.Method == http.MethodOptions {
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID")
			w.WriteHeader(http.StatusNoContent)
			return
		}
		h.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func httpErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

// POST /events: NDJSON. Invalid lines are skipped and counted; 400 only if
// no line was valid. Always 202 otherwise.
func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	sc := bufio.NewScanner(http.MaxBytesReader(w, r.Body, maxEventsBody))
	sc.Buffer(make([]byte, 64<<10), maxLine)
	var evs []Event
	var raws [][]byte
	var res IngestResult
	line := 0
	for sc.Scan() {
		line++
		b := bytes.TrimSpace(sc.Bytes())
		if len(b) == 0 {
			continue
		}
		var e Event
		if err := decodeEvent(b, &e); err != nil {
			res.Rejected++
			if len(res.Errors) < 20 {
				res.Errors = append(res.Errors, fmt.Sprintf("line %d: %v", line, err))
			}
			continue
		}
		evs = append(evs, e)
		raws = append(raws, append([]byte(nil), b...))
	}
	if err := sc.Err(); err != nil {
		httpErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(evs) == 0 && res.Rejected > 0 {
		writeJSON(w, http.StatusBadRequest, res)
		return
	}
	ir, err := s.Ingest(evs, raws)
	ir.Rejected, ir.Errors = res.Rejected, res.Errors
	if err != nil {
		s.Log.Printf("ingest: %v", err)
		writeJSON(w, http.StatusInternalServerError, ir)
		return
	}
	writeJSON(w, http.StatusAccepted, ir)
}

func (s *Server) handleScreenPost(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var f Frame
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxFrameBody)).Decode(&f); err != nil {
		httpErr(w, http.StatusBadRequest, err.Error())
		return
	}
	f.LaneID = id
	if f.TS == "" {
		f.TS = ts(s.now())
	}
	b, _ := json.Marshal(f)
	s.Screens.Put(id, b)
	w.WriteHeader(http.StatusAccepted)
}

func (s *Server) handleScreenLatest(w http.ResponseWriter, r *http.Request) {
	b := s.Screens.Latest(r.PathValue("id"))
	if b == nil {
		httpErr(w, http.StatusNotFound, "no frame")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(b)
}

func sseStart(w http.ResponseWriter) *http.ResponseController {
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no")
	rc := http.NewResponseController(w)
	rc.SetWriteDeadline(time.Time{})
	w.WriteHeader(http.StatusOK)
	io.WriteString(w, "retry: 2000\n\n")
	rc.Flush()
	return rc
}

func writeSSE(w io.Writer, m Msg) error {
	var b strings.Builder
	if m.ID != 0 {
		fmt.Fprintf(&b, "id: %d\n", m.ID)
	}
	fmt.Fprintf(&b, "event: %s\n", m.Event)
	for _, l := range strings.Split(string(m.Data), "\n") {
		fmt.Fprintf(&b, "data: %s\n", l)
	}
	b.WriteString("\n")
	_, err := io.WriteString(w, b.String())
	return err
}

// GET /stream: full `state` first, then (with Last-Event-ID or ?last_event_id=)
// the replay backlog, then live messages.
func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	lastStr := r.Header.Get("Last-Event-ID")
	if lastStr == "" {
		lastStr = r.URL.Query().Get("last_event_id")
	}
	last, perr := strconv.ParseUint(lastStr, 10, 64)
	sub, back := s.Hub.Subscribe(last, lastStr != "" && perr == nil)
	defer s.Hub.Unsubscribe(sub)
	rc := sseStart(w)
	full, _ := json.Marshal(s.Snapshot())
	if writeSSE(w, Msg{Event: "state", Data: full}) != nil {
		return
	}
	for _, m := range back {
		if writeSSE(w, m) != nil {
			return
		}
	}
	rc.Flush()
	ka := time.NewTicker(15 * time.Second)
	defer ka.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-ka.C:
			if _, err := io.WriteString(w, ": ka\n\n"); err != nil {
				return
			}
			rc.Flush()
		case m, ok := <-sub.ch:
			if !ok {
				return // dropped as too slow; client reconnects with Last-Event-ID
			}
			if writeSSE(w, m) != nil {
				return
			}
			rc.Flush()
		}
	}
}

// GET /lanes/{id}/screen/stream: event `screen`, latest frame first.
func (s *Server) handleScreenStream(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	ch, latest := s.Screens.Subscribe(id)
	defer s.Screens.Unsubscribe(id, ch)
	rc := sseStart(w)
	if latest != nil {
		writeSSE(w, Msg{Event: "screen", Data: latest})
		rc.Flush()
	}
	ka := time.NewTicker(15 * time.Second)
	defer ka.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-ka.C:
			if _, err := io.WriteString(w, ": ka\n\n"); err != nil {
				return
			}
			rc.Flush()
		case b := <-ch:
			if writeSSE(w, Msg{Event: "screen", Data: b}) != nil {
				return
			}
			rc.Flush()
		}
	}
}

func (s *Server) handleLane(w http.ResponseWriter, r *http.Request) {
	card, err := s.LaneDetail(r.PathValue("id"))
	if err != nil {
		httpErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	if card == nil {
		httpErr(w, http.StatusNotFound, "unknown lane")
		return
	}
	writeJSON(w, http.StatusOK, card)
}

func (s *Server) handleFocus(w http.ResponseWriter, r *http.Request) {
	var body struct {
		LaneID string `json:"lane_id"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&body); err != nil || body.LaneID == "" {
		httpErr(w, http.StatusBadRequest, "need {\"lane_id\"}")
		return
	}
	s.Focus(body.LaneID)
	w.WriteHeader(http.StatusAccepted)
}
