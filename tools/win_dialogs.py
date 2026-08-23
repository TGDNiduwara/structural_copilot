"""
tools/win_dialogs.py
====================
Shared Win32 window/dialog primitives + an interactive-safe dialog guard.

Moved verbatim out of batch/headless_driver.py (STEP 1 of the interactive
dialog-safety refactor) so both the batch runner and the interactive
RobotBridge path use the SAME low-level window enumeration / text / click
helpers without copy-paste.

All ctypes imports happen INSIDE each function body so that importing this
module never hard-fails on non-Windows dev/CI machines - the same convention
batch/headless_driver.py already used.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("structural_copilot.win_dialogs")

#: Generic markers that distinguish a modal prompt from benign tool/progress
#: windows when no specific pattern matches.
_DIALOG_MARKERS = ("instabilit", "continue", "warning", "error",
                  "question", "confirm", "do you want")

#: The save-prompt Robot pops when Project.New() discards a project that has
#: results (Interactive=1 path). GUESS vs CONFIRMED: the exact substring and
#: button label below are inferred from the bug report, NOT yet captured live
#: via _window_text() against the real Robot UI - they live in this one dict
#: so a single edit fixes them after live verification.
SAVE_PROMPT_PATTERNS: Dict[str, Dict[str, str]] = {
    "save changes to structure": {"action": "click", "button_text": "No"},
}


# --------------------------------------------------------------------------- #
# Low-level Win32 primitives (moved verbatim from batch/headless_driver.py)
# --------------------------------------------------------------------------- #

def _is_dialog_like(title_lower: str) -> bool:
    return any(m in title_lower for m in _DIALOG_MARKERS)


def _enum_windows(pids) -> List[Tuple[int, str, str]]:
    """Visible top-level windows owned by any of the given PIDs:
    [(hwnd, title, class_name)]. Pure Win32 (ctypes), no COM."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    wproc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    out = []
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value in pids:
                cls = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(hwnd, cls, 128)
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    out.append((hwnd, buf.value, cls.value))
        return True
    user32.EnumWindows(wproc(_cb), 0)
    return out


def _window_text(hwnd) -> str:
    """Title plus all child control texts — the dialog BODY, which holds
    the real message when a dialog's title is just the generic app name
    (as observed: the instability modal's title is "Robot Structural
    Analysis Professional 2027" while its static/button children carry
    "Instability ... Do you want to continue?" / Yes / No)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    parts = []
    n = user32.GetWindowTextLengthW(hwnd)
    if n:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        parts.append(buf.value)
    cproc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _lparam):
        nn = user32.GetWindowTextLengthW(h)
        if nn:
            b = ctypes.create_unicode_buffer(nn + 1)
            user32.GetWindowTextW(h, b, nn + 1)
            parts.append(b.value)
        return True
    user32.EnumChildWindows(hwnd, cproc(_cb), 0)
    return " | ".join(parts)


def _click_button(parent_hwnd, text) -> int:
    """Sends BM_CLICK to the first Button child whose text contains
    `text`. Returns how many buttons were clicked."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    cproc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    found = []
    def _cb(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value.lower() == "button":
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if text and text.lower() in buf.value.lower():
                    found.append(hwnd)
        return True
    user32.EnumChildWindows(parent_hwnd, cproc(_cb), 0)
    for h in found:
        user32.SendMessageW(h, 0x00F5, 0, 0)  # BM_CLICK
    return len(found)


def _robot_pids() -> Set[int]:
    """Returns the set of live robot.exe PIDs (tasklist; no new deps)."""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq robot.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "robot.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


# --------------------------------------------------------------------------- #
# Interactive-safe dialog guard
# --------------------------------------------------------------------------- #

def watch_and_dismiss(pids, patterns, timeout_s: float = 30.0,
                      poll_s: float = 0.25,
                      on_unknown: str = "wait") -> dict:
    """Background-thread-friendly watcher for a BOUNDED window of time.

    Auto-clicks any dialog matching `patterns` (same {substr: {action,
    button_text, benign}} shape as batch.headless_driver.DEFAULT_DIALOG_PATTERNS).
    Returns a dict describing what happened:
      {"outcome": "dismissed" | "dismissed_benign" | "unknown_seen" |
                   "timeout" | "clean",
       "title": str|None, "button": str|None}

    Unlike batch's watcher, on_unknown="wait" (the default, and the ONLY
    mode used by the interactive path) NEVER force-kills the process - an
    unrecognized dialog just gets logged with its full title/body text (for
    adding a new pattern later) and the watcher keeps polling until
    timeout_s elapses, then returns outcome="timeout" so the caller can
    raise a clear, actionable error telling the human to check the visible
    Robot window themselves.

    This is a plain function: the caller starts it on a background thread
    BEFORE invoking the blocking COM call, then joins it afterwards.
    """
    if on_unknown not in ("wait",):
        raise ValueError(
            f"on_unknown must be 'wait' (interactive-safe); got {on_unknown!r}")

    deadline = time.time() + timeout_s
    result: Dict[str, Optional[str]] = {
        "outcome": "clean", "title": None, "button": None}

    while time.time() < deadline:
        try:
            for hwnd, title, cls in _enum_windows(pids):
                if (cls or "").lower() == "robobatrobot97":
                    continue  # Robot main window (class, not title: the
                    # instability modal's own title is just the generic app
                    # name, so class is the safe signal)
                text = _window_text(hwnd)
                low = text.lower()
                if not low:
                    continue
                matched = None
                matched_key = None
                for key, spec in patterns.items():
                    if key in low:
                        matched = spec
                        matched_key = key
                        break
                if matched is not None:
                    bt = str(matched.get("button_text", ""))
                    clicked = _click_button(hwnd, bt)
                    benign = bool(matched.get("benign", False))
                    result["outcome"] = ("dismissed_benign" if benign
                                         else "dismissed")
                    result["title"] = title or text[:80]
                    result["button"] = bt
                    result["matched"] = matched_key
                    if matched_key == "instabilit":
                        result["instability_seen"] = True
                        result["instability_title"] = title or text[:80]
                    result["matched"] = matched_key
                    logger.warning(
                        "watch_and_dismiss %s %r (button=%r, clicked=%s)",
                        "auto-dismissed benign" if benign else "auto-dismissed",
                        result["title"], bt, clicked)
                    # Keep watching: Robot may raise more than one dialog.
                    time.sleep(1.0)
                    continue
                if not _is_dialog_like(low):
                    continue  # benign tool/progress window, ignore
                # UNRECOGNIZED but dialog-like: NEVER kill - just record and
                # keep polling so the caller can tell the human to click it.
                result["outcome"] = "unknown_seen"
                result["title"] = title or text[:80]
                logger.error(
                    "watch_and_dismiss: UNKNOWN dialog %r - NOT killing. "
                    "Human must click it in the visible Robot window. "
                    "Add a pattern for it (body text: %r).",
                    result["title"], text[:200])
        except Exception as exc:  # noqa: BLE001
            logger.debug("watch_and_dismiss poll error: %s", exc)
        time.sleep(poll_s)

    if result["outcome"] == "clean":
        return result  # timeout with no dialog ever seen - the expected case
    return result
