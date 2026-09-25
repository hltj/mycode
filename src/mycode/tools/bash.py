"""
工具模块 - 注册所有 AI 智能体可用的工具
"""
import os
import re
import subprocess
from typing import Annotated
from mycode import config
from mycode.tools_registry import ToolsRegistry


def _load_dangerous_patterns() -> list[str]:
    """读取并切分危险命令正则列表。"""
    return config.get_list("bash_dangerous") or []


@ToolsRegistry.tool(description="运行 bash 命令")
def bash(command: Annotated[str, "要执行的 bash 命令"]) -> str:
    for pat in _load_dangerous_patterns():
        if re.search(pat, command):
            # 不向模型暴露具体模式
            return 'Error: 拒绝执行危险命令'
    timeout = config.get_int("bash_timeout", 60)
    try:
        result = subprocess.run(command, cwd=os.getcwd(), shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return f"Error: 超时 ({timeout}s)"
    except Exception as e:
        return f"Error: {e}"
