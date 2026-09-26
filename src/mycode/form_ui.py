"""
通用表单界面模块。

与具体业务解耦的多字段表单输入界面（基于 prompt_toolkit 独立
``Application``），用于供应商变量配置（``/provider`` 的 env 填写、
自定义供应商表单）等场景复用。

设计概要（详见 ``docs/dev/form_ui_design.md``）：

- 字段模型 ``FormField``：name / label / initial / placeholder /
  password / required / hint / validator。
- 布局：可选标题（``ask-title``）→ 可选描述（markdown 渲染，复用
  ``ask_ui`` 的描述逻辑）→ 空行 → 字段行（相邻字段之间空行分隔）→
  空行 → 错误行（``ask-unanswered``）→ 提示行（``ask-description``）。
- 交互：``↑``/``↓``/``C-p``/``C-n`` 环形切换字段；``Enter`` 非末字段
  跳下一字段、末字段校验并提交；``Alt-Enter``（``ESC`` ``Enter`` 序列）
  任意位置直接提交；``Ctrl-C`` 取消。
- 校验：required 空值与 validator 返回的错误串都会阻止提交，首个错误
  展示在错误行、焦点跳到出错字段。
- 返回 ``FormResult``：``values``（{name: 输入值}；取消为空 dict）、
  ``aborted``、``focus_index``（回传恢复焦点用）。
- 状态持久化：``FormField.buffer`` 可注入既有 ``Buffer``（复用时文本
  与光标跨调用保留）；``focus_index`` 入参/返回。

可注入 ``input`` / ``output``（用于测试中驱动按键序列）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, cast

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import Container
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import (
    AfterInput,
    ConditionalProcessor,
    PasswordProcessor,
)

from mycode import renderer as _renderer_mod
from mycode.ask_ui import _description_fragments

# 复用 renderer 已登记的样式类（见 ask_ui / form_ui 设计文档「样式」节）
_STYLE_TITLE = "class:ask-title"
_STYLE_DESCRIPTION = "class:ask-description"
_STYLE_ACTIVE = "class:ask-active"
_STYLE_UNANSWERED = "class:ask-unanswered"
_STYLE_PLACEHOLDER = "class:placeholder"
_STYLE_INPUT = "class:mycode-input"

# 提示行文案（与 cli 输入框 placeholder「↵ 换行，Alt-↵（ESC ↵）发送」风格一致）
_HINT_LINE = "↑↓ 切换字段 · ↵ 下一字段/提交 · Alt-↵（ESC ↵）提交 · Ctrl-C 取消"


@dataclass
class FormField:
    """表单字段数据。

    Attributes:
        name: 字段标识（返回值 dict 的键）。
        label: 展示标签。
        initial: 初始值（回填现值用）；注入 ``buffer`` 时以 buffer 为准。
        placeholder: 占位文本（空值时灰显）。
        password: True 时密码式掩码显示。
        required: True 时空值不允许提交。
        hint: 补充说明（随 label 展示，暗灰）。
        validator: 校验函数：返回 None 通过；返回字符串为错误提示
            （阻止提交）。required 检查先于 validator。
        buffer: 可选，注入既有 Buffer（多次调用复用时文本与光标保留）。
    """

    name: str
    label: str
    initial: str = ""
    placeholder: str = ""
    password: bool = False
    required: bool = False
    hint: str = ""
    validator: Optional[Callable[[str], Optional[str]]] = None
    buffer: Optional[Buffer] = None


@dataclass
class FormResult:
    """表单整体交互结果。

    Attributes:
        values: {字段 name: 输入值}；aborted 时为空 dict。
        aborted: True 表示 Ctrl-C 取消。
        focus_index: 提交/取消时的焦点字段索引（下次调用回传恢复焦点）。
    """

    values: dict[str, str] = field(default_factory=dict)
    aborted: bool = False
    focus_index: int = 0


def _is_alt_enter(seq: list) -> bool:
    """判断按键序列是否为 Alt-Enter（``escape`` + ``enter`` 两步）。"""
    return (
        len(seq) == 2
        and getattr(seq[0], "key", "") == "escape"
        and getattr(seq[1], "key", "") == "enter"
    )


class _FormState:
    """form_ui 内部状态。

    ``fields`` 为字段数组；``buffers`` 与字段一一对应（注入或按 initial
    新建）；``focus`` 为当前焦点字段索引；``error`` 为当前校验错误文本
    （提交被阻止时非空）。
    """

    def __init__(
        self,
        fields: list[FormField],
        title: str = "",
        description: str = "",
    ) -> None:
        if not fields:
            # 空数组防御：构造一个空 label 的占位字段，避免索引越界
            fields = [FormField(name="", label="")]
        self.fields: list[FormField] = list(fields)
        self.title: str = title
        self.description: str = description
        self.focus: int = 0
        self.error: str = ""
        self.finished: bool = False
        self.buffers: list[Buffer] = [
            f.buffer if f.buffer is not None else Buffer()
            for f in self.fields
        ]
        # 注入的 buffer 保留原文本；新建的 buffer 回填 initial
        for f, buf in zip(self.fields, self.buffers):
            if f.buffer is None and f.initial:
                buf.insert_text(f.initial)

    # ---- 当前字段便捷视图 ----
    @property
    def current(self) -> FormField:
        return self.fields[self.focus]

    @property
    def current_buffer(self) -> Buffer:
        return self.buffers[self.focus]

    def validate(self) -> str:
        """校验全部字段：通过返回空串；失败返回首个错误并聚焦出错字段。

        required 检查先于 validator（required 的提示固定为
        「label 不能为空」）；任一字段失败即返回，后续字段不再校验。
        """
        for i, f in enumerate(self.fields):
            value = self.buffers[i].text
            if f.required and not value.strip():
                self.focus = i
                return f"{f.label}不能为空"
            if f.validator is not None:
                err = f.validator(value)
                if err:
                    self.focus = i
                    return err
        return ""


def _field_label_fragments(state: _FormState, idx: int):
    """构造字段行 label 部分（前缀 + label + hint + 冒号）的 fragments。"""
    f = state.fields[idx]
    active = idx == state.focus
    style = _STYLE_ACTIVE if active else ""
    frags = [("❯ " if active else "  ", "")] if _renderer_mod.RENDER_STYLE != "classic" else [("> " if active else "  ", "")]
    # 前缀着色并入首片段：用 (style, prefix) 表达，简化为单 fragment
    prefix = ("❯ " if active else "  ") if _renderer_mod.RENDER_STYLE != "classic" else ("> " if active else "  ")
    frags = [(style, prefix)]
    frags.append((style, f.label))
    if f.hint:
        frags.append((_STYLE_DESCRIPTION, f"（{f.hint}）"))
    frags.append((style, ": "))
    return frags


def _build_field_row(state: _FormState, idx: int) -> VSplit:
    """构造单个字段行：label Window + 输入框 Window 的 VSplit。"""
    f = state.fields[idx]
    active = idx == state.focus
    label_win = Window(
        content=FormattedTextControl(
            _field_label_fragments(state, idx),
            # 当前行可聚焦（成为布局默认焦点），非输入控件不闪现光标
            focusable=active,
            show_cursor=False,
        ),
        height=1,
        dont_extend_width=True,
    )

    # 占位文本：空值时在内容后灰显 placeholder（与 PromptSession 同款机制）
    buf = state.buffers[idx]
    processors: list = []
    if f.password:
        # password 掩码：PasswordProcessor 把字符渲染为 *（与
        # prompt_toolkit 内建 password 输入一致）
        processors.append(PasswordProcessor())
    if f.placeholder:
        def _show() -> bool:
            return not bool(buf.text)

        processors.append(
            ConditionalProcessor(
                AfterInput(text=f" {f.placeholder}", style=_STYLE_PLACEHOLDER),
                filter=Condition(_show),
            )
        )

    input_win = Window(
        content=BufferControl(
            buffer=buf,
            input_processors=processors,
            focusable=active,
        ),
        height=1,
        style=_STYLE_INPUT,
    )
    return VSplit([label_win, input_win])


def _build_hint_line() -> Window:
    """构造底部提示行（暗灰）。"""
    return Window(
        content=FormattedTextControl([(_STYLE_DESCRIPTION, _HINT_LINE)]),
        height=1,
        dont_extend_width=True,
    )


def _build_form_layout(state: _FormState) -> HSplit:
    """构建表单整体布局。

    布局结构：标题（可选）→ 空行 → 描述（可选，仅标题存在时展示）→
    空行 → 字段行（字段间空行分隔，避免输入区连成块）→ 空行 →
    错误/空行 → 提示行。
    """
    rows: list[Container] = []
    if state.title:
        rows.append(Window(
            content=FormattedTextControl([(_STYLE_TITLE, state.title)]),
            height=1,
            dont_extend_width=True,
        ))
        if state.description:
            # 标题与描述都存在时，二者之间空一行（与 ask_ui header 一致）
            rows.append(Window(content=FormattedTextControl(""), height=1))
    if state.description:
        desc_frags, desc_style = _description_fragments(state.description)
        rows.append(Window(
            content=FormattedTextControl(desc_frags),
            style=desc_style,
            wrap_lines=True,
            dont_extend_height=True,
            dont_extend_width=True,
        ))
    if state.title or state.description:
        # header 区（标题/描述）与字段区之间空一行
        rows.append(Window(content=FormattedTextControl(""), height=1))

    # 字段行：相邻字段之间插入空行（输入区上下留白，不连成块）
    for i in range(len(state.fields)):
        if i:
            rows.append(Window(content=FormattedTextControl(""), height=1))
        rows.append(_build_field_row(state, i))

    # 字段区与底部之间空一行
    rows.append(Window(content=FormattedTextControl(""), height=1))
    # 错误行：有错误时亮黄展示，否则空行占位（布局稳定不跳动）
    error_win = Window(
        content=FormattedTextControl(
            [(_STYLE_UNANSWERED, state.error)] if state.error else ""
        ),
        height=1,
        dont_extend_width=True,
    )
    rows.append(error_win)
    rows.append(_build_hint_line())
    return HSplit(rows)


def _field_row_offset(state: _FormState) -> int:
    """返回根 HSplit 中第一个字段行的 child 索引（header 区所占 child 数）。

    字段间空行使字段 i 的 child 下标为 ``offset + 2*i``（每字段一行 +
    其前面的空行），焦点定位见 ``_focused_window``。
    """
    # header：标题 1 +（标题与描述间的空行 1，仅二者都存在）+ 描述 1
    # （各自存在才计）+ 与字段区之间的空行 1
    offset = 0
    if state.title:
        offset += 1
        if state.description:
            offset += 1
    if state.description:
        offset += 1
    if state.title or state.description:
        offset += 1
    return offset


def _focused_window(state: _FormState, rows: list[Container]) -> Window:
    """按当前焦点取字段行（VSplit）的输入框 Window。

    第一个字段在 ``offset``，之后每个字段前有一个空行 child，
    故第 i 个字段位于 ``offset + 2*i``。
    """
    row = rows[_field_row_offset(state) + 2 * state.focus]
    return cast(Window, cast(VSplit, row).children[1])


def form_ui(
    fields: list[FormField],
    *,
    title: str = "",
    description: str = "",
    focus_index: int = 0,
    style=None,
    input=None,
    output=None,
) -> FormResult:
    """运行一次表单界面，返回 ``FormResult``。

    Args:
        fields: 字段数组（每个为 ``FormField``）。空数组防御为单个占位字段。
        title: 可选标题（``ask-title`` 样式）。
        description: 可选描述（展示在标题下方；支持多行，default 风格下
            markdown 渲染，复用 ask_ui 的描述渲染逻辑）。
        focus_index: 初始焦点字段索引（越界回退 0），多次调用间维持焦点。
        style: 可选，prompt_toolkit ``Style`` 实例，由调用方传入，让
            ``class:placeholder`` / ``class:mycode-input`` 等样式类生效。
        input: 可选，注入的 prompt_toolkit input（测试用）。
        output: 可选，注入的 prompt_toolkit output（测试用）。
    """
    state = _FormState(fields, title=title, description=description)
    if 0 <= focus_index < len(state.fields):
        state.focus = focus_index

    result = FormResult(focus_index=state.focus)
    kb = KeyBindings()

    def _rebuild(app) -> None:
        """重建布局并聚焦当前字段的输入框。"""
        app.layout.container = _build_form_layout(state)
        rows = cast(HSplit, app.layout.container).children
        app.layout.focus(_focused_window(state, rows))

    def _try_submit(event) -> None:
        """校验并提交：失败时展示错误并聚焦出错字段（界面不退出）。"""
        err = state.validate()
        if err:
            state.error = err
            result.focus_index = state.focus
            _rebuild(event.app)
            return
        state.finished = True
        result.values = {
            f.name: buf.text for f, buf in zip(state.fields, state.buffers)
        }
        result.focus_index = state.focus
        event.app.exit()

    def _move(event, delta: int) -> None:
        """环形切换字段；移动即清除当前错误提示。"""
        if state.finished:
            return
        n = len(state.fields)
        state.focus = (state.focus + delta) % n
        state.error = ""
        result.focus_index = state.focus
        _rebuild(event.app)

    @kb.add("down")
    @kb.add("c-n")
    def _down(event):
        _move(event, +1)

    @kb.add("up")
    @kb.add("c-p")
    def _up(event):
        _move(event, -1)

    @kb.add("enter")
    def _enter(event):
        """Enter：末字段尝试提交；其余字段跳到下一字段（同向下的移动）。"""
        if state.finished:
            return
        if state.focus >= len(state.fields) - 1:
            _try_submit(event)
            return
        _move(event, +1)

    @kb.add("escape", "enter")
    def _alt_enter(event):
        """Alt-Enter（ESC Enter 序列）：任意位置直接尝试提交。"""
        if state.finished:
            return
        _try_submit(event)

    @kb.add("c-c")
    def _ctrl_c(event):
        if state.finished:
            return
        state.finished = True
        result.aborted = True
        result.focus_index = state.focus
        event.app.exit()

    app: Application = Application(
        layout=Layout(_build_form_layout(state)),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        style=style,
        input=input,
        output=output,
    )
    # 初始焦点：当前字段的输入框
    rows = cast(HSplit, app.layout.container).children
    app.layout.focus(_focused_window(state, rows))

    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        result.aborted = True

    return result
