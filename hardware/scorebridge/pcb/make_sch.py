#!/usr/bin/env python3
"""Build scorebridge.kicad_sch from design.py (needs `pip install kiutils==1.4.8` and KiCad 7's symbol libraries).

Every pin gets a net label instead of drawn wires, so the schematic always matches design.py (and therefore the
board). Pins with no net get a no-connect flag. Also writes the project symbol library (the WT32-ETH01 symbol).
"""
import copy
import os
import sys
import uuid

from kiutils.items.common import Effects, Fill, Font, Justify, Position, Property, Stroke
from kiutils.items.schitems import (HierarchicalSheetInstance, LocalLabel, NoConnect, SchematicSymbol,
                                    SymbolProjectInstance, SymbolProjectPath, Text)
from kiutils.items.syitems import SyRect
from kiutils.schematic import Schematic
from kiutils.symbol import Symbol, SymbolLib, SymbolPin

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design as D  # noqa: E402

SYM_ROOT = os.environ.get("KICAD7_SYMBOL_DIR", "/usr/share/kicad/symbols")
PROJECT = "scorebridge"

# where each part goes on the A3 sheet (mm, KiCad schematic coordinates: y down)
PLACE = {
    "J1": (40, 55), "R1": (70, 40), "R2": (80, 40), "U6": (100, 75), "F1": (75, 95), "C5": (90, 105),
    "U2": (150, 65), "C3": (130, 40), "R7": (180, 45), "Q1": (205, 75), "Q2": (205, 100), "R5": (185, 75),
    "R6": (185, 100), "C4": (230, 60), "SW1": (230, 85), "C6": (110, 105), "C7": (125, 105),
    "U1": (310, 80), "JP1": (360, 115), "SW2": (375, 130), "J3": (370, 55),
    "J2": (35, 185), "R3": (70, 165), "D2": (80, 180), "U3": (110, 175), "R4": (130, 160), "C8": (125, 150),
    "U5": (110, 235), "C14": (140, 220), "R8": (65, 225), "JP2": (65, 245), "R9": (80, 215), "R10": (80, 255),
    "R12": (150, 245),
    "U4": (230, 200), "C9": (190, 180), "C10": (270, 180), "C11": (275, 205), "C12": (275, 225),
    "C13": (215, 160), "R11": (195, 225),
    "R13": (320, 175), "R14": (340, 175), "R15": (360, 175), "R16": (380, 175),
    "D3": (320, 200), "D4": (340, 200), "D5": (360, 200), "D6": (380, 200),
    "H1": (320, 250), "H2": (335, 250), "H3": (350, 250), "H4": (365, 250),
}
BLOCKS = [
    ("USB-C power, ESD protection, 500 mA fuse", 20, 22),
    ("USB serial (CH340C) and auto-program circuit", 125, 22),
    ("WT32-ETH01: ESP32 + Ethernet + Wi-Fi", 280, 22),
    ("DB9 console input. Current loop is isolated by the H11L1", 20, 140),
    ("RS-485 / RS-422 (receive only)", 60, 200),
    ("RS-232", 185, 140),
    ("Status LEDs (front edge)", 310, 155),
]


def new_uuid():
    return str(uuid.uuid4())


_libs = {}


def lib_symbols(lib):
    if lib not in _libs:
        _libs[lib] = SymbolLib.from_file(os.path.join(SYM_ROOT, f"{lib}.kicad_sym"))
    return _libs[lib]


def flatten(lib, name):
    """Library symbol with any `extends` resolved (schematics embed flattened copies)."""
    syms = {s.entryName: s for s in lib_symbols(lib).symbols}
    s = syms[name]
    if s.extends:
        parent = flatten(lib, s.extends)
        out = copy.deepcopy(parent)
        out.entryName = name
        out.extends = None
        out.properties = copy.deepcopy(s.properties)
        for u in out.units:
            u.entryName = name
    else:
        out = copy.deepcopy(s)
    out.libraryNickname = lib
    return out


def wt32_symbol():
    pins_l = [(1, "EN1", "input"), (2, "GND", "power_in"), (3, "3V3", "power_out"), (4, "EN", "input"),
              (5, "IO32", "bidirectional"), (6, "IO33", "bidirectional"), (7, "IO5", "bidirectional"),
              (8, "IO17", "bidirectional"), (9, "GND", "passive"), (10, "3V3", "power_out"), (11, "GND", "passive"),
              (12, "5V", "power_in"), (13, "LINK", "output")]
    pins_r = [(26, "IO1/TXD0", "bidirectional"), (25, "IO3/RXD0", "bidirectional"), (24, "IO0/BOOT/ETH_CLK", "bidirectional"),
              (23, "GND", "passive"), (22, "IO39", "input"), (21, "IO36", "input"), (20, "IO15", "bidirectional"),
              (19, "IO14", "bidirectional"), (18, "IO12", "bidirectional"), (17, "IO35", "input"),
              (16, "IO4", "bidirectional"), (15, "IO2", "bidirectional"), (14, "GND", "passive")]
    name = "WT32-ETH01"
    s = Symbol(entryName=name, libraryNickname=D.LIB, inBom=True, onBoard=True)
    s.pinNamesOffset = 1.016
    eff = Effects(font=Font(height=1.27, width=1.27))
    hid = Effects(font=Font(height=1.27, width=1.27), hide=True)
    s.properties = [
        Property(key="Reference", value="U", id=0, position=Position(0, 19.05, 0), effects=eff),
        Property(key="Value", value=name, id=1, position=Position(0, -19.05, 0), effects=eff),
        Property(key="Footprint", value=f"{D.LIB}:{name}", id=2, position=Position(0, -21.59, 0), effects=hid),
        Property(key="Datasheet", value="https://github.com/egnor/wt32-eth01", id=3, position=Position(0, -24.13, 0), effects=hid),
    ]
    g = Symbol(entryName=name, unitId=0, styleId=1)
    g.graphicItems = [SyRect(start=Position(-12.7, 16.51), end=Position(12.7, -16.51),
                             stroke=Stroke(width=0.254), fill=Fill(type="background"))]
    p = Symbol(entryName=name, unitId=1, styleId=1)
    for i, (num, pname, et) in enumerate(pins_l):
        p.pins.append(SymbolPin(electricalType=et, graphicalStyle="line", position=Position(-15.24, 15.24 - i * 2.54, 0),
                                length=2.54, name=pname, number=str(num)))
    for i, (num, pname, et) in enumerate(pins_r):
        p.pins.append(SymbolPin(electricalType=et, graphicalStyle="line", position=Position(15.24, 15.24 - i * 2.54, 180),
                                length=2.54, name=pname, number=str(num)))
    s.units = [g, p]
    return s


def symbol_pins(sym):
    out = []
    for u in [sym] + list(sym.units):
        if u is not sym and (u.unitId not in (0, 1) or u.styleId not in (0, 1)):
            continue
        out.extend(u.pins)
    return out


def label(text, x, y, pin_angle):
    ang = (pin_angle + 180) % 360
    just = "left" if ang in (0, 90) else "right"
    return LocalLabel(text=text, position=Position(round(x, 2), round(y, 2), ang),
                      effects=Effects(font=Font(height=1.27, width=1.27), justify=Justify(horizontally=just)),
                      uuid=new_uuid())


def build(out_path):
    sch = Schematic.create_new()
    sch.version = 20230121
    sch.generator = "scorebridge"
    sch.paper.paperSize = "A3"
    sch.titleBlock = None
    root = sch.uuid = new_uuid()
    sch.sheetInstances = [HierarchicalSheetInstance(instancePath="/", page="1")]

    # project symbol library (WT32-ETH01)
    wt = wt32_symbol()
    plib = SymbolLib(version="20220914", generator="scorebridge")
    plib.symbols = [copy.deepcopy(wt)]
    plib.to_file(os.path.join(HERE, f"{D.LIB}.kicad_sym"))

    embedded = {}

    def get_sym(libid):
        if libid not in embedded:
            lib, name = libid.split(":")
            embedded[libid] = wt if lib == D.LIB else flatten(lib, name)
        return embedded[libid]

    for ref, p in D.P.items():
        sym = get_sym(p["sym"])
        X, Y = PLACE[ref]
        ss = SchematicSymbol(libraryNickname=sym.libraryNickname, entryName=sym.entryName,
                             position=Position(X, Y, 0), unit=1, inBom=not ref.startswith("H"), onBoard=True,
                             uuid=new_uuid())
        props = []
        for prop in sym.properties:
            if prop.key.startswith("ki_"):
                continue
            q = copy.deepcopy(prop)
            q.position = Position(X + prop.position.X, Y - prop.position.Y, prop.position.angle)
            if q.key == "Reference":
                q.value = ref
            elif q.key == "Value":
                q.value = p["value"]
            elif q.key == "Footprint":
                q.value = p["fp"]
            props.append(q)
        nid = max([q.id for q in props if q.id is not None] + [3]) + 1
        if p["mpn"]:
            props.append(Property(key="MPN", value=p["mpn"], id=nid, position=Position(X, Y, 0),
                                  effects=Effects(font=Font(height=1.27, width=1.27), hide=True)))
        ss.properties = props
        pins = symbol_pins(sym)
        ss.pins = {pin.number: new_uuid() for pin in pins}
        ss.instances = [SymbolProjectInstance(name=PROJECT, paths=[
            SymbolProjectPath(sheetInstancePath=f"/{root}", reference=ref, unit=1)])]
        sch.schematicSymbols.append(ss)

        nets = {str(k): v for k, v in p["pads"].items()}
        done = set()
        for pin in pins:
            x, y = X + pin.position.X, Y - pin.position.Y
            key = (round(x, 2), round(y, 2))
            if key in done:
                continue
            done.add(key)
            net = nets.get(pin.number)
            if net:
                sch.labels.append(label(net, x, y, pin.position.angle))
            elif pin.electricalType != "no_connect":
                sch.noConnects.append(NoConnect(position=Position(round(x, 2), round(y, 2)), uuid=new_uuid()))

    # power flags so the supply nets are driven (ERC)
    get_sym("power:PWR_FLAG")  # embed the symbol
    for i, net in enumerate(["VBUS", "+5V", "GND"]):
        X, Y = 20 + i * 15, 118
        ss = SchematicSymbol(libraryNickname="power", entryName="PWR_FLAG", position=Position(X, Y, 0), unit=1,
                             inBom=False, onBoard=True, uuid=new_uuid())
        ss.properties = [Property(key="Reference", value=f"#FLG0{i + 1}", id=0, position=Position(X, Y - 3, 0),
                                  effects=Effects(font=Font(height=1.27, width=1.27), hide=True)),
                         Property(key="Value", value="PWR_FLAG", id=1, position=Position(X, Y - 3.8, 0),
                                  effects=Effects(font=Font(height=1.27, width=1.27)))]
        ss.pins = {"1": new_uuid()}
        ss.instances = [SymbolProjectInstance(name=PROJECT, paths=[
            SymbolProjectPath(sheetInstancePath=f"/{root}", reference=f"#FLG0{i + 1}", unit=1)])]
        sch.schematicSymbols.append(ss)
        sch.labels.append(label(net, X, Y, 90))

    sch.libSymbols = list(embedded.values())
    for title, x, y in BLOCKS:
        sch.texts.append(Text(text=title, position=Position(x, y, 0),
                              effects=Effects(font=Font(height=2.0, width=2.0, bold=True), justify=Justify(horizontally="left")),
                              uuid=new_uuid()))
    notes = [
        f"{D.TITLE} v{D.REVISION}: WT32-ETH01 carrier that streams a scoreboard console's data to ScoreReader over Ethernet, Wi-Fi or USB.",
        "Inputs: current loop -> IO35, RS-232 -> IO14, RS-485 -> IO4. LEDs: NET IO32, CLIENT IO33, DATA IO2 (PWR on 3V3).",
        "DB9 pinout: " + ", ".join(f"{p} {n}" for p, n in sorted(D.DB9_PINS.items()) if n) + " (edit DB9_PINS in design.py).",
        "JP1 (bridged): auto-program on IO0, cut it if Ethernet misbehaves (IO0 is the PHY's 50 MHz clock). JP2: 120R RS-485 termination.",
        "Generated by make_sch.py from design.py: change design.py and rebuild rather than editing this sheet by hand.",
    ]
    for i, t in enumerate(notes):
        sch.texts.append(Text(text=t, position=Position(20, 268 + i * 4.5, 0),
                              effects=Effects(font=Font(height=1.8, width=1.8), justify=Justify(horizontally="left")),
                              uuid=new_uuid()))
    sch.to_file(out_path)

    with open(os.path.join(HERE, "sym-lib-table"), "w") as f:
        f.write('(sym_lib_table\n  (version 7)\n  (lib (name "scorebridge")(type "KiCad")(uri "${KIPRJMOD}/scorebridge.kicad_sym")(options "")(descr "ScoreBridge parts"))\n)\n')


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "scorebridge.kicad_sch"))
    print("schematic written")
