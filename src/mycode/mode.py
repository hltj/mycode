"""
模式与权限系统模块。

将工具划分为若干操作类别，并按当前模式（询问 / 自动 / 全权）决定
某次工具调用是否需要人工确认。确认交互界面（同意/编辑/拒绝菜单）
见 ``mycode.confirm``。

工具操作类别：
    - 危险（dangerous）：bash 且命中 BASH_DANGEROUS
    - 注意（caution）：bash 且命中 BASH_CAUTION
    - 未知（unknown）：bash 且未命中上述两类
    - 写（write）：write / edit / patch
    - 读（read）：ls / glob / grep / read
    - 内部（internal）：todo_write
"""

from __future__ import annotations

import os
import re
from enum import Enum


# ===================================================================
# 模式
# ===================================================================

class Mode(str, Enum):
    """三种模式：询问 / 自动 / 全权。"""
    ASK = "ask"    # 询问
    AUTO = "auto"  # 自动（默认）
    YOLO = "yolo"  # 全权

    @property
    def label(self) -> str:
        return {
            Mode.ASK: "询问",
            Mode.AUTO: "自动",
            Mode.YOLO: "全权",
        }[self]


_MODE_ORDER = [Mode.AUTO, Mode.YOLO, Mode.ASK]

# 模式显示色（ANSI 前景 + 粗体）
MODE_COLOR: dict[Mode, str] = {
    Mode.ASK: "\x1B[38;2;0;0;255;1m",    # 蓝
    Mode.AUTO: "\x1B[38;2;0;204;0;1m",   # 绿
    Mode.YOLO: "\x1B[38;2;255;165;0;1m", # 橙
}


class ModeState:
    """模式状态持有者（session 公共字段的内存化）。

    作为模块级单例被 cli / renderer 共享，避免循环 import。
    """
    def __init__(self, mode: Mode = Mode.AUTO) -> None:
        self.mode = mode

    def get(self) -> Mode:
        return self.mode

    def set(self, mode: Mode) -> Mode:
        self.mode = mode
        return self.mode

    def cycle(self) -> Mode:
        """循环切换：自动 → 全权 → 询问 → 自动。"""
        i = _MODE_ORDER.index(self.mode)
        self.mode = _MODE_ORDER[(i + 1) % len(_MODE_ORDER)]
        return self.mode


# 模块级单例（cli / renderer 共用）
MODE_STATE = ModeState()


# ===================================================================
# 工具分类
# ===================================================================

class ToolCategory(str, Enum):
    DANGEROUS = "dangerous"  # 危险
    CAUTION = "caution"      # 注意
    UNKNOWN = "unknown"      # 未知
    WRITE = "write"          # 写
    READ = "read"            # 读
    INTERNAL = "internal"    # 内部


# 非 bash 工具名 → 类别
_TOOL_CATEGORIES: dict[str, ToolCategory] = {
    "write": ToolCategory.WRITE,
    "edit": ToolCategory.WRITE,
    "patch": ToolCategory.WRITE,
    "ls": ToolCategory.READ,
    "glob": ToolCategory.READ,
    "grep": ToolCategory.READ,
    "read": ToolCategory.READ,
    "todo_write": ToolCategory.INTERNAL,
}


def _load_patterns(env_name: str) -> list[str]:
    """读取并切分逗号分隔的正则列表。"""
    raw = os.getenv(env_name, "")
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _load_dangerous_patterns() -> list[str]:
    return _load_patterns("BASH_DANGEROUS")


def _load_caution_patterns() -> list[str]:
    return _load_patterns("BASH_CAUTION")


def _classify_bash(command: str) -> ToolCategory:
    """按 bash 命令字符串分类。"""
    for pat in _load_dangerous_patterns():
        try:
            if re.search(pat, command):
                return ToolCategory.DANGEROUS
        except re.error:
            continue
    for pat in _load_caution_patterns():
        try:
            if re.search(pat, command):
                return ToolCategory.CAUTION
        except re.error:
            continue
    return ToolCategory.UNKNOWN


def classify_tool(func_name: str, args: dict | None) -> ToolCategory:
    """根据工具名与参数分类一次工具调用。"""
    if func_name == "bash":
        command = args.get("command", "") if isinstance(args, dict) else ""
        return _classify_bash(command)
    # 非 bash 工具：按工具名查表
    return _TOOL_CATEGORIES.get(func_name, ToolCategory.UNKNOWN)


def is_bash_tool(category: ToolCategory) -> bool:
    """是否为 bash 工具（危险/注意/未知 均来自 bash）。"""
    return category in (ToolCategory.DANGEROUS, ToolCategory.CAUTION, ToolCategory.UNKNOWN)


def needs_confirmation(mode: Mode, category: ToolCategory) -> bool:
    """判断一次调用在当前模式下是否需要确认。

    危险操作不在此处判定（所有模式一律拒绝）。返回值表示是否弹出确认界面。
    """
    if mode == Mode.ASK:
        # 内部、读取无需确认；写、未知、注意均需确认
        return category in (ToolCategory.WRITE, ToolCategory.UNKNOWN, ToolCategory.CAUTION)
    if mode == Mode.AUTO:
        # 仅注意需确认
        return category == ToolCategory.CAUTION
    if mode == Mode.YOLO:
        return False
    return False


__all__ = [
    "Mode",
    "ModeState",
    "MODE_STATE",
    "MODE_COLOR",
    "ToolCategory",
    "classify_tool",
    "is_bash_tool",
    "needs_confirmation",
]