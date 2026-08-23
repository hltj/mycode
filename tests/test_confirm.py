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

import pytest

import mycode.confirm as confirm_mod
import mycode.ask_ui as ask_ui_mod
from mycode.ask_ui import AskOption, AskResult
from mycode.mode import ToolCategory


# ===================================================================
# 动作映射：confirm_tool 通过 mock ask_ui 验证
# ===================================================================

class TestConfirmToolMapping:
    def test_approve(self, monkeypatch):
        """选中「同意」→ ``APPROVE``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(
                                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                                input=None,
                            ).to_dict())
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.APPROVE
        assert extra is None

    def test_reject_with_reason(self, monkeypatch):
        """选中「拒绝」+ 输入理由 → ``REJECT``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(
                                selected=[confirm_mod.ConfirmAction.REJECT.value],
                                input="不想执行",
                            ).to_dict())
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT
        assert extra == "不想执行"

    def test_reject_with_whitespace_reason_is_no_reason(self, monkeypatch):
        """选中「拒绝」+ 仅空白 → 视作无理由 → ``REJECT_NO_REASON``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(
                                selected=[confirm_mod.ConfirmAction.REJECT.value],
                                input="   ",
                            ).to_dict())
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT_NO_REASON
        assert extra is None

    def test_reject_without_reason(self, monkeypatch):
        """选中「拒绝」+ 输入框为空 → ``REJECT_NO_REASON``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(
                                selected=[confirm_mod.ConfirmAction.REJECT.value],
                                input="",
                            ).to_dict())
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.REJECT_NO_REASON
        assert extra is None

    def test_abort_returns_cancel(self, monkeypatch):
        """abort（selected 空）→ ``CANCEL``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(
                                selected=[], input=None, aborted=True,
                            ).to_dict())
        action, extra = confirm_mod.confirm_tool(
            "bash", ToolCategory.UNKNOWN, "echo hi")
        assert action == confirm_mod.ConfirmAction.CANCEL
        assert extra is None

    def test_edit_flow(self, monkeypatch):
        """选中「编辑」→ 调编辑器 + 返回 ``EDIT``。"""
        monkeypatch.setattr(ask_ui_mod, "ask_ui",
                            lambda *a, **kw: AskResult(
                                selected=[confirm_mod.ConfirmAction.EDIT.value],
                                input=None,
                            ).to_dict())
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
                            lambda *a, **kw: AskResult(
                                selected=[confirm_mod.ConfirmAction.EDIT.value],
                                input=None,
                            ).to_dict())
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
                return AskResult(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                ).to_dict()
            return AskResult(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
            ).to_dict()

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
                return AskResult(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                ).to_dict()
            return AskResult(
                selected=[confirm_mod.ConfirmAction.EDIT.value],
                input=None,
            ).to_dict()

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
        captured_kwargs: list = []

        def _ask_ui_stub(*a, **kw):
            ask_calls["count"] += 1
            captured_kwargs.append(dict(kw))
            if ask_calls["count"] == 1:
                return AskResult(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                    cursor_index=1,  # 焦点在「编辑」上
                ).to_dict()
            # 第二次：直接 approve 退出循环
            return AskResult(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
                cursor_index=0,
            ).to_dict()

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
        assert captured_kwargs[1]["cursor_index"] == 1

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
            buf = kw.get("custom_buffer")
            seen_buffers.append(buf)
            if ask_calls["count"] == 1:
                # 模拟用户在自定义选项输入文本
                buf.text = PRELOADED_REASON
                buf.cursor_position = len(buf.text)
                return AskResult(
                    selected=[confirm_mod.ConfirmAction.EDIT.value],
                    input=None,
                    cursor_index=1,
                ).to_dict()
            # 第二次：直接 approve 退出循环（验证 buffer 仍含先前输入）
            return AskResult(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
                cursor_index=0,
            ).to_dict()

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
            return AskResult(
                selected=[confirm_mod.ConfirmAction.EDIT.value],
                input=None,
            ).to_dict()

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
            captured_buffers.append(kw.get("custom_buffer"))
            ask_calls["count"] += 1
            # 两次都返回 EDIT；confirm_tool 会反复进入 _run_edit_view
            return AskResult(
                selected=[confirm_mod.ConfirmAction.EDIT.value],
                input=None,
            ).to_dict()

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
            buf: Buffer = kw.get("custom_buffer")
            if ask_calls["count"] == 1:
                # 模拟用户在自定义选项输入文字
                buf.text = "想改理由"
                buf.cursor_position = len(buf.text)
                return AskResult(
                    selected=[confirm_mod.ConfirmAction.REJECT.value],
                    input="想改理由",
                ).to_dict()
            return AskResult(
                selected=[confirm_mod.ConfirmAction.APPROVE.value],
                input=None,
            ).to_dict()

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
    """编辑视图布局：提示符 + 多行输入框（与 cli 输入区共用背景）。"""

    def test_layout_root_is_vsplit(self):
        """编辑视图根容器是 VSplit（提示 + 输入框）。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout.containers import VSplit
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from unittest.mock import patch
        from prompt_toolkit.application import Application

        seen_layout = {}

        class _FakeApp(Application):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                seen_layout["container"] = self.layout.container

            def run(self):
                return None

        buf = Buffer(multiline=True)
        with create_pipe_input() as inp:
            inp.send_text("\x03")
            with patch("mycode.confirm.Application", _FakeApp):
                confirm_mod._run_edit_view(buf, input=inp, output=DummyOutput())
        # 根容器是 VSplit（编辑 >> 提示 + 输入框）
        assert isinstance(seen_layout["container"], VSplit)

    def test_edit_prompt_and_input_side_by_side(self):
        """「编辑 >> 」提示与输入框同行并列（VSplit 两个子元素）。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout.containers import VSplit, Window
        from prompt_toolkit.layout.controls import BufferControl
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from unittest.mock import patch
        from prompt_toolkit.application import Application

        seen_layout = {}

        class _FakeApp(Application):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                seen_layout["container"] = self.layout.container

            def run(self):
                return None

        buf = Buffer(multiline=True)
        with create_pipe_input() as inp:
            inp.send_text("\x03")
            with patch("mycode.confirm.Application", _FakeApp):
                confirm_mod._run_edit_view(buf, input=inp, output=DummyOutput())
        container = seen_layout["container"]
        assert isinstance(container, VSplit)
        # 两个子元素：提示（Window）+ 输入框（Window）
        assert len(container.children) == 2
        prompt_win, input_win = container.children
        assert isinstance(prompt_win, Window)
        assert isinstance(input_win, Window)
        assert isinstance(input_win.content, BufferControl)

    def test_edit_prompt_adjacent_to_input(self):
        """「编辑 >> 」提示紧挨输入框：提示文本含尾随空格。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout.containers import VSplit, Window
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from unittest.mock import patch
        from prompt_toolkit.application import Application

        seen_layout = {}

        class _FakeApp(Application):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                seen_layout["container"] = self.layout.container

            def run(self):
                return None

        buf = Buffer(multiline=True)
        buf.text = "echo hi"
        with create_pipe_input() as inp:
            inp.send_text("\x03")
            with patch("mycode.confirm.Application", _FakeApp):
                confirm_mod._run_edit_view(buf, input=inp, output=DummyOutput())
        container = seen_layout["container"]
        prompt_win = container.children[0]
        control = prompt_win.content
        assert isinstance(control, FormattedTextControl)
        frags = control.text if hasattr(control, "text") else control()
        text = "".join(t for _, t in frags) if isinstance(frags, list) else str(frags)
        # 「编辑 >> 」含一个尾随空格，贴合输入框
        assert text == "编辑 >> "


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
    """编辑视图根容器挂上 class:mycode-input（与 cli 输入区共用背景）。"""

    def test_root_layout_has_mycode_input_style(self):
        """直接构造编辑视图布局，根 VSplit 挂上 class:mycode-input。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.layout.containers import VSplit
        from prompt_toolkit.application import Application
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from unittest.mock import patch

        seen_root = {}

        class _FakeApp(Application):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                seen_root["style"] = self.layout.container.style

            def run(self):
                return None

        buf = Buffer(multiline=True)
        buf.text = "echo hi"

        with create_pipe_input() as inp:
            inp.send_text("\x03")  # Ctrl-C 立即退出
            with patch("mycode.confirm.Application", _FakeApp):
                confirm_mod._run_edit_view(buf, input=inp, output=DummyOutput())
        # 关键：编辑视图根容器挂上 class:mycode-input
        assert seen_root["style"] == "class:mycode-input"

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
