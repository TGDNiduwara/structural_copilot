"""
tools/bracing_registry.py
=========================
[EUROCODE Phase A — eurocode_scope.md §3, decision D6]
Unbraced-length (bracing) data model.

Robot has no concept of bracing points or member buckling lengths, so this
module is an EXPLICIT engineer-input layer sitting next to the structure
model. It stores, per bar:

    lcr_y         buckling length about the major axis (m)
    lcr_z         buckling length about the minor axis (m)
    lcr_lt        lateral-torsional unbraced length (m)
    brace_points  intermediate bracing points as FRACTIONS of the bar
                  length in [0, 1] (e.g. 0.5 = a purlin at mid-span).
                  Brace points shorten Lcr_LT only (the longest sub-span
                  between braces); they are NOT assumed to brace the
                  member for major/minor-axis column buckling.

DEFAULT-AND-WARN discipline (same as every other check in this repo):
consumers MUST resolve through ``lcr_lt_for`` / ``lcr_z_for`` /
``lcr_y_for``, which return the value AND its source ("explicit",
"brace_points" for a derived value, or "defaulted" = full bar length with
a warning). A defaulted value is a conservative assumption, NOT a verified
bracing condition, and every downstream result must carry the source.

Validation (D6): Lcr < 0 rejected with ValueError; Lcr > 2.5 x bar length
flagged as a suspicious K-factor (warning, never silently accepted).

Pure Python — no COM / pywin32 dependency. The registry instance lives on
RobotBridge.bracing (session-scoped; the batch runner reaches it via
session.bridge.bracing).

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("structural_copilot.bracing_registry")

#: Values above 2.5 x bar length are almost always data-entry errors
#: (a single unbraced segment with an effective-length factor > ~2.5 is
#: not a real structure) — surfaced as a warning, not silently accepted.
SUSPICIOUS_K_FACTOR = 2.5


class BracingRegistry:
    """Per-bar unbraced-length / bracing-point side-table (pure dict)."""

    def __init__(self) -> None:
        self._entries: Dict[int, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Entry lifecycle
    # ------------------------------------------------------------------ #

    def set_bracing(
        self,
        bar_id: int,
        lcr_y: Optional[float] = None,
        lcr_z: Optional[float] = None,
        lcr_lt: Optional[float] = None,
        brace_points: Optional[Sequence[float]] = None,
        bar_length: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Stores/updates the bracing data for ``bar_id``.

        ``None`` leaves the previous value untouched (first call creates a
        fully-defaulted entry). Negative unbraced lengths raise ValueError;
        ``brace_points`` must be fractions in [0, 1] (0.0/1.0 allowed but
        no-ops — the bar ends are always implicit braces).

        Parameters
        ----------
        bar_id : existing bar number (validated by the caller/bridge layer)
        lcr_y, lcr_z, lcr_lt : explicit unbraced lengths in metres
        brace_points : bracing positions as fractions of bar length in
            [0, 1]; only shortens ``lcr_lt`` (longest sub-span between
            braces)
        bar_length : physical bar length in metres, when known (used to
            validate the suspicious-K warning at store time)
        """
        bar_id = int(bar_id)
        entry = self._entries.setdefault(bar_id, {
            "lcr_y": None, "lcr_z": None, "lcr_lt": None,
            "brace_points": None,
        })
        for key, val in (("lcr_y", lcr_y), ("lcr_z", lcr_z),
                         ("lcr_lt", lcr_lt)):
            if val is not None:
                value = float(val)
                if value < 0.0:
                    raise ValueError(
                        f"bracing {key} for bar {bar_id} must be >= 0, got "
                        f"{value}")
                entry[key] = value
        if brace_points is not None:
            pts = sorted(float(p) for p in brace_points)
            for p in pts:
                if not (0.0 <= p <= 1.0):
                    raise ValueError(
                        f"brace_points for bar {bar_id} must be fractions in "
                        f"[0, 1], got {p}")
            # Drop redundant no-op end points (0.0 / 1.0) for cleanliness.
            entry["brace_points"] = [p for p in pts if 0.0 < p < 1.0]
        if bar_length is not None and float(bar_length) > 0.0:
            for key in ("lcr_y", "lcr_z", "lcr_lt"):
                if entry[key] is not None and \
                        entry[key] > SUSPICIOUS_K_FACTOR * float(bar_length):
                    logger.warning(
                        "bracing %s for bar %s (%.3f m) is %.3f m — a "
                        "K-factor of %.1f is suspicious; verify the input.",
                        key, bar_id, float(bar_length), entry[key],
                        entry[key] / float(bar_length))
        logger.info("bracing set for bar %s: %s", bar_id, entry)
        return dict(entry)

    def remove(self, bar_id: int) -> bool:
        """Drops the entry for ``bar_id`` (True if one existed)."""
        return self._entries.pop(int(bar_id), None) is not None

    def clear(self) -> int:
        """Empties the registry; returns how many entries were removed."""
        n = len(self._entries)
        self._entries.clear()
        return n

    def get(self, bar_id: int) -> Dict[str, Any]:
        """Raw stored entry for ``bar_id`` (empty dict if none)."""
        return dict(self._entries.get(int(bar_id), {}))

    def all_bars(self) -> List[int]:
        """Sorted bar ids with a stored entry."""
        return sorted(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------ #
    # Resolution (default-and-warn contract)
    # ------------------------------------------------------------------ #

    def _resolve_lcr(self, bar_id: int, key: str, length_m: float,
                     derived: Optional[float] = None,
                     derived_source: str = "brace_points") \
            -> Tuple[float, str, List[str]]:
        """Returns (value, source, warnings) for one unbraced-length key.

        ``derived`` is the value to use when the key itself was not given
        explicitly but CAN be derived from other explicit bracing data
        (e.g. lcr_lt from brace_points).
        """
        length = float(length_m)
        warnings: List[str] = []
        entry = self._entries.get(int(bar_id), {})
        explicit = entry.get(key)
        if explicit is not None:
            if explicit > SUSPICIOUS_K_FACTOR * length:
                warnings.append(
                    f"bar {bar_id} {key}={explicit:.3f} m is > "
                    f"{SUSPICIOUS_K_FACTOR} x bar length ({length:.3f} m) — "
                    "suspicious K-factor; verify the input.")
            return explicit, "explicit", warnings
        if derived is not None:
            return derived, derived_source, warnings
        warnings.append(
            f"bar {bar_id}: no explicit {key} set — defaulting to the full "
            f"bar length ({length:.3f} m). This is a CONSERVATIVE "
            "assumption, not a verified bracing condition.")
        return length, "defaulted", warnings

    def lcr_y_for(self, bar_id: int, length_m: float) \
            -> Tuple[float, str, List[str]]:
        """Major-axis buckling length -> (value_m, source, warnings)."""
        return self._resolve_lcr(int(bar_id), "lcr_y", length_m)

    def lcr_z_for(self, bar_id: int, length_m: float) \
            -> Tuple[float, str, List[str]]:
        """Minor-axis buckling length -> (value_m, source, warnings)."""
        return self._resolve_lcr(int(bar_id), "lcr_z", length_m)

    def lcr_lt_for(self, bar_id: int, length_m: float) \
            -> Tuple[float, str, List[str]]:
        """Lateral-torsional unbraced length -> (value_m, source, warnings).

        Resolution order:
          1. explicit ``lcr_lt`` if given,
          2. longest sub-span between ``brace_points`` (incl. the implicit
             end braces at 0.0 and 1.0),
          3. full bar length (conservative default — warning emitted).
        """
        entry = self._entries.get(int(bar_id), {})
        derived = None
        pts = entry.get("brace_points")
        if pts and float(length_m) > 0.0:
            bounds = [0.0] + list(pts) + [1.0]
            spans = [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]
            longest = max(spans)
            if longest < 1.0:
                derived = round(longest * float(length_m), 6)
        return self._resolve_lcr(int(bar_id), "lcr_lt", length_m,
                                 derived=derived)

    def resolve(self, bar_id: int, length_m: float) -> Dict[str, Any]:
        """Full resolved summary for one bar (values + sources + warnings).

        This is the row shape consumers (LTB / buckling / tool handlers)
        and the ``get_bracing`` tool return.
        """
        length = float(length_m)
        lcr_y, src_y, warn_y = self.lcr_y_for(bar_id, length)
        lcr_z, src_z, warn_z = self.lcr_z_for(bar_id, length)
        lcr_lt, src_lt, warn_lt = self.lcr_lt_for(bar_id, length)
        entry = self._entries.get(int(bar_id), {})
        return {
            "bar_id": int(bar_id),
            "length_m": round(length, 6),
            "lcr_y_m": round(lcr_y, 6),
            "lcr_z_m": round(lcr_z, 6),
            "lcr_lt_m": round(lcr_lt, 6),
            "lcr_y_source": src_y,
            "lcr_z_source": src_z,
            "lcr_lt_source": src_lt,
            "brace_points": list(entry.get("brace_points") or []),
            "warnings": warn_y + warn_z + warn_lt,
        }


