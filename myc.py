#!/usr/bin/env python3

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openai.types.chat import ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam, \
    ChatCompletionMessageParam, ChatCompletionToolMessageParam, \
    ChatCompletionAssistantMessageParam
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

client = OpenAI(
    api_key=os.getenv('API_KEY'),
    base_url=os.getenv('BASE_URL'),
)

# 智能体自循环
def agent_loop(messages: list[ChatCompletionMessageParam]):
    tools = ToolsRegistry.get_tools()
    while True:
        # 调用模型
        model = os.getenv('MODEL_NAME')
        # noinspection PyTypeChecker
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )

        # noinspection PyUnresolvedReferences
        choice = response.choices[0]

        # 消息列表追加模型回复并输出
        message = choice.message
        content = message.content
        # noinspection PyTypeChecker
        messages.append(
            ChatCompletionAssistantMessageParam(role='assistant', content=content, tool_calls=message.tool_calls)
        )
        if content and content.strip():
            print(f"\x1B[1;34mAI【{model}】:\x1B[0m\n{content.strip(chr(0x0A))}\n")

        # 非工具调用结束循环
        if choice.finish_reason != 'tool_calls':
            return

        # 处理各个工具调用
        for tool_call in (message.tool_calls or []):
            func_name = tool_call.function.name
            handler = ToolsRegistry.get_handler(func_name)
            if handler is None:
                tool_result = f"Error: Unknown tool '{func_name}'"
            else:
                # 解析工具参数并执行
                # noinspection PyUnresolvedReferences
                args = eval(tool_call.function.arguments)
                print(f"\x1B[1;36m调用工具 - {func_name}\x1B[0m\n```json\n{tool_call.function.arguments}\n```")
                tool_result = handler(**args)
                print(f"\x1B[1;36m工具输出:\x1B[0m\n```{f'{chr(0x0A)}{tool_result}'.rstrip(chr(0x0A))}\n```")

            # 消息列表追加工具执行结果
            messages.append(
                ChatCompletionToolMessageParam(role='tool', tool_call_id=tool_call.id, content=tool_result)
            )


class MycCommandCompleter(Completer):
    """用于 / 开头命令补全的 Completer"""

    COMMANDS = ["/q", "/quit"]

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith('/'):
            for cmd in self.COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display=cmd)


if __name__ == '__main__':
    # 消息列表初始化系统提示词
    hist_messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(role='system', content=SYSTEM),
    ]

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
                print('')
                break
            # 消息列表追加用户输入并进入智能体循环
            hist_messages.append(ChatCompletionUserMessageParam(role='user', content=user_input))
            agent_loop(hist_messages)
        except EOFError:
            # Ctrl-D: 退出程序
            print('')
            break
        except KeyboardInterrupt:
            # Ctrl-C: 结束当前执行，恢复到提示符
            print("\n")
            continue
