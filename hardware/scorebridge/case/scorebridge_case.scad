// ScoreBridge case: two halves held together by four M3 screws that also clamp the PCB.
//
//   part = "bottom"  tray (print as is, flat side down)
//   part = "top"     lid  (rendered upside down, ready to print with its top on the bed)
//   part = "coupon"  just the port wall of the lid: print this first (~15 min) to check the DB9, USB-C,
//                    RJ45 and LED cut-outs against your assembled board before printing the whole case
//   part = "assembly" both halves in place, for a quick look ("exploded" pulls them apart)
//
// openscad -D 'part="bottom"' -o bottom.stl scorebridge_case.scad
//
// Hardware: 4x M3 x 12 mm socket/button-head screws from the bottom, 4x M3 heat-set inserts (M3 x 5.7, 4.0 mm
// hole) in the lid posts. No inserts? set insert_d = 2.6 and use M3 self-tapping (or thread-forming) screws.
// Mounting ears take #8 / 4 mm screws (countersunk).

part = "assembly";

// ---------------------------------------------------------------- board (must match hardware/scorebridge/pcb/design.py)
W = 75;  H = 61;  PCB_T = 1.6;  PCB_R = 3;
HOLES = [[4.0, 17.0], [72.0, 19.5], [3.8, 57.2], [71.2, 57.2]];   // KiCad coordinates (y down, ports at y = 0)
DB9_X = 18.42;   USB_X = 68.5;   RJ45_X = 49.0;
LED_X = [10.27, 16.77, 23.27, 29.77];
LED_NAMES = ["PWR", "NET", "CLIENT", "DATA"];

// ---------------------------------------------------------------- heights above the PCB's top surface (tweak after the coupon)
DB9_Z = 6.3;          // centre of the D-sub shell
USB_Z = 1.6;          // centre of the USB-C receptacle
RJ45_BOTTOM = 3.9;    // WT32 module sits on its 2.5 mm header spacer: 2.5 + 1.6 PCB
RJ45_TOP = 17.6;
LED_Z = 2.0;          // 3 mm right-angle LEDs, 2 mm centre height
HEADROOM = 18.6;      // PCB top to inside of the lid

// ---------------------------------------------------------------- case
gap = 0.5;            // PCB edge to inside wall
wall = 2.2;
floor_t = 2.0;
top_t = 2.0;
standoff = 5.0;       // room under the PCB for through-hole leads
fillet = 2.5;         // rounding of the lid's top edge
post_d = 7.0;
screw_d = 3.4;
head_d = 6.4;         // counterbore for the screw head
head_depth = 3.0;
insert_d = 4.0;       // heat-set insert hole (2.6 for self-tapping screws)
insert_depth = 6.0;
ear_len = 12;  ear_w = 20;  ear_t = 3.5;  ear_hole = 4.5;  ear_csk = 8.8;
label_depth = 0.6;

$fn = 48;
e = 0.01;
Z_PCB = floor_t + standoff;          // bottom of the PCB
Z_SPLIT = Z_PCB + PCB_T;             // the halves meet at the PCB's top surface
Z_TOP = Z_SPLIT + HEADROOM + top_t;
R_IN = PCB_R + gap;
R_OUT = R_IN + wall;

function bx(x) = x;                  // KiCad -> case coordinates (y flipped, ports at the back, LEDs at the front)
function by(y) = H - y;

module rrect(w, h, r) { offset(r) offset(-r) square([w, h]); }
module inner_2d() { translate([-gap, -gap]) rrect(W + 2 * gap, H + 2 * gap, R_IN); }
module outer_2d(grow = 0) { translate([-gap - wall - grow, -gap - wall - grow]) rrect(W + 2 * (gap + wall + grow), H + 2 * (gap + wall + grow), R_OUT + grow); }

// outer body with a rounded top edge (stack of offsets, hulled)
module body() {
    steps = 6;
    hull() {
        linear_extrude(Z_TOP - fillet) outer_2d();
        for (i = [1 : steps]) {
            a = 90 * i / steps;
            translate([0, 0, Z_TOP - fillet + fillet * sin(a)])
                linear_extrude(e) outer_2d(-fillet * (1 - cos(a)));
        }
    }
}

module cavity() {
    translate([0, 0, floor_t]) linear_extrude(Z_TOP - top_t - floor_t) inner_2d();
}

module posts() {
    for (h = HOLES) translate([bx(h[0]), by(h[1]), 0]) {
        cylinder(d = post_d, h = Z_PCB);                                   // tray boss under the PCB
        translate([0, 0, Z_SPLIT]) cylinder(d = post_d, h = Z_TOP - Z_SPLIT - e);  // lid post on top of the PCB
    }
}

module post_holes() {
    for (h = HOLES) translate([bx(h[0]), by(h[1]), 0]) {
        translate([0, 0, -e]) cylinder(d = screw_d, h = Z_SPLIT + 1);
        translate([0, 0, -e]) cylinder(d = head_d, h = head_depth + e);
        translate([0, 0, Z_SPLIT - e]) cylinder(d = insert_d, h = insert_depth);
    }
}

// cut-outs, as solids poking through the walls. Anything that has to pass the lid's wall as it is lowered
// over the board is extended down to the split line (an open-bottomed notch).
PORT_Y0 = H + gap - 1;               // just inside the back wall
PORT_LEN = wall + 3;
module back_cut(x, zc, w, h, r = 1, to_split = false) {
    translate([x, PORT_Y0, Z_SPLIT]) rotate([-90, 0, 0]) linear_extrude(PORT_LEN)
        hull() {
            translate([-w / 2, -(zc + h / 2)]) rrect(w, h, r);
            if (to_split) translate([-w / 2, -zc]) square([w, zc + 0.2]);
        }
}

module db9_cut() {
    // trapezoid D (shell 16.3 wide at the top, 10 degree sides) + clearance, open to the split line
    top_w = 17.4; bot_w = 14.6; h = 9.4;
    translate([DB9_X, PORT_Y0, Z_SPLIT]) rotate([-90, 0, 0]) linear_extrude(PORT_LEN)
        hull() {
            translate([0, -DB9_Z]) offset(1) offset(-1)
                polygon([[-top_w / 2, -h / 2], [top_w / 2, -h / 2], [bot_w / 2, h / 2], [-bot_w / 2, h / 2]]);
            translate([-bot_w / 2, -DB9_Z]) square([bot_w, DB9_Z + 0.2]);
        }
    // #4-40 screwlock holes, 25 mm apart (fit the hex screwlocks from outside after closing the case)
    for (s = [-12.5, 12.5]) translate([DB9_X + s, PORT_Y0, Z_SPLIT + DB9_Z]) rotate([-90, 0, 0]) cylinder(d = 3.3, h = PORT_LEN);
    // thin the wall to 1 mm around the connector so the mating plug seats fully
    translate([DB9_X - 16, H + gap + 1.0, Z_SPLIT + DB9_Z - 7.2]) cube([32, wall + 2, 14.4]);
}

module usb_cut() {
    // room for the plug's over-mould (up to ~12.5 x 7 mm); crosses the split line, so both halves are notched
    translate([USB_X, PORT_Y0, Z_SPLIT]) rotate([-90, 0, 0]) linear_extrude(PORT_LEN)
        translate([-6.5, -(USB_Z + 3.7)]) rrect(13, 7.4, 2.5);
}

module rj45_cut() {
    back_cut(RJ45_X, (RJ45_BOTTOM + RJ45_TOP) / 2, 16.8, RJ45_TOP - RJ45_BOTTOM + 0.6, 0.8, true);
}

module led_cuts() {
    for (x = LED_X) translate([x, -gap - wall - 1, Z_SPLIT]) rotate([-90, 0, 0]) linear_extrude(wall + 2)
        hull() {
            translate([0, -LED_Z]) circle(d = 3.4);
            translate([-1.7, -LED_Z]) square([3.4, LED_Z + 0.2]);
        }
}

module ears() {
    for (side = [0, 1]) {
        x0 = side == 0 ? -gap - wall : W + gap + wall;
        dir = side == 0 ? -1 : 1;
        difference() {
            hull() {
                translate([x0 - dir * 1, H / 2 - ear_w / 2, 0]) cube([2, ear_w, ear_t]);
                translate([x0 + dir * (ear_len - ear_w / 4), H / 2, 0]) cylinder(d = ear_w / 2 + 2, h = ear_t);
                translate([x0 + dir * (ear_len - ear_w / 2) - 1, H / 2 - ear_w / 2, 0]) cube([2, ear_w, ear_t]);
            }
            translate([x0 + dir * (ear_len - 4.5), H / 2, -e]) {
                cylinder(d = ear_hole, h = ear_t + 1);
                translate([0, 0, ear_t - (ear_csk - ear_hole) / 2]) cylinder(d1 = ear_hole, d2 = ear_csk, h = (ear_csk - ear_hole) / 2 + 2 * e);
            }
        }
    }
}

module labels() {
    // engraved into the lid top
    translate([0, 0, Z_TOP - label_depth]) linear_extrude(label_depth + 1) {
        translate([W / 2, H / 2 + 2]) text("ScoreBridge", size = 6.5, halign = "center", valign = "center",
                                               font = "DejaVu Sans:style=Bold");
        translate([DB9_X, H - 4.5]) text("CONSOLE", size = 3, halign = "center", font = "DejaVu Sans:style=Bold");
        translate([RJ45_X, H - 4.5]) text("ETHERNET", size = 3, halign = "center", font = "DejaVu Sans:style=Bold");
        translate([USB_X, H - 4.5]) text("USB", size = 3, halign = "center", font = "DejaVu Sans:style=Bold");
        for (i = [0 : 3]) translate([LED_X[i], 3.5]) text(LED_NAMES[i], size = 2.2, halign = "center", font = "DejaVu Sans:style=Bold");
    }
}

module whole() {
    difference() {
        union() {
            difference() { body(); cavity(); }
            intersection() { posts(); translate([0, 0, -1]) linear_extrude(Z_TOP + 2) inner_2d(); }
            ears();
        }
        post_holes();
        db9_cut();
        usb_cut();
        rj45_cut();
        led_cuts();
        labels();
    }
}

module bottom_half() { intersection() { whole(); translate([-50, -50, -1]) cube([W + 100, H + 100, Z_SPLIT + 1]); } }
module top_half() { intersection() { whole(); translate([-50, -50, Z_SPLIT]) cube([W + 100, H + 100, Z_TOP]); } }

if (part == "bottom") bottom_half();
else if (part == "top") translate([0, H, Z_TOP]) rotate([180, 0, 0]) top_half();
else if (part == "coupon")
    translate([0, H, Z_TOP]) rotate([180, 0, 0])
        intersection() { top_half(); translate([-50, H - 8, Z_SPLIT]) cube([W + 100, 30, Z_TOP]); }
else if (part == "exploded") {
    color("#3a3f47") bottom_half();
    color("#c9ced6") translate([0, 0, 22]) top_half();
    color("#1f7a3a") translate([0, 0, Z_PCB + 11]) linear_extrude(PCB_T) rrect(W, H, PCB_R);
}
else if (part == "assembly") {
    color("dimgray") bottom_half();
    color("silver", 0.85) translate([0, 0, 0.3]) top_half();
    color("green") translate([0, 0, Z_PCB]) linear_extrude(PCB_T) rrect(W, H, PCB_R);
}
