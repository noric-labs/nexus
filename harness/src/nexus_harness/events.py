"""Event schema v1 construction and delivery to the scoreboard (NDJSON, retries)."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("nexus.events")

EVENT_TYPES = {"crash", "sanitizer", "hang", "visual", "agent_error", "heartbeat",
               "lane_start", "lane_end"}
FAILURE_TYPES = {"crash", "sanitizer", "hang", "visual", "agent_error"}


def utc_ts(t: float | None = None) -> str:
    """RFC 3339 UTC with milliseconds, e.g. 2026-10-21T12:00:00.123Z."""
    dt = datetime.fromtimestamp(time.time() if t is None else t, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class Identity:
    lane_id: str
    game: str
    fault: str
    seed: int
    model: str = ""
    game_ver: str = ""
    heat_id: str = ""


def make_event(ident: Identity, type_: str, *, ts: float | None = None, **fields) -> dict:
    """Build one schema-v1 event. Optional fields with value None are omitted."""
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    ev = {
        "v": 1,
        "event_id": str(uuid.uuid4()),
        "ts": utc_ts(ts),
        "heat_id": ident.heat_id,
        "lane_id": ident.lane_id,
        "game": ident.game,
        "game_ver": ident.game_ver,
        "fault": ident.fault,
        "seed": ident.seed,
        "model": ident.model,
        "type": type_,
    }
    for k, v in fields.items():
        if v is not None:
            ev[k] = v
    if not ev["game_ver"]:
        del ev["game_ver"]
    return ev


def http_json(method: str, url: str, body: bytes | None = None, *, timeout: float = 5.0,
              content_type: str = "application/json"):
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return r.status, (json.loads(data) if data.strip() else None)


def fetch_heat(base_url: str, timeout: float = 3.0) -> tuple[str, dict] | None:
    """Heat id to stamp on events: `id` during the race, `next_id` during the pit."""
    try:
        _, st = http_json("GET", base_url.rstrip("/") + "/api/state", timeout=timeout)
    except (OSError, ValueError) as e:
        log.debug("GET /api/state failed: %s", e)
        return None
    heat = (st or {}).get("heat") or {}
    hid = heat.get("next_id") if heat.get("phase") == "pit" else heat.get("id")
    return (hid or ""), heat


class EventSender:
    """Background NDJSON sender. Every event is also appended to a local spool file.
    Failed batches are retried with exponential backoff (0.5 s doubling to 30 s) until
    they are accepted or the sender is closed; a 4xx other than 408/429 drops the batch
    (it can never succeed). The scoreboard is idempotent on event_id, so retries are safe."""

    def __init__(self, base_url: str | None, spool: Path | None = None, *,
                 max_batch: int = 100, backoff_max: float = 30.0, timeout: float = 5.0):
        self.url = base_url.rstrip("/") + "/events" if base_url else None
        self.spool = spool
        self.max_batch = max_batch
        self.backoff_max = backoff_max
        self.timeout = timeout
        self.q: queue.Queue[dict] = queue.Queue()
        self.sent = 0
        self.failures = 0
        self._closing = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._run, name="event-sender", daemon=True)
        self._t.start()

    def send(self, ev: dict) -> None:
        if self.spool:
            with self._lock, open(self.spool, "a") as f:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        if self.url:
            self.q.put(ev)
            self._idle.clear()

    def _post(self, batch: list[dict]) -> bool:
        body = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in batch).encode()
        try:
            status, _ = http_json("POST", self.url, body, timeout=self.timeout,
                                  content_type="application/x-ndjson")
            return 200 <= status < 300
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code not in (408, 429):
                log.error("scoreboard rejected batch (%s); dropping %d events", e.code, len(batch))
                return True
            log.warning("POST /events: HTTP %s", e.code)
        except (OSError, ValueError) as e:
            log.warning("POST /events failed: %s", e)
        return False

    def _run(self) -> None:
        pending: list[dict] = []
        delay = 0.5
        while True:
            if not pending:
                try:
                    pending.append(self.q.get(timeout=0.2))
                except queue.Empty:
                    if self.q.empty():
                        self._idle.set()
                    if self._closing.is_set():
                        return
                    continue
            while len(pending) < self.max_batch:
                try:
                    pending.append(self.q.get_nowait())
                except queue.Empty:
                    break
            if self._post(pending):
                self.sent += len(pending)
                pending = []
                delay = 0.5
                continue
            self.failures += 1
            if self._give_up:
                return
            self._closing.wait(delay)
            delay = min(delay * 2, self.backoff_max)

    _give_up = False

    def flush(self, timeout: float = 10.0) -> bool:
        return self._idle.wait(timeout)

    def close(self, timeout: float = 10.0) -> bool:
        ok = self.flush(timeout)
        self._give_up = True
        self._closing.set()
        self._t.join(2.0)
        return ok
