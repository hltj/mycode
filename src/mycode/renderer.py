#!/usr/bin/env python3
"""
渲染器（处理器）模块：按渲染风格（default / classic）定义所有消息与
事件的终端展示方式。公共渲染流程在 _Renderer 基类复用，风格差异由
_DefaultRenderer / _ClassicRenderer 子类覆写。default 风格下 assistant
正文用 rich Markdown 渲染，代码块用 rich 语法高亮；classic 风格保持
纯代码围栏与原样输出。
"""

import difflib
import io
import json
import os
import re
from typing import Any, NoReturn, cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import to_container
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import CodeBlock, Markdown
from rich.syntax import Syntax

from mycode.session import (
    SessionRecord,
    UserMessage,
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
    InterruptEvent,
    ExceptionEvent,
    ModeChangeEvent,
    NoticeEvent,
    AgentMessage,
    ExceptionData,
    NoticeData,
    ToolResultData,
)
from mycode.mode import Mode, MODE_STATE, MODE_COLOR


def assert_never(arg: NoReturn) -> NoReturn:
    raise AssertionError(f"未处理的消息类型: {type(arg).__name__}")


# ---------------------------------------------------------------------------
# 待办事项渲染辅助
# ---------------------------------------------------------------------------
# ANSI 转义常量
_RESET = "\x1B[0m"
_GRAY = "\x1B[90m"          # bright black = 深灰
_STRIKE = "\x1B[9m"         # 删除线
_BOLD_WHITE = "\x1B[1;37m"  # 粗+白（在多数终端等价于亮白，对中文可见度比纯粗体好）
_GREEN = "\x1B[32m"                  # 绿（classic 待办完成标记）
_ORANGE = "\x1B[38;2;255;165;0m"     # 橙（classic 待办进行中标记）
_INPUT_BG = "\x1B[48;2;51;51;51m"    # 灰背景（default 用户输入区）
# 仅复位前景色与粗体（39=默认前景，22=正常字重），保留背景色
_FG_DEFAULT = "\x1B[39;22m"

# 渲染风格：default / classic（main() 中根据命令行参数设置）
RENDER_STYLE = "default"

# 模式 → 提示符样式类（default / classic 共用）
_MODE_PROMPT_STYLES: dict[Mode, str] = {
    Mode.AUTO: 'mycode-prompt',
    Mode.ASK: 'mycode-prompt-ask',
    Mode.YOLO: 'mycode-prompt-yolo',
}

# 输入区占位文字（多行输入框为空时显示）：Enter 换行，Alt-Enter（ESC 再按
# Enter）发送。开头留 1 个空格避免与行首光标符号重合。用灰色斜体渲染，
# 区别于输入文字（见 create_prompt_style）。
_INPUT_PLACEHOLDER = " ↵ 换行，Alt-↵（ESC ↵）发送"

# default 风格：模式 → 提示符核心（竖线 + 标记，不含尾随空格）
_DEFAULT_PROMPT_PREFIXES: dict[Mode, str] = {
    Mode.AUTO: "│",
    Mode.ASK: "│?",
    Mode.YOLO: "│!",
}

# 状态符号（default 风格）：emoji 自带颜色，不依赖 ANSI 上色
# （避免彩色 emoji 字体忽略 ANSI）。
_TODO_SYMBOLS: dict[str, str] = {
    "pending": "🔳",      # 白色方块
    "in_progress": "🟧",   # 橙色方块
    "completed": "✅️",     # 绿色对勾
}

# 状态符号（classic 风格）：复选框形式
_TODO_SYMBOLS_CLASSIC: dict[str, str] = {
    "pending": "- [ ]:",
    "in_progress": f"- [{_ORANGE}>{_RESET}]:",
    "completed": f"- [{_GREEN}x{_RESET}]:",
}

# 待办事项渲染模板：``{sym}`` 替换符号、``{title}`` 替换文本。
# emoji 后面保留 1 个 ASCII 空格作为分隔（emoji 自带的视觉宽度不算）。
# 所有风格共用同一套模板，仅替换符号。
_TODO_FORMATS: dict[str, str] = {
    # 已完成：标题灰色 + 删除线
    "completed": f"{{sym}} {_GRAY}{_STRIKE}{{title}}{_RESET}",
    # 进行中：标题粗+白（视觉强调）
    "in_progress": f"{{sym}} {_BOLD_WHITE}{{title}}{_RESET}",
    # 未开始：全部普通样式
    "pending": "{sym} {title}",
}


def _code_fence(text: str) -> str:
    """根据内容中最长连续反引号长度选择围栏定界符。

    若内容中出现 3 重反引号，则定界符需用 4 重，以此类推，避免
    内容中的反引号提前终止代码块。最短为 3 重。
    """
    longest = 0
    cur = 0
    for ch in text:
        if ch == '`':
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 0
    return '`' * max(3, longest + 1)


def _yaml_dump(parsed: dict) -> str:
    """把工具调用参数 dict 转为 YAML 文本（含换行字符串用 block literal）。

    含换行的字符串用 block literal（``|-``）输出，避免换行折叠翻倍。
    去掉 ``yaml.dump`` 末尾的换行，让 rich 语法高亮不会多渲染一整个
    背景空行（YAML 块末尾不再出现多余空行）。
    """
    import yaml

    def _block_str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
        """含换行的字符串用 block literal（`|-`）输出，避免换行折叠翻倍。"""
        style = '|' if '\n' in data else None
        return dumper.represent_scalar(
            'tag:yaml.org,2002:str', data, style=style)

    class _BlockStrDumper(yaml.SafeDumper):
        pass
    _BlockStrDumper.add_representer(str, _block_str)
    return yaml.dump(parsed, Dumper=_BlockStrDumper,
                     allow_unicode=True, sort_keys=False,
                     default_flow_style=False,
                     width=64 * 1024).rstrip(chr(0x0A))


def _old_new_diff(old: str, new: str, filename: str) -> str:
    """生成 old 与 new 之间的 unified diff 文本（不带行号；相同内容返回空）。

    供 edit 工具调用的渲染回退使用：去掉参数展示后，用 diff 语法高亮
    展示 new_text 相对 old_text 的改动。相同时返回空字符串（无改动）。

    注意：这是**片段级** diff —— 行号从片段首行（1）算起，上下文只来自
    片段自身，因此行号不是原始文件行号。能读取到文件时优先用
    :func:`_edit_file_diff` 做整文件级 diff。
    """
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=filename, tofile=filename, lineterm="",
        )
    ).rstrip(chr(0x0A))


def _redact_hunk_headers(diff_text: str) -> str:
    """把 unified diff 的 hunk 头 ``@@ -行号 +行号 @@`` 替换为 ``@@ ... @@``。

    replay 时片段级 diff 的行号从 1 算起、无真实文件语义，保留具体行号
    反而误导；用 ``...`` 省略行号占位，保留 ``@@`` 分隔符与 ``-``/``+``
    改动行。
    """
    return re.sub(
        r"^@@ .*? @@$", "@@ ... @@", diff_text, flags=re.MULTILINE,
    ).rstrip(chr(0x0A))


def _strip_diff_file_headers(diff_text: str) -> str:
    """去掉 unified diff 开头的 ``---`` 与 ``+++`` 文件头两行。

    edit 工具渲染的 diff 中 ``--- fromfile`` / ``+++ tofile`` 与 YAML 里
    已有的 file_path 重复且占视觉空间，实时与重放都去掉。若文本不以
    ``---`` 开头则原样返回。
    """
    lines = diff_text.split("\n")
    if lines and lines[0].startswith("--- ") and len(lines) > 1 \
            and lines[1].startswith("+++ "):
        return "\n".join(lines[2:]).rstrip(chr(0x0A))
    return diff_text.rstrip(chr(0x0A))


def _edit_file_diff(file_path: str, old_text: str, new_text: str,
                    replace_all: bool) -> str | None:
    """基于文件真实内容生成 edit 替换的 unified diff（与工具语义一致）。

    工具调用渲染时文件尚未被修改，读到的就是「替换前」的完整内容；对
    「替换前 vs 替换后」做整文件 diff，因此行号是**原始文件行号**
    （unified_diff 从文件首行起算）；上下文沿用 unified_diff 默认 3 行。

    替换逻辑与 edit 工具一致：首个匹配或全部匹配（``replace_all``）。
    以下情况无法构建而返回 ``None``（由调用方回退到片段级
    :func:`_old_new_diff`）：

    - 路径为空 / 越界 / 命中保护正则（``safe_path`` 拒绝）；
    - 文件不存在或读取失败；
    - ``old_text`` 为空，或未在文件中出现（可能已应用过）；
    - 非 ``replace_all`` 但匹配多处（工具本会拒绝执行）。
    """
    if not old_text:
        return None
    try:
        from mycode.tools._safe_path import safe_path
        abs_path = safe_path(file_path).abs
    except ValueError:
        return None
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    count = content.count(old_text)
    if count == 0:
        return None
    if count > 1 and not replace_all:
        return None
    new_content = content.replace(old_text, new_text, count if replace_all else 1)
    old_lines, new_lines = content.splitlines(), new_content.splitlines()
    return "\n".join(
        difflib.unified_diff(
            old_lines, new_lines,
            fromfile=file_path, tofile=file_path,
            lineterm="",
        )
    ).rstrip(chr(0x0A))


# 工具调用参数缓存：tool_call_id -> args_dict
# render_tool_call 写入，render_tool_result 按 id 取回（用于 read 文件名
# 推断语法），取用后清掉避免内存无限增长。
_TOOL_CALL_INFO: dict[str, dict] = {}


def _tool_call_args(tool_call_id: str) -> dict:
    """取回指定 tool_call_id 的调用参数；缺失时返回空 dict。"""
    return _TOOL_CALL_INFO.pop(tool_call_id, {})


# 默认风格带行号语法高亮渲染：与输入区一致的深灰背景区分代码块区域
_CODE_BG_RGB = "rgb(30,30,30)"
# 语法高亮主题，可用环境变量 MYCODE_SYNTAX_THEME 覆盖（如 nord / gruvbox-dark / zenburn）
_CODE_THEME = os.getenv("MYCODE_SYNTAX_THEME", "nord")

# ANSI 控制码（CSI / OSC）检测：工具输出若自带颜色控制码，不再语法高亮，
# 否则会对控制码序列再次上色、导致转义码泄漏到终端。
_ANSI_MARK_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-~]")

# read 渲染相关常量
_HIGHLIGHT_MUTED = "\x1B[38;5;110m"   # 蓝灰（read 末尾截断/剩余提示行，default 风格；与背景区分、柔和不刺眼）
_CODE_BG = "\x1B[48;5;234m"     # 深灰背景（default 代码块画布，对应 rich 的 rgb(30,30,30) → 256 色 234）
_READ_LINE_RE = re.compile(r"^\s*\d+\t(.*)$", re.MULTILINE)
_READ_LINENO_RE = re.compile(r"^\s*(\d+)\t", re.MULTILINE)
_TRUNCATE_MARK_RE = re.compile(r"^\.\.\.\s")


def _has_ansi_control(text: str) -> bool:
    """判断文本是否包含 ANSI 控制码。有则不应进行语法高亮。"""
    return bool(_ANSI_MARK_RE.search(text))


def _is_truncate_marker(line: str) -> bool:
    """判断行是否为截断/剩余提示行（以 ``...`` 开头）。"""
    return bool(_TRUNCATE_MARK_RE.match(line))


def _split_read_output(text: str) -> tuple[list[str], list[str]]:
    """把 read 工具输出拆为「普通带行号行」与「末尾提示行」。

    read 工具返回结构：带行号的行 + 可选的截断/剩余提示行。提示行是
    最后一行以 ``...`` 开头的那行（``... 已截断`` / ``... 剩余 N 行``）。

    处理规则：
    1. 若最后一行以 ``...`` 开头，单独拿出（不参与行号剥离）；
    2. 其他带行号的行，去掉行号交给 rich 处理成带行号的；
    3. 若有第 1 步拿出的行，渲染完带行号内容后再补上该行输出。
    """
    lines = str(text).split("\n")
    marker: list[str] = []
    if lines and _is_truncate_marker(lines[-1]):
        marker.append(lines.pop())
    # 去掉行号前缀，内容交给 rich 重新生成连续行号；不是带行号的保留原样
    normal = [m.group(1) if (m := _READ_LINE_RE.match(line)) else line
              for line in lines]
    return normal, marker


def _first_read_lineno(text: str) -> int:
    """解析 read 结果文本的首行行号；解析不到时返回 1。"""
    m = _READ_LINENO_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 1


def _guess_read_language(text: str) -> str | None:
    """根据内容猜测语法高亮语言。

    用 Pygments ``guess_lexer`` 自动检测；猜不到（纯文本 / 失败）时返回
    ``None``（由 ``_syntax_plain`` 统一兜底为 ``markdown``）。
    """
    try:
        from pygments.lexers import guess_lexer  # type: ignore[import-untyped]
    except Exception:
        return None
    try:
        lexer = guess_lexer(text)
    except Exception:
        return None
    if not lexer.aliases:
        return None
    alias = lexer.aliases[0]
    if alias == "text":
        return None
    return alias


def _guess_filename_language(filename: str, text: str) -> str | None:
    """根据文件名 + 内容猜测语法高亮语言（优先文件名）。

    用 Pygments ``guess_lexer_for_filename`` —— 文件扩展名 / 已知文件名
    （如 ``.bashrc``、``Makefile``）直接命中；命中失败再退回到内容
    ``guess_lexer``。拿不到别名或结果为纯文本时返回 ``None``
    （由 ``_syntax_plain`` 统一兜底为 ``markdown``）。
    """
    candidates: list[tuple[str, str]] = [("filename", filename), ("content", text)]
    for kind, source in candidates:
        if not source:
            continue
        try:
            if kind == "filename":
                from pygments.lexers import guess_lexer_for_filename
                lexer = guess_lexer_for_filename(filename, text, stripnl=False)
            else:
                from pygments.lexers import guess_lexer
                lexer = guess_lexer(text)
        except Exception:
            continue
        if not lexer.aliases:
            continue
        alias = lexer.aliases[0]
        if alias != "text":
            return alias
    return None


def _terminal_columns() -> int:
    """当前终端宽度（列数），获取失败时回退到 80。"""
    import shutil
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _syntax_plain(code: str, language: str | None = None,
                  line_numbers: bool = False, start_line: int = 1,
                  top_padding: int = 1, bottom_padding: int = 1) -> str:
    """返回 rich 渲染后的代码文本（含 ANSI 转义）。

    在**终端宽度**下用 rich 渲染：短行背景色铺满整行、超长行自动换行，
    行号连续自然排列。``language`` 为 ``None`` 时使用 ``markdown`` 词法分析
    （识别 markdown 语法如标题/代码块，纯文本行无额外着色）；词法器解析
    失败时同样回退到 ``markdown``。

    代码块上下各留 ``top_padding`` / ``bottom_padding`` 行纯背景留白
    （默认各 1 行），与 markdown 渲染风格中的代码块上下各 1 行留白节奏一致。

    若内容自带 ANSI 控制码（工具输出可能包含颜色转义），不再语法高亮——
    原样返回（补一个尾部换行），避免对控制码再次上色导致转义序列泄漏。
    """
    if _has_ansi_control(code):
        return code + (chr(0x0A) if not code.endswith(chr(0x0A)) else "")
    lexer = language or "markdown"
    padding = (top_padding, 0, bottom_padding, 0)
    try:
        syntax = Syntax(code, lexer, theme=_CODE_THEME, line_numbers=line_numbers,
                        start_line=start_line, word_wrap=True,
                        background_color=_CODE_BG_RGB, padding=padding)
    except Exception:
        syntax = Syntax(code, "markdown", theme=_CODE_THEME, line_numbers=line_numbers,
                        start_line=start_line, word_wrap=True,
                        background_color=_CODE_BG_RGB, padding=padding)
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, width=_terminal_columns()).print(syntax)
    return buf.getvalue()


def _markdown_plain(markup: str) -> None:
    """用 rich Markdown 把 assistant 正文渲染到终端（default 风格）。

    - 段落 / 标题 / 列表 / 表格 / 引用 / 分割线等按 rich 默认样式渲染，
      字体、行数继承默认主题（markdown.code 等内联样式即富文本着色）；
    - 代码块交给覆写版 ``CodeBlock``：与工具输出/read 一致用深灰背景
      ``_CODE_BG_RGB``、``_CODE_THEME``，避免富文本对 ``` 围栏做二次上色；
      代码块上下各留 1 行同背景色留白，与 .md 渲染风格的代码块留白一致；
    - ANSI 控制码豁免：正文自带的 ANSI 转义原样输出，不经过 markdown
      解析（否则转义序列可能在代码块/链接里被再次装箱导致泄漏）；
    - 链接保留 rich 默认行为（``hyperlinks`` 开终端 OSC8 超链接，默认 True）。
    """
    if _has_ansi_control(markup):
        print(markup.rstrip(chr(0x0A)))
        return

    class _CodeBlockBg(CodeBlock):
        """代码块子类：背景色/主题与工具输出一致，替换 rich 默认内联上色。

        代码块上下各绘制 1 行纯背景留白（``top_padding`` / ``bottom_padding``），
        与代码块正文行同背景色、视觉连成一体。
        """

        def __rich_console__(self, console: Console, options: Any) -> Any:
            code = str(self.text).rstrip(chr(0x0A))
            try:
                yield Syntax(code, self.lexer_name, theme=_CODE_THEME,
                             word_wrap=True, padding=(1, 0, 1, 0),
                             background_color=_CODE_BG_RGB)
            except Exception:
                yield Syntax(code, "text", theme=_CODE_THEME,
                             word_wrap=True, padding=(1, 0, 1, 0),
                             background_color=_CODE_BG_RGB)

    # 实例级覆写 elements：仅影响本次打印，不改 rich.markdown 全局映射，
    # 避免污染其他使用方。
    elements = dict(Markdown.elements)
    elements["fence"] = _CodeBlockBg
    elements["code_block"] = _CodeBlockBg

    md = Markdown(markup, code_theme=_CODE_THEME)
    md.elements = elements  # type: ignore[misc]  # 实例级覆写 ClassVar 映射
    console = Console(force_terminal=True, width=_terminal_columns())
    try:
        console.print(md)
    except Exception:
        # 极端输入导致 rich 解析/渲染失败时兜底为纯文本
        print(markup.rstrip(chr(0x0A)))


def _spacer_text(bg: str) -> str:
    """生成 1 行纯背景空行（不含换行），用作方块上下留白。

    ``bg`` 指定背景色：代码块用 ``_CODE_BG``（与 rich 代码块背景
    一致），输入区用 ``_INPUT_BG``。行内容用空格填满整行（仅背景可见）；
    换行由调用方 ``print`` 负责。
    """
    columns = _terminal_columns()
    return bg + " " * columns + _RESET


def _wrap_by_display_width(body: str, columns: int) -> list[str]:
    """按显示宽度分行，返回每段宽度恰为 ``columns`` 的段落列表。

    用于 default 风格用户消息渲染：背景色需铺满每一行，因此不能依赖
    终端自动换行——自动换行后末尾不满整行的短段没有背景色，且宽字符
    在行尾的截断行为各终端不一致。规则：

    - 宽字符（占 2 列）在行尾剩余空间放不下时，当前行以空格补满；
    - 零宽字符（如 emoji 修饰符）跟随前一字符，不计宽度；
    - 每个段落（含最后一段）均用空格补齐到 ``columns`` 列，
      保证背景全量覆盖；
    - 恰好整倍数换行时不会产生多余的全空格行。
    """
    from prompt_toolkit.utils import get_cwidth
    columns = max(columns, 1)
    segments: list[str] = []
    cur = ""
    cur_w = 0
    for ch in body:
        w = get_cwidth(ch)
        if w == 0:
            cur += ch
            continue
        if cur and cur_w + w > columns:
            # 放不下：补满当前行并换行
            segments.append(cur + " " * (columns - cur_w))
            cur, cur_w = "", 0
        cur += ch
        cur_w += w
        if cur_w >= columns:
            segments.append(cur)
            cur, cur_w = "", 0
    # 末尾段补齐；恰好整行结束时 cur 为空且已有段落，不追加空行
    if cur or not segments:
        segments.append(cur + " " * (columns - cur_w))
    return segments


# ===================================================================
# 渲染器（处理器）—— 不同渲染风格各有一个渲染器，公共逻辑在基类复用
# ===================================================================

class _Renderer:
    """渲染器基类：定义某一渲染风格下所有消息/事件的展示方式。

    公共渲染流程（工具调用 YAML 参数、代码围栏、待办列表、异常
    traceback 等）在基类中实现；各风格差异（标题文本、emoji、用户
    消息样式、提示符、输入区背景）由子类覆写。
    """

    # 待办状态符号（子类各自定义）与模板（所有风格共用）
    symbols: dict[str, str]
    formats: dict[str, str] = _TODO_FORMATS
    # 异常 traceback 渲染语言：default 传 "python" 做高亮；classic 传空走无标签围栏
    traceback_language: str = ""

    # ---- 待办事项 ----
    def format_todos(self, state: list[dict] | None = None) -> str:
        from mycode.tools.todo_write import get_todos
        items = state if state is not None else get_todos()
        if not items:
            return "(TODO 列表为空)"
        return "\n".join(
            self.formats[it["status"]].format(
                sym=self.symbols[it["status"]],
                title=it["title"],
            )
            for it in items
        )

    # ---- 标题文本（子类差异点） ----
    def ai_title(self, model: str) -> str:
        raise NotImplementedError

    def tool_call_title(self, func_name: str) -> str:
        raise NotImplementedError

    def tool_result_title(self) -> str:
        raise NotImplementedError

    def notice_text(self, content: str) -> str:
        raise NotImplementedError

    def exception_title(self, exc_type: str, exc_message: str) -> str:
        raise NotImplementedError

    # ---- 代码块渲染 ----
    def render_code_block(self, body: str, language: str | None = None) -> None:
        """渲染一块代码/文本（无行号）。

        基类默认用普通代码围栏（classic 风格沿用）；default 子类覆写为
        rich 语法高亮。``language`` 为语言标签（如工具调用 YAML），
        有标签时跟在围栏同一行（`` ```yaml``），无标签时围栏单独一行。
        """
        fence = _code_fence(body)
        if language:
            print(f"{fence}{language}\n{body.rstrip(chr(0x0A))}\n{fence}")
        else:
            print(f"{fence}\n{body.rstrip(chr(0x0A))}\n{fence}")

    def render_read_output(self, content: str, file_path: str = "") -> None:
        """渲染 read 工具返回（带行号内容）。

        基类默认用代码围栏包装（classic 风格沿用）；default 子类覆写为
        rich 带行号语法高亮。``file_path`` 为调用时的文件路径，可辅助
        推断语法语言。
        """
        fence = _code_fence(content)
        print(f"{fence}\n{content.rstrip(chr(0x0A))}\n{fence}")

    def render_notice_additional(self, additional: str) -> None:
        """渲染提醒附带的额外内容（如代码块）。

        基类默认原样输出（classic 风格沿用）；default 子类覆写为解析
        围栏后做语法高亮。
        """
        print(additional.rstrip(chr(0x0A)))

    # ---- 渲染（公共流程在基类） ----
    def render_assistant(self, message: ChatCompletionAssistantMessageParam, model: str) -> None:
        # AI 回复：标题（风格差异）+ 正文；无正文（纯 tool_calls）时仅标题
        content = message.get("content")
        title = self.ai_title(model)
        if content and str(content).strip():
            print(f"\x1B[35m{title}\x1B[0m")
            self.render_assistant_body(str(content))
            print()
        else:
            print(f"\x1B[35m{title}\x1B[0m")

    def render_assistant_body(self, body: str) -> None:
        """渲染 assistant 正文。

        基类与 classic 风格沿用原样输出；default 子类覆写为 rich
        Markdown 渲染。
        """
        print(body.strip(chr(0x0A)))

    def render_tool_call(self, tool_call: ChatCompletionMessageFunctionToolCallParam,
                         replay: bool = False) -> None:
        func_name = tool_call["function"]["name"]
        args_ = tool_call["function"]["arguments"]
        try:
            parsed = json.loads(args_)
        except (json.JSONDecodeError, TypeError):
            yaml_text = args_
            parsed = None
        else:
            if not isinstance(parsed, dict):
                yaml_text = args_
                parsed = None
            else:
                yaml_text = _yaml_dump(parsed)
        # 记录工具调用参数供结果渲染时推断语言
        call_id = tool_call.get("id", "")
        if call_id and isinstance(parsed, dict):
            _TOOL_CALL_INFO[call_id] = dict(parsed)
        print(f"\x1B[1;34m{self.tool_call_title(func_name)}\x1B[0m")
        self.render_tool_call_params(
            func_name, parsed or {}, yaml_text, call_id=call_id, replay=replay,
        )
        print()

    def render_tool_call_params(
        self,
        func_name: str,
        params: dict,
        yaml_text: str,
        call_id: str = "",
        replay: bool = False,
    ) -> None:
        """渲染工具调用参数。

        基类（classic 风格）沿用统一 YAML 参数块展示（代码围栏）；
        default 子类覆写为对 ``bash`` / ``write`` / ``patch`` / ``edit``
        四个工具做特化：YAML 中去掉正文/命令/diff 等大字段，随后在新
        代码块中按对应语法展示内容（带行号的 ``bash`` / 不带行号的
        ``diff``、按 ``file_path`` 识别语言的 ``write``）。

        ``replay`` 为 True 表示历史重放（文件可能已不存在/已改变），
        edit 场景下不读实际文件。
        """
        self.render_code_block(yaml_text, language="yaml")

    def render_tool_result(self, tool_result: ToolResultData) -> None:
        # todo_write 特化渲染：先输出当前待办列表再输出结果
        if tool_result.get("tool_name") == "todo_write":
            print(f"\x1B[1;36mTODO 列表:\x1B[0m\n{self.format_todos()}\n")
        tool_result_content = tool_result.get("content", "")
        print(f"\x1B[1;34m{self.tool_result_title()}\x1B[0m")
        # 内容自带 ANSI 控制码时不做任何语法高亮/行号重构，原样输出
        # （避免控制码被再次上色泄漏转义序列）。
        if _has_ansi_control(tool_result_content):
            self.render_code_block(tool_result_content)
        elif tool_result.get("tool_name") == "read" and _READ_LINE_RE.search(tool_result_content):
            # read：带行号渲染，语言按调用时 file_path 推断
            call_id = tool_result.get("tool_call_id", "")
            args = _tool_call_args(call_id)
            file_path = str(args.get("file_path", "")) if args else ""
            self.render_read_output(tool_result_content, file_path=file_path)
        else:
            # 仅 read/bash 走语法猜测；其余工具写死 text（纯文本不猜）
            tool_name = tool_result.get("tool_name", "")
            if tool_name == "bash":
                self.render_code_block(tool_result_content)
            else:
                self.render_code_block(tool_result_content, language="text")
        print()

    def render_notice(self, notice: NoticeData) -> None:
        """系统级提醒（陈旧待办、命令已更新等）：黄色高亮。

        提醒文本（``display_content`` 非空时优先于 ``content``）整体用提醒
        样式渲染（``notice_text``，子类可加 💡 前缀等）；
        ``additional_content``，如代码块，由 ``render_notice_additional``
        渲染（default 语法高亮 / classic 原样输出）。
        """
        heading = notice.get("display_content") or notice.get("content", "")
        print(f"\x1B[1;33m{self.notice_text(heading)}\x1B[0m")
        additional = notice.get("additional_content", "")
        if additional:
            self.render_notice_additional(additional)
        print()

    def render_exception(self, exc: ExceptionData) -> None:
        exc_type = exc.get("type", "Unknown")
        exc_message = exc.get("message", "")
        traceback_str = exc.get("traceback", str(exc))
        print(f"\x1B[1;31m{self.exception_title(exc_type, exc_message)}\x1B[0m")
        self.render_code_block(traceback_str, language=self.traceback_language)
        print()

    def render_interrupt(self) -> None:
        # 交互状态输出空行
        print('\n')

    def render_retry_hint(self) -> None:
        """中断/异常后的重试提示。

        由 CLI 在接收下一轮用户输入前调用：当最后一条非工具事件是中断
        （InterruptEvent，含真实 Ctrl-C 与用户取消）或异常
        （ExceptionEvent）时，提示用户可通过 Ctrl-T 或 /retry 重新进入
        agent 循环继续被中断的对话。
        """
        raise NotImplementedError

    # ---- 用户消息 / 提示符 / 输入区（子类差异点） ----
    def render_user_message(self, text: str, mode: Mode) -> None:
        raise NotImplementedError

    def prompt_prefix(self, mode: Mode) -> str:
        """返回提示符文本（不含尾随空格）。"""
        raise NotImplementedError

    def input_placeholder(self) -> FormattedText:
        """输入框为空时的占位文字：``↵ 换行，Alt-↵（ESC ↵）发送``。

        用 ``FormattedText`` 携带 ``class:placeholder`` 样式（灰显斜体），
        prompt_toolkit 的 ``AfterInput`` 处理器会原样保留该片段样式；
        纯文本字符串只能得到空样式、无法灰显。
        """
        return FormattedText([("class:placeholder", _INPUT_PLACEHOLDER)])

    def prompt_fragments(self) -> list[tuple[str, str]]:
        mode = MODE_STATE.get()
        return [(f"class:{_MODE_PROMPT_STYLES[mode]}", self.prompt_prefix(mode) + " ")]

    def render_mode_change(self, mode: str) -> None:
        # 模式切换：输出一行提示（保持简洁，不打扰流水）
        print(f"\x1B[90m已切换到【{mode}】模式\x1B[0m\n")

    def render_resume_hint(self, cmd: str) -> None:
        """渲染退出时的「继续本次会话」恢复命令。

        提示语在公共流程中统一输出，命令本身交给 ``render_resume_cmd``：
        classic 原样输出纯文本，default 用 rich 渲染成 markdown 内联代码。
        """
        print("\n可通过以下命令继续本次会话：")
        self.render_resume_cmd(cmd)

    def render_resume_cmd(self, cmd: str) -> None:
        raise NotImplementedError

    def create_prompt_style(self) -> Style:
        raise NotImplementedError

    def apply_input_style(self, session: PromptSession) -> None:
        pass


class _DefaultRenderer(_Renderer):
    """默认渲染风格：emoji 标题 + 灰色输入区。"""

    symbols = _TODO_SYMBOLS
    traceback_language = "python"

    def ai_title(self, model: str) -> str:
        return f"🤖 {model}"

    def tool_call_title(self, func_name: str) -> str:
        return f"🔧 调用工具 - {func_name}"

    def tool_result_title(self) -> str:
        return "📤 工具输出"

    def notice_text(self, content: str) -> str:
        return f"💡 {content}"

    def exception_title(self, exc_type: str, exc_message: str) -> str:
        return f"❌ 异常 - {exc_type} - {exc_message}"

    def render_assistant_body(self, body: str) -> None:
        """default：assistant 正文用 rich Markdown 渲染（标题/列表/表格/代码块等）。"""
        _markdown_plain(body)

    def render_resume_cmd(self, cmd: str) -> None:
        """default：恢复命令放进 markdown 内联代码，经 rich 渲染。"""
        _markdown_plain(f"`{cmd}`")

    def render_retry_hint(self) -> None:
        """default：Ctrl-T 用 <kbd> 标签、/retry 用内联代码渲染 markdown。"""
        _markdown_plain("按 <kbd>Ctrl-T</kbd> 或输 `/retry` 命令重试")
        print()

    def render_code_block(self, body: str, language: str | None = None) -> None:
        """default：rich 语法高亮渲染代码块（不带行号）。

        未显式指定语言时按内容猜测（Pygments ``guess_lexer``），识别不到
        回退 ``markdown``。内容自带 ANSI 控制码时不做猜测、改用代码围栏
        原样包裹（同 classic，避免控制码被再次上色泄漏转义序列）。

        代码块上下各留 1 行纯背景空行（`_syntax_plain` 的 padding），与
        assistant 正文代码块 / read 的留白节奏一致。
        """
        body = body.rstrip(chr(0x0A))
        if _has_ansi_control(body):
            # ANSI 内容不做语法高亮：用代码围栏原样包裹（同基类/classic）
            super().render_code_block(body)
            return
        if language is None:
            language = _guess_read_language(body)
        print(_syntax_plain(body, language=language), end="")

    def render_read_output(self, content: str, file_path: str = "") -> None:
        """default：read 工具返回用 rich 带行号语法高亮渲染。

        read 返回的每行形如 ``行号\\t内容``；先按既定规则拆分：
        1. 最后一行若以 ``...`` 开头则单独拿出；
        2. 其余带行号的行去掉行号，交给 rich 处理成带行号的；
        3. 若有单独拿出的行再补上输出。

        语言优先按调用时的 ``file_path``（扩展名）推断，其次按内容猜测。
        代码块上下各留 1 行纯背景空行（`_syntax_plain` 的 padding）。
        截断/剩余提示行并入同一代码块区域，**紧贴带行号正文最后一行**
        （中间无空行）；只有提示行（无带行号正文）时提示行前由 1 行背景
        空行作顶部留白。
        """
        read_content = content.rstrip(chr(0x0A))
        normal, marker = _split_read_output(read_content)
        if normal:
            joined = "\n".join(normal)
            language = _guess_filename_language(file_path, joined)
            # 带行号正文块：上 1 行留白 + 带行号正文 + 底部留白（有提示行时
            # 底部不额外留白，提示行紧贴正文最后一行）
            print(_syntax_plain(
                joined, language=language,
                line_numbers=True, start_line=_first_read_lineno(read_content),
                bottom_padding=0 if marker else 1,
            ), end="")
        if marker:
            # 提示行：紧贴正文最后一行（中间无空行；无正文时先铺 1 行背景
            # 空行作顶部留白），用蓝灰前景 + 深灰背景、按显示宽度补空格
            # 铺满整行；下方铺 1 行背景留白收尾。
            if not normal:
                print(_spacer_text(_CODE_BG))
            columns = _terminal_columns()
            marker_line = marker[0].rstrip(chr(0x0A))
            from prompt_toolkit.utils import get_cwidth
            pad = max(0, columns - sum(get_cwidth(ch) for ch in marker_line))
            print(f"{_HIGHLIGHT_MUTED}{_CODE_BG}{marker_line}"
                  f"{' ' * pad}{_RESET}", end="")
            print(_spacer_text(_CODE_BG))

    def render_tool_call_params(
        self,
        func_name: str,
        params: dict,
        yaml_text: str,
        call_id: str = "",
        replay: bool = False,
    ) -> None:
        """default：工具调用参数渲染（bash / write / patch / edit 特化）。

        与工具调用 YAML 一贯的语法高亮不同，这四个工具做特化处理：
        YAML 中先去掉大正文/大 diff 字段（避免双份大文本展示），添加一个
        空行后，在新代码块中按各自语法再次展示：

        - ``bash``：去 ``command``，新代码块用 ``bash`` 语法、**带行号**
          展示 command 文本；
        - ``write``：去 ``content``，新代码块按 ``file_path`` 推断语法
          （Pygments），**带行号**展示 content；
        - ``patch``：去 ``diff``，新代码块用 ``diff`` 语法、**不带行号**
          展示 diff（保留 ``---``/``+++``/``@@``/``+``/``-`` 着色）；
        - ``edit``：去 ``old_text``/``new_text``，新代码块用 ``diff`` 语法、
          **不带行号**展示二者的 unified diff。

        ``params`` 为空（参数无法解析成 dict / 非法 JSON / 非对象）时无法
        特化，回退旧版行为：直接原样展示原始参数字符串。

        ``replay`` 为 True（历史重放）时文件可能已不存在/已改变：edit
        不读实际文件，改用片段级 diff 并把无真实语义的 ``@@`` 行号替换
        为 ``@@ ... @@``。
        """
        if not params:
            self.render_code_block(yaml_text, language="yaml")
            return

        def _p(name: str, default: Any = "") -> Any:
            return params.get(name, default)

        # YAML 摘要块（write/patch/edit 特化场景）：顶部带 1 行背景留白
        # 与标题隔开；中间用 1 行背景空行与后续内容大代码块分隔；整体
        # 视觉上是一个连续背景的代码块，上下各留 1 行背景（底部 1 行
        # 由内容块提供）。
        def _emit_yaml(chunk: dict) -> None:
            if chunk:
                # YAML 摘要块：上下各留 1 行带背景留白
                print(_syntax_plain(
                    _yaml_dump(chunk), language="yaml",
                    top_padding=1, bottom_padding=1,
                ), end="")
                # 区块之间的普通空行（不带背景），分隔 YAML 摘要块与内容块
                print()

        if func_name == "bash":
            # YAML 中不再展示 command（避免双份命令文本）；新代码块直接
            # 用 bash 语法带行号展示命令文本、上下各留 1 行背景留白
            command = str(_p("command"))
            if command:
                print(_syntax_plain(
                    command, language="bash", line_numbers=True,
                ), end="")
        elif func_name == "write":
            # YAML 中去掉 content；新代码块按 file_path 推断语法、带行号展示
            _emit_yaml({"file_path": _p("file_path")})
            content = str(_p("content"))
            if content:
                lang = _guess_filename_language(str(_p("file_path")), content)
                print(_syntax_plain(
                    content.rstrip(chr(0x0A)), language=lang,
                    line_numbers=True, top_padding=1, bottom_padding=1,
                ), end="")
        elif func_name == "patch":
            # YAML 中去掉 diff；新代码块用 diff 语法、不带行号展示
            _emit_yaml({"dir_path": str(_p("dir_path"))})
            diff = str(_p("diff"))
            if diff:
                print(_syntax_plain(
                    diff, language="diff", top_padding=1, bottom_padding=1,
                ), end="")
        elif func_name == "edit":
            # YAML 中去掉 old_text/new_text；新代码块用 diff 语法、不带行号
            # 展示二者的 unified diff（去掉开头的 ---/+++ 文件头两行）。
            # 优先基于文件真实内容做整文件 diff（行号 = 原始文件行号，
            # 上下文用 unified_diff 默认 3 行）；文件不可用 / old_text 已
            # 不在文件中时回退片段级 diff。replay 时文件可能已不存在/已改
            # 变，不读文件，一律用片段级 diff，并把无真实语义的 @@ 行号
            # 替换为 @@ ... @@。
            old_text = str(_p("old_text"))
            new_text = str(_p("new_text"))
            file_path = str(_p("file_path"))
            replace_all = _p("replace_all") in (True, "True", "true")
            chunk: dict = {"file_path": file_path}
            if replace_all:
                chunk["replace_all"] = True
            _emit_yaml(chunk)
            edit_diff: str | None
            if replay:
                edit_diff = _old_new_diff(old_text, new_text, file_path)
                if edit_diff:
                    edit_diff = _redact_hunk_headers(edit_diff)
            else:
                edit_diff = _edit_file_diff(
                    file_path, old_text, new_text, replace_all,
                )
                if edit_diff is None:
                    edit_diff = _old_new_diff(old_text, new_text, file_path)
            if edit_diff:
                # 去掉开头的 --- / +++ 文件头两行（实时与重放一致）
                edit_diff = _strip_diff_file_headers(edit_diff)
                print(_syntax_plain(
                    edit_diff, language="diff",
                    top_padding=1, bottom_padding=1,
                ), end="")
        else:
            # 非特化工具：保持完整 YAML 参数块展示（上下各留 1 行背景）
            self.render_code_block(yaml_text, language="yaml")

    def render_notice_additional(self, additional: str) -> None:
        """default：解析围栏语言后做语法高亮；非围栏/残缺围栏原样输出。

        仅当附加内容为完整规范代码块时高亮：开头围栏（3 重以上 + 可选语言
        标签 + 换行）、正文、以及作为最后一行的收尾围栏。其余格式（纯文本、
        围栏缺失 / 空正文 / 收尾后还有内容）一律原样输出，避免误高亮反引号。

        边界规则：
        - 收尾围栏长度必须 >= 开头围栏长度（markdown 语义），避免正文里
          的短反引号行抢先闭合长开头围栏；
        - 收尾围栏取**最后一个**满足条件的行（正文中间的反引号行不提前闭合），
          且它之后必须是字符串结尾。
        """
        additional = additional.rstrip(chr(0x0A))
        m = re.match(r"^(`{3,})[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n", additional)
        if not m:
            print(additional)
            return
        open_fence, lang = m.group(1), m.group(2)
        body = additional[m.end():]
        # 收尾围栏长度 >= 开头围栏长度，且从尾部找最后一个匹配
        end_pat = re.compile(r"\n`{%d,}[ \t]*$" % len(open_fence))
        end_m = None
        for em in end_pat.finditer(body):
            end_m = em
        if end_m is None or not body[:end_m.start()].strip():
            print(additional)
            return
        chunk = body[:end_m.start()]
        print(_syntax_plain(chunk.rstrip(chr(0x0A)), language=lang or None), end="")

    def render_user_message(self, text: str, mode: Mode) -> None:
        """灰色背景输入块。竖线（含模式标记 ?/!，与提示符同色）仅出现在
        第一行行首（同输入区提示符），其余行（含续行/换行行）无竖线且
        不缩进（同输入区 ``prompt_continuation=''``）。按显示宽度自行分行，
        每段均填充到终端宽度，保证背景从消息第一行到最后一行全量覆盖。

        背景块上下各加 1 行同背景色空行（上下留白，见「统一留白规则」），
        空行以空格填满一行、仅背景可见。
        """
        columns = _terminal_columns()
        lines = str(text).split('\n')
        prompt_color = MODE_COLOR[mode]
        bar = self.prompt_prefix(mode)  # "│" / "│?" / "│!"

        # 上下各留 1 行同背景色空行（与输入区灰色背景一致）
        print(_spacer_text(bg=_INPUT_BG))
        for i, line in enumerate(lines):
            # 第一行带提示符（含模式标记），后续行顶格无前缀
            prefix = bar + " " if i == 0 else ""
            for seg in _wrap_by_display_width(f"{prefix}{line}", columns):
                if i == 0 and seg.startswith(bar):
                    # 提示符（竖线+标记）用当前模式颜色渲染，空格+正文恢复默认前景
                    tail = seg[len(bar):]
                    seg = f"{prompt_color}{bar}{_FG_DEFAULT}{tail}"
                print(f"{_INPUT_BG}{seg}{_RESET}")
        print(_spacer_text(bg=_INPUT_BG))
        print()  # 消息块（含上下背景行）结束后的空行，分隔下方内容

    def prompt_prefix(self, mode: Mode) -> str:
        """default 风格提示符：竖线 + 模式标记（?/!），不含尾随空格。"""
        return _DEFAULT_PROMPT_PREFIXES[mode]

    def create_prompt_style(self) -> Style:
        return Style.from_dict({
            '': 'bg:#333333',
            'mycode-prompt': '#00CC00 bold',
            'mycode-prompt-ask': '#0000FF bold',
            'mycode-prompt-yolo': '#FFA500 bold',
            'mycode-input': 'bg:#333333',
            # 占位文字：灰色斜体，与输入文字（继承背景色）区分
            'placeholder': 'italic fg:#999999',
        })

    def apply_input_style(self, session: PromptSession) -> None:
        # 灰色背景挂到布局根容器（HSplit）：parent_style 会下发给所有
        # 子窗口及「剩余空间」占位窗口，从而使输入区从首行铺满到终端底部。
        # container 实际是 _Split（有 style 属性），cast 过 mypy。
        container = cast(Any, session.app.layout.container)
        container.style = "class:mycode-input"

        # 输入区上下各预留 1 行固定高度的灰色背景空行，输入窗口底部始终保留
        # 1 行空白背景，多行输入无论换行多少次都不会触达终端最底行；顶部
        # 1 行则隔开上方内容。
        # 注意：不能设 dont_extend_width=True——那会让空文本窗口只占 1 个
        # 字符宽（背景无法铺满整行）；留白行宽度由根容器 HSplit 铺满。
        def _make_blank() -> Any:
            return to_container(Window(
                FormattedTextControl(""),
                height=Dimension.exact(1),
                style="class:mycode-input",
                dont_extend_height=True,
            ))

        # 仅在尚未插入时插入（防止重复调用 apply_input_style 叠加）
        if not getattr(container, "_input_blank_inserted", False):
            container.children.insert(0, _make_blank())      # 顶部留白
            container.children.append(_make_blank())         # 底部留白
            container._input_blank_inserted = True


class _ClassicRenderer(_Renderer):
    """classic 渲染风格：复选框待办 + ``myc > `` 提示符。"""

    symbols = _TODO_SYMBOLS_CLASSIC

    def ai_title(self, model: str) -> str:
        return f"AI【{model}】"

    def tool_call_title(self, func_name: str) -> str:
        return f"调用工具 - {func_name}"

    def tool_result_title(self) -> str:
        return "工具输出"

    def notice_text(self, content: str) -> str:
        return content

    def exception_title(self, exc_type: str, exc_message: str) -> str:
        return f"异常 - {exc_type} - {exc_message}"

    def render_resume_cmd(self, cmd: str) -> None:
        print(cmd)

    def render_retry_hint(self) -> None:
        """classic：纯文本提示。"""
        print("按 Ctrl-T 或输 /retry 命令重试")
        print()

    def render_user_message(self, text: str, mode: Mode) -> None:
        color = MODE_COLOR[mode]
        print(f"{color}{self.prompt_prefix(mode)} \x1B[0m{text}\n")

    def prompt_prefix(self, mode: Mode) -> str:
        """classic 风格提示符：``myc[模式] >``，不含尾随空格。"""
        return f"myc[{mode.label}] >"

    def create_prompt_style(self) -> Style:
        return Style.from_dict({
            'mycode-prompt': '#00CC00 bold',
            'mycode-prompt-ask': '#0000FF bold',
            'mycode-prompt-yolo': '#FFA500 bold',
            'placeholder': 'italic fg:#999999',
        })


# 渲染器实例缓存（按 RENDER_STYLE 惰性实例化）
_RENDERERS: dict[str, _Renderer] = {}


def set_render_style(style: str) -> None:
    """设置当前渲染风格（default / classic）。

    修改模块级 ``RENDER_STYLE``。供 CLI 入口（``main``）与其他模块
    切换渲染风格；切换后下一次 ``_get_renderer`` 会惰性实例化对应
    渲染器。
    """
    global RENDER_STYLE
    RENDER_STYLE = style


def _get_renderer() -> _Renderer:
    """返回当前渲染风格对应的渲染器实例。"""
    if RENDER_STYLE not in _RENDERERS:
        _RENDERERS[RENDER_STYLE] = (
            _ClassicRenderer() if RENDER_STYLE == "classic" else _DefaultRenderer()
        )
    return _RENDERERS[RENDER_STYLE]


def _format_todos(state: list[dict] | None = None) -> str:
    """将待办列表格式化为带状态符号与样式的可读字符串。

    渲染使用的符号与模板由当前渲染处理器（``_get_renderer``）提供：
      - ``default``：``completed``（已完成）以 ``✅️`` 开头，
        ``in_progress``（进行中）以 ``🟧`` 开头，``pending``（待处理）以
        ``🔳`` 开头（均后跟 1 空格）。
      - ``classic``：``completed``（已完成）→ ``- [x]:``（x 绿色），
        ``in_progress``（进行中）→ ``- [>]:``（> 橙色），``pending``（待处理）
        → ``- [ ]:``。

    :param state: 待办状态列表；``None`` 时取当前内存状态。
    :returns: 形如 ``"✅️ 步骤 1\n🟧 步骤 2"``（default）或
        ``"- [x]: 步骤 1\n- [>]: 步骤 2"``（classic）的字符串；
        空状态返回 ``"(TODO 列表为空)"``。
    """
    from mycode.tools.todo_write import get_todos
    items = state if state is not None else get_todos()
    if not items:
        return "(TODO 列表为空)"
    return _get_renderer().format_todos(items)


def _render_common(msg: AgentMessage) -> None:
    """共享渲染逻辑：委托给当前渲染风格对应的渲染器。"""
    renderer = _get_renderer()
    match msg:
        case SessionRecord():
            # SessionRecord 仅用于文件标识，不渲染
            pass
        case UserMessage(message=message, mode=mode):
            # CLI 里用户消息 content 始终为 str，cast 掉 openai 的联合类型
            renderer.render_user_message(cast(str, message.get('content', '')), Mode(mode))
        case AssistantMessage(message=message, model=model):
            renderer.render_assistant(message, model)
        case ToolCallEvent(tool_call=tool_call):
            renderer.render_tool_call(tool_call)
        case ToolResultEvent(tool_result=tool_result):
            renderer.render_tool_result(tool_result)
        case InterruptEvent():
            renderer.render_interrupt()
        case NoticeEvent(notice=notice):
            renderer.render_notice(notice)
        case ModeChangeEvent(mode=mode):
            renderer.render_mode_change(mode)
        case ExceptionEvent(exception=exc):
            renderer.render_exception(exc)
        case _ as unreachable:
            assert_never(unreachable)


def render_terminal(msg: AgentMessage) -> None:
    """实时交互渲染"""
    _render_common(msg)


def render_replay(msg: AgentMessage) -> None:
    """历史重放渲染：所有类型均输出。

    InterruptEvent 分两种：
      - 真实 Ctrl-C（``interrupt.abort False``）：输出 ^C 及空行；
      - abort（取消/无理由拒绝，``interrupt.abort True``）：不输出 ^C。

    ToolCallEvent 单独处理：重放时文件可能已不存在/已改变，edit 工具
    不读实际文件，改用片段级 diff 并把无真实语义的 ``@@`` 行号替换为
    ``@@ ... @@``（其余工具与实时渲染行为一致）。
    """
    match msg:
        case InterruptEvent(interrupt={"abort": False}):
            print("^C")
            print()
        case ToolCallEvent(tool_call=tool_call):
            _get_renderer().render_tool_call(tool_call, replay=True)
        case _:
            _render_common(msg)


def _prompt_fragments() -> list[tuple[str, str]]:
    """提示符片段：委托给当前渲染风格对应的渲染器。"""
    return _get_renderer().prompt_fragments()
