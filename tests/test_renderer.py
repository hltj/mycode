"""
渲染器（mycode.renderer）的单元与渲染测试。

覆盖：
- _render_common 对 ToolResultEvent / ToolCallEvent / AssistantMessage /
  UserMessage 的渲染
- _format_todos 的 default / classic 两种风格
- _code_fence / _wrap_by_display_width 辅助函数
- default 风格各标题行 emoji
"""

from __future__ import annotations

import pytest

from openai.types.chat import ChatCompletionAssistantMessageParam

import mycode.renderer as renderer
from mycode.renderer import (
    _render_common,
    _code_fence,
    _format_todos,
    _get_renderer,
    _wrap_by_display_width,
    _prompt_fragments,
)
from mycode.session import (
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
    UserMessage,
    ReminderEvent,
    ExceptionEvent,
)

from tests._helpers import make_tool_call, make_assistant_with_tool_calls

# 兼容旧名（_make_tool_call / _make_assistant_with_tool_calls）
_make_tool_call = make_tool_call
_make_assistant_with_tool_calls = make_assistant_with_tool_calls


class TestRenderCommonToolResult:
    """_render_common 中 ToolResultEvent 的渲染（含 todo_write 特化）。"""

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_todo_write_renders_list_before_result(self, monkeypatch):
        """todo_write 工具结果渲染：先输出待办列表，再输出结果。"""
        from mycode.tools.todo_write import todo_write, reset_todos
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        reset_todos()
        todo_write([
            {"title": "步骤 1", "status": "completed"},
            {"title": "步骤 2", "status": "in_process"},
        ])
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c1", "content": "TODO 列表已更新（2 项）"},
            tool_name="todo_write",
        )
        out = self._capture(lambda: _render_common(ev))
        # 待办列表标题 + 符号 + 内容应先出现
        assert "TODO 列表:" in out
        assert "✅️" in out
        assert "步骤 1" in out
        assert "🟧" in out
        assert "步骤 2" in out
        # 然后是工具输出
        idx_list = out.index("TODO 列表:")
        idx_result = out.index("工具输出")
        assert idx_list < idx_result
        assert "TODO 列表已更新（2 项）" in out

    def test_other_tool_renders_only_result(self, monkeypatch):
        """非 todo_write 工具：只渲染工具输出，无 TODO 列表标题。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c2", "content": "hello world"},
            tool_name="bash",
        )
        out = self._capture(lambda: _render_common(ev))
        assert "TODO 列表:" not in out
        assert "工具输出" in out
        assert "工具输出:" not in out
        # 工具输出标题为普通加粗蓝色
        assert "\x1B[1;34m工具输出\x1B[0m" in out
        assert "hello world" in out

    def test_tool_result_without_tool_name_renders_only_result(self):
        """旧历史无 tool_name 时也能正常渲染（向下兼容）。"""
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c3", "content": "ok"},
            # tool_name 默认空
        )
        out = self._capture(lambda: _render_common(ev))
        assert "TODO 列表:" not in out
        assert "ok" in out

    def test_tool_result_without_backticks_uses_3_fence(self):
        """无反引号内容：用 3 重反引号定界。"""
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c4", "content": "hello world"},
            tool_name="bash",
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\n```\nhello world\n```\n" in out

    def test_tool_result_with_triple_backtick_uses_4_fence(self):
        """内容含 3 重反引号：定界符升为 4 重。"""
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c5",
                     "content": "代码块:\n```python\nprint(1)\n```\n结束"},
            tool_name="bash",
        )
        out = self._capture(lambda: _render_common(ev))
        # 4 重反引号包裹，且 3 重反引号保留在内容内
        assert "````\n代码块:\n```python\nprint(1)\n```\n结束\n````\n" in out

    def test_tool_result_with_longer_backtick_run_escalates(self):
        """内容含 4 重反引号：定界符升为 5 重，以此类推。"""
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c6",
                     "content": "````\ninner\n````\n"},
            tool_name="bash",
        )
        out = self._capture(lambda: _render_common(ev))
        assert "`````\n````\ninner\n````\n`````\n" in out

    def test_code_fence_helper(self):
        """_code_fence 辅助函数：根据最长连续反引号决定围栏长度。"""
        from mycode.renderer import _code_fence
        assert _code_fence("no backticks") == "```"
        assert _code_fence("a ``` b") == "````"
        assert _code_fence("`````long") == "``````"
        # 空内容也取最短 3 重
        assert _code_fence("") == "```"



class TestRenderCommonToolCall:
    """_render_common 中 ToolCallEvent 的输出样式（classic 风格标题）。"""

    @pytest.fixture(autouse=True)
    def classic_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_tool_call_title_blue_no_colon(self):
        """调用工具标题为普通加粗蓝色，且不带冒号。"""
        ev = ToolCallEvent(
            model="m",
            tool_call=_make_tool_call(call_id="c1", name="bash", args='{"command": "ls"}'),
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[1;34m调用工具 - bash\x1B[0m" in out
        assert "调用工具 - bash:" not in out
        assert "command" in out



class TestRenderCommonAssistantUser:
    """_render_common 中 AssistantMessage / UserMessage 的输出样式（classic）。"""

    @pytest.fixture(autouse=True)
    def classic_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_assistant_with_content_still_renders_title_and_text(self):
        """有文字输出的助手消息：标题 + 正文（原有行为不变）。"""
        ev = AssistantMessage(model="m", message=ChatCompletionAssistantMessageParam(
            role="assistant", content="hello"))
        out = self._capture(lambda: _render_common(ev))
        # 标题为紫色（不加粗）、无冒号
        assert "\x1B[35mAI【m】\x1B[0m" in out
        assert "hello" in out

    def test_assistant_tool_calls_only_renders_title(self):
        """仅有 tool_calls、无文字输出的助手消息也要展示标题。"""
        ev = AssistantMessage(
            model="m",
            message=_make_assistant_with_tool_calls(_make_tool_call()),
        )
        out = self._capture(lambda: _render_common(ev))
        assert "AI【m】" in out
        assert "AI【m】:" not in out

    def test_assistant_content_none_with_tool_calls_renders_title(self):
        """content 为 None（纯 tool_calls）的助手消息同样展示标题。"""
        ev = AssistantMessage(
            model="m",
            message=ChatCompletionAssistantMessageParam(
                role="assistant", content=None,
                tool_calls=[_make_tool_call()],
            ),
        )
        out = self._capture(lambda: _render_common(ev))
        assert "AI【m】" in out
        assert "AI【m】:" not in out

    def test_user_message_trailing_blank_line(self):
        """用户消息输出之后要加一个空行。"""
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage

        ev = UserMessage(model="m", message=ChatCompletionUserMessageParam(
            role="user", content="hi"))
        out = self._capture(lambda: _render_common(ev))
        assert "hi" in out
        # print 自身带一个换行，再加一个空行 => 结尾为 "\n\n"
        assert out.endswith("\n\n")


class TestFormatTodos:
    """renderer._format_todos 的单元测试：CLI 内部的 TODO 渲染辅助。

    注意：以下用例断言的是 default 风格（emoji 符号）默认行为。
    classic 风格见 TestFormatTodosClassic。
    """

    @pytest.fixture(autouse=True)
    def default_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")

    def _reset(self):
        from mycode.tools.todo_write import reset_todos
        reset_todos()

    def test_empty_state_returns_placeholder(self):
        from mycode.renderer import _format_todos
        self._reset()
        out = _format_todos([])
        assert out == "(TODO 列表为空)"

    def test_empty_state_when_no_arg_uses_get_todos(self):
        """无参时从 get_todos() 取值；空状态返回占位符。"""
        from mycode.renderer import _format_todos
        self._reset()
        assert _format_todos() == "(TODO 列表为空)"

    def test_single_completed(self):
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "做完了", "status": "completed"}])
        # 已完成：emoji + 1 空格 + 标题灰色 + 删除线
        assert out == "✅️ \x1B[90m\x1B[9m做完了\x1B[0m"

    def test_single_in_process(self):
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "进行中", "status": "in_process"}])
        # 进行中：🟧 + 1 空格 + 标题粗+白
        assert out == "🟧 \x1B[1;37m进行中\x1B[0m"

    def test_single_pending(self):
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "待办", "status": "pending"}])
        # 未开始：🔳 + 1 空格 + 普通文本
        assert out == "🔳 待办"

    def test_mixed_statuses_order_preserved(self):
        from mycode.renderer import _format_todos
        items = [
            {"title": "a", "status": "completed"},
            {"title": "b", "status": "in_process"},
            {"title": "c", "status": "pending"},
        ]
        out = _format_todos(items)
        assert out == (
            "✅️ \x1B[90m\x1B[9ma\x1B[0m\n"
            "🟧 \x1B[1;37mb\x1B[0m\n"
            "🔳 c"
        )

    def test_explicit_state_overrides_get_todos(self):
        """显式传入 state 时不读内存状态。"""
        from mycode.renderer import _format_todos
        from mycode.tools.todo_write import todo_write, get_todos
        self._reset()
        todo_write([{"title": "ignored", "status": "completed"}])
        # 显式传入不同内容，验证不读 get_todos
        out = _format_todos([{"title": "explicit", "status": "pending"}])
        assert out == "🔳 explicit"
        # get_todos 不受影响（仍然是被 set 的）
        assert get_todos()[0]["title"] == "ignored"

    def test_unknown_status_raises_keyerror(self):
        """未知 status 抛 KeyError：上游 todo_write 已保证 status 合法。"""
        import pytest
        from mycode.renderer import _format_todos
        with pytest.raises(KeyError):
            _format_todos([{"title": "x", "status": "weird"}])



class TestFormatTodosClassic:
    """_format_todos 的 classic 风格：复选框符号。"""

    @pytest.fixture(autouse=True)
    def classic_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")

    def test_completed_checkbox_green_x(self):
        """completed → ``- [x]:``，其中 x 为绿色；标题灰色+删除线。"""
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "做完了", "status": "completed"}])
        assert out == "- [\x1B[32mx\x1B[0m]: \x1B[90m\x1B[9m做完了\x1B[0m"

    def test_in_process_checkbox_orange_arrow(self):
        """in_process → ``- [>]:``，其中 > 为橙色；标题粗+白。"""
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "进行中", "status": "in_process"}])
        assert out == "- [\x1B[38;2;255;165;0m>\x1B[0m]: \x1B[1;37m进行中\x1B[0m"

    def test_pending_checkbox_empty(self):
        """pending → ``- [ ]:``；标题普通样式。"""
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "待办", "status": "pending"}])
        assert out == "- [ ]: 待办"

    def test_mixed_statuses_classic(self):
        from mycode.renderer import _format_todos
        items = [
            {"title": "a", "status": "completed"},
            {"title": "b", "status": "in_process"},
            {"title": "c", "status": "pending"},
        ]
        out = _format_todos(items)
        assert out == (
            "- [\x1B[32mx\x1B[0m]: \x1B[90m\x1B[9ma\x1B[0m\n"
            "- [\x1B[38;2;255;165;0m>\x1B[0m]: \x1B[1;37mb\x1B[0m\n"
            "- [ ]: c"
        )

    def test_empty_state_classic(self):
        from mycode.renderer import _format_todos
        assert _format_todos([]) == "(TODO 列表为空)"



class TestRenderStyle:
    """渲染风格 default / classic 的差异验证。"""

    @pytest.fixture(autouse=True)
    def default_style(self, monkeypatch):
        # 每个用例自行设置；默认恢复 default（模块默认值）
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    # ---- AI 标题 ----

    def test_ai_title_classic(self, monkeypatch):
        """classic：``AI【{模型}】``。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        assert renderer._get_renderer().ai_title("m") == "AI【m】"

    def test_ai_title_default(self):
        """default：机器人 emoji + 模型名，无【】。"""
        assert renderer._get_renderer().ai_title("m") == "🤖 m"

    def test_assistant_default_renders_robot_title(self):
        ev = AssistantMessage(model="m", message=ChatCompletionAssistantMessageParam(
            role="assistant", content="hi"))
        out = self._capture(lambda: _render_common(ev))
        assert "🤖 m" in out
        assert "AI【m】" not in out

    # ---- 用户消息 ----

    def test_user_message_classic(self, monkeypatch):
        """classic：``myc[自动] > `` 前缀（跟随模式）+ 末尾空行。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage
        ev = UserMessage(model="m", message=ChatCompletionUserMessageParam(
            role="user", content="hi"))
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[38;2;0;204;0;1mmyc[自动] > \x1B[0mhi" in out
        assert out.endswith("\n\n")

    def test_user_message_default_bar_gray_bg(self, monkeypatch):
        """default：首行竖线（提示符色）+ 灰色背景填充 + 末尾空行。"""
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage
        monkeypatch.setenv("COLUMNS", "10")
        ev = UserMessage(model="m", message=ChatCompletionUserMessageParam(
            role="user", content="hi"))
        out = self._capture(lambda: _render_common(ev))
        # "│ hi" 显示宽度 4，COLUMNS=10 → 补 6 空格；竖线为提示符绿色加粗
        expected = (
            "\x1B[48;2;51;51;51m"
            "\x1B[38;2;0;204;0;1m│\x1B[39;22m"
            " hi      \x1B[0m"
        )
        assert expected in out
        assert "myc >" not in out
        assert out.endswith("\n\n")

    def test_user_message_default_multiline(self, monkeypatch):
        """default 多行：仅第一行有竖线，续行无竖线不缩进，均全量填充。"""
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage
        monkeypatch.setenv("COLUMNS", "10")
        ev = UserMessage(model="m", message=ChatCompletionUserMessageParam(
            role="user", content="a\nb"))
        out = self._capture(lambda: _render_common(ev))
        # 第一行："│ a" 宽 3 → 补 7 空格，竖线提示符色
        first = (
            "\x1B[48;2;51;51;51m"
            "\x1B[38;2;0;204;0;1m│\x1B[39;22m"
            " a       \x1B[0m"
        )
        assert first in out
        # 第二行："b" 宽 1 → 补 9 空格，无竖线无缩进
        assert "\x1B[48;2;51;51;51mb         \x1B[0m" in out

    def test_user_message_default_wide_char_padding(self, monkeypatch):
        """default 宽字符（中文占 2 列）：按显示宽度计算填充。"""
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage
        monkeypatch.setenv("COLUMNS", "10")
        ev = UserMessage(model="m", message=ChatCompletionUserMessageParam(
            role="user", content="你好"))
        out = self._capture(lambda: _render_common(ev))
        # "│ 你好" 显示宽度 1+1+2+2=6，COLUMNS=10 → 补 4 个空格
        expected = (
            "\x1B[48;2;51;51;51m"
            "\x1B[38;2;0;204;0;1m│\x1B[39;22m"
            " 你好    \x1B[0m"
        )
        assert expected in out

    def test_user_message_default_overwide_wraps_and_pads(self, monkeypatch):
        """default 超宽行（超过终端宽度）：自行分行，每段均填充。"""
        from openai.types.chat import ChatCompletionUserMessageParam
        from mycode.session import UserMessage
        monkeypatch.setenv("COLUMNS", "5")
        ev = UserMessage(model="m", message=ChatCompletionUserMessageParam(
            role="user", content="abcdefgh"))
        out = self._capture(lambda: _render_common(ev))
        # "│ abcdefgh" 显示宽度 10 > 5：按宽度分行，每段补齐到 5 列。
        # 仅第一行首段含提示符色竖线，换行续段顶格无竖线。
        first_seg = (
            "\x1B[48;2;51;51;51m"
            "\x1B[38;2;0;204;0;1m│\x1B[39;22m"
            " abc\x1B[0m\n"
        )
        assert first_seg in out
        assert "\x1B[48;2;51;51;51mdefgh\x1B[0m\n" in out

    # ---- 默认模式标题行 emoji ----

    def test_default_emoji_titles(self):
        """default 模式各标题行开头带 emoji + 空格。"""
        r = renderer._get_renderer()
        assert r.tool_call_title("bash") == "🔧 调用工具 - bash"
        assert r.tool_result_title() == "📤 工具输出"
        assert r.reminder_text("hi") == "💡 hi"
        assert r.exception_title("T", "M") == "❌ 异常 - T - M"

    def test_classic_no_emoji_titles(self, monkeypatch):
        """classic 模式标题行无 emoji。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        r = renderer._get_renderer()
        assert r.tool_call_title("bash") == "调用工具 - bash"
        assert r.tool_result_title() == "工具输出"
        assert r.reminder_text("hi") == "hi"
        assert r.exception_title("T", "M") == "异常 - T - M"

    def test_default_tool_call_renders_emoji(self):
        """default 模式调用工具标题带 🔧。"""
        ev = ToolCallEvent(
            model="m",
            tool_call=_make_tool_call(call_id="c1", name="bash", args='{"command": "ls"}'),
        )
        out = self._capture(lambda: _render_common(ev))
        assert "🔧 调用工具 - bash" in out

    def test_default_tool_result_renders_emoji(self):
        """default 模式工具输出标题带 📤。"""
        ev = ToolResultEvent(
            model="m",
            message={"role": "tool", "tool_call_id": "c2", "content": "hello"},
            tool_name="bash",
        )
        out = self._capture(lambda: _render_common(ev))
        assert "📤 工具输出" in out

    def test_default_reminder_renders_emoji(self):
        """default 模式系统提醒带 💡。"""
        from mycode.session import ReminderEvent
        ev = ReminderEvent(model="m", content="提醒")
        out = self._capture(lambda: _render_common(ev))
        assert "💡 提醒" in out

    def test_default_exception_renders_emoji(self):
        """default 模式异常标题带 ❌。"""
        from mycode.session import ExceptionEvent
        ev = ExceptionEvent(model="m", exception={
            "type": "ValueError", "message": "boom", "traceback": "tb",
        })
        out = self._capture(lambda: _render_common(ev))
        assert "❌ 异常 - ValueError - boom" in out

    # ---- _wrap_by_display_width ----

    def test_wrap_short_line_padded(self):
        """短行补齐到 columns 列。"""
        assert renderer._wrap_by_display_width("│ hi", 10) == ["│ hi      "]

    def test_wrap_long_line_split(self):
        """超宽行按宽度分段，末段补齐。"""
        assert renderer._wrap_by_display_width("│ " + "a" * 15, 10) == [
            "│ aaaaaaaa", "aaaaaaa   "]

    def test_wrap_exact_multiple_no_extra_line(self):
        """宽度恰为整数倍：不产生多余的全空格行。"""
        assert renderer._wrap_by_display_width("│ ab", 4) == ["│ ab"]

    def test_wrap_wide_char_at_boundary(self):
        """宽字符在行尾放不下：当前行以空格补满，宽字符转下行。"""
        # "│ 你" 宽 4，"好" 再占 2 放不下（columns=5）；
        # 第二段 "好" 宽 2，补 3 个空格
        assert renderer._wrap_by_display_width("│ 你好", 5) == ["│ 你 ", "好   "]

    def test_wrap_zero_width_char_follows(self):
        """零宽字符（emoji 变体选择符等）跟随前一字符，不计宽度。"""
        # ✅(2) + \ufe0f(0) + a(1) = 宽 3；竖线+空格 2 → 总宽 5，恰整行
        assert renderer._wrap_by_display_width("│ ✅️a", 5) == ["│ ✅️a"]

    def test_wrap_empty_body(self):
        """空内容也产出一行（补齐空格）。"""
        assert renderer._wrap_by_display_width("│ ", 5) == ["│    "]

    # ---- 提示符片段 ----

    def test_prompt_fragments_classic(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        assert renderer._prompt_fragments() == [('class:mycode-prompt', 'myc[自动] > ')]

    def test_prompt_fragments_default(self):
        assert renderer._prompt_fragments() == [('class:mycode-prompt', '│ ')]
