"""
batch/test_chat_build_tools.py
==============================
Offline tests for the build-and-optimize chat tools added in
agent/tool_registry.py: export_structure_spec, list_available_sections,
apply_self_weight, preview_structure_geometry, and the set_support
"spring" option.

NO Robot COM: tools that touch the bridge use a _FakeBridge double (the
pattern from batch/test_export_candidate.py); the pure helpers
(available_sections / _self_weight_kn_m / plot_structure_wireframe) run
directly. The spring load/self-weight RobotOM paths themselves get live-
verified when a Robot seat is free.

Run:  python batch/test_chat_build_tools.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from agent.tool_registry import (
    TOOL_SCHEMAS,
    ToolExecutionError,
    ToolExecutor,
    _validate_tool_arguments,
)
from tools.diagram_tool import plot_structure_wireframe
from tools.robot_tool import RobotBridge
from tools.section_sizing import available_sections, section_families


class _FakeBridge:
    """Minimal RobotBridge double recording the calls the tools make."""

    def __init__(self, spec=None, geometry=None):
        self._spec = spec or {
            "project": "3D",
            "nodes": [{"id": 1, "x": 0, "z": 0}, {"id": 2, "x": 6, "z": 0}],
            "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "IPE 300"}],
            "supports": [{"node": 1, "type": "pinned"}],
            "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
            "loads": [{"kind": "bar_uniform", "bar": 1, "case": 1,
                       "direction": "Z", "value": -10.0}],
        }
        self._geometry = geometry or {
            "project": "3D",
            "nodes": {1: [0.0, 0.0, 0.0], 2: [6.0, 0.0, 0.0],
                      3: [6.0, 0.0, 3.0], 4: [0.0, 0.0, 3.0]},
            "bars": {1: [1, 2], 2: [2, 3], 3: [3, 4], 4: [4, 1]},
        }
        self.supports_calls = []
        self.self_weight_calls = []

    def is_alive(self):
        return True

    def export_structure_spec(self):
        return self._spec

    def get_model_geometry(self):
        return self._geometry

    def validate_stability(self):
        return {"ok": True, "mechanism": False, "nodes": [], "dofs": [],
                "message": "no mechanism detected (rank 4/4)"}

    def _iter_all_cases(self):
        objs = getattr(self, "case_objects", [])
        return [(i + 1, obj) for i, obj in enumerate(objs)]

    def _as_combination(self, obj):
        return None

    def define_combination(self, name, case_factors, combination_type="ULS"):
        calls = getattr(self, "combo_calls", None)
        if calls is None:
            self.combo_calls = []
            calls = self.combo_calls
        calls.append((name, dict(case_factors), combination_type))
        return {"name": name, "case_id": 100 + len(calls)}

    def apply_self_weight(self, case_id, density=7850.0):
        self.self_weight_calls.append((case_id, density))
        return {"case_id": case_id, "bars": 2, "total_self_weight_kn": 0.83,
                "density_kg_m3": density, "per_bar": []}

    def set_support(self, node_id, support_type="fixed",
                    spring_stiffness=None):
        self.supports_calls.append((node_id, support_type, spring_stiffness))


def _executor_with_fake(fake):
    ex = ToolExecutor()
    ex.robot = fake
    ex._robot_connected = True
    return ex


def test_export_structure_spec():
    names = {s["name"] for s in TOOL_SCHEMAS}
    assert "export_structure_spec" in names
    fake = _FakeBridge()
    ex = _executor_with_fake(fake)
    r = ex._tool_export_structure_spec()
    assert r["status"] == "ok"
    geom = r["geometry"]
    for key in ("project", "nodes", "bars", "supports", "cases", "loads"):
        assert key in geom, key
    assert geom["project"] == "3D"
    assert geom["bars"][0]["section"] == "IPE 300"
    assert geom["loads"][0]["kind"] == "bar_uniform"
    assert r["counts"]["nodes"] == 2 and r["counts"]["bars"] == 1
    _validate_tool_arguments("export_structure_spec", {})
    print("EXPORT SPEC: live-model -> geometry JSON shape (reverse of "
          "build_from_spec), no required params")


def test_list_available_sections():
    ipe = available_sections("IPE")
    assert "IPE 300" in ipe and "IPE 500" in ipe, ipe[:3]
    hea = available_sections("hea")   # case-insensitive
    assert "HEA 200" in hea
    assert section_families() == sorted(section_families())
    all_names = available_sections()
    assert len(all_names) > len(ipe)
    try:
        available_sections("NOPE")
        raise AssertionError("unknown family must raise")
    except ValueError:
        pass
    ex = ToolExecutor()
    r = ex._tool_list_available_sections("IPE")   # no Robot needed
    assert r["status"] == "ok" and r["count"] == len(ipe)
    assert r["sections"] == ipe
    print("SECTIONS: IPE/HEA series from the suggest_section catalog; "
          "family filter + error handling OK")


def test_apply_self_weight():
    assert abs(RobotBridge._self_weight_kn_m(42.3) - 42.3 * 9.81 / 1000.0) \
        < 1e-9
    fake = _FakeBridge()
    ex = _executor_with_fake(fake)
    r = ex._tool_apply_self_weight(1, density=7850.0)
    assert r["status"] == "ok" and r["total_self_weight_kn"] == 0.83
    assert fake.self_weight_calls == [(1, 7850.0)], fake.self_weight_calls
    ex._tool_apply_self_weight(3)
    assert fake.self_weight_calls[-1] == (3, 7850.0)
    print("SELF-WEIGHT: gravity conversion correct, handler forwards "
          "case_id + density to the bridge")

def test_set_support_spring():
    fake = _FakeBridge()
    ex = _executor_with_fake(fake)
    r = ex._tool_set_support(2, "spring", {"UZ": 100000.0})
    assert r["status"] == "ok"
    assert fake.supports_calls == [(2, "spring", {"UZ": 100000.0})]
    # spring without stiffness -> clean error (never reaches Robot).
    try:
        ex._tool_set_support(2, "spring", None)
        raise AssertionError("spring without stiffness must be rejected")
    except ToolExecutionError as exc:
        assert "spring_stiffness" in str(exc), exc
    # Non-spring types still pass through with stiffness ignored.
    ex._tool_set_support(2, "pinned")
    assert fake.supports_calls[-1] == (2, "pinned", None)
    _validate_tool_arguments("set_support",
                             {"node_id": 1, "support_type": "spring",
                              "spring_stiffness": {"UZ": 100000.0}})
    print("SPRING: stiffness passes through; spring-without-stiffness "
          "rejected; fixed/pinned path unchanged")


def test_preview_structure_geometry():
    # Pure wireframe (3D axonometric path) writes a real PNG.
    tmp = tempfile.mkdtemp(prefix="preview_")
    out = os.path.join(tmp, "wire.png")
    plot_structure_wireframe(
        {1: [0, 0, 0], 2: [6, 0, 0], 3: [6, 0, 3], 4: [0, 0, 3]},
        {1: [1, 2], 2: [2, 3], 3: [3, 4], 4: [4, 1]}, out)
    assert os.path.exists(out) and os.path.getsize(out) > 0

    # Handler: fake geometry -> saved path under generated/.
    fake = _FakeBridge()
    ex = _executor_with_fake(fake)
    r = ex._tool_preview_structure_geometry("test_wireframe.png")
    assert r["status"] == "ok" and os.path.exists(r["file_path"])
    assert r["nodes"] == 4 and r["bars"] == 4
    assert r["file_path"].endswith(".png")

    # No geometry yet -> clean error (real executor, empty bookkeeping).
    ex2 = ToolExecutor()
    try:
        ex2._tool_preview_structure_geometry("nothing.png")
        raise AssertionError("empty geometry must raise")
    except ToolExecutionError as exc:
        assert "No geometry to preview" in str(exc), exc
    print("PREVIEW: wireframe PNG generated (planar + 3D), handler "
          "returns saved path, empty model rejected")


def test_check_model_stability():
    from types import SimpleNamespace
    mc = RobotBridge._mechanism_check
    # Stable: 2-node pinned beam (translations fixed, rotations free).
    stable = mc({1: (0.0, 0.0), 2: (6.0, 0.0)},
                [(1, 2, 1e-3, 1e-6)], {1: (1, 1, 0), 2: (1, 1, 0)})
    assert stable["ok"] is True and stable["mechanism"] is False, stable
    assert "no mechanism detected" in stable["message"], stable
    # Mechanism: an unsupported node with no bar -> rank-deficient.
    mech = mc({1: (0.0, 0.0), 2: (3.0, 0.0)}, [], {1: (1, 1, 0)})
    assert mech["ok"] is False and mech["mechanism"] is True, mech
    assert 2 in mech["nodes"], mech
    assert "likely mechanism" in mech["message"], mech
    # Empty model.
    assert mc({}, [], {})["message"] == "no nodes in model"

    # Handler passthrough.
    fake = _FakeBridge()
    ex = _executor_with_fake(fake)
    r = ex._tool_check_model_stability()
    assert r["status"] == "ok" and r["ok"] is True and r["mechanism"] is False
    _validate_tool_arguments("check_model_stability", {})
    print("STABILITY: pure rank check flags a floating-node mechanism, "
          "passes a pinned beam; handler returns ok/rank/message")


def test_generate_code_combinations():
    from types import SimpleNamespace
    cf = RobotBridge.eurocode_combination_factors
    # 1 permanent + 1 imposed -> ULS 1.35/1.5 + SLS 1.0/1.0.
    plans = cf([(1, "permanent"), (2, "imposed")], "ULS_SLS_basic")
    assert [p["name"] for p in plans] == ["ULS_2", "SLS_char"], plans
    assert plans[0]["case_factors"] == {1: 1.35, 2: 1.5}, plans
    assert plans[0]["combination_type"] == "ULS"
    assert plans[1]["case_factors"] == {1: 1.0, 2: 1.0}
    assert plans[1]["combination_type"] == "SLS"
    # 1 permanent + 2 imposed -> one ULS per leading variable, others 1.05.
    plans2 = cf([(1, "permanent"), (2, "imposed"), (3, "imposed")])
    assert [p["name"] for p in plans2] == ["ULS_2", "ULS_3", "SLS_char"]
    assert plans2[0]["case_factors"] == {1: 1.35, 2: 1.5, 3: 1.05}
    assert plans2[1]["case_factors"] == {1: 1.35, 2: 1.05, 3: 1.5}
    # Permanent-only and the subset sets.
    only_perm = cf([(1, "permanent")])
    assert only_perm[0]["case_factors"] == {1: 1.35}
    assert len(cf([(1, "permanent"), (2, "imposed")], "ULS_only")) == 1
    assert len(cf([(1, "permanent"), (2, "imposed")], "SLS_only")) == 1
    for bad in (([], "ULS_SLS_basic"), ([(1, "wind")], "ULS_SLS_basic"),
                ([(1, "permanent")], "nope")):
        try:
            cf(*bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass

    # Handler: reads real simple cases, calls define_combination once per plan.
    fake = _FakeBridge()
    fake.case_objects = [SimpleNamespace(Nature=0), SimpleNamespace(Nature=1)]
    ex = _executor_with_fake(fake)
    r = ex._tool_generate_code_combinations("ULS_SLS_basic")
    assert r["status"] == "ok" and r["count"] == 2, r
    assert fake.combo_calls == [
        ("ULS_2", {1: 1.35, 2: 1.5}, "ULS"),
        ("SLS_char", {1: 1.0, 2: 1.0}, "SLS"),
    ], fake.combo_calls
    print("COMBINATIONS: EN 1990 factors (1.35/1.5, multi-variable 1.05, "
          "SLS 1.0), handler drives define_combination per plan")


def main():
    print("=" * 72)
    print("Chat build-tools tests (offline, fake bridge)")
    print("=" * 72)
    test_export_structure_spec()
    test_list_available_sections()
    test_apply_self_weight()
    test_set_support_spring()
    test_preview_structure_geometry()
    test_check_model_stability()
    test_generate_code_combinations()
    print()
    print("ALL CHAT BUILD-TOOLS TESTS PASSED")


if __name__ == "__main__":
    main()

