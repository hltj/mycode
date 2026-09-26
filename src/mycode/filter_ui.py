"""
通用筛选选择界面模块。

与具体业务解耦的分页 + 关键词筛选选择界面（基于 prompt_toolkit 独立
``Application``），用于候选项较多的场景（如 models.dev 的 182 家供应商、
单家数百个模型）。

设计概要（详见 ``docs/dev/filter_ui_design.md``）：

- 选项模型 ``FilterOption``：label（筛选匹配对象）/ value / description
  （不参与匹配）/ selected（多选初始勾选态）。
- 布局：标题（可选）→ 空行 → 描述（可选，markdown 渲染，复用 ask_ui 的
  ``_description_fragments``）→ 空行 → 筛选框 → 空行 → 选项行（当前页）→
  分页状态行 → 空行 → 提示行。
- 焦点模型：默认焦点在筛选框；筛选框 ``↓`` 进入列表、列表首项 ``↑`` 回
  筛选框；``tab`` 切换；筛选框内 ``←``/``→`` 移动输入光标（不翻页）、
  ``pgup``/``pgdown`` 翻页；列表内 ``↑``/``↓`` 页内环形移动、
  ``←``/``→``/``pgup``/``pgdown`` 翻页；焦点在列表时字符输入被丢弃。
- 过滤：关键词按不区分大小写的子串匹配 ``label``；关键词变化后页码回 1、
  光标回首项；无匹配时空态展示且 Enter 无操作。
- 交互：``space`` 多选切换勾选 / 单选选定；``enter`` 单选返回当前项 /
  多选提交全部勾选项；``Ctrl-C`` 取消。
- 返回 ``FilterResult``：selected / aborted / keyword / cursor_index /
  page / checked。
- 状态持久化：``keyword`` / ``cursor_index`` 入参恢复，``keyword_buffer``
  注入既有 Buffer（关键词文本与光标跨调用保留）；多选勾选态经
  ``FilterOption.selected`` 传入。

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
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor

from mycode import renderer as _renderer_mod
from mycode.ask_ui import _description_fragments

_STYLE_TITLE = "class:ask-title"
_STYLE_DESCRIPTION = "class:ask-description"
_STYLE_ACTIVE = "class:ask-active"
_STYLE_PLACEHOLDER = "class:placeholder"
_STYLE_INPUT = "class:mycode-input"

# 每页默认行数
DEFAULT_PAGE_SIZE = 15

_KEYWORD_LABEL = "筛选: "
_KEYWORD_PLACEHOLDER = "输入关键词筛选"
_HINT_LINE = "↑↓ 移动 · 空格 勾选/选择 · ←→ PgUp PgDn 翻页 · ↵ 确定 · Ctrl-C 取消"
_NO_MATCH_LINE = "无匹配项"


@dataclass
class FilterOption:
    """筛选选项数据。

    Attributes:
        label: 展示文本（筛选匹配对象）。
        value: 取值；无 value 回退 label。
        description: 可选描述，展示在 label 之后（暗灰；不参与匹配）。
        selected: 多选模式的初始勾选态。
    """

    label: str
    value: Optional[str] = None
    description: Optional[str] = None
    selected: bool = False

    def effective_value(self) -> str:
        """返回该选项的取值。"""
        return self.value if self.value is not None else self.label


@dataclass
class FilterResult:
    """筛选界面整体交互结果。

    Attributes:
        selected: 选中项 value 列表（无 value 回退 label）；单选长度 0 或 1，
            多选按过滤后列表顺序；aborted 时为空列表。
        aborted: True 表示 Ctrl-C 取消。
        keyword: 提交/取消时的筛选关键词（下次调用回传恢复）。
        cursor_index: 提交/取消时光标在“过滤后列表”中的索引。
        page: 提交/取消时的页码（1-based）。
        checked: 多选提交时的勾选集合（过滤后列表索引）。
    """

    selected: list[str] = field(default_factory=list)
    aborted: bool = False
    keyword: str = ""
    cursor_index: int = 0
    page: int = 1
    checked: set[int] = field(default_factory=set)


class _FilterState:
    """filter_ui 内部状态。

    ``options`` 为全量选项；``filtered`` 为当前关键词过滤后的子集
    （索引即 FilterResult 的 cursor_index / checked 基准）；``page`` 为
    当前页码（1-based）；``cursor`` 为光标在过滤后列表中的索引；
    ``focus_list`` 表示焦点是否在选项列表（False 为筛选框）。
    """

    def __init__(
        self,
        options: list[FilterOption],
        *,
        title: str = "",
        description: str = "",
        multi: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        keyword: str = "",
        keyword_buffer: Optional[Buffer] = None,
        cursor_index: int = 0,
    ) -> None:
        self.options: list[FilterOption] = list(options)
        self.title: str = title
        self.description: str = description
        self.multi: bool = multi
        self.page_size: int = max(1, page_size)
        self.keyword_buffer: Buffer = keyword_buffer or Buffer()
        if keyword and not self.keyword_buffer.text:
            self.keyword_buffer.insert_text(keyword)
        self.cursor: int = 0
        self.page: int = 1
        self.focus_list: bool = False
        self.finished: bool = False
        # 多选勾选集合：基线是 options 的稳定标识（按对象 id），避免过滤
        # 变化导致 checked 索引漂移；提交时映射回过滤后索引。
        self._checked_ids: set[int] = {
            id(o) for o in self.options if o.selected
        }
        self.filtered: list[FilterOption] = []
        self._last_applied: str = self.keyword_buffer.text
        # 筛选框行在根布局中的 child 下标（_build_layout 填写，焦点定位用）
        self._keyword_row_index: int = 0
        self.apply_filter()
        if 0 <= cursor_index < len(self.filtered):
            self.cursor = cursor_index
            # 恢复光标对应的页码
            self.page = self.cursor // self.page_size + 1

    # ---- 过滤 ----
    def apply_filter(self) -> None:
        """按当前关键词刷新过滤后列表；页码/光标重置。"""
        kw = self.keyword_buffer.text.strip().lower()
        if kw:
            self.filtered = [
                o for o in self.options
                if kw in o.label.lower()
            ]
        else:
            self.filtered = list(self.options)
        self.page = 1
        self.cursor = 0

    @property
    def keyword(self) -> str:
        """当前筛选关键词文本（来自 Buffer）。"""
        return self.keyword_buffer.text

    def sync_keyword(self) -> bool:
        """检测关键词文本是否变化（Buffer 文本 vs 上次应用的过滤词）。

        变化时重新应用过滤并返回 True。
        """
        current = self.keyword_buffer.text
        if current != self._last_applied:
            self._last_applied = current
            self.apply_filter()
            return True
        return False

    # ---- 分页 ----
    @property
    def total_pages(self) -> int:
        n = len(self.filtered)
        return max(1, (n + self.page_size - 1) // self.page_size)

    def page_items(self) -> list[tuple[int, FilterOption]]:
        """当前页的 (过滤后索引, 选项) 列表。"""
        start = (self.page - 1) * self.page_size
        end = start + self.page_size
        return list(enumerate(self.filtered))[start:end]

    def page_status(self) -> str:
        return f"第 {self.page}/{self.total_pages} 页 · 共 {len(self.filtered)} 项"

    def turn_page(self, delta: int) -> None:
        """环形翻页；翻页后光标停在当前页首行。"""
        total = self.total_pages
        self.page = ((self.page - 1 + delta) % total) + 1
        self.cursor = (self.page - 1) * self.page_size

    # ---- 光标 ----
    def move_cursor(self, delta: int) -> None:
        """页内环形移动光标（不出当前页）。"""
        start = (self.page - 1) * self.page_size
        end = min(start + self.page_size, len(self.filtered))
        if start >= end:
            return
        count = end - start
        offset = (self.cursor - start + delta) % count
        self.cursor = start + offset

    # ---- 勾选（多选）----
    def is_checked(self, opt: FilterOption) -> bool:
        return id(opt) in self._checked_ids

    def toggle_check(self) -> None:
        if not self.multi or not self.filtered:
            return
        opt = self.filtered[self.cursor]
        if id(opt) in self._checked_ids:
            self._checked_ids.discard(id(opt))
        else:
            self._checked_ids.add(id(opt))
            opt.selected = True
        # 同步 selected 标记（供 options 列表复用时回传）
        opt.selected = id(opt) in self._checked_ids

    def checked_values(self) -> list[str]:
        return [
            o.effective_value()
            for o in self.filtered
            if id(o) in self._checked_ids
        ]

    def checked_indices(self) -> set[int]:
        return {
            i for i, o in enumerate(self.filtered)
            if id(o) in self._checked_ids
        }


def _mark_str(state: _FilterState, active: bool, checked: bool) -> str:
    """选项行前缀（风格与 ask_ui 一致）。

    - default：单选 ``❯ 🟢 ``（当前行）/``  ⚪ ``；多选 ``❯ ✅ ``/``❯ 🔳 ``
      （当前行）与 ``  ✅ ``/``  🔳 ``（非当前行）。
    - classic：单选 ``> ``/``  ``；多选 ``> [x] ``/``  [ ] `` 等。
    """
    classic = _renderer_mod.RENDER_STYLE == "classic"
    if state.multi:
        row = "> " if classic else "❯ "
        row = row if active else "  "
        box = ("[x] " if checked else "[ ] ") if classic else ("✅ " if checked else "🔳 ")
        return row + box
    if classic:
        return "> " if active else "  "
    return "❯ 🟢 " if active else "  ⚪ "


def _build_option_row(
    state: _FilterState,
    idx: int,
    opt: FilterOption,
    active: bool,
):
    """构造单个选项行：前缀 + label + description（暗灰）。"""
    checked = state.is_checked(opt)
    mark = _mark_str(state, active, checked)
    style = _STYLE_ACTIVE if active else ""
    fragments: list[tuple[str, str]] = [(style, f"{mark}{opt.label}")]
    if opt.description:
        fragments.append((_STYLE_DESCRIPTION, f"  {opt.description}"))
    win = Window(
        content=FormattedTextControl(
            fragments,  # type: ignore[arg-type]
            focusable=active,
            show_cursor=False,
        ),
        height=1,
        dont_extend_width=True,
    )
    return win


def _build_keyword_row(state: _FilterState) -> VSplit:
    """构造筛选框行：label Window + 关键词输入框。"""
    label_win = Window(
        content=FormattedTextControl(
            [(_STYLE_DESCRIPTION, _KEYWORD_LABEL)],
            show_cursor=False,
        ),
        height=1,
        dont_extend_width=True,
    )

    def _show_placeholder() -> bool:
        return not bool(state.keyword_buffer.text)

    input_win = Window(
        content=BufferControl(
            buffer=state.keyword_buffer,
            input_processors=[
                ConditionalProcessor(
                    AfterInput(
                        text=f" {_KEYWORD_PLACEHOLDER}",
                        style=_STYLE_PLACEHOLDER,
                    ),
                    filter=Condition(_show_placeholder),
                ),
            ],
            # 焦点在筛选框时可聚焦；焦点在列表时不可（避免 prompt_toolkit
            # 默认聚焦它）
            focusable=not state.focus_list,
        ),
        height=1,
        style=_STYLE_INPUT,
    )
    return VSplit([label_win, input_win])


def _build_layout(state: _FilterState) -> HSplit:
    """构建整体布局。

    结构：标题（可选）→ 空行 → 描述（可选）→ 空行 → 筛选框 → 空行 →
    选项行（当前页）→ 分页状态行 → 空行 → 提示行。
    """
    rows: list[Container] = []
    if state.title:
        rows.append(Window(
            content=FormattedTextControl([(_STYLE_TITLE, state.title)]),
            height=1,
            dont_extend_width=True,
        ))
        if state.description:
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
        rows.append(Window(content=FormattedTextControl(""), height=1))

    keyword_row_idx = len(rows)
    rows.append(_build_keyword_row(state))
    rows.append(Window(content=FormattedTextControl(""), height=1))

    if state.filtered:
        for idx, opt in state.page_items():
            rows.append(_build_option_row(state, idx, opt, idx == state.cursor))
    else:
        rows.append(Window(
            content=FormattedTextControl(
                [( _STYLE_DESCRIPTION, _NO_MATCH_LINE)]
            ),
            height=1,
            dont_extend_width=True,
        ))

    rows.append(Window(
        content=FormattedTextControl(
            [(_STYLE_DESCRIPTION, state.page_status())]
        ),
        height=1,
        dont_extend_width=True,
    ))
    rows.append(Window(content=FormattedTextControl(""), height=1))
    rows.append(Window(
        content=FormattedTextControl([(_STYLE_DESCRIPTION, _HINT_LINE)]),
        height=1,
        dont_extend_width=True,
    ))
    # 记录筛选框行在根布局中的下标（焦点定位用）
    state._keyword_row_index = keyword_row_idx
    return HSplit(rows)


def _current_option_window(state: _FilterState, rows: list[Container]) -> Window:
    """取当前光标选项行的 Window（或空态行）。"""
    # 选项区起始 child：header 区 + 筛选框 + 空行
    opt_start = state._keyword_row_index + 2
    page_pos = state.cursor - (state.page - 1) * state.page_size
    row = rows[opt_start + page_pos]
    return cast(Window, row)


def filter_ui(
    options: list[FilterOption],
    *,
    title: str = "",
    description: str = "",
    multi: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    keyword: str = "",
    keyword_buffer: Optional[Buffer] = None,
    cursor_index: int = 0,
    style=None,
    input=None,
    output=None,
) -> FilterResult:
    """运行一次筛选选择界面，返回 ``FilterResult``。

    Args:
        options: 全量选项列表。
        title: 可选标题。
        description: 可选描述（支持多行 / markdown）。
        multi: 是否多选。
        page_size: 每页行数（默认 15）。
        keyword: 初始筛选关键词（keyword_buffer 为空时生效）。
        keyword_buffer: 注入既有关键词输入框 Buffer（文本与光标跨调用
            保留，优先于 keyword）。
        cursor_index: 初始光标在过滤后列表中的索引（同时恢复页码）。
        style: 可选，prompt_toolkit ``Style`` 实例。
        input: 可选，注入的 prompt_toolkit input（测试用）。
        output: 可选，注入的 prompt_toolkit output（测试用）。
    """
    state = _FilterState(
        options,
        title=title,
        description=description,
        multi=multi,
        page_size=page_size,
        keyword=keyword,
        keyword_buffer=keyword_buffer,
        cursor_index=cursor_index,
    )
    state._last_applied = state.keyword_buffer.text
    state.apply_filter()
    if 0 <= cursor_index < len(state.filtered):
        state.cursor = cursor_index
        state.page = cursor_index // state.page_size + 1

    result = FilterResult(
        keyword=state.keyword_buffer.text,
        cursor_index=state.cursor,
        page=state.page,
        checked=state.checked_indices(),
    )
    kb = KeyBindings()

    def _rebuild(app) -> None:
        """重新应用过滤（若关键词变化）并重建布局、更新焦点。"""
        if state.sync_keyword():
            pass  # apply_filter 已在 sync_keyword 内执行
        app.layout.container = _build_layout(state)
        rows = cast(HSplit, app.layout.container).children
        if state.focus_list and state.filtered:
            app.layout.focus(_current_option_window(state, rows))
        else:
            # 焦点在筛选框：聚焦关键词输入框（VSplit children[1]）
            kw_row = cast(VSplit, rows[state._keyword_row_index])
            app.layout.focus(cast(Window, kw_row.children[1]))

    def _finish(event) -> None:
        # 提交前先同步关键词过滤（焦点在筛选框直接回车时，Buffer 文本
        # 可能尚未经 _rebuild 应用）
        state.sync_keyword()
        state.finished = True
        result.keyword = state.keyword_buffer.text
        result.cursor_index = state.cursor
        result.page = state.page
        result.checked = state.checked_indices()
        if state.multi:
            result.selected = state.checked_values()
        else:
            if state.filtered:
                result.selected = [state.filtered[state.cursor].effective_value()]
        event.app.exit()

    @kb.add("down")
    @kb.add("c-n")
    def _down(event):
        if state.finished:
            return
        if not state.focus_list:
            if state.filtered:
                state.focus_list = True
                # 光标不在当前页（如恢复态与页码不一致）时重置到页首；
                # 已在当前页则保留（恢复语义）
                page_start = (state.page - 1) * state.page_size
                page_end = min(page_start + state.page_size, len(state.filtered))
                if not (page_start <= state.cursor < page_end):
                    state.cursor = page_start
                _rebuild(event.app)
            return
        state.move_cursor(+1)
        _rebuild(event.app)

    @kb.add("up")
    @kb.add("c-p")
    def _up(event):
        if state.finished:
            return
        if not state.focus_list:
            return
        page_start = (state.page - 1) * state.page_size
        if state.cursor <= page_start:
            # 列表首项 ↑ 回筛选框
            state.focus_list = False
            _rebuild(event.app)
            return
        state.move_cursor(-1)
        _rebuild(event.app)

    @kb.add("left", filter=Condition(lambda: state.focus_list and not state.finished))
    @kb.add("c-b", filter=Condition(lambda: state.focus_list and not state.finished))
    def _left(event):
        """列表内 ← 翻页；筛选框内不绑定（留给输入框移动光标）。"""
        if state.finished:
            return
        state.turn_page(-1)
        _rebuild(event.app)

    @kb.add("right", filter=Condition(lambda: state.focus_list and not state.finished))
    @kb.add("c-f", filter=Condition(lambda: state.focus_list and not state.finished))
    def _right(event):
        """列表内 → 翻页；筛选框内不绑定（留给输入框移动光标）。"""
        if state.finished:
            return
        state.turn_page(+1)
        _rebuild(event.app)

    @kb.add("pageup")
    def _pgup(event):
        if state.finished:
            return
        state.turn_page(-1)
        _rebuild(event.app)

    @kb.add("pagedown")
    def _pgdn(event):
        if state.finished:
            return
        state.turn_page(+1)
        _rebuild(event.app)

    @kb.add("tab")
    def _tab(event):
        if state.finished:
            return
        if state.focus_list:
            state.focus_list = False
        else:
            if state.filtered:
                state.focus_list = True
                # 与 down 一致：光标已在当前页则保留
                page_start = (state.page - 1) * state.page_size
                page_end = min(page_start + state.page_size, len(state.filtered))
                if not (page_start <= state.cursor < page_end):
                    state.cursor = page_start
        _rebuild(event.app)

    @kb.add(" ")
    def _space(event):
        if state.finished:
            return
        if not state.focus_list:
            # 筛选框内空格：作为关键词字符输入
            state.keyword_buffer.insert_text(" ")
            event.app.invalidate()
            return
        if state.multi:
            state.toggle_check()
            _rebuild(event.app)
            return
        # 单选：等价 Enter
        _finish(event)

    @kb.add("enter")
    def _enter(event):
        if state.finished:
            return
        if not state.focus_list:
            # 筛选框内回车：切到列表（无匹配则不切换）；提交统一在列表内
            # 按 Enter 触发，避免筛选框回车误提交。
            state.sync_keyword()
            if not state.filtered:
                event.app.invalidate()
                return
            state.focus_list = True
            page_start = (state.page - 1) * state.page_size
            page_end = min(page_start + state.page_size, len(state.filtered))
            if not (page_start <= state.cursor < page_end):
                state.cursor = page_start
            _rebuild(event.app)
            return
        # 列表内：提交当前项（单选）/ 全部勾选（多选）
        if not state.filtered:
            event.app.invalidate()
            return
        _finish(event)

    @kb.add("c-c")
    def _ctrl_c(event):
        if state.finished:
            return
        state.sync_keyword()
        state.finished = True
        result.aborted = True
        result.cursor_index = state.cursor
        result.page = state.page
        result.keyword = state.keyword_buffer.text
        event.app.exit()

    @kb.add("<any>", filter=Condition(lambda: state.focus_list and not state.finished))
    def _on_char(event):
        """焦点在列表时丢弃字符输入（关键词编辑只在筛选框内）。

        经 ``filter=Condition`` 只在列表焦点时生效，不影响筛选框内的
        默认 Buffer 编辑（字符正常进入关键词 Buffer）。
        """
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
    # 初始焦点：筛选框
    rows = cast(HSplit, app.layout.container).children
    kw_row = cast(VSplit, rows[state._keyword_row_index])
    app.layout.focus(cast(Window, kw_row.children[1]))

    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        result.aborted = True

    return result
