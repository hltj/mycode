"""
测试共享辅助：fixture 与构造辅助函数。

由 test_cli.py / test_renderer.py 等测试模块复用，避免重复定义。
（fake_env 属 autouse fixture，放在 conftest.py 中。）
"""

from __future__ import annotations

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
