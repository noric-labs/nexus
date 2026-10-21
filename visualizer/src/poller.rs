//! Background poller for the scoreboard's full-state endpoint.

use std::thread;
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender, bounded};

use crate::state::State;

pub enum PollEvent {
    State(Box<State>),
    Disconnected(String),
}

pub const POLL_INTERVAL: Duration = Duration::from_millis(250);

pub fn spawn(base_url: String) -> Receiver<PollEvent> {
    let (tx, rx) = bounded(4);
    thread::spawn(move || poll_loop(&base_url, &tx));
    rx
}

fn poll_loop(base_url: &str, tx: &Sender<PollEvent>) {
    let url = api_url(base_url);
    loop {
        let event = match fetch(&url) {
            Ok(state) => PollEvent::State(Box::new(state)),
            Err(err) => PollEvent::Disconnected(err),
        };
        if tx.send(event).is_err() {
            return;
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn api_url(base_url: &str) -> String {
    let base = base_url.trim_end_matches('/');
    if base.starts_with("http://") || base.starts_with("https://") {
        format!("{base}/api/state")
    } else {
        format!("http://{base}/api/state")
    }
}

fn fetch(url: &str) -> Result<State, String> {
    let body = ureq::get(url)
        .timeout(Duration::from_secs(2))
        .call()
        .map_err(|e| e.to_string())?
        .into_string()
        .map_err(|e| e.to_string())?;
    serde_json::from_str(&body).map_err(|e| format!("decode: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_the_state_url() {
        assert_eq!(api_url("localhost:8080"), "http://localhost:8080/api/state");
        assert_eq!(api_url("http://box:8088/"), "http://box:8088/api/state");
    }
}
