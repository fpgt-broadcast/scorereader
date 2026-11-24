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
errors=$(grep -cE "Severity: error" fab/drc.rpt || true)
echo "DRC: $errors errors (see fab/drc.rpt)"

rm -rf fab/gerbers && mkdir -p fab/gerbers
kicad-cli pcb export gerbers -o fab/gerbers/ --subtract-soldermask --no-x2 \
    -l F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts $B >/dev/null
kicad-cli pcb export drill -o fab/gerbers/ --format excellon --excellon-units mm --excellon-separate-th \
    --generate-map --map-format gerberx2 $B >/dev/null
(cd fab/gerbers && rm -f ../scorebridge-gerbers.zip && zip -q ../scorebridge-gerbers.zip *)
kicad-cli pcb export pos -o fab/scorebridge-cpl-raw.csv --format csv --units mm --side front $B >/dev/null
$PY - <<'EOF'
import csv, re, sys
sys.path.insert(0, ".")
import design as D
THT = ("DSUB", "LED_D3", "DIP-6", "PinHeader", "WT32")
def refkey(r):
    m = re.match(r"([A-Z]+)(\d+)", r)
    return (m.group(1), int(m.group(2)))
def is_tht(ref):
    return any(t in D.P[ref]["fp"] for t in THT)
def write_bom(path, refs):
    groups = {}
    for ref in refs:
        p = D.P[ref]
        groups.setdefault((p["value"], p["fp"].split(":")[1], p["mpn"]), []).append(ref)
    rows = sorted(groups.items(), key=lambda kv: refkey(sorted(kv[1], key=refkey)[0]))
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["Item", "Designator", "Qty", "Manufacturer", "Mfg Part #", "Description", "Package", "Type", "Notes"])
        for i, ((value, fp, mpn), rs) in enumerate(rows, 1):
            rs = sorted(rs, key=refkey)
            notes = "; ".join(sorted({D.P[r]["note"] for r in rs if D.P[r]["note"]}))
            if rs[0] in D.NOT_PLACED:
                notes = ("DO NOT FIT. " + notes).strip()
            w.writerow([i, ",".join(rs), len(rs), D.manufacturer(mpn), mpn, value, fp,
                        "THT" if is_tht(rs[0]) else "SMD", notes])
parts = [r for r in D.P if not r.startswith("H")]
write_bom("fab/scorebridge-bom-full.csv", parts)
placed = [r for r in parts if not is_tht(r) and r not in D.NOT_PLACED]
write_bom("fab/scorebridge-bom.csv", placed)  # what PCBWay assembles
with open("fab/scorebridge-cpl-raw.csv") as f, open("fab/scorebridge-cpl.csv", "w", newline="") as g:
    w = csv.writer(g, lineterminator="\n")
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    for row in csv.DictReader(f):
        if row["Ref"] in placed:
            w.writerow([row["Ref"], row["PosX"], row["PosY"], "Top" if row["Side"] == "top" else "Bottom", row["Rot"]])
print(f"assembly: {len(placed)} parts placed")
EOF
rm -f fab/scorebridge-cpl-raw.csv
echo "fab outputs:"; ls -1 fab
