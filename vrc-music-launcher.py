#!/usr/bin/env python3
"""
VRChat Music OSC Launcher
=========================
Mini launcher grafico per trasmettere il brano in riproduzione (Spotify, browser, MPRIS)
e il livello di volume di Spotify nella Chatbox di VRChat via OSC (127.0.0.1:9000).

Funzionalità:
- Grande pulsante ON / OFF per abilitare/disabilitare l'invio al volo.
- Monitoraggio del livello di volume interno di Spotify in tempo reale.
- Aggiornamento immediato della Chatbox e dell'interfaccia quando modifichi il volume.
- Si attiva solo quando VRChat è in esecuzione.
- Chiusura automatica pulita quando esci da VRChat (opzionale, attiva di default).
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

from PyQt6.QtCore import Qt, QTimer, QSocketNotifier
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QFrame, QSizePolicy
)

PID_FILE = "/tmp/vrc_music_launcher.pid"
OSC_IP = "127.0.0.1"
OSC_PORT = 9000
OSC_ADDR = "/chatbox/input"

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
        cmd = ['playerctl', '-p', 'spotify,%any', 'metadata', '--format', '{{volume}}|{{status}}|{{playerName}}|{{artist}}|{{title}}']
        res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8').strip()
        if not res:
            return None, None, None, None, None
        parts = res.split('|', 4)
        if len(parts) == 5:
            return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip()
    except Exception:
        pass
    return None, None, None, None, None


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
        self.setFixedSize(410, 540)

        self.is_on = True
        self.vrchat_detected_once = False
        self.last_sent_text = None
        self.last_sent_time = 0.0
        self.cooldown = 4.0
        self.last_detected_volume = None

        # Socket UDP per invio OSC
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Gestione segnali (es. SIGUSR1 per toggle remoto da tastiera)
        self.setup_signal_handling()

        self.setup_ui()
        self.update_toggle_button_ui()

        # Timer principale (controlla VRChat, musica e volume ogni secondo)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(1000)

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
        """)

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(12)

        # 1. Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)
        title_label = QLabel("🎵 VRChat Music OSC")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #cdd6f4;")
        subtitle_label = QLabel("Condividi musica e volume nella Chatbox di VRChat")
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
        self.toggle_btn.setFixedHeight(75)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(self.toggle_state)
        main_layout.addWidget(self.toggle_btn)

        # 4. Scheda Brano Corrente (Now Playing + Volume)
        song_card = QFrame()
        song_card.setProperty("class", "card")
        song_card_layout = QVBoxLayout(song_card)
        song_card_layout.setContentsMargins(12, 12, 12, 12)
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

        self.osc_status_label = QLabel("Stato Chatbox: In attesa")
        self.osc_status_label.setStyleSheet("font-size: 11px; color: #a6adc8; font-style: italic;")
        song_card_layout.addWidget(self.osc_status_label)

        main_layout.addWidget(song_card)

        # 5. Opzioni
        options_layout = QVBoxLayout()
        options_layout.setSpacing(7)

        self.show_volume_chk = QCheckBox("Mostra livello volume nella Chatbox (es. 🔉 46%)")
        self.show_volume_chk.setChecked(True)
        self.show_volume_chk.toggled.connect(self.on_option_changed)
        options_layout.addWidget(self.show_volume_chk)

        self.auto_close_chk = QCheckBox("Chiudi launcher automaticamente all'uscita di VRChat")
        self.auto_close_chk.setChecked(True)
        options_layout.addWidget(self.auto_close_chk)

        self.clear_on_pause_chk = QCheckBox("Pulisci chatbox quando metti in pausa o premi OFF")
        self.clear_on_pause_chk.setChecked(True)
        options_layout.addWidget(self.clear_on_pause_chk)

        main_layout.addLayout(options_layout)

        # 6. Barra inferiore (Info porta + Esci)
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 4, 0, 0)

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

    def on_option_changed(self):
        # Forza invio immediato al cambio impostazione se attivo
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

        # Leggi stato da playerctl (volume, status, player, artist, title)
        vol_str, status, player, artist, title = get_player_metadata()
        vol_text, vol_pct = format_volume(vol_str)

        # Aggiorna badge volume
        if vol_text:
            self.volume_badge.setText(vol_text)
            self.volume_badge.setVisible(True)
        else:
            self.volume_badge.setVisible(False)

        # Rileva variazione di volume
        volume_changed = False
        if vol_pct is not None and self.last_detected_volume != vol_pct:
            volume_changed = True
            self.last_detected_volume = vol_pct

        if status:
            clean_t = clean_text(title)
            clean_a = clean_text(artist) if artist else ""
            display_title = f"{clean_t} - {clean_a}" if clean_a else clean_t

            icon = "▶" if status.lower() == "playing" else "⏸"
            self.track_label.setText(f"{icon} {display_title}")
            self.player_label.setText(f"Player: {player.capitalize()} ({status.capitalize()})")
        else:
            self.track_label.setText("Nessun brano attivo")
            self.player_label.setText("Player: Non rilevato")

        # Se il pulsante è su OFF
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

            # Aggiungi indicazione volume se abilitata
            if self.show_volume_chk.isChecked() and vol_text:
                osc_msg = f"🎵 {song_part} [{vol_text}]"
            else:
                osc_msg = f"🎵 {song_part}"

            if len(osc_msg) > 140:
                osc_msg = osc_msg[:137] + "..."

            now = time.time()
            text_changed = (osc_msg != self.last_sent_text)
            cooldown_passed = (now - self.last_sent_time >= self.cooldown)

            # Invia se il testo è cambiato (nuovo brano o cambio volume dopo almeno 1.2s) o per cooldown periodico
            if text_changed or cooldown_passed:
                # Protezione anti-spam se il volume cambia molto in fretta (slider mosso a mano)
                if not text_changed and not cooldown_passed:
                    return
                if text_changed and (now - self.last_sent_time < 1.2):
                    return

                self.send_chatbox(osc_msg)
                self.last_sent_text = osc_msg
                self.last_sent_time = now
                self.osc_status_label.setText(f"Inviato: {osc_msg}")
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
            vol, st, pl, ar, ti = get_player_metadata()
            vol_text, _ = format_volume(vol)
            print(f"VRChat: {'Attivo (PID ' + str(pid) + ')' if running else 'Non aperto'}")
            print(f"Musica: {ti} - {ar} [{st}] ({pl})")
            if vol_text:
                print(f"Volume {pl}: {vol_text}")
            sys.exit(0)
        elif arg in ('--help', '-h'):
            print("Uso: music [OPZIONI]")
            print("  (senza opzioni)    Apre il mini-launcher grafico con pulsante ON/OFF e volume Spotify")
            print("  --toggle, -t       Abilita o disabilita al volo il launcher già aperto")
            print("  --status, -s       Mostra lo stato di VRChat, della musica e del volume")
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