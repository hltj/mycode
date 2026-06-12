#!/usr/bin/env python3

import os
import sys
import argparse
from typing import cast
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openai.types.chat import ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam, \
    ChatCompletionMessageParam, ChatCompletionToolMessageParam, \
    ChatCompletionAssistantMessageParam, ChatCompletionMessageToolCallUnionParam, ChatCompletionMessageFunctionToolCallParam
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completion, Completer
from prompt_toolkit.styles import Style

# 加载 .env 环境变量
load_dotenv()

# APP_HOME_DIR 和 历史记录文件
APP_HOME_DIR = Path(os.getenv('MYCODE_HOME_DIR', os.path.expanduser('~/.mycode')))
APP_HOME_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = APP_HOME_DIR / 'history.txt'

# 系统提示词
SYSTEM = f"你是编程智能体 mycode。当前在 {os.getcwd()}。使用 bash 完成任务。直接做勿解释。"

# 导入工具
# noinspection PyUnusedImports
from tools import bash
from tools_reg import ToolsRegistry
from session import SessionHistory, SessionMessage, SessionToolCall, find_latest_session_file, get_session_file

client = OpenAI(
    api_key=os.getenv('API_KEY'),
    base_url=os.getenv('BASE_URL'),
)


def replay_history(session_hist_: SessionHistory):
    """以实时输出格式重放历史会话"""
    for entry in session_hist_.entries:
        if isinstance(entry, SessionMessage):
            msg = entry.message
            role = msg.get("role")
            if role == "user":
                print(f"\x1B[38;2;0;204;0;1mmyc > \x1B[0m{msg.get('content', '')}")
            elif role == "assistant":
                content = msg.get("content")
                if content and str(content).strip():
                    print(f"\x1B[1;34mAI【{entry.model}】:\x1B[0m\n{str(content).strip(chr(0x0A))}\n")

            elif role == "tool":
                tool_result = msg.get("content", "")
                print(f"\x1B[1;36m工具输出:\x1B[0m\n```{f'{chr(0x0A)}{tool_result}'.rstrip(chr(0x0A))}\n```")
        elif isinstance(entry, SessionToolCall):
            tc = entry.tool_call
            func_name = tc["function"]["name"]
            args_ = tc["function"]["arguments"]
            print(f"\x1B[1;36m调用工具 - {func_name}\x1B[0m\n```json\n{args_}\n```")


# 智能体自循环
def agent_loop(messages: list[ChatCompletionMessageParam], session_hist_: SessionHistory | None = None):
    tools = ToolsRegistry.get_tools()
    while True:
        # 调用模型
        model_ = os.getenv('MODEL_NAME') or ''
        # noinspection PyTypeChecker
        response = client.chat.completions.create(
            model=model_,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )

        # noinspection PyUnresolvedReferences
        choice = response.choices[0]

        # 消息列表追加模型回复并输出
        message = choice.message
        content = message.content
        # 将 Pydantic tool_calls 转为 list[ChatCompletionMessageToolCallUnionParam]
        serialized_tool_calls: list[ChatCompletionMessageToolCallUnionParam] = [tc.model_dump() for tc in (message.tool_calls or [])]
        assistant_msg: ChatCompletionMessageParam = ChatCompletionAssistantMessageParam(
            role='assistant', content=content, tool_calls=serialized_tool_calls
        )
        messages.append(assistant_msg)
        
        if session_hist_ is not None:
            session_hist_.append_message(assistant_msg, model_)
        
        if content and content.strip():
            print(f"\x1B[1;34mAI【{model_}】:\x1B[0m\n{content.strip(chr(0x0A))}\n")

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

            # 保存工具调用记录到会话历史
            if session_hist_ is not None:
                session_hist_.append_tool_call(tool_call, model_)

            args_str = tool_call["function"]["arguments"]
            print(f"\x1B[1;36m调用工具 - {func_name}\x1B[0m\n```json\n{args_str}\n```")
            if handler is None:
                tool_result = f"Error: Unknown tool '{func_name}'"
            else:
                # 解析工具参数并执行
                # noinspection PyUnresolvedReferences
                args_ = eval(tool_call["function"]["arguments"])
                tool_result = handler(**args_)
            print(f"\x1B[1;36m工具输出:\x1B[0m\n```{f'{chr(0x0A)}{tool_result}'.rstrip(chr(0x0A))}\n```")

            # 消息列表追加工具执行结果
            tool_msg: ChatCompletionMessageParam = ChatCompletionToolMessageParam(
                role='tool', tool_call_id=tool_call["id"], content=tool_result
            )
            messages.append(tool_msg)

            if session_hist_ is not None:
                session_hist_.append_message(tool_msg, model_)


class MycCommandCompleter(Completer):
    """用于 / 开头命令补全的 Completer"""

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


if __name__ == '__main__':
    args = parse_args()
    
    model = os.getenv('MODEL_NAME') or ''
    
    # 消息列表初始化系统提示词
    hist_messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(role='system', content=SYSTEM),
    ]
    
    session_hist: SessionHistory | None = None
    
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

    # 输出头部信息
    print("【mycode】")
    print(f"会话 ID: {session_hist.session_uuid}")
    print()

    if args.resume or args.continue_session:
        replay_history(session_hist)

    # 定义提示符样式
    prompt_style = Style.from_dict({
        'mycode-prompt': '#00CC00 bold',
    })

    # 配置 prompt_toolkit session
    completer = MycCommandCompleter()
    session = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
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
            user_msg: ChatCompletionMessageParam = ChatCompletionUserMessageParam(role='user', content=user_input)
            hist_messages.append(user_msg)
            
            if session_hist is not None:
                session_hist.append_message(user_msg, model)
            
            agent_loop(hist_messages, session_hist)
        except EOFError:
            # Ctrl-D: 退出程序
            break
        except KeyboardInterrupt:
            # Ctrl-C: 结束当前执行，恢复到提示符
            print("\n")
            continue

    # 退出时输出如何继续本次会话的指引
    entry_count = len([e for e in session_hist.entries if isinstance(e, SessionMessage)])
    if entry_count == 0:
        # 没有任何用户消息，删除只有 session 记录的文件
        session_hist.file_path.unlink(missing_ok=True)
        print("\n无输入，会话已清理。")
    else:
        print(f"\n可通过以下命令继续本次会话：\n{sys.argv[0]} -r {session_hist.session_uuid}")
