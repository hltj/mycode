"""
ask_user 工具测试。

覆盖：
- build_ask_options：预置选项 + 自定义选项拼接、单选推荐选项前置与
  ``（推荐）`` 后缀（value 保持原始标签）、多选不调整、自定义标签/占位
  的默认值与指定值；
- ask_user 多问题：一次 1-4 个问题的构建与透传、每问题独立 header /
  question / options / multi / custom_label / placeholder；
- ask_user 返回值：answers 数组的 JSON 序列化（selected / input /
  skipped）、ask_ui 参数透传；
- 参数校验：questions 为空 / 超 4 个 / header 缺失 / options 非数组；
- abort：ask_ui aborted → 抛 ``AbortLoop``，agent_loop 捕获后分发工具
  结果事件并弹出一轮循环。
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

from mycode.ask_ui import AskAnswer, AskResult
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
        questions,
        *,
        result=None,
    ):
        """调用 ask_user，mock ask_ui_impl 返回指定 AskResult。"""
        stub = MagicMock(return_value=result)
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             patch.object(ask_user_mod, "_get_renderer") as mock_renderer:
            mock_renderer.return_value.create_prompt_style.return_value = "STYLE"
            out = ask_user_mod.ask_user(questions=questions)
        return out, stub, mock_renderer

    def test_single_selection_json(self):
        out, stub, _ = self._run(
            questions=[{"header": "标题", "options": [{"label": "A"}]}],
            result=AskResult(answers=[AskAnswer(selected=["A"], input=None)]),
        )
        assert json.loads(out) == {"answers": [{"header": "标题", "selected": ["A"]}]}
        # ask_ui 收到正确参数（单个问题数组）
        args, kwargs = stub.call_args
        q = args[0][0]  # questions[0]
        # header 作为 AskQuestion.title
        assert q.title == "标题"
        assert q.description == ""
        assert q.multi is False
        assert kwargs["style"] == "STYLE"
        assert [o.label for o in q.options] == ["A", "其他"]

    def test_question_passed_as_description(self):
        out, stub, _ = self._run(
            questions=[{"header": "标题", "question": "完整问题？",
                        "options": [{"label": "A"}]}],
            result=AskResult(answers=[AskAnswer(selected=["A"], input=None)]),
        )
        q = stub.call_args.args[0][0]
        assert q.description == "完整问题？"
        assert q.title == "标题"

    def test_custom_input_included(self):
        out, _, _ = self._run(
            questions=[{"header": "Q"}],
            result=AskResult(answers=[AskAnswer(selected=["其他"], input="自定义内容")]),
        )
        assert json.loads(out) == {"answers": [{"header": "Q", "selected": ["其他"], "input": "自定义内容"}]}

    def test_custom_empty_input_included(self):
        """选中自定义但输入为空串时也带出 input 字段。"""
        out, _, _ = self._run(
            questions=[{"header": "Q"}],
            result=AskResult(answers=[AskAnswer(selected=["其他"], input="")]),
        )
        assert json.loads(out) == {"answers": [{"header": "Q", "selected": ["其他"], "input": ""}]}

    def test_multi_selection_ordered(self):
        out, stub, _ = self._run(
            questions=[{"header": "Q", "multi": True,
                        "options": [{"label": "X"}, {"label": "Y"}]}],
            result=AskResult(answers=[AskAnswer(selected=["Y", "X"], input=None)]),
        )
        assert json.loads(out) == {"answers": [{"header": "Q", "selected": ["Y", "X"]}]}
        assert stub.call_args.args[0][0].multi is True

    def test_no_input_field_when_custom_not_selected(self):
        out, _, _ = self._run(
            questions=[{"header": "Q"}],
            result=AskResult(answers=[AskAnswer(selected=["A"], input=None)]),
        )
        assert "input" not in json.loads(out)["answers"][0]

    def test_skipped_marked_when_unanswered(self):
        """未作答问题带 skipped=true，且没有任何选中/输入字段。"""
        out, _, _ = self._run(
            questions=[{"header": "一"}, {"header": "二"}],
            result=AskResult(answers=[
                AskAnswer(selected=["A"], input=None),
                AskAnswer(selected=[], skipped=True),
            ]),
        )
        answers = json.loads(out)["answers"]
        assert answers[0] == {"header": "一", "selected": ["A"]}
        assert answers[1] == {"header": "二", "selected": [], "skipped": True}

    def test_recommended_option_passes_through(self):
        """每问题选项各自走 build_ask_options（推荐前置 + 自定义在后）。"""
        out, stub, _ = self._run(
            questions=[{"header": "Q",
                        "options": [{"label": "A"},
                                    {"label": "B", "recommended": True}]}],
            result=AskResult(answers=[AskAnswer(selected=["B"], input=None)]),
        )
        q = stub.call_args.args[0][0]
        assert [o.label for o in q.options] == ["B（推荐）", "A", "其他"]
        assert json.loads(out) == {"answers": [{"header": "Q", "selected": ["B"]}]}

    def test_per_question_custom_label_and_placeholder(self):
        """每问题独立 custom_label / placeholder。"""
        out, stub, _ = self._run(
            questions=[
                {"header": "一", "custom_label": "自定义一", "placeholder": "输入甲"},
                {"header": "二", "custom_label": "自定义二", "placeholder": "输入乙"},
            ],
            result=AskResult(answers=[
                AskAnswer(selected=["自定义一"], input=""),
                AskAnswer(selected=["A"], input=None),
            ]),
        )
        qs = stub.call_args.args[0]
        assert [o.label for o in qs[0].options][-1] == "自定义一"
        assert [o.description for o in qs[0].options][-1] == "输入甲"
        assert [o.label for o in qs[1].options][-1] == "自定义二"
        assert [o.description for o in qs[1].options][-1] == "输入乙"

    def test_two_questions_build_passthrough(self):
        """两个问题的 AskQuestion 数组与 header 依次对应。"""
        out, stub, _ = self._run(
            questions=[
                {"header": "第一个", "options": [{"label": "A"}]},
                {"header": "第二个", "multi": True, "options": [{"label": "B"}]},
            ],
            result=AskResult(answers=[
                AskAnswer(selected=["A"], input=None),
                AskAnswer(selected=["B"], input=None),
            ]),
        )
        qs = stub.call_args.args[0]
        assert len(qs) == 2
        assert qs[0].title == "第一个"
        assert qs[1].title == "第二个"
        assert qs[1].multi is True
        assert json.loads(out) == {"answers": [
            {"header": "第一个", "selected": ["A"]},
            {"header": "第二个", "selected": ["B"]},
        ]}

    def test_four_questions_allowed(self):
        """最多支持 4 个问题。"""
        questions = [{"header": f"Q{i}"} for i in range(4)]
        out, stub, _ = self._run(
            questions=questions,
            result=AskResult(answers=[
                AskAnswer(selected=[], input=None) for _ in range(4)
            ]),
        )
        assert len(stub.call_args.args[0]) == 4
        assert len(json.loads(out)["answers"]) == 4

    def test_empty_questions_rejected(self):
        """questions 为空时返回错误文本，不进入 ask_ui。"""
        stub = MagicMock(return_value=AskResult())
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             patch.object(ask_user_mod, "_get_renderer") as mock_renderer:
            out = ask_user_mod.ask_user(questions=[])
        assert out == "Error: questions 至少需要 1 个问题"
        stub.assert_not_called()

    def test_too_many_questions_rejected(self):
        """超过 4 个问题返回错误文本，不进入 ask_ui。"""
        questions = [{"header": f"Q{i}"} for i in range(5)]
        stub = MagicMock(return_value=AskResult())
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             patch.object(ask_user_mod, "_get_renderer") as mock_renderer:
            out = ask_user_mod.ask_user(questions=questions)
        assert out == "Error: questions 最多支持 4 个问题"
        stub.assert_not_called()

    def test_missing_header_rejected(self):
        """缺少 header 或 header 非字符串返回错误文本。"""
        stub = MagicMock(return_value=AskResult())
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             patch.object(ask_user_mod, "_get_renderer") as mock_renderer:
            for bad in [{"question": "无标题"}, {"header": ""}, {"header": 123}]:
                out = ask_user_mod.ask_user(questions=[bad])
                assert out.startswith("Error: questions[0] 缺少必填字段 header")
        stub.assert_not_called()

    def test_non_dict_question_rejected(self):
        """questions 内非 dict 项返回明确索引错误。"""
        stub = MagicMock(return_value=AskResult())
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             patch.object(ask_user_mod, "_get_renderer") as mock_renderer:
            out = ask_user_mod.ask_user(questions=["bad"])
        assert out == "Error: questions[0] 必须是对象"
        stub.assert_not_called()

    def test_options_not_list_rejected(self):
        """options 非数组返回错误文本。"""
        stub = MagicMock(return_value=AskResult())
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             patch.object(ask_user_mod, "_get_renderer") as mock_renderer:
            out = ask_user_mod.ask_user(questions=[{"header": "Q", "options": "bad"}])
        assert out == "Error: questions[0].options 必须是数组"
        stub.assert_not_called()


# ===================================================================
# abort：ask_ui aborted → 抛 AbortLoop
# ===================================================================

class TestAskUserAbort:
    def test_aborted_raises_abort_loop(self):
        stub = MagicMock(return_value=AskResult(aborted=True))
        with patch.object(ask_user_mod, "ask_ui_impl", stub), \
             pytest.raises(AbortLoop) as exc_info:
            ask_user_mod.ask_user(questions=[{"header": "Q"}])
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
            "function": {"name": "ask_user", "arguments": json.dumps(
                {"questions": [{"header": "测试"}]}
            )},
        }
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=_FakeResponse(
            _FakeMessage(content="", tool_calls=[_FakeTC(tc)])
        ))

        # ask_user 内部 ask_ui 返回 aborted
        stub = MagicMock(return_value=AskResult(aborted=True))
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


# ===================================================================
# 工具注册：JSON Schema 定义
# ===================================================================

class TestAskUserRegistration:
    def test_tool_def_questions_required_array(self):
        """注册的工具定义：questions 为必填数组参数（含 header 描述）。"""
        import mycode.cli as cli  # 导入触发 tools 注册（含 ask_user）
        from mycode.tools_registry import ToolsRegistry

        tool_def = ToolsRegistry.get_tool_def("ask_user")
        assert tool_def is not None
        parameters = tool_def["function"]["parameters"]
        assert "questions" in parameters["required"]
        props = parameters["properties"]["questions"]
        assert props["type"] == "array"

        # 缺失必填参数时友好报错
        assert cli._check_missing_params("ask_user", {}) == ["questions"]

    def test_tool_def_questions_schema_expanded(self):
        """questions 用 list[AskQuestionParam]，schema 精确展开问题与选项结构。

        - item 是 object，required 仅 header（其余字段 total=False）；
        - header / question / multi / custom_label / placeholder 的 type 与
          描述透传；
        - options 是 array，其 item 展开 label/description/recommended，
          label 必填。
        """
        from mycode.tools_registry import ToolsRegistry

        tool_def = ToolsRegistry.get_tool_def("ask_user")
        assert tool_def is not None
        questions = tool_def["function"]["parameters"]["properties"]["questions"]
        q_items = questions["items"]

        # header 必填；其余字段（total=False）不进 required
        assert q_items["required"] == ["header"]
        assert q_items["properties"]["header"]["type"] == "string"
        assert q_items["properties"]["header"]["description"] == "问题简短标题（必填）"
        assert q_items["properties"]["question"]["type"] == "string"
        assert q_items["properties"]["multi"]["type"] == "boolean"
        assert q_items["properties"]["custom_label"]["type"] == "string"
        assert q_items["properties"]["placeholder"]["type"] == "string"

        # options 元素展开
        opt_items = q_items["properties"]["options"]["items"]
        assert opt_items["required"] == ["label"]
        assert opt_items["properties"]["label"]["type"] == "string"
        assert opt_items["properties"]["label"]["description"] == "选项标签（必填）"
        assert opt_items["properties"]["description"]["type"] == "string"
        assert opt_items["properties"]["recommended"]["type"] == "boolean"
