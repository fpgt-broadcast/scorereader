#!/usr/bin/env python3
"""
ScoreReader - open-source replacement for Sportzcast ScoreConnect III
=====================================================================

Reads live scoreboard data straight from a Sportzcast ScoreLinkII / ScoreBot over
USB (Microchip MCP2221, VID 04D8 PID 00DD) - or any USB-serial adapter wired to the
console - decodes Daktronics All Sport 5000 data, and serves it to graphics:

  * Web dashboard ........ http://localhost:8765/
  * vMix Data Source ..... http://localhost:8765/data.json   (also /data.xml, /data.csv)
  * vMix TCP API push .... SetText straight into a title input (optional)
  * MQTT ................. ScoreConnect-compatible broker, bot/{N}/json|sbdata|raw
  * Plain text files ..... one .txt per field (optional)

Protocols (auto-detected):
  * Daktronics current loop (scoreboard driver output, 55 AA frames, 7-segment data)
    - football (console code 6601) and soccer (7601)
  * Daktronics RTD (J6 serial port, SYN/SOH/STX/ETB text packets) - all sports

See docs/PROTOCOL.md for the wire formats and README.md for usage.
Run  `python scorereader.py --help`  for options.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as _dt
import io
import json
import os
import queue
import random
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape as xml_escape

__version__ = "2.1.2"

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)  # PyInstaller support
# A Mac app (ScoreReader.app) can't write inside itself (macOS may even run it from a read-only copy), so its
# settings, logs and text files live in ~/Library/Application Support/ScoreReader. Everything else keeps them
# next to the program, as before.
MAC_APP = sys.platform == "darwin" and getattr(sys, "frozen", False) and ".app/Contents/" in APP_DIR
DATA_DIR = (os.path.join(os.path.expanduser("~"), "Library", "Application Support", "ScoreReader")
            if MAC_APP else APP_DIR)
ASSETS_DIR = os.path.join(RES_DIR, "assets")  # icon.svg / icon.png / menubar.png
CONFIG_PATH = os.path.join(DATA_DIR, "scorereader_config.json")
FIELDS_PATH = os.path.join(RES_DIR, "daktronics_fields.json")
STATE_PATH = os.path.join(DATA_DIR, "scorereader_state.bin")
LOG_DIR = os.path.join(DATA_DIR, "logs")

SCORELINK_VID, SCORELINK_PID = 0x04D8, 0x00DD

DEFAULT_CONFIG = {
    "source": "scorelink",          # scorelink | serial | tcp | demo
    "port": "auto",                 # auto | COM5 | /dev/cu.usbmodem1101 ...
    "baud": 19200,                  # scoreboard data baud (Daktronics All Sport 5000 = 19200)
    "sport": "football",
    # Scoreboard brand + sport, as a ScoreConnect vendor-sport id (see scoreboards/catalog.json), e.g.
    # "PIC55AA_Nevco_Basketball". Blank = the built-in Daktronics All Sport 5000 decoders (current loop + RTD),
    # which pick football/soccer from the console automatically. "connection" picks the wiring option.
    "scoreboard": {"protocol": "", "connection": 0},
    "scorelink": {
        "use_gpio_mode_switch": True,   # drive MCP2221 GP2 (needs `hidapi`)
        "probe_on_start": True,         # ask the PIC its version/config at startup (log only)
        "send_config_on_start": False,  # re-send config_commands every start
        "command_baud": 57600,
        # exactly what ScoreConnect III sent for Daktronics football (current loop, inverted):
        "config_commands": [">M0X0X", ">T0", ">D0", ">M8I1N"],
        "configured_for": "",           # brand/wiring the bot was last configured for (set automatically)
    },
    "tcp": {"host": "", "port": 0},     # for serial-to-ethernet converters
    "http_port": 8765,
    "vmix": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8099,
        "input": "Scorebug",
        # field -> title element. String = SetText. Object forms:
        #   {"name": "HomePoss.Source", "visible": true}       -> SetImageVisibleOn/Off
        #   {"name": "HomePoss.Text", "true": "<", "false": ""} -> SetText with mapped text
        "fields": {
            "clock": "Clock.Text",
            "play_clock": "PlayClock.Text",
            "home_score": "HomeScore.Text",
            "guest_score": "GuestScore.Text",
            "period_ordinal": "Quarter.Text",
            "down_distance": "DownDistance.Text",
            "ball_on": "BallOn.Text",
            "home_timeouts": "HomeTOL.Text",
            "guest_timeouts": "GuestTOL.Text",
            "home_poss": {"name": "HomePoss.Text", "true": "◀", "false": ""},
            "guest_poss": {"name": "GuestPoss.Text", "true": "▶", "false": ""},
        },
    },
    "mqtt": {
        # Sportzcast/ScoreConnect III-compatible MQTT (for OSE etc.)
        "enabled": True,
        "mode": "broker",            # broker = host our own (like ScoreConnect), client = publish to broker_host
        "bot_number": 0,             # topics bot/{bot_number}/json|sbdata|raw  - match what OSE expects
        "tcp_port": 1883,
        "ws_port": 8083,
        "broker_host": "127.0.0.1",
        "broker_port": 1883,
        "publish_rate": 10,          # max messages/sec on json & sbdata
        "heartbeat_seconds": 1.0,    # re-publish even when nothing changed
        "publish_raw": True,
    },
    # Team info added to every output (data.json, vMix, MQTT). A blank name falls back to the name the
    # console sends (RTD only), then to the legacy mqtt.home_team_name / guest_team_name, then HOME / GUEST.
    "teams": {"home_name": "", "guest_name": "", "home_abbr": "", "guest_abbr": "",
              "home_color": "", "guest_color": ""},
    "text_files": {"enabled": False, "folder": "text_out"},
    # POST the data (same fields as data.obj.json) to a URL whenever it changes, at most every min_interval_ms.
    "webhook": {"enabled": False, "url": "", "min_interval_ms": 500},
    # False = show the first-run setup on the dashboard. Configs saved by older versions count as set up.
    "setup_done": False,
    "record_raw": True,
    "save_state": False,             # restore the last RTD buffer after a restart (scorereader_state.bin)
    # data.json/xml/csv and text files carry only the fields used in the sport; True adds every raw field
    # (Daktronics RTD manual fields, Sportzcast contract fields). The dashboard always shows everything.
    "output_all_fields": False,
    "strict_daktronics_header": True,
}

BUILTIN_BOT_KEY = "daktronics-builtin"  # scorelink.configured_for after the bot is set up for the built-in decoders

# Sources that are never written to scorereader_config.json, so the next normal start is always live data.
RUNTIME_ONLY_SOURCES = ("demo", "replay")


# --------------------------------------------------------------------------- utils
_PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    line = f"{_dt.datetime.now():%H:%M:%S} {msg}"
    with _PRINT_LOCK:
        try:
            print(line, flush=True)
        except (OSError, ValueError, AttributeError):
            pass  # the console window was closed (Mac menu bar keeps running): the line still reaches the dashboard
    STATUS.add_log(line)


def detach_console(path: str) -> None:
    """Send further console output to a log file, so the program keeps running after its window is closed."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        f = open(os.devnull, "w")
    with _PRINT_LOCK:
        sys.stdout = sys.stderr = f


def set_window_title(title: str) -> None:
    """Name the console window/tab, so the taskbar (Windows) or Terminal (Mac/Linux) shows "ScoreReader"."""
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        elif sys.stdout is not None and sys.stdout.isatty():
            sys.stdout.write(f"\033]0;{title}\007")
            sys.stdout.flush()
    except Exception:
        pass


def deep_merge(base: dict, over: dict) -> dict:
    """Recursive merge that returns a fully independent copy (changing it never touches base/over)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "fields":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        stored = json.load(f)
    if isinstance(stored, dict) and "setup_done" not in stored:
        stored["setup_done"] = True  # made by a version without the setup wizard: it was set up by hand
    return deep_merge(DEFAULT_CONFIG, stored)


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def get_path(d: dict, path: str, default=None):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def set_path(d: dict, path: str, value) -> None:
    *parents, last = path.split(".")
    for k in parents:
        d = d.setdefault(k, {})
    d[last] = value


# Settings editable from the dashboard.  path: (group, label, type, apply)
#   type:  str | int | port | bool | color | choice
#   apply: live    = takes effect immediately
#          reader  = restarts the data source (and clears the decoded values)
#          vmix    = reconnects the vMix TCP push
#          restart = saved now, used the next time ScoreReader starts
SETTINGS = {
    "source": ("Data source", "Source", "choice", "reader"),
    "port": ("Data source", "Serial port", "str", "reader"),
    "baud": ("Data source", "Baud", "int", "reader"),
    "tcp.host": ("Data source", "TCP host", "str", "reader"),
    "tcp.port": ("Data source", "TCP port", "port", "reader"),
    "replay_file": ("Data source", "Capture to replay", "choice", "reader"),
    "scorelink.probe_on_start": ("Data source", "Ask the bot for its settings at start", "bool", "reader"),
    "scorelink.send_config_on_start": ("Data source", "Re-send the bot configuration at start", "bool", "reader"),
    "scoreboard.protocol": ("Scoreboard", "Scoreboard brand and sport", "choice", "reader"),
    "scoreboard.connection": ("Scoreboard", "Wiring", "int", "reader"),
    "sport": ("Scoreboard", "Sport (used when the console doesn't identify itself)", "choice", "live"),
    "teams.home_name": ("Teams", "Home name", "str", "live"),
    "teams.home_abbr": ("Teams", "Home abbreviation", "str", "live"),
    "teams.home_color": ("Teams", "Home color", "color", "live"),
    "teams.guest_name": ("Teams", "Guest name", "str", "live"),
    "teams.guest_abbr": ("Teams", "Guest abbreviation", "str", "live"),
    "teams.guest_color": ("Teams", "Guest color", "color", "live"),
    "vmix.enabled": ("vMix", "Push values to vMix (TCP API)", "bool", "vmix"),
    "vmix.host": ("vMix", "vMix host", "str", "vmix"),
    "vmix.port": ("vMix", "vMix TCP port", "port", "vmix"),
    "vmix.input": ("vMix", "Title input name or number", "str", "vmix"),
    "mqtt.enabled": ("MQTT", "MQTT output", "bool", "restart"),
    "mqtt.bot_number": ("MQTT", "Bot number (topics bot/N/…)", "int", "live"),
    "mqtt.mode": ("MQTT", "Mode", "choice", "restart"),
    "mqtt.tcp_port": ("MQTT", "Broker TCP port", "port", "restart"),
    "mqtt.ws_port": ("MQTT", "Broker WebSocket port", "port", "restart"),
    "mqtt.broker_host": ("MQTT", "External broker host (client mode)", "str", "restart"),
    "mqtt.broker_port": ("MQTT", "External broker port (client mode)", "port", "restart"),
    "webhook.enabled": ("Webhook", "POST the data to a URL on every change", "bool", "webhook"),
    "webhook.url": ("Webhook", "Webhook URL", "url", "webhook"),
    "webhook.min_interval_ms": ("Webhook", "At most one POST every (ms)", "int", "live"),
    "text_files.enabled": ("Files", "Write one .txt file per field", "bool", "live"),
    "record_raw": ("Files", "Record raw bytes to logs/", "bool", "live"),
    "save_state": ("Files", "Remember board values after a restart (RTD only)", "bool", "live"),
    "http_port": ("Advanced", "Dashboard / data.json port", "port", "restart"),
    "output_all_fields": ("Advanced", "Include every raw field in data.json / XML / CSV", "bool", "live"),
    "setup_done": ("Advanced", "First-run setup finished", "bool", "live"),
}


def list_captures() -> list[str]:
    try:
        names = [n for n in os.listdir(LOG_DIR) if n.endswith(".bin")]
    except OSError:
        return []
    return sorted(names, key=lambda n: os.path.getmtime(os.path.join(LOG_DIR, n)), reverse=True)[:50]


def coerce_setting(path: str, value, choices: dict):
    """Validate one dashboard setting. Returns the clean value or raises ValueError with a readable message."""
    kind = SETTINGS[path][2]
    if kind == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind in ("int", "port"):
        try:
            n = int(str(value).strip())
        except ValueError:
            raise ValueError("must be a whole number") from None
        lo, hi = (1, 65535) if kind == "port" else (0, 10_000_000)
        if path == "mqtt.ws_port":
            lo = 0  # 0 = WebSocket off
        if not lo <= n <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return n
    if kind == "color":
        v = str(value or "").strip()
        if v and not (len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])):
            raise ValueError("must look like #1E3A8A (or be blank)")
        return v.upper()
    if kind == "choice":
        v = str(value)
        if v not in choices.get(path, ()):
            raise ValueError(f"'{v}' is not one of: {', '.join(choices.get(path, ())) or 'none available'}")
        return v
    v = str(value if value is not None else "").strip()
    if len(v) > 200:
        raise ValueError("is too long")
    if kind == "url" and v and not v.lower().startswith(("http://", "https://")):
        raise ValueError("must start with http:// or https:// (or be blank)")
    return v


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


class Status:
    def __init__(self):
        self.lock = threading.Lock()
        self.source_state = "starting"
        self.source_detail = ""
        self.bytes_in = 0
        self.packets = 0
        self.bad_packets = 0
        self.unknown_headers: dict[str, int] = {}
        self.last_packet_time = 0.0
        self.last_byte_time = 0.0
        self.vmix_state = "disabled"
        self.pic_info: dict[str, str] = {}
        self.logs: list[str] = []
        self.recent_hex = ""
        self.webhook_state = "disabled"
        self.reads: dict[str, tuple[float, str]] = {}  # "/data.xml" -> (time, client address) of the last read

    def note_read(self, path: str, client: str):
        with self.lock:
            self.reads[path] = (time.time(), client)

    def add_log(self, line: str):
        with self.lock:
            self.logs.append(line)
            del self.logs[:-200]

    def set_source(self, state: str, detail: str = ""):
        with self.lock:
            self.source_state, self.source_detail = state, detail

    def reset_counters(self):
        """Called when the data source changes, so the dashboard doesn't mix the old and new sources."""
        with self.lock:
            self.bytes_in = self.packets = self.bad_packets = 0
            self.unknown_headers = {}
            self.last_packet_time = self.last_byte_time = 0.0
            self.pic_info = {}
            self.recent_hex = ""


STATUS = Status()


# --------------------------------------------------------------------------- RTD decoder
class DaktronicsRTD:
    """Daktronics 'Enhanced RTD' stream decoder.

    Packet: SYN(0x16) ... SOH(0x01) header STX(0x02) data EOT(0x04) checksum ETB(0x17)
    header = '004210' + 4-digit zero-based offset into the console's RTD buffer.
    Only changed regions are sent, so we keep a persistent buffer.
    """

    SYN, SOH, STX, EOT, ETB = 0x16, 0x01, 0x02, 0x04, 0x17

    def __init__(self, strict_header: bool = True, size: int = 1024):
        self.buf = bytearray(b" " * size)
        self.changed_at = [0.0] * size
        self.pending = bytearray()
        self.strict = strict_header
        self.lock = threading.Lock()
        self.version = 0  # bumps on every change

    def load(self, path: str):
        try:
            with open(path, "rb") as f:
                data = f.read()
            with self.lock:
                self.buf[: len(data)] = data[: len(self.buf)]
            log(f"Restored last known scoreboard state ({len(data)} bytes)")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Could not restore state: {e}")

    def save(self, path: str):
        with self.lock:
            data = bytes(self.buf)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def feed(self, data: bytes) -> int:
        """Feed raw bytes; returns number of packets applied."""
        self.pending += data
        applied = 0
        while True:
            s = self.pending.find(self.SYN)
            if s < 0:
                self.pending.clear()
                break
            e = self.pending.find(self.ETB, s + 1)
            if e < 0:
                if s:
                    del self.pending[:s]
                if len(self.pending) > 4096:  # garbage / wrong baud
                    self.pending.clear()
                break
            pkt = bytes(self.pending[s + 1 : e])
            del self.pending[: e + 1]
            if self._apply(pkt):
                applied += 1
        return applied

    def _apply(self, pkt: bytes) -> bool:
        soh = pkt.find(bytes([self.SOH]))
        if soh < 0:
            STATUS.bad_packets += 1
            return False
        rest = pkt[soh + 1 :]
        stx = rest.find(bytes([self.STX]))
        if stx < 0:
            return False  # header-only packet (no data)
        header, rest = rest[:stx], rest[stx + 1 :]
        eot = rest.find(bytes([self.EOT]))
        data = rest[:eot] if eot >= 0 else rest
        h = header.decode("latin-1", "replace")
        if self.strict and not h.startswith("004210"):
            key = "".join(c if 32 <= ord(c) < 127 else "." for c in h[:24])
            if len(STATUS.unknown_headers) < 20 or key in STATUS.unknown_headers:
                STATUS.unknown_headers[key] = STATUS.unknown_headers.get(key, 0) + 1
            return False
        try:
            offset = int(h[-4:])
        except ValueError:
            STATUS.bad_packets += 1
            return False
        if not data:
            return False
        end = offset + len(data)
        with self.lock:
            if end > len(self.buf):
                grow = end - len(self.buf) + 256
                self.buf += b" " * grow
                self.changed_at += [0.0] * grow
            if self.buf[offset:end] != data:
                now = time.time()
                for i in range(offset, end):
                    if self.buf[i] != data[i - offset]:
                        self.changed_at[i] = now
                self.buf[offset:end] = data
                self.version += 1
        STATUS.packets += 1
        STATUS.last_packet_time = time.time()
        return True

    def snapshot(self) -> bytes:
        with self.lock:
            return bytes(self.buf)


def encode_rtd_packet(offset: int, text: str) -> bytes:
    """Build a Daktronics RTD packet (used by demo mode / tests)."""
    body = b"\x01" + f"004210{offset:04d}".encode() + b"\x02" + text.encode("latin-1") + b"\x04"
    csum = f"{sum(body) & 0xFF:02X}".encode()
    return b"\x16" + body + csum + b"\x17"


# --------------------------------------------------------------------------- Daktronics current-loop (scoreboard driver) protocol
SEG7 = {0x3F: "0", 0x06: "1", 0x5B: "2", 0x4F: "3", 0x66: "4", 0x6D: "5", 0x7D: "6", 0x07: "7", 0x7F: "8",
        0x6F: "9", 0x67: "9", 0x00: " "}


def seg(b: int) -> str:
    return SEG7.get(b & 0x7F, " ")


class DaktronicsCurrentLoop:
    """Decoder for the All Sport 5000 *current loop* output (what a ScoreBot on M8 / current-loop receives).

    Frame: 55 AA | addr | len | x | x | 22 | data ... | checksum      (len+5 bytes after 55 AA)
    sum(addr..checksum) + 0x55 + 0xAA == 0 (mod 256).  Data bytes are 7-segment patterns.
    Field layout = ScoreConnect III 'Daktronics Main FootballDecoder' (console code 6601).
    """

    def __init__(self):
        self.pending = bytearray()
        self.frames: dict[int, tuple[bytes, float]] = {}
        self.lock = threading.Lock()
        self.version = 0
        self.packets = 0
        self.bad = 0
        self.last_time = 0.0
        self.console_code = ""
        self.sport_fallback = lambda: "football"  # set by App -> configured sport
        self.custom_fields = lambda: {}  # set by App -> cfg["custom_fields"]
        self.st = {"clock_mode": ":", "clock_digits": "    ", "tenths": "", "play_clock": "",
                   "home_shots": "", "guest_shots": "", "home_corners": "", "guest_corners": "", "horn": False,
                   "clock_running": False, "home_score": "", "guest_score": "", "period": "",
                   "home_poss": False, "guest_poss": False, "home_timeouts": "", "guest_timeouts": "",
                   "ball_on": "", "down": "", "to_go": ""}

    def feed(self, data: bytes) -> int:
        self.pending += data
        n_ok = 0
        while True:
            i = self.pending.find(b"\x55\xaa")
            if i < 0:
                if len(self.pending) > 1:
                    del self.pending[:-1]
                break
            if i:
                del self.pending[:i]
            if len(self.pending) < 4:
                break
            ln = self.pending[3]
            total = 2 + ln + 5
            if len(self.pending) < total:
                break
            pkt = bytes(self.pending[2:total])
            if pkt[4] == 0x22 and (sum(pkt) + 0x55 + 0xAA) & 0xFF == 0:
                del self.pending[:total]
                self._apply(pkt)
                n_ok += 1
            else:
                self.bad += 1
                del self.pending[:2]
        return n_ok

    CODE_SPORT = {"6601": "football", "7601": "soccer"}

    @property
    def sport(self) -> str:
        return self.CODE_SPORT.get(self.console_code) or self.sport_fallback()

    def _apply(self, p: bytes):
        addr, n = p[0], len(p)
        now = time.time()
        self.packets += 1
        self.last_time = now
        STATUS.packets += 1
        STATUS.last_packet_time = now
        st = dict(self.st)
        if addr == 0xFE:
            self.console_code = p[5:9].decode("latin-1", "replace")
        elif addr == 1:
            if n > 10:
                st["play_clock"] = (seg(p[9]) + (seg(p[10]) if n > 11 else "")).strip()
            if n > 9:
                st["clock_mode"] = ":" if p[8] else "."
            self._set_clock(st, p, n)
            if n > 12:
                st["clock_running"] = (p[11] & 0x40) == 0
        elif self.sport == "soccer":
            self._apply_soccer(st, p, addr, n)
        elif addr == 15:
            self._set_clock(st, p, n)
            if n > 13:
                st["home_score"] = (seg(p[11]) + seg(p[12])).strip()
            if n > 15:
                st["guest_score"] = (seg(p[13]) + seg(p[14])).strip()
            if n > 18:
                st["home_poss"] = p[17] == 7
                st["guest_poss"] = p[17] == 56
            if n > 19:
                st["home_timeouts"] = seg(p[18]).strip()
            if n > 20:
                st["guest_timeouts"] = seg(p[19]).strip()
            if n > 16:
                st["period"] = seg(p[15]).strip()
        elif addr == 16:
            if n > 7:
                st["ball_on"] = (seg(p[5]) + seg(p[6])).strip()
            if n > 8:
                st["down"] = seg(p[7]).strip()
            if n > 10:
                st["to_go"] = (seg(p[8]) + seg(p[9])).strip()
        with self.lock:
            prev = self.frames.get(addr)
            self.frames[addr] = (p, now)
            if st != self.st or prev is None or prev[0] != p:
                self.st = st
                self.version += 1

    def _apply_soccer(self, st, p, addr, n):
        """ScoreConnect 'Daktronics Main SoccerDecoder' (code 7601) + addr 72 stats panel."""
        two = lambda a: (seg(p[a]) + seg(p[a + 1])).strip() if n > a + 1 else ""  # noqa: E731
        if addr == 11:
            self._set_clock(st, p, n)
            if n > 17:
                st["home_score"] = two(15)
            if n > 19:
                st["guest_score"] = two(17)
            if n > 20:
                st["period"] = seg(p[19]).strip()
        elif addr == 12:
            if n > 14:
                st["horn"] = bool(p[13] & 0x80)
            if n > 17:
                st["home_shots"] = two(15)
            if n > 19:
                st["guest_shots"] = two(17)
        elif addr == 13:
            # ScoreConnect's spot for corners - only used if this board has no stats panel (addr 72)
            hc, gc = two(15), two(17)
            seen72 = self.frames.get(72)
            if (hc or gc) and not (seen72 and time.time() - seen72[1] < 10):
                st["home_corners"], st["guest_corners"] = hc, gc
        elif addr == 72 and n > 20:
            # stats panel: [CKICKS h][SOG h] ... [CKICKS g][SOG g]
            st["home_corners"], st["guest_corners"] = two(5), two(15)
            hs, gs = two(8), two(18)
            if hs or gs:
                st["home_shots"], st["guest_shots"] = hs, gs

    @staticmethod
    def _set_clock(st, p, n):
        d = [seg(p[i]) if n > i + 1 else " " for i in range(5, 9)]
        if st["clock_mode"] == ".":
            st["clock_digits"], st["tenths"] = d[0] + d[1], d[2]
        else:
            st["clock_digits"], st["tenths"] = "".join(d), ""

    def data(self) -> dict:
        st = self.st
        if st["clock_mode"] == ".":
            clock = f"{st['clock_digits'].strip()}.{st['tenths']}".strip(".")
        else:
            c = st["clock_digits"]
            clock = f"{c[:2].strip()}:{c[2:]}".strip(":") if c.strip() else ""
        per = st["period"]
        if self.sport == "soccer":
            return self._custom({
                "clock": clock.strip(),
                "clock_running": st["clock_running"],
                "home_score": st["home_score"] or "0",
                "guest_score": st["guest_score"] or "0",
                "period": per,
                "period_ordinal": ordinal(int(per)) if per.isdigit() and int(per) > 0 else per,
                "home_shots": st["home_shots"],
                "guest_shots": st["guest_shots"],
                "home_corners": st["home_corners"],
                "guest_corners": st["guest_corners"],
                "horn": st["horn"],
                "sport": "soccer",
                "console_code": self.console_code,
                "protocol": "daktronics_current_loop",
            })
        down = st["down"]
        dtxt = ordinal(int(down)) if down.isdigit() else down
        out = {
            "sport": "football",
            "clock": clock.strip(),
            "clock_running": st["clock_running"],
            "play_clock": st["play_clock"].lstrip("0") or ("0" if st["play_clock"] else ""),
            "home_score": st["home_score"] or "0",
            "guest_score": st["guest_score"] or "0",
            "period": per,
            "period_ordinal": ordinal(int(per)) if per.isdigit() and int(per) > 0 else per,
            "home_poss": st["home_poss"],
            "guest_poss": st["guest_poss"],
            "home_timeouts": st["home_timeouts"],
            "guest_timeouts": st["guest_timeouts"],
            "ball_on": st["ball_on"],
            "down": dtxt,
            "to_go": st["to_go"],
            "down_distance": f"{dtxt} & {st['to_go']}" if dtxt and st["to_go"] else dtxt,
            "console_code": self.console_code,
            "protocol": "daktronics_current_loop",
        }
        return self._custom(out)

    def _custom(self, out: dict) -> dict:
        """Apply config 'custom_fields': {"name": {"addr": 72, "bytes": [0, 1]}} (byte index = hex column position)."""
        for name, spec in (self.custom_fields() or {}).items():
            try:
                fr = self.frames.get(int(spec["addr"]))
                if fr is None:
                    continue
                body = fr[0][5:-1]
                out[name] = "".join(seg(body[i]) for i in spec["bytes"] if i < len(body)).strip()
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def address_table(self) -> list:
        now = time.time()
        with self.lock:
            items = sorted(self.frames.items())
        rows = []
        for a, (p, t) in items:
            body = p[5:-1]
            rows.append({"addr": a, "len": p[1], "age": round(now - t, 1),
                         "digits": "".join(seg(b) if (b & 0x7F) in SEG7 else "?" for b in body[:40]),
                         "hex": body[:40].hex(" ")})
        return rows

# --------------------------------------------------------------------------- sport fields
class SportMap:
    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.sports = json.load(f)

    def names(self):
        return sorted(self.sports)

    def fields(self, sport: str):
        return self.sports.get(sport, {"fields": []})["fields"]

    @staticmethod
    def _clean_clock(s: str) -> str:
        s = s.strip()
        if ":" in s:
            m, sec = s.split(":", 1)
            if m.strip() in ("", "0", "00"):
                s = sec.strip()
        if s.isdigit():
            s = str(int(s)) if s else s
        return s

    def extract(self, sport: str, buf: bytes) -> dict:
        out: dict = {}
        for f in self.fields(sport):
            p, ln = f["pos"] - 1, f["len"]
            raw = buf[p : p + ln].decode("latin-1", "replace") if p < len(buf) else ""
            if f["type"] == "bool":
                val = raw.strip() != ""
            elif f["type"] == "int":
                t = raw.strip()
                val = int(t) if t.isdigit() else t
            else:
                val = raw.strip()
            out[f["name"]] = val
        out.update(self.simple(sport, out))
        return out

    def simple(self, sport: str, d: dict) -> dict:
        """Friendly, sport-agnostic aliases for graphics."""
        g = d.get
        s: dict = {}

        def score(v):
            v = str(v if v is not None else "").strip()
            return str(int(v)) if v.isdigit() else (v or "0")

        s["clock"] = str(g("main_clock_time", "")).strip()
        s["home_name"] = g("home_team_name", "")
        s["guest_name"] = g("guest_team_name", "")
        s["home_abbr"] = g("home_team_abbreviation", "")
        s["guest_abbr"] = g("guest_team_abbreviation", "")
        s["home_score"] = score(g("home_team_score"))
        s["guest_score"] = score(g("guest_team_score"))
        s["home_timeouts"] = str(g("home_time_outs_left_total", ""))
        s["guest_timeouts"] = str(g("guest_time_outs_left_total", ""))
        per = next((g(k) for k in ("period", "quarter", "half", "inning") if k in d), "")
        s["period"] = str(per)
        s["period_ordinal"] = ordinal(per) if isinstance(per, int) and per > 0 else str(per)
        per_txt = next((g(k) for k in ("period_text", "quarter_text", "half_text", "inning_text") if k in d), "")
        s["period_text"] = str(per_txt or "").strip()
        s["home_poss"] = bool(g("home_possession_indicator", False))
        s["guest_poss"] = bool(g("guest_possession_indicator", False))
        s["clock_running"] = not bool(g("main_clock_stopped", False))
        if "play_clock_time" in d:
            s["play_clock"] = self._clean_clock(str(g("play_clock_time")))
        if "shot_clock_time" in d:
            s["shot_clock"] = self._clean_clock(str(g("shot_clock_time")))
        if sport == "football":
            down = str(g("down", "")).strip()
            togo = g("to_go", "")
            s["down"], s["to_go"] = down, str(togo)
            s["down_distance"] = f"{down} & {togo}" if down and togo != "" else down
            s["ball_on"] = str(g("ball_on", ""))
        if sport == "soccer":  # same names as the current-loop soccer output
            for side in ("home", "guest"):
                s[f"{side}_shots"] = str(g(f"{side}_shots_on_goal_total", "")).strip()
                s[f"{side}_corners"] = str(g(f"{side}_corner_kicks_total", "")).strip()
                s[f"{side}_saves"] = str(g(f"{side}_saves_total", "")).strip()
        if sport == "basketball":
            s["home_fouls"] = str(g("home_team_fouls", ""))
            s["guest_fouls"] = str(g("guest_team_fouls", ""))
            s["home_bonus"] = bool(g("home_1_on_1_bonus_indicator") or g("home_2_shot_bonus_indicator"))
            s["guest_bonus"] = bool(g("guest_1_on_1_bonus_indicator") or g("guest_2_shot_bonus_indicator"))
        if sport == "baseball":
            for k in ("ball", "strike", "out"):
                s[k + "s"] = str(g(k, ""))
            s["home_hits"], s["guest_hits"] = str(g("home_hits", "")), str(g("guest_hits", ""))
            s["home_errors"], s["guest_errors"] = str(g("home_errors", "")), str(g("guest_errors", ""))
        return s


# --------------------------------------------------------------------------- multi-vendor contracts -> output fields
def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper() or (i + 1 < len(name) and name[i + 1].islower())):
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


class Data(dict):
    """Output fields plus `raw_keys`: fields that aren't used by the sport's graphics (raw RTD/contract fields).
    The dashboard shows everything; data.json/xml/csv and text files leave raw_keys out by default."""
    raw_keys: frozenset = frozenset()

    def copy(self):
        d = Data(self)
        d.raw_keys = self.raw_keys
        return d

    def public(self) -> dict:
        return {k: v for k, v in self.items() if k not in self.raw_keys}


def contract_to_data(contract: str, c: dict) -> dict:
    """ScoreReader output fields for a brand decoder's contract (scoreboards.contracts.flat), with a plain
    snake_case fallback so data still flows if the contracts module can't handle this contract."""
    try:
        from scoreboards import contracts

        full, raw = contracts.flat(contract, c, _split=True)
        if isinstance(full, dict):
            d = Data(full)
            d.raw_keys = frozenset(raw)
            return d
    except Exception as e:
        if not getattr(contract_to_data, "_warned", False):
            contract_to_data._warned = True
            log(f"Output mapping for {contract} unavailable ({e}); showing raw contract fields")
    d = {_snake(k): (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in c.items()}
    d.setdefault("sport", str(c.get("EventId", "")).lower())
    return d


RTD_CONTRACTS = {"basketball": ("Basketball", "Basketball"), "baseball": ("BaseballSoftball", "Baseball"),
                 "softball": ("BaseballSoftball", "Softball"), "hockey_lacrosse": ("HockeyLacrosse", "Hockey"),
                 "hockey": ("HockeyLacrosse", "Hockey"), "lacrosse": ("HockeyLacrosse", "Lacrosse"),
                 "volleyball": ("Volleyball", "Volleyball")}


def rtd_contract(sport: str, d: dict):
    """(contract, dict) for MQTT from the built-in RTD decoder's fields, for sports other than football/soccer
    (those keep build_payloads). Only sets fields the sport's Sportzcast contract has. None if not mapped."""
    if sport not in RTD_CONTRACTS:
        return None
    name, event = RTD_CONTRACTS[sport]

    def digits(v, n):
        t = str(v if v is not None else "").strip()
        return list(t.rjust(n)[-n:])

    c = {"EventId": event, "InternalClockStatus": "R" if d.get("clock_running") else "S"}
    clock = str(d.get("clock", "")).strip()
    if "." in clock and ":" not in clock:
        sec, t = clock.split(".", 1)
        st, so = digits(sec, 2)
        c.update(ClockMode=".", ClockSecondTens=st, ClockSecondOnes=so, ClockTenthsSecond=(t[:1] or "0"))
    elif ":" in clock:
        m, sec = clock.split(":", 1)
        mt, mo = digits(m, 2)
        st, so = digits(sec.rjust(2, "0"), 2)
        c.update(ClockMode=":", ClockMinutesTens=mt, ClockMinutesOnes=mo, ClockSecondTens=st, ClockSecondOnes=so)
    period = str(d.get("period", "")).strip()[-1:] or " "
    if name == "BaseballSoftball":
        for side, run in (("Home", "home_score"), ("Guest", "guest_score")):
            c[f"{side}RunTens"], c[f"{side}RunOnes"] = digits(d.get(run), 2)
            c[f"{side}HitsTens"], c[f"{side}HitsOnes"] = digits(d.get(f"{side.lower()}_hits"), 2)
            c[f"{side}Errors"] = (str(d.get(f"{side.lower()}_errors", "")).strip()[-1:] or " ")
        c["InningTens"], c["InningOnes"] = digits(d.get("period"), 2)
        for k in ("balls", "strikes", "outs"):
            c[k.capitalize()] = str(d.get(k, "")).strip()[-1:] or " "
    else:
        width = 3 if name == "Basketball" else 2
        for side in ("Home", "Guest"):
            ds = digits(d.get(f"{side.lower()}_score"), width)
            if width == 3:
                c[f"{side}ScoreHundreds"] = ds.pop(0)
            c[f"{side}ScoreTens"], c[f"{side}ScoreOnes"] = ds
            c[f"{side}Timeouts"] = str(d.get(f"{side.lower()}_timeouts", "")).strip()[-1:] or " "
            c[f"{side}Possession"] = ("<" if side == "Home" else ">") if d.get(f"{side.lower()}_poss") else " "
        c["Period"] = period
        if name == "Basketball":
            for side in ("Home", "Guest"):
                c[f"{side}FoulTens"], c[f"{side}FoulOnes"] = digits(d.get(f"{side.lower()}_fouls"), 2)
            c["ShotClockSecondTens"], c["ShotClockSecondOnes"] = digits(d.get("shot_clock"), 2)
    try:
        from scoreboards import contracts

        allowed = {p[0] for p in contracts._spec(name).props} | set(contracts._BASE_CHARS) | {
            "EventId", "InternalClockStatus", "ClockMode"}
        c = {k: v for k, v in c.items() if k in allowed}
    except Exception:
        return None
    return name, c


# --------------------------------------------------------------------------- Windows HID (no hidapi needed)
class WinHID:
    """Talk to a HID device on Windows using only built-in DLLs (hid.dll / setupapi / kernel32)."""

    def __init__(self, vid: int, pid: int):
        import ctypes
        from ctypes import wintypes as wt

        self.ct, self.wt = ctypes, wt
        hid = ctypes.WinDLL("hid")
        sa = ctypes.WinDLL("setupapi", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.hid, self.k32 = hid, k32

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class IFDATA(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wt.DWORD),
                        ("Reserved", ctypes.c_size_t)]

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]

        self.OVERLAPPED = OVERLAPPED
        guid = GUID()
        hid.HidD_GetHidGuid(ctypes.byref(guid))
        sa.SetupDiGetClassDevsW.restype = wt.HANDLE
        sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
        sa.SetupDiEnumDeviceInterfaces.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wt.DWORD,
                                                   ctypes.POINTER(IFDATA)]
        sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [wt.HANDLE, ctypes.POINTER(IFDATA), ctypes.c_void_p,
                                                        wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
        sa.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
        k32.CreateEventW.restype = wt.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        for fn in ("ReadFile", "WriteFile"):
            getattr(k32, fn).argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                         ctypes.POINTER(OVERLAPPED)]
        k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
        k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        k32.WaitForSingleObject.restype = wt.DWORD
        k32.CancelIo.argtypes = [wt.HANDLE]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        hid.HidD_FlushQueue.argtypes = [wt.HANDLE]

        want = f"vid_{vid:04x}&pid_{pid:04x}"
        hdev = sa.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x02 | 0x10)  # PRESENT | DEVICEINTERFACE
        paths = []
        try:
            i = 0
            while True:
                ifd = IFDATA()
                ifd.cbSize = ctypes.sizeof(IFDATA)
                if not sa.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), i, ctypes.byref(ifd)):
                    break
                i += 1
                need = wt.DWORD(0)
                sa.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
                buf = ctypes.create_string_buffer(need.value + 4)
                ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if sa.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), buf, need.value + 4, None, None):
                    path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                    if want in path.lower():
                        paths.append(path)
        finally:
            sa.SetupDiDestroyDeviceInfoList(hdev)
        if not paths:
            raise RuntimeError("ScoreLinkII HID interface not found in Windows (is the bot plugged in?)")
        GENERIC_RW, SHARE_RW, OPEN_EXISTING, OVERLAPPED_FLAG = 0xC0000000, 0x3, 3, 0x40000000
        self.h = k32.CreateFileW(paths[0], GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, OVERLAPPED_FLAG, None)
        if self.h in (None, 0, ctypes.c_void_p(-1).value):
            raise OSError(f"Could not open ScoreLinkII HID (error {ctypes.get_last_error()})")
        self.event = k32.CreateEventW(None, True, False, None)

    def _io(self, fn, buf, n, timeout_ms):
        ct, wt, k32 = self.ct, self.wt, self.k32
        ov = self.OVERLAPPED()
        ov.hEvent = self.event
        done = wt.DWORD(0)
        ok = fn(self.h, buf, n, ct.byref(done), ct.byref(ov))
        if not ok:
            err = ct.get_last_error()
            if err != 997:  # ERROR_IO_PENDING
                raise OSError(f"HID I/O error {err}")
            if k32.WaitForSingleObject(self.event, timeout_ms) != 0:
                k32.CancelIo(self.h)
                raise IOError("HID I/O timed out")
            if not k32.GetOverlappedResult(self.h, ct.byref(ov), ct.byref(done), False):
                raise OSError(f"HID I/O error {ct.get_last_error()}")
        return done.value

    def write(self, data):
        buf = self.ct.create_string_buffer(bytes(data), 65)
        self._io(self.k32.WriteFile, buf, 65, 1000)

    def read(self, n=64, timeout_ms=1000):
        buf = self.ct.create_string_buffer(65)
        got = self._io(self.k32.ReadFile, buf, 65, timeout_ms)
        return list(buf.raw[1:got])  # drop report id

    def flush(self):
        self.hid.HidD_FlushQueue(self.h)

    def close(self):
        self.k32.CloseHandle(self.event)
        self.k32.CloseHandle(self.h)

# --------------------------------------------------------------------------- MCP2221 (ScoreLinkII) GPIO
class MCP2221:
    """Minimal MCP2221 HID driver: just enough to drive GP2 (ScoreLinkII mode select)."""

    def __init__(self):
        self.flush = lambda: None
        if sys.platform == "win32":  # built-in Windows HID, no extra packages
            w = WinHID(SCORELINK_VID, SCORELINK_PID)
            self.dev = w
            self._write = w.write
            self._read = lambda: w.read(64, 1000)
            self.flush = w.flush
            return
        import hid  # mac/linux: pip install hidapi

        devs = [d for d in hid.enumerate(SCORELINK_VID, SCORELINK_PID)]
        if not devs:
            raise RuntimeError("MCP2221 HID interface not found (is the ScoreBot plugged in?)")
        path = devs[0]["path"]
        if hasattr(hid, "device"):  # cython-hidapi ("hidapi" on PyPI)
            self.dev = hid.device()
            self.dev.open_path(path)
            self._write = lambda b: self.dev.write(b)
            self._read = lambda: self.dev.read(64, 1000)
        else:  # "hid" ctypes package
            self.dev = hid.Device(path=path)
            self._write = lambda b: self.dev.write(bytes(b))
            self._read = lambda: list(self.dev.read(64, 1000))

    def cmd(self, payload: list[int]) -> list[int]:
        buf = [0] * 64
        buf[: len(payload)] = payload
        self.flush()
        self._write([0x00] + buf)  # report id 0
        resp = self._read()
        if not resp or resp[0] != payload[0]:
            raise IOError(f"MCP2221 bad response to 0x{payload[0]:02X}: {resp[:8] if resp else resp}")
        return resp

    def get_gp2(self):
        r = self.cmd([0x51])
        return r[6], r[7]  # value, direction (0xEE = pin not in GPIO mode)

    def set_gp2(self, high: bool):
        val, direction = self.get_gp2()
        if val == 0xEE or direction != 0:
            # pin not configured as GPIO output -> read-modify-write SRAM GP settings
            sram = self.cmd([0x61])
            gp = list(sram[22:26])
            gp[2] = (0x10 if high else 0x00)  # bit4 value, bit3 dir(0=out), bits2:0 = 000 GPIO
            self.cmd([0x60, 0, 0, 0, 0, 0, 0, 0x80] + gp)
        # Set GPIO output values: GP2 block = bytes 10..13 (alter out, value, alter dir, dir)
        p = [0x50, 0] + [0] * 16
        p[10:14] = [0xFF, 1 if high else 0, 0xFF, 0]
        self.cmd(p)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- sources
def list_serial_ports():
    from serial.tools import list_ports

    return list(list_ports.comports())


def find_scorelink_port() -> str | None:
    for p in list_serial_ports():
        if p.vid == SCORELINK_VID and p.pid == SCORELINK_PID:
            return p.device
    for p in list_serial_ports():
        txt = f"{p.description} {p.manufacturer} {p.product}".lower()
        if "scorelink" in txt or "mcp2221" in txt:
            return p.device
    return None


def resolve_port(cfg) -> str:
    port = cfg.get("port", "auto")
    if port and port != "auto":
        return port
    p = find_scorelink_port()
    if p:
        return p
    ports = [p.device for p in list_serial_ports() if "bluetooth" not in p.device.lower()]
    if len(ports) == 1:
        return ports[0]
    raise RuntimeError(
        "Could not auto-detect the ScoreBot COM port. Ports seen: "
        + (", ".join(ports) or "none")
        + ". Set \"port\" in scorereader_config.json or pass --port COMx"
    )


def pic_command(ser, cmd: str, timeout=0.6) -> str:
    """Send '>XX\\r' to the ScoreLinkII PIC (command mode) and return the CR-terminated reply."""
    if not cmd.startswith(">"):
        cmd = ">" + cmd
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    ser.flush()
    deadline = time.time() + timeout
    resp = bytearray()
    while time.time() < deadline:
        b = ser.read(1)
        if not b:
            continue
        if b == b"\r":
            if resp:
                break
            continue
        resp += b
    text = resp.decode("latin-1", "replace").strip()
    if ">" in text:
        text = text[text.index(">") + 1 :]
    return text


def _readback(cmd: str) -> tuple:
    """(query, verify) for a PIC write, the way ScoreConnect checks it (falls back to a simple check)."""
    try:
        from scoreboards import hardware

        return hardware.readback_query(cmd), lambda reply: hardware.verify_reply(cmd, reply)
    except Exception:
        q = cmd[:3] + "?" if cmd.startswith(">M") else cmd[:2] + "?"
        return q, lambda reply: bool(reply)


def scorelink_session(cfg, ser, mcp, send_config: bool, probe: bool, commands=None, data_baud=None) -> dict:
    """Enter command mode, query/configure the PIC, return to data mode. Returns info dict.

    commands/data_baud: the brand's bot configuration (from scoreboards.hardware); default = the configured
    scorelink.config_commands at cfg["baud"] (Daktronics current loop, 19200).
    """
    sl = cfg["scorelink"]
    commands = list(commands if commands is not None else sl["config_commands"])
    data_baud = int(data_baud or cfg["baud"])
    info = {}
    if mcp is None:
        log("ScoreLink: no GPIO control available -> skipping PIC command mode")
        return info
    mcp.set_gp2(False)  # command mode
    time.sleep(0.05)
    ser.baudrate = sl["command_baud"]
    time.sleep(0.05)
    ser.reset_input_buffer()
    try:
        if probe or send_config:
            for q in ("R?", "B?", "U?", "M?", "T?", "D?", "M8?"):
                r = pic_command(ser, ">" + q)
                info[q] = r
                log(f"PIC {q:4} -> {r or '(no reply)'}")
        if send_config:
            ok = True
            for c in commands:
                query, verify = _readback(c)
                for attempt in range(10 if c.startswith(">M") else 4):  # ScoreConnect's retry counts
                    pic_command(ser, c)
                    time.sleep(0.3)  # PIC writes EEPROM
                    r = pic_command(ser, query)
                    if verify(r):
                        break
                else:
                    ok = False
                log(f"PIC set {c} -> readback {r or '(no reply)'}" + ("" if verify(r) else "  (NOT CONFIRMED)"))
            info["configured"] = ok
    finally:
        mcp.set_gp2(True)  # data mode
        time.sleep(0.05)
        ser.baudrate = data_baud
        time.sleep(0.05)
        ser.reset_input_buffer()
    STATUS.pic_info = info
    return info


class Reader(threading.Thread):
    def __init__(self, cfg, decoder: DaktronicsRTD, stop: threading.Event, cl=None, vendor=None, bot=None):
        """vendor: a scoreboards Protocol for the chosen brand/sport (None = built-in Daktronics decoders).
        bot: the ScoreBot set-up for that brand, see App.bot_plan()."""
        super().__init__(daemon=True)
        self.cfg, self.decoder, self.stop = cfg, decoder, stop
        self.cl = cl
        self.vendor = vendor
        self.bot = bot or {}
        self.cl_other = None  # ScoreConnect's Daktronics autodetect, for console codes other than 6601/7601
        self.raw_file = None
        self.reinit_request = threading.Event()
        self.raw_sink: queue.Queue | None = None  # for --sniff
        self.raw_hook = None  # e.g. MQTT bot/N/raw

    def _feed(self, data: bytes):
        if self.raw_hook is not None:
            self.raw_hook(data)
        if self.vendor is not None:
            n = self.vendor.feed(data)
            if n:
                STATUS.packets += n
                STATUS.last_packet_time = time.time()
            return
        if self.cl is not None:
            self.cl.feed(data)
            if self.cl_other is not None:
                self.cl_other.feed(data)
            if self.cl.packets > 20:
                return  # current-loop stream confirmed: RTD decoder would only see noise
        self.decoder.feed(data)

    def _record(self, data: bytes):
        if self.raw_sink is not None:
            self.raw_sink.put(data)
        STATUS.bytes_in += len(data)
        STATUS.last_byte_time = time.time()
        STATUS.recent_hex = (STATUS.recent_hex + data.hex(" ") + " ")[-600:]
        if self.cfg.get("record_raw"):
            if self.raw_file is None:
                os.makedirs(LOG_DIR, exist_ok=True)
                fn = os.path.join(LOG_DIR, f"raw_{_dt.datetime.now():%Y%m%d_%H%M%S}.bin")
                self.raw_file = open(fn, "ab")
            self.raw_file.write(data)
            self.raw_file.flush()

    def run(self):
        src = self.cfg["source"]
        try:
            while not self.stop.is_set():
                try:
                    if src == "demo":
                        self.run_demo()
                    elif src == "replay":
                        self.run_replay()
                    elif src == "tcp":
                        self.run_tcp()
                    else:
                        self.run_serial(scorelink=(src == "scorelink"))
                except Exception as e:
                    if self.stop.is_set():
                        break
                    msg = str(e)
                    if "Access is denied" in msg or "PermissionError" in repr(e) or "busy" in msg.lower():
                        msg += "  -> The port is in use. Quit ScoreConnect III AND stop its Windows service " \
                               "(admin cmd: net stop ScoreConnectIII), then this will retry automatically."
                    STATUS.set_source("error", msg)
                    log(f"Source error: {msg}")
                    if os.environ.get("SCOREREADER_DEBUG"):
                        traceback.print_exc()
                self.stop.wait(3)
        finally:
            if self.raw_file is not None:
                try:
                    self.raw_file.close()
                except OSError:
                    pass
                self.raw_file = None

    def run_serial(self, scorelink: bool):
        import serial

        port = resolve_port(self.cfg)
        STATUS.set_source("connecting", port)
        mcp = None
        if scorelink and self.cfg["scorelink"].get("use_gpio_mode_switch", True):
            try:
                mcp = MCP2221()
            except ImportError:
                log("hidapi not installed -> can't switch ScoreLinkII modes (Mac/Linux: pip install hidapi)")
            except Exception as e:
                log(f"MCP2221 GPIO unavailable: {e}")
        bot = self.bot
        baud = int(bot.get("baud") or self.cfg["baud"]) if scorelink else int(self.cfg["baud"])
        ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.2)

        def session(configure: bool):
            info = scorelink_session(self.cfg, ser, mcp, configure, configure or sl.get("probe_on_start", True),
                                     commands=bot.get("commands"), data_baud=baud)
            if configure and info.get("configured") and bot.get("on_configured"):
                bot["on_configured"]()
            return info

        try:
            log(f"Opened {port}")
            sl = self.cfg["scorelink"]
            if scorelink and bot.get("note"):
                log(bot["note"])
            if scorelink and mcp is not None:
                configure = bool(sl.get("send_config_on_start", False) or bot.get("configure"))
                if bot.get("configure"):
                    log(f"Configuring the ScoreBot for {bot.get('label', 'this scoreboard')}")
                session(configure)
                log(f"ScoreLinkII in DATA mode, reading at {baud} baud")
            STATUS.set_source("connected", f"{port} @ {baud}")
            while not self.stop.is_set():
                if self.reinit_request.is_set():
                    self.reinit_request.clear()
                    if scorelink and mcp is not None:
                        session(True)
                data = ser.read(4096)
                if data:
                    self._record(data)
                    self._feed(data)
        finally:
            ser.close()
            if mcp:
                mcp.close()

    def run_replay(self):
        path = self.cfg.get("replay_file") or ""
        if not path:
            raise RuntimeError("No capture chosen to replay (pick one in Settings, or pass --replay FILE)")
        if not os.path.isabs(path) and not os.path.exists(path):
            path = os.path.join(LOG_DIR, path)  # dashboard picks a file name from logs/
        STATUS.set_source("connected", f"REPLAY {os.path.basename(path)}")
        with open(path, "rb") as f:
            raw = f.read()
        log(f"Replaying {len(raw)} bytes from {path} (~19200 baud speed)")
        for i in range(0, len(raw), 192):
            if self.stop.is_set():
                return
            chunk = raw[i : i + 192]
            STATUS.bytes_in += len(chunk)
            self._feed(chunk)
            time.sleep(0.1 / self.cfg.get("replay_speed", 1))
        log("Replay finished")
        self.stop.wait(3600)

    def run_tcp(self):  # noqa
        host, port = self.cfg["tcp"]["host"], int(self.cfg["tcp"]["port"])
        STATUS.set_source("connecting", f"{host}:{port}")
        with socket.create_connection((host, port), timeout=5) as s:
            s.settimeout(1)
            STATUS.set_source("connected", f"tcp {host}:{port}")
            while not self.stop.is_set():
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    raise ConnectionError("TCP source closed")
                self._record(data)
                self._feed(data)

    def run_demo(self):
        STATUS.set_source("connected", "DEMO simulator (fake football game)")
        log("DEMO mode: generating a fake Daktronics football stream")
        clock = 12 * 60
        play = 40
        home = guest = 0
        q = 1
        pos_home = True
        down, togo, ball = 1, 10, 25
        last_play = time.time()
        while not self.stop.is_set():
            pk = b""

            def put(pos, length, text, right=False):  # pos is 1-based, like the Daktronics manual
                t = str(text)
                t = t.rjust(length) if right else t.ljust(length)
                return encode_rtd_packet(pos - 1, t[:length])

            m, s = divmod(clock, 60)
            pk += put(1, 5, f"{m:2d}:{s:02d}")
            pk += put(201, 8, play, right=True)
            if time.time() - last_play > 6:
                last_play = time.time()
                if random.random() < 0.15:
                    if pos_home:
                        home += 7
                    else:
                        guest += 3
                    pos_home = not pos_home
                    down, togo, ball = 1, 10, 25
                else:
                    gain = random.randint(-2, 12)
                    togo -= gain
                    ball = max(1, min(49, ball + gain))
                    down = 1 if togo <= 0 else down + 1
                    togo = 10 if togo <= 0 else togo
                    if down > 4:
                        pos_home, down, togo = not pos_home, 1, 10
                play = 40
            pk += put(48, 20, "HOME TEAM") + put(68, 20, "VISITORS")
            pk += put(88, 10, "HOME") + put(98, 10, "VIS")
            pk += put(108, 4, home, right=True) + put(112, 4, guest, right=True)
            pk += put(122, 2, 3, right=True) + put(130, 2, 2, right=True)
            pk += put(142, 2, q, right=True)
            pk += put(210, 1, "<" if pos_home else " ") + put(215, 1, ">" if not pos_home else " ")
            dtxt = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}[min(down, 4)]
            pk += put(220, 2, ball, right=True) + put(222, 3, dtxt) + put(225, 2, togo, right=True)
            # simulate serial chunking
            for i in range(0, len(pk), 37):
                chunk = pk[i : i + 37]
                self._record(chunk)
                self._feed(chunk)
            time.sleep(1.0)
            clock = clock - 1 if clock > 0 else 12 * 60
            play = play - 1 if play > 0 else 40
            if clock == 0:
                q = q % 4 + 1


# --------------------------------------------------------------------------- outputs
class VmixPusher(threading.Thread):
    def __init__(self, cfg, stop):
        super().__init__(daemon=True)
        self.cfg, self.stop = cfg, stop
        self.q: queue.Queue = queue.Queue()
        self.last_sent: dict = {}
        self.sock = None

    def submit(self, data: dict):
        self.q.put(data)

    def _commands(self, data: dict):
        v = self.cfg["vmix"]
        inp = urllib.parse.quote(str(v["input"]))
        for field, target in v.get("fields", {}).items():
            if field not in data:
                continue
            val = data[field]
            if isinstance(target, dict):
                name = target.get("name", "")
                if target.get("visible"):
                    kind = "Image" if name.endswith(".Source") else "Text"
                    fn = f"Set{kind}Visible{'On' if val else 'Off'}"
                    yield (field, fn, f"Input={inp}&SelectedName={urllib.parse.quote(name)}", bool(val))
                    continue
                if isinstance(val, bool):
                    val = target.get("true", "1") if val else target.get("false", "")
            else:
                name = target
                if isinstance(val, bool):
                    val = "1" if val else ""
            yield (field, "SetText",
                   f"Input={inp}&SelectedName={urllib.parse.quote(name)}&Value={urllib.parse.quote(str(val))}",
                   str(val))

    def _connect(self):
        v = self.cfg["vmix"]
        s = socket.create_connection((v["host"], int(v["port"])), timeout=3)
        s.settimeout(None)
        threading.Thread(target=self._drain, args=(s,), daemon=True).start()
        self.sock = s
        self.last_sent.clear()  # full refresh after (re)connect
        STATUS.vmix_state = f"connected {v['host']}:{v['port']}"
        log(f"vMix TCP API connected ({v['host']}:{v['port']}) -> input '{v['input']}'")

    @staticmethod
    def _drain(s):
        try:
            while s.recv(4096):
                pass
        except OSError:
            pass

    def _close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def run(self):
        latest = None
        while not self.stop.is_set():
            try:
                latest = self.q.get(timeout=0.5)
                while not self.q.empty():
                    latest = self.q.get_nowait()
            except queue.Empty:
                pass
            if latest is None or self.stop.is_set():
                continue
            try:
                if self.sock is None:
                    self._connect()
                for field, fn, query, key in self._commands(latest):
                    if self.last_sent.get(field) == key:
                        continue
                    self.sock.sendall(f"FUNCTION {fn} {query}\r\n".encode("utf-8"))
                    self.last_sent[field] = key
            except OSError as e:
                STATUS.vmix_state = f"error: {e} (retrying)"
                self._close()
                self.stop.wait(2)
        self._close()


class WebhookPusher(threading.Thread):
    """POSTs the latest data as JSON (the same object as /data.obj.json) to webhook.url whenever it changes,
    at most once every webhook.min_interval_ms. Only the newest data is sent; a failed POST is retried."""

    def __init__(self, cfg, stop):
        super().__init__(daemon=True)
        self.cfg, self.stop = cfg, stop
        self.latest: dict | None = None
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.sent = 0

    def submit(self, data: dict):
        with self.lock:
            self.latest = data
        self.event.set()

    def _interval(self) -> float:
        try:
            return max(0.05, int(get_path(self.cfg, "webhook.min_interval_ms", 500)) / 1000)
        except (TypeError, ValueError):
            return 0.5

    def run(self):
        import urllib.request

        url = str(get_path(self.cfg, "webhook.url", "") or "")
        STATUS.webhook_state = f"waiting for data · {url}"
        last_body = None
        while not self.stop.is_set():
            if not self.event.wait(1):
                continue
            self.event.clear()
            with self.lock:
                d = self.latest
            body = json.dumps(d, ensure_ascii=False).encode("utf-8")
            if body == last_body:
                continue
            try:
                req = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Content-Type": "application/json", "User-Agent": "ScoreReader"})
                with urllib.request.urlopen(req, timeout=3) as r:
                    r.read(1024)
                last_body = body
                self.sent += 1
                STATUS.webhook_state = f"ok · {self.sent} sent · last {_dt.datetime.now():%H:%M:%S}"
            except Exception as e:
                STATUS.webhook_state = f"error: {e} (retrying)"
                self.event.set()  # try the newest data again after the wait
                self.stop.wait(2)
                continue
            self.stop.wait(self._interval())


def flatten(d: dict) -> dict:
    """Booleans as "1"/"0", the way data.json/xml/csv and the webhook carry them."""
    return {k: (("1" if v else "0") if isinstance(v, bool) else v) for k, v in d.items()}


_LAN_IP = [0.0, None]


def lan_ip() -> str:
    """This computer's LAN address, for the copy-paste URLs on the dashboard (checked at most every 30 s)."""
    if time.time() - _LAN_IP[0] > 30:
        ip = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("10.255.255.255", 1))  # no packet is sent
                ip = s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            pass
        _LAN_IP[:] = [time.time(), ip if ip and not ip.startswith("127.") else None]
    return _LAN_IP[1] or "localhost"


def write_text_files(folder: str, data: dict, cache: dict):
    os.makedirs(folder, exist_ok=True)
    for k, v in data.items():
        sv = ("1" if v else "") if isinstance(v, bool) else str(v)
        if cache.get(k) != sv:
            with open(os.path.join(folder, f"{k}.txt"), "w", encoding="utf-8") as f:
                f.write(sv)
            cache[k] = sv


# --------------------------------------------------------------------------- web server
DASHBOARD_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>ScoreReader</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0e1116;--card:#171b22;--line:#222a35;--field:#0f1319;--ink:#e8ecf1;--mut:#8a94a3;--ok:#2fbf71;--bad:#e5484d;--warn:#f5a524;--acc:#5b8def}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,Segoe UI,sans-serif}
[hidden]{display:none!important}
header{display:flex;gap:14px;align-items:center;padding:10px 22px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:18px;margin:0}h2{font-size:24px;margin:0 0 6px}h3{font-size:16px;margin:0 0 10px}
.mut{color:var(--mut)}.small{font-size:13px}.mono{font-family:ui-monospace,Consolas,monospace}
.pill{padding:3px 10px;border-radius:99px;background:#222a35;font-size:12px;white-space:nowrap}
.ok{color:var(--ok)}.bad{color:var(--bad)}.okb{background:rgba(47,191,113,.18);color:var(--ok)}.warnb{background:rgba(245,165,36,.18);color:var(--warn)}.badb{background:rgba(229,72,77,.18);color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
button,select,input{font:inherit;color:var(--ink)}
button{background:#222a35;border:1px solid #333d4b;border-radius:8px;padding:7px 13px;cursor:pointer;white-space:nowrap}
button:hover{border-color:#4a5566}button:active{transform:translateY(1px)}
.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.sm{padding:4px 10px;font-size:13px}
input,select{background:var(--field);border:1px solid #333d4b;border-radius:8px;padding:8px 10px;width:100%}
input[type=checkbox]{width:auto}
a{color:var(--acc)}
.tabs{display:flex;gap:4px;margin-left:12px;flex-wrap:wrap}.tabs button{background:none;border:0;color:var(--mut);padding:6px 12px;border-radius:7px}
.tabs button.on{background:#222a35;color:var(--ink)}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex:none;background:#3a4350;vertical-align:middle}
.dot.g{background:var(--ok);box-shadow:0 0 8px var(--ok)}.dot.y{background:var(--warn);box-shadow:0 0 8px var(--warn)}.dot.r{background:var(--bad);box-shadow:0 0 8px var(--bad)}
.row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-top:1px solid var(--line);gap:10px}
.row.first{border-top:0;padding-top:0}
.copy{font:12px ui-monospace,Consolas,monospace;background:var(--field);border:1px solid var(--line);border-radius:6px;padding:5px 8px;display:flex;justify-content:space-between;gap:8px;margin-top:6px;align-items:center}
.copy span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.copy b{color:var(--ok);font-weight:400;cursor:pointer;flex:none}
.sw{width:38px;height:22px;border-radius:11px;background:#333d4b;position:relative;flex:none;cursor:pointer;border:0;padding:0;transition:background .2s}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#cfd6df;transition:left .2s}
.sw.on{background:var(--ok)}.sw.on::after{left:19px;background:#fff}
.banner{background:var(--warn);color:#111;padding:10px 22px;font-weight:600;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.banner button{background:#111;color:#fff;border-color:#111}
main{padding:20px 22px}
/* home */
.home{display:grid;grid-template-columns:minmax(240px,300px) 1fr minmax(280px,340px);gap:18px;align-items:start}
@media(max-width:1100px){.home{grid-template-columns:1fr 1fr}.home .game{grid-column:1/-1;order:-1}}
@media(max-width:700px){.home{grid-template-columns:1fr}main{padding:14px 12px}header{padding:10px 12px}}
.stack{display:flex;flex-direction:column;gap:14px}
.home>*,.stack>*,.two>*,.teams>*{min-width:0}.gn>*{min-width:0}.tn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gn{display:grid;grid-template-columns:1fr auto 1fr;align-items:start;text-align:center;gap:10px}
.tn{font-weight:700;letter-spacing:.04em;display:flex;align-items:center;justify-content:center;gap:8px;min-height:24px}
.tn .arrow{color:var(--warn);font-size:18px;width:14px}
.cbar{height:4px;border-radius:2px;margin:6px auto 0;width:70px;background:#333d4b}
.big{font:700 72px/1 ui-monospace,Consolas,monospace;margin-top:6px}.clock{font:700 56px/1 ui-monospace,Consolas,monospace}
@media(max-width:700px){.big{font-size:52px}.clock{font-size:40px}}
.to{display:flex;gap:5px;justify-content:center;margin-top:10px;min-height:6px}.to i{width:16px;height:6px;border-radius:2px;background:var(--warn)}.to i.u{background:#333d4b}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(108px,1fr));gap:10px;margin-top:18px}
.stat{background:#12161c;border:1px solid var(--line);border-radius:9px;padding:10px;text-align:center}
.stat .v{font:700 22px ui-monospace,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.stat .k{color:var(--mut);font-size:12px}
.fold{cursor:pointer;user-select:none}.fold .chev{display:inline-block;transition:transform .2s;color:var(--mut)}.fold.open .chev{transform:rotate(90deg)}
.foldbody{overflow:hidden;transition:max-height .3s ease;max-height:0}.foldbody.open{max-height:600px}
/* teams */
.teams{display:grid;grid-template-columns:1fr 70px 1fr;gap:18px;align-items:start}
@media(max-width:800px){.teams{grid-template-columns:1fr}}
label.f{display:block;margin-top:14px}label.f>span{display:block;color:var(--mut);font-size:13px;margin-bottom:5px}
.colr{display:flex;gap:8px}.colr input[type=color]{width:46px;padding:2px;height:40px;flex:none}
/* diagnostics */
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:start}@media(max-width:900px){.two{grid-template-columns:1fr}}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mut);font-weight:500}td.v{font-family:ui-monospace,Consolas,monospace}
.grid{font:12px/1.2 ui-monospace,Consolas,monospace;overflow:auto}.grid .r{display:flex;white-space:pre}.grid .n{width:46px;color:var(--mut)}
.c{width:13px;text-align:center;border-left:1px solid #1d232c}.c.hot{background:var(--acc);color:#fff}.c.warm{background:rgba(91,141,239,.3)}
pre{margin:0;max-height:260px;overflow:auto;font-size:12px;color:var(--mut);white-space:pre-wrap}
.tools{display:flex;gap:8px;flex-wrap:wrap}
/* settings */
.setwrap{display:grid;grid-template-columns:220px 1fr;gap:0;min-height:calc(100vh - 140px)}
@media(max-width:800px){.setwrap{grid-template-columns:1fr}.snav{display:flex;flex-wrap:wrap;border-right:0!important}}
.snav{border-right:1px solid var(--line);padding:4px 12px 0 0}.snav button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--mut);padding:9px 12px;border-radius:8px}
.snav button.on{background:#222a35;color:var(--ink)}
.spane{padding:0 0 0 26px;max-width:860px}@media(max-width:800px){.spane{padding:14px 0 0}}
fieldset{border:1px solid var(--line);border-radius:12px;margin:16px 0 0;padding:6px 16px 12px;background:var(--card)}
legend{color:var(--ink);font-weight:600;padding:0 6px}
label.set{display:grid;grid-template-columns:1fr minmax(180px,300px);gap:12px;align-items:center;padding:7px 0}
label.set>input[type=checkbox]{justify-self:end}
.setbar{position:sticky;bottom:0;background:var(--bg);padding:14px 0;display:flex;gap:10px;align-items:center;margin-top:8px}
#setmsg{flex:1;font-size:13px}
/* reveal animation (brand -> sport -> wiring) */
.reveal{display:grid;grid-template-rows:0fr;opacity:0;transform:translateY(-6px);transition:grid-template-rows .4s ease,opacity .3s ease .08s,transform .3s ease .08s}
.reveal>div{overflow:hidden}.reveal.open{grid-template-rows:1fr;opacity:1;transform:none}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.chip{padding:7px 14px;border:1px solid #333d4b;border-radius:99px;background:#12161c;cursor:pointer;opacity:0;transform:translateY(6px);transition:opacity .3s,transform .3s,background .15s,border-color .15s}
.reveal.open .chip{opacity:1;transform:none}.chip.sel{border-color:var(--acc);background:rgba(91,141,239,.18)}
@media(prefers-reduced-motion:reduce){.reveal,.chip,.foldbody,.sw,.sw::after{transition:none!important}}
/* wizard */
#wiz{position:fixed;inset:0;background:var(--bg);z-index:20;overflow:auto}
.steps{display:flex;margin:26px auto 0;max-width:820px;padding:0 12px}.st{flex:1;text-align:center;font-size:13px;color:var(--mut)}
.st b{display:block;margin:0 auto 6px;width:30px;height:30px;border-radius:50%;line-height:30px;background:#222a35;color:var(--mut)}
.st.on b{background:var(--acc);color:#fff}.st.done b{background:var(--ok);color:#06210f}.st.on{color:var(--ink)}
.wiz{max-width:820px;margin:22px auto 40px}
.dev{display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid var(--line);border-radius:10px;margin-top:10px;background:#12161c;cursor:pointer}
.dev.sel{border-color:var(--acc);background:rgba(91,141,239,.08)}.dev .ic{width:42px;height:42px;border-radius:9px;background:#222a35;display:grid;place-items:center;font-size:20px;flex:none}
.dev .t{flex:1;min-width:0}.dev .t div:first-child{font-weight:600}
.wfoot{display:flex;justify-content:space-between;align-items:center;margin-top:22px;gap:12px;flex-wrap:wrap}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--mut);border-top-color:transparent;border-radius:50%;animation:sp 1s linear infinite;vertical-align:-1px}
@keyframes sp{to{transform:rotate(360deg)}}
.live{display:flex;gap:26px;align-items:baseline;font:600 22px ui-monospace,Consolas,monospace;flex-wrap:wrap}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#222a35;border:1px solid #333d4b;padding:8px 16px;border-radius:8px;z-index:30}
</style></head><body>
<header><h1>ScoreReader</h1>
 <nav class="tabs" id="tabs"><button data-t="home" class="on">Home</button><button data-t="teams">Teams</button><button data-t="diag">Diagnostics</button><button data-t="settings">Settings</button></nav>
 <span style="flex:1"></span><span id="hstat" class="mut small"></span><span id="overall" class="pill">…</span></header>
<div class="banner" id="demo" hidden><span id="demotxt"></span><button onclick="goLive()">Back to live data</button></div>

<main>
<!-- ================= HOME ================= -->
<section id="t-home">
<div class="home">
 <div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Scoreboard in</h3><span class="dot" id="indot"></span></div>
  <div style="font-weight:600;margin-top:10px" id="intitle">…</div><div class="mut small" id="indetail"></div>
  <div class="row" style="margin-top:12px"><span class="mut">Console</span><span id="inconsole" style="text-align:right"></span></div>
  <div class="row"><span class="mut">Sport</span><span id="insport"></span></div>
  <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap"><button onclick="openSettings('input')">Change…</button><button onclick="openWizard(1)">Search again</button></div>
 </div>

 <div class="card game"><h3>Game now</h3>
  <div class="gn">
   <div><div class="tn"><span class="arrow" id="hp"></span><span id="hn">HOME</span><span class="arrow"></span></div><div class="cbar" id="hc"></div><div class="big" id="hs">0</div><div class="to" id="hto"></div><div class="mut small" id="htol"></div></div>
   <div style="padding-top:8px"><div class="clock" id="clk">--:--</div><div style="font-size:18px;margin-top:6px" id="per"></div><div class="mut small" id="run"></div></div>
   <div><div class="tn"><span class="arrow"></span><span id="gn">GUEST</span><span class="arrow" id="gp"></span></div><div class="cbar" id="gc"></div><div class="big" id="gs">0</div><div class="to" id="gto"></div><div class="mut small" id="gtol"></div></div>
  </div>
  <div class="stats" id="stats"></div>
 </div>

 <div class="stack">
  <div class="card"><h3>vMix</h3>
   <div class="row first"><span><span class="dot" id="xmldot"></span>&nbsp; Data source (XML)</span><span class="mut small" id="xmlage"></span></div>
   <div class="copy"><span id="xmlurl"></span><b onclick="copyText(xmlurl.textContent)">Copy</b></div>
   <div class="row" style="margin-top:10px"><span><span class="dot" id="stdot"></span>&nbsp; SetText push</span><button class="sw" id="stsw" onclick="toggle('vmix.enabled',this)" title="Push values into a vMix title over the TCP API"></button></div>
   <div class="mut small" id="ststate"></div>
  </div>
  <div class="card">
   <div class="fold" id="netfold" onclick="foldNet()" style="display:flex;justify-content:space-between;align-items:center;gap:8px">
    <h3 style="margin:0">Network outputs</h3><span style="display:flex;align-items:center;gap:8px"><span class="dot" id="netdot"></span><span class="mut small" id="netsum"></span><span class="chev">▸</span></span></div>
   <div class="foldbody" id="netbody"><div style="padding-top:12px">
    <div class="row"><span><span class="dot" id="mqdot"></span>&nbsp; MQTT</span><span class="mut small" id="mqstate"></span></div>
    <div class="copy" id="mqcopyrow"><span id="mqaddr"></span><b onclick="copyText(mqaddr.textContent)">Copy</b></div>
    <div class="row" style="margin-top:8px"><span><span class="dot" id="jsdot"></span>&nbsp; JSON</span><span class="mut small mono" id="jsurl"></span></div>
    <div class="row"><span><span class="dot" id="csdot"></span>&nbsp; CSV</span><span class="mut small mono" id="csurl"></span></div>
    <div class="row"><span><span class="dot" id="whdot"></span>&nbsp; Webhook</span><button class="sw" id="whsw" onclick="toggleWebhook(this)"></button></div>
    <div class="mut small" id="whstate"></div>
    <div style="margin-top:10px"><a href="#" onclick="openSettings('network');return false">Network output settings →</a></div>
   </div></div>
  </div>
 </div>
</div>
</section>

<!-- ================= TEAMS ================= -->
<section id="t-teams" hidden>
<div class="teams">
 <div class="card"><h3>Home</h3>
  <label class="f"><span>Name</span><input id="tm_home_name" placeholder="from the console"></label>
  <label class="f"><span>Abbreviation</span><input id="tm_home_abbr"></label>
  <label class="f"><span>Colour</span><span class="colr"><input type="color" id="tm_home_pick" oninput="tm_home_color.value=this.value.toUpperCase()"><input id="tm_home_color" placeholder="#C8102E" oninput="syncPick('home')"></span></label>
  <div class="mut small" style="margin-top:12px" id="tm_home_hint"></div></div>
 <div style="display:grid;place-items:center;min-height:60px;padding-top:120px"><button onclick="swapTeams()" title="Swap home and guest">⇄</button></div>
 <div class="card"><h3>Guest</h3>
  <label class="f"><span>Name</span><input id="tm_guest_name" placeholder="from the console"></label>
  <label class="f"><span>Abbreviation</span><input id="tm_guest_abbr"></label>
  <label class="f"><span>Colour</span><span class="colr"><input type="color" id="tm_guest_pick" oninput="tm_guest_color.value=this.value.toUpperCase()"><input id="tm_guest_color" placeholder="#6B7A8F" oninput="syncPick('guest')"></span></label>
  <div class="mut small" style="margin-top:12px" id="tm_guest_hint"></div></div>
</div>
<div class="card" style="display:flex;align-items:center;gap:12px;margin-top:18px;flex-wrap:wrap"><span class="mut">Changes apply live (no restart). Blank = what the console sends.</span><span style="flex:1"></span><span id="tmmsg" class="small"></span><button onclick="clearTeams()">Clear</button><button class="primary" onclick="saveTeams()">Save</button></div>
</section>

<!-- ================= DIAGNOSTICS ================= -->
<section id="t-diag" hidden>
<div class="two">
 <div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><h3 style="margin:0">All fields</h3><input id="flt" placeholder="filter…" style="width:180px;padding:5px 9px"></div>
  <div class="mut small" style="margin:6px 0 4px">Names = keys in data.json / data.xml</div><table id="ft"></table></div>
 <div class="stack">
  <div class="card"><h3 id="ptitle">Raw data</h3><div id="cltab"></div><div class="grid" id="grid"></div></div>
  <div class="card"><h3>Tools</h3>
   <div class="tools">
    <button class="sm" id="recbtn" onclick="toggleRecord()">● Record raw bytes</button>
    <button class="sm" onclick="replayPick()">Replay a capture…</button>
    <button class="sm" onclick="postSettings({source:'demo'})">Demo game</button>
    <button class="sm" onclick="reinit()" title="Re-run the ScoreLinkII probe and set-up">Re-init bot</button>
   </div>
   <div id="replaybox" hidden style="margin-top:10px;display:flex;gap:8px"><select id="replaysel"></select><button class="sm primary" onclick="startReplay()">Play</button></div>
   <h3 style="margin-top:16px">Log</h3><pre id="log"></pre>
   <div class="mut small" style="margin-top:10px">Last bytes (hex):</div><pre id="hex" class="mono" style="max-height:120px"></pre>
   <div class="mut small" style="margin-top:10px"><a href="/data.json" target="_blank">data.json</a> · <a href="/data.xml" target="_blank">data.xml</a> · <a href="/data.csv" target="_blank">data.csv</a> · <a href="/api/state" target="_blank">api/state</a></div>
  </div>
 </div>
</div>
</section>

<!-- ================= SETTINGS ================= -->
<section id="t-settings" hidden>
<div class="setwrap">
 <div class="snav" id="snav"></div>
 <div class="spane"><div id="setform"></div><datalist id="portlist"></datalist>
  <div class="setbar"><button class="primary" onclick="saveSettings()">Save</button><span id="setmsg" class="mut"></span><button onclick="openWizard(1)">Run setup again</button></div>
  <div class="mut small">Saved to <span id="cfgpath"></span>. Demo and replay are never saved: ScoreReader always starts with live data.</div>
 </div>
</div>
</section>
</main>

<!-- ================= WIZARD ================= -->
<div id="wiz" hidden>
<header><h1>ScoreReader</h1><span class="mut">Setup</span><span style="flex:1"></span><button onclick="finishWizard(true)">Skip, I'll set it up myself</button></header>
<div class="steps" id="wsteps"></div>
<div class="wiz card" id="wbody"></div>
</div>
<div class="toast" id="toast" hidden></div>

<script>
const H={'X-ScoreReader':'1'};
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
const truthy=v=>v===true||v==='1'||v===1||v==='true';
let ST=null,S=null,TAB='home';
function toast(t){const e=$('toast');e.textContent=t;e.hidden=false;clearTimeout(e._t);e._t=setTimeout(()=>e.hidden=true,2200);}
function copyText(t){(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>toast('Copied'),()=>{const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();try{document.execCommand('copy');toast('Copied')}catch(e){}a.remove();});}
async function postSettings(ch){const r=await fetch('/api/settings',{method:'POST',headers:{...H,'Content-Type':'application/json'},body:JSON.stringify(ch)});const j=await r.json();if(!j.ok)toast(Object.values(j.errors||{}).join(' · ')||'Not saved');S=null;return j;}
async function getSettings(){S=await (await fetch('/api/settings')).json();return S;}
async function reinit(){await fetch('/api/reinit',{method:'POST',headers:H});toast('Re-init requested');}
async function goLive(){await postSettings({source:ST&&ST.live_source||'scorelink'});}
function lsGet(k){try{return localStorage.getItem(k)}catch(e){return null}}function lsSet(k,v){try{localStorage.setItem(k,v)}catch(e){}}

/* ---------- tabs ---------- */
function showTab(t){TAB=t;document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t==t));
 ['home','teams','diag','settings'].forEach(x=>$('t-'+x).hidden=x!=t);
 if(t=='teams')loadTeams();if(t=='settings')loadSettings();lsSet('sr_tab',t);}
$('tabs').onclick=e=>{const b=e.target.closest('button');if(b)showTab(b.dataset.t);};

/* ---------- home ---------- */
const SRCNAME={scorelink:'Sportzcast ScoreBot (USB)',serial:'Serial / USB',tcp:'Network',demo:'Demo game',replay:'Replay'};
function inputTitle(s){if(s.source=='tcp'){const h=s.tcp.host||'';return (s.tcp.port==10001||/scorebridge/i.test(h)?'ScoreBridge · ':'Network · ')+h;}
 if(s.source=='serial')return 'Serial · '+(s.port||'auto');return SRCNAME[s.source]||s.source;}
function ageTxt(a){if(a==null)return 'not read yet';if(a<2)return 'read just now';if(a<90)return 'read '+Math.round(a)+' s ago';if(a<5400)return 'read '+Math.round(a/60)+' min ago';return 'not read lately';}
function readDot(a){return a==null?'':(a<15?'g':'y');}
function setDot(id,c){$(id).className='dot'+(c?' '+c:'');}
const BOXES=[
 {k:'down_distance',l:'Down & distance'},{k:'ball_on',l:'Ball on'},{k:'play_clock',l:'Play clock'},{k:'shot_clock',l:'Shot clock'},
 {k:'timeout_clock',l:'Timeout clock'},
 {f:d=>d.balls!==undefined&&(d.balls+d.strikes+d.outs)!==''?`${d.balls||0}-${d.strikes||0}-${d.outs||0}`:null,l:'Balls-Strikes-Outs'},
 {p:'shots',l:'Shots'},{p:'corners',l:'Corners'},{p:'saves',l:'Saves'},{p:'fouls',l:'Fouls'},{p:'hits',l:'Hits'},{p:'errors',l:'Errors'},{p:'penalties',l:'Penalties'},
 {f:d=>'home_bonus' in d?(truthy(d.home_double_bonus)||truthy(d.guest_double_bonus)?'Double ':'')+([truthy(d.home_bonus)?abbr(d,'home'):'',truthy(d.guest_bonus)?abbr(d,'guest'):''].filter(Boolean).join(' · ')||'—'):null,l:'Bonus'},
 {f:d=>'possession' in d&&'down_distance' in d?(d.possession=='H'?abbr(d,'home'):d.possession=='V'?abbr(d,'guest'):'—'):null,l:'Possession'},
];
function abbr(d,s){return d[s+'_abbr']||d[s+'_name']||s.toUpperCase();}
function timeouts(el,lab,v,max){const n=parseInt(v,10);if(isNaN(n)){el.innerHTML='';$(lab).textContent=v?v+' timeouts':'';return;}
 el.innerHTML=Array.from({length:Math.max(n,max)},(_,i)=>`<i class="${i<n?'':'u'}"></i>`).join('');$(lab).textContent=n+' timeout'+(n==1?'':'s');}
function renderGame(d){
 $('hn').textContent=d.home_name||'HOME';$('gn').textContent=d.guest_name||'GUEST';
 $('hc').style.background=d.home_color||'';$('gc').style.background=d.guest_color||'';
 $('hs').textContent=d.home_score??'0';$('gs').textContent=d.guest_score??'0';
 $('hp').textContent=truthy(d.home_poss)?'◀':'';$('gp').textContent=truthy(d.guest_poss)?'▶':'';
 $('clk').textContent=d.clock||'--:--';
 const pt=d.period_text&&!/^\d+$/.test(d.period_text)?d.period_text:'';
 $('per').textContent=pt||(d.period_ordinal?d.period_ordinal+({football:' quarter',basketball:'',soccer:' half'}[d.sport]||''):'');
 $('run').innerHTML='clock_running' in d?(truthy(d.clock_running)?'clock running <span class="dot g" style="width:8px;height:8px"></span>':'clock stopped'):'';
 const mx=Math.max(parseInt(d.home_timeouts)||0,parseInt(d.guest_timeouts)||0,3);
 timeouts($('hto'),'htol',d.home_timeouts,mx);timeouts($('gto'),'gtol',d.guest_timeouts,mx);
 let h='';for(const b of BOXES){let v=null;
  if(b.k){if(b.k in d&&String(d[b.k]).trim()!=='')v=d[b.k];}
  else if(b.p){const a=d['home_'+b.p],c=d['guest_'+b.p];if((a??'')!==''||(c??'')!=='')v=`${a||0} – ${c||0}`;}
  else v=b.f(d);
  if(v!=null)h+=`<div class="stat"><div class="v" title="${esc(v)}">${esc(v)}</div><div class="k">${b.l}</div></div>`;}
 $('stats').innerHTML=h;}
function renderHome(s){
 const o=s.outputs,base=`http://${o.lan_ip}:${o.http_port}`;
 const age=s.packet_age,live=s.source_state=='connected'&&age!=null&&age<10;
 setDot('indot',s.source_state=='error'?'r':live?'g':'y');
 $('intitle').textContent=inputTitle(s);
 $('indetail').textContent=s.source_state=='error'?s.source_detail:(s.source_state+(age!=null?' · last data '+(age<1?age.toFixed(1):Math.round(age))+' s ago':' · no data yet'));
 $('inconsole').textContent=s.cl_table&&s.cl_table.length&&!s.vendor_mode?'Daktronics (current loop)':s.vendor_mode?s.scoreboard:(s.packets?'Daktronics (RTD)':'—');
 $('insport').textContent=(s.data.sport||s.sport||'')+(s.vendor_mode?'':' (auto)');
 renderGame(s.data);
 $('xmlurl').textContent=base+'/data.xml';$('xmlage').textContent=ageTxt(o.xml.age);setDot('xmldot',readDot(o.xml.age));
 $('stsw').classList.toggle('on',o.vmix.enabled);
 setDot('stdot',!o.vmix.enabled?'':o.vmix.state.startsWith('connected')?'g':'r');
 $('ststate').textContent=o.vmix.enabled?`${o.vmix.state} · input "${o.vmix.input}"`:'';
 // network outputs
 const m=o.mqtt;const mqc=!m.enabled?'':m.state.startsWith('error')?'r':(m.state.startsWith('listening')||m.state.startsWith('connected'))?'g':'y';
 setDot('mqdot',mqc);
 $('mqstate').textContent=!m.enabled?(m.configured?'on after a restart':'off'):(m.clients+' client'+(m.clients==1?'':'s'));
 $('mqcopyrow').hidden=!m.enabled;
 $('mqaddr').textContent=m.mode=='client'?`→ ${m.broker_host}:${m.broker_port} · bot ${m.bot}`:`${o.lan_ip}:${m.tcp_port}${m.ws_port?' · ws :'+m.ws_port:''} · bot ${m.bot}`;
 const ja=Math.min(o.json.age??1e9,o.json_obj.age??1e9);const jage=ja==1e9?null:ja;
 setDot('jsdot',readDot(jage));setDot('csdot',readDot(o.csv.age));
 $('jsurl').textContent=':'+o.http_port+'/data.json';$('csurl').textContent=':'+o.http_port+'/data.csv';
 $('jsurl').title=ageTxt(jage);$('csurl').title=ageTxt(o.csv.age);
 const w=o.webhook;$('whsw').classList.toggle('on',w.enabled);setDot('whdot',!w.enabled?'':w.state.startsWith('error')?'r':w.state.startsWith('ok')?'g':'y');
 $('whstate').textContent=w.enabled?w.state:'';
 const on=[m.enabled&&'MQTT',jage!=null&&'JSON',o.csv.age!=null&&'CSV',w.enabled&&'Webhook'].filter(Boolean);
 $('netsum').textContent=on.length?on.join(' · '):'none in use';
 const bad=(m.enabled&&mqc=='r')||(w.enabled&&w.state.startsWith('error'));
 setDot('netdot',bad?'r':on.length?'g':'');
 // header
 const problems=[s.source_state=='error'&&'scoreboard',!live&&s.source_state!='error'&&'no data',o.vmix.enabled&&!o.vmix.state.startsWith('connected')&&'vMix',bad&&'network output'].filter(Boolean);
 const ov=$('overall');ov.textContent=problems.length?'Check: '+problems.join(', '):'All good';ov.className='pill '+(problems.length?(s.source_state=='error'?'badb':'warnb'):'okb');
}
function foldNet(open){const f=$('netfold'),b=$('netbody');const o=open??!b.classList.contains('open');b.classList.toggle('open',o);f.classList.toggle('open',o);lsSet('sr_net',o?'1':'0');}
async function toggle(path,btn){const on=!btn.classList.contains('on');btn.classList.toggle('on',on);await postSettings({[path]:on});}
async function toggleWebhook(btn){const on=!btn.classList.contains('on');
 if(on&&!(S||await getSettings()).values['webhook.url']){toast('Set the webhook URL first');openSettings('network');return;}
 await toggle('webhook.enabled',btn);}

/* ---------- teams ---------- */
const TM=['home_name','home_abbr','home_color','guest_name','guest_abbr','guest_color'];
async function loadTeams(){await getSettings();TM.forEach(k=>$('tm_'+k).value=S.values['teams.'+k]||'');syncPick('home');syncPick('guest');$('tmmsg').textContent='';
 const d=ST&&ST.data||{};}
function syncPick(side){const v=$('tm_'+side+'_color').value;if(/^#[0-9a-f]{6}$/i.test(v))$('tm_'+side+'_pick').value=v;}
function swapTeams(){for(const k of ['name','abbr','color']){const a=$('tm_home_'+k),b=$('tm_guest_'+k);[a.value,b.value]=[b.value,a.value];}syncPick('home');syncPick('guest');$('tmmsg').textContent='Swapped: press Save';}
function clearTeams(){TM.forEach(k=>$('tm_'+k).value='');$('tmmsg').textContent='Cleared: press Save';}
async function saveTeams(){const ch={};TM.forEach(k=>ch['teams.'+k]=$('tm_'+k).value.trim());const r=await postSettings(ch);
 $('tmmsg').className='small '+(r.ok?'ok':'bad');$('tmmsg').textContent=r.ok?'Saved':Object.values(r.errors).join(' · ');}

/* ---------- diagnostics ---------- */
function renderDiag(s){
 const d=s.data,f=$('flt').value.toLowerCase(),meta={};s.fields.forEach(m=>meta[m.name]=m);
 let h='<tr><th>key</th><th>value</th><th>from</th></tr>';
 for(const [k,v] of Object.entries(d)){if(f&&!k.includes(f))continue;const m=meta[k];if(m&&(v===''||v===false)&&!f)continue;
  h+=`<tr><td>${esc(k)}</td><td class="v">${esc(v)}</td><td class="mut">${m?'pos '+m.pos+'+'+m.len:'derived'}</td></tr>`;}
 $('ft').innerHTML=h;
 if(s.contract&&s.contract.length){$('grid').innerHTML='';$('ptitle').textContent=s.protocol+' · decoded fields';
  $('cltab').innerHTML='<table><tr><th>field</th><th>value</th></tr>'+s.contract.map(r=>`<tr><td>${esc(r.k)}</td><td class="v">${esc(r.v).replace(/ /g,'·')}</td></tr>`).join('')+'</table>';}
 else if(s.cl_table.length){$('grid').innerHTML='';$('ptitle').textContent=s.protocol+' · console addresses';
  let t='<div class="mut small" style="margin-bottom:6px">Blue = changed in the last second</div><table><tr><th>addr</th><th>len</th><th>7-seg digits</th><th>age</th><th>hex</th></tr>';
  s.cl_table.filter(r=>r.addr<0xE0).forEach(r=>{t+=`<tr><td>${r.addr}</td><td>${r.len}</td><td class="v" style="${r.age<1?'background:rgba(91,141,239,.3)':''}">${esc(r.digits).replace(/ /g,'·')}</td><td class="mut">${r.age}s</td><td class="mut v" style="font-size:11px">${esc(r.hex)}</td></tr>`});
  $('cltab').innerHTML=t+'</table>';}
 else{$('cltab').innerHTML='';$('ptitle').textContent='Raw RTD buffer (1-based positions; blue = just changed)';
  const raw=s.raw,ch=s.changed_ago,W=50;let last=0,g='';for(let i=0;i<raw.length;i++)if(raw[i]!=' ')last=i;
  for(let r=0;r<=last;r+=W){g+=`<div class="r"><span class="n">${r+1}</span>`;
   for(let i=r;i<Math.min(r+W,raw.length);i++){const a=ch[i],cls=a<1?'hot':(a<10?'warm':'');const c=raw[i]==' '?'&nbsp;':esc(raw[i]);g+=`<span class="c ${cls}" title="pos ${i+1}">${c}</span>`;}g+='</div>';}
  $('grid').innerHTML=g;}
 const lg=$('log'),atEnd=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;lg.textContent=s.logs.slice(-80).join('\n');if(atEnd)lg.scrollTop=1e9;
 $('hex').textContent=s.recent_hex;
 const rb=$('recbtn');rb.textContent=s.record_raw?'■ Stop recording raw bytes':'● Record raw bytes';rb.classList.toggle('primary',s.record_raw);
}
async function toggleRecord(){await postSettings({record_raw:!ST.record_raw});}
async function replayPick(){await getSettings();const c=S.choices.replay_file||[];const b=$('replaybox');
 if(!c.length){toast('No captures in logs/ yet: turn on Record raw bytes first');return;}
 $('replaysel').innerHTML=c.map(x=>`<option>${esc(x)}</option>`).join('');b.hidden=!b.hidden;}
async function startReplay(){await postSettings({source:'replay',replay_file:$('replaysel').value});$('replaybox').hidden=true;}

/* ---------- settings ---------- */
const SECTIONS=[['input','Scoreboard input',['Data source','Scoreboard']],['vmix','vMix',['vMix']],['network','Network outputs',['MQTT','Webhook']],['files','Files',['Files']],['advanced','Advanced',['Advanced']]];
const SRC={scorelink:'ScoreBot / ScoreLinkII (USB)',serial:'Serial or USB (incl. ScoreBridge on USB, 115200)',tcp:'Network (ScoreBridge, serial server)',demo:'Demo: simulated game (not saved)',replay:'Replay a capture (not saved)'};
const SHOW={port:['scorelink','serial'],baud:['scorelink','serial'],'scorelink.probe_on_start':['scorelink'],'scorelink.send_config_on_start':['scorelink'],'tcp.host':['tcp'],'tcp.port':['tcp'],replay_file:['replay']};
const HIDE=new Set(['setup_done','output_all_fields']);let SEC=lsGet('sr_sec')||'input';
function sid(p){return 'set_'+p.replace(/\./g,'_');}
function openSettings(sec){if(sec){SEC=sec;lsSet('sr_sec',sec);}showTab('settings');}
function brandOf(pid){for(const b of S.scoreboards)if(b.sports.some(x=>x.id==pid))return b;return null;}
function sbPicker(){const pid=S.values['scoreboard.protocol']||'',b=brandOf(pid);
 return `<label class="set"><span>Scoreboard brand</span><select id="sb_brand" onchange="sbBrand()"><option value="">Daktronics All Sport 5000 (detects the sport itself)</option>`+
  S.scoreboards.map(x=>`<option ${b&&b.name==x.name?'selected':''}>${esc(x.name)}</option>`).join('')+'</select></label>'+
  `<div class="reveal" id="sb_rv"><div><label class="set"><span>Sport / console layout</span><select id="${sid('scoreboard.protocol')}"></select></label>`+
  `<label class="set" id="sb_conn_l"><span>Wiring</span><select id="${sid('scoreboard.connection')}"></select></label></div></div>`;}
function sbBrand(keep){const bn=$('sb_brand').value,b=S.scoreboards.find(x=>x.name==bn);
 const sp=$(sid('scoreboard.protocol')),cn=$(sid('scoreboard.connection'));
 sp.innerHTML=b?b.sports.map(x=>`<option value="${esc(x.id)}">${esc(x.description)}</option>`).join(''):'<option value=""></option>';
 cn.innerHTML=b?b.connections.map((c,i)=>`<option value="${i}">${esc(c)}</option>`).join(''):'<option value="0"></option>';
 if(keep){sp.value=S.values['scoreboard.protocol']||'';cn.value=String(S.values['scoreboard.connection']||0);}
 $('sb_rv').classList.toggle('open',!!b);$('sb_conn_l').hidden=!b||b.connections.length<2;showFor();}
function inputFor(sp){const v=S.values[sp.path],id=sid(sp.path);
 if(sp.type=='bool')return `<input type="checkbox" id="${id}" ${v?'checked':''}>`;
 if(sp.type=='choice'){const ch=S.choices[sp.path]||[];if(!ch.length)return '<span class="mut">none yet</span>';
  return `<select id="${id}">`+ch.map(c=>`<option value="${esc(c)}" ${c==v?'selected':''}>${esc(sp.path=='source'?(SRC[c]||c):(sp.path=='mqtt.mode'?{broker:'Built-in broker (others connect to this computer)',client:'Publish to another broker'}[c]||c:c))}</option>`).join('')+'</select>';}
 if(sp.type=='color')return `<span class="colr"><input type="color" value="${esc(v||'#000000')}" oninput="${id}.value=this.value.toUpperCase()"><input id="${id}" value="${esc(v)}" placeholder="#1E3A8A"></span>`;
 return `<input id="${id}" value="${esc(v)}"${sp.path=='port'?' list="portlist"':''}${sp.type=='int'||sp.type=='port'?' inputmode="numeric"':''}${sp.type=='url'?' placeholder="http://…"':''}>`;}
function setRow(sp){return `<label class="set" data-path="${sp.path}"><span>${esc(sp.label)}${sp.apply=='restart'?' <em class="mut small">(after restart)</em>':''}</span>${inputFor(sp)}</label>`;}
async function loadSettings(){await getSettings();renderSettings();}
function renderSettings(){
 $('snav').innerHTML=SECTIONS.map(([k,l])=>`<button data-s="${k}" class="${k==SEC?'on':''}">${l}</button>`).join('');
 $('snav').onclick=e=>{const b=e.target.closest('button');if(!b)return;SEC=b.dataset.s;lsSet('sr_sec',SEC);document.querySelectorAll('#snav button').forEach(x=>x.classList.toggle('on',x==b));document.querySelectorAll('.spn').forEach(p=>p.hidden=p.dataset.s!=SEC);};
 const by={};S.spec.forEach(sp=>(by[sp.group]=by[sp.group]||[]).push(sp));
 const base=`http://${(ST&&ST.outputs.lan_ip)||location.hostname}:${S.values.http_port}`;
 let h='';
 for(const [k,l,groups] of SECTIONS){h+=`<div class="spn" data-s="${k}" ${k==SEC?'':'hidden'}><h2>${l}</h2>`;
  if(k=='input')h+='<div class="mut">Where the scoreboard data comes from. <a href="#" onclick="openWizard(1);return false">Search for devices</a></div>';
  if(k=='vmix')h+=`<div class="mut">vMix reads the score as a Data Source: Data Sources → Add → XML →</div><div class="copy" style="max-width:520px"><span>${esc(base)}/data.xml</span><b onclick="copyText('${esc(base)}/data.xml')">Copy</b></div>`;
  if(k=='network')h+='<div class="mut">Other apps that read the score over the network. vMix has its own section.</div>';
  for(const g of groups){const list=(by[g]||[]).filter(sp=>!HIDE.has(sp.path));if(!list.length)continue;
   h+=`<fieldset><legend>${esc(g=='Data source'?'Source':g)}</legend>`;
   if(g=='MQTT')h+='<div class="mut small">ScoreConnect-compatible. OpenStatsEngine, and anything that used ScoreConnect, reads this.</div>';
   list.forEach(sp=>{if(sp.path=='scoreboard.protocol'){h+=sbPicker();return;}if(sp.path=='scoreboard.connection')return;h+=setRow(sp);});
   if(g=='Files')h+=`<p class="mut small" style="margin:4px 0">Text files go to ${esc(S.text_folder)}</p>`;
   h+='</fieldset>';
   if(g=='MQTT'){const oa=S.spec.find(x=>x.path=='output_all_fields');
    h+=`<fieldset><legend>JSON / CSV</legend><div class="mut small">Always on.</div><div class="copy" style="max-width:520px"><span>${esc(base)}/data.json</span><b onclick="copyText('${esc(base)}/data.json')">Copy</b></div><div class="copy" style="max-width:520px"><span>${esc(base)}/data.csv</span><b onclick="copyText('${esc(base)}/data.csv')">Copy</b></div>${oa?setRow(oa):''}</fieldset>`;}
  }
  h+='</div>';}
 $('setform').innerHTML=h;$('cfgpath').textContent=S.config_path;$('setmsg').textContent='';$('setmsg').className='mut';
 $('portlist').innerHTML='<option value="auto">auto-detect</option>'+S.ports.map(p=>`<option value="${esc(p.device)}">${esc(p.description)}${p.scorelink?' (ScoreBot)':''}</option>`).join('');
 const src=$(sid('source'));if(src)src.onchange=showFor;const mm=$(sid('mqtt.mode'));if(mm)mm.onchange=showFor;sbBrand(true);}
const MQSHOW={'mqtt.tcp_port':'broker','mqtt.ws_port':'broker','mqtt.broker_host':'client','mqtt.broker_port':'client'};
function showFor(){const src=($(sid('source'))||{}).value,bp=$('sb_brand'),mm=($(sid('mqtt.mode'))||{}).value;
 document.querySelectorAll('label.set[data-path]').forEach(l=>{const p=l.dataset.path,w=SHOW[p];l.hidden=(!!w&&!w.includes(src))||(p=='sport'&&bp&&bp.value!='')||(MQSHOW[p]&&MQSHOW[p]!=mm);});}
async function saveSettings(){const ch={};
 S.spec.forEach(sp=>{const el=$(sid(sp.path));if(!el)return;const lab=el.closest('label');if(lab&&lab.hidden&&sp.path!='source'&&sp.path!='scoreboard.protocol')return;
  let v=sp.type=='bool'?el.checked:el.value;if(sp.path=='scoreboard.protocol'&&$('sb_brand').value=='')v='';
  if(String(v)!==String(S.values[sp.path]??''))ch[sp.path]=v;});
 if(!Object.keys(ch).length){$('setmsg').textContent='Nothing changed.';return;}
 const r=await postSettings(ch);
 if(!r.ok){$('setmsg').className='bad';$('setmsg').textContent=Object.values(r.errors).join(' · ');return;}
 await getSettings();renderSettings();
 $('setmsg').className='ok';$('setmsg').textContent='Saved.'+(r.restart_needed.length?' Restart ScoreReader to apply: '+r.restart_needed.join(', '):'');}

/* ---------- setup wizard ---------- */
const WSTEPS=['Connect','Scoreboard','vMix','Network outputs','Done'];let W={step:1,devs:null,sel:null,other:false};
async function openWizard(step){W={step:step||1,devs:null,sel:null,other:false,brand:null,proto:'',conn:0};await getSettings();$('wiz').hidden=false;wRender();if(W.step==1)wSearch();}
async function finishWizard(skip){await postSettings({setup_done:true});$('wiz').hidden=true;if(!skip)showTab('home');}
function wGo(n){W.step=n;wRender();}
function wSteps(){$('wsteps').innerHTML=WSTEPS.map((t,i)=>`<div class="st ${i+1<W.step?'done':i+1==W.step?'on':''}"><b>${i+1<W.step?'✓':i+1}</b>${t}</div>`).join('');}
async function wSearch(){W.devs=null;W.sel=null;wRender();
 try{W.devs=(await (await fetch('/api/discover')).json()).devices;}catch(e){W.devs=[];}
 if(W.devs.length&&W.sel==null)W.sel=0;if(W.step==1)wRender();}
const ICON={scorebridge:'⇄',scorebridge_usb:'⌁',scorelink:'⌁',serial:'⌁'};
function wRender(){wSteps();const b=$('wbody');const s=W.step;
 if(s==1){const d=W.devs;
  let h=`<h2>Where does the scoreboard data come from?</h2><div class="mut">`+(d==null?'<span class="spin"></span> Looking on the network and USB…':`${d.length?'<span class="ok">'+d.length+' found</span>':'Nothing found'} &nbsp;<button class="sm" onclick="wSearch()">Search again</button>`)+'</div>';
  (d||[]).forEach((x,i)=>{h+=`<div class="dev ${W.sel===i&&!W.other?'sel':''}" onclick="W.sel=${i};W.other=false;wRender()"><div class="ic">${ICON[x.kind]||'⌁'}</div><div class="t"><div>${esc(x.title)}</div><div class="mut small">${esc(x.detail)}${x.receiving?'':''}</div></div>${i==0&&x.kind=='scorebridge'&&x.receiving?'<span class="pill okb">Recommended</span>':''}</div>`;});
  h+=`<div class="dev ${W.other?'sel':''}" onclick="W.other=true;wRender()"><div class="ic">＋</div><div class="t"><div>Other</div><div class="mut small">Type a network address, pick a serial port, or try the demo game</div></div></div>`;
  h+=`<div class="reveal ${W.other?'open':''}"><div><div style="padding:6px 4px 0">
   <label class="f"><span>Connection</span><select id="w_os" onchange="wOther()"><option value="tcp">Network address (ScoreBridge, serial-to-Ethernet box)</option><option value="serial">Serial port (USB-serial cable, ScoreBridge on USB)</option><option value="scorelink">Sportzcast ScoreBot (ScoreLinkII), auto-detect</option><option value="demo">Demo game (not saved)</option></select></label>
   <div id="w_tcp" style="display:flex;gap:10px"><label class="f" style="flex:3"><span>Address</span><input id="w_host" placeholder="scorebridge.local or 192.168.1.50" value="${esc(S.values['tcp.host']||'')}"></label><label class="f" style="flex:1"><span>Port</span><input id="w_port" value="${esc(S.values['tcp.port']||10001)}"></label></div>
   <div id="w_ser" hidden style="display:flex;gap:10px"><label class="f" style="flex:3"><span>Port</span><select id="w_sp">${S.ports.map(p=>`<option value="${esc(p.device)}">${esc(p.device)} · ${esc(p.description)}</option>`).join('')||'<option value="">no serial ports found</option>'}</select></label><label class="f" style="flex:1"><span>Baud</span><select id="w_baud"><option>115200</option><option>19200</option><option>9600</option><option>38400</option></select></label></div>
  </div></div></div>`;
  const chosen=W.other||(d&&d.length&&W.sel!=null);
  h+=`<div class="wfoot"><span class="mut small">A ScoreBridge is found by its name on the network (mDNS), then by a quick scan for port 10001.</span><button class="primary" ${chosen?'':'disabled'} onclick="wUse()">${W.other?'Use this':d&&W.sel!=null&&d[W.sel]?'Use '+esc(d[W.sel].title.split(' · ')[0]):'Next'} →</button></div>`;
  b.innerHTML=h;if(W.other)wOther();return;}
 if(s==2){const pid=S.values['scoreboard.protocol']||'';const cur=W.brand??(brandOf(pid)||{}).name??'';W.brand=cur;if(!W.proto)W.proto=pid;
  const br=S.scoreboards.find(x=>x.name==cur);
  let h=`<h2>Which scoreboard is it?</h2><div class="mut">A Daktronics console names its sport by itself; other brands need it picked.</div>
  <label class="f"><span>Brand</span><select id="w_brand" onchange="wBrand(this.value)"><option value="">Daktronics All Sport 5000 (detects the sport itself)</option>${S.scoreboards.map(x=>`<option ${x.name==cur?'selected':''}>${esc(x.name)}</option>`).join('')}</select></label>
  <div class="reveal" id="w_rv"><div><label class="f"><span>Sport</span></label><div class="chips" id="w_chips"></div>
   <div class="reveal" id="w_modrv"><div><label class="f"><span>Console model / layout</span><select id="w_model" onchange="W.proto=this.value"></select></label></div></div>
   <div id="w_connw"><label class="f"><span>Wiring</span><select id="w_conn" onchange="W.conn=+this.value"></select></label></div></div></div>
  <div id="w_dak" style="margin-top:14px"></div>
  <div style="margin-top:18px"><h3>Live check</h3><div class="live" id="w_live"></div><div class="mut small" style="margin-top:4px">Change the clock or score on the console: this should move.</div></div>
  <div class="wfoot"><button onclick="wGo(1)">← Back</button><button class="primary" onclick="wSaveBoard()">Next →</button></div>`;
  b.innerHTML=h;wBrand(cur,true);wLive();return;}
 if(s==3){const o=ST.outputs,url=`http://${o.lan_ip}:${o.http_port}/data.xml`,on=!!S.values['vmix.enabled'];
  b.innerHTML=`<h2>Connect vMix</h2><div class="mut">In vMix: <b style="color:var(--ink)">Settings → Data Sources → Add → XML</b>, paste this address, and link the title fields to the values you want.</div>
  <div class="copy" style="font-size:14px;padding:8px 10px;margin-top:12px"><span>${esc(url)}</span><b onclick="copyText('${esc(url)}')">Copy</b></div>
  <div class="mut small" style="margin-top:8px" id="w_xml"></div>
  <div class="row" style="margin-top:18px"><span><b>Also push values with SetText</b><div class="mut small">Optional: sends each value straight into one vMix title over the TCP API.</div></span><button class="sw ${on?'on':''}" id="w_stsw" onclick="this.classList.toggle('on');$('w_strv').classList.toggle('open',this.classList.contains('on'))"></button></div>
  <div class="reveal ${on?'open':''}" id="w_strv"><div><div style="display:flex;gap:10px"><label class="f" style="flex:2"><span>vMix computer</span><input id="w_vh" value="${esc(S.values['vmix.host']||'127.0.0.1')}"></label><label class="f" style="flex:2"><span>Title input name or number</span><input id="w_vi" value="${esc(S.values['vmix.input']||'')}"></label></div></div></div>
  <div class="wfoot"><button onclick="wGo(2)">← Back</button><button class="primary" onclick="wSaveVmix()">Next →</button></div>`;wLive();return;}
 if(s==4){const o=ST.outputs,m=!!S.values['mqtt.enabled'],wh=!!S.values['webhook.enabled'];
  b.innerHTML=`<h2>Network outputs</h2><div class="mut">For other apps on the network. Leave them as they are if you're not sure.</div>
  <div class="row" style="margin-top:16px"><span><b>MQTT</b><div class="mut small">ScoreConnect-compatible: OpenStatsEngine and anything that used ScoreConnect read this.</div></span><button class="sw ${m?'on':''}" id="w_mq" onclick="this.classList.toggle('on');$('w_mqrv').classList.toggle('open',this.classList.contains('on'))"></button></div>
  <div class="reveal ${m?'open':''}" id="w_mqrv"><div><div style="display:flex;gap:10px;align-items:flex-end"><label class="f" style="flex:1"><span>Bot number</span><input id="w_bot" value="${esc(S.values['mqtt.bot_number'])}"></label>
   <div style="flex:3"><div class="mut small">Point the other app at</div><div class="copy"><span>${esc(o.lan_ip)}:${esc(S.values['mqtt.tcp_port'])} (WebSocket :${esc(S.values['mqtt.ws_port'])})</span><b onclick="copyText('${esc(o.lan_ip)}')">Copy</b></div></div></div></div></div>
  <div class="row" style="margin-top:12px"><span><b>JSON / CSV</b><div class="mut small">Always on: :${o.http_port}/data.json and /data.csv</div></span><span class="pill okb">on</span></div>
  <div class="row" style="margin-top:4px"><span><b>Webhook</b><div class="mut small">POSTs the data as JSON to a URL on every change.</div></span><button class="sw ${wh?'on':''}" id="w_wh" onclick="this.classList.toggle('on');$('w_whrv').classList.toggle('open',this.classList.contains('on'))"></button></div>
  <div class="reveal ${wh?'open':''}" id="w_whrv"><div><label class="f"><span>URL</span><input id="w_whurl" placeholder="http://…" value="${esc(S.values['webhook.url']||'')}"></label></div></div>
  <div class="mut small" id="w_msg" style="margin-top:10px"></div>
  <div class="wfoot"><button onclick="wGo(3)">← Back</button><button class="primary" onclick="wSaveNet()">Next →</button></div>`;return;}
 if(s==5){b.innerHTML=`<h2>All set</h2><div class="mut">You can change any of this later in Settings, or run the setup again from there.</div><div id="w_sum" style="margin-top:14px"></div>
  <div class="wfoot"><button onclick="wGo(4)">← Back</button><button class="primary" onclick="finishWizard()">Go to the dashboard →</button></div>`;wLive();}
}
function wOther(){const v=$('w_os').value;$('w_tcp').hidden=v!='tcp';$('w_ser').hidden=v!='serial';}
async function wUse(){let ch;
 if(W.other){const v=$('w_os').value;
  ch=v=='tcp'?{source:'tcp','tcp.host':$('w_host').value.trim(),'tcp.port':$('w_port').value.trim()}:v=='serial'?{source:'serial',port:$('w_sp').value,baud:$('w_baud').value}:v=='scorelink'?{source:'scorelink',port:'auto'}:{source:'demo'};
  if(v=='tcp'&&!ch['tcp.host']){toast('Type the address');return;}}
 else ch=W.devs[W.sel].settings;
 const r=await postSettings(ch);if(r.ok){await getSettings();wGo(2);}}
function wEvents(br){const ev={};br.sports.forEach(x=>(ev[x.event||'Other']=ev[x.event||'Other']||[]).push(x));return ev;}
function wBrand(name,first){W.brand=name;const br=S.scoreboards.find(x=>x.name==name);const rv=$('w_rv');
 if(br){if(!br.sports.some(x=>x.id==W.proto))W.proto=br.sports.find(x=>/football/i.test(x.event||''))?.id||br.sports[0].id;
  const ev=wEvents(br),cur=(br.sports.find(x=>x.id==W.proto)||{}).event||'Other';
  $('w_chips').innerHTML=Object.keys(ev).map((e,i)=>`<span class="chip ${e==cur?'sel':''}" data-e="${esc(e)}" style="transition-delay:${Math.min(i,12)*45}ms" onclick="wEvent(this.dataset.e)">${esc(e.replace(/([a-z])([A-Z])/g,'$1 $2'))}</span>`).join('');
  $('w_conn').innerHTML=br.connections.map((c,i)=>`<option value="${i}">${esc(c)}</option>`).join('');
  if(!first)W.conn=0;$('w_conn').value=String(W.conn||0);$('w_connw').hidden=br.connections.length<2;wModels(br,cur,true);}
 else W.proto='';
 if(first&&br){rv.style.transition='none';rv.offsetHeight;rv.classList.add('open');requestAnimationFrame(()=>rv.style.transition='');}
 else{rv.classList.remove('open');if(br){rv.querySelectorAll('.chip').forEach(c=>c.style.opacity);requestAnimationFrame(()=>requestAnimationFrame(()=>rv.classList.add('open')));}}
 $('w_dak').hidden=!!br;wDak();}
function wModels(br,e,keep){const list=wEvents(br)[e]||[];const strip=d=>d.replace(new RegExp('^'+br.name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\s+','i'),'');
 if(!keep||!list.some(x=>x.id==W.proto))W.proto=list[0]?list[0].id:'';
 $('w_model').innerHTML=list.map(x=>`<option value="${esc(x.id)}" ${x.id==W.proto?'selected':''}>${esc(strip(x.description))}</option>`).join('');
 $('w_modrv').classList.toggle('open',list.length>1);}
function wEvent(e){const br=S.scoreboards.find(x=>x.name==W.brand);document.querySelectorAll('#w_chips .chip').forEach(c=>c.classList.toggle('sel',c.dataset.e==e));wModels(br,e,false);}
function wDak(){const el=$('w_dak');if(!el||el.hidden||!ST)return;
 el.innerHTML=ST.cl_table&&ST.cl_table.length?`<span class="ok">✓</span> Sport detected: <b>${esc(ST.data.sport||ST.sport)}</b>`:
  `<span class="mut">Waiting for the console to say which sport it is. If it never does (RTD port), pick it:</span> <select id="w_sport" style="width:auto;margin-left:6px">${(S.choices.sport||[]).map(x=>`<option ${x==S.values.sport?'selected':''}>${esc(x)}</option>`).join('')}</select>`;}
async function wSaveBoard(){const ch={'scoreboard.protocol':W.proto||''};if(W.proto)ch['scoreboard.connection']=W.conn||0;
 const sp=$('w_sport');if(!W.proto&&sp)ch.sport=sp.value;
 const r=await postSettings(ch);if(r.ok){await getSettings();wGo(3);}}
async function wSaveVmix(){const on=$('w_stsw').classList.contains('on');const ch={'vmix.enabled':on};if(on){ch['vmix.host']=$('w_vh').value.trim();ch['vmix.input']=$('w_vi').value.trim();}
 const r=await postSettings(ch);if(r.ok){await getSettings();wGo(4);}}
async function wSaveNet(){const m=$('w_mq').classList.contains('on'),wh=$('w_wh').classList.contains('on');
 const ch={'mqtt.enabled':m,'webhook.enabled':wh};if(m)ch['mqtt.bot_number']=$('w_bot').value.trim();if(wh)ch['webhook.url']=$('w_whurl').value.trim();
 if(wh&&!ch['webhook.url']){toast('Type the webhook URL, or turn it off');return;}
 const r=await postSettings(ch);if(r.ok){await getSettings();W.restart=r.restart_needed.length>0;wGo(5);}}
function wLive(){if($('wiz').hidden||!ST)return;const d=ST.data,s=ST;
 const l=$('w_live');if(l)l.innerHTML=`<span>${esc(abbr(d,'home'))} ${esc(d.home_score??0)}</span><span>${esc(d.clock||'--:--')} · ${esc(d.period_ordinal||'')}</span><span>${esc(abbr(d,'guest'))} ${esc(d.guest_score??0)}</span><span class="small ${s.packet_age!=null&&s.packet_age<10?'ok':'mut'}" style="font-family:system-ui">${s.packet_age==null?'no data yet':s.packet_age<10?'● live':'last data '+Math.round(s.packet_age)+' s ago'}</span>`;
 const x=$('w_xml');if(x){const a=s.outputs.xml.age;x.innerHTML=a!=null&&a<30?'<span class="ok">✓ vMix (or something) is reading it</span>':'Waiting for vMix to read it…';}
 if(W.step==2)wDak();
 const sm=$('w_sum');if(sm){const o=s.outputs,live=s.packet_age!=null&&s.packet_age<10;
  const it=[[live,'Scoreboard data',inputTitle(s)],[o.xml.age!=null,'vMix reading data.xml',o.xml.age!=null?ageTxt(o.xml.age):'not yet: add the Data Source in vMix'],
   [!o.vmix.enabled||o.vmix.state.startsWith('connected'),'vMix SetText push',o.vmix.enabled?o.vmix.state:'off'],
   [true,'MQTT',o.mqtt.enabled?o.mqtt.clients+' client(s)':o.mqtt.configured?'starts after a restart of ScoreReader':'off'],
   [!o.webhook.enabled||o.webhook.state.startsWith('ok'),'Webhook',o.webhook.enabled?o.webhook.state:'off']];
  sm.innerHTML=it.map(([ok,t,d])=>`<div class="row"><span>${ok?'<span class="ok">✓</span>':'<span style="color:var(--warn)">!</span>'} &nbsp;${esc(t)}</span><span class="mut small">${esc(d)}</span></div>`).join('');}}

/* ---------- poll ---------- */
let firstTick=true;
async function tick(){
 try{const s=await (await fetch('/api/state')).json();ST=s;
  $('hstat').textContent=`${s.bytes_in.toLocaleString()} bytes · ${s.packets.toLocaleString()} packets · ${s.bad_packets} bad`;
  const dm=$('demo');dm.hidden=s.source_saved;if(!s.source_saved)$('demotxt').textContent=s.source=='demo'?'DEMO MODE: simulated game, not the scoreboard':'REPLAY: playing back '+(s.replay_file||'a capture')+', not live data';
  renderHome(s);if(TAB=='diag')renderDiag(s);wLive();
  if(firstTick){firstTick=false;if(!s.setup_done)openWizard(1);}
 }catch(e){$('overall').textContent='dashboard lost connection';$('overall').className='pill badb';}
}
foldNet(lsGet('sr_net')=='1');
const t0=lsGet('sr_tab');if(t0&&t0!='settings')showTab(t0);
$('flt').oninput=()=>ST&&renderDiag(ST);
setInterval(tick,250);tick();
</script></body></html>"""


class App:
    def __init__(self, cfg, saved: dict | None = None):
        """cfg = the running configuration (config file + command-line overrides).
        saved = the configuration as stored in scorereader_config.json. Only explicit changes from the dashboard
        are written back, so options like --demo or --no-mqtt never end up in the file."""
        self.cfg = cfg
        self.saved = saved if saved is not None else json.loads(json.dumps(cfg))
        self.stop = threading.Event()
        self.running = False
        self.gen = 0  # bumped on any settings change, so outputs refresh even if the board hasn't changed
        self.swap_lock = threading.Lock()  # held while the decoders are replaced or read
        self.settings_lock = threading.Lock()
        self.sports = SportMap(FIELDS_PATH)
        if cfg["sport"] not in self.sports.sports:
            log(f"Unknown sport '{cfg['sport']}', using football. Options: {', '.join(self.sports.names())}")
            cfg["sport"] = "football"
        self.decoder, self.cl = self._new_decoders()
        self.mqtt = None
        if cfg.get("mqtt", {}).get("enabled"):
            from sportzcast_mqtt import SportzcastMqttOutput

            self.mqtt = SportzcastMqttOutput(cfg, log=log)
        self.reader = self._new_reader()
        self.vmix = VmixPusher(cfg, threading.Event()) if cfg["vmix"].get("enabled") else None
        self.webhook = self._new_webhook()
        self.data: dict = {}
        self.data_lock = threading.Lock()

    # ---- components
    def _new_decoders(self):
        dec = DaktronicsRTD(self.cfg.get("strict_daktronics_header", True))
        cl = DaktronicsCurrentLoop()
        cl.sport_fallback = lambda: self.cfg["sport"]
        cl.custom_fields = lambda: self.cfg.get("custom_fields", {})
        self.vendor = None
        self.cl_other = None
        pid = self.protocol_id()
        if not pid:
            try:
                import scoreboards

                self.cl_other = scoreboards.create("Daktronics_Autodetect")
            except Exception:
                self.cl_other = None
        if pid:
            try:
                import scoreboards

                self.vendor = scoreboards.create(pid)
                log(f"Scoreboard: {self.protocol_label()}")
            except Exception as e:
                log(f"Scoreboard '{pid}' is not available ({e}); using the built-in Daktronics decoders")
        return dec, cl

    def protocol_id(self) -> str:
        return str(get_path(self.cfg, "scoreboard.protocol", "") or "")

    def protocol_label(self) -> str:
        pid = self.protocol_id()
        if not pid:
            return "Daktronics All Sport 5000 (built-in)"
        try:
            import scoreboards

            vs = scoreboards.vendor_sport(pid) or {}
            return vs.get("description") or pid
        except Exception:
            return pid

    def bot_plan(self) -> dict:
        """How to set up the ScoreBot for the chosen brand and wiring (built-in Daktronics: the config file's
        scorelink.config_commands at cfg["baud"], sent only when asked, exactly as before)."""
        pid = self.protocol_id()
        if not pid or self.vendor is None:
            # Built-in Daktronics: if another brand reconfigured the bot, put the Daktronics set-up back once.
            last = str(get_path(self.cfg, "scorelink.configured_for", "") or "")
            if last and last != BUILTIN_BOT_KEY:
                return {"label": "Daktronics All Sport 5000 (built-in)", "commands": None, "baud": None,
                        "configure": True, "on_configured": self._remember_bot(BUILTIN_BOT_KEY)}
            return {}
        try:
            import scoreboards
            from scoreboards import hardware

            vs = scoreboards.vendor_sport(pid) or {}
            configs = hardware.configs_for_vendor(vs.get("vendor", ""))
        except Exception as e:
            return {"note": f"No bot set-up known for {pid} ({e}); reading at {self.cfg['baud']} baud"}
        if not configs:
            return {"note": f"No wiring known for {pid}; reading at {self.cfg['baud']} baud"}
        idx = int(get_path(self.cfg, "scoreboard.connection", 0) or 0)
        vc = configs[idx] if 0 <= idx < len(configs) else configs[0]
        label = f"{self.protocol_label()} - {vc.get('connection', 'wired')}"
        try:
            plan = hardware.plan(vc)
        except Exception as e:
            return {"label": label, "note": f"Can't set up the bot for {label} automatically ({e})"}
        if plan.get("device") != "pic" or plan.get("dynamic"):
            return {"label": label, "baud": plan.get("data_baud") or None,
                    "note": f"{label}: this wiring ({plan.get('hardware')}) is configured outside the ScoreLinkII; "
                            "ScoreReader only reads the data"}
        key = f"{pid}#{vc.get('hardware')}"
        return {"label": label, "commands": plan["pic_commands"], "baud": plan["data_baud"],
                "configure": get_path(self.cfg, "scorelink.configured_for", "") != key,
                "on_configured": self._remember_bot(key)}

    def _remember_bot(self, key: str):
        def remember():
            set_path(self.saved, "scorelink.configured_for", key)
            set_path(self.cfg, "scorelink.configured_for", key)
            try:
                save_config(self.saved)
            except OSError:
                pass
        return remember

    def _new_reader(self):
        r = Reader(self.cfg, self.decoder, threading.Event(), self.cl, vendor=self.vendor, bot=self.bot_plan())
        r.cl_other = self.cl_other
        if self.mqtt:
            r.raw_hook = self.mqtt.raw
        return r

    def restart_reader(self):
        """Stop the data source, clear every decoded value, and start the source currently in cfg."""
        old = self.reader
        old.stop.set()
        if old.is_alive():
            old.join(timeout=5)
            if old.is_alive():
                log("The previous data source is still closing; the new one will retry until the port is free")
        with self.swap_lock:
            self.decoder, self.cl = self._new_decoders()
            STATUS.reset_counters()
            STATUS.set_source("starting", self.cfg["source"])
            self.reader = self._new_reader()
            self.gen += 1
        log(f"Data source: {self.cfg['source']}")
        if self.running:
            self.reader.start()

    def restart_vmix(self):
        if self.vmix:
            self.vmix.stop.set()
            self.vmix = None
        if self.cfg["vmix"].get("enabled"):
            self.vmix = VmixPusher(self.cfg, threading.Event())
            STATUS.vmix_state = "connecting"
            if self.running:
                self.vmix.start()
        else:
            STATUS.vmix_state = "disabled"

    def _new_webhook(self):
        if get_path(self.cfg, "webhook.enabled") and get_path(self.cfg, "webhook.url"):
            return WebhookPusher(self.cfg, threading.Event())
        STATUS.webhook_state = "disabled" if not get_path(self.cfg, "webhook.enabled") else "no URL set"
        return None

    def restart_webhook(self):
        if self.webhook:
            self.webhook.stop.set()
        self.webhook = self._new_webhook()
        if self.webhook and self.running:
            self.webhook.start()
            self.gen += 1  # send the current data straight away

    # ---- settings
    def live_source(self) -> str:
        """The source to go back to after demo/replay: the saved one, or the ScoreBot."""
        s = self.saved.get("source", "scorelink")
        return "scorelink" if s in RUNTIME_ONLY_SOURCES else s

    def setting_choices(self) -> dict:
        try:
            import scoreboards

            protocols = sorted(scoreboards.protocols())
        except Exception:
            protocols = []
        return {"source": ["scorelink", "serial", "tcp", "demo", "replay"], "sport": self.sports.names(),
                "mqtt.mode": ["broker", "client"], "replay_file": list_captures(),
                "scoreboard.protocol": [""] + protocols}

    def scoreboards_json(self) -> list:
        """Brands with their implemented sports and wiring options, for the Settings pickers."""
        try:
            import scoreboards

            impl = scoreboards.protocols()
            cat = scoreboards.catalog()
        except Exception:
            return []
        out = []
        for v in cat.get("vendors", []):
            sports = [{"id": r["enum"], "description": r.get("description") or r["enum"], "event": r.get("event_type")}
                      for r in cat.get("vendor_sports", []) if r.get("vendor") == v["name"] and r.get("enum") in impl]
            if sports:
                out.append({"name": v["name"], "sports": sorted(sports, key=lambda x: x["description"]),
                            "connections": [c.get("connection", "") + (" - " + c["hardware"] if c.get("hardware") else "")
                                            for c in v.get("configs", [])]})
        return sorted(out, key=lambda v: v["name"].lower())

    def settings_json(self) -> dict:
        try:
            ports = [{"device": p.device, "description": p.description,
                      "scorelink": (p.vid, p.pid) == (SCORELINK_VID, SCORELINK_PID)} for p in list_serial_ports()]
        except Exception:
            ports = []
        return {
            "values": {p: get_path(self.cfg, p, "") for p in SETTINGS},
            "spec": [{"path": p, "group": g, "label": lbl, "type": t, "apply": a} for p, (g, lbl, t, a) in SETTINGS.items()],
            "choices": self.setting_choices(),
            "ports": ports,
            "runtime_only_sources": list(RUNTIME_ONLY_SOURCES),
            "scoreboards": self.scoreboards_json(),
            "live_source": self.live_source(),
            "config_path": CONFIG_PATH,
            "text_folder": os.path.join(DATA_DIR, get_path(self.cfg, "text_files.folder", "text_out")),
        }

    def apply_settings(self, changes: dict) -> dict:
        """Validate and apply dashboard settings. All-or-nothing: if any value is invalid, nothing changes."""
        if not isinstance(changes, dict):
            return {"ok": False, "errors": {"": "expected a JSON object"}}
        choices = self.setting_choices()
        clean, errors = {}, {}
        for path, value in changes.items():
            if path not in SETTINGS:
                errors[path] = "is not a setting that can be changed here"
                continue
            try:
                clean[path] = coerce_setting(path, value, choices)
            except ValueError as e:
                errors[path] = f"{SETTINGS[path][1]} {e}"
        if errors:
            return {"ok": False, "errors": errors}
        with self.settings_lock:
            changed = {p: v for p, v in clean.items() if get_path(self.cfg, p) != v}
            persist = False
            for path, value in changed.items():
                set_path(self.cfg, path, value)
                # demo/replay are never saved, so the next start is always live data
                runtime_only = path == "replay_file" or (path == "source" and value in RUNTIME_ONLY_SOURCES)
                if not runtime_only:
                    set_path(self.saved, path, value)
                    persist = True
                log(f"Setting {path} = {value!r}" + (" (this session only)" if runtime_only else ""))
            if persist:
                save_config(self.saved)
            kinds = {SETTINGS[p][3] for p in changed}
            if "reader" in kinds:
                self.restart_reader()
            if "vmix" in kinds:
                self.restart_vmix()
            if "webhook" in kinds:
                self.restart_webhook()
            self.gen += 1
        return {"ok": True, "changed": sorted(changed),
                "restart_needed": sorted(p for p in changed if SETTINGS[p][3] == "restart")}

    # ---- data
    def current(self) -> dict:
        """Every field (for the dashboard)."""
        with self.data_lock:
            d = self.data
            return d.copy() if isinstance(d, Data) else dict(d)

    def public(self, d: dict) -> dict:
        """The fields served to graphics: only those used in the sport, unless output_all_fields is on."""
        if self.cfg.get("output_all_fields") or not isinstance(d, Data):
            return dict(d)
        return d.public()

    def add_team_info(self, d: dict) -> None:
        """Team names/abbreviations/colors from Settings. A blank name falls back to what the console sent
        (RTD), then the legacy mqtt.home_team_name/guest_team_name, then HOME/GUEST."""
        t = self.cfg.get("teams", {}) or {}
        m = self.cfg.get("mqtt", {}) or {}
        for side in ("home", "guest"):
            d[f"{side}_name"] = (str(t.get(f"{side}_name") or "").strip() or str(d.get(f"{side}_name") or "").strip()
                                 or str(m.get(f"{side}_team_name") or "").strip() or side.upper())
            d[f"{side}_abbr"] = (str(t.get(f"{side}_abbr") or "").strip()
                                 or str(d.get(f"{side}_abbr") or "").strip())
            d[f"{side}_color"] = str(t.get(f"{side}_color") or "").strip()

    def vendor_contract(self):
        """(contract name, contract dict) when a ScoreConnect-style decoder supplies the data: the chosen brand,
        or, on the built-in Daktronics path, a current-loop console code the built-in decoder doesn't know
        (anything but football 6601 / soccer 7601). None = the built-in decoder."""
        v = self.vendor
        if v is None:
            o, cl = self.cl_other, self.cl
            if (o is None or not cl.packets or cl.console_code in cl.CODE_SPORT
                    or getattr(o.decoder, "selected", None) is None):
                return None
            v = o
        return v.contract_name, (v.full_contract() if hasattr(v, "full_contract") else v.snapshot())

    def mqtt_contract(self, d: dict):
        """Contract for the MQTT payloads; None = football/soccer via build_payloads (byte-compatible as before)."""
        vc = self.vendor_contract()
        if vc is not None or self.cl.packets:
            return vc
        return rtd_contract(self.cfg["sport"], d)

    def build_data(self) -> dict:
        with self.swap_lock:
            vc = self.vendor_contract()
            if vc is not None:
                d = contract_to_data(*vc)
                d["protocol"] = self.protocol_id() or (self.cl_other.decoder.selected if self.cl_other else "")
            elif self.cl.packets:
                d = self.cl.data()
            else:
                sport = self.cfg["sport"]
                d = Data(self.sports.extract(sport, self.decoder.snapshot()))
                friendly = set(self.sports.simple(sport, d))
                d.raw_keys = frozenset(f["name"] for f in self.sports.fields(sport)) - friendly
                d.setdefault("sport", sport)
        # single possession field for graphics: "H", "V" or "" (no possession lit on the board)
        if "home_poss" in d:
            d["possession"] = "H" if d.get("home_poss") else ("V" if d.get("guest_poss") else "")
        self.add_team_info(d)
        return d

    def update_loop(self):
        seen = None
        text_cache: dict = {}
        last_save = 0.0
        while not self.stop.is_set():
            v = (self.gen, self.decoder.version, self.cl.version, self.vendor.version if self.vendor else 0,
                 self.cl_other.version if self.cl_other else 0)
            if v != seen:
                seen = v
                d = self.build_data()
                with self.data_lock:
                    self.data = d
                vm = self.vmix
                if vm:
                    vm.submit(d)
                if self.mqtt:
                    self.mqtt.update(d, contract=self.mqtt_contract(d))
                wh = self.webhook
                if wh:
                    wh.submit(flatten(self.public(d)))
                tf = self.cfg.get("text_files", {})
                if tf.get("enabled"):
                    try:
                        write_text_files(os.path.join(DATA_DIR, tf.get("folder", "text_out")), self.public(d),
                                         text_cache)
                    except OSError as e:
                        log(f"text file write error: {e}")
                if self.cfg.get("save_state") and time.time() - last_save > 2:
                    last_save = time.time()
                    try:
                        self.decoder.save(STATE_PATH)
                    except OSError:
                        pass
            self.stop.wait(0.03)
        # app stopping: stop the threads we own
        self.reader.stop.set()
        if self.vmix:
            self.vmix.stop.set()
        if self.webhook:
            self.webhook.stop.set()

    def outputs_json(self) -> dict:
        """What the Home page shows about each output: on/off, state, and who read it lately."""
        now = time.time()
        with STATUS.lock:
            reads = dict(STATUS.reads)

        def read(path):
            t, who = reads.get(path, (0, ""))
            return {"age": round(now - t, 1) if t else None, "by": who}

        m = self.cfg.get("mqtt", {}) or {}
        out = self.mqtt.out if self.mqtt else None
        return {
            "lan_ip": lan_ip(),
            "http_port": int(self.cfg["http_port"]),
            "xml": read("/data.xml"), "json": read("/data.json"), "json_obj": read("/data.obj.json"),
            "csv": read("/data.csv"),
            "vmix": {"enabled": bool(self.cfg["vmix"].get("enabled")), "state": STATUS.vmix_state,
                     "host": self.cfg["vmix"].get("host"), "input": self.cfg["vmix"].get("input")},
            "mqtt": {"enabled": self.mqtt is not None, "state": out.status if out else "disabled",
                     "clients": getattr(out, "client_count", 0) if out else 0, "mode": m.get("mode"),
                     "bot": m.get("bot_number"), "tcp_port": m.get("tcp_port"), "ws_port": m.get("ws_port"),
                     "broker_host": m.get("broker_host"), "broker_port": m.get("broker_port"),
                     "configured": bool(m.get("enabled"))},
            "webhook": {"enabled": self.webhook is not None, "state": STATUS.webhook_state,
                        "url": get_path(self.cfg, "webhook.url", "")},
            "text_files": bool(get_path(self.cfg, "text_files.enabled")),
        }

    def state_json(self) -> dict:
        with self.swap_lock:
            dec, cl = self.decoder, self.cl
        buf = dec.snapshot()
        now = time.time()
        with dec.lock:
            ch = [round(now - t, 1) if t else 9999 for t in dec.changed_at[: len(buf)]]
        lp = STATUS.last_packet_time
        return {
            "source": self.cfg["source"],
            "source_saved": self.cfg["source"] not in RUNTIME_ONLY_SOURCES,
            "live_source": self.live_source(),
            "replay_file": os.path.basename(str(self.cfg.get("replay_file") or "")),
            "source_state": STATUS.source_state,
            "source_detail": STATUS.source_detail,
            "bytes_in": STATUS.bytes_in,
            "packets": STATUS.packets,
            "bad_packets": STATUS.bad_packets,
            "unknown_headers": {} if cl.packets else dict(list(STATUS.unknown_headers.items())[:5]),
            "packet_age": (now - lp) if lp else None,
            "vmix_state": STATUS.vmix_state,
            "mqtt_state": self.mqtt.status if self.mqtt else "disabled",
            "pic_info": STATUS.pic_info,
            "sport": (self.current().get("sport") or self.cfg["sport"]) if self.vendor_contract() else
                     (cl.sport if cl.packets else self.cfg["sport"]),
            "vendor_mode": self.vendor is not None,
            "sports": self.sports.names(),
            "fields": [{"name": f["name"], "pos": f["pos"], "len": f["len"]} for f in self.sports.fields(self.cfg["sport"])],
            "data": self.current(),
            "raw": buf.decode("latin-1", "replace").replace("\x00", " "),
            "changed_ago": ch,
            "logs": STATUS.logs[-80:],
            "recent_hex": STATUS.recent_hex,
            "protocol": (self.protocol_label() if self.vendor is not None else
                         "Daktronics current loop (code " + (cl.console_code or "?") + ")" if cl.packets
                         else "Daktronics RTD"),
            "scoreboard": self.protocol_label(),
            "contract": self._contract_rows(),
            "cl_table": cl.address_table(),
            "cl_bad": cl.bad,
            "outputs": self.outputs_json(),
            "setup_done": bool(self.cfg.get("setup_done")),
            "tcp": {"host": get_path(self.cfg, "tcp.host", ""), "port": get_path(self.cfg, "tcp.port", 0)},
            "port": self.cfg.get("port"),
            "record_raw": bool(self.cfg.get("record_raw")),
            "version": __version__,
        }

    def _contract_rows(self) -> list:
        vc = self.vendor_contract()
        if vc is None:
            return []
        v = self.vendor or self.cl_other
        rows = [{"k": k, "v": str(val)} for k, val in sorted(vc[1].items()) if not isinstance(val, (dict, list))]
        return [{"k": "(contract)", "v": vc[0]}, {"k": "(packets / bad)", "v": f"{v.packets} / {v.bad}"}] + rows

    def make_handler(app):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                d = app.public(app.current())  # only the fields used in the sport (unless output_all_fields)
                flat = flatten(d)
                if u.path in ("/data.json", "/data.obj.json", "/data.xml", "/data.csv"):
                    STATUS.note_read(u.path, self.client_address[0])
                if u.path in ("/", "/index.html"):
                    return self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
                if u.path == "/data.json":
                    return self._send(200, json.dumps([flat], ensure_ascii=False), "application/json")
                if u.path == "/data.obj.json":
                    return self._send(200, json.dumps(flat, ensure_ascii=False), "application/json")
                if u.path == "/data.xml":
                    x = "".join(f"<{k}>{xml_escape(str(v))}</{k}>" for k, v in flat.items())
                    return self._send(200, f'<?xml version="1.0" encoding="utf-8"?><scoreboard><data>{x}</data></scoreboard>',
                                      "application/xml")
                if u.path == "/data.csv":
                    s = io.StringIO()
                    w = csv.writer(s)
                    w.writerow(flat.keys())
                    w.writerow(flat.values())
                    return self._send(200, s.getvalue(), "text/csv")
                if u.path in ("/favicon.svg", "/favicon.ico", "/icon.png"):
                    name, ctype = (("icon.png", "image/png") if u.path == "/icon.png"
                                   else ("icon.svg", "image/svg+xml"))
                    try:
                        with open(os.path.join(ASSETS_DIR, name), "rb") as f:
                            return self._send(200, f.read(), ctype)
                    except OSError:
                        return self._send(404, "not found", "text/plain")
                if u.path == "/api/state":
                    return self._send(200, json.dumps(app.state_json()), "application/json")
                if u.path == "/api/settings":
                    return self._send(200, json.dumps(app.settings_json(), ensure_ascii=False), "application/json")
                if u.path == "/api/discover":
                    import discovery

                    scan = (urllib.parse.parse_qs(u.query).get("scan") or ["1"])[0] != "0"
                    try:
                        found = discovery.discover(scan=scan)
                    except Exception as e:  # never break the wizard: it still offers "Other"
                        log(f"Device search failed: {e}")
                        found = []
                    return self._send(200, json.dumps({"devices": found}, ensure_ascii=False), "application/json")
                return self._send(404, "not found", "text/plain")

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                # The custom header makes browsers send a CORS preflight for cross-site requests, which this
                # server never approves, so another web page open on this PC can't change settings.
                if self.headers.get("X-ScoreReader") != "1":
                    return self._send(403, "missing X-ScoreReader: 1 header", "text/plain")
                if u.path == "/api/settings":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        changes = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    except (ValueError, UnicodeDecodeError):
                        return self._send(400, json.dumps({"ok": False, "errors": {"": "invalid JSON"}}),
                                          "application/json")
                    res = app.apply_settings(changes)
                    return self._send(200 if res["ok"] else 400, json.dumps(res), "application/json")
                if u.path == "/api/sport":
                    name = (qs.get("name") or [""])[0]
                    res = app.apply_settings({"sport": name})
                    if res["ok"]:
                        cl = app.cl
                        if cl.console_code in cl.CODE_SPORT:
                            log(f"Console code {cl.console_code} overrides the sport: {cl.sport}")
                        return self._send(200, "ok", "text/plain")
                    return self._send(400, "unknown sport", "text/plain")
                if u.path == "/api/reinit":
                    app.reader.reinit_request.set()
                    log("Re-init requested from dashboard")
                    return self._send(200, "ok", "text/plain")
                return self._send(404, "not found", "text/plain")

        return H

    def run(self):
        """Start everything and block until Ctrl+C."""
        self.start()
        try:
            while not self.stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log("Stopping…")
        finally:
            self.shutdown()

    @property
    def dashboard_url(self) -> str:
        return f"http://localhost:{int(self.cfg['http_port'])}/"

    def start(self):
        """Start the source, outputs and web server in background threads, then return."""
        if self.cfg.get("save_state"):
            self.decoder.load(STATE_PATH)
        self.running = True
        self.reader.start()
        if self.vmix:
            STATUS.vmix_state = "connecting"
            self.vmix.start()
        if self.mqtt:
            self.mqtt.start()
        if self.webhook:
            self.webhook.start()
        threading.Thread(target=self.update_loop, daemon=True).start()
        port = int(self.cfg["http_port"])
        srv = ThreadingHTTPServer(("0.0.0.0", port), self.make_handler())
        self._srv = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"Dashboard:        http://localhost:{port}/")
        log(f"vMix data source: http://localhost:{port}/data.json   (also /data.xml, /data.csv)")
        if self.mqtt:
            log(f"MQTT topics:      bot/{self.mqtt.bot}/json, bot/{self.mqtt.bot}/sbdata, bot/{self.mqtt.bot}/raw")

    def shutdown(self):
        if getattr(self, "_shut", False):
            return
        self._shut = True
        self.stop.set()
        self.reader.stop.set()
        srv = getattr(self, "_srv", None)
        if srv is not None:
            srv.shutdown()
        if self.cfg.get("save_state"):
            try:
                self.decoder.save(STATE_PATH)
            except OSError:
                pass


# --------------------------------------------------------------------------- CLI tools
def cmd_list_ports():
    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
    for p in ports:
        tag = "  <-- ScoreLinkII / MCP2221" if (p.vid, p.pid) == (SCORELINK_VID, SCORELINK_PID) else ""
        vidpid = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
        print(f"{p.device:28} {vidpid}  {p.description}{tag}")
    try:
        m = MCP2221()
        val, d = m.get_gp2()
        m.close()
        print(f"\nScoreLinkII USB control interface: OK (GP2={val}, {'DATA' if val == 1 else 'COMMAND' if val == 0 else 'not GPIO'} mode)")
    except ImportError:
        print("\n(hidapi not installed - can't check MCP2221 GPIO interface; only needed on Mac/Linux)")
    except Exception as e:
        print(f"\nScoreLinkII USB control interface: {e}")


def cmd_list_scoreboards():
    import scoreboards

    impl = scoreboards.protocols()
    rows = [r for r in scoreboards.catalog().get("vendor_sports", []) if r.get("enum") in impl]
    print("(blank)".ljust(44), "Daktronics All Sport 5000, built-in current loop + RTD (default)")
    for r in sorted(rows, key=lambda r: (r["vendor"].lower(), r["description"])):
        print(r["enum"].ljust(44), f'{r["vendor"]}: {r["description"]}')
    print(f"\n{len(rows)} scoreboard layouts. Use: scorereader.py --scoreboard ID   (or pick one in Settings)")
    for mod, err in scoreboards.load_errors().items():
        print(f"warning: scoreboards/{mod}.py failed to load: {err}")


def cmd_probe(cfg, configure: bool):
    import serial

    port = resolve_port(cfg)
    mcp = MCP2221()
    ser = serial.Serial(port, cfg["baud"], timeout=0.2)
    try:
        print(f"Port {port}")
        val, d = mcp.get_gp2()
        print(f"GP2 before: value={val} dir={d}  ({'DATA mode' if val == 1 else 'COMMAND mode' if val == 0 else 'not GPIO'})")
        info = scorelink_session(cfg, ser, mcp, send_config=configure, probe=True)
        val, d = mcp.get_gp2()
        print(f"GP2 after:  value={val} ({'DATA mode' if val == 1 else '?'})")
        if not info.get("R?"):
            print("\nNo reply from the PIC. Check: ScoreConnect III fully closed (incl. service), right port, bot powered.")
    finally:
        ser.close()
        mcp.close()


def cmd_sniff(cfg, seconds: float):
    """Raw hex dump of whatever the bot sends in data mode (no decoding)."""
    stop = threading.Event()
    dec = DaktronicsRTD(cfg.get("strict_daktronics_header", True))
    rd = Reader(cfg, dec, stop, DaktronicsCurrentLoop())
    rd.raw_sink = queue.Queue()
    rd.start()
    t_end = time.time() + seconds
    total = 0
    try:
        while time.time() < t_end:
            try:
                data = rd.raw_sink.get(timeout=0.5)
            except queue.Empty:
                continue
            total += len(data)
            for i in range(0, len(data), 24):
                ch = data[i : i + 24]
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in ch)
                print(f"{ch.hex(' '):<72} {asc}")
    except KeyboardInterrupt:
        pass
    stop.set()
    print(f"\n{total} bytes, {STATUS.packets} valid packets decoded (RTD + current loop).")
    if total and not STATUS.packets:
        print("Bytes but no RTD packets -> wrong baud? try --baud 9600 or 38400, or check scoreboard type.")
    if not total:
        print("No bytes. Is the console on and sending? Clock running? Try --probe to check the bot.")


def main():
    ap = argparse.ArgumentParser(description="ScoreReader - ScoreLinkII / Daktronics scoreboard data to vMix, MQTT and more")
    ap.add_argument("--version", action="version", version=f"ScoreReader {__version__}")
    ap.add_argument("--port", help="serial port (COM5, /dev/cu.usbmodem...) - default auto")
    ap.add_argument("--baud", type=int, help="scoreboard data baud (default 19200)")
    ap.add_argument("--sport", help="football, basketball, soccer, baseball, volleyball, hockey_lacrosse, ...")
    ap.add_argument("--source", choices=["scorelink", "serial", "tcp", "demo", "replay"],
                    help="scorelink = ScoreBot over USB; serial = plain USB-serial cable to the console")
    ap.add_argument("--demo", action="store_true", help="fake football game (test vMix without hardware)")
    ap.add_argument("--vmix", action="store_true", help="enable vMix TCP push (input name in config)")
    ap.add_argument("--vmix-input", help="vMix title input name/number")
    ap.add_argument("--http-port", type=int)
    ap.add_argument("--mqtt-bot", type=int, metavar="N", help="bot number for MQTT topics bot/N/... (default 0)")
    ap.add_argument("--no-mqtt", action="store_true", help="disable the Sportzcast-compatible MQTT output")
    ap.add_argument("--scoreboard", metavar="ID",
                    help="scoreboard brand+sport id (see --list-scoreboards); this session only")
    ap.add_argument("--list-scoreboards", action="store_true", help="list supported scoreboard brands/sports and exit")
    ap.add_argument("--replay", metavar="FILE", help="replay a raw capture from logs/raw_*.bin")
    ap.add_argument("--no-menubar", action="store_true", help="Mac: don't show the ScoreReader menu bar icon")
    ap.add_argument("--fresh", action="store_true", help="ignore the saved last-known scoreboard state")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--probe", action="store_true", help="talk to the ScoreLinkII PIC (version/config) and exit")
    ap.add_argument("--configure-bot", action="store_true",
                    help="re-send the ScoreConnect config commands (Daktronics football) to the bot and exit")
    ap.add_argument("--sniff", type=float, nargs="?", const=20, metavar="SECONDS",
                    help="hex-dump raw bytes from the bot (default 20s) and exit")
    # Old macOS versions pass a "-psn_…" process serial number to apps opened from Finder.
    a = ap.parse_args([x for x in sys.argv[1:] if not x.startswith("-psn_")])

    if hasattr(signal, "SIGTERM"):  # systemd / launchd stop = Ctrl+C: shut down cleanly
        def _on_term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _on_term)

    saved = load_config()                   # what's in scorereader_config.json
    cfg = json.loads(json.dumps(saved))     # running config = saved + command-line options (never written back)
    if a.port:
        cfg["port"] = a.port
    if a.baud:
        cfg["baud"] = a.baud
    if a.sport:
        cfg["sport"] = a.sport
    if a.source:
        cfg["source"] = a.source
    if a.demo:
        cfg["source"] = "demo"
    if a.replay:
        cfg["source"], cfg["replay_file"], cfg["record_raw"] = "replay", a.replay, False
    if a.mqtt_bot is not None:
        cfg["mqtt"]["bot_number"] = a.mqtt_bot
    if a.no_mqtt:
        cfg["mqtt"]["enabled"] = False
    if a.vmix:
        cfg["vmix"]["enabled"] = True
    if a.vmix_input:
        cfg["vmix"]["input"] = a.vmix_input
    if a.http_port:
        cfg["http_port"] = a.http_port

    if a.scoreboard is not None:
        cfg.setdefault("scoreboard", {})["protocol"] = a.scoreboard
    if a.list_ports:
        return cmd_list_ports()
    if a.list_scoreboards:
        return cmd_list_scoreboards()
    if a.probe or a.configure_bot:
        try:
            return cmd_probe(cfg, configure=a.configure_bot)
        except ImportError:
            print("hidapi is not installed (Mac/Linux only):  pip3 install hidapi")
        except Exception as e:
            print(f"Probe failed: {e}")
            if "denied" in str(e).lower():
                print("-> ScoreConnect III (or its service) still has the port. Run the stop-service .bat as admin.")
        return
    if a.sniff is not None:
        return cmd_sniff(cfg, a.sniff)
    if a.fresh and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
    print(f"ScoreReader {__version__}  |  source={cfg['source']}  sport={cfg['sport']}  baud={cfg['baud']}  config={CONFIG_PATH}")
    if saved.get("source") in RUNTIME_ONLY_SOURCES:
        print(f"NOTE: scorereader_config.json has \"source\": \"{saved['source']}\" - that is not live scoreboard data. "
              "Pick the live source in Settings.")
    set_window_title(f"ScoreReader {__version__}")
    if MAC_APP:  # no Terminal window: keep the log in a file (menu bar > Open logs folder)
        detach_console(os.path.join(LOG_DIR, "scorereader.log"))
    app = App(cfg, saved=saved)
    if sys.platform == "darwin" and not a.no_menubar:
        try:
            import menubar

            if menubar.available():
                app.start()
                if MAC_APP:  # opened from Finder/Launchpad: show the dashboard so it's clear it started
                    import webbrowser

                    threading.Timer(1.5, webbrowser.open, args=(app.dashboard_url,)).start()
                return menubar.run(app)  # owns the main thread until Quit
        except Exception as e:
            log(f"Menu bar icon unavailable ({e}); running in this window only")
            if app.running:
                try:
                    while not app.stop.is_set():
                        time.sleep(1)
                except KeyboardInterrupt:
                    log("Stopping…")
                finally:
                    app.shutdown()
                return
    app.run()


if __name__ == "__main__":
    main()
