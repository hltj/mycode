"""todo_write 工具：在内存中维护待办列表，不做持久化。"""

import json
from typing import Annotated, Iterable, Literal, TypedDict, get_args

from mycode import config
from mycode.tools_registry import ToolsRegistry

# 模块级内存状态。测试可通过 ``reset_todos()`` 重置。
_todo_state: list[dict] = []

# 陈旧度提醒：自上次 todo_write 以来的"assistant 消息数"。
# 超过阈值且存在未完成待办时，agent_loop 会往 messages 注入提醒。
# 阈值可通过配置项 ``todo_stale_threshold`` 覆盖，默认 5。
_STALE_THRESHOLD: int = config.get_int("todo_stale_threshold", 5)
_stale_rounds: int = 0

# 同时处于进行中的待办上限。可通过配置项
# ``todo_max_in_progress`` 覆盖，默认 3。
_MAX_IN_PROGRESS: int = config.get_int("todo_max_in_progress", 3)

Status = Literal["pending", "in_progress", "completed"]
# status 的合法取值（从 Literal 推导，单一来源）
VALID_STATUS = get_args(Status)


class TodoItem(TypedDict):
    """单个待办项。"""

    title: Annotated[str, "待办标题，非空字符串"]
    status: Annotated[Status, "状态：待处理 / 进行中 / 已完成"]


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


def _parse_todo_item(item: object, index: int) -> dict:
    """校验并规范化单个待办项（纯函数）。

    Args:
        item: 待办项原始数据（模型传来的 dict）。
        index: 在 items 中的下标（用于错误定位）。

    Returns:
        {"title": str, "status": str}。

    Raises:
        _TodoItemError: 校验失败，message 即错误文本。
    """
    if not isinstance(item, dict):
        raise _TodoItemError(f"Error: 第 {index} 项不是 dict")
    title = item.get("title")
    status = item.get("status")
    if not isinstance(title, str) or not title:
        raise _TodoItemError(f"Error: 第 {index} 项 title 必须是非空字符串")
    if status not in VALID_STATUS:
        raise _TodoItemError(
            f"Error: 第 {index} 项 status 必须是 {VALID_STATUS} 之一，实际为 {status!r}")
    return {"title": title, "status": status}


class _TodoItemError(ValueError):
    """待办项校验失败；message 即返回给模型的错误文本。"""


@ToolsRegistry.tool(
    description=(
        "整体替换内存中的待办列表"
        f"（进行中的项最多同时 {_MAX_IN_PROGRESS} 个）。"
        "状态仅保存在内存，不持久化到磁盘，会话恢复时由历史工具调用重建。"
    )
)
def todo_write(
    items: Annotated[list[TodoItem], "待办项列表"],
) -> str:
    """整体替换内存待办列表，返回结果文本。"""
    # 运行时防御：复杂参数虽在 schema 层有约束，但模型可能传非 list / 非 dict /
    # 非法 status，逐项校验并规范化。
    if not isinstance(items, list):
        return f"Error: items 必须是 list，实际为 {type(items).__name__}"
    try:
        new_state = [_parse_todo_item(it, i) for i, it in enumerate(items)]
    except _TodoItemError as e:
        return str(e)

    # 校验：状态为进行中的项数不超过上限
    # （上限由环境变量 MYCODE_TODO_MAX_IN_PROGRESS 配置，默认 3）
    in_progress_count = sum(1 for it in new_state if it["status"] == "in_progress")
    if in_progress_count > _MAX_IN_PROGRESS:
        return f"Error: 最多同时 {_MAX_IN_PROGRESS} 项进行中，实际 {in_progress_count} 项"

    _todo_state.clear()
    _todo_state.extend(new_state)
    # 写入成功即视为"已更新进度"，清零陈旧度计数。
    reset_stale_rounds()

    n = len(new_state)
    return f"TODO 列表已更新（{n} 项）" if n else "TODO 列表已清空"


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
    "TodoItem",
    "Status",
]
