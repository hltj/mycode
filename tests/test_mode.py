"""
模式与权限系统测试。

覆盖：
- mode.py：工具分类、needs_confirmation 决策矩阵、模式切换
- session.py：ModeChangeEvent 持久化往返、SessionData.mode 字段
- renderer.py：三种模式下的提示符片段
- cli.py：模式切换命令、dangerous 拒绝、取消/无理由拒绝跳出
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import mycode.mode as mode_mod
import mycode.confirm as confirm_mod
import mycode.renderer as renderer
import mycode.cli as cli
from mycode.mode import (
    Mode,
    ModeState,
    MODE_STATE,
    ToolCategory,
    classify_tool,
    needs_confirmation,
    is_bash_tool,
)
from mycode.session import (
    ModeChangeEvent,
    SessionHistory,
    _dict_to_agent_message,
    _msg_to_dict,
)


# ===================================================================
# 工具分类
# ===================================================================

class TestClassifyTool:
    def test_bash_dangerous(self, monkeypatch):
        monkeypatch.setenv("BASH_DANGEROUS", "sudo,rm -rf")
        assert classify_tool("bash", {"command": "sudo make"}) == ToolCategory.DANGEROUS
        assert classify_tool("bash", {"command": "rm -rf /"}) == ToolCategory.DANGEROUS

    def test_bash_caution(self, monkeypatch):
        monkeypatch.setenv("BASH_DANGEROUS", "sudo")
        monkeypatch.setenv("BASH_CAUTION", "rm")
        # "rm file" 命中注意（不含危险 sudo/rm -rf）
        assert classify_tool("bash", {"command": "rm file.txt"}) == ToolCategory.CAUTION

    def test_bash_unknown(self, monkeypatch):
        monkeypatch.setenv("BASH_DANGEROUS", "")
        monkeypatch.setenv("BASH_CAUTION", "")
        assert classify_tool("bash", {"command": "echo hi"}) == ToolCategory.UNKNOWN

    def test_write_tools(self):
        for name in ("write", "edit", "patch"):
            assert classify_tool(name, {}) == ToolCategory.WRITE

    def test_read_tools(self):
        for name in ("ls", "glob", "grep", "read"):
            assert classify_tool(name, {}) == ToolCategory.READ

    def test_internal_tool(self):
        assert classify_tool("todo_write", {}) == ToolCategory.INTERNAL

    def test_unknown_tool_name(self):
        assert classify_tool("unknown_tool", {}) == ToolCategory.UNKNOWN

    def test_bash_missing_command(self):
        monkey = pytest.MonkeyPatch()
        monkey.setenv("BASH_DANGEROUS", "")
        monkey.setenv("BASH_CAUTION", "")
        assert classify_tool("bash", {}) == ToolCategory.UNKNOWN
        monkey.undo()


# ===================================================================
# 决策矩阵
# ===================================================================

class TestNeedsConfirmation:
    def test_dangerous_never_confirmed(self):
        # 危险统一拒绝，不进入确认判定
        for m in Mode:
            assert needs_confirmation(m, ToolCategory.DANGEROUS) is False

    def test_ask_mode(self):
        assert needs_confirmation(Mode.ASK, ToolCategory.INTERNAL) is False
        assert needs_confirmation(Mode.ASK, ToolCategory.READ) is False
        assert needs_confirmation(Mode.ASK, ToolCategory.WRITE) is True
        assert needs_confirmation(Mode.ASK, ToolCategory.UNKNOWN) is True
        assert needs_confirmation(Mode.ASK, ToolCategory.CAUTION) is True

    def test_auto_mode(self):
        assert needs_confirmation(Mode.AUTO, ToolCategory.INTERNAL) is False
        assert needs_confirmation(Mode.AUTO, ToolCategory.READ) is False
        assert needs_confirmation(Mode.AUTO, ToolCategory.WRITE) is False
        assert needs_confirmation(Mode.AUTO, ToolCategory.UNKNOWN) is False
        assert needs_confirmation(Mode.AUTO, ToolCategory.CAUTION) is True

    def test_yolo_mode(self):
        for cat in (ToolCategory.INTERNAL, ToolCategory.READ,
                    ToolCategory.WRITE, ToolCategory.UNKNOWN, ToolCategory.CAUTION):
            assert needs_confirmation(Mode.YOLO, cat) is False


class TestModeState:
    def test_default_auto(self):
        assert ModeState().get() == Mode.AUTO

    def test_cycle(self):
        st = ModeState(Mode.AUTO)
        assert st.cycle() == Mode.YOLO
        assert st.cycle() == Mode.ASK
        assert st.cycle() == Mode.AUTO

    def test_set_get(self):
        st = ModeState()
        st.set(Mode.YOLO)
        assert st.get() == Mode.YOLO


# ===================================================================
# ModeChangeEvent 持久化
# ===================================================================

class TestModeChangePersistence:
    def test_roundtrip(self):
        ev = ModeChangeEvent(model="m", mode="ask")
        ev.id = "x"
        ev.time = "t"
        d = _msg_to_dict(ev)
        assert d["type"] == "mode_change"
        assert d["mode"] == "ask"
        loaded = _dict_to_agent_message(d)
        assert isinstance(loaded, ModeChangeEvent)
        assert loaded.mode == "ask"

    def test_session_records_mode(self, tmp_path):
        sh = SessionHistory(cwd=str(tmp_path), model="m")
        assert sh.mode == Mode.AUTO

    def test_session_mode_roundtrip(self, tmp_path):
        sh = SessionHistory(cwd=str(tmp_path), model="m")
        # 切换模式并持久化为 mode_change 事件
        sh.mode = Mode.YOLO
        sh.append(ModeChangeEvent(model="m", mode="yolo"))
        loaded = SessionHistory.load(sh.file_path)
        # 从最后一次 ModeChangeEvent 恢复模式
        assert loaded.mode == Mode.YOLO
        # 通过 get_messages 不应包含 mode_change（不进模型上下文）
        assert all(m.get("role") != "tool" for m in loaded.get_messages())

    def test_session_mode_roundtrip_no_change_event(self, tmp_path):
        """无 ModeChangeEvent 时回退到 session 记录初值。"""
        sh = SessionHistory(cwd=str(tmp_path), model="m")
        loaded = SessionHistory.load(sh.file_path)
        assert loaded.mode == Mode.AUTO


# ===================================================================
# 提示符（renderer）
# ===================================================================

class TestPromptFragmentsByMode:
    @pytest.fixture(autouse=True)
    def reset_mode(self):
        MODE_STATE.set(Mode.AUTO)
        yield

    def test_classic_auto(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        MODE_STATE.set(Mode.AUTO)
        assert renderer._prompt_fragments() == [('class:mycode-prompt', 'myc[自动] > ')]

    def test_classic_ask(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        MODE_STATE.set(Mode.ASK)
        assert renderer._prompt_fragments() == [('class:mycode-prompt-ask', 'myc[询问] > ')]

    def test_classic_yolo(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        MODE_STATE.set(Mode.YOLO)
        assert renderer._prompt_fragments() == [('class:mycode-prompt-yolo', 'myc[全权] > ')]

    def test_default_auto(self):
        MODE_STATE.set(Mode.AUTO)
        assert renderer._prompt_fragments() == [('class:mycode-prompt', '│ ')]

    def test_default_ask(self):
        MODE_STATE.set(Mode.ASK)
        assert renderer._prompt_fragments() == [('class:mycode-prompt-ask', '│? ')]

    def test_default_yolo(self):
        MODE_STATE.set(Mode.YOLO)
        assert renderer._prompt_fragments() == [('class:mycode-prompt-yolo', '│! ')]


class TestUserMessagePromptColorByMode:
    """用户消息提示符颜色跟随当前模式。"""

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def _render_user(self, style):
        import mycode.renderer as renderer_mod
        # 直接调用 _render_common 渲染 UserMessage（mode 取当前 MODE_STATE）
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage
        ev = UserMessage(
            model="m",
            message=ChatCompletionUserMessageParam(role="user", content="hi"),
            mode=MODE_STATE.get().value,
        )
        saved = renderer_mod.RENDER_STYLE
        renderer_mod.RENDER_STYLE = style
        try:
            return self._capture(lambda: renderer_mod._render_common(ev))
        finally:
            renderer_mod.RENDER_STYLE = saved

    def test_classic_color_follows_mode(self):
        for mode, color in [
            (Mode.AUTO, mode_mod.MODE_COLOR[Mode.AUTO]),
            (Mode.ASK, mode_mod.MODE_COLOR[Mode.ASK]),
            (Mode.YOLO, mode_mod.MODE_COLOR[Mode.YOLO]),
        ]:
            MODE_STATE.set(mode)
            label = mode.label
            out = self._render_user("classic")
            assert f"{color}myc[{label}] > \x1B[0mhi" in out

    def test_default_bar_color_follows_mode(self):
        import os
        old = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = "10"
        try:
            for mode, color, bar in [
                (Mode.AUTO, mode_mod.MODE_COLOR[Mode.AUTO], "│"),
                (Mode.ASK, mode_mod.MODE_COLOR[Mode.ASK], "│?"),
                (Mode.YOLO, mode_mod.MODE_COLOR[Mode.YOLO], "│!"),
            ]:
                MODE_STATE.set(mode)
                out = self._render_user("default")
                assert f"{color}{bar}\x1B[39;22m hi" in out
        finally:
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old

    def test_default_user_bar_mark_follows_mode(self):
        """default 用户消息前缀带模式标记（?/!）跟随模式。"""
        import os
        old = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = "10"
        try:
            for mode, bar in [
                (Mode.AUTO, "│"),
                (Mode.ASK, "│?"),
                (Mode.YOLO, "│!"),
            ]:
                MODE_STATE.set(mode)
                out = self._render_user("default")
                assert f"{mode_mod.MODE_COLOR[mode]}{bar}\x1B[39;22m" in out
        finally:
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old

    @pytest.fixture(autouse=True)
    def reset_mode(self):
        MODE_STATE.set(Mode.AUTO)
        yield


# ===================================================================
# cli 模式切换
# ===================================================================

class TestSwitchMode:
    def test_switch_mode_dispatches_event(self):
        bus = cli.AgentEventBus()
        captured = []
        bus.register(lambda msg: captured.append(msg))
        cli._switch_mode("m", bus, Mode.ASK)
        assert MODE_STATE.get() == Mode.ASK
        assert len(captured) == 1
        assert isinstance(captured[0], ModeChangeEvent)
        assert captured[0].mode == "ask"
        MODE_STATE.set(Mode.AUTO)
        MODE_STATE.set(Mode.AUTO)  # 复位



# ===================================================================
# agent_loop 模式权限集成
# ===================================================================

class _FakeChoice:
    def __init__(self, message, finish_reason="tool_calls"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, message, finish_reason="tool_calls"):
        self.choices = [_FakeChoice(message, finish_reason=finish_reason)]


class _FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeTC:
    def __init__(self, tc_param):
        self._data = tc_param

    def model_dump(self):
        return dict(self._data)


def _run_call(func_name, args, *, mode=Mode.AUTO, handler=None):
    """跑一次 agent_loop 的单工具调用，返回 (tool消息content, captured events)。"""
    from tests._helpers import make_tool_call, make_assistant_with_tool_calls
    from mycode.tools_registry import ToolsRegistry

    MODE_STATE.set(mode)
    messages: list = []
    captured: list = []
    bus = cli.AgentEventBus()
    bus.register(lambda m: captured.append(m))

    tc = make_tool_call(call_id="call_p", name=func_name, args=json.dumps(args))
    side_effects = [
        _FakeResponse(_FakeMessage(content="", tool_calls=[_FakeTC(tc)])),
        _FakeResponse(_FakeMessage(content="done", tool_calls=[]), finish_reason="stop"),
    ]
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(side_effect=side_effects)

    real_handler = handler
    if real_handler is None:
        real_handler = ToolsRegistry.get_handler(func_name)

    with patch.object(cli, "client", fake_client), \
         patch.object(cli, "_prompt_user_input", return_value=None), \
         patch.object(cli.ToolsRegistry, "get_handler", return_value=real_handler), \
         patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
        cli.agent_loop(messages, bus, model="test-model")

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    content = tool_msgs[0]["content"] if tool_msgs else None
    MODE_STATE.set(Mode.AUTO)
    return content, captured


class TestAgentLoopModePolicy:
    def test_dangerous_rejected_in_all_modes(self, monkeypatch):
        monkeypatch.setenv("BASH_DANGEROUS", "sudo")
        for mode in (Mode.ASK, Mode.AUTO, Mode.YOLO):
            content, _ = _run_call("bash", {"command": "sudo make"}, mode=mode,
                                   handler=lambda **_: "ran")
            assert "拒绝执行危险命令" in content

    def test_auto_caution_requires_no_auto_execution(self, monkeypatch):
        # 自动模式 + 注意（bash rm）→ 需确认；这里模拟用户同意
        monkeypatch.setenv("BASH_DANGEROUS", "")
        monkeypatch.setenv("BASH_CAUTION", "^rm ")
        with patch.object(cli, "confirm_tool",
                          return_value=(confirm_mod.ConfirmAction.APPROVE, None)):
            content, _ = _run_call("bash", {"command": "rm file.txt"}, mode=Mode.AUTO,
                                   handler=lambda **_: "executed")
        assert content == "executed"

    def test_auto_caution_reject(self, monkeypatch):
        monkeypatch.setenv("BASH_DANGEROUS", "")
        monkeypatch.setenv("BASH_CAUTION", "^rm ")
        with patch.object(cli, "confirm_tool",
                          return_value=(confirm_mod.ConfirmAction.REJECT, "不需要")):
            content, _ = _run_call("bash", {"command": "rm file.txt"}, mode=Mode.AUTO,
                                   handler=lambda **_: "ran")
        assert "拒绝执行" in content

    def test_yolo_no_confirm(self):
        with patch.object(cli, "confirm_tool") as mock_confirm:
            content, _ = _run_call("write", {"file_path": "/tmp/x", "content": "hi"},
                                   mode=Mode.YOLO, handler=lambda **_: "wrote")
            mock_confirm.assert_not_called()
        assert content == "wrote"


class TestAgentLoopEditCommand:
    """编辑 bash 命令：有变化分发提醒事件并注入模型，无变化直接执行。"""

    def _run_edit(self, original, edited, confirm_side_effect):
        MODE_STATE.set(Mode.ASK)
        captured: list = []
        messages: list = []
        bus = cli.AgentEventBus()
        bus.register(lambda m: captured.append(m))
        confirm_mock = MagicMock()
        confirm_mock.side_effect = confirm_side_effect
        with patch.object(cli, "confirm_tool", confirm_mock):
            result = cli._run_tool_with_permission(
                "bash",
                {"command": original},
                lambda **kwargs: f"ran:{kwargs.get('command')}",
                model="test-model",
                bus=bus,
                messages=messages,
            )
        MODE_STATE.set(Mode.AUTO)
        return result, captured, messages

    def test_edit_changed_dispatches_notice_and_injects(self):
        """编辑后命令有变化：分发 NoticeEvent（渲染+持久化）并注入模型消息。"""
        from mycode.session import NoticeEvent

        result, captured, messages = self._run_edit(
            "echo a", "echo b",
            [(confirm_mod.ConfirmAction.EDIT, "echo b")],
        )
        # 执行新命令
        assert result == "ran:echo b"
        # 恰好一条 NoticeEvent
        notices = [e for e in captured if isinstance(e, NoticeEvent)]
        assert len(notices) == 1, captured
        ev = notices[0]
        assert ev.notice["display_content"] == "命令修改为："
        assert ev.notice["content"] == "用户将命令修改为："
        assert ev.notice["tag_name"] == "notice"
        assert "```bash\necho b\n```" in ev.notice["additional_content"]
        # 注入 messages：content 整体包 <notice>，附加内容代码块照常
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) == 1, messages
        assert ev.to_user_msg() in messages
        assert "<notice>用户将命令修改为：</notice>" in user_msgs[0]["content"]
        assert "```bash\necho b\n```" in user_msgs[0]["content"]

    def test_edit_unchanged_directly_executes_no_notice(self):
        """编辑后命令无变化：不分发 NoticeEvent，直接继续执行。"""
        from mycode.session import NoticeEvent

        result, captured, messages = self._run_edit(
            "echo a", "echo a",
            [(confirm_mod.ConfirmAction.EDIT, "echo a")],
        )
        assert result == "ran:echo a"
        assert not [e for e in captured if isinstance(e, NoticeEvent)]
        assert not [m for m in messages if m.get("role") == "user"]

    def test_edit_nonexistent_bus_messages_ok(self):
        """bus / messages 未提供（默认 None）时不报错。"""
        MODE_STATE.set(Mode.ASK)
        with patch.object(cli, "confirm_tool",
                          return_value=(confirm_mod.ConfirmAction.EDIT, "echo new")):
            result = cli._run_tool_with_permission(
                "bash",
                {"command": "echo old"},
                lambda **kwargs: f"ran:{kwargs.get('command')}",
            )
        assert result == "ran:echo new"
        MODE_STATE.set(Mode.AUTO)

    def test_edit_to_dangerous_still_dispatches_notice(self, monkeypatch):
        """编辑成危险命令：先分发提醒事件，再以危险拒绝（危险判断在提醒之后）。"""
        from mycode.session import NoticeEvent
        monkeypatch.setenv("BASH_DANGEROUS", "sudo")
        MODE_STATE.set(Mode.ASK)
        captured: list = []
        messages: list = []
        bus = cli.AgentEventBus()
        bus.register(lambda m: captured.append(m))
        with patch.object(cli, "confirm_tool",
                          return_value=(confirm_mod.ConfirmAction.EDIT, "sudo make")):
            result = cli._run_tool_with_permission(
                "bash",
                {"command": "echo hi"},
                lambda **kwargs: f"ran:{kwargs.get('command')}",
                model="test-model",
                bus=bus,
                messages=messages,
            )
        MODE_STATE.set(Mode.AUTO)
        # 拒绝执行危险命令
        assert "拒绝执行危险命令" in result
        # 但仍先分发 NoticeEvent 并注入模型
        notices = [e for e in captured if isinstance(e, NoticeEvent)]
        assert len(notices) == 1, captured
        assert notices[0].notice["tag_name"] == "notice"
        assert notices[0].notice["display_content"] == "命令修改为："
        assert "```bash\nsudo make\n```" in notices[0].notice["additional_content"]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) == 1, messages


class TestAgentLoopAbortBreaksLoop:
    def test_abort_breaks_loop(self):
        """无理由拒绝/取消应跳出 agent 循环，不再继续第二轮交互。"""
        from tests._helpers import make_tool_call, make_assistant_with_tool_calls
        from mycode.tools_registry import ToolsRegistry

        MODE_STATE.set(Mode.ASK)
        messages: list = []
        captured = []
        bus = cli.AgentEventBus()
        bus.register(lambda m: captured.append(m))

        tc = make_tool_call(call_id="call_abort", name="bash", args=json.dumps({"command": "echo hi"}))
        # 只给一轮响应 → 若跳出，不会第二次调用 create
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=_FakeResponse(
            _FakeMessage(content="", tool_calls=[_FakeTC(tc)])
        ))

        with patch.object(cli, "client", fake_client), \
             patch.object(cli, "confirm_tool",
                          return_value=(confirm_mod.ConfirmAction.CANCEL, None)), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=lambda **_: "ran"), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            cli.agent_loop(messages, bus, model="test-model")

        # 只有一次 create（跳出）
        assert fake_client.chat.completions.create.call_count == 1
        # tool 消息为取消占位（中文，非 Error: 后）
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "取消" in tool_msgs[0]["content"]
        MODE_STATE.set(Mode.AUTO)

    def test_reject_no_reason_breaks_loop_with_chinese_message(self):
        """无理由拒绝跳出，结果为中文。"""
        from tests._helpers import make_tool_call
        from mycode.tools_registry import ToolsRegistry

        MODE_STATE.set(Mode.ASK)
        messages: list = []
        bus = cli.AgentEventBus()
        tc = make_tool_call(call_id="call_rej", name="bash", args=json.dumps({"command": "echo hi"}))
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=_FakeResponse(
            _FakeMessage(content="", tool_calls=[_FakeTC(tc)])
        ))
        with patch.object(cli, "client", fake_client), \
             patch.object(cli, "confirm_tool",
                          return_value=(confirm_mod.ConfirmAction.REJECT_NO_REASON, None)), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=lambda **_: "ran"), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            cli.agent_loop(messages, bus, model="test-model")
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "拒绝执行" in tool_msgs[0]["content"]
        assert "理由" in tool_msgs[0]["content"]
        MODE_STATE.set(Mode.AUTO)

    def test_reject_with_reason_has_similarity_to_no_reason(self):
        """带理由拒绝与无理由拒绝结果相似，只是后者无理由。"""
        assert "拒绝执行" in confirm_mod.format_reject("不需要")
        assert "拒绝执行" in confirm_mod.format_reject_no_reason()
        assert "不需要" in confirm_mod.format_reject("不需要")
        assert "理由" in confirm_mod.format_reject_no_reason()


class TestInterruptAbortReplay:
    """InterruptEvent.abort 标记：replay 时 abort 不渲染 ^C，真实 Ctrl-C 渲染 ^C。"""

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_ctrl_c_replay_renders_caret_c(self):
        from mycode.session import InterruptEvent
        ev = InterruptEvent(model="m", interrupt={"abort": False})
        out = self._capture(lambda: cli.render_replay(ev))
        assert "^C" in out

    def test_abort_replay_no_caret_c(self):
        from mycode.session import InterruptEvent
        ev = InterruptEvent(model="m", interrupt={"abort": True})
        out = self._capture(lambda: cli.render_replay(ev))
        assert "^C" not in out

    def test_abort_roundtrips_through_persistence(self, tmp_path):
        from mycode.session import (
            SessionHistory, InterruptEvent, _msg_to_dict, _dict_to_agent_message,
        )
        ev = InterruptEvent(model="m", interrupt={"abort": True})
        d = _msg_to_dict(ev)
        assert d["interrupt"]["abort"] is True
        loaded = _dict_to_agent_message(d)
        assert isinstance(loaded, InterruptEvent)
        assert loaded.interrupt["abort"] is True

class TestReplayModeSync:
    """用户消息自带模式字段，渲染按其 mode（无需依赖全局 MODE_STATE）。"""

    def test_user_message_uses_its_own_mode(self, monkeypatch):
        """UserMessage 自带 yolo 模式，渲染用 yolo 前缀，与全局 MODE_STATE 无关。"""
        import io
        from contextlib import redirect_stdout
        from openai.types.chat import ChatCompletionUserMessageParam
        import mycode.renderer as renderer_mod
        from mycode.session import UserMessage

        monkeypatch.setattr(renderer_mod, "RENDER_STYLE", "classic")
        MODE_STATE.set(Mode.AUTO)  # 全局保持 auto，但消息自带 yolo

        ev = UserMessage(
            model="m",
            message=ChatCompletionUserMessageParam(role="user", content="hi"),
            mode="yolo",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            renderer_mod._render_common(ev)

        out = buf.getvalue()
        # 用户消息提示符应为 yolo 模式（myc[全权]），而非全局 auto
        assert "myc[全权] >" in out
        assert "myc[自动] >" not in out
        MODE_STATE.set(Mode.AUTO)
