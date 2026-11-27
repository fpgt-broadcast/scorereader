# ScoreReader reference

The details behind the [README](../README.md): outputs, settings, the command line and how to fix a value that reads wrong.

## Contents
- [vMix](#vmix)
- [MQTT (OSE and ScoreConnect clients)](#mqtt-ose-and-scoreconnect-clients)
- [Webhook](#webhook)
- [Data fields](#data-fields)
- [Dashboard and first-run setup](#dashboard-and-first-run-setup)
- [Scoreboard brands](#scoreboard-brands)
- [Without a ScoreBot](#without-a-scorebot)
- [Fixing a value that reads wrong (`custom_fields`)](#fixing-a-value-that-reads-wrong-custom_fields)
- [Mac, Linux and Raspberry Pi details](#mac-linux-and-raspberry-pi-details)
- [Command line](#command-line)
- [Settings file](#settings-file)
- [Development](#development)

## vMix

**Data Source (easiest):** in vMix, go to *Settings → Data Sources → Add → XML*, enter `http://<this-computer>:8765/data.xml` (the dashboard's **Home → vMix** box has it ready to copy), and set the shortest refresh interval. JSON is also served at `/data.json` (a one-row array) and `/data.obj.json` (a plain object), and CSV at `/data.csv`. The dashboard shows when each was last read and from which address.

**Direct push (fastest clock):** turn on **Settings → vMix → Push values to vMix** and enter the title input's name. To choose which title fields get which values, edit `vmix.fields` in `scorereader_config.json`:

```json
"vmix": {
  "enabled": true, "host": "127.0.0.1", "port": 8099, "input": "Scorebug",
  "fields": {
    "clock": "Clock.Text",
    "home_score": "HomeScore.Text",
    "down_distance": "DownDistance.Text",
    "home_poss": {"name": "HomePoss.Text", "true": "◀", "false": ""},
    "guest_poss": {"name": "GuestArrow.Source", "visible": true}
  }
}
```

- A plain string uses `SetText`.
- `{"visible": true}` shows or hides a layer: `SetImageVisibleOn`/`Off` for `.Source` layers, `SetTextVisibleOn`/`Off` for `.Text` layers.
- `true`/`false` turns an on/off value into text.

Only changed values are sent.

## MQTT (OSE and ScoreConnect clients)

ScoreReader runs its own MQTT broker that looks like ScoreConnect III's, so OSE and other ScoreConnect clients keep working.

| | |
|---|---|
| MQTT | `<pc-ip>:1883` |
| WebSocket | `ws://<pc-ip>:8083/mqtt` |
| Topics | `bot/<N>/json` (Sportzcast JSON), `bot/<N>/sbdata` (150-character string), `bot/<N>/raw` |
| Login / QoS | none / 0, same as ScoreConnect |

- **Bot number:** set it in **Settings → Network outputs → MQTT** (or `--mqtt-bot N`) to the number your client subscribes to.
- **Team names:** come from the **Teams** tab. Older configs' `mqtt.home_team_name`/`guest_team_name` still work as a fallback.
- **Updates:** values are published on every change and at least once a second, and a new subscriber gets the latest values straight away.
- **Existing broker:** set mode to `client` and enter the broker's host to publish into Mosquitto or similar instead.
- **Clients can't publish on `bot/…`:** the broker acknowledges such a PUBLISH but doesn't pass it on, so nobody on the network (or a web page, over the WebSocket port) can put other scores in front of OSE. Other topics are forwarded as before.

## Webhook

**Settings → Network outputs → Webhook** POSTs the data to a URL whenever it changes: `Content-Type: application/json`, the same object as `/data.obj.json` (on/off values as `"1"`/`"0"`). It sends at most one POST every `webhook.min_interval_ms` (default 500 ms), always with the newest values; a failed POST is retried every 2 seconds, and the dashboard shows the last result.

```json
"webhook": {"enabled": true, "url": "http://192.168.1.20:5000/score", "min_interval_ms": 500}
```

## Data fields

`/data.json` (and XML/CSV, and the text files) carry only the fields used in the current sport; a field with no data yet is blank. The dashboard lists every field, including raw ones. To serve the raw fields too (every Daktronics RTD manual field, or every Sportzcast contract field for other brands), turn on **Settings → Network outputs → JSON / CSV → Include every raw field**. On/off values are `1`/`0`.

**Football**

| field | example |
|---|---|
| `clock`, `clock_running` | `4:32` or `59.8`, `1`/`0` |
| `play_clock` | `25` |
| `home_score`, `guest_score` | `14` |
| `period`, `period_ordinal` | `2`, `2nd` |
| `down_distance` | `3rd & 7` |
| `down`, `to_go`, `ball_on` | `3rd`, `7`, `35` |
| `home_timeouts`, `guest_timeouts` | `3` |
| `possession` | `H`, `V` or blank |
| `home_poss`, `guest_poss` | `1`/`0` |

**Soccer:** `clock`, `clock_running`, `home_score`, `guest_score`, `period`, `period_ordinal`, `home_shots`, `guest_shots`, `home_corners`, `guest_corners`, plus `horn` (current loop) or `home_saves`/`guest_saves` (RTD).

**Fields follow the sport.** A basketball game has no `down_distance` or `ball_on`, and MQTT sends a basketball message, not a football one. On the built-in Daktronics decoder, football (console code 6601) and soccer (7601) use ScoreReader's own layouts; any other console code (basketball 1101/1103, baseball 5501, hockey 4401, volleyball 2101, wrestling 3101 …) switches automatically to ScoreConnect's layout for that sport. The default vMix field list is for football; for other sports, map their fields in `vmix.fields`.

**Every sport** also has `home_name`, `guest_name`, `home_abbr`, `guest_abbr`, `home_color` and `guest_color` from the Teams tab.

**Other sports and brands** add their own fields (for example `shot_clock`, `home_fouls`, `balls`, `strikes`, `outs`), and, with **Include every raw field** on, every field of the Sportzcast contract in snake_case (or, for Daktronics RTD, every field in the Daktronics RTD manual).

## Dashboard and first-run setup

The dashboard (`http://<this-computer>:8765/`) has four tabs:

- **Home:** where the data comes from, the game right now (with the sport's own extras: down & distance, shot clock, balls-strikes-outs, shots, fouls …), the vMix Data Source address and SetText push, and **Network outputs** (MQTT, JSON, CSV, webhook), folded shut until you open it.
- **Teams:** names, abbreviations and colours that override what the console sends, with a swap button.
- **Diagnostics:** every field, the console addresses or raw RTD buffer, the log and the last bytes, plus Record raw bytes, Replay a capture, Demo game and Re-init bot.
- **Settings:** Scoreboard input, vMix, Network outputs, Files and Advanced, and **Run setup again**.

**First-run setup** opens when `scorereader_config.json` is new (`"setup_done": false`). Configs from older versions count as already set up. The device search (`GET /api/discover`):

1. asks the network for ScoreBridges by mDNS (`_scorebridge._tcp.local`, which the ScoreBridge firmware announces) and tries the name `scorebridge.local`;
2. only if that finds nothing, scans this computer's /24 for TCP port 10001 and keeps the hosts whose `http://<ip>/api/status` answers like a ScoreBridge (a few seconds);
3. lists USB serial ports: the ScoreBot (MCP2221, 04D8:00DD), the ScoreBridge's CH340 (any CH340 cable looks the same), and other USB-serial adapters.

A ScoreBridge found by mDNS is saved by its `.local` name; one found by the scan is saved by IP, so give it a fixed IP on its settings page.

**ScoreBridge network (DHCP or static IP).** Needs ScoreBridge firmware 1.1 or newer. With the source set to a ScoreBridge on USB (Serial) or on the network (TCP), Settings › Scoreboard input shows a **ScoreBridge network** card: what address it has now, DHCP or Static IP, and **Send to ScoreBridge**. The box restarts (about 5 s). Over the network, ScoreReader then reads it at its new address (the static IP, or `name.local` for DHCP). In first-run setup, picking a ScoreBridge on USB offers **Put it on the network**: the address is sent over USB, ScoreReader waits for the box to answer on the network and switches to it.

API: `GET /api/bridge/network[?via=usb&port=COM7 | ?via=net&host=…]`, `POST /api/bridge/network` `{"dhcp": false, "ip": "192.168.1.50", "mask": "255.255.255.0", "gateway": "192.168.1.1", "dns": "", "hostname": "scorebridge"}` (plus optional `via`/`port`/`host`; default = the current source), `POST /api/bridge/wait` `{"address": "…"}`. On the box: `GET/POST /api/network` (form fields `host ip mask gw dns`, blank ip = DHCP); on USB one line `@SB GET` or `@SB NET host=… ip=… mask=… gw=… dns=…`, answered with JSON between `\r\n@SB>` and `<@SB\r\n`. A settings password on the box blocks changes over the network (change them on its own page).

**Who can use the dashboard.** It has no login: anyone on the network can open it, so keep ScoreReader on the production network. Two things keep *web pages* out: every POST, and the two GETs that act (`/api/discover`, `/api/bridge/network`), need the header `X-ScoreReader: 1`, which another site's page can't send; and requests whose `Host` isn't an IP, `localhost`, a plain computer name or a `.local` name get 421, so a page can't point its own domain at this computer (DNS rebinding). If you open the dashboard by a real DNS name, add it with `SCOREREADER_ALLOWED_HOSTS=scores.example.org` (comma-separated).

## Scoreboard brands

ScoreReader can decode about 300 brand/sport layouts, which covers everything ScoreConnect III supports. Pick one in **Settings → Scoreboard**: the brand, then the sport or console layout, then the wiring if the brand has more than one. You can also start with `--scoreboard ID`; `--list-scoreboards` prints every ID.

- **Daktronics All Sport 5000 (built-in)** is the default. It detects football or soccer from the console by itself and handles known board quirks (such as soccer corners on the stats panel).
- **Bot setup:** when you choose another brand, ScoreReader sets up the ScoreBot for it the first time it connects, using the same commands and speed ScoreConnect uses, and remembers that. **Re-init bot** sends the setup again.
- **Not yet tested on real boards:** the other brands were written from ScoreConnect's decoder tables and tested with made-up data. If something reads wrong, send a capture (`logs/raw_*.bin`) along with the brand and sport. Each brand's wire format is in [vendors/](vendors/).

## Without a ScoreBot

ScoreReader only needs the console's raw bytes, so other hardware can stand in for the ScoreBot (details and build files in [`hardware/`](../hardware/README.md)):

- **Plain USB-RS232 adapter:** for RS-232 outputs (Colorado Timing, the All Sport 5000's J6 RTD port). Set **Source: Serial**, the adapter's port, and the console's baud.
- **ScoreBridge** (`hardware/scorebridge/`): our own board around a WT32-ETH01, with current-loop, RS-232 and RS-485 inputs.
  - Over Ethernet or Wi-Fi: the setup finds it by itself. By hand: **Source: Network**, the bridge's name or IP, port **10001**.
  - Over USB: set **Source: Serial** at **115200**.
  - The console's speed and polarity are set on the bridge's own web page.
- **Nano bridge** (`hardware/nano-bridge/`): an Arduino Nano and an H11L1 optocoupler, for current-loop consoles. Set **Source: Serial** at **115200**.

On these, ScoreReader can't set up the input for a brand automatically the way it does for the ScoreBot. Set the speed and polarity on the device itself. Brands the ScoreBot's own chip decodes (Nevco, Daktronics All Sport 4000, Fair-Play MP-69/70) still need the ScoreBot.

## Fixing a value that reads wrong (`custom_fields`)

Daktronics current-loop boards can be wired differently. To point a field at the right place:

1. Change the value on the console.
2. In the dashboard's address table, note which row (address) and which byte changed. The hex column counts from byte 0.
3. Add an entry like this to `scorereader_config.json`:

```json
"custom_fields": {
  "home_corners":  {"addr": 72, "bytes": [0, 1]},
  "guest_corners": {"addr": 72, "bytes": [10, 11]}
}
```

## Mac, Linux and Raspberry Pi details

- **Mac app (`ScoreReader.app`):** runs in the menu bar (status and score, Open dashboard, Re-init bot, Demo mode, Open logs folder, Quit) and the Dock, with no Terminal window. Quit with the menu bar's **Quit ScoreReader** or ⌘Q. Settings, logs and text files are in `~/Library/Application Support/ScoreReader`. Running from source (`mac/Start ScoreReader.command`) shows the same menu bar icon plus a Terminal window; closing that window leaves ScoreReader running in the menu bar. `--no-menubar` turns the icon off.
- **Windows:** the download is a `.zip` with a `ScoreReader` folder; extract it and run `ScoreReader.exe` (keep the folder together, its other files are needed). Settings and logs are stored in that folder. `ScoreReader.exe` shows the logo, and its window is titled "ScoreReader <version>". If Windows Defender still flags it, it's a false positive: allow it under **Windows Security → Protection history**, and report it at microsoft.com/wdsi/filesubmission. If the taskbar shows the Windows Terminal icon instead, set **Settings → System → For developers → Terminal** to **Windows Console Host**.
- **Serial port:** the bot shows up as `/dev/cu.usbmodem…` on a Mac and as `/dev/ttyACM0` or `/dev/scorebot` on Linux. Port `auto` finds it.
- **What `linux/install.sh` does:**
  - Installs `python3-serial` and `python3-hid`.
  - Adds a udev rule, so ScoreReader can reach the bot without root. The rule also creates `/dev/scorebot` and keeps ModemManager off the port.
  - Stops the kernel's `hid_mcp2221` driver from taking the bot's mode-switch line.
- **Start at boot:** `./linux/install.sh --service` installs a systemd service named `scorereader`. Check it with `systemctl status scorereader`, and follow its log with `journalctl -u scorereader -f`.
- **Raspberry Pi:** use 64-bit Raspberry Pi OS on a Pi 4 or 5 for the ready-made `ScoreReader-linux-arm64` build. Any Pi with Python 3 can run from source.

## Command line

```
scorereader.py [--port COM5] [--baud 19200] [--sport soccer] [--source scorelink|serial|tcp|demo|replay]
               [--scoreboard ID] [--list-scoreboards]
               [--vmix] [--vmix-input NAME] [--http-port 8765] [--mqtt-bot N] [--no-mqtt]
               [--list-ports] [--probe] [--configure-bot] [--sniff [SECONDS]] [--replay FILE]
               [--fresh] [--demo] [--version]
```

| Option | Use |
|---|---|
| `--demo` | a simulated football game, for building graphics without a console |
| `--list-ports` | serial ports, with the ScoreBot tagged |
| `--probe` | ask the bot for its firmware and settings, then leave it ready to read data |
| `--configure-bot` | re-send the Daktronics football bot setup (`M0X0X T0 D0 M8I1N`) |
| `--sniff 20` | show the raw bytes for 20 seconds |
| `--replay logs/raw_….bin` | play back a capture |
| `--list-scoreboards` | every supported brand/sport ID |
| `--scoreboard ID` | use that brand/sport for this session only (Settings saves it) |
| `--source serial` | no bot: a plain USB-serial adapter on the console's J6 RTD port |
| `--fresh` | forget remembered board values (only used with "Remember board values" on) |

Command-line options apply to that session only and are never saved.

## Settings file

Settings are stored in `scorereader_config.json` next to the program (on the Mac app, in `~/Library/Application Support/ScoreReader`), which is created on first run. The dashboard's Settings tab edits it for you; demo and replay are never saved, so ScoreReader always starts on live data. Every change applies as soon as you save, MQTT and the dashboard port included (after a port change the dashboard reopens on the new port; update the vMix data source URL to match). If the new dashboard port is already in use, nothing changes. Raw bytes are logged to `logs/raw_*.bin` unless you turn that off.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python scorereader.py --demo        # dashboard at http://localhost:8765
```

- **Code layout and contributor notes:** [CLAUDE.md](../CLAUDE.md).
- **Wire formats:** [PROTOCOL.md](PROTOCOL.md) for Daktronics and the ScoreBot, and [vendors/](vendors/) for every other brand.
- **CI:** runs the tests on Windows, macOS and Linux and builds one-file apps for Windows, macOS (Apple silicon), Linux and Raspberry Pi. Pushing a `v*` tag publishes a release with all four attached.
