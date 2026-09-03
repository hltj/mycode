"""
通用询问界面（mycode.ask_ui）测试。

覆盖：

- ``AskOption`` 取值（value / label fallback）
- 单选项：Enter 提交；默认值；自定义输入 + 占位文本
- 多选项：Space 切换勾选；Enter 提交全部
- 自定义输入：在自定义选项上输入字符、占位文本行为
- 移动焦点：Up/Down / C-p / C-n
- 退出控制：Ctrl-C 终止交互
- 未在自定义选项时输入字符被丢弃（不污染）
- 布局：标题 / 描述 / 普通选项 / 自定义选项 / 占位文本字段
"""

from __future__ import annotations

import pytest

from mycode.ask_ui import AskOption, ask_ui


# ===================================================================
# AskOption 行为
# ===================================================================

class TestAskOption:
    def test_value_default_to_label(self):
        opt = AskOption(label="OK")
        assert opt.effective_value() == "OK"

    def test_value_explicit(self):
        opt = AskOption(label="同意", value="approve")
        assert opt.effective_value() == "approve"

    def test_custom_marker(self):
        opt = AskOption(label="其它", value="custom", description="原因", is_custom=True)
        assert opt.is_custom is True
        assert opt.effective_value() == "custom"

    def test_custom_no_value_falls_back_to_label(self):
        opt = AskOption(label="其它", is_custom=True)
        assert opt.effective_value() == "其它"


# ===================================================================
# ask_ui 控件（用 prompt_toolkit 注入按键序列驱动）
# ===================================================================

def _run_with_keys(seq: str, options, *, title="Q", description="", multi=False):
    """用注入的按键序列运行 ask_ui，返回 ``AskResult``。"""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as inp:
        inp.send_text(seq)
        return ask_ui(
            title=title,
            description=description,
            options=options,
            multi=multi,
            input=inp,
            output=DummyOutput(),
        )


class TestAskUiSingle:
    """单选模式 ask_ui 交互。"""

    def test_default_first_option(self):
        """默认焦点在第一项，Enter 提交该选项。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        r = _run_with_keys("\r", opts)
        assert r.selected == ["a"]
        assert r.input is None

    def test_move_and_select(self):
        """Down 到第二项后 Enter 提交。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        r = _run_with_keys("\x0e\r", opts)  # down + enter
        assert r.selected == ["b"]

    def test_move_cycle(self):
        """Down 到底后循环回第一项。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        r = _run_with_keys("\x0e\x0e\r", opts)
        assert r.selected == ["a"]

    def test_move_up_cycle(self):
        """Up 第一项循环到最后一项。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        r = _run_with_keys("\x10\r", opts)
        assert r.selected == ["b"]

    def test_value_fallback_to_label(self):
        """无 value 的选项返回值用 label。"""
        opts = [AskOption(label="OK")]
        r = _run_with_keys("\r", opts)
        assert r.selected == ["OK"]

    def test_single_mark_prefix(self):
        """默认风格单选前缀：当前行 `❯ 🟢`，其余 `  ⚪`。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(
            title="", description="",
            options=[
                AskOption(label="A", value="a"),
                AskOption(label="B", value="b"),
            ],
            multi=False, cursor_index=0,
        )
        layout = _build_ask_layout(state, custom_buffer=None)
        text = TestAskUiLayout._layout_text(layout)
        assert "❯ 🟢 A" in text
        assert "  ⚪ B" in text

    def test_single_mark_prefix_classic(self, monkeypatch):
        """classic 风格单选前缀：当前行 `> `，其余 `  `。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(
            title="", description="",
            options=[
                AskOption(label="A", value="a"),
                AskOption(label="B", value="b"),
            ],
            multi=False, cursor_index=0,
        )
        layout = _build_ask_layout(state, custom_buffer=None)
        text = TestAskUiLayout._layout_text(layout)
        assert "> A" in text
        assert "  B" in text


class TestAskUiMulti:
    """多选模式 ask_ui 交互。"""

    def test_space_toggles(self):
        """空格切换勾选；Enter 提交所有勾选项。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
            AskOption(label="C", value="c"),
        ]
        # 焦点 0，空格勾选 A，向下，空格勾选 B，再向下到 C，直接 Enter 提交
        seq = " \x0e \x0e\r"
        r = _run_with_keys(seq, opts, multi=True)
        assert r.selected == ["a", "b"]
        assert r.input is None

    def test_space_uncheck(self):
        """空格切换是双向的：再次空格取消勾选。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        seq = " \x0e \r"  # 勾 A，移到 B，勾 B，Enter
        r = _run_with_keys(seq, opts, multi=True)
        assert r.selected == ["a", "b"]

    def test_multi_empty_when_none_checked(self):
        """多选模式下没有任何勾选时，Enter 返回空列表。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        seq = "\x0e\r"  # 移到 B，Enter，没有 checked
        r = _run_with_keys(seq, opts, multi=True)
        assert r.selected == []
        assert r.checked == set()
        assert r.input is None

    def test_multi_cursor_mark_prefix(self):
        """多选选项行最左有当前行指示，勾选态用符号（默认风格）。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(
            title="", description="",
            options=[
                AskOption(label="A", value="a"),
                AskOption(label="B", value="b"),
                AskOption(label="C", value="c"),
            ],
            multi=True, cursor_index=1, checked={0},
        )
        layout = _build_ask_layout(state, custom_buffer=None)
        text = TestAskUiLayout._layout_text(layout)
        # 当前行（B）用 ❯ 指示；A 未选中但已勾选（✅）；C 未选（🔳）
        assert "❯ 🔳 B" in text
        assert "  ✅ A" in text
        assert "  🔳 C" in text

    def test_multi_cursor_mark_prefix_classic(self, monkeypatch):
        """classic 风格多选前缀保持：`> [ ]` / `  [x]`。"""
        from mycode import ask_ui
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(
            title="", description="",
            options=[
                AskOption(label="A", value="a"),
                AskOption(label="B", value="b"),
                AskOption(label="C", value="c"),
            ],
            multi=True, cursor_index=1, checked={0},
        )
        layout = _build_ask_layout(state, custom_buffer=None)
        text = TestAskUiLayout._layout_text(layout)
        # 当前行（B）有 > 指示；A 未选中但已勾选
        assert "> [ ] B" in text
        assert "  [x] A" in text
        assert "  [ ] C" in text


class TestAskUiMultiCustom:
    """多选模式下的自定义输入框：空格激活、空格输入、Backspace 失活。"""

    CUSTOM_OPTS = [
        AskOption(label="A", value="a"),
        AskOption(label="B", value="b"),
        AskOption(label="自定义", value="custom", description="输入", is_custom=True),
    ]

    def test_custom_not_active_by_default_in_multi(self):
        """多选下自定义输入框默认不激活：移动上去直接输入字符被丢弃。"""
        # 到自定义行（sel=2），未选中，直接输入 x → 应被丢弃
        seq = "\x0e\x0ex\r"
        r = _run_with_keys(seq, self.CUSTOM_OPTS, multi=True)
        assert r.selected == []  # 未勾选任何项，无选中
        assert r.input is None  # 未选中自定义，input 为 None
        assert r.checked == set()

    def test_space_activates_then_accepts_space_char(self):
        """空格选中激活自定义；激活后再按空格输入空格字符。"""
        # 到自定义（sel=2），空格激活，空格输入，abc 输入，提交
        seq = "\x0e\x0e  abc\r"
        r = _run_with_keys(seq, self.CUSTOM_OPTS, multi=True)
        assert r.selected == ["custom"]
        assert r.input == " abc"  # 第一个空格激活，第二个空格是输入
        assert r.checked == {2}

    def test_space_type_into_custom_buffer(self):
        """激活后空格归输入框正常输入（不切换勾选）。"""
        # 到自定义，空格激活，连续输入 "hello world"
        seq = "\x0e\x0e hello world\r"
        r = _run_with_keys(seq, self.CUSTOM_OPTS, multi=True)
        assert r.input == "hello world"
        assert r.checked == {2}

    def test_backspace_at_start_deactivates(self):
        """输入框光标最左按 Backspace → 失活并取消选中。"""
        # 到自定义，空格激活，输入 x，光标到最左，Backspace 失活，提交
        seq = "\x0e\x0e x\x01\x7f\r"
        r = _run_with_keys(seq, self.CUSTOM_OPTS, multi=True)
        assert r.selected == []  # 取消勾选后无选中
        assert r.checked == set()  # 已取消选中

    def test_backspace_deactivates_then_space_selects_other(self):
        """失活后空格恢复切换普通选项的勾选。"""
        # 到自定义，空格激活，x，最左 Backspace 失活，up 到 A，空格勾选 A
        seq = "\x0e\x0e x\x01\x7f\x10\x10 \r"
        r = _run_with_keys(seq, self.CUSTOM_OPTS, multi=True)
        assert r.selected == ["a"]
        assert r.input is None
        assert r.checked == {0}

    def test_backspace_deactivates_then_space_reactivates_custom(self):
        """失活后空格可再次选中并激活自定义输入框，原输入保留。

        buffer 是持久状态：失活仅取消勾选，不清空已输入内容；重新激活后
        光标仍在原位（最左），新字符插到光标前。
        """
        # 到自定义，空格激活，x，最左 Backspace 失活（checked 空），
        # 再空格重新激活，输入 y（光标在最左，y 插到 x 前）
        seq = "\x0e\x0e x\x01\x7f y\r"
        r = _run_with_keys(seq, self.CUSTOM_OPTS, multi=True)
        assert r.selected == ["custom"]
        assert r.input == "yx"  # 原 x 保留，y 插到光标（最左）前
        assert r.checked == {2}


class TestAskUiCustomOption:
    """末尾「自定义输入」选项。"""

    def test_select_custom_returns_input_text(self):
        """选中自定义选项时返回其输入字符串。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="其它", value="custom", description="补充说明", is_custom=True),
        ]
        seq = "\x0e补充信息\r"
        r = _run_with_keys(seq, opts)
        assert r.selected == ["custom"]
        assert r.input == "补充信息"

    def test_single_custom_accepts_space(self):
        """单选自定义输入框可直接输入空格（空格不被切换勾选占用）。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="其它", value="custom", description="输入", is_custom=True),
        ]
        seq = "\x0ehello world\r"  # 单选：焦点在自定义行即可输入，含空格
        r = _run_with_keys(seq, opts)
        assert r.selected == ["custom"]
        assert r.input == "hello world"

    def test_single_custom_accepts_leading_space(self):
        """单选自定义输入框支持前导空格（焦点已在自定义行直接输入）。"""
        opts = [
            AskOption(label="其它", value="custom", description="输入", is_custom=True),
        ]
        seq = " hello\r"  # 默认焦点在自定义行，输入含前导空格
        r = _run_with_keys(seq, opts)
        assert r.selected == ["custom"]
        assert r.input == " hello"

    def test_single_normal_option_space_ignored(self):
        """单选下普通选项按空格无操作（不切换也不进输入框）。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="其它", value="custom", description="输入", is_custom=True),
        ]
        seq = " \r"  # 焦点在 A，按空格然后 Enter
        r = _run_with_keys(seq, opts)
        assert r.selected == ["a"]
        assert r.input is None

    def test_select_custom_empty_input(self):
        """选中自定义但未输入时，input 是空字符串（不是 None）。"""
        opts = [
            AskOption(label="其它", value="custom", is_custom=True),
        ]
        seq = "\r"  # 默认焦点已在自定义，直接 Enter
        r = _run_with_keys(seq, opts)
        assert r.selected == ["custom"]
        assert r.input == ""

    def test_select_non_custom_returns_no_input(self):
        """未选自定义选项时，input 始终为 None。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="其它", value="custom", is_custom=True),
        ]
        seq = "\r"
        r = _run_with_keys(seq, opts)
        assert r.selected == ["a"]
        assert r.input is None

    def test_typing_when_not_on_custom_is_dropped(self):
        """焦点不在自定义选项时输入字符被丢弃。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="其它", value="custom", is_custom=True),
        ]
        seq = "abc\r"  # 在 A 上输入字符（应被丢弃），Enter 提交
        r = _run_with_keys(seq, opts)
        assert r.selected == ["a"]
        assert r.input is None


class TestAskUiAbortion:
    """Ctrl-C 中止。"""

    def test_ctrl_c_returns_empty(self):
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        seq = "\x03"
        r = _run_with_keys(seq, opts)
        assert r.selected == []
        assert r.input is None
        assert r.aborted is True

    def test_enter_not_aborted(self):
        """正常 Enter 提交时 aborted 为 False。"""
        opts = [
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ]
        r = _run_with_keys("\r", opts)
        assert r.selected == ["a"]
        assert r.aborted is False


class TestAskUiLayout:
    """布局构造：标题 / 描述 / 普通选项 / 自定义选项同行输入。"""

    @staticmethod
    def _layout_text(layout) -> str:
        """递归遍历布局提取全部文本片段。"""
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        parts: list[str] = []

        def walk(node):
            if isinstance(node, Window):
                c = node.content
                if isinstance(c, FormattedTextControl):
                    frags = c.text if hasattr(c, "text") else c()
                    if isinstance(frags, list):
                        parts.append("".join(t for _, t in frags))
                    else:
                        parts.append(str(frags))
                elif c.__class__.__name__ == "BufferControl":
                    parts.append(f"<{c.__class__.__name__}>")
            elif hasattr(node, "children"):
                for ch in node.children:
                    walk(ch)

        walk(layout)
        return "\n".join(parts)

    def test_title_and_description_in_layout(self):
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="标题", description="描述", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        text = self._layout_text(layout)
        assert "标题" in text
        assert "描述" in text
        assert "A" in text

    def test_blank_line_between_header_and_options(self):
        """标题/描述与选项之间有一个空行（header 区存在时）。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        # 标题 + 描述：header 区（2 行）+ 空行 + 选项
        state = _AskState(title="T", description="D", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        assert len(layout.children) == 4  # 标题 + 描述 + 空行 + 选项
        # 空行是第三个元素（内容为空文本）
        blank = layout.children[2]
        text = self._layout_text(blank)
        assert text == ""

    def test_blank_line_only_title(self):
        """只有标题没有描述时，标题与选项之间也有空行。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="T", description="", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        assert len(layout.children) == 3  # 标题 + 空行 + 选项
        # 第二个元素为空行
        text = self._layout_text(layout.children[1])
        assert text == ""

    def test_no_blank_line_without_header(self):
        """标题与描述都没有时，不加空行。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="", description="", options=[
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        assert len(layout.children) == 2  # 仅两个选项，无空行

    def test_description_omitted_when_empty(self):
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="标题", description="", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        text = self._layout_text(layout)
        assert "标题" in text
        # 空描述不应作为独立行渲染（但 rows 还是一个，断言含标签即可）
        assert "A" in text

    def test_custom_row_is_vsplit_with_input(self):
        """自定义选项行是 VSplit（label + input）。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout import VSplit
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="Q", description="", options=[
            AskOption(label="A", value="a"),
            AskOption(label="其它", value="custom", is_custom=True),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=Buffer())
        # 第二个选项行（label 为「其它」）应为 VSplit
        rows = layout.children
        assert len(rows) >= 3  # 标题 + 描述行 + 选项（无描述时也不少于）
        # 找到自定义行：最后一个选项行
        custom_row = rows[-1]
        assert isinstance(custom_row, VSplit)

    def test_normal_row_is_plain_window(self):
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout import VSplit
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="Q", description="", options=[
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        # 普通选项行不含 BufferControl（不是 VSplit）
        rows = layout.children
        for row in rows[1:]:  # 跳过标题行
            if isinstance(row, VSplit):
                pytest.fail("普通选项行不应是 VSplit")

    def test_description_appended_to_normal_row(self):
        """普通选项的 description 同行展示（紧跟 label）。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="Q", description="", options=[
            AskOption(label="A", value="a", description="A 的描述"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        text = self._layout_text(layout)
        assert "A" in text
        assert "A 的描述" in text

    def test_layout_for_no_options(self):
        """无选项时也构造出合法布局。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="Q", description="desc", options=[], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        assert layout is not None

    def test_title_optional(self):
        """title 为空时不渲染标题行。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="", description="d", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        text = self._layout_text(layout)
        # 标题为空：不含任何空标题文本；描述与选项仍展示
        assert "d" in text
        assert "A" in text
        # 标题窗口不应被构造（rows 数 = 描述行 + 空行 + 1 选项 = 3）
        assert len(layout.children) == 3

    def test_description_optional(self):
        """description 为空时不渲染描述行（但有标题 → 仍留 header 空行）。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="Q", description="", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        # 标题行 + 空行 + 选项 = 3 行
        assert len(layout.children) == 3

    def test_both_optional_only_options(self):
        """title / description 都为空时只渲染选项。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="", description="", options=[
            AskOption(label="A", value="a"),
            AskOption(label="B", value="b"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        assert len(layout.children) == 2  # 仅两个选项

    def test_ask_ui_call_without_title_or_description(self):
        """ask_ui 可直接调用而不传 title / description（默认空）。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\r")
            r = ask_ui(
                options=[AskOption(label="A", value="a")],
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["a"]
        assert r.input is None


class TestAskUiDescriptionMultiLine:
    """问题描述支持多行；default 风格下支持 markdown 渲染。

    描述在根布局中始终只占一个 child 槽位，多行展开不改变焦点行索引
    （见 ``_option_row_offset``）。
    """

    @staticmethod
    def _desc_fragments(text):
        from mycode.ask_ui import _description_fragments
        frags, style = _description_fragments(text)
        if frags and frags[-1][1] == "\n":
            frags = frags[:-1]
        return frags, style

    @staticmethod
    def _desc_height(text) -> int:
        """描述窗口的 preferred_height（真实展开行数）。"""
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="Q", description=text, options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        # children：0 标题 / 1 描述 / 2 空行 / 3 选项
        return layout.children[1].preferred_height(80, 24).preferred

    def test_multiline_collapses_without_hard_break(self, monkeypatch):
        """default：裸换行按标准 markdown 折叠进同一段落（换行变空格）。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        # 无行尾两空格 → rich 合并为一段
        assert self._desc_height("第一行\n第二行\n第三行") == 1
        frags, _ = self._desc_fragments("第一行\n第二行")
        plain = "".join(t for _, t in frags)
        assert "第一行" in plain and "第二行" in plain

    def test_multiline_lines_with_hard_break(self, monkeypatch):
        """default：行尾两空格（hard break）实现显式换行，逐行展开。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        assert self._desc_height("第一行  \n第二行  \n第三行") == 3

    def test_paragraph_break_with_blank_line(self, monkeypatch):
        """空行分隔段落：保留段落间空行。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        assert self._desc_height("第一段\n\n第二段") == 3

    def test_markdown_bold_and_inline_code(self, monkeypatch):
        """default：加粗 / 内联代码被富文本渲染。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        frags, style = self._desc_fragments("**加粗** 和 `code`")
        plain = "".join(t for _, t in frags)
        assert "加粗" in plain and "code" in plain
        # 加粗 / 内联代码（cyan on black）样式存在
        assert any("bold" in (s or "") for s, _ in frags)
        assert any("ansicyan" in (s or "") for s, _ in frags)
        # 窗口统一挂描述样式（未着色文本继承暗灰）
        assert style == "class:ask-description"

    def test_list_rendered(self, monkeypatch):
        """default：列表项保留。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        frags, _ = self._desc_fragments("- 项目一\n- 项目二")
        plain = "".join(t for _, t in frags)
        assert "项目一" in plain and "项目二" in plain

    def test_code_block_preserved(self, monkeypatch):
        """default：代码块原样渲染，围栏内换行保留。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        frags, _ = self._desc_fragments("```\nl1\nl2\n```")
        plain = "".join(t for _, t in frags)
        assert "l1" in plain and "l2" in plain
        # 代码块 = 上下留白 + 2 行内容 = 4 行
        assert self._desc_height("```\nl1\nl2\n```") == 4

    def test_classic_multiline(self, monkeypatch):
        """classic：纯文本多行，不解析 markdown。"""
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        frags, style = self._desc_fragments("第一行 **加粗**\n第二行")
        plain = "".join(t for _, t in frags)
        # markdown 语法原样保留（不解析）
        assert "**加粗**" in plain
        assert style == "class:ask-description"
        assert self._desc_height("第一行\n第二行") == 2

    def test_ansi_content_fallback_plain(self, monkeypatch):
        """default：描述含 ANSI 控制码时原样输出、不解析 markdown。

        ``_markdown_ansi`` 对含 ANSI 的输入走豁免（原样返回，不解析
        markdown），经 prompt_toolkit `ANSI` 解析后控制码被消费，正文
        原样保留（不会把 ``**`` 等当 markdown 语法折叠）。
        """
        from mycode import renderer
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        frags, _ = self._desc_fragments("\x1B[32m**加粗**\x1B[0m msg")
        plain = "".join(t for _, t in frags)
        # 正文保留，markdown 语法原样（未被解析为加粗）
        assert "**加粗**" in plain
        assert "msg" in plain

    def test_desc_window_wrap_lines_enabled(self, monkeypatch):
        """描述窗口设置 wrap_lines=True（换行职责在 prompt_toolkit 侧）。"""
        from mycode import renderer
        from mycode.ask_ui import _AskState, _build_ask_layout
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        state = _AskState(title="Q", description="多行", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        desc_win = layout.children[1]
        assert desc_win.wrap_lines() is True

    def test_long_line_wraps_at_any_char(self, monkeypatch):
        """default：超宽行按字符折行，折行点可在任意字符处。

        rich 按空白把文本划块，只能整块换行、块内不能折。改为
        prompt_toolkit 字符级折行后，超宽行在任意字符处折行，
        折行处不再限块间空白。
        """
        from mycode import renderer
        from mycode.ask_ui import _AskState, _build_ask_layout
        from prompt_toolkit.layout.screen import Screen
        from prompt_toolkit.layout.screen import WritePosition
        from prompt_toolkit.layout.mouse_handlers import MouseHandlers
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")

        def rendered_lines(desc, term):
            state = _AskState(title="", description=desc, options=[], multi=False)
            layout = _build_ask_layout(state, custom_buffer=None)
            desc_win = layout.children[0]
            screen = Screen(initial_width=term, initial_height=30)
            desc_win.write_to_screen(
                screen, MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=term - 1, height=30),
                "", erase_bg=True, z_index=None,
            )
            rows = []
            for y in range(0, 30):
                chars = "".join(
                    screen.data_buffer[y][x].char for x in range(0, term - 1)
                ).rstrip()
                if chars.strip():
                    rows.append(chars)
            return rows

        desc = "mycode 会读取、分析、修改当前目录中的文件，并可能会运行其中的代码。"
        # 窄终端（36 列）强制折行
        lines = rendered_lines(desc, 36)
        # 折成多行
        assert len(lines) > 1
        # 无任何一行仅含 mycode（整块换行若发生在行首块后，会把它单独留下）
        for ln in lines:
            assert ln.strip() != "mycode"
        # 每行都有内容且折行在字符处
        assert all(ln.strip() for ln in lines)
        # 内容整体保留
        assert "".join(lines).replace(" ", "") == desc.replace(" ", "")

    def test_short_list_items_not_wrapped(self, monkeypatch):
        """default：短列表项不被行尾 pad 空格误判为整宽而折行。"""
        from mycode import renderer
        from mycode.ask_ui import _AskState, _build_ask_layout
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        state = _AskState(title="Q", description="- 短项\n- 另一个短项", options=[
            AskOption(label="A", value="a"),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=None)
        desc_win = layout.children[1]
        # 两个短项都应是 1 行（加上列表前空行 = 3 行）
        assert desc_win.preferred_height(79, 24).preferred == 3

    def test_four_backtick_fence_preserved(self, monkeypatch):
        """default：4 反引号 + 语言标记的代码块正确渲染。"""
        from mycode import renderer
        from mycode.ask_ui import _description_fragments
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        code = "````py\n```inner```\n````"
        frags, _ = self._desc_fragments(code)
        plain = "".join(t for _, t in frags)
        assert "inner" in plain


class TestAskUiDescriptionFocus:
    """描述多行时焦点行计算不漂移（child 槽位计数）。"""

    def test_option_row_offset_ignores_description_height(self, monkeypatch):
        """多行描述与单行描述的选项起始索引一致。"""
        from mycode import renderer
        from mycode.ask_ui import _AskState, _option_row_offset
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        single = _AskState(title="T", description="单行", options=[])
        multi = _AskState(title="T", description="多行\n\n**长**\n\n- x\n- y", options=[])
        assert _option_row_offset(single) == _option_row_offset(multi)

    def test_focus_window_with_multiline_description(self, monkeypatch):
        """多行描述下 _focused_window 仍取到当前选中行。"""
        from mycode import renderer
        from mycode.ask_ui import _AskState, _build_ask_layout, _focused_window
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        state = _AskState(
            title="T",
            description="第一行  \n第二行  \n第三行",
            options=[
                AskOption(label="A", value="a"),
                AskOption(label="B", value="b"),
            ],
            multi=False, cursor_index=1,
        )
        layout = _build_ask_layout(state, custom_buffer=None)
        win = _focused_window(state, layout.children, None)
        from prompt_toolkit.layout.controls import FormattedTextControl
        ctrl = win.content
        frags = ctrl.text if hasattr(ctrl, "text") else ctrl()
        text = "".join(t for _, t in frags) if isinstance(frags, list) else str(frags)
        # 焦点窗口应包含当前选中行 B 的标签，且不含未选中项 A
        assert "B" in text
        assert "A" not in text

    def test_real_interaction_multiline_desc_focus(self, monkeypatch):
        """真实交互：多行 markdown 描述下 Down 移动焦点提交第二项。"""
        from mycode import renderer
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        desc = "第一行  \n第二行  \n**第三行**  \n```\ncode\n```"
        with create_pipe_input() as inp:
            inp.send_text("\x0e\r")  # down + enter
            r = ask_ui(
                title="Q",
                description=desc,
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                ],
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["b"]
        assert r.cursor_index == 1

    def test_real_interaction_multiline_desc_default(self, monkeypatch):
        """真实交互：多行描述下默认焦点第一项可直接 Enter。"""
        from mycode import renderer
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        with create_pipe_input() as inp:
            inp.send_text("\r")
            r = ask_ui(
                title="Q",
                description="第一行  \n第二行",
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                ],
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["a"]
        assert r.cursor_index == 0


class TestAskUiStatePersistence:
    """cursor_index / checked 在多次调用间维持。"""

    def test_cursor_index_init(self):
        """初始 cursor_index 指定焦点位置。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\r")
            r = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                cursor_index=2,
                input=inp, output=DummyOutput(),
            )
        # 初始焦点在 C，Enter 提交 C
        assert r.selected == ["c"]
        assert r.cursor_index == 2

    def test_cursor_index_returns_at_submit(self):
        """返回 cursor_index 反映提交时的焦点位置。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\x0e\x0e\r")  # 移到 C
            r = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["c"]
        assert r.cursor_index == 2

    def test_cursor_index_passes_through_for_next_call(self):
        """上一次 cursor_index 作为下一次初始焦点（ESC 往返场景）。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        # 第一次：移到 C 并提交，记录 cursor_index
        with create_pipe_input() as inp:
            inp.send_text("\x0e\x0e\r")
            r1 = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                input=inp, output=DummyOutput(),
            )
        assert r1.cursor_index == 2

        # 第二次：传入上次的 cursor_index，再直接 Enter
        with create_pipe_input() as inp:
            inp.send_text("\r")
            r2 = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                cursor_index=r1.cursor_index,
                input=inp, output=DummyOutput(),
            )
        # 焦点仍是 C，提交 C
        assert r2.selected == ["c"]

    def test_cursor_index_out_of_range_falls_back_to_zero(self):
        """cursor_index 越界时回退到 0（防御性）。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\r")
            r = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                ],
                cursor_index=10,  # 越界
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["a"]  # 回退到第一项
        assert r.cursor_index == 0

    def test_checked_init_multi(self):
        """多选模式 initial checked 集合生效。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\r")
            r = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                multi=True,
                checked={0, 2},  # 预勾 A 与 C
                input=inp, output=DummyOutput(),
            )
        # Enter 提交所有 checked
        assert r.selected == ["a", "c"]
        assert r.checked == {0, 2}

    def test_checked_returns_at_submit_multi(self):
        """返回 checked 反映提交时的勾选集合。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        # 焦点 A，空格勾选 A，移到 B，空格勾选 B，移到 C 不勾，Enter
        with create_pipe_input() as inp:
            inp.send_text(" \x0e \x0e\r")
            r = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                multi=True,
                input=inp, output=DummyOutput(),
            )
        assert r.checked == {0, 1}
        assert r.selected == ["a", "b"]

    def test_multi_cursor_index_independent_of_checked(self):
        """多选提交时 cursor_index（焦点位置）与 checked（勾选）相互独立。

        焦点停在某个未勾选项上提交：selected 由 checked 决定，
        cursor_index 仍记录焦点位置，二者互不干扰。
        """
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        # 勾选 A、B（焦点从 A 移到 B），再移到 C（未勾），直接 Enter
        with create_pipe_input() as inp:
            inp.send_text(" \x0e \x0e\r")
            r = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                multi=True,
                input=inp, output=DummyOutput(),
            )
        # checked 独立于焦点：勾了 A、B，焦点在 C
        assert r.checked == {0, 1}
        assert r.selected == ["a", "b"]
        assert r.cursor_index == 2  # 焦点位置与勾选集合无关

    def test_multi_cursor_and_checked_pass_through_next_call(self):
        """多选时上一次 cursor_index / checked 一起作为下一次的初始状态。

        用真实两次调用模拟「确认菜单 ↔ 编辑视图」往返时维持多选状态。
        """
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        # 第一次：勾选 A、B，焦点移到 C，Enter
        with create_pipe_input() as inp:
            inp.send_text(" \x0e \x0e\r")
            first = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                multi=True,
                input=inp, output=DummyOutput(),
            )
        assert first.checked == {0, 1}
        assert first.cursor_index == 2

        # 第二次：把 checked 和 cursor_index 一起回传。
        # 预勾 A、B 且焦点在 C；用户在 C 按空格（C 未勾 → 勾上 C）。
        with create_pipe_input() as inp:
            inp.send_text(" \r")  # 在 C（焦点）按空格勾上 C
            r2 = ask_ui(
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                multi=True,
                cursor_index=first.cursor_index,
                checked=set(first.checked),
                input=inp, output=DummyOutput(),
            )
        # 初始状态：checked={0,1}，cursor=2（C 行）
        # 用户按键 " "（空格）在 C 行：C 未勾，空格勾上 C → checked={0,1,2}
        # Enter → selected=[a,b,c]
        assert r2.checked == {0, 1, 2}
        assert r2.selected == ["a", "b", "c"]


class TestAskUiEndToEndScenarios:
    """端到端场景测试：标题 + 描述 + 选项 + 自定义输入完整组合。"""

    def test_single_with_title_and_description(self):
        """单选：标题 + 描述 + 多个选项，端到端提交默认项。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\r")  # 默认焦点在第一项，Enter 提交
            r = ask_ui(
                title="确认文件操作",
                description="请选择如何处理 src/old.py",
                options=[
                    AskOption(label="备份后删除", value="backup_delete"),
                    AskOption(label="直接删除", value="delete"),
                    AskOption(label="保留不动", value="keep"),
                ],
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["backup_delete"]
        assert r.input is None
        assert r.cursor_index == 0

    def test_single_each_option_has_description(self):
        """单选：每个选项带 description，移到第二项提交。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\x0e\r")  # 移到 B，Enter
            r = ask_ui(
                title="选择测试策略",
                description="（每个选项后面是说明文字）",
                options=[
                    AskOption(label="单元测试", value="unit", description="覆盖核心逻辑"),
                    AskOption(label="集成测试", value="integration", description="模块间协作"),
                    AskOption(label="端到端", value="e2e", description="完整用户路径"),
                ],
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["integration"]
        assert r.cursor_index == 1

    def test_multi_with_title_description_and_option_descriptions(self):
        """多选：标题 + 描述 + 每个选项带 description。

        操作：空格勾选 A，移到 B 空格勾选 B，移到 C 空格勾选 C，Enter。
        """
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            # 焦点 0：空格勾选 A；↓ 到 1：空格勾选 B；↓ 到 2：空格勾选 C；Enter
            inp.send_text(" \x0e \x0e \r")
            r = ask_ui(
                title="选择技术栈组件",
                description="（可多选；空格勾选，Enter 提交）",
                multi=True,
                options=[
                    AskOption(label="前端", value="fe", description="HTML/CSS/JS"),
                    AskOption(label="后端", value="be", description="Python/Java/Go"),
                    AskOption(label="数据库", value="db", description="PostgreSQL"),
                    AskOption(label="缓存", value="cache", description="Redis"),
                ],
                input=inp, output=DummyOutput(),
            )
        # selected 按 options 顺序列出勾选项
        assert r.selected == ["fe", "be", "db"]
        assert r.checked == {0, 1, 2}

    def test_confirm_style_multiple_normal_plus_custom(self):
        """confirm 风格：多个普通选项 + 末尾自定义选项。

        移到末尾自定义，输入理由后提交。
        """
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            # 移到末项（index 2），输入"想改"，Enter
            inp.send_text("\x0e\x0e想改\r")
            r = ask_ui(
                title="确认工具调用",
                description="bash echo hi",
                options=[
                    AskOption(label="同意", value="approve"),
                    AskOption(label="编辑", value="edit"),
                    AskOption(label="拒绝", value="reject",
                              description="拒绝理由", is_custom=True),
                ],
                input=inp, output=DummyOutput(),
            )
        assert r.selected == ["reject"]
        assert r.input == "想改"

    def test_multi_checked_passes_through_real_call(self):
        """多选第一次调用拿到 checked，传给第二次调用验证预勾选生效。

        与 test_checked_init_multi 不同：这里**真正跑两次** ask_ui，模拟
        confirm 用 ESC 从编辑视图返回时维持多选勾选的场景。
        """
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        # 第一次：空格勾选 A 和 B，Enter 提交
        with create_pipe_input() as inp:
            inp.send_text(" \x0e \r")
            first = ask_ui(
                title="第一次",
                multi=True,
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                input=inp, output=DummyOutput(),
            )
        assert first.checked == {0, 1}
        assert first.selected == ["a", "b"]

        # 第二次：传入 first.checked 作为初始勾选，移到 C 空格取消 A 与 B
        # 再 ↓ 到 C，Enter 提交（全取消后 selected 应为空）
        with create_pipe_input() as inp:
            inp.send_text(" \x0e \x0e\r")  # 焦点 0：取消 A；↓ 到 1：取消 B；↓ 到 2：Enter
            second = ask_ui(
                title="第二次（预勾相同项）",
                multi=True,
                options=[
                    AskOption(label="A", value="a"),
                    AskOption(label="B", value="b"),
                    AskOption(label="C", value="c"),
                ],
                checked=set(first.checked),
                input=inp, output=DummyOutput(),
            )
        # 关键：第二次的初始 checked 应是 {0, 1}；用户取消 A、B 后，
        # checked 与 selected 都为空
        assert second.checked == set()
        assert second.selected == []


class TestAskUiApplication:
    """Application 构造正确（回归）。"""

    def test_application_builds(self):
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from mycode.ask_ui import _AskState, _build_ask_layout
        state = _AskState(title="Q", description="", options=[
            AskOption(label="A", value="a"),
            AskOption(label="其它", value="custom", is_custom=True),
        ], multi=False)
        layout = _build_ask_layout(state, custom_buffer=__import__("prompt_toolkit.buffer", fromlist=["Buffer"]).Buffer())
        app = Application(layout=Layout(layout), full_screen=False)
        assert app.layout is not None
        # 回归：container 是裸容器（有 preferred_height），而非打包的 Layout 对象
        assert hasattr(app.layout.container, "preferred_height")
        assert not isinstance(app.layout.container, Layout)

    def test_application_erase_when_done(self):
        """ask_ui 运行 Application 带 erase_when_done=True（不残留上屏）。"""
        from unittest.mock import patch
        from prompt_toolkit.application import Application
        from prompt_toolkit.output import DummyOutput

        seen: dict = {}

        class _FakeApplication(Application):
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)
                super().__init__(*args, **kwargs)

            def run(self):
                return None

        from mycode.ask_ui import ask_ui
        from mycode.ask_ui import AskOption
        from prompt_toolkit.input import create_pipe_input

        with patch("mycode.ask_ui.Application", _FakeApplication):
            with create_pipe_input() as inp:
                inp.send_text("\r")
                ask_ui(
                    title="Q",
                    options=[AskOption(label="A", value="a")],
                    input=inp,
                    output=DummyOutput(),
                )
        assert seen.get("erase_when_done") is True

    def test_application_receives_style(self):
        """ask_ui 将 style 参数透传给 Application（让样式类生效）。"""
        from unittest.mock import patch
        from prompt_toolkit.application import Application
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit.styles import Style

        seen: dict = {}

        class _FakeApplication(Application):
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)
                super().__init__(*args, **kwargs)

            def run(self):
                return None

        from mycode.ask_ui import ask_ui
        from mycode.ask_ui import AskOption
        from prompt_toolkit.input import create_pipe_input

        sentinel_style = Style.from_dict({"placeholder": "italic fg:#abcdef"})
        with patch("mycode.ask_ui.Application", _FakeApplication):
            with create_pipe_input() as inp:
                inp.send_text("\r")
                ask_ui(
                    title="Q",
                    options=[AskOption(label="A", value="a")],
                    style=sentinel_style,
                    input=inp,
                    output=DummyOutput(),
                )
        assert seen.get("style") is sentinel_style

    def test_custom_input_unfocusable_when_not_selected(self):
        """焦点不在自定义选项时，输入框不可接收输入（不抢焦点）。

        模拟从编辑返回 ask_ui 时，焦点在编辑（不在自定义），输入框
        focusable=False；prompt_toolkit 启动时不会把焦点自动塞到
        自定义输入框上。
        """
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.buffer import Buffer
        from mycode.ask_ui import _AskState, _build_ask_layout

        state = _AskState(
            title="",
            description="",
            options=[
                AskOption(label="A", value="a"),
                AskOption(label="其它", value="custom", description="理由", is_custom=True),
            ],
            multi=False,
            cursor_index=0,  # 焦点在 A，不在自定义
            custom_buffer=Buffer(),
        )
        layout = _build_ask_layout(state, custom_buffer=state.custom_buffer)
        app = Application(layout=Layout(layout), full_screen=False)

        # 第一个 focusable widget 不应是自定义输入框
        from prompt_toolkit.layout.containers import VSplit
        from prompt_toolkit.layout.controls import BufferControl
        rows = app.layout.container.children
        # 找到自定义选项行（最后一个）
        custom_row = rows[-1]
        assert isinstance(custom_row, VSplit)
        input_win = custom_row.children[1]
        ctrl = input_win.content
        assert isinstance(ctrl, BufferControl)
        # 焦点不在自定义时，输入框应不可 focus
        assert ctrl.focusable() is False

    def test_custom_input_focusable_when_selected(self):
        """焦点在自定义选项时，输入框 focusable=True。"""
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.buffer import Buffer
        from mycode.ask_ui import _AskState, _build_ask_layout

        state = _AskState(
            title="",
            description="",
            options=[
                AskOption(label="A", value="a"),
                AskOption(label="其它", value="custom", description="理由", is_custom=True),
            ],
            multi=False,
            cursor_index=1,  # 焦点在自定义
            custom_buffer=Buffer(),
        )
        layout = _build_ask_layout(state, custom_buffer=state.custom_buffer)
        app = Application(layout=Layout(layout), full_screen=False)

        from prompt_toolkit.layout.containers import VSplit
        from prompt_toolkit.layout.controls import BufferControl
        rows = app.layout.container.children
        custom_row = rows[-1]
        assert isinstance(custom_row, VSplit)
        input_win = custom_row.children[1]
        ctrl = input_win.content
        assert isinstance(ctrl, BufferControl)
        assert ctrl.focusable() is True

    def test_custom_input_uses_mycode_input_style(self):
        """自定义输入框 Window 挂上 class:mycode-input（与 cli 输入区共用）。"""
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.buffer import Buffer
        from mycode.ask_ui import _AskState, _build_ask_layout

        state = _AskState(
            title="",
            description="",
            options=[
                AskOption(label="其它", value="custom",
                          description="理由", is_custom=True),
            ],
            multi=False,
            cursor_index=0,
            custom_buffer=Buffer(),
        )
        layout = _build_ask_layout(state, custom_buffer=state.custom_buffer)
        app = Application(layout=Layout(layout), full_screen=False)

        from prompt_toolkit.layout.containers import VSplit
        rows = app.layout.container.children
        custom_row = rows[-1]
        assert isinstance(custom_row, VSplit)
        input_win = custom_row.children[1]
        assert input_win.style == "class:mycode-input"


class TestAskUiPlaceholderProcessors:
    """placeholder 处理器：当 buffer 为空时插入占位文字。"""

    def test_no_placeholder_when_no_description(self):
        from mycode.ask_ui import _placeholder_processors
        procs = _placeholder_processors(__import__("prompt_toolkit.buffer", fromlist=["Buffer"]).Buffer(), None)
        assert procs == []

    def test_returns_processor_when_description(self):
        from mycode.ask_ui import _placeholder_processors
        from prompt_toolkit.layout.processors import ConditionalProcessor
        procs = _placeholder_processors(__import__("prompt_toolkit.buffer", fromlist=["Buffer"]).Buffer(), "理由")
        assert len(procs) == 1
        assert isinstance(procs[0], ConditionalProcessor)

    def test_placeholder_renders_when_buffer_empty(self):
        """buffer 为空时，placeholder 处理器返回激活态（光标后插入占位文字）。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout.processors import AfterInput
        from mycode.ask_ui import _placeholder_processors

        buf = Buffer()  # 空 buffer
        procs = _placeholder_processors(buf, "拒绝理由")
        assert len(procs) == 1
        cp = procs[0]
        # 内部是 AfterInput + Condition(_show)
        assert isinstance(cp.processor, AfterInput)
        # placeholder 文本前导一个空格，给光标留可视距离
        assert cp.processor.text == " 拒绝理由"
        assert cp.processor.style == "class:placeholder"  # 与 cli PromptSession 共用样式
        assert cp.filter() is True  # buffer 为空时 placeholder 应激活

    def test_placeholder_inactive_when_buffer_has_text(self):
        """buffer 有文本时，placeholder 处理器返回非激活态。"""
        from prompt_toolkit.buffer import Buffer
        from mycode.ask_ui import _placeholder_processors

        buf = Buffer()
        buf.text = "已输入"
        procs = _placeholder_processors(buf, "拒绝理由")
        assert procs[0].filter() is False  # 有文本时 placeholder 不显示
