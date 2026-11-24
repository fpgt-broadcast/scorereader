#!/usr/bin/env python3
"""Compare a KiCad netlist exported from the schematic against design.py (the board's source).
   kicad-cli sch export netlist -o /tmp/sb.net scorebridge.kicad_sch && python3 check_netlist.py /tmp/sb.net"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design as D  # noqa: E402

text = open(sys.argv[1]).read()
got = {}
for m in re.finditer(r'\(net \(code "?\d+"?\) \(name "([^"]*)"\)(.*?)\)\s*(?=\(net |\)\s*\)\s*$)', text, re.S):
    name = m.group(1).lstrip("/")
    for r, p in re.findall(r'\(node \(ref "([^"]+)"\) \(pin "([^"]+)"\)', m.group(2)):
        if not r.startswith("#"):
            got[(r, p)] = name
want = {(ref, str(pad)): net for ref, part in D.P.items() for pad, net in part["pads"].items()}
# the USB-C footprint has stacked pads the symbol shows once; the DB9 shell pin is "0" in both
problems = []
for k, net in want.items():
    if got.get(k) != net:
        problems.append(f"{k[0]}.{k[1]}: board {net}, schematic {got.get(k)}")
for k, net in got.items():
    if not net.startswith("unconnected-") and not net.startswith("Net-") and k not in want:
        problems.append(f"{k[0]}.{k[1]}: schematic {net}, board none")
print("\n".join(problems) if problems else f"schematic matches design.py ({len(want)} pin connections)")
sys.exit(1 if problems else 0)
