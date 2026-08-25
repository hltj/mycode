"""
测试共享辅助：fixture 与构造辅助函数。

由 test_cli.py / test_renderer.py 等测试模块复用，避免重复定义。
（fake_env 属 autouse fixture，放在 conftest.py 中。）
"""

from __future__ import annotations

from typing import Sequence

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
)
from openai.types.chat.chat_completion_message_function_tool_call_param import Function


def make_tool_call(call_id: str = "call_1", name: str = "boom", args: str = "{}"):
    return ChatCompletionMessageFunctionToolCallParam(
        id=call_id,
        type="function",
        function=Function(name=name, arguments=args),
    )


def make_assistant_with_tool_calls(*tcs):
    return ChatCompletionAssistantMessageParam(
        role="assistant",
        content="",
        tool_calls=list(tcs),
    )


# ---------------------------------------------------------------------------
# 颜色相关辅助：根据当前 Console 的 color_system 动态生成 ANSI 转义码
#
# 渲染器（renderer.py）经 rich 输出 ANSI 转义码时，rich 会按 Console.color_system
# 自动选择输出格式：
#   - ColorSystem.TRUECOLOR（真彩色，24-bit）：\x1b[38;2;R;G;Bm
#   - ColorSystem.EIGHT_BIT（256 色）          ：\x1b[38;5;Nm
#   - ColorSystem.STANDARD（16 色）            ：\x1b[31m 等基础色
#
# 测试不应硬编码某种格式的颜色值——同一 RGB 三元组在 256 色与真彩色下分别输出
# "38;5;N" 与 "38;2;R;G;B"，硬编码其一会在另一种终端下全部失败。
# 以下 helper 把 RGB 三元组按当前 Console 的 color_system 转成对应格式的 ANSI
# 转义序列（含前导 \x1b[ 与结尾 m），供测试断言使用。
# ---------------------------------------------------------------------------


def _current_color_system():
    """当前 Console 的 color_system（ColorSystem 枚举值）。"""
    from rich.console import Console
    return Console(force_terminal=True)._color_system


def ansi_fg(rgb: Sequence[int], *, bold: bool = False) -> str:
    """按当前 color_system 计算前景色 ANSI 转义序列（含 ``\\x1b[`` 与 ``m``）。

    ``rgb`` 为 3 个 0-255 整数组成的 RGB 三元组；``bold=True`` 额外加粗属性。
    """
    from rich.style import Style

    s = Style(color=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})", bold=bold or None)
    codes = s._make_ansi_codes(_current_color_system())
    return f"\x1b[{codes}m" if codes else ""


def ansi_bg(rgb: Sequence[int]) -> str:
    """按当前 color_system 计算背景色 ANSI 转义序列（含 ``\\x1b[`` 与 ``m``）。"""
    from rich.style import Style

    s = Style(bgcolor=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})")
    codes = s._make_ansi_codes(_current_color_system())
    return f"\x1b[{codes}m" if codes else ""


def ansi_fg_bg(
    rgb_fg: Sequence[int] | None = None,
    rgb_bg: Sequence[int] | None = None,
    *,
    bold: bool = False,
) -> str:
    """按当前 color_system 计算「前景+背景+加粗」合并的 ANSI 转义序列。"""
    from rich.style import Style

    s = Style(
        color=f"rgb({rgb_fg[0]},{rgb_fg[1]},{rgb_fg[2]})" if rgb_fg else None,
        bgcolor=f"rgb({rgb_bg[0]},{rgb_bg[1]},{rgb_bg[2]})" if rgb_bg else None,
        bold=bold or None,
    )
    codes = s._make_ansi_codes(_current_color_system())
    return f"\x1b[{codes}m" if codes else ""


def _fg_sgr(rgb: Sequence[int], *, bold: bool = False) -> str:
    """前景色的 SGR 码（不含 ``\\x1b[`` 与 ``m``），便于拼接复合样式。"""
    from rich.style import Style

    s = Style(color=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})", bold=bold or None)
    return s._make_ansi_codes(_current_color_system())


def _bg_sgr(rgb: Sequence[int]) -> str:
    """背景色的 SGR 码（不含 ``\\x1b[`` 与 ``m``），便于拼接复合样式。"""
    from rich.style import Style

    s = Style(bgcolor=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})")
    return s._make_ansi_codes(_current_color_system())
