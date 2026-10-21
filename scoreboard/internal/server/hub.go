package server

import (
	"encoding/json"
	"sync"
)

// Msg is one SSE message. ID 0 means "no id" (not replayable; used for state).
type Msg struct {
	ID    uint64
	Event string
	Data  []byte
}

type sub struct {
	ch     chan Msg
	closed bool
}

// Hub fans SSE messages out to subscribers and keeps a ring buffer of
// id-carrying messages for Last-Event-ID replay. Slow subscribers are dropped
// (their channel is closed; the browser reconnects with Last-Event-ID).
type Hub struct {
	mu   sync.Mutex
	seq  uint64
	ring []Msg // ordered by id, len <= size
	size int
	subs map[*sub]struct{}
}

func NewHub(size int) *Hub { return &Hub{size: size, subs: map[*sub]struct{}{}} }

// Publish assigns an id, stores the message in the ring and fans it out.
func (h *Hub) Publish(event string, v any) Msg {
	data, _ := json.Marshal(v)
	h.mu.Lock()
	defer h.mu.Unlock()
	h.seq++
	m := Msg{ID: h.seq, Event: event, Data: data}
	h.ring = append(h.ring, m)
	if len(h.ring) > h.size {
		h.ring = append(h.ring[:0:0], h.ring[len(h.ring)-h.size:]...)
	}
	h.fanout(m)
	return m
}

// Broadcast fans out a message without an id (not stored for replay).
func (h *Hub) Broadcast(event string, data []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.fanout(Msg{Event: event, Data: data})
}

func (h *Hub) fanout(m Msg) {
	for s := range h.subs {
		select {
		case s.ch <- m:
		default:
			h.drop(s)
		}
	}
}

func (h *Hub) drop(s *sub) {
	if !s.closed {
		s.closed = true
		close(s.ch)
	}
	delete(h.subs, s)
}

// Subscribe registers a subscriber and returns the backlog after lastID
// (whole ring if lastID is older than the ring; nothing if lastID == 0 and
// replay is false).
func (h *Hub) Subscribe(lastID uint64, replay bool) (*sub, []Msg) {
	h.mu.Lock()
	defer h.mu.Unlock()
	s := &sub{ch: make(chan Msg, 256)}
	h.subs[s] = struct{}{}
	var back []Msg
	if replay {
		for _, m := range h.ring {
			if m.ID > lastID {
				back = append(back, m)
			}
		}
	}
	return s, back
}

func (h *Hub) Unsubscribe(s *sub) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.drop(s)
}

// Subscribers returns the current subscriber count.
func (h *Hub) Subscribers() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.subs)
}

// Frame is a lane screen frame as posted by the lane.
type Frame struct {
	LaneID string    `json:"lane_id"`
	TS     string    `json:"ts"`
	Cols   int       `json:"cols"`
	Rows   int       `json:"rows"`
	Cursor [2]int    `json:"cursor"`
	Lines  []string  `json:"lines"`
}

// Screens keeps the latest frame per lane and fans frames out per lane.
type Screens struct {
	mu     sync.Mutex
	latest map[string][]byte
	subs   map[string]map[chan []byte]struct{}
}

func NewScreens() *Screens {
	return &Screens{latest: map[string][]byte{}, subs: map[string]map[chan []byte]struct{}{}}
}

func (s *Screens) Put(lane string, data []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.latest[lane] = data
	for ch := range s.subs[lane] {
		select {
		case ch <- data:
		default: // slow viewer: skip this frame, the next one supersedes it
		}
	}
}

func (s *Screens) Latest(lane string) []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.latest[lane]
}

func (s *Screens) Subscribe(lane string) (chan []byte, []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ch := make(chan []byte, 4)
	if s.subs[lane] == nil {
		s.subs[lane] = map[chan []byte]struct{}{}
	}
	s.subs[lane][ch] = struct{}{}
	return ch, s.latest[lane]
}

func (s *Screens) Unsubscribe(lane string, ch chan []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.subs[lane], ch)
	if len(s.subs[lane]) == 0 {
		delete(s.subs, lane)
	}
}
