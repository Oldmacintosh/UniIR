//    part = "both";  -> preview body + door (door pulled out)
//    part = "base";  -> Export STL  (print open-back up, no supports)
//    part = "door";  -> Export STL  (lay flat in slicer)


part = "both";          // "both" | "base" | "door"
$fn  = 64;

// ---- shell ----
wall    = 2.4;
floor_t = 2.4;
lidfit  = 0.45;         // back-door base clearance

// ---- body form (capsule: rounded front, sliced flat at back & bottom) ----
body_r   = 38;          // dome / body radius  (~76 mm wide & tall)
sph_y    = 12;          // front/back sphere centre offset
back_y   = -25;         // flat back plane (door)
bottom_z = -28;         // flat bottom plane (sits on desk)

// ---- ESP32 mounting: standoffs + snap clips into the 4 board holes ----
esp_hole_dx = 25;       // hole spacing across WIDTH (x), centre-to-centre  (measured 2.5 cm)
esp_hole_dy = 48;       // hole spacing along LENGTH (y), centre-to-centre  (measured 4.8 cm)
esp_hole_d  = 4.0;      // board mounting-hole diameter                     (measured 0.4 cm)
esp_yoff    = 4;        // shift toward front so USB reaches the door
stand_h     = 9;        // standoff height — raises board so bottom pins clear
stand_d     = 6.0;      // standoff post diameter (bigger than the hole, so the board rests on it)
board_t     = 1.6;      // ESP32 PCB thickness

// ---- HC-SR501 PIR (front) ----
pir_dome_d = 24.5;
pir_z      = 6;

// ---- KY-022 IR receiver (middle of the TOP, pointing straight up) ----
recv_d  = 8;            // hole diameter
recv_y  = 0;            // 0 = dead centre of the top; + moves it toward the front

// ---- IR LEDs (5 mm, plain holes — glue them) ----
led_d   = 5.3;
cled_z  = -14;          // front-centre LED height
side_az = 65;           // side LED azimuth from forward (90=straight side, lower=more onto front dome)
side_el = 0;            // side LED elevation (raise to tilt them up)
top_el  = 68;           // top LED elevation on the dome (90=straight up, lower=more forward)

// ---- back door (snap-fit) ----
door_t = 2.4;
lip_h  = 7;             // how deep the lip reaches into the case
snap   = 0.8;           // snap-bead protrusion. interference at the rim = snap - lidfit. lower = easier
// USB opening in the door — set usb_z to your connector's real height
usb_w  = 14;
usb_h  = 10;
usb_z  = -12.5;         // centre height of the USB connector (board is raised on the standoffs)

// ---- derived ----
back_cy = -sph_y;
r_back  = sqrt(body_r*body_r - (back_y-back_cy)*(back_y-back_cy));
r_in    = sqrt((body_r-wall)*(body_r-wall) - (back_y-back_cy)*(back_y-back_cy));
floor_z = bottom_z + floor_t;

// =====================================================================
//  helpers
// =====================================================================
// a +Y hole (the part faces outward along +Y); place at desired x/z
module led_y(dd) { translate([0,-5,0]) rotate([-90,0,0]) cylinder(h=70, d=dd); }

// place a child (modelled along +Y outward) on the FRONT dome surface
module place_dome(el, az) {
    translate([0, sph_y, 0]) rotate([0,0,az]) rotate([el,0,0])
        translate([0, body_r, 0]) children();
}

// snap-clip post: standoff + split peg + barb that clicks into a board hole
module clip_post() {
    peg_d  = esp_hole_d - 0.4;     // peg passes through the hole
    barb_d = esp_hole_d + 0.5;     // smaller barb so it snaps in easily (was +1.0)
    cylinder(d=stand_d, h=stand_h);                       // standoff (board rests on top)
    difference() {
        union() {
            translate([0,0,stand_h]) cylinder(d=peg_d, h=board_t);             // peg through hole
            translate([0,0,stand_h+board_t])
                cylinder(d1=barb_d, d2=peg_d-0.8, h=1.6);                       // retaining barb (tapered)
        }
        translate([-0.5, -barb_d, stand_h+0.4]) cube([1.0, 2*barb_d, board_t+2.4]); // flex slot
    }
}

module body_outer() {
    difference() {
        hull() {
            translate([0,  sph_y, 0]) sphere(body_r);
            translate([0, -sph_y, 0]) sphere(body_r);
        }
        translate([-200, back_y-400, -200]) cube([400,400,400]);   // flat back
        translate([-200, -200, bottom_z-400]) cube([400,400,400]); // flat bottom
    }
}

module body_inner() {
    difference() {
        hull() {
            translate([0,  sph_y, 0]) sphere(body_r-wall);
            translate([0, -sph_y, 0]) sphere(body_r-wall);
        }
        translate([-200, -200, floor_z-400]) cube([400,400,400]); // leave a floor
    }
}

module mounts() {
    for (sx=[-1,1]) for (sy=[-1,1])
        translate([sx*esp_hole_dx/2, esp_yoff + sy*esp_hole_dy/2, floor_z]) clip_post();
}

module holes() {
    translate([0,      0, pir_z])  led_y(pir_dome_d);                // PIR dome (front)
    translate([0,      0, cled_z]) led_y(led_d);                     // centre LED (front)
    translate([0, recv_y, -5]) cylinder(h=60, d=recv_d);            // KY-022 receiver (middle of top, +Z)
    place_dome(top_el, 0)        led_y(led_d);                       // top LED (on dome curve)
    place_dome(side_el,  side_az) led_y(led_d);                      // left LED (on dome curve)
    place_dome(side_el, -side_az) led_y(led_d);                      // right LED (on dome curve)
}

module base() {
    difference() {
        union() {
            difference() { body_outer(); body_inner(); }   // hollow shell, open back
            mounts();
        }
        holes();
    }
}

module door() {
    lip_r  = r_in - lidfit;        // lip slides into the opening with clearance
    bead_r = lip_r + snap;         // bead is larger than the rim -> it clicks past and is held
    ramp   = 2.5;
    bead_y = 2.0;                  // bead sits just inside the rim
    difference() {
        union() {
            // outer plate — covers the whole flat back, clipped to the flat BOTTOM
            intersection() {
                translate([0, back_y, 0]) rotate([90,0,0]) cylinder(h=door_t, r=r_back);
                translate([-200,-200,bottom_z]) cube([400,400,400]);
            }
            // lip + snap bead — clipped to the cavity FLOOR so it can't jam on the floor
            intersection() {
                union() {
                    // hollow lip ring
                    difference() {
                        translate([0, back_y+lip_h, 0]) rotate([90,0,0]) cylinder(h=lip_h, r=lip_r);
                        translate([0, back_y+lip_h+0.1, 0]) rotate([90,0,0]) cylinder(h=lip_h+0.2, r=lip_r-2.4);
                    }
                    // snap bead: ramp on the tip side, catch shoulder toward the plate
                    difference() {
                        translate([0, back_y+bead_y+ramp, 0]) rotate([90,0,0]) cylinder(h=ramp, r1=lip_r, r2=bead_r);
                        translate([0, back_y+bead_y+ramp+0.1, 0]) rotate([90,0,0]) cylinder(h=ramp+0.2, r=lip_r-2.4);
                    }
                }
                translate([-200,-200,floor_z]) cube([400,400,400]);
            }
        }
        // USB opening through the plate at the connector height
        translate([-usb_w/2, back_y-door_t-2, usb_z-usb_h/2]) cube([usb_w, door_t+lip_h+4, usb_h]);
    }
}

// ---- layout / export ----
if (part == "base" || part == "both") base();
if (part == "both") translate([0, -20, 0]) door();
if (part == "door") rotate([90,0,0]) door();
