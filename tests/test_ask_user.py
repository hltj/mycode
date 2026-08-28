"""
ask_user 工具测试。

覆盖：
- build_ask_options：预置选项 + 自定义选项拼接、单选推荐选项前置与
  ``（推荐）`` 后缀（value 保持原始标签）、多选不调整、自定义标签/占位
  的默认值与指定值；
- ask_user 返回值：selected / input 的 JSON 序列化、ask_ui 参数透传；
- abort：ask_ui aborted → 抛 ``AbortLoop``，agent_loop 捕获后分发工具
  结果事件并弹出一轮循环。
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

from mycode.ask_ui import AskResult
from mycode.session import AbortLoop

# 取真正的子模块对象（包 ``__init__`` 的 ``from ... import ask_user`` 会把
# 包级 ``ask_user`` 属性覆盖成函数，因此用 import_module 显式获取子模块）
ask_user_mod = importlib.import_module("mycode.tools.ask_user")


# ===================================================================
# build_ask_options：纯函数选项拼接
# ===================================================================

class TestBuildAskOptions:
    def test_single_preset_plus_custom(self):
        opts = ask_user_mod.build_ask_options(
            [{"label": "A"}], "其他", "输入你的回答")
        assert [o.label for o in opts] == ["A", "其他"]
        assert opts[0].effective_value() == "A"
        assert opts[-1].is_custom is True
        assert opts[-1].label == "其他"
        assert opts[-1].description == "输入你的回答"

    def test_single_preset_can_be_recommended(self):
        """单选仅一个非自定义选项时仍可推荐（末尾还有自定义输入作备选）。"""
        opts = ask_user_mod.build_ask_options(
            [{"label": "A", "recommended": True}], "其他", "占位", multi=False)
        assert [o.label for o in opts] == ["A（推荐）", "其他"]
        assert opts[0].effective_value() == "A"
        assert opts[-1].is_custom is True

    def test_single_preset_not_recommended(self):
        """单个非自定义选项未标推荐时不加后缀。"""
        opts = ask_user_mod.build_ask_options(
            [{"label": "A"}], "其他", "占位", multi=False)
        assert [o.label for o in opts] == ["A", "其他"]
        assert opts[0].label == "A"

    def test_no_preset_only_custom(self):
        opts = ask_user_mod.build_ask_options(None, "其他", "占位")
        assert len(opts) == 1
        assert opts[0].is_custom is True

    def test_empty_preset_only_custom(self):
        opts = ask_user_mod.build_ask_options([], "其他", "占位")
        assert len(opts) == 1
        assert opts[0].is_custom is True

    def test_custom_label_and_placeholder_override(self):
        opts = ask_user_mod.build_ask_options(
            [{"label": "A"}], "自定义标签", "自定义占位")
        custom = opts[-1]
        assert custom.label == "自定义标签"
        assert custom.description == "自定义占位"

    def test_invalid_options_skipped(self):
        """非 dict / 空 label 的选项被跳过。"""
        opts = ask_user_mod.build_ask_options(
            ["bad", {"label": ""}, {"label": "OK"}], "其他", "占位")
        assert [o.label for o in opts if not o.is_custom] == ["OK"]


class TestBuildAskOptionsRecommended:
    def test_recommended_moved_to_first_single(self):
        """单选：推荐选项提到第一位，label 加（推荐），value 为原始标签。"""
        opts = ask_user_mod.build_ask_options(
            [
                {"label": "A", "description": "说明A"},
                {"label": "B", "recommended": True, "description": "说明B"},
                {"label": "C"},
            ],
            "其他", "输入你的回答", multi=False,
        )
        labels = [o.label for o in opts]
        assert labels[0] == "B（推荐）"
        # value 保持原始标签
        assert opts[0].effective_value() == "B"
        # 描述保留
        assert opts[0].description == "说明B"
        # 其余选项顺序保持、自定义在最后
        assert labels == ["B（推荐）", "A", "C", "其他"]
        assert [o.effective_value() for o in opts] == ["B", "A", "C", "其他"]

    def test_recommended_already_first(self):
        """推荐选项原本在第一项时原位保留并加后缀。"""
        opts = ask_user_mod.build_ask_options(
            [{"label": "X", "recommended": True}, {"label": "Y"}],
            "其他", "占位", multi=False,
        )
        assert [o.label for o in opts] == ["X（推荐）", "Y", "其他"]
        assert opts[0].effective_value() == "X"

    def test_first_recommended_wins(self):
        """多个 recommended 时取第一个。"""
        opts = ask_user_mod.build_ask_options(
            [
                {"label": "A"},
                {"label": "B", "recommended": True},
                {"label": "C", "recommended": True},
            ],
            "其他", "占位", multi=False,
        )
        assert opts[0].effective_value() == "B"
        assert opts[0].label == "B（推荐）"

    def test_multi_keeps_order_no_suffix(self):
        """多选：不调整顺序、不加推荐后缀。"""
        opts = ask_user_mod.build_ask_options(
            [{"label": "A"}, {"label": "B", "recommended": True}],
            "其他", "占位", multi=True,
        )
        assert [o.label for o in opts] == ["A", "B", "其他"]
        assert [o.effective_value() for o in opts] == ["A", "B", "其他"]

    def test_multi_custom_label_unaffected(self):
        opts = ask_user_mod.build_ask_options(
            [{"label": "A", "recommended": True}], "自定", "占位", multi=True)
        assert opts[-1].label == "自定"


# ===================================================================
# ask_user 返回值与 ask_ui 参数透传
# ===================================================================

class TestAskUserResult:
    def _run(
        self,
        *,
        title="标题",
        question=None,
        options=None,
        multi=False,
        custom_label="其他",
        placeholder="输入你的回答",
        result=None,
    ):
        """调用 ask_user，mock ask_ui_impl 返回指定 AskResult。"""
        stub = MagicMock(return_value=result)
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             patch.object(ask_user_mod, "_get_renderer") as mock_renderer:
            mock_renderer.return_value.create_prompt_style.return_value = "STYLE"
            out = ask_user_mod.ask_user(
                title=title,
                question=question,
                options=options,
                multi=multi,
                custom_label=custom_label,
                placeholder=placeholder,
            )
        return out, stub, mock_renderer

    def test_single_selection_json(self):
        out, stub, _ = self._run(
            options=[{"label": "A"}],
            result=AskResult(selected=["A"], input=None),
        )
        assert json.loads(out) == {"selected": ["A"]}
        # ask_ui 收到正确参数
        kwargs = stub.call_args.kwargs
        assert kwargs["title"] == "标题"
        # question 为 None 时传空串（ask_ui 的 description 是 str，空串跳过描述行）
        assert kwargs["description"] == ""
        assert kwargs["multi"] is False
        assert kwargs["style"] == "STYLE"
        assert [o.label for o in kwargs["options"]] == ["A", "其他"]

    def test_question_passed_as_description(self):
        out, stub, _ = self._run(
            question="完整问题？",
            options=[{"label": "A"}],
            result=AskResult(selected=["A"], input=None),
        )
        assert stub.call_args.kwargs["description"] == "完整问题？"
        assert stub.call_args.kwargs["title"] == "标题"

    def test_custom_input_included(self):
        out, _, _ = self._run(
            result=AskResult(selected=["其他"], input="自定义内容"),
        )
        assert json.loads(out) == {"selected": ["其他"], "input": "自定义内容"}

    def test_custom_empty_input_included(self):
        """选中自定义但输入为空串时也带出 input 字段。"""
        out, _, _ = self._run(
            result=AskResult(selected=["其他"], input=""),
        )
        assert json.loads(out) == {"selected": ["其他"], "input": ""}

    def test_multi_selection_ordered(self):
        out, stub, _ = self._run(
            multi=True,
            options=[{"label": "X"}, {"label": "Y"}],
            result=AskResult(selected=["Y", "X"], input=None),
        )
        assert json.loads(out) == {"selected": ["Y", "X"]}
        assert stub.call_args.kwargs["multi"] is True

    def test_no_input_field_when_custom_not_selected(self):
        out, _, _ = self._run(
            result=AskResult(selected=["A"], input=None),
        )
        assert "input" not in json.loads(out)


# ===================================================================
# abort：ask_ui aborted → 抛 AbortLoop
# ===================================================================

class TestAskUserAbort:
    def test_aborted_raises_abort_loop(self):
        stub = MagicMock(return_value=AskResult(selected=[], input=None, aborted=True))
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             pytest.raises(AbortLoop) as exc_info:
            ask_user_mod.ask_user(title="标题")
        assert exc_info.value.tool_result == "Error: 用户中止回答"


# ===================================================================
# agent_loop 集成：abort 跳出循环
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


class TestAgentLoopAskUserAbort:
    def test_abort_dispatches_tool_result_and_breaks(self):
        """ask_user 用户中止：补上 tool 占位消息 + InterruptEvent(abort=True)，
        且只调用一次模型即跳出 agent 循环。"""
        import mycode.cli as cli
        from mycode.session import InterruptEvent, ToolResultEvent

        messages: list = []
        captured: list = []
        bus = cli.AgentEventBus()
        bus.register(lambda m: captured.append(m))

        tc = {
            "id": "call_ask",
            "type": "function",
            "function": {"name": "ask_user", "arguments": json.dumps({"title": "测试"})},
        }
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=_FakeResponse(
            _FakeMessage(content="", tool_calls=[_FakeTC(tc)])
        ))

        # ask_user 内部 ask_ui 返回 aborted
        stub = MagicMock(return_value=AskResult(selected=[], input=None, aborted=True))
        with patch.object(cli, "client", fake_client), \
             patch.object(cli.ToolsRegistry, "get_handler",
                          return_value=ask_user_mod.ask_user), \
             patch.object(cli.ToolsRegistry, "get_tools", return_value=[]), \
             patch.object(ask_user_mod, "ask_ui_impl", stub):
            cli.agent_loop(messages, bus, model="test-model")

        # 只调用一次模型（跳出循环，不再继续下一轮）
        assert fake_client.chat.completions.create.call_count == 1
        # tool 占位消息含「用户中止」
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "Error: 用户中止回答"
        # 事件：ToolCallEvent + ToolResultEvent + InterruptEvent(abort=True)
        assert any(isinstance(e, ToolResultEvent) for e in captured)
        interrupts = [e for e in captured if isinstance(e, InterruptEvent)]
        assert len(interrupts) == 1
        assert interrupts[0].interrupt["abort"] is True