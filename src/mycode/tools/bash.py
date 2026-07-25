#!/usr/bin/env python3
"""
工具模块 - 注册所有 AI 智能体可用的工具
"""
import os
import subprocess
from typing import Annotated
from mycode.tools_registry import ToolsRegistry


@ToolsRegistry.tool(description="运行 bash 命令")
def bash(command: Annotated[str, "要执行的 bash 命令"]) -> str:
    timeout = int(os.getenv('BASH_TIMEOUT') or 60)
    dangerous_raw = os.getenv('BASH_DANGEROUS', '')
    dangerous = [d.strip() for d in dangerous_raw.split(',') if d.strip()]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(command, cwd=os.getcwd(), shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({timeout}s)"
    except Exception as e:
        return f"Error: {e}"
