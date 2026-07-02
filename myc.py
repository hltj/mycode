#!/usr/bin/env python3

import os
import sys
import argparse
from dataclasses import dataclass
from pathlib import Path
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
APP_HOME_DIR = Path(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode')))
APP_HOME_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = APP_HOME_DIR / 'history.txt'

# 系统提示词
SYSTEM = f"你是编程智能体 mycode。当前在 {os.getcwd()}。使用 bash 完成任务。直接做勿解释。"

# ---------------------------------------------------------------------------
# 导入工具
# ---------------------------------------------------------------------------
# noinspection PyUnusedImports
from tools import bash
from tools_reg import ToolsRegistry

client = OpenAI(
    api_key=os.getenv('API_KEY'),
    base_url=os.getenv('BASE_URL'),
)

# ===================================================================
# ADT —— 消息/动作类型
# ===================================================================

@dataclass
class UserMessage:
    msg_param: ChatCompletionUserMessageParam
    model: str

@dataclass
class AssistantMessage:
    msg_param: ChatCompletionAssistantMessageParam
    model: str

@dataclass
class ToolCallEvent:
    tool_call: ChatCompletionMessageFunctionToolCallParam
    model: str

@dataclass
class ToolResultEvent:
    msg_param: ChatCompletionToolMessageParam
    model: str

AgentMessage = UserMessage | AssistantMessage | ToolCallEvent | ToolResultEvent


def assert_never(arg: NoReturn) -> NoReturn:
    raise AssertionError(f"未处理的消息类型: {type(arg).__name__}")


# ===================================================================
# 事件总线
# ===================================================================

Handler = Callable[[AgentMessage], None]


class AgentEventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def register(self, handler: Handler) -> Handler:
        self._handlers.append(handler)
        return handler

    def dispatch(self, msg: AgentMessage) -> None:
        for handler in self._handlers:
            handler(msg)


# ===================================================================
# 处理器 —— 持久化
# ===================================================================

from session import SessionHistory


def make_persist_handler(session_hist: SessionHistory) -> Handler:
    """持久化处理器工厂：闭包捕获 session_hist"""
    def persist(msg: AgentMessage) -> None:
        match msg:
            case UserMessage(msg_param, model):
                session_hist.append_message(msg_param, model)
            case AssistantMessage(msg_param, model):
                session_hist.append_message(msg_param, model)
            case ToolCallEvent(tool_call, model):
                session_hist.append_tool_call(tool_call, model)
            case ToolResultEvent(msg_param, model):
                session_hist.append_message(msg_param, model)
            case _ as unreachable:
                assert_never(unreachable)
    return persist


# ===================================================================
# 处理器 —— 渲染（分层委托消除重复）
# ===================================================================

def _render_common(msg: AgentMessage) -> None:
    """共享渲染逻辑：AI回复 / 工具调用 / 工具结果"""
    match msg:
        case AssistantMessage(msg_param, model):
            content = msg_param.get("content")
            if content and str(content).strip():
                print(f"\x1B[1;34mAI【{model}】:\x1B[0m\n{str(content).strip(chr(0x0A))}\n")
        case ToolCallEvent(tool_call, _):
            func_name = tool_call["function"]["name"]
            args_ = tool_call["function"]["arguments"]
            print(f"\x1B[1;36m调用工具 - {func_name}\x1B[0m\n```json\n{args_}\n```")
        case ToolResultEvent(msg_param, _):
            tool_result = msg_param.get("content", "")
            print(f"\x1B[1;36m工具输出:\x1B[0m\n```{f'{chr(0x0A)}{tool_result}'.rstrip(chr(0x0A))}\n```")
        case UserMessage(_, _):
            # 实时交互中不渲染用户消息（prompt_toolkit 已显示）
            pass


def render_terminal(msg: AgentMessage) -> None:
    """实时交互渲染：UserMessage → prompt_toolkit 已显示，跳过"""
    _render_common(msg)


def render_replay(msg: AgentMessage) -> None:
    """历史重放渲染：所有类型均输出，UserMessage 单独处理后委托"""
    match msg:
        case UserMessage(msg_param, _):
            print(f"\x1B[38;2;0;204;0;1mmyc > \x1B[0m{msg_param.get('content', '')}")
        case _:
            _render_common(msg)


# ===================================================================
# replay_history —— 遍历 entries → dispatch render_replay
# ===================================================================

def replay_history(session_hist: SessionHistory) -> None:
    """重放历史会话"""
    from session import SessionMessage, SessionToolCall

    bus_replay = AgentEventBus()
    bus_replay.register(render_replay)

    for entry in session_hist.entries:
        if isinstance(entry, SessionMessage):
            msg = entry.message
            role = msg.get("role")
            if role == "user":
                user_param: ChatCompletionUserMessageParam = msg  # type: ignore[assignment]
                bus_replay.dispatch(UserMessage(msg_param=user_param, model=entry.model))
            elif role == "assistant":
                asst_param: ChatCompletionAssistantMessageParam = msg  # type: ignore[assignment]
                bus_replay.dispatch(AssistantMessage(msg_param=asst_param, model=entry.model))
            elif role == "tool":
                tool_param: ChatCompletionToolMessageParam = msg  # type: ignore[assignment]
                bus_replay.dispatch(ToolResultEvent(msg_param=tool_param, model=entry.model))
        elif isinstance(entry, SessionToolCall):
            tc = entry.tool_call
            bus_replay.dispatch(ToolCallEvent(tool_call=tc, model=entry.model))


# ===================================================================
# 智能体自循环
# ===================================================================

def agent_loop(
    messages: list[ChatCompletionMessageParam],
    bus: AgentEventBus,
    model: str,
) -> None:
    tools = ToolsRegistry.get_tools()
    while True:
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
        bus.dispatch(AssistantMessage(msg_param=assistant_msg, model=model))

        # 非工具调用结束循环
        if choice.finish_reason != 'tool_calls':
            return

        # 处理各个工具调用（tool_call 是联合类型：function / custom，只处理 function）
        for tc in serialized_tool_calls:
            if tc.get("type") != "function":
                continue

            tool_call = cast(ChatCompletionMessageFunctionToolCallParam, tc)
            func_name = tool_call["function"]["name"]
            handler = ToolsRegistry.get_handler(func_name)

            # dispatch 工具调用事件
            bus.dispatch(ToolCallEvent(tool_call=tool_call, model=model))

            args_str = tool_call["function"]["arguments"]
            if handler is None:
                tool_result = f"Error: Unknown tool '{func_name}'"
            else:
                # 解析工具参数并执行
                # noinspection PyUnresolvedReferences
                args_ = eval(args_str)
                tool_result = handler(**args_)

            # 消息列表追加工具执行结果
            tool_msg = ChatCompletionToolMessageParam(
                role='tool', tool_call_id=tool_call["id"], content=tool_result
            )
            messages.append(tool_msg)

            # dispatch 工具结果事件
            bus.dispatch(ToolResultEvent(msg_param=tool_msg, model=model))


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
    parser = argparse.ArgumentParser(description='mycode - 编程智能体')
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


from session import SessionHistory, SessionMessage, find_latest_session_file, get_session_file

def main():
    args = parse_args()

    model = os.getenv('MODEL_NAME') or ''

    # 消息列表初始化系统提示词
    hist_messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(role='system', content=SYSTEM),
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
    bus = AgentEventBus()
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
            bus.dispatch(UserMessage(msg_param=user_msg, model=model))

            agent_loop(hist_messages, bus, model)

        except EOFError:
            # Ctrl-D: 退出程序
            break

        except KeyboardInterrupt:
            # Ctrl-C: 结束当前执行，恢复到提示符
            print("\n")
            continue

    # ---- 退出提示 ----

    entry_count = len([e for e in session_hist.entries if isinstance(e, SessionMessage)])
    if entry_count == 0:
        # 没有任何用户消息，删除只有 session 记录的文件
        session_hist.file_path.unlink(missing_ok=True)
        print("\n无输入，会话已清理。")
    else:
        print(f"\n可通过以下命令继续本次会话：\n{sys.argv[0]} -r {session_hist.session_uuid}")

if __name__ == '__main__':
    main()
