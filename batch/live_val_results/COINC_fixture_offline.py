"""COINC_fixture_offline.py - build F2/F3 geometries, inventory coincident pairs."""
from __future__ import annotations
import sys, json
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor

def compose(ex, steps):
    for st in steps:
        ex.dispatch("compose_structure", {"action": "step", "step": st})
    fin = json.loads(ex.dispatch("compose_structure", {"action": "finish"}))
    return fin["geometry"]

def coincident_pairs(g):
    seen, pairs = {}, []
    for n in g["nodes"]:
        k = (round(n["x"],6), round(n["y"],6), round(n["z"],6))
        if k in seen:
            pairs.append((seen[k], n["id"]))
        else:
            seen[k] = n["id"]
    return pairs

def zero_len(g):
    c = {n["id"]:(n["x"],n["y"],n["z"]) for n in g["nodes"]}
    return [b["id"] for b in g["bars"] if c[b["n1"]] == c[b["n2"]]]

F2_STEPS = [
    {"op": "chord", "name": "deck_a", "kind": "straight", "span": 6.0,
     "n_panels": 6, "elevation": 0.0, "plane": 0.0, "section": "IPE 200"},
    {"op": "chord", "name": "top_a", "kind": "straight", "span": 6.0,
     "n_panels": 6, "elevation": 2.0, "plane": 0.0, "section": "IPE 200"},
    {"op": "web", "top": "top_a", "bottom": "deck_a", "pattern": "pratt",
     "web_section": "L 50x50x5"},
    {"op": "chord", "name": "arch_a", "kind": "arc", "span": 6.0,
     "rise": 1.0, "n_panels": 6, "elevation": 0.0, "plane": 0.0,
     "section": "IPE 200"},
    {"op": "web", "top": "arch_a", "bottom": "deck_a", "pattern": "pratt",
     "web_section": "L 50x50x5"},
    {"op": "chord", "name": "inv_a", "kind": "arc", "span": 6.0,
     "rise": 0.5, "n_panels": 6, "elevation": 0.0, "plane": 0.0,
     "arch": "down", "section": "IPE 200"},
    {"op": "web", "top": "inv_a", "bottom": "deck_a", "pattern": "pratt",
     "web_section": "L 50x50x5"},
    {"op": "copy", "source": "top_a", "name": "ghost_a", "y_shift": 0.0},
    {"op": "bracing", "plane_a": "top_a", "plane_b": "ghost_a",
     "pattern": "cross", "section": "L 50x50x5"},
    {"op": "support", "chain": "deck_a", "type": "pinned"},
]
F3_STEPS = [
    {"op": "chord", "name": "deck_a", "kind": "straight", "span": 6.0,
     "n_panels": 6, "elevation": 0.0, "plane": 0.0, "section": "IPE 200"},
    {"op": "chord", "name": "top_a", "kind": "straight", "span": 6.0,
     "n_panels": 6, "elevation": 2.0, "plane": 0.0, "section": "IPE 200"},
    {"op": "web", "top": "top_a", "bottom": "deck_a", "pattern": "pratt",
     "web_section": "L 50x50x5"},
    {"op": "support", "chain": "deck_a", "type": "pinned"},
]

ex = ToolExecutor(robot_visible=False)
g2 = compose(ex, F2_STEPS)
print("F2: nodes", len(g2["nodes"]), "bars", len(g2["bars"]))
print("F2 coincident pairs:", coincident_pairs(g2))
print("F2 zero-length bars:", zero_len(g2))
g3 = compose(ex, F3_STEPS)
print("F3: nodes", len(g3["nodes"]), "bars", len(g3["bars"]))
print("F3 coincident pairs:", coincident_pairs(g3))
json.dump({"F2": g2, "F3": g3}, open(r"batch/live_val_results/COINC_fixtures.json", "w"), indent=1)
