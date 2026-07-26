"""
TDD tests for cli.py —— 聚焦 agent_loop 中工具执行被打断/异常时的消息补齐行为。

agent_loop 的核心职责之一：每个 tool_call 都必须对应一个 tool 消息，
否则下次恢复会话时模型供应商会因 "tool call result does not follow tool call"
而校验失败（HTTP 400）。这些测试验证：

1. 工具正常返回：补上正常的 tool 消息；
2. 工具执行抛 KeyboardInterrupt：补上"中断"tool 消息，agent_loop 自然返回；
3. 工具执行抛普通 Exception：补上"失败"tool 消息，agent_loop 自然返回；
4. eval 解析参数失败：补上错误 tool 消息，agent_loop 自然返回。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionToolMessageParam,
)
from openai.types.chat.chat_completion_message_function_tool_call_param import Function

import mycode.cli as cli
from mycode.session import (
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_env(monkeypatch, tmp_path):
    """设置最小环境变量 + 临时 HOME，避免污染真实 ~/.mycode"""
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("MYCODE_HOME_DIR", str(tmp_path / ".mycode"))
    yield


def _make_tool_call(call_id: str = "call_1", name: str = "boom", args: str = "{}"):
    return ChatCompletionMessageFunctionToolCallParam(
        id=call_id,
        type="function",
        function=Function(name=name, arguments=args),
    )


def _make_assistant_with_tool_calls(*tcs):
    return ChatCompletionAssistantMessageParam(
        role="assistant",
        content="",
        tool_calls=list(tcs),
    )


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


def _make_completion_message(*tcs, content=""):
    return _FakeMessage(content=content, tool_calls=[
        _FakeTC(tc) for tc in tcs
    ])


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
        assert "interrupted" in tool_msgs[0]["content"].lower()
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
        assert "failed" in tool_msgs[0]["content"].lower() or "SyntaxError" in tool_msgs[0]["content"]

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
        assert "interrupted" in c1_msg["content"].lower()
        assert "interrupted" in c2_msg["content"].lower() and "skipped" in c2_msg["content"].lower()
        assert "interrupted" in c3_msg["content"].lower() and "skipped" in c3_msg["content"].lower()
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
        assert "skipped" in c3_msg["content"].lower() and "ValueError" in c3_msg["content"]
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
