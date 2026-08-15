#!/usr/bin/env python3
"""
渲染器（处理器）模块：按渲染风格（default / classic）定义所有消息与
事件的终端展示方式。公共渲染流程在 _Renderer 基类复用，风格差异由
_DefaultRenderer / _ClassicRenderer 子类覆写。
"""

import json
from typing import Any, NoReturn, cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

from mycode.session import (
    SessionRecord,
    UserMessage,
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
    InterruptEvent,
    ExceptionEvent,
    ModeChangeEvent,
    ReminderEvent,
    AgentMessage,
    ExceptionData,
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
    "in_process": "🟧",   # 橙色方块
    "completed": "✅️",     # 绿色对勾
}

# 状态符号（classic 风格）：复选框形式
_TODO_SYMBOLS_CLASSIC: dict[str, str] = {
    "pending": "- [ ]:",
    "in_process": f"- [{_ORANGE}>{_RESET}]:",
    "completed": f"- [{_GREEN}x{_RESET}]:",
}

# 待办事项渲染模板：``{sym}`` 替换符号、``{title}`` 替换文本。
# emoji 后面保留 1 个 ASCII 空格作为分隔（emoji 自带的视觉宽度不算）。
# 所有风格共用同一套模板，仅替换符号。
_TODO_FORMATS: dict[str, str] = {
    # 已完成：标题灰色 + 删除线
    "completed": f"{{sym}} {_GRAY}{_STRIKE}{{title}}{_RESET}",
    # 进行中：标题粗+白（视觉强调）
    "in_process": f"{{sym}} {_BOLD_WHITE}{{title}}{_RESET}",
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

    def reminder_text(self, content: str) -> str:
        raise NotImplementedError

    def exception_title(self, exc_type: str, exc_message: str) -> str:
        raise NotImplementedError

    # ---- 渲染（公共流程在基类） ----
    def render_assistant(self, message: ChatCompletionAssistantMessageParam, model: str) -> None:
        # AI 回复：标题（风格差异）+ 正文；无正文（纯 tool_calls）时仅标题
        content = message.get("content")
        title = self.ai_title(model)
        if content and str(content).strip():
            print(f"\x1B[35m{title}\x1B[0m\n{str(content).strip(chr(0x0A))}\n")
        else:
            print(f"\x1B[35m{title}\x1B[0m")

    def render_tool_call(self, tool_call: ChatCompletionMessageFunctionToolCallParam) -> None:
        func_name = tool_call["function"]["name"]
        args_ = tool_call["function"]["arguments"]
        try:
            parsed = json.loads(args_)
        except (json.JSONDecodeError, TypeError):
            yaml_text = args_
        else:
            import yaml

            def _block_str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
                """含换行的字符串用 block literal（`|-`）输出，避免换行折叠翻倍。"""
                style = '|' if '\n' in data else None
                return dumper.represent_scalar(
                    'tag:yaml.org,2002:str', data, style=style)

            class _BlockStrDumper(yaml.SafeDumper):
                pass
            _BlockStrDumper.add_representer(str, _block_str)
            yaml_text = yaml.dump(parsed, Dumper=_BlockStrDumper,
                                  allow_unicode=True, sort_keys=False,
                                  default_flow_style=False, width=64 * 1024)
        print(f"\x1B[1;34m{self.tool_call_title(func_name)}\x1B[0m\n```yaml\n{yaml_text}```")

    def render_tool_result(self, message: ChatCompletionToolMessageParam, tool_name: str) -> None:
        # todo_write 特化渲染：先输出当前待办列表再输出结果
        if tool_name == "todo_write":
            print(f"\x1B[1;36mTODO 列表:\x1B[0m\n{self.format_todos()}\n")
        # 工具结果 content 由我方内部构造，始终为 str，cast 掉联合类型
        tool_result = cast(str, message.get("content", ""))
        fence = _code_fence(tool_result)
        print(f"\x1B[1;34m{self.tool_result_title()}\x1B[0m\n{fence}{f'{chr(0x0A)}{tool_result}'.rstrip(chr(0x0A))}\n{fence}")

    def render_reminder(
        self,
        content: str,
        display_content: str = "",
        additional_content: str = "",
    ) -> None:
        """系统级提醒（陈旧待办、命令已更新等）：黄色高亮。

        提醒文本（``display_content`` 非空时优先于 ``content``）整体用提醒
        样式渲染（``reminder_text``，子类可加 💡 前缀等）；
        ``additional_content``，如代码块，照常原样输出，不带提醒样式。
        """
        heading = display_content or content
        lines: list[str] = [f"\x1B[1;33m{self.reminder_text(heading)}\x1B[0m"]
        if additional_content:
            lines.append(additional_content.rstrip(chr(0x0A)))
        print("\n".join(lines) + "\n")

    def render_exception(self, exc: ExceptionData) -> None:
        exc_type = exc.get("type", "Unknown")
        exc_message = exc.get("message", "")
        traceback_str = exc.get("traceback", str(exc))
        print(f"\x1B[1;31m{self.exception_title(exc_type, exc_message)}\x1B[0m")
        print("```")
        print(traceback_str.rstrip("\n"))
        print("```")

    def render_interrupt(self) -> None:
        # 交互状态输出空行
        print('\n')

    # ---- 用户消息 / 提示符 / 输入区（子类差异点） ----
    def render_user_message(self, text: str, mode: Mode) -> None:
        raise NotImplementedError

    def prompt_prefix(self, mode: Mode) -> str:
        """返回提示符文本（不含尾随空格）。"""
        raise NotImplementedError

    def prompt_fragments(self) -> list[tuple[str, str]]:
        mode = MODE_STATE.get()
        return [(f"class:{_MODE_PROMPT_STYLES[mode]}", self.prompt_prefix(mode) + " ")]

    def render_mode_change(self, mode: str) -> None:
        # 模式切换：输出一行提示（保持简洁，不打扰流水）
        print(f"\x1B[90m已切换到【{mode}】模式\x1B[0m\n")

    def create_prompt_style(self) -> Style:
        raise NotImplementedError

    def apply_input_style(self, session: PromptSession) -> None:
        pass


class _DefaultRenderer(_Renderer):
    """默认渲染风格：emoji 标题 + 灰色输入区。"""

    symbols = _TODO_SYMBOLS

    def ai_title(self, model: str) -> str:
        return f"🤖 {model}"

    def tool_call_title(self, func_name: str) -> str:
        return f"🔧 调用工具 - {func_name}"

    def tool_result_title(self) -> str:
        return "📤 工具输出"

    def reminder_text(self, content: str) -> str:
        return f"💡 {content}"

    def exception_title(self, exc_type: str, exc_message: str) -> str:
        return f"❌ 异常 - {exc_type} - {exc_message}"

    def render_user_message(self, text: str, mode: Mode) -> None:
        """灰色背景输入块。竖线（含模式标记 ?/!，与提示符同色）仅出现在
        第一行行首（同输入区提示符），其余行（含续行/换行行）无竖线且
        不缩进（同输入区 ``prompt_continuation=''``）。按显示宽度自行分行，
        每段均填充到终端宽度，保证背景从消息第一行到最后一行全量覆盖。
        """
        import shutil
        columns = shutil.get_terminal_size().columns
        lines = str(text).split('\n')
        prompt_color = MODE_COLOR[mode]
        bar = self.prompt_prefix(mode)  # "│" / "│?" / "│!"
        for i, line in enumerate(lines):
            # 第一行带提示符（含模式标记），后续行顶格无前缀
            prefix = bar + " " if i == 0 else ""
            for seg in _wrap_by_display_width(f"{prefix}{line}", columns):
                if i == 0 and seg.startswith(bar):
                    # 提示符（竖线+标记）用当前模式颜色渲染，空格+正文恢复默认前景
                    tail = seg[len(bar):]
                    seg = f"{prompt_color}{bar}{_FG_DEFAULT}{tail}"
                print(f"{_INPUT_BG}{seg}{_RESET}")
        print()

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
        })

    def apply_input_style(self, session: PromptSession) -> None:
        # 灰色背景挂到布局根容器（HSplit）：parent_style 会下发给所有
        # 子窗口及「剩余空间」占位窗口，从而使输入区从首行铺满到终端底部。
        # container 实际是 _Split（有 style 属性），cast 过 mypy。
        container = cast(Any, session.app.layout.container)
        container.style = "class:mycode-input"


class _ClassicRenderer(_Renderer):
    """classic 渲染风格：复选框待办 + ``myc > `` 提示符。"""

    symbols = _TODO_SYMBOLS_CLASSIC

    def ai_title(self, model: str) -> str:
        return f"AI【{model}】"

    def tool_call_title(self, func_name: str) -> str:
        return f"调用工具 - {func_name}"

    def tool_result_title(self) -> str:
        return "工具输出"

    def reminder_text(self, content: str) -> str:
        return content

    def exception_title(self, exc_type: str, exc_message: str) -> str:
        return f"异常 - {exc_type} - {exc_message}"

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
      - ``default``：``completed`` 以 ``✅️`` 开头，``in_process`` 以
        ``🟧`` 开头，``pending`` 以 ``🔳`` 开头（均后跟 1 空格）。
      - ``classic``：``completed`` → ``- [x]:``（x 绿色），``in_process``
        → ``- [>]:``（> 橙色），``pending`` → ``- [ ]:``。

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
        case ToolResultEvent(message=message, tool_name=tool_name):
            renderer.render_tool_result(message, tool_name)
        case InterruptEvent():
            renderer.render_interrupt()
        case ReminderEvent(content=content, display_content=display_content,
                           additional_content=additional_content):
            renderer.render_reminder(content, display_content, additional_content)
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
      - 真实 Ctrl-C（``abort=False``）：输出 ^C 及空行；
      - abort（取消/无理由拒绝，``abort=True``）：不输出 ^C，仅空行。
    """
    match msg:
        case InterruptEvent(abort=False):
            print("^C")
            print()
        case _:
            _render_common(msg)


def _prompt_fragments() -> list[tuple[str, str]]:
    """提示符片段：委托给当前渲染风格对应的渲染器。"""
    return _get_renderer().prompt_fragments()
