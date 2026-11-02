"""Extract one copper layer from each KiCad board and lay the boards side by side in one SVG.

Each board is BOARD.kicad_pcb[@LAYER][#REV][+mirror]; LAYER defaults to B.Cu. With #REV
the board is read from that git branch or commit of its repo (git show), so the
checked-out branch doesn't matter; without it the file on disk is used. +mirror flips
the board left to right.

usage (from the repo root, with the board repos checked out alongside this one):
    python tools/pcb_to_svg.py docs/images/pcb-copper.svg \\
        ../phyduino_pico/phyduino_pico.kicad_pcb@F.Cu#main+mirror \\
        ../phyduino_pico2/phyduino_pico2.kicad_pcb@B.Cu
"""
import math
import os
import re
import subprocess
import sys

TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse(text):
    stack = [[]]
    for m in TOKEN.finditer(text):
        t = m.group(0)
        if t == '(':
            stack.append([])
        elif t == ')':
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(t[1:-1] if t.startswith('"') else t)
    return stack[0][0]


def kids(node, name):
    return [n for n in node if isinstance(n, list) and n and n[0] == name]


def kid(node, name):
    k = kids(node, name)
    return k[0] if k else None


def xy(node):
    return float(node[1]), float(node[2])


def layers_of(node):
    lay = kid(node, 'layers') or kid(node, 'layer')
    return lay[1:] if lay else []


def on_layer(node, layer):
    return any(l in (layer, '*.Cu') for l in layers_of(node))


def rot(px, py, deg):
    a = math.radians(-deg)  # KiCad: positive rotation is counter-clockwise on screen (y down)
    return px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)


def read_board(path, rev):
    if not rev:
        return open(path, encoding='utf-8').read()
    repo, name = os.path.split(os.path.abspath(path))
    return subprocess.run(['git', '-C', repo, 'show', f'{rev}:./{name}'],
                          check=True, capture_output=True, encoding='utf-8').stdout


def extract(path, layer, rev):
    root = parse(read_board(path, rev))
    tracks, vias, pads, pours, edges = [], [], [], [], []

    for s in kids(root, 'segment'):
        if kid(s, 'layer')[1] == layer:
            tracks.append((*xy(kid(s, 'start')), *xy(kid(s, 'end')), float(kid(s, 'width')[1])))

    for v in kids(root, 'via'):
        x, y = xy(kid(v, 'at'))
        vias.append((x, y, float(kid(v, 'size')[1]), float(kid(v, 'drill')[1])))

    for z in kids(root, 'zone'):
        for fp in kids(z, 'filled_polygon'):
            if kid(fp, 'layer')[1] == layer:
                pours.append([xy(p) for p in kid(fp, 'pts') if p[0] == 'xy'])

    for g in kids(root, 'gr_line') + kids(root, 'gr_arc'):
        if kid(g, 'layer')[1] == 'Edge.Cuts':
            edges.append(g)

    for f in kids(root, 'footprint'):
        at = kid(f, 'at')
        fx, fy = xy(at)
        frot = float(at[3]) if len(at) > 3 else 0.0
        for p in kids(f, 'pad'):
            if not on_layer(p, layer):
                continue
            pat = kid(p, 'at')
            px, py = xy(pat)
            prot = float(pat[3]) if len(pat) > 3 else 0.0
            dx, dy = rot(px, py, frot)
            w, h = (float(v) for v in kid(p, 'size')[1:3])
            drill = kid(p, 'drill')
            d = float(drill[1]) if drill and len(drill) > 1 and drill[1] != 'oval' else 0.0
            shape = None if p[2] == 'np_thru_hole' else p[3]
            pads.append((fx + dx, fy + dy, w, h, prot, shape, d))

    return tracks, vias, pads, pours, edges


def bbox(edges):
    xs, ys = [], []
    for g in edges:
        for k in ('start', 'end', 'mid'):
            n = kid(g, k)
            if n:
                xs.append(float(n[1]))
                ys.append(float(n[2]))
    return min(xs), min(ys), max(xs), max(ys)


def f(v):
    return f'{v:.2f}'.rstrip('0').rstrip('.')


def board_svg(path, layer, rev, mirror, ox):
    tracks, vias, pads, pours, edges = extract(path, layer, rev)
    x0, y0, x1, y1 = bbox(edges)
    if mirror:
        out = [f'<g transform="translate({f(ox + x1)} {f(-y0)}) scale(-1 1)">']
    else:
        out = [f'<g transform="translate({f(ox - x0)} {f(-y0)})">']

    out.append('<g class="pour">')
    for poly in pours:
        out.append('<path d="M' + ' L'.join(f'{f(x)} {f(y)}' for x, y in poly) + 'Z"/>')
    out.append('</g>')

    # Tracks and pads are opaque inside one translucent group, so where segments
    # overlap (every bend, every pad entry) the copper isn't drawn twice as bright
    out.append('<g class="cu">')
    out.append('<g class="trk">')
    for xa, ya, xb, yb, w in tracks:
        out.append(f'<path d="M{f(xa)} {f(ya)}L{f(xb)} {f(yb)}" stroke-width="{f(w)}"/>')
    out.append('</g>')

    out.append('<g class="pad">')
    for x, y, w, h, r, shape, d in pads:
        if shape is None:  # non-plated hole: no copper, hole only
            continue
        if shape == 'circle' or (shape == 'oval' and w == h):
            out.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(w / 2)}"/>')
        else:
            rx = min(w, h) / 2 if shape in ('oval', 'roundrect') else 0
            if shape == 'roundrect':
                rx = min(w, h) * 0.25
            out.append(f'<rect x="{f(x - w / 2)}" y="{f(y - h / 2)}" width="{f(w)}" height="{f(h)}" rx="{f(rx)}"'
                       + (f' transform="rotate({f(-r)} {f(x)} {f(y)})"' if r else '') + '/>')
    for x, y, size, drill in vias:
        out.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(size / 2)}"/>')
    out.append('</g>')
    out.append('</g>')

    out.append('<g class="hole">')
    for x, y, *_ , d in pads:
        if d:
            out.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(d / 2)}"/>')
    for x, y, size, drill in vias:
        out.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(drill / 2)}"/>')
    out.append('</g>')

    out.append(f'<rect class="edge" x="{f(x0)}" y="{f(y0)}" width="{f(x1 - x0)}" height="{f(y1 - y0)}" rx="1"/>')
    out.append('</g>')
    stats = dict(tracks=len(tracks), vias=len(vias), pads=len(pads), pours=len(pours))
    return '\n'.join(out), x1 - x0, y1 - y0, stats


def main():
    out_path, boards = sys.argv[1], sys.argv[2:]
    gap = 8.0
    parts, ox, height = [], 0.0, 0.0
    for b in boards:
        mirror = b.endswith('+mirror')
        spec, _, rev = b.removesuffix('+mirror').partition('#')
        path, _, layer = spec.partition('@')
        layer = layer or 'B.Cu'
        svg, w, h, stats = board_svg(path, layer, rev, mirror, ox)
        print(path, layer, rev or '(working tree)', 'mirrored' if mirror else '',
              f'{w:.1f} x {h:.1f} mm', stats, file=sys.stderr)
        parts.append(svg)
        ox += w + gap
        height = max(height, h)
    width = ox - gap
    style = ('.pour{fill:#00B3E3;fill-opacity:.07}'
             '.cu{opacity:.42}'
             '.trk{fill:none;stroke:#00B3E3;stroke-linecap:round}'
             '.pad{fill:#00B3E3}'
             '.hole{fill:#152849}'
             '.edge{fill:none;stroke:#00B3E3;stroke-opacity:.25;stroke-width:.3}')
    with open(out_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write('<!-- Hero background: one copper layer per board, left to right:\n'
                 + ''.join(f'       {b.replace("--", "-")}\n' for b in boards)
                 + '     Generated by tools/pcb_to_svg.py. Regenerate rather than hand-editing. -->\n')
        fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(width)} {f(height)}" '
                 f'width="{f(width * 10)}" height="{f(height * 10)}">\n')
        fh.write(f'<style>{style}</style>\n')
        fh.write('\n'.join(parts))
        fh.write('\n</svg>\n')


main()
