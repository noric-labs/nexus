//! Palette and lane colours, mirroring `scoreboard/web/js/theme.js`.
#![expect(
    clippy::arithmetic_side_effects,
    clippy::as_conversions,
    clippy::indexing_slicing,
    reason = "hash values are reduced modulo a small palette"
)]

use bevy::prelude::Color;

pub const BG: Color = Color::srgb_u8(0x0b, 0x0b, 0x14);
pub const TEXT: Color = Color::srgb_u8(0xe8, 0xe8, 0xf0);
pub const MUTED: Color = Color::srgb_u8(0x9a, 0x9a, 0xb8);
pub const GOLD: Color = Color::srgb_u8(0xff, 0xd2, 0x3f);
pub const SILVER: Color = Color::srgb_u8(0xc8, 0xd0, 0xe0);
pub const BRONZE: Color = Color::srgb_u8(0xd0, 0x80, 0x3a);
pub const RED: Color = Color::srgb_u8(0xff, 0x00, 0x4d);
pub const GREEN: Color = Color::srgb_u8(0x00, 0xe4, 0x36);
pub const AMBER: Color = Color::srgb_u8(0xff, 0xa3, 0x00);
pub const WHITE: Color = Color::srgb_u8(0xff, 0xf1, 0xe8);
pub const BLACK: Color = Color::srgb_u8(0x00, 0x00, 0x00);
pub const ENDED_BODY: Color = Color::srgb_u8(0x4a, 0x4a, 0x5a);
pub const ENDED_ACCENT: Color = Color::srgb_u8(0x5f, 0x5f, 0x6f);
pub const STALE_BODY: Color = Color::srgb_u8(0x6a, 0x6a, 0x7a);

const LIVERY: [(u8, u8, u8); 14] = [
    (0xff, 0x00, 0x4d),
    (0xff, 0xa3, 0x00),
    (0xff, 0xec, 0x27),
    (0x00, 0xe4, 0x36),
    (0x29, 0xad, 0xff),
    (0xff, 0x77, 0xa8),
    (0xc2, 0xc3, 0xc7),
    (0xab, 0x52, 0x36),
    (0x83, 0x76, 0x9c),
    (0xff, 0xcc, 0xaa),
    (0x1d, 0xe9, 0xb6),
    (0x7e, 0x25, 0x53),
    (0x00, 0x87, 0x51),
    (0xff, 0x6c, 0x24),
];

const KNOWN_FAULTS: [&str; 10] = [
    "none",
    "malloc-1pct",
    "malloc-0.1pct",
    "io-save-5pct",
    "strace-enospc-save",
    "strace-eintr-read",
    "winch-storm",
    "rlimit-tight",
    "time-warp",
    "keystorm",
];

const ACCENTS: [(u8, u8, u8); 7] = [
    (0xff, 0xf1, 0xe8),
    (0xff, 0xd2, 0x3f),
    (0x29, 0xad, 0xff),
    (0x00, 0xe4, 0x36),
    (0xff, 0x77, 0xa8),
    (0xff, 0xa3, 0x00),
    (0x83, 0x76, 0x9c),
];

pub fn livery_rgb(i: usize) -> (u8, u8, u8) {
    LIVERY[i % LIVERY.len()]
}

fn color(rgb: (u8, u8, u8)) -> Color {
    Color::srgb_u8(rgb.0, rgb.1, rgb.2)
}

/// FNV-1a, matching the web UI's `hash`.
pub fn hash(s: &str) -> u32 {
    s.bytes().fold(2166136261u32, |h, b| {
        (h ^ u32::from(b)).wrapping_mul(16777619)
    })
}

pub fn fault_color(fault: &str) -> Color {
    let i = KNOWN_FAULTS
        .iter()
        .position(|f| *f == fault)
        .unwrap_or_else(|| hash(fault) as usize % LIVERY.len());
    color(LIVERY[i])
}

pub fn game_color(game: &str) -> Color {
    let rgb = match game {
        "brogue" => (0xff, 0xd2, 0x3f),
        "frotz-zork1" => (0x29, 0xad, 0xff),
        "greed" => (0x00, 0xe4, 0x36),
        "dcss" => (0xff, 0x77, 0xa8),
        _ => ACCENTS[hash(game) as usize % ACCENTS.len()],
    };
    color(rgb)
}

pub fn type_color(kind: &str) -> Color {
    match kind {
        "sanitizer" => Color::srgb_u8(0xff, 0x3d, 0xf5),
        "crash" => Color::srgb_u8(0xff, 0x44, 0x22),
        "hang" => Color::srgb_u8(0xff, 0xb0, 0x00),
        "error_exit" => Color::srgb_u8(0x29, 0xad, 0xff),
        "visual" => Color::srgb_u8(0x00, 0xff, 0x9c),
        _ => MUTED,
    }
}

pub fn type_short(kind: &str) -> &'static str {
    match kind {
        "sanitizer" => "SANI",
        "crash" => "CRSH",
        "hang" => "HANG",
        "error_exit" => "EXIT",
        "visual" => "VIS",
        "agent_error" => "AGNT",
        _ => "?",
    }
}

pub fn game_short(game: &str) -> String {
    match game {
        "brogue" => "BRG".to_string(),
        "frotz-zork1" => "ZRK".to_string(),
        "greed" => "GRD".to_string(),
        "dcss" => "DCS".to_string(),
        "" => "?".to_string(),
        _ => game.chars().take(3).collect::<String>().to_uppercase(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_faults_are_stable() {
        assert_eq!(fault_color("none"), fault_color("none"));
        assert_ne!(fault_color("none"), fault_color("keystorm"));
    }
}
