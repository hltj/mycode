"""
确认交互（mycode.confirm）测试。

``confirm.py`` 的职责是在 ``ask_ui`` 结果之上完成动作映射：

- 选中「同意」→ ``APPROVE``
- 选中「编辑」（仅 bash）→ 启动独立多行编辑视图
- 选中「拒绝」（自定义选项）：
    - 有理由 → ``REJECT``（带理由）
    - 无理由 → ``REJECT_NO_REASON``
- abort / Ctrl-C / 未选已知选项 → ``CANCEL``

ask_ui 自身的 UI 行为详见 ``tests/test_ask_ui.py``。
"""

from __future__ import annotations

import io

import pytest

import mycode.confirm as confirm_mod
import mycode.ask_ui as ask_ui_mod
from mycode.ask_ui import AskAnswer, AskOption, AskResult
from mycode.mode import MODE_STATE, Mode, ToolCategory


def _edit_view_container(buf_text: str = "echo hi", style: str = "default"):
    """运行 _run_edit_view（FakeApp 截获布局根容器），返回根容器。

    以 Ctrl-C 立即退出；渲染风格经 renderer.RENDER_STYLE 指定。
    """
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from unittest.mock import patch
    from prompt_toolkit.application import Application
    import mycode.renderer as renderer_mod

    seen: dict = {}

    class _FakeApp(Application):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["container"] = self.layout.container

        def run(self):
            return None

    buf = Buffer(multiline=True)
    buf.text = buf_text
    saved = renderer_mod.RENDER_STYLE
    renderer_mod.RENDER_STYLE = style
    try:
        with create_pipe_input() as inp:
            inp.send_text("\x03")  # Ctrl-C 立即退出
            with patch("mycode.confirm.Application", _FakeApp):
                confirm_mod._run_edit_view(buf, input=inp, output=DummyOutput())
    finally:
        renderer_mod.RENDER_STYLE = saved
    return seen["container"]


def _ftc_text(window) -> tuple[list, str]:
    """取 Window 上 FormattedTextControl 的 (fragments, 纯文本)。"""
    frags = window.content.text
    return frags, "".join(t for _, t in frags) if isinstance(frags, list) else str(frags)


# ===================================================================
# 动作映射：confirm_tool 通过 mock ask_ui 验证
# ===================================================================

class TestConfirmToolMapping:
    def test_approve(self, monkeypatch):
        """选中「同意」→ ``APPROVE``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(answers=[AskAnswer(
                                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                                input=None,
                            )]))
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.APPROVE
        assert extra is None

    def test_reject_with_reason(self, monkeypatch):
        """选中「拒绝」+ 输入理由 → ``REJECT``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(answers=[AskAnswer(
                                selected=[confirm_mod.ConfirmAction.REJECT.value],
                                input="不想执行",
                            )]))
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT
        assert extra == "不想执行"

    def test_reject_with_whitespace_reason_is_no_reason(self, monkeypatch):
        """选中「拒绝」+ 仅空白 → 视作无理由 → ``REJECT_NO_REASON``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(answers=[AskAnswer(
                                selected=[confirm_mod.ConfirmAction.REJECT.value],
                                input="   ",
                            )]))
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT_NO_REASON
        assert extra is None

    def test_reject_without_reason(self, monkeypatch):
        """选中「拒绝」+ 输入框为空 → ``REJECT_NO_REASON``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(answers=[AskAnswer(
                                selected=[confirm_mod.ConfirmAction.REJECT.value],
                                input="",
                            )]))
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT_NO_REASON
        assert extra is None

    def test_abort_returns_cancel(self, monkeypatch):
        """abort（selected 空）→ ``CANCEL``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(aborted=True))
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.CANCEL
        assert extra is None

    def test_edit_flow(self, monkeypatch):
        """选中「编辑」→ 调编辑器 + 返回 ``EDIT``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(answers=[AskAnswer(
                                selected=[confirm_mod.ConfirmAction.EDIT.value],
                                input=None,
                            )]))
        monkeypatch.setattr(confirm_mod, "_run_edit_view",
                            lambda edit_buffer, input=None, output=None, style=None:
                                confirm_mod._EditOutcome(action="finish", text="echo edited"))
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.EDIT
        assert extra == "echo edited"

    def test_edit_aborted_returns_cancel(self, monkeypatch):
        """编辑器返回 ``abort``（Ctrl-C）→ ``CANCEL``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(answers=[AskAnswer(
                                selected=[confirm_mod.ConfirmAction.EDIT.value],
                                input=None,
                            )]))
        monkeypatch.setattr(confirm_mod, "_run_edit_view",
                            lambda edit_buffer, input=None, output=None, style=None:
                                confirm_mod._EditOutcome(action="abort"))
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.CANCEL
        assert extra is None

    def test_edit_back_reasks(self, monkeypatch):
        """编辑器返回 ``back``（ESC）→ 重新询问 ask_ui。"""
        ask_calls = {"count": 0}

        def _ask_ui_stub(*a, **kw):
            ask_calls["count"] += 1
            # 第一次返回 EDIT，第二次返回 APPROVE
            if ask_calls["count"] == 1:
                return AskResult(answers=[AskAnswer(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                )])
            return AskResult(answers=[AskAnswer(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
            )])

        edit_calls = {"count": 0}

        def _edit_stub(edit_buffer, input=None, output=None, style=None):
            edit_calls["count"] += 1
            return confirm_mod._EditOutcome(action="back")

        monkeypatch.setattr(ask_ui_mod, "ask_ui", _ask_ui_stub)
        monkeypatch.setattr(confirm_mod, "_run_edit_view", _edit_stub)

        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.APPROVE
        assert extra is None
        assert ask_calls["count"] == 2
        assert edit_calls["count"] == 1

    def test_edit_back_then_finish(self, monkeypatch):
        """编辑器 ESC 返回后再次进入，最后 Alt+Enter 提交。"""
        ask_calls = {"count": 0}

        def _ask_ui_stub(*a, **kw):
            ask_calls["count"] += 1
            if ask_calls["count"] <= 2:
                return AskResult(answers=[AskAnswer(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                )])
            return AskResult(answers=[AskAnswer(
                selected=[confirm_mod.ConfirmAction.EDIT.value],
                input=None,
            )])

        edit_calls = {"count": 0}

        def _edit_stub(edit_buffer, input=None, output=None, style=None):
            edit_calls["count"] += 1
            if edit_calls["count"] == 1:
                return confirm_mod._EditOutcome(action="back")
            return confirm_mod._EditOutcome(action="finish", text="echo ok")

        monkeypatch.setattr(ask_ui_mod, "ask_ui", _ask_ui_stub)
        monkeypatch.setattr(confirm_mod, "_run_edit_view", _edit_stub)

        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.EDIT
        assert extra == "echo ok"
        assert ask_calls["count"] == 2  # 第二次 ask 返回 EDIT → 编辑视图提交
        assert edit_calls["count"] == 2  # 第一次 back，第二次 finish

    def test_cursor_index_preserved_across_calls(self, monkeypatch):
        """ESC 从编辑视图返回时，ask_ui 焦点回到上次离开时的选项。

        模拟：第一次 ask 焦点移到「编辑」（选项 1）并提交 → 进入编辑
        视图 → ESC 返回 → 第二次 ask 应把焦点放在「编辑」（选项 1）
        上，而非重置到「同意」（选项 0）。
        """
        ask_calls = {"count": 0}
        captured_qs: list = []

        def _ask_ui_stub(*a, **kw):
            ask_calls["count"] += 1
            captured_qs.append(a[0][0])  # AskQuestion
            if ask_calls["count"] == 1:
                return AskResult(answers=[AskAnswer(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                    cursor_index=1,  # 焦点在「编辑」上
                )])
            # 第二次：直接 approve 退出循环
            return AskResult(answers=[AskAnswer(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
                cursor_index=0,
            )])

        # 编辑视图：直接 ESC 返回
        monkeypatch.setattr(ask_ui_mod, "ask_ui", _ask_ui_stub)
        monkeypatch.setattr(confirm_mod, "_run_edit_view",
                            lambda edit_buffer, input=None, output=None, style=None:
                                confirm_mod._EditOutcome(action="back"))

        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.APPROVE
        assert ask_calls["count"] == 2
        # 关键：第二次调用 ask_ui 时传入的 cursor_index 是上次的 1（编辑位置）
        # 而非默认的 0（同意位置）
        assert captured_qs[1].cursor_index == 1

    def test_reject_buffer_preserved_when_esc_to_edit(self, monkeypatch):
        """ESC 从编辑返回时拒绝理由 buffer 仍持有用户输入。

        模拟：用户在拒绝选项输入"想改理由" → ESC 切到其它选项 → 又
        切回拒绝（input 应仍为"想改理由"）。这里通过两次 ask_ui 调用
        都拿到同一 buffer 实例，且 buffer 文本在调用间保持来验证。
        """
        ask_calls = {"count": 0}
        seen_buffers: list = []
        # 模拟用户在自定义选项输入了文本（在第一次 ask_ui 调用前修改 buffer）
        PRELOADED_REASON = "想改理由"

        def _ask_ui_stub(*a, **kw):
            ask_calls["count"] += 1
            buf = a[0][0].custom_buffer
            seen_buffers.append(buf)
            if ask_calls["count"] == 1:
                # 模拟用户在自定义选项输入文本
                buf.text = PRELOADED_REASON
                buf.cursor_position = len(buf.text)
                return AskResult(answers=[AskAnswer(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                    cursor_index=1,
                )])
            # 第二次：直接 approve 退出循环（验证 buffer 仍含先前输入）
            return AskResult(answers=[AskAnswer(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
                cursor_index=0,
            )])

        monkeypatch.setattr(ask_ui_mod, "ask_ui", _ask_ui_stub)
        monkeypatch.setattr(confirm_mod, "_run_edit_view",
                            lambda edit_buffer, input=None, output=None, style=None:
                                confirm_mod._EditOutcome(action="back"))

        confirm_mod.confirm_tool("bash", ToolCategory.UNKNOWN, "echo hi")
        # 关键 1：两次调用拿到**同一** buffer 实例
        assert seen_buffers[0] is seen_buffers[1]
        # 关键 2：buffer 文本经过 ESC 往返后仍保留
        assert seen_buffers[1].text == PRELOADED_REASON

    def test_edit_buffer_state_preserved_across_calls(self, monkeypatch):
        """ESC 返回再进编辑时，buffer 文本与光标位置不重置。

        confirm_tool 持有持久 edit_buffer，每次 _run_edit_view 使用同一实例。
        """
        ask_calls = {"count": 0}

        def _ask_ui_stub(*a, **kw):
            ask_calls["count"] += 1
            # 两次 ask 都返回 EDIT（让 confirm_tool 反复进入编辑视图）
            return AskResult(answers=[AskAnswer(
                selected=[confirm_mod.ConfirmAction.EDIT.value],
                input=None,
            )])

        # 第一次进入：追加 " -x" 后 ESC 返回
        # 第二次进入：buffer 仍含 " -x"，再追加 "Y"，提交
        edit_calls = {"count": 0}
        captured_buffers = []

        def _edit_stub(edit_buffer, input=None, output=None, style=None):
            edit_calls["count"] += 1
            captured_buffers.append(edit_buffer)
            if edit_calls["count"] == 1:
                edit_buffer.text += " -x"
                edit_buffer.cursor_position = len(edit_buffer.text)
                return confirm_mod._EditOutcome(action="back")
            edit_buffer.text += "Y"
            edit_buffer.cursor_position = len(edit_buffer.text)
            return confirm_mod._EditOutcome(action="finish", text=edit_buffer.text)

        monkeypatch.setattr(ask_ui_mod, "ask_ui", _ask_ui_stub)
        monkeypatch.setattr(confirm_mod, "_run_edit_view", _edit_stub)

        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.EDIT
        assert extra == "echo hi -xY"
        # 关键：两次调用使用**同一** edit_buffer 实例（持久 buffer）
        assert captured_buffers[0] is captured_buffers[1]
        assert ask_calls["count"] == 2
        assert edit_calls["count"] == 2

    def test_reject_buffer_state_preserved_across_calls(self, monkeypatch):
        """拒绝时自定义输入 buffer 在多次 ask_ui 调用间保持同一实例。

        通过 EDIT → back 路径触发 confirm_tool 循环，验证两次 ask_ui
        调用收到的 custom_buffer 是同一对象。
        """
        captured_buffers: list = []
        ask_calls = {"count": 0}

        def _ask_ui_stub(*a, **kw):
            captured_buffers.append(a[0][0].custom_buffer)
            ask_calls["count"] += 1
            # 两次都返回 EDIT；confirm_tool 会反复进入 _run_edit_view
            return AskResult(answers=[AskAnswer(
                selected=[confirm_mod.ConfirmAction.EDIT.value],
                input=None,
            )])

        # 第一次编辑：ESC 返回（→ 重新询问）
        # 第二次编辑：直接提交，避免无谓循环
        edit_calls = {"count": 0}

        def _edit_stub(edit_buffer, input=None, output=None, style=None):
            edit_calls["count"] += 1
            if edit_calls["count"] == 1:
                return confirm_mod._EditOutcome(action="back")
            return confirm_mod._EditOutcome(action="finish", text="echo ok")

        monkeypatch.setattr(ask_ui_mod, "ask_ui", _ask_ui_stub)
        monkeypatch.setattr(confirm_mod, "_run_edit_view", _edit_stub)

        confirm_mod.confirm_tool("bash", ToolCategory.UNKNOWN, "echo hi")
        # 关键：confirm_tool 持 ask_buffer 持久实例，每次 ask_ui 调用都
        # 传入**同一** custom_buffer；否则 ESC 返回后用户输入会丢失。
        assert ask_calls["count"] == 2
        assert edit_calls["count"] == 2
        assert captured_buffers[0] is captured_buffers[1]

    def test_reject_input_preserved_when_reselecting(self, monkeypatch):
        """用户输入拒绝理由并提交，confirm_tool 能取到该字符串。"""
        from prompt_toolkit.buffer import Buffer

        # 第一次 ask_ui：模拟用户输入"想改理由"到 custom_buffer，再提交
        # （直接修改 buffer 模拟交互效果）
        ask_calls = {"count": 0}

        def _ask_ui_stub(*a, **kw):
            ask_calls["count"] += 1
            buf: Buffer = a[0][0].custom_buffer
            if ask_calls["count"] == 1:
                # 模拟用户在自定义选项输入文字
                buf.text = "想改理由"
                buf.cursor_position = len(buf.text)
                return AskResult(answers=[AskAnswer(
                    selected=[confirm_mod.ConfirmAction.REJECT.value],
                    input="想改理由",
                )])
            return AskResult(answers=[AskAnswer(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
            )])

        monkeypatch.setattr(ask_ui_mod, "ask_ui", _ask_ui_stub)
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT
        assert extra == "想改理由"
        assert ask_calls["count"] == 1


# ===================================================================
# 选项构造：保证 ask_ui 收到的选项结构正确
# ===================================================================

class TestBuildConfirmOptions:
    def test_bash_includes_edit(self):
        """bash 工具提供 编辑 + 同意 + 拒绝（自定义）三项。"""
        opts = confirm_mod._build_confirm_options(show_edit=True)
        assert len(opts) == 3
        assert opts[0].value == confirm_mod.ConfirmAction.APPROVE.value
        assert opts[1].value == confirm_mod.ConfirmAction.EDIT.value
        assert opts[2].is_custom is True
        assert opts[2].value == confirm_mod.ConfirmAction.REJECT.value
        assert opts[2].description  # placeholder 文本

    def test_non_bash_excludes_edit(self):
        """非 bash 工具不显示 编辑。"""
        opts = confirm_mod._build_confirm_options(show_edit=False)
        assert len(opts) == 2
        values = [o.value for o in opts]
        assert values == [
            confirm_mod.ConfirmAction.APPROVE.value,
            confirm_mod.ConfirmAction.REJECT.value,
        ]
        # 拒绝仍是末尾自定义选项
        assert opts[-1].is_custom is True


# ===================================================================
# 编辑视图（独立多行）：验证最基本的 Application 构造与提交
# ===================================================================

class TestRunEditView:
    def _make_buffer(self, text="echo hi"):
        from prompt_toolkit.buffer import Buffer
        b = Buffer(multiline=True)
        b.text = text
        b.cursor_position = len(text)
        return b

    def test_returns_text_after_alt_enter(self):
        """Alt+Enter 提交编辑，返回 finish + 文本。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        buf = self._make_buffer("echo hi")
        with create_pipe_input() as inp:
            inp.send_text("\x1b\r")  # Alt+Enter
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        assert result.action == "finish"
        assert result.text == "echo hi"

    def test_ctrl_c_returns_abort(self):
        """Ctrl-C 中止编辑，返回 abort。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        buf = self._make_buffer("echo hi")
        with create_pipe_input() as inp:
            inp.send_text("\x03")
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        assert result.action == "abort"

    def test_escape_returns_back(self):
        """ESC 返回编辑视图，返回 back。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        buf = self._make_buffer("echo hi")
        with create_pipe_input() as inp:
            inp.send_text("\x1b")  # ESC
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        assert result.action == "back"

    def test_multiline_preserved(self):
        """多行命令往返一致。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        cmd = "cat <<EOF\nline1\nline2\nEOF"
        buf = self._make_buffer(cmd)
        with create_pipe_input() as inp:
            inp.send_text("\x1b\r")
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        assert result.action == "finish"
        assert result.text == cmd

    def test_buffer_state_preserved_across_calls(self):
        """ESC 返回再进入后，buffer 文本与光标位置不重置。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        cmd = "echo hi"
        buf = self._make_buffer(cmd)
        # 第一次进入：在末尾追加 " -x"，光标到末尾，再 ESC 返回
        with create_pipe_input() as inp:
            inp.send_text(" -x\x1b")  # 输入 -x 后 ESC
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        assert result.action == "back"
        assert buf.text == "echo hi -x"
        assert buf.cursor_position == len("echo hi -x")

        # 第二次进入：在原光标位置（末尾）插入 "Y"，再 ESC 返回
        prev_pos = buf.cursor_position
        with create_pipe_input() as inp:
            inp.send_text("Y\x1b")
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        assert result.action == "back"
        # 第二次进入时光标继承自上一次（末尾）；插入 Y 在末尾
        assert buf.text == "echo hi -xY"
        assert buf.cursor_position == prev_pos + 1

    def _edit_seq(self, *keys):
        """提交式按键序列：拼接按键后以 Alt+Enter 收尾提交。"""
        return "".join(keys) + "\x1b\r"

    def _run_edit(self, seq, cmd):
        """用指定按键序列运行编辑视图，返回 (action, text)。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        buf = self._make_buffer(cmd)
        with create_pipe_input() as inp:
            inp.send_text(seq)
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        return result.action, result.text

    def test_edit_first_line_start(self):
        """第一行行首插入：光标回到第一行开头后插入 X。"""
        # 光标默认在末尾；先 Home 到第一行（或 c-a 行首）
        # 多行下用 c-p 上移两次到第一行，再 c-a 到行首，插入 X
        action, text = self._run_edit(
            self._edit_seq("\x10\x10\x01X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "Xaaaa\nbbbb\ncccc"

    def test_edit_first_line_middle(self):
        """第一行中间插入。"""
        # 第一行行首 + 右移 1 字符到第 2 列插入 X
        action, text = self._run_edit(
            self._edit_seq("\x10\x10\x01\x06X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aXaaa\nbbbb\ncccc"

    def test_edit_first_line_end(self):
        """第一行末尾插入。"""
        # 第一行行首 + c-e 到行尾，末尾插入 X
        action, text = self._run_edit(
            self._edit_seq("\x10\x10\x01\x05X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aaaaX\nbbbb\ncccc"

    def test_edit_last_line_start(self):
        """最后一行行首插入。"""
        # 光标默认在最后一行末尾，直接 c-a 是行首
        action, text = self._run_edit(
            self._edit_seq("\x01X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aaaa\nbbbb\nXcccc"

    def test_edit_last_line_end(self):
        """最后一行末尾插入：光标默认在末尾，直接输入 X。"""
        action, text = self._run_edit(
            self._edit_seq("X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aaaa\nbbbb\nccccX"

    def test_edit_last_line_middle(self):
        """最后一行行中插入。"""
        action, text = self._run_edit(
            self._edit_seq("\x01\x06X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aaaa\nbbbb\ncXccc"

    def test_edit_middle_line_start(self):
        """中间行行首插入。"""
        # 上移一次到第 2 行，c-a 行首插入
        action, text = self._run_edit(
            self._edit_seq("\x10\x01X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aaaa\nXbbbb\ncccc"

    def test_edit_middle_line_middle(self):
        """中间行行中插入。"""
        action, text = self._run_edit(
            self._edit_seq("\x10\x01\x06X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aaaa\nbXbbb\ncccc"

    def test_edit_middle_line_end(self):
        """中间行行尾插入。"""
        action, text = self._run_edit(
            self._edit_seq("\x10\x01\x05X"), "aaaa\nbbbb\ncccc")
        assert action == "finish"
        assert text == "aaaa\nbbbbX\ncccc"

    def test_multiline_command_preserved(self):
        """多行命令在编辑中保持完整，Alt+Enter 提交后原样返回。"""
        cmd = "cat <<EOF\nline1\nline2\nEOF"
        action, text = self._run_edit(self._edit_seq(), cmd)
        assert action == "finish"
        assert text == cmd

    def test_alt_enter_submits_multiline_but_enter_does_not(self):
        """普通 Enter 不提交（多行换行），Alt+Enter 提交。"""
        cmd = "cat <<EOF\nx\nEOF"
        # 只按 Enter（换行）再 Alt+Enter 提交：Enter 应插入换行而非提交
        buf = self._make_buffer(cmd)
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            inp.send_text("\r\x1b\r")  # Enter 换行 + Alt+Enter 提交
            result = confirm_mod._run_edit_view(
                buf, input=inp, output=DummyOutput())
        assert result.action == "finish"
        # 末尾 Enter 插入一个换行
        assert result.text == cmd + "\n"


# ===================================================================
# 编辑视图布局
# ===================================================================

class TestEditViewLayout:
    """编辑视图布局（classic / default 两风格分别验证）。

    提示符经 ``BeforeInput`` processor 只加在输入首行行首（用当前模式
    样式类着色），后续行顶格不缩进（同 cli 提示词输入框的
    ``prompt_continuation=''``）：

    - classic：根为 HSplit，仅含输入框窗口（挂 mycode-input），提示符
      “编辑 >> ” 内嵌于输入首行；
    - default：根为 HSplit = 标题行 “编辑待执行命令：”（灰底块外）+
      上留白行 + 输入行（挂 mycode-input）+ 下留白行；提示符为纯竖线
      “│ ”（不带模式标记 ? / !）。
    """

    @pytest.fixture(autouse=True)
    def reset_mode(self):
        MODE_STATE.set(Mode.AUTO)
        yield
        MODE_STATE.set(Mode.AUTO)

    # ---- 公共：提示符 processor ----

    def _prompt_proc(self, container):
        """取输入窗口 BufferControl 的 BeforeInput processor。"""
        from prompt_toolkit.layout.containers import HSplit
        from prompt_toolkit.layout.controls import BufferControl
        from prompt_toolkit.layout.processors import BeforeInput

        def _walk(node):
            if isinstance(node, HSplit):
                for ch in node.children:
                    yield from _walk(ch)
            else:
                yield node

        for win in _walk(container):
            ctrl = getattr(win, "content", None)
            if isinstance(ctrl, BufferControl):
                for proc in ctrl.input_processors:
                    if isinstance(proc, BeforeInput):
                        return proc
        raise AssertionError("未找到 BeforeInput processor")

    def _prompt_frags(self, container):
        """BeforeInput 的 (fragments, 纯文本)。"""
        proc = self._prompt_proc(container)
        frags = proc.text
        return frags, "".join(t for _, t in frags)

    def test_prompt_only_on_first_line(self, monkeypatch):
        """提示符是 BeforeInput processor（只作用于首行），后续行不缩进。"""
        from prompt_toolkit.layout.processors import BeforeInput
        for style in ("classic", "default"):
            assert isinstance(self._prompt_proc(_edit_view_container(style=style)),
                              BeforeInput)

    # ---- classic ----

    def test_classic_root_is_hsplit_single_input(self):
        """classic：根 HSplit 单输入窗口（挂 mycode-input），无标题/留白。"""
        from prompt_toolkit.layout.containers import HSplit
        container = _edit_view_container(style="classic")
        assert isinstance(container, HSplit)
        assert len(container.children) == 1
        assert container.style == "class:mycode-input"

    def test_classic_prompt_text_and_mode_style(self):
        """classic：提示符 “编辑 >> ” 文本不变，样式类随模式。"""
        cases = [
            (Mode.AUTO, "class:mycode-prompt"),
            (Mode.ASK, "class:mycode-prompt-ask"),
            (Mode.YOLO, "class:mycode-prompt-yolo"),
        ]
        for mode, style_cls in cases:
            MODE_STATE.set(mode)
            container = _edit_view_container(style="classic")
            frags, text = self._prompt_frags(container)
            assert text == "编辑 >> "
            assert (style_cls, "编辑 >> ") in frags

    # ---- default ----

    def test_default_root_is_hsplit_with_title_and_blanks(self):
        """default：根 HSplit = 标题行 + 上空行 + 输入行 + 下空行。"""
        from prompt_toolkit.layout.containers import HSplit, Window
        container = _edit_view_container(style="default")
        assert isinstance(container, HSplit)
        children = container.children
        assert len(children) == 4
        title_win, top_blank, input_row, bottom_blank = children
        # 标题行在最上方（灰底块之外）：“编辑待执行命令：”、无背景样式；
        # 根容器也不挂 mycode-input（否则 parent_style 会给标题行下发灰底）
        assert isinstance(title_win, Window)
        assert _ftc_text(title_win)[1] == "编辑待执行命令："
        assert title_win.style in (None, "")
        assert container.style in (None, "")
        # 上下空行：1 行高、mycode-input 背景
        for blank in (top_blank, bottom_blank):
            assert isinstance(blank, Window)
            assert blank.height.min == 1 and blank.height.max == 1
            assert blank.style == "class:mycode-input"
        # 输入行挂输入区背景
        assert input_row.style == "class:mycode-input"

    def test_default_prompt_bar_without_mode_mark(self):
        """default：提示符为纯竖线 “│ ”（不带 ?/!），样式类随模式。"""
        cases = [
            (Mode.AUTO, "class:mycode-prompt"),
            (Mode.ASK, "class:mycode-prompt-ask"),
            (Mode.YOLO, "class:mycode-prompt-yolo"),
        ]
        for mode, style_cls in cases:
            MODE_STATE.set(mode)
            container = _edit_view_container(style="default")
            frags, text = self._prompt_frags(container)
            assert text == "│ "
            assert (style_cls, "│ ") in frags

    def test_default_bar_reuses_prompt_prefix(self):
        """default：竖线复用 renderer 的 prompt_prefix（去掉模式标记）。"""
        from mycode.renderer import _get_renderer
        MODE_STATE.set(Mode.ASK)
        container = _edit_view_container(style="default")
        # prompt_prefix("ask") = "│?"，编辑提示符去掉标记只剩 "│"
        assert self._prompt_frags(container)[1].rstrip() \
            == _get_renderer().prompt_prefix(Mode.ASK)[:1]

    def test_default_input_window_is_buffer_control(self):
        """default：输入行是 BufferControl（多行编辑 buffer）。"""
        from prompt_toolkit.layout.containers import HSplit
        from prompt_toolkit.layout.controls import BufferControl
        container = _edit_view_container(style="default")
        assert isinstance(container.children[2].children[0].content, BufferControl)

    def test_default_blanks_share_mycode_input_style(self):
        """default：留白行与输入行挂 class:mycode-input，根容器不带。"""
        container = _edit_view_container(style="default")
        # 根容器不挂背景样式（标题行在块外，不能继承灰底）
        assert container.style in (None, "")
        assert container.children[1].style == "class:mycode-input"
        assert container.children[2].style == "class:mycode-input"
        assert container.children[-1].style == "class:mycode-input"

    def test_render_continuation_lines_not_indented(self):
        """渲染级验证：多行命令仅首行带提示符，后续行顶格不缩进。"""
        import asyncio
        import re
        import threading
        from collections import namedtuple
        from unittest.mock import patch
        import mycode.renderer as renderer_mod
        from mycode.confirm import _run_edit_view
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from prompt_toolkit.renderer import Renderer

        cmd = "cat <<'EOF'\nhello\nEOF"

        def _render(style_name: str) -> list[str]:
            """在专用线程的新事件循环内渲染一帧，返回非空纯文本行。

            线程隔离是必须的：其他用例真实跑过 ``app.run()`` 后主线程的
            事件循环会被关闭，主线程 ``run_until_complete`` 会抛
            ``RuntimeError``；新线程里 ``new_event_loop`` 不受影响。
            """
            result: dict = {}

            def _work():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    saved = renderer_mod.RENDER_STYLE
                    renderer_mod.RENDER_STYLE = style_name
                    Size = namedtuple("Size", "rows columns")
                    buf = io.StringIO()
                    out = Vt100_Output(
                        stdout=buf,
                        get_size=lambda: Size(24, 80),
                        term="xterm-256color",
                    )
                    seen: dict = {}

                    class _F(confirm_mod.Application):
                        def __init__(self, *a, **kw):
                            super().__init__(*a, **kw)
                            seen["app"] = self

                        def run(self):
                            return None

                    b = Buffer(multiline=True, history=None)
                    b.text = cmd
                    with create_pipe_input() as inp:
                        inp.send_text("\x03")
                        with patch("mycode.confirm.Application", _F):
                            _run_edit_view(b, input=inp, output=out)
                    app = seen["app"]
                    style = renderer_mod._get_renderer().create_prompt_style()
                    renderer = Renderer(style=style, output=out)

                    async def _go():
                        from prompt_toolkit.application.current import set_app
                        with set_app(app):
                            renderer.render(app, app.layout, is_done=False)

                    loop.run_until_complete(_go())
                    result["lines"] = [
                        plain.rstrip()
                        for plain in (
                            re.sub(
                                r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07",
                                "", line,
                            )
                            for line in buf.getvalue().split("\r\n")
                        )
                        if plain.strip()
                    ]
                finally:
                    renderer_mod.RENDER_STYLE = saved
                    loop.close()
                    asyncio.set_event_loop(None)

            t = threading.Thread(target=_work)
            t.start()
            t.join()
            assert "lines" in result, "渲染线程异常退出"
            return result["lines"]

        # default：标题行 + 首行竖线 + 后续行顶格
        default_lines = _render("default")
        assert default_lines[0] == "编辑待执行命令："
        assert default_lines[1] == "│ cat <<'EOF'"
        assert default_lines[2:] == ["hello", "EOF"]

        # classic：首行提示符 + 后续行顶格
        classic_lines = _render("classic")
        assert classic_lines[0] == "编辑 >> cat <<'EOF'"
        assert classic_lines[1:] == ["hello", "EOF"]


# ===================================================================
# 格式化辅助
# ===================================================================

class TestFormatHelpers:
    def test_format_reject(self):
        assert confirm_mod.format_reject("理由") == "Error: 用户拒绝执行：理由"

    def test_format_reject_no_reason(self):
        assert confirm_mod.format_reject_no_reason() == "Error: 用户拒绝执行，未提供理由"

    def test_format_cancel(self):
        assert confirm_mod.format_cancel() == "Error: 用户取消操作"


class TestEditViewStyle:
    """编辑视图输入区样式（与 cli 输入区共用 class:mycode-input 背景）。

    - classic：根 VSplit 挂 class:mycode-input（样式表里该类为空）；
    - default：根 HSplit 不带背景样式（标题行在灰底块外），背景挂到
      留白行与 VSplit 输入行。
    """

    def test_classic_root_has_mycode_input_style(self):
        """classic：根 VSplit 挂上 class:mycode-input。"""
        container = _edit_view_container(style="classic")
        assert container.style == "class:mycode-input"

    def test_default_background_not_on_root(self):
        """default：根容器不挂背景样式（标题行在灰底块外）。"""
        container = _edit_view_container(style="default")
        assert container.style in (None, "")

    def test_edit_view_receives_style(self):
        """_run_edit_view 将 style 透传给 Application。"""
        from unittest.mock import patch
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit.styles import Style

        seen: dict = {}

        class _FakeApp(Application):
            def __init__(self, *a, **kw):
                seen.update(kw)
                super().__init__(*a, **kw)

            def run(self):
                return None

        sentinel = Style.from_dict({"mycode-input": "bg:#abcdef"})
        buf = Buffer(multiline=True)
        with create_pipe_input() as inp:
            inp.send_text("\x03")
            with patch("mycode.confirm.Application", _FakeApp):
                confirm_mod._run_edit_view(
                    buf, input=inp, output=DummyOutput(), style=sentinel)
        assert seen.get("style") is sentinel

    def test_edit_view_has_erase_when_done(self):
        """_run_edit_view 的 Application 带 erase_when_done=True（不残留上屏）。"""
        from unittest.mock import patch
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        seen: dict = {}

        class _FakeApp(Application):
            def __init__(self, *a, **kw):
                seen.update(kw)
                super().__init__(*a, **kw)

            def run(self):
                return None

        buf = Buffer(multiline=True)
        with create_pipe_input() as inp:
            inp.send_text("\x03")
            with patch("mycode.confirm.Application", _FakeApp):
                confirm_mod._run_edit_view(buf, input=inp, output=DummyOutput())
        assert seen.get("erase_when_done") is True
