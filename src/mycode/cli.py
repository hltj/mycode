#!/usr/bin/env python3

import os
import sys
import json
import argparse
from typing import Callable, NoReturn, cast

from dotenv import load_dotenv
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
    ChatCompletionAssistantMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallUnionParam,
    ChatCompletionMessageFunctionToolCallParam,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completion, Completer
from prompt_toolkit.styles import Style

# ---------------------------------------------------------------------------
# 加载 .env 环境变量
# ---------------------------------------------------------------------------
load_dotenv()

# APP_HOME_DIR 和 历史记录文件
APP_HOME_DIR = __import__('pathlib').Path(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode')))
APP_HOME_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = APP_HOME_DIR / 'history.txt'

# 系统提示词
_BASE_SYSTEM_PROMPT = f"你是编程智能体 mycode。当前在 {os.getcwd()}。使用工具完成任务。直接做勿解释。"
_ADDITIONAL = os.getenv('ADDITIONAL_SYSTEM_PROMPT')
SYSTEM_PROMPT = _BASE_SYSTEM_PROMPT + (("\n" + _ADDITIONAL) if _ADDITIONAL else "")

# ---------------------------------------------------------------------------
# TODO 渲染辅助
# ---------------------------------------------------------------------------
# ANSI 转义常量
_RESET = "\x1B[0m"
_GRAY = "\x1B[90m"          # bright black = 深灰
_STRIKE = "\x1B[9m"         # 删除线
_BOLD_WHITE = "\x1B[1;37m"  # 粗+白（在多数终端等价于亮白，对中文可见度比纯粗体好）

# 状态符号：emoji 自带颜色，不依赖 ANSI 上色（避免彩色 emoji 字体忽略 ANSI）。
_TODO_SYMBOL: dict[str, str] = {
    "pending": "🔳",      # 白色方块
    "in_process": "🟧",   # 橙色方块
    "completed": "✅️",     # 绿色对勾
}

# 渲染模板：``{sym}`` 替换符号、``{title}`` 替换文本。
# emoji 后面保留 1 个 ASCII 空格作为分隔（emoji 自带的视觉宽度不算）。
_TODO_FORMATS: dict[str, str] = {
    # 已完成：标题灰色 + 删除线
    "completed": f"{{sym}} {_GRAY}{_STRIKE}{{title}}{_RESET}",
    # 进行中：标题粗+白（视觉强调）
    "in_process": f"{{sym}} {_BOLD_WHITE}{{title}}{_RESET}",
    # 未开始：全部普通样式
    "pending": "{sym} {title}",
}


def _format_todos(state: list[dict] | None = None) -> str:
    """将 TODO 列表格式化为带状态符号与样式的可读字符串。

    渲染规则：
      - ``completed``：以 ``✅️`` 开头（后跟 1 空格），标题灰色带删除线。
      - ``in_process``：以 ``🟧`` 开头（后跟 1 空格），标题粗体+白色。
      - ``pending``：以 ``🔳`` 开头（后跟 1 空格），标题普通样式。

    :param state: TODO 状态列表；``None`` 时取当前内存状态。
    :returns: 形如 ``"✅️ 步骤 1\n🟧 步骤 2"`` 的字符串；空状态返回
        ``"(TODO 列表为空)"``。
    """
    from mycode.tools.todo_write import get_todos
    items = state if state is not None else get_todos()
    if not items:
        return "(TODO 列表为空)"
    return "\n".join(
        _TODO_FORMATS[it["status"]].format(
            sym=_TODO_SYMBOL[it["status"]],
            title=it["title"],
        )
        for it in items
    )


# ---------------------------------------------------------------------------
# 导入工具
# ---------------------------------------------------------------------------
from mycode.tools_registry import ToolsRegistry

client = OpenAI(
    api_key=os.getenv('API_KEY'),
    base_url=os.getenv('BASE_URL'),
)

# ===================================================================
# 从 session 导入 ADT 类型
# ===================================================================

from mycode.session import (
    SessionRecord,
    UserMessage,
    AssistantMessage,
    ToolCallEvent,
    ToolResultEvent,
    InterruptEvent,
    ExceptionEvent,
    ReminderEvent,
    AgentMessage,
    SessionHistory,
)


def assert_never(arg: NoReturn) -> NoReturn:
    raise AssertionError(f"未处理的消息类型: {type(arg).__name__}")


# ===================================================================
# 事件总线
# ===================================================================

Handler = Callable[[AgentMessage], None]


class AgentEventBus:
    def __init__(self, session_hist: SessionHistory | None = None) -> None:
        self._handlers: list[Handler] = []
        self._session_hist = session_hist

    def register(self, handler: Handler) -> Handler:
        self._handlers.append(handler)
        return handler

    def dispatch(self, msg: AgentMessage) -> None:
        if self._session_hist is not None:
            self._session_hist.inject_meta(msg)
        for handler in self._handlers:
            handler(msg)


# ===================================================================
# 处理器 —— 持久化
# ===================================================================


def make_persist_handler(session_hist: SessionHistory) -> Handler:
    """持久化处理器工厂：闭包捕获 session_hist"""
    def persist(msg: AgentMessage) -> None:
        session_hist.append(msg)
    return persist


# ===================================================================
# 处理器 —— 渲染（分层委托消除重复）
# ===================================================================

def _render_common(msg: AgentMessage) -> None:
    """共享渲染逻辑：用户输入 / AI回复 / 工具调用 / 工具结果"""
    match msg:
        case SessionRecord():
            # SessionRecord 仅用于文件标识，不渲染
            pass
        case UserMessage(message=message):
            print(f"\x1B[38;2;0;204;0;1mmyc > \x1B[0m{message.get('content', '')}")
        case AssistantMessage(message=message, model=model):
            content = message.get("content")
            if content and str(content).strip():
                print(f"\x1B[1;34mAI【{model}】:\x1B[0m\n{str(content).strip(chr(0x0A))}\n")
        case ToolCallEvent(tool_call=tool_call):
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
            print(f"\x1B[1;36m调用工具 - {func_name}\x1B[0m\n```yaml\n{yaml_text}```")
        case ToolResultEvent(message=message, tool_name=tool_name):
            # todo_write 特化渲染：先输出当前 TODO 列表再输出结果
            if tool_name == "todo_write":
                print(f"\x1B[1;36mTODO 列表:\x1B[0m\n{_format_todos()}\n")
            tool_result = message.get("content", "")
            print(f"\x1B[1;36m工具输出:\x1B[0m\n```{f'{chr(0x0A)}{tool_result}'.rstrip(chr(0x0A))}\n```")
        case InterruptEvent():
            # 交互状态输出空行
            print('\n')
        case ReminderEvent(content=content):
            # 系统级提醒（陈旧 TODO 等）：黄色高亮
            print(f"\x1B[1;33m{content}\x1B[0m\n")
        case ExceptionEvent(exception=exc):
            exc_type = exc.get("type", "Unknown")
            exc_message = exc.get("message", "")
            traceback_str = exc.get("traceback", str(exc))
            print(f"\x1B[1;31m异常 - {exc_type} - {exc_message}\x1B[0m")
            print("```")
            print(traceback_str.rstrip("\n"))
            print("```")
        case _ as unreachable:
            assert_never(unreachable)


def render_terminal(msg: AgentMessage) -> None:
    """实时交互渲染"""
    _render_common(msg)


def render_replay(msg: AgentMessage) -> None:
    """历史重放渲染：所有类型均输出；InterruptEvent 输出 ^C 及空行"""
    match msg:
        case InterruptEvent():
            print("^C")
            print()
        case _:
            _render_common(msg)


# ===================================================================
# replay handlers —— 同步工具状态
# ===================================================================

def make_replay_todo_sync_handler() -> Handler:
    """replay 时把 todo_write 的 ToolCallEvent 实时同步到 _todo_state。

    这样后续 dispatch 的对应 ToolResultEvent 渲染时，能看到调用
    "那一刻"的 TODO 列表，而不是历史最终态。
    """
    from mycode.tools.todo_write import todo_write as todo_write_func

    def sync(msg: AgentMessage) -> None:
        if not isinstance(msg, ToolCallEvent):
            return
        tool_call = getattr(msg, "tool_call", None)
        if not isinstance(tool_call, dict):
            return
        func = tool_call.get("function")
        if not isinstance(func, dict) or func.get("name") != "todo_write":
            return
        try:
            args = json.loads(func.get("arguments", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        items = args.get("items") if isinstance(args, dict) else None
        if not isinstance(items, list):
            return
        todo_write_func(items)

    return sync


# ===================================================================
# replay_history —— 遍历 entries → dispatch render_replay
# ===================================================================

def replay_history(session_hist: SessionHistory) -> None:
    """重放历史会话"""
    bus_replay = AgentEventBus()
    # bus 按注册顺序 dispatch：render 先于 sync。
    # ToolCallEvent 派发时 render 会打印"调用工具"提示，紧接着 sync
    # 写入 _todo_state；同一 entry 处理完才到下一个，因此对应的
    # ToolResultEvent 渲染时已经看到最新状态。
    bus_replay.register(render_replay)
    bus_replay.register(make_replay_todo_sync_handler())

    for entry in session_hist.entries:
        bus_replay.dispatch(entry)


# ===================================================================
# 智能体自循环
# ===================================================================

def agent_loop(
    messages: list[ChatCompletionMessageParam],
    bus: AgentEventBus,
    model: str,
) -> None:
    from mycode.tools.todo_write import (
        bump_stale_rounds,
        should_remind_stale_todo,
        format_stale_reminder,
        reset_stale_rounds as reset_todo_stale,
    )
    tools = ToolsRegistry.get_tools()
    while True:
        # ---- 陈旧 TODO 提醒 ----
        # 每产生一个 assistant 消息前自增 stale；超过阈值且存在未完成
        # TODO 时，往 messages 注入一条 user role 提示（让模型看到），
        # 同时派发 ReminderEvent（让终端显示 + 持久化，与 replay 渲染一致），
        # 然后清零 stale（避免连续打扰）。
        bump_stale_rounds()
        if should_remind_stale_todo():
            reminder_text = format_stale_reminder()
            # 进 messages：让模型下次 API 调用能看到
            messages.append(ChatCompletionUserMessageParam(
                role='user', content=reminder_text
            ))
            # 进 bus：渲染 + 持久化（走 ReminderEvent 而非 UserMessage，
            # 避免 replay 时被当成用户输入显示 ``myc > `` 前缀）
            bus.dispatch(ReminderEvent(model=model, content=reminder_text))
            reset_todo_stale()

        # 调用模型
        # noinspection PyTypeChecker
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )

        # noinspection PyUnresolvedReferences
        choice = response.choices[0]
        message = choice.message
        content = message.content

        # 将 Pydantic tool_calls 转为 list[ChatCompletionMessageToolCallUnionParam]
        serialized_tool_calls = cast(
            list[ChatCompletionMessageToolCallUnionParam],
            [tc.model_dump() for tc in (message.tool_calls or [])]
        )

        # 消息列表追加模型回复
        assistant_msg = ChatCompletionAssistantMessageParam(
            role='assistant', content=content, tool_calls=serialized_tool_calls
        )
        messages.append(assistant_msg)

        # dispatch AI 回复
        bus.dispatch(AssistantMessage(model=model, message=assistant_msg))

        # 非工具调用结束循环
        if choice.finish_reason != 'tool_calls':
            return

        # 收集所有 function 类型的 tool_call（按 assistant 返回顺序）。
        # 后续无论工具是被异常打断还是正常完成，都能精准定位剩余未处理的项，
        # 以便为它们补占位 tool 消息，避免 assistant.tool_calls 出现"孤儿子项"。
        pending: list[ChatCompletionMessageFunctionToolCallParam] = [
            cast(ChatCompletionMessageFunctionToolCallParam, tc)
            for tc in serialized_tool_calls
            if tc.get("type") == "function"
        ]

        # 中断/异常原因：正常路径保持 None；赋值后仅用于 isinstance 判断与
        # 构造 skip_message，不再向上抛（agent_loop 自身处理完毕即返回）。
        cause: BaseException | None = None
        # 记录异常发生在 pending 中的索引位置（0-based），用于精确补齐剩余项。
        # 异常项本身已在 finally 中补上 tool 消息；只需为「此索引之后」的
        # 尚未开始执行的 tool_call 补占位消息，已正常完成的更早项不动。
        interrupted_at: int = -1

        for idx, tool_call in enumerate(pending):
            func_name = tool_call["function"]["name"]
            handler = ToolsRegistry.get_handler(func_name)

            # dispatch 工具调用事件
            bus.dispatch(ToolCallEvent(model=model, tool_call=tool_call))

            args_str = tool_call["function"]["arguments"]
            tool_result: str | None = None
            try:
                if handler is None:
                    tool_result = f"Error: Unknown tool '{func_name}'"
                else:
                    # 解析工具参数并执行
                    # noinspection PyUnresolvedReferences
                    args_ = json.loads(args_str)
                    tool_result = handler(**args_)
            except KeyboardInterrupt as e:
                # Ctrl-C 中断工具执行：先 dispatch InterruptEvent 让 ^C 后
                # 立即换行，然后用占位错误信息补齐 tool 消息（finally 中），
                # 最后跳出循环、agent_loop 自然返回。
                cause = e
                interrupted_at = idx
                tool_result = "Error: Tool execution interrupted by user"
                bus.dispatch(InterruptEvent(model=model))
                break
            except BaseException as e:
                # 工具执行抛异常：补一条 tool 错误消息、dispatch ExceptionEvent
                # 并跳出循环。注意：traceback 必须在 except 块内获取，
                # 否则 sys.exc_info() 已被清除，format_exc() 会返回
                # "NoneType: None"。
                import traceback as _tb
                cause = e
                interrupted_at = idx
                tool_result = f"Error: Tool execution failed: {type(e).__name__}: {e}"
                bus.dispatch(ExceptionEvent(model=model, exception={
                    "type": type(e).__name__,
                    "message": str(e),
                    "traceback": _tb.format_exc().rstrip(),
                }))
                break
            finally:
                # 始终补上对应的 tool 消息与 ToolResultEvent，
                # 避免下次恢复会话时模型供应商校验失败
                # （tool call result does not follow tool call）。
                # finally 在 break 前执行，能保证补上消息后再跳出循环。
                if tool_result is None:
                    tool_result = "Error: Tool execution terminated abnormally"
                tool_msg = ChatCompletionToolMessageParam(
                    role='tool', tool_call_id=tool_call["id"], content=tool_result
                )
                messages.append(tool_msg)
                bus.dispatch(ToolResultEvent(model=model, message=tool_msg, tool_name=func_name))

        # 如果因为异常/Ctrl-C 提前跳出循环，assistant 消息里仍包含所有
        # pending 中的 tool_calls，但只到 interrupted_at（含）的项补上了
        # tool 消息。必须为「此索引之后」的剩余未处理的 tool_call 也补占位
        # tool 消息，并 dispatch 相应事件，保持消息序列对模型合法。
        if cause is not None:
            # 提示事件（InterruptEvent / ExceptionEvent）已在各 except 块内
            # dispatch —— 此处仅构造 skip_message 并补齐剩余 tool_call。
            skip_message = (
                "Error: Tool execution skipped: interrupted by user"
                if isinstance(cause, KeyboardInterrupt)
                else f"Error: Tool execution skipped due to previous error: "
                     f"{type(cause).__name__}: {cause}"
            )

            # 为「此索引之后」的剩余未处理的 tool_call 补占位 tool 消息
            # 并 dispatch 相应事件，保持消息序列对模型合法。
            for remaining in pending[interrupted_at + 1:]:
                bus.dispatch(ToolCallEvent(model=model, tool_call=remaining))
                tool_msg = ChatCompletionToolMessageParam(
                    role='tool', tool_call_id=remaining["id"], content=skip_message
                )
                messages.append(tool_msg)
                bus.dispatch(ToolResultEvent(model=model, message=tool_msg, tool_name=func_name))
            # 所有提示事件已 dispatch、补齐已完成。让 agent_loop 自然返回，
            # 由外层 main() 继续接受下一轮用户输入。
            return


# ===================================================================
# CLI 命令补全 & 参数解析
# ===================================================================

class MycCommandCompleter(Completer):
    COMMANDS = ["/q", "/quit"]

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith('/'):
            for cmd in self.COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display=cmd)


def parse_args():
    parser = argparse.ArgumentParser(prog='mycode', description='mycode - 编程智能体')
    parser.add_argument('-r', '--resume', type=str, metavar='SESSION_ID',
                        help='恢复指定会话')
    parser.add_argument('-c', '--continue', dest='continue_session', action='store_true',
                        help='恢复当前目录的最新会话')
    # 将 -h/--help 的 help 文本改为中文
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            action.help = '显示此帮助信息并退出'
    return parser.parse_args()


# ===================================================================
# main
# ===================================================================

from mycode.session import find_latest_session_file, get_session_file

def main():
    args = parse_args()

    model = os.getenv('MODEL_NAME') or ''

    # 消息列表初始化系统提示词
    hist_messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(role='system', content=SYSTEM_PROMPT),
    ]

    session_hist: SessionHistory

    if args.resume:
        session_id = args.resume
        session_file = get_session_file(session_id)
        if session_file is None:
            print(f"未找到会话 ID 为 {session_id} 的会话。")
            sys.exit(1)

        session_hist = SessionHistory.load(session_file)
        hist_messages.extend(session_hist.get_messages())
    elif args.continue_session:
        session_file = find_latest_session_file()
        if session_file is None:
            print(f"未找到当前目录的会话记录。")
            sys.exit(1)

        session_hist = SessionHistory.load(session_file)
        hist_messages.extend(session_hist.get_messages())
    else:
        session_hist = SessionHistory(cwd=os.getcwd(), model=model)

    # ---- 组装事件总线（CLI 场景：持久化 + 终端渲染） ----
    bus = AgentEventBus(session_hist=session_hist)
    bus.register(make_persist_handler(session_hist))
    bus.register(render_terminal)

    # 输出开头信息
    print("【mycode】")
    print(f"会话 ID: {session_hist.session_uuid}")
    print()

    if args.resume or args.continue_session:
        replay_history(session_hist)

    # ---- prompt_toolkit 配置 ----

    # 定义提示符样式
    prompt_style = Style.from_dict({
        'mycode-prompt': '#00CC00 bold',
    })

    # 配置 prompt_toolkit session
    completer = MycCommandCompleter()
    session: PromptSession[str] = PromptSession(
        history=FileHistory(HISTORY_FILE),
        completer=completer,
        multiline=True,
        style=prompt_style,
        prompt_continuation='',
        erase_when_done=True,
    )

    while True:
        try:
            user_input = session.prompt([('class:mycode-prompt', 'myc > ')])
            if user_input.strip() in {"/q", "/quit"}:
                break

            # 消息列表追加用户输入并进入智能体循环
            user_msg = ChatCompletionUserMessageParam(role='user', content=user_input)
            hist_messages.append(user_msg)

            # dispatch 用户消息（持久化 + 渲染）
            bus.dispatch(UserMessage(model=model, message=user_msg))

            agent_loop(hist_messages, bus, model)

        except EOFError:
            # Ctrl-D: 退出程序
            break

        except KeyboardInterrupt:
            # Ctrl-C 在 prompt 阶段被触发：agent_loop 未运行，相当于什么都没
            # 中断；输出换行让光标回到新行，再 continue 重新接受下一轮输入。
            print()
            continue

        except Exception as e:
            # 外层自身产生的异常（如 session.prompt / 消息解析等）；
            # agent_loop 内部的异常已由它自身处理。
            import traceback as tb
            tb_lines = tb.format_exc().rstrip()
            bus.dispatch(ExceptionEvent(model=model, exception={
                "type": type(e).__name__,
                "message": str(e),
                "traceback": tb_lines,
            }))
            continue

    # ---- 退出提示 ----

    entry_count = len([e for e in session_hist.entries if isinstance(e, (UserMessage, AssistantMessage, ToolResultEvent))])
    if entry_count == 0:
        # 没有任何用户消息，删除只有 session 记录的文件
        session_hist.file_path.unlink(missing_ok=True)
        print("\n无输入，会话已清理。")
    else:
        print(f"\n可通过以下命令继续本次会话：\n{sys.argv[0]} -r {session_hist.session_uuid}")

if __name__ == '__main__':
    main()
