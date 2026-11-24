# ScoreBridge

A small box that does a ScoreBot's job without a ScoreBot. It reads the scoreboard console on a **20 mA current loop**, **RS-232** or **RS-485/422** input, and streams the raw bytes, unchanged, to ScoreReader over **Ethernet**, **Wi-Fi** or **USB**. ScoreReader decodes them exactly as it decodes data from the ScoreBot.

```
console ──DB9──► ScoreBridge ──Ethernet / Wi-Fi──► ScoreReader   Source: TCP,    host <bridge IP>, port 10001
                             └──USB-C──────────────► ScoreReader   Source: Serial, baud 115200
```

The board is a carrier for a **WT32-ETH01** module (ESP32 with Ethernet and Wi-Fi). It adds the console inputs, USB-C power and a USB serial port, four status LEDs and a DB9 connector.

> **Status: v1.0, not built yet.** The board passes KiCad's design rule check, and its schematic and layout come from the same netlist. Nothing has been fabricated, printed or flashed yet, and the firmware hasn't been compiled. Build one and test it (below) before it goes to a game. The things most likely to need a tweak are listed under [Unverified](#unverified).

## What's here

| Path | What it is |
|---|---|
| `pcb/fab/scorebridge-gerbers.zip` | **Upload this to PCBWay** (Gerbers and drill files) |
| `pcb/fab/scorebridge-bom.csv`, `pcb/fab/scorebridge-cpl.csv` | Bill of materials and pick-and-place file, only needed if PCBWay assembles it |
| `pcb/scorebridge.kicad_pro` / `.kicad_pcb` / `.kicad_sch` | The KiCad 7 project. Open it to look around or to edit it. |
| `pcb/scorebridge.pdf` | Schematic, printable |
| `pcb/design.py` | The design itself: parts, nets, placement and DB9 pinout. The scripts below build everything from it. |
| `case/scorebridge_case.scad` | The case, in OpenSCAD |
| `case/stl/` | Ready-to-print `bottom.stl`, `top.stl` and `fit-coupon.stl` |
| `firmware/` | ESP32 firmware (PlatformIO, or Arduino IDE via `ScoreBridge/ScoreBridge.ino`) |
| `images/` | Renders of the board and case |

## 1. Order the board

On PCBWay, choose **PCB Instant Quote**, upload `pcb/fab/scorebridge-gerbers.zip`, and use these settings:

| Setting | Value |
|---|---|
| Layers | 2 |
| Size | 75 × 61 mm (read from the files) |
| Material / thickness | FR-4, **1.6 mm** (the case is designed for 1.6 mm) |
| Min track / spacing | 6/6 mil (the design uses 0.15 mm clearance and 0.25 mm tracks) |
| Min hole | 0.3 mm |
| Surface finish | HASL lead-free (or ENIG) |
| Copper | 1 oz |
| Solder mask / silkscreen | Any colour |

Every part can be hand-soldered (0805 passives, SOIC chips, through-hole connectors). If you'd rather PCBWay fit the small parts, add **PCB Assembly** and upload the BOM and CPL. The CPL only lists the SMD parts; you solder the through-hole parts and the WT32 yourself.

## 2. Parts

The full list is in `pcb/fab/scorebridge-bom.csv`. The parts worth choosing carefully:

| Part | Buy | Notes |
|---|---|---|
| U1 | **WT32-ETH01** (the original ESP32 version, *not* the "-EVO") | Get the one with header pins on the bottom. ETH02 boards use the same pinout. |
| J2 | DE-9 **male**, right angle, **with a metal bracket**, PCB mount | Its footprint needs the front pin row 7.7 mm behind the board edge and 25 mm mounting holes. Check the datasheet drawing before ordering. |
| U3 | H11L1M (DIP-6) | Optocoupler with a Schmitt trigger; this is what makes a clean current-loop input |
| U4 / U5 | MAX3232E / MAX3485E (or SP3232EEN / SP3485EN) | The 3.3 V versions |
| U2 | CH340C | USB serial; needs no crystal |
| J1 | HRO TYPE-C-31-M-12 | 16-pin USB-C |
| D3–D6 | 3 mm LEDs: red, green, yellow, orange | Bent 90° so they point out of the front edge (PWR, NET, CLIENT, DATA) |
| Case | 4× M3 × 12 mm screws, 4× M3 heat-set inserts (M3 × 5.7, 4.0 mm hole) | Or M3 self-tapping screws; set `insert_d = 2.6` in the .scad |

## 3. Assemble

1. **SMD parts first:** CH340C, MAX3232, MAX3485, USBLC6, transistors, passives, then the USB-C connector.
2. **Through-hole parts:**
   - The H11L1.
   - The four LEDs: lay them flat with the lens past the front edge, longer leg (anode) in the round pad.
   - The DB9.
   - Leave J3 empty.
3. **WT32-ETH01 last.**
   - **Check its orientation before soldering:** the RJ45 points out over the back edge, and the module's row labelled **5V / 3V3 / GND / EN** is on the USB-C side. The row with IO0, IO1 and IO3 is on the DB9 side.
   - **Seat it:** push the header pins through until the black plastic spacer sits on the board, then solder. This sets the height the case expects.
   - **Trim** the leads underneath to under 4 mm.
4. **Solder jumpers:**
   - JP1 comes bridged; leave it. It lets the USB port flash the ESP32 automatically.
   - JP2 stays open. Bridge it only if this is the last device on an RS-485 line.

## 4. Flash and set up

1. **Connect USB:** plug USB-C into a computer. The PWR LED lights and a COM port appears; Windows usually installs the CH340 driver by itself.
2. **Flash the firmware:**
   - PlatformIO: `cd firmware && pio run -t upload`.
   - Arduino IDE: install the "esp32" boards, open `firmware/ScoreBridge/ScoreBridge.ino`, choose "ESP32 Dev Module" and upload.
   - If the upload doesn't start, hold **BOOT**, tap **RESET** and release BOOT.
   - Later updates can go over the network: the settings page → **Firmware update**, upload the `.bin`.
3. **Connect to its settings page:**
   - **On Ethernet:** plug in a cable. The NET LED goes solid once it has an address. Open `http://scorebridge.local/`, or look up its IP in your router's DHCP list.
   - **No network:** after 15 seconds it starts its own Wi-Fi network, **ScoreBridge-XXXX**, password `scorebridge`; the NET LED blinks. Join it and open `http://192.168.4.1/`.
4. **Pick the console:** click a preset on the settings page (Daktronics current loop, Electro-Mech current loop, Colorado Timing V1/V2, Daktronics RTD), then **Save**. The settings page shows bytes arriving, live.
5. **Set up ScoreReader:**
   - Settings → **Source: TCP**.
   - **Host:** the bridge's IP address. A fixed IP (set on the bridge's page, or a DHCP reservation) is the most reliable choice on game day.
   - **Port:** `10001`.
   - Pick the scoreboard brand as usual. The CLIENT LED lights while ScoreReader is connected.
   - Over USB instead: **Source: Serial**, its COM port, **Baud: 115200**.
6. **Check the data:** the **Last bytes** line on the bridge's page shows the raw data. Over USB, `python scorereader.py --port COM7 --baud 115200 --sniff 20` shows the same.
   - Daktronics current loop: `55 AA` frames.
   - Electro-Mech: bytes alternating between under `80` and `80`-or-over.
   - Garbage: untick or tick **Invert** on the bridge's page.

**Resetting the network settings:** power-cycle it three times within a few seconds of it starting. That clears the network settings and the settings password, and opens the setup Wi-Fi network. The BOOT button can't do this, because on the WT32 that pin carries the Ethernet clock.

## 5. Wire the console

The DB9 is male, with the pins like a PC serial port for RS-232:

| DB9 pin | Signal | Use |
|---|---|---|
| 1 | Current loop + | Daktronics / Electro-Mech scoreboard driver output |
| 6 | Current loop − | |
| 2 | RS-232 in (console TX) | Colorado Timing, Daktronics J6 RTD |
| 5 | Ground | RS-232 / RS-485 ground (**not** the current loop, which is isolated) |
| 7 | RS-485 / 422 A (+) | |
| 8 | RS-485 / 422 B (−) | |

- **Current loop:** the H11L1 isolates this input from everything else, so the console's loop and your network equipment never share a ground.
  - **Measure the port first:** a regulated 20 mA loop is what it expects (the 100 Ω resistor limits the current otherwise).
  - **If there's no data:** swap the + and − wires, or flip **Invert**.
- **Reusing the Sportzcast fan-out cable:** use a meter to find which DB9 pin goes to each plug on the fan-out. If they differ from the table, change `DB9_PINS` in `pcb/design.py` and rebuild *before* ordering boards, and the cable plugs straight in.
- **ScoreBot-decoded brands won't work:** brands where the ScoreBot's own chip decodes the data (Nevco, Daktronics All Sport 4000, Fair-Play MP-69/70) still need the ScoreBot. ScoreBridge passes raw bytes; Daktronics All Sport 5000, Electro-Mech and Colorado Timing are all raw.

## 6. Print the case

Ports, LEDs and screw ears on the case:

| Side | What's there |
|---|---|
| Back | DB9 (CONSOLE), Ethernet, USB-C |
| Front | The four LEDs |
| Left and right | Screw-down ears for a permanent install, for #8 / 4 mm countersunk screws |

The two halves are held together by four M3 screws from underneath, which also clamp the board.

1. **Print `case/stl/fit-coupon.stl` first** (just the back wall, about 15 minutes) and hold it against the assembled board. If a cut-out is off, adjust the heights at the top of `scorebridge_case.scad` (`DB9_Z`, `USB_Z`, `RJ45_BOTTOM` …) and re-export.
2. **Print `bottom.stl` and `top.stl`:** PETG or PLA, 0.2 mm layers, 3 walls, no supports. Both are already oriented for printing.
3. **Fit the inserts:** press the heat-set inserts into the four posts in the lid.
4. **Prepare the DB9:** unscrew the DB9's two hex screwlocks.
5. **Close it up:** drop the board into the bottom half, put the lid on and fit the four M3 screws from underneath.
6. **Refit the screwlocks** from the outside, through the back wall; this also holds the connector to the case.

Exporting the STLs again after a change:

```bash
cd case
for p in bottom top coupon; do openscad -D "part=\"$p\"" -o stl/$p.stl scorebridge_case.scad; done
```

## Firmware reference

| | |
|---|---|
| TCP port | 10001 (changeable). Raw bytes to every connected client, up to 4 at once, so two ScoreReader machines can share one console. |
| USB | Raw bytes at 115200 (can be turned off on the settings page) |
| LEDs | **PWR** power · **NET** solid = Ethernet or Wi-Fi up, blinking = setup Wi-Fi network · **CLIENT** ScoreReader connected · **DATA** flickers with every byte from the console |
| `GET /api/status` | JSON: network, clients, input settings, byte counters, the last bytes in hex |
| `POST /api/serial` | `input=cl\|rs232\|rs485&baud=19200&invert=1&format=8N1[&save=1]`: switch the input without the page (meant for ScoreReader to set it from the chosen scoreboard later) |
| Settings password | Optional (user `admin`); also protects firmware updates |

## Rebuilding the design files

Everything is generated from `pcb/design.py`, so edit that, never the KiCad files by hand, unless you're finishing the routing in KiCad yourself.

```bash
cd pcb
./build.sh                 # KiCad 7: place, autoroute (route.py), ground pours, DRC, Gerbers/BOM/CPL in fab/
python3 make_sch.py        # schematic (pip install kiutils==1.4.8)
kicad-cli sch export netlist -o /tmp/sb.net scorebridge.kicad_sch && python3 check_netlist.py /tmp/sb.net
```

`route.py` is a small autorouter written for this board. Its routing is valid (DRC-clean) but not pretty. You can rip up and reroute anything in KiCad and then run `./build.sh --no-route` to regenerate the fab files from your edits.

## Unverified

- **WT32-ETH01 position and size:** the pin positions come from the datasheet's pin table and the community footprint at github.com/egnor/wt32-eth01. Sources disagree on how far the RJ45 overhangs the module's edge (5–8 mm), so the case cut-out allows for either. Check with the fit coupon.
- **DB9 shell height:** assumed 6.3 mm above the board (`DB9_Z`); it depends on the connector you buy. Check with the fit coupon.
- **Firmware:** not compiled yet, because the ESP32 toolchain couldn't be downloaded in the environment where it was written. It targets the Arduino-ESP32 core 2.0.x and 3.x; the first `pio run` will show any typo.
- **Current-loop polarity and Invert:** what the Daktronics and Electro-Mech loops need with this optocoupler is a guess (the ScoreBot reads both inverted). Flipping the setting is free.
- **Routing:** the board is autorouted. It passes DRC, but a person should look it over, especially the USB pair, before ordering a big batch.
