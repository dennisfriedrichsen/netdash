"""Draw the dashboard's OS icons to a PNG, so they can be looked at.

netdash has no SVG rasteriser available and no browser, and icons are the one
thing in this repo that cannot be reviewed by reading it. Three icon bugs
shipped before this existed -- a Linux penguin that was a snowman, and two
earlier fixes for it that rendered as a donut and as a bowtie. Every one of
them looked perfectly reasonable as coordinates.

This is deliberately small and only understands what OS_ICONS actually uses:
<circle>, <ellipse>, <rect rx>, <path> with M L H V A C S Z in both cases,
filled or stroked. It is not a general SVG renderer and should not grow into
one; if an icon needs something this cannot draw, that is a good reason to
draw the icon differently.

  python3 tests/icons/rasterize.py            # every family, 120px + true 19px
  python3 tests/icons/rasterize.py linux hub  # just these

Writes icons.png in the current directory. Needs node, to read the specs out
of app.js rather than keeping a second copy of them here that could drift.
"""

import json
import math
import os
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))

INK = (24, 28, 34)        # currentColor
KO = (250, 250, 250)      # var(--surface-1), the knockout colour
BG = (250, 250, 250)
VIEWBOX = 24.0


# ---------- path parsing ----------

def _tokens(d):
    out, num, i = [], "", 0
    while i < len(d):
        c = d[i]
        if c.isalpha():
            if num:
                out.append(float(num)); num = ""
            out.append(c)
        elif c in "-+" and num and num[-1] not in "eE":
            out.append(float(num)); num = c
        elif c in " ,\t\n":
            if num:
                out.append(float(num)); num = ""
        elif c == "." and "." in num and "e" not in num.lower():
            out.append(float(num)); num = "."
        else:
            num += c
        i += 1
    if num:
        out.append(float(num))
    return out


def _arc(x0, y0, rx, ry, phi, large, sweep, x, y, steps=24):
    """SVG elliptical arc -> points, via the endpoint->centre parameterisation."""
    if rx == 0 or ry == 0 or (x0 == x and y0 == y):
        return [(x, y)]
    rx, ry, phi = abs(rx), abs(ry), math.radians(phi)
    cs, sn = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x0 - x) / 2.0, (y0 - y) / 2.0
    x1 = cs * dx2 + sn * dy2
    y1 = -sn * dx2 + cs * dy2
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam); rx *= s; ry *= s
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cx1 = co * rx * y1 / ry
    cy1 = -co * ry * x1 / rx
    cx = cs * cx1 - sn * cy1 + (x0 + x) / 2.0
    cy = sn * cx1 + cs * cy1 + (y0 + y) / 2.0

    def ang(ux, uy, vx, vy):
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        if d == 0:
            return 0.0
        c = max(-1.0, min(1.0, (ux * vx + uy * vy) / d))
        a = math.acos(c)
        return -a if ux * vy - uy * vx < 0 else a

    t1 = ang(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = ang((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    pts = []
    for i in range(1, steps + 1):
        t = t1 + dt * i / steps
        px = cs * rx * math.cos(t) - sn * ry * math.sin(t) + cx
        py = sn * rx * math.cos(t) + cs * ry * math.sin(t) + cy
        pts.append((px, py))
    return pts


def _cubic(p0, p1, p2, p3, steps=18):
    out = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        u = 1 - t
        out.append((u*u*u*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t*t*t*p3[0],
                    u*u*u*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t*t*t*p3[1]))
    return out


def flatten(d):
    """Path data -> [(points, closed), ...]."""
    t = _tokens(d)
    subs, cur, closed = [], [], False
    x = y = sx = sy = 0.0
    prev_c2 = None
    cmd = None
    i = 0

    def flush():
        if len(cur) > 1:
            subs.append((list(cur), closed))

    while i < len(t):
        if isinstance(t[i], str):
            cmd = t[i]; i += 1
        rel = cmd.islower()
        C = cmd.upper()
        if C == "M":
            flush(); cur[:] = []; closed = False
            nx, ny = t[i], t[i+1]; i += 2
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            sx, sy = x, y
            cur.append((x, y))
            cmd = "l" if rel else "L"
            prev_c2 = None
        elif C == "L":
            nx, ny = t[i], t[i+1]; i += 2
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            cur.append((x, y)); prev_c2 = None
        elif C == "H":
            nx = t[i]; i += 1
            x = x + nx if rel else nx
            cur.append((x, y)); prev_c2 = None
        elif C == "V":
            ny = t[i]; i += 1
            y = y + ny if rel else ny
            cur.append((x, y)); prev_c2 = None
        elif C == "A":
            rx, ry, rot, la, sw, nx, ny = t[i:i+7]; i += 7
            ex, ey = (x + nx, y + ny) if rel else (nx, ny)
            cur.extend(_arc(x, y, rx, ry, rot, int(la), int(sw), ex, ey))
            x, y = ex, ey; prev_c2 = None
        elif C in ("C", "S"):
            if C == "C":
                c1x, c1y, c2x, c2y, nx, ny = t[i:i+6]; i += 6
                if rel:
                    c1x, c1y = x + c1x, y + c1y
                    c2x, c2y = x + c2x, y + c2y
            else:
                c2x, c2y, nx, ny = t[i:i+4]; i += 4
                if rel:
                    c2x, c2y = x + c2x, y + c2y
                c1x, c1y = (2*x - prev_c2[0], 2*y - prev_c2[1]) if prev_c2 else (x, y)
            ex, ey = (x + nx, y + ny) if rel else (nx, ny)
            cur.extend(_cubic((x, y), (c1x, c1y), (c2x, c2y), (ex, ey)))
            prev_c2 = (c2x, c2y); x, y = ex, ey
        elif C == "Z":
            i += 0
            closed = True
            cur.append((sx, sy))
            flush(); cur[:] = []
            x, y = sx, sy
            closed = False
            prev_c2 = None
            if i < len(t) and not isinstance(t[i], str):
                i += 1
        else:
            raise ValueError("unsupported path command %r in %r" % (cmd, d))
    flush()
    return subs


# ---------- shapes -> geometry ----------

def _ellipse_pts(cx, cy, rx, ry, n=64):
    return [(cx + rx*math.cos(2*math.pi*i/n), cy + ry*math.sin(2*math.pi*i/n))
            for i in range(n)]


def _rrect_pts(x, y, w, h, r, n=8):
    r = min(r, w/2.0, h/2.0)
    pts = []
    for cx, cy, a0 in ((x+w-r, y+r, -math.pi/2), (x+w-r, y+h-r, 0),
                       (x+r, y+h-r, math.pi/2), (x+r, y+r, math.pi)):
        for i in range(n + 1):
            a = a0 + math.pi/2 * i/n
            pts.append((cx + r*math.cos(a), cy + r*math.sin(a)))
    return pts


def geometry(tag, a):
    """-> (subpaths, is_closed_area). Coordinates in viewBox units."""
    if tag == "circle":
        return [(_ellipse_pts(float(a["cx"]), float(a["cy"]),
                              float(a["r"]), float(a["r"])), True)]
    if tag == "ellipse":
        return [(_ellipse_pts(float(a["cx"]), float(a["cy"]),
                              float(a["rx"]), float(a["ry"])), True)]
    if tag == "rect":
        return [(_rrect_pts(float(a["x"]), float(a["y"]), float(a["width"]),
                            float(a["height"]), float(a.get("rx", 0) or 0)), True)]
    if tag == "path":
        return flatten(a["d"])
    raise ValueError("unsupported element <%s>" % tag)


# ---------- rasterising ----------

def _inside(px, py, pts):
    """Nonzero winding, which is the SVG default."""
    w = 0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if y1 <= py:
            if y2 > py and (x2-x1)*(py-y1) - (px-x1)*(y2-y1) > 0:
                w += 1
        else:
            if y2 <= py and (x2-x1)*(py-y1) - (px-x1)*(y2-y1) < 0:
                w -= 1
    return w != 0


def _dist_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2-x1, y2-y1
    L = dx*dx + dy*dy
    if L == 0:
        return math.hypot(px-x1, py-y1)
    t = max(0.0, min(1.0, ((px-x1)*dx + (py-y1)*dy) / L))
    return math.hypot(px - (x1+t*dx), py - (y1+t*dy))


def render(shapes, size, ss=4):
    """shapes: [(tag, attrs)]. Later shapes paint over earlier ones."""
    W = H = size * ss
    scale = VIEWBOX / size
    prepared = []
    for tag, a in shapes:
        subs = geometry(tag, a)
        fill = str(a.get("fill", ""))
        sw = a.get("stroke-width")
        colour = KO if "surface" in fill else INK
        stroke = float(sw) if sw else None
        # Bounding box per shape. Without it this is pixels x shapes x segments
        # -- 56 million distance tests for a six-shape icon, which took minutes.
        # Almost every pixel misses almost every shape, and a box rejects it in
        # four comparisons.
        xs = [p[0] for pts, _c in subs for p in pts]
        ys = [p[1] for pts, _c in subs for p in pts]
        m = (stroke or 0) / 2.0 + 0.01
        box = (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
        prepared.append((subs, colour, stroke, fill != "none" and sw is None, box))
    buf = [[BG]*W for _ in range(H)]
    for yy in range(H):
        uy = (yy + 0.5)/ss * scale
        row = buf[yy]
        for xx in range(W):
            ux = (xx + 0.5)/ss * scale
            for subs, colour, sw, filled, box in prepared:
                if ux < box[0] or ux > box[2] or uy < box[1] or uy > box[3]:
                    continue
                hit = False
                if filled:
                    for pts, _c in subs:
                        if _inside(ux, uy, pts):
                            hit = True; break
                if not hit and sw:
                    half = sw/2.0
                    for pts, _c in subs:
                        for i in range(len(pts)-1):
                            if _dist_seg(ux, uy, pts[i][0], pts[i][1],
                                         pts[i+1][0], pts[i+1][1]) <= half:
                                hit = True; break
                        if hit: break
                if hit:
                    row[xx] = colour
    out = []
    for y in range(size):
        r = []
        for x in range(size):
            a = b = c = 0
            for dy in range(ss):
                for dx in range(ss):
                    p = buf[y*ss+dy][x*ss+dx]; a += p[0]; b += p[1]; c += p[2]
            n = ss*ss
            r.append((a//n, b//n, c//n))
        out.append(r)
    return out


def zoom(img, f):
    return [[p for p in row for _ in range(f)] for row in img for _ in range(f)]


def tile(images, gap=6, bg=BG):
    h = max(len(i) for i in images)
    w = sum(len(i[0]) for i in images) + gap*(len(images)-1)
    canvas = [[bg]*w for _ in range(h)]
    x0 = 0
    for im in images:
        for y, row in enumerate(im):
            for x, px in enumerate(row):
                canvas[y][x0+x] = px
        x0 += len(im[0]) + gap
    return canvas


def stack(rows_of_images, gap=6, bg=BG):
    tiles = [tile(r, gap, bg) for r in rows_of_images]
    w = max(len(t[0]) for t in tiles)
    out = []
    for t in tiles:
        for row in t:
            out.append(row + [bg]*(w - len(row)))
        out.extend([[bg]*w for _ in range(gap)])
    return out


def write_png(path, rows):
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBB", *p) for p in r) for r in rows)
    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b""))


def load_icons():
    """Read OS_ICONS out of app.js, so this cannot drift from what ships."""
    js = ('var r=require("%s/tests/js/render.js");'
          'console.log(JSON.stringify(r.ctx.OS_ICONS));' % ROOT)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("could not read OS_ICONS via node:\n" + out.stderr)
    return json.loads(out.stdout)


def check(icons):
    """Parse every icon's geometry without drawing it. Fast enough for the suite.

    Catches the failures that are silent in a browser: a path command this
    cannot read, a malformed `d`, an element with no geometry at all. It says
    nothing about whether an icon looks right -- only a person looking at the
    PNG can say that.
    """
    bad = []
    for fam in sorted(icons):
        for spec in icons[fam]:
            tag, a = spec[0], spec[1]
            try:
                subs = geometry(tag, a)
            except Exception as e:
                bad.append("%s: <%s> %s" % (fam, tag, e)); continue
            if not subs or not any(len(p) > 1 for p, _c in subs):
                bad.append("%s: <%s> has no drawable geometry" % (fam, tag))
                continue
            for pts, _c in subs:
                for x, y in pts:
                    if not (-1 <= x <= 25 and -1 <= y <= 25):
                        bad.append("%s: <%s> leaves the 24x24 viewBox at %.1f,%.1f"
                                   % (fam, tag, x, y))
                        break
    print(json.dumps({"families": len(icons), "errors": bad}))
    return 1 if bad else 0


def main():
    icons = load_icons()
    if sys.argv[1:2] == ["--check"]:
        sys.exit(check(icons))
    want = sys.argv[1:] or sorted(icons)
    missing = [w for w in want if w not in icons]
    if missing:
        sys.exit("no such family: %s (have: %s)"
                 % (", ".join(missing), ", ".join(sorted(icons))))
    big, small = [], []
    for fam in want:
        shapes = [(s[0], s[1]) for s in icons[fam]]
        big.append(render(shapes, 96))
        small.append(zoom(render(shapes, 19), 4))
        sys.stderr.write("drew %s\n" % fam)
    write_png("icons.png", stack([big, small]))
    print("icons.png: %s  (96px, then true 19px at 4x)" % " ".join(want))


if __name__ == "__main__":
    main()
