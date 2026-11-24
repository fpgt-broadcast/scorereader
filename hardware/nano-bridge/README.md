# Nano current-loop bridge (USB)

A quick stand-in for the ScoreBot on a **current-loop** console (Daktronics All Sport 5000 scoreboard output, Electro-Mech), built from an Arduino Nano (a left-over CrunchLabs board works) and an optocoupler. It plugs into the computer by USB, and ScoreReader reads it as a plain serial port. For Ethernet, Wi-Fi and RS-232/RS-485 inputs, build the [ScoreBridge](../scorebridge/README.md) instead.

## Wiring

```
loop + ──[100 Ω]──┬──► H11L1 pin 1 (LED anode)
                  ├─|◄─ 1N4148 (cathode to pin 1), protects against reverse wiring
loop − ───────────┴──── H11L1 pin 2 (LED cathode)

H11L1 pin 6 (VCC) ── Nano 5V          H11L1 pin 5 (GND) ── Nano GND
H11L1 pin 4 (out) ──┬── Nano D8
                    └── 1 kΩ ── Nano 5V
```

Jumpers to GND, read when the Nano starts:

| D4 | D5 | Console speed |
|---|---|---|
| open | open | 19200 (Daktronics current loop) |
| GND | open | 2400 (Electro-Mech) |
| open | GND | 9600 |
| GND | GND | 38400 |

Polarity:
- **D6 open:** inverted, the usual setting for a current loop.
- **D6 to GND:** normal. Use it if the data comes out garbled.

Measure the console port first. With a meter on mA across its two terminals, a regulated loop reads about 20 mA.

## Use

1. Upload `nano-bridge/nano-bridge.ino` with the Arduino IDE (board "Arduino Nano"; try "ATmega328P (Old Bootloader)" if the upload fails).
2. In ScoreReader, set:
   - **Source:** Serial
   - **Port:** the Nano's COM port
   - **Baud:** **115200**, whatever the console's own speed is
   - **Scoreboard:** blank for Daktronics, `Electromech_<sport>` for Electro-Mech
3. Check the data with `python scorereader.py --port COM7 --baud 115200 --sniff 20`:
   - Daktronics: `55 AA` frames.
   - Electro-Mech: bytes alternating between under `0x80` and `0x80`-or-over.
   - Garbage: put the jumper on D6 (or take it off) and reset the Nano.

Notes:
- **CH340 driver:** most Nano clones use a CH340 USB chip. If no COM port appears, install its driver.
- **Reset on connect:** opening the port resets the Nano. That's harmless; the console keeps sending.
- **Not tested on hardware:** check it with `--sniff` before a game.
