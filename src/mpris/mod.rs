//! Async D-Bus/MPRIS layer.
//!
//! Discovers every `org.mpris.MediaPlayer2.*` service on the session bus,
//! reads its `Player` properties (PlaybackStatus + Metadata), and exposes a
//! change-notification stream driven by `PropertiesChanged` / `NameOwnerChanged`
//! D-Bus signals so the daemon reacts immediately instead of polling blindly.

use anyhow::{Context, Result};
use futures_util::stream::StreamExt;
use std::collections::HashMap;
use std::convert::TryFrom;
use tokio::sync::mpsc;
use zbus::zvariant::OwnedValue;
use zbus::{fdo, Connection, MessageStream, MessageType};

const MPRIS_PREFIX: &str = "org.mpris.MediaPlayer2.";
const PLAYER_IFACE: &str = "org.mpris.MediaPlayer2.Player";
const MPRIS_PATH: &str = "/org/mpris/MediaPlayer2";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlaybackStatus {
    Playing,
    Paused,
    Stopped,
}

impl PlaybackStatus {
    fn from_str(s: &str) -> Self {
        match s {
            "Playing" => PlaybackStatus::Playing,
            "Paused" => PlaybackStatus::Paused,
            _ => PlaybackStatus::Stopped,
        }
    }
}

#[derive(Debug, Clone)]
pub struct PlayerInfo {
    /// Full D-Bus bus name, e.g. "org.mpris.MediaPlayer2.spotify".
    pub bus_name: String,
    pub status: PlaybackStatus,
    pub artist: String,
    pub title: String,
    pub album: String,
}

impl PlayerInfo {
    /// Lowercase fragment used to match against `player_priority` entries,
    /// e.g. "org.mpris.MediaPlayer2.spotify" -> "spotify",
    /// "org.mpris.MediaPlayer2.firefox.instance123" -> "firefox.instance123".
    pub fn short_name(&self) -> String {
        self.bus_name
            .strip_prefix(MPRIS_PREFIX)
            .unwrap_or(&self.bus_name)
            .to_lowercase()
    }
}

/// Lists every currently-owned `org.mpris.MediaPlayer2.*` bus name.
pub async fn list_mpris_names(conn: &Connection) -> Result<Vec<String>> {
    let dbus = fdo::DBusProxy::new(conn)
        .await
        .context("failed to build org.freedesktop.DBus proxy")?;
    let names = dbus.list_names().await.context("ListNames call failed")?;
    Ok(names
        .into_iter()
        .map(|n| n.to_string())
        .filter(|n| n.starts_with(MPRIS_PREFIX))
        .collect())
}

fn extract_string(props: &HashMap<String, OwnedValue>, key: &str) -> String {
    props
        .get(key)
        .and_then(|v| String::try_from(v.clone()).ok())
        .unwrap_or_default()
}

/// `xesam:artist` is `as` (array of strings) per the MPRIS spec; we join
/// multiple artists with ", ".
fn extract_artist(metadata: &HashMap<String, OwnedValue>) -> String {
    if let Some(v) = metadata.get("xesam:artist") {
        if let Ok(list) = Vec::<String>::try_from(v.clone()) {
            return list.join(", ");
        }
        // Some players (incorrectly) expose a single string instead of an array.
        if let Ok(s) = String::try_from(v.clone()) {
            return s;
        }
    }
    String::new()
}

/// Fetches `Player` properties for a single MPRIS bus name via
/// `org.freedesktop.DBus.Properties.GetAll`.
pub async fn fetch_player(conn: &Connection, bus_name: &str) -> Result<PlayerInfo> {
    let props_proxy = fdo::PropertiesProxy::builder(conn)
        .destination(bus_name.to_owned())
        .context("invalid destination bus name")?
        .path(MPRIS_PATH)
        .context("invalid MPRIS object path")?
        .build()
        .await
        .context("failed to build Properties proxy")?;

    let props = props_proxy
        .get_all(zbus::names::InterfaceName::try_from(PLAYER_IFACE).unwrap())
        .await
        .with_context(|| format!("GetAll failed for {bus_name}"))?;

    let status = PlaybackStatus::from_str(&extract_string(&props, "PlaybackStatus"));

    let metadata: HashMap<String, OwnedValue> = props
        .get("Metadata")
        .and_then(|v| HashMap::<String, OwnedValue>::try_from(v.clone()).ok())
        .unwrap_or_default();

    let title = extract_string(&metadata, "xesam:title");
    let album = extract_string(&metadata, "xesam:album");
    let artist = extract_artist(&metadata);

    Ok(PlayerInfo {
        bus_name: bus_name.to_string(),
        status,
        artist,
        title,
        album,
    })
}

/// Fetches info for every active MPRIS player currently on the bus.
pub async fn fetch_all_players(conn: &Connection) -> Result<Vec<PlayerInfo>> {
    let names = list_mpris_names(conn).await?;
    let mut result = Vec::with_capacity(names.len());
    for name in names {
        match fetch_player(conn, &name).await {
            Ok(info) => result.push(info),
            Err(e) => tracing::debug!(player = %name, error = %e, "skipping player, failed to read properties"),
        }
    }
    Ok(result)
}

/// Picks the "best" player among all currently active ones, given a priority
/// list of lowercase name fragments (e.g. `["spotify", "firefox"]`).
///
/// Rules:
/// - Players with `Playing` status are preferred over `Paused` ones; `Stopped`
///   players are ignored entirely.
/// - Within the same status tier, players are ordered by their position in
///   `priority` (earlier = higher priority); players not listed in `priority`
///   are considered last, in whatever order D-Bus returned them.
pub fn select_active_player(players: &[PlayerInfo], priority: &[String]) -> Option<PlayerInfo> {
    let rank = |p: &PlayerInfo| -> usize {
        let short = p.short_name();
        priority
            .iter()
            .position(|prefix| short.contains(prefix.to_lowercase().as_str()))
            .unwrap_or(priority.len())
    };

    let mut candidates: Vec<&PlayerInfo> = players
        .iter()
        .filter(|p| p.status != PlaybackStatus::Stopped)
        .collect();

    candidates.sort_by_key(|p| {
        let status_tier = match p.status {
            PlaybackStatus::Playing => 0,
            PlaybackStatus::Paused => 1,
            PlaybackStatus::Stopped => 2,
        };
        (status_tier, rank(p))
    });

    candidates.first().map(|p| (*p).clone())
}

/// Subscribes to `PropertiesChanged` and `NameOwnerChanged` signals on a
/// dedicated session-bus connection and forwards a "something changed" tick
/// through `tx` every time one arrives. The caller is expected to re-query
/// the full player list on each tick (state is cheap to re-fetch and this
/// keeps the logic simple and race-free).
pub async fn spawn_change_watcher(tx: mpsc::Sender<()>) -> Result<()> {
    let conn = Connection::session()
        .await
        .context("failed to connect to the D-Bus session bus")?;

    conn.call_method(
        Some("org.freedesktop.DBus"),
        "/org/freedesktop/DBus",
        Some("org.freedesktop.DBus"),
        "AddMatch",
        &("type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'",),
    )
    .await
    .context("failed to register PropertiesChanged match rule")?;

    conn.call_method(
        Some("org.freedesktop.DBus"),
        "/org/freedesktop/DBus",
        Some("org.freedesktop.DBus"),
        "AddMatch",
        &("type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged'",),
    )
    .await
    .context("failed to register NameOwnerChanged match rule")?;

    let mut stream = MessageStream::from(&conn);
    while let Some(msg) = stream.next().await {
        let msg = match msg {
            Ok(m) => m,
            Err(e) => {
                tracing::warn!(error = %e, "error reading D-Bus message stream");
                continue;
            }
        };
        if msg.header().ok().and_then(|h| h.message_type().ok()) == Some(MessageType::Signal) {
            // We only registered two signal types, either is worth a re-check.
            if tx.send(()).await.is_err() {
                break; // receiver dropped, shut down watcher
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn player(bus_suffix: &str, status: PlaybackStatus) -> PlayerInfo {
        PlayerInfo {
            bus_name: format!("{MPRIS_PREFIX}{bus_suffix}"),
            status,
            artist: "Artist".into(),
            title: "Title".into(),
            album: "Album".into(),
        }
    }

    #[test]
    fn prefers_playing_over_paused() {
        let players = vec![
            player("vlc", PlaybackStatus::Paused),
            player("spotify", PlaybackStatus::Playing),
        ];
        let priority = vec!["spotify".to_string(), "vlc".to_string()];
        let chosen = select_active_player(&players, &priority).unwrap();
        assert_eq!(chosen.short_name(), "spotify");
    }

    #[test]
    fn respects_priority_order_when_multiple_playing() {
        let players = vec![
            player("firefox", PlaybackStatus::Playing),
            player("spotify", PlaybackStatus::Playing),
        ];
        let priority = vec!["spotify".to_string(), "firefox".to_string()];
        let chosen = select_active_player(&players, &priority).unwrap();
        assert_eq!(chosen.short_name(), "spotify");
    }

    #[test]
    fn falls_back_to_paused_when_nothing_playing() {
        let players = vec![player("vlc", PlaybackStatus::Paused)];
        let priority = vec!["spotify".to_string(), "vlc".to_string()];
        let chosen = select_active_player(&players, &priority).unwrap();
        assert_eq!(chosen.short_name(), "vlc");
    }

    #[test]
    fn ignores_stopped_players() {
        let players = vec![player("vlc", PlaybackStatus::Stopped)];
        let priority = vec!["spotify".to_string()];
        assert!(select_active_player(&players, &priority).is_none());
    }

    #[test]
    fn unlisted_player_still_selected_if_playing_alone() {
        let players = vec![player("unknownplayer", PlaybackStatus::Playing)];
        let priority = vec!["spotify".to_string()];
        let chosen = select_active_player(&players, &priority).unwrap();
        assert_eq!(chosen.short_name(), "unknownplayer");
    }

    #[test]
    fn no_players_returns_none() {
        let players: Vec<PlayerInfo> = vec![];
        assert!(select_active_player(&players, &[]).is_none());
    }
}
