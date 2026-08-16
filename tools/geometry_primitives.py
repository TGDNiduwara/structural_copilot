"""
tools/geometry_primitives.py
=============================
Composable geometry primitives for the structural copilot (Part A of the
geometry/section-scaling refactor).

These are PURE functions — no COM, no Robot dependency. They only produce
node/bar dicts in the existing spec schema:

    nodes:  {"id": int, "x": float, "y": float, "z": float}
    bars:   {"id": int, "n1": int, "n2": int, "section": str}

The design intent (general_structural_modeling_plan.md): one layer that
translates a JSON schema into Robot COM calls, rather than one bespoke
Python function per shape. These primitives are that layer's building
blocks — a cylinder, cone, dome, parabola, catenary, helix or arch are all
just different ``fn`` / ``radius_fn`` callables fed to the same
composable functions.

Coordinates are rounded to 6 decimal places (the existing templates'
convention) so specs remain compact and diff-stable.

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Sequence, Tuple

# A curve function maps t in [0, 1] to an (x, y, z) point.
CurveFn = Callable[[float], Tuple[float, float, float]]


def nodes_along_curve(
    fn: CurveFn,
    n_points: int,
    start_id: int = 1,
) -> List[Dict[str, Any]]:
    """Samples ``fn`` at ``n_points`` evenly spaced t values in [0, 1] and
    returns one node dict per sample, with sequential ids from ``start_id``.

    ``fn(t)`` for t in [0, 1] returns ``(x, y, z)``. The caller supplies the
    curve — a circular arc, parabola, catenary, sine wave, helix, or a
    trivial straight line are all just different ``fn`` callables.

    Node ids are ``start_id`` .. ``start_id + n_points - 1``. This is the
    single primitive that replaces the coordinate-generation logic
    previously duplicated inside ``truss_spec`` (flat chords) and
    ``cylindrical_tank_spec`` (circular rings).

    Parameters
    ----------
    fn : curve function t -> (x, y, z)
    n_points : number of sample points (``panels + 1`` for ``panels`` bays)
    start_id : id of the first node
    """
    n = max(2, int(n_points))
    nodes: List[Dict[str, Any]] = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        x, y, z = fn(t)
        nodes.append({
            "id": start_id + i,
            "x": round(float(x), 6),
            "y": round(float(y), 6),
            "z": round(float(z), 6),
        })
    return nodes

def connect_chords(
    chain_a_ids: Sequence[int],
    chain_b_ids: Sequence[int],
    section: str,
    pattern: str = "pratt",
    chord_a_section: str = None,
    chord_b_section: str = None,
    start_id: int = 1,
) -> List[Dict[str, Any]]:
    """Connects two equal-length node chains into a truss-style web.

    Generalizes the top/bottom-chord + vertical + diagonal wiring used by
    ``truss_spec`` so it works on ANY two equal-length node chains — they may
    come from ``nodes_along_curve`` with any ``fn`` (flat, arched, helical).

    Bar ids are sequential from ``start_id`` in this exact order (matches
    the historical ``truss_spec`` wiring so behavior is unchanged when the
    chains happen to be flat):

      1..n              chain_a chord bars  (chain_a[i] - chain_a[i+1])
      n+1..2n           chain_b chord bars  (chain_b[i] - chain_b[i+1])
      2n+1..3n+1        web bars            (pratt: chain_a[i] - chain_b[i])
      3n+2..5n+1        diagonals           (pratt: chain_a[i]-chain_b[i+1],
                                             then chain_a[i+1]-chain_b[i])

    ``section`` is the web-member section; ``chord_a_section`` /
    ``chord_b_section`` default to ``section`` when not given.

    Parameters
    ----------
    chain_a_ids, chain_b_ids : node-id lists, BOTH of length n+1
    section : section name for web (vertical/diagonal) members
    pattern : "pratt" (verticals + both diagonal directions) or
              "warren" (alternating diagonals, NO verticals)
    chord_a_section, chord_b_section : optional chord member sections
    start_id : id of the first bar
    """
    a = list(chain_a_ids)
    b = list(chain_b_ids)
    if len(a) != len(b):
        raise ValueError(
            f"connect_chords requires equal-length chains, got "
            f"{len(a)} vs {len(b)}")
    n = len(a) - 1
    if n < 1:
        raise ValueError("connect_chords requires chains of at least 2 nodes")
    pattern = str(pattern).lower()
    if pattern not in ("pratt", "warren"):
        raise ValueError(
            f"Unsupported pattern '{pattern}' (supported: pratt, warren)")

    sec_a = chord_a_section if chord_a_section is not None else section
    sec_b = chord_b_section if chord_b_section is not None else section

    bars: List[Dict[str, Any]] = []
    bid = start_id

    def B(n1: int, n2: int, sec: str) -> None:
        nonlocal bid
        bars.append({"id": bid, "n1": int(n1), "n2": int(n2), "section": sec})
        bid += 1

    # 1) chord bars along each chain
    for i in range(n):
        B(a[i], a[i + 1], sec_a)
        B(b[i], b[i + 1], sec_b)

    if pattern == "pratt":
        # 2) verticals
        for i in range(n + 1):
            B(a[i], b[i], section)
        # 3) diagonals both directions
        for i in range(n):
            B(a[i], b[i + 1], section)
            B(a[i + 1], b[i], section)
    else:  # warren — alternating diagonals only, no verticals
        for i in range(n):
            if i % 2 == 0:
                B(a[i], b[i + 1], section)
            else:
                B(a[i + 1], b[i], section)

    return bars

def radial_ring(
    center_fn: Callable[[float], Tuple[float, float, float]],
    radius_fn: Callable[[float], float],
    segments: int,
    levels: int,
    start_id: int = 1,
) -> List[Dict[str, Any]]:
    """Builds a faceted ring of ``segments`` nodes at each of ``levels``
    height levels (a generalized cylinder / cone / dome / hyperboloid).

    ``center_fn(level_ratio)`` returns the (x, y, z) center of the ring at
    ``level_ratio = level / (levels - 1)`` in [0, 1]; ``radius_fn`` returns
    the ring radius at that same ratio. A constant ``radius_fn`` produces a
    cylinder; a linearly decreasing one produces a cone; a dome/hyperboloid
    are just different ``radius_fn`` callables.

    Node ids follow ``start_id + level * segments + seg`` (level-major, then
    segment around the ring), matching the historical
    ``cylindrical_tank_spec`` numbering.

    Parameters
    ----------
    center_fn : level_ratio -> (cx, cy, cz)
    radius_fn : level_ratio -> radius
    segments : nodes per ring (>= 3)
    levels : number of rings (>= 2)
    start_id : id of the first node
    """
    segs = max(3, int(segments))
    lv = max(2, int(levels))
    nodes: List[Dict[str, Any]] = []
    for level in range(lv):
        ratio = level / (lv - 1) if lv > 1 else 0.0
        cx, cy, cz = center_fn(ratio)
        rad = float(radius_fn(ratio))
        for seg in range(segs):
            theta = 2.0 * math.pi * seg / segs
            nodes.append({
                "id": start_id + level * segs + seg,
                "x": round(float(cx) + rad * math.cos(theta), 6),
                "y": round(float(cy) + rad * math.sin(theta), 6),
                "z": round(float(cz), 6),
            })
    return nodes

# --------------------------------------------------------------------------
# Ready-made curve callables (optional composable helpers)
# --------------------------------------------------------------------------

def straight_line_fn(span: float, elevation: float = 0.0) -> CurveFn:
    """Flat horizontal line from (0, 0, elevation) to (span, 0, elevation).

    Trivial but explicit — used for straight truss chords / decks.
    """
    span = float(span)
    elev = float(elevation)

    def fn(t: float) -> Tuple[float, float, float]:
        return (round(t * span, 6), 0.0, elev)

    return fn


def circular_arc_fn(span: float, rise: float) -> CurveFn:
    """Circular arc from (0, 0, 0) to (span, 0, 0) peaking at ``rise``
    above the chord line at mid-span (a circular segment with sagitta
    ``rise`` over chord ``span``).

    For a dome / through-arch / bowstring truss. ``rise`` must be >= 0; a
    zero/negative rise returns a flat line. A semicircle corresponds to
    ``rise == span / 2``; ``rise`` may exceed that for a pointed arch.
    """
    span = float(span)
    rise = max(float(rise), 0.0)
    half = span / 2.0
    if half <= 0.0 or rise <= 0.0:
        return straight_line_fn(span, 0.0)
    radius = (half * half + rise * rise) / (2.0 * rise)
    # The arc center sits `center_offset` below the chord line.
    center_offset = radius - rise

    def fn(t: float) -> Tuple[float, float, float]:
        x = t * span
        dx = x - half
        z = math.sqrt(max(radius * radius - dx * dx, 0.0)) - center_offset
        return (round(x, 6), 0.0, round(z, 6))

    return fn



