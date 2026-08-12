"""
确认交互界面模块。

包含工具调用确认菜单（同意 / 编辑 / 拒绝）的独立控件与状态管理。
逻辑（动作枚举、确认流程、结果格式化）集中在 mode.py 之外，此处
只负责交互展示。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window, Layout, VSplit
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl

from mycode.mode import ToolCategory, is_bash_tool


class ConfirmAction(str, Enum):
    APPROVE = "approve"              # 同意执行
    REJECT = "reject"                # 拒绝（带理由）
    REJECT_NO_REASON = "reject_plain"  # 无理由拒绝 → 跳出 agent 循环
    EDIT = "edit"                    # 编辑后执行（bash）
    CANCEL = "cancel"                # 取消 → 跳出 agent 循环


class OptionKind(str, Enum):
    """确认菜单选项类型。"""
    APPROVE = "approve"  # 同意
    EDIT = "edit"        # 编辑
    REJECT = "reject"    # 拒绝


class Option:
    """确认菜单选项：类型 + 显示文本（不含序号，序号由渲染统一附加）。"""
    def __init__(self, kind: OptionKind, label: str) -> None:
        self.kind = kind
        self.label = label

    @property
    def is_reject(self) -> bool:
        return self.kind == OptionKind.REJECT


class _ConfirmState:
    """确认菜单状态。"""
    def __init__(self, show_edit: bool) -> None:
        self.show_edit = show_edit
        self.sel = 0
        self.reason = ""
        self.abort = False
        self.editing = False  # 是否处于编辑界面（编辑视图替换确认菜单）

    @property
    def options(self) -> list[Option]:
        """统一选项列表：同意 + [编辑 >>] + 拒绝。"""
        opts: list[Option] = [Option(OptionKind.APPROVE, "同意")]
        if self.show_edit:
            opts.append(Option(OptionKind.EDIT, "编辑 >>"))
        opts.append(Option(OptionKind.REJECT, "拒绝"))
        return opts

    @property
    def max_sel(self) -> int:
        return len(self.options) - 1

    @property
    def is_reject_selected(self) -> bool:
        """当前选中项是否为拒绝。"""
        return self.options[self.sel].is_reject

    def menu_fragments(self) -> list[tuple[str, str]]:
        """用于布局的菜单片段（选项统一编号渲染）。"""
        def line(idx: int, text: str) -> list[tuple[str, str]]:
            mark = "> " if self.sel == idx else "  "
            style = "class:mycode-confirm-active" if self.sel == idx else ""
            return [(style, mark + text + "\n")]
        frags: list[tuple[str, str]] = []
        for idx, opt in enumerate(self.options):
            suffix = "：" if opt.is_reject else ""
            frags += line(idx, f"{idx + 1}. {opt.label}{suffix}")
        return frags


def _line_window(text: str, active: bool) -> Window:
    """单行文本窗口（不横向撑满，便于与输入框紧挨）。"""
    style = "class:mycode-confirm-active" if active else ""
    return Window(
        content=FormattedTextControl([(style, text)]),
        height=1,
        always_hide_cursor=True,
        dont_extend_width=True,
    )


def _build_confirm_layout(state: _ConfirmState, reason_buffer: Buffer):
    """构建确认菜单布局。

    选项统一编号渲染（同意 1、[编辑 >> 2]、拒绝 最后）；拒绝行与理由
    输入框横向并列，输入框紧挨拒绝行后一个空格，仅选中拒绝时显示。
    """
    rows: list = []
    for idx, opt in enumerate(state.options):
        num = idx + 1
        suffix = "：" if opt.is_reject else ""
        text = f"{num}. {opt.label}{suffix}"
        active = state.sel == idx
        mark = "> " if active else "  "
        label = mark + text
        if opt.is_reject:
            if state.is_reject_selected:
                reason_window = Window(
                    content=BufferControl(buffer=reason_buffer),
                    height=1,
                )
                rows.append(VSplit([_line_window(label, True), reason_window]))
            else:
                rows.append(_line_window(label, False))
        else:
            rows.append(_line_window(label, active))
    return HSplit(rows)


def _build_edit_layout(edit_buffer: Buffer):
    """构建编辑界面布局（多行，替换确认菜单）。"""
    prompt = _line_window("编辑 >> ", False)
    edit = Window(
        content=BufferControl(buffer=edit_buffer),
        # 多行编辑：高度随内容自适应，最小 1 行
        height=lambda: max(1, min(10, edit_buffer.document.line_count)),
    )
    return VSplit([prompt, edit])


def _run_confirm_menu(
    state: _ConfirmState,
    command: str | None = None,
    input=None,
    output=None,
    reason_buffer: Buffer | None = None,
) -> tuple[str, str]:
    """运行确认/编辑一体界面，返回 (动作, 文本)。

    动作为：
        "approve"   同意执行
        "reject"    拒绝（text 为理由）
        "reject_plain" 无理由拒绝
        "edit"      编辑（text 为编辑后的命令）
        "back"      编辑返回确认菜单（仅内部状态机使用）
        "abort"     Ctrl-C 中止

    确认菜单与编辑界面在同一个 Application 内切换视图（state.editing），
    编辑时确认菜单被替换，返回时恢复，不会叠加。
    """
    if reason_buffer is None:
        reason_buffer = Buffer()
    edit_buffer = Buffer(multiline=True)
    edit_buffer.text = command or ""
    edit_buffer.cursor_position = len(edit_buffer.text)

    kb = KeyBindings()

    def _view():
        """按状态返回当前视图（确认菜单 或 编辑界面）。"""
        if state.editing:
            return _build_edit_layout(edit_buffer)
        return _build_confirm_layout(state, reason_buffer)

    def _rebuild(app) -> None:
        app.layout.container = _view()
        if state.editing:
            app.layout.focus(edit_buffer)
        elif state.is_reject_selected:
            app.layout.focus(reason_buffer)

    # 单次 run 内完成所有交互：视图切换在 key binding 内部处理，
    # 避免多次 app.run() 在非全屏模式下于同一位置累积渲染旧菜单。
    result: dict = {"action": None, "text": ""}

    def _finish(action: str) -> None:
        result["action"] = action
        if action == "reject":
            result["text"] = reason_buffer.text.strip()
        elif action == "edit":
            result["text"] = edit_buffer.text

    @kb.add("down")
    @kb.add("c-n")
    def _down(event):
        if state.editing:
            # 编辑视图：光标下移一行
            event.app.current_buffer.cursor_down()
        elif state.sel < state.max_sel:
            state.sel += 1
            state.reason = reason_buffer.text
            _rebuild(event.app)

    @kb.add("up")
    @kb.add("c-p")
    def _up(event):
        if state.editing:
            # 编辑视图：光标上移一行
            event.app.current_buffer.cursor_up()
        elif state.sel > 0:
            state.sel -= 1
            state.reason = reason_buffer.text
            _rebuild(event.app)

    @kb.add("enter")
    def _enter(event):
        if state.editing:
            # 编辑视图：Enter 插入换行（多行编辑）
            event.app.current_buffer.insert_text("\n")
            return
        # 按当前选项类型分发
        kind = state.options[state.sel].kind
        if kind == OptionKind.APPROVE:
            _finish("approve")
            event.app.exit()
        elif kind == OptionKind.REJECT:
            _finish("reject" if reason_buffer.text.strip() else "reject_plain")
            event.app.exit()
        else:  # EDIT
            # 进入编辑视图（不退出 run）
            state.editing = True
            _rebuild(event.app)

    @kb.add("escape", "enter")
    def _alt_enter(event):
        # Alt+Enter：编辑视图提交；确认菜单忽略
        if state.editing:
            _finish("edit")
            event.app.exit()

    @kb.add("escape")
    def _escape(event):
        if state.editing:
            # 编辑返回确认菜单（不退出 run）
            state.editing = False
            _rebuild(event.app)

    @kb.add("c-c")
    def _abort(event):
        result["action"] = "abort"
        event.app.exit()

    @kb.add("<any>")
    def _input(event):
        if state.editing or state.is_reject_selected:
            # 编辑/拒绝视图：正常输入字符
            event.app.current_buffer.insert_text(event.data or "")
        else:
            # 其余选项：丢弃输入，避免焦点残留导致输入进入理由缓冲区
            event.app.invalidate()

    app: Application = Application(
        layout=Layout(_view()),
        key_bindings=kb,
        full_screen=False,
        input=input,
        output=output,
    )
    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        return ("abort", "")
    action = result["action"] or "abort"
    return (action, result.get("text", ""))


def confirm_tool(
    func_name: str,
    category: ToolCategory,
    command: str | None,
    input=None,
    output=None,
) -> tuple[ConfirmAction, Optional[str]]:
    """对一次工具调用弹出确认界面。

    返回 (动作, 附加文本)：
        APPROVE         → (ConfirmAction.APPROVE, None)
        REJECT          → (ConfirmAction.REJECT, reason)
        REJECT_NO_REASON→ (ConfirmAction.REJECT_NO_REASON, None)
        EDIT            → (ConfirmAction.EDIT, edited_command)
        CANCEL          → (ConfirmAction.CANCEL, None)
    """
    # 只有 bash 工具需要确认时才有【编辑】
    show_edit = is_bash_tool(category)
    state = _ConfirmState(show_edit)
    action, text = _run_confirm_menu(state, command, input=input, output=output)
    match action:
        case "abort":
            return (ConfirmAction.CANCEL, None)
        case "approve":
            return (ConfirmAction.APPROVE, None)
        case "reject":
            return (ConfirmAction.REJECT, text)
        case "reject_plain":
            return (ConfirmAction.REJECT_NO_REASON, None)
        case "edit":
            return (ConfirmAction.EDIT, text)
        # 兜底
        case _:
            return (ConfirmAction.CANCEL, None)


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