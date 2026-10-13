#!/usr/bin/env python3
"""
VRChat Music OSC Launcher
=========================
Mini launcher grafico per trasmettere il brano in riproduzione (Spotify, browser, MPRIS),
il livello di volume di Spotify e i testi sincronizzati (Lyrics) nella Chatbox di VRChat via OSC (127.0.0.1:9000).

Funzionalità:
- Grande pulsante ON / OFF per abilitare/disabilitare l'invio al volo.
- Selezione modalità Chatbox:
  * Solo Titolo e Volume
  * Solo Lyrics (Testo)
  * Entrambi (Titolo sopra, Lyrics sotto su nuova riga)
- Supporto testi sincronizzati (Lyrics) via LRCLIB con anteprima in tempo reale.
- Monitoraggio del livello di volume interno di Spotify.
- Riconoscimento automatico del processo VRChat e auto-chiusura opzionale.
- Salvataggio automatico delle impostazioni (QSettings).
- Nessun processo orfano in background quando chiuso.
- Supporto toggle da linea di comando / scorciatoia tastiera: `music --toggle`.
"""

import os
import sys
import re
import time
import socket
import signal
import subprocess
import atexit
import json
import urllib.request
import urllib.parse
import threading

from PyQt6.QtCore import Qt, QTimer, QSocketNotifier, QSettings
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QFrame, QComboBox, QSizePolicy
)

PID_FILE = "/tmp/vrc_music_launcher.pid"
OSC_IP = "127.0.0.1"
OSC_PORT = 9000
OSC_ADDR = "/chatbox/input"

MODE_TITLE_ONLY = 0
MODE_LYRICS_ONLY = 1
MODE_BOTH = 2

CLEAN_PATTERNS = [
    r'\[\s*(?:official\s+)?(?:music\s+)?video\s*\]',
    r'\(\s*(?:official\s+)?(?:music\s+)?video\s*\)',
    r'\[\s*(?:official\s+)?audio\s*\]',
    r'\(\s*(?:official\s+)?audio\s*\)',
    r'\[\s*lyric(?:s)?\s*(?:video)?\s*\]',
    r'\(\s*lyric(?:s)?\s*(?:video)?\s*\)',
    r'\[\s*visualizer\s*\]',
    r'\(\s*visualizer\s*\)',
    r'\[\s*hd\s*\]',
    r'\(\s*hd\s*\)',
    r'\[\s*4k\s*\]',
    r'\(\s*4k\s*\)',
    r'\(\s*remaster(?:ed)?(?:\s+\d{4})?\s*\)',
    r'\[\s*remaster(?:ed)?(?:\s+\d{4})?\s*\]',
    r'\bft\.?\s+[^(\[]+',
    r'\bfeat\.?\s+[^(\[]+',
]


def clean_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    for pat in CLEAN_PATTERNS:
        cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -_')
    return cleaned


def format_volume(vol_str):
    if not vol_str:
        return "", None
    try:
        pct = int(round(float(vol_str) * 100))
        pct = max(0, min(100, pct))
        if pct == 0:
            icon = "🔇"
        elif pct < 33:
            icon = "🔈"
        elif pct < 67:
            icon = "🔉"
        else:
            icon = "🔊"
        return f"{icon} {pct}%", pct
    except (ValueError, TypeError):
        return "", None


def parse_lrc(lrc_text: str):
    if not lrc_text:
        return []
    lines = []
    pattern = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)')
    for line in lrc_text.splitlines():
        match = pattern.match(line.strip())
        if match:
            total_sec = int(match.group(1)) * 60 + float(match.group(2))
            text = match.group(3).strip()
            lines.append((total_sec, text))
    return sorted(lines, key=lambda x: x[0])


def get_current_lyric_line(lyrics_list, pos_sec):
    if not lyrics_list or pos_sec is None:
        return None
    current = None
    for ts, text in lyrics_list:
        if ts <= pos_sec:
            current = text
        else:
            break
    return current


def osc_pad(b: bytes) -> bytes:
    return b + b'\x00' * ((4 - len(b) % 4) % 4)


def osc_str(s: str) -> bytes:
    return osc_pad(s.encode('utf-8') + b'\x00')


def build_chatbox_packet(text: str, send_immediately: bool = True, notification: bool = False) -> bytes:
    addr = osc_str(OSC_ADDR)
    tag_chars = ',s'
    tag_chars += 'T' if send_immediately else 'F'
    tag_chars += 'T' if notification else 'F'
    tags = osc_str(tag_chars)
    payload = osc_str(text)
    return addr + tags + payload


def is_vrchat_running():
    try:
        for pid_dir in os.listdir('/proc'):
            if pid_dir.isdigit():
                try:
                    with open(f'/proc/{pid_dir}/comm', 'r') as f:
                        comm = f.read().strip()
                        if comm in ('VRChat.exe', 'vrchat.exe'):
                            return True, int(pid_dir)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
    except Exception:
        pass
    return False, None


def get_player_metadata():
    try:
        cmd = ['playerctl', '-p', 'spotify,%any', 'metadata', '--format', '{{volume}}|{{status}}|{{playerName}}|{{artist}}|{{title}}|{{position}}']
        res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8').strip()
        if not res:
            return None, None, None, None, None, None
        parts = res.split('|', 5)
        if len(parts) == 6:
            pos_sec = None
            if parts[5].strip().isdigit():
                pos_sec = float(parts[5].strip()) / 1_000_000.0
            return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip(), pos_sec
    except Exception:
        pass
    return None, None, None, None, None, None


def get_running_launcher_pid():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            pass
    return None


class MusicLauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VRChat Music OSC Launcher")
        self.setFixedSize(430, 620)

        # Gestione preferenze persistenti
        self.settings = QSettings("vrc-mpris-osc", "MusicLauncher")

        self.is_on = True
        self.vrchat_detected_once = False
        self.last_sent_text = None
        self.last_sent_time = 0.0
        self.cooldown = 4.0
        self.last_detected_volume = None

        # Gestione Lyrics
        self.lyrics_cache = {}  # (artist, title): [(ts, text)]
        self.current_lyrics_key = None
        self.current_lyrics_lines = []
        self.lyrics_fetching = False

        # Socket UDP per invio OSC
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Gestione segnali (es. SIGUSR1 per toggle remoto da tastiera)
        self.setup_signal_handling()

        self.setup_ui()
        self.update_toggle_button_ui()

        # Timer principale (controlla VRChat, musica, volume e lyrics ogni 800ms)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(800)

        # Esegui subito il primo controllo
        self.on_tick()

    def setup_signal_handling(self):
        self.rsock, self.wsock = socket.socketpair()
        self.rsock.setblocking(False)
        self.wsock.setblocking(False)

        signal.signal(signal.SIGUSR1, lambda s, f: None)
        signal.set_wakeup_fd(self.wsock.fileno())

        self.sig_notifier = QSocketNotifier(self.rsock.fileno(), QSocketNotifier.Type.Read)
        self.sig_notifier.activated.connect(self.on_posix_signal)

    def on_posix_signal(self):
        try:
            data = self.rsock.recv(1024)
            for sig in data:
                if sig == signal.SIGUSR1:
                    self.toggle_state()
        except Exception:
            pass

    def setup_ui(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e2e;
            }
            QWidget {
                color: #cdd6f4;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            }
            QFrame.card {
                background-color: #181825;
                border: 1px solid #313244;
                border-radius: 12px;
                padding: 10px;
            }
            QComboBox {
                background-color: #181825;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 13px;
                font-weight: 500;
            }
            QComboBox:hover {
                border: 1px solid #89b4fa;
            }
            QComboBox::drop-down {
                border: none;
                padding-right: 8px;
            }
            QComboBox QAbstractItemView {
                background-color: #181825;
                color: #cdd6f4;
                selection-background-color: #313244;
                selection-color: #89b4fa;
                border: 1px solid #45475a;
                border-radius: 6px;
                padding: 4px;
            }
            QCheckBox {
                color: #bac2de;
                font-size: 12px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 1px solid #45475a;
                background-color: #313244;
            }
            QCheckBox::indicator:checked {
                background-color: #89b4fa;
                border: 1px solid #89b4fa;
            }
            QPushButton#quitBtn {
                background-color: #313244;
                color: #bac2de;
                border: 1px solid #45475a;
                border-radius: 8px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton#quitBtn:hover {
                background-color: #45475a;
                color: #cdd6f4;
            }
            QLabel.volumeBadge {
                background-color: #313244;
                color: #f9e2af;
                font-weight: bold;
                font-size: 12px;
                padding: 3px 8px;
                border-radius: 6px;
            }
            QFrame.lyricsBox {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 6px 10px;
            }
        """)

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(18, 14, 18, 14)
        main_layout.setSpacing(10)

        # 1. Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)
        title_label = QLabel("🎵 VRChat Music OSC")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #cdd6f4;")
        subtitle_label = QLabel("Musica, Volume e Testi (Lyrics) nella Chatbox di VRChat")
        subtitle_label.setStyleSheet("font-size: 11px; color: #a6adc8;")
        header_layout.addWidget(title_label)
        header_layout.addWidget(subtitle_label)
        main_layout.addLayout(header_layout)

        # 2. VRChat Status Card
        vrchat_card = QFrame()
        vrchat_card.setProperty("class", "card")
        vrchat_card_layout = QHBoxLayout(vrchat_card)
        vrchat_card_layout.setContentsMargins(12, 8, 12, 8)

        self.vrchat_status_label = QLabel("○ Verifica VRChat in corso...")
        self.vrchat_status_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #9399b2;")
        vrchat_card_layout.addWidget(self.vrchat_status_label)
        main_layout.addWidget(vrchat_card)

        # 3. Grande Pulsante ON / OFF
        self.toggle_btn = QPushButton()
        self.toggle_btn.setFixedHeight(70)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(self.toggle_state)
        main_layout.addWidget(self.toggle_btn)

        # 4. Scheda Brano Corrente (Now Playing + Volume + Lyrics Preview)
        song_card = QFrame()
        song_card.setProperty("class", "card")
        song_card_layout = QVBoxLayout(song_card)
        song_card_layout.setContentsMargins(12, 10, 12, 10)
        song_card_layout.setSpacing(6)

        card_title = QLabel("BRANO IN RIPRODUZIONE")
        card_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #6c7086; letter-spacing: 1px;")
        song_card_layout.addWidget(card_title)

        self.track_label = QLabel("Nessun brano attivo")
        self.track_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #cdd6f4;")
        self.track_label.setWordWrap(True)
        song_card_layout.addWidget(self.track_label)

        # Riga info con player e volume badge
        player_row = QHBoxLayout()
        player_row.setSpacing(10)

        self.player_label = QLabel("Player: Non rilevato")
        self.player_label.setStyleSheet("font-size: 12px; color: #89b4fa;")
        player_row.addWidget(self.player_label)

        player_row.addStretch()

        self.volume_badge = QLabel("🔊 --%")
        self.volume_badge.setProperty("class", "volumeBadge")
        player_row.addWidget(self.volume_badge)

        song_card_layout.addLayout(player_row)

        # Box per Anteprima Testo (Lyrics)
        lyrics_box = QFrame()
        lyrics_box.setProperty("class", "lyricsBox")
        lyrics_box_layout = QVBoxLayout(lyrics_box)
        lyrics_box_layout.setContentsMargins(8, 6, 8, 6)

        self.lyrics_preview_label = QLabel("🎤 Lyrics: In attesa di riproduzione...")
        self.lyrics_preview_label.setStyleSheet("font-size: 11px; color: #f5c2e7; font-style: italic;")
        self.lyrics_preview_label.setWordWrap(True)
        lyrics_box_layout.addWidget(self.lyrics_preview_label)

        song_card_layout.addWidget(lyrics_box)

        self.osc_status_label = QLabel("Stato Chatbox: In attesa")
        self.osc_status_label.setStyleSheet("font-size: 11px; color: #a6adc8; font-style: italic;")
        song_card_layout.addWidget(self.osc_status_label)

        main_layout.addWidget(song_card)

        # 5. Selezione Modalità Chatbox (Solo Titolo / Solo Lyrics / Entrambi)
        mode_layout = QVBoxLayout()
        mode_layout.setSpacing(4)
        mode_title = QLabel("MODALITÀ VISUALIZZAZIONE CHATBOX")
        mode_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #6c7086; letter-spacing: 1px;")
        mode_layout.addWidget(mode_title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("🎵 Solo Titolo (con Volume)", MODE_TITLE_ONLY)
        self.mode_combo.addItem("🎤 Solo Lyrics (Testo)", MODE_LYRICS_ONLY)
        self.mode_combo.addItem("🎵 + 🎤 Entrambi (Titolo sopra, Lyrics sotto)", MODE_BOTH)

        saved_mode = self.settings.value("chatbox_mode", MODE_TITLE_ONLY, type=int)
        if 0 <= saved_mode <= 2:
            self.mode_combo.setCurrentIndex(saved_mode)
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)

        mode_layout.addWidget(self.mode_combo)
        main_layout.addLayout(mode_layout)

        # 6. Opzioni Checkbox
        options_layout = QVBoxLayout()
        options_layout.setSpacing(5)

        saved_vol = self.settings.value("show_volume", True, type=bool)
        self.show_volume_chk = QCheckBox("🔊 Includi percentuale volume (es. 🔉 46%)")
        self.show_volume_chk.setChecked(saved_vol)
        self.show_volume_chk.toggled.connect(self.on_volume_chk_changed)
        options_layout.addWidget(self.show_volume_chk)

        saved_autoclose = self.settings.value("auto_close", True, type=bool)
        self.auto_close_chk = QCheckBox("Chiudi launcher automaticamente all'uscita di VRChat")
        self.auto_close_chk.setChecked(saved_autoclose)
        self.auto_close_chk.toggled.connect(lambda v: self.settings.setValue("auto_close", v))
        options_layout.addWidget(self.auto_close_chk)

        saved_clear = self.settings.value("clear_on_pause", True, type=bool)
        self.clear_on_pause_chk = QCheckBox("Pulisci chatbox quando metti in pausa o premi OFF")
        self.clear_on_pause_chk.setChecked(saved_clear)
        self.clear_on_pause_chk.toggled.connect(lambda v: self.settings.setValue("clear_on_pause", v))
        options_layout.addWidget(self.clear_on_pause_chk)

        main_layout.addLayout(options_layout)

        # 7. Barra inferiore (Info porta + Esci)
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 2, 0, 0)

        port_info = QLabel("OSC: 127.0.0.1:9000")
        port_info.setStyleSheet("font-size: 11px; color: #6c7086;")
        bottom_layout.addWidget(port_info)

        bottom_layout.addStretch()

        quit_btn = QPushButton("Esci")
        quit_btn.setObjectName("quitBtn")
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.clicked.connect(self.close)
        bottom_layout.addWidget(quit_btn)

        main_layout.addLayout(bottom_layout)

    def update_toggle_button_ui(self):
        if self.is_on:
            self.toggle_btn.setText("🟢 ON\n(Musica Attiva - Clicca per Disattivare)")
            self.toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #a6e3a1;
                    color: #11111b;
                    border: 2px solid #94e2d5;
                    border-radius: 14px;
                    font-size: 16px;
                    font-weight: bold;
                    padding: 6px;
                }
                QPushButton:hover {
                    background-color: #94e2d5;
                }
                QPushButton:pressed {
                    background-color: #89dceb;
                }
            """)
        else:
            self.toggle_btn.setText("🔴 OFF\n(Musica Disattivata - Clicca per Attivare)")
            self.toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #313244;
                    color: #f38ba8;
                    border: 2px solid #f38ba8;
                    border-radius: 14px;
                    font-size: 16px;
                    font-weight: bold;
                    padding: 6px;
                }
                QPushButton:hover {
                    background-color: #45475a;
                }
                QPushButton:pressed {
                    background-color: #585b70;
                }
            """)

    def on_mode_changed(self, index):
        self.settings.setValue("chatbox_mode", index)
        self.last_sent_text = None
        self.on_tick()

    def on_volume_chk_changed(self, checked):
        self.settings.setValue("show_volume", checked)
        self.last_sent_text = None
        self.on_tick()

    def toggle_state(self):
        self.is_on = not self.is_on
        self.update_toggle_button_ui()
        if not self.is_on:
            if self.clear_on_pause_chk.isChecked():
                self.send_chatbox_clear()
            self.osc_status_label.setText("Stato Chatbox: Disattivato manualmente (OFF)")
        else:
            self.osc_status_label.setText("Stato Chatbox: Attivato (ON)")
            self.last_sent_text = None
            self.on_tick()

    def fetch_lyrics_async(self, artist: str, title: str):
        key = (artist.lower().strip(), title.lower().strip())
        if key in self.lyrics_cache:
            self.current_lyrics_key = key
            self.current_lyrics_lines = self.lyrics_cache[key]
            return

        if self.lyrics_fetching:
            return

        self.current_lyrics_key = key
        self.current_lyrics_lines = []
        self.lyrics_fetching = True

        def worker():
            lrc_text = None
            try:
                params = urllib.parse.urlencode({'track_name': title, 'artist_name': artist})
                url = f"https://lrclib.net/api/get?{params}"
                req = urllib.request.Request(url, headers={'User-Agent': 'vrc-mpris-osc/0.1.0'})
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get('syncedLyrics'):
                        lrc_text = data['syncedLyrics']
            except Exception:
                pass

            if not lrc_text:
                try:
                    q = urllib.parse.urlencode({'q': f"{artist} {title}"})
                    url = f"https://lrclib.net/api/search?{q}"
                    req = urllib.request.Request(url, headers={'User-Agent': 'vrc-mpris-osc/0.1.0'})
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        results = json.loads(resp.read().decode())
                        if isinstance(results, list):
                            for item in results:
                                if item.get('syncedLyrics'):
                                    lrc_text = item['syncedLyrics']
                                    break
                except Exception:
                    pass

            parsed = parse_lrc(lrc_text) if lrc_text else []
            self.lyrics_cache[key] = parsed
            if self.current_lyrics_key == key:
                self.current_lyrics_lines = parsed
            self.lyrics_fetching = False

        threading.Thread(target=worker, daemon=True).start()

    def send_chatbox(self, text: str):
        try:
            packet = build_chatbox_packet(text, send_immediately=True, notification=False)
            self.sock.sendto(packet, (OSC_IP, OSC_PORT))
        except Exception as e:
            print(f"Errore invio OSC: {e}", file=sys.stderr)

    def send_chatbox_clear(self):
        self.send_chatbox("")
        self.last_sent_text = None

    def on_tick(self):
        vrchat_running, vrc_pid = is_vrchat_running()

        # Aggiorna indicatore VRChat
        if vrchat_running:
            self.vrchat_detected_once = True
            self.vrchat_status_label.setText(f"● VRChat in esecuzione (PID {vrc_pid})")
            self.vrchat_status_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #a6e3a1;")
        else:
            self.vrchat_status_label.setText("○ In attesa dell'avvio di VRChat...")
            self.vrchat_status_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #9399b2;")

            # Auto-chiusura se VRChat è stato chiuso dopo essere stato attivo
            if self.vrchat_detected_once and self.auto_close_chk.isChecked():
                self.send_chatbox_clear()
                self.vrchat_status_label.setText("● VRChat chiuso. Chiusura launcher...")
                self.timer.stop()
                QTimer.singleShot(1000, self.close)
                return

        # Leggi stato da playerctl (volume, status, player, artist, title, position)
        vol_str, status, player, artist, title, pos_sec = get_player_metadata()
        vol_text, vol_pct = format_volume(vol_str)

        # Aggiorna badge volume
        if vol_text:
            self.volume_badge.setText(vol_text)
            self.volume_badge.setVisible(True)
        else:
            self.volume_badge.setVisible(False)

        current_lyric_line = None

        if status:
            clean_t = clean_text(title)
            clean_a = clean_text(artist) if artist else ""
            display_title = f"{clean_t} - {clean_a}" if clean_a else clean_t

            icon = "▶" if status.lower() == "playing" else "⏸"
            self.track_label.setText(f"{icon} {display_title}")
            self.player_label.setText(f"Player: {player.capitalize()} ({status.capitalize()})")

            # Recupera lyrics per il brano corrente
            key = (clean_a.lower().strip(), clean_t.lower().strip())
            if self.current_lyrics_key != key:
                self.fetch_lyrics_async(clean_a, clean_t)

            current_lyric_line = get_current_lyric_line(self.current_lyrics_lines, pos_sec)

            # Aggiorna label anteprima Lyrics
            if current_lyric_line:
                self.lyrics_preview_label.setText(f'🎤 "{current_lyric_line}"')
            elif self.current_lyrics_lines:
                self.lyrics_preview_label.setText("🎤 [Pausa strumentale / Intro]")
            elif self.lyrics_fetching:
                self.lyrics_preview_label.setText("🎤 Ricerca testo in corso...")
            else:
                self.lyrics_preview_label.setText("🎤 Nessun testo sincronizzato disponibile")
        else:
            self.track_label.setText("Nessun brano attivo")
            self.player_label.setText("Player: Non rilevato")
            self.lyrics_preview_label.setText("🎤 Lyrics: In attesa di riproduzione...")

        # Se il pulsante principale è su OFF
        if not self.is_on:
            return

        # Se VRChat non è aperto, non inviare pacchetti
        if not vrchat_running:
            self.osc_status_label.setText("Stato Chatbox: In attesa di VRChat...")
            self.last_sent_text = None
            return

        # Se il player è in riproduzione
        if status and status.lower() == 'playing' and title:
            clean_t = clean_text(title)
            clean_a = clean_text(artist) if artist else ""
            song_part = f"{clean_t} - {clean_a}" if clean_a else clean_t

            # Formattazione base del titolo con eventuale volume
            if self.show_volume_chk.isChecked() and vol_text:
                title_msg = f"🎵 {song_part} [{vol_text}]"
            else:
                title_msg = f"🎵 {song_part}"

            mode = self.mode_combo.currentIndex()

            if mode == MODE_TITLE_ONLY:
                osc_msg = title_msg
            elif mode == MODE_LYRICS_ONLY:
                if current_lyric_line:
                    osc_msg = f"🎤 {current_lyric_line}"
                else:
                    osc_msg = title_msg
            elif mode == MODE_BOTH:
                if current_lyric_line:
                    # Titolo sopra, Lyrics sotto
                    line1 = title_msg
                    line2 = f"🎤 {current_lyric_line}"
                    combined = f"{line1}\n{line2}"
                    if len(combined) <= 140:
                        osc_msg = combined
                    else:
                        avail = 140 - len(line1) - 2
                        if avail > 15:
                            line2 = line2[:avail - 1] + "…"
                            osc_msg = f"{line1}\n{line2}"
                        else:
                            osc_msg = combined[:137] + "…"
                else:
                    osc_msg = title_msg
            else:
                osc_msg = title_msg

            if len(osc_msg) > 140:
                osc_msg = osc_msg[:137] + "..."

            now = time.time()
            text_changed = (osc_msg != self.last_sent_text)
            cooldown_passed = (now - self.last_sent_time >= self.cooldown)

            # Invia se il testo è cambiato (nuova riga lyrics, cambio volume) o dopo cooldown
            if text_changed or cooldown_passed:
                # Rate limit minimo per VRChat (1.4s tra un invio e l'altro)
                if text_changed and (now - self.last_sent_time < 1.4):
                    return

                self.send_chatbox(osc_msg)
                self.last_sent_text = osc_msg
                self.last_sent_time = now

                # Mostra anteprima compatta nello stato
                display_status = osc_msg.replace('\n', ' ⮑  ')
                self.osc_status_label.setText(f"Inviato: {display_status}")
        else:
            # Musica in pausa o ferma
            if self.last_sent_text is not None:
                if self.clear_on_pause_chk.isChecked():
                    self.send_chatbox_clear()
                self.last_sent_text = None
                self.osc_status_label.setText("Chatbox pulita (Musica in pausa)")

    def closeEvent(self, event):
        if hasattr(self, 'timer'):
            self.timer.stop()
        if hasattr(self, 'sock'):
            try:
                if self.clear_on_pause_chk.isChecked():
                    self.send_chatbox_clear()
                self.sock.close()
            except Exception:
                pass
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except OSError:
            pass
        event.accept()


def cleanup_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except OSError:
        pass


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip()
        if arg in ('--toggle', '-t'):
            pid = get_running_launcher_pid()
            if pid:
                os.kill(pid, signal.SIGUSR1)
                print(f"[VRC Music] Segnale toggle inviato con successo al processo {pid}.")
                sys.exit(0)
            else:
                print("[VRC Music] Nessun launcher attivo trovato da abilitare/disabilitare.")
                sys.exit(1)
        elif arg in ('--status', '-s'):
            running, pid = is_vrchat_running()
            vol, st, pl, ar, ti, pos = get_player_metadata()
            vol_text, _ = format_volume(vol)
            print(f"VRChat: {'Attivo (PID ' + str(pid) + ')' if running else 'Non aperto'}")
            print(f"Musica: {ti} - {ar} [{st}] ({pl})")
            if vol_text:
                print(f"Volume {pl}: {vol_text}")
            if pos is not None:
                print(f"Posizione: {pos:.1f}s")
            sys.exit(0)
        elif arg in ('--help', '-h'):
            print("Uso: music [OPZIONI]")
            print("  (senza opzioni)    Apre il mini-launcher grafico con opzioni Titolo / Lyrics / Entrambi")
            print("  --toggle, -t       Abilita o disabilita al volo il launcher già aperto")
            print("  --status, -s       Mostra lo stato di VRChat, musica, volume e posizione")
            print("  --help, -h         Mostra questo messaggio di aiuto")
            sys.exit(0)

    existing_pid = get_running_launcher_pid()
    if existing_pid:
        print(f"[VRC Music] Il launcher è già in esecuzione (PID {existing_pid}). Invio segnale toggle.")
        os.kill(existing_pid, signal.SIGUSR1)
        sys.exit(0)

    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        atexit.register(cleanup_pid)
    except Exception as e:
        print(f"Attenzione: impossibile creare PID file: {e}", file=sys.stderr)

    app = QApplication(sys.argv)
    app.setApplicationName("VRChat Music OSC Launcher")

    window = MusicLauncherWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()