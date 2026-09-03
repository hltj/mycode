"""
通用询问界面模块。

为各工具（包括 confirm）提供交互式问答界面：

- 标题 + 描述（描述支持多行；default 风格下支持 markdown 渲染）
- 普通选项（label / 可选 value / 可选 description）
- 末尾「自定义输入」选项（与选项值一起返回）
- 单选 / 多选支持

返回值::

    ``AskResult`` 数据类，字段：

    - ``selected``：选中项 ``value`` 列表（无 ``value`` 时回退 ``label``）；
      单选长度 1，多选按 options 顺序列出所有勾选项。
    - ``input``：仅当选中了自定义选项时为输入框当前文本（可能为空串），
      其余情况为 ``None``。
    - ``cursor_index``：提交时焦点所在选项索引（供下次调用维持焦点）。
    - ``checked``：多选模式下提交时的勾选集合（供下次调用维持勾选）。
    - ``aborted``：True 表示用户以 Ctrl-C 中止了交互；此时 ``selected``
      为空列表、``input`` 为 ``None``。

调用约定：``options`` 列表中**最后一个**元素建议为 ``is_custom=True``，
ask_ui 自动为其渲染输入框；占位文字取该选项的 ``description``。

可注入 ``input`` / ``output``（用于测试中驱动按键序列）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, cast

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import Container
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.formatted_text.base import StyleAndTextTuples
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor

from mycode import renderer as _renderer_mod


# 选项 / 标题的样式类名（在渲染器 prompt style 中注册）
_STYLE_TITLE = "class:ask-title"
_STYLE_DESCRIPTION = "class:ask-description"
_STYLE_ACTIVE = "class:ask-active"
# 与 cli.py 中 PromptSession 的 placeholder 共用样式类
_STYLE_PLACEHOLDER = "class:placeholder"


@dataclass
class AskOption:
    """ask_ui 选项数据。

    Attributes:
        label: 必填，选项显示标题。
        value: 可选，选项返回值（无 ``value`` 时回退 ``label``）。
        description: 可选描述。
            - 普通选项：展示在 ``label`` 之后。
            - ``is_custom=True``：作为输入框占位文本。
        is_custom: True 表示该选项为「自定义输入」选项，UI 在标签后
            追加输入框；该选项的 ``value`` 与普通选项返回同等处理。
    """

    label: str
    value: Optional[str] = None
    description: Optional[str] = None
    is_custom: bool = False

    def effective_value(self) -> str:
        """返回该选项的取值。"""
        return self.value if self.value is not None else self.label


@dataclass
class AskResult:
    """ask_ui 交互结果。

    Attributes:
        selected: 选中项 value 列表（顺序与 options 一致）。
        input: 自定义输入字符串（仅当选中 ``is_custom`` 选项时有意义）。
        cursor_index: 提交时焦点所在选项索引（用于下次调用维持焦点位置）。
        checked: 多选模式下提交时的勾选集合（用于下次调用维持勾选）。
        aborted: True 表示用户以 Ctrl-C 终止交互。
    """

    selected: list[str] = field(default_factory=list)
    input: Optional[str] = None
    cursor_index: int = 0
    checked: set[int] = field(default_factory=set)
    aborted: bool = False


class _AskState:
    """ask_ui 内部状态。"""

    def __init__(
        self,
        title: str = "",
        description: str = "",
        options: list[AskOption] | None = None,
        multi: bool = False,
        custom_buffer: Buffer | None = None,
        cursor_index: int = 0,
        checked: set[int] | None = None,
    ) -> None:
        self.title = title
        self.description = description
        self.options = list(options or [])
        self.multi = multi
        # 焦点 / 勾选可由调用方注入（用于多次调用间维持状态）
        self.sel: int = cursor_index if options is not None and 0 <= cursor_index < len(self.options) else 0
        self.checked: set[int] = set(checked) if checked is not None else set()
        self.finished: bool = False
        self.custom_buffer = custom_buffer
        # 末尾自定义选项索引（-1 表示无）
        self.custom_idx: int = next(
            (i for i, o in enumerate(self.options) if o.is_custom),
            -1,
        )

    @property
    def custom_active(self) -> bool:
        """自定义输入框是否激活。

        单选：焦点位于自定义选项行即为激活；
        多选：需被空格显式选中（``custom_idx in checked``）才激活。
        """
        if self.custom_idx < 0:
            return False
        if self.multi:
            return self.custom_idx in self.checked
        return self.sel == self.custom_idx

    def custom_focusable(self) -> bool:
        """自定义输入框是否可聚焦（多选需先激活，单选跟随 sel）。"""
        return self.custom_active

    def toggle_custom_active(self) -> None:
        """切换多选自定义输入框的激活态（选中 ⇄ 取消选中）。"""
        if self.custom_idx < 0:
            return
        if self.custom_idx in self.checked:
            self.checked.discard(self.custom_idx)
        else:
            self.checked.add(self.custom_idx)


def _strip_trailing_pad(frags: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """剥离 rich 块级元素（列表/引用/代码块）行尾的 pad 空格。

    rich 为了让块级元素背景色铺满整行，会在 ``soft_wrap`` 渲染下仍给
    行尾补齐空格到终端宽度。ask_ui 描述交给 prompt_toolkit ``wrap_lines``
    折行后，这些 pad 空格会污染宽度测量（短列表项被误判为整宽而折行）。
    逐行清理：换行符之前连续出现的纯空格 fragment 直接丢弃。

    代码块留白行（整行都是带背景的空格，rich 为代码块填充的上下留白）
    全剥后背景会丢失——压成单个带背景的空格（宽度 1 不会造成误折行），
    视觉上留白背景仍在。rich 代码块底部留白也可能是「自带背景样式的
    换行符」片段（``(bg, '\\n')``），同样补一个背景空格。
    """
    out: list[tuple[str, str]] = []
    pending: list[tuple[str, str]] = []
    for style, text in frags:
        if text == "\n":
            # 分支1：行结束——pending 里的空格即为行尾 pad。
            # 若它们带背景（代码块上下留白行），压成单个背景空格保留背景色；
            # 否则整组剥掉。随后清空 pending，换行符本尊写入 out。
            bg_style = next((s for s, _ in pending if s and "bg" in s), None)
            if not bg_style and style and "bg" in style:
                bg_style = style
            if bg_style:
                out.append((bg_style, " "))
            pending = []
            out.append((style, "\n"))
        elif text.strip() == "":
            # 分支2：纯空白片段（空格/制表符）→ 无法立即判断是行中分隔还是
            # 行尾 pad，先暂存 pending，待看到后面片段再裁决去留。
            pending.append((style, text))
        else:
            # 分支3：含非空白内容——说明 pending 里的空格在行中间必须保留，
            # 先把它们落进 out，再追加当前内容片段。
            if pending:
                out.extend(pending)
                pending = []
            out.append((style, text))
    # 收尾：文本以空格结尾（无换行）时 pending 仍有残留——同样规则：
    # 带背景压成单个背景空格（代码块底部留白），否则丢弃（rich 输出末尾空白）。
    if pending:
        bg_style = next((s for s, _ in pending if s and "bg" in s), None)
        if bg_style:
            out.append((bg_style, " "))
    return out


def _description_fragments(text: str) -> tuple[list, str]:
    """把问题描述文本转成 prompt_toolkit 富文本 fragments。

    换行策略：rich 侧用 ``soft_wrap`` **不折行**，真正按终端宽度断行交给
    prompt_toolkit 的 ``wrap_lines``——逐字符按显示宽度折行，中文可任意
    汉字断行。rich 按空白把文本划分为若干块，折行只发生在块间空白处，
    无法在汉字间折行；因此描述窗口必须设置 ``wrap_lines=True``。

    - classic 风格：纯文本 + 暗灰样式，按原文本换行（多行描述）。
    - default 风格：直接交给 rich Markdown ``soft_wrap`` 渲染成富文本
      （加粗 / 内联代码 / 列表 / 代码块等），剥离块级元素行尾 pad 空格
      后解析为 fragments。按标准 markdown 语义：
        - 相邻行（无空行）会被 rich 折叠为同一段落（换行变空格）；
        - 需要显式换行时入参应使用 hard break（行尾两个空格，
          ``line1  \\nline2``），与 ``PromptSession`` / markdown 行为一致；
        - 空行分隔、列表、代码块等结构与普通 markdown 相同。
       ask_ui 不做任何文本改写，换行语义完全由入参的 markdown 决定。

    两种风格都返回 ``_STYLE_DESCRIPTION`` 作为窗口样式：未着色的纯文本
    部分继承暗灰描述色，富文本样式（加粗 / 内联代码 / 代码块等）叠加生效。

    返回 ``(fragments, style)``。markdown 渲染异常时回退纯文本，
    保证界面不因描述格式崩溃。
    """
    if _renderer_mod.RENDER_STYLE == "classic":
        return [(_STYLE_DESCRIPTION, text)], _STYLE_DESCRIPTION
    try:
        ansi = _renderer_mod._markdown_ansi(text, soft_wrap=True)
        frags = list(ANSI(ansi).__pt_formatted_text__())
        # rich 输出末尾带换行（fragments 末尾会多一个空串行），去掉它
        if frags and frags[-1][1] == "\n":
            frags = frags[:-1]
        frags = _strip_trailing_pad(frags)
        return frags, _STYLE_DESCRIPTION
    except Exception:
        return [(_STYLE_DESCRIPTION, text)], _STYLE_DESCRIPTION


def _placeholder_processors(buf: Buffer, placeholder: str | None):
    """为 Buffer 生成动态 placeholder 处理器。

    当 ``buf.text`` 为空时，在内容**之后**追加 ``placeholder`` 文本；
    用 ``AfterInput`` 与 ``PromptSession.placeholder`` 行为一致。

    前导一个空格让光标与 placeholder 之间留出可视距离；样式类复用
    ``class:placeholder``，与 ``PromptSession.placeholder`` 一致。
    """
    if not placeholder:
        return []

    def _show() -> bool:
        return not bool(buf.text)

    return [
        ConditionalProcessor(
            AfterInput(text=f" {placeholder}", style=_STYLE_PLACEHOLDER),
            filter=Condition(_show),
        )
    ]


def _mark_str(multi: bool, active: bool, checked: bool) -> str:
    """计算选项前缀。

    传统风格（classic）：单选当前行 ``> ``、其余 ``  ``；多选在左侧
    加 ``> `` 指示当前行、勾选态用 ``[x] ``/``[ ] ``。

    默认风格：用符号前缀（统一 5 列宽度以对齐标签）——
        - 单选：当前行 ``❯ 🟢 ``，其余 ``  ⚪ ``。
        - 多选：左侧 ``❯ ``（当前行）/``  ``，勾选态 ``✅ ``/``🔳 ``。
    """
    if _renderer_mod.RENDER_STYLE == "classic":
        if multi:
            mark = "> " if active else "  "
            box = "[x] " if checked else "[ ] "
            return mark + box
        return "> " if active else "  "
    # 默认风格：符号前缀
    if multi:
        row = "❯ " if active else "  "
        box = "✅ " if checked else "🔳 "
        return row + box
    return "❯ 🟢 " if active else "  ⚪ "


def _build_option_window(
    state: _AskState,
    idx: int,
    opt: AskOption,
    active: bool,
    custom_buffer: Buffer | None,
):
    """构造单个选项行（普通 / 自定义两种形态）。"""
    checked = idx in state.checked
    mark = _mark_str(state.multi, active, checked)
    style = _STYLE_ACTIVE if active else ""

    label_text = f"{mark}{opt.label}"
    if opt.is_custom:
        label_text += ": "

    fragments: StyleAndTextTuples = [(style, label_text)]
    if opt.description and not opt.is_custom:
        fragments.append((_STYLE_DESCRIPTION, f"  {opt.description}"))
    # 当前行 focusable=True（成为布局默认焦点，取代标题行），但
    # show_cursor=False（普通选项不是输入框，不闪现光标）。
    # 自定义选项的 label 同样可 focus：多选未激活输入框时，焦点落在
    # label 行（无光标）而非无处可去；激活后焦点转到输入框。
    label_ctrl = FormattedTextControl(
        fragments,
        focusable=active,
        show_cursor=False,
    )

    label_win = Window(
        content=label_ctrl,
        height=1,
        dont_extend_width=True,
    )

    if not opt.is_custom or custom_buffer is None:
        return label_win

    # 自定义选项：标签后追加输入框
    # focusable 跟随激活态：多选需按空格显式选中后才激活可输入，避免把
    # 空格当作切换勾选而无法输入；单选跟随 sel（焦点到自定义行即激活）。
    # 未激活时，焦点不落在输入框上（避免 prompt_toolkit 默认聚焦它）。
    # style="class:mycode-input"：与 cli 提示词输入区共用样式类，
    # default 风格下有灰色背景，classic 风格为空（保持原风格）。
    processors = _placeholder_processors(custom_buffer, opt.description)
    input_win = Window(
        content=BufferControl(
            buffer=custom_buffer,
            input_processors=processors,
            focusable=state.custom_focusable(),
        ),
        height=1,
        style="class:mycode-input",
    )
    return VSplit([label_win, input_win])


def _build_ask_layout(
    state: _AskState,
    custom_buffer: Buffer | None,
) -> HSplit:
    """构建询问界面整体布局：标题 → 描述 → 各选项行。

    标题与描述都非空才展示对应行。
    """
    rows: list = []

    # 标题（非空时展示）
    if state.title:
        rows.append(Window(
            content=FormattedTextControl([(_STYLE_TITLE, state.title)]),
            height=1,
            dont_extend_width=True,
        ))

    # 描述（非空时展示）
    # - 问题描述支持多行与 markdown：default 风格经 rich Markdown 渲染成
    #   富文本（加粗 / 内联代码 / 列表 / 代码块等），classic 保留纯文本；
    #   文本里的真实换行按行展开。
    # - wrap_lines=True：宽度断行由 prompt_toolkit 逐字符进行（中文可任意
    #   汉字换行），rich 侧 soft_wrap 不折行，折行不限于块间空白处。
    #   行高由折行结果自动决定（dont_extend_height）。
    # - 描述行高随内容扩展不影响最高层布局的基线（见 _option_row_offset）：
    #   描述无论占几行，在根 HSplit 中都只占一个 child 槽位，焦点行索引
    #   仍按 child 下标计算，不随描述行数漂移。
    if state.description:
        desc_frags, desc_style = _description_fragments(state.description)
        rows.append(Window(
            content=FormattedTextControl(
                desc_frags,
                focusable=False,
            ),
            style=desc_style,
            wrap_lines=True,
            dont_extend_height=True,
            dont_extend_width=True,
        ))

    # 标题 / 描述与选项之间加一个空行（header 区存在时才加）
    if state.title or state.description:
        rows.append(Window(
            content=FormattedTextControl(""),
            height=1,
        ))

    # 选项
    for idx, opt in enumerate(state.options):
        active = idx == state.sel
        rows.append(_build_option_window(state, idx, opt, active, custom_buffer))

    return HSplit(rows)


def _option_row_offset(state: _AskState) -> int:
    """返回根 HSplit 中第一个选项的 child 索引（即 header 所占 child 数）。

    该索引用作焦点定位的下标：``_focused_window`` 以 ``offset + sel``
    取当前选中选项在根 HSplit 中的 child。描述即使多行也只是单个 child，
    故该值不随描述行数变化。
    """
    offset = 0
    if state.title:
        offset += 1
    if state.description:
        offset += 1
    # 有标题或描述时，header 区后有分隔空行
    if state.title or state.description:
        offset += 1
    return offset


def _focused_window(
    state: _AskState,
    rows: list[Container],
    custom_buffer: Buffer | None,
) -> Window:
    """按索引直接取当前选中行的可聚焦 Window。

    rows 是根 HSplit 的 children：前 ``_option_row_offset`` 个为标题/描述，
    之后每个选项占一个元素；自定义选项是 ``VSplit([label, input])``。

    - 自定义行已激活：可聚焦窗口是输入框 ``children[1]``。
    - 自定义行未激活 / 普通选项：可聚焦窗口是 label 行（即行本身，
      对于自定义行是 VSplit 的 ``children[0]``，普通选项则是整行 Window）。
    """
    row = rows[_option_row_offset(state) + state.sel]
    if state.sel == state.custom_idx and state.custom_active and custom_buffer is not None:
        # 自定义选项行已激活：VSplit，输入框在 children[1]
        return cast(Window, cast(VSplit, row).children[1])
    if state.sel == state.custom_idx and custom_buffer is not None:
        # 自定义行未激活：VSplit，聚焦 label（children[0]）
        return cast(Window, cast(VSplit, row).children[0])
    return cast(Window, row)


def _run_ask_ui(
    state: _AskState,
    input=None,
    output=None,
    style=None,
) -> AskResult:
    """运行 ask_ui 交互界面，返回结果。

    当 ``state.custom_buffer`` 由外部传入时复用该 buffer（其文本与光标位置
    在调用间保持）。默认（``None``）时为本次调用新建 buffer。

    ``style`` 由调用方传入，否则 prompt_toolkit 用默认样式，
    class:placeholder / class:mycode-input 等样式表项不会生效。
    """
    if state.custom_idx >= 0:
        custom_buffer: Buffer | None = state.custom_buffer
        if custom_buffer is None:
            custom_buffer = Buffer()
            state.custom_buffer = custom_buffer
    else:
        custom_buffer = None

    result = AskResult()
    kb = KeyBindings()

    def _rebuild(app) -> None:
        """重建布局并更新焦点。

        - 选中自定义选项：焦点落到输入框。
        - 其他选项：焦点落到当前选中的普通选项行（show_cursor=False，
          不闪现光标），避免 prompt_toolkit 默认把焦点落到标题等首行。
        """
        app.layout.container = _build_ask_layout(state, custom_buffer)
        if not state.options:
            return
        # 按索引直接取当前选中行并聚焦（自定义激活则聚焦输入框，
        # 否则聚焦 label 行 / 普通选项行）
        rows = cast(HSplit, app.layout.container).children
        win = _focused_window(state, rows, custom_buffer)
        app.layout.focus(win)

    def _move(event, delta: int) -> None:
        if state.finished or not state.options:
            return
        n = len(state.options)
        state.sel = (state.sel + delta) % n
        _rebuild(event.app)

    def _finish(event) -> None:
        """按当前焦点 / 勾选集合收集结果并退出。

        多选：selected 只含勾选项（无勾选时为空列表）；
        单选：selected 为当前焦点项。
        """
        state.finished = True
        if state.multi:
            sel_idxs = sorted(state.checked)
        else:
            sel_idxs = [state.sel]
        for idx in sel_idxs:
            result.selected.append(state.options[idx].effective_value())
        if state.custom_idx in sel_idxs and custom_buffer is not None:
            result.input = custom_buffer.text
        else:
            result.input = None
        # 记录提交时的焦点 / 勾选，供外部维持状态
        result.cursor_index = state.sel
        result.checked = set(state.checked)
        event.app.exit()

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
        if state.finished:
            return
        _finish(event)

    @kb.add(" ")
    def _space(event):
        """空格键：自定义输入框激活时输入空格，否则按多选/单选分发。

        - 自定义输入框激活（单选或多选均适用）：空格插入输入框。多选下
          激活意味着已选中，单选下激活即焦点在自定义行。
        - 多选 + 未激活：切换勾选（选中自定义行时同时激活其输入框）。
        - 单选 + 未激活（普通选项行）：无操作。
        """
        if state.finished:
            return
        is_custom_row = state.sel == state.custom_idx
        # 自定义输入框已激活：空格作为普通字符输入
        if state.custom_active and is_custom_row and custom_buffer is not None:
            custom_buffer.insert_text(" ")
            event.app.invalidate()
            return
        if not state.multi:
            # 单选 + 非自定义行：空格无实质作用（不做切换勾选）
            event.app.invalidate()
            return
        # 多选：切换勾选
        if state.sel in state.checked:
            state.checked.discard(state.sel)
        else:
            state.checked.add(state.sel)
        _rebuild(event.app)

    @kb.add("c-h")
    @kb.add("backspace")
    def _backspace(event):
        """Backspace：光标在输入框最左时取消自定义激活；否则正常删除。

        - 多选 + 自定义输入框激活 + 光标在最左（``cursor_position==0``）：
          失活并取消选中（空格恢复切换功能）。
        - 其他情况（含单选）：正常删除光标前字符。
        """
        if state.finished:
            return
        on_custom_input = (
            state.sel == state.custom_idx
            and state.custom_active
            and custom_buffer is not None
        )
        if on_custom_input:
            if state.multi and custom_buffer.cursor_position == 0:
                state.checked.discard(state.custom_idx)
                _rebuild(event.app)
                return
            custom_buffer.delete_before_cursor()
            event.app.invalidate()
            return
        # 其余情况：丢弃（避免进入默认缓冲区造成污染）
        event.app.invalidate()

    @kb.add("c-c")
    def _ctrl_c(event):
        if state.finished:
            return
        result.aborted = True
        event.app.exit()

    @kb.add("<any>")
    def _on_char(event):
        """仅当自定义输入框激活时接受字符输入，其余情况丢弃。

        多选未激活的自定义行不接受字符（空格需先选中激活）；普通选项
        上的输入同样丢弃，避免进入默认缓冲区造成污染。
        """
        if state.finished:
            return
        if state.custom_active and custom_buffer is not None:
            event.app.current_buffer.insert_text(event.data or "")
        else:
            # 丢弃：避免键入字符进入默认缓冲区造成污染
            event.app.invalidate()

    layout = _build_ask_layout(state, custom_buffer)
    app: Application = Application(
        layout=Layout(layout),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        style=style,
        input=input,
        output=output,
    )
    # 初始焦点：聚焦当前选中行（自定义已激活则输入框，否则 label/普通行），
    # 不会落到标题等首行。无选项时无焦点。
    if state.options:
        rows = cast(HSplit, app.layout.container).children
        app.layout.focus(_focused_window(state, rows, custom_buffer))

    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        result.aborted = True

    return result


def ask_ui(
    title: str = "",
    options: list[AskOption] | None = None,
    *,
    description: str = "",
    multi: bool = False,
    custom_buffer: Buffer | None = None,
    cursor_index: int = 0,
    checked: set[int] | None = None,
    style=None,
    input=None,
    output=None,
) -> AskResult:
    """运行一次询问界面，返回 ``AskResult``。

    Args:
        title: 标题（可空；空时跳过对应行）。
        options: 选项列表。建议最后一个选项 ``is_custom=True``；
            ask_ui 为其自动渲染输入框（占位文字 = ``description``）。
        description: 描述文本（可空），位于标题下方。
        multi: False 单选（默认）；True 多选（空格切换、Enter 提交全部）。
        custom_buffer: 可选，自定义输入框的 Buffer 实例；传入时复用（保留
            文本与光标位置），未传时新建。
        cursor_index: 初始焦点选项索引（默认 0）；调用间可注入以维持
            上次离开时的焦点位置。
        checked: 多选模式下的初始勾选集合（默认空）；调用间可注入以维持
            上次离开时的勾选状态。
        style: 可选，prompt_toolkit ``Style`` 实例，由调用方传入，让
            ``class:placeholder`` 与 ``class:mycode-input`` 等样式类生效。
        input: 可选，注入的 prompt_toolkit input（测试用）。
        output: 可选，注入的 prompt_toolkit output（测试用）。

    Returns:
        ``AskResult`` 数据类，字段：

            - ``selected``：提交的选项 value 列表。
            - ``input``：仅当选中自定义选项为输入框文本（可为空串），
              否则为 ``None``。
            - ``cursor_index``：提交时焦点所在选项索引。
            - ``checked``：提交时的勾选集合。
            - ``aborted``：True 表示用户以 Ctrl-C 中止；此时其余字段
              不反映提交状态（``selected`` 为空列表、``input`` 为
              ``None``）。调用方应以 ``aborted`` 为准判断是否取消。

        其中 ``cursor_index`` / ``checked`` 反映提交时的焦点与勾选状态，
        可在下次调用时回传给 ``ask_ui`` 维持位置。
    """
    state = _AskState(
        title=title,
        description=description,
        options=options,
        multi=multi,
        custom_buffer=custom_buffer,
        cursor_index=cursor_index,
        checked=checked,
    )
    return _run_ask_ui(state, input=input, output=output, style=style)


__all__ = [
    "AskOption",
    "AskResult",
    "ask_ui",
]
