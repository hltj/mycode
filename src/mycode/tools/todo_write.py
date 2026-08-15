#!/usr/bin/env python3
"""todo_write 工具：在内存中维护待办列表，不做持久化。"""

import json
import os
from typing import Annotated, Iterable

from mycode.tools_registry import ToolsRegistry

# 模块级内存状态。测试可通过 ``reset_todos()`` 重置。
_todo_state: list[dict] = []

# 陈旧度提醒：自上次 todo_write 以来的"assistant 消息数"。
# 超过阈值且存在未完成待办时，agent_loop 会往 messages 注入提醒。
# 阈值可通过环境变量 ``MYCODE_STALE_THRESHOLD`` 覆盖，默认 5。
_STALE_THRESHOLD: int = int(os.getenv("MYCODE_STALE_THRESHOLD", "5"))
_stale_rounds: int = 0

VALID_STATUS = ("pending", "in_progress", "completed")


def reset_todos() -> None:
    """重置内存待办状态（供测试使用）。"""
    _todo_state.clear()


def get_todos() -> list[dict]:
    """读取当前待办状态（供测试使用）。"""
    return list(_todo_state)


# ---------------------------------------------------------------------------
# 陈旧度提醒
# ---------------------------------------------------------------------------

def bump_stale_rounds() -> None:
    """每产生一个 assistant 消息，自增 1（agent_loop 顶部调用）。"""
    global _stale_rounds
    _stale_rounds += 1


def reset_stale_rounds() -> None:
    """清零陈旧度计数（todo_write 成功后调用，或注入提醒后调用）。"""
    global _stale_rounds
    _stale_rounds = 0


def get_stale_rounds() -> int:
    """读取当前陈旧度计数（供测试与排错使用）。"""
    return _stale_rounds


def get_unfinished_todos() -> list[dict]:
    """返回未完成的待办（status 为 pending 或 in_progress）。"""
    return [it for it in _todo_state if it.get("status") in ("pending", "in_progress")]


def should_remind_stale_todo() -> bool:
    """是否应触发陈旧待办提醒。"""
    return bool(get_unfinished_todos()) and _stale_rounds >= _STALE_THRESHOLD


def format_stale_reminder() -> str:
    return "有未完成的 todo 最近未更新，请使用 todo_write 工具更新状态。"


def rebuild_from_history(entries: Iterable) -> None:
    """从会话历史重建待办状态。

    遍历 entries 中的所有 ``ToolCallEvent``，按时间顺序 replay
    名为 ``todo_write`` 的调用。每次 todo_write 整体替换状态，
    所以最终状态由最后一次成功调用决定。格式异常的调用被跳过，
    不影响后续 replay。
    """
    reset_todos()
    for entry in entries:
        # 仅依赖 ``tool_call`` 属性 + ``function.name``，避免与 session 模块互相 import
        tool_call = getattr(entry, "tool_call", None)
        if not isinstance(tool_call, dict):
            continue
        func = tool_call.get("function")
        if not isinstance(func, dict) or func.get("name") != "todo_write":
            continue
        try:
            args = json.loads(func.get("arguments", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = args.get("items") if isinstance(args, dict) else None
        if not isinstance(items, list):
            continue
        # 复用 todo_write 本体（含校验 + 状态写入）
        try:
            todo_write(items)
        except Exception:
            continue


@ToolsRegistry.tool(
    description=(
        "整体替换内存中的待办列表。items 是 dict 数组，每个 dict 含"
        " title (str) 与 status (str，pending/in_progress/completed 之一；"
        "同时只能有一项 in_progress)。状态仅保存在内存，不持久化到磁盘，"
        "会话恢复时由历史工具调用重建。"
    )
)
def todo_write(
    items: Annotated[list[dict], "待办项列表，每项含 title 和 status"],
) -> str:
    if not isinstance(items, list):
        return f"Error: items 必须是 list，实际为 {type(items).__name__}"

    new_state: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return f"Error: 第 {i} 项不是 dict"
        title = item.get("title")
        status = item.get("status")
        if not isinstance(title, str) or not title:
            return f"Error: 第 {i} 项 title 必须是非空字符串"
        if status not in VALID_STATUS:
            return f"Error: 第 {i} 项 status 必须是 {VALID_STATUS} 之一，实际为 {status!r}"
        new_state.append({"title": title, "status": status})

    # 校验：status 为 in_progress 应有且仅有一项
    in_progress_count = sum(1 for it in new_state if it["status"] == "in_progress")
    if in_progress_count > 1:
        return f"Error: 同时只能有一项 in_progress，实际 {in_progress_count} 项"

    _todo_state.clear()
    _todo_state.extend(new_state)
    # 写入成功即视为"已更新进度"，清零陈旧度计数。
    reset_stale_rounds()

    n = len(new_state)
    if n == 0:
        return "TODO 列表已清空"
    return f"TODO 列表已更新（{n} 项）"


__all__ = [
    "todo_write",
    "reset_todos",
    "get_todos",
    "rebuild_from_history",
    "bump_stale_rounds",
    "reset_stale_rounds",
    "get_stale_rounds",
    "get_unfinished_todos",
    "should_remind_stale_todo",
    "format_stale_reminder",
]
