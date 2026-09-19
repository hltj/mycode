"""
通用询问界面模块。

为各工具（包括 confirm）提供交互式问答界面：

- 标题 + 描述（描述支持多行；default 风格下支持 markdown 渲染）
- 普通选项（label / 可选 value / 可选 description）
- 末尾「自定义输入」选项（与选项值一起返回）
- 单选 / 多选支持
- **多问题模式**：一次询问多个问题；每个问题独立作答，顶部横向排列
  各问题短标题（含复选框 + 末尾「提交」），左右键切换、Enter 选定，
  最后一题回车进入提交预览页（确认 / 取消）。

返回值::

    ``AskResult`` 数据类，字段：

    - ``answers``：长度与问题数一致的答案数组（每个元素是一个
      ``AskAnswer``），单问题即长度 1；顺序与问题数组一致。
    - ``aborted``：True 表示用户以 Ctrl-C 中止，或多问题预览页选了「取消」；
      此时 ``answers`` 为空列表。

调用约定：``options`` 列表中**最后一个**元素建议为 ``is_custom=True``，
ask_ui 自动为其渲染输入框；占位文字取该选项的 ``description``。

可注入 ``input`` / ``output``（用于测试中驱动按键序列）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import chain
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
# 问题描述：粗体，与选项描述的暗灰区分
_STYLE_QUESTION = "class:ask-question"
_STYLE_ACTIVE = "class:ask-active"
# 提交预览页「未回答」亮黄
_STYLE_UNANSWERED = "class:ask-unanswered"
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
class AskAnswer:
    """一道问题的答案。

    Attributes:
        selected: 选中项 value 列表（顺序与 options 一致）；
            单选长度 1，多选按 options 顺序列出勾选项。
        input: 自定义输入字符串（仅当选中 ``is_custom`` 选项时有意义）。
        cursor_index: 提交时焦点所在选项索引（用于多次调用间维持焦点位置）。
        checked: 多选模式下提交时的勾选集合（用于多次调用间维持勾选）。
        skipped: True 表示该问题**未作答**（用户未按 Enter 选定就在
            提交预览页确认提交）。与此相对，多选下用户主动勾选 0 项时
            ``selected`` 为空列表但 ``skipped`` 为 False。单问题模式
            Ctrl-C 中止时 ``aborted`` 为 True 且整体 ``answers`` 为空，
            不涉及单题 skipped。
    """

    selected: list[str] = field(default_factory=list)
    input: Optional[str] = None
    cursor_index: int = 0
    checked: set[int] = field(default_factory=set)
    skipped: bool = False


@dataclass
class AskResult:
    """ask_ui 整体交互结果。

    Attributes:
        answers: 每道问题的答案数组（顺序与问题数组一致）；单问题即
            长度 1 的数组。每道问题的答案对应一个 ``AskAnswer``。
        aborted: True 表示用户整体中止（Ctrl-C 或预览页选「取消」）；
            此时 ``answers`` 为空列表，调用方应以 ``aborted`` 为准
            判断是否取消。
    """

    answers: list[AskAnswer] = field(default_factory=list)
    aborted: bool = False


@dataclass
class AskQuestion:
    """每个问题的数据。

    Attributes:
        title: 问题短标题（多问题标题行的横向展示字段）。多问题模式下
            各问题应提供短标题用于标题行展示；仅单问题（长度为 1 的
            数组，或 confirm / 目录信任等无标题场景）可空串。
        description: 可选，完整问题描述（展示在当前问题正下方）。
        options: 选项列表，规则与 ``ask_ui`` 的 ``options`` 一致
            （建议末尾 ``is_custom=True`` 渲染输入框）。
        multi: 该问题是否多选（默认 False 单选）。
        custom_buffer: 可选，复用该问题自定义输入框的 Buffer。
        cursor_index: 初始焦点选项索引（多次调用间维持焦点）。
        checked: 多选模式初始勾选集合。
    """

    title: str = ""
    description: str = ""
    options: list[AskOption] | None = None
    multi: bool = False
    custom_buffer: Buffer | None = None
    cursor_index: int = 0
    checked: set[int] | None = None


class _AskState:
    """ask_ui 内部状态。

    ``questions`` 为问题数组。
    运行时状态（焦点 / 勾选 / 自定义 buffer / 是否已回答）按问题分别保存，
    ``sel`` / ``checked`` / ``custom_idx`` / ``title`` / ``description`` /
    ``options`` / ``multi`` 为「当前问题」的便捷视图。

    ``q_index``：当前问题索引；多问题模式下 ``-1`` 表示提交预览页。
    ``preview_sel``：预览页选中项（0 确认 / 1 取消）。
    """

    def __init__(
        self,
        questions: list[AskQuestion],
    ) -> None:
        if not questions:
            # 空数组视为单问题（无选项无标题），避免各属性索引越界
            questions = [AskQuestion(title="", description="", options=[])]
        self.questions: list[AskQuestion] = list(questions)
        self.q_index: int = 0
        self.preview_sel: int = 0
        self.finished: bool = False
        # 每个问题的运行时状态，保持与 questions 同序
        opts_list = [q.options or [] for q in self.questions]
        self._sels: list[int] = [
            q.cursor_index if 0 <= q.cursor_index < len(opts) else 0
            for q, opts in zip(self.questions, opts_list)
        ]
        self._checkeds: list[set[int]] = [
            set(q.checked or []) for q in self.questions
        ]
        self._custom_buffers: list[Buffer | None] = [
            q.custom_buffer for q in self.questions
        ]
        self._custom_idxs: list[int] = [
            next((i for i, o in enumerate(opts) if o.is_custom), -1)
            for opts in opts_list
        ]
        self._answered: list[bool] = [False] * len(self.questions)

    # ---- 模式判断 ----
    @property
    def multi_question(self) -> bool:
        """是否多问题模式（问题数 > 1）。"""
        return len(self.questions) > 1

    @property
    def preview(self) -> bool:
        """当前是否位于提交预览页（仅多问题模式）。"""
        return self.multi_question and self.q_index < 0

    # ---- 当前问题便捷视图 ----
    @property
    def idx(self) -> int:
        """当前问题索引（预览页时回退到最后一个问题）。"""
        return max(self.q_index, 0)

    @property
    def title(self) -> str:
        return self.questions[self.idx].title

    @property
    def description(self) -> str:
        return self.questions[self.idx].description

    @property
    def options(self) -> list[AskOption]:
        return list(self.questions[self.idx].options or [])

    @property
    def multi(self) -> bool:
        return self.questions[self.idx].multi

    @property
    def sel(self) -> int:
        return self._sels[self.idx]

    @sel.setter
    def sel(self, value: int) -> None:
        self._sels[self.idx] = value

    @property
    def checked(self) -> set[int]:
        return self._checkeds[self.idx]

    @property
    def custom_idx(self) -> int:
        return self._custom_idxs[self.idx]

    @property
    def custom_buffer(self) -> Buffer | None:
        return self._custom_buffers[self.idx]

    @property
    def answered(self) -> bool:
        """当前问题是否已被 Enter 选定答案。"""
        return self._answered[self.idx]

    # ---- 自定义输入框激活态 ----
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

    - classic 风格：纯文本 + ``_STYLE_QUESTION`` 样式（粗体），
      按原文本换行（多行描述）。
    - default 风格：直接交给 rich Markdown ``soft_wrap`` 渲染成富文本
      （加粗 / 内联代码 / 列表 / 代码块等），剥离块级元素行尾 pad 空格
      后解析为 fragments。按标准 markdown 语义：
        - 相邻行（无空行）会被 rich 折叠为同一段落（换行变空格）；
        - 需要显式换行时入参应使用 hard break（行尾两个空格，
          ``line1  \\nline2``），与 ``PromptSession`` / markdown 行为一致；
        - 空行分隔、列表、代码块等结构与普通 markdown 相同。
       ask_ui 不做任何文本改写，换行语义完全由入参的 markdown 决定。

    两种风格都返回 ``_STYLE_QUESTION`` 作为窗口样式：未着色的纯文本
    部分继承粗体，富文本样式（加粗 / 内联代码 / 代码块等）叠加生效。
    问题描述与选项描述不同：选项描述沿用 ``_STYLE_DESCRIPTION``
    （暗灰），此处为问题描述。

    返回 ``(fragments, style)``。markdown 渲染异常时回退纯文本，
    保证界面不因描述格式崩溃。
    """
    if _renderer_mod.RENDER_STYLE == "classic":
        return [(_STYLE_QUESTION, text)], _STYLE_QUESTION
    try:
        ansi = _renderer_mod._markdown_ansi(text, soft_wrap=True)
        # ANSI 解析产出的 fragments 实际均为 2 元组 (style, text)，收窄类型
        # 以匹配 _strip_trailing_pad（list 不变量，宽类型无法直接传入/赋值）
        frags = cast("list[tuple[str, str]]", list(ANSI(ansi).__pt_formatted_text__()))
        # rich 输出末尾带换行（fragments 末尾会多一个空串行），去掉它
        if frags and frags[-1][1] == "\n":
            frags = frags[:-1]
        frags = _strip_trailing_pad(frags)
        return frags, _STYLE_QUESTION
    except Exception:
        return [(_STYLE_QUESTION, text)], _STYLE_QUESTION


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


def _ask_checkbox(checked: bool) -> str:
    """复选框符号，与多选选项的复选框风格一致。

    default：``✅``/``🔳``；classic：``[x]``/``[ ]``。
    """
    if _renderer_mod.RENDER_STYLE == "classic":
        return "[x] " if checked else "[ ] "
    return "✅ " if checked else "🔳 "


def _build_question_tabs(state: _AskState):
    """构造多问题顶部标题行（横向排列，末尾「提交」）。

    - 每个问题前都带一个复选框（``_ask_checkbox``，与多选复选框风格一致）；
      复选框默认未勾选，问题已按 Enter 选定答案后置为勾选。
    - 当前问题的标题用标题色（``_STYLE_TITLE``），其余问题为普通文本颜色。
    - 末尾「提交」用当前标题色展示（横向导航的终点）。
    """
    items = [
        (
            _STYLE_TITLE if (i == state.idx and not state.preview) else "",
            f"{_ask_checkbox(state._answered[i])}{(q.title or '_')}",
        )
        for i, q in enumerate(state.questions)
    ]
    # 每项之间插一分隔 fragment
    frags: StyleAndTextTuples = list(chain(*[
        [("", "  "), item] if i else [item]
        for i, item in enumerate(items)
    ]))
    # 末尾「提交」：位于提交预览页时用标题色（当前导航焦点），否则普通色
    frags.append((_STYLE_TITLE if state.preview else "", "   提交"))
    return Window(content=FormattedTextControl(frags), height=1, dont_extend_width=True)


def _answer_summary(state: _AskState, i: int) -> str:
    """第 i 个问题已选答案的摘要文本（供预览页展示）。"""
    q = state.questions[i]
    opts = q.options or []
    custom_idx = state._custom_idxs[i]
    checked = state._checkeds[i]
    if q.multi:
        sel_idxs = sorted(checked)
    elif 0 <= state._sels[i] < len(opts):
        sel_idxs = [state._sels[i]]
    else:
        sel_idxs = []
    parts = [opts[x].effective_value() for x in sel_idxs if 0 <= x < len(opts)]
    inp: str | None = None
    if custom_idx >= 0 and custom_idx in checked:
        cb = state._custom_buffers[i]
        inp = cb.text if cb is not None else ""
    if parts:
        text = "、".join(parts)
        if inp:
            text = f"{text}（{inp}）"
        return text
    if inp is not None:
        return inp
    return "未回答"


def _build_preview_layout(state: _AskState) -> HSplit:
    """构造提交预览页：标题行 + 问题答案清单 + 确认/取消单选。

    - 顶部仍显示多问题标题行（每个问题名为普通文本，末尾「提交」标题色）。
    - 逐行列出「问题名：答案」；未作答的显示亮黄「未回答」。
    - 末尾空行后是「确认 / 取消」单选（无标题与描述）。
    """
    question_rows = [
        Window(
            content=FormattedTextControl([
                ("", f"{(q.title or '_')}："),
                (
                    "" if state._answered[i] else _STYLE_UNANSWERED,
                    _answer_summary(state, i) if state._answered[i] else "未回答",
                ),
            ]),
            height=1,
            dont_extend_width=True,
        )
        for i, q in enumerate(state.questions)
    ]
    # 确认/取消单选行（前缀用单选风格的 _mark_str）
    confirm_rows = [
        Window(
            content=FormattedTextControl(
                [( _STYLE_ACTIVE if i == state.preview_sel else "",
                   f"{_mark_str(False, i == state.preview_sel, False)}{label}")],
                focusable=i == state.preview_sel,
                show_cursor=False,
            ),
            height=1,
            dont_extend_width=True,
        )
        for i, label in enumerate(["确认", "取消"])
    ]
    rows = [
        _build_question_tabs(state),
        Window(content=FormattedTextControl(""), height=1),
        *question_rows,
        Window(content=FormattedTextControl(""), height=1),
        *confirm_rows,
    ]
    return HSplit(rows)


def _build_ask_layout(
    state: _AskState,
    custom_buffer: Buffer | None,
) -> HSplit:
    """构建询问界面整体布局：标题 → 描述 → 各选项行。

    - 多问题模式：标题行是横向排列的短标题 + 末尾「提交」。
    - 单问题模式：标题行是单问题标题（与历史行为一致）。
    标题与描述都非空才展示对应行；标题行或描述存在时增加分隔空行。
    """
    rows: list = []

    # 标题行（多问题为横向 tabs；单问题为原标题）
    if state.multi_question:
        rows.append(_build_question_tabs(state))
    elif state.title:
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
    if state.multi_question or state.title or state.description:
        rows.append(Window(
            content=FormattedTextControl(""),
            height=1,
        ))

    # 选项
    rows.extend(
        _build_option_window(state, idx, opt, idx == state.sel, custom_buffer)
        for idx, opt in enumerate(state.options)
    )

    return HSplit(rows)


def _option_row_offset(state: _AskState) -> int:
    """返回根 HSplit 中第一个选项的 child 索引（即 header 所占 child 数）。

    该索引用作焦点定位的下标：``_focused_window`` 以 ``offset + sel``
    取当前选中选项在根 HSplit 中的 child。描述即使多行也只是单个 child，
    故该值不随描述行数变化。多问题模式标题行为横向 tabs，同样占一个 child。
    """
    offset = 0
    if state.multi_question or state.title:
        offset += 1
    if state.description:
        offset += 1
    # 有标题或描述时，header 区后有分隔空行
    if state.multi_question or state.title or state.description:
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


def _build_layout(state: _AskState) -> HSplit:
    """按当前状态构造整体布局：预览页或当前问题布局。"""
    if state.preview:
        return _build_preview_layout(state)
    return _build_ask_layout(state, state.custom_buffer)


def _focus_from_layout(
    app, state: _AskState,
) -> Window | None:
    """从当前 app 布局中取出应聚焦的 Window。

    必须在 ``app.layout.container`` 已更新后调用（focus 要求元素属于
    当前 layout）。预览页聚焦确认/取消行；问题页聚焦当前选中选项行。
    """
    container = app.layout.container
    if state.preview:
        rows = cast(HSplit, container).children
        # children[-2] 确认（preview_sel=0）、children[-1] 取消（preview_sel=1）
        return cast(Window, rows[-2 + state.preview_sel])
    if state.options:
        rows = cast(HSplit, container).children
        return _focused_window(state, rows, state.custom_buffer)
    return None


def _run_ask_ui(
    state: _AskState,
    input=None,
    output=None,
    style=None,
) -> AskResult:
    """运行 ask_ui 交互界面，返回结果。

    - 单问题：与历史行为一致（标题/描述 + 选项行，Enter 提交）。
    - 多问题：标题行横向排列短标题 + 末尾「提交」；每个问题独立作答，
      Enter 选定答案并切到下一问题；最后一个问题 Enter 后进入提交预览页。
      预览页列出各问题答案（未回答显示亮黄），「确认」可整体返回，
      按「取消」即整体取消回答。左右键在问题间切换，预览页左切到
      最后一个问题、右切到第一个问题。

    ``state`` 的重建使用各问题的 custom_buffer（默认新建，文本与光标位置
    在问题间保持）。

    ``style`` 由调用方传入，否则 prompt_toolkit 用默认样式，
    class:placeholder / class:mycode-input 等样式表项不会生效。
    """
    result = AskResult()
    kb = KeyBindings()

    # 各问题的自定义输入框：外部传入的复用，否则按需新建。
    # 逐个问题对应处理：无自定义选项的问题（custom_idx 为 -1）保持 None，
    # 其余为已有 buffer 或新建 Buffer，整体仍与 questions 一一对应。
    state._custom_buffers[:] = [
        buf if (idx < 0 or buf is not None) else Buffer()
        for buf, idx in zip(state._custom_buffers, state._custom_idxs)
    ]

    def _current_custom() -> Buffer | None:
        """当前问题布局对应的输入框（无自定义选项时 None）。"""
        if state.preview or state.custom_idx < 0:
            return None
        return state.custom_buffer

    def _rebuild(app) -> None:
        """重建布局并更新焦点。

        多问题模式：当前问题布局或提交预览页；焦点落到当前选中行的
        可聚焦 Window（自定义激活则输入框）。预览页焦点落到确认/取消行。
        """
        app.layout.container = _build_layout(state)
        focus = _focus_from_layout(app, state)
        if focus is None:
            return
        app.layout.focus(focus)

    def _move_option(event, delta: int) -> None:
        """在问题选项内移动焦点（环形）。"""
        if state.finished:
            return
        opts = state.options
        if not opts or state.preview:
            return
        n = len(opts)
        state.sel = (state.sel + delta) % n
        _rebuild(event.app)

    def _finish(event) -> None:
        """提交预览页确认 / 单问题提交：收集所有答案并退出。

        - 预览页停在「取消」时：整体取消回答（等同 Ctrl-C 中止，
          ``aborted=True``），不收集答案。
        - 其余情况：每道问题的答案收集成 ``AskAnswer`` 存入
          ``result.answers``（顺序与问题数组一致）。
        """
        if state.preview and state.preview_sel == 1:
            state.finished = True
            result.aborted = True
            event.app.exit()
            return
        state.finished = True
        result.answers = [_collect_answer(i) for i in range(len(state.questions))]
        event.app.exit()

    def _collect_answer(i: int) -> AskAnswer:
        """按第 i 个问题的当前状态实时收集答案。

        - 未被 Enter 选定（未作答）：返回 ``skipped=True`` 的空答案
          （``selected`` 为空列表、``input`` 为 None），提交预览页据此
          显示「未回答」，并与「多选主动勾选 0 项」区分。
        - 已选定：按当时的焦点 / 勾选 / 自定义输入文本收集（再次编辑时
          以最新状态为准，不做缓存）。
        """
        q = state.questions[i]
        opts = q.options or []
        custom_idx = state._custom_idxs[i]
        checked = state._checkeds[i]
        sel_p = state._sels[i]
        if not state._answered[i]:
            return AskAnswer(
                selected=[], cursor_index=sel_p,
                checked=set(checked), input=None, skipped=True,
            )
        if q.multi:
            sel_idxs = sorted(c for c in checked if c < len(opts))
        else:
            sel_idxs = [sel_p] if 0 <= sel_p < len(opts) else []
        ar = AskAnswer(
            selected=[opts[x].effective_value() for x in sel_idxs],
            cursor_index=sel_p,
            checked=set(checked),
        )
        if custom_idx in sel_idxs:
            cb = state._custom_buffers[i]
            ar.input = cb.text if cb is not None else ""
        else:
            ar.input = None
        return ar

    def _answer_current(event) -> None:
        """选定当前问题的答案，按情形收尾或切到下一题。

        - 选定答案：标记为已回答（标题行复选框转勾选）。
        - 多问题模式：切到下一个问题；已经是最后一个问题时进入
          提交预览页。
        - 单问题模式：直接提交退出。
        """
        if state.finished:
            return
        if state.preview:
            _finish(event)
            return
        q = state.questions[state.idx]
        if not (q.options or []):
            return
        state._answered[state.idx] = True
        if state.multi_question:
            if state.idx >= len(state.questions) - 1:
                state.q_index = -1
                state.preview_sel = 0
            else:
                state.q_index = state.idx + 1
        else:
            _finish(event)
            return
        _rebuild(event.app)

    def _jump_to_preview(event) -> None:
        """跳到提交预览页（末尾「提交」）。"""
        if state.finished or not state.multi_question:
            return
        state.q_index = -1
        state.preview_sel = 0
        _rebuild(event.app)

    def _nav_question(event, delta: int) -> None:
        """左右切换问题 / 预览页。

        - 处于问题：左移上一个问题、右移下一个问题（环形）。
        - 处于预览页：右移回到第一个问题，左移回到最后一个问题。
        """
        if state.finished or not state.multi_question:
            return
        if state.preview:
            state.q_index = 0 if delta > 0 else len(state.questions) - 1
            _rebuild(event.app)
            return
        n = len(state.questions)
        state.q_index = (state.idx + delta) % n
        _rebuild(event.app)

    @kb.add("down")
    @kb.add("c-n")
    def _down(event):
        if state.preview:
            state.preview_sel = 1
            _rebuild(event.app)
            return
        _move_option(event, +1)

    @kb.add("up")
    @kb.add("c-p")
    def _up(event):
        if state.preview:
            state.preview_sel = 0
            _rebuild(event.app)
            return
        _move_option(event, -1)

    def _can_switch_question() -> bool:
        """左右键是否用于切换问题。

        自定义输入框激活且焦点在其上时：左右键留给输入框移动光标，
        不用于切换问题。预览页左右键始终用于导航。
        """
        if state.finished:
            return False
        if state.preview:
            return True
        if state.custom_idx < 0 or not state.custom_active:
            return True
        return not _current_custom_focused()

    def _current_custom_focused() -> bool:
        """当前焦点是否落在当前问题的自定义输入框上。"""
        if state.custom_idx < 0 or not state.custom_active:
            return False
        try:
            from prompt_toolkit.application import get_app
            return get_app().layout.current_buffer is _current_custom()
        except Exception:
            return False

    # 左右切换问题：自定义输入框激活且焦点在其中时不拦截（左右键留给
    # 输入框移动光标）。filter 求值时才有事件上下文，用 Condition 闭包。
    @kb.add("left", filter=Condition(_can_switch_question))
    @kb.add("c-b", filter=Condition(_can_switch_question))
    def _left(event):
        _nav_question(event, -1)

    @kb.add("right", filter=Condition(_can_switch_question))
    @kb.add("c-f", filter=Condition(_can_switch_question))
    def _right(event):
        _nav_question(event, +1)

    @kb.add("tab")
    def _tab(event):
        """Tab：多问题模式跳到提交预览页（普通 tab 忽略）。"""
        if state.preview:
            event.app.invalidate()
            return
        _jump_to_preview(event)

    @kb.add("enter")
    def _enter(event):
        _answer_current(event)

    @kb.add(" ")
    def _space(event):
        """空格键：自定义输入框激活时输入空格，否则按多选/单选分发。

        - 自定义输入框激活（单选或多选均适用）：空格插入输入框。多选下
          激活意味着已选中，单选下激活即焦点在自定义行。
        - 多选 + 未激活：切换勾选（选中自定义行时同时激活其输入框）。
        - 单选 + 未激活（普通选项行）：无操作。
        - 预览页：空格做任何单选行的选择/切换（等价 Enter）。
        """
        if state.finished:
            return
        if state.preview:
            _finish(event)
            return
        is_custom_row = state.sel == state.custom_idx
        # 自定义输入框已激活：空格作为普通字符输入
        if state.custom_active and is_custom_row and _current_custom() is not None:
            _current_custom().insert_text(" ")
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
            not state.preview
            and state.sel == state.custom_idx
            and state.custom_active
            and _current_custom() is not None
        )
        if on_custom_input:
            cb = _current_custom()
            if state.multi and cb.cursor_position == 0:
                state.checked.discard(state.custom_idx)
                _rebuild(event.app)
                return
            cb.delete_before_cursor()
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
        上的输入同样丢弃，避免进入默认缓冲区造成污染。预览页丢弃字符。
        """
        if state.finished:
            return
        if state.preview:
            event.app.invalidate()
            return
        cb = _current_custom()
        if state.custom_active and cb is not None:
            cb.insert_text(event.data or "")
        else:
            # 丢弃：避免键入字符进入默认缓冲区造成污染
            event.app.invalidate()

    app: Application = Application(
        layout=Layout(_build_layout(state)),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        style=style,
        input=input,
        output=output,
    )
    # 初始焦点：聚焦当前选中行（自定义已激活则输入框，否则 label/普通行），
    # 不会落到标题等首行。无选项时无焦点；预览页聚焦确认/取消行。
    focus = _focus_from_layout(app, state)
    if focus is not None:
        app.layout.focus(focus)

    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        result.aborted = True

    return result


def ask_ui(
    questions: list[AskQuestion],
    *,
    style=None,
    input=None,
    output=None,
) -> AskResult:
    """运行一次询问界面，返回 ``AskResult``。

    Args:
        questions: 问题数组。单问题传长度为 1 的数组；每个问题分别指定
            ``AskQuestion``（含标题 / 描述 / 选项 / 是否多选 / 自定义
            输入框 buffer / 初始焦点 / 初始勾选）。
        style: 可选，prompt_toolkit ``Style`` 实例，由调用方传入，让
            ``class:placeholder`` 与 ``class:mycode-input`` 等样式类生效。
        input: 可选，注入的 prompt_toolkit input（测试用）。
        output: 可选，注入的 prompt_toolkit output（测试用）。

    Returns:
        ``AskResult`` 数据类，字段：

            - ``answers``：每道问题的答案（按问题顺序），每个都是
               ``AskAnswer``（含 ``selected`` / ``input`` / ``cursor_index`` /
               ``checked`` / ``skipped``）；单问题即长度 1 的数组。
               未作答的问题（用户未按 ``Enter`` 选定就在提交预览页
               确认提交）对应答案 ``skipped=True``、``selected`` 为空列表。
            - ``aborted``：True 表示用户以 Ctrl-C 中止，或多问题预览页
              选了「取消」；此时 ``answers`` 为空列表。调用方应以
              ``aborted`` 为准判断是否取消。

        其中每道问题答案的 ``cursor_index`` / ``checked`` 反映提交时的
        焦点与勾选状态，可在下次调用时回传给对应的 ``AskQuestion`` 维持位置。
    """
    state = _AskState(questions=questions)
    return _run_ask_ui(state, input=input, output=output, style=style)


__all__ = [
    "AskAnswer",
    "AskOption",
    "AskQuestion",
    "AskResult",
    "ask_ui",
]
