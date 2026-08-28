//! Configuration loading for vrc-mpris-osc.
//!
//! Reads `~/.config/vrc-mpris-osc/config.toml` (or a path given via `--config`)
//! and merges it with hard-coded defaults, so the daemon runs out of the box
//! even without a config file.

use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};

/// OSC transport settings (where VRChat is listening).
#[derive(Debug, Deserialize, Clone)]
#[serde(default)]
pub struct OscConfig {
    pub host: String,
    pub port: u16,
    pub address: String,
}

impl Default for OscConfig {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_string(),
            port: 9000,
            address: "/chatbox/input".to_string(),
        }
    }
}

/// How the chatbox message is built.
#[derive(Debug, Deserialize, Clone)]
#[serde(default)]
pub struct ChatboxConfig {
    /// Template string. Supported placeholders: {artist}, {title}, {album}, {status}.
    pub template: String,
    pub max_length: usize,
    pub send_immediately: bool,
    pub notification: bool,
}

impl Default for ChatboxConfig {
    fn default() -> Self {
        Self {
            template: "🎵 {artist} - {title}".to_string(),
            max_length: 140,
            send_immediately: true,
            notification: false,
        }
    }
}

/// General behavior toggles.
#[derive(Debug, Deserialize, Clone)]
#[serde(default)]
pub struct BehaviorConfig {
    /// Send an empty string to the chatbox when the active player is paused.
    pub clear_on_pause: bool,
    /// Send an empty string to the chatbox when no player is active.
    pub clear_on_stop: bool,
    /// Ordered list of MPRIS bus name fragments (lowercase), highest priority first.
    /// Example: ["spotify", "firefox", "chromium", "vlc"].
    /// Any playing player not in this list is considered after all listed ones,
    /// in the order D-Bus reports it.
    pub player_priority: Vec<String>,
}

impl Default for BehaviorConfig {
    fn default() -> Self {
        Self {
            clear_on_pause: true,
            clear_on_stop: true,
            player_priority: vec![
                "spotify".to_string(),
                "firefox".to_string(),
                "chromium".to_string(),
                "chrome".to_string(),
                "vlc".to_string(),
            ],
        }
    }
}

/// Rate limiting to avoid VRChat's anti-spam chatbox throttling.
#[derive(Debug, Deserialize, Clone)]
#[serde(default)]
pub struct LimiterConfig {
    /// Minimum seconds between two OSC sends, unless the track actually changed.
    pub cooldown_seconds: u64,
}

impl Default for LimiterConfig {
    fn default() -> Self {
        Self { cooldown_seconds: 4 }
    }
}

/// Text-cleaning options.
#[derive(Debug, Deserialize, Clone)]
#[serde(default)]
pub struct CleanerConfig {
    /// Additional user-supplied regex patterns (applied after the built-in ones),
    /// matches are removed from the string.
    pub extra_patterns: Vec<String>,
}

impl Default for CleanerConfig {
    fn default() -> Self {
        Self { extra_patterns: Vec::new() }
    }
}

#[derive(Debug, Deserialize, Clone, Default)]
#[serde(default)]
pub struct Config {
    pub osc: OscConfig,
    pub chatbox: ChatboxConfig,
    pub behavior: BehaviorConfig,
    pub limiter: LimiterConfig,
    pub cleaner: CleanerConfig,
}

impl Config {
    /// Default config path: `~/.config/vrc-mpris-osc/config.toml`.
    pub fn default_path() -> Result<PathBuf> {
        let base = dirs::config_dir().context("could not determine XDG config directory")?;
        Ok(base.join("vrc-mpris-osc").join("config.toml"))
    }

    /// Load configuration from `path` if it exists; otherwise return defaults.
    /// If `path` was explicitly requested but doesn't exist, this still falls
    /// back to defaults but the caller can log a warning.
    pub fn load(path: &Path) -> Result<(Self, bool)> {
        if path.exists() {
            let raw = std::fs::read_to_string(path)
                .with_context(|| format!("failed to read config file {}", path.display()))?;
            let cfg: Config = toml::from_str(&raw)
                .with_context(|| format!("failed to parse config file {}", path.display()))?;
            Ok((cfg, true))
        } else {
            Ok((Config::default(), false))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_sane() {
        let cfg = Config::default();
        assert_eq!(cfg.osc.port, 9000);
        assert_eq!(cfg.osc.address, "/chatbox/input");
        assert_eq!(cfg.chatbox.max_length, 140);
        assert!(cfg.behavior.clear_on_pause);
        assert!(cfg.behavior.clear_on_stop);
        assert_eq!(cfg.limiter.cooldown_seconds, 4);
    }

    #[test]
    fn missing_file_falls_back_to_defaults() {
        let (cfg, found) = Config::load(Path::new("/nonexistent/path/config.toml")).unwrap();
        assert!(!found);
        assert_eq!(cfg.osc.host, "127.0.0.1");
    }

    #[test]
    fn parses_partial_toml_with_defaults_for_missing_fields() {
        let dir = std::env::temp_dir().join(format!("vrc-mpris-osc-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.toml");
        std::fs::write(
            &path,
            r#"
            [chatbox]
            template = "{artist}: {title}"
            "#,
        )
        .unwrap();

        let (cfg, found) = Config::load(&path).unwrap();
        assert!(found);
        assert_eq!(cfg.chatbox.template, "{artist}: {title}");
        // untouched sections keep their defaults
        assert_eq!(cfg.osc.port, 9000);
        assert_eq!(cfg.limiter.cooldown_seconds, 4);

        std::fs::remove_dir_all(&dir).ok();
    }
}
