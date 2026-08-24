"""
tools/result_store.py
=====================
[PHASE 2] Session-scoped, in-memory snapshot store for iterative structural
design. Lets the agent save the current model + results under a key (e.g. a
variant name like "HEB200" or "pinned-base"), then list/compare/retrieve
variants to decide which design is best.

Pure Python — no COM / pywin32 dependency. Instantiated on the ToolExecutor
so each Streamlit session gets its own store.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger("structural_copilot.result_store")
logger.setLevel(logging.INFO)


def _df_to_markdown(df: pd.DataFrame | None, max_rows: int = 12) -> str:
    """Renders a DataFrame as a compact markdown table (capped rows)."""
    if df is None or df.empty:
        return "_no data_"
    shown = df.head(max_rows)
    lines = [
        "| " + " | ".join(str(c) for c in shown.columns) + " |",
        "|" + "|".join(["---"] * len(shown.columns)) + "|",
    ]
    for _, row in shown.iterrows():
        cells = []
        for v in row:
            cells.append(f"{v:.2f}" if isinstance(v, float) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    if len(df) > max_rows:
        lines.append(f"_(showing first {max_rows} of {len(df)} rows)_")
    return "\n".join(lines)


class ResultStore:
    """In-memory dict of named result snapshots (key -> snapshot dict)."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}

    # --- [RS_STORE] ---
    def store(
        self,
        key: str,
        summary: dict[str, Any] | None,
        member_forces: pd.DataFrame | None,
        reactions: pd.DataFrame | None,
        boq: pd.DataFrame | None,
        utilization: pd.DataFrame | None = None,
        ltb_status: str | None = None,
        connection_status: str | None = None,
    ) -> str:
        """Saves a snapshot under `key` (overwrites if the key exists) and
        returns a one-line confirmation summary."""
        if not key or not str(key).strip():
            return "Error: store_result requires a non-empty 'key'."
        key = str(key).strip()

        bars = int(summary.get("bars", 0)) if summary else 0
        nodes = int(summary.get("nodes", 0)) if summary else 0
        total_weight = None
        if boq is not None and not boq.empty and "Total_Weight_kg" in boq.columns:
            try:
                total_weight = float(pd.to_numeric(boq["Total_Weight_kg"], errors="coerce").sum())
            except Exception:
                total_weight = None
        max_my = None
        if (
            member_forces is not None
            and not member_forces.empty
            and "MY_kNm" in member_forces.columns
        ):
            try:
                max_my = float(member_forces["MY_kNm"].abs().max())
            except Exception:
                max_my = None

        # [P4] Utilization summary — makes snapshots mean "does it pass",
        # not just "how much does it weigh".
        max_util = gov_check = None
        n_pass = n_fail = n_uncheckable = 0
        if (
            utilization is not None
            and not utilization.empty
            and "Utilization" in utilization.columns
        ):
            try:
                u = utilization
                n_pass = int((u["Status"] == "PASS").sum())
                n_fail = int((u["Status"] == "FAIL").sum())
                n_uncheckable = int((u["Status"] == "NOT_CHECKABLE").sum())
                valid = pd.to_numeric(u["Utilization"], errors="coerce")
                if valid.notna().any():
                    idx = valid.idxmax()
                    max_util = round(float(valid.max()), 4)
                    gov_check = str(u.loc[idx, "Governing_Check"])
            except Exception:
                max_util = gov_check = None

        self._snapshots[key] = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary or {},
            "member_forces": member_forces.copy() if member_forces is not None else None,
            "reactions": reactions.copy() if reactions is not None else None,
            "boq": boq.copy() if boq is not None else None,
            "utilization": utilization.copy() if utilization is not None else None,
            "bars": bars,
            "nodes": nodes,
            "total_weight_kg": total_weight,
            "max_abs_my_knm": max_my,
            "max_utilization": max_util,
            "governing_check": gov_check,
            "ltb_status": ltb_status,
            "connection_status": connection_status,
            "n_pass": n_pass,
            "n_fail": n_fail,
            "n_not_checkable": n_uncheckable,
        }
        logger.info("Stored result snapshot '%s' (%s bars).", key, bars)

        bits = [f"Stored '{key}': {bars} bars / {nodes} nodes"]
        if total_weight is not None:
            bits.append(f"{total_weight:.0f} kg total weight")
        if max_my is not None:
            bits.append(f"max |MY| {max_my:.1f} kNm")
        if max_util is not None:
            bits.append(
                f"max utilization {max_util:.2f} ({gov_check}); "
                f"{n_pass} pass / {n_fail} fail"
                + (f" / {n_uncheckable} not checkable" if n_uncheckable else "")
            )
        return ", ".join(bits) + "."

    # --- [RS_RETRIEVE] ---
    def retrieve(self, key: str) -> str:
        """Returns the stored snapshot formatted as readable markdown text."""
        snap = self._snapshots.get(str(key).strip())
        if snap is None:
            available = ", ".join(self._snapshots) or "(none)"
            return (
                f"Error: no stored result named '{key}'. "
                f"Available keys: {available}. "
                "Call store_result first."
            )

        lines = [
            f"### Stored result '{key}' (saved {snap['timestamp']})",
            f"- Model: {snap['bars']} bars / {snap['nodes']} nodes",
        ]
        if snap.get("total_weight_kg") is not None:
            lines.append(f"- Total steel weight: {snap['total_weight_kg']:.1f} kg")
        if snap.get("max_abs_my_knm") is not None:
            lines.append(f"- Max |MY|: {snap['max_abs_my_knm']:.2f} kNm")
        if snap.get("max_utilization") is not None:
            verdict = "PASSES" if snap.get("n_fail", 0) == 0 else "FAILS"
            lines.append(
                f"- Max utilization: {snap['max_utilization']:.2f} "
                f"({snap.get('governing_check')}) — "
                f"{snap.get('n_pass', 0)} pass / {snap.get('n_fail', 0)} fail"
                + (
                    f" / {snap.get('n_not_checkable', 0)} not checkable"
                    if snap.get("n_not_checkable")
                    else ""
                )
                + f" => design {verdict} the analytical check"
            )
        sections = (snap.get("summary") or {}).get("sections") or {}
        if sections:
            lines.append("- Sections: " + ", ".join(f"{k} x{v}" for k, v in sections.items()))
        lines.append("\n**Member forces**\n" + _df_to_markdown(snap["member_forces"]))
        lines.append("\n**Reactions**\n" + _df_to_markdown(snap["reactions"]))
        lines.append("\n**Bill of materials**\n" + _df_to_markdown(snap["boq"]))
        lines.append(
            "\n**Utilization (analytical check)**\n" + _df_to_markdown(snap.get("utilization"))
        )
        return "\n".join(lines)

    # --- [RS_LIST_CLEAR] ---
    def list_results(self) -> str:
        """One line per stored variant: key, timestamp, weight, max moment."""
        if not self._snapshots:
            return (
                "No stored results yet. Build a model, run solve + the "
                "export_* tools, then call store_result with a variant "
                "name such as 'HEB200'."
            )
        lines = [f"{len(self._snapshots)} stored result(s):"]
        for key, snap in self._snapshots.items():
            bits = [f"- '{key}' [{snap['timestamp']}] {snap['bars']} bars"]
            if snap.get("total_weight_kg") is not None:
                bits.append(f"{snap['total_weight_kg']:.0f} kg")
            if snap.get("max_abs_my_knm") is not None:
                bits.append(f"max |MY| {snap['max_abs_my_knm']:.1f} kNm")
            if snap.get("max_utilization") is not None:
                verdict = "PASS" if snap.get("n_fail", 0) == 0 else "FAIL"
                bits.append(
                    f"util {snap['max_utilization']:.2f} "
                    f"({snap.get('governing_check')}) [{verdict}]"
                )
            if snap.get("ltb_status"):
                bits.append(f"ltb {snap['ltb_status']}")
            if snap.get("connection_status"):
                bits.append(f"conn {snap['connection_status']}")
            lines.append(" | ".join(bits))
        return "\n".join(lines)

    def clear(self) -> str:
        """Empties the store; confirms how many entries were cleared."""
        n = len(self._snapshots)
        self._snapshots.clear()
        return f"Cleared {n} stored result(s)."

    @property
    def keys(self) -> list[str]:
        return list(self._snapshots.keys())
