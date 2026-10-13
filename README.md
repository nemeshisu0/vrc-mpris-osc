# vrc-mpris-osc

A lightweight, native Linux daemon that watches whatever you're listening to
(Spotify, Firefox/Chrome tabs, VLC, mpv, and anything else that speaks
[MPRIS](https://specifications.freedesktop.org/mpris-spec/latest/)) and shows
it in your **VRChat Chatbox** via OSC — automatically, with no manual typing.

```
🎵 Tame Impala - The Less I Know The Better
```

## Why

VRChat's Chatbox is a great, low-friction way to tell people in-world what
you're listening to, but nothing does this for you on Linux out of the box:

- Windows users have several OSC "now playing" tools; Linux users mostly
  don't, or existing scripts are one-off polling hacks tied to a single
  player.
- Raw MPRIS metadata is messy: YouTube/Spotify titles are full of
  `[Official Video]`, `(Remastered 2024)`, `feat. Someone`, and other noise
  you don't want broadcast to a room full of people.
- If you have several media apps open at once (a Spotify client *and* a
  YouTube tab, say), something has to decide which one "wins".
- Sending an OSC message on every single property tick will trip VRChat's
  chatbox anti-spam throttle if you're not careful.

`vrc-mpris-osc` solves all four: it listens for D-Bus/MPRIS events instead of
polling blindly, sanitizes titles with a configurable regex pipeline,
picks the right player using a priority list you define, and rate-limits its
own output so it never spams the chatbox.

## How it works

```
┌──────────┐   PropertiesChanged /   ┌──────────┐   clean + template   ┌──────────┐   rate-limited   ┌────────────────┐
│  MPRIS   │   NameOwnerChanged      │  Player  │   ────────────────►  │  Cleaner │   ─────────────► │  OSC UDP client │──► VRChat
│ players  │ ───────────────────►   │ selector │                       │ + config │                   │ 127.0.0.1:9000  │
└──────────┘   (D-Bus signals)       └──────────┘                       └──────────┘                   └────────────────┘
```

1. **`mpris`** — connects to the session D-Bus bus, subscribes to
   `PropertiesChanged` and `NameOwnerChanged` signals, and re-reads the full
   set of `org.mpris.MediaPlayer2.*` players whenever something changes
   (plus a 10-second fallback poll as a safety net for misbehaving players).
2. Player selection picks the best candidate: `Playing` players are
   preferred over `Paused` ones, `Stopped` players are ignored, and ties are
   broken using the `player_priority` list from your config (e.g. always
   prefer Spotify over a browser tab).
3. **`cleaner`** — strips common noise (`[Official Video]`,
   `(Remastered 2024)`, `feat. X`, etc.) from artist/title/album via regex,
   plus any custom patterns you add, then renders your template string and
   truncates it to a configurable max length.
4. **`limiter`** — only actually sends an OSC packet when the track changed,
   or when the cooldown window has elapsed for a repeat of the same text.
5. **`osc`** — encodes an OSC message and sends it over UDP to VRChat's
   `/chatbox/input` endpoint, with `send_immediately` and `notification`
   flags as configured.

When nothing is playing (or the active player is paused, depending on your
config), the daemon sends an empty string to clear the chatbox instead of
leaving stale text on screen.

## Installation

### From source (any distro with Rust + Cargo)

```bash
git clone https://github.com/yourname/vrc-mpris-osc.git
cd vrc-mpris-osc
cargo build --release
install -Dm755 target/release/vrc-mpris-osc ~/.cargo/bin/vrc-mpris-osc
```

Requires Rust 1.75 or newer (`rustup` or your distro's Rust package).

### Arch Linux / AUR

A ready-to-use `PKGBUILD` is included in this repository:

```bash
git clone https://github.com/yourname/vrc-mpris-osc.git
cd vrc-mpris-osc
makepkg -si
```

### Running as a systemd user service

A user unit file is provided in `vrc-mpris-osc.service`:

```bash
mkdir -p ~/.config/systemd/user
cp vrc-mpris-osc.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vrc-mpris-osc
```

Check logs with:

```bash
journalctl --user -u vrc-mpris-osc -f
```

(If you installed via the AUR package, the unit is already placed under
`/usr/lib/systemd/user/` and points at `/usr/bin/vrc-mpris-osc`.)

## Configuration

On first run, `vrc-mpris-osc` looks for
`~/.config/vrc-mpris-osc/config.toml`. If it doesn't exist, built-in
defaults are used and nothing needs to be created — the daemon works
out of the box.

To customize it, copy the annotated example:

```bash
mkdir -p ~/.config/vrc-mpris-osc
cp config.example.toml ~/.config/vrc-mpris-osc/config.toml
```

Every section is optional — omitted keys fall back to their defaults. See
`config.example.toml` for the full, commented reference. Summary:

```toml
[osc]
host = "127.0.0.1"
port = 9000
address = "/chatbox/input"

[chatbox]
template = "🎵 {artist} - {title}"   # {artist} {title} {album} {status} {volume}
max_length = 140
send_immediately = true
notification = false

[behavior]
clear_on_pause = true
clear_on_stop = true
player_priority = ["spotify", "firefox", "chromium", "chrome", "vlc"]

[limiter]
cooldown_seconds = 4

[cleaner]
extra_patterns = []   # additional regexes to strip from titles/artists/albums
```

### Player priority

`player_priority` is matched against the *suffix* of each player's D-Bus bus
name in lowercase, e.g. `org.mpris.MediaPlayer2.spotify` → `spotify`,
`org.mpris.MediaPlayer2.firefox.instance1234` → `firefox.instance1234`
(which still contains, and therefore matches, `"firefox"`). Whichever
*playing* player appears earliest in the list wins; anything not listed is
considered lowest priority (but still selected if it's the only one playing).

## CLI flags

```
vrc-mpris-osc [OPTIONS]

Options:
      --config <PATH>   Path to an alternate config.toml
                         (default: ~/.config/vrc-mpris-osc/config.toml)
      --dry-run         Print what would be sent to stdout instead of
                         actually sending OSC packets (useful for debugging
                         your template/cleaning rules)
      --verbose         Enable debug-level logging
  -h, --help            Print help
  -V, --version         Print version
```

Example for testing your setup without touching VRChat:

```bash
vrc-mpris-osc --dry-run --verbose
```

## Project layout

```
src/
├── main.rs      # CLI, wiring, main event loop
├── mpris/       # async D-Bus/MPRIS discovery, signal watcher, player selection
├── cleaner/     # regex-based title/artist sanitization + truncation
├── osc/         # OSC-over-UDP client for /chatbox/input
├── config/      # config.toml loading with sane defaults
└── limiter/     # cooldown-based rate limiter
```

Run the test suite (unit tests cover the cleaner's regex rules, the rate
limiter's timing logic, config defaults/merging, player-selection priority,
and OSC packet encoding) with:

```bash
cargo test
```

## Troubleshooting

- **Nothing shows up in the chatbox** — make sure VRChat's OSC is enabled
  (Action Menu → Options → OSC → Enabled), and that no firewall is blocking
  local UDP traffic on port 9000.
- **Wrong player is picked** — adjust `player_priority` in your config; the
  list is matched top to bottom.
- **Titles still look messy** — add your own regex to `cleaner.extra_patterns`
  in `config.toml`.
- **Getting spam-limited by VRChat** — raise `limiter.cooldown_seconds`.

## Installation

### Via Cargo
```bash
cargo install --git https://github.com/nemeshisu0/vrc-mpris-osc
```

## GUI Mini-Launcher (PyQt6)

If you prefer a visual window with an **ON / OFF** toggle button instead of a daemon service:

```bash
python3 vrc-music-launcher.py
```

### Key Features:
- **Big ON / OFF switch**: Enable or disable chatbox broadcasts on the fly with a single click. When turned OFF, it clears the chatbox instantly.
- **Spotify Internal Volume**: Dynamically detects and displays your Spotify volume level (`🔇`, `🔈`, `🔉`, `🔊`) in both the launcher and the VRChat chatbox.
- **VRChat Process Sync**: Automatically detects when `VRChat.exe` is running, stays idle when the game is closed, and automatically exits when you quit VRChat.
- **Zero background leftovers**: Clean process and socket termination when closed.
- **CLI / Hotkey Toggle**: Toggle the launcher remotely while in VR by running:
  ```bash
  python3 vrc-music-launcher.py --toggle
  ```
  *(Compatible with Hyprland, Sway, i3, or KDE global shortcuts!)*

## License

MIT — see [`LICENSE`](LICENSE).
