mod cleaner;
mod config;
mod limiter;
mod mpris;
mod osc;

use anyhow::{Context, Result};
use clap::Parser;
use config::Config;
use limiter::RateLimiter;
use std::path::PathBuf;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::MissedTickBehavior;
use zbus::Connection;

/// vrc-mpris-osc: bridges Linux MPRIS media players to the VRChat OSC Chatbox.
#[derive(Parser, Debug)]
#[command(name = "vrc-mpris-osc", version, about)]
struct Cli {
    /// Path to an alternate config.toml (default: ~/.config/vrc-mpris-osc/config.toml)
    #[arg(long)]
    config: Option<PathBuf>,

    /// Print what would be sent to stdout instead of actually sending OSC packets.
    #[arg(long)]
    dry_run: bool,

    /// Enable verbose (debug-level) logging.
    #[arg(long)]
    verbose: bool,
}

/// Periodic fallback re-check interval, in case a signal is ever missed
/// (e.g. a player that doesn't emit PropertiesChanged correctly).
const FALLBACK_POLL_INTERVAL: Duration = Duration::from_secs(10);

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    let filter = if cli.verbose { "debug" } else { "info" };
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::new(filter))
        .init();

    let config_path = match &cli.config {
        Some(p) => p.clone(),
        None => Config::default_path().context("resolving default config path")?,
    };

    let (cfg, found) = Config::load(&config_path)?;
    if found {
        tracing::info!(path = %config_path.display(), "loaded configuration");
    } else {
        tracing::warn!(
            path = %config_path.display(),
            "no config file found, using built-in defaults"
        );
    }

    if cli.dry_run {
        tracing::info!("dry-run mode: OSC packets will be printed, not sent");
    }

    // Dedicated connection for querying player state on demand.
    let query_conn = Connection::session()
        .await
        .context("failed to connect to the D-Bus session bus")?;

    let osc_client = if cli.dry_run {
        None
    } else {
        Some(
            osc::OscClient::connect(&cfg.osc.host, cfg.osc.port, &cfg.osc.address)
                .await
                .context("failed to set up OSC UDP client")?,
        )
    };

    let (tx, mut rx) = mpsc::channel::<()>(16);

    // Background task: watches D-Bus for PropertiesChanged/NameOwnerChanged
    // and pokes the main loop whenever something might have changed.
    {
        let tx = tx.clone();
        tokio::spawn(async move {
            if let Err(e) = mpris::spawn_change_watcher(tx).await {
                tracing::error!(error = %e, "D-Bus change watcher exited with an error");
            }
        });
    }

    let mut limiter = RateLimiter::new(Duration::from_secs(cfg.limiter.cooldown_seconds));
    let mut last_sent_was_empty = true; // avoid spamming empty clears repeatedly

    // Kick off an initial evaluation immediately at startup.
    let _ = tx.send(()).await;

    let mut fallback_timer = tokio::time::interval(FALLBACK_POLL_INTERVAL);
    fallback_timer.set_missed_tick_behavior(MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            _ = rx.recv() => {},
            _ = fallback_timer.tick() => {
                tracing::debug!("fallback poll tick");
            },
            _ = tokio::signal::ctrl_c() => {
                tracing::info!("received Ctrl-C, shutting down");
                break;
            }
        }

        if let Err(e) = evaluate_and_send(
            &query_conn,
            &cfg,
            osc_client.as_ref(),
            cli.dry_run,
            &mut limiter,
            &mut last_sent_was_empty,
        )
        .await
        {
            tracing::error!(error = %e, "error while evaluating/sending chatbox update");
        }
    }

    Ok(())
}

async fn evaluate_and_send(
    conn: &Connection,
    cfg: &Config,
    osc_client: Option<&osc::OscClient>,
    dry_run: bool,
    limiter: &mut RateLimiter,
    last_sent_was_empty: &mut bool,
) -> Result<()> {
    let players = mpris::fetch_all_players(conn).await?;
    let chosen = mpris::select_active_player(&players, &cfg.behavior.player_priority);

    let text = match &chosen {
        None => {
            if cfg.behavior.clear_on_stop {
                String::new()
            } else {
                return Ok(()); // nothing to do, keep whatever is currently shown
            }
        }
        Some(player) if player.status == mpris::PlaybackStatus::Paused => {
            if cfg.behavior.clear_on_pause {
                String::new()
            } else {
                return Ok(());
            }
        }
        Some(player) => {
            let artist = cleaner::clean_field(&player.artist, &cfg.cleaner.extra_patterns);
            let title = cleaner::clean_title(&player.title, &player.artist, &cfg.cleaner.extra_patterns);
            let album = cleaner::clean_field(&player.album, &cfg.cleaner.extra_patterns);
            let status = "Playing";
            cleaner::render_template(
                &cfg.chatbox.template,
                &artist,
                &title,
                &album,
                status,
                cfg.chatbox.max_length,
            )
        }
    };

    // Avoid re-sending an empty clear message over and over.
    if text.is_empty() && *last_sent_was_empty {
        return Ok(());
    }

    if !limiter.should_send(&text) {
        tracing::debug!(text = %text, "rate-limited, skipping send");
        return Ok(());
    }

    *last_sent_was_empty = text.is_empty();

    if dry_run || osc_client.is_none() {
        println!("[dry-run] /chatbox/input -> \"{text}\"");
    } else if let Some(client) = osc_client {
        client
            .send_chatbox(&text, cfg.chatbox.send_immediately, cfg.chatbox.notification)
            .await?;
        tracing::info!(text = %text, "sent chatbox update");
    }

    Ok(())
}
