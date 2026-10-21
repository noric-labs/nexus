mod cars;
mod colors;
mod feed;
mod hud;
mod paint;
mod poller;
mod state;
mod track;

use std::time::{Duration, Instant};

use bevy::camera::{Projection, ScalingMode};
use bevy::prelude::*;
use bevy::render::view::screenshot::{Screenshot, save_to_disk};
use bevy::window::{Window, WindowPlugin, WindowResolution};

use cars::CarVisuals;
use feed::Feed;
use hud::HudTimer;
use track::Track;

pub const SCREEN_W: f32 = 960.0;
pub const SCREEN_H: f32 = 540.0;

pub fn screen_to_world(x: f32, y: f32) -> Vec2 {
    Vec2::new(x - SCREEN_W * 0.5, SCREEN_H * 0.5 - y)
}

struct Args {
    url: String,
    scale: u32,
    screenshot_after: Option<f32>,
    screenshot_path: String,
}

impl Default for Args {
    fn default() -> Self {
        Self {
            url: std::env::var("NEXUS_SCOREBOARD")
                .unwrap_or_else(|_| "http://localhost:8080".to_string()),
            scale: 2,
            screenshot_after: None,
            screenshot_path: "screenshot.png".to_string(),
        }
    }
}

fn parse_args() -> Result<Option<Args>, String> {
    let mut args = Args::default();
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--url" => {
                args.url = it.next().ok_or("--url needs a value")?;
            }
            "--scale" => {
                args.scale = it
                    .next()
                    .ok_or("--scale needs a value")?
                    .parse()
                    .map_err(|e| format!("--scale must be a positive integer: {e}"))?;
            }
            "--screenshot-after" => {
                args.screenshot_after = Some(
                    it.next()
                        .ok_or("--screenshot-after needs seconds")?
                        .parse()
                        .map_err(|e| format!("--screenshot-after must be seconds: {e}"))?,
                );
            }
            "--screenshot-path" => {
                args.screenshot_path = it.next().ok_or("--screenshot-path needs a value")?;
            }
            "-h" | "--help" => {
                print_help();
                return Ok(None);
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    if args.scale == 0 {
        return Err("--scale must be at least 1".to_string());
    }
    Ok(Some(args))
}

fn print_help() {
    println!(
        "visualizer - Torment Nexus race visualizer\n\n\
         USAGE:\n  visualizer [OPTIONS]\n\n\
         OPTIONS:\n\
         \x20 --url URL                 scoreboard base URL (default http://localhost:8080)\n\
         \x20 --scale N                 window size 960N x 540N (default 2)\n\
         \x20 --screenshot-after SECS   save a screenshot after SECS and exit\n\
         \x20 --screenshot-path PATH    screenshot destination (default screenshot.png)\n\
         \x20 -h, --help                show this help"
    );
}

#[derive(Resource)]
struct ShotConfig {
    after: Option<f32>,
    path: String,
    fired: bool,
    exit_at: Option<Instant>,
}

fn main() -> std::process::ExitCode {
    let args = match parse_args() {
        Ok(Some(args)) => args,
        Ok(None) => return std::process::ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("visualizer: {err}");
            return std::process::ExitCode::from(2);
        }
    };

    let rx = poller::spawn(args.url.clone());
    let width = 960u32.saturating_mul(args.scale);
    let height = 540u32.saturating_mul(args.scale);

    App::new()
        .insert_resource(ClearColor(colors::BG))
        .insert_resource(Feed::new(rx))
        .insert_resource(Track::default())
        .insert_resource(CarVisuals::default())
        .insert_resource(HudTimer::default())
        .insert_resource(ShotConfig {
            after: args.screenshot_after,
            path: args.screenshot_path,
            fired: false,
            exit_at: None,
        })
        .add_plugins(
            DefaultPlugins
                .set(WindowPlugin {
                    primary_window: Some(Window {
                        title: "Torment Nexus".to_string(),
                        resolution: WindowResolution::new(width, height),
                        resizable: true,
                        ..default()
                    }),
                    ..default()
                })
                .set(ImagePlugin::default_nearest()),
        )
        .add_systems(Startup, (setup_camera, setup_track, hud::setup_hud))
        .add_systems(
            Update,
            (
                feed::ingest,
                cars::animate_cars,
                hud::update_hud,
                take_screenshot,
                auto_exit,
            )
                .chain(),
        )
        .run();

    std::process::ExitCode::SUCCESS
}

fn setup_camera(mut commands: Commands) {
    commands.spawn((
        Camera2d,
        Projection::from(OrthographicProjection {
            scaling_mode: ScalingMode::Fixed {
                width: SCREEN_W,
                height: SCREEN_H,
            },
            ..OrthographicProjection::default_2d()
        }),
    ));
}

fn setup_track(mut commands: Commands, mut images: ResMut<Assets<Image>>, track: Res<Track>) {
    let image = paint::track_image(&track);
    commands.spawn((
        Sprite::from_image(images.add(image)),
        Transform::from_xyz(0.0, 0.0, 0.0),
    ));
}

fn take_screenshot(time: Res<Time>, mut commands: Commands, mut cfg: ResMut<ShotConfig>) {
    if cfg.fired {
        return;
    }
    let Some(after) = cfg.after else {
        return;
    };
    if time.elapsed_secs() < after {
        return;
    }
    cfg.fired = true;
    cfg.exit_at = Some(
        Instant::now()
            .checked_add(Duration::from_secs(1))
            .unwrap_or_else(Instant::now),
    );
    commands
        .spawn(Screenshot::primary_window())
        .observe(save_to_disk(cfg.path.clone()));
}

fn auto_exit(mut exit: MessageWriter<AppExit>, cfg: Res<ShotConfig>) {
    if cfg.exit_at.is_some_and(|at| Instant::now() >= at) {
        exit.write(AppExit::Success);
    }
}
