# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pesi (pesi3d.de)
"""Membrane — soap-film skins and pressurised skins from edges, for IngeTrazo.

An independent implementation for IngeTrazo; no third-party code is used.

Select a **closed loop of edges** (loose edges and/or a group of edges) and
run **Membrane ▸ Skin from Edges…**. The loop may be bent in 3D (a saddle,
a hypar, a tent edge). A mesh is laid inside it and relaxed into a
*minimal surface* — the shape a soap film takes in a wire frame.

* **Pressure** inflates the skin outwards (positive) or sucks it in
  (negative). 100 % is a hemisphere over a circle.
* **Inner closed loops** are mast rings (holes) or fixed lines the skin
  continues past. **Open edge chains** inside the boundary are fixed lines
  (ridge or valley cables), selected **guide points** (Tape Measure) are
  fixed points (mast tops, low points).
* **Tension ratio** makes the skin stiffer along one axis (warp : weft).

The result is ONE group that remembers its source and settings — select it
and run **Edit Membrane…** to change the mesh, pressure etc. Every run is one
undo step; the Live Preview shows the result in the model while the dialog
stays open (the viewport can still be orbited).

Install: copy this file into the plugins folder
(Extensions ▸ Open plugins folder; on Windows %APPDATA%\\ingetrazo\\plugins)
and restart IngeTrazo. Needs IngeTrazo ≥ 0.5 (extension API 2).
"""
from __future__ import annotations

import json
import math
import time

KEY = "membrane_tool"
TITLE = "Membrane"
VERSION = "1.0"
SETTINGS_KEY = "plugins/membrane_tool/params"
MIN_DIV = 4
MAX_DIV = 250
MAX_TRIANGLES = 150_000        # pure-Python Delaunay + solver: keep it sane
WARN_TRIANGLES = 40_000
MAX_PRESSURE = 100.0           # % — 100 = hemisphere over a circle
EPS = 1e-12


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def default_params() -> dict:
    return {
        "res_mode": "div",        # "div" | "size"
        "div": 30,                # divisions on the long side
        "size": 0.5,              # metres (mesh size)
        "iters": 30,              # relaxation rounds
        "pressure": 0.0,          # %  (-100 … 100)
        "ratio": 1.0,             # tension long : short axis
        "inner": "holes",         # "holes" | "lines"
        "soft": True,             # soft edges (smooth look)
        "flip": False,            # flip the faces
        "follow": True,           # dialog: read the selection again on change
        "force": False,           # build over the safe limit (never saved)
    }


def normalize_params(p) -> dict:
    out = default_params()
    if isinstance(p, dict):
        for k, v in p.items():
            if k in out:
                out[k] = v
    try:
        out["div"] = int(min(MAX_DIV, max(MIN_DIV, int(out["div"]))))
        out["size"] = float(max(1e-4, float(out["size"])))
        out["iters"] = int(min(500, max(1, int(out["iters"]))))
        out["pressure"] = float(min(MAX_PRESSURE, max(-MAX_PRESSURE,
                                                      float(out["pressure"]))))
        out["ratio"] = float(min(10.0, max(0.1, float(out["ratio"]))))
    except (TypeError, ValueError):
        return default_params()
    if out["res_mode"] not in ("div", "size"):
        out["res_mode"] = "div"
    if out["inner"] not in ("holes", "lines"):
        out["inner"] = "holes"
    for k in ("soft", "flip", "force", "follow"):
        out[k] = bool(out[k])
    return out


def load_params() -> dict:
    try:
        from PySide6.QtCore import QSettings
        raw = QSettings().value(SETTINGS_KEY, "")
        if raw:
            return normalize_params(json.loads(raw))
    except Exception:  # noqa: BLE001
        pass
    return default_params()


def save_params(p: dict) -> None:
    try:
        from PySide6.QtCore import QSettings
        p = {k: v for k, v in p.items() if k != "force"}
        QSettings().setValue(SETTINGS_KEY, json.dumps(p))
    except Exception:  # noqa: BLE001
        pass


class MembraneError(Exception):
    """A user-facing refusal."""


# ---------------------------------------------------------------------------
# Source data from the selection
# ---------------------------------------------------------------------------

def _xyz(p) -> tuple:
    return (float(p.x()), float(p.y()), float(p.z()))


def membrane_data(group):
    data = (getattr(group, "ext", None) or {}).get(KEY)
    return data if isinstance(data, dict) and "segs" in data else None


def _world_edges(group):
    from core.group import world_mesh
    try:
        return list(world_mesh(group).edges)
    except Exception:  # noqa: BLE001
        return list(group.mesh.edges)


def gather(scene):
    """``{"segs": [(x0,y0,z0,x1,y1,z1), …], "points": [(x,y,z), …]}`` from
    the selection: loose edges, every edge inside selected groups (a
    membrane made by this plugin is skipped) and guide points."""
    from core.group import Group
    from core.mesh import Edge
    try:
        from core.guide import Guide
    except Exception:  # noqa: BLE001
        Guide = None
    segs, pts = [], []
    seen = set()
    for ent in scene.selection:
        if Guide is not None and isinstance(ent, Guide):
            if not getattr(ent, "is_line", False):
                pts.append(_xyz(ent.point))
            continue
        if isinstance(ent, Edge):
            edges = [ent]
        elif isinstance(ent, Group) and membrane_data(ent) is None:
            edges = _world_edges(ent)
        else:
            continue
        for e in edges:
            a, b = _xyz(e.a), _xyz(e.b)
            k = (a, b) if a <= b else (b, a)
            if k in seen or a == b:
                continue
            seen.add(k)
            segs.append(a + b)
    return {"segs": segs, "points": pts}


def _incircle_ok(cx, cy, r2, px, py):
    return (cx - px) ** 2 + (cy - py) ** 2 < r2


# ---------------------------------------------------------------------------
# Edges → polylines
# ---------------------------------------------------------------------------

def chain_segments(segs, tol=1e-6):
    """Join segments into polylines (lists of (x, y, z)); a closed loop
    repeats its first point at the end."""
    def key(x, y, z):
        return (round(x / tol), round(y / tol), round(z / tol))

    adj: dict = {}
    pos: dict = {}
    edges = []
    for i, s in enumerate(segs):
        ka, kb = key(*s[:3]), key(*s[3:])
        pos[ka] = tuple(s[:3])
        pos[kb] = tuple(s[3:])
        adj.setdefault(ka, []).append(i)
        adj.setdefault(kb, []).append(i)
        edges.append((ka, kb))
    used = [False] * len(segs)

    def walk(start_k, first_e):
        line = [start_k]
        k, e = start_k, first_e
        while e is not None:
            used[e] = True
            a, b = edges[e]
            k = b if a == k else a
            line.append(k)
            if len(adj[k]) != 2:
                break
            nxt = [j for j in adj[k] if not used[j]]
            e = nxt[0] if nxt else None
        return line

    lines = []
    # Open chains first (from ends / junctions), then the closed loops.
    for k, es in adj.items():
        if len(es) != 2:
            for e in es:
                if not used[e]:
                    lines.append(walk(k, e))
    for e in range(len(segs)):
        if not used[e]:
            lines.append(walk(edges[e][0], e))
    return [[pos[k] for k in ln] for ln in lines]


# ---------------------------------------------------------------------------
# Geometry helpers (numpy)
# ---------------------------------------------------------------------------


# Delaunay (Bowyer–Watson with a neighbour walk)

def delaunay(xy):
    """Delaunay triangulation of (n, 2) points → (m, 3) CCW index array.

    Bowyer–Watson with a neighbour map: each point is found by walking from
    the last new triangle (points inserted in a snake order over a grid, so
    the walk is short) and only the triangles around it are tested — about
    linear time instead of quadratic."""
    import numpy as np
    xy = np.asarray(xy, dtype=float)
    n = len(xy)
    if n < 3:
        raise MembraneError("At least three points are needed.")
    mn, mx = xy.min(0), xy.max(0)
    span = float(max(mx - mn)) or 1.0
    c = (mn + mx) / 2
    P = ((xy - c) / span).tolist()
    big = 1.0e4
    P += [[-big, -big], [big, -big], [0.0, big]]
    X = [q[0] for q in P]
    Y = [q[1] for q in P]

    tri = []          # [a, b, c] CCW, or None when deleted
    cc = []           # (cx, cy, r2)
    edge = {}         # directed edge (a, b) -> triangle id

    def circ(a, b, c3):
        ax, ay, bx, by, cx_, cy_ = X[a], Y[a], X[b], Y[b], X[c3], Y[c3]
        d = 2.0 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
        if abs(d) < 1e-300:
            return (0.0, 0.0, math.inf)
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx_ * cx_ + cy_ * cy_
        ux = (a2 * (by - cy_) + b2 * (cy_ - ay) + c2 * (ay - by)) / d
        uy = (a2 * (cx_ - bx) + b2 * (ax - cx_) + c2 * (bx - ax)) / d
        return (ux, uy, (ax - ux) ** 2 + (ay - uy) ** 2)

    def add(a, b, c3):
        t = len(tri)
        tri.append([a, b, c3])
        cc.append(circ(a, b, c3))
        edge[(a, b)] = t
        edge[(b, c3)] = t
        edge[(c3, a)] = t
        return t

    last = add(n, n + 1, n + 2)

    # Insertion order: snake over a √n × √n grid of buckets (locality).
    k = max(1, int(math.sqrt(n / 4.0)))
    gx = np.minimum((((xy[:, 0] - mn[0]) / (span or 1)) * k).astype(int), k - 1)
    gy = np.minimum((((xy[:, 1] - mn[1]) / (span or 1)) * k).astype(int), k - 1)
    gx = np.where(gy % 2 == 1, k - 1 - gx, gx)
    order = np.lexsort((xy[:, 0], gx, gy)).tolist()

    def orient(a, b, px, py):
        return (X[b] - X[a]) * (py - Y[a]) - (Y[b] - Y[a]) * (px - X[a])

    for i in order:
        px, py = X[i], Y[i]
        # 1. Walk to the triangle that holds the point.
        t = last
        if tri[t] is None:
            t = next(j for j in range(len(tri) - 1, -1, -1) if tri[j] is not None)
        steps = 0
        while True:
            a, b, c3 = tri[t]
            if orient(a, b, px, py) < 0:
                nt = edge.get((b, a))
            elif orient(b, c3, px, py) < 0:
                nt = edge.get((c3, b))
            elif orient(c3, a, px, py) < 0:
                nt = edge.get((a, c3))
            else:
                break
            steps += 1
            if nt is None or steps > 4 * len(tri) + 10:
                # Fallback (degenerate walk): any triangle whose circle
                # holds the point.
                nt = next(j for j, tt in enumerate(tri) if tt is not None
                          and _incircle_ok(*cc[j], px, py))
                t = nt
                break
            t = nt
        # 2. The cavity: triangles around it whose circle holds the point.
        bad = {t}
        stack = [t]
        while stack:
            u = stack.pop()
            a, b, c3 = tri[u]
            for e0, e1 in ((a, b), (b, c3), (c3, a)):
                v = edge.get((e1, e0))
                if v is not None and v not in bad:
                    cx_, cy_, r2 = cc[v]
                    if (cx_ - px) ** 2 + (cy_ - py) ** 2 < r2:
                        bad.add(v)
                        stack.append(v)
        # 3. Its rim, then re-triangulate as a fan to the point.
        rim = []
        for u in bad:
            a, b, c3 = tri[u]
            for e0, e1 in ((a, b), (b, c3), (c3, a)):
                if edge.get((e1, e0)) not in bad:
                    rim.append((e0, e1))
        for u in bad:
            a, b, c3 = tri[u]
            for e in ((a, b), (b, c3), (c3, a)):
                if edge.get(e) == u:
                    del edge[e]
            tri[u] = None
        for e0, e1 in rim:
            last = add(e0, e1, i)

    T = np.asarray([tt for tt in tri if tt is not None], dtype=np.int64)
    T = T[(T < n).all(1)]
    A, B, C = xy[T[:, 0]], xy[T[:, 1]], xy[T[:, 2]]
    cr = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])
    T[cr < 0] = T[cr < 0][:, [0, 2, 1]]
    return T[np.abs(cr) > span * span * 1e-14]


# ---------------------------------------------------------------------------
# Geometry helpers (numpy)
# ---------------------------------------------------------------------------

def fit_plane(P):
    """Best-fit plane of (n, 3) points → ``(c, e1, e2, n)``: e1 along the
    longest spread, ``e1 × e2 = n``, n pointing up (or +Y, +X for walls)."""
    import numpy as np
    P = np.asarray(P, dtype=float)
    c = P.mean(0)
    _u, _s, vt = np.linalg.svd(P - c, full_matrices=False)
    e1, n = vt[0], vt[2]
    for k in (2, 1, 0):
        if abs(n[k]) > 1e-6:
            if n[k] < 0:
                n = -n
            break
    e1 = e1 - n * float(e1 @ n)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    return c, e1, e2, n


def poly_area(xy):
    import numpy as np
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def inside_poly(poly, X, Y):
    """Even-odd point-in-polygon, vectorised over arrays X, Y."""
    import numpy as np
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    res = np.zeros(X.shape, dtype=bool)
    px, py = poly[:, 0], poly[:, 1]
    qx, qy = np.roll(px, -1), np.roll(py, -1)
    for x0, y0, x1, y1 in zip(px.tolist(), py.tolist(), qx.tolist(), qy.tolist()):
        if y0 == y1:
            continue
        cond = (y0 > Y) != (y1 > Y)
        xi = x0 + (Y - y0) * (x1 - x0) / (y1 - y0)
        res ^= cond & (X < xi)
    return res


def resample(poly, closed, h):
    """Subdivide a 3D polyline so no piece is longer than ``h``; the
    original vertices (corners) are kept. A closed loop is returned
    without the repeated end point."""
    import numpy as np
    P = np.asarray(poly, dtype=float)
    if closed and len(P) > 1 and np.allclose(P[0], P[-1]):
        P = P[:-1]
    out = []
    m = len(P) if closed else len(P) - 1
    for i in range(m):
        a, b = P[i], P[(i + 1) % len(P)]
        k = max(1, int(math.ceil(float(np.linalg.norm(b - a)) / h - 1e-9)))
        for j in range(k):
            out.append(a + (b - a) * (j / k))
    if not closed:
        out.append(P[-1])
    return np.asarray(out, dtype=float)


def _min_dist2(A, B, chunk=2048):
    """Squared distance from every row of A (n, 2) to the nearest row of B."""
    import numpy as np
    if len(B) == 0:
        return np.full(len(A), np.inf)
    out = np.empty(len(A))
    for s in range(0, len(A), chunk):
        a = A[s:s + chunk]
        d = ((a[:, None, :] - B[None, :, :]) ** 2).sum(-1)
        out[s:s + chunk] = d.min(1)
    return out


# ---------------------------------------------------------------------------
# What the selection means: boundary, inner loops, fixed lines and points
# ---------------------------------------------------------------------------

def classify(data, p):
    """Source → a description of the domain (plane, loops, mesh size)."""
    import numpy as np
    segs = data.get("segs") or []
    lines = chain_segments(segs) if segs else []
    closed, opened = [], []
    for ln in lines:
        if len(ln) >= 4 and np.allclose(ln[0], ln[-1], atol=1e-6):
            closed.append(np.asarray(ln[:-1], dtype=float))
        elif len(ln) >= 2:
            opened.append(np.asarray(ln, dtype=float))
    if not closed:
        raise MembraneError(
            "No closed loop in the selection — select edges that form a "
            "closed boundary (a wire frame).")
    c, e1, e2, n = fit_plane(np.vstack(closed))

    def to2(P):
        Q = np.asarray(P, dtype=float) - c
        return np.stack([Q @ e1, Q @ e2], 1)

    areas = [abs(poly_area(to2(L))) for L in closed]
    io = int(np.argmax(areas))
    if areas[io] < 1e-12:
        raise MembraneError(
            "The boundary loop is seen edge-on from every side — it "
            "encloses no area.")
    outer = closed[io]
    o2 = to2(outer)
    inner, ignored = [], 0
    for i, L in enumerate(closed):
        if i == io:
            continue
        l2 = to2(L)
        if inside_poly(o2, l2[:, 0], l2[:, 1]).all():
            inner.append(L)
        else:
            ignored += 1
    lines_in, pts_in = [], []
    for L in opened:
        l2 = to2(L)
        ok = inside_poly(o2, l2[:, 0], l2[:, 1])
        if ok.any():
            lines_in.append(L)
        else:
            ignored += 1
    pts = np.asarray(data.get("points") or [], dtype=float).reshape(-1, 3)
    if len(pts):
        p2 = to2(pts)
        ok = inside_poly(o2, p2[:, 0], p2[:, 1])
        ignored += int((~ok).sum())
        pts_in = pts[ok]
    else:
        pts_in = pts
    span = o2.max(0) - o2.min(0)
    long_side = float(span.max())
    area = areas[io]
    if p["inner"] == "holes":
        area -= sum(abs(poly_area(to2(L))) for L in inner)
    h = long_side / p["div"] if p["res_mode"] == "div" else p["size"]
    h = max(h, long_side / (MAX_DIV * 4))
    return {
        "plane": (c, e1, e2, n), "outer": outer, "inner": inner,
        "lines": lines_in, "points": pts_in, "ignored": ignored,
        "h": h, "long": long_side, "area": max(area, 1e-12),
        "diameter": 2.0 * math.sqrt(areas[io] / math.pi),
        "to2": to2,
    }


def estimate_triangles(dom):
    """Expected triangle count — cheap, for the dialog."""
    h = dom["h"]
    n_in = dom["area"] / (h * h * math.sqrt(3) / 2)
    rim = sum(len(resample(L, True, h)) for L in [dom["outer"]] + dom["inner"])
    return int(2 * n_in + rim)


def check_size(n, force=False):
    if n > MAX_TRIANGLES and not force:
        raise MembraneError(
            f"About {n:,} triangles — over the safe limit of "
            f"{MAX_TRIANGLES:,}. Use fewer divisions.")


# ---------------------------------------------------------------------------
# The mesh: a triangular lattice inside, the constraints resampled, Delaunay
# made conforming by splitting the boundary where an edge is missing
# ---------------------------------------------------------------------------

def build_mesh(dom, p, max_rounds=8):
    """→ ``(V3 fixed-or-start positions, V2, fixed mask, T)``."""
    import numpy as np
    to2 = dom["to2"]
    c, e1, e2, _n = dom["plane"]
    h = dom["h"]
    holes = p["inner"] == "holes"
    # Constraints: (polyline 3D, closed, cuts the domain)
    cons = [[resample(dom["outer"], True, h), True, True]]
    for L in dom["inner"]:
        cons.append([resample(L, True, h), True, holes])
    for L in dom["lines"]:
        cons.append([resample(L, False, h), False, False])
    o2 = to2(dom["outer"])
    hole2 = [to2(L) for L in dom["inner"]] if holes else []

    def in_domain(X, Y):
        ok = inside_poly(o2, X, Y)
        for hp in hole2:
            ok &= ~inside_poly(hp, X, Y)
        return ok

    # The lattice (equilateral triangles: no cocircular quads for Delaunay)
    mn, mx = o2.min(0), o2.max(0)
    dy = h * math.sqrt(3) / 2
    ys = np.arange(mn[1] + dy / 2, mx[1], dy)
    rows = []
    for r, y in enumerate(ys):
        x0 = mn[0] + (h / 2 if r % 2 else h / 4)
        xs = np.arange(x0, mx[0], h)
        rows.append(np.stack([xs, np.full(len(xs), y)], 1))
    lat = np.vstack(rows) if rows else np.zeros((0, 2))
    if len(lat):
        lat = lat[in_domain(lat[:, 0], lat[:, 1])]
    fixed_pts = np.asarray(dom["points"], dtype=float).reshape(-1, 3)
    if len(fixed_pts) and holes and hole2:
        fp2 = to2(fixed_pts)
        fixed_pts = fixed_pts[in_domain(fp2[:, 0], fp2[:, 1])]

    T = None
    for _round in range(max_rounds):
        # Fixed points: constraint vertices + guide points, deduplicated in 2D
        P3, P2, key_of = [], [], {}
        tol = max(h * 1e-4, 1e-9)
        idx_lists = []

        def add(q3):
            q2 = to2(q3[None, :])[0]
            k = (round(q2[0] / tol), round(q2[1] / tol))
            if k not in key_of:
                key_of[k] = len(P3)
                P3.append(q3)
                P2.append(q2)
            return key_of[k]

        for poly, closed_, cut in cons:
            if not cut:
                q2 = to2(poly)
                keep = in_domain(q2[:, 0], q2[:, 1]) if closed_ is False else \
                    np.ones(len(poly), bool)
                idx_lists.append([add(q) if k else None
                                  for q, k in zip(poly, keep)])
            else:
                idx_lists.append([add(q) for q in poly])
        for q in fixed_pts:
            add(q)
        nf = len(P3)
        F2 = np.asarray(P2)
        free2 = lat
        if len(free2):
            free2 = free2[_min_dist2(free2, F2) > (0.5 * h) ** 2]
        V2 = np.vstack([F2, free2]) if len(free2) else F2
        T = delaunay(V2)
        cen = V2[T].mean(1)
        T = T[in_domain(cen[:, 0], cen[:, 1])]
        # Conformity: every constraint segment must be a mesh edge
        es = set()
        for a, b, d in T.tolist():
            es.add((a, b) if a < b else (b, a))
            es.add((b, d) if b < d else (d, b))
            es.add((d, a) if d < a else (a, d))
        changed = False
        for ci, (poly, closed_, cut) in enumerate(cons):
            ids = idx_lists[ci]
            m = len(ids) if closed_ else len(ids) - 1
            new = []
            for k in range(m):
                a, b = ids[k], ids[(k + 1) % len(ids)]
                new.append(poly[k])
                if a is None or b is None or a == b:
                    continue
                if ((a, b) if a < b else (b, a)) not in es:
                    new.append((poly[k] + poly[(k + 1) % len(poly)]) / 2)
                    changed = True
            if not closed_:
                new.append(poly[-1])
            cons[ci][0] = np.asarray(new)
        if not changed:
            break
    V3 = np.empty((len(V2), 3))
    V3[:nf] = np.asarray(P3)
    if len(V2) > nf:
        V3[nf:] = c + np.outer(V2[nf:, 0], e1) + np.outer(V2[nf:, 1], e2)
    fixed = np.zeros(len(V2), dtype=bool)
    fixed[:nf] = True
    # Drop unused vertices (fixed points that fell into a hole, …)
    used = np.zeros(len(V2), dtype=bool)
    used[T.ravel()] = True
    remap = -np.ones(len(V2), dtype=np.int64)
    remap[used] = np.arange(int(used.sum()))
    return V3[used], V2[used], fixed[used], remap[T]


# ---------------------------------------------------------------------------
# The solver: cotangent Laplacian (Pinkall–Polthier) + pressure, solved by
# preconditioned conjugate gradients on the free vertices
# ---------------------------------------------------------------------------

def _cg(matvec, rhs, x0, minv, tol=1e-8, maxit=2000):
    import numpy as np
    x = x0.copy()
    r = rhs - matvec(x)
    z = minv[:, None] * r
    d = z.copy()
    rz = (r * z).sum(0)
    bn = np.linalg.norm(rhs, axis=0) + 1e-30
    for _k in range(maxit):
        if (np.linalg.norm(r, axis=0) <= tol * bn).all():
            break
        Ad = matvec(d)
        alpha = rz / ((d * Ad).sum(0) + 1e-300)
        x += alpha * d
        r -= alpha * Ad
        z = minv[:, None] * r
        rz2 = (r * z).sum(0)
        d = z + (rz2 / (rz + 1e-300)) * d
        rz = rz2
    return x


def relax(V3, V2, T, fixed, p, diameter, progress=None):
    """Minimal surface (+ pressure) through the fixed vertices.
    Returns ``(V, last_move_relative)``."""
    import numpy as np
    shift = V3[fixed].mean(0) if fixed.any() else V3.mean(0)
    V = V3 - shift            # local coordinates: far-away models stay exact
    V3 = V.copy()
    n = len(V)
    # Unique edges and, per triangle corner, the edge opposite it
    opp = np.concatenate([T[:, [1, 2]], T[:, [2, 0]], T[:, [0, 1]]])
    key = np.sort(opp, 1)
    E, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    free = ~fixed
    fid = -np.ones(n, dtype=np.int64)
    fid[free] = np.arange(int(free.sum()))
    m = int(free.sum())
    if m == 0:
        return V + shift, 0.0
    # Anisotropy from the (fixed) parameter directions
    d2 = V2[E[:, 1]] - V2[E[:, 0]]
    cs2 = d2[:, 0] ** 2 / ((d2 ** 2).sum(1) + 1e-300)
    r = math.sqrt(p["ratio"])
    aniso = r * cs2 + (1.0 / r) * (1.0 - cs2)
    a_f, b_f = free[E[:, 0]], free[E[:, 1]]
    ff = a_f & b_f
    fa = a_f & ~b_f          # free a, fixed b
    fb = ~a_f & b_f
    I_ff = np.concatenate([fid[E[ff, 0]], fid[E[ff, 1]]])
    J_ff = np.concatenate([fid[E[ff, 1]], fid[E[ff, 0]]])
    I3 = (I_ff[:, None] * 3 + np.arange(3)[None, :]).ravel()
    c_pr = 4.0 * p["pressure"] / (100.0 * max(diameter, 1e-12))
    flat = np.zeros((n, 3))
    flat[:, :2] = V2
    iters = p["iters"]
    move = 0.0
    alpha = 1.0 if abs(p["pressure"]) < 1e-9 else 0.5
    for it in range(iters):
        G = flat if it == 0 else V          # round 1: harmonic stretch
        A, B, C = G[T[:, 0]], G[T[:, 1]], G[T[:, 2]]

        def cot(u, v):
            cr = np.linalg.norm(np.cross(u, v), axis=1)
            return (u * v).sum(1) / np.maximum(cr, 1e-300)

        ct = np.concatenate([cot(B - A, C - A), cot(C - B, A - B),
                             cot(A - C, B - C)])
        w = 0.5 * np.bincount(inv, weights=ct, minlength=len(E))
        floor = 1e-2 * float(np.abs(w).mean() + 1e-300)
        w = np.maximum(w, floor) * aniso
        diag = (np.bincount(fid[E[a_f, 0]], weights=w[a_f], minlength=m)
                + np.bincount(fid[E[b_f, 1]], weights=w[b_f], minlength=m))
        diag = np.maximum(diag, 1e-300)
        W_ff = np.concatenate([w[ff], w[ff]])
        W3 = W_ff[:, None]
        rhs = np.zeros((m, 3))
        for k in range(3):
            rhs[:, k] += np.bincount(fid[E[fa, 0]], weights=w[fa] * V[E[fa, 1], k],
                                     minlength=m)
            rhs[:, k] += np.bincount(fid[E[fb, 1]], weights=w[fb] * V[E[fb, 0], k],
                                     minlength=m)
        if it > 0 and c_pr != 0.0:
            A, B, C = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
            av = np.cross(B - A, C - A) / 6.0
            bv = np.zeros((n, 3))
            for k in range(3):
                for j in range(3):
                    bv[:, k] += np.bincount(T[:, j], weights=av[:, k], minlength=n)
            rhs += c_pr * bv[free]

        def matvec(x):
            s = np.bincount(I3, weights=(W3 * x[J_ff]).ravel(),
                            minlength=3 * m).reshape(m, 3)
            return diag[:, None] * x - s

        maxit = int(min(4000, 50 + 6 * math.sqrt(m) * 4))
        x = _cg(matvec, rhs, V[free], 1.0 / diag, 1e-6, maxit)
        a = 1.0 if it == 0 else alpha
        new = V[free] + a * (x - V[free])
        delta = np.zeros((n, 3))
        delta[free] = new - V[free]
        V[free] = new
        # Settled = the SHAPE no longer changes: only the motion across the
        # skin counts, vertices sliding along it do not.
        A, B, C = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
        fn = np.cross(B - A, C - A)
        vn = np.zeros((n, 3))
        for j in range(3):
            for k in range(3):
                vn[:, k] += np.bincount(T[:, j], weights=fn[:, k], minlength=n)
        vn /= np.maximum(np.linalg.norm(vn, axis=1), 1e-300)[:, None]
        move = float(np.abs((delta * vn).sum(1)).max()) / max(diameter, 1e-12)
        if not np.isfinite(V).all() or np.abs(V - V3.mean(0)).max() > 20 * diameter:
            raise MembraneError(
                "The skin does not settle — the pressure is too high for "
                "this boundary. Lower the pressure.")
        if progress is not None:
            progress(it + 1, iters)
    return V + shift, move


# ---------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------

def compute(data, p, progress=None):
    """Source + params → ``(V, T, info)`` — no IngeTrazo objects."""
    import numpy as np
    p = normalize_params(p)
    t0 = time.perf_counter()
    dom = classify(data, p)
    check_size(estimate_triangles(dom), p.get("force", False))
    V3, V2, fixed, T = build_mesh(dom, p)
    if len(T) == 0:
        raise MembraneError("No mesh could be laid inside the boundary.")
    check_size(len(T), p.get("force", False))
    V, move = relax(V3, V2, T, fixed, p, dom["diameter"], progress)
    if p["flip"]:
        T = T[:, [0, 2, 1]]
    info = {"triangles": int(len(T)), "seconds": time.perf_counter() - t0,
            "move": move, "ignored": dom["ignored"],
            "inner": len(dom["inner"]), "lines": len(dom["lines"]),
            "points": int(len(dom["points"]))}
    return np.asarray(V), np.asarray(T), info


def build_group(V, T, p, rec, name="Membrane"):
    import numpy as np
    from core.group import Group
    from core.mesh import Mesh
    pos = V[T].reshape(-1, 3)
    sizes = np.full(len(T), 3, dtype=np.int64)
    mesh = Mesh()
    mesh.add_faces_bulk(np.ascontiguousarray(pos, dtype=float), sizes,
                        np.ones(len(sizes), dtype=np.int64))
    if p["soft"]:
        for e in mesh.edges:
            if len(e.faces) >= 2:
                e.soft = True
    g = Group(mesh=mesh, name=name)
    g.ext = {KEY: rec}
    return g


def make_membrane(data, p, name="Membrane", progress=None):
    """→ ``(Group, info)``"""
    p = normalize_params(p)
    V, T, info = compute(data, p, progress)
    rec = {"version": VERSION,
           "params": {k: v for k, v in p.items() if k != "force"},
           "segs": [[round(float(v), 6) for v in s] for s in data["segs"]],
           "points": [[round(float(v), 6) for v in q]
                      for q in (data.get("points") or [])]}
    return build_group(V, T, p, rec, name), info


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def _make_command_class():
    from core.history import Command, InsertGroupCommand

    class MembraneCommand(Command):
        """Insert a new membrane, or replace ``old`` by ``new`` in place.
        ``select=False`` (the preview) leaves the user's selection alone, so
        edges can still be added to it while the dialog is open."""

        def __init__(self, new, old=None, select=True):
            self.new = new
            self.old = old
            self.select = select
            self._saved = None
            self._insert = InsertGroupCommand(new) if old is None else None
            self._owner = None
            self._index = None

        def _owner_of(self, scene, g):
            if g in scene.groups:
                return scene.groups
            ctx = getattr(scene, "edit_group", None)
            kids = getattr(ctx, "children", None)
            if kids is not None and g in kids:
                return kids
            return None

        def do(self, scene):
            self._saved = set(scene.selection)
            if self._insert is not None:
                self._insert.do(scene)
            else:
                owner = self._owner_of(scene, self.old)
                if owner is None:
                    raise RuntimeError("The membrane is no longer in the model.")
                i = owner.index(self.old)
                owner[i] = self.new
                self._owner, self._index = owner, i
                scene.selection.discard(self.old)
            scene.selection.clear()
            if self.select:
                scene.selection.add(self.new)
            else:
                scene.selection.update(self._saved - {self.old})
            scene.version += 1

        def undo(self, scene):
            if self._insert is not None:
                self._insert.undo(scene)
            elif self._owner is not None and self.new in self._owner:
                self._owner[self._owner.index(self.new)] = self.old
            scene.selection.discard(self.new)
            if self.select:
                if self.old is not None:
                    scene.selection.add(self.old)
            elif self.old is not None and self._saved and self.old in self._saved:
                scene.selection.add(self.old)   # the rest is the user's now
            scene.version += 1

    return MembraneCommand


_CMD = None



def run_membrane(viewport, data, p, old=None, progress=None, select=True):
    """Build and insert (or replace ``old``) as one undo step. Returns the
    command (``cmd.info`` holds the statistics). ``select=False``: keep the
    current selection (preview)."""
    global _CMD
    if _CMD is None:
        _CMD = _make_command_class()
    name = old.name if old is not None else _next_name(viewport.scene)
    g, info = make_membrane(data, p, name, progress)
    if old is not None:
        g.layer = old.layer
        g.material = getattr(old, "material", None)
    cmd = _CMD(g, old, select)
    cmd.info = info
    hist = viewport.history
    hist.execute(cmd)
    err = getattr(hist, "last_error", None)
    if err:
        raise RuntimeError(err)
    notify = getattr(viewport, "notify_scene_changed", None)
    if notify:
        notify()
    viewport.update()
    return cmd


def _next_name(scene):
    used = {g.name for g in scene.groups}
    if "Membrane" not in used:
        return "Membrane"
    n = 2
    while f"Membrane {n}" in used:
        n += 1
    return f"Membrane {n}"


def _unit():
    try:
        from core import units
        code = units.model_unit()
        short = {"in": "in", "in-frac": "in", "ft": "ft", "ft-in": "ft",
                 "ft-in-frac": "ft"}.get(code, code)
        return units.bare_number_scale(), short
    except Exception:  # noqa: BLE001
        return 1.0, "m"


def _fmt(v, scale, unit):
    return f"{v / scale:,.2f} {unit}"


def _settle_text(move):
    """How far the last round still moved the skin (share of its size)."""
    if move < 2e-3:            # < 0.2 % of its size per round
        return "settled", ""
    return ("still moving — raise Iterations (or the shape has no "
            "equilibrium: a mast too high for a soap film)"), "#e8912d"


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------

def _make_dialog_class():
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                                   QComboBox, QDialog, QDoubleSpinBox,
                                   QFormLayout, QGroupBox, QHBoxLayout,
                                   QLabel, QMessageBox, QPushButton,
                                   QRadioButton, QSpinBox, QVBoxLayout)

    class MembraneDialog(QDialog):
        _PV_ON = ("QPushButton { background: #2e9e4f; color: white; "
                  "font-weight: bold; border: 1px solid #1f7a3a; "
                  "border-radius: 3px; padding: 3px 8px; }")
        _BANNER = {
            "live": "background: #1f5f33; color: #e8ffe9;",
            "busy": "background: #7a5a12; color: #fff6e0;",
            "paused": "background: #5c4a1a; color: #ffe9b8;",
            "error": "background: #7a1f1f; color: #ffecec;",
        }

        def __init__(self, viewport, data, old=None, parent=None):
            super().__init__(parent)
            self.viewport = viewport
            self.data = data
            self.old = old
            self.scale_m, self.ulabel = _unit()
            self.p = (normalize_params(membrane_data(old).get("params"))
                      if old is not None else load_params())
            self.dom = None
            self._preview_cmd = None
            self._block = None
            self._busy = False
            self._computing = False
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(300)
            self._timer.timeout.connect(self._refresh_preview)
            what = "Edit Membrane" if old is not None else "Skin from Edges"
            self.setWindowTitle(f"{TITLE} {VERSION} — {what}")
            self.setModal(False)
            self.setAttribute(Qt.WA_DeleteOnClose, True)
            self._sel_note = ""
            self._sig = self._selection_sig()
            self._sel_timer = QTimer(self)
            self._sel_timer.setInterval(400)
            self._sel_timer.timeout.connect(self._poll_selection)
            self._build_ui()
            self._load_into_ui()
            self._sel_timer.start()

        # ---- UI ------------------------------------------------------------
        def _dspin(self, lo, hi, dec, suffix=None):
            s = QDoubleSpinBox(self)
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setSuffix(f" {self.ulabel}" if suffix is None else suffix)
            s.setKeyboardTracking(False)
            s.setMinimumWidth(110)
            s.valueChanged.connect(self._changed)
            return s

        def _check(self, text, tip=""):
            c = QCheckBox(text, self)
            c.setToolTip(tip)
            c.toggled.connect(self._changed)
            return c

        def _build_ui(self):
            lay = QVBoxLayout(self)

            src = QGroupBox("Source", self)
            sl = QFormLayout(src)
            self.src_lbl = QLabel(self)
            self.src_lbl.setWordWrap(True)
            self.src_lbl.setMinimumWidth(360)
            sl.addRow(self.src_lbl)
            self.inner = QComboBox(self)
            self.inner.addItem("Holes — open rings (mast tops, skylights)", "holes")
            self.inner.addItem("Fixed lines — the skin continues inside", "lines")
            self.inner.setToolTip(
                "Closed loops inside the boundary: cut out as rings the "
                "skin is fixed to, or only held by them.")
            self.inner.currentIndexChanged.connect(self._changed)
            sl.addRow("Inner loops:", self.inner)
            srow = QHBoxLayout()
            self.follow = QCheckBox("Follow selection", self)
            self.follow.setToolTip(
                "While the dialog is open, a changed selection is read again "
                "by itself: add or remove edges, loops, cables or guide "
                "points in the viewport and the membrane follows.")
            self.follow.toggled.connect(self._follow_toggled)
            srow.addWidget(self.follow)
            srow.addStretch(1)
            self.b_read = QPushButton("↻ Read selection", self)
            self.b_read.setToolTip("Take the current selection as the source "
                                   "now.")
            self.b_read.clicked.connect(lambda: self._read_selection(True))
            srow.addWidget(self.b_read)
            sl.addRow(srow)
            lay.addWidget(src)

            mbox = QGroupBox("Mesh", self)
            ml = QFormLayout(mbox)
            self.r_div = QRadioButton("Divisions (long side)", self)
            self.r_size = QRadioButton("Mesh size", self)
            grp = QButtonGroup(self)
            grp.addButton(self.r_div)
            grp.addButton(self.r_size)
            self.r_div.toggled.connect(self._changed)
            self.div = QSpinBox(self)
            self.div.setRange(MIN_DIV, MAX_DIV)
            self.div.setKeyboardTracking(False)
            self.div.setToolTip("Triangles along the longest side of the "
                                "boundary. 20–60 is plenty for most skins.")
            self.div.valueChanged.connect(self._changed)
            self.size = self._dspin(0.001, 1e5, 3)
            self.size.setToolTip("Edge length of the triangles.")
            ml.addRow(self.r_div, self.div)
            ml.addRow(self.r_size, self.size)
            self.mesh_lbl = QLabel(self)
            self.mesh_lbl.setWordWrap(True)
            ml.addRow(self.mesh_lbl)
            self.warn_lbl = QLabel(self)
            self.warn_lbl.setWordWrap(True)
            ml.addRow(self.warn_lbl)
            self.force = self._check(
                "Build anyway — at my own risk",
                "Over the safe limit IngeTrazo may become very slow or run "
                "out of memory. Never remembered.")
            ml.addRow(self.force)
            lay.addWidget(mbox)

            sbox = QGroupBox("Shape", self)
            fl = QFormLayout(sbox)
            self.iters = QSpinBox(self)
            self.iters.setRange(1, 500)
            self.iters.setKeyboardTracking(False)
            self.iters.setToolTip(
                "Relaxation rounds. 1 = only stretched into the frame; the "
                "soap-film shape needs 10–30, high pressure more.")
            self.iters.valueChanged.connect(self._changed)
            fl.addRow("Iterations:", self.iters)
            self.pressure = self._dspin(-MAX_PRESSURE, MAX_PRESSURE, 1, " %")
            self.pressure.setSingleStep(5.0)
            self.pressure.setToolTip(
                "0 = soap film (minimal surface).\nPositive inflates the skin "
                "outwards (up, or to the front of a wall), negative sucks "
                "it in.\n100 % = a hemisphere over a circle — the most a skin "
                "can carry.")
            fl.addRow("Pressure:", self.pressure)
            self.ratio = self._dspin(0.1, 10.0, 2, "")
            self.ratio.setSingleStep(0.1)
            self.ratio.setPrefix("1 : ")
            self.ratio.setToolTip(
                "Stress along the long axis of the boundary : across it "
                "(warp : weft). 1 = the same in every direction (soap film); "
                "higher keeps the skin straighter along the long axis.")
            fl.addRow("Tension ratio (long : short):", self.ratio)
            lay.addWidget(sbox)

            obox = QGroupBox("Output", self)
            ol = QVBoxLayout(obox)
            self.soft = self._check("Soft edges (smooth look)",
                                    "The triangle edges are softened.")
            self.flip = self._check("Flip faces",
                                    "Turn the front side of the skin around.")
            ol.addWidget(self.soft)
            ol.addWidget(self.flip)
            lay.addWidget(obox)

            self.pv_lbl = QLabel(self)
            self.pv_lbl.setWordWrap(True)
            self.pv_lbl.setVisible(False)
            lay.addWidget(self.pv_lbl)

            row = QHBoxLayout()
            self.preview = QPushButton("Preview", self)
            self.preview.setCheckable(True)
            self.preview.toggled.connect(self._preview_toggled)
            reset = QPushButton("Reset", self)
            reset.setToolTip("Back to the default settings.")
            reset.clicked.connect(self._reset)
            self.ok_btn = QPushButton("OK", self)
            self.ok_btn.setDefault(True)
            self.ok_btn.clicked.connect(self.accept)
            cancel = QPushButton("Cancel", self)
            cancel.clicked.connect(self.reject)
            row.addWidget(self.preview)
            row.addWidget(reset)
            row.addStretch(1)
            row.addWidget(self.ok_btn)
            row.addWidget(cancel)
            lay.addLayout(row)
            self._style_preview(False)

        def _load_into_ui(self):
            self._busy = True
            p, s = self.p, self.scale_m
            self.r_div.setChecked(p["res_mode"] == "div")
            self.r_size.setChecked(p["res_mode"] == "size")
            self.div.setValue(p["div"])
            self.size.setValue(p["size"] / s)
            self.iters.setValue(p["iters"])
            self.pressure.setValue(p["pressure"])
            self.ratio.setValue(p["ratio"])
            self.inner.setCurrentIndex(max(0, self.inner.findData(p["inner"])))
            self.soft.setChecked(p["soft"])
            self.flip.setChecked(p["flip"])
            self.follow.setChecked(p["follow"])
            self.force.setChecked(False)
            p["force"] = False
            self._busy = False
            self._sync()

        def _read_ui(self):
            p, s = self.p, self.scale_m
            p["res_mode"] = "size" if self.r_size.isChecked() else "div"
            p["div"] = self.div.value()
            p["size"] = self.size.value() * s
            p["iters"] = self.iters.value()
            p["pressure"] = self.pressure.value()
            p["ratio"] = self.ratio.value()
            p["inner"] = self.inner.currentData()
            p["soft"] = self.soft.isChecked()
            p["flip"] = self.flip.isChecked()
            p["follow"] = self.follow.isChecked()
            p["force"] = self.force.isChecked()
            self.p = normalize_params(p)

        def _sync(self):
            """Source text, mesh estimate, limits — no building."""
            p = self.p
            self.div.setEnabled(p["res_mode"] == "div")
            self.size.setEnabled(p["res_mode"] == "size")
            try:
                self.dom = classify(self.data, p)
            except MembraneError as exc:
                self.dom = None
                self.src_lbl.setText(f"<b>{exc}</b>")
                self.mesh_lbl.setText("")
                self.warn_lbl.setVisible(False)
                self.force.setVisible(False)
                self.ok_btn.setEnabled(False)
                self.inner.setEnabled(False)
                self._block = str(exc)
                return
            d = self.dom
            parts = ["1 boundary loop"]
            if d["inner"]:
                parts.append(f"{len(d['inner'])} inner loop"
                             f"{'s' if len(d['inner']) > 1 else ''}")
            if d["lines"]:
                parts.append(f"{len(d['lines'])} fixed line"
                             f"{'s' if len(d['lines']) > 1 else ''}")
            if len(d["points"]):
                parts.append(f"{len(d['points'])} fixed point"
                             f"{'s' if len(d['points']) > 1 else ''}")
            txt = " · ".join(parts)
            txt += (f"<br>Size {_fmt(d['long'], self.scale_m, self.ulabel)} "
                    f"(long side)")
            if d["ignored"]:
                txt += (f"<br><span style='color:#e8912d'>{d['ignored']} "
                        f"item(s) outside the boundary — ignored.</span>")
            if self._sel_note:
                txt += f"<br><span style='color:#5fbf73'>{self._sel_note}</span>"
            elif self.old is not None:
                txt += "<br>Source stored with the membrane."
            self.src_lbl.setText(txt)
            self.inner.setEnabled(bool(d["inner"]))
            n = estimate_triangles(d)
            self.mesh_lbl.setText(
                f"Triangle size {_fmt(d['h'], self.scale_m, self.ulabel)} "
                f"(≈ {n:,} triangles)")
            too_big = n > MAX_TRIANGLES
            if too_big:
                warn = (f"<b>Too many triangles</b> — safe limit "
                        f"{MAX_TRIANGLES:,}. Use fewer divisions.")
                color = "#e0483e"
            elif n > WARN_TRIANGLES:
                warn, color = "Large: slow to build.", "#e8912d"
            else:
                warn, color = "", ""
            style = f"color: {color};" if color else ""
            self.mesh_lbl.setStyleSheet(style)
            self.warn_lbl.setStyleSheet(style)
            self.warn_lbl.setText(warn)
            self.warn_lbl.setVisible(bool(warn))
            if not too_big and self.force.isChecked():
                self.force.blockSignals(True)
                self.force.setChecked(False)
                self.force.blockSignals(False)
                p["force"] = False
            self.force.setVisible(too_big)
            allowed = (not too_big) or self.force.isChecked()
            self.ok_btn.setEnabled(allowed)
            self._block = None if allowed else (
                f"Over the safe limit of {MAX_TRIANGLES:,} triangles — use "
                f"fewer divisions or tick «Build anyway».")
            if self.isVisible():
                QTimer.singleShot(0, self.adjustSize)

        def _banner(self, kind, text):
            self.pv_lbl.setStyleSheet(
                self._BANNER[kind] + " padding: 6px 8px; border-radius: 3px;")
            self.pv_lbl.setText(text)
            self.pv_lbl.setVisible(True)

        def _style_preview(self, on):
            if on:
                self.preview.setText("● Live Preview ON")
                self.preview.setStyleSheet(self._PV_ON)
                self.preview.setToolTip("The preview follows every change by "
                                        "itself — click to switch it off.")
            else:
                self.preview.setText("Preview")
                self.preview.setStyleSheet("")
                self.preview.setToolTip("Show the membrane in the model; it "
                                        "then updates by itself on every "
                                        "change.")
                self.pv_lbl.setVisible(False)
            if self.isVisible():
                QTimer.singleShot(0, self.adjustSize)

        def _changed(self, *_a):
            if self._busy:
                return
            self._read_ui()
            self._sync()
            if self.preview.isChecked():
                if not self._block:
                    self._banner("busy", "⟳ Updating the preview…")
                self._timer.start()

        def _reset(self):
            self.p = default_params()
            self._load_into_ui()
            self._changed()

        # ---- building --------------------------------------------------------
        def _progress(self, k, n):
            self._banner("busy", f"⟳ Relaxing the skin… {k} / {n}")
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)

        # ---- following the selection -----------------------------------------
        def _selection_sig(self):
            try:
                return frozenset(id(e) for e in self.viewport.scene.selection)
            except Exception:  # noqa: BLE001
                return frozenset()

        def _poll_selection(self):
            if self._computing or not self.follow.isChecked():
                return
            sig = self._selection_sig()
            if sig != self._sig:
                self._sig = sig
                self._read_selection(False)

        def _follow_toggled(self, on):
            if self._busy:
                return
            self.p["follow"] = bool(on)
            if on:                       # catch up with what changed meanwhile
                self._sig = self._selection_sig()
                self._read_selection(False)

        @staticmethod
        def _key(data):
            return (sorted(tuple(round(v, 6) for v in s) for s in data["segs"]),
                    sorted(tuple(round(v, 6) for v in q)
                           for q in (data.get("points") or [])))

        def _read_selection(self, manual):
            """Take the current selection as the new source (edges, groups,
            guide points). A selection without edges — nothing, or only the
            membrane — keeps the source as it is."""
            if self._computing:
                return
            data = gather(self.viewport.scene)
            if not data["segs"]:
                if manual:
                    self.viewport.flash_status(
                        f"{TITLE}: no edges in the selection — the source "
                        f"stays as it is.", 4000)
                return
            if not manual and self._key(data) == self._key(self.data):
                return
            self.data = data
            self._sel_note = (f"↻ Selection read again "
                              f"({time.strftime('%H:%M:%S')}): "
                              f"{len(data['segs'])} edges"
                              + (f", {len(data['points'])} points"
                                 if data["points"] else ""))
            self._sync()
            if self.preview.isChecked():
                if not self._block:
                    self._banner("busy", "⟳ Updating the preview…")
                self._timer.start()

        def _run(self, select=True):
            self._computing = True
            try:
                return run_membrane(self.viewport, self.data, self.p, self.old,
                                    self._progress, select)
            finally:
                self._computing = False

        def _result_text(self, info):
            txt, color = _settle_text(info["move"])
            col = f"<span style='color:{color}'>{txt}</span>" if color else txt
            return (f"{info['triangles']:,} triangles "
                    f"({info['seconds']:.1f} s) · {col}")

        def _undo_preview(self):
            cmd = self._preview_cmd
            self._preview_cmd = None
            if cmd is None:
                return
            stack = getattr(self.viewport.history, "undo_stack", [])
            if stack and stack[-1] is cmd:
                self.viewport.history.undo()
                self.viewport.update()

        def _refresh_preview(self):
            if self._computing:            # a change during a build: later
                self._timer.start()
                return
            self._undo_preview()
            if not self.preview.isChecked():
                return
            if self._block:
                self._banner("paused", f"⏸ <b>Preview paused</b> — "
                             f"{self._block} It comes back by itself.")
                return
            self._banner("busy", "⟳ Updating the preview…")
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            try:
                self._preview_cmd = self._run(select=False)
                self._preview_cmd.key = self._state_key()
                self._banner("live", "● <b>LIVE PREVIEW</b> — "
                             + self._result_text(self._preview_cmd.info)
                             + " · changes apply instantly")
            except Exception as exc:  # noqa: BLE001
                self._preview_cmd = None
                self._banner("error", f"Preview failed: {exc}")

        def _preview_toggled(self, on):
            self._style_preview(on)
            if on:
                self._refresh_preview()
            else:
                self._timer.stop()
                self._undo_preview()

        # ---- close -------------------------------------------------------------
        def _state_key(self):
            """What the result depends on — source and settings."""
            p = {k: v for k, v in self.p.items() if k != "follow"}
            return (self._key(self.data), json.dumps(p, sort_keys=True))

        def _keep_preview(self):
            """The live preview already IS the result for these settings:
            keep it (it is one undo step already) instead of building again."""
            cmd = self._preview_cmd
            if cmd is None or getattr(cmd, "key", None) != self._state_key():
                return None
            stack = getattr(self.viewport.history, "undo_stack", [])
            if not stack or stack[-1] is not cmd:
                return None
            scene = self.viewport.scene
            cmd.select = True               # redo/undo now behave like OK
            scene.selection.clear()
            scene.selection.add(cmd.new)
            scene.version += 1
            self._preview_cmd = None
            notify = getattr(self.viewport, "notify_scene_changed", None)
            if notify:
                notify()
            self.viewport.update()
            return cmd

        def accept(self):
            if self._computing:
                return
            self._sel_timer.stop()
            self._read_ui()
            self._timer.stop()
            save_params(self.p)
            cmd = self._keep_preview()
            if cmd is None:
                self._undo_preview()
                try:
                    cmd = self._run()
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, TITLE, str(exc))
                    self._sel_timer.start()
                    return
            info = cmd.info
            txt, _c = _settle_text(info["move"])
            self.viewport.flash_status(
                f"{TITLE}: «{cmd.new.name}» — {info['triangles']:,} triangles "
                f"in {info['seconds']:.1f} s, {txt.split(' —')[0]} "
                f"(one undo step)", 6000)
            super().accept()

        def reject(self):
            if self._computing:
                return
            self._sel_timer.stop()
            self._timer.stop()
            self._undo_preview()
            self._read_ui()
            save_params(self.p)
            super().reject()

    return MembraneDialog


_DIALOG = None
_OPEN = None


def _dialog(viewport, data, old, parent):
    """Non-modal: the viewport stays usable (orbit, pan, zoom) while the
    dialog is open. One dialog at a time."""
    global _DIALOG, _OPEN
    if _DIALOG is None:
        _DIALOG = _make_dialog_class()
    if _OPEN is not None:
        try:
            _OPEN.reject()
        except RuntimeError:
            pass
    dlg = _DIALOG(viewport, data, old, parent or viewport.window())
    _OPEN = dlg
    dlg.show()
    dlg.raise_()
    return dlg


def selected_membrane(scene):
    from core.group import Group
    for ent in scene.selection:
        if isinstance(ent, Group) and membrane_data(ent) is not None:
            return ent
    return None


def show_skin(viewport, parent=None):
    data = gather(viewport.scene)
    if not data["segs"]:
        viewport.flash_status(
            f"{TITLE}: select a closed loop of edges (or a group of edges) "
            f"first.", 5000)
        return
    _dialog(viewport, data, None, parent)


def show_edit(viewport, parent=None):
    g = selected_membrane(viewport.scene)
    if g is None:
        viewport.flash_status(
            f"{TITLE}: select a membrane made with this plugin first.", 5000)
        return
    rec = membrane_data(g)
    data = {"segs": [tuple(s) for s in rec.get("segs", [])],
            "points": [tuple(q) for q in rec.get("points", [])]}
    _dialog(viewport, data, g, parent)


def setup(app) -> None:
    from PySide6.QtCore import QTimer
    sub = app.add_menu(TITLE)
    sub.addAction("Skin from Edges…",
                  lambda: show_skin(app.viewport, app.window))
    sub.addSeparator()
    sub.addAction("Edit Membrane…", lambda: show_edit(app.viewport, app.window))

    def later(fn):
        return lambda _c=False: QTimer.singleShot(
            0, lambda: fn(app.viewport, app.window))

    def context(menu, selection) -> None:
        scene = app.viewport.scene
        if not scene.selection:
            return
        menu.addSeparator()
        if selected_membrane(scene) is not None:
            menu.addAction("Edit Membrane…").triggered.connect(later(show_edit))
            return
        sm = menu.addMenu(TITLE)
        sm.addAction("Skin from Edges…").triggered.connect(later(show_skin))

    app.add_context_menu(context)
