//! Scoreboard `/api/state` types and the small pure helpers the renderer needs.
#![expect(
    clippy::arithmetic_side_effects,
    clippy::as_conversions,
    reason = "timestamps, scores and clock formatting use bounded numeric math"
)]

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Deserializer};

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default)]
pub struct State {
    pub full: bool,
    pub server_time: String,
    pub heat: Heat,
    #[serde(deserialize_with = "null_default")]
    pub config: Config,
    #[serde(deserialize_with = "null_default")]
    pub lanes: Vec<Lane>,
    #[serde(deserialize_with = "null_default")]
    pub scores: HashMap<String, f64>,
    #[serde(deserialize_with = "null_default")]
    pub standings: Standings,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Config {
    pub lap_points: f64,
    pub heat_secs: f64,
    pub pit_secs: f64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            lap_points: 20.0,
            heat_secs: 900.0,
            pit_secs: 60.0,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default)]
pub struct Heat {
    pub id: String,
    pub seq: i32,
    pub phase: String,
    pub started_at: String,
    pub ends_at: String,
    pub pit_ends_at: String,
    pub next_id: String,
    pub remaining_ms: i64,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default)]
pub struct Lane {
    pub lane_id: String,
    pub game: String,
    pub game_ver: String,
    pub fault: String,
    pub seed: u64,
    pub model: String,
    pub heat_id: String,
    pub status: String,
    pub last_seen: String,
    pub turn: i64,
    pub tokens_in: i64,
    pub tokens_out: i64,
    pub pos: i32,
    pub score: f64,
    pub laps: i32,
    pub lap_frac: f64,
    pub hits: i32,
    pub clusters: i32,
    pub season_points: i32,
    pub last_failure: Option<Failure>,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default)]
pub struct Failure {
    #[serde(rename = "type")]
    pub kind: String,
    pub detail: String,
    pub ts: String,
    pub points: f64,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default)]
pub struct Standings {
    #[serde(deserialize_with = "null_default")]
    pub lanes: Vec<Entry>,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default)]
pub struct Entry {
    pub pos: i32,
    pub lane_id: String,
    pub score: f64,
}

fn null_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}

impl State {
    pub fn lap_points(&self) -> f64 {
        if self.config.lap_points > 0.0 {
            self.config.lap_points
        } else {
            20.0
        }
    }

    pub fn phase(&self) -> &str {
        match self.heat.phase.as_str() {
            "race" | "pit" => self.heat.phase.as_str(),
            _ => "race",
        }
    }

    pub fn lane_score(&self, lane: &Lane) -> f64 {
        self.scores
            .get(&lane.lane_id)
            .copied()
            .unwrap_or(lane.score)
    }

    pub fn leader(&self) -> Option<&Entry> {
        self.standings.lanes.iter().find(|e| e.score > 0.0)
    }

    pub fn gap(&self, lane_id: &str) -> Option<f64> {
        let lead = self.leader()?;
        let score = self.scores.get(lane_id).copied()?;
        (score > 0.0).then_some(lead.score - score)
    }

    /// Ranked participants first, then the rest by lane id.
    pub fn ranked_lanes(&self) -> Vec<&Lane> {
        let mut lanes: Vec<&Lane> = self.lanes.iter().collect();
        lanes.sort_by(|a, b| {
            let pa = if a.pos > 0 { a.pos } else { i32::MAX };
            let pb = if b.pos > 0 { b.pos } else { i32::MAX };
            pa.cmp(&pb).then_with(|| a.lane_id.cmp(&b.lane_id))
        });
        lanes
    }

    pub fn counts(&self) -> (usize, usize, usize) {
        self.lanes.iter().fold((0, 0, 0), |mut acc, l| {
            match l.status.as_str() {
                "stale" => acc.1 += 1,
                "ended" => acc.2 += 1,
                _ => acc.0 += 1,
            }
            acc
        })
    }
}

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

pub fn parse_time_ms(s: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|dt| dt.timestamp_millis())
}

/// Milliseconds left in the current phase, by the server clock.
pub fn remaining_ms(heat: &Heat, offset_ms: i64, now_ms: i64) -> i64 {
    let end = if heat.phase == "pit" {
        &heat.pit_ends_at
    } else {
        &heat.ends_at
    };
    parse_time_ms(end)
        .map(|e| (e - (now_ms + offset_ms)).max(0))
        .unwrap_or(heat.remaining_ms.max(0))
}

pub fn fmt_clock(ms: i64) -> String {
    let s = (ms.max(0) + 999) / 1000;
    format!("{}:{:02}", s / 60, s % 60)
}

pub fn fmt_pts(x: f64) -> String {
    let r = (x * 100.0).round() / 100.0;
    if r.fract() == 0.0 {
        format!("{r:.0}")
    } else if (r * 10.0).fract() == 0.0 {
        format!("{r:.1}")
    } else {
        format!("{r:.2}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = r#"{
      "full": true,
      "server_time": "2026-10-21T12:00:00Z",
      "heat": {"id":"h-0001","seq":1,"phase":"race","started_at":"2026-10-21T11:59:00Z",
               "ends_at":"2026-10-21T12:14:00Z","pit_ends_at":"2026-10-21T12:15:00Z",
               "next_id":"h-0002","remaining_ms":840000},
      "config": {"lap_points":20,"weights":{"crash":3}},
      "lanes": [
        {"lane_id":"l-001","game":"brogue","fault":"none","status":"running","pos":1,"score":8,
         "last_failure":{"type":"crash","detail":"SIGSEGV","ts":"2026-10-21T12:00:00Z","points":3}},
        {"lane_id":"l-002","game":"dcss","fault":"malloc-1pct","status":"stale","pos":0,"score":0}
      ],
      "scores": {"l-001":8,"l-002":0},
      "standings": {"lanes":[{"pos":1,"lane_id":"l-001","score":8}]}
    }"#;

    #[test]
    fn parses_a_full_snapshot() {
        let s: State = serde_json::from_str(FIXTURE).expect("fixture");
        assert_eq!(s.lanes.len(), 2);
        assert_eq!(s.lap_points(), 20.0);
        assert_eq!(s.leader().map(|e| e.lane_id.as_str()), Some("l-001"));
        assert_eq!(s.gap("l-001"), Some(0.0));
        assert_eq!(s.gap("l-002"), None);
        assert_eq!(
            s.lanes[0].last_failure.as_ref().map(|f| f.kind.as_str()),
            Some("crash")
        );
        assert_eq!(s.counts(), (1, 1, 0));
    }

    #[test]
    fn defaults_survive_nulls_and_missing_fields() {
        let s: State = serde_json::from_str(
            r#"{"heat":{},"lanes":null,"scores":null,"standings":null,"config":null}"#,
        )
        .expect("nulls");
        assert!(s.lanes.is_empty());
        assert_eq!(s.lap_points(), 20.0);
        assert_eq!(s.phase(), "race");
    }

    #[test]
    fn clock_counts_down_on_the_server_clock() {
        let h = Heat {
            phase: "race".into(),
            ends_at: "2026-10-21T12:00:10Z".into(),
            ..Default::default()
        };
        let now = parse_time_ms("2026-10-21T12:00:00Z").expect("timestamp");
        assert_eq!(remaining_ms(&h, 0, now), 10_000);
        assert_eq!(fmt_clock(65_000), "1:05");
        assert_eq!(fmt_clock(0), "0:00");
    }

    #[test]
    fn ranks_participants_before_others() {
        let s: State = serde_json::from_str(FIXTURE).expect("fixture");
        let ids: Vec<&str> = s
            .ranked_lanes()
            .iter()
            .map(|l| l.lane_id.as_str())
            .collect();
        assert_eq!(ids, vec!["l-001", "l-002"]);
    }
}
