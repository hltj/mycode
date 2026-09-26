"""
通用筛选选择界面（mycode.filter_ui）测试。

覆盖：

- ``FilterOption`` 数据与默认值 / effective_value
- 单选：Enter 提交当前项；空格选定；移动光标后提交
- 多选：Space 切换勾选；Enter 提交全部勾选项；初始勾选态
- 过滤：关键词不区分大小写子串匹配 label；过滤后页码/光标重置；
  无匹配显示空态；description 不参与匹配
- 分页：page_size 分页；pgup/pgdown 与列表内 ←→ 翻页；翻页后光标页首；
  页内环形移动
- 焦点切换：筛选框 ↓ 进入列表；列表首项 ↑ 回筛选框；tab 两区域切换；
  筛选框内 ←→ 移动光标（不翻页）、pgup/pgdown 翻页；焦点在列表时
  字符输入被忽略
- 取消：Ctrl-C 终止（aborted=True）
- 状态持久化：keyword / cursor_index / checked 回传恢复
- 布局：标题 / 描述 / 筛选框 / 选项行前缀 / 分页状态行 / 提示行
"""

from __future__ import annotations

import pytest

from mycode.filter_ui import FilterOption, FilterResult, filter_ui


def _mk(n: int, prefix: str = "opt") -> list[FilterOption]:
    return [FilterOption(label=f"{prefix}{i}", value=f"v{i}") for i in range(n)]


def _run_with_keys(
    seq: str,
    options: list[FilterOption],
    *,
    title: str = "",
    description: str = "",
    multi: bool = False,
    page_size: int = 5,
    keyword: str = "",
    cursor_index: int = 0,
):
    """用注入的按键序列运行 filter_ui，返回 ``FilterResult``。"""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as inp:
        inp.send_text(seq)
        return filter_ui(
            options,
            title=title,
            description=description,
            multi=multi,
            page_size=page_size,
            keyword=keyword,
            cursor_index=cursor_index,
            input=inp,
            output=DummyOutput(),
        )


# ===================================================================
# FilterOption 数据
# ===================================================================

class TestFilterOption:
    def test_defaults(self):
        o = FilterOption(label="A")
        assert o.value is None
        assert o.description is None
        assert o.selected is False

    def test_effective_value(self):
        assert FilterOption(label="A", value="a").effective_value() == "a"
        assert FilterOption(label="A").effective_value() == "A"


# ===================================================================
# 单选
# ===================================================================

class TestSingleSelect:
    def test_enter_submit_first(self):
        """筛选框回车切列表、列表回车提交第一项。"""
        r = _run_with_keys("\r\r", _mk(3))
        assert r.aborted is False
        assert r.selected == ["v0"]

    def test_move_and_submit(self):
        """↓ 进列表、再 ↓ 移动后 Enter 提交第二项。"""
        r = _run_with_keys("\x1b[B\x1b[B\r", _mk(3))
        assert r.selected == ["v1"]

    def test_space_selects_single(self):
        """单选模式 Space 选定当前项（等价 Enter）。"""
        r = _run_with_keys("\x1b[B\x1b[B ", _mk(3))
        assert r.selected == ["v1"]

    def test_value_fallback_to_label(self):
        opts = [FilterOption(label="Only")]
        r = _run_with_keys("\r\r", opts)
        assert r.selected == ["Only"]


# ===================================================================
# 多选
# ===================================================================

class TestMultiSelect:
    def test_space_toggles_and_enter_submits(self):
        """多选：进列表后 Space 勾选第一项、↓ Space 勾选第二项，Enter 提交。"""
        r = _run_with_keys("\x1b[B \x1b[B \r", _mk(3), multi=True)
        assert r.aborted is False
        assert r.selected == ["v0", "v1"]

    def test_space_untoggle(self):
        """再次 Space 取消勾选。"""
        r = _run_with_keys("\x1b[B  \r", _mk(3), multi=True)
        assert r.selected == []

    def test_initial_selected_state(self):
        """selected=True 的初始勾选项直接随 Enter 提交。"""
        opts = _mk(3)
        opts[1].selected = True
        # 筛选框回车切列表、列表回车提交
        r = _run_with_keys("\r\r", opts, multi=True)
        assert r.selected == ["v1"]

    def test_submit_keeps_unfocused_checked(self):
        """勾选不因光标移动而丢失。"""
        r = _run_with_keys("\x1b[B \x1b[B\x1b[B \x1b[B\x1b[B \r", _mk(3), multi=True)
        assert sorted(r.selected) == ["v0", "v1", "v2"]


# ===================================================================
# 过滤
# ===================================================================

class TestFilter:
    def test_keyword_filters_case_insensitive(self):
        """输入关键词后仅匹配项可选（不区分大小写）。"""
        opts = [
            FilterOption(label="Alpha", value="a"),
            FilterOption(label="Beta", value="b"),
            FilterOption(label="alphabet", value="c"),
        ]
        # 输入 alp 过滤出 Alpha/alphabet，↓ 进列表，Enter 选第一项
        r = _run_with_keys("alp\x1b[B\r", opts)
        assert r.selected == ["a"]
        assert r.keyword == "alp"

    def test_keyword_no_match(self):
        """无匹配时空态，Enter 不提交任何项（界面不退出）。"""
        opts = [FilterOption(label="Alpha"), FilterOption(label="Beta")]
        # zzz 无匹配 → Enter 无效（界面不退出）→ Ctrl-C 中止确认
        r = _run_with_keys("zzz\r\x03", opts)
        assert r.aborted is True
        assert r.selected == []
        # 清空关键词（backspace×3）后回车切列表、再回车提交第一项
        r2 = _run_with_keys("zzz\x7f\x7f\x7f\r\r", opts)
        assert r2.aborted is False
        assert r2.selected == ["Alpha"]

    def test_description_not_matched(self):
        """description 不参与匹配。"""
        opts = [
            FilterOption(label="A", description="xyz"),
            FilterOption(label="B"),
        ]
        # xyz 只出现在 description → label 均不匹配 → 空态（Enter 无效，
        # Ctrl-C 中止确认未选中任何项）
        r = _run_with_keys("xyz\x1b[B\r\x03", opts)
        assert r.aborted is True
        assert r.selected == []
        # 改用 b 过滤出 B：回车切列表、再回车提交
        r2 = _run_with_keys("b\r\r", opts)
        assert r2.selected == ["B"]

    def test_filter_resets_page_and_cursor(self):
        """关键词变化后页码回 1、光标回第一项。"""
        opts = _mk(12)  # page_size=5 → 3 页
        # 进列表翻到第 2 页（pgdn）→ ↑ 回筛选框 → 输入 1 过滤 → Enter
        # （焦点在列表时字符被丢弃，必须先回筛选框）
        r = _run_with_keys("\x1b[B\x1b[6~\x1b[A1\r\r", opts)
        # 过滤 "1" 匹配 opt1/opt10/opt11，Enter 选过滤后第一项 opt1
        assert r.selected == ["v1"]
        assert r.page == 1


# ===================================================================
# 分页
# ===================================================================

class TestPagination:
    def test_page_size_split(self):
        """超过 page_size 自动分页；↓ 移动不出当前页（页内环形）。"""
        opts = _mk(7)  # page_size=5 → 2 页
        # ↓×7：第 1 个进列表（v0），后续 6 次页内环形：v1 v2 v3 v4 v0 v1
        r = _run_with_keys("\x1b[B" * 7 + "\r", opts)
        assert r.selected == ["v1"]

    def test_pgdn_pgup(self):
        """pgdown 翻页后光标在页首；pgup 翻回。"""
        opts = _mk(12)  # 3 页
        # 进列表 → pgdn 到第 2 页 → Enter 选 v5（第 2 页首项）
        r = _run_with_keys("\x1b[B\x1b[6~\r", opts)
        assert r.selected == ["v5"]
        assert r.page == 2

    def test_left_right_in_list(self):
        """列表内 ←→ 翻页（筛选框内 ←→ 是移动光标）。"""
        opts = _mk(12)
        # 进列表 → → 翻到第 2 页 → Enter 选 v5
        r = _run_with_keys("\x1b[B\x1b[C\r", opts)
        assert r.selected == ["v5"]

    def test_last_page_partial(self):
        """末页不足 page_size：↓ 环形只在当页项内。"""
        opts = _mk(7)  # 2 页，末页 2 项
        # 进列表 → pgdn 到第 2 页 → ↓ 环形（2 项）→ Enter 选 v6
        r = _run_with_keys("\x1b[B\x1b[6~\x1b[B\r", opts)
        assert r.selected == ["v6"]

    def test_page_wrap(self):
        """末页 → 再翻回第 1 页（环形翻页）。"""
        opts = _mk(7)
        r = _run_with_keys("\x1b[B\x1b[6~\x1b[6~\r", opts)
        # 2 页：pgdn pgdn 回到第 1 页 → 选 v0
        assert r.selected == ["v0"]


# ===================================================================
# 焦点切换
# ===================================================================

class TestFocus:
    def test_down_from_keyword_enters_list(self):
        """筛选框 ↓ 进入列表首项。"""
        opts = _mk(3)
        r = _run_with_keys("\x1b[B\r", opts)
        assert r.selected == ["v0"]

    def test_up_from_first_option_returns_to_keyword(self):
        """列表首项 ↑ 回筛选框；再 ↓ 又进列表。"""
        opts = _mk(3)
        # ↓ 进列表 → ↑ 回筛选框 → ↓ 又进列表 → Enter
        r = _run_with_keys("\x1b[B\x1b[A\x1b[B\r", opts)
        assert r.selected == ["v0"]

    def test_tab_toggles_focus(self):
        """tab 在筛选框与列表间切换焦点。"""
        opts = _mk(3)
        # tab 进列表 → ↓ → Enter
        r = _run_with_keys("\t\x1b[B\r", opts)
        assert r.selected == ["v1"]

    def test_pgdn_in_keyword_box(self):
        """焦点在筛选框时 pgup/pgdown 也翻页（↓ 后仍停在筛选框外页码变化）。"""
        opts = _mk(12)
        # 筛选框内 pgdn 翻到第 2 页 → ↓ 进列表（页首）→ Enter 选 v5
        r = _run_with_keys("\x1b[6~\x1b[B\r", opts)
        assert r.selected == ["v5"]
        assert r.page == 2

    def test_left_right_in_keyword_box_no_page_turn(self):
        """筛选框内 ←→ 不翻页（移动输入光标），仅 pgup/pgdown 翻页。"""
        opts = _mk(12)
        # 筛选框内 → 移动光标不翻页：↓ 进列表仍在第 1 页 → Enter 选 v0
        r = _run_with_keys("\x1b[C\x1b[B\r", opts)
        assert r.selected == ["v0"]
        assert r.page == 1

    def test_arrow_moves_cursor_in_keyword_box(self):
        """筛选框内 ←→ 移动输入光标（← 把光标从词尾移回词首）。"""
        opts = _mk(12)
        # 输入 10 →（在末尾）← 回 1 与 0 之间，再输 2 → 关键词 120 → Enter 无匹配无效
        # 换用简单可断言：输入 1 →（在末尾）← → 光标回到开头，再输 2 → 21
        # 过滤 21 无匹配 → Enter 无效 → Ctrl-C 中止（keyword 为 "21"）
        r = _run_with_keys("1\x1b[D2\r\x03", opts)
        assert r.aborted is True
        assert r.keyword == "21"

    def test_keyword_typing_while_list_focused(self):
        """焦点在列表时输入字符 → 转回筛选框输入（可选实现：字符直接过滤）。
        简化：焦点在列表时字符键被忽略。
        """
        opts = [FilterOption(label="Alpha"), FilterOption(label="Beta")]
        # 进列表后输入 b —— 字符被忽略（仍全部选项），Enter 选 Alpha
        r = _run_with_keys("\x1b[Bb\r", opts)
        assert r.selected == ["Alpha"]


# ===================================================================
# 取消
# ===================================================================

class TestCancel:
    def test_ctrl_c_aborts(self):
        r = _run_with_keys("x\x03", _mk(3))
        assert r.aborted is True
        assert r.selected == []


# ===================================================================
# 状态持久化
# ===================================================================

class TestStatePersistence:
    def test_keyword_restore(self):
        """keyword 入参：初始即过滤。"""
        opts = _mk(12)
        r = _run_with_keys("\x1b[B\r", opts, keyword="1")
        assert r.selected == ["v1"]

    def test_cursor_index_restore(self):
        """cursor_index 入参：初始光标在过滤后列表指定项（进列表保留）。"""
        opts = _mk(5)
        r = _run_with_keys("\x1b[B\r", opts, cursor_index=2)
        assert r.selected == ["v2"]

    def test_keyword_buffer_shared_between_calls(self):
        """keyword_buffer 注入：关键词文本与光标跨调用保留。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from mycode.filter_ui import filter_ui
        buf = Buffer()
        opts = _mk(12)
        with create_pipe_input() as inp:
            inp.send_text("1\x1b[B\r")  # 输入 1 → ↓ 进列表 → Enter
            r = filter_ui(opts, page_size=5, keyword_buffer=buf,
                          input=inp, output=DummyOutput())
        assert r.selected == ["v1"]
        assert buf.text == "1"
        # 第二次调用注入同一 buffer：无需再输入关键词即已过滤
        with create_pipe_input() as inp:
            inp.send_text("\x1b[B\r")
            r2 = filter_ui(opts, page_size=5, keyword_buffer=buf,
                           input=inp, output=DummyOutput())
        assert r2.selected == ["v1"]
        assert r2.keyword == "1"


# ===================================================================
# 布局
# ===================================================================

class TestLayout:
    def test_layout_children_structure(self):
        """布局：标题 → 空行 → 描述 → 空行 → 筛选框 → 空行 → 选项行 →
        分页状态行 → 空行 → 提示行。"""
        from mycode.filter_ui import _FilterState, _build_layout
        from prompt_toolkit.layout.containers import HSplit, Window, VSplit
        state = _FilterState(
            _mk(3), title="T", description="D", page_size=5,
        )
        layout = _build_layout(state)
        children = layout.children
        assert isinstance(children[0], Window)   # 标题
        assert isinstance(children[1], Window)   # 标题与描述间空行
        assert isinstance(children[2], Window)   # 描述
        assert isinstance(children[3], Window)   # header 与筛选框间空行
        # 之后：筛选行（VSplit）… 至少存在一个 VSplit（筛选框）
        vsplits = [c for c in children if isinstance(c, VSplit)]
        assert vsplits

    def test_layout_description_without_title(self):
        """只有描述无标题：无标题行与中间空行，直接描述 + 空行 + 筛选框。"""
        from mycode.filter_ui import _FilterState, _build_layout
        from prompt_toolkit.layout.containers import Window, VSplit
        state = _FilterState(_mk(3), description="D", page_size=5)
        children = _build_layout(state).children
        assert isinstance(children[0], Window)   # 描述
        assert isinstance(children[1], Window)   # header 与筛选框间空行
        # 筛选框行为 VSplit
        assert isinstance(children[2], VSplit)

    def test_layout_title_without_description(self):
        """只有标题无描述：标题 + 空行 + 筛选框（无标题与描述间空行）。"""
        from mycode.filter_ui import _FilterState, _build_layout
        from prompt_toolkit.layout.containers import Window, VSplit
        state = _FilterState(_mk(3), title="T", page_size=5)
        children = _build_layout(state).children
        assert isinstance(children[0], Window)   # 标题
        assert isinstance(children[1], Window)   # header 与筛选框间空行
        assert isinstance(children[2], VSplit)   # 筛选框行

    def test_layout_no_header(self):
        """无标题与描述：无 header 行，首 child 即筛选框行。"""
        from mycode.filter_ui import _FilterState, _build_layout
        from prompt_toolkit.layout.containers import VSplit
        state = _FilterState(_mk(3), page_size=5)
        children = _build_layout(state).children
        assert isinstance(children[0], VSplit)   # 筛选框行

    def test_hint_line_text(self):
        """底部提示行包含 PgUp/PgDn 翻页。"""
        from mycode.filter_ui import _HINT_LINE
        assert "PgUp" in _HINT_LINE
        assert "PgDn" in _HINT_LINE

    def test_page_status_line(self):
        """分页状态行文本：第 x/y 页 · 共 n 项。"""
        from mycode.filter_ui import _FilterState, _build_layout
        from prompt_toolkit.layout.containers import Window
        state = _FilterState(_mk(12), page_size=5)
        text = state.page_status()
        assert text == "第 1/3 页 · 共 12 项"

    def test_filtered_count_status(self):
        """过滤后状态行反映过滤数量。"""
        from mycode.filter_ui import _FilterState
        state = _FilterState(_mk(12), page_size=5)
        state.keyword_buffer.insert_text("1")
        state.apply_filter()
        assert state.page_status() == "第 1/1 页 · 共 3 项"  # 1/10/11

    def test_option_prefix_single(self):
        """单选前缀：当前行 ❯ 🟢（default）/> （classic）。"""
        from mycode.filter_ui import _FilterState, _mark_str
        state = _FilterState(_mk(3))
        assert _mark_str(state, active=True, checked=False) .startswith("❯")

    def test_option_prefix_multi(self):
        """多选前缀：勾选 ❯ ✅ / 未勾 ❯ 🔳（default）。"""
        from mycode.filter_ui import _FilterState, _mark_str
        state = _FilterState(_mk(3), multi=True)
        assert _mark_str(state, active=True, checked=True).startswith("❯ ✅")
        assert _mark_str(state, active=False, checked=False).startswith("  🔳")
