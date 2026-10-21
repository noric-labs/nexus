//! Live scoreboard state plus failure-flash tracking.
#![expect(
    clippy::arithmetic_side_effects,
    reason = "durations and counters are bounded"
)]

use std::collections::HashMap;
use std::time::{Duration, Instant};

use bevy::prelude::*;
use crossbeam_channel::Receiver;

use crate::poller::PollEvent;
use crate::state::{State, now_ms, parse_time_ms};

const FLASH_SECS: u64 = 3;

#[derive(Debug, Clone, PartialEq)]
struct FailureKey {
    ts: String,
    kind: String,
    points: f64,
    hits: i32,
}

#[derive(Debug, Clone)]
pub struct Flash {
    pub kind: String,
    pub until: Instant,
}

#[derive(Debug, Clone)]
pub struct LatestFailure {
    pub lane: String,
    pub kind: String,
    pub points: f64,
    pub detail: String,
}

#[derive(Resource)]
pub struct Feed {
    rx: Receiver<PollEvent>,
    pub state: State,
    pub connected: bool,
    pub last_error: String,
    pub offset_ms: i64,
    pub heat_id: String,
    pub flashes: HashMap<String, Flash>,
    pub latest: Option<LatestFailure>,
    pub states: u64,
    seen: HashMap<String, FailureKey>,
}

impl Feed {
    pub fn new(rx: Receiver<PollEvent>) -> Self {
        Self {
            rx,
            state: State::default(),
            connected: false,
            last_error: String::new(),
            offset_ms: 0,
            heat_id: String::new(),
            flashes: HashMap::new(),
            latest: None,
            states: 0,
            seen: HashMap::new(),
        }
    }

    pub fn flashing(&self, lane_id: &str) -> Option<&Flash> {
        self.flashes
            .get(lane_id)
            .filter(|flash| flash.until > Instant::now())
    }
}

pub fn ingest(mut feed: ResMut<Feed>) {
    let now = Instant::now();
    let events: Vec<PollEvent> = feed.rx.try_iter().collect();
    for event in events {
        match event {
            PollEvent::State(state) => {
                let state = *state;
                if state.heat.id != feed.heat_id {
                    feed.heat_id.clone_from(&state.heat.id);
                    feed.seen.clear();
                    feed.flashes.clear();
                    feed.latest = None;
                }
                if let Some(server) = parse_time_ms(&state.server_time) {
                    feed.offset_ms = server - now_ms();
                }
                for lane in &state.lanes {
                    let Some(failure) = &lane.last_failure else {
                        continue;
                    };
                    let key = FailureKey {
                        ts: failure.ts.clone(),
                        kind: failure.kind.clone(),
                        points: failure.points,
                        hits: lane.hits,
                    };
                    if feed.seen.get(&lane.lane_id) == Some(&key) {
                        continue;
                    }
                    feed.seen.insert(lane.lane_id.clone(), key);
                    feed.flashes.insert(
                        lane.lane_id.clone(),
                        Flash {
                            kind: failure.kind.clone(),
                            until: now + Duration::from_secs(FLASH_SECS),
                        },
                    );
                    feed.latest = Some(LatestFailure {
                        lane: lane.lane_id.clone(),
                        kind: failure.kind.clone(),
                        points: failure.points,
                        detail: failure.detail.clone(),
                    });
                }
                feed.flashes.retain(|_, flash| flash.until > now);
                feed.state = state;
                feed.states += 1;
                feed.connected = true;
                feed.last_error.clear();
            }
            PollEvent::Disconnected(err) => {
                feed.connected = false;
                feed.last_error = err;
            }
        }
    }
}
