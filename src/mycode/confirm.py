"""
确认交互界面模块。

对一次工具调用弹出确认界面：调用方拿到 ``ConfirmAction`` 枚举与可选
附加文本（拒绝理由 / 编辑后命令）。

具体交互界面（询问与选项选择）由 ``mycode.ask_ui`` 提供，本模块只负责
将 ask_ui 的结果映射为 ``ConfirmAction``，并提供结果文本的格式化辅助。

公开 API：
    - ``ConfirmAction``：动作枚举。
    - ``confirm_tool``：确认入口；返回 ``(ConfirmAction, Optional[str])``。
    - ``format_reject`` / ``format_reject_no_reason`` / ``format_cancel``：
      与 ``ConfirmAction`` 对应的结果文本构造。
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

from mycode import ask_ui as _ask_ui_mod
from mycode.ask_ui import AskOption
from mycode.mode import ToolCategory, is_bash_tool
from mycode.renderer import _get_renderer


class ConfirmAction(str, Enum):
    APPROVE = "approve"                # 同意执行
    REJECT = "reject"                  # 拒绝（带理由）
    REJECT_NO_REASON = "reject_plain"  # 无理由拒绝 → 跳出 agent 循环
    EDIT = "edit"                      # 编辑后执行（bash）
    CANCEL = "cancel"                  # 取消 → 跳出 agent 循环


# ===================================================================
# 编辑视图（独立的多行命令编辑，与 ask_ui 解耦）
# ===================================================================

class _EditOutcome(NamedTuple):
    """编辑视图退出结果。"""
    action: str        # "finish" | "back" | "abort"
    text: str = ""


def _run_edit_view(
    edit_buffer: Buffer,
    input=None,
    output=None,
    style=None,
) -> _EditOutcome:
    """运行多行命令编辑视图。

    Args:
        edit_buffer: 已存在的多行 buffer（持有文本与光标位置；调用方
            在 ESC 返回时保留 buffer 状态以便再次进入时不丢失编辑）。
        input/output: prompt_toolkit 的可注入 input/output（测试用）。
        style: prompt_toolkit ``Style``（通常 ``renderer.create_prompt_style()``），
            让 ``class:mycode-input`` 等样式类生效。

    Returns:
        ``_EditOutcome``：
            - ``action="finish"`` + ``text``：Alt+Enter 提交编辑。
            - ``action="back"``：按 ESC，请求回到确认菜单。
            - ``action="abort"``：Ctrl-C 中止整个确认流程。
    """
    kb = KeyBindings()
    outcome = _EditOutcome(action="abort")

    @kb.add("escape", "enter")
    def _alt_enter(event):
        nonlocal outcome
        outcome = _EditOutcome(action="finish", text=edit_buffer.text)
        event.app.exit()

    @kb.add("escape")
    def _escape(event):
        nonlocal outcome
        outcome = _EditOutcome(action="back")
        event.app.exit()

    @kb.add("c-c")
    def _abort(event):
        nonlocal outcome
        outcome = _EditOutcome(action="abort")
        event.app.exit()

    @kb.add("<any>")
    def _on_char(event):
        event.app.current_buffer.insert_text(event.data or "")

    # style="class:mycode-input"：与 cli 提示词输入区共用样式类，
    # default 风格下有灰色背景（与 ask_ui 自定义输入框保持视觉一致），
    # classic 风格为空（保持原风格）。
    layout = VSplit([
        Window(
            content=FormattedTextControl("编辑 >> "),
            height=1,
            dont_extend_width=True,
        ),
        Window(
            content=BufferControl(buffer=edit_buffer),
            height=lambda: max(1, min(10, edit_buffer.document.line_count)),
        ),
    ], style="class:mycode-input")
    app: Application = Application(
        layout=Layout(layout),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        style=style,
        input=input,
        output=output,
    )
    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        return _EditOutcome(action="abort")

    return outcome


# ===================================================================
# 公开入口
# ===================================================================

def _build_confirm_options(show_edit: bool) -> list[AskOption]:
    """构造 ask_ui 选项列表：同意 / [编辑] / 拒绝（拒绝为 is_custom）。"""
    opts: list[AskOption] = [
        AskOption(label="同意", value=ConfirmAction.APPROVE.value),
    ]
    if show_edit:
        opts.append(AskOption(label="编辑 >>", value=ConfirmAction.EDIT.value))
    opts.append(AskOption(
        label="拒绝",
        value=ConfirmAction.REJECT.value,
        description="拒绝理由",
        is_custom=True,
    ))
    return opts


def confirm_tool(
    func_name: str,
    category: ToolCategory,
    command: str | None,
    input=None,
    output=None,
) -> tuple[ConfirmAction, Optional[str]]:
    """对一次工具调用弹出确认界面。

    返回 ``(动作, 附加文本)``：

    - ``APPROVE``         → ``(ConfirmAction.APPROVE, None)``
    - ``REJECT``          → ``(ConfirmAction.REJECT, reason)``
    - ``REJECT_NO_REASON``→ ``(ConfirmAction.REJECT_NO_REASON, None)``
    - ``EDIT``            → ``(ConfirmAction.EDIT, edited_command)``
    - ``CANCEL``          → ``(ConfirmAction.CANCEL, None)``

    进入编辑视图后：
    - Alt+Enter → 提交编辑（``EDIT``）。
    - ESC      → 返回确认菜单（重新询问，不计为取消）。编辑 buffer
                 与拒绝理由 buffer 的文本 / 光标位置、当前选项焦点
                 在返回时全部保留。
    - Ctrl-C   → 取消整个确认流程（``CANCEL``）。
    """
    show_edit = is_bash_tool(category)
    options = _build_confirm_options(show_edit)
    initial_command = command or ""
    # 与 cli 提示词输入框共用样式表（让 class:placeholder / class:mycode-input
    # 等样式类生效）
    style = _get_renderer().create_prompt_style()

    # 持久 buffer：拒绝理由 + 编辑命令。跨 ask_ui / _run_edit_view 调用
    # 保留文本与光标位置，ESC 返回后用户输入与变更不丢失。
    ask_buffer = Buffer()
    edit_buffer = Buffer(multiline=True)
    edit_buffer.text = initial_command
    edit_buffer.cursor_position = len(edit_buffer.text)

    # 持久 ask 状态：ESC 从编辑视图返回时，焦点回到原选项（而非重置到第一项）。
    cursor_index = 0
    checked: set[int] = set()

    while True:
        result = _ask_ui_mod.ask_ui(
            options=options,
            custom_buffer=ask_buffer,
            cursor_index=cursor_index,
            checked=checked,
            style=style,
            input=input,
            output=output,
        )
        # 维持 ask 状态：把提交时的焦点 / 勾选记下，下次调用回传
        cursor_index = int(result["cursor_index"])
        checked = set(result["checked"])

        selected = list(result["selected"])
        custom_input = result["input"]

        # 自定义输入（拒绝）分支：无理由 vs 有理由
        if ConfirmAction.REJECT.value in selected:
            reason = (custom_input or "").strip()
            if reason:
                return (ConfirmAction.REJECT, reason)
            return (ConfirmAction.REJECT_NO_REASON, None)

        if ConfirmAction.APPROVE.value in selected:
            return (ConfirmAction.APPROVE, None)

        if ConfirmAction.EDIT.value in selected:
            # 仅 bash 工具会出现 EDIT 选项；编辑视图可能反复进入（ESC 返回）
            outcome = _run_edit_view(
                edit_buffer,
                input=input,
                output=output,
                style=style,
            )
            if outcome.action == "finish":
                return (ConfirmAction.EDIT, outcome.text)
            if outcome.action == "abort":
                return (ConfirmAction.CANCEL, None)
            # action == "back"：重新询问，edit_buffer / cursor_index / checked 保留
            continue

        # abort / 空选择 / Ctrl-C → 取消
        return (ConfirmAction.CANCEL, None)


# ===================================================================
# 结果文本格式化
# ===================================================================

def format_reject(reason: str) -> str:
    """构造带理由拒绝执行的结果文本。"""
    return f"Error: 用户拒绝执行：{reason}"


def format_reject_no_reason() -> str:
    """构造无理由拒绝执行的结果文本。"""
    return "Error: 用户拒绝执行，未提供理由"


def format_cancel() -> str:
    """构造取消操作的结果文本。"""
    return "Error: 用户取消操作"


__all__ = [
    "ConfirmAction",
    "confirm_tool",
    "format_reject",
    "format_reject_no_reason",
    "format_cancel",
]