#!/bin/bash
# Rebuild the ScoreBridge board from design.py and produce everything PCBWay needs.
#   ./build.sh            place -> autoroute -> ground pours -> DRC -> fab/ (Gerbers zip, BOM, pick-and-place)
#   ./build.sh --no-route keep the routing in scorebridge.kicad_pcb (after hand edits in KiCad), just redo fab/
# Needs KiCad 7 (kicad-cli and its Python module, /usr/bin/python3 on Debian/Ubuntu).
set -euo pipefail
cd "$(dirname "$0")"
PY=${KICAD_PYTHON:-/usr/bin/python3}
export KICAD7_FOOTPRINT_DIR=${KICAD7_FOOTPRINT_DIR:-/usr/share/kicad/footprints}
B=scorebridge.kicad_pcb

if [ "${1:-}" != "--no-route" ]; then
    $PY make_pcb.py place $B
    $PY route.py $B $B
    $PY make_pcb.py finish $B
fi
mkdir -p fab
$PY make_pcb.py drc $B fab/drc.rpt
errors=$(grep -E "Severity: error" fab/drc.rpt | wc -l)
echo "DRC: $errors errors (see fab/drc.rpt)"

rm -rf fab/gerbers && mkdir -p fab/gerbers
kicad-cli pcb export gerbers -o fab/gerbers/ --subtract-soldermask --no-x2 \
    -l F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts $B >/dev/null
kicad-cli pcb export drill -o fab/gerbers/ --format excellon --excellon-units mm --excellon-separate-th \
    --generate-map --map-format gerberx2 $B >/dev/null
(cd fab/gerbers && rm -f ../scorebridge-gerbers.zip && zip -q ../scorebridge-gerbers.zip *)
kicad-cli pcb export pos -o fab/scorebridge-cpl-raw.csv --format csv --units mm --side front $B >/dev/null
$PY - <<'EOF'
import csv, sys
sys.path.insert(0, ".")
import design as D
# BOM grouped by value + footprint + part number (PCBWay's columns)
groups = {}
for ref, p in D.P.items():
    if ref.startswith("H"):
        continue
    key = (p["value"], p["fp"].split(":")[1], p["mpn"])
    groups.setdefault(key, []).append(ref)
def refkey(r):
    import re
    m = re.match(r"([A-Z]+)(\d+)", r)
    return (m.group(1), int(m.group(2)))
tht = ("DSUB", "LED_D3", "DIP-6", "PinHeader", "WT32")
with open("fab/scorebridge-bom.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Item", "Designator", "Qty", "Value", "Package", "Mfg Part #", "Type", "Notes"])
    rows = sorted(groups.items(), key=lambda kv: refkey(sorted(kv[1], key=refkey)[0]))
    for i, ((value, fp, mpn), refs) in enumerate(rows, 1):
        refs = sorted(refs, key=refkey)
        notes = "; ".join(sorted({D.P[r]["note"] for r in refs if D.P[r]["note"]}))
        if refs[0].startswith("J3"):
            notes = "DO NOT FIT (optional programming header)"
        typ = "THT" if any(t in fp for t in tht) else "SMD"
        w.writerow([i, ",".join(refs), len(refs), value, fp, mpn, typ, notes])
# pick-and-place: SMD parts only, PCBWay column names
with open("fab/scorebridge-cpl-raw.csv") as f, open("fab/scorebridge-cpl.csv", "w", newline="") as g:
    r = csv.DictReader(f)
    w = csv.writer(g)
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    for row in r:
        ref = row["Ref"]
        fp = D.P.get(ref, {}).get("fp", "")
        if ref.startswith(("H", "J3")) or any(t in fp for t in tht):
            continue
        w.writerow([ref, row["PosX"], row["PosY"], "Top" if row["Side"] == "top" else "Bottom", row["Rot"]])
EOF
rm -f fab/scorebridge-cpl-raw.csv
echo "fab outputs:"; ls -1 fab
