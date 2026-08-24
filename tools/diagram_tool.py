"""
tools/diagram_tool.py
======================
Structural diagram plotting engine — renders Shear Force Diagrams (SFD) and
Bending Moment Diagrams (BMD) from a member-forces DataFrame, per-bar,
stacked into a single labeled figure suitable for embedding in a Word
calculation report.

Requires: matplotlib, pandas (`pip install matplotlib pandas`)
"""

from __future__ import annotations

import logging
import os

import pandas as pd


def _ensure_matplotlib():
    """Lazily imports matplotlib with the headless Agg backend.

    Deferred so that importing app.py (and its tool chain via
    agent/tool_registry.py) does not pay matplotlib's non-trivial import
    cost unless diagrams are actually rendered. This significantly shortens
    the first page load.
    """
    import matplotlib

    matplotlib.use("Agg")  # headless-safe backend for server / Streamlit contexts
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    return plt, mticker


logger = logging.getLogger("structural_copilot.diagram_tool")
logger.setLevel(logging.INFO)


def plot_structure_wireframe(
    nodes,
    bars,
    save_path: str,
    title: str = "Structure geometry",
) -> str:
    """Renders a simple node/bar wireframe of the current model.

    ``nodes``: dict {id: [x, y, z]} (or list of {id, x, y, z}) and
    ``bars``: dict {id: [n1, n2]} — the shape RobotBridge.get_model_geometry
    returns. Planar models (all y ~ 0) are drawn in the dominant X-Z plane;
    3D models get an axonometric view with equal axis aspect. Returns the
    saved path. Pure function (no Robot COM); matplotlib is imported lazily.
    """
    plt, _ = _ensure_matplotlib()

    if isinstance(nodes, dict):
        coords = {int(k): [float(v[0]), float(v[1]), float(v[2])] for k, v in nodes.items()}
    else:
        coords = {
            int(n["id"]): [float(n.get("x", 0.0)), float(n.get("y", 0.0)), float(n.get("z", 0.0))]
            for n in nodes
        }
    if not coords:
        raise ValueError("No nodes to preview - build geometry first.")
    if isinstance(bars, dict):
        pairs = {int(k): (int(v[0]), int(v[1])) for k, v in bars.items()}
    else:
        pairs = {int(b["id"]): (int(b["n1"]), int(b["n2"])) for b in bars}

    all_flat = all(abs(c[1]) < 1e-9 for c in coords.values())
    fig = plt.figure(figsize=(9, 6))
    if all_flat:
        ax = fig.add_subplot(111)
        xs = [c[0] for c in coords.values()]
        zs = [c[2] for c in coords.values()]
        for n1, n2 in pairs.values():
            if n1 in coords and n2 in coords:
                ax.plot(
                    [coords[n1][0], coords[n2][0]],
                    [coords[n1][2], coords[n2][2]],
                    color="#1F618D",
                    lw=1.6,
                )
        ax.scatter(xs, zs, color="#C0392B", s=24, zorder=3)
        for nid, c in coords.items():
            ax.annotate(
                str(nid),
                (c[0], c[2]),
                textcoords="offset points",
                xytext=(4, 4),
                fontsize=7,
                color="#444444",
            )
        ax.set_xlabel("X (m)")
        ax.set_ylabel("Z (m)")
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.set_aspect("equal", adjustable="datalim")
    else:
        ax = fig.add_subplot(111, projection="3d")
        xs = [c[0] for c in coords.values()]
        ys = [c[1] for c in coords.values()]
        zs = [c[2] for c in coords.values()]
        for n1, n2 in pairs.values():
            if n1 in coords and n2 in coords:
                ax.plot(
                    [coords[n1][0], coords[n2][0]],
                    [coords[n1][1], coords[n2][1]],
                    [coords[n1][2], coords[n2][2]],
                    color="#1F618D",
                    lw=1.6,
                )
        ax.scatter(xs, ys, zs, color="#C0392B", s=24, zorder=3)
        ax.set_xlabel("X (m)")
        ax.set_ylabel("Y (m)")
        ax.set_zlabel("Z (m)")
        ax.set_title(title)
        ax.set_box_aspect((1, 1, 1))
    fig.tight_layout()
    fig.savefig(save_path, dpi=150)
    plt.close(fig)
    return save_path


class DiagramGenerator:
    """Generates SFD / BMD figures from Robot member-force export DataFrames."""

    def __init__(self, dpi: int = 160, figsize=(11, 4.5)):
        self.dpi = dpi
        self.figsize = figsize

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def plot_bmd(self, member_forces_df: pd.DataFrame, save_path: str) -> str:
        """Renders bending moment diagrams (MY_kNm vs Position_m) per bar."""
        return self._plot_envelope(
            df=member_forces_df,
            value_col="MY_kNm",
            title="Bending Moment Diagram (BMD)",
            ylabel="Moment, M (kN·m)",
            save_path=save_path,
            fill_color="#C0392B",
            invert_fill=True,  # convention: sagging moment plotted below axis
        )

    def plot_sfd(self, member_forces_df: pd.DataFrame, save_path: str) -> str:
        """Renders shear force diagrams (FZ_kN vs Position_m) per bar."""
        return self._plot_envelope(
            df=member_forces_df,
            value_col="FZ_kN",
            title="Shear Force Diagram (SFD)",
            ylabel="Shear, V (kN)",
            save_path=save_path,
            fill_color="#1F618D",
            invert_fill=False,
        )

    # ------------------------------------------------------------------ #
    # Internal rendering logic
    # ------------------------------------------------------------------ #

    def _plot_envelope(
        self,
        df: pd.DataFrame,
        value_col: str,
        title: str,
        ylabel: str,
        save_path: str,
        fill_color: str,
        invert_fill: bool = False,
    ) -> str:
        plt, mticker = _ensure_matplotlib()  # lazy import (faster app load)
        if df is None or df.empty:
            logger.warning("Empty member-forces DataFrame passed to diagram generator.")
            df = pd.DataFrame(columns=["Bar_ID", "Position_m", value_col])

        # [FIX M12] Validate that required columns exist
        required_cols = {"Bar_ID", "Position_m", value_col}
        if not required_cols.issubset(set(df.columns)):
            missing = required_cols - set(df.columns)
            logger.error(
                "DataFrame missing required columns for diagram: %s. Available: %s",
                missing,
                list(df.columns),
            )
            # Create empty placeholder figure
            fig, ax = plt.subplots(figsize=self.figsize)
            ax.text(
                0.5,
                0.5,
                f"Missing data columns: {missing}",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontsize=12,
                color="red",
            )
            ax.set_title(title, fontsize=14, fontweight="bold", color="#1F3864")
            os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
            fig.savefig(save_path, dpi=self.dpi, bbox_inches="tight")
            plt.close(fig)
            return save_path

        bar_ids = sorted(df["Bar_ID"].unique()) if not df.empty else []
        n_bars = max(len(bar_ids), 1)

        fig, axes = plt.subplots(
            nrows=1,
            ncols=n_bars,
            figsize=(self.figsize[0], self.figsize[1]),
            sharey=True,
            squeeze=False,
        )
        axes = axes[0]

        fig.suptitle(title, fontsize=14, fontweight="bold", color="#1F3864")

        global_max = df[value_col].abs().max() if not df.empty else 1.0
        if pd.isna(global_max) or global_max == 0:
            global_max = 1.0
        y_pad = global_max * 0.20

        for idx, bar_id in enumerate(bar_ids):
            ax = axes[idx]
            bar_df = df[df["Bar_ID"] == bar_id].sort_values("Position_m")
            x = bar_df["Position_m"].values
            y = bar_df[value_col].values
            plot_y = -y if invert_fill else y

            ax.plot(x, plot_y, color=fill_color, linewidth=1.8)
            ax.fill_between(x, plot_y, 0, color=fill_color, alpha=0.25)
            ax.axhline(0, color="black", linewidth=0.8)

            # Annotate max/min governing values along this bar
            if len(plot_y) > 0:
                max_idx = plot_y.argmax()
                min_idx = plot_y.argmin()
                ax.annotate(
                    f"{plot_y[max_idx]:.2f}",
                    xy=(x[max_idx], plot_y[max_idx]),
                    xytext=(0, 6),
                    textcoords="offset points",
                    ha="center",
                    fontsize=7.5,
                    color="#1F3864",
                    fontweight="bold",
                )
                if min_idx != max_idx:
                    ax.annotate(
                        f"{plot_y[min_idx]:.2f}",
                        xy=(x[min_idx], plot_y[min_idx]),
                        xytext=(0, -12),
                        textcoords="offset points",
                        ha="center",
                        fontsize=7.5,
                        color="#1F3864",
                        fontweight="bold",
                    )

            ax.set_title(f"Bar {bar_id}", fontsize=10, fontweight="bold")
            ax.set_xlabel("Position (m)", fontsize=9)
            ax.grid(True, linestyle="--", linewidth=0.4, alpha=0.6)
            ax.set_ylim(-(global_max + y_pad), (global_max + y_pad))
            ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=6))
            ax.tick_params(labelsize=8)

        axes[0].set_ylabel(ylabel, fontsize=10)

        fig.tight_layout(rect=[0, 0, 1, 0.93])

        os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
        fig.savefig(save_path, dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)

        logger.info("Saved %s to %s", title, save_path)
        return save_path

    def plot_both(
        self,
        member_forces_df: pd.DataFrame,
        sfd_path: str,
        bmd_path: str,
    ) -> tuple[str, str]:
        """Convenience helper: renders both diagrams in one call."""
        sfd = self.plot_sfd(member_forces_df, sfd_path)
        bmd = self.plot_bmd(member_forces_df, bmd_path)
        return sfd, bmd
