"""
tools package
=============
Exposes the four automation bridges that make up the Structural Multi-App
Agent's tool surface:

    - robot_tool.RobotBridge      -> Autodesk Robot Structural Analysis COM
    - excel_tool.ExcelReporter    -> Excel workbook generation
    - diagram_tool.DiagramGenerator -> Matplotlib SFD/BMD rendering
    - word_tool.WordReporter      -> Word calculation report generation
"""

from .robot_tool import RobotBridge, get_robot_bridge
from .excel_tool import ExcelReporter
from .diagram_tool import DiagramGenerator
from .word_tool import WordReporter

__all__ = [
    "RobotBridge",
    "get_robot_bridge",
    "ExcelReporter",
    "DiagramGenerator",
    "WordReporter",
]
