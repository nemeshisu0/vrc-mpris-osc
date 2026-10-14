#!/usr/bin/env python3
"""
VRChat Music OSC Launcher
=========================
Mini launcher grafico per trasmettere il brano in riproduzione (Spotify, browser, MPRIS),
il livello di volume di Spotify e i testi sincronizzati (Lyrics) nella Chatbox di VRChat via OSC (127.0.0.1:9000).

Funzionalità:
- Sagoma del Minion personalizzata (default: (-⊙_⊙-)) prima del titolo della canzone.
- Download automatico delle lyrics con notifica di conferma ("Le lyrics sono giuste?").
- Inserimento ed editing manuale dei testi tramite copia-incolla con auto-sincronizzazione.
- Supporto testi ibrido:
  1. File locali manuali in ~/.config/vrc-mpris-osc/lyrics/ (priorità massima).
  2. Testi sincronizzati ufficiali con timestamp (LRCLIB).
  3. Auto-timing proporzionale su testi semplici (LRCLIB plainLyrics o Genius).
- Grande pulsante ON / OFF per abilitare/disabilitare l'invio al volo.
- Selezione modalità Chatbox: Solo Titolo, Solo Lyrics, o Entrambi (Titolo sopra, Lyrics sotto).
- Monitoraggio volume Spotify e auto-chiusura all'uscita da VRChat.
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
import html

from PyQt6.QtCore import Qt, QTimer, QSocketNotifier, QSettings
from PyQt6.QtGui import QFont, QColor, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QFrame, QComboBox, QDialog,
    QTextEdit, QMessageBox, QSizePolicy
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else "/home/nemesy/Documents/mucis vr chart"
MINION_ICON_PATH = os.path.join(SCRIPT_DIR, "minion.png")
LYRICS_DIR = os.path.expanduser("~/.config/vrc-mpris-osc/lyrics")
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


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()


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


def auto_time_lyrics(plain_text: str, duration_sec: float = None):
    if not plain_text:
        return []
    raw_lines = [l.strip() for l in plain_text.splitlines() if l.strip()]
    lines = []
    for l in raw_lines:
        if l.startswith('[') and l.endswith(']'):
            continue
        if 'contributors' in l.lower() or ('lyrics' in l.lower() and len(l) < 25):
            continue
        lines.append(l)

    if not lines:
        return []

    n = len(lines)
    if duration_sec and duration_sec > 15:
        start_time = min(10.0, duration_sec * 0.05)
        end_time = max(start_time + 10.0, duration_sec * 0.95)
        span = end_time - start_time
        return [(start_time + (i / n) * span, text) for i, text in enumerate(lines)]
    else:
        return [(i * 3.5, text) for i, text in enumerate(lines)]


def fetch_genius_lyrics(artist: str, title: str):
    query = f"{artist} {title}"
    url = "https://genius.com/api/search/multi?q=" + urllib.parse.quote(query)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'})
        with urllib.request.urlopen(req, timeout=3.5) as r:
            data = json.loads(r.read().decode())
            song_url = None
            for s in data.get('response', {}).get('sections', []):
                if s.get('type') == 'song':
                    hits = s.get('hits', [])
                    if hits:
                        song_url = hits[0].get('result', {}).get('url')
                        break
        if not song_url:
            return None

        req2 = urllib.request.Request(song_url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'})
        with urllib.request.urlopen(req2, timeout=3.5) as r2:
            page_html = r2.read().decode('utf-8', errors='ignore')
            containers = re.findall(r'<div[^>]*data-lyrics-container=\"true\"[^>]*>(.*?)</div>', page_html, re.DOTALL)
            if not containers:
                return None
            text = '\n'.join(containers)
            text = re.sub(r'<br\s*/?>', '\n', text)
            text = re.sub(r'<[^>]+>', '', text)
            return html.unescape(text)
    except Exception:
        return None


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
        cmd = ['playerctl', '-p', 'spotify,%any', 'metadata', '--format', '{{volume}}|{{status}}|{{playerName}}|{{artist}}|{{title}}|{{position}}|{{mpris:length}}']
        res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8').strip()
        if not res:
            return None, None, None, None, None, None, None
        parts = res.split('|', 6)
        if len(parts) == 7:
            pos_sec = None
            if parts[5].strip().isdigit():
                pos_sec = float(parts[5].strip()) / 1_000_000.0
            len_sec = None
            if parts[6].strip().isdigit():
                len_sec = float(parts[6].strip()) / 1_000_000.0
            return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip(), pos_sec, len_sec
    except Exception:
        pass
    return None, None, None, None, None, None, None


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


class ManualLyricsDialog(QDialog):
    """Finestra di dialogo per visualizzare, incollare o modificare le lyrics manualmente."""
    def __init__(self, parent, artist: str, title: str, current_lines: list, duration_sec: float):
        super().__init__(parent)
        self.setWindowTitle(f"Modifica Lyrics: {title}")
        self.resize(500, 520)
        self.artist = artist
        self.title = title
        self.duration_sec = duration_sec
        self.result_lines = None

        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e2e;
                color: #cdd6f4;
            }
            QLabel {
                color: #cdd6f4;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            }
            QTextEdit {
                background-color: #181825;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 8px;
                padding: 10px;
                font-family: monospace;
                font-size: 13px;
            }
            QPushButton {
                border-radius: 8px;
                padding: 8px 14px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton#saveBtn {
                background-color: #a6e3a1;
                color: #11111b;
            }
            QPushButton#saveBtn:hover {
                background-color: #94e2d5;
            }
            QPushButton#cancelBtn {
                background-color: #313244;
                color: #bac2de;
                border: 1px solid #45475a;
            }
            QPushButton#deleteBtn {
                background-color: #313244;
                color: #f38ba8;
                border: 1px solid #f38ba8;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        header = QLabel(f"✏️ Incolla o Modifica Testo per: {title}")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #f9e2af;")
        layout.addWidget(header)

        info = QLabel(
            "Puoi incollare il testo completo copiato da internet (Google, Spotify, ecc.).\n"
            "Se il testo non ha timestamp [00:12], verrà sincronizzato automaticamente a tempo con la musica!"
        )
        info.setStyleSheet("font-size: 11px; color: #a6adc8;")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Incolla qui il testo della canzone...")

        # Prepopola con le lyrics esistenti se disponibili
        if current_lines:
            text_lines = []
            for ts, l in current_lines:
                mins = int(ts // 60)
                secs = ts % 60
                text_lines.append(f"[{mins:02d}:{secs:05.2f}] {l}")
            self.text_edit.setPlainText("\n".join(text_lines))

        layout.addWidget(self.text_edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.delete_btn = QPushButton("🗑️ Rimuovi Manuale")
        self.delete_btn.setObjectName("deleteBtn")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.clicked.connect(self.on_delete)
        btn_row.addWidget(self.delete_btn)

        btn_row.addStretch()

        self.cancel_btn = QPushButton("Annulla")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("💾 Salva e Applica")
        self.save_btn.setObjectName("saveBtn")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(self.on_save)
        btn_row.addWidget(self.save_btn)

        layout.addLayout(btn_row)

    def on_save(self):
        content = self.text_edit.toPlainText().strip()
        if not content:
            QMessageBox.warning(self, "Attenzione", "Il testo è vuoto!")
            return

        # Verifica se ha formato LRC o è testo semplice
        if re.search(r'\[\d+:\d+(?:\.\d+)?\]', content):
            parsed = parse_lrc(content)
        else:
            parsed = auto_time_lyrics(content, self.duration_sec)

        if not parsed:
            QMessageBox.warning(self, "Errore", "Impossibile elaborare il testo inserito.")
            return

        # Salva su file locale
        os.makedirs(LYRICS_DIR, exist_ok=True)
        filename = f"{sanitize_filename(self.artist)} - {sanitize_filename(self.title)}.lrc"
        file_path = os.path.join(LYRICS_DIR, filename)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                for ts, l in parsed:
                    mins = int(ts // 60)
                    secs = ts % 60
                    f.write(f"[{mins:02d}:{secs:05.2f}] {l}\n")
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Impossibile salvare il file: {e}")
            return

        self.result_lines = parsed
        self.accept()

    def on_delete(self):
        filename = f"{sanitize_filename(self.artist)} - {sanitize_filename(self.title)}.lrc"
        file_path = os.path.join(LYRICS_DIR, filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        self.result_lines = "DELETED"
        self.accept()


class MusicLauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VRChat Music OSC Launcher (Minion Edition)")
        self.setFixedSize(430, 680)

        # Imposta icona grafica Minion per la finestra
        if os.path.exists(MINION_ICON_PATH):
            self.setWindowIcon(QIcon(MINION_ICON_PATH))

        # Gestione preferenze persistenti
        self.settings = QSettings("vrc-mpris-osc", "MusicLauncher")

        # Canzoni per cui l'utente ha già confermato che le lyrics sono giuste
        confirmed_raw = self.settings.value("confirmed_songs", [])
        self.confirmed_songs = set(confirmed_raw) if isinstance(confirmed_raw, list) else set()

        self.is_on = True
        self.vrchat_detected_once = False
        self.last_sent_text = None
        self.last_sent_time = 0.0
        self.cooldown = 4.0
        self.last_detected_volume = None

        # Info brano corrente
        self.current_artist = ""
        self.current_title = ""
        self.current_len_sec = None

        # Gestione Lyrics
        self.lyrics_cache = {}  # (artist, title): (lines, source_type)
        self.current_lyrics_key = None
        self.current_lyrics_lines = []
        self.current_lyrics_source = "NONE"
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
                padding: 5px 10px;
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
            QPushButton#editLyricsBtn {
                background-color: #313244;
                color: #f9e2af;
                border: 1px solid #45475a;
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton#editLyricsBtn:hover {
                background-color: #45475a;
                color: #ffffff;
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
            QFrame.notifCard {
                background-color: #2a283e;
                border: 1px solid #f9e2af;
                border-radius: 8px;
                padding: 8px 10px;
            }
        """)

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(18, 12, 18, 12)
        main_layout.setSpacing(8)

        # 1. Header con Minion Icon
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        if os.path.exists(MINION_ICON_PATH):
            minion_img = QLabel()
            pix = QIcon(MINION_ICON_PATH).pixmap(36, 36)
            minion_img.setPixmap(pix)
            header_layout.addWidget(minion_img)

        title_vbox = QVBoxLayout()
        title_vbox.setSpacing(1)
        title_label = QLabel("(-⊙_⊙-) VRChat Music OSC")
        title_label.setStyleSheet("font-size: 17px; font-weight: bold; color: #f9e2af;")
        subtitle_label = QLabel("Musica, Volume e Testi nella Chatbox di VRChat")
        subtitle_label.setStyleSheet("font-size: 11px; color: #a6adc8;")
        title_vbox.addWidget(title_label)
        title_vbox.addWidget(subtitle_label)
        header_layout.addLayout(title_vbox)

        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        # 2. VRChat Status Card
        vrchat_card = QFrame()
        vrchat_card.setProperty("class", "card")
        vrchat_card_layout = QHBoxLayout(vrchat_card)
        vrchat_card_layout.setContentsMargins(12, 6, 12, 6)

        self.vrchat_status_label = QLabel("○ Verifica VRChat in corso...")
        self.vrchat_status_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #9399b2;")
        vrchat_card_layout.addWidget(self.vrchat_status_label)
        main_layout.addWidget(vrchat_card)

        # 3. Grande Pulsante ON / OFF
        self.toggle_btn = QPushButton()
        self.toggle_btn.setFixedHeight(64)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(self.toggle_state)
        main_layout.addWidget(self.toggle_btn)

        # 4. Scheda Brano Corrente (Now Playing + Volume + Lyrics Preview)
        song_card = QFrame()
        song_card.setProperty("class", "card")
        song_card_layout = QVBoxLayout(song_card)
        song_card_layout.setContentsMargins(12, 8, 12, 8)
        song_card_layout.setSpacing(5)

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

        # Box per Anteprima Testo (Lyrics) con tasto per modificare/incollare
        lyrics_box = QFrame()
        lyrics_box.setProperty("class", "lyricsBox")
        lyrics_box_layout = QVBoxLayout(lyrics_box)
        lyrics_box_layout.setContentsMargins(8, 5, 8, 5)
        lyrics_box_layout.setSpacing(4)

        lyrics_top_row = QHBoxLayout()
        lyrics_tag_label = QLabel("TESTO SINCRONIZZATO:")
        lyrics_tag_label.setStyleSheet("font-size: 9px; font-weight: bold; color: #6c7086;")
        lyrics_top_row.addWidget(lyrics_tag_label)

        lyrics_top_row.addStretch()

        self.edit_lyrics_btn = QPushButton("✏️ Modifica / Incolla")
        self.edit_lyrics_btn.setObjectName("editLyricsBtn")
        self.edit_lyrics_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_lyrics_btn.clicked.connect(self.open_lyrics_editor)
        lyrics_top_row.addWidget(self.edit_lyrics_btn)

        lyrics_box_layout.addLayout(lyrics_top_row)

        self.lyrics_preview_label = QLabel("🎤 Lyrics: In attesa di riproduzione...")
        self.lyrics_preview_label.setStyleSheet("font-size: 11px; color: #f5c2e7; font-style: italic;")
        self.lyrics_preview_label.setWordWrap(True)
        lyrics_box_layout.addWidget(self.lyrics_preview_label)

        song_card_layout.addWidget(lyrics_box)

        # Banner Notifica per confermare o correggere le Lyrics
        self.notif_frame = QFrame()
        self.notif_frame.setProperty("class", "notifCard")
        self.notif_frame.setVisible(False)
        notif_layout = QVBoxLayout(self.notif_frame)
        notif_layout.setContentsMargins(8, 6, 8, 6)
        notif_layout.setSpacing(4)

        self.notif_label = QLabel("🔔 Testo trovato per la prima volta. Le lyrics sono giuste?")
        self.notif_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #f9e2af;")
        self.notif_label.setWordWrap(True)
        notif_layout.addWidget(self.notif_label)

        notif_btn_row = QHBoxLayout()
        notif_btn_row.setSpacing(8)

        self.confirm_btn = QPushButton("✅ Sì, sono giuste")
        self.confirm_btn.setStyleSheet("background-color: #a6e3a1; color: #11111b; font-size: 11px; font-weight: bold; border-radius: 5px; padding: 4px 8px;")
        self.confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.confirm_btn.clicked.connect(self.confirm_current_lyrics)
        notif_btn_row.addWidget(self.confirm_btn)

        self.reject_btn = QPushButton("✏️ No / Incolla manualmente")
        self.reject_btn.setStyleSheet("background-color: #45475a; color: #cdd6f4; font-size: 11px; border-radius: 5px; padding: 4px 8px;")
        self.reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reject_btn.clicked.connect(self.open_lyrics_editor)
        notif_btn_row.addWidget(self.reject_btn)

        notif_btn_row.addStretch()
        notif_layout.addLayout(notif_btn_row)

        song_card_layout.addWidget(self.notif_frame)

        self.osc_status_label = QLabel("Stato Chatbox: In attesa")
        self.osc_status_label.setStyleSheet("font-size: 11px; color: #a6adc8; font-style: italic;")
        song_card_layout.addWidget(self.osc_status_label)

        main_layout.addWidget(song_card)

        # 5. Selezione Modalità Chatbox e Sagoma Minion
        selectors_row = QHBoxLayout()
        selectors_row.setSpacing(8)

        # Selettore Sagoma Minion (modificabile e personalizzabile, default (-⊙_⊙-))
        icon_box = QVBoxLayout()
        icon_box.setSpacing(3)
        icon_title = QLabel("SAGOMA MINION")
        icon_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #6c7086; letter-spacing: 1px;")
        icon_box.addWidget(icon_title)

        self.icon_combo = QComboBox()
        self.icon_combo.setEditable(True)
        self.icon_combo.addItem("(-⊙_⊙-)")
        self.icon_combo.addItem("(-⊙‿⊙-)")
        self.icon_combo.addItem("(-⊙-)")
        self.icon_combo.addItem("c(⊙_⊙)ɔ")
        self.icon_combo.addItem("[⊙_⊙]")
        self.icon_combo.addItem("🥽")
        self.icon_combo.addItem("🍌")
        self.icon_combo.addItem("🎵")

        saved_icon_text = self.settings.value("custom_minion_text", "(-⊙_⊙-)", type=str)
        if saved_icon_text:
            self.icon_combo.setEditText(saved_icon_text)
        self.icon_combo.currentTextChanged.connect(self.on_icon_text_changed)

        icon_box.addWidget(self.icon_combo)
        selectors_row.addLayout(icon_box, stretch=1)

        # Selettore Modalità
        mode_box = QVBoxLayout()
        mode_box.setSpacing(3)
        mode_title = QLabel("MODALITÀ CHATBOX")
        mode_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #6c7086; letter-spacing: 1px;")
        mode_box.addWidget(mode_title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Titolo + Volume", MODE_TITLE_ONLY)
        self.mode_combo.addItem("🎤 Solo Lyrics", MODE_LYRICS_ONLY)
        self.mode_combo.addItem("Entrambi (Titolo + Lyrics)", MODE_BOTH)

        saved_mode = self.settings.value("chatbox_mode", MODE_TITLE_ONLY, type=int)
        if 0 <= saved_mode <= 2:
            self.mode_combo.setCurrentIndex(saved_mode)
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        mode_box.addWidget(self.mode_combo)
        selectors_row.addLayout(mode_box, stretch=2)

        main_layout.addLayout(selectors_row)

        # 6. Opzioni Checkbox
        options_layout = QVBoxLayout()
        options_layout.setSpacing(4)

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

    def get_current_minion_prefix(self) -> str:
        text = self.icon_combo.currentText().strip()
        return text if text else "(-⊙_⊙-)"

    def on_icon_text_changed(self, text):
        self.settings.setValue("custom_minion_text", text)
        self.last_sent_text = None
        self.on_tick()

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

    def confirm_current_lyrics(self):
        """L'utente conferma che le lyrics scaricate sono giuste."""
        if self.current_lyrics_key:
            self.confirmed_songs.add(self.current_lyrics_key)
            self.settings.setValue("confirmed_songs", list(self.confirmed_songs))
        self.notif_frame.setVisible(False)

    def open_lyrics_editor(self):
        """Apre la finestra per incollare o modificare le lyrics."""
        dlg = ManualLyricsDialog(
            self,
            self.current_artist,
            self.current_title,
            self.current_lyrics_lines,
            self.current_len_sec or 180.0
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            key = (self.current_artist.lower().strip(), self.current_title.lower().strip())
            if dlg.result_lines == "DELETED":
                if key in self.lyrics_cache:
                    del self.lyrics_cache[key]
                self.current_lyrics_key = None
                self.notif_frame.setVisible(False)
                self.on_tick()
            elif dlg.result_lines:
                self.lyrics_cache[key] = (dlg.result_lines, "MANUALE")
                self.current_lyrics_lines = dlg.result_lines
                self.current_lyrics_source = "MANUALE"
                self.confirmed_songs.add(key)
                self.settings.setValue("confirmed_songs", list(self.confirmed_songs))
                self.notif_frame.setVisible(False)
                self.last_sent_text = None
                self.on_tick()

    def check_local_lyrics(self, artist: str, title: str, duration_sec: float):
        """Verifica se esiste un file .lrc o .txt personalizzato salvato nella cartella locale."""
        if not os.path.exists(LYRICS_DIR):
            return None
        candidates = [
            f"{sanitize_filename(artist)} - {sanitize_filename(title)}.lrc",
            f"{sanitize_filename(title)} - {sanitize_filename(artist)}.lrc",
            f"{sanitize_filename(title)}.lrc",
            f"{sanitize_filename(artist)} - {sanitize_filename(title)}.txt",
            f"{sanitize_filename(title)}.txt",
        ]
        for filename in candidates:
            path = os.path.join(LYRICS_DIR, filename)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if re.search(r'\[\d+:\d+(?:\.\d+)?\]', content):
                        parsed = parse_lrc(content)
                    else:
                        parsed = auto_time_lyrics(content, duration_sec)
                    if parsed:
                        return parsed
                except Exception:
                    pass
        return None

    def fetch_lyrics_hybrid_async(self, artist: str, title: str, duration_sec: float = None):
        key = (artist.lower().strip(), title.lower().strip())

        # 1. Controlla prima la cache in memoria
        if key in self.lyrics_cache:
            self.current_lyrics_key = key
            self.current_lyrics_lines, self.current_lyrics_source = self.lyrics_cache[key]
            self.update_notification_banner(key)
            return

        # 2. Controlla se l'utente ha salvato un file manuale locale
        local_lines = self.check_local_lyrics(artist, title, duration_sec)
        if local_lines:
            self.lyrics_cache[key] = (local_lines, "MANUALE")
            self.current_lyrics_key = key
            self.current_lyrics_lines = local_lines
            self.current_lyrics_source = "MANUALE"
            self.notif_frame.setVisible(False)
            return

        if self.lyrics_fetching:
            return

        self.current_lyrics_key = key
        self.current_lyrics_lines = []
        self.current_lyrics_source = "SEARCHING"
        self.lyrics_fetching = True
        self.notif_frame.setVisible(False)

        def worker():
            synced_lrc = None
            plain_lrc = None

            # LRCLIB match esatto per syncedLyrics
            try:
                params = urllib.parse.urlencode({'track_name': title, 'artist_name': artist})
                url = f"https://lrclib.net/api/get?{params}"
                req = urllib.request.Request(url, headers={'User-Agent': 'vrc-mpris-osc/0.1.0'})
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get('syncedLyrics'):
                        synced_lrc = data['syncedLyrics']
                    elif data.get('plainLyrics'):
                        plain_lrc = data['plainLyrics']
            except Exception:
                pass

            # LRCLIB ricerca libera
            if not synced_lrc:
                try:
                    q = urllib.parse.urlencode({'q': f"{artist} {title}"})
                    url = f"https://lrclib.net/api/search?{q}"
                    req = urllib.request.Request(url, headers={'User-Agent': 'vrc-mpris-osc/0.1.0'})
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        results = json.loads(resp.read().decode())
                        if isinstance(results, list):
                            for item in results:
                                if item.get('syncedLyrics'):
                                    synced_lrc = item['syncedLyrics']
                                    break
                            if not synced_lrc and not plain_lrc:
                                for item in results:
                                    if item.get('plainLyrics'):
                                        plain_lrc = item['plainLyrics']
                                        break
                except Exception:
                    pass

            if synced_lrc:
                parsed = parse_lrc(synced_lrc)
                if parsed:
                    self.lyrics_cache[key] = (parsed, "SYNCED")
                    if self.current_lyrics_key == key:
                        self.current_lyrics_lines = parsed
                        self.current_lyrics_source = "SYNCED"
                    self.lyrics_fetching = False
                    return

            # Fallback plainLyrics da LRCLIB auto-sincronizzato con la durata
            if plain_lrc:
                timed = auto_time_lyrics(plain_lrc, duration_sec)
                if timed:
                    self.lyrics_cache[key] = (timed, "AUTO_TIMED")
                    if self.current_lyrics_key == key:
                        self.current_lyrics_lines = timed
                        self.current_lyrics_source = "AUTO_TIMED"
                    self.lyrics_fetching = False
                    return

            # Fallback Genius per brani italiani o poco noti
            genius_text = fetch_genius_lyrics(artist, title)
            if genius_text:
                timed = auto_time_lyrics(genius_text, duration_sec)
                if timed:
                    self.lyrics_cache[key] = (timed, "GENIUS_TIMED")
                    if self.current_lyrics_key == key:
                        self.current_lyrics_lines = timed
                        self.current_lyrics_source = "GENIUS_TIMED"
                    self.lyrics_fetching = False
                    return

            self.lyrics_cache[key] = ([], "NOT_FOUND")
            if self.current_lyrics_key == key:
                self.current_lyrics_lines = []
                self.current_lyrics_source = "NOT_FOUND"
            self.lyrics_fetching = False

        threading.Thread(target=worker, daemon=True).start()

    def update_notification_banner(self, key):
        """Mostra la notifica se è la prima volta che si scaricano le lyrics per questo brano."""
        if not self.current_title:
            self.notif_frame.setVisible(False)
            return

        if key in self.confirmed_songs or self.current_lyrics_source in ("MANUALE", "NONE", "SEARCHING"):
            self.notif_frame.setVisible(False)
            return

        if self.current_lyrics_lines:
            self.notif_label.setText(f"🔔 Testo trovato per '{self.current_title}'. Le lyrics sono giuste?")
            self.confirm_btn.setVisible(True)
            self.reject_btn.setText("✏️ No / Incolla manualmente")
            self.notif_frame.setVisible(True)
        elif self.current_lyrics_source == "NOT_FOUND":
            self.notif_label.setText(f"⚠️ Nessun testo trovato per '{self.current_title}'.")
            self.confirm_btn.setVisible(False)
            self.reject_btn.setText("📋 Incolla Testo Manuale")
            self.notif_frame.setVisible(True)
        else:
            self.notif_frame.setVisible(False)

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

        if vrchat_running:
            self.vrchat_detected_once = True
            self.vrchat_status_label.setText(f"● VRChat in esecuzione (PID {vrc_pid})")
            self.vrchat_status_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #a6e3a1;")
        else:
            self.vrchat_status_label.setText("○ In attesa dell'avvio di VRChat...")
            self.vrchat_status_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #9399b2;")

            if self.vrchat_detected_once and self.auto_close_chk.isChecked():
                self.send_chatbox_clear()
                self.vrchat_status_label.setText("● VRChat chiuso. Chiusura launcher...")
                self.timer.stop()
                QTimer.singleShot(1000, self.close)
                return

        vol_str, status, player, artist, title, pos_sec, len_sec = get_player_metadata()
        vol_text, vol_pct = format_volume(vol_str)

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

            self.current_artist = clean_a
            self.current_title = clean_t
            self.current_len_sec = len_sec

            icon = "▶" if status.lower() == "playing" else "⏸"
            self.track_label.setText(f"{icon} {display_title}")
            self.player_label.setText(f"Player: {player.capitalize()} ({status.capitalize()})")

            key = (clean_a.lower().strip(), clean_t.lower().strip())
            if self.current_lyrics_key != key:
                self.fetch_lyrics_hybrid_async(clean_a, clean_t, len_sec)

            current_lyric_line = get_current_lyric_line(self.current_lyrics_lines, pos_sec)

            # Aggiorna il banner di notifica se il download è terminato
            if not self.lyrics_fetching:
                self.update_notification_banner(key)

            src_tag = ""
            if self.current_lyrics_source == "MANUALE":
                src_tag = " [Manuale]"
            elif self.current_lyrics_source == "SYNCED":
                src_tag = " [Originale]"
            elif self.current_lyrics_source in ("AUTO_TIMED", "GENIUS_TIMED"):
                src_tag = " [Auto-Sync]"

            if current_lyric_line:
                self.lyrics_preview_label.setText(f'🎤{src_tag} "{current_lyric_line}"')
            elif self.current_lyrics_lines:
                self.lyrics_preview_label.setText(f"🎤{src_tag} [Pausa strumentale / Intro]")
            elif self.lyrics_fetching:
                self.lyrics_preview_label.setText("🎤 Ricerca testo (LRCLIB & Genius)...")
            else:
                self.lyrics_preview_label.setText("🎤 Nessun testo disponibile per questo brano")
        else:
            self.track_label.setText("Nessun brano attivo")
            self.player_label.setText("Player: Non rilevato")
            self.lyrics_preview_label.setText("🎤 Lyrics: In attesa di riproduzione...")
            self.notif_frame.setVisible(False)

        if not self.is_on:
            return

        if not vrchat_running:
            self.osc_status_label.setText("Stato Chatbox: In attesa di VRChat...")
            self.last_sent_text = None
            return

        if status and status.lower() == 'playing' and title:
            clean_t = clean_text(title)
            clean_a = clean_text(artist) if artist else ""
            song_part = f"{clean_t} - {clean_a}" if clean_a else clean_t

            # Sagoma Minion personalizzata (default: (-⊙_⊙-))
            minion_prefix = self.get_current_minion_prefix()

            if self.show_volume_chk.isChecked() and vol_text:
                title_msg = f"{minion_prefix} {song_part} [{vol_text}]"
            else:
                title_msg = f"{minion_prefix} {song_part}"

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

            if text_changed or cooldown_passed:
                if text_changed and (now - self.last_sent_time < 1.4):
                    return

                self.send_chatbox(osc_msg)
                self.last_sent_text = osc_msg
                self.last_sent_time = now

                display_status = osc_msg.replace('\n', ' ⮑  ')
                self.osc_status_label.setText(f"Inviato: {display_status}")
        else:
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
            vol, st, pl, ar, ti, pos, length = get_player_metadata()
            vol_text, _ = format_volume(vol)
            print(f"VRChat: {'Attivo (PID ' + str(pid) + ')' if running else 'Non aperto'}")
            print(f"Musica: {ti} - {ar} [{st}] ({pl})")
            if vol_text:
                print(f"Volume {pl}: {vol_text}")
            if pos is not None:
                print(f"Posizione: {pos:.1f}s / {length:.1f}s" if length else f"Posizione: {pos:.1f}s")
            sys.exit(0)
        elif arg in ('--help', '-h'):
            print("Uso: music [OPZIONI]")
            print("  (senza opzioni)    Apre il mini-launcher grafico con sagoma Minion, volume e lyrics")
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
    if os.path.exists(MINION_ICON_PATH):
        app.setWindowIcon(QIcon(MINION_ICON_PATH))

    window = MusicLauncherWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()