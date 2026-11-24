"""ScoreBridge v1 carrier board: the single source of truth for parts, nets and placement.

make_pcb.py (KiCad's own Python, /usr/bin/python3) builds the board from this file and make_sch.py builds the
schematic, so the two can't drift apart. Coordinates are millimetres from the board's top-left corner, with the
port edge (DB9, USB-C, RJ45) along the top (y = 0) and the LEDs along the bottom edge.

To remap the DB9 (e.g. to match an existing Sportzcast fan-out cable) edit DB9_PINS and rebuild.
"""

BOARD_W = 75.0
BOARD_H = 61.0
CORNER_R = 3.0
TITLE = "ScoreBridge"
REVISION = "1.0"

# DB9 (male, PC-style) pin -> signal. RS-232 receive is on pin 2 and ground on pin 5, like a PC serial port,
# so a standard serial cable works for RS-232 sources.
DB9_PINS = {
    1: "CL_P",      # current loop +  (into the H11L1 LED)
    6: "CL_N",      # current loop -
    2: "RS232_IN",  # RS-232 data from the console (PC RXD pin)
    5: "GND",
    7: "RS485_A",   # RS-485 / RS-422 A (non-inverting)
    8: "RS485_B",   # RS-485 / RS-422 B (inverting)
    3: None, 4: None, 9: None,
}

# Mounting / case-screw holes (M3); the case posts clamp the board here.
HOLES = [(4.0, 17.0), (72.0, 19.5), (3.8, 57.2), (71.2, 57.2)]

# Area under the WT32's Wi-Fi antenna: no copper on any layer.
ANTENNA_KEEPOUT = (35.5, 51.0, 62.5, BOARD_H)

# ref: (value, footprint, symbol, (x, y, rotation), {pad: net}, mpn, note)
LIB = "scorebridge"
P = {}


def part(ref, value, fp, sym, pos, pads, mpn="", note=""):
    P[ref] = dict(value=value, fp=fp, sym=sym, pos=pos, pads=pads, mpn=mpn, note=note)


R0805 = "Resistor_SMD:R_0805_2012Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"

# --- WT32-ETH01 module (ESP32 + Ethernet + Wi-Fi), soldered straight onto its header pins
part("U1", "WT32-ETH01", f"{LIB}:WT32-ETH01", f"{LIB}:WT32-ETH01", (49.0, 30.5, 180), {
    2: "GND", 3: "+3V3", 4: "EN", 9: "GND", 10: "+3V3", 11: "GND", 12: "+5V",
    5: "LED_NET",        # IO32
    6: "LED_CLIENT",     # IO33
    14: "GND", 15: "LED_DATA",   # IO2 (strapping pin: the LED to GND keeps it low, which is what flashing needs)
    16: "RS485_GPIO",    # IO4
    17: "CL_RX",         # IO35 (input only)
    19: "RS232_GPIO",    # IO14
    23: "GND", 24: "IO0", 25: "RXD0", 26: "TXD0",
}, "WT32-ETH01", "Wireless-Tag; buy the version with header pins on the bottom")

# --- USB-C: power in + USB serial (CH340C) for setup, flashing and a USB data link
part("J1", "USB-C", "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12", "Connector:USB_C_Receptacle_USB2.0_16P",
     (68.5, 3.1, 180), {
    "A1": "GND", "A12": "GND", "B1": "GND", "B12": "GND",
    "A4": "VBUS", "A9": "VBUS", "B4": "VBUS", "B9": "VBUS",
    "A5": "CC1", "B5": "CC2", "A6": "USB_DP", "B6": "USB_DP", "A7": "USB_DM", "B7": "USB_DM",
    "S1": "GND"}, "TYPE-C-31-M-12", "HRO (LCSC C165948)")
part("R1", "5.1k", R0805, "Device:R", (72.9, 10.8, 90), {1: "CC1", 2: "GND"})
part("R2", "5.1k", R0805, "Device:R", (64.1, 10.8, 90), {1: "CC2", 2: "GND"})
part("U6", "USBLC6-2SC6", "Package_TO_SOT_SMD:SOT-23-6", "Power_Protection:USBLC6-2SC6", (68.5, 12.0, 0),
     {1: "USB_DP", 6: "USB_DP", 3: "USB_DM", 4: "USB_DM", 2: "GND", 5: "VBUS"}, "USBLC6-2SC6", "USB ESD")
part("F1", "500mA PTC", "Fuse:Fuse_1206_3216Metric", "Device:Polyfuse", (64.3, 16.6, 90), {1: "VBUS", 2: "+5V"},
     "MF-NSMF050-2", "resettable fuse, 0.5 A hold")
part("U2", "CH340C", "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm", "Interface_USB:CH340C", (68.8, 28.5, 0), {
    1: "GND", 2: "CH_TXD", 3: "TXD0", 4: "+3V3", 5: "USB_DP", 6: "USB_DM", 13: "DTR", 14: "RTS", 16: "+3V3"},
    "CH340C", "USB-UART, internal clock, runs at 3.3 V")
part("C3", "100n", C0805, "Device:C", (73.7, 28.5, 90), {1: "+3V3", 2: "GND"})
part("R7", "470", R0805, "Device:R", (63.6, 28.5, 90), {1: "CH_TXD", 2: "RXD0"})
# auto-program circuit (esptool toggles DTR/RTS)
part("Q1", "MMBT3904", "Package_TO_SOT_SMD:SOT-23", "Transistor_BJT:MMBT3904", (65.0, 36.0, 0),
     {1: "Q1_B", 2: "RTS", 3: "EN"}, "MMBT3904")
part("Q2", "MMBT3904", "Package_TO_SOT_SMD:SOT-23", "Transistor_BJT:MMBT3904", (70.5, 36.0, 0),
     {1: "Q2_B", 2: "DTR", 3: "IO0_AUTO"}, "MMBT3904")
part("R5", "10k", R0805, "Device:R", (65.0, 40.5, 0), {1: "DTR", 2: "Q1_B"})
part("R6", "10k", R0805, "Device:R", (70.5, 40.5, 0), {1: "RTS", 2: "Q2_B"})
part("C4", "1u", C0805, "Device:C", (73.8, 44.5, 90), {1: "EN", 2: "GND"}, note="slows EN on power-up")
part("SW1", "RESET", "Button_Switch_SMD:SW_Push_1P1T_NO_CK_KMR2", "Switch:SW_Push", (68.0, 45.5, 0),
     {1: "EN", 2: "GND"}, "KMR221GLFS")
part("C5", "10u", C0805, "Device:C", (64.6, 50.8, 90), {1: "+5V", 2: "GND"})
part("C6", "10u", C0805, "Device:C", (68.0, 50.8, 90), {1: "+3V3", 2: "GND"})
part("C7", "100n", C0805, "Device:C", (71.4, 50.8, 90), {1: "+3V3", 2: "GND"})
part("JP1", "AUTO-BOOT", "Jumper:SolderJumper-2_P1.3mm_Bridged_RoundedPad1.0x1.5mm", "Jumper:SolderJumper_2_Bridged", (33.6, 49.0, 90),
     {1: "IO0_AUTO", 2: "IO0"}, note="cut if Ethernet is ever flaky (IO0 carries the 50 MHz PHY clock)")
part("SW2", "BOOT", "Button_Switch_SMD:SW_Push_1P1T_NO_CK_KMR2", "Switch:SW_Push", (30.0, 44.5, 90),
     {1: "IO0", 2: "GND"}, "KMR221GLFS")

# --- current-loop input: optocoupler, isolated from everything else
part("R3", "100", "Resistor_SMD:R_1206_3216Metric", "Device:R", (17.2, 15.4, 0), {1: "CL_P", 2: "CL_PR"},
     note="1206; limits the LED current if the loop isn't current-regulated")
part("D2", "1N4148W", "Diode_SMD:D_SOD-123", "Device:D", (17.2, 18.8, 180), {1: "CL_PR", 2: "CL_N"}, "1N4148W",
     "reverse protection across the opto LED")
part("U3", "H11L1M", "Package_DIP:DIP-6_W7.62mm", "Isolator:H11L1", (22.0, 15.6, 0),
     {1: "CL_PR", 2: "CL_N", 4: "CL_RX", 5: "GND", 6: "+3V3"}, "H11L1M", "Schmitt-trigger optocoupler")
part("R4", "1k", R0805, "Device:R", (33.2, 22.6, 90), {1: "+3V3", 2: "CL_RX"})
part("C8", "100n", C0805, "Device:C", (33.2, 16.4, 90), {1: "+3V3", 2: "GND"})

# --- RS-485 / RS-422 input (receive only)
part("U5", "MAX3485E", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "Interface_UART:MAX3485", (11.5, 23.6, 0),
     {1: "RS485_RO", 2: "GND", 3: "GND", 4: "+3V3", 5: "GND", 6: "RS485_A", 7: "RS485_B", 8: "+3V3"},
     "MAX3485EESA+", "3.3 V RS-485 transceiver, receive only (alt. SP3485EN)")
part("C14", "100n", C0805, "Device:C", (16.9, 23.6, 90), {1: "+3V3", 2: "GND"})
part("R8", "120", R0805, "Device:R", (5.6, 23.4, 90), {1: "RS485_A", 2: "TERM"}, note="termination")
part("JP2", "TERM", "Jumper:SolderJumper-2_P1.3mm_Open_RoundedPad1.0x1.5mm", "Jumper:SolderJumper_2_Open", (5.6, 27.0, 90),
     {1: "TERM", 2: "RS485_B"}, note="bridge only at the end of an RS-485 line")
part("R9", "47k", R0805, "Device:R", (9.6, 29.0, 0), {1: "+3V3", 2: "RS485_A"}, note="idle bias")
part("R10", "47k", R0805, "Device:R", (14.0, 29.0, 0), {1: "RS485_B", 2: "GND"}, note="idle bias")
part("R12", "1k", R0805, "Device:R", (24.4, 25.8, 0), {1: "RS485_RO", 2: "RS485_GPIO"})

# --- RS-232 input
part("U4", "MAX3232E", "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm", "Interface_UART:MAX3232", (14.0, 37.5, 0), {
    1: "C1P", 2: "VP", 3: "C1N", 4: "C2P", 5: "C2N", 6: "VN", 10: "GND", 11: "GND", 12: "RS232_RX",
    13: "RS232_IN", 15: "GND", 16: "+3V3"}, "MAX3232EESE+", "RS-232 receiver (alt. SP3232EEN)")
part("C9", "100n", C0805, "Device:C", (8.0, 33.6, 90), {1: "C1P", 2: "C1N"})
part("C10", "100n", C0805, "Device:C", (8.0, 37.5, 90), {1: "C2P", 2: "C2N"})
part("C11", "100n", C0805, "Device:C", (8.0, 41.4, 90), {1: "VP", 2: "GND"})
part("C12", "100n", C0805, "Device:C", (20.2, 41.4, 90), {1: "GND", 2: "VN"})
part("C13", "100n", C0805, "Device:C", (20.2, 33.6, 90), {1: "+3V3", 2: "GND"})
part("R11", "1k", R0805, "Device:R", (24.4, 31.0, 0), {1: "RS232_RX", 2: "RS232_GPIO"})

# --- DB9 data connector
part("J2", "DB9 male", "Connector_Dsub:DSUB-9_Male_Horizontal_P2.77x2.84mm_EdgePinOffset7.70mm_Housed_MountingHolesOffset9.12mm",
     "Connector:DE9_Plug_MountingHoles", (23.96, 10.54, 180),
     dict({p: n for p, n in DB9_PINS.items() if n}, **{"0": "GND"}),
     "", "DE-9 male, right angle, metal bracket with #4-40 inserts, 25 mm hole spacing")

# --- programming header (optional, not fitted; fallback if the CH340 isn't used)
part("J3", "PROG", "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical", "Connector_Generic:Conn_01x06",
     (3.0, 33.6, 0), {1: "GND", 2: "+3V3", 3: "TXD0", 4: "RXD0", 5: "EN", 6: "IO0"}, note="optional, leave empty")

# --- LEDs on the bottom edge: PWR, NET, CLIENT, DATA (lenses poke through the case wall)
LED_FP = "LED_THT:LED_D3.0mm_Horizontal_O1.27mm_Z2.0mm"
for i, (ref, rref, name, net, colour) in enumerate([
        ("D3", "R13", "PWR", "+3V3", "red"), ("D4", "R14", "NET", "LED_NET", "green"),
        ("D5", "R15", "CLIENT", "LED_CLIENT", "yellow"), ("D6", "R16", "DATA", "LED_DATA", "orange")]):
    x = 9.0 + i * 6.5
    part(ref, f"{name} {colour}", LED_FP, "Device:LED", (x, 56.6, 0), {1: "GND", 2: f"{name}_A"},
         f"3 mm {colour} LED", f"{name} indicator")
    part(rref, "1k" if name == "PWR" else "330", R0805, "Device:R", (x + 1.27, 51.6, 90), {1: net, 2: f"{name}_A"})

# --- mounting holes
for i, (x, y) in enumerate(HOLES, 1):
    part(f"H{i}", "M3", "MountingHole:MountingHole_3.2mm_M3", "Mechanical:MountingHole", (x, y, 0), {})

POWER_NETS = {"GND", "+5V", "+3V3", "VBUS"}

LED_COLOURS = {"D3": "red", "D4": "green", "D5": "yellow", "D6": "orange"}
