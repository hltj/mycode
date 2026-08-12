"""
cli.py 的测试：智能体自循环与 CLI 交互逻辑。

渲染（renderer）相关测试见 test_renderer.py。本文件覆盖：

1. agent_loop 工具执行被打断/异常时的消息补齐行为：
   - 每个 tool_call 都必须对应一个 tool 消息，避免供应商因
     "tool call result does not follow tool call" 校验失败（HTTP 400）；
   - 工具正常 / KeyboardInterrupt / 普通异常 / eval 参数失败的各分支。
2. 用户输入读取（_prompt_user_input）：输入中 Ctrl-C 静默继续、Ctrl-D 退出。
3. prompt_toolkit 会话创建（_create_prompt_session）：按渲染风格配置样式。
4. 命令行参数解析（parse_args）：-s/--style 默认值等。
5. 历史重放（replay_history）与 todo_write 状态同步。
6. 陈旧待办提醒机制（阈值、重置、注入）。
7. ReminderEvent 的渲染一致性与 JSONL 往返。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import mycode.cli as cli
import mycode.renderer as renderer
from mycode.session import ToolResultEvent
from mycode.session import (
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
)

from tests._helpers import make_tool_call, make_assistant_with_tool_calls

# 兼容旧名（_make_tool_call / _make_assistant_with_tool_calls）
_make_tool_call = make_tool_call
_make_assistant_with_tool_calls = make_assistant_with_tool_calls


class _FakeChoice:
    def __init__(self, message, finish_reason="tool_calls"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeUsage:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0


class _FakeResponse:
    def __init__(self, message, finish_reason="tool_calls"):
        self.choices = [_FakeChoice(message, finish_reason=finish_reason)]
        self.usage = _FakeUsage()


class _FakeMessage:
    """模拟 openai ChatCompletionMessage，含 content 与 tool_calls"""
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeTC:
    """模拟 openai ChatCompletionMessageToolCall，能 model_dump"""
    def __init__(self, tc_param):
        self._data = tc_param

    def model_dump(self):
        return dict(self._data)


# ---------------------------------------------------------------------------
# 工具执行被 Ctrl-C / 异常打断时补齐 tool 消息
# ---------------------------------------------------------------------------


def _run_one_tool_call(handler_side_effect, *, call_id="call_x", name="boom", args="{}"):
    """
    单次 tool_call 场景下跑 agent_loop 的工具处理分支。
    handler_side_effect: 注入到 ToolsRegistry.get_handler 返回的 callable，
                         调用时产生给定效果。
    """
    messages: list = []
    bus = cli.AgentEventBus()
    captured: list = []
    bus.register(lambda msg: captured.append(msg))

    tc = _make_tool_call(call_id=call_id, name=name, args=args)
    asst_msg_param = _make_assistant_with_tool_calls(tc)

    # 第一次模型返回 assistant + tool_call（finish_reason=tool_calls）
    # 第二次模型返回纯文本回复（finish_reason=stop）—— 跳出 while True
    final_msg = _FakeMessage(content="done", tool_calls=[])
    side_effects = [
        _FakeResponse(_FakeMessage(content="", tool_calls=[_FakeTC(tc)])),
        _FakeResponse(final_msg, finish_reason="stop"),
    ]
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(side_effect=side_effects)

    with patch.object(cli, "client", fake_client), \
         patch.object(cli.ToolsRegistry, "get_handler", return_value=handler_side_effect), \
         patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
        cli.agent_loop(messages, bus, model="test-model")

    return messages, captured


class TestAgentLoopToolResultBackfill:
    """验证工具执行被打断/异常时仍会补上对应的 tool 消息"""

    def test_normal_tool_call_backfills_tool_message(self):
        """正常路径：补上包含 handler 返回内容的 tool 消息"""
        def handler(**_):
            return "ok"

        messages, captured = _run_one_tool_call(handler)

        # 找到 tool 消息
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["role"] == "tool"
        assert tool_msgs[0]["tool_call_id"] == "call_x"
        assert tool_msgs[0]["content"] == "ok"
        # 渲染事件：ToolCallEvent + ToolResultEvent
        event_types = [type(e).__name__ for e in captured]
        assert "ToolCallEvent" in event_types
        assert "ToolResultEvent" in event_types

    def test_keyboard_interrupt_backfills_tool_message_and_returns(self):
        """Ctrl-C 中断工具时：补上"interrupted"tool 消息，agent_loop 自然返回。"""
        def handler(**_):
            raise KeyboardInterrupt

        # agent_loop 不抛：内部已 dispatch InterruptEvent + 补齐
        messages, captured = _run_one_tool_call(handler)

        # 从外层异常路径也能验证（_run_one_tool_call 内部不接 KeyboardInterrupt，
        # 它会一路冒泡）。重新构造捕获场景验证消息序列：
        messages, captured = [], []
        bus = cli.AgentEventBus()
        captured_holder = []

        def capturing(msg):
            captured_holder.append(msg)

        bus.register(capturing)
        tc = _make_tool_call(call_id="call_intr", name="boom")
        side_effects = [
            _FakeResponse(_FakeMessage(content="", tool_calls=[_FakeTC(tc)])),
        ]
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(side_effect=side_effects)

        def handler_intr(**_):
            raise KeyboardInterrupt

        # agent_loop 不抛：内部已处理 InterruptEvent + 补齐
        with patch.object(cli, "client", fake_client), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=handler_intr), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            cli.agent_loop(messages, bus, model="test-model")

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_intr"
        assert "中断" in tool_msgs[0]["content"]
        # 渲染事件：ToolCallEvent + ToolResultEvent + InterruptEvent 都已发出
        event_types = [type(e).__name__ for e in captured_holder]
        assert "ToolCallEvent" in event_types
        assert "ToolResultEvent" in event_types
        assert "InterruptEvent" in event_types

    def test_exception_backfills_tool_message_and_returns(self):
        """工具执行抛普通 Exception 时：补上"failed"tool 消息，agent_loop 自然返回。"""
        messages, captured = [], []
        bus = cli.AgentEventBus()
        captured_holder = []
        bus.register(lambda msg: captured_holder.append(msg))

        tc = _make_tool_call(call_id="call_err", name="boom")
        side_effects = [
            _FakeResponse(_FakeMessage(content="", tool_calls=[_FakeTC(tc)])),
        ]
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(side_effect=side_effects)

        def handler_exc(**_):
            raise ValueError("boom-error")

        # agent_loop 不抛：内部已 dispatch ExceptionEvent（含 traceback）+ 补齐
        with patch.object(cli, "client", fake_client), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=handler_exc), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            cli.agent_loop(messages, bus, model="test-model")

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_err"
        assert "ValueError" in tool_msgs[0]["content"]
        assert "boom-error" in tool_msgs[0]["content"]
        # 渲染事件齐全：ToolCallEvent + ToolResultEvent + ExceptionEvent
        event_types = [type(e).__name__ for e in captured_holder]
        assert "ToolCallEvent" in event_types
        assert "ToolResultEvent" in event_types
        assert "ExceptionEvent" in event_types

    def test_eval_argument_failure_backfills_tool_message(self):
        """模型产出的 arguments 不可 eval 时：补上错误 tool 消息"""
        messages, captured = [], []
        bus = cli.AgentEventBus()
        captured_holder = []
        bus.register(lambda msg: captured_holder.append(msg))

        tc = _make_tool_call(call_id="call_badargs", name="bash", args="not valid python")
        side_effects = [
            _FakeResponse(_FakeMessage(content="", tool_calls=[_FakeTC(tc)])),
        ]
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(side_effect=side_effects)

        # agent_loop 不抛：内部已 dispatch ExceptionEvent + 补齐
        with patch.object(cli, "client", fake_client), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=lambda **_: "unused"), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            cli.agent_loop(messages, bus, model="test-model")

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_badargs"
        assert "失败" in tool_msgs[0]["content"] or "SyntaxError" in tool_msgs[0]["content"]

    def test_backfilled_tool_message_id_matches_tool_call_id(self):
        """补齐的 tool 消息的 tool_call_id 必须与对应 tool_call.id 一致——
        这是模型供应商校验 'tool call result does not follow tool call' 的关键。"""
        messages: list = []
        bus = cli.AgentEventBus()
        tc = _make_tool_call(call_id="call_match", name="boom")
        side_effects = [
            _FakeResponse(_FakeMessage(content="", tool_calls=[_FakeTC(tc)])),
        ]
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(side_effect=side_effects)

        def handler(**_):
            raise RuntimeError("x")

        # agent_loop 不抛：内部已 dispatch ExceptionEvent + 补齐
        with patch.object(cli, "client", fake_client), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=handler), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            cli.agent_loop(messages, bus, model="test-model")

        # 找到 assistant（带 tool_calls）与对应的 tool 消息
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(assistant_msgs) == 1
        assert len(tool_msgs) == 1
        # assistant.tool_calls 的 id 与 tool.tool_call_id 一致
        tc_ids_in_assistant = {tc["id"] for tc in assistant_msgs[0]["tool_calls"]}
        assert tool_msgs[0]["tool_call_id"] in tc_ids_in_assistant



class TestAgentLoopMultiToolCallBackfill:
    """assistant 一次返回多个 tool_call：任一被 Ctrl-C 或异常中断时，
    中断项本身的 ToolCallEvent 是「正常发起」（handler 已被调用），
    只是其 ToolResultEvent / tool 消息变成了错误占位；
    其后所有未触达的 tool_call 则必须同时补齐 ToolCallEvent 与
    ToolResultEvent / tool 消息，使 assistant.tool_calls 与
    tool.tool_call_id 一一对应，避免模型供应商的 400 校验失败。"""

    @staticmethod
    def _run_multi(handlers_by_id, tcs):
        """
        让 assistant 一次返回多个 tool_call；handlers_by_id 按 tool_call_id 给出
        每个 handler 的副作用（返回值 / 抛异常 / 不调用）。
        返回 (messages, captured_events, reraised_exc)
        """
        messages: list = []
        captured: list = []
        bus = cli.AgentEventBus()
        bus.register(lambda msg: captured.append(msg))

        # 第一次 create：返回 assistant + 所有 tool_call（finish_reason=tool_calls）
        # 由于异常会让 agent_loop 抛出，不会再有第二次 create
        fake_response = _FakeResponse(
            _FakeMessage(content="", tool_calls=[_FakeTC(tc) for tc in tcs]),
            finish_reason="tool_calls",
        )
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=fake_response)

        # 按调用顺序消耗 handlers_by_id 中的每个 callable。
        # side_effect_handler 弹出下一个并真正调用它（让 raise_* 立即抛出）。
        handler_iter = iter(handlers_by_id)

        def side_effect_handler(**kwargs):
            h = next(handler_iter)
            return h(**kwargs)

        with patch.object(cli, "client", fake_client), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=side_effect_handler), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            try:
                cli.agent_loop(messages, bus, model="test-model")
                reraised = None
            except BaseException as e:
                reraised = e

        return messages, captured, reraised

    def test_first_tool_keyboard_interrupt_backfills_all_remaining(self):
        """c1 的 ToolCallEvent 正常发起，handler 启动后被 Ctrl-C 中断，
        故其 ToolResultEvent / tool 消息是「中断占位」；
        c2、c3 完全未被触达，二者的 ToolCallEvent 与 ToolResultEvent
        都是补齐的占位。验证 3 个 tool 消息齐全且 id 与 tool_call.id 一致。"""
        tcs = [
            _make_tool_call(call_id="c1", name="t1"),
            _make_tool_call(call_id="c2", name="t2"),
            _make_tool_call(call_id="c3", name="t3"),
        ]

        def raise_intr(**_):
            raise KeyboardInterrupt

        def would_run(**_):
            return "should-not-run"

        # handler 按调用顺序触发：c1=中断、c2/c3=不会被调用
        handlers = [raise_intr, would_run, would_run]

        messages, captured, _ = self._run_multi(handlers, tcs)
        # agent_loop 不抛：内部已 dispatch InterruptEvent + 补齐
        # 3 个 tool 消息齐全，tool_call_id 与 tc.id 一一对应
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 3
        ids = {m["tool_call_id"] for m in tool_msgs}
        assert ids == {"c1", "c2", "c3"}
        # c1 是中断占位，c2/c3 是 skipped 占位
        c1_msg = next(m for m in tool_msgs if m["tool_call_id"] == "c1")
        c2_msg = next(m for m in tool_msgs if m["tool_call_id"] == "c2")
        c3_msg = next(m for m in tool_msgs if m["tool_call_id"] == "c3")
        assert "中断" in c1_msg["content"]
        assert "中断" in c2_msg["content"] and "跳过" in c2_msg["content"]
        assert "中断" in c3_msg["content"] and "跳过" in c3_msg["content"]
        # ToolCallEvent / ToolResultEvent 各 3 次
        tc_events = [e for e in captured if isinstance(e, ToolCallEvent)]
        tr_events = [e for e in captured if isinstance(e, ToolResultEvent)]
        assert len(tc_events) == 3
        assert len(tr_events) == 3

    def test_second_tool_exception_backfills_all_remaining(self):
        """c1 全程正常；c2 的 ToolCallEvent 正常发起但 handler 抛异常，
        其 ToolResultEvent / tool 消息是「失败占位」；
        c3 未被触达，二者都是补齐的占位。"""
        tcs = [
            _make_tool_call(call_id="c1", name="t1"),
            _make_tool_call(call_id="c2", name="t2"),
            _make_tool_call(call_id="c3", name="t3"),
        ]

        def ok(**_):
            return "c1-result"

        def bad(**_):
            raise ValueError("c2-boom")

        def unused(**_):
            return "unused"

        handlers = [ok, bad, unused]

        messages, captured, _ = self._run_multi(handlers, tcs)
        # agent_loop 不抛：内部已 dispatch ExceptionEvent + 补齐
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 3
        ids = {m["tool_call_id"] for m in tool_msgs}
        assert ids == {"c1", "c2", "c3"}
        c1_msg = next(m for m in tool_msgs if m["tool_call_id"] == "c1")
        c2_msg = next(m for m in tool_msgs if m["tool_call_id"] == "c2")
        c3_msg = next(m for m in tool_msgs if m["tool_call_id"] == "c3")
        assert c1_msg["content"] == "c1-result"
        assert "ValueError" in c2_msg["content"] and "c2-boom" in c2_msg["content"]
        assert "跳过" in c3_msg["content"] and "ValueError" in c3_msg["content"]
        # ToolCallEvent / ToolResultEvent 各 3 次
        assert sum(1 for e in captured if isinstance(e, ToolCallEvent)) == 3
        assert sum(1 for e in captured if isinstance(e, ToolResultEvent)) == 3

    def test_last_tool_exception_backfills_only_current(self):
        """c1、c2 全程正常；c3 的 ToolCallEvent 正常发起但 handler 抛异常，
        其 ToolResultEvent / tool 消息是「失败占位」。
        验证只有 c3 需要占位处理，前面已完成的项不被重复补消息。"""
        tcs = [
            _make_tool_call(call_id="c1", name="t1"),
            _make_tool_call(call_id="c2", name="t2"),
            _make_tool_call(call_id="c3", name="t3"),
        ]
        handlers = [
            lambda **_: "r1",
            lambda **_: "r2",
            lambda **_: (_ for _ in ()).throw(RuntimeError("c3-boom")),
        ]
        messages, captured, _ = self._run_multi(handlers, tcs)
        # agent_loop 不抛：内部已 dispatch ExceptionEvent + 补齐
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 3
        c1 = next(m for m in tool_msgs if m["tool_call_id"] == "c1")["content"]
        c2 = next(m for m in tool_msgs if m["tool_call_id"] == "c2")["content"]
        c3 = next(m for m in tool_msgs if m["tool_call_id"] == "c3")["content"]
        assert c1 == "r1"
        assert c2 == "r2"
        assert "RuntimeError" in c3 and "c3-boom" in c3

    def test_all_tool_calls_have_matching_tool_message_ids(self):
        """即使首个 tool_call 就被 Ctrl-C 中断，assistant.tool_calls 中的每个 id
        仍必须能在 tool 消息中找到对应 tool_call_id —— 这是模型供应商校验
        'tool call result does not follow tool call' 的硬性条件。"""
        tcs = [
            _make_tool_call(call_id="id-a", name="t1"),
            _make_tool_call(call_id="id-b", name="t2"),
            _make_tool_call(call_id="id-c", name="t3"),
        ]

        def raise_early(**_):
            raise KeyboardInterrupt

        handlers = [raise_early, raise_early, raise_early]

        messages, captured, _ = self._run_multi(handlers, tcs)
        # agent_loop 不抛：内部已 dispatch InterruptEvent + 补齐

        # assistant.tool_calls 与 tool.tool_call_id 必须一一对应
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(assistant_msgs) == 1
        asst_tool_call_ids = {tc["id"] for tc in assistant_msgs[0]["tool_calls"]}
        tool_call_ids = {m["tool_call_id"] for m in tool_msgs}
        assert asst_tool_call_ids == tool_call_ids == {"id-a", "id-b", "id-c"}



class TestAgentLoopInterruptIndicatorDispatch:
    """验证 Ctrl-C / 异常 中断工具时，InterruptEvent / ExceptionEvent
    必须在补齐剩余 tool_call 之前 dispatch，否则终端上 ^C 后没有紧跟换行、
    异常 traceback 也被补位的工具输出挤到末尾。"""

    def _capture_event_order(self, handlers, tcs):
        """跑一次多 tool_call 场景，返回 (event_names_in_order, reraised)"""
        messages: list = []
        captured: list = []
        bus = cli.AgentEventBus()
        bus.register(lambda msg: captured.append(msg))

        fake_response = _FakeResponse(
            _FakeMessage(content="", tool_calls=[_FakeTC(tc) for tc in tcs]),
            finish_reason="tool_calls",
        )
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=fake_response)

        handler_iter = iter(handlers)

        def side_effect_handler(**kwargs):
            h = next(handler_iter)
            return h(**kwargs)

        with patch.object(cli, "client", fake_client), \
             patch.object(cli.ToolsRegistry, "get_handler", return_value=side_effect_handler), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]):
            try:
                cli.agent_loop(messages, bus, model="test-model")
                reraised = None
            except BaseException as e:
                reraised = e

        event_names = [type(e).__name__ for e in captured]
        return event_names, reraised

    def test_interrupt_event_dispatched_before_c1_tool_result(self):
        """Ctrl-C 中断：InterruptEvent 必须在 c1 ToolResultEvent 之前出现，
        否则 ^C 后没有紧跟空行、视觉上 ^C 会挤在 c1 工具输出前面。
        此顺序与异常路径中 ExceptionEvent 出现在 c1 ToolResultEvent 之前对齐。"""
        tcs = [
            _make_tool_call(call_id="c1", name="t1"),
            _make_tool_call(call_id="c2", name="t2"),
            _make_tool_call(call_id="c3", name="t3"),
        ]

        def raise_intr(**_):
            raise KeyboardInterrupt

        def unused(**_):
            return "x"

        event_names, _ = self._capture_event_order(
            [raise_intr, unused, unused], tcs
        )

        # 期望顺序：Assistant → c1 ToolCall → InterruptEvent（紧跟 ^C，
        # 让终端立即换行）→ c1 ToolResult（中断占位）→ c2 补齐 → c3 补齐
        assert event_names == [
            "AssistantMessage",
            "ToolCallEvent",
            "InterruptEvent",
            "ToolResultEvent",
            "ToolCallEvent", "ToolResultEvent",
            "ToolCallEvent", "ToolResultEvent",
        ]
        # agent_loop 不抛：内部已 dispatch InterruptEvent + 补齐
    def test_exception_event_dispatched_before_backfilled_tool_call_events(self):
        """普通异常：ExceptionEvent 必须在补齐的 ToolCallEvent 之前出现。"""
        tcs = [
            _make_tool_call(call_id="c1", name="t1"),
            _make_tool_call(call_id="c2", name="t2"),
            _make_tool_call(call_id="c3", name="t3"),
        ]

        def boom(**_):
            raise ValueError("boom")

        def unused(**_):
            return "x"

        event_names, _ = self._capture_event_order(
            [boom, unused, unused], tcs
        )

        # 期望顺序：c1 ToolCall → c1 ToolResult（失败占位）→ ExceptionEvent →
        #           c2 补齐 → c3 补齐
        # 期望顺序：Assistant → c1 ToolCall → ExceptionEvent（含 traceback，
        # 在 except 块内捕获以避免 sys.exc_info 被清除）→ c1 ToolResult（异常占位）→
        #           c2 补齐 → c3 补齐
        assert event_names == [
            "AssistantMessage",
            "ToolCallEvent",
            "ExceptionEvent",
            "ToolResultEvent",
            "ToolCallEvent", "ToolResultEvent",
            "ToolCallEvent", "ToolResultEvent",
        ]
        # 异常信息在 ExceptionEvent 中携带（agent_loop 内部已 dispatch）

    def test_interrupt_event_dispatched_only_once_on_ctrl_c(self):
        """Ctrl-C 时 InterruptEvent 仅 dispatch 一次，避免与外层重复。"""
        # 这里仅验证 agent_loop 内部恰好 dispatch 一次 InterruptEvent；
        # 外层 main() 之所以不再 dispatch，见源码修改（删除了 dispatch）。
        tcs = [_make_tool_call(call_id="c1", name="t1")]

        def raise_intr(**_):
            raise KeyboardInterrupt

        event_names, _ = self._capture_event_order([raise_intr], tcs)

        interrupt_count = sum(1 for n in event_names if n == "InterruptEvent")
        assert interrupt_count == 1



class TestPromptUserInput:
    """cli._prompt_user_input：Ctrl-C 发生在输入过程中的静默处理。"""

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_returns_input_on_success(self):
        """正常输入：返回输入文本。"""
        session = MagicMock()
        session.prompt = MagicMock(return_value="hello")
        assert cli._prompt_user_input(session) == "hello"

    def test_keyboard_interrupt_during_input_returns_none_silently(self):
        """输入过程中 Ctrl-C：返回 None 且不输出任何内容。"""
        session = MagicMock()
        session.prompt = MagicMock(side_effect=KeyboardInterrupt)
        holder: dict = {}
        out = self._capture(
            lambda: holder.update(result=cli._prompt_user_input(session)))
        assert holder["result"] is None
        assert out == ""  # 无任何输出

    def test_eof_error_propagates(self):
        """Ctrl-D（EOFError）照常向上抛出，由外层退出程序。"""
        session = MagicMock()
        session.prompt = MagicMock(side_effect=EOFError)
        with pytest.raises(EOFError):
            cli._prompt_user_input(session)



class TestCreatePromptSession:
    """cli._create_prompt_session：按渲染风格配置输入会话。"""

    @pytest.fixture(autouse=True)
    def default_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")

    def test_classic_no_root_bg_and_no_layout_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        """classic：根样式无背景，布局根容器无额外样式。"""
        from prompt_toolkit.utils import to_str
        session = cli._create_prompt_session()
        root_style = session.app.layout.container.style
        assert to_str(root_style) == ""
        # 提示符样式存在但根样式（''）不含背景
        attrs = session.style.get_attrs_for_style_str("")
        assert attrs.bgcolor in (None, "")

    def test_default_root_bg_and_layout_style(self):
        """default：根样式有灰色背景，布局根容器挂 mycode-input 样式。"""
        session = cli._create_prompt_session()
        # 根样式带背景（使有内容的单元格继承）
        attrs = session.style.get_attrs_for_style_str("")
        assert attrs.bgcolor is not None and attrs.bgcolor != ""
        # 布局根容器挂样式类，使整块输入区（含空白行）填充背景
        assert session.app.layout.container.style == "class:mycode-input"
        # mycode-input 类本身也定义灰色背景
        inp_attrs = session.style.get_attrs_for_style_str("class:mycode-input")
        assert inp_attrs.bgcolor is not None and inp_attrs.bgcolor != ""

    def test_default_prompt_symbol(self):
        """default 提示符为居左竖线。"""
        cli._create_prompt_session()  # 仅确保可创建
        assert cli._prompt_fragments() == [('class:mycode-prompt', '│ ')]



class TestStyleArg:
    """命令行参数 -s/--style 的解析。"""

    def _parse(self, argv):
        from mycode.cli import parse_args
        with patch("sys.argv", ["mycode"] + argv):
            return parse_args()

    def test_default_is_default(self):
        assert self._parse([]).style == "default"

    def test_short_flag(self):
        assert self._parse(["-s", "default"]).style == "default"

    def test_long_flag(self):
        assert self._parse(["--style", "classic"]).style == "classic"

    def test_invalid_choice_exits(self):
        import pytest
        with pytest.raises(SystemExit):
            self._parse(["-s", "fancy"])



class TestReplayTodoSync:
    """replay 时 todo_write 的 ToolCallEvent 同步 + 渲染验证。"""

    @pytest.fixture(autouse=True)
    def default_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def _make_history_with_two_todo_writes(self):
        """构造一个 SessionHistory，包含两次 todo_write 调用。"""
        import json
        from mycode.session import SessionHistory, ToolCallEvent, ToolResultEvent
        sh = SessionHistory(cwd="/tmp", model="m")
        sh.entries = [
            ToolCallEvent(
                model="m",
                tool_call={
                    "id": "tc1",
                    "type": "function",
                    "function": {
                        "name": "todo_write",
                        "arguments": json.dumps({
                            "items": [{"title": "第一步", "status": "completed"}],
                        }),
                    },
                },
            ),
            ToolResultEvent(
                model="m",
                message={"role": "tool", "tool_call_id": "tc1",
                          "content": "TODO 列表已更新（1 项）"},
                tool_name="todo_write",
            ),
            ToolCallEvent(
                model="m",
                tool_call={
                    "id": "tc2",
                    "type": "function",
                    "function": {
                        "name": "todo_write",
                        "arguments": json.dumps({
                            "items": [{"title": "新一步", "status": "pending"}],
                        }),
                    },
                },
            ),
            ToolResultEvent(
                model="m",
                message={"role": "tool", "tool_call_id": "tc2",
                          "content": "TODO 列表已更新（1 项）"},
                tool_name="todo_write",
            ),
        ]
        return sh

    def test_replay_renders_state_at_call_time(self):
        """replay 时每次 todo_write 的 ToolResultEvent 渲染对应调用时刻的状态。"""
        from mycode.tools.todo_write import reset_todos, get_todos
        from mycode.cli import (
            AgentEventBus, render_replay,
            make_replay_todo_sync_handler,
        )
        reset_todos()
        sh = self._make_history_with_two_todo_writes()

        bus = AgentEventBus()
        bus.register(render_replay)
        bus.register(make_replay_todo_sync_handler())
        out = self._capture(lambda: [bus.dispatch(e) for e in sh.entries])

        # 第一次渲染（对应 tc1）应显示 "✅️ 第一步"
        assert "✅️" in out
        assert "第一步" in out
        # 第二次渲染（对应 tc2）应显示 "🔳 新一步"，不再有 "第一步"
        # 用相对位置判断：第一次渲染中的待办列表只能有 "第一步"。
        # 注意 completed 带 ANSI 样式（可影响符号与标题的紧邻匹配），
        # 因此分别断言「状态符号 + 标题」的存在性，并用两种符号做交叉鉴别：
        #   第一次只有 ✅️（completed），第二次只有 🔳（pending）。
        first_idx = out.index("TODO 列表:")
        second_idx = out.index("TODO 列表:", first_idx + 1)
        first_block = out[first_idx:second_idx]
        second_block = out[second_idx:]
        assert "✅️" in first_block and "第一步" in first_block
        assert "🔳" not in first_block            # 第一次状态为 completed
        assert "🔳 新一步" in second_block
        assert "✅️" not in second_block           # 第二次状态为 pending

        # replay 结束后 _todo_state 应该是最终态（新一步）
        final = get_todos()
        assert len(final) == 1
        assert final[0]["title"] == "新一步"

    def test_replay_skips_non_todo_write_calls(self):
        """非 todo_write 的 ToolCallEvent 不应触发 _todo_state 变化。"""
        import json
        from mycode.session import SessionHistory, ToolCallEvent, ToolResultEvent
        from mycode.tools.todo_write import reset_todos, get_todos
        from mycode.cli import (
            AgentEventBus, render_replay,
            make_replay_todo_sync_handler,
        )
        reset_todos()
        sh = SessionHistory(cwd="/tmp", model="m")
        sh.entries = [
            ToolCallEvent(
                model="m",
                tool_call={
                    "id": "b1", "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})},
                },
            ),
            ToolResultEvent(
                model="m",
                message={"role": "tool", "tool_call_id": "b1", "content": "x"},
                tool_name="bash",
            ),
        ]
        bus = AgentEventBus()
        bus.register(render_replay)
        bus.register(make_replay_todo_sync_handler())
        self._capture(lambda: [bus.dispatch(e) for e in sh.entries])
        # bash 调用不触发 todo 状态变化
        assert get_todos() == []

    def test_replay_handles_malformed_arguments(self):
        """arguments 解析失败的 todo_write ToolCallEvent 被静默跳过，不抛异常。"""
        import json
        from mycode.session import SessionHistory, ToolCallEvent, ToolResultEvent
        from mycode.tools.todo_write import reset_todos, get_todos
        from mycode.cli import (
            AgentEventBus, render_replay,
            make_replay_todo_sync_handler,
        )
        reset_todos()
        sh = SessionHistory(cwd="/tmp", model="m")
        sh.entries = [
            ToolCallEvent(
                model="m",
                tool_call={
                    "id": "bad", "type": "function",
                    "function": {"name": "todo_write", "arguments": "{not json"},
                },
            ),
            # 紧接着一次合法的 todo_write
            ToolCallEvent(
                model="m",
                tool_call={
                    "id": "ok", "type": "function",
                    "function": {
                        "name": "todo_write",
                        "arguments": json.dumps({
                            "items": [{"title": "ok", "status": "pending"}],
                        }),
                    },
                },
            ),
        ]
        bus = AgentEventBus()
        bus.register(render_replay)
        bus.register(make_replay_todo_sync_handler())
        # 不应抛异常
        self._capture(lambda: [bus.dispatch(e) for e in sh.entries])
        # 最终状态应是合法的那个调用
        state = get_todos()
        assert len(state) == 1
        assert state[0]["title"] == "ok"



class TestStaleTodoReminder:
    """待办陈旧度提醒机制：超过阈值轮数未更新时触发。"""

    def _reset(self):
        from mycode.tools.todo_write import (
            reset_todos, reset_stale_rounds, get_stale_rounds,
        )
        reset_todos()
        reset_stale_rounds()
        assert get_stale_rounds() == 0

    def test_no_remind_when_no_unfinished(self):
        """无 todo 或全部完成时不提醒。"""
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
        )
        self._reset()
        # 没有 todo
        for _ in range(10):
            bump_stale_rounds()
        assert not should_remind_stale_todo()
        # 全部 completed
        todo_write([
            {"title": "A", "status": "completed"},
            {"title": "B", "status": "completed"},
        ])
        for _ in range(10):
            bump_stale_rounds()
        assert not should_remind_stale_todo()

    def test_no_remind_below_threshold(self):
        """未到阈值不提醒。"""
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
            get_stale_rounds,
        )
        self._reset()
        todo_write([{"title": "A", "status": "in_process"}])
        # todo_write 后 stale 已清零
        assert get_stale_rounds() == 0
        bump_stale_rounds()
        bump_stale_rounds()
        assert get_stale_rounds() == 2
        assert not should_remind_stale_todo()

    def test_remind_at_threshold(self):
        """达到阈值时提醒。"""
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
        )
        self._reset()
        todo_write([{"title": "A", "status": "in_process"}])
        for _ in range(5):
            bump_stale_rounds()
        assert should_remind_stale_todo()

    def test_todo_write_resets_stale(self):
        """再次 todo_write（哪怕同样内容）清零 stale。"""
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
        )
        self._reset()
        todo_write([{"title": "A", "status": "in_process"}])
        for _ in range(5):
            bump_stale_rounds()
        assert should_remind_stale_todo()
        # 再次调用 todo_write
        todo_write([{"title": "A", "status": "in_process"}])
        assert not should_remind_stale_todo()
        for _ in range(2):
            bump_stale_rounds()
        assert not should_remind_stale_todo()  # 累积未到阈值

    def test_reminder_injection_resets_stale(self):
        """注入提醒（手动调 reset）后 stale 清零，下一轮需重新累积。"""
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
            reset_stale_rounds,
        )
        self._reset()
        todo_write([{"title": "A", "status": "in_process"}])
        for _ in range(5):
            bump_stale_rounds()
        assert should_remind_stale_todo()
        # 注入提醒（agent_loop 实际会做的事）
        if should_remind_stale_todo():
            reset_stale_rounds()
        assert not should_remind_stale_todo()

    def test_reminder_format_is_simple_no_todo_list(self):
        """提醒正文是简洁文本，不含待办列表也不重复状态定义。

        ``<reminder>`` 标签由 ``ReminderEvent.to_user_msg()`` 加，format
        输出纯文本。
        """
        from mycode.tools.todo_write import format_stale_reminder, todo_write
        self._reset()
        todo_write([
            {"title": "任务 B", "status": "in_process"},
            {"title": "任务 C", "status": "pending"},
        ])
        text = format_stale_reminder()
        # 整体文案与 todo 内容无关
        assert text == "有未完成的 todo 最近未更新，请使用 todo_write 工具更新状态。"
        # 标签由 to_user_msg 加，format 输出不含
        assert "<reminder>" not in text
        assert "</reminder>" not in text
        # 不附带待办项（避免依赖内存状态；模型须从历史读取）
        assert "任务 B" not in text
        assert "任务 C" not in text
        # 不重复状态符号
        assert "[>]" not in text
        assert "[ ]" not in text
        # 不重复状态名（工具描述里已有）
        assert "completed" not in text
        assert "in_process" not in text
        assert "pending" not in text

    def test_threshold_read_from_env(self, monkeypatch):
        """阈值由环境变量 MYCODE_STALE_THRESHOLD 控制。"""
        # 用 importlib 绕开 mycode.tools.__init__ 里同名属性拦截，
        # 否则 ``import mycode.tools.todo_write as tw`` 会被解析成函数对象。
        import importlib
        tw = importlib.import_module("mycode.tools.todo_write")
        monkeypatch.setattr(tw, "_STALE_THRESHOLD", 5)
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
        )
        self._reset()
        todo_write([{"title": "A", "status": "in_process"}])
        # 阈值 5：4 次不触发
        for _ in range(4):
            bump_stale_rounds()
        assert not should_remind_stale_todo()
        # 第 5 次触发
        bump_stale_rounds()
        assert should_remind_stale_todo()

    def test_pending_only_still_triggers_reminder(self):
        """只有 pending 也能触发提醒（in_process 不是必要条件）。"""
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
        )
        self._reset()
        todo_write([
            {"title": "A", "status": "pending"},
            {"title": "B", "status": "pending"},
        ])
        for _ in range(5):
            bump_stale_rounds()
        assert should_remind_stale_todo()

    def test_empty_todo_list_does_not_trigger(self):
        """显式清空 todo 列表后不提醒（todo_write([])）。"""
        from mycode.tools.todo_write import (
            bump_stale_rounds, should_remind_stale_todo, todo_write,
        )
        self._reset()
        todo_write([
            {"title": "A", "status": "in_process"},
        ])
        todo_write([])  # 清空
        for _ in range(10):
            bump_stale_rounds()
        assert not should_remind_stale_todo()



class TestReminderEvent:
    """ReminderEvent：系统级提醒事件，区别于 UserMessage。

    渲染一致性：实时与 replay 都走 ``_render_common`` 的黄色高亮分支，
    不会被误渲染为用户输入。此处用 classic 风格（标题无 emoji）断言
    纯文本内容。
    """

    @pytest.fixture(autouse=True)
    def classic_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")

    def _capture(self, fn):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_replay_renders_reminder_as_reminder(self):
        """replay 时 ReminderEvent 显示为黄色高亮，不是 ``myc > `` 前缀。

        注意：ReminderEvent.content 不含 ``<reminder>`` 标签（标签只在
        ``to_user_msg()`` 喂给模型时加），渲染纯文本更友好。
        """
        from mycode.session import ReminderEvent
        from mycode.cli import render_replay

        event = ReminderEvent(model="m", content="hello")
        out = self._capture(lambda: render_replay(event))

        # 黄色 ANSI + 纯文本 content（不含 <reminder> 标签）
        assert "\x1B[1;33mhello\x1B[0m" in out
        assert "<reminder>" not in out
        # 关键：不出现用户输入前缀
        assert "myc >" not in out

    def test_terminal_render_skips_user_but_renders_reminder(self):
        """实时路径（render_terminal）也渲染 ReminderEvent。"""
        from mycode.session import ReminderEvent
        from mycode.cli import render_terminal

        event = ReminderEvent(model="m", content="hello")
        out = self._capture(lambda: render_terminal(event))

        assert "\x1B[1;33mhello\x1B[0m" in out
        assert "<reminder>" not in out
        assert "myc >" not in out

    def test_to_user_msg_wraps_content_in_reminder_tag(self):
        """to_user_msg() 把 content 用 ``<reminder>`` 标签包裹后返回。"""
        from mycode.session import ReminderEvent

        event = ReminderEvent(model="m", content="提醒正文")
        msg = event.to_user_msg()
        assert msg["role"] == "user"
        assert msg["content"] == "<reminder>提醒正文</reminder>"

    def test_replay_user_message_still_uses_myc_prefix(self):
        """UserMessage 在 replay 中仍然用 ``myc[...] > `` 前缀（跟随模式）。"""
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage
        from mycode.cli import render_replay

        msg = ChatCompletionUserMessageParam(role="user", content="hi")
        event = UserMessage(model="m", message=msg)
        out = self._capture(lambda: render_replay(event))

        assert "myc[自动] >" in out
        assert "hi" in out

    def test_reminder_event_roundtrips_through_jsonl(self, tmp_path):
        """ReminderEvent 写入 JSONL 后能正确读回。"""
        import json
        from mycode.session import ReminderEvent, _dict_to_agent_message

        original = ReminderEvent(model="gpt-4", content="<reminder>x</reminder>")
        original.id = "r1"
        original.time = "2026-01-01T00:00:00Z"

        path = tmp_path / "reminder.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": original.time,
                "type": "reminder",
                "id": original.id,
                "parent_id": None,
                "model": original.model,
                "content": original.content,
            }, ensure_ascii=False) + "\n")

        with open(path, "r", encoding="utf-8") as f:
            data = json.loads(f.readline())
        loaded = _dict_to_agent_message(data)

        assert isinstance(loaded, ReminderEvent)
        assert loaded.model == original.model
        assert loaded.content == original.content
        assert loaded.id == original.id



class TestReminderInGetMessages:
    """会话恢复时 ``get_messages()`` 把 ReminderEvent 包成 user message 返回。

    目的：``-r`` / ``--continue`` 恢复会话后，模型仍能看到 reminder
    内容（与实时注入一致），不会因为 reminder 被存为 ReminderEvent 而丢失。
    """

    def _make_history(self, tmp_path) -> "SessionHistory":
        from mycode.session import SessionHistory
        return SessionHistory(cwd=str(tmp_path), model="m")

    def test_get_messages_includes_reminder_as_user_message(self, tmp_path):
        """ReminderEvent 在 get_messages() 里转为 ChatCompletionUserMessageParam。

        content 字段不带 ``<reminder>`` 标签，由 ``to_user_msg()`` 加上。
        """
        from mycode.session import ReminderEvent
        sh = self._make_history(tmp_path)
        sh.append(ReminderEvent(model="m", content="hi"))

        msgs = sh.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "<reminder>hi</reminder>"

    def test_get_messages_order_matches_entries(self, tmp_path):
        """get_messages() 输出顺序与 entries 一致（reminder 保留时间位置）。"""
        from openai.types.chat import (
            ChatCompletionUserMessageParam,
            ChatCompletionAssistantMessageParam,
        )
        from mycode.session import (
            ReminderEvent, UserMessage, AssistantMessage,
        )

        sh = self._make_history(tmp_path)
        # 注入 user → reminder → assistant
        sh.append(UserMessage(model="m", message=ChatCompletionUserMessageParam(
            role="user", content="ask"
        )))
        sh.append(ReminderEvent(model="m", content="r"))
        sh.append(AssistantMessage(model="m", message=ChatCompletionAssistantMessageParam(
            role="assistant", content="answer"
        )))

        msgs = sh.get_messages()
        assert [m["role"] for m in msgs] == ["user", "user", "assistant"]
        assert msgs[0]["content"] == "ask"
        assert msgs[1]["content"] == "<reminder>r</reminder>"
        assert msgs[2]["content"] == "answer"

    def test_entry_count_excludes_reminder(self, tmp_path):
        """cli.py 退出时计算的 entry_count 不把 reminder 当作"消息"。

        reminder 是系统级注入，不是真实用户/助手对话，不应触发会话保留逻辑。
        """
        from mycode.session import (
            ReminderEvent, UserMessage, AssistantMessage, ToolResultEvent,
        )
        sh = self._make_history(tmp_path)
        sh.append(ReminderEvent(model="m", content="<reminder>r</reminder>"))

        # 跟 cli.py 退出时的判定一致
        entry_count = len([e for e in sh.entries if isinstance(
            e, (UserMessage, AssistantMessage, ToolResultEvent)
        )])
        assert entry_count == 0  # reminder 不算
