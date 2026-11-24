#!/usr/bin/python3
"""Small grid autorouter for the ScoreBridge board (KiCad 7 Python: /usr/bin/python3 route.py board.kicad_pcb).

Two layers, 0.1 mm grid, A* with 45-degree moves, turn and via penalties. Every copper object is stamped onto
per-layer grids with the clearance already added, so a track centre-line only needs to stay on free cells.
Signals first (shortest first), then power, then GND (the ground pours added afterwards cover anything left).
It is deliberately simple; it exists so the board can be rebuilt end to end without a GUI. You can always
rip up and reroute by hand in KiCad, or run Freerouting on the exported DSN instead.
"""
import heapq
import math
import os
import sys

import numpy as np
import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design as D  # noqa: E402

G = 0.1                      # grid pitch, mm
CLR = 0.16                   # copper clearance used by the router, mm (board rule is 0.15; PCBWay standard is 0.1)
EDGE_CLR = 0.45              # copper to board edge
HOLE_CLR = 0.3               # copper to non-plated hole
W_SIG, W_PWR = 0.25, 0.5
VIA_SIG, VIA_PWR = (0.6, 0.3), (0.8, 0.4)
# inflation radii per class: [signal, power]
R_T = (CLR + W_SIG / 2, CLR + W_PWR / 2)              # track centre-lines
R_V = (CLR + VIA_SIG[0] / 2, CLR + VIA_PWR[0] / 2)    # via centres
VIA_COST, TURN_COST = 30.0, 0.6
OX, OY = 100.0, 100.0

NX, NY = int(round(D.BOARD_W / G)) + 1, int(round(D.BOARD_H / G)) + 1
FREE, BLOCK, MULTI = 0, -1, -2
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


def cell_xy(ix, iy):
    return OX + ix * G, OY + iy * G


class Grid:
    def __init__(self):
        # [class][layer]: where a track centre-line (T) or a via centre (V) of that class may go
        self.T = [np.zeros((2, NY, NX), np.int32) for _ in (0, 1)]
        self.V = [np.zeros((2, NY, NX), np.int32) for _ in (0, 1)]
        ys, xs = np.mgrid[0:NY, 0:NX]
        self.X = xs * G
        self.Y = ys * G

    def _mark(self, arr, layer, mask, owner):
        cur = arr[layer]
        if owner == BLOCK:
            cur[mask] = BLOCK
            return
        sel_free = mask & (cur == FREE)
        cur[sel_free] = owner
        clash = mask & (cur != FREE) & (cur != owner) & (cur != BLOCK)
        cur[clash] = MULTI

    def window(self, x0, y0, x1, y1):
        i0, i1 = max(0, int((x0) / G) - 1), min(NX, int((x1) / G) + 2)
        j0, j1 = max(0, int((y0) / G) - 1), min(NY, int((y1) / G) + 2)
        return j0, j1, i0, i1

    def stamp_segment(self, layer, a, b, half_w, owner):
        (ax, ay), (bx, by) = a, b
        r = half_w + max(R_V)
        j0, j1, i0, i1 = self.window(min(ax, bx) - r, min(ay, by) - r, max(ax, bx) + r, max(ay, by) + r)
        X, Y = self.X[j0:j1, i0:i1], self.Y[j0:j1, i0:i1]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 == 0:
            d = np.hypot(X - ax, Y - ay)
        else:
            t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0, 1)
            d = np.hypot(X - (ax + t * dx), Y - (ay + t * dy))
        for arr, rr in ((self.T[0], R_T[0]), (self.T[1], R_T[1]), (self.V[0], R_V[0]), (self.V[1], R_V[1])):
            sub = arr[layer, j0:j1, i0:i1]
            mask = d <= half_w + rr
            if owner == BLOCK:
                sub[mask] = BLOCK
            else:
                sub[mask & (sub == FREE)] = owner
                sub[mask & (sub != FREE) & (sub != owner) & (sub != BLOCK)] = MULTI

    def stamp_disc(self, layer, c, rad, owner):
        self.stamp_segment(layer, c, c, rad, owner)


def board_local(v):
    return pcbnew.ToMM(v.x) - OX, pcbnew.ToMM(v.y) - OY


def build_grid(board, net_ids):
    g = Grid()
    # board edge (rounded rectangle)
    X, Y, r = g.X, g.Y, D.CORNER_R
    cx = np.clip(X, r, D.BOARD_W - r)
    cy = np.clip(Y, r, D.BOARD_H - r)
    inside_dist = r - np.hypot(X - cx, Y - cy)  # distance from the edge, inside the rounded rectangle
    straight = np.minimum.reduce([X, Y, D.BOARD_W - X, D.BOARD_H - Y])
    corner = (np.abs(X - cx) > 0) & (np.abs(Y - cy) > 0)
    edge_d = np.where(corner, inside_dist, straight)
    x0, y0, x1, y1 = D.ANTENNA_KEEPOUT
    for c in (0, 1):
        for l in (0, 1):
            g.T[c][l][edge_d < EDGE_CLR + (W_SIG, W_PWR)[c] / 2] = BLOCK
            g.V[c][l][edge_d < EDGE_CLR + (VIA_SIG, VIA_PWR)[c][0] / 2] = BLOCK
            # antenna keep-out
            rt, rv = R_T[c] - CLR, R_V[c] - CLR
            g.T[c][l][(X >= x0 - rt) & (X <= x1 + rt) & (Y >= y0 - rt) & (Y <= y1 + rt)] = BLOCK
            g.V[c][l][(X >= x0 - rv) & (X <= x1 + rv) & (Y >= y0 - rv) & (Y <= y1 + rv)] = BLOCK
    # pads
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            owner = net_ids.get(pad.GetNetname(), BLOCK) if pad.GetNetname() else BLOCK
            layers = [l for l, lid in ((0, pcbnew.F_Cu), (1, pcbnew.B_Cu)) if pad.IsOnLayer(lid)]
            bb = pad.GetBoundingBox()
            bx0, by0 = board_local(bb.GetOrigin())
            bx1, by1 = board_local(bb.GetEnd())
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                hc = board_local(pad.GetPosition())
                hr = pcbnew.ToMM(pad.GetDrillSize().x) / 2 + HOLE_CLR - CLR
                for l in (0, 1):
                    g.stamp_disc(l, hc, hr, BLOCK)
                continue
            if pad.HasHole():  # drilled pads also need hole clearance on both layers
                layers = [0, 1]
            rmax = max(R_V)
            j0, j1, i0, i1 = g.window(bx0 - rmax, by0 - rmax, bx1 + rmax, by1 + rmax)
            radii = [(g.T[0], R_T[0]), (g.T[1], R_T[1]), (g.V[0], R_V[0]), (g.V[1], R_V[1])]
            for j in range(j0, j1):
                for i in range(i0, i1):
                    p = pcbnew.VECTOR2I(pcbnew.FromMM(OX + i * G), pcbnew.FromMM(OY + j * G))
                    if not pad.HitTest(p, pcbnew.FromMM(rmax)):
                        continue
                    for arr, rr in radii:
                        if pad.HitTest(p, pcbnew.FromMM(rr)):
                            for l in layers:
                                _mark_cell(arr, l, j, i, owner)
    return g


def _mark_cell(arr, l, j, i, owner):
    cur = arr[l, j, i]
    if owner == BLOCK or cur == BLOCK:
        arr[l, j, i] = BLOCK
    elif cur == FREE:
        arr[l, j, i] = owner
    elif cur != owner:
        arr[l, j, i] = MULTI


def pad_cells(pad):
    """Grid cells whose centre lies on the pad copper (where a track may end)."""
    bb = pad.GetBoundingBox()
    x0, y0 = board_local(bb.GetOrigin())
    x1, y1 = board_local(bb.GetEnd())
    out = []
    for j in range(max(0, int(y0 / G)), min(NY, int(y1 / G) + 2)):
        for i in range(max(0, int(x0 / G)), min(NX, int(x1 / G) + 2)):
            p = pcbnew.VECTOR2I(pcbnew.FromMM(OX + i * G), pcbnew.FromMM(OY + j * G))
            if pad.HitTest(p, 0):
                out.append((i, j))
    if not out:
        c = board_local(pad.GetPosition())
        out.append((int(round(c[0] / G)), int(round(c[1] / G))))
    return out


def astar(g, k, starts, goals, goal_center, power):
    """starts/goals: sets of (i, j, layer). Returns a list of (i, j, layer) or None."""
    INF = 1e18
    best = {}
    heap = []
    gx, gy = goal_center

    def h(i, j):
        dx, dy = abs(i - gx), abs(j - gy)
        return 1.3 * ((max(dx, dy) - min(dx, dy)) + 1.41421356 * min(dx, dy))  # weighted A*: faster, near-optimal

    c = 1 if power else 0
    T, V = g.T[c], g.V[c]
    for (i, j, l) in starts:
        st = (i, j, l, 8)
        best[st] = 0.0
        heapq.heappush(heap, (h(i, j), 0.0, st, None))
    parent = {}
    seen = set()
    while heap:
        f, cost, st, par = heapq.heappop(heap)
        if st in seen:
            continue
        seen.add(st)
        parent[st] = par
        i, j, l, d = st
        if (i, j, l) in goals:
            path = []
            while st is not None:
                path.append(st[:3])
                st = parent[st]
            return path[::-1]
        if len(seen) > 3000000:
            return None
        for nd, (dx, dy) in enumerate(DIRS):
            ni, nj = i + dx, j + dy
            if not (0 <= ni < NX and 0 <= nj < NY):
                continue
            o = T[l, nj, ni]
            if o != FREE and o != k:
                continue
            # preferred directions: top layer vertical, bottom layer horizontal (keeps each layer from walling off the other)
            if dx and dy:
                step = 1.41421356 * 1.15
            elif (dx and l == 0) or (dy and l == 1):
                step = 1.6
            else:
                step = 1.0
            turn = 0.0
            if d != 8 and d != nd:
                diff = min((nd - d) % 8, (d - nd) % 8)
                turn = TURN_COST * diff
                if diff > 2:
                    continue
            nc = cost + step + turn
            ns = (ni, nj, l, nd)
            if nc < best.get(ns, INF):
                best[ns] = nc
                heapq.heappush(heap, (nc + h(ni, nj), nc, ns, st))
        # via
        ol = 1 - l
        if V[0, j, i] in (FREE, k) and V[1, j, i] in (FREE, k) and T[ol, j, i] in (FREE, k):
            ns = (i, j, ol, 8)
            nc = cost + VIA_COST
            if nc < best.get(ns, INF):
                best[ns] = nc
                heapq.heappush(heap, (nc + h(i, j), nc, ns, st))
    return None


def simplify(path):
    """Split a cell path into per-layer polylines (corner points only) and via positions."""
    segs, vias = [], []
    cur = [path[0]]
    for p in path[1:]:
        if p[2] != cur[-1][2]:
            segs.append(cur)
            vias.append(p[:2])
            cur = [p]
        else:
            cur.append(p)
    segs.append(cur)
    out = []
    for s in segs:
        if len(s) < 2:
            continue
        pts = [s[0]]
        for a, b in zip(s[1:], s[2:] + [None]):
            if b is None:
                pts.append(a)
                break
            d1 = (a[0] - pts[-1][0], a[1] - pts[-1][1])
            d2 = (b[0] - a[0], b[1] - a[1])
            n1 = (int(math.copysign(1, d1[0])) if d1[0] else 0, int(math.copysign(1, d1[1])) if d1[1] else 0)
            n2 = (int(math.copysign(1, d2[0])) if d2[0] else 0, int(math.copysign(1, d2[1])) if d2[1] else 0)
            if n1 != n2:
                pts.append(a)
        out.append(pts)
    return out, vias


# fine-pitch USB first, then the long nets that have to cross under the WT32 module between its header pins
PRIORITY = ["CC1", "CC2", "USB_DP", "USB_DM", "LED_NET", "LED_CLIENT", "LED_DATA", "IO0_AUTO", "TXD0", "RXD0", "EN",
            "IO0", "CL_RX", "RS232_GPIO", "RS485_GPIO"]


def route(board_path, out_path, first=()):
    board = pcbnew.LoadBoard(board_path)
    nets = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            n = pad.GetNetname()
            if n:
                nets.setdefault(n, []).append(pad)
    net_ids = {n: i + 1 for i, n in enumerate(sorted(nets))}
    g = build_grid(board, net_ids)
    print(f"grid {NX}x{NY}, {len(nets)} nets")

    def span(n):
        xs = [pcbnew.ToMM(p.GetPosition().x) for p in nets[n]]
        ys = [pcbnew.ToMM(p.GetPosition().y) for p in nets[n]]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))

    head = [n for n in list(first) + PRIORITY if n in nets]
    head = list(dict.fromkeys(head))
    order = head + sorted((n for n in nets if n not in D.POWER_NETS and len(nets[n]) > 1 and n not in head), key=span)
    order += [n for n in ("VBUS", "+5V", "+3V3", "GND") if n in nets and n not in head]
    failed = []
    for name in order:
        k = net_ids[name]
        power = name in D.POWER_NETS
        width = W_PWR if power else W_SIG
        via_d, via_drill = VIA_PWR if power else VIA_SIG
        if name == "GND":
            width = 0.4
        pads = nets[name]
        cells = {id(p): pad_cells(p) for p in pads}
        layers_of = {id(p): [l for l, lid in ((0, pcbnew.F_Cu), (1, pcbnew.B_Cu)) if p.IsOnLayer(lid)] for p in pads}
        # greedy order: start with the first pad, always connect the closest remaining pad to the tree
        tree = {(i, j, l) for (i, j) in cells[id(pads[0])] for l in layers_of[id(pads[0])]}
        tree_pts = [board_local(pads[0].GetPosition())]
        remaining = pads[1:]
        ok = True
        while remaining:
            def dist(p):
                c = board_local(p.GetPosition())
                return min(math.hypot(c[0] - t[0], c[1] - t[1]) for t in tree_pts)
            remaining.sort(key=dist)
            tgt = remaining.pop(0)
            goals = {(i, j, l) for (i, j) in cells[id(tgt)] for l in layers_of[id(tgt)]}
            if goals & tree:
                tree |= goals
                continue
            gc = board_local(tgt.GetPosition())
            path = astar(g, k, tree, goals, (gc[0] / G, gc[1] / G), power)
            if path is None:
                failed.append((name, tgt.GetParent().GetReference() + "." + tgt.GetNumber()))
                ok = False
                continue
            polys, vias = simplify(path)
            for pts in polys:
                lid = pcbnew.F_Cu if pts[0][2] == 0 else pcbnew.B_Cu
                for a, b in zip(pts, pts[1:]):
                    t = pcbnew.PCB_TRACK(board)
                    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(OX + a[0] * G), pcbnew.FromMM(OY + a[1] * G)))
                    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(OX + b[0] * G), pcbnew.FromMM(OY + b[1] * G)))
                    t.SetWidth(pcbnew.FromMM(width))
                    t.SetLayer(lid)
                    t.SetNet(board.FindNet(name))
                    board.Add(t)
                    g.stamp_segment(pts[0][2], (a[0] * G, a[1] * G), (b[0] * G, b[1] * G), width / 2, k)
            for (i, j) in vias:
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(OX + i * G), pcbnew.FromMM(OY + j * G)))
                v.SetWidth(pcbnew.FromMM(via_d))
                v.SetDrill(pcbnew.FromMM(via_drill))
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                v.SetNet(board.FindNet(name))
                board.Add(v)
                for l in (0, 1):
                    g.stamp_disc(l, (i * G, j * G), via_d / 2, k)
            for (i, j, l) in path:
                tree.add((i, j, l))
            tree |= goals
            tree_pts.append(gc)
        print(("ok    " if ok else "FAIL  ") + name)
    pcbnew.SaveBoard(out_path, board)
    return failed


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]
    import shutil
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(), "unrouted.kicad_pcb")
    shutil.copy(src, tmp)  # every pass starts again from the unrouted board
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(pro):
        shutil.copy(pro, os.path.splitext(tmp)[0] + ".kicad_pro")
    src = tmp
    first = []
    for attempt in range(10):
        failed = route(src, dst, first)
        bad = [n for n, _ in failed if n != "GND"]
        print(f"pass {attempt + 1}: unrouted {failed}")
        if not bad:
            break
        first = list(dict.fromkeys(bad + first))  # route the failures first next time
    sys.exit(2 if bad else 0)
