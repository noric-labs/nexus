//! Car entities, smoothing and the top-10 number plates.
#![expect(
    clippy::as_conversions,
    reason = "car animation and sprite sizing use bounded f32 math"
)]

use std::collections::HashMap;

use bevy::prelude::*;

use crate::colors::{
    BLACK, BRONZE, ENDED_ACCENT, ENDED_BODY, GOLD, SILVER, STALE_BODY, WHITE, fault_color,
    game_color,
};
use crate::feed::Feed;
use crate::screen_to_world;
use crate::state::Lane;
use crate::track::{Track, grid_slot};

#[derive(Component)]
pub struct Car {
    lane_id: String,
    d: f32,
    lat: f32,
    initialized: bool,
}

#[derive(Component)]
pub(crate) struct Stripe {
    lane_id: String,
}

#[derive(Component)]
pub(crate) struct Plate {
    lane_id: String,
}

#[derive(Component)]
pub(crate) struct PlateText {
    lane_id: String,
}

#[derive(Resource, Default)]
pub struct CarVisuals {
    cars: HashMap<String, Entity>,
    plates: HashMap<String, (Entity, Entity)>,
}

struct CarVis {
    wx: f32,
    wy: f32,
    big: bool,
    accent: Color,
    pos: i32,
}

struct Targets<'a> {
    lanes: HashMap<&'a str, &'a Lane>,
    scores: HashMap<&'a str, f64>,
    grid: HashMap<&'a str, usize>,
    lap_points: f64,
}

impl<'a> Targets<'a> {
    fn new(feed: &'a Feed) -> Self {
        let lanes = feed
            .state
            .lanes
            .iter()
            .map(|lane| (lane.lane_id.as_str(), lane))
            .collect();
        let scores = feed
            .state
            .lanes
            .iter()
            .map(|lane| (lane.lane_id.as_str(), feed.state.lane_score(lane)))
            .collect();
        let mut grid: Vec<&str> = feed
            .state
            .lanes
            .iter()
            .filter(|lane| feed.state.lane_score(lane) <= 0.0)
            .map(|lane| lane.lane_id.as_str())
            .collect();
        grid.sort_unstable();
        let grid = grid
            .into_iter()
            .enumerate()
            .map(|(i, id)| (id, i))
            .collect();
        Self {
            lanes,
            scores,
            grid,
            lap_points: feed.state.lap_points(),
        }
    }

    fn target(&self, track: &Track, lane_id: &str) -> (f32, f32) {
        let score = self.scores.get(lane_id).copied().unwrap_or(0.0);
        if score > 0.0 {
            (
                (score / self.lap_points) as f32 * track.length,
                track.lane_lat(lane_id),
            )
        } else {
            grid_slot(self.grid.get(lane_id).copied().unwrap_or(0))
        }
    }
}

#[expect(
    clippy::too_many_arguments,
    clippy::type_complexity,
    reason = "Bevy systems take one query parameter per entity group"
)]
pub fn animate_cars(
    mut commands: Commands,
    time: Res<Time>,
    feed: Res<Feed>,
    track: Res<Track>,
    mut visuals: ResMut<CarVisuals>,
    mut cars: Query<(&mut Car, &mut Transform, &mut Sprite)>,
    mut stripes: Query<(&Stripe, &mut Sprite), Without<Car>>,
    mut plates: Query<
        (&Plate, &mut Transform, &mut Sprite, &mut Visibility),
        (Without<Car>, Without<Stripe>, Without<PlateText>),
    >,
    mut plate_texts: Query<
        (
            &PlateText,
            &mut Transform,
            &mut Text2d,
            &mut TextColor,
            &mut Visibility,
        ),
        (Without<Car>, Without<Stripe>, Without<Plate>),
    >,
) {
    let targets = Targets::new(&feed);

    let gone: Vec<String> = visuals
        .cars
        .keys()
        .filter(|id| !targets.lanes.contains_key(id.as_str()))
        .cloned()
        .collect();
    for id in gone {
        if let Some(entity) = visuals.cars.remove(&id) {
            commands.entity(entity).despawn();
        }
        if let Some((bg, text)) = visuals.plates.remove(&id) {
            commands.entity(bg).despawn();
            commands.entity(text).despawn();
        }
    }

    for lane in &feed.state.lanes {
        if visuals.cars.contains_key(&lane.lane_id) {
            continue;
        }
        let (d, lat) = targets.target(&track, &lane.lane_id);
        let car = spawn_car(&mut commands, &lane.lane_id, d, lat, &track);
        visuals.cars.insert(lane.lane_id.clone(), car);
        let plate = spawn_plate(&mut commands, &lane.lane_id);
        visuals.plates.insert(lane.lane_id.clone(), plate);
    }

    let mut seen: HashMap<String, CarVis> = HashMap::new();
    let dt = time.delta_secs().min(0.1);
    for (mut car, mut transform, mut sprite) in &mut cars {
        let Some(lane) = targets.lanes.get(car.lane_id.as_str()) else {
            continue;
        };
        let (target_d, target_lat) = targets.target(&track, &car.lane_id);
        if !car.initialized {
            car.d = target_d;
            car.lat = target_lat;
            car.initialized = true;
        }
        let diff = target_d - car.d;
        if diff < -0.5 || diff > track.length * 4.0 {
            car.d = target_d;
        } else if diff > 0.0 {
            car.d += diff.min((40.0f32).max(diff * 2.2) * dt);
        }
        car.lat += (target_lat - car.lat) * (dt * 3.0).min(1.0);

        let point = track.at(car.d, car.lat);
        let world = screen_to_world(point.x, point.y);
        transform.translation = world.extend(3.0);
        transform.rotation = Quat::from_rotation_z(-point.ang);

        let score = targets
            .scores
            .get(car.lane_id.as_str())
            .copied()
            .unwrap_or(0.0);
        let big = lane.pos > 0 && lane.pos <= 10 && score > 0.0;
        sprite.custom_size = Some(if big {
            Vec2::new(11.0, 7.0)
        } else {
            Vec2::new(7.0, 5.0)
        });

        let flashing = feed.flashing(&car.lane_id).is_some();
        let blinking = lane.status == "stale" && (time.elapsed_secs() * 2.0) as i64 % 2 == 1;
        let (mut body, mut accent) = (fault_color(&lane.fault), game_color(&lane.game));
        if lane.status == "ended" {
            body = ENDED_BODY;
            accent = ENDED_ACCENT;
        } else if blinking {
            body = STALE_BODY;
        }
        if flashing {
            body = WHITE;
            accent = WHITE;
        }
        sprite.color = body;
        seen.insert(
            car.lane_id.clone(),
            CarVis {
                wx: world.x,
                wy: world.y,
                big,
                accent,
                pos: lane.pos,
            },
        );
    }

    for (stripe, mut sprite) in &mut stripes {
        let Some(vis) = seen.get(&stripe.lane_id) else {
            continue;
        };
        sprite.custom_size = Some(if vis.big {
            Vec2::new(11.0, 2.0)
        } else {
            Vec2::new(7.0, 1.0)
        });
        sprite.color = vis.accent;
    }

    for (plate, mut transform, mut sprite, mut visibility) in &mut plates {
        match seen.get(&plate.lane_id) {
            Some(vis) if vis.big => {
                transform.translation = Vec3::new(vis.wx, vis.wy + 10.0, 3.6);
                sprite.custom_size = Some(Vec2::new(14.0, 10.0));
                sprite.color = plate_color(vis.pos);
                *visibility = Visibility::Visible;
            }
            _ => *visibility = Visibility::Hidden,
        }
    }

    for (text, mut transform, mut content, mut color, mut visibility) in &mut plate_texts {
        match seen.get(&text.lane_id) {
            Some(vis) if vis.big => {
                transform.translation = Vec3::new(vis.wx, vis.wy + 10.0, 4.0);
                content.0 = vis.pos.to_string();
                color.0 = BLACK;
                *visibility = Visibility::Visible;
            }
            _ => *visibility = Visibility::Hidden,
        }
    }
}

fn plate_color(pos: i32) -> Color {
    match pos {
        1 => GOLD,
        2 => SILVER,
        3 => BRONZE,
        _ => WHITE,
    }
}

fn spawn_car(commands: &mut Commands, lane_id: &str, d: f32, lat: f32, track: &Track) -> Entity {
    let point = track.at(d, lat);
    let world = screen_to_world(point.x, point.y);
    let car = commands
        .spawn((
            Sprite::from_color(WHITE, Vec2::new(7.0, 5.0)),
            Transform::from_translation(world.extend(3.0))
                .with_rotation(Quat::from_rotation_z(-point.ang)),
            Car {
                lane_id: lane_id.to_string(),
                d,
                lat,
                initialized: true,
            },
        ))
        .id();
    commands.entity(car).with_children(|parent| {
        parent.spawn((
            Sprite::from_color(WHITE, Vec2::new(7.0, 1.0)),
            Transform::from_xyz(0.0, 0.0, 0.1),
            Stripe {
                lane_id: lane_id.to_string(),
            },
        ));
    });
    car
}

fn spawn_plate(commands: &mut Commands, lane_id: &str) -> (Entity, Entity) {
    let bg = commands
        .spawn((
            Sprite::from_color(WHITE, Vec2::new(14.0, 10.0)),
            Transform::from_xyz(0.0, 0.0, 3.6),
            Visibility::Hidden,
            Plate {
                lane_id: lane_id.to_string(),
            },
        ))
        .id();
    let text = commands
        .spawn((
            Text2d::new(""),
            TextFont {
                font_size: FontSize::Px(8.0),
                ..default()
            },
            TextColor(BLACK),
            Transform::from_xyz(0.0, 0.0, 4.0),
            Visibility::Hidden,
            PlateText {
                lane_id: lane_id.to_string(),
            },
        ))
        .id();
    (bg, text)
}
