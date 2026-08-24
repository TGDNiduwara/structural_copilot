"""tools/diagnostics/opensees_crosscheck.py

[PHASE A - developer-facing diagnostic ONLY.]

Independent-solver diff tool: builds the SAME geometry spec (the JSON shape
used by create_structure_from_spec / build_structure_from_spec) as an
OpenSeesPy elastic-frame model, solves it, and reports reactions + member
forces so they can be compared against Robot (or closed form) during
discrepancy investigations (e.g. the bar_uniform / coincident-node
under-transfer class of bug).

NOT wired into tool_registry, the chat tools, app.py, or the batch
optimizer. openseespy is a dev-only dependency (requirements-dev.txt).

Usage:
    python tools/diagnostics/opensees_crosscheck.py --spec spec.json [--divisions 4]
    python tools/diagnostics/opensees_crosscheck.py --self-test

Section properties are resolved OFFLINE from nominal dims (idealized);
properties marked approximate. Reactions are in GLOBAL axes (unambiguous);
member forces are LOCAL bar-end forces (sign conventions documented in
README.md).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from typing import Any

E_STEEL = 210.0e9  # Pa
NU_STEEL = 0.3
G_STEEL = E_STEEL / (2.0 * (1.0 + NU_STEEL))

# ---------------------------------------------------------------------------
# Offline section-property resolver (nominal dims -> idealized A/Iy/Iz/J)
# ---------------------------------------------------------------------------
#: Idealized I-section dims (h, b, tw, tf, r) in METERS (EN 10365 nominal).
I_DIMS: dict[str, tuple[float, float, float, float, float]] = {
    "IPE 200": (0.200, 0.100, 0.0056, 0.0085, 0.012),
    "IPE 300": (0.300, 0.150, 0.0071, 0.0107, 0.015),
    "IPE 400": (0.400, 0.180, 0.0086, 0.0135, 0.021),
    "IPE 500": (0.500, 0.200, 0.0102, 0.0160, 0.021),
    "HEA 200": (0.190, 0.200, 0.0065, 0.0100, 0.018),
    "HEB 200": (0.200, 0.200, 0.0090, 0.0150, 0.018),
    "HEB 240": (0.240, 0.240, 0.0100, 0.0170, 0.021),
}


def _i_props(h, b, tw, tf, r):
    """Idealized rolled-I: A, Iy, Iz, J from nominal dims (fillets ignored;
    J via the section_data thin-wall + corner model)."""
    A = 2.0 * b * tf + (h - 2.0 * tf) * tw
    Iy = (
        b * tf**3 / 6.0 + 2.0 * tf * b * (h / 2.0 - tf / 2.0) ** 2 + tw * (h - 2.0 * tf) ** 3 / 12.0
    )
    Iz = 2.0 * tf * b**3 / 12.0 + (h - 2.0 * tf) * tw**3 / 12.0
    J = (2.0 * b * tf**3 + (h - 2.0 * tf) * tw**3) / 3.0 + 2.0 * 0.105 * (r + tw / 2.0) ** 4
    return A, Iy, Iz, J


def _fallback_i(family: str, depth_mm: float):
    """Rough nominal for I-families not in I_DIMS (clearly approximate)."""
    h = depth_mm * 1e-3
    b = (0.66 * h) if family in ("IPE", "IPN") else h
    tw = 0.011 * h
    tf = 0.03 * h
    r = 0.06 * h
    A, Iy, Iz, J = _i_props(h, b, tw, tf, r)
    return {"A": A, "Iy": Iy, "Iz": Iz, "J": J, "approx": True}


def section_props_offline(name: str) -> dict[str, Any]:
    """Resolve A/Iy/Iz/J (m2/m4/m4/m4) for a catalog-style section name."""
    key = str(name).strip().upper()
    m = re.match(r"CHS\s+([\d.]+)X([\d.]+)", key)
    if m:
        D = float(m.group(1)) * 1e-3
        t = float(m.group(2)) * 1e-3
        d = D - 2.0 * t
        A = math.pi * (D * D - d * d) / 4.0
        I = math.pi * (D**4 - d**4) / 64.0
        J = math.pi * (D**4 - d**4) / 32.0
        return {"A": A, "Iy": I, "Iz": I, "J": J, "approx": False}
    if key in I_DIMS:
        A, Iy, Iz, J = _i_props(*I_DIMS[key])
        return {"A": A, "Iy": Iy, "Iz": Iz, "J": J, "approx": False}
    m = re.match(r"(IPE|IPN|HEA|HEB|HEM)\s+(\d+)", key)
    if m:
        return _fallback_i(m.group(1), float(m.group(2)))
    m = re.match(r"L\s+([\d.]+)X([\d.]+)X([\d.]+)", key)
    if m:
        b = float(m.group(1)) * 1e-3
        t = float(m.group(3)) * 1e-3
        A = 2.0 * b * t - t * t
        rho = 0.29 * b  # rough radius of gyration for an equal-leg angle
        I = A * rho * rho
        J = (b * t**3 + (b - t) * t**3) / 3.0
        return {"A": A, "Iy": I, "Iz": I, "J": J, "approx": True}
    m = re.match(r"(RHS|SHS)\s+([\d.]+)X([\d.]+)X([\d.]+)", key)
    if m:
        b = float(m.group(2)) * 1e-3
        h = float(m.group(3)) * 1e-3 if m.group(1) == "RHS" else b
        t = float(m.group(4)) * 1e-3
        A = 2.0 * t * (b + h) - 4.0 * t * t
        Iy = (b * h**3 - (b - 2.0 * t) * (h - 2.0 * t) ** 3) / 12.0
        Iz = (h * b**3 - (h - 2.0 * t) * (b - 2.0 * t) ** 3) / 12.0
        a0 = (b - t) * (h - t)
        p = 2.0 * (b + h - 2.0 * t)
        J = 4.0 * a0 * a0 * t / p if p > 0 else 0.0
        return {"A": A, "Iy": Iy, "Iz": Iz, "J": J, "approx": True}
    raise ValueError(
        f"section_props_offline: unsupported section name {name!r}. Add it to "
        "I_DIMS or use an explicit CHS/RHS/SHS/L form."
    )


# ---------------------------------------------------------------------------
# OpenSeesPy 3D elastic-frame model builder
# ---------------------------------------------------------------------------
def _local_axes(i: tuple, j: tuple) -> tuple[list, list, list]:
    """Orthonormal local axes: x along the element, z from a safe reference."""
    x = [j[0] - i[0], j[1] - i[1], j[2] - i[2]]
    L = math.sqrt(sum(c * c for c in x))
    x = [c / L for c in x]
    if abs(x[2]) > 0.999:
        zref = [1.0, 0.0, 0.0]  # element along Z -> pick X as reference
    else:
        zref = [0.0, 0.0, 1.0]
    y = [
        zref[1] * x[2] - zref[2] * x[1],
        zref[2] * x[0] - zref[0] * x[2],
        zref[0] * x[1] - zref[1] * x[0],
    ]
    yl = math.sqrt(sum(c * c for c in y))
    y = [c / yl for c in y]
    z = [
        x[1] * y[2] - x[2] * y[1],
        x[2] * y[0] - x[0] * y[2],
        x[0] * y[1] - x[1] * y[0],
    ]
    return x, y, z


def _vec3_add(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def _vec3_sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _vec3_scale(a, s):
    return [a[0] * s, a[1] * s, a[2] * s]


def _vec3_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec3_len(a):
    return math.sqrt(_vec3_dot(a, a))


def solve_spec(spec: dict[str, Any], divisions: int = 4) -> dict[str, Any]:
    """Build + solve the spec in OpenSeesPy. Returns per-case results.

    Reactions: global axes [FX_kN, FZ_kN, MY_kNm] per supported node.
    Forces: local bar-end forces at stations 0..1 (step 1/divisions),
    columns [Bar_ID, Position_m, N_kN, Vy_kN, Vz_kN, Mx_kNm, My_kNm, Mz_kNm].
    """
    import openseespy.opensees as ops  # dev-only dependency

    nodes = {
        int(n["id"]): (float(n.get("x", 0.0)), float(n.get("y", 0.0)), float(n.get("z", 0.0)))
        for n in spec.get("nodes", []) or []
    }
    bars = spec.get("bars", []) or []
    supports = spec.get("supports", []) or []
    cases = spec.get("cases", []) or [{"id": 1}]
    loads = spec.get("loads", []) or []

    all_results: dict[int, dict[str, Any]] = {}

    for case in cases:
        cid = int(case["id"])
        ops.wipe()
        ops.model("basic", "-ndm", 3, "-ndf", 6)
        for nid, (x, y, z) in nodes.items():
            ops.node(nid, x, y, z)

        # geometry transforms + per-bar sub-mesh
        transf_of_bar: dict[int, int] = {}
        for b in bars:
            i = nodes[int(b["n1"])]
            j = nodes[int(b["n2"])]
            _, _, zl = _local_axes(i, j)
            ttag = 100_000 + int(b["id"])
            ops.geomTransf("Linear", ttag, float(zl[0]), float(zl[1]), float(zl[2]))
            transf_of_bar[int(b["id"])] = ttag

        # sub-element nodes and elements (interior nodes get fresh tags)
        sub_node_counter = 1_000_000

        elem_info: list[tuple[int, int, int]] = []  # (bar_id, k, element_tag)
        elem_stations: dict[int, list[tuple[int, int]]] = {}  # bar_id -> [(tag, k)]
        sub_nodes_by_bar: dict[int, list[int]] = {}
        sub_end_nodes: dict[int, list[int]] = {}
        sub_xyz_by_bar: dict[int, list[tuple]] = {}
        bar_len: dict[int, float] = {}

        for b in bars:
            bid = int(b["id"])
            i = nodes[int(b["n1"])]
            j = nodes[int(b["n2"])]
            L = _vec3_len(_vec3_sub(j, i))
            tag_list: list[int] = []
            xyz_list: list[tuple] = []
            node_list: list[int] = [int(b["n1"])]
            prev = int(b["n1"])
            prev_xyz = i
            for k in range(1, divisions + 1):
                ratio = k / divisions
                end_xyz = _vec3_add(i, _vec3_scale(_vec3_sub(j, i), ratio))
                if k == divisions:
                    end = int(b["n2"])
                else:
                    sub_node_counter += 1
                    ops.node(sub_node_counter, *end_xyz)
                    end = sub_node_counter
                node_list.append(end)
                tag = len(elem_info) + 1
                props = section_props_offline(str(b.get("section") or "IPE 200"))
                ops.element(
                    "elasticBeamColumn",
                    tag,
                    prev,
                    end,
                    props["A"],
                    E_STEEL,
                    G_STEEL,
                    props["J"],
                    props["Iy"],
                    props["Iz"],
                    transf_of_bar[bid],
                )
                elem_info.append((bid, k, tag))
                tag_list.append(tag)
                xyz_list.append(end_xyz)
                prev = end
                prev_xyz = end_xyz
            sub_nodes_by_bar[bid] = [int(b["n1"])] + tag_list  # element tags (for -ele)
            sub_end_nodes[bid] = node_list  # node tag at each sub-boundary
            sub_xyz_by_bar[bid] = [i] + xyz_list
            bar_len[bid] = L

        # Torsional anchor: a collinear 3D beam chain with translation-only
        # supports is free to twist about its own axis (a zero-energy mode -
        # each element sees zero relative twist). Real beam practice anchors
        # torsion at one support; fix MX on the first support node. This does
        # NOT affect in-plane statics (it only constrains the out-of-plane
        # rotational mode).
        torsional_anchor_done = False
        for s in supports:
            nid = int(s["node"])
            typ = str(s.get("type") or "pinned").lower()
            anchor_mx = 1 if (not torsional_anchor_done and typ != "fixed") else 0
            torsional_anchor_done = True
            if typ == "fixed":
                ops.fix(nid, 1, 1, 1, 1, 1, 1)
            elif typ == "roller_x":
                ops.fix(nid, 0, 1, 1, anchor_mx, 0, 0)
            else:
                ops.fix(nid, 1, 1, 1, anchor_mx, 0, 0)

        # every load must live inside an active load pattern for eleLoad to work
        ops.timeSeries("Linear", 1)
        ops.pattern("Plain", 1, 1)
        case_loads = [ld for ld in loads if int(ld.get("case", 1)) == cid]
        for ld in case_loads:
            kind = ld.get("kind")
            if kind == "nodal":
                nid = int(ld["node"])
                ops.load(
                    nid,
                    float(ld.get("fx", 0.0)),
                    float(ld.get("fy", 0.0)),
                    float(ld.get("fz", 0.0)),
                    0.0,
                    float(ld.get("my", 0.0)),
                    0.0,
                )
            elif kind == "bar_uniform":
                bid = int(ld["bar"])
                val = float(ld.get("value", 0.0))
                direction = str(ld.get("direction") or "Z").upper()
                gvec = {"X": (val, 0.0, 0.0), "Y": (0.0, val, 0.0), "Z": (0.0, 0.0, val)}[direction]
                pts = sub_xyz_by_bar[bid]
                for k in range(1, len(pts)):
                    xa, ya, za = _local_axes(pts[k - 1], pts[k])
                    ax = _vec3_dot(gvec, xa)
                    fy = _vec3_dot(gvec, ya)
                    fz = _vec3_dot(gvec, za)
                    tag = sub_nodes_by_bar[bid][k]
                    ops.eleLoad("-ele", tag, "-type", "-beamUniform", float(fy), float(fz))
                    # -beamUniform is transverse-only: the AXIAL component of a
                    # global-direction UDL on an inclined bar is applied as
                    # equivalent nodal end loads (exact for reactions).
                    seg_len = _vec3_len(_vec3_sub(pts[k], pts[k - 1]))
                    if abs(ax) > 1e-12 and seg_len > 0:
                        # axial component acts ALONG the bar axis: lump the full
                        # vector ax*xhat (not just the X part) to the segment ends
                        n_i = sub_end_nodes[bid][k - 2] if k >= 2 else int(b["n1"])
                        n_j = sub_end_nodes[bid][k - 1]
                        half = _vec3_scale(xa, ax * seg_len / 2.0)
                        ops.load(n_i, half[0], half[1], half[2], 0.0, 0.0, 0.0)
                        ops.load(n_j, half[0], half[1], half[2], 0.0, 0.0, 0.0)
            elif kind == "bar_concentrated":
                bid = int(ld["bar"])
                ratio = float(ld.get("ratio", 0.5))
                f = (float(ld.get("fx", 0.0)), float(ld.get("fy", 0.0)), float(ld.get("fz", 0.0)))
                pts = sub_xyz_by_bar[bid]
                # find the sub-element containing ratio, apply at the local ratio within it
                seg = min(int(ratio * divisions), divisions - 1)
                xa, ya, za = _local_axes(pts[seg], pts[seg + 1])
                py = _vec3_dot(f, ya)
                pz = _vec3_dot(f, za)
                tag = sub_nodes_by_bar[bid][seg + 1]
                xlocal = (ratio * divisions) - seg
                ops.eleLoad("-ele", tag, "-type", "-beamPoint", float(py), float(pz), float(xlocal))

        ops.system("BandGeneral")
        ops.numberer("RCM")
        ops.constraints("Plain")
        ops.integrator("LoadControl", 1.0)
        ops.algorithm("Linear")
        ops.analysis("Static")
        ok = ops.analyze(1)
        ops.reactions()

        reac = []
        for s in supports:
            r = ops.nodeReaction(int(s["node"]))
            reac.append(
                {
                    "Node_ID": int(s["node"]),
                    "Support_Type": str(s.get("type") or "pinned"),
                    "FX_kN": float(r[0]),
                    "FY_kN": float(r[1]),
                    "FZ_kN": float(r[2]),
                    "MY_kNm": float(r[4]),
                }
            )

        forces = []
        for bid, k, tag in elem_info:
            f = list(ops.eleForce(tag))
            end_i = f[:6]
            end_j = f[6:]
            # station (k-1)/divisions uses sub-element k's i-end; station k/divisions uses its j-end
            if k == 1:
                forces.append(
                    {
                        "Bar_ID": bid,
                        "Position_m": 0.0,
                        "N_kN": end_i[0],
                        "Vy_kN": end_i[1],
                        "Vz_kN": end_i[2],
                        "Mx_kNm": end_i[3],
                        "My_kNm": end_i[4],
                        "Mz_kNm": end_i[5],
                    }
                )
            forces.append(
                {
                    "Bar_ID": bid,
                    "Position_m": round(bar_len[bid] * k / divisions, 4),
                    "N_kN": end_j[0],
                    "Vy_kN": end_j[1],
                    "Vz_kN": end_j[2],
                    "Mx_kNm": end_j[3],
                    "My_kNm": end_j[4],
                    "Mz_kNm": end_j[5],
                }
            )

        all_results[cid] = {"analyze_ok": bool(ok == 0), "reactions": reac, "forces": forces}
        ops.wipe()

    return all_results


# ---------------------------------------------------------------------------
# Reporting / comparison helpers
# ---------------------------------------------------------------------------
def print_case(cid: int, res: dict[str, Any]) -> None:
    print(f"\n===== Case {cid} =====")
    print(f"  analyze_ok: {res['analyze_ok']}")
    print("  reactions (global axes):")
    for r in res["reactions"]:
        print(
            f"    node {r['Node_ID']:>5} {r['Support_Type']:<8} "
            f"FX={r['FX_kN']:9.4f} FZ={r['FZ_kN']:9.4f} MY={r['MY_kNm']:9.4f}"
        )
    print("  member forces (local, kN/kNm):")
    for f in res["forces"]:
        print(
            f"    bar {f['Bar_ID']:>4} @{f['Position_m']:5.2f} "
            f"N={f['N_kN']:9.4f} Vz={f['Vz_kN']:9.4f} My={f['My_kNm']:9.4f}"
        )


def equilibrium_check(res: dict[str, Any]) -> None:
    """Sanity: sum of vertical reactions vs total applied vertical load.
    The model carries the loads internally; reactions should balance."""
    total_fz = sum(r["FZ_kN"] for r in res["reactions"])
    print(f"  equilibrium: sum(FZ reactions) = {total_fz:.4f} kN")


def diff_against_robot(open_res: dict[str, Any], robot_reactions, robot_forces) -> None:
    """Print per-component diffs vs a Robot DataFrame set (passed by caller)."""
    rmap = {int(r["Node_ID"]): r for r in robot_reactions}
    print("  --- diff vs Robot (reactions, global) ---")
    max_pct = 0.0
    for r in open_res["reactions"]:
        rr = rmap.get(int(r["Node_ID"]))
        if rr is None:
            continue
        for comp, key in (("FX", "FX_kN"), ("FZ", "FZ_kN"), ("MY", "MY_kNm")):
            a = float(r[key])
            b = float(rr[key])
            denom = max(abs(a), abs(b), 1e-9)
            pct = abs(a - b) / denom * 100.0
            max_pct = max(max_pct, pct)
            print(
                f"    node {r['Node_ID']:>4} {comp}: OpenSees={a:9.4f} Robot={b:9.4f} diff={a - b:9.4f} ({pct:6.2f}%)"
            )
    print(f"    max reaction component diff: {max_pct:.2f}%")


# ---------------------------------------------------------------------------
# Built-in closed-form self-tests (the trust gate before any real use)
# ---------------------------------------------------------------------------
PORTAL_SPEC = {
    "project": "3D",
    "nodes": [
        {"id": 1, "x": 0, "z": 0},
        {"id": 2, "x": 0, "z": 3.0},
        {"id": 3, "x": 6.0, "z": 3.0},
        {"id": 4, "x": 6.0, "z": 0},
    ],
    "bars": [
        {"id": 1, "n1": 1, "n2": 2, "section": "HEB 240"},
        {"id": 2, "n1": 2, "n2": 3, "section": "IPE 500"},
        {"id": 3, "n1": 3, "n2": 4, "section": "HEB 240"},
    ],
    "supports": [
        {"node": 1, "type": "pinned"},
        {"node": 4, "type": "pinned"},
    ],
    "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
    "loads": [{"kind": "bar_uniform", "bar": 2, "case": 1, "direction": "Z", "value": -10.0}],
}

CHS_BEAM_SPEC = {
    "project": "3D",
    "nodes": [
        {"id": 1, "x": 0.0, "z": 0.0},
        {"id": 2, "x": 1.0, "z": 0.0},
        {"id": 3, "x": 2.0, "z": 0.0},
        {"id": 4, "x": 3.0, "z": 0.0},
        {"id": 5, "x": 4.0, "z": 0.0},
        {"id": 6, "x": 5.0, "z": 0.0},
    ],
    "bars": [
        {"id": 1, "n1": 1, "n2": 2, "section": "CHS 139.7x5"},
        {"id": 2, "n1": 2, "n2": 3, "section": "CHS 139.7x5"},
        {"id": 3, "n1": 3, "n2": 4, "section": "CHS 139.7x5"},
        {"id": 4, "n1": 4, "n2": 5, "section": "CHS 139.7x5"},
        {"id": 5, "n1": 5, "n2": 6, "section": "CHS 139.7x5"},
    ],
    "supports": [{"node": 1, "type": "pinned"}, {"node": 6, "type": "roller_x"}],
    "cases": [{"id": 1, "name": "UDL", "nature": "permanent"}],
    "loads": [
        {"kind": "bar_uniform", "bar": b, "case": 1, "direction": "Z", "value": -5.0}
        for b in range(1, 6)
    ],
}

CANTILEVER_SPEC = {
    "project": "3D",
    "nodes": [
        {"id": 1, "x": 0.0, "z": 0.0},
        {"id": 2, "x": 5.0, "z": 0.0},
    ],
    "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "IPE 300"}],
    "supports": [{"node": 1, "type": "fixed"}],
    "cases": [{"id": 1, "name": "TIP", "nature": "permanent"}],
    "loads": [{"kind": "nodal", "node": 2, "case": 1, "fz": -50.0}],
}


def run_closedform_selftest() -> int:
    """Trust gate: prove the OpenSeesPy solver against closed-form statics."""
    fails = 0

    def check(tag, got, want, tol_pct=0.1):
        nonlocal fails
        pct = abs(got - want) / abs(want) * 100.0 if want else 0.0
        ok = pct <= tol_pct
        print(f"  [{'OK' if ok else 'FAIL'}] {tag}: got {got:.4f} want {want:.4f} ({pct:.3f}%)")
        if not ok:
            fails += 1

    print("\n=== SELF-TEST 1: portal frame (pinned base, UDL beam) ===")
    res = solve_spec(PORTAL_SPEC, divisions=8)[1]
    assert res["analyze_ok"], "portal analyze failed"
    base_fz = [r["FZ_kN"] for r in res["reactions"]]
    check("col axial = wL/2 = 30 kN (each base)", base_fz[0], 30.0, 0.1)
    check("col axial = wL/2 = 30 kN (each base)", base_fz[1], 30.0, 0.1)
    check("sum reactions = wL = 60 kN", sum(base_fz), 60.0, 0.1)
    beam = [f for f in res["forces"] if f["Bar_ID"] == 2]
    m_mid = max(abs(f["My_kNm"]) for f in beam if abs(f["Position_m"] - 3.0) < 0.01)
    m_end = max(
        abs(f["My_kNm"])
        for f in beam
        if f["Position_m"] < 0.01 or abs(f["Position_m"] - 6.0) < 0.01
    )
    check("M_end + M_mid = wL^2/8 = 45 kNm", m_end + m_mid, 45.0, 0.2)

    print("\n=== SELF-TEST 2: simply-supported CHS beam (UDL) ===")
    res = solve_spec(CHS_BEAM_SPEC, divisions=4)[1]
    assert res["analyze_ok"], "chs beam analyze failed"
    fz = [r["FZ_kN"] for r in res["reactions"]]
    check("sum reactions = wL = 25 kN", sum(fz), 25.0, 0.1)
    check("reaction L = 12.5 kN", fz[0], 12.5, 0.1)
    # simply-supported UDL: the maximum |My| anywhere is the true midspan moment
    m_mid = max(abs(f["My_kNm"]) for f in res["forces"])
    check("midspan MY = wL^2/8 = 15.625 kNm", m_mid, 15.625, 0.1)

    print("\n=== SELF-TEST 3: cantilever (tip load) ===")
    res = solve_spec(CANTILEVER_SPEC, divisions=4)[1]
    assert res["analyze_ok"], "cantilever analyze failed"
    r0 = res["reactions"][0]
    check("base shear = P = 50 kN", r0["FZ_kN"], 50.0, 0.1)
    check("base moment = P*L = 250 kNm", abs(r0["MY_kNm"]), 250.0, 0.2)

    print("\n" + ("SELF-TEST: ALL PASS" if fails == 0 else f"SELF-TEST: {fails} FAILURE(S)"))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenSeesPy cross-check (Phase A diagnostic)")
    ap.add_argument(
        "--spec", help="path to a geometry spec JSON (create_structure_from_spec shape)"
    )
    ap.add_argument(
        "--divisions", type=int, default=4, help="sub-elements per bar (station resolution)"
    )
    ap.add_argument("--self-test", action="store_true", help="run the closed-form trust gate")
    ap.add_argument(
        "--robot", action="store_true", help="(reserved) diff against a live Robot build"
    )
    args = ap.parse_args()

    if args.self_test:
        return run_closedform_selftest()
    if not args.spec:
        ap.error("provide --spec or --self-test")

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    print(
        f"Solving spec {args.spec} ({len(spec.get('nodes', []))} nodes, {len(spec.get('bars', []))} bars)"
    )
    results = solve_spec(spec, divisions=args.divisions)
    for cid, res in results.items():
        print_case(cid, res)
        equilibrium_check(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
