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
from collections.abc import Callable, Sequence
from typing import Any

# A curve function maps t in [0, 1] to an (x, y, z) point.
CurveFn = Callable[[float], tuple[float, float, float]]


def nodes_along_curve(
    fn: CurveFn,
    n_points: int,
    start_id: int = 1,
) -> list[dict[str, Any]]:
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
    nodes: list[dict[str, Any]] = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        x, y, z = fn(t)
        nodes.append(
            {
                "id": start_id + i,
                "x": round(float(x), 6),
                "y": round(float(y), 6),
                "z": round(float(z), 6),
            }
        )
    return nodes


def connect_chords(
    chain_a_ids: Sequence[int],
    chain_b_ids: Sequence[int],
    section: str,
    pattern: str = "pratt",
    chord_a_section: str = None,
    chord_b_section: str = None,
    start_id: int = 1,
) -> list[dict[str, Any]]:
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
        raise ValueError(f"connect_chords requires equal-length chains, got {len(a)} vs {len(b)}")
    n = len(a) - 1
    if n < 1:
        raise ValueError("connect_chords requires chains of at least 2 nodes")
    pattern = str(pattern).lower()
    if pattern not in ("pratt", "warren"):
        raise ValueError(f"Unsupported pattern '{pattern}' (supported: pratt, warren)")

    sec_a = chord_a_section if chord_a_section is not None else section
    sec_b = chord_b_section if chord_b_section is not None else section

    bars: list[dict[str, Any]] = []
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
    center_fn: Callable[[float], tuple[float, float, float]],
    radius_fn: Callable[[float], float],
    segments: int,
    levels: int,
    start_id: int = 1,
) -> list[dict[str, Any]]:
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
    nodes: list[dict[str, Any]] = []
    for level in range(lv):
        ratio = level / (lv - 1) if lv > 1 else 0.0
        cx, cy, cz = center_fn(ratio)
        rad = float(radius_fn(ratio))
        for seg in range(segs):
            theta = 2.0 * math.pi * seg / segs
            nodes.append(
                {
                    "id": start_id + level * segs + seg,
                    "x": round(float(cx) + rad * math.cos(theta), 6),
                    "y": round(float(cy) + rad * math.sin(theta), 6),
                    "z": round(float(cz), 6),
                }
            )
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

    def fn(t: float) -> tuple[float, float, float]:
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

    def fn(t: float) -> tuple[float, float, float]:
        x = t * span
        dx = x - half
        z = math.sqrt(max(radius * radius - dx * dx, 0.0)) - center_offset
        return (round(x, 6), 0.0, round(z, 6))

    return fn


# --------------------------------------------------------------------------
# [COMPOSE] Standalone chord / web / bracing / support primitives
# --------------------------------------------------------------------------
# A "chain" is the composable unit returned by the chord generators and
# consumed by web/bracing/support ops:
#
#   chain = {
#       "nodes":  [{id, x, y, z}, ...],          # n_panels+1 nodes
#       "bars":   [{id, n1, n2, section}, ...],  # n_panels chord bars
#       "section": str,                          # chord section
#       "first": int, "last": int,               # endpoint node ids
#       "ids":    [int, ...],                    # node ids in order
#   }
#
# These are PURE (no COM) and independently offline-testable; the named
# templates (truss_spec / arch_truss_spec) are re-implemented on top of
# them, and compose_structure chains them for arbitrary shapes.
# --------------------------------------------------------------------------


def _chain_ids(chain) -> list[int]:
    """Node ids in order from a chain dict, a list of node dicts, or a bare
    id list."""
    if isinstance(chain, dict):
        return [int(n["id"]) for n in chain["nodes"]]
    if chain and isinstance(chain[0], dict):
        return [int(n["id"]) for n in chain]
    return [int(i) for i in chain]


def generate_straight_chord(
    span: float,
    n_panels: int,
    elevation: float = 0.0,
    plane: float = 0.0,
    section: str = "IPE 200",
    start_id: int = 1,
) -> dict[str, Any]:
    """Straight horizontal chain along X at ``(y=plane, z=elevation)``:
    ``n_panels+1`` nodes from x=0 to x=span plus the ``n_panels`` chord
    bars joining them. Returns a chain dict (see module docstring).

    ``plane`` is the constant Y offset — use it to place the chain into a
    second plane (e.g. the far arch of a twin-arch bridge) before bracing
    two chains together with ``connect_bracing``.
    """
    n = max(2, int(n_panels))
    span = float(span)
    elev = float(elevation)
    y_plane = float(plane)
    sec = str(section or "IPE 200")

    def fn(t: float):
        return (round(t * span, 6), y_plane, elev)

    nodes = nodes_along_curve(fn, n + 1, start_id=start_id)
    ids = [int(nd["id"]) for nd in nodes]
    bars = [
        {"id": start_id + n + i, "n1": ids[i], "n2": ids[i + 1], "section": sec} for i in range(n)
    ]
    return {
        "nodes": nodes,
        "bars": bars,
        "section": sec,
        "first": ids[0],
        "last": ids[-1],
        "ids": ids,
    }


def generate_arc_chord(
    span: float,
    rise: float,
    n_panels: int,
    elevation: float = 0.0,
    plane: float = 0.0,
    arch: str = "up",
    section: str = "IPE 200",
    start_id: int = 1,
) -> dict[str, Any]:
    """Circular-arc chain from x=0 to x=span at ``(y=plane)``. ``arch``:
      "up"   -> z rises from ``elevation`` to ``elevation + rise`` at mid-span
      "down" -> z sags from ``elevation + rise`` at the ends to ``elevation``
                at mid-span (inverted arch / hogging cable).
    Returns the same chain dict shape as ``generate_straight_chord``.
    """
    n = max(2, int(n_panels))
    span = float(span)
    rise = max(float(rise), 0.0)
    elev = float(elevation)
    y_plane = float(plane)
    arch = str(arch or "up").lower()
    if arch not in ("up", "down"):
        raise ValueError(f"arch must be 'up' or 'down', got {arch!r}")
    sec = str(section or "IPE 200")
    base = circular_arc_fn(span, rise)  # z in [0, rise] at y=0

    def fn(t: float):
        x, _, z = base(t)
        zz = z if arch == "up" else (rise - z)
        return (x, y_plane, round(elev + zz, 6))

    nodes = nodes_along_curve(fn, n + 1, start_id=start_id)
    ids = [int(nd["id"]) for nd in nodes]
    bars = [
        {"id": start_id + n + i, "n1": ids[i], "n2": ids[i + 1], "section": sec} for i in range(n)
    ]
    return {
        "nodes": nodes,
        "bars": bars,
        "section": sec,
        "first": ids[0],
        "last": ids[-1],
        "ids": ids,
    }


def connect_web_pattern(
    top_chain,
    bottom_chain,
    pattern: str = "pratt",
    web_section: str = None,
    chord_a_section: str = None,
    chord_b_section: str = None,
    start_id: int = 1,
) -> list[dict[str, Any]]:
    """Web bars (verticals/diagonals) between TWO chains plus the chord bars
    along each chain. This is the compose-layer name for ``connect_chords``:
    it accepts chain dicts (using each chain's own ``section`` for its chord
    bars) or bare id lists (both chord sections default to ``web_section``).
    Explicit ``chord_a_section`` / ``chord_b_section`` override the chain
    dict's own section (needed when the caller passes bare node id lists).
    ``pattern``: "pratt" (verticals + both diagonals) or "warren"
    (alternating diagonals only).
    """
    a = _chain_ids(top_chain)
    b = _chain_ids(bottom_chain)
    if chord_a_section is not None:
        sec_a = str(chord_a_section)
    elif isinstance(top_chain, dict) and top_chain.get("section"):
        sec_a = str(top_chain.get("section"))
    else:
        sec_a = web_section or "IPE 200"
    if chord_b_section is not None:
        sec_b = str(chord_b_section)
    elif isinstance(bottom_chain, dict) and bottom_chain.get("section"):
        sec_b = str(bottom_chain.get("section"))
    else:
        sec_b = web_section or "IPE 200"
    return connect_chords(
        a,
        b,
        str(web_section or "IPE 200"),
        pattern=pattern,
        chord_a_section=sec_a,
        chord_b_section=sec_b,
        start_id=start_id,
    )


def connect_bracing(
    plane_a_chain,
    plane_b_chain,
    pattern: str = "cross",
    section: str = "IPE 200",
    start_id: int = 1,
) -> list[dict[str, Any]]:
    """Bracing bars BETWEEN two parallel chains (different planes) — the
    twin-arch / twin-truss / double-deck case that ``connect_chords`` cannot
    express (that one braces two chains of the SAME truss).

    ``pattern``:
      "cross"      -> X-bracing: a[i]-b[i+1] then a[i+1]-b[i] per panel
      "transverse" -> diaphragm ties a[i]-b[i]
    Both chains must have the same number of nodes (n_panels+1). Bar lengths
    are NOT constrained here — geometry is the caller's; this only wires
    topology and validates that every endpoint exists in one of the chains.
    """
    a = _chain_ids(plane_a_chain)
    b = _chain_ids(plane_b_chain)
    if len(a) != len(b):
        raise ValueError(
            f"connect_bracing requires equal node counts, got {len(a)} "
            f"vs {len(b)} (different panel counts between the two planes?)"
        )
    n = len(a) - 1
    if n < 1:
        raise ValueError("connect_bracing requires chains of at least 2 nodes")
    pattern = str(pattern or "cross").lower()
    if pattern not in ("cross", "transverse"):
        raise ValueError(f"Unsupported bracing pattern '{pattern}' (supported: cross, transverse)")
    sec = str(section or "IPE 200")
    valid = set(a) | set(b)

    bars: list[dict[str, Any]] = []
    bid = start_id

    def B(n1: int, n2: int) -> None:
        nonlocal bid
        if n1 not in valid or n2 not in valid:
            raise ValueError(
                f"connect_bracing bar {bid} references unknown node "
                f"{n1 if n1 not in valid else n2} (endpoints must come from "
                "one of the two chains)"
            )
        bars.append({"id": bid, "n1": int(n1), "n2": int(n2), "section": sec})
        bid += 1

    if pattern == "cross":
        for i in range(n):
            B(a[i], b[i + 1])
            B(a[i + 1], b[i])
    else:  # transverse
        for i in range(n + 1):
            B(a[i], b[i])
    return bars


def apply_support_pattern(
    node_ids: Sequence[int],
    support_type: str = "pinned",
) -> list[dict[str, Any]]:
    """Support assignments ``[{node, type}]`` for the given node ids — the
    reusable form of what every template hardcodes inline."""
    st = str(support_type or "pinned").lower()
    return [{"node": int(nid), "type": st} for nid in node_ids]


def merge_coincident_nodes(geometry: dict[str, Any]) -> dict[str, Any]:
    # Merge distinct nodes at IDENTICAL coordinates into one (lowest id wins),
    # rewriting every bar endpoint / support node / nodal-load reference and
    # dropping the duplicate nodes.
    #
    # [AUDIT] Live-verified: Robot's SOLVER merges coincident-but-distinct
    # nodes during Calculate() (a 35-node composed model solved to a 25-node
    # live model; the merged-away ids are exactly the coincident pairs). That
    # silent merge is the root cause of the bar-uniform load shortfall (loads
    # on bars incident to merged-away nodes are dropped) and makes
    # export_structure_spec round-trips lossy. Merging HERE - the single place
    # compose geometry is finalized - makes the spec identical to what Robot
    # will actually analyze, so the class cannot occur for compose models.
    # PURE (no COM): takes/returns a spec dict {nodes, bars, supports, loads}.
    nodes = geometry.get("nodes") or []
    bars = geometry.get("bars") or []
    supports = geometry.get("supports") or []
    loads = geometry.get("loads") or []

    coord_to_id: dict[tuple[float, float, float], int] = {}
    remap: dict[int, int] = {}
    for n in nodes:
        nid = int(n["id"])
        key = (
            round(float(n.get("x", 0.0)), 6),
            round(float(n.get("y", 0.0)), 6),
            round(float(n.get("z", 0.0)), 6),
        )
        if key in coord_to_id:
            remap[nid] = coord_to_id[key]
        else:
            coord_to_id[key] = nid
    if not remap:
        out = dict(geometry)
        out["__merged_coincident_nodes"] = 0
        return out
    keep = set(coord_to_id.values())

    def r(nid: int) -> int:
        return remap.get(int(nid), int(nid))

    out = dict(geometry)
    out["nodes"] = [n for n in nodes if int(n["id"]) in keep]
    out["bars"] = [dict(b, n1=r(b["n1"]), n2=r(b["n2"])) for b in bars]
    if isinstance(supports, list):
        out["supports"] = [dict(s, node=r(s["node"])) for s in supports]
    if isinstance(loads, list):
        out["loads"] = [
            dict(ld, node=r(ld["node"])) if str(ld.get("kind")) == "nodal" else dict(ld)
            for ld in loads
        ]
    out["__merged_coincident_nodes"] = len(remap)
    return out
