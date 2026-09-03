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

import re

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

from tests._helpers import (
    make_tool_call,
    make_assistant_with_tool_calls,
    ansi_fg,
    ansi_bg,
    ansi_fg_bg,
    _fg_sgr,
    _bg_sgr,
)

# 兼容旧名（_make_tool_call / _make_assistant_with_tool_calls）
_make_tool_call = make_tool_call
_make_assistant_with_tool_calls = make_assistant_with_tool_calls


def _strip_ansi(text: str) -> str:
    """剥离 ANSI 转义序列，便于断言用户可见内容。"""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ---------------------------------------------------------------------------
# 渲染颜色常量（RGB 三元组）：
# 这些是 pygments nord 主题的原始 RGB 值，rich 在 truecolor 下原样输出、
# 在 256 色下自动降级为 256 索引（如 nord Keyword #81a1c1 → 38;5;109）。
# 测试断言的颜色码由 ansi_fg/ansi_bg/ansi_fg_bg 动态生成（按当前 Console
# color_system 切换 24-bit / 256 色 / 16 色格式），避免硬编码 256 色码。
# ---------------------------------------------------------------------------

# 代码块背景色（renderer.py 中 _CODE_BG_RGB）
_CODE_BG_RGB = (30, 30, 30)
# nord Keyword 颜色 #81a1c1（bold 时变亮；token 默认就用此色）
_NORD_KEYWORD_RGB = (129, 161, 193)
# nord Text 颜色 #d8dee9（普通文本 token）
_NORD_TEXT_RGB = (216, 222, 233)
# gruvbox-dark Keyword 颜色 #fb4934
_GRUVBOX_KEYWORD_RGB = (251, 73, 52)
# 行号前景：rich 把背景色与 nord Text 按 30% 混合得到 (85,87,90)
# （见 rich.syntax.Syntax._get_line_numbers_color blend=0.3）
from rich.color import blend_rgb
_LINE_NUM_RGB = blend_rgb(_CODE_BG_RGB, _NORD_TEXT_RGB, cross_fade=0.3)
_LINE_NUM_RGB = (_LINE_NUM_RGB.red, _LINE_NUM_RGB.green, _LINE_NUM_RGB.blue)


# 渲染器源码中硬编码的 ANSI 常量（与 Console color_system 无关，固定 256 色）。
# 这些是 renderer.py 直接拼接到 print 的字符串（如 _spacer_text(_CODE_BG)），
# 不会经过 rich 的颜色降级；测试断言这些字符串出现时仍按源码固定值断言。
_HIGHLIGHT_MUTED_RAW = "\x1B[38;5;110m"   # renderer._HIGHLIGHT_MUTED
_CODE_BG_RAW = "\x1B[48;5;234m"           # renderer._CODE_BG


# 按当前 color_system 动态生成函数（用于渲染输出断言）。
# 注：原 _SYNTAX_TOKEN / _SYNTAX_TOKEN_BOLD 在 256 色终端下是 "\x1B[38;5;109" /
# "\x1B[1;38;5;109"，但当前真彩色终端会输出 "\x1B[38;2;129;161;193"。用 helper
# 生成与终端能力匹配的 SGR 子串（不含 \x1b[ 和 m），便于子串匹配
# （rich 经常把前景+背景合并到同一个 SGR，所以"独立前景 ANSI 序列"匹配不成立）。
# 用函数而非模块常量，因为 _current_color_system() 依赖运行时环境变量
# （parametrize fixture 会按测试切换）。
def _syntax_token():
    """nord Keyword 前景色 SGR 子串（按当前 color_system）。"""
    return _fg_sgr(_NORD_KEYWORD_RGB)

def _syntax_token_bold():
    """nord Keyword bold 前景色 SGR 子串（按当前 color_system）。"""
    return _fg_sgr(_NORD_KEYWORD_RGB, bold=True)

# 行号前景：rich 把背景与 nord Text 按 30% 混合得到 (85,87,90)
# （见 rich.syntax.Syntax._get_line_numbers_color blend=0.3）
# 用前景 SGR 子串（不含 \x1b[ 和 m）做"是否包含行号色"的负向断言锚点。
_LINE_NUM_COLOR_FRAGMENT = _fg_sgr(_LINE_NUM_RGB)


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
    """default 风格下 rich 语法高亮渲染（工具调用 YAML / 工具结果 / read）。

    通过 ``_set_color_system`` autouse fixture，每个测试在真彩色 / 256 色 /
    16 色三种终端能力下各验证一次，覆盖各种 color_system 下的渲染输出。
    """

    @pytest.fixture(autouse=True)
    def _auto_color_system(self, _set_color_system):
        """在三种 color_system 下自动覆盖当前测试。"""
        return _set_color_system

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
        """default 工具调用参数（非特化工具）：无 ```yaml 围栏，纯语法高亮渲染。"""
        ev = ToolCallEvent(
            model="m",
            tool_call=_make_tool_call(call_id="c1", name="ls", args='{"dir_path": "."}'),
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[1;34m🔧 调用工具 - ls\x1B[0m" in out
        # 无代码围栏
        assert "```" not in out
        # YAML 键语法高亮（nord Keyword 前景 + 代码块背景；前景/背景合并到一个 SGR）
        assert f"{ansi_fg_bg(_NORD_KEYWORD_RGB, _CODE_BG_RGB)}dir_path\x1b[0m" in out
        assert "dir_path" in _strip_ansi(out)

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
        assert ansi_bg(_CODE_BG_RGB) in out

    def test_code_block_top_bottom_blank(self):
        """default 普通代码块：上下各留 1 行纯背景空行，与 markdown 风格统一。"""
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": "hello", "tool_name": "bash"},
        )
        out = self._capture(lambda: _render_common(ev))
        plain = _strip_ansi(out)
        lines = plain.split("\n")
        # 结构：0 标题 / 1 上留白 / 2 正文 / 3 下留白
        assert lines[0].startswith("📤 工具输出")
        assert lines[1].strip() == ""
        assert lines[2].strip() == "hello"
        assert lines[3].strip() == ""
        # 留白行带代码块背景
        assert ansi_bg(_CODE_BG_RGB) in out

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
        # 行号 5 + alpha：rich 行号前景（灰 blend）+ nord Text 前景，都带代码块背景
        # （行号段与正文段是两个独立 SGR，中间有 RESET）
        assert f"{self._line_num_sgr('5')}\x1b[0m{ansi_fg_bg(_NORD_TEXT_RGB, _CODE_BG_RGB)}alpha" in out
        # 截断提示行：源码 hardcoded 的蓝灰前景 + 深灰背景（与代码块同画布），无行号
        assert f"{_HIGHLIGHT_MUTED_RAW}{_CODE_BG_RAW}... 剩余 5 行未显示（已设置 offset/limit）" in out
        assert f"{_HIGHLIGHT_MUTED_RAW}... 剩余" not in out  # 提示行不再无背景
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

    def test_read_output_padding_blank_lines_no_lineno(self):
        """default read：代码块上下各留 1 行纯背景空行且不含行号。"""
        content = "  1\tline one\n  2\tline two\n"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        # 结构：0 标题 / 1 上留白 / 2-3 正文 / 4 下留白
        plain = _strip_ansi(out)
        lines = plain.split("\n")
        assert lines[1].strip() == ""
        assert "1 line one" in lines[2]
        assert "2 line two" in lines[3]
        assert lines[4].strip() == ""
        # 背景画布行（rich 渲染的代码块背景）存在
        assert ansi_bg(_CODE_BG_RGB) in out

    def test_read_marker_has_bg_and_no_lineno(self):
        """default read 截断提示行：蓝灰前景 + 深灰背景，无行号，上下有留白。"""
        content = "  1\tline one\n... 已截断"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        # 提示行带背景色与蓝灰前景（renderer 中 hardcoded 字符串拼接，与 color_system 无关）
        assert f"{_HIGHLIGHT_MUTED_RAW}{_CODE_BG_RAW}... 已截断" in out
        # 提示行后紧跟 1 行纯背景空行（无行号）收尾
        assert f"{_CODE_BG_RAW}                                                                                \x1b[0m\n" in out

    def test_read_marker_no_background_before(self):
        """default read 截断提示行：不能以无背景的纯蓝灰形式出现（需带画布背景）。"""
        content = "  1\tline one\n... 已截断"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        assert f"{_HIGHLIGHT_MUTED_RAW}... 已截断" not in out

    def test_read_marker_adjacent_to_body_no_blank(self):
        """default read：行号正文与截断提示行之间无空行。"""
        content = "  1\tline one\n  2\tline two\n... 剩余 2 行未显示"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        plain = _strip_ansi(out)
        lines = plain.split("\n")
        # 结构：0 标题 / 1 上留白 / 2-3 正文 / 4 提示行 / 5 下留白
        assert "1 line one" in lines[2]
        assert "2 line two" in lines[3]
        assert lines[4].strip() == "... 剩余 2 行未显示"
        assert lines[5].strip() == ""
        # 提示行与正文最后一行之间无空行（lines[3] 直接到 lines[4]）

    def test_read_only_marker_no_lineno_block(self):
        """default read 只有截断行（无行号正文）：走普通代码块渲染，上下留白。"""
        content = "... 已截断"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        plain = _strip_ansi(out)
        lines = plain.split("\n")
        # 结构：0 标题 / 1 上留白 / 2 提示行 / 3 下留白
        assert lines[0].startswith("📤 工具输出")
        assert lines[1].strip() == ""
        assert lines[2].strip() == "... 已截断"
        assert lines[3].strip() == ""
        # 无带行号的空块（行号色段不出现：256 色下 38;5;240，真彩色下 38;2;85;87;90，
        # 都通过"行号前景+代码块背景"组合片段的有无做交叉验证）
        assert _LINE_NUM_COLOR_FRAGMENT not in out
        # 内容带画布深灰背景（rich 渲染的 nord Text + 代码块背景）
        assert f"{ansi_fg_bg(_NORD_TEXT_RGB, _CODE_BG_RGB)}... 已截断" in out

    def test_read_output_python_highlight(self):
        """default read python 源码：自动检测语言并语法高亮 + 行号。"""
        content = "  1\timport json\n  2\timport os\n"
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "c", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        # Python 关键字 `import` 上色（nord Keyword bold + 代码块背景）
        assert f"{ansi_fg_bg(_NORD_KEYWORD_RGB, _CODE_BG_RGB, bold=True)}import\x1b[0m" in out
        # 行号仍保留（行号前景 + 代码块背景；STANDARD 下退化为 dim）
        self._assert_line_number_for(out, num="1")

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
        assert _syntax_token() not in out

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
        assert f"{ansi_fg_bg(_NORD_KEYWORD_RGB, _CODE_BG_RGB, bold=True)}import\x1b[0m" in out

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
        assert _syntax_token() not in out
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
        assert _syntax_token() not in out

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
        assert _syntax_token() not in out
        assert _syntax_token_bold() not in out

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
        assert f"{ansi_fg_bg(_NORD_KEYWORD_RGB, _CODE_BG_RGB, bold=True)}import\x1b[0m" in out

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
        # 围栏被剥离，yaml 键语法高亮（nord Keyword + 代码块背景）
        assert "```" not in out
        assert f"{ansi_fg_bg(_NORD_KEYWORD_RGB, _CODE_BG_RGB)}command\x1b[0m" in out

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
        assert _syntax_token() not in out
        # 开头 3 重、收尾 4 重（4>=3）：闭合 → 语法高亮
        ev = NoticeEvent(model="m", notice={
            "tag_name": "notice", "content": "c", "display_content": "c",
            "additional_content": "```py\nx=1\n````",
        })
        out = self._capture(lambda: _render_common(ev))
        assert "````" not in out
        assert _syntax_token() in out or _syntax_token_bold() in out

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
        """default 内容含 ANSI 控制码：用代码围栏原样包裹，不做语法高亮。"""
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
        assert _syntax_token() not in out
        # 代码围栏原样包裹（同 classic），末尾空行
        assert "\n```\n\x1B[32mgreen\x1B[0m files\n```\n" in out
        assert out.endswith("\n\n")

    def test_ansi_control_in_read_plain(self):
        """default read 内容含 ANSI：用代码围栏原样包裹。"""
        content = "\x1B[31merror\x1B[0m file"
        cc = _make_tool_call(call_id="ans2", name="read", args='{"file_path": "a.bin"}')
        self._capture(lambda: _render_common(ToolCallEvent(model="m", tool_call=cc)))
        ev = ToolResultEvent(
            model="m",
            tool_result={"tool_call_id": "ans2", "content": content, "tool_name": "read"},
        )
        out = self._capture(lambda: _render_common(ev))
        assert "\x1B[31merror\x1B[0m file" in out
        assert _syntax_token() not in out
        # 代码围栏包裹，末尾空行
        assert "\n```\n\x1B[31merror\x1B[0m file\n```\n" in out
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
            # gruvbox-dark 关键字色：#fb4934（256 色下 → 38;5;203；truecolor → 38;2;251;73;52）
            # 仅断言前景 SGR 子串存在（前景/背景常合并为一个完整 SGR）
            assert _fg_sgr(_GRUVBOX_KEYWORD_RGB) in out
        finally:
            # 撤销环境变量后再 reload，避免下一个测试仍看到 gruvbox-dark 主题
            monkeypatch.undo()
            importlib.reload(renderer)

    def test_has_ansi_control_helper(self):
        """_has_ansi_control 辅助函数。"""
        assert renderer._has_ansi_control("\x1B[31mred") is True
        assert renderer._has_ansi_control("plain") is False
        assert renderer._has_ansi_control("a\n\x1B[0m") is True

    def _capture_tool_call(self, tool_call):
        return self._capture(lambda: _render_common(
            ToolCallEvent(model="m", tool_call=tool_call)))

    def _assert_line_number_for(self, out, num: str = "1"):
        """断言输出包含 rich 行号渲染：行号 ``num``（默认 1）。

        真彩色 / 256 色终端下行号前景用 blend_rgb 后的 (85,87,90)；
        16 色（standard）终端下行号退化为 dim 样式（无前景色），
        只剩背景色 + dim 属性——所以分两种情况断言。
        """
        assert f"{self._line_num_sgr(num)}\x1b[0m" in out

    def _line_num_sgr(self, num: str) -> str:
        """当前 color_system 下"行号 + 空格"的 ANSI 序列（到 ``m`` 为止）。

        调用方可在末尾拼接 ``\\x1b[0m`` 复位、再拼下一段 SGR。
        """
        from tests._helpers import _current_color_system
        from rich.color import ColorSystem
        if _current_color_system() == ColorSystem.STANDARD:
            # 标准 16 色下行号前景不可用，rich 退化为 dim + 背景
            return f"\x1b[2;40m{num} "
        return f"{ansi_fg_bg(_LINE_NUM_RGB, _CODE_BG_RGB)}{num} "

    def test_tool_call_bash_specialized(self):
        """default bash 工具调用特化：YAML 去掉 command，bash 语法带行号展示。"""
        import json
        args = json.dumps({"command": "ls -la"})
        out = self._capture_tool_call(_make_tool_call(call_id="b1", name="bash", args=args))
        # YAML 中不再展示 command 键
        assert "command" not in _strip_ansi(out)
        # 命令文本以 bash 语法带行号出现
        plain = _strip_ansi(out)
        assert "ls -la" in plain
        self._assert_line_number_for(out)
        # 无代码围栏
        assert "```" not in out

    def test_tool_call_bash_title_adjacent_command(self):
        """default bash 工具调用：标题后紧跟命令代码块（先 1 行留白再命令）。"""
        import json
        args = json.dumps({"command": "ls -la"})
        out = self._capture_tool_call(_make_tool_call(call_id="b1", name="bash", args=args))
        lines = out.split("\n")
        # 标题行后的下一行即 bash 命令代码块的上方留白（纯背景空行，无行号）
        assert lines[0] == "\x1B[1;34m🔧 调用工具 - bash\x1B[0m"
        assert "ls -la" in _strip_ansi(out)

    def test_tool_call_bash_padding_no_lineno(self):
        """default bash 命令：代码块上下各 1 行留白，留白行无行号。"""
        import json
        args = json.dumps({"command": "echo a\necho b"})
        out = self._capture_tool_call(_make_tool_call(call_id="b2", name="bash", args=args))
        plain = _strip_ansi(out)
        lines = plain.split("\n")
        # 结构：0 标题 / 1 上留白 / 2-3 正文（行号）/ 4 下留白
        assert lines[0].startswith("🔧 调用工具 - bash")
        assert lines[1].strip() == ""
        assert "1 echo a" in lines[2]
        assert "2 echo b" in lines[3]
        assert lines[4].strip() == ""
        # 留白行不含行号数字前缀
        assert not re.match(r"^\s*\d", lines[1])
        assert not re.match(r"^\s*\d", lines[4])

    def test_tool_call_yaml_no_trailing_blank(self):
        """default 特化工具 YAML 参数块：YAML 上下带背景留白，区块间普通空行分隔。"""
        import json
        # write 结构：标题 / YAML 上留白 / YAML 行 / YAML 下留白 /
        #            区块间普通空行 / 内容块顶部留白 / 内容行 / 内容块底部留白。
        args = json.dumps({"file_path": "a.py", "content": "x"})
        out = self._capture_tool_call(_make_tool_call(call_id="w3", name="write", args=args))
        lines = out.split("\n")
        # 定位 YAML 行（file_path 键）
        idx = next(i for i, l in enumerate(lines) if "file_path" in _strip_ansi(l))
        # YAML 行之后紧跟 YAML 底部带背景留白，再隔 1 个普通空行（区块间分隔）
        assert _strip_ansi(lines[idx + 1]).strip() == ""  # YAML 底部留白
        assert _strip_ansi(lines[idx + 2]).strip() == ""  # 区块间普通空行
        # 然后才是内容块顶部留白（带背景）+ 内容行
        assert "x" in _strip_ansi(lines[idx + 4])

    def test_tool_call_bash_multiline_line_numbers(self):
        """default bash 工具调用：多行命令带连续行号。"""
        import json
        args = json.dumps({"command": "echo a\necho b"})
        out = self._capture_tool_call(_make_tool_call(call_id="b2", name="bash", args=args))
        plain = _strip_ansi(out)
        # 两行命令均出现，且行号 1 与 2 均有
        assert "echo a" in plain and "echo b" in plain
        # 行号 2：行号前景 + 代码块背景（STANDARD 下退化为 dim 样式）
        assert f"{self._line_num_sgr('2')}\x1b[0m" in out
        assert "command" not in plain

    def test_tool_call_write_specialized(self):
        """default write 工具调用特化：YAML 只留 file_path，content 带行号展示。"""
        import json
        args = json.dumps({"file_path": "src/a.py", "content": "import json\nimport os\n"})
        out = self._capture_tool_call(_make_tool_call(call_id="w1", name="write", args=args))
        plain = _strip_ansi(out)
        # YAML 中保留 file_path、去掉 content 键
        assert "file_path: src/a.py" in plain
        assert "content" not in plain
        # 新代码块中展示 content（Python 关键字 import 带语法色 + 行号）
        assert "import json" in plain and "import os" in plain
        # 带语法着色（关键词 token 前景 + 代码块背景）且带行号 1
        assert f"{ansi_fg_bg(_NORD_KEYWORD_RGB, _CODE_BG_RGB, bold=True)}import\x1b[0m" in out
        self._assert_line_number_for(out)

    def test_tool_call_write_no_file_path(self):
        """default write 缺 file_path：YAML 为空，仍带行号展示 content。"""
        import json
        args = json.dumps({"content": "print('hi')\n"})
        out = self._capture_tool_call(_make_tool_call(call_id="w2", name="write", args=args))
        plain = _strip_ansi(out)
        assert "print('hi')" in plain
        # 无 file_path 时 YAML 块为空：内容前应是空行（标题后直接空行）
        assert "\x1B[1;34m🔧 调用工具 - write\x1B[0m\n" in out

    def test_tool_call_patch_specialized(self):
        """default patch 工具调用特化：YAML 去掉 diff，diff 语法不带行号展示。"""
        import json
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        args = json.dumps({"dir_path": ".", "diff": diff})
        out = self._capture_tool_call(_make_tool_call(call_id="p1", name="patch", args=args))
        plain = _strip_ansi(out)
        # YAML 保留 dir_path、去掉 diff 键
        assert "dir_path: ." in plain
        assert "diff" not in plain
        # diff 文本整体出现
        assert "--- a/x.py" in plain and "+++ b/x.py" in plain
        assert "-old" in plain and "+new" in plain
        # 不带行号：无 rich 行号前景（行号前景 + 代码块背景组合）
        assert f"{ansi_fg_bg(_LINE_NUM_RGB, _CODE_BG_RGB)}1 " not in out

    def test_tool_call_edit_specialized(self):
        """default edit 工具调用特化：YAML 去掉 old_text/new_text，diff 无行号展示。"""
        import json
        args = json.dumps({
            "file_path": "x.py",
            "old_text": "foo",
            "new_text": "bar\nbaz",
            "replace_all": True,
        })
        out = self._capture_tool_call(_make_tool_call(call_id="e1", name="edit", args=args))
        plain = _strip_ansi(out)
        # YAML 保留 file_path 与 replace_all，去掉 old_text/new_text
        assert "file_path: x.py" in plain
        assert "replace_all: true" in plain
        assert "old_text" not in plain and "new_text" not in plain
        # 二者的 unified diff 以 diff 语法整体出现；开头文件头两行已去掉
        assert "--- x.py" not in plain and "+++ x.py" not in plain
        assert "@@ -1 +1,2 @@" in plain or "@@ ... @@" in plain
        assert "-foo" in plain and "+bar" in plain and "+baz" in plain
        # 不带行号
        assert f"{ansi_fg_bg(_LINE_NUM_RGB, _CODE_BG_RGB)}1 " not in out

    def test_tool_call_edit_identical_no_diff(self):
        """default edit 相同内容：无 diff 生成，YAML 后不再输出代码块。"""
        import json
        args = json.dumps({
            "file_path": "x.py",
            "old_text": "same",
            "new_text": "same",
        })
        out = self._capture_tool_call(_make_tool_call(call_id="e2", name="edit", args=args))
        plain = _strip_ansi(out)
        assert "file_path: x.py" in plain
        assert "old_text" not in plain and "new_text" not in plain
        # 无 diff 行
        assert "+++ " not in plain and "--- " not in plain

    def test_tool_call_edit_file_level_diff_real_lineno(self, tmp_path, monkeypatch):
        """default edit 文件存在：整文件 diff，行号为原始文件真实行号。"""
        import json
        monkeypatch.chdir(tmp_path)
        p = tmp_path / "m.py"
        p.write_text(
            '"""doc"""\n'
            'def hello():\n'
            '    greeting = "hello"\n'
            '    return greeting\n'
            '\n'
            '\n'
            'def world():\n'
            '    return "world"\n',
            encoding="utf-8",
        )
        old_text = 'def world():\n    return "world"\n'
        new_text = 'def world():\n    return "bob"\n'
        args = json.dumps({"file_path": "m.py", "old_text": old_text, "new_text": new_text})
        out = self._capture_tool_call(_make_tool_call(call_id="ef", name="edit", args=args))
        plain = _strip_ansi(out)
        # hunk 行号从文件真实行号算起（起始 > 1，而非片段从 1 算起）
        m = re.search(r"@@ -(\d+),\d+ \+(\d+),\d+ @@", plain)
        assert m, f"应有 hunk 头, got: {plain!r}"
        assert int(m.group(1)) > 1 and int(m.group(2)) > 1, \
            f"行号应从原始文件行号算起（而非从 1 的片段行号）, got {m.group(1)}/{m.group(2)}"
        # 邻近行参照（def world 在 hunk 中；不强制包含更远的 hello）
        assert "def world():" in plain
        # 开头文件头两行已去掉
        assert "--- m.py" not in plain and "+++ m.py" not in plain
        # 改动行
        assert '-    return "world"' in plain
        assert '+    return "bob"' in plain

    def test_tool_call_edit_fallback_fragment_diff(self, monkeypatch):
        """default edit 文件不可用（越界/不存在）时回退片段级 diff（行号从 1 算起）。"""
        import json
        args = json.dumps({
            "file_path": "/no/such/dir/x.py",
            "old_text": "foo",
            "new_text": "bar",
        })
        out = self._capture_tool_call(_make_tool_call(call_id="ef2", name="edit", args=args))
        plain = _strip_ansi(out)
        # 片段级 diff 行号从 1 开始（@@ -1 +1 @@）
        assert "@@ -1 +1 @@" in plain
        assert "-foo" in plain and "+bar" in plain

    def test_tool_call_edit_old_text_absent_fallbacks(self, tmp_path, monkeypatch):
        """default edit old_text 不在文件中：回退片段级 diff（无法做整文件替换）。"""
        import json
        monkeypatch.chdir(tmp_path)
        p = tmp_path / "m.py"
        p.write_text("a = 1\nb = 2\n", encoding="utf-8")
        args = json.dumps({
            "file_path": "m.py",
            "old_text": "不存在的文本",
            "new_text": "x",
        })
        out = self._capture_tool_call(_make_tool_call(call_id="ef3", name="edit", args=args))
        plain = _strip_ansi(out)
        assert "@@ -1 +1 @@" in plain
        # 不展示文件里没有的内容
        assert "a = 1" not in plain

    def test_replay_edit_diff_redacts_hunk_lineno(self, tmp_path, monkeypatch):
        """replay 时 edit 不读文件：片段级 diff，@@ 行号替换为 @@ ... @@。"""
        import json
        # 文件存在但内容不同：replay 不应读它（也不该展示它的行号）
        monkeypatch.chdir(tmp_path)
        (tmp_path / "m.py").write_text(
            '"""不同内容"""\ndef x():\n    return "x"\n', encoding="utf-8",
        )
        old_text = 'def world():\n    return "world"\n'
        new_text = 'def world():\n    return "bob"\n'
        args = json.dumps({"file_path": "m.py",
                           "old_text": old_text, "new_text": new_text})
        tc = _make_tool_call(call_id="re1", name="edit", args=args)
        out = self._capture(lambda: renderer.render_replay(
            ToolCallEvent(model="m", tool_call=tc)))
        plain = _strip_ansi(out)
        # 片段级 diff：@@ 行号被替换为 @@ ... @@（无具体行号）
        assert "@@ ... @@" in plain
        assert "@@ -1 +1 @@" not in plain
        # 不读文件：不出现文件实际内容里的行
        assert "不同内容" not in plain
        # 开头文件头两行已去掉
        assert "--- m.py" not in plain and "+++ m.py" not in plain
        # 改动行保留
        assert '-    return "world"' in plain
        assert '+    return "bob"' in plain

    def test_tool_call_bash_empty_command(self):
        """default bash 空 command：仅标题（无 YAML 无代码块）。"""
        import json
        args = json.dumps({"command": ""})
        out = self._capture_tool_call(_make_tool_call(call_id="b3", name="bash", args=args))
        plain = _strip_ansi(out)
        assert plain.strip() == "🔧 调用工具 - bash"

    def test_tool_call_specialized_invalid_json_fallback_plain(self):
        """default 特化工具参数无法解析成 JSON：回退原样展示原始参数字符串。"""
        for name in ("bash", "write", "patch", "edit"):
            out = self._capture_tool_call(
                _make_tool_call(call_id=f"bad_{name}", name=name, args="{not json"))
            plain = _strip_ansi(out)
            # 标靶标题 + 原始参数字符串原样出现
            assert f"🔧 调用工具 - {name}" in plain
            assert "{not json" in plain

    def test_tool_call_specialized_non_dict_json_fallback_plain(self):
        """default 特化工具参数为合法 JSON 但顶层非 dict：也回退旧版输出。"""
        out = self._capture_tool_call(
            _make_tool_call(call_id="arr", name="bash", args="[1, 2, 3]"))
        plain = _strip_ansi(out)
        assert "🔧 调用工具 - bash" in plain
        assert "[1, 2, 3]" in plain

    def test_tool_call_specialized_partial_dict_still_specialized(self):
        """default 特化工具参数为 dict 但缺字段：仍走特化（不回退旧版）。"""
        import json
        # bash 缺 command 键：不展示原始 JSON，仅标题
        out = self._capture_tool_call(
            _make_tool_call(call_id="nk", name="bash", args=json.dumps({"nokey": 1})))
        plain = _strip_ansi(out)
        assert "🔧 调用工具 - bash" in plain
        assert "nokey" not in plain

    def test_classic_tool_call_bash_unchanged(self, monkeypatch):
        """classic 风格 bash 工具调用：保持 ```yaml 参数围栏（特化不变）。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        args = '{"command": "ls -la"}'
        out = self._capture_tool_call(_make_tool_call(call_id="b4", name="bash", args=args))
        # classic 保持完整 YAML 参数块 + 代码围栏
        assert "\n```yaml\ncommand: ls -la\n```\n" in out
        assert "🔧" not in out



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


class TestRenderRetryHint:
    """render_retry_hint：default 用 <kbd>/内联代码渲染、classic 纯文本。"""

    def _strip_ansi(self, text: str) -> str:
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def _capture(self, fn):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_default_uses_kbd_and_inline_code(self, monkeypatch):
        """default：Ctrl-T 用 <kbd> 标签、/retry 用 markdown 内联代码渲染。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        out = self._capture(lambda: _get_renderer().render_retry_hint())
        # <kbd> 标签被 rich 识别为 Markdown 斜体样式（呈黄色高亮等）
        assert "Ctrl-T" in out
        assert "/retry" in out
        # 无字面 <kbd> 标签残留（rich 已消费标签语义）
        assert "<kbd>" not in out
        # 无字面反引号残留（内联代码被消费）
        assert "`" not in out
        # 结尾是空行（print 自带回车）
        assert out.endswith("\n\n")

    def test_classic_plain_text(self, monkeypatch):
        """classic：纯文本提示，无 ANSI 且无 <kbd> 标签。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        out = self._capture(lambda: _get_renderer().render_retry_hint())
        assert "Ctrl-T" in out and "/retry" in out
        assert "<kbd>" not in out
        assert self._strip_ansi(out) == out  # 无 ANSI 控制码
        assert out.endswith("\n\n")


class TestDefaultAssistantMarkdown:
    """default 风格下 assistant 正文用 rich Markdown 渲染。

    通过 ``_set_color_system`` autouse fixture，每个测试在真彩色 / 256 色 /
    16 色三种终端能力下各验证一次，覆盖各种 color_system 下的渲染输出。
    """

    @pytest.fixture(autouse=True)
    def _auto_color_system(self, _set_color_system):
        """在三种 color_system 下自动覆盖当前测试。"""
        return _set_color_system

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
        """代码块：subclass 覆写用背景色 rgb(30,30,30)，非 rich 默认。"""
        out = self._capture("```python\nimport os\n```")
        # 代码块背景色与工具输出一致
        assert ansi_bg(_CODE_BG_RGB) in out
        # 不是 rich 默认 code_block 样式（cyan on black = 36;40m）
        assert "\x1B[36;40m" not in out
        # 内容保留
        assert "import os" in _strip_ansi(out)
        # 无 ``` 围栏
        assert "```" not in out

    def test_code_fence_top_bottom_blank(self):
        """assistant 代码块：上下各留 1 行纯背景空行（与 .md 风格统一）。"""
        out = self._capture("```python\nimport os\n```")
        full_lines = [l.strip() for l in _strip_ansi(out).split("\n")]
        code_idx = full_lines.index("import os")
        # import os 前一行与后一行都是空白（上下留白）
        assert full_lines[code_idx - 1] == ""
        assert full_lines[code_idx + 1] == ""

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
        assert _syntax_token() not in out

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

    def test_markdown_ansi_returns_ansi_string(self):
        """_markdown_ansi 返回带 ANSI 的渲染字符串（供布局复用）。"""
        from mycode.renderer import _markdown_ansi
        out = _markdown_ansi("**加粗**")
        assert isinstance(out, str)
        assert "\x1B[1m加粗\x1B[0m" in out
        # 带尾部换行（print 语义）
        assert out.endswith("\n")

    def test_markdown_ansi_preserves_ansi_input(self):
        """_markdown_ansi 对含 ANSI 控制码的输入原样返回（豁免 markdown 解析）。"""
        from mycode.renderer import _markdown_ansi
        out = _markdown_ansi("\x1B[32mgreen\x1B[0m msg")
        assert "\x1B[32mgreen\x1B[0m msg" in out

    def test_markdown_ansi_multiline_collapsed(self):
        """_markdown_ansi 对普通裸换行按 markdown 合并（hard break 才能真正分行）。

        这是 ask_ui 描述需要先转 hard break 的原因；本用例锁定该行为以防
        回归（若 rich 未来改变合并策略，ask_ui 侧的 soft-break 处理也要升级）。
        """
        from mycode.renderer import _markdown_ansi
        import re
        _strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
        out = _strip(_markdown_ansi("第一行\n第二行"))
        for line in out.rstrip("\n").split("\n"):
            assert line.strip() == "第一行 第二行"


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
        """default：首行竖线（提示符色）+ 灰色背景填充 + 上下各留 1 行背景空行。"""
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
        # 上下各 1 行同背景色空行（无内容）；底部背景行后还有一个空行分隔
        assert out.startswith("\x1B[48;2;51;51;51m          \x1B[0m\n")
        assert out.endswith("\x1B[48;2;51;51;51m          \x1B[0m\n\n")

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


class TestRenderResumeHint:
    """退出时的「继续本次会话」恢复命令渲染。

    default 风格把命令放进反引号包裹的 markdown 内联代码里，经 rich 渲染；
    classic 风格保持纯文本原样输出，不带 markdown 格式。
    """

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_classic_plain_and_no_backtick(self, monkeypatch):
        """classic：原始命令文本，无反引号/markdown 格式。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        cmd = "myc -r 1234-abcd"
        out = self._capture(lambda: _get_renderer().render_resume_hint(cmd))
        assert "可通过以下命令继续本次会话：" in out
        assert cmd in out
        assert "`" not in out
        assert "\x1b[" not in out  # 无 ANSI 着色

    def test_classic_trailing_single_newline(self, monkeypatch):
        """classic：尾部无多余空行（提示语后只有命令一行）。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "classic")
        out = self._capture(lambda: _get_renderer().render_resume_hint("myc -r x"))
        assert out.endswith("myc -r x\n")
        assert "\n\n" not in out

    def test_default_backtick_and_ansi_present(self, monkeypatch):
        """default：命令被反引号包裹，经 rich 渲染出内联代码（带 ANSI 着色）。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        cmd = "myc -r 1234-abcd"
        out = self._capture(lambda: _get_renderer().render_resume_hint(cmd))
        assert "可通过以下命令继续本次会话：" in out
        # rich 内联代码渲染：ANSI 着色序列存在
        assert "\x1b[" in out
        # 反引号不残留（rich 解析后是着色文本而非字面反引号）
        assert "`" not in out
        # 命令内容出现在渲染结果里（剥离 ANSI 后）
        assert cmd in _strip_ansi(out)

    def test_default_trailing_single_newline(self, monkeypatch):
        """default：尾部无多余空行（恢复命令后只有一个换行收尾）。"""
        monkeypatch.setattr(renderer, "RENDER_STYLE", "default")
        out = self._capture(lambda: _get_renderer().render_resume_hint("myc -r x"))
        assert not out.endswith("\n\n")
        assert "\n\n" not in out

    def test_heading_shared_across_styles(self, monkeypatch):
        """提示语「可通过以下命令继续本次会话：」两风格一致（基类公共流程）。"""
        for style in ("classic", "default"):
            monkeypatch.setattr(renderer, "RENDER_STYLE", style)
            out = self._capture(lambda: _get_renderer().render_resume_hint("myc -r x"))
            assert "可通过以下命令继续本次会话：" in out
