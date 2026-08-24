"""Template / compose-structure tool handlers.

[FIX 06] Extracted verbatim from agent/tool_registry.py - dispatch binds
these functions onto ToolExecutor as `_tool_*` methods, so the public API
and behaviour are unchanged.
"""

from __future__ import annotations

import structlog

from agent.tools._shared import ToolExecutionError

logger = structlog.get_logger("structural_copilot.template_handlers")  # [FIX 08]


def tool_create_structure_from_spec(self, spec=None) -> dict:
    self._ensure_robot()  # [WP1 fix] connect before touching the bridge
    summary = self.robot.build_structure_from_spec(spec)
    logger.info("Built structure from spec: %s", summary)
    return {"status": "ok", **summary}


def tool_compose_structure(
    self,
    action: str = "step",
    step: dict = None,
    steps: list = None,
) -> dict:
    """Arbitrary-shape composition from verified geometry primitives
    (see the compose_structure schema for the full contract)."""
    action = str(action or "step").lower()
    if action == "reset":
        return self._compose_reset()
    if action == "finish":
        return self._compose_finish()
    if action == "batch":
        if not steps:
            raise ToolExecutionError(
                "compose_structure action='batch' requires a 'steps' list "
                "(keep it SMALL: <=5-6 steps; larger assemblies should use "
                "one action='step' call per step)."
            )
        last = None
        for st in steps:
            last = self._compose_apply_step(st or {})
        return {
            "status": "ok",
            "message": f"applied {len(steps)} step(s)",
            "last": last,
            "chain_count": len(self._compose_chains),
            "bars_so_far": len(self._compose_bars),
            "supports_so_far": len(self._compose_supports),
        }
    if action == "step":
        if not step:
            raise ToolExecutionError("compose_structure action='step' requires a 'step' dict.")
        res = self._compose_apply_step(step)
        res["chain_count"] = len(self._compose_chains)
        res["bars_so_far"] = len(self._compose_bars)
        res["supports_so_far"] = len(self._compose_supports)
        return res
    raise ToolExecutionError(
        f"compose_structure: unknown action '{action}' (step | batch | finish | reset)."
    )


def tool_get_structure_summary(self) -> dict:
    self._ensure_robot()  # [WP1 fix]
    summary = self.robot.get_structure_summary()
    return {"status": "ok", **summary}


def tool_create_rectangular_grid_frame(
    self,
    levels: int = 2,
    bays_x: int = 2,
    bays_y: int = 2,
    bay_width_x: float = 5.0,
    bay_width_y: float = 5.0,
    level_height: float = 3.5,
    column_section: str = None,
    beam_x_section: str = None,
    beam_y_section: str = None,
) -> dict:
    self._ensure_robot()  # [WP1 fix]
    summary = self.robot.create_rectangular_grid_frame(
        levels=levels,
        bays_x=bays_x,
        bays_y=bays_y,
        bay_width_x=bay_width_x,
        bay_width_y=bay_width_y,
        level_height=level_height,
        column_section=column_section,
        beam_x_section=beam_x_section,
        beam_y_section=beam_y_section,
    )
    return {"status": "ok", **summary}


def tool_create_truss(
    self,
    span: float = 12.0,
    height: float = 2.0,
    panels: int = 6,
    top_section: str = None,
    bottom_section: str = None,
    web_section: str = None,
) -> dict:
    self._ensure_robot()  # [WP1 fix]
    summary = self.robot.create_truss(
        span=span,
        height=height,
        panels=panels,
        top_section=top_section,
        bottom_section=bottom_section,
        web_section=web_section,
    )
    return {"status": "ok", **summary}


def tool_create_braced_frame(
    self,
    height: float = 6.0,
    width: float = 6.0,
    column_section: str = None,
    beam_section: str = None,
    brace_section: str = None,
) -> dict:
    self._ensure_robot()  # [WP1 fix]
    summary = self.robot.create_braced_frame(
        height=height,
        width=width,
        column_section=column_section,
        beam_section=beam_section,
        brace_section=brace_section,
    )
    return {"status": "ok", **summary}


def tool_create_arch_truss(
    self,
    span: float = 30.0,
    rise: float = 5.0,
    panels: int = 10,
    top_section: str = None,
    bottom_section: str = None,
    web_section: str = None,
    arch_chord: str = "top",
) -> dict:
    self._ensure_robot()  # [Part A] bowstring / arch truss template
    summary = self.robot.create_arch_truss(
        span=span,
        rise=rise,
        panels=panels,
        top_section=top_section,
        bottom_section=bottom_section,
        web_section=web_section,
        arch_chord=arch_chord,
    )
    return {"status": "ok", **summary}


def tool_create_cylindrical_tank(
    self,
    radius: float = 2.5,
    height: float = 5.0,
    segments: int = 16,
    ring_levels: int = 2,
    section_vertical: str = None,
    section_ring: str = None,
) -> dict:
    self._ensure_robot()
    summary = self.robot.create_cylindrical_tank(
        radius=radius,
        height=height,
        segments=segments,
        ring_levels=ring_levels,
        section_vertical=section_vertical,
        section_ring=section_ring,
    )
    return {"status": "ok", **summary}
