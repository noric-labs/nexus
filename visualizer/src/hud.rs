//! Header, timing tower and footer.
#![expect(
    clippy::as_conversions,
    clippy::indexing_slicing,
    reason = "row layout is bounded by MAX_ROWS"
)]

use bevy::prelude::*;
use bevy::sprite::Anchor;

use crate::colors::{
    AMBER, BRONZE, GOLD, GREEN, MUTED, RED, SILVER, TEXT, WHITE, type_color, type_short,
};
use crate::feed::Feed;
use crate::screen_to_world;
use crate::state::{fmt_clock, fmt_pts, now_ms, remaining_ms};

const MAX_ROWS: usize = 40;
const ROW_TOP: f32 = 36.0;
const ROW_H: f32 = 12.0;

#[derive(Component)]
pub enum HudText {
    Title,
    Clock,
    Phase,
    Heat,
    Leader,
    Counts,
    Conn,
    Latest,
    TowerRow(usize),
}

#[derive(Component)]
pub enum HudSprite {
    PhaseBadge,
    RowBg(usize),
}

#[derive(Resource)]
pub struct HudTimer(pub Timer);

impl Default for HudTimer {
    fn default() -> Self {
        Self(Timer::from_seconds(0.1, TimerMode::Repeating))
    }
}

fn label(
    text: &str,
    size: f32,
    color: Color,
    x: f32,
    y: f32,
    anchor: Anchor,
    z: f32,
) -> impl Bundle {
    let world = screen_to_world(x, y);
    (
        Text2d::new(text),
        TextFont {
            font_size: FontSize::Px(size),
            ..default()
        },
        TextColor(color),
        Transform::from_translation(world.extend(z)),
        anchor,
    )
}

pub fn setup_hud(mut commands: Commands) {
    commands.spawn((
        label(
            "TORMENT NEXUS",
            16.0,
            GOLD,
            4.0,
            1.0,
            Anchor::TOP_LEFT,
            11.0,
        ),
        HudText::Title,
    ));
    commands.spawn((
        label("RACE", 8.0, Color::BLACK, 182.0, 10.0, Anchor::CENTER, 11.0),
        HudText::Phase,
    ));
    commands.spawn((
        label("0:00", 16.0, WHITE, 250.0, 9.0, Anchor::CENTER, 11.0),
        HudText::Clock,
    ));
    commands.spawn((
        label("h-0001", 8.0, MUTED, 286.0, 4.0, Anchor::TOP_LEFT, 11.0),
        HudText::Heat,
    ));
    commands.spawn((
        label("", 9.0, GOLD, 400.0, 2.0, Anchor::TOP_LEFT, 11.0),
        HudText::Leader,
    ));
    commands.spawn((
        label("", 9.0, MUTED, 560.0, 4.0, Anchor::TOP_LEFT, 11.0),
        HudText::Counts,
    ));
    commands.spawn((
        label(
            "CONNECTING",
            9.0,
            AMBER,
            956.0,
            4.0,
            Anchor::TOP_RIGHT,
            11.0,
        ),
        HudText::Conn,
    ));

    commands.spawn((
        Sprite::from_color(AMBER, Vec2::new(44.0, 14.0)),
        Transform::from_translation(screen_to_world(182.0, 10.0).extend(10.8)),
        HudSprite::PhaseBadge,
    ));

    commands.spawn(label(
        "POS LANE   GAME FAULT       GAP",
        8.0,
        MUTED,
        710.0,
        24.0,
        Anchor::TOP_LEFT,
        10.5,
    ));

    for i in 0..MAX_ROWS {
        let y = ROW_TOP + i as f32 * ROW_H;
        commands.spawn((
            Sprite::from_color(WHITE, Vec2::new(250.0, 11.0)),
            Transform::from_translation(screen_to_world(830.0, y + 5.0).extend(9.5)),
            Visibility::Hidden,
            HudSprite::RowBg(i),
        ));
        commands.spawn((
            label("", 9.0, MUTED, 710.0, y, Anchor::TOP_LEFT, 10.5),
            HudText::TowerRow(i),
        ));
    }

    commands.spawn((
        label(
            "AI AGENTS PLAY GAMES UNDER INJECTED FAULTS",
            9.0,
            MUTED,
            40.0,
            529.0,
            Anchor::TOP_LEFT,
            11.0,
        ),
        HudText::Latest,
    ));
}

pub fn update_hud(
    time: Res<Time>,
    mut timer: ResMut<HudTimer>,
    feed: Res<Feed>,
    mut texts: Query<(&HudText, &mut Text2d, &mut TextColor)>,
    mut sprites: Query<(&HudSprite, &mut Sprite, &mut Visibility)>,
) {
    if !timer.0.tick(time.delta()).just_finished() {
        return;
    }

    let state = &feed.state;
    let phase = state.phase();
    let remaining = remaining_ms(&state.heat, feed.offset_ms, now_ms());
    let clock = fmt_clock(remaining);
    let (running, stale, ended) = state.counts();
    let leader = state.leader().map(|e| {
        let lane = state
            .lanes
            .iter()
            .find(|l| l.lane_id == e.lane_id)
            .map(|l| l.lane_id.as_str())
            .unwrap_or(e.lane_id.as_str());
        format!("P{} {} {} PTS", e.pos, lane, fmt_pts(e.score))
    });
    let ranked = state.ranked_lanes();
    let mut row_colors: [Option<Color>; MAX_ROWS] = [None; MAX_ROWS];

    for (kind, mut content, mut color) in &mut texts {
        match kind {
            HudText::Title => {}
            HudText::Clock => content.0 = clock.clone(),
            HudText::Phase => content.0 = phase.to_uppercase(),
            HudText::Heat => {
                content.0 = format!("{}  {} PTS/LAP", state.heat.id, fmt_pts(state.lap_points()));
            }
            HudText::Leader => {
                content.0 = leader.clone().unwrap_or_default();
                color.0 = GOLD;
            }
            HudText::Counts => {
                content.0 = format!(
                    "{} LANES  {} RUN {} STL {} END",
                    state.lanes.len(),
                    running,
                    stale,
                    ended
                );
            }
            HudText::Conn => {
                if !feed.connected {
                    content.0 = if feed.states == 0 {
                        "CONNECTING".into()
                    } else {
                        "DISCONNECTED".into()
                    };
                    color.0 = if feed.states == 0 { AMBER } else { RED };
                } else {
                    content.0 = "LIVE".into();
                    color.0 = GREEN;
                }
            }
            HudText::Latest => {
                if let Some(latest) = &feed.latest {
                    content.0 = format!(
                        "{}  {} +{}  {}",
                        latest.lane,
                        type_short(&latest.kind),
                        fmt_pts(latest.points),
                        latest.detail
                    );
                    color.0 = type_color(&latest.kind);
                } else if feed.connected {
                    content.0 = "AI AGENTS PLAY GAMES UNDER INJECTED FAULTS".into();
                    color.0 = MUTED;
                } else {
                    content.0 = format!("DISCONNECTED: {}", feed.last_error);
                    color.0 = RED;
                }
            }
            HudText::TowerRow(i) => {
                let (line, row_color, flash) = match ranked.get(*i) {
                    Some(lane) => {
                        let pos = if lane.pos > 0 {
                            lane.pos.to_string()
                        } else {
                            "-".to_string()
                        };
                        let gap = if lane.pos == 1 {
                            "LEADER".to_string()
                        } else {
                            state
                                .gap(&lane.lane_id)
                                .map(|g| format!("+{}", fmt_pts(g)))
                                .unwrap_or_else(|| "-".into())
                        };
                        let line = format!(
                            "{:>2} {:<6} {:<4} {:<11} {:>6}",
                            pos,
                            truncate(&lane.lane_id, 6),
                            crate::colors::game_short(&lane.game),
                            truncate(&lane.fault, 11),
                            gap
                        );
                        let flash = feed.flashing(&lane.lane_id).map(|f| f.kind.clone());
                        let row_color = if flash.is_some() {
                            WHITE
                        } else {
                            match lane.pos {
                                1 => GOLD,
                                2 => SILVER,
                                3 => BRONZE,
                                p if p > 0 && p <= 10 => TEXT,
                                _ => MUTED,
                            }
                        };
                        (line, row_color, flash)
                    }
                    None => (String::new(), MUTED, None),
                };
                content.0 = line;
                color.0 = row_color;
                if let Some(kind) = flash {
                    row_colors[*i] = Some(type_color(&kind));
                }
            }
        }
    }

    for (kind, mut sprite, mut visibility) in &mut sprites {
        match kind {
            HudSprite::PhaseBadge => {
                sprite.color = if phase == "pit" { AMBER } else { GREEN };
            }
            HudSprite::RowBg(i) => match row_colors[*i] {
                Some(color) => {
                    sprite.color = color.with_alpha(0.30);
                    *visibility = Visibility::Visible;
                }
                None => *visibility = Visibility::Hidden,
            },
        }
    }
}

fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
