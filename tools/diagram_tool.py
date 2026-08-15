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
from typing import Optional

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
                "DataFrame missing required columns for diagram: %s. "
                "Available: %s", missing, list(df.columns)
            )
            # Create empty placeholder figure
            fig, ax = plt.subplots(figsize=self.figsize)
            ax.text(0.5, 0.5, f"Missing data columns: {missing}",
                    transform=ax.transAxes, ha="center", va="center",
                    fontsize=12, color="red")
            ax.set_title(title, fontsize=14, fontweight="bold", color="#1F3864")
            os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
            fig.savefig(save_path, dpi=self.dpi, bbox_inches="tight")
            plt.close(fig)
            return save_path

        bar_ids = sorted(df["Bar_ID"].unique()) if not df.empty else []
        n_bars = max(len(bar_ids), 1)

        fig, axes = plt.subplots(
            nrows=1, ncols=n_bars, figsize=(self.figsize[0], self.figsize[1]),
            sharey=True, squeeze=False,
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
                    xytext=(0, 6), textcoords="offset points",
                    ha="center", fontsize=7.5, color="#1F3864", fontweight="bold",
                )
                if min_idx != max_idx:
                    ax.annotate(
                        f"{plot_y[min_idx]:.2f}",
                        xy=(x[min_idx], plot_y[min_idx]),
                        xytext=(0, -12), textcoords="offset points",
                        ha="center", fontsize=7.5, color="#1F3864", fontweight="bold",
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
