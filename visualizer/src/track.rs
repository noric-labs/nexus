//! Track geometry, ported from `scoreboard/web/js/track.js`.
#![expect(
    clippy::as_conversions,
    reason = "track geometry mirrors the web UI f32 math"
)]

use bevy::prelude::Resource;

use crate::colors::hash;

pub const CX: f32 = 352.0;
pub const CY: f32 = 270.0;
pub const STRAIGHT: f32 = 150.0;
pub const RADIUS: f32 = 168.0;
pub const HALF_WIDTH: f32 = 22.0;
pub const START_OFFSET: f32 = 40.0;

#[derive(Debug, Clone, Copy)]
pub struct Point {
    pub x: f32,
    pub y: f32,
    pub ang: f32,
}

#[derive(Debug, Clone, Copy)]
pub struct Offset {
    pub d: f32,
    pub curve: bool,
    pub a: f32,
}

#[derive(Debug, Clone, Copy, Resource)]
pub struct Track {
    pub cx: f32,
    pub cy: f32,
    pub s: f32,
    pub r: f32,
    pub half: f32,
    pub start_off: f32,
    pub length: f32,
    pub u0: f32,
}

impl Default for Track {
    fn default() -> Self {
        let length = 4.0 * STRAIGHT + 2.0 * std::f32::consts::PI * RADIUS;
        Self {
            cx: CX,
            cy: CY,
            s: STRAIGHT,
            r: RADIUS,
            half: HALF_WIDTH,
            start_off: START_OFFSET,
            length,
            u0: STRAIGHT + START_OFFSET,
        }
    }
}

impl Track {
    pub fn start_x(&self) -> f32 {
        self.cx + self.start_off
    }

    /// Point at distance `d` along the centre line, offset `lat` outward.
    pub fn at(&self, d: f32, lat: f32) -> Point {
        let mut u = (d + self.u0).rem_euclid(self.length);
        let (x, y, ang, nx, ny);
        if u < 2.0 * self.s {
            x = self.cx - self.s + u;
            y = self.cy + self.r;
            ang = 0.0;
            nx = 0.0;
            ny = 1.0;
        } else {
            u -= 2.0 * self.s;
            if u < std::f32::consts::PI * self.r {
                let t = std::f32::consts::FRAC_PI_2 - u / self.r;
                nx = t.cos();
                ny = t.sin();
                x = self.cx + self.s + self.r * nx;
                y = self.cy + self.r * ny;
                ang = (-nx).atan2(ny);
            } else {
                u -= std::f32::consts::PI * self.r;
                if u < 2.0 * self.s {
                    x = self.cx + self.s - u;
                    y = self.cy - self.r;
                    ang = std::f32::consts::PI;
                    nx = 0.0;
                    ny = -1.0;
                } else {
                    u -= 2.0 * self.s;
                    let t = -std::f32::consts::FRAC_PI_2 - u / self.r;
                    nx = t.cos();
                    ny = t.sin();
                    x = self.cx - self.s + self.r * nx;
                    y = self.cy + self.r * ny;
                    ang = (-nx).atan2(ny);
                }
            }
        }
        Point {
            x: x + nx * lat,
            y: y + ny * lat,
            ang,
        }
    }

    /// Signed distance from the centre line (positive = outside).
    pub fn off(&self, x: f32, y: f32) -> Offset {
        if (x - self.cx).abs() <= self.s {
            Offset {
                d: (y - self.cy).abs() - self.r,
                curve: false,
                a: 0.0,
            }
        } else {
            let ccx = if x > self.cx {
                self.cx + self.s
            } else {
                self.cx - self.s
            };
            Offset {
                d: (x - ccx).hypot(y - self.cy) - self.r,
                curve: true,
                a: (y - self.cy).atan2(x - ccx),
            }
        }
    }

    /// Lateral slot for a lane, so cars at equal distance spread across the track.
    pub fn lane_lat(&self, lane_id: &str) -> f32 {
        ((hash(lane_id) % 7) as f32 - 3.0) * 5.5
    }
}

/// Grid slot for a scoreless lane: `(distance, lateral)`.
pub fn grid_slot(i: usize) -> (f32, f32) {
    let row = i / 5;
    let col = i % 5;
    (
        -12.0 - row as f32 * 10.0 - (col % 2) as f32 * 4.0,
        (col as f32 - 2.0) * 8.0,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn start_line_sits_on_the_bottom_straight() {
        let t = Track::default();
        let p = t.at(0.0, 0.0);
        assert!((p.x - t.start_x()).abs() < 0.01);
        assert!((p.y - (t.cy + t.r)).abs() < 0.01);
        assert!(p.ang.abs() < 0.01);
    }

    #[test]
    fn a_lap_returns_to_the_line() {
        let t = Track::default();
        let a = t.at(0.0, 0.0);
        let b = t.at(t.length, 0.0);
        assert!((a.x - b.x).abs() < 0.01);
        assert!((a.y - b.y).abs() < 0.01);
    }

    #[test]
    fn lane_offsets_stay_on_the_asphalt() {
        let t = Track::default();
        for id in ["l-001", "l-040", "lane", ""] {
            let lat = t.lane_lat(id);
            assert!((-16.5..=16.5).contains(&lat));
        }
    }

    #[test]
    fn offset_is_zero_on_the_centre_line() {
        let t = Track::default();
        let p = t.at(t.length * 0.37, 0.0);
        assert!(t.off(p.x, p.y).d.abs() < 0.01);
    }
}
