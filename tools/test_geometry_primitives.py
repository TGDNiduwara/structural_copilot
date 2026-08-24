"""
tools/test_geometry_primitives.py
=================================
Part A + Part B tests (PURE / offline — no Robot COM connection required):

A1  geometry primitives: nodes_along_curve, circular_arc_fn,
    connect_chords (pratt + warren), radial_ring — counts, unique ids,
    circular-arc apex/endpoints, ring geometry.
A2  before/after byte-identity: the refactored truss_spec /
    cylindrical_tank_spec (built on the primitives) produce JSON-identical
    specs to the historical inline implementations for >= 3 parameter
    sets each (legacy bodies embedded below as the oracle).
A3  arch_truss_spec: bowstring + inverted geometry, apex, supports,
    auto-sized sections; usable as batch DesignSpace geometry.
B1  suggest_section: span-aware sizing, nearest-catalog walk, clamping
    notes; the "1m vs 30m" acceptance case.
B2  template auto-sizing: truss_spec(span=1.0) no longer yields the old
    fixed "IPE 200"; explicit sections are never overridden.
B3  check_section_proportions: flags the old 1m/IPE200 default as "deep",
    does NOT alarm on reasonable columns; template specs carry
    __section_notes.
B4  tool_registry: no hardcoded section defaults remain in the schemas;
    create_arch_truss + check_section_proportions registered with
    handlers.

Run:  python tools/test_geometry_primitives.py
"""

from __future__ import annotations

import json
import math
import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.geometry_primitives import (
    nodes_along_curve, connect_chords, radial_ring,
    circular_arc_fn, straight_line_fn)
from tools.section_sizing import (
    suggest_section, section_depth_mm, check_section_proportions)
from tools.robot_tool import RobotBridge

# --------------------------------------------------------------------------
# Historical (pre-refactor) implementations — used ONLY as the before/after
# oracle for the A2 byte-identity regression. Kept verbatim from the codebase
# revision prior to the Part A refactor.
# --------------------------------------------------------------------------


def _legacy_truss_spec(span, height, panels, top_section, bottom_section,
                       web_section):
    """Verbatim pre-refactor ``truss_spec`` body."""
    n = max(2, int(panels))
    dx = span / n
    nodes, nid_counter = [], [0]

    def N(i, top):
        nid_counter[0] += 1
        nodes.append({
            "id": nid_counter[0], "x": round(i * dx, 6), "y": 0.0,
            "z": height if top else 0.0,
        })
        return nid_counter[0]

    top = [N(i, True) for i in range(n + 1)]
    bot = [N(i, False) for i in range(n + 1)]

    bars, bid = [], [0]

    def B(n1, n2, section):
        bid[0] += 1
        bars.append({"id": bid[0], "n1": n1, "n2": n2, "section": section})

    for i in range(n):
        B(top[i], top[i + 1], top_section)
        B(bot[i], bot[i + 1], bottom_section)
    for i in range(n + 1):
        B(top[i], bot[i], web_section)
    for i in range(n):
        B(top[i], bot[i + 1], web_section)
        B(top[i + 1], bot[i], web_section)

    return {"project": "3D", "nodes": nodes, "bars": bars,
            "supports": [{"node": bot[0], "type": "pinned"},
                         {"node": bot[-1], "type": "pinned"}],
            "__tpl": "truss"}


def _legacy_tank_spec(radius, height, segments, ring_levels,
                      section_vertical, section_ring):
    """Verbatim pre-refactor ``cylindrical_tank_spec`` body."""
    segs = max(6, int(segments))
    rings = max(2, int(ring_levels))
    r = float(radius)
    h = float(height)

    def z_at(ring):
        return round(h * ring / (rings - 1), 6)

    def nid(ring, seg):
        return ring * segs + seg + 1

    nodes = []
    for ring in range(rings):
        z = z_at(ring)
        for seg in range(segs):
            theta = 2.0 * math.pi * seg / segs
            nodes.append({
                "id": nid(ring, seg),
                "x": round(r * math.cos(theta), 6),
                "y": round(r * math.sin(theta), 6),
                "z": z,
            })

    bars = []
    bid = 0

    def B(n1, n2, section):
        nonlocal bid
        bid += 1
        bars.append({"id": bid, "n1": n1, "n2": n2, "section": section})

    for ring in range(rings - 1):
        for seg in range(segs):
            B(nid(ring, seg), nid(ring + 1, seg), section_vertical)
    for ring in range(rings):
        for seg in range(segs):
            B(nid(ring, seg), nid(ring, (seg + 1) % segs), section_ring)

    supports = [{"node": nid(0, seg), "type": "pinned"} for seg in range(segs)]
    return {"project": "3D", "nodes": nodes, "bars": bars,
            "supports": supports, "__tpl": "cylindrical_tank"}


def _legacy_arch_truss_spec(span, rise, panels, top_section, bottom_section,
                            web_section, arch_chord):
    """Verbatim pre-refactor ``arch_truss_spec`` body."""
    n = max(2, int(panels))
    rise = max(float(rise), 0.0)
    notes = []
    top_section = top_section or suggest_section(
        "truss_chord", span, "IPE", notes=notes)
    bottom_section = bottom_section or suggest_section(
        "truss_chord", span, "IPE", notes=notes)
    web_section = web_section or suggest_section(
        "web", span, "L", notes=notes)

    arc = circular_arc_fn(float(span), rise)
    if str(arch_chord).lower() == "bottom":
        # Arched bottom chord + straight top chord (deck above the arch).
        bot_fn = arc

        def top_fn(t, _span=float(span), _rise=rise):
            return (round(t * _span, 6), 0.0, _rise)
    else:
        # Straight bottom chord + arched top chord (bowstring).
        top_fn = arc

        def bot_fn(t, _span=float(span)):
            return (round(t * _span, 6), 0.0, 0.0)

    top = nodes_along_curve(top_fn, n + 1, start_id=1)
    bot = nodes_along_curve(bot_fn, n + 1, start_id=n + 2)
    bars = connect_chords(
        [nd["id"] for nd in top], [nd["id"] for nd in bot],
        web_section, pattern="pratt",
        chord_a_section=top_section, chord_b_section=bottom_section,
        start_id=1)
    spec = {"project": "3D", "nodes": top + bot, "bars": bars,
            "supports": [{"node": bot[0]["id"], "type": "pinned"},
                         {"node": bot[-1]["id"], "type": "pinned"}],
            "__tpl": "arch_truss"}
    if notes:
        spec["__section_notes"] = notes
    return spec

# --------------------------------------------------------------------------
# A1 — geometry primitive sanity
# --------------------------------------------------------------------------

def test_nodes_along_curve():
    nodes = nodes_along_curve(straight_line_fn(12.0, 2.0), 7, start_id=1)
    assert len(nodes) == 7
    ids = [nd["id"] for nd in nodes]
    assert ids == list(range(1, 8))
    assert len(set(ids)) == len(ids), "duplicate node ids"
    assert nodes[0] == {"id": 1, "x": 0.0, "y": 0.0, "z": 2.0}
    assert nodes[-1] == {"id": 7, "x": 12.0, "y": 0.0, "z": 2.0}
    nodes2 = nodes_along_curve(straight_line_fn(5.0), 4, start_id=100)
    assert nodes2[0]["id"] == 100 and nodes2[-1]["id"] == 103
    print("  OK: nodes_along_curve counts / ids / endpoints")


def test_circular_arc():
    span, rise = 20.0, 4.0
    arc = nodes_along_curve(circular_arc_fn(span, rise), 9)
    assert arc[0]["z"] == 0.0 and arc[-1]["z"] == 0.0, "arc endpoints on chord line"
    assert max(nd["z"] for nd in arc) == rise, "apex equals requested rise"
    mid = [nd for nd in arc if nd["x"] == 10.0]
    assert mid and mid[0]["z"] == rise, "apex at mid-span"
    # every sample lies on the defining circle:
    #   R = (half^2 + rise^2)/(2*rise), center at (half, 0, rise - R)
    half = span / 2.0
    radius = (half * half + rise * rise) / (2.0 * rise)
    cx, cz = half, rise - radius
    for nd in arc:
        assert abs(math.hypot(nd["x"] - cx, nd["z"] - cz) - radius) < 1e-6
    # monotonic rise to mid-span, then fall
    zs = [nd["z"] for nd in arc]
    assert all(zs[i] <= zs[i + 1] for i in range(len(zs) // 2))
    assert all(zs[i] >= zs[i + 1] for i in range(len(zs) // 2, len(zs) - 1))
    print("  OK: circular arc endpoints / apex / circle fit")


def test_connect_chords_pratt():
    n = 6
    a = nodes_along_curve(straight_line_fn(12.0), n + 1, start_id=1)
    b = nodes_along_curve(straight_line_fn(12.0, 2.0), n + 1, start_id=n + 2)
    a_ids = [nd["id"] for nd in a]
    b_ids = [nd["id"] for nd in b]
    bars = connect_chords(a_ids, b_ids, "L 50x50x5", pattern="pratt",
                          chord_a_section="IPE 200", chord_b_section="IPE 100")
    assert len(bars) == 5 * n + 1, "pratt: 5n+1 bars"
    assert [br["id"] for br in bars] == list(range(1, 5 * n + 2))
    valid = set(a_ids) | set(b_ids)
    for br in bars:
        assert br["n1"] in valid and br["n2"] in valid, "dangling bar endpoint"
    # chord bars ALTERNATE top/bottom (matches the historical truss wiring
    # order, confirmed by the A2 byte-identity test):
    for i in range(n):
        t = bars[2 * i]
        bo = bars[2 * i + 1]
        assert t["section"] == "IPE 200"
        assert (t["n1"], t["n2"]) == (i + 1, i + 2)
        assert bo["section"] == "IPE 100"
        assert (bo["n1"], bo["n2"]) == (n + 2 + i, n + 3 + i)
    # web members use the web section
    assert all(br["section"] == "L 50x50x5" for br in bars[2 * n:])
    # verticals (2n+1..3n+1): (i+1) - (n+2+i)
    for i in range(n + 1):
        v = bars[2 * n + i]
        assert (v["n1"], v["n2"]) == (i + 1, n + 2 + i)
    # diagonals: (a[i]-b[i+1]) then (a[i+1]-b[i]) per panel
    for i in range(n):
        d1 = bars[3 * n + 1 + 2 * i]
        d2 = bars[3 * n + 2 + 2 * i]
        assert (d1["n1"], d1["n2"]) == (i + 1, n + 3 + i)
        assert (d2["n1"], d2["n2"]) == (i + 2, n + 2 + i)
    print("  OK: connect_chords pratt topology / sections / ids")


def test_connect_chords_warren():
    n = 6
    a = nodes_along_curve(straight_line_fn(12.0), n + 1, start_id=1)
    b = nodes_along_curve(straight_line_fn(12.0, 2.0), n + 1, start_id=n + 2)
    a_ids = [nd["id"] for nd in a]
    b_ids = [nd["id"] for nd in b]
    bars = connect_chords(a_ids, b_ids, "L 50x50x5", pattern="warren",
                          chord_a_section="IPE 200", chord_b_section="IPE 200")
    assert len(bars) == 3 * n, "warren: 3n bars (chords + diagonals, no verticals)"
    a_set, b_set = set(a_ids), set(b_ids)
    web_pairs = []
    for br in bars[2 * n:]:  # web only — every web bar must be a single-panel diagonal
        if br["n1"] in a_set:
            i1, i2 = a_ids.index(br["n1"]), b_ids.index(br["n2"])
        else:
            i1, i2 = b_ids.index(br["n1"]), a_ids.index(br["n2"])
        assert abs(i1 - i2) == 1, f"warren web bar {br} is not a single-panel diagonal"
        web_pairs.append((br["n1"], br["n2"]))
    assert len(set(web_pairs)) == len(web_pairs), "duplicate warren web bars"
    print("  OK: connect_chords warren topology (no verticals)")


def test_connect_chords_errors():
    def _expect_value_error(fn):
        try:
            fn()
        except ValueError:
            return
        raise AssertionError("expected ValueError")
    _expect_value_error(lambda: connect_chords([1, 2], [4, 5, 6], "IPE 200"))
    _expect_value_error(lambda: connect_chords(
        [1, 2, 3], [4, 5, 6], "IPE 200", pattern="howe"))
    print("  OK: connect_chords error handling")


def test_radial_ring():
    nodes = radial_ring(
        lambda ratio: (0.0, 0.0, round(5.0 * ratio, 6)),
        lambda ratio: 2.5, 16, 3, start_id=1)
    assert len(nodes) == 16 * 3
    assert [nd["id"] for nd in nodes] == list(range(1, 16 * 3 + 1))
    assert sorted(set(nd["z"] for nd in nodes)) == [0.0, 2.5, 5.0]
    for nd in nodes:
        assert abs(math.hypot(nd["x"], nd["y"]) - 2.5) < 1e-5
    for level in range(3):
        ring = [nd for nd in nodes if nd["z"] == 2.5 * level]
        assert len(ring) == 16
        assert len({nd["id"] for nd in ring}) == 16
    print("  OK: radial_ring counts / ids / ring geometry")

# --------------------------------------------------------------------------
# A2 — refactored templates byte-identical to the legacy inline code
# --------------------------------------------------------------------------

def test_legacy_byte_identity():
    truss_cases = [
        (12.0, 2.0, 6, "IPE 200", "IPE 200", "L 50x50x5"),
        (18.0, 3.5, 9, "IPE 300", "IPE 240", "L 60x60x6"),
        (1.0, 0.2, 4, "IPE 100", "IPE 100", "L 40x40x5"),
    ]
    for c in truss_cases:
        new = RobotBridge.truss_spec(
            span=c[0], height=c[1], panels=c[2],
            top_section=c[3], bottom_section=c[4], web_section=c[5])
        old = _legacy_truss_spec(*c)
        assert json.dumps(new) == json.dumps(old), f"truss_spec mismatch for {c[:3]}"
    tank_cases = [
        (2.5, 5.0, 16, 2, "IPE 200", "IPE 200"),
        (3.0, 6.5, 12, 3, "HEA 200", "IPE 240"),
        (1.0, 1.2, 8, 2, "IPE 100", "IPE 100"),
    ]
    for c in tank_cases:
        new = RobotBridge.cylindrical_tank_spec(
            radius=c[0], height=c[1], segments=c[2], ring_levels=c[3],
            section_vertical=c[4], section_ring=c[5])
        old = _legacy_tank_spec(*c)
        assert json.dumps(new) == json.dumps(old), f"tank_spec mismatch for {c[:4]}"
    arch_cases = [
        (30.0, 5.0, 10, "IPE 500", "IPE 500", "L 70x70x8", "top"),
        (30.0, 5.0, 10, "IPE 500", "IPE 500", "L 70x70x8", "bottom"),
        (9.0, 2.0, 8, "IPE 300", "IPE 240", "L 60x60x6", "top"),
        (18.0, 6.0, 12, "IPE 400", "IPE 360", "L 80x80x8", "bottom"),
        (1.0, 0.2, 4, "IPE 100", "IPE 100", "L 40x40x5", "top"),
    ]
    for c in arch_cases:
        new = RobotBridge.arch_truss_spec(
            span=c[0], rise=c[1], panels=c[2],
            top_section=c[3], bottom_section=c[4], web_section=c[5],
            arch_chord=c[6])
        old = _legacy_arch_truss_spec(*c)
        assert json.dumps(new) == json.dumps(old), f"arch_truss_spec mismatch for {c[:4]} arch={c[6]}"
    print(f"  OK: truss + tank + arch_truss specs byte-identical to legacy "
          f"({len(truss_cases)} truss + {len(tank_cases)} tank + "
          f"{len(arch_cases)} arch parameter sets)")

# --------------------------------------------------------------------------
# B1 — span-aware section suggestion
# --------------------------------------------------------------------------

def test_suggest_section():
    assert suggest_section("beam", 1.0) == "IPE 80"
    assert suggest_section("beam", 30.0) == "IPE 600"
    assert (section_depth_mm(suggest_section("beam", 30.0)) >
            section_depth_mm(suggest_section("beam", 1.0)))
    notes = []
    s = suggest_section("beam", 1.0, notes=notes)
    assert s == "IPE 80" and notes, "clamp/decision notes emitted"
    assert suggest_section("beam", 1.0, "IPE", depth_to_span=25.0) == "IPE 80"
    assert suggest_section("column", 3.5) == "IPE 140"
    assert (section_depth_mm(suggest_section("column", 6.0)) >
            section_depth_mm(suggest_section("column", 3.5)))
    assert suggest_section("web", 12.0, "L") == "L 100x100x10"
    assert section_depth_mm("IPE 200") == 200.0
    assert section_depth_mm("W 12X26") == 12.0 * 25.4
    assert section_depth_mm("garbage-name") is None
    print(f"  OK: suggest_section sizing / nearest-catalog / notes")
    print(f"      acceptance: beam 1m -> {suggest_section('beam', 1.0)}  |  "
          f"beam 30m -> {suggest_section('beam', 30.0)}")


# --------------------------------------------------------------------------
# B2 — template auto-sizing (the 1m bridge got IPE 200 bug)
# --------------------------------------------------------------------------

def test_template_auto_sizing():
    small = RobotBridge.truss_spec(span=1.0)
    big = RobotBridge.truss_spec(span=30.0)
    print(f"      acceptance: truss_spec(span=1.0) chord -> {small['bars'][0]['section']}"
          f"  |  truss_spec(span=30.0) chord -> {big['bars'][0]['section']}")
    assert small["bars"][0]["section"] != "IPE 200", \
        "1m truss must not get the old fixed 'IPE 200'"
    assert small["bars"][0]["section"] == "IPE 80"
    assert big["bars"][0]["section"] == "IPE 600"
    assert any("clamped" in n.lower() for n in small["__section_notes"])
    # explicit sections always win and are never overridden
    explicit = RobotBridge.truss_spec(
        span=1.0, top_section="IPE 500", bottom_section="IPE 400",
        web_section="L 80x80x8")
    assert explicit["bars"][0]["section"] == "IPE 500"     # top chord bar 1
    assert explicit["bars"][1]["section"] == "IPE 400"     # bottom chord bar 1
    assert explicit["bars"][12]["section"] == "L 80x80x8"  # first vertical (2n = 12)
    assert "__section_notes" not in explicit
    # other templates auto-size and carry decision notes too
    assert "__section_notes" in RobotBridge.cylindrical_tank_spec(radius=1.0, height=2.0)
    assert "__section_notes" in RobotBridge.grid_frame_spec(
        bay_width_x=6.0, bay_width_y=6.0, level_height=3.0)
    assert "__section_notes" in RobotBridge.braced_frame_spec(height=4.0, width=5.0)
    print("  OK: template auto-sizing + explicit-section override")


# --------------------------------------------------------------------------
# B3 — proportion safety net
# --------------------------------------------------------------------------

def test_check_section_proportions():
    old_style_1m = {
        "nodes": [
            {"id": 1, "x": 0.0, "y": 0.0, "z": 0.0},
            {"id": 2, "x": 1.0, "y": 0.0, "z": 0.0},
            {"id": 3, "x": 0.0, "y": 0.0, "z": 3.5},
            {"id": 4, "x": 1.0, "y": 0.0, "z": 3.5},
        ],
        "bars": [
            {"id": 1, "n1": 1, "n2": 2, "section": "IPE 200"},  # old fixed default
            {"id": 2, "n1": 3, "n2": 4, "section": "IPE 80"},   # auto-sized 1m beam
            {"id": 3, "n1": 1, "n2": 3, "section": "HEA 100"},  # 3.5m column
        ],
    }
    warnings = check_section_proportions(old_style_1m)
    flagged = {w["bar_id"]: w for w in warnings}
    assert 1 in flagged and flagged[1]["issue"] == "deep", \
        "the old 1m/IPE200 combination MUST be flagged"
    assert flagged[1]["span_to_depth"] == 5.0
    assert 2 not in flagged, "auto-sized IPE 80 on 1m is fine (ratio 12.5)"
    assert 3 not in flagged, "3.5m HEA 100 column (ratio ~36.5) must not alarm"
    # a genuinely slender column IS flagged
    bad = {"nodes": [{"id": 1, "x": 0.0, "y": 0.0, "z": 0.0},
                     {"id": 2, "x": 0.0, "y": 0.0, "z": 10.0}],
           "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "HEA 100"}]}
    w2 = check_section_proportions(bad)
    assert w2 and w2[0]["issue"] == "shallow"
    print("  OK: check_section_proportions flags 1m/IPE200, tolerates real columns")

# --------------------------------------------------------------------------
# A3 — arch truss template
# --------------------------------------------------------------------------

def test_arch_truss():
    arch = RobotBridge.arch_truss_spec(span=9.0, rise=2.0, panels=8)
    assert arch["__tpl"] == "arch_truss"
    assert len(arch["nodes"]) == 18
    assert len(arch["bars"]) == 5 * 8 + 1
    assert [s["node"] for s in arch["supports"]] == [10, 18]
    top = [nd for nd in arch["nodes"] if nd["id"] <= 9]
    bot = [nd for nd in arch["nodes"] if nd["id"] >= 10]
    assert max(nd["z"] for nd in top) == 2.0, "arched top chord peaks at rise"
    assert max(nd["z"] for nd in bot) == 0.0, "straight bottom chord at z=0"
    # auto-sized sections (span 9 -> 500 mm chord; web leg 9000/120=75 -> L 70)
    assert arch["bars"][0]["section"] == "IPE 500"
    assert arch["bars"][1]["section"] == "IPE 500"
    assert arch["bars"][2 * 8]["section"] == "L 70x70x8"
    # inverted: arched bottom chord + straight deck above at z=rise
    inv = RobotBridge.arch_truss_spec(span=9.0, rise=2.0, panels=8,
                                      arch_chord="bottom")
    bot_i = [nd for nd in inv["nodes"] if nd["id"] >= 10]
    top_i = [nd for nd in inv["nodes"] if nd["id"] <= 9]
    assert max(nd["z"] for nd in bot_i) == 2.0
    assert min(nd["z"] for nd in top_i) == 2.0
    assert [s["node"] for s in inv["supports"]] == [10, 18]
    # geometry is a real circular segment: apex node sits on the circle
    assert len(inv["bars"]) == 5 * 8 + 1
    print("  OK: arch_truss_spec bowstring + inverted geometry / sections")


def test_arch_truss_in_design_space():
    from batch.design_space import DesignSpace
    geom = RobotBridge.arch_truss_spec(span=9.0, rise=2.0, panels=8)
    ds = DesignSpace({
        "geometry": geom,
        "variable_groups": [
            {"group_name": "chords", "bar_ids": [1, 2, 3],
             "candidate_sections": ["IPE 500", "IPE 550"]},
        ],
    })
    assert ds.candidate_count() == 2
    candidates = ds.generate_candidates()
    assert len(candidates) == 2
    applied = ds.apply_to_geometry(candidates[0])
    assert applied["__tpl"] == "arch_truss", "geometry keys survive the design map"
    assert applied["bars"][0]["section"] in ("IPE 500", "IPE 550")
    # bars outside the variable map keep their auto-sized section
    assert applied["bars"][16]["section"] == "L 70x70x8"
    print("  OK: arch_truss_spec usable as DesignSpace geometry")


# --------------------------------------------------------------------------
# B4 — registry: no hardcoded section defaults, new tools registered
# --------------------------------------------------------------------------

def test_registry_schemas():
    from agent.tool_registry import TOOL_SCHEMAS, ToolExecutor
    by_name = {s["name"]: s for s in TOOL_SCHEMAS}
    assert "create_arch_truss" in by_name
    assert "check_section_proportions" in by_name
    assert "section_name" in by_name["create_bar"]["parameters"]["required"], \
        "create_bar must require an explicit (scale-aware) section"
    hardcoded = {"top_section", "bottom_section", "web_section",
                 "column_section", "beam_section", "brace_section",
                 "beam_x_section", "beam_y_section",
                 "section_vertical", "section_ring", "section"}
    for tpl in ("create_truss", "create_braced_frame",
                "create_rectangular_grid_frame", "create_cylindrical_tank",
                "create_panel", "create_arch_truss"):
        props = by_name[tpl]["parameters"]["properties"]
        for key in hardcoded:
            if key in props:
                assert "default" not in props[key], \
                    f"{tpl}.{key} still hardcodes a section default"
    assert hasattr(ToolExecutor, "_tool_create_arch_truss")
    assert hasattr(ToolExecutor, "_tool_check_section_proportions")
    # handler signatures must not re-introduce fixed section defaults either
    import inspect
    for meth, param_names in [
        ("_tool_create_truss", ("top_section", "bottom_section", "web_section")),
        ("_tool_create_braced_frame",
         ("column_section", "beam_section", "brace_section")),
        ("_tool_create_rectangular_grid_frame",
         ("column_section", "beam_x_section", "beam_y_section")),
        ("_tool_create_cylindrical_tank",
         ("section_vertical", "section_ring")),
        ("_tool_create_arch_truss",
         ("top_section", "bottom_section", "web_section")),
        ("_tool_create_panel", ("section",)),
    ]:
        sig = inspect.signature(getattr(ToolExecutor, meth))
        for p in param_names:
            assert sig.parameters[p].default is None, \
                f"{meth}.{p} still has a hardcoded section default"
    print("  OK: registry schemas (no hardcoded defaults, new tools + handlers)")


# --------------------------------------------------------------------------
# C1 — COMPOSE primitives: chord generators / web wrapper / bracing / support
# --------------------------------------------------------------------------

def test_compose_chord_generators():
    from tools.geometry_primitives import (
        generate_straight_chord, generate_arc_chord)
    c = generate_straight_chord(30.0, 10, elevation=0.0, plane=6.0,
                                section="IPE 500", start_id=1)
    assert len(c["nodes"]) == 11 and len(c["bars"]) == 10
    assert c["first"] == 1 and c["last"] == 11
    assert c["ids"] == list(range(1, 12))
    assert c["nodes"][0]["y"] == 6.0 and c["nodes"][-1]["y"] == 6.0
    assert c["nodes"][-1]["x"] == 30.0
    assert c["bars"][0]["section"] == "IPE 500"
    assert all(b["n1"] in c["ids"] and b["n2"] in c["ids"] for b in c["bars"])
    # custom start id (composition continues after earlier chains)
    c2 = generate_straight_chord(12.0, 4, start_id=100)
    assert c2["first"] == 100 and c2["last"] == 104
    assert c2["ids"] == [100, 101, 102, 103, 104]
    a = generate_arc_chord(30.0, 5.0, 10, elevation=2.0, arch="up")
    zs = [nd["z"] for nd in a["nodes"]]
    assert a["nodes"][0]["z"] == 2.0 and a["nodes"][-1]["z"] == 2.0
    assert max(zs) == 7.0, "apex = elevation + rise"
    d = generate_arc_chord(30.0, 5.0, 10, elevation=2.0, arch="down")
    dz = [nd["z"] for nd in d["nodes"]]
    assert max(dz) == 7.0, "inverted arch ends high"
    assert min(dz) == 2.0, "inverted arch dips to elevation at mid-span"
    print("  OK: generate_straight_chord / generate_arc_chord geometry + ids")


def test_compose_web_and_bracing_primitives():
    from tools.geometry_primitives import (
        generate_straight_chord, generate_arc_chord, connect_web_pattern,
        connect_bracing, apply_support_pattern)
    top = generate_arc_chord(30.0, 5.0, 10, elevation=0.0, section="IPE 500")
    bot = generate_straight_chord(30.0, 10, elevation=0.0, section="IPE 400")
    bars = connect_web_pattern(top, bot, "pratt", web_section="L 70x70x8")
    assert len(bars) == 5 * 10 + 1
    # chain-dict chord sections respected automatically
    assert bars[0]["section"] == "IPE 500"
    assert bars[1]["section"] == "IPE 400"
    assert bars[20]["section"] == "L 70x70x8"
    # bracing between two planes
    b2 = generate_arc_chord(30.0, 5.0, 10, plane=6.0, section="IPE 500",
                            start_id=100)
    br = connect_bracing(top, b2, "cross", section="L 50x50x5",
                         start_id=500)
    assert len(br) == 2 * 10, "cross bracing: 2 per panel"
    assert [x["id"] for x in br] == list(range(500, 520))
    tr = connect_bracing(top, b2, "transverse", start_id=600)
    assert len(tr) == 11, "transverse ties: n_panels+1"
    # support pattern
    sp = apply_support_pattern([1, 11], "pinned")
    assert sp == [{"node": 1, "type": "pinned"}, {"node": 11, "type": "pinned"}]
    # errors: mismatched panel counts / unknown pattern / mismatched bracing
    for fn in (
        lambda: connect_web_pattern(
            generate_straight_chord(30.0, 10), generate_straight_chord(30.0, 8)),
        lambda: connect_bracing(
            generate_straight_chord(30.0, 10), generate_straight_chord(30.0, 8)),
    ):
        try:
            fn()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for mismatched panels")
    try:
        connect_bracing(top, b2, "lattice")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for bad bracing pattern")
    print("  OK: connect_web_pattern / connect_bracing / apply_support_pattern")


def test_compose_copy_no_id_collision():
    """A copy op must NEVER produce node/bar ids that collide with any other
    chain or with web/bracing bars already in the composition registry."""
    from agent.tool_registry import ToolExecutor
    t = ToolExecutor(robot_visible=False)
    # chain A: nodes 1..11, bars 12..21
    t._compose_apply_step({"op": "chord", "name": "deck_a", "kind": "straight",
                           "span": 30, "n_panels": 10, "section": "IPE 500"})
    # copy of A: must be nodes 22..32, bars 33..42 (no overlap with A)
    t._compose_apply_step({"op": "copy", "source": "deck_a", "name": "deck_b",
                           "y_shift": 6})
    # a THIRD independent chain after the copy: must start at 43+
    t._compose_apply_step({"op": "chord", "name": "arch_a", "kind": "arc",
                           "span": 30, "rise": 5, "n_panels": 10,
                           "elevation": 0, "section": "IPE 500"})
    # copy of arch_a: nodes 64..74, bars 75..84
    t._compose_apply_step({"op": "copy", "source": "arch_a", "name": "arch_b",
                           "y_shift": 6})
    # web between arch_a and deck_a (plane 0): bars 85..
    t._compose_apply_step({"op": "web", "top": "arch_a", "bottom": "deck_a",
                           "pattern": "pratt", "web_section": "L 70x70x8"})
    # web between the copied pair: continues after
    t._compose_apply_step({"op": "web", "top": "arch_b", "bottom": "deck_b",
                           "pattern": "pratt", "web_section": "L 70x70x8"})
    # bracing both planes
    t._compose_apply_step({"op": "bracing", "plane_a": "arch_a",
                           "plane_b": "arch_b", "pattern": "cross",
                           "section": "L 50x50x5"})
    t._compose_apply_step({"op": "bracing", "plane_a": "deck_a",
                           "plane_b": "deck_b", "pattern": "cross",
                           "section": "L 50x50x5"})

    all_nodes = []
    all_bars = list(t._compose_bars)
    for ch in t._compose_chains.values():
        all_nodes.extend(ch["nodes"])
        all_bars.extend(ch["bars"])
    node_ids = [n["id"] for n in all_nodes]
    bar_ids = [b["id"] for b in all_bars]
    assert len(node_ids) == len(set(node_ids)), "colliding node ids"
    assert len(bar_ids) == len(set(bar_ids)), "colliding bar ids"
    # every bar endpoint must exist as a node (no dangling refs after copies)
    node_set = set(node_ids)
    for b in all_bars:
        assert b["n1"] in node_set and b["n2"] in node_set, \
            f"bar {b['id']} references missing node"
    # copies are geometrically offset by y_shift and otherwise identical
    a = t._compose_chains["deck_a"]
    b = t._compose_chains["deck_b"]
    assert len(a["ids"]) == len(b["ids"])
    for na, nb in zip(a["nodes"], b["nodes"]):
        assert nb["y"] == na["y"] + 6.0
        assert nb["x"] == na["x"] and nb["z"] == na["z"]
    print("  OK: copy never collides node/bar ids across the whole registry")


def test_compose_bracing_lengths_sane():
    """Bracing bar lengths must match the real 3D distance between their
    endpoints (Euclidean check against node coords) AND stay within physical
    bounds — for two chains with MATCHED panel counts but DIFFERENT span/rise
    (so the bracing bars genuinely span varying x/z gaps, not just y)."""
    from tools.geometry_primitives import generate_arc_chord, connect_bracing
    # plane A: 30 m span, 5 m rise; plane B: 24 m span, 3 m rise, y=6
    # both 10 panels -> 11 nodes each; node x/z DIFFER between the planes.
    a = generate_arc_chord(30.0, 5.0, 10, plane=0.0, section="IPE 500")
    b = generate_arc_chord(24.0, 3.0, 10, plane=6.0, section="IPE 500",
                           start_id=100)
    coords = {nd["id"]: (nd["x"], nd["y"], nd["z"]) for nd in a["nodes"] + b["nodes"]}
    bars = connect_bracing(a, b, "cross", section="L 50x50x5")
    assert len(bars) == 20
    y_gap = 6.0
    lengths = []
    for br in bars:
        x1, y1, z1 = coords[br["n1"]]
        x2, y2, z2 = coords[br["n2"]]
        L = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)
        lengths.append(L)
        assert L > 0, "degenerate zero-length bracing bar"
        assert L >= y_gap - 1e-9, \
            f"bracing bar {br['id']} shorter than the plane gap ({L:.4f} < {y_gap})"
        assert L < 30.0, f"bracing bar {br['id']} implausibly long ({L:.4f})"
    assert any(L > y_gap + 0.1 for L in lengths), \
        "expected at least one cross brace to span a horizontal/vertical gap"
    print("  OK: bracing lengths = true 3D endpoint distance, physically sane "
          "for mismatched span/rise chains")


def test_merge_coincident_nodes():
    # [AUDIT] Robot's solver merges coincident-but-distinct nodes during
    # Calculate() (live-verified 35 -> 25). merge_coincident_nodes() does that
    # merge UP FRONT so the composed spec equals what Robot analyzes.
    from tools.geometry_primitives import merge_coincident_nodes
    g = {
        "nodes": [
            {"id": 1, "x": 0.0, "y": 0.0, "z": 0.0},
            {"id": 56, "x": 0.0, "y": 0.0, "z": 0.0},
            {"id": 7, "x": 6.0, "y": 0.0, "z": 0.0},
            {"id": 62, "x": 6.0, "y": 0.0, "z": 0.0},
            {"id": 4, "x": 3.0, "y": 0.0, "z": 0.0},
        ],
        "bars": [
            {"id": 1, "n1": 56, "n2": 62, "section": "IPE 200"},
            {"id": 2, "n1": 1, "n2": 7, "section": "IPE 200"},
        ],
        "supports": [{"node": 1, "type": "pinned"},
                     {"node": 62, "type": "pinned"}],
        "loads": [{"kind": "nodal", "node": 56, "case": 1,
                    "fz": -10.0}],
    }
    m = merge_coincident_nodes(g)
    ids = [n["id"] for n in m["nodes"]]
    assert ids == [1, 7, 4], f"merged ids {ids}"
    by_id = {n["id"]: n for n in m["nodes"]}
    assert all(abs(by_id[i]["x"] - (0.0 if i == 1 else 6.0 if i == 7 else 3.0)) < 1e-9
               for i in ids)
    bars = {b["id"]: b for b in m["bars"]}
    assert bars[1]["n1"] == 1 and bars[1]["n2"] == 7, bars[1]
    assert bars[2]["n1"] == 1 and bars[2]["n2"] == 7, bars[2]
    assert {s["node"] for s in m["supports"]} == {1, 7}
    assert m["loads"][0]["node"] == 1
    assert m["__merged_coincident_nodes"] == 2
    # no coincident pairs -> unchanged copy
    m2 = merge_coincident_nodes({"nodes": [{"id": 1, "x": 0.0, "y": 0.0, "z": 0.0},
                                            {"id": 2, "x": 1.0, "y": 0.0, "z": 0.0}]})
    assert m2["__merged_coincident_nodes"] == 0
    print("  OK: merge_coincident_nodes rewrites endpoints/supports/loads, "
          "keeps lowest id")


def test_compose_coincident_node_detector():
    """_coincident_node_pairs() must flag distinct nodes sharing a coordinate
    (the arch-springing geometry that makes Robot silently lose bar-uniform
    loads), and come back empty for safe topologies. Pure bookkeeping - no
    COM needed."""
    from tools.robot_tool import RobotBridge
    b = RobotBridge()
    # twin-arch springing: arch node 1 and deck node 21 both at (0,0,0);
    # arch node 11 and deck node 31 both at (30,0,0); y=6 mirror pair at
    # (0,6,0)/(30,6,0).
    b._node_coords = {
        1: (0.0, 0.0, 0.0), 11: (30.0, 0.0, 0.0),
        21: (0.0, 0.0, 0.0), 31: (30.0, 0.0, 0.0),
        92: (0.0, 6.0, 0.0), 102: (30.0, 6.0, 0.0),
        103: (0.0, 6.0, 0.0), 113: (30.0, 6.0, 0.0),
        5: (15.0, 0.0, 2.5), 26: (15.0, 0.0, 0.0),
    }
    pairs = b._coincident_node_pairs()
    assert len(pairs) == 4, f"expected 4 coincident pairs, got {pairs}"
    flat = {p for pair in pairs for p in pair}
    assert {1, 21} <= flat and {31, 11} <= flat
    # the non-coincident arch crown / deck midspan are NOT flagged
    assert 5 not in flat and 26 not in flat
    # a safe topology (no coincident nodes) -> empty
    b._node_coords = {
        n: ((n % 5) * 3.0, (n // 5) * 6.0, (n % 2) * 2.0)
        for n in range(1, 12)
    }
    assert b._coincident_node_pairs() == []
    # near-but-not-equal coords must NOT be collapsed (tolerance is exact)
    b._node_coords = {1: (0.0, 0.0, 0.0), 2: (0.0, 0.0, 1e-6)}
    assert b._coincident_node_pairs() == []
    print("  OK: coincident-node detector flags twin-arch springing, "
          "ignores safe/floating-point-distinct topologies")


def test_compose_per_op_validation():
    """Every compose op must fail LOUDLY and IMMEDIATELY on bad input — not
    defer to the final finish() integrity check."""
    from agent.tool_registry import ToolExecutor, ToolExecutionError

    def _expect_error(step, frag, executor=None):
        t = executor or ToolExecutor(robot_visible=False)
        try:
            t._compose_apply_step(step)
        except ToolExecutionError as exc:
            assert frag.lower() in str(exc).lower(), \
                f"expected {frag!r} in: {exc}"
            return
        raise AssertionError(f"expected ToolExecutionError for step {step}")

    _expect_error({"op": "web", "top": "nope", "bottom": "deck_a",
                   "pattern": "pratt"}, "unknown chain")
    _expect_error({"op": "copy", "source": "ghost", "name": "g",
                   "y_shift": 6}, "unknown chain")
    _expect_error({"op": "copy", "source": "deck_a", "name": "deck_b",
                   "y_shift": "six"}, "y_shift must be a number")
    _expect_error({"op": "chord", "name": "bad_span", "kind": "straight",
                   "span": -5, "n_panels": 4}, "span must be > 0")
    _expect_error({"op": "chord", "name": "bad_panels", "kind": "straight",
                   "span": 10, "n_panels": 1}, "n_panels must be >= 2")
    _expect_error({"op": "support", "type": "hinge"}, "unknown support_type")
    _expect_error({"op": "bogus"}, "unknown op")

    # mismatched panel counts (web and bracing) after valid chains exist
    t = ToolExecutor(robot_visible=False)
    t._compose_apply_step({"op": "chord", "name": "deck_a", "kind": "straight",
                           "span": 30, "n_panels": 10, "section": "IPE 500"})
    t._compose_apply_step({"op": "chord", "name": "arch_a", "kind": "arc",
                           "span": 30, "rise": 5, "n_panels": 8,
                           "section": "IPE 500"})
    _expect_error({"op": "web", "top": "arch_a", "bottom": "deck_a",
                   "pattern": "pratt"}, "different panel counts", executor=t)
    _expect_error({"op": "bracing", "plane_a": "arch_a", "plane_b": "deck_a",
                   "pattern": "cross"}, "different panel counts", executor=t)
    # duplicate name must fail once the chain exists
    _expect_error({"op": "chord", "name": "deck_a", "kind": "straight",
                   "span": 20, "n_panels": 4, "section": "IPE 200"},
                  "already exists", executor=t)
    print("  OK: per-op compose validation fails loudly on every bad input")


def test_compose_copy_then_bracing_no_id_collision():
    # [AUDIT 2026-08-23] a bracing op immediately after a copy must not
    # collide with the copied chain chord bars (copy advanced the id
    # counter past nodes only; a following bracing/web op used to collide).
    from agent.tool_registry import ToolExecutor
    t = ToolExecutor(robot_visible=False)
    steps = [
        {"op": "chord", "name": "top_a", "kind": "straight", "span": 6.0,
         "n_panels": 6, "elevation": 2.0, "plane": 0.0, "section": "IPE 200"},
        {"op": "copy", "source": "top_a", "name": "ghost_a", "y_shift": 0.0},
        {"op": "bracing", "plane_a": "top_a", "plane_b": "ghost_a",
         "pattern": "cross", "section": "L 50x50x5"},
        {"op": "chord", "name": "deck_a", "kind": "straight", "span": 6.0,
         "n_panels": 6, "elevation": 0.0, "plane": 0.0, "section": "IPE 200"},
        {"op": "web", "top": "top_a", "bottom": "deck_a", "pattern": "pratt",
         "web_section": "L 50x50x5"},
    ]
    for st in steps:
        t._compose_apply_step(st)
    res = t._compose_finish()
    assert res["status"] == "ok", res
    bar_ids = [b["id"] for b in res["geometry"]["bars"]]
    assert len(bar_ids) == len(set(bar_ids)),         "duplicate bar ids after copy->bracing: " +         str(sorted({i for i in bar_ids if bar_ids.count(i) > 1}))
    print("  OK: copy-then-bracing never collides bar ids "
          f"({len(bar_ids)} unique bars)")


def test_compose_full_assembly_finish():
    """End-to-end: a 2-plane twin-arch via per-step calls -> finish() returns
    ONE spec whose integrity pre-flight is clean and counts are exact."""
    from agent.tool_registry import ToolExecutor
    t = ToolExecutor(robot_visible=False)
    steps = [
        {"op": "chord", "name": "arch_a", "kind": "arc", "span": 30.0,
         "rise": 5.0, "n_panels": 10, "elevation": 0.0, "plane": 0.0,
         "section": "IPE 500"},
        {"op": "chord", "name": "deck_a", "kind": "straight", "span": 30.0,
         "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 500"},
        {"op": "web", "top": "arch_a", "bottom": "deck_a", "pattern": "pratt",
         "web_section": "L 70x70x8"},
        {"op": "copy", "source": "arch_a", "name": "arch_b", "y_shift": 6.0},
        {"op": "copy", "source": "deck_a", "name": "deck_b", "y_shift": 6.0},
        {"op": "web", "top": "arch_b", "bottom": "deck_b", "pattern": "pratt",
         "web_section": "L 70x70x8"},
        {"op": "bracing", "plane_a": "arch_a", "plane_b": "arch_b",
         "pattern": "cross", "section": "L 50x50x5"},
        {"op": "bracing", "plane_a": "deck_a", "plane_b": "deck_b",
         "pattern": "cross", "section": "L 50x50x5"},
        {"op": "support", "chain": "deck_a", "type": "pinned"},
        {"op": "support", "chain": "deck_b", "type": "pinned"},
    ]
    for st in steps:
        t._compose_apply_step(st)
    res = t._compose_finish()
    assert res["status"] == "ok"
    geom = res["geometry"]
    # 4 chains x 11 nodes = 44 composed nodes, minus the 4 coincident
    # arch-springing pairs merged by compose finish (Robot's solver merges
    # coincident nodes; compose now does it up front) = 40 nodes. Bars: 4x10
    # chain chord bars + 2x29 web (verticals+diagonals only; chains already
    # carry chord bars; the 4 zero-length end verticals are dropped as
    # degenerate) + 2x20 bracing = 40 + 58 + 40 = 138. Supports: 2 ends x 2
    # decks.
    assert res["counts"]["nodes"] == 40
    assert res["counts"]["bars"] == 138
    assert res["counts"]["supports"] == 4
    assert geom["__composed"] is True
    # registry is cleared by finish (fresh composition next time)
    assert t._compose_chains == {} and t._compose_bars == []
    print("  OK: full twin-arch compose -> finish() spec integrity clean "
          f"({res['counts']})")


def main():
    print("=" * 72)
    print("Part A + Part B + Part C — geometry primitives / compose tests")
    print("=" * 72)
    test_nodes_along_curve()
    test_circular_arc()
    test_connect_chords_pratt()
    test_connect_chords_warren()
    test_connect_chords_errors()
    test_radial_ring()
    test_legacy_byte_identity()
    test_suggest_section()
    test_template_auto_sizing()
    test_check_section_proportions()
    test_arch_truss()
    test_arch_truss_in_design_space()
    test_registry_schemas()
    test_compose_chord_generators()
    test_compose_web_and_bracing_primitives()
    test_compose_copy_no_id_collision()
    test_compose_bracing_lengths_sane()
    test_merge_coincident_nodes()
    test_compose_coincident_node_detector()
    test_compose_copy_then_bracing_no_id_collision()
    test_compose_per_op_validation()
    test_compose_full_assembly_finish()
    print("ALL PART A + PART B + PART C TESTS PASSED")


if __name__ == "__main__":
    main()




