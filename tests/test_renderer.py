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
    _split_read_output,
    _first_read_lineno,
)
from mycode.session import (
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
    UserMessage,
    NoticeEvent,
    ExceptionEvent,
)

from tests._helpers import make_tool_call, make_assistant_with_tool_calls

# 兼容旧名（_make_tool_call / _make_assistant_with_tool_calls）
_make_tool_call = make_tool_call
_make_assistant_with_tool_calls = make_assistant_with_tool_calls


def _strip_ansi(text: str) -> str:
    """剥离 ANSI 转义序列，便于断言用户可见内容。"""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# 语法高亮主题色（nord）：语法 token 统一为 low-saturation 蓝灰 109
_SYNTAX_TOKEN = "\x1B[38;5;109"
_SYNTAX_TOKEN_BOLD = "\x1B[1;38;5;109"


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
            {"title": "步骤 2", "status": "in_progress"},
        ])
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c1",
                "content": "TODO 列表已更新（2 项）",
                "tool_name": "todo_write",
            },
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
        assert "TODO 列表已更新（2 项）" in _strip_ansi(out)

    def test_other_tool_renders_only_result(self, monkeypatch):
        """非 todo_write 工具：只渲染工具输出，无 TODO 列表标题。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c2",
                "content": "hello world",
                "tool_name": "bash",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "TODO 列表:" not in out
        assert "工具输出" in out
        assert "工具输出:" not in out
        # 工具输出标题为普通加粗蓝色
        assert "\x1B[1;34m工具输出\x1B[0m" in out
        assert "hello world" in _strip_ansi(out)

    def test_tool_result_without_tool_name_renders_only_result(self):
        """旧历史无 tool_name 时也能正常渲染（向下兼容）。"""
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c3",
                "content": "ok",
                "tool_name": "",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "TODO 列表:" not in out
        assert "ok" in out

    def test_tool_result_without_backticks_uses_3_fence(self, monkeypatch):
        """classic 无反引号内容：用 3 重反引号定界。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c4",
                "content": "hello world",
                "tool_name": "bash",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\n```\nhello world\n```\n" in out

    def test_tool_result_with_triple_backtick_uses_4_fence(self, monkeypatch):
        """classic 内容含 3 重反引号：定界符升为 4 重。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c5",
                "content": "代码块:\n```python\nprint(1)\n```\n结束",
                "tool_name": "bash",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        # 4 重反引号包裹，且 3 重反引号保留在内容内
        assert "````\n代码块:\n```python\nprint(1)\n```\n结束\n````\n" in out

    def test_tool_result_with_longer_backtick_run_escalates(self, monkeypatch):
        """classic 内容含 4 重反引号：定界符升为 5 重，以此类推。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c6",
                "content": "````\ninner\n````\n",
                "tool_name": "bash",
            },
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


class TestDefaultSyntaxHighlight:
    """default 风格下 rich 语法高亮渲染（工具调用 YAML / 工具结果 / read）。"""

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

    def test_tool_call_yaml_no_fence(self):
        """default 工具调用参数：无 ```yaml 围栏，纯语法高亮渲染。"""
        ev = ToolCallEvent(
            model="m",
            tool_call=_make_tool_call(call_id="c1", name="bash", args='{"command": "ls"}'),
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[1;34m🔧 调用工具 - bash\x1B[0m" in out
        # 无代码围栏
        assert "```" not in out
        # YAML 键语法高亮（nord 柔和蓝灰 109，带背景色 234）
        assert f"{_SYNTAX_TOKEN};48;5;234mcommand\x1B[0m" in out
        assert "ls" in out

    def test_tool_result_code_highlight(self):
        """default 工具结果（非 read）：rich 语法高亮渲染，无围栏。"""
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": "hello world", "tool_name": "bash"},
        )
        out = self._capture(lambda: _render_common(ev))
        assert "```" not in out
        assert "hello world" in _strip_ansi(out)

    def test_code_block_has_background(self):
        """default 代码块设背景色（rgb(30,30,30) → 256 色 234），与围栏区分。"""
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": "hello", "tool_name": "bash"},
        )
        out = self._capture(lambda: _render_common(ev))
        assert "hello" in _strip_ansi(out)
        assert "48;5;234" in out

    def test_read_output_line_numbers(self):
        """default read 输出：带行号语法高亮，剥离原始行号后由 rich 重新编号。"""
        content = "  1\tline one\n  2\tline two\n"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        # 行号由 rich 渲染（背景色 234；用剥 ANSI 后的内容验证行号前缀）
        plain = _strip_ansi(out)
        assert "1 line one" in plain
        assert "2 line two" in plain
        # 无 ``` 围栏
        assert "```" not in out

    def test_read_output_offset_lines(self):
        """default read offset>1：rich 行号从真实初始行号开始。"""
        content = "     5\talpha\n... 剩余 5 行未显示（已设置 offset/limit）"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[38;5;240;48;5;234m5 \x1B[0m\x1B[38;5;188;48;5;234malpha" in out
        # 截断提示单独抽出并以蓝灰展示（与背景区分、柔和不刺眼）
        assert "\x1B[38;5;110m... 剩余 5 行未显示（已设置 offset/limit）\x1B[0m" in out
        assert "\x1B[1;32m" not in out

    def test_read_error_no_line_numbers(self):
        """default read 错误/越界返回：非行号内容走普通代码块渲染。"""
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c",
                "content": "Error: 不是文件或不存在: x.py",
                "tool_name": "read",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "```" not in out
        assert "Error: 不是文件或不存在: x.py" in _strip_ansi(out)

    def test_read_output_python_highlight(self):
        """default read python 源码：自动检测语言并语法高亮 + 行号。"""
        content = "  1\timport json\n  2\timport os\n"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        # Python 关键字 `import` 上色（ansi_dark 下 94m，带背景色）
        assert "\x1B[1;38;5;109;48;5;234mimport\x1B[0m" in out
        # 行号仍保留
        assert "\x1B[38;5;240;48;5;234m1 \x1B[0m" in out

    def test_read_output_plain_text_no_ansi(self):
        """default read 纯文本：自动检测不到语言，仅行号无语法着色。"""
        content = "  1\tline one\n  2\tline two\n"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        # 纯文本无语法着色（不含 nord 语法 token 色 109），但有行号与背景
        assert "```" not in out
        assert "line one" in _strip_ansi(out)
        assert _SYNTAX_TOKEN not in out

    def test_read_output_filename_inferred(self):
        """default read 用调用时 file_path 推断语言（.py 扩展名 → python）。"""
        # 先派发 ToolCallEvent 记录 file_path
        cc = _make_tool_call(call_id="c1", name="read", args='{"file_path": "src/a.py"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c1",
                "content": "  1\timport json\n  2\timport os\n",
                "tool_name": "read",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[1;38;5;109;48;5;234mimport\x1B[0m" in out

    def test_read_output_filename_invalid_fallback_content(self):
        """default read 文件名不识别（如 .txt）时按内容猜，纯文本无着色。"""
        cc = _make_tool_call(call_id="c1", name="read", args='{"file_path": "data.txt"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "c1",
                "content": "  1\tline one\n",
                "tool_name": "read",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        # .txt 推断不到 → 内容猜也不到 → 无语法着色，保留行号
        assert _SYNTAX_TOKEN not in out
        assert "1 line one" in _strip_ansi(out)

    def test_bash_output_plain_no_highlight(self):
        """default bash 纯文本输出：无语法着色但有背景。"""
        cc = _make_tool_call(call_id="b1", name="bash", args='{"command": "ls"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "b1",
                "content": "total 244\nfile1.py\nfile2.py",
                "tool_name": "bash",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "total 244" in _strip_ansi(out)
        assert "\x1B[94" not in out  # 纯文件列表无 python 语法色

    def test_other_tool_text_no_guess(self):
        """default 非 read/bash 工具（如 write）：写死 text，内容不猜语法。"""
        # 即使内容形如 python 源码，也不做语法猜测
        cc = _make_tool_call(call_id="w1", name="write", args='{"file_path": "a.py", "content": "x"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "w1",
                "content": "已写入 3 字节到 a.py",
                "tool_name": "write",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "已写入 3 字节到 a.py" in _strip_ansi(out)
        assert _SYNTAX_TOKEN not in out

    def test_other_tool_pythonish_content_no_highlight(self):
        """default write 返回 python 片段：写死 text，无语法色。"""
        cc = _make_tool_call(call_id="w2", name="write", args='{"file_path": "a.py", "content": "x"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "w2",
                "content": "import json\nimport os\n",
                "tool_name": "write",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "import json" in _strip_ansi(out)
        assert _SYNTAX_TOKEN not in out
        assert _SYNTAX_TOKEN_BOLD not in out

    def test_bash_output_python_guessed(self):
        """default bash 输出含 Python 代码：内容自动猜成 python 高亮。"""
        cc = _make_tool_call(call_id="b2", name="bash", args='{"command": "python -c ..."}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={
                "tool_call_id": "b2",
                "content": "import json\nimport os\n\nclass Foo:\n    pass\n",
                "tool_name": "bash",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[1;38;5;109;48;5;234mimport\x1B[0m" in out

    def test_exception_traceback_highlight(self):
        """default 异常 traceback：rich 语法高亮渲染。"""
        from mycode.session import ExceptionEvent
        ev = ExceptionEvent(model="m", exception={
            "type": "ValueError", "message": "boom", "traceback": "tb",
        })
        out = self._capture(lambda: _render_common(ev))
        assert "❌ 异常 - ValueError - boom" in out
        assert "```" not in out
        assert "tb" in out

    def test_notice_additional_fence_highlight(self):
        """default 提醒附加内容：解析围栏语言后语法高亮。"""
        from mycode.session import NoticeEvent
        ev = NoticeEvent(model="m", notice={
            "tag_name": "notice", "content": "分派提醒", "display_content": "分派提醒",
            "additional_content": "```yaml\ncommand: ls\n```",
        })
        out = self._capture(lambda: _render_common(ev))
        # 围栏被剥离，yaml 键语法高亮
        assert "```" not in out
        assert "\x1B[38;5;109;48;5;234mcommand\x1B[0m" in out

    def test_notice_additional_plain(self):
        """default 提醒附加内容非围栏：原样输出。"""
        from mycode.session import NoticeEvent
        ev = NoticeEvent(model="m", notice={
            "tag_name": "notice", "content": "c", "display_content": "c",
            "additional_content": "plain\n",
        })
        out = self._capture(lambda: _render_common(ev))
        assert "plain" in out

    def test_notice_additional_closing_fence_length(self):
        """end_m 边界：收尾围栏长度 >= 开头围栏才闭合，否则原样。"""
        from mycode.session import NoticeEvent
        # 开头 4 重、收尾 3 重（3<4）：不闭合 → 原样输出
        ev = NoticeEvent(model="m", notice={
            "tag_name": "notice", "content": "c", "display_content": "c",
            "additional_content": "````py\nx=1\n```",
        })
        out = self._capture(lambda: _render_common(ev))
        assert "````py\nx=1\n```" in out
        assert _SYNTAX_TOKEN not in out
        # 开头 3 重、收尾 4 重（4>=3）：闭合 → 语法高亮
        ev = NoticeEvent(model="m", notice={
            "tag_name": "notice", "content": "c", "display_content": "c",
            "additional_content": "```py\nx=1\n````",
        })
        out = self._capture(lambda: _render_common(ev))
        assert "````" not in out
        assert _SYNTAX_TOKEN in out or _SYNTAX_TOKEN_BOLD in out

    def test_split_read_output_helper(self):
        """_split_read_output：末尾 ```...``` 行单独拿出，带行号行剥离行号。"""
        normal, marker = renderer._split_read_output("  1\ta\n... 剩余 2 行")
        assert normal == ["a"]
        assert marker == ["... 剩余 2 行"]

    def test_first_read_lineno_helper(self):
        """_first_read_lineno：解析首行行号。"""
        assert renderer._first_read_lineno("   5\ta") == 5
        assert renderer._first_read_lineno("no numbers") == 1

    def test_ansi_control_not_highlighted(self):
        """default 内容含 ANSI 控制码：原样输出，不做语法高亮/二次上色。"""
        content = "\x1B[32mgreen\x1B[0m files"
        cc = _make_tool_call(call_id="ans1", name="bash", args='{"command": "ls --color"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "ans1", "content": content, "tool_name": "bash"},
        )
        out = self._capture(lambda: _render_common(ev))
        # ANSI 控制码原样保留，无 nord 语法色
        assert "\x1B[32mgreen\x1B[0m files" in out
        assert "38;5;109" not in out
        # 代码块后补空行（ANSI 内容也要带末尾空行）
        assert out.endswith("\n\n")

    def test_ansi_control_in_read_plain(self):
        """default read 内容含 ANSI：绕过高亮，直接输出。"""
        content = "\x1B[31merror\x1B[0m file"
        cc = _make_tool_call(call_id="ans2", name="read", args='{"file_path": "a.bin"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "ans2", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[31merror\x1B[0m file" in out
        assert "38;5;109" not in out
        # read 含 ANSI 也补空行
        assert out.endswith("\n\n")

    def test_syntax_theme_env_var(self, monkeypatch):
        """MYCODE_SYNTAX_THEME 环境变量可覆盖语法高亮主题。"""
        monkeypatch.setenv("MYCODE_SYNTAX_THEME", "gruvbox-dark")
        # 重新加载使常量读取新环境变量
        import importlib
        importlib.reload(renderer)
        try:
            assert renderer._CODE_THEME == "gruvbox-dark"
            ev = ToolResultEvent(
                model="m",
                tool_result={"tool_call_id": "c", "content": "import json\n", "tool_name": "bash"},
            )
            out = self._capture(lambda: _render_common(ev))
            # gruvbox-dark 关键字色 203
            assert "\x1B[38;5;203" in out
        finally:
            importlib.reload(renderer)

    def test_has_ansi_control_helper(self):
        """_has_ansi_control 辅助函数。"""
        assert renderer._has_ansi_control("\x1B[31mred") is True
        assert renderer._has_ansi_control("plain") is False
        assert renderer._has_ansi_control("a\n\x1B[0m") is True



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


class TestDefaultAssistantMarkdown:
    """default 风格下 assistant 正文用 rich Markdown 渲染。"""

    @pytest.fixture(autouse=True)
    def default_style(self, monkeypatch):
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")

    def _capture(self, content):
        import io
        from contextlib import redirect_stdout
        ev = AssistantMessage(model="m", message=ChatCompletionAssistantMessageParam(
            role="assistant", content=content))
        buf = io.StringIO()
        with redirect_stdout(buf):
            _render_common(ev)
        return buf.getvalue()

    def test_plain_text_preserved(self):
        """普通文本：正文仍出现，标题为 default 机器人样式。"""
        out = self._capture("hello world")
        assert "🤖 m" in out
        assert "hello world" in _strip_ansi(out)
        # 无 ``` 围栏字样（Markdown 已解析，不是原样输出）
        assert out.endswith("\n\n")

    def test_inline_highlight(self):
        """行内样式：加粗/斜体/内联代码被 rich 富文本着色。"""
        out = self._capture("**加粗** 和 `code`")
        assert "\x1B[1m加粗\x1B[0m" in out  # 加粗
        assert "\x1B[1;36;40mcode\x1B[0m" in out  # 内联代码（cyan on black）
        assert "**加粗**" not in out  # markdown 语法被解析

    def test_heading_rendered(self):
        """标题：# 一级标题被 rich 解析为加粗下划线。"""
        out = self._capture("# 标题")
        assert "\x1B[1;4m标题\x1B[0m" in out
        assert "# 标题" not in out

    def test_code_fence_uses_code_background(self):
        """代码块：subclass 覆写用背景色 rgb(30,30,30)（256 色 234），非 rich 默认。"""
        out = self._capture("```python\nimport os\n```")
        # 代码块背景色与工具输出一致（48;5;234）
        assert "48;5;234" in out
        # 不是 rich 默认 code_block 样式（cyan on black = 36;40m）
        assert "\x1B[36;40m" not in out
        # 内容保留
        assert "import os" in _strip_ansi(out)
        # 无 ``` 围栏
        assert "```" not in out

    def test_list_rendered(self):
        """列表：bullet 符号 + 项目文本。"""
        out = self._capture("- 项目一\n- 项目二")
        assert "项目一" in _strip_ansi(out)
        assert "项目二" in _strip_ansi(out)
        assert "•" in out or _strip_ansi(out).count("  ") > 0

    def test_ansi_content_plain(self):
        """正文含 ANSI 控制码：原样输出、不经过 markdown 解析（避免二次上色）。"""
        out = self._capture("\x1B[32mgreen\x1B[0m msg")
        # ANSI 原样保留
        assert "\x1B[32mgreen\x1B[0m msg" in out
        # 无语法高亮/nord 色
        assert "38;5;109" not in out

    def test_assistant_title_line_then_body(self):
        """标题行与正文分行：标题独占一行，正文从下一行开始。"""
        out = self._capture("**加粗**")
        assert out.startswith("\x1B[35m🤖 m\x1B[0m\n")

    def test_assistant_content_empty_whitespace_title_only(self):
        """正文为空白：仅标题，不渲染正文。"""
        out = self._capture("   \n  ")
        assert "🤖 m" in out
        # 标题不带冒号
        assert "🤖 m:" not in out


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

    def test_single_in_progress(self):
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "进行中", "status": "in_progress"}])
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
            {"title": "b", "status": "in_progress"},
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

    def test_in_progress_checkbox_orange_arrow(self):
        """in_progress → ``- [>]:``，其中 > 为橙色；标题粗+白。"""
        from mycode.renderer import _format_todos
        out = _format_todos([{"title": "进行中", "status": "in_progress"}])
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
            {"title": "b", "status": "in_progress"},
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
        assert r.notice_text("hi") == "💡 hi"
        assert r.exception_title("T", "M") == "❌ 异常 - T - M"

    def test_classic_no_emoji_titles(self, monkeypatch):
        """classic 模式标题行无 emoji。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        r = renderer._get_renderer()
        assert r.tool_call_title("bash") == "调用工具 - bash"
        assert r.tool_result_title() == "工具输出"
        assert r.notice_text("hi") == "hi"
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
            tool_result={
                "tool_call_id": "c2",
                "content": "hello",
                "tool_name": "bash",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        assert "📤 工具输出" in out

    def test_default_notice_renders_emoji(self):
        """default 模式系统提醒带 💡。"""
        from mycode.session import NoticeEvent
        ev = NoticeEvent(
            model="m", notice={"tag_name": "reminder", "content": "提醒"}
        )
        out = self._capture(lambda: _render_common(ev))
        assert "💡 提醒" in out

    def test_default_notice_body_plain_output(self):
        """命令已更新提醒：提醒文本整体用提醒格式，附加内容代码块default语法高亮。"""
        from mycode.session import NoticeEvent
        ev = NoticeEvent(
            model="m",
            notice={
                "tag_name": "notice",
                "content": "用户将命令修改为：",
                "display_content": "命令修改为：",
                "additional_content": "```bash\nls -la\n```",
            },
        )
        out = self._capture(lambda: _render_common(ev))
        # 提醒文本用提醒格式（黄色 + 💡），不带 <reminder> 标签
        assert "\x1B[1;33m💡 命令修改为：\x1B[0m" in out
        # 附加内容代码块 default 走语法高亮（保留 bash 内容的 ls -la，无 ``` 围栏）
        assert "ls" in out and "-la" in out
        assert "```" not in out
        assert "用户将命令修改为：" not in out
        assert "<reminder>" not in out

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
