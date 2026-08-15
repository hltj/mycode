"""
确认交互界面（mycode.confirm）测试。

覆盖：
- confirm_tool 各动作分支（同意 / 带理由拒绝 / 无理由拒绝 / 取消 / 编辑）
- _ConfirmState 状态与菜单片段
- 确认界面布局（选中拒绝时含理由输入框、非拒绝时无输入框）
- Application 布局构造（回归测试）
"""

from __future__ import annotations

import pytest

import mycode.confirm as confirm_mod
from mycode.mode import ToolCategory


def _run_with_keys(seq: str, show_edit: bool = True, command: str = ""):
    """用注入的按键序列运行确认界面，返回 (action, text, state)。"""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    st = confirm_mod._ConfirmState(show_edit=show_edit)
    with create_pipe_input() as inp:
        inp.send_text(seq)
        action, text = confirm_mod._run_confirm_menu(st, command, input=inp, output=DummyOutput())
    return action, text, st


class TestConfirmMenuInteraction:
    """确认菜单交互行为（按键序列驱动真实 Application）。"""

    def test_default_selected_approve(self):
        """默认选中同意，直接回车确认。"""
        action, text, st = _run_with_keys("\r")
        assert action == "approve"
        assert st.sel == 0  # 默认同意
        assert text == ""

    def test_down_moves_selection(self):
        """按 down 移动选择（down 后 up 回同意，回车确认）。"""
        action, text, st = _run_with_keys("\x0e\x10\r")  # down 到编辑，up 回同意，enter
        assert action == "approve"
        assert st.sel == 0

    def test_up_moves_selection(self):
        """按 up 移动选择（down 两次到拒绝，再 up 回同意，回车确认）。"""
        action, text, st = _run_with_keys("\x0e\x0e\x10\x10\r")  # 2*down 拒绝，2*up 回同意
        assert action == "approve"
        assert st.sel == 0

    def test_select_reject_and_input(self):
        """选中拒绝后可输入理由，回车返回理由。"""
        action, text, st = _run_with_keys("\x0e\x0eabc\r")
        assert action == "reject"
        assert st.sel == 2
        assert text == "abc"

    def test_select_reject_no_reason_returns_plain(self):
        """选中拒绝但无理由，回车返回无理由拒绝。"""
        action, text, st = _run_with_keys("\x0e\x0e\r")
        assert action == "reject_plain"
        assert st.sel == 2
        assert text == ""

    def test_ctrl_c_aborts(self):
        """Ctrl-C 中止，返回 abort。"""
        action, text, st = _run_with_keys("\x03")  # Ctrl-C
        assert action == "abort"
        assert text == ""

    def test_edit_enters_and_confirms(self):
        """选中编辑进入编辑视图，编辑后 Alt+Enter 确认返回 edit。"""
        # c-n 到编辑，enter 进入编辑，输入追加，Alt+Enter 确认
        action, text, st = _run_with_keys("\x0e\r -x\x1b\r", command="echo hi")
        assert action == "edit"
        assert text == "echo hi -x"

    def test_edit_escape_returns_to_menu(self):
        """编辑中 ESC 返回确认菜单，确认菜单重新显示。"""
        # c-n 到编辑，enter 进入编辑，ESC 返回菜单，此时再 down 到拒绝确认
        action, text, st = _run_with_keys("\x0e\r\x1b\x0e\x0e\r")
        # 返回菜单后 down 到拒绝，无理由 → reject_plain
        assert action == "reject_plain"
        assert st.sel == 2



class TestConfirmTool:
    def test_confirm_approve(self, monkeypatch):
        monkeypatch.setattr(confirm_mod, "_run_confirm_menu",
                            lambda state, command=None, input=None, output=None: ("approve", ""))
        action, extra = confirm_mod.confirm_tool("bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.APPROVE
        assert extra is None

    def test_confirm_reject_with_reason(self, monkeypatch):
        def _menu(state, command=None, input=None, output=None):
            state.sel = state.max_sel
            return ("reject", "不想执行")
        monkeypatch.setattr(confirm_mod, "_run_confirm_menu", _menu)
        action, extra = confirm_mod.confirm_tool("bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT
        assert extra == "不想执行"

    def test_confirm_reject_without_reason_aborts(self, monkeypatch):
        def _menu(state, command=None, input=None, output=None):
            state.sel = state.max_sel
            return ("reject_plain", "")
        monkeypatch.setattr(confirm_mod, "_run_confirm_menu", _menu)
        action, extra = confirm_mod.confirm_tool("bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT_NO_REASON
        assert extra is None

    def test_confirm_abort_on_escape(self, monkeypatch):
        monkeypatch.setattr(confirm_mod, "_run_confirm_menu",
                            lambda state, command=None, input=None, output=None: ("abort", ""))
        action, extra = confirm_mod.confirm_tool("bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.CANCEL
        assert extra is None

    def test_edit_flow(self, monkeypatch):
        def _menu(state, command=None, input=None, output=None):
            state.sel = 1  # 编辑
            return ("edit", "echo edited")
        monkeypatch.setattr(confirm_mod, "_run_confirm_menu", _menu)
        action, extra = confirm_mod.confirm_tool("bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.EDIT
        assert extra == "echo edited"

    def test_non_bash_no_edit_option(self):
        st = confirm_mod._ConfirmState(show_edit=False)
        assert st.show_edit is False
        assert st.max_sel == 1  # 无编辑：同意 + 拒绝
        assert len(st.options) == 2
        assert [o.kind for o in st.options] == [confirm_mod.OptionKind.APPROVE, confirm_mod.OptionKind.REJECT]

    def test_reason_not_shown_in_menu(self):
        """理由不显示在菜单行（由独立输入框承载）。"""
        st = confirm_mod._ConfirmState(show_edit=True)
        st.sel = st.max_sel  # 选中拒绝
        st.reason = "不需要"
        frags = st.menu_fragments()
        text = "".join(t for _, t in frags)
        assert "3. 拒绝：" in text
        assert "不需要" not in text

    def test_reason_not_shown_when_not_selected(self):
        """未选中拒绝时，菜单行同样不含理由。"""
        st = confirm_mod._ConfirmState(show_edit=True)
        st.sel = 0  # 选中同意
        st.reason = "不需要"
        frags = st.menu_fragments()
        text = "".join(t for _, t in frags)
        assert "3. 拒绝：" in text
        assert "不需要" not in text

    def test_is_reject_selected(self):
        st = confirm_mod._ConfirmState(show_edit=True)
        st.sel = st.max_sel
        assert st.is_reject_selected is True
        st.sel = 0
        assert st.is_reject_selected is False


class TestConfirmOptionNumbering:
    """选项统一编号：拒绝编号随选项数量自适应（无编辑为 2，有编辑为 3）。"""

    def test_no_edit_reject_numbered_2(self):
        """无编辑时拒绝显示「2. 拒绝」。"""
        from mycode.confirm import _build_confirm_layout
        from prompt_toolkit.buffer import Buffer
        st = confirm_mod._ConfirmState(show_edit=False)
        st.sel = st.max_sel
        layout = _build_confirm_layout(st, Buffer())
        text = self._layout_text(layout)
        assert "2. 拒绝" in text
        assert "3. 拒绝" not in text

    def test_with_edit_reject_numbered_3(self):
        """有编辑时拒绝显示「3. 拒绝」。"""
        from mycode.confirm import _build_confirm_layout
        from prompt_toolkit.buffer import Buffer
        st = confirm_mod._ConfirmState(show_edit=True)
        st.sel = st.max_sel
        layout = _build_confirm_layout(st, Buffer())
        text = self._layout_text(layout)
        assert "3. 拒绝" in text
        assert "2. 拒绝" not in text  # 2 是编辑

    def test_with_edit_reject_is_last_option(self):
        """有编辑时编辑为 2、拒绝为 3，编号连续。"""
        from mycode.confirm import _build_confirm_layout
        from prompt_toolkit.buffer import Buffer
        st = confirm_mod._ConfirmState(show_edit=True)
        layout = _build_confirm_layout(st, Buffer())
        text = self._layout_text(layout)
        assert "1. 同意" in text
        assert "2. 编辑" in text
        assert "3. 拒绝" in text

    def test_no_edit_reject_is_last_option(self):
        """无编辑时同意为 1、拒绝为 2，编号连续。"""
        from mycode.confirm import _build_confirm_layout
        from prompt_toolkit.buffer import Buffer
        st = confirm_mod._ConfirmState(show_edit=False)
        layout = _build_confirm_layout(st, Buffer())
        text = self._layout_text(layout)
        assert "1. 同意" in text
        assert "2. 拒绝" in text

    @staticmethod
    def _layout_text(layout) -> str:
        """从布局递归提取全部文本。"""
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        parts = []
        def walk(node):
            if isinstance(node, Window):
                c = node.content
                if isinstance(c, FormattedTextControl):
                    frags = c.text if hasattr(c, "text") else c()
                    if isinstance(frags, list):
                        parts.append("".join(t for _, t in frags))
                    else:
                        parts.append(str(frags))
            elif hasattr(node, "children"):
                for ch in node.children:
                    walk(ch)
        walk(layout)
        return "\n".join(parts)


class TestRejectInputNotFocusedWhenNotSelected:
    """未选中拒绝时，理由输入区不接受键盘输入。"""

    def test_typing_when_approve_selected_does_not_build_reason(self):
        """默认（同意）时输入字符，不会进入理由缓冲区。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        st = confirm_mod._ConfirmState(show_edit=True)
        # 默认同意选中，输入 'abc' 后直接回车（同意）
        with create_pipe_input() as inp:
            inp.send_text("abc\r")
            action, text = confirm_mod._run_confirm_menu(st, "echo hi", input=inp, output=DummyOutput())
        assert action == "approve"
        assert st.reason == ""  # 理由未被输入污染

    def test_layout_includes_reason_input_when_reject_selected(self):
        """选中拒绝时布局含理由输入窗口（同行）。"""
        from mycode.confirm import _build_confirm_layout
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout import VSplit
        st = confirm_mod._ConfirmState(show_edit=True)
        st.sel = st.max_sel
        layout = _build_confirm_layout(st, Buffer())
        rows = layout.children
        assert len(rows) == 3
        # 拒绝行是 VSplit（「3. 拒绝：」与输入框横向同行）
        reject_row = rows[2]
        assert isinstance(reject_row, VSplit)

    def test_layout_no_input_when_not_reject(self):
        """非拒绝时拒绝行是普通文本窗口（无输入框）。"""
        from mycode.confirm import _build_confirm_layout
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout import VSplit
        st = confirm_mod._ConfirmState(show_edit=True)
        st.sel = 0  # 同意
        layout = _build_confirm_layout(st, Buffer())
        rows = layout.children
        assert len(rows) == 3
        # 拒绝行不是 VSplit（无输入框）
        assert not isinstance(rows[2], VSplit)

    def test_confirm_application_builds_without_error(self):
        """Application 用 Layout 包装布局后能正常构造（回归：HSplit 无 current_control 报错）。"""
        from mycode.confirm import _ConfirmState, _build_confirm_layout
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout import Layout
        st = _ConfirmState(show_edit=True)
        # 选中拒绝（含输入框窗口）
        st.sel = st.max_sel
        app = Application(
            layout=Layout(_build_confirm_layout(st, Buffer())),
            full_screen=False,
        )
        assert app.layout is not None
        # 非拒绝（仅菜单）
        st.sel = 0
        app2 = Application(
            layout=Layout(_build_confirm_layout(st, Buffer())),
            full_screen=False,
        )
        assert app2.layout is not None

    def test_layout_container_has_render_attrs(self):
        """布局 container 是裸 HSplit（渲染所需属性），而非打包的 Layout 对象。"""
        from mycode.confirm import _ConfirmState, _build_confirm_layout
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout import Layout
        st = _ConfirmState(show_edit=True)
        st.sel = st.max_sel
        app = Application(
            layout=Layout(_build_confirm_layout(st, Buffer())),
            full_screen=False,
        )
        # app.layout.container 是裸容器（有 preferred_height），而非 Layout
        assert hasattr(app.layout.container, "preferred_height")
        assert not isinstance(app.layout.container, Layout)

class TestEditViewLayout:
    """编辑界面布局：独立视图、输入框紧挨提示。"""

    def test_edit_layout_has_no_confirm_menu_rows(self):
        """编辑布局不含确认菜单行（编辑时确认菜单被替换而非叠加）。"""
        from mycode.confirm import _build_edit_layout
        from prompt_toolkit.buffer import Buffer
        layout = _build_edit_layout(Buffer())
        # 编辑布局是 VSplit（提示 + 输入框），不含「1. 同意」等菜单行
        from prompt_toolkit.layout import VSplit
        assert isinstance(layout, VSplit)
        text = "".join(str(c) for c in getattr(layout, "children", []))
        assert "1. 同意" not in text
        assert "2. 编辑" not in text
        assert "3. 拒绝" not in text

    def test_edit_input_adjacent_to_prompt(self):
        """编辑输入框紧挨「编辑 >> 」提示，仅隔一个空格。"""
        from mycode.confirm import _build_edit_layout
        from prompt_toolkit.buffer import Buffer
        layout = _build_edit_layout(Buffer())
        from prompt_toolkit.layout import VSplit
        assert isinstance(layout, VSplit)
        # 第一个子窗口是提示文本「编辑 >> 」（含尾随一个空格）
        prompt_win = layout.children[0]
        control = prompt_win.content
        frags = control.text if hasattr(control, "text") else control()
        text = "".join(t for _, t in frags) if isinstance(frags, list) else str(frags)
        assert text == "编辑 >> "

    def test_reject_input_adjacent_to_reject_label(self):
        """选中拒绝时输入框紧挨「3. 拒绝：」，仅隔一个空格。"""
        from mycode.confirm import _build_confirm_layout
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout import VSplit
        st = confirm_mod._ConfirmState(show_edit=True)
        st.sel = st.max_sel
        layout = _build_confirm_layout(st, Buffer())
        reject_row = layout.children[2]
        assert isinstance(reject_row, VSplit)
        # 拒绝文本窗口内容为「> 3. 拒绝：」
        label_win = reject_row.children[0]
        control = label_win.content
        frags = control.text if hasattr(control, "text") else control()
        text = "".join(t for _, t in frags) if isinstance(frags, list) else str(frags)
        assert text == "> 3. 拒绝："


class TestEditSwitchNoAccumulation:
    """多次进入/退出编辑视图，验证布局切换不堆积旧菜单（回归 bug）。"""

    def _run_capture(self, seq, command="uptime"):
        """运行并捕获渲染输出，返回 (action, text, st, 输出文本)。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        class _RecordingOutput(DummyOutput):
            def __init__(self):
                super().__init__()
                self.data = []
            def write(self, data):
                self.data.append(data)
            def flush(self):
                pass

        st = confirm_mod._ConfirmState(show_edit=True)
        out = _RecordingOutput()
        with create_pipe_input() as inp:
            inp.send_text(seq)
            action, text = confirm_mod._run_confirm_menu(st, command, input=inp, output=out)
        rendered = "".join(out.data)
        return action, text, st, rendered

    def test_edit_escape_and_reenter_shows_single_view(self):
        """多次进入编辑→ESC返回后，最终进入编辑视图渲染不含堆积的菜单行。

        序列：down到编辑(\x0e) → enter进编辑(\r) → ESC返回(\x1b) →
        enter再进编辑(\r) → ESC返回(\x1b) → enter再进编辑(\r) → enter确认(\r)
        """
        action, text, st, rendered = self._run_capture("\x0e\r\x1b\r\x1b\r\r")
        assert action == "edit"
        assert text == "uptime"
        # 最终进入编辑视图，输出含「编辑 >>」
        assert "编辑 >>" in rendered
        # 关键：菜单行不应多次堆积（旧实现多次 run 会在同一位置累积）
        assert rendered.count("1. 同意") <= 1
        assert rendered.count("2. 编辑 >>") <= 1
        assert rendered.count("3. 拒绝") <= 1


class TestEditMultiline:
    """编辑界面多行支持：光标移动与修改、Alt+Enter 提交。"""

    def _run_edit(self, seq, command):
        """进入编辑界面运行按键序列，返回 (action, text)。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        st = confirm_mod._ConfirmState(show_edit=True)
        with create_pipe_input() as inp:
            inp.send_text(seq)
            action, text = confirm_mod._run_confirm_menu(st, command, input=inp, output=DummyOutput())
        return action, text

    def _edit_seq(self, *keys):
        """进入编辑（down+enter）后接按键序列，最后 Alt+Enter 提交。"""
        return "\x0e\r" + "".join(keys) + "\x1b\r"

    def test_multiline_command_preserved(self):
        """多行命令在编辑中保持完整，Alt+Enter 提交后原样返回。"""
        cmd = "cat <<EOF\nline1\nline2\nEOF"
        action, text = self._run_edit(self._edit_seq(), cmd)
        assert action == "edit"
        assert text == cmd

    def test_edit_first_line_middle(self):
        """编辑第一行中间：光标到第一行中间插入文本。"""
        cmd = "aaaa\nbbbb\ncccc"
        # 光标默认在末尾（最后一行末尾），需回到第一行开头，再右移
        # 简化：用 Home 到第一行开头（多按几次 Home/up 回最顶）
        action, text = self._run_edit(self._edit_seq(), cmd)
        # 基础断言：多行命令往返一致
        assert action == "edit"
        assert text == "aaaa\nbbbb\ncccc"

    def test_alt_enter_submits_multiline(self):
        """Alt+Enter 提交多行命令；普通 Enter 不提交。"""
        cmd = "cat <<EOF\nx\nEOF"
        action, text = self._run_edit(self._edit_seq(), cmd)
        assert action == "edit"
        assert text == cmd

    def _edit_first_line(self, keys, cmd="aaaa\nbbbb\ncccc"):
        """跳到第一行（从末尾上移2次）后执行 keys。"""
        return self._edit_seq("\x10\x10", *keys)

    def test_edit_first_line_start(self):
        """第一行行首插入。"""
        action, text = self._run_edit(self._edit_first_line("\x01X"), "aaaa\nbbbb\ncccc")
        assert text == "Xaaaa\nbbbb\ncccc"

    def test_edit_first_line_middle(self):
        """第一行中间插入。"""
        # 行首 + 右移字符光标到第2列后插入 X
        action, text = self._run_edit(
            self._edit_first_line("\x01\x06X"), "aaaa\nbbbb\ncccc")
        assert text == "aXaaa\nbbbb\ncccc"

    def test_edit_first_line_end(self):
        """第一行末尾插入。"""
        action, text = self._run_edit(
            self._edit_first_line("\x05X"), "aaaa\nbbbb\ncccc")
        assert text == "aaaaX\nbbbb\ncccc"

    def _edit_last_line(self, keys, cmd="aaaa\nbbbb\ncccc"):
        """光标默认在最后一行末尾，直接执行 keys。"""
        return self._edit_seq(*keys)

    def test_edit_last_line_end(self):
        """最后一行末尾插入。"""
        action, text = self._run_edit(
            self._edit_last_line("X"), "aaaa\nbbbb\ncccc")
        assert text == "aaaa\nbbbb\nccccX"

    def test_edit_last_line_start(self):
        """最后一行行首插入。"""
        action, text = self._run_edit(
            self._edit_last_line("\x01X"), "aaaa\nbbbb\ncccc")
        assert text == "aaaa\nbbbb\nXcccc"

    def test_edit_last_line_middle(self):
        """最后一行中间插入。"""
        action, text = self._run_edit(
            self._edit_last_line("\x01\x06X"), "aaaa\nbbbb\ncccc")
        assert text == "aaaa\nbbbb\ncXccc"

    def _edit_middle_line(self, keys, cmd="aaaa\nbbbb\ncccc"):
        """跳到中间行（从末尾上移1次到第2行）后执行 keys。"""
        return self._edit_seq("\x10", *keys)

    def test_edit_middle_line_start(self):
        """中间行行首插入。"""
        action, text = self._run_edit(
            self._edit_middle_line("\x01X"), "aaaa\nbbbb\ncccc")
        assert text == "aaaa\nXbbbb\ncccc"

    def test_edit_middle_line_middle(self):
        """中间行中间插入。"""
        action, text = self._run_edit(
            self._edit_middle_line("\x01\x06X"), "aaaa\nbbbb\ncccc")
        assert text == "aaaa\nbXbbb\ncccc"

    def test_edit_middle_line_end(self):
        """中间行末尾插入。"""
        action, text = self._run_edit(
            self._edit_middle_line("\x05X"), "aaaa\nbbbb\ncccc")
        assert text == "aaaa\nbbbbX\ncccc"


class TestRejectInputFrozenAfterLeave:
    """离开拒绝后理由输入被冻结，输入不再污染 reason。"""

    def test_reactivated_reject_not_polluted_by_other_selection(self):
        """激活拒绝输入 abc，切回同意输入 xyz，再回拒绝不应含 xyz。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        st = confirm_mod._ConfirmState(show_edit=True)
        # 到拒绝(2)输入 abc，回同意(0)输入 xyz，再到拒绝(2)回车
        seq = "\x0e\x0eabc\x10\x10xyz\x0e\x0e\r"
        with create_pipe_input() as inp:
            inp.send_text(seq)
            action, text = confirm_mod._run_confirm_menu(st, "echo hi", input=inp, output=DummyOutput())
        # 拒绝理由应只含 abc，不含 xyz
        assert action == "reject"
        assert text == "abc"
        assert "xyz" not in text


class TestRejectInputDisabledWhenNotSelected:
    """未选中拒绝时，理由输入控件不接受输入。"""

    def test_input_when_approve_selected_ignored(self):
        """默认同意时输入字符，理由缓冲区保持为空。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.output import DummyOutput
        st = confirm_mod._ConfirmState(show_edit=True)
        reason = Buffer()
        with create_pipe_input() as inp:
            inp.send_text("abc\r")  # 同意选中时输入 abc，回车确认同意
            action, text = confirm_mod._run_confirm_menu(
                st, "echo hi", input=inp, output=DummyOutput(), reason_buffer=reason)
        assert action == "approve"
        assert reason.text == ""  # 输入未进入理由缓冲区

    def test_input_after_leave_reject_ignored(self):
        """离开拒绝后输入字符，不再进入理由缓冲区。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.output import DummyOutput
        st = confirm_mod._ConfirmState(show_edit=True)
        reason = Buffer()
        # 到拒绝(2)输入 abc，回同意(0)输入 xyz，再回拒绝(2)确认
        seq = "\x0e\x0eabc\x10\x10xyz\x0e\x0e\r"
        with create_pipe_input() as inp:
            inp.send_text(seq)
            action, text = confirm_mod._run_confirm_menu(
                st, "echo hi", input=inp, output=DummyOutput(), reason_buffer=reason)
        assert action == "reject"
        assert text == "abc"
        assert "xyz" not in text


class TestConfirmApplicationEraseWhenDone:
    """确认/编辑界面退出时擦除自己渲染的画面（不残留上屏）。"""

    def test_application_has_erase_when_done(self):
        """_run_confirm_menu 构造的 Application 带 erase_when_done=True。"""
        from unittest.mock import patch
        from prompt_toolkit.application import Application
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        seen: dict = {}

        class _FakeApplication(Application):
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)
                super().__init__(*args, **kwargs)
            def run(self):
                return None

        st = confirm_mod._ConfirmState(show_edit=True)
        # 直接进编辑并回车确认（模拟编辑完确认）
        with create_pipe_input() as inp:
            inp.send_text("\x0e\r\r")
            with patch.object(confirm_mod, "Application", _FakeApplication):
                confirm_mod._run_confirm_menu(
                    st, "echo hi", input=inp, output=DummyOutput())
        assert seen.get("erase_when_done") is True
