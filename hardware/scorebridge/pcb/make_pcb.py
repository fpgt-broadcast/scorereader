#!/usr/bin/python3
"""Build scorebridge.kicad_pcb from design.py (run with KiCad 7's Python: /usr/bin/python3 make_pcb.py).

  make_pcb.py place  board.kicad_pcb            footprints, nets, outline, silkscreen, antenna keep-out
  make_pcb.py dsn    board.kicad_pcb out.dsn    export for Freerouting
  make_pcb.py finish board.kicad_pcb in.ses     add Freerouting's tracks, ground pours, fill
  make_pcb.py drc    board.kicad_pcb report.rpt
`./build.sh` runs the whole pipeline, then makes the fab outputs.
"""
import os
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design as D  # noqa: E402

FP_ROOT = os.environ.get("KICAD7_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
OX, OY = 100.0, 100.0  # where the board sits on KiCad's page


def mm(v):
    return pcbnew.FromMM(v)


def pt(x, y):
    return pcbnew.VECTOR2I(mm(OX + x), mm(OY + y))


def lib_path(lib):
    if lib == D.LIB:
        return os.path.join(HERE, f"{D.LIB}.pretty")
    return os.path.join(FP_ROOT, f"{lib}.pretty")


def setup_rules(board):
    ds = board.GetDesignSettings()
    ds.m_TrackMinWidth = mm(0.2)
    ds.m_ViasMinSize = mm(0.6)
    ds.m_ViasMinAnnularWidth = mm(0.15)
    ds.m_MinThroughDrill = mm(0.3)
    ds.m_MinClearance = mm(0.15)
    ds.m_CopperEdgeClearance = mm(0.4)
    ds.m_HoleToHoleMin = mm(0.25)
    ns = ds.m_NetSettings
    default = ns.m_DefaultNetClass
    default.SetClearance(mm(0.15))
    default.SetTrackWidth(mm(0.25))
    default.SetViaDiameter(mm(0.6))
    default.SetViaDrill(mm(0.3))
    power = pcbnew.NETCLASS("Power")
    power.SetClearance(mm(0.15))
    power.SetTrackWidth(mm(0.5))
    power.SetViaDiameter(mm(0.8))
    power.SetViaDrill(mm(0.4))
    ns.m_NetClasses["Power"] = power
    return power


def outline(board):
    w, h, r = D.BOARD_W, D.BOARD_H, D.CORNER_R
    layer = pcbnew.Edge_Cuts

    def seg(a, b):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(*a))
        s.SetEnd(pt(*b))
        s.SetLayer(layer)
        s.SetWidth(mm(0.1))
        board.Add(s)

    def arc(c, start, mid, end):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_ARC)
        s.SetArcGeometry(pt(*start), pt(*mid), pt(*end))
        s.SetLayer(layer)
        s.SetWidth(mm(0.1))
        board.Add(s)

    k = r * (1 - 0.70710678)
    seg((r, 0), (w - r, 0))
    arc((w - r, r), (w - r, 0), (w - k, k), (w, r))
    seg((w, r), (w, h - r))
    arc((w - r, h - r), (w, h - r), (w - k, h - k), (w - r, h))
    seg((w - r, h), (r, h))
    arc((r, h - r), (r, h), (k, h - k), (0, h - r))
    seg((0, h - r), (0, r))
    arc((r, r), (0, r), (k, k), (r, 0))


def rect_zone(board, x0, y0, x1, y1, layer=None, net=None, keepout=False, layers=None, prio=0):
    z = pcbnew.ZONE(board)
    if layers is not None:
        ls = pcbnew.LSET()
        for l in layers:
            ls.AddLayer(l)
        z.SetLayerSet(ls)
    else:
        z.SetLayer(layer)
    poly = z.Outline()
    poly.NewOutline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        poly.Append(mm(OX + x), mm(OY + y))
    if keepout:
        z.SetIsRuleArea(True)
        z.SetDoNotAllowCopperPour(True)
        z.SetDoNotAllowTracks(True)
        z.SetDoNotAllowVias(True)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
        z.SetZoneName("ANTENNA_KEEPOUT")
    else:
        z.SetNet(net)
        z.SetAssignedPriority(prio)
        z.SetLocalClearance(mm(0.3))
        z.SetMinThickness(mm(0.25))
        z.SetThermalReliefGap(mm(0.4))
        z.SetThermalReliefSpokeWidth(mm(0.5))
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    board.Add(z)
    return z


def text(board, s, x, y, size=1.0, layer=pcbnew.F_SilkS, bold=False, mirror=False, thick=0.15):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(s)
    t.SetPosition(pt(x, y))
    t.SetLayer(layer)
    t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
    t.SetTextThickness(mm(thick))
    t.SetBold(bold)
    if mirror:
        t.SetMirrored(True)
    board.Add(t)


def build(out_path):
    board = pcbnew.NewBoard(out_path)
    power = setup_rules(board)
    nets = {}

    def net(name):
        if name not in nets:
            n = pcbnew.NETINFO_ITEM(board, name)
            board.Add(n)
            if name in D.POWER_NETS:
                n.SetNetClass(power)
            nets[name] = n
        return nets[name]

    for ref, p in D.P.items():
        lib, name = p["fp"].split(":")
        fp = pcbnew.FootprintLoad(lib_path(lib), name)
        if fp is None:
            raise SystemExit(f"footprint {p['fp']} not found")
        fp.SetReference(ref)
        fp.SetValue(p["value"])
        x, y, rot = p["pos"]
        fp.SetPosition(pt(x, y))
        fp.SetOrientationDegrees(rot)
        fp.SetFPIDAsString(p["fp"])
        if p["mpn"]:
            fp.SetProperty("MPN", p["mpn"])
        if p["note"]:
            fp.SetProperty("Note", p["note"])
        pads = {str(k): v for k, v in p["pads"].items()}
        for pad in fp.Pads():
            n = pads.get(pad.GetNumber())
            if n:
                pad.SetNet(net(n))
        # keep reference text small and off the pads
        fp.Reference().SetTextSize(pcbnew.VECTOR2I(mm(0.8), mm(0.8)))
        fp.Reference().SetTextThickness(mm(0.12))
        if ref.startswith("H"):
            fp.Reference().SetVisible(False)
        board.Add(fp)

    outline(board)

    # silkscreen
    text(board, "RESET", 68.0, 48.3, 0.8, thick=0.12)
    text(board, "BOOT", 30.0, 40.6, 0.8, thick=0.12)
    # bottom: title, LED names, DB9 pinout (mirrored so it reads correctly from below)
    text(board, f"{D.TITLE} v{D.REVISION}", 18.0, 46.0, 1.5, layer=pcbnew.B_SilkS, mirror=True, bold=True, thick=0.25)
    text(board, "WT32-ETH01 carrier for ScoreReader", 18.0, 48.6, 0.9, layer=pcbnew.B_SilkS, mirror=True, thick=0.15)
    for ref, label in (("D3", "PWR"), ("D4", "NET"), ("D5", "CLIENT"), ("D6", "DATA")):
        x = D.P[ref]["pos"][0] + 1.27
        text(board, label, x, 54.0, 0.9, layer=pcbnew.B_SilkS, mirror=True, thick=0.15)
    lines = ["DB9 (male)"] + [f"{p}: {n}" for p, n in sorted(D.DB9_PINS.items()) if n]
    for i, s in enumerate(lines):
        text(board, s, 22.0, 26.0 + i * 1.6, 1.0, layer=pcbnew.B_SilkS, mirror=True, thick=0.15)
    text(board, "JP1: cut = no auto-boot", 68.0, 33.0, 0.8, layer=pcbnew.B_SilkS, mirror=True, thick=0.12)
    text(board, "JP2: RS-485 120R term", 68.0, 34.6, 0.8, layer=pcbnew.B_SilkS, mirror=True, thick=0.12)

    # Wi-Fi antenna keep-out (all copper layers)
    rect_zone(board, *D.ANTENNA_KEEPOUT, keepout=True, layers=[pcbnew.F_Cu, pcbnew.B_Cu])
    board.Save(out_path)
    fix_project(out_path)
    return board


def fix_project(pcb_path):
    """KiCad 7 keeps net classes and rules in the .kicad_pro, which the Python API can't fully set (and which a
    SaveBoard of a board loaded from elsewhere overwrites with defaults), so write them into the JSON."""
    import json
    pro = os.path.splitext(pcb_path)[0] + ".kicad_pro"
    d = json.load(open(pro))
    classes = d["net_settings"]["classes"]
    default = next(c for c in classes if c["name"] == "Default")
    default.update(clearance=0.15, track_width=0.25, via_diameter=0.6, via_drill=0.3)
    power = next((c for c in classes if c["name"] == "Power"), None)
    if power is None:
        power = dict(default, name="Power")
        classes.append(power)
    power.update(clearance=0.15, track_width=0.5, via_diameter=0.8, via_drill=0.4)
    d["net_settings"]["netclass_patterns"] = [{"netclass": "Power", "pattern": n} for n in sorted(D.POWER_NETS)]
    rules = d["board"]["design_settings"]["rules"]
    rules.update(min_clearance=0.15, min_copper_edge_clearance=0.4, min_silk_clearance=0.0, min_resolved_spokes=1,
                 min_track_width=0.2, min_via_diameter=0.6, min_through_hole_diameter=0.3, min_hole_clearance=0.25)
    json.dump(d, open(pro, "w"), indent=2, sort_keys=True)


# ---------------------------------------------------------------- Specctra (Freerouting) round trip
def export_dsn(pcb_path, dsn_path):
    board = pcbnew.LoadBoard(pcb_path)
    if not pcbnew.ExportSpecctraDSN(board, dsn_path):
        raise SystemExit("DSN export failed")


def _sexp(text):
    tokens, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            tokens.append(c)
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            j = text.index('"', i + 1)
            tokens.append(text[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            tokens.append(text[i:j])
            i = j
    stack = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(t)
    return stack[0][0]


def _find(node, key):
    for c in node:
        if isinstance(c, list) and c and c[0] == key:
            yield c


def import_ses(board, ses_path):
    """Add Freerouting's wires and vias to the board (KiCad 7's ImportSpecctraSES needs the GUI's board)."""
    ses = _sexp(open(ses_path).read())
    routes = next(_find(ses, "routes"))
    res = next(_find(routes, "resolution"))
    per_mm = {"um": 1000.0, "mm": 1.0, "mil": 1 / 0.0254, "inch": 1 / 25.4}[res[1]] * float(res[2])

    def to_nm(x, y):
        return pcbnew.VECTOR2I(pcbnew.FromMM(float(x) / per_mm), pcbnew.FromMM(-float(y) / per_mm))

    layers = {"F.Cu": pcbnew.F_Cu, "B.Cu": pcbnew.B_Cu}
    vias = {}
    lib = next(_find(routes, "library_out"), [])
    for ps in _find(lib, "padstack"):
        for shape in _find(ps, "shape"):
            circ = next(_find(shape, "circle"), None)
            if circ:
                vias[ps[1]] = float(circ[2]) / per_mm
    netout = next(_find(routes, "network_out"))
    count = [0, 0]
    for netnode in _find(netout, "net"):
        name = netnode[1]
        ni = board.FindNet(name)
        if ni is None:
            raise SystemExit(f"net {name} from SES not on board")
        for w in _find(netnode, "wire"):
            path = next(_find(w, "path"))
            layer, width, coords = path[1], float(path[2]) / per_mm, path[3:]
            pts = [to_nm(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]
            for a, b in zip(pts, pts[1:]):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(a)
                t.SetEnd(b)
                t.SetWidth(pcbnew.FromMM(width))
                t.SetLayer(layers[layer])
                t.SetNet(ni)
                board.Add(t)
                count[0] += 1
        for v in _find(netnode, "via"):
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(to_nm(v[2], v[3]))
            dia = vias.get(v[1], 0.6)
            via.SetWidth(pcbnew.FromMM(dia))
            via.SetDrill(pcbnew.FromMM(0.4 if dia >= 0.8 else 0.3))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNet(ni)
            board.Add(via)
            count[1] += 1
    return count


def finish(pcb_path, ses_path=None):
    """Load the placed board, add routing from the SES file, add and fill the ground pours, save."""
    fix_project(pcb_path)
    board = pcbnew.LoadBoard(pcb_path)
    if ses_path:
        tracks, vias = import_ses(board, ses_path)
        print(f"imported {tracks} track segments, {vias} vias")
    gnd = board.FindNet("GND")
    rect_zone(board, 0, 0, D.BOARD_W, D.BOARD_H, layer=pcbnew.F_Cu, net=gnd)
    rect_zone(board, 0, 0, D.BOARD_W, D.BOARD_H, layer=pcbnew.B_Cu, net=gnd)
    pcbnew.SaveBoard(pcb_path, board)
    board = pcbnew.LoadBoard(pcb_path)  # a freshly loaded board fills correctly (holes knocked out)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(pcb_path, board)
    fix_project(pcb_path)
    return board


def drc(pcb_path, report):
    fix_project(pcb_path)
    board = pcbnew.LoadBoard(pcb_path)
    pcbnew.WriteDRCReport(board, report, pcbnew.EDA_UNITS_MILLIMETRES, True)


if __name__ == "__main__":
    cmd, path = sys.argv[1], sys.argv[2]
    if cmd == "place":
        build(path)
    elif cmd == "dsn":
        export_dsn(path, sys.argv[3])
    elif cmd == "finish":
        finish(path, sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "drc":
        drc(path, sys.argv[3])
    else:
        raise SystemExit("usage: make_pcb.py place|dsn|finish|drc board.kicad_pcb [file]")
    print(cmd, "done")
