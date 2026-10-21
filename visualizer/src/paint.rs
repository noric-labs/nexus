//! Bakes the static 960x540 race background once, mirroring `track.js` and
//! the scenery part of `ui.js`.
#![expect(
    clippy::arithmetic_side_effects,
    clippy::as_conversions,
    clippy::cast_sign_loss,
    clippy::indexing_slicing,
    clippy::lossy_float_literal,
    reason = "pixel and track arithmetic is bounded by the 960x540 image"
)]

use std::f32::consts::PI;

use bevy::asset::RenderAssetUsages;
use bevy::image::ImageSampler;
use bevy::prelude::*;
use bevy::render::render_resource::{Extent3d, TextureDimension, TextureFormat};

use crate::colors::livery_rgb;
use crate::track::Track;

pub const WIDTH: usize = 960;
pub const HEIGHT: usize = 540;
/// The track and scenery occupy the left pane; the tower uses the rest.
pub const TRACK_WIDTH: usize = 704;

type Rgb = (u8, u8, u8);

const BG: Rgb = (0x0b, 0x0b, 0x14);
const PANEL: Rgb = (0x14, 0x14, 0x2a);
const LINE: Rgb = (0x3b, 0x3b, 0x6a);
const RED: Rgb = (0xff, 0x00, 0x4d);
const GOLD: Rgb = (0xff, 0xd2, 0x3f);

const GRASS_1: Rgb = (0x1f, 0x6b, 0x2a);
const GRASS_2: Rgb = (0x23, 0x7a, 0x30);
const GRASS_IN_1: Rgb = (0x1a, 0x5a, 0x24);
const GRASS_IN_2: Rgb = (0x1d, 0x63, 0x28);
const ASPHALT_1: Rgb = (0x3a, 0x3a, 0x4a);
const ASPHALT_2: Rgb = (0x34, 0x34, 0x3f);
const ASPHALT_3: Rgb = (0x42, 0x42, 0x4f);
const WHITE_LINE: Rgb = (0xd8, 0xd8, 0xe0);
const KERB_RED: Rgb = (0xe0, 0x20, 0x3a);
const KERB_WHITE: Rgb = (0xf0, 0xf0, 0xf0);
const CHECKER_DARK: Rgb = (0x10, 0x10, 0x10);
const SAND: Rgb = (0xc8, 0xa8, 0x68);

fn put(px: &mut [u8], x: i32, y: i32, c: Rgb) {
    if x < 0 || y < 0 || x >= WIDTH as i32 || y >= HEIGHT as i32 {
        return;
    }
    let i = (y as usize * WIDTH + x as usize) * 4;
    px[i] = c.0;
    px[i + 1] = c.1;
    px[i + 2] = c.2;
    px[i + 3] = 255;
}

fn fill(px: &mut [u8], x: i32, y: i32, w: i32, h: i32, c: Rgb) {
    for py in y..y + h {
        for px_x in x..x + w {
            put(px, px_x, py, c);
        }
    }
}

pub fn track_image(track: &Track) -> Image {
    let mut px = vec![0u8; WIDTH * HEIGHT * 4];
    for y in 0..HEIGHT as i32 {
        for x in 0..WIDTH as i32 {
            put(
                &mut px,
                x,
                y,
                if (x as usize) < TRACK_WIDTH {
                    BG
                } else {
                    PANEL
                },
            );
        }
    }

    paint_grass_and_asphalt(&mut px, track);
    paint_scenery(&mut px);
    paint_chrome(&mut px);

    let mut image = Image::new(
        Extent3d {
            width: WIDTH as u32,
            height: HEIGHT as u32,
            depth_or_array_layers: 1,
        },
        TextureDimension::D2,
        px,
        TextureFormat::Rgba8UnormSrgb,
        RenderAssetUsages::MAIN_WORLD | RenderAssetUsages::RENDER_WORLD,
    );
    image.sampler = ImageSampler::nearest();
    image
}

fn paint_grass_and_asphalt(px: &mut [u8], t: &Track) {
    let mut r: u32 = 7;
    let mut rnd = || {
        r = r.wrapping_mul(1664525).wrapping_add(1013904223);
        r as f32 / 4294967296.0
    };
    let sx = t.start_x() as i32;
    let straight_left = (t.cx - t.s) as i32;

    for y in 0..HEIGHT as i32 {
        for x in 0..TRACK_WIDTH as i32 {
            let o = t.off(x as f32 + 0.5, y as f32 + 0.5);
            let ad = o.d.abs();
            let c = if ad < t.half {
                let n = rnd();
                let mut c = if n < 0.12 {
                    ASPHALT_2
                } else if n > 0.9 {
                    ASPHALT_3
                } else {
                    ASPHALT_1
                };
                if ad >= t.half - 1.5 && !o.curve {
                    c = WHITE_LINE;
                }
                if !o.curve && y > t.cy as i32 && x >= sx - 2 && x < sx + 4 {
                    c = if ((((x - sx + 2) >> 1) + (y >> 1)) % 2) != 0 {
                        CHECKER_DARK
                    } else {
                        KERB_WHITE
                    };
                }
                if !o.curve
                    && y > t.cy as i32
                    && x < sx - 6
                    && x > straight_left + 8
                    && (sx - x - 2) % 10 == 0
                    && ad < t.half - 4.0
                {
                    c = WHITE_LINE;
                }
                c
            } else if ad < t.half + 3.0 && o.curve {
                let seg = ((o.a + PI) * t.r / 5.0).floor() as i32;
                if seg % 2 == 0 { KERB_WHITE } else { KERB_RED }
            } else if ad < t.half + 6.0 && o.curve && o.d > 0.0 {
                if rnd() < 0.5 { SAND } else { GRASS_1 }
            } else {
                let inside = o.d < 0.0;
                let stripe = ((x as f32 + y as f32 * 0.5) / 18.0).floor() as i32 % 2 == 0;
                let mut c = if inside {
                    if stripe { GRASS_IN_1 } else { GRASS_IN_2 }
                } else if stripe {
                    GRASS_1
                } else {
                    GRASS_2
                };
                if rnd() < 0.04 {
                    c = GRASS_IN_1;
                }
                c
            };
            put(px, x, y, c);
        }
    }
}

fn paint_scenery(px: &mut [u8]) {
    // Grandstand along the top straight.
    fill(px, 150, 26, 404, 44, (0x2a, 0x2a, 0x3e));
    fill(px, 146, 24, 412, 4, (0x44, 0x44, 0x5e));
    let crowd = [
        (0xff, 0x00, 0x4d),
        (0xff, 0xa3, 0x00),
        (0xff, 0xec, 0x27),
        (0x29, 0xad, 0xff),
        (0xff, 0x77, 0xa8),
        (0xff, 0xf1, 0xe8),
        (0x00, 0xe4, 0x36),
        (0x83, 0x76, 0x9c),
    ];
    let mut r: u32 = 99;
    let mut rnd = || {
        r = r.wrapping_mul(1103515245).wrapping_add(12345);
        r as f32 / 4294967296.0
    };
    let mut y = 31;
    while y < 66 {
        let mut x = 154;
        while x < 550 {
            if rnd() >= 0.18 {
                let c = crowd[(rnd() * crowd.len() as f32) as usize % crowd.len()];
                fill(px, x, y, 2, 2, c);
            }
            x += 3;
        }
        y += 4;
    }

    // Advertising boards. The web UI draws bitmap text here; blocks read the
    // same at a glance and keep the bake independent of the font.
    let ads = [
        ((0xff, 0x00, 0x4d), 44),
        ((0xff, 0x3d, 0xf5), 38),
        ((0x29, 0xad, 0xff), 48),
        ((0xff, 0xa3, 0x00), 54),
        ((0x00, 0xe4, 0x36), 40),
    ];
    let mut x = 160;
    for (col, w) in ads {
        fill(px, x, 70, w, 9, (0x10, 0x10, 0x18));
        fill(px, x, 70, 4, 9, col);
        x += w + 12;
    }

    // Pit building along the bottom.
    fill(px, 150, 474, 404, 40, (0x3a, 0x3a, 0x52));
    fill(px, 146, 472, 412, 3, (0x55, 0x55, 0x7a));
    for i in 0..12 {
        let x = 160 + i * 32;
        fill(
            px,
            x,
            486,
            26,
            24,
            if i % 2 == 0 {
                (0x1d, 0x1d, 0x2e)
            } else {
                (0x24, 0x24, 0x3a)
            },
        );
        fill(px, x, 480, 26, 3, livery_rgb(i as usize));
    }
    fill(px, 320, 462, 64, 9, (0x10, 0x10, 0x18));
    fill(px, 320, 462, 64, 1, GOLD);
    fill(px, 320, 470, 64, 1, GOLD);
}

fn paint_chrome(px: &mut [u8]) {
    fill(px, 0, 0, WIDTH as i32, 20, (0, 0, 0));
    fill(px, 0, 19, WIDTH as i32, 1, RED);
    fill(px, 0, 526, WIDTH as i32, 1, LINE);
    fill(px, 0, 527, WIDTH as i32, 13, (0, 0, 0));
    fill(px, TRACK_WIDTH as i32, 20, 1, 506, LINE);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bakes_a_full_rgba_frame() {
        let image = track_image(&Track::default());
        assert_eq!(image.width(), WIDTH as u32);
        assert_eq!(image.height(), HEIGHT as u32);
    }
}
